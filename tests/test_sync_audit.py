"""Phase 02：增量导出的历史消息回看（审计游标）。

所有用例都用**合成加密库**（与 `test_reader_export_pipeline` 同一套 `enc_db` /
`build_shard`），不碰真实聊天库。要验证「旧行被改写」，就必须能**改写源库**——
所以每条用例自建一棵树，不复用模块级夹具。
"""
from __future__ import annotations

import pathlib
import tracemalloc

import pytest

from agent_wechat import errors, exclusions, export, keys, messages, paths, purge, store

from test_reader_export_pipeline import ACCT, FRIEND, PW, SELF, T0, build_shard, enc_db, md5


def _set_paths(monkeypatch, wx: pathlib.Path, home: pathlib.Path) -> None:
    monkeypatch.setattr(paths, "XWECHAT_FILES", wx)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")
    keys.save_cached_hex(SELF, PW.encode().hex())


def _base(tmp_path, monkeypatch, rows: list[tuple], *, shard: str = "message_0.db",
          users: list[str] | None = None, friend_known: bool = True):
    """一棵最小合成树：session / contact / shard（加密）。返回 (wx, home)。"""
    root = tmp_path / "wx"
    wx, home = root / "xwechat_files", root / "home"
    home.mkdir(parents=True, mode=0o700)
    ds = wx / ACCT / "db_storage"
    session_users = [FRIEND] if friend_known else [SELF]
    enc_db(ds / "session" / "session.db", {
        "SessionTable": ("CREATE TABLE SessionTable (username TEXT)",
                         [(u,) for u in session_users])})
    enc_db(ds / "contact" / "contact.db", {
        "name2id": ("CREATE TABLE name2id (username TEXT)",
                    [(FRIEND,)] if friend_known else []),
        "contact": ("CREATE TABLE contact (username TEXT, alias TEXT, remark TEXT, "
                    "nick_name TEXT, delete_flag INTEGER)",
                    [(FRIEND, None, "张三", "zc", None)] if friend_known else []),
    })
    build_shard(wx, ACCT, shard, {FRIEND: rows},
                users if users is not None else ([FRIEND] if friend_known else []))
    _set_paths(monkeypatch, wx, home)
    return wx, home


def _rows(n: int, *, start: int = 1, prefix: str = "第") -> list[tuple]:
    return [(i, 1, 1, T0 + i, f"{prefix}{i}版正文", 0) for i in range(start, start + n)]


def _content(con, local_id: int = 1) -> str:
    key = f"{ACCT}|{md5(FRIEND)}|message_0.db|{local_id}"
    return con.execute("SELECT content FROM messages WHERE msg_key=?", (key,)).fetchone()[0]


# ---------------------------------------------------------------- 回看覆盖一整段历史

def test_audit_reaches_old_rows_over_several_runs(tmp_path, monkeypatch):
    """游标绕圈：每轮只看一小段，若干轮后覆盖到最早的那条。

    编辑第 1 条 → 第 1 轮看 3-4、第 2 轮 5-6、第 3 轮绕回 0、第 4 轮才看 1-2。
    """
    wx, _ = _base(tmp_path, monkeypatch, _rows(6))
    assert export.run_export(ACCT, audit_rows=2) == 6
    with store.open() as con:
        assert store.load_audit_cursors(con, ACCT) == {("message_0.db", md5(FRIEND)): 2}

    build_shard(wx, ACCT, "message_0.db",
                {FRIEND: [(1, 1, 1, T0 + 1, "改过的正文", 0)] + _rows(6)[1:]}, [FRIEND])

    assert export.run_export(ACCT, audit_rows=2) == 0
    with store.open() as con:
        assert store.load_audit_cursors(con, ACCT)[("message_0.db", md5(FRIEND))] == 4
        assert _content(con) == "第1版正文", "这一轮还没轮到它"
    assert export.run_export(ACCT, audit_rows=2) == 0      # → 6
    assert export.run_export(ACCT, audit_rows=2) == 0      # 空段 → 绕回 0
    with store.open() as con:
        assert store.load_audit_cursors(con, ACCT)[("message_0.db", md5(FRIEND))] == 0
    assert export.run_export(ACCT, audit_rows=2) == 0      # 1-2：终于看见改写
    with store.open() as con:
        assert _content(con) == "改过的正文"


