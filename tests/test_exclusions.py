"""exclusions 模块的单元测试。**不得指向真实 ~/.agent-wechat**（红线同 test_purge.py）。"""
import pathlib

import pytest

from agent_wechat import exclusions, paths


@pytest.fixture(autouse=True)
def _never_touch_real_home(monkeypatch, tmp_path):
    """红线（同 `tests/test_purge.py` 的 autouse fixture）：**每一个**测试都不得指向真实数据。

    `store.DB_PATH` 必须单独 patch：它是 `paths.HOME_DIR / "index.db"`，在**导入时**就绑定了，
    只 patch `paths.HOME_DIR` 对它无效——而 `store.open()` 走的就是它。本文件里
    `run_export` / `store.open()` 这类调用会因此落到真实的 `~/.agent-wechat/index.db` 上。
    """
    from agent_wechat import store
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")
    # 断言查的是**结果**，不是「patch 了哪些常量」——漏 patch 的那一刻就要当场红
    assert store.DB_PATH.is_relative_to(tmp_path)
    assert exclusions.path().is_relative_to(tmp_path)
    yield


def test_path_is_call_time_and_follows_home_dir():
    """重定向 HOME_DIR 之后路径必须跟着走——import 时绑定值的常量会让红线 fixture 静默失效。"""
    before = exclusions.path()
    assert before.parent == paths.HOME_DIR
    assert exclusions.path().name == "exclusions.db"


def test_add_is_idempotent_and_count_matches(tmp_path):
    import sqlite3
    con = sqlite3.connect(str(tmp_path / "x.db"))
    con.execute("ATTACH DATABASE ? AS exclusions", (str(tmp_path / "ex.db"),))
    con.executescript(exclusions.SCHEMA)
    rows = [("a|m1|message_0.db|1", "a", "m1"), ("a|m1|message_0.db|2", "a", "m1")]
    assert exclusions.add(con, rows) == 2
    assert exclusions.add(con, rows) == 2, "重复写入必须幂等（INSERT OR IGNORE）"
    assert exclusions.count(con) == 2
    assert exclusions.by_chat(con) == [("m1", 2)]


def test_derived_columns_match_msg_key(tmp_path):
    """account / chat_md5 两列是 msg_key 的派生副本，存在的唯一凭据是它们不失真（spec §4）。"""
    import sqlite3
    con = sqlite3.connect(str(tmp_path / "x.db"))
    con.execute("ATTACH DATABASE ? AS exclusions", (str(tmp_path / "ex.db"),))
    con.executescript(exclusions.SCHEMA)
    exclusions.add(con, [("acct|deadbeef|message_3.db|77", "acct", "deadbeef")])
    key, acct, md5 = con.execute(
        "SELECT msg_key, account, chat_md5 FROM exclusions.excluded").fetchone()
    parts = key.split("|")
    assert acct == parts[0] and md5 == parts[1]


def test_remove_chat_is_exact_match_not_like(tmp_path):
    """删得准，靠的是等值而不是 LIKE——`_` 与 `%` 都是 LIKE 的通配符，而这里用等值更直接。"""
    import sqlite3
    con = sqlite3.connect(str(tmp_path / "x.db"))
    con.execute("ATTACH DATABASE ? AS exclusions", (str(tmp_path / "ex.db"),))
    con.executescript(exclusions.SCHEMA)
    exclusions.add(con, [("a_c|m1|message_0.db|1", "a_c", "m1"),
                         ("abc|m1|message_0.db|1", "abc", "m1")])
    assert exclusions.remove_chat(con, "a_c", "m1") == 1
    left = [r[0] for r in con.execute("SELECT account FROM exclusions.excluded")]
    assert left == ["abc"], f"`a_c` 的前缀误伤了别的账号：{left}"


def test_connect_attaches_and_creates_the_table(tmp_path):
    """`store.connect()` 之后，排除表必须在同一个连接上可见、且已建好。"""
    from agent_wechat import store
    con = store.connect(tmp_path / "index.db")
    try:
        assert con.execute(
            "SELECT count(*) FROM exclusions.excluded").fetchone()[0] == 0
        assert (tmp_path / "exclusions.db").exists(), "ATTACH 应当建出同目录的排除清单"
        assert con.execute("SELECT count(*) FROM main.sqlite_master "
                           "WHERE name IN ('excluded','idx_excluded_chat')").fetchone()[0] == 0
    finally:
        con.close()


