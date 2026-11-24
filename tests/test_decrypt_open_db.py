"""`decrypt.open_db`：进程内 SQLCipher（默认）与 CLI 回退两个后端（Phase 02）。

合成加密库两种造法：
- `build_fake_wechat_db`（sqlcipher CLI + 文本口令，与 test_decrypt_pipeline 同源）
- `_raw_key_db`（sqlcipher3 + raw key + 显式 salt）——能在**同一进程里**保持写连接不关，
  造出「还在 WAL 里、没 checkpoint」的行，模拟微信正在写的库。
"""
import os
import pathlib
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
    monkeypatch.setattr(decrypt, "_HAS_SQLCIPHER3", False)      # 模块导入时 import 失败
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


# ---------- 快照：能克隆就克隆（APFS clonefile），不行就复制 ----------

def _triplet(d):
    d.mkdir()
    db = d / "x.db"
    db.write_bytes(os.urandom(8192))
    (d / "x.db-wal").write_bytes(os.urandom(4096))
    return db


def test_snapshot_clones_and_the_clone_is_independent_of_the_source(tmp_path, monkeypatch):
    probe = tmp_path / "probe"
    probe.write_bytes(b"x")
    if decrypt._clonefile is None or decrypt._clonefile(
            bytes(probe), bytes(tmp_path / "probe2"), 0) != 0:
        pytest.skip("本机 / tmp_path 所在的卷不支持 clonefile(2)")
    db = _triplet(tmp_path / "src")
    before = {p.name: p.read_bytes() for p in db.parent.iterdir()}
    copied = []
    monkeypatch.setattr(decrypt.shutil, "copy2", lambda *a, **k: copied.append(a))
    td = tmp_path / "td"
    td.mkdir()
    dst = decrypt.snapshot(db, td)
    assert copied == [], "能克隆时不该退回逐字节复制"
    assert {p.name: p.read_bytes() for p in td.iterdir()} == before   # 两个文件都在、逐字节相同
    assert not (td / "x.db-shm").exists()                             # 源没有的不凭空造
    with open(dst, "r+b") as f:                                        # 写时复制：改克隆不动源
        f.write(b"\0" * 16)
    assert {p.name: p.read_bytes() for p in db.parent.iterdir()} == before


def test_snapshot_falls_back_to_copy_when_cloning_fails(tmp_path, monkeypatch):
    """跨卷（HOME 不在微信所在的卷）/ 非 APFS：clonefile 失败 → copy2，结果相同。"""
    monkeypatch.setattr(decrypt, "_clonefile", lambda src, dst, flags: -1)
    db = _triplet(tmp_path / "src")
    td = tmp_path / "td"
    td.mkdir()
    decrypt.snapshot(db, td)
    assert {p.name: p.read_bytes() for p in td.iterdir()} == \
        {p.name: p.read_bytes() for p in db.parent.iterdir()}


def _wal_source(d):
    """明文 WAL 库（快照与加密无关）：5 行已 checkpoint 进主库，3 行（6–8）只在 -wal 里。
    写连接不关，模拟微信。行宽 900 字节：这个布局下「旧主库 + 新 WAL」恰好读出一个空洞。"""
    d.mkdir()
    w = sqlite3.connect(d / "src.db", isolation_level=None)
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("PRAGMA wal_autocheckpoint=0")
    w.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    for i in range(1, 6):
        w.execute("INSERT INTO t VALUES (?, ?)", (i, "x" * 900))
    w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for i in range(6, 9):
        w.execute("INSERT INTO t VALUES (?, ?)", (i, "y" * 900))
    return d / "src.db", w


def _ids(snap):
    con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return [r[0] for r in con.execute("SELECT id FROM t ORDER BY id")]
    finally:
        con.close()


@pytest.mark.parametrize("clone", [True, False], ids=["clone", "copy"])
def test_a_checkpoint_between_copying_the_db_and_the_wal_is_retaken(tmp_path, monkeypatch, clone):
    """主库拷完、-wal 还没拷时微信 checkpoint 并重启 WAL：旧主库 + 新 WAL 读出 1–5、9，
    **6–8 静默缺席**（integrity_check 照样 ok），增量导出会把水位线推到 9、再也看不见 6–8。"""
    if not clone:
        monkeypatch.setattr(decrypt, "_clonefile", None)
    db, w = _wal_source(tmp_path / "src")
    orig, fired = decrypt._clone_or_copy, []

    def racy(src, dst):
        orig(src, dst)
        if src == db and not fired:          # 只撞第一次
            fired.append(1)
            w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            w.execute("INSERT INTO t VALUES (9, ?)", ("z" * 900,))
    monkeypatch.setattr(decrypt, "_clone_or_copy", racy)
    td = tmp_path / "td"
    td.mkdir()
    try:
        assert _ids(decrypt.snapshot(db, td)) == list(range(1, 10))
    finally:
        w.close()
    assert fired