def test_repeated_audit_is_idempotent(tmp_path, monkeypatch):
    _base(tmp_path, monkeypatch, _rows(3))
    assert export.run_export(ACCT) == 3
    for _ in range(3):
        assert export.run_export(ACCT) == 0
    with store.open() as con:
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 3
        assert con.execute("SELECT count(*) FROM pending_reembed").fetchone()[0] == 0, "没有改写就不该投递重嵌"


# ---------------------------------------------------------------- 跨分片 / 重复 local_id

def test_same_local_id_in_two_shards_is_tracked_separately(tmp_path, monkeypatch):
    wx, _ = _base(tmp_path, monkeypatch, [(1, 1, 1, T0 + 1, "分片零正文", 0)])
    build_shard(wx, ACCT, "message_1.db",
                {FRIEND: [(1, 1, 1, T0 - 1, "分片一正文", 0)]}, [FRIEND])
    assert export.run_export(ACCT, audit_rows=10) == 2

    # 两个分片里同名 local_id 各自改写
    build_shard(wx, ACCT, "message_0.db",
                {FRIEND: [(1, 1, 1, T0 + 1, "分片零改后", 0)]}, [FRIEND])
    build_shard(wx, ACCT, "message_1.db",
                {FRIEND: [(1, 1, 1, T0 - 1, "分片一改后", 0)]}, [FRIEND])
    assert export.run_export(ACCT, audit_rows=10) == 0
    with store.open() as con:
        got = dict(con.execute(
            "SELECT shard, content FROM messages WHERE account=?", (ACCT,)).fetchall())
        assert got == {"message_0.db": "分片零改后", "message_1.db": "分片一改后"}


# ---------------------------------------------------------------- 排除 / purge 不复活

def test_purged_chat_is_not_resurrected_by_the_audit(tmp_path, monkeypatch):
    wx, _ = _base(tmp_path, monkeypatch, _rows(3))
    assert export.run_export(ACCT) == 3
    purge.execute_chat(purge.plan_chat(
        [{"account": ACCT, "chat_id": FRIEND, "chat_md5": md5(FRIEND)}]))

    # 源库改写 + 重扫：墓碑必须挡住，且不投递重嵌
    build_shard(wx, ACCT, "message_0.db",
                {FRIEND: [(1, 1, 1, T0 + 1, "复活尝试", 0)] + _rows(3)[1:]}, [FRIEND])
    assert export.run_export(ACCT) == 0
    with store.open() as con:
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert exclusions.count(con) == 3
        assert con.execute("SELECT count(*) FROM pending_reembed").fetchone()[0] == 0


# ---------------------------------------------------------------- 会话名恢复

