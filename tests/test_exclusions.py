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


@pytest.mark.parametrize("name,table", [
    ("upsert_batch", "messages"),
    ("upsert_media_refs", "media_refs"),
    ("upsert_msg_parts", "msg_parts"),
])
def test_write_entry_points_read_exclusions_inside_the_write_transaction(name, table,
                                                                        tmp_path, monkeypatch):
    """**三个写入口的排除判据都必须在写事务里读**（spec §5.4「判据与写入同事务」）。

    钉的是**后果**，不是「有没有那句 BEGIN」——光有 BEGIN 也不够：判据若读在 BEGIN 之前，
    窗口照样存在。构造：在 `_filter_excluded` **返回之后**、本函数写库之前，让**另一条连接**
    （同进程，等价于另一个进程里的 `purge --chat`）把窗口里会发生的事做一遍
    （阶段 1 写墓碑、阶段 2 删数据），`busy_timeout=0` 让它立刻报错而非挂 5 秒。

    - 修好之后：本连接此刻已持写锁，那个写者**进不来**（实测 main 与 ATTACH 的 exclusions
      两边都报 `database is locked`），窗口不存在 ⇒ 本批照常写入（没有墓碑）;
    - 丢掉事务保护（这三个函数里**任一处**）：墓碑当场提交成功，随后的写把已删的行写回去，
      而此后**没有任何东西会清它**（永久复活）⇒ 两条断言都红：
      `blocked` 为假、`表 ⋈ excluded` 计数为 1。

    `upsert_batch` 那句 BEGIN IMMEDIATE 是既有代码——它在本次修复前**没有任何测试**
    （删掉它全套照样绿），这条把它一并护住。
    """
    import sqlite3
    from agent_wechat import store

    key = "a|aaaa|message_0.db|1"
    msg = {"msg_key": key, "account": "a", "chat_md5": "aaaa", "chat_id": "victim",
           "shard": "message_0.db", "local_id": 1, "server_id": 1, "ts": 100,
           "msg_type": 1, "sender_id": None, "sender_name": None, "is_self": 0,
           "content": "本该删掉的", "media_hint": None}
    calls = {
        "upsert_batch": lambda con: store.upsert_batch(con, [msg]),
        "upsert_media_refs": lambda con: store.upsert_media_refs(
            con, [(key, "image", "f" * 32, None)]),
        "upsert_msg_parts": lambda con: store.upsert_msg_parts(
            con, [(key, 57, None, None, b"<x/>")]),
    }

    real = store._filter_excluded
    seen: dict = {}

    def purge_in_the_window(con, keys):
        out = real(con, keys)                 # ← 判据在这里读完，窗口从这里开始
        other = None
        try:
            other = store.connect()
            other.execute("PRAGMA busy_timeout=0")
            exclusions.add(other, [(key, "a", "aaaa")])           # 阶段 1：墓碑
            other.execute(f"DELETE FROM {table} WHERE msg_key=?", (key,))   # 阶段 2：删
            other.commit()
            seen["blocked"] = False
        except sqlite3.OperationalError as e:
            # `store.connect()` 自己在被锁时也会抛——那同样是「写者进不来」，一并算挡住。
            seen["blocked"] = "locked" in str(e)
        finally:
            if other is not None:
                other.rollback()
                other.close()
        return out

    monkeypatch.setattr(store, "_filter_excluded", purge_in_the_window)
    with store.open() as con:
        assert calls[name](con) == 0, "没有墓碑提交成功，这一批本来就该照常写入"
    with store.open() as con:
        resurrected = con.execute(
            f"SELECT count(*) FROM {table} t JOIN exclusions.excluded e "
            "ON e.msg_key = t.msg_key").fetchone()[0]
        assert resurrected == 0, \
            f"{name}：墓碑已提交而那一行还在——已删的行被写回来了（永久复活）"
        assert seen.get("blocked"), \
            f"{name}：另一个写者没被挡住——判据读在写事务之外，窗口存在"


