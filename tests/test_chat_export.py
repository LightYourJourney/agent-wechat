# tests/test_chat_export.py
"""甲的回填与导出的用例层测试。夹具按 spec §2.5 的实测形态构造，内容全部合成。"""
import pytest

from agent_wechat import store, usecases


class _FakeReader:
    """替掉 MessageReader：只提供 iter_parts_input。
    **注意**：本夹具不碰 `store.DB_PATH`（store.connect 会建目录），全部走临时库。"""

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


def test_rebuild_is_idempotent(con, monkeypatch):
    rows = [(1, "acct|aaa|message_0.db|1",
             '<msg><appmsg><title>t</title><type>5</type><url>u</url></appmsg></msg>')]
    monkeypatch.setattr(usecases, "ChatContext", lambda acct=None: type(
        "C", (), {"account": "acct", "reader": _FakeReader(rows)})())
    usecases.appmsg_rebuild_parts(account="acct")
    usecases.appmsg_rebuild_parts(account="acct")
    assert store.count_msg_parts(con, "acct") == 1