def test_audit_repairs_a_degraded_chat_id(tmp_path, monkeypatch):
    """先按 `md5:<hex>` 落库，等联系人名册补上后，回看把它改成真名。

    这正是任务里「会话标识恢复」那一项：它只可能发生在**旧行**上，前向扫描看不见。
    """
    wx, _ = _base(tmp_path, monkeypatch, [(1, 1, 1, T0 + 1, "正文", 0)], friend_known=False)
    assert export.run_export(ACCT) == 1
    key = f"{ACCT}|{md5(FRIEND)}|message_0.db|1"
    with store.open() as con:
        assert con.execute("SELECT chat_id FROM messages WHERE msg_key=?",
                           (key,)).fetchone()[0] == messages.degraded_chat_id(md5(FRIEND))

    # 名册补上：session / contact / 分片 Name2Id 都能把 md5 反查成 FRIEND
    enc_db(wx / ACCT / "db_storage" / "session" / "session.db", {
        "SessionTable": ("CREATE TABLE SessionTable (username TEXT)", [(FRIEND,)])})
    enc_db(wx / ACCT / "db_storage" / "contact" / "contact.db", {
        "name2id": ("CREATE TABLE name2id (username TEXT)", [(FRIEND,)]),
        "contact": ("CREATE TABLE contact (username TEXT, alias TEXT, remark TEXT, "
                    "nick_name TEXT, delete_flag INTEGER)",
                    [(FRIEND, None, "张三", "zc", None)]),
    })
    build_shard(wx, ACCT, "message_0.db",
                {FRIEND: [(1, 1, 1, T0 + 1, "正文", 0)]}, [FRIEND])

    assert export.run_export(ACCT) == 0
    with store.open() as con:
        assert con.execute("SELECT chat_id FROM messages WHERE msg_key=?",
                           (key,)).fetchone()[0] == FRIEND
        assert con.execute("SELECT count(*) FROM pending_reembed WHERE msg_key=?",
                           (key,)).fetchone()[0] == 1, "会话名改了也要重嵌"


# ---------------------------------------------------------------- 失败语义

def test_missing_shard_is_eviction_not_deletion(tmp_path, monkeypatch):
    """缓存淘汰：分片文件不在了 → 这轮看不到它，但**不删索引里的行、不推进它的游标**。"""
    wx, _ = _base(tmp_path, monkeypatch, [(1, 1, 1, T0 + 1, "分片零", 0)])
    build_shard(wx, ACCT, "message_1.db",
                {FRIEND: [(1, 1, 1, T0 - 1, "分片一", 0)]}, [FRIEND])
    assert export.run_export(ACCT, audit_rows=10) == 2

    bad = wx / ACCT / "db_storage" / "message" / "message_1.db"
    for suf in ("", "-wal", "-shm"):
        pathlib.Path(str(bad) + suf).unlink(missing_ok=True)

    from agent_wechat import reader
    r = reader.MessageReader(ACCT)
    list(r.iter_all(audit_cursors={("message_0.db", md5(FRIEND)): 0}, audit_rows=10,
                    audit_sink=lambda rows, refs: None))
    assert ("message_1.db", md5(FRIEND)) not in r.audit_marks
    with store.open() as con:
        assert con.execute(
            "SELECT count(*) FROM messages WHERE shard='message_1.db'").fetchone()[0] == 1, \
            "扫描不到不等于删除"


def test_unreadable_shard_fails_loudly_and_changes_nothing(tmp_path, monkeypatch):
    """源**在但读不了**：导出整体报错（不静默跳过），水位线/游标/索引一律不动。

    与「分片不见了」分开：消失是可解释的淘汰，读不了是故障。把后者静默成「这段没有
    消息」正是任务书 §6 要禁止的那种「把扫描不到当删除」。
    """
    wx, _ = _base(tmp_path, monkeypatch, [(1, 1, 1, T0 + 1, "分片零", 0)])
    build_shard(wx, ACCT, "message_1.db",
                {FRIEND: [(1, 1, 1, T0 - 1, "分片一", 0)]}, [FRIEND])
    assert export.run_export(ACCT, audit_rows=10) == 2
    with store.open() as con:
        before_cursors = store.load_audit_cursors(con, ACCT)
        before_rows = con.execute("SELECT count(*) FROM messages").fetchone()[0]

    bad = wx / ACCT / "db_storage" / "message" / "message_1.db"
    for suf in ("", "-wal", "-shm"):
        pathlib.Path(str(bad) + suf).unlink(missing_ok=True)
    bad.write_bytes(b"not a database")

    with pytest.raises(errors.DecryptError):
        export.run_export(ACCT, audit_rows=10)
    with store.open() as con:
        assert store.load_audit_cursors(con, ACCT) == before_cursors
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == before_rows


