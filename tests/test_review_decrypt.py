"""全量审查（解密 / 密钥 / 引导 / 读取层）修复的回归用例。全部用合成数据，不碰真机。"""
import os
import pathlib
import shlex
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from agent_wechat import bootstrap, decrypt, errors, keys, paths, reader, usecases
from agent_wechat.errors import CaptureError
from test_decrypt_pipeline import build_fake_wechat_db
from test_msg_key_v10 import ACCT, wx  # noqa: F401  （夹具）
from test_reader_export_pipeline import FRIEND, GROUP, SELF, T0, build_shard

sqlcipher3 = pytest.importorskip("sqlcipher3")

# 目录名里带 sqlite URI 的保留字符（`?` 截断路径、`#` 截断片段、`%` 被当转义）、空格与中文
ODD = "a?b#c%41 d 中文"


@pytest.fixture(autouse=True)
def _backend(monkeypatch):
    monkeypatch.delenv(decrypt.BACKEND_ENV, raising=False)
    decrypt._derive_cached.cache_clear()


def _chat_rows(enc, pp):
    with decrypt.open_db(enc, pp) as con:
        con.text_factory = bytes
        return con.execute("SELECT Message FROM Chat_Msg ORDER BY MesLocalID").fetchall()


# ---------- 1. sqlite URI 转义 ----------

@pytest.mark.parametrize("backend", ["inproc", "cli"])
def test_home_with_uri_metacharacters_opens_the_right_file_read_only(tmp_path, monkeypatch, backend):
    enc, pp = build_fake_wechat_db(tmp_path)
    home = tmp_path / ODD / "home"
    monkeypatch.setattr(paths, "HOME_DIR", home)
    if backend == "cli":
        monkeypatch.setenv(decrypt.BACKEND_ENV, "cli")
    rows = _chat_rows(enc, pp)
    assert len(rows) == 10 and rows[-1][0] == "消息9".encode()
    # 拼接 URI 时 `?` 之后被当查询串：会在数据目录**外**新建一个空库 `a`
    assert not (tmp_path / "a").exists()


def test_open_plain_escapes_the_path_and_stays_read_only(tmp_path):
    d = tmp_path / ODD
    d.mkdir()
    plain = d / "plain.db"
    con = sqlite3.connect(plain)
    con.execute("CREATE TABLE t (v)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()
    ro = decrypt.open_plain(plain)
    try:
        assert ro.execute("SELECT v FROM t").fetchall() == [(1,)]
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO t VALUES (2)")
    finally:
        ro.close()
    assert not (tmp_path / "a").exists()


# ---------- 2. ATTACH 的单引号 ----------

def test_cli_backend_survives_a_single_quote_in_the_home_dir(tmp_path, monkeypatch):
    enc, pp = build_fake_wechat_db(tmp_path)
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path / "it's" / "home")
    monkeypatch.setenv(decrypt.BACKEND_ENV, "cli")
    assert len(_chat_rows(enc, pp)) == 10


# ---------- 3. 跨线程收尾的 open_db ----------

def test_open_db_finished_in_another_thread_releases_its_slot(monkeypatch):
    @__import__("contextlib").contextmanager
    def fake(db, passphrase):
        yield object()
    monkeypatch.setattr(decrypt, "_open_db_unbounded", fake)
    sem = threading.BoundedSemaphore(decrypt.OPEN_DB_CONCURRENCY)
    monkeypatch.setattr(decrypt, "_open_db_slots", sem)
    monkeypatch.setattr(decrypt, "_open_db_depth", type(decrypt._open_db_depth)())

    errs: list[BaseException] = []
    for _ in range(decrypt.OPEN_DB_CONCURRENCY + 1):
        cm = decrypt.open_db(pathlib.Path("x.db"), b"k")
        cm.__enter__()

        def close(cm=cm):
            try:
                cm.__exit__(None, None, None)      # 生成器在别的线程被收尾
            except BaseException as exc:          # noqa: BLE001
                errs.append(exc)
        th = threading.Thread(target=close)
        th.start()
        th.join(5)
    assert errs == []
    assert decrypt._open_db_depth.n == 0
    got = []
    th = threading.Thread(target=lambda: got.append(sem.acquire(timeout=2) and sem.release()))
    th.start()
    th.join(5)
    assert got == [None], "名额泄漏：新线程拿不到 open_db 的名额"
    # 同线程嵌套仍只占一个名额
    with decrypt.open_db(pathlib.Path("a.db"), b"k"):
        with decrypt.open_db(pathlib.Path("b.db"), b"k"):
            assert sem._value == decrypt.OPEN_DB_CONCURRENCY - 1
    assert sem._value == decrypt.OPEN_DB_CONCURRENCY