def test_excluded_rows_are_never_written_but_watermark_still_advances(tmp_path):
    """两条一起钉：① 被排除的行不写库；② **水位线照常推进**——跳过写、不跳过水位线，
    否则该会话的增量导出会卡在被排除的那一段反复重扫（spec §5.4 第 1 条）。"""
    from agent_wechat import store
    con = store.connect()                      # 走 patch 过的 DB_PATH，不另开路径
    exclusions.add(con, [("a|aaaa|message_0.db|1", "a", "aaaa")])
    con.commit()
    rows = [{"msg_key": "a|aaaa|message_0.db|1", "account": "a", "chat_md5": "aaaa",
             "chat_id": "victim", "shard": "message_0.db", "local_id": 1, "server_id": 1,
             "ts": 100, "msg_type": 1, "sender_id": None, "sender_name": None,
             "is_self": 0, "content": "被排除的", "media_hint": None},
            {"msg_key": "a|aaaa|message_0.db|2", "account": "a", "chat_md5": "aaaa",
             "chat_id": "victim", "shard": "message_0.db", "local_id": 2, "server_id": 2,
             "ts": 200, "msg_type": 1, "sender_id": None, "sender_name": None,
             "is_self": 0, "content": "正常的", "media_hint": None}]
    skipped = store.upsert_batch(con, rows)
    assert skipped == 1
    assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM messages WHERE content='正常的'"
                       ).fetchone()[0] == 1
    wm = con.execute("SELECT max_local_id FROM watermark WHERE chat_md5='aaaa'").fetchone()[0]
    assert wm == 2, f"水位线必须推进到整批的 max，实际 {wm}"
    con.close()


def test_full_reimport_does_not_resurrect(tmp_path):
    """**「永久」这个承诺的判据本身**（spec §7 第 2 条）：删掉再全量重导，一条都不许回来。

    **必须走 `store.connect()` 而不是自开一个路径**：`run_export` 内部用的是 `store.open()`
    → `store.DB_PATH`。种墓碑种在另一个文件里的话，这条测试会「通过」而什么都没验到。
    """
    from agent_wechat import store
    from agent_wechat import export as export_mod
    con = store.connect()
    exclusions.add(con, [("a|aaaa|message_0.db|1", "a", "aaaa")])
    con.commit()
    con.close()
    # 用桩 reader 模拟「微信库里那条还在」
    row = {"msg_key": "a|aaaa|message_0.db|1", "account": "a", "chat_md5": "aaaa",
           "chat_id": "victim", "shard": "message_0.db", "local_id": 1, "server_id": 1,
           "ts": 100, "msg_type": 1, "sender_id": None, "sender_name": None,
           "is_self": 0, "content": "本该删掉的", "media_hint": None}
    class _R:
        def iter_all(self, **kw): yield [row], []
    n = export_mod.run_export("a", full=True, on_reader=None, _reader_factory=lambda _a: _R())
    assert n == 0, "被排除的行不许计入导出条数"
    con = store.connect()
    assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    con.close()


def test_watermark_advances_when_the_excluded_row_is_the_batch_max(tmp_path):
    """**水位线走全量 rows 的判别性形状**：被排除的那条恰好是批内 max 时必须照样推进。

    为什么不能只靠上面那条（被排除的行 `local_id=1`、正常的是 `2`）：两种实现——水位线走
    全量 rows / 只走写入的 rows——在那里**同值**（都是 2），断言红不了。这里把 max 那条
    排掉之后，「只走写入的行」会停在 1，该会话的增量导出于是卡在被排除的那一段反复重扫
    （spec §5.4 第 1 条）。
    """
    from agent_wechat import store
    con = store.connect()
    exclusions.add(con, [("a|aaaa|message_0.db|2", "a", "aaaa")])
    con.commit()
    rows = [{"msg_key": "a|aaaa|message_0.db|1", "account": "a", "chat_md5": "aaaa",
             "chat_id": "victim", "shard": "message_0.db", "local_id": 1, "server_id": 1,
             "ts": 100, "msg_type": 1, "sender_id": None, "sender_name": None,
             "is_self": 0, "content": "正常的", "media_hint": None},
            {"msg_key": "a|aaaa|message_0.db|2", "account": "a", "chat_md5": "aaaa",
             "chat_id": "victim", "shard": "message_0.db", "local_id": 2, "server_id": 2,
             "ts": 200, "msg_type": 1, "sender_id": None, "sender_name": None,
             "is_self": 0, "content": "被排除的", "media_hint": None}]
    assert store.upsert_batch(con, rows) == 1
    wm = con.execute("SELECT max_local_id FROM watermark WHERE chat_md5='aaaa'").fetchone()[0]
    assert wm == 2, f"水位线必须越过被排除的那条推进到整批的 max，实际 {wm}"
    con.close()