def test_appends_to_the_wal_alone_do_not_force_a_retake(tmp_path, monkeypatch):
    """没有 checkpoint、只是 -wal 变长：拍到的是一致的前缀，不重拍（重拍在拷贝回退时代价不小）。"""
    db, w = _wal_source(tmp_path / "src")
    orig, calls = decrypt._clone_or_copy, []

    def appending(src, dst):
        calls.append(src.name)
        orig(src, dst)
        if src == db:
            w.execute("INSERT INTO t VALUES (9, ?)", ("z" * 900,))
    monkeypatch.setattr(decrypt, "_clone_or_copy", appending)
    td = tmp_path / "td"
    td.mkdir()
    try:
        assert _ids(decrypt.snapshot(db, td)) == list(range(1, 10))
    finally:
        w.close()
    assert calls.count("src.db") == 1


def test_a_db_rewritten_on_every_attempt_is_a_source_error_not_a_torn_snapshot(tmp_path, monkeypatch):
    db, w = _wal_source(tmp_path / "src")
    orig = decrypt._clone_or_copy
    n = iter(range(100, 200))

    def busy(src, dst):
        orig(src, dst)
        if src == db:
            w.execute("INSERT INTO t VALUES (?, ?)", (next(n), "z" * 900))
            w.execute("PRAGMA wal_checkpoint(PASSIVE)")
    monkeypatch.setattr(decrypt, "_clone_or_copy", busy)
    td = tmp_path / "td"
    td.mkdir()
    try:
        with pytest.raises(errors.SourceUnreadable, match="反复改写"):
            decrypt.snapshot(db, td)
    finally:
        w.close()


@pytest.mark.parametrize("clone", [True, False], ids=["clone", "copy"])
def test_open_db_reads_the_same_rows_whichever_way_the_snapshot_was_taken(tmp_path, monkeypatch, clone):
    if not clone:
        monkeypatch.setattr(decrypt, "_clonefile", None)
    enc, pp = build_fake_wechat_db(tmp_path)
    with decrypt.open_db(enc, pp) as con:
        assert _rows(con)


# ---------- 并发上限 ----------

def _counting_backend(monkeypatch, hold):
    import contextlib
    import threading
    state = {"now": 0, "peak": 0}
    lock = threading.Lock()

    @contextlib.contextmanager
    def fake(db, passphrase):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        try:
            hold()
            yield object()
        finally:
            with lock:
                state["now"] -= 1
    monkeypatch.setattr(decrypt, "_open_db_unbounded", fake)
    return state


def test_at_most_a_bounded_number_of_threads_hold_a_snapshot(monkeypatch):
    """MCP 的同步工具跑在工作线程里：8 路并发此前各拍一份快照、同时解密（峰值 RSS 234 MB）。"""
    import threading
    import time
    state = _counting_backend(monkeypatch, lambda: time.sleep(0.05))

    def use():
        with decrypt.open_db(pathlib.Path("x.db"), b"k"):
            pass
    ts = [threading.Thread(target=use) for _ in range(3 * decrypt.OPEN_DB_CONCURRENCY)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert state["peak"] == decrypt.OPEN_DB_CONCURRENCY and state["now"] == 0


def test_nested_opens_in_one_thread_do_not_take_a_second_slot(monkeypatch):
    """每个线程都先占满名额、再在里面打开第二个库：按「每次打开」计数的话这里当场死锁。"""
    import threading
    n = decrypt.OPEN_DB_CONCURRENCY
    barrier = threading.Barrier(n, timeout=5)
    _counting_backend(monkeypatch, lambda: None)
    done = []

    def nested():
        with decrypt.open_db(pathlib.Path("a.db"), b"k"):
            barrier.wait()
            with decrypt.open_db(pathlib.Path("b.db"), b"k"):
                done.append(1)
    ts = [threading.Thread(target=nested, daemon=True) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    assert len(done) == n


def test_out_of_order_close_in_one_thread_still_releases_the_slot(monkeypatch):
    """同一线程里两个 `open_db` 不按 LIFO 关闭（先关外层、后关内层）：此前 finally 「恢复进入时的
    深度」，计数卡在 1，这个线程此后再也不占名额——并发上限对它失效。"""
    _counting_backend(monkeypatch, lambda: None)
    a = decrypt.open_db(pathlib.Path("a.db"), b"k")
    b = decrypt.open_db(pathlib.Path("b.db"), b"k")
    a.__enter__()
    b.__enter__()
    a.__exit__(None, None, None)
    b.__exit__(None, None, None)
    assert decrypt._open_db_depth.n == 0
    with decrypt.open_db(pathlib.Path("c.db"), b"k"):
        assert decrypt._open_db_depth.n == 1