# ---------- 7. 第 1 页 HMAC 通过 → 源不可读，而不是密钥失效 ----------

def _multi_page_master_db(d):
    """sqlite_master 跨多页的加密库（raw key + 显式 salt），返回 (库, passphrase)。"""
    pp, salt = os.urandom(32), os.urandom(keys.SALT_SZ)
    key = keys.derive_key(pp, salt)
    db = d / "multi.db"
    con = sqlcipher3.connect(str(db))
    con.execute(f"PRAGMA key = \"x'{key.hex()}{salt.hex()}'\"")
    con.execute("PRAGMA cipher_compatibility = 4")
    for i in range(200):
        con.execute(f"CREATE TABLE t_{i:03d}_{'x' * 40} (id INTEGER PRIMARY KEY, v TEXT)")
    con.commit()
    con.close()
    return db, pp


def test_a_damaged_later_page_is_source_unreadable_not_a_key_mismatch(tmp_path):
    db, pp = _multi_page_master_db(tmp_path)
    raw = bytearray(db.read_bytes())
    assert len(raw) > 4 * keys.PAGE_SZ
    for off in range(keys.PAGE_SZ, len(raw), keys.PAGE_SZ):   # 第 1 页完好，其后每页坏一个字节
        raw[off + 100] ^= 0xFF
    db.write_bytes(bytes(raw))
    with pytest.raises(errors.SourceUnreadable) as ei:
        with decrypt.open_db(db, pp):
            pass
    assert "verify-keys" not in (ei.value.hint or "") and "bootstrap" not in (ei.value.hint or "")


def test_a_wrong_key_is_still_a_key_mismatch(tmp_path):
    db, _pp = _multi_page_master_db(tmp_path)
    with pytest.raises(errors.DecryptError) as ei:
        with decrypt.open_db(db, os.urandom(32)):
            pass
    assert not isinstance(ei.value, errors.SourceUnreadable)
    assert "verify-keys" in (ei.value.hint or "")


# ---------- 4. 单条坏 zstd 帧：占位 + 可见降级，不中止整页 / 整轮导出 ----------

def test_one_corrupt_zstd_frame_is_a_placeholder_with_a_visible_degradation(wx):  # noqa: F811
    import zstandard
    from agent_wechat import export, store
    build_shard(paths.XWECHAT_FILES, ACCT, "message_0.db", {FRIEND: [
        (1, 1, 1, T0 + 1, "第一条", 0),
        (2, 1, 1, T0 + 2, b"\x28\xb5\x2f\xfdgarbage", 4),                 # 坏帧
        (3, 1, 1, T0 + 3, zstandard.compress("第三条".encode())[:-3], 4),  # 截断帧
        (4, 1, 1, T0 + 4, zstandard.compress("第四条".encode()), 4),
    ]}, [FRIEND, SELF])
    r = reader.MessageReader(ACCT)
    got = [m.content for m in r.recent(FRIEND)]
    assert got == ["第一条", "[解压失败]", "[解压失败]", "第四条"]
    assert [d.capability for d in usecases.collect_degradations(r)] == ["message_content"]

    degraded: list = []
    assert export.run_export(ACCT, on_reader=lambda rr: degraded.extend(
        usecases.collect_degradations(rr))) == 4
    assert [d.db for d in degraded] == ["message/message_0.db"]
    with store.open() as con:
        assert [row[0] for row in con.execute(
            "SELECT content FROM messages ORDER BY ts")] == got