def test_cursor_is_not_saved_when_the_write_is_not_committed(tmp_path, monkeypatch):
    """游标在数据提交**之后**才写：这一步失败，下一轮重看同一段（幂等），不是丢历史。"""
    wx, _ = _base(tmp_path, monkeypatch, [(1, 1, 1, T0 + 1, "第一版", 0)])
    assert export.run_export(ACCT) == 1
    build_shard(wx, ACCT, "message_0.db",
                {FRIEND: [(1, 1, 1, T0 + 1, "第二版", 0)]}, [FRIEND])

    real = store.save_audit_cursors
    calls = {"n": 0}

    def boom(con, account, marks):
        calls["n"] += 1
        raise errors.IndexUnavailable("模拟游标写失败", hint="测试注入")

    monkeypatch.setattr(store, "save_audit_cursors", boom)
    with pytest.raises(errors.IndexUnavailable):
        export.run_export(ACCT)
    with store.open() as con:
        assert _content(con) == "第二版", "数据已经提交（顺序是数据先、游标后）"
        assert store.load_audit_cursors(con, ACCT) == \
            {("message_0.db", md5(FRIEND)): 0}, "游标没被这次失败的写入改动"

    monkeypatch.setattr(store, "save_audit_cursors", real)
    assert export.run_export(ACCT) == 0            # 重看同一段，幂等
    with store.open() as con:
        assert _content(con) == "第二版"
        assert store.load_audit_cursors(con, ACCT) == {("message_0.db", md5(FRIEND)): 0}


# ---------------------------------------------------------------- 规模与内存

def test_audit_peak_memory_is_bounded(tmp_path, monkeypatch):
    """回看的峰值内存由「每表一段 + 一个 yield 缓冲」封顶，不随历史长度增长。"""
    rows = [(i, 1, 1, T0 + i, f"历史消息{i:04d}的正文", 0) for i in range(1, 401)]
    _base(tmp_path, monkeypatch, rows)
    assert export.run_export(ACCT, audit_rows=0) == 400

    from agent_wechat import reader
    r = reader.MessageReader(ACCT)

    def peak(fn) -> int:
        tracemalloc.start()
        try:
            fn()
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    def audit_run() -> int:
        seen = []
        list(r.iter_all(batch=100, audit_cursors={}, audit_rows=25,
                        audit_sink=lambda rows, refs: seen.append(len(rows))))
        return sum(seen)

    # 两条路径各自真的读了多少行（与内存测量分开，避免 lambda 里的 sum 干扰）
    assert sum(len(b[0]) for b in r.iter_all(batch=400)) == 400
    assert audit_run() == 25, "一次回看只取每表一段"

    full = peak(lambda: sum(len(b[0]) for b in r.iter_all(batch=400)))
    audit = peak(audit_run)
    # 绝对值有界才是契约：峰值的大头是解密/sqlite 的固定开销，不是历史长度。
    # 不拿它与 `full` 比大小——两条路径的固定开销相同、差值在 tracemalloc 噪声里。
    assert audit < 1_500_000, f"一次回看的峰值必须有界：audit={audit}（full={full}）"


# ---------------------------------------------------------------- 游标是多账号隔离的

def test_audit_cursors_are_per_account():
    import sqlite3
    from agent_wechat import migrations

    con = sqlite3.connect(":memory:")
    try:
        con.executescript(store.SCHEMA)
        store.save_audit_cursors(con, "acct_a", {("message_0.db", "m" * 32): 7})
        store.save_audit_cursors(con, "acct_b", {("message_0.db", "m" * 32): 3})
        assert store.load_audit_cursors(con, "acct_a") == {("message_0.db", "m" * 32): 7}
        assert store.load_audit_cursors(con, "acct_b") == {("message_0.db", "m" * 32): 3}
        store.clear_audit_cursors(con, "acct_a")
        assert store.load_audit_cursors(con, "acct_a") == {}
        assert store.load_audit_cursors(con, "acct_b") == {("message_0.db", "m" * 32): 3}
    finally:
        con.close()
