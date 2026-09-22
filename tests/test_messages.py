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


# ---------- 哨兵三：贴纸显示串 ----------

def test_sticker_display_with_name():
    assert messages.sticker_display("d0f46ed3ce8bac33df5f7055656d8bd9", "滑稽") == "[贴纸:滑稽]"


def test_sticker_display_without_name_uses_short_id():
    """无名 → 稳定短 ID（md5 前 8 位）。

    这是 D2 的全部价值所在：LLM 能据此分辨「同一贴纸被发了 5 次」与「5 个不同贴纸」。
    短 ID 必须**稳定**——同 md5 恒同串，否则那个信号就没了。
    """
    md5 = "d0f46ed3ce8bac33df5f7055656d8bd9"
    assert messages.sticker_display(md5, None) == "[贴纸#d0f46ed3]"
    assert messages.sticker_display(md5, "") == "[贴纸#d0f46ed3]"


def test_sticker_display_truncates_the_name_to_60():
    """名字是**载荷可控**的：`_sticker_name` 的兜底支路扫整个解码串、返回无界的中文串，
    不截就原样进 `messages.content`（→ trigram FTS → MCP 信封）。60 与 `appmsg_hint` 同口径。

    **断言的是长度本身**（60 与 61 的边界都钉住），不是「看着不长」——后者把切片删掉也绿。
    短 ID 支路不受影响（8 位十六进制），下面的往返一并确认截断后的串仍能被解析端认回来。
    """
    md5 = "d0f46ed3ce8bac33df5f7055656d8bd9"
    assert messages.sticker_display(md5, "很" * 60) == "[贴纸:" + "很" * 60 + "]"
    over = messages.sticker_display(md5, "很" * 200)
    assert len(over) == len("[贴纸:") + 60 + 1        # 前缀 + 60 字 + "]"
    assert over == "[贴纸:" + "很" * 60 + "]"
    assert messages.sticker_name_of(over) == "很" * 60
    assert messages.sticker_display(md5, None) == "[贴纸#d0f46ed3]"   # 短 ID 支路不受截断影响


def test_sticker_display_short_id_is_stable_and_distinct():
    a = "d0f46ed3" + "0" * 24
    b = "d0f46ed3" + "f" * 24          # 前 8 位相同 → 短 ID 相同（可接受：实测 6,123 个贴纸）
    c = "1" * 32
    assert messages.sticker_display(a, None) == messages.sticker_display(b, None)
    assert messages.sticker_display(a, None) != messages.sticker_display(c, None)


def test_sticker_display_roundtrip_parsers():
    """生产端与解析端必须互认——本模块存在的理由就是防这对漂移。"""
    assert messages.is_sticker_display("[贴纸:滑稽]")
    assert messages.is_sticker_display("[贴纸#d0f46ed3]")
    assert not messages.is_sticker_display("[sticker]")
    assert messages.sticker_name_of("[贴纸:滑稽]") == "滑稽"
    assert messages.sticker_name_of("[贴纸#d0f46ed3]") is None