def test_appmsg_rebuild_reports_rows_actually_written(tmp_path, monkeypatch):
    """**I-3**：`parts` 报**实际落库**的条数，不许把被墓碑挡下的算进去。

    回调与返回值的口径必须一致，否则 CLI 会印「完成：本轮写入 msg_parts N 条」而实际一条没写
    ——同一份交付里另两处（`run_export` / `media_rebuild_refs` 的返回值）早已是落库口径，
    所以这是不一致，不是设计选择。

    **判别性**：把返回值改回缓冲计数（`return {"parts": parts, …}`）即红。
    """
    from agent_wechat import store, usecases

    key = "acct|aaaa|message_0.db|1"
    raw = "<msg><appmsg><title>标题</title><type>5</type></appmsg></msg>"

    class _FakeReader:
        def iter_parts_input(self):
            yield 1001, key, raw

    monkeypatch.setattr(usecases, "ChatContext", lambda acct=None: type(
        "C", (), {"account": "acct", "reader": _FakeReader()})())
    with store.open() as con:                     # 先落墓碑：这批载荷整批都该被挡下
        exclusions.add(con, [(key, "acct", "aaaa")])
    res = usecases.appmsg_rebuild_parts(account="acct")
    assert res["skipped"] == 1, f"前提：那条载荷应当被墓碑挡下：{res}"
    assert res["parts"] == 0, f"落库 0 条就得报 0：{res}"
    with store.open() as con:
        assert con.execute("SELECT count(*) FROM msg_parts").fetchone()[0] == 0, \
            "前提：确实一行都没落库"


def test_parts_write_after_the_temp_table_write_is_still_locked(tmp_path, monkeypatch):
    """`appmsg_rebuild_parts` 的**真实形态**：它先写 TEMP 表（sid_map）、再写 msg_parts。

    上面那条参数化测试用的是干净的连接（自动提交），所以 `upsert_msg_parts` 里那句
    `if not con.in_transaction: BEGIN IMMEDIATE` 能触发。本形态**不能**：TEMP 写会先开出
    一个 **deferred** 事务——`con.in_transaction` 为真（实测 True），而主库写锁还没拿到
    （实测：此刻另一个连接**可以**提交墓碑）。于是判据的读与随后的写之间照样有窗口，
    这个洞只能由**调用方**堵：`appmsg_rebuild_parts` 是这次写入的事务 owner
    （commit 在 `store.open()` 出口），它在入口把事务开成 IMMEDIATE。

    判别性：删掉 `usecases.appmsg_rebuild_parts` 入口那句 `BEGIN IMMEDIATE`，本测试红
    （实测：墓碑提交成功、`msg_parts` 里那行载荷被写回来）。
    """
    import sqlite3
    from agent_wechat import store, usecases

    key = "acct|aaaa|message_0.db|1"
    raw = "<msg><appmsg><title>标题</title><type>5</type></appmsg></msg>"

    class _FakeReader:
        def iter_parts_input(self):
            yield 1001, key, raw

    monkeypatch.setattr(usecases, "ChatContext", lambda acct=None: type(
        "C", (), {"account": "acct", "reader": _FakeReader()})())

    real = store._filter_excluded
    seen: dict = {}

    def purge_in_the_window(con, keys):
        out = real(con, keys)
        other = None
        try:
            other = store.connect()
            other.execute("PRAGMA busy_timeout=0")
            exclusions.add(other, [(key, "acct", "aaaa")])          # 阶段 1：墓碑
            other.execute("DELETE FROM msg_parts WHERE msg_key=?", (key,))   # 阶段 2：删
            other.commit()
            seen["blocked"] = False
        except sqlite3.OperationalError as e:
            seen["blocked"] = "locked" in str(e)
        finally:
            if other is not None:
                other.rollback()
                other.close()
        return out

    monkeypatch.setattr(store, "_filter_excluded", purge_in_the_window)
    res = usecases.appmsg_rebuild_parts(account="acct")
    # `parts == 1` 在这里**不是**「多报」：本测试挡住的正是那次墓碑写入，所以这一批
    # `skipped == 0`、那一条载荷真的落库了（`parts` 自终审 I-3 起是**实际落库**数，
    # 见 `test_appmsg_rebuild_reports_rows_actually_written`）。两句一起断言，把
    # 「1 条缓冲 = 1 条落库」钉死——只写 `parts == 1` 的话，一个恒报缓冲数的实现照样过。
    assert res["parts"] == 1 and res["skipped"] == 0, f"夹具没走到写入那一步：{res}"
    with store.open() as con:
        resurrected = con.execute(
            "SELECT count(*) FROM msg_parts t JOIN exclusions.excluded e "
            "ON e.msg_key = t.msg_key").fetchone()[0]
        assert resurrected == 0, "墓碑已提交而载荷还在——appmsg 原始载荷被写回来了（永久复活）"
        assert seen.get("blocked"), \
            "appmsg rebuild 的入口没把事务开成 IMMEDIATE——判据读在写事务之外，窗口存在"