# ---------- 5. 群前缀：自己发的不剥；能解出发送者时只在 head 等于它时剥 ----------

def test_group_prefix_is_only_stripped_when_it_names_the_resolved_sender(wx):  # noqa: F811
    build_shard(paths.XWECHAT_FILES, ACCT, "message_0.db", {GROUP: [
        (1, 1, 2, T0 + 1, "提醒:\n明天开会", 0),          # 自己发的：真机从不带前缀
        (2, 1, 1, T0 + 2, f"{FRIEND}:\n收到", 0),         # 别人发的：head == Name2Id 解出的发送者
        (3, 1, 1, T0 + 3, "备注:\n别削我", 0),            # 解出了发送者但 head 对不上：不剥
        (4, 1, 99, T0 + 4, "wxid_otherchat:\n嗨", 0),    # 解不出发送者：维持原判据
    ]}, [FRIEND, SELF])
    msgs = reader.MessageReader(ACCT).recent(GROUP)
    assert [(m.sender_id, m.is_self, m.content) for m in msgs] == [
        (SELF, True, "提醒:\n明天开会"),
        (FRIEND, False, "收到"),
        (FRIEND, False, "备注:\n别削我"),
        ("wxid_otherchat", False, "嗨"),
    ]


# ---------- 6. before_ts 翻页按时间取，不按 local_id ----------

def test_paging_with_before_ts_takes_the_newest_by_time_not_by_local_id(wx):  # noqa: F811
    # local_id 与时间顺序不一致（历史迁移 / 补同步：旧消息拿到了更大的 local_id）
    wx([(1, T0 + 100, "新A"), (2, T0 + 101, "新B"), (3, T0 + 10, "迁1"), (4, T0 + 11, "迁2"),
        (5, T0 + 500, "更新")])
    r = reader.MessageReader(ACCT)
    assert [m.content for m in r.recent(FRIEND, limit=2, before_ts=T0 + 200)] == ["新A", "新B"]
    assert [m.content for m in r.recent(FRIEND, limit=2, before_ts=T0 + 100)] == ["迁1", "迁2"]


# ---------- 8. lldb 提前退出：立即报错，不等满 timeout ----------

def test_capture_stops_as_soon_as_lldb_exits_early(monkeypatch, tmp_path):
    import subprocess as _sub
    stub = tmp_path / "fake_lldb.sh"            # 桩：像 attach 失败的 lldb 那样打一行错误就退出
    stub.write_text("#!/bin/sh\necho 'error: attach failed: (stub) not allowed to attach'\nexit 1\n")
    stub.chmod(0o755)
    monkeypatch.setattr(bootstrap, "shutil", SimpleNamespace(which=lambda name: "/usr/bin/lldb"))
    monkeypatch.setattr(bootstrap.account, "wechat_pid", lambda: 4242)

    def fake_popen(argv, **kw):
        assert "TERM=dumb lldb" in argv[2]
        cmd = argv[2].replace("TERM=dumb lldb", f"TERM=dumb {shlex.quote(str(stub))}")
        return _sub.Popen([argv[0], argv[1], cmd], **kw)
    monkeypatch.setattr(bootstrap, "subprocess", SimpleNamespace(
        Popen=fake_popen, PIPE=_sub.PIPE, STDOUT=_sub.STDOUT, TimeoutExpired=_sub.TimeoutExpired))
    t0 = time.monotonic()
    with pytest.raises(CaptureError, match="attach failed"):
        bootstrap.capture_passphrase_lldb(timeout=8)
    assert time.monotonic() - t0 < 6, "lldb 已退出却还在等 timeout"


# ---------- 9. 缓存不是合法 UTF-8 = 空缓存 ----------

def test_a_cache_file_that_is_not_utf8_is_treated_as_empty(tmp_path, monkeypatch):
    cache = tmp_path / "passphrase.json"
    cache.write_bytes(b'{"accounts": {"\xff\xfe": {}}}')
    monkeypatch.setattr(keys, "CACHE_FILE", cache)
    assert keys._load_cache() == {"accounts": {}}
    assert keys.load_cached_hex("x") is None
