"""`decrypt.open_db`：进程内 SQLCipher（默认）与 CLI 回退两个后端（Phase 02）。

合成加密库两种造法：
- `build_fake_wechat_db`（sqlcipher CLI + 文本口令，与 test_decrypt_pipeline 同源）
- `_raw_key_db`（sqlcipher3 + raw key + 显式 salt）——能在**同一进程里**保持写连接不关，
  造出「还在 WAL 里、没 checkpoint」的行，模拟微信正在写的库。
"""
import os
import shutil
import sqlite3
import sys

import pytest

from agent_wechat import decrypt, errors, keys
from test_decrypt_pipeline import build_fake_wechat_db

sqlcipher3 = pytest.importorskip("sqlcipher3")


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(keys.paths, "HOME_DIR", tmp_path / "home")
    monkeypatch.delenv(decrypt.BACKEND_ENV, raising=False)
    decrypt._derive_cached.cache_clear()


def _rows(con):
    return con.execute("SELECT * FROM Chat_Msg ORDER BY MesLocalID").fetchall()


def _awdec_dirs():
    t = decrypt.tmp_dir()
    return sorted(p.name for p in t.iterdir() if p.name.startswith("awdec-")) if t.is_dir() else []


def _raw_key_db(d, *, rows=(), keep_open=False):
    pp, salt = os.urandom(32), os.urandom(keys.SALT_SZ)
    key = keys.derive_key(pp, salt)
    db = d / "raw.db"
    con = sqlcipher3.connect(str(db))
    con.execute(f"PRAGMA key = \"x'{key.hex()}{salt.hex()}'\"")
    con.execute("PRAGMA cipher_compatibility = 4")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA wal_autocheckpoint = 0")
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.executemany("INSERT INTO t VALUES (?, ?)", rows)
    con.commit()
    if not keep_open:
        con.close()
        con = None
    return db, pp, con


# ---------- 两个后端读出同样的东西 ----------

def test_inproc_and_cli_backends_read_the_same_rows(tmp_path, monkeypatch):
    enc, pp = build_fake_wechat_db(tmp_path)
    assert decrypt.backend() == "inproc"
    with decrypt.open_db(enc, pp) as con:
        con.text_factory = bytes
        inproc = _rows(con)
    monkeypatch.setenv(decrypt.BACKEND_ENV, "cli")
    assert decrypt.backend() == "cli"
    with decrypt.open_db(enc, pp) as con:
        con.text_factory = bytes
        cli = _rows(con)
    assert inproc == cli and len(inproc) == 10
    assert inproc[-1][2] == "消息9".encode()