def test_tombstone_committed_mid_export_still_blocks_later_batches(tmp_path):
    """**并发**（spec §5.4、计划的 Review Focus 第 1 条）：导出**跑到一半**时另一个进程
    （`purge --chat`）才提交墓碑，后面那批仍不许被写回去。

    判别性：判据一旦被写成「整次运行只加载一次集合」（那正是被 §5.4 否掉的初版），
    这条会红——第 1 批时清单还是空的。判据必须**每批现查**、且与写同事务。
    中间那步故意走**另一条连接**（同进程，等价于另一个进程），模拟提交真实发生。
    """
    from agent_wechat import store
    from agent_wechat import export as export_mod

    def _row(local_id: int, content: str) -> dict:
        return {"msg_key": f"a|aaaa|message_0.db|{local_id}", "account": "a",
                "chat_md5": "aaaa", "chat_id": "victim", "shard": "message_0.db",
                "local_id": local_id, "server_id": local_id, "ts": local_id * 100,
                "msg_type": 1, "sender_id": None, "sender_name": None, "is_self": 0,
                "content": content, "media_hint": None}

    doomed = _row(2, "本该删掉的")

    class _R:
        def iter_all(self, **kw):
            yield [_row(1, "正常的")], []
            con = store.connect()            # ← 导出进行中，另一个写者提交墓碑
            exclusions.add(con, [(doomed["msg_key"], "a", "aaaa")])
            con.commit()
            con.close()
            yield [doomed], []

    n = export_mod.run_export("a", full=True, on_reader=None, _reader_factory=lambda _a: _R())
    assert n == 1, "第 2 批在被挡下之前就写进去了（陈旧集合）"
    con = store.connect()
    assert [r[0] for r in con.execute("SELECT content FROM messages")] == ["正常的"]
    con.close()


def test_msg_parts_and_media_refs_intercepts(tmp_path):
    """另外两处出口（spec §5.4 第 2、3 条）：`appmsg rebuild` 与 `media rebuild-refs`
    都是**全量重扫微信库**，被排除的 `msg_key` 一条都不许落回这两张表。

    与 `upsert_batch` 分开钉：三处各写各的 `keep`，改坏一处不会连带另两处变红。
    """
    from agent_wechat import store
    con = store.connect()
    exclusions.add(con, [("a|aaaa|message_0.db|1", "a", "aaaa")])
    con.commit()
    parts = [("a|aaaa|message_0.db|1", 57, None, None, b"<excluded/>"),
             ("a|aaaa|message_0.db|2", 57, None, None, b"<kept/>")]
    refs = [("a|aaaa|message_0.db|1", "image", "f" * 32, None),
            ("a|aaaa|message_0.db|2", "video", "d" * 32, None)]
    assert store.upsert_msg_parts(con, parts) == 1
    assert store.upsert_media_refs(con, refs) == 1
    # 数用**不 join messages 的那两个入口**（`count_msg_parts` / `count_media_refs`）：
    # 它们才是核查的判据，而这里根本没有 messages 行可 join。
    assert store.count_msg_parts(con, "a") == 1
    assert store.count_media_refs(con, "a") == 1
    assert [r[0] for r in con.execute(
        "SELECT substr(msg_key, -1) FROM msg_parts")] == ["2"], "被排除的那条落库了"
    assert [r[0] for r in con.execute(
        "SELECT substr(msg_key, -1) FROM media_refs")] == ["2"], "被排除的那条落库了"
    con.close()
