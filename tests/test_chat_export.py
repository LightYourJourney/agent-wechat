# tests/test_chat_export.py
"""甲的回填与导出的用例层测试。夹具按 spec §2.5 的实测形态构造，内容全部合成。"""
import pytest

from agent_wechat import store, usecases


class _FakeReader:
    """替掉 MessageReader：只提供 iter_parts_input。
    **注意**：本夹具**必须**把 `store.DB_PATH` patch 到临时库 —— `store.connect()`
    会执行整个 SCHEMA，指错路径就是把 DDL 写进用户真实的 5.3 GB index.db。"""

    def __init__(self, rows):
        self._rows = rows

    def iter_parts_input(self):
        yield from self._rows


@pytest.fixture()
def con(tmp_path, monkeypatch):
    db = tmp_path / "index.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    c = store.connect(db)
    yield c
    c.close()


def test_rebuild_resolves_ref_msg_key_from_server_id(con, monkeypatch):
    """回指：svrid 命中同一次扫描里的 server_id 时，ref_msg_key 必须被解析出来。
    回归：把 `ref_msg_key` 的解析删掉，这条断言会红。"""
    rows = [
        (1001, "acct|aaa|message_0.db|1", None),                      # 被引用的原文
        (1002, "acct|aaa|message_0.db|2",
         '<msg><appmsg><title>同意</title><type>57</type><refermsg>'
         '<displayname>张三</displayname><content>明天开会</content>'
         '<svrid>1001</svrid></refermsg></appmsg></msg>'),
    ]
    monkeypatch.setattr(usecases, "ChatContext", lambda acct=None: type(
        "C", (), {"account": "acct", "reader": _FakeReader(rows)})())
    res = usecases.appmsg_rebuild_parts(account="acct")
    assert res["parts"] == 1 and res["resolved"] == 1
    got = store.msg_parts_by_keys(con, ["acct|aaa|message_0.db|2"])
    assert got["acct|aaa|message_0.db|2"][2] == "acct|aaa|message_0.db|1"


def test_rebuild_leaves_ref_null_when_target_is_unknown(con, monkeypatch):
    """目标不在本次扫描里 → ref_msg_key 为 NULL，**不是 0、不是空串**。
    渲染端靠 NULL 决定「退化成内联显示」，空串会让它以为能跳。"""
    rows = [(None, "acct|aaa|message_0.db|2",
             '<msg><appmsg><type>57</type><refermsg><content>x</content>'
             '<svrid>999999</svrid></refermsg></appmsg></msg>')]
    monkeypatch.setattr(usecases, "ChatContext", lambda acct=None: type(
        "C", (), {"account": "acct", "reader": _FakeReader(rows)})())
    res = usecases.appmsg_rebuild_parts(account="acct")
    assert res["unresolved"] == 1
    assert store.msg_parts_by_keys(con, ["acct|aaa|message_0.db|2"])[
        "acct|aaa|message_0.db|2"][2] is None


def test_rebuild_counts_rejected_and_malformed_separately(con, monkeypatch):
    """回归：两者混计则「我们主动拒绝」与「解不开」在报告里长得一样。"""
    rows = [
        (None, "acct|aaa|message_0.db|1", "<!DOCTYPE x [<!ENTITY a 'b'>]><msg/>"),
        (None, "acct|aaa|message_0.db|2", "<msg><unclosed></msg>"),
    ]
    monkeypatch.setattr(usecases, "ChatContext", lambda acct=None: type(
        "C", (), {"account": "acct", "reader": _FakeReader(rows)})())
    res = usecases.appmsg_rebuild_parts(account="acct")
    assert res["rejected"] == 1 and res["malformed"] == 1 and res["parts"] == 0


def test_rebuild_does_not_cross_link_a_foreign_account(con, monkeypatch):
    """回归：`msg_parts` 是**多账号共用**的表，而 `sid_map` 只装本账号的 server_id。

    阶段 2 的 `UPDATE` 若**不加账号前缀限定**，异账号的同号 `server_id` 会把引用接到错的消息
    上——那是往用户真实库里写一条**错的引用关系**。删掉那个 `substr` 谓词，这条会红。

    （T4 审阅者在双账号种子上实测：不加限定时外来行的 `ref_msg_key` 变成了
    `'acct|aaa|message_0.db|1'`；加上后保持 NULL。）
    """
    # 先塞一条**别的账号**的行，它的 ref_svrid 会被本账号的引用撞上
    store.upsert_msg_parts(con, [("other|zzz|message_0.db|9", 57, 1001, None, b"<msg/>")])
    con.commit()        # 释放写锁：rebuild 走 store.open() 另开一条连接，不提交会撞 "database is locked"
    rows = [
        (1001, "acct|aaa|message_0.db|1", None),
        (1002, "acct|aaa|message_0.db|2",
         '<msg><appmsg><title>同意</title><type>57</type><refermsg>'
         '<displayname>张三</displayname><content>明天开会</content>'
         '<svrid>1001</svrid></refermsg></appmsg></msg>'),
    ]
    monkeypatch.setattr(usecases, "ChatContext", lambda acct=None: type(
        "C", (), {"account": "acct", "reader": _FakeReader(rows)})())
    usecases.appmsg_rebuild_parts(account="acct")
    assert store.msg_parts_by_keys(
        con, ["other|zzz|message_0.db|9"])["other|zzz|message_0.db|9"][2] is None, \
        "异账号的行被跨账号接上了"


