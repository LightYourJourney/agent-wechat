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