def test_backend_falls_back_to_cli_when_sqlcipher3_cannot_be_imported(monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlcipher3", None)        # import → ImportError
    assert decrypt.backend() == "cli"


def test_wal_rows_are_visible_even_when_the_snapshot_has_no_shm(tmp_path):
    """微信库是 WAL 模式：没 checkpoint 的行只在 -wal 里。快照必须把它们读出来，
    而且快照缺 -shm（复制时微信刚好在重建它）也不能因此读不了。"""
    src = tmp_path / "src"
    src.mkdir()
    db, pp, writer = _raw_key_db(src, rows=[(i, f"v{i}") for i in range(5)], keep_open=True)
    try:
        assert (src / "raw.db-wal").stat().st_size > 0
        cp = tmp_path / "copy"
        cp.mkdir()
        shutil.copy2(db, cp / "raw.db")
        shutil.copy2(src / "raw.db-wal", cp / "raw.db-wal")      # 刻意不带 -shm
        with decrypt.open_db(cp / "raw.db", pp) as con:
            assert [r[0] for r in con.execute("SELECT id FROM t ORDER BY id")] == [0, 1, 2, 3, 4]
    finally:
        writer.close()


# ---------- 异常口径 ----------

def test_wrong_key_is_a_decrypt_error_that_does_not_echo_the_key(tmp_path, monkeypatch):
    enc, _ = build_fake_wechat_db(tmp_path)
    wrong = b"\x01" * 32
    for backend in ("inproc", "cli"):
        if backend == "cli":
            monkeypatch.setenv(decrypt.BACKEND_ENV, "cli")
        with pytest.raises(errors.DecryptError) as ei:
            with decrypt.open_db(enc, wrong):
                pass
        p1 = enc.read_bytes()[:keys.PAGE_SZ]
        key_hex = keys.derive_key(wrong, p1[:keys.SALT_SZ]).hex()
        text = f"{ei.value} {ei.value.hint} {ei.value.__cause__!r}"
        assert key_hex not in text, backend


def test_sql_errors_surface_as_the_same_sqlite3_classes(tmp_path):
    """sqlcipher3 的异常不是 sqlite3.Error 的子类：不翻译的话仓里的 `except sqlite3.*` 全失效。"""
    enc, pp = build_fake_wechat_db(tmp_path)
    with decrypt.open_db(enc, pp) as con:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("SELECT * FROM no_such_table")
        with pytest.raises(sqlite3.OperationalError):           # 只读打开：快照也不许写
            con.execute("CREATE TABLE x(a)")
        with pytest.raises(sqlite3.Error):                      # 取行时才出错的那一类
            list(con.execute("SELECT abs(-9223372036854775807 - 1) FROM Chat_Msg"))


def test_closing_from_another_thread_is_a_sqlite3_error(tmp_path):
    """审查发现的边界：持有 open_db 的生成器被**别的线程**终结时，close 也要走翻译——
    不然冒出的是裸 sqlcipher3 异常，`except sqlite3.Error` 接不住（旧路径同场景是 sqlite3 的）。"""
    import threading
    enc, pp = build_fake_wechat_db(tmp_path)
    with decrypt.open_db(enc, pp) as con:
        raw = con._con
        caught = []

        def other():
            try:
                decrypt._CipherConnection(raw).close()
            except Exception as exc:                            # noqa: BLE001 — 收集类型
                caught.append(exc)
        t = threading.Thread(target=other)
        t.start(); t.join()
        assert caught and isinstance(caught[0], sqlite3.ProgrammingError)


def test_a_page_that_fails_hmac_mid_query_is_a_decrypt_error(tmp_path):
    """进程内是**按页惰性**解密：撕裂/损坏的页在查询中途才暴露。明文时代同一个库在
    `sqlcipher_export` 那步就以 DecryptError 失败——异常类型必须保持这个口径。"""
    src = tmp_path / "src"
    src.mkdir()
    db, pp, _ = _raw_key_db(src, rows=[(i, "x" * 300) for i in range(2000)])
    raw = bytearray(db.read_bytes())
    assert len(raw) // keys.PAGE_SZ > 20
    off = 15 * keys.PAGE_SZ + 100                               # 第 16 页中间的一个字节
    raw[off] ^= 0xFF
    db.write_bytes(bytes(raw))
    with decrypt.open_db(db, pp) as con:                        # page 1 完好：打开照常成功
        with pytest.raises(errors.DecryptError):
            for _ in con.execute("SELECT v FROM t"):
                pass


# ---------- 资源与安全边界 ----------

def test_no_plaintext_ever_reaches_the_disk(tmp_path):
    enc, pp = build_fake_wechat_db(tmp_path)
    with decrypt.open_db(enc, pp) as con:
        assert _rows(con)
        files = [p for p in decrypt.tmp_dir().rglob("*") if p.is_file()]
        assert files, "快照应在 tmp/awdec-* 下"
        assert not any(p.name == "plain.db" for p in files)
        for p in files:
            assert "消息".encode() not in p.read_bytes(), f"{p.name} 含明文"
    assert _awdec_dirs() == [], "退出后快照目录要删干净"


def test_snapshot_and_connection_are_released_when_the_body_raises(tmp_path, monkeypatch):
    enc, pp = build_fake_wechat_db(tmp_path)
    closed = []
    real = decrypt._CipherConnection.close
    monkeypatch.setattr(decrypt._CipherConnection, "close",
                        lambda self: (closed.append(1), real(self))[1])
    with pytest.raises(RuntimeError):
        with decrypt.open_db(enc, pp):
            raise RuntimeError("调用方自己的错误")
    assert closed == [1]
    assert _awdec_dirs() == []


def test_the_snapshot_dir_is_recognizable_by_the_stale_sweeper(tmp_path):
    """崩溃残留靠目录名里的 pid 认领（EA-019）——进程内后端用同一个前缀。"""
    enc, pp = build_fake_wechat_db(tmp_path)
    with decrypt.open_db(enc, pp):
        names = _awdec_dirs()
        assert len(names) == 1
        m = decrypt._STALE_TMP_RE.match(names[0])
        assert m and int(m.group(1)) == os.getpid()


def test_key_derivation_is_cached_per_passphrase_and_salt(tmp_path, monkeypatch):
    enc, pp = build_fake_wechat_db(tmp_path)
    calls = []
    real = keys.derive_key
    monkeypatch.setattr(keys, "derive_key", lambda p, s: (calls.append(1), real(p, s))[1])
    for _ in range(3):
        with decrypt.open_db(enc, pp) as con:
            con.execute("SELECT 1").fetchone()
    assert len(calls) == 1
    with pytest.raises(errors.DecryptError):                    # 换了 passphrase：不复用旧钥
        with decrypt.open_db(enc, b"\x02" * 32):
            pass
    assert len(calls) == 2
