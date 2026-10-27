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

SHARD = "message_0.db"


def _set_paths(monkeypatch, wx: pathlib.Path, home: pathlib.Path) -> None:
    monkeypatch.setattr(paths, "XWECHAT_FILES", wx)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")
    keys.save_cached_hex(SELF, PW.encode().hex())


def _base(tmp_path, monkeypatch, rows: list[tuple], *, shard: str = SHARD,
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


def _key(local_id: int = 1, shard: str = SHARD) -> str:
    return f"{ACCT}|{md5(FRIEND)}|{shard}|{local_id}"


def _content(con, local_id: int = 1) -> str:
    return con.execute("SELECT content FROM messages WHERE msg_key=?",
                       (_key(local_id),)).fetchone()[0]


def _cursors(con) -> dict:
    return store.load_audit_cursors(con, ACCT)


# ---------------------------------------------------------------- 回看覆盖一整段历史

def test_audit_reaches_old_rows_over_several_runs(tmp_path, monkeypatch):
    """游标绕圈：每轮只看一小段，若干轮后覆盖到最早的那条。

    编辑第 1 条 → 第 1 轮看 3-4、第 2 轮 5-6、第 3 轮绕回 0、第 4 轮才看 1-2。
    """
    wx, _ = _base(tmp_path, monkeypatch, _rows(6))
    assert export.run_export(ACCT, audit_rows=2) == 6
    with store.open() as con:
        assert _cursors(con) == {(SHARD, md5(FRIEND)): 2}

    build_shard(wx, ACCT, SHARD,
                {FRIEND: [(1, 1, 1, T0 + 1, "改过的正文", 0)] + _rows(6)[1:]}, [FRIEND])

    assert export.run_export(ACCT, audit_rows=2) == 0
    with store.open() as con:
        assert _cursors(con)[(SHARD, md5(FRIEND))] == 4
        assert _content(con) == "第1版正文", "这一轮还没轮到它"
    assert export.run_export(ACCT, audit_rows=2) == 0      # → 6
    assert export.run_export(ACCT, audit_rows=2) == 0      # 空段 → 绕回 0
    with store.open() as con:
        assert _cursors(con)[(SHARD, md5(FRIEND))] == 0
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
        assert con.execute("SELECT count(*) FROM pending_reembed").fetchone()[0] == 0, \
            "没有改写就不该投递重嵌"


# ---------------------------------------------------------------- 跨分片 / 重复 local_id

def test_same_local_id_in_two_shards_is_tracked_separately(tmp_path, monkeypatch):
    wx, _ = _base(tmp_path, monkeypatch, [(1, 1, 1, T0 + 1, "分片零正文", 0)])
    build_shard(wx, ACCT, "message_1.db",
                {FRIEND: [(1, 1, 1, T0 - 1, "分片一正文", 0)]}, [FRIEND])
    assert export.run_export(ACCT, audit_rows=10) == 2

    # 两个分片里同名 local_id 各自改写
    build_shard(wx, ACCT, SHARD,
                {FRIEND: [(1, 1, 1, T0 + 1, "分片零改后", 0)]}, [FRIEND])
    build_shard(wx, ACCT, "message_1.db",
                {FRIEND: [(1, 1, 1, T0 - 1, "分片一改后", 0)]}, [FRIEND])
    assert export.run_export(ACCT, audit_rows=10) == 0
    with store.open() as con:
        got = dict(con.execute(
            "SELECT shard, content FROM messages WHERE account=?", (ACCT,)).fetchall())
        assert got == {SHARD: "分片零改后", "message_1.db": "分片一改后"}


# ---------------------------------------------------------------- 排除 / purge 不复活

def test_purged_chat_is_not_resurrected_by_the_audit(tmp_path, monkeypatch):
    wx, _ = _base(tmp_path, monkeypatch, _rows(3))
    assert export.run_export(ACCT) == 3
    purge.execute_chat(purge.plan_chat(
        [{"account": ACCT, "chat_id": FRIEND, "chat_md5": md5(FRIEND)}]))

    # 源库改写 + 重扫：墓碑必须挡住，且不投递重嵌
    build_shard(wx, ACCT, SHARD,
                {FRIEND: [(1, 1, 1, T0 + 1, "复活尝试", 0)] + _rows(3)[1:]}, [FRIEND])
    assert export.run_export(ACCT) == 0
    with store.open() as con:
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert exclusions.count(con) == 3
        assert con.execute("SELECT count(*) FROM pending_reembed").fetchone()[0] == 0


# ---------------------------------------------------------------- 会话名恢复

def test_audit_repairs_a_degraded_chat_id(tmp_path, monkeypatch):
    """先按 `md5:<hex>` 落库，等联系人名册补上后，回看把它改成真名。"""
    wx, _ = _base(tmp_path, monkeypatch, [(1, 1, 1, T0 + 1, "正文", 0)], friend_known=False)
    assert export.run_export(ACCT) == 1
    with store.open() as con:
        assert con.execute("SELECT chat_id FROM messages WHERE msg_key=?",
                           (_key(),)).fetchone()[0] == messages.degraded_chat_id(md5(FRIEND))

    # 名册补上：session / contact / 分片 Name2Id 都能把 md5 反查成 FRIEND
    enc_db(wx / ACCT / "db_storage" / "session" / "session.db", {
        "SessionTable": ("CREATE TABLE SessionTable (username TEXT)", [(FRIEND,)])})
    enc_db(wx / ACCT / "db_storage" / "contact" / "contact.db", {
        "name2id": ("CREATE TABLE name2id (username TEXT)", [(FRIEND,)]),
        "contact": ("CREATE TABLE contact (username TEXT, alias TEXT, remark TEXT, "
                    "nick_name TEXT, delete_flag INTEGER)",
                    [(FRIEND, None, "张三", "zc", None)]),
    })
    build_shard(wx, ACCT, SHARD, {FRIEND: [(1, 1, 1, T0 + 1, "正文", 0)]}, [FRIEND])

    assert export.run_export(ACCT) == 0
    with store.open() as con:
        assert con.execute("SELECT chat_id FROM messages WHERE msg_key=?",
                           (_key(),)).fetchone()[0] == FRIEND
        assert con.execute("SELECT count(*) FROM pending_reembed WHERE msg_key=?",
                           (_key(),)).fetchone()[0] == 1, "会话名改了也要重嵌"


def test_a_degraded_name_never_overwrites_a_real_one(tmp_path):
    """名字源临时读不出来时，回看会把 `chat_id` 解码成 `md5:<hex>`——**不许写坏真名**。

    这是回看独有的风险：前向扫描只碰新行，而回看会重读早已解析好的旧行。反向仍然要更新
    （`md5:<hex>` → 真名），否则「会话标识恢复」就失灵了。
    """
    db = tmp_path / "i.db"
    con = store.connect(db)
    try:
        good = messages.make_row(account="a", chat_md5="m" * 32, chat_id="wxid_friend01",
                                 shard=SHARD, local_id=1, ts=1, msg_type=1,
                                 sender_id=None, sender_name=None, is_self=0,
                                 content="正文", media_hint=None)
        assert store.upsert_batch(con, [good]) == 0
        degraded = dict(good, chat_id=messages.degraded_chat_id("m" * 32))
        store.upsert_batch(con, [degraded])
        assert con.execute("SELECT chat_id FROM messages").fetchone()[0] == "wxid_friend01"
        assert con.execute("SELECT count(*) FROM pending_reembed").fetchone()[0] == 0, \
            "降级名不算变更，不该投递重嵌"

        con.execute("UPDATE messages SET chat_id=?", (messages.degraded_chat_id("m" * 32),))
        con.commit()
        store.upsert_batch(con, [good])
        assert con.execute("SELECT chat_id FROM messages").fetchone()[0] == "wxid_friend01", \
            "退化 → 真名必须能修好"
    finally:
        con.close()


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
    list(r.iter_all(audit_cursors={(SHARD, md5(FRIEND)): 0}, audit_rows=10,
                    audit_sink=lambda rows, refs: None))
    assert ("message_1.db", md5(FRIEND)) not in r.audit_marks
    with store.open() as con:
        assert con.execute(
            "SELECT count(*) FROM messages WHERE shard='message_1.db'").fetchone()[0] == 1, \
            "扫描不到不等于删除"


def test_unreadable_shard_fails_loudly_and_writes_nothing(tmp_path, monkeypatch):
    """源**在但读不了**：导出整体报错，游标与索引都不动。

    这类失败发生在 `iter_all` 的**开头**（`md5_map()` 要把所有分片都解一次来反查会话名），
    任何批次都还没写——所以「什么都不变」是真的。**不要**把它读成「导出是原子的」：
    如果是跑到一半才失败（磁盘错、锁），前面的批次已经提交、不会回滚，只有游标被扣住
    （见 `test_a_locked_index_leaves_cursors_unadvanced` 与游标顺序用例）。
    """
    wx, _ = _base(tmp_path, monkeypatch, [(1, 1, 1, T0 + 1, "分片零", 0)])
    build_shard(wx, ACCT, "message_1.db",
                {FRIEND: [(1, 1, 1, T0 - 1, "分片一", 0)]}, [FRIEND])
    assert export.run_export(ACCT, audit_rows=10) == 2
    with store.open() as con:
        before_cursors = _cursors(con)

    # 分片零里有一条改写——但坏分片会让 md5_map 先失败，它不会被提交
    build_shard(wx, ACCT, SHARD,
                {FRIEND: [(1, 1, 1, T0 + 1, "分片零改后", 0)]}, [FRIEND])
    bad = wx / ACCT / "db_storage" / "message" / "message_1.db"
    for suf in ("", "-wal", "-shm"):
        pathlib.Path(str(bad) + suf).unlink(missing_ok=True)
    bad.write_bytes(b"not a database")

    with pytest.raises(errors.DecryptError):
        export.run_export(ACCT, audit_rows=10)
    with store.open() as con:
        assert _cursors(con) == before_cursors
        assert _content(con) == "分片零", "失败发生在任何写入之前"
        assert con.execute(
            "SELECT count(*) FROM messages WHERE shard='message_1.db'").fetchone()[0] == 1


def test_a_locked_index_leaves_cursors_unadvanced(tmp_path, monkeypatch):
    """数据库锁：写不进去时导出失败、游标不动，锁释放后重跑即可恢复。"""
    wx, _ = _base(tmp_path, monkeypatch, _rows(3))
    assert export.run_export(ACCT, audit_rows=10) == 3
    with store.open() as con:
        before = _cursors(con)

    build_shard(wx, ACCT, SHARD,
                {FRIEND: [(1, 1, 1, T0 + 1, "锁期间改写", 0)] + _rows(3)[1:]}, [FRIEND])
    holder = store.connect()
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO display_names VALUES ('a','x','y','z')")
    try:
        with pytest.raises(errors.IndexUnavailable):
            export.run_export(ACCT, audit_rows=10)
    finally:
        holder.rollback()
        holder.close()

    with store.open() as con:
        assert _cursors(con) == before
        assert _content(con) == "第1版正文", "写不进去就没有半截数据"
    assert export.run_export(ACCT, audit_rows=10) == 0
    with store.open() as con:
        assert _content(con) == "锁期间改写", "锁释放后重跑就能续上"


def test_cursor_is_not_saved_when_the_write_is_not_committed(tmp_path, monkeypatch):
    """游标在数据提交**之后**才写：这一步失败，下一轮重看同一段（幂等），不是丢历史。

    用 5 行 + `audit_rows=2`：游标会从 2 推进到 4，断言「还是 2」才有区分度
    （单行夹具里游标恒为 0，写过没写过都一样——那种断言是空转）。
    """
    wx, _ = _base(tmp_path, monkeypatch, _rows(5))
    assert export.run_export(ACCT, audit_rows=2) == 5
    with store.open() as con:
        assert _cursors(con)[(SHARD, md5(FRIEND))] == 2
    # 改写第 3 条（下一段 3-4 才会看到它）
    build_shard(wx, ACCT, SHARD,
                {FRIEND: _rows(2) + [(3, 1, 1, T0 + 3, "第三版正文", 0)] + _rows(5)[3:]},
                [FRIEND])

    real = store.save_audit_cursors

    def boom(con, account, marks):
        raise errors.IndexUnavailable("模拟游标写失败", hint="测试注入")

    monkeypatch.setattr(store, "save_audit_cursors", boom)
    with pytest.raises(errors.IndexUnavailable):
        export.run_export(ACCT, audit_rows=2)
    with store.open() as con:
        assert _content(con, 3) == "第三版正文", "数据已经提交（顺序是数据先、游标后）"
        assert _cursors(con)[(SHARD, md5(FRIEND))] == 2, "游标没被这次失败的写入改动"

    monkeypatch.setattr(store, "save_audit_cursors", real)
    assert export.run_export(ACCT, audit_rows=2) == 0      # 重看同一段，幂等
    with store.open() as con:
        assert _content(con, 3) == "第三版正文"
        assert _cursors(con)[(SHARD, md5(FRIEND))] == 4


# ---------------------------------------------------------------- full 的语义

def test_full_skips_the_audit_and_account_full_clears_cursors(tmp_path, monkeypatch):
    """`--full` 的前向扫描已经读完整段历史——**不做回看**，也不该读游标。

    账号级 full 同时把游标清零（下一轮增量从 0 重新走）；单会话 full 不动游标。
    """
    wx, _ = _base(tmp_path, monkeypatch, _rows(5))
    assert export.run_export(ACCT, audit_rows=2) == 5
    with store.open() as con:
        assert _cursors(con)[(SHARD, md5(FRIEND))] == 2

    # 单会话 full：改写被前向扫描修正，游标保持原位（不回看）
    build_shard(wx, ACCT, SHARD,
                {FRIEND: [(1, 1, 1, T0 + 1, "full 改后", 0)] + _rows(5)[1:]}, [FRIEND])
    assert export.run_export(ACCT, full=True, chat_id=FRIEND, audit_rows=2) == 5
    with store.open() as con:
        assert _content(con) == "full 改后"
        assert _cursors(con)[(SHARD, md5(FRIEND))] == 2, "单会话 full 不动游标"

    # 账号级 full：游标清零
    assert export.run_export(ACCT, full=True, audit_rows=2) == 5
    with store.open() as con:
        assert _cursors(con) == {}


# ---------------------------------------------------------------- 规模与峰值内存

def test_audit_peak_memory_is_flat_across_history_sizes(tmp_path, monkeypatch):
    """**比较**而不是单点：同样 `audit_rows`，历史 200 行与 800 行的回看峰值应当接近。

    只测 400 行 + 一个绝对阈值抓不到 O(历史) 回归；两个量级一起看才像一次比较。
    """
    from agent_wechat import reader

    def measure(n_rows: int) -> int:
        build_shard(tmp_path / "wx" / "xwechat_files", ACCT, SHARD,
                    {FRIEND: [(i, 1, 1, T0 + i, f"历史消息{i:04d}的正文", 0)
                              for i in range(1, n_rows + 1)]}, [FRIEND])
        r = reader.MessageReader(ACCT)
        # 前向判据顶到表尾（resume=极大值）→ 只测回看那一段
        resume = {(SHARD, md5(FRIEND)): 10 ** 9}

        def run() -> int:
            seen = []
            list(r.iter_all(resume=dict(resume), batch=100, audit_cursors={},
                            audit_rows=25,
                            audit_sink=lambda rows, refs: seen.append(len(rows))))
            return sum(seen)

        tracemalloc.start()
        try:
            assert run() == 25, "一次回看只取每表一段"
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    _base(tmp_path, monkeypatch, [(1, 1, 1, T0 + 1, "先建树", 0)])
    small = measure(200)
    large = measure(800)
    assert large < 1_500_000, f"回看峰值必须有界：{large}"
    assert large < small * 2, f"历史翻 4 倍，回看峰值不该跟着涨：{small} → {large}"


# ---------------------------------------------------------------- 游标是多账号隔离的

def test_audit_cursors_are_per_account():
    import sqlite3

    con = sqlite3.connect(":memory:")
    try:
        con.executescript(store.SCHEMA)
        store.save_audit_cursors(con, "acct_a", {(SHARD, "m" * 32): 7})
        store.save_audit_cursors(con, "acct_b", {(SHARD, "m" * 32): 3})
        assert store.load_audit_cursors(con, "acct_a") == {(SHARD, "m" * 32): 7}
        assert store.load_audit_cursors(con, "acct_b") == {(SHARD, "m" * 32): 3}
        store.clear_audit_cursors(con, "acct_a")
        assert store.load_audit_cursors(con, "acct_a") == {}
        assert store.load_audit_cursors(con, "acct_b") == {(SHARD, "m" * 32): 3}
    finally:
        con.close()