def test_rebuild_is_idempotent(con, monkeypatch):
    rows = [(1, "acct|aaa|message_0.db|1",
             '<msg><appmsg><title>t</title><type>5</type><url>u</url></appmsg></msg>')]
    monkeypatch.setattr(usecases, "ChatContext", lambda acct=None: type(
        "C", (), {"account": "acct", "reader": _FakeReader(rows)})())
    usecases.appmsg_rebuild_parts(account="acct")
    usecases.appmsg_rebuild_parts(account="acct")
    assert store.count_msg_parts(con, "acct") == 1


def test_messages_in_range_takes_the_most_recent_n_not_the_oldest(con, monkeypatch):
    """**取向**是这个函数唯一容易写错的地方（spec §4.4）。
    范围里有 10 条、limit=3，必须是**最近 3 条**，且 truncated=True。
    回归：把 ORDER BY 改成 ASC 再取前 3，这条断言会红。"""
    con.executemany(
        "INSERT INTO messages (msg_key, account, chat_md5, chat_id, shard, local_id,"
        " ts, msg_type, is_self, content, exported_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(f"acct|aaa|message_0.db|{i}", "acct", "aaa", "wxid_testacct02", "message_0.db",
          i, 1000 + i, 1, 0, f"m{i}", 0) for i in range(1, 11)])
    con.commit()
    from agent_wechat import search
    rows, truncated = search.messages_in_range(
        "wxid_testacct02", account="acct", limit=3)
    assert truncated is True
    # **必须**是普通 dict，不是 `sqlite3.Row`：T6 的 `chatview.build` 要 dict
    # （`json.dumps` 序列化、`.get()`、`{**r}` 三样 `Row` 都不支持）。
    # 少了这条，把 `[dict(r) for r in ...]` 简化成 `list(reversed(...))` 会让本文件三条用例
    # **全绿**，而故障推迟到 T6 才爆（T5 审阅者实测：改完 3 passed）。
    assert type(rows[0]) is dict, "必须是 dict，不是 sqlite3.Row"
    assert [r["content"] for r in rows] == ["m8", "m9", "m10"]     # 正序，且是最新的三条


def test_messages_in_range_filters_and_reports_not_truncated(con):
    """**下界是含的**（`[lo, hi)`）：`after_ts=1005` 带回 ts=1005 那条。

    回归：把 `m.ts >= ?` 改成 `m.ts > ?`，或把 `before_ts` 的 `<` 改成 `<=`，这条会红。

    **初稿这条写错过，值得记下来**：原写 `after_ts=1004` 期望 `["m5","m6","m7"]`，
    那**只有独占下界才成立**（含下界会多带回 m4）。而独占下界与本仓三处已落地事实矛盾：
    `search.py:39` 同名参数用 `>=`、`usecases.py:460` 的月份筛选是
    `(m.ts >= ? AND m.ts < ?)`、`tests/test_media.py:143` 把 `month_bounds` 钉成
    `[lo, hi)`「下界含、相邻月首尾相接无缝隙」。**而 T6 把 `month_bounds` 的值直接喂给
    `after_ts`/`before_ts`** —— 照初稿写就是给 `chat export --months` 的**每月第一秒开一个
    静默数据丢失的缝**。（T5 实施者发现并拒绝改断言，标 `xfail(strict=True)` 请裁定。）
    """
    con.executemany(
        "INSERT INTO messages (msg_key, account, chat_md5, chat_id, shard, local_id,"
        " ts, msg_type, is_self, content, exported_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(f"acct|aaa|message_0.db|{i}", "acct", "aaa", "wxid_testacct02", "message_0.db",
          i, 1000 + i, 1, 0, f"m{i}", 0) for i in range(1, 11)])
    con.commit()
    from agent_wechat import search
    rows, truncated = search.messages_in_range(
        "wxid_testacct02", account="acct", after_ts=1005, before_ts=1008)
    assert [r["content"] for r in rows] == ["m5", "m6", "m7"]
    assert truncated is False


def test_messages_in_range_is_scoped_to_one_account_and_one_chat(con):
    """回归：删掉 `m.account = ?` 或 `m.chat_id = ?` 中**任一个**谓词，这条会红。

    （T5 交付时这两个谓词**都没有**夹具钉住：E1/E2 两个变异各自删掉一个，
    全套用例仍然全绿。见 task-5-report.md §3。）

    两条都必须钉：`messages` 是**多账号共用**的表（T4b 的跨账号错接就出在这上面），
    且同一账号下各会话也各是各的。夹具让三条行 **ts 全相同**，于是「谁被带回来」
    只由谓词决定，与 limit、与排序都无关。
    """
    con.executemany(
        "INSERT INTO messages (msg_key, account, chat_md5, chat_id, shard, local_id,"
        " ts, msg_type, is_self, content, exported_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [("acct|aaa|message_0.db|1", "acct", "aaa", "wxid_testacct02",
          "message_0.db", 1, 5000, 1, 0, "本会话", 0),
         ("other|aaa|message_0.db|1", "other", "aaa", "wxid_testacct02",
          "message_0.db", 1, 5000, 1, 0, "异账号同会话", 0),
         ("acct|bbb|message_0.db|1", "acct", "bbb", "wxid_otherchat",
          "message_0.db", 1, 5000, 1, 0, "同账号异会话", 0)])
    con.commit()
    from agent_wechat import search
    rows, truncated = search.messages_in_range("wxid_testacct02", account="acct")
    assert [r["content"] for r in rows] == ["本会话"]
    assert truncated is False   # truncated 的「假」这一半（修复轮后 filters_and_reports 也钉它）
