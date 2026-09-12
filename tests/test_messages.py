"""messages 行契约：列清单防漂移、行构造器、两条哨兵的编解码、类型词汇表。

不打开任何数据库（只读 store.SCHEMA 的 DDL 文本），因此天然不碰真实用户数据。
"""
import hashlib

import pytest

from agent_wechat import messages, reader, store


def _ddl_columns() -> tuple[str, ...]:
    """从 store.SCHEMA 的 CREATE TABLE messages 里抽出列名（按 DDL 顺序）。"""
    body = store.SCHEMA.split("CREATE TABLE IF NOT EXISTS messages (", 1)[1]
    body = body.split(");", 1)[0]
    return tuple(line.split("--", 1)[0].split()[0]
                 for line in body.splitlines() if line.split("--", 1)[0].strip())


# ---------- 列清单 ----------

def test_columns_match_messages_ddl_exactly():
    """COLUMNS 必须与 messages 表的 DDL 逐列同序一致——防两处各自漂移。"""
    assert messages.COLUMNS == _ddl_columns()


def test_row_columns_are_ddl_columns_minus_db_bookkeeping():
    """行契约 = 表的列去掉 id（库分配）与 exported_at（store 记账）。"""
    assert set(messages.COLUMNS) - set(messages.ROW_COLUMNS) == {"id", "exported_at"}
    assert len(messages.ROW_COLUMNS) == len(set(messages.ROW_COLUMNS)) == 14


def test_store_insert_columns_track_row_contract():
    """消费端（store 的 INSERT）绑定的是行契约 + exported_at，不另抄一份清单。"""
    assert store._INSERT_COLUMNS == (*messages.ROW_COLUMNS, "exported_at")


def test_column_list_renders_and_rejects_unknown():
    assert messages.column_list("id", "content", prefix="m.") == "m.id, m.content"
    with pytest.raises(ValueError, match="没有这些列"):
        messages.column_list("no_such_column")


# ---------- 行构造器 ----------

def test_make_row_key_set_equals_row_columns():
    row = messages.make_row(
        account="acct", chat_md5="deadbeef", chat_id="wxid_a", shard="message_0.db",
        local_id=7, ts=1700000000, msg_type=messages.TYPE_TEXT, sender_id=None,
        sender_name=None, is_self=0, content="正文", media_hint=None)
    assert tuple(row) == messages.ROW_COLUMNS          # 键集合与顺序都钉住
    assert row["msg_key"] == "acct|deadbeef|message_0.db|7"


def test_row_key_is_the_four_field_tuple():
    assert messages.row_key("a", "m", "s", 3) == "a|m|s|3"
    # local_id 在会话内 / 分片内都会重复 → 四个字段缺一不可
    assert messages.row_key("a", "m1", "s", 3) != messages.row_key("a", "m2", "s", 3)
    assert messages.row_key("a", "m", "s1", 3) != messages.row_key("a", "m", "s2", 3)


# ---------- 哨兵一：md5:<hex> ----------

def test_md5_sentinel_round_trip():
    for chat_id in ("wxid_testacct01", "888@chatroom", "gh_official", "filehelper"):
        md5hex = messages.chat_md5(chat_id)
        assert md5hex == hashlib.md5(chat_id.encode()).hexdigest()
        degraded = messages.degraded_chat_id(md5hex)
        assert degraded == f"md5:{md5hex}"
        assert messages.chat_md5(degraded) == md5hex   # 退化 id 不得被再 md5 一次


def test_md5_of_matches_hashlib():
    assert messages.md5_of("chatA") == hashlib.md5(b"chatA").hexdigest()


# ---------- 哨兵二：[appmsg: ...] ----------

def test_appmsg_hint_two_forms():
    assert messages.appmsg_hint("分享标题") == "[appmsg: 分享标题]"
    assert messages.appmsg_hint(None) == "[appmsg]"
    assert messages.appmsg_hint("") == "[appmsg]"


def test_appmsg_hint_truncates_title_to_60():
    assert messages.appmsg_hint("标" * 80) == "[appmsg: " + "标" * 60 + "]"


def test_has_appmsg_title_only_accepts_titled_form():
    assert messages.has_appmsg_title("[appmsg: 分享标题]") is True
    assert messages.has_appmsg_title("[appmsg]") is False       # 无标题
    assert messages.has_appmsg_title("[type49]") is False       # 无 <title> 时的兜底文案
    assert messages.has_appmsg_title("[image]") is False


def test_appmsg_sentinel_round_trip():
    for title in ("标题", "x" * 100):
        assert messages.has_appmsg_title(messages.appmsg_hint(title)) is True
    for empty in (None, ""):
        assert messages.has_appmsg_title(messages.appmsg_hint(empty)) is False


# ---------- 类型词汇表 ----------

def test_type_names_complete_and_frozen():
    assert messages.TYPE_NAMES == {
        1: "text", 3: "image", 34: "voice", 43: "video", 47: "sticker",
        48: "location", 51: "status", 10000: "system", 10002: "revoke"}
    # 49 刻意不在表里：reader._type_name(49) 的 "[type49]" 已冻结在索引内容与 MCP 返回里
    assert messages.TYPE_APPMSG == 49 and 49 not in messages.TYPE_NAMES


def test_reader_reexports_type_names():
    assert reader.TYPE_NAMES is messages.TYPE_NAMES
