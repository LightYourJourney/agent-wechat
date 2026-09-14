"""schema 声明清单的完整性测试。

这份清单是「关于微信的知识」——它的对照物是 docs/schema-4.1.13.md：四个能力声明的
每一列都要能在那份文档里找到。`contact.db` 的声明从 T7 起也纳入核对（此前那份文档只有
描述性列举，`chat_room` / `ext_buffer` 连一行 DDL 都没有，核对基准不存在）。
"""
import pathlib
import sqlite3

import pytest

from agent_wechat import errors, schema


def test_every_capability_declares_at_least_one_requirement():
    for cap, reqs in schema.CAPABILITIES.items():
        assert reqs, f"能力 {cap} 没有任何声明"
        for r in reqs:
            assert r.db, f"{cap} 的声明缺 db"
            assert r.table, f"{cap} 的声明缺 table"


def test_column_lists_are_non_empty_and_unique():
    """空元组只在「只要求表存在」时有意义；本清单里没有这种声明，所以全部非空。
    重复的列名说明是手抄时抄重了。"""
    for cap, reqs in schema.CAPABILITIES.items():
        for r in reqs:
            assert r.columns, f"{cap}.{r.table} 没有声明列"
            assert len(set(r.columns)) == len(r.columns), f"{cap}.{r.table} 有重复列名"


def test_the_four_capabilities_are_exactly_these():
    """能力名是降级策略表的键（T5）——多一个少一个都会让策略表失配。"""
    assert set(schema.CAPABILITIES) == {"contacts", "roster", "messages", "username_pool"}


def test_all_declarations_match_the_schema_doc():
    """四个能力声明的每一列，都要能在 docs/schema-4.1.13.md 里找到（逐列核对）。

    这条测试的价值是「声明不是凭记忆写的」——文档改了它就会红。`contact.db` 的声明
    也在这里：它曾在文档里无一行 DDL（`chat_room` / `ext_buffer` 只有名字或干脆缺席），
    T7 补齐后才有了可核对的基准。
    """
    doc = (pathlib.Path(__file__).resolve().parents[1]
           / "docs" / "schema-4.1.13.md").read_text()
    declared = {c for reqs in schema.CAPABILITIES.values() for r in reqs
                for c in r.columns}
    missing = [c for c in declared if c not in doc]
    assert not missing, f"这些列在 schema-4.1.13.md 里找不到，声明可能与实测脱节：{missing}"


def test_schema_drift_carries_the_fields_the_degrade_table_needs():
    d = errors.SchemaDrift("roster", "contact/contact.db", "chat_room")
    assert d.capability == "roster" and d.table == "chat_room"
    assert d.missing_columns == ()
    d2 = errors.SchemaDrift("contacts", "contact/contact.db", "contact",
                            missing_columns=("nick_name",))
    assert d2.missing_columns == ("nick_name",)
    assert isinstance(d2, errors.AgentWeChatError)     # core 层的契约


def _con(*ddl: str):
    con = sqlite3.connect(":memory:")
    for s in ddl:
        con.execute(s)
    return con


def test_probe_ok_when_table_and_columns_match():
    con = _con("CREATE TABLE contact (username TEXT, alias TEXT, remark TEXT, nick_name TEXT)")
    r = schema.probe(con, schema.Requirement("contact/contact.db", "contact",
                                             ("username", "alias", "remark", "nick_name")))
    assert r.ok and r.missing_columns == ()


def test_probe_reports_missing_table_as_empty_columns():
    """实测（spec §2.2）：PRAGMA table_info 对不存在的表返回**空 list**、不抛异常。
    所以「表不存在」与「表在但没有任何列」在这个探针下不可区分——而后者在 SQLite 里
    不可能存在，所以空 = 表不存在是安全的读法。"""
    con = _con()
    r = schema.probe(con, schema.Requirement("contact/contact.db", "nope", ("a",)))
    assert not r.ok and r.missing_columns == ()


def test_probe_names_the_missing_columns():
    """只报缺的、不报多出来的——微信**新增**列不是漂移（我们不用它就不会崩）。"""
    con = _con("CREATE TABLE contact (username TEXT, extra_col TEXT)")
    r = schema.probe(con, schema.Requirement("contact/contact.db", "contact",
                                             ("username", "nick_name")))
    assert not r.ok and r.missing_columns == ("nick_name",)


def test_probe_accepts_an_explicit_table_name_for_the_wildcard():
    """`Msg_*` 的真名是 md5、运行时才知道，所以探测要能接收显式的真实表名。"""
    con = _con("CREATE TABLE Msg_abc123 (local_id INTEGER, local_type INTEGER, "
               "real_sender_id INTEGER, create_time INTEGER, message_content BLOB, "
               "WCDB_CT_message_content INTEGER)")
    req = schema.Requirement("message/*.db", "Msg_*",
                             ("local_id", "local_type", "real_sender_id", "create_time",
                              "message_content", "WCDB_CT_message_content"))
    assert schema.probe(con, req, table="Msg_abc123").ok
    assert not schema.probe(con, req, table="Msg_missing").ok


def test_probe_works_on_a_connection_that_returns_bytes():
    """`reader._open` 的连接统一 `text_factory = bytes`（库内 TEXT 含非 UTF-8 字节，
    docs/schema-4.1.13.md:71）——那时 `PRAGMA table_info` 的列名以 **bytes** 回来。
    不归一化的话每个声明列都被判成「缺」：**健康库上四个能力全部误报漂移**。
    （T3 把探测接进 reader 时实测踩到，T2 的单测用的都是普通连接所以没暴露。）"""
    con = _con("CREATE TABLE contact (username TEXT, nick_name TEXT)")
    con.text_factory = bytes
    req = schema.Requirement("contact/contact.db", "contact", ("username", "nick_name"))
    assert schema.probe(con, req).ok


def test_probe_is_silent_on_hostile_table_names():
    """表名来自 sqlite_master（微信自己的），但仍然不该有注入面——实测带引号的表名
    同样返回空 list、不抛。"""
    con = _con()
    r = schema.probe(con, schema.Requirement("x.db", "t", ("a",)), table="t' OR 1=1 --")
    assert not r.ok
