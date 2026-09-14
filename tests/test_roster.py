"""roster 的纯逻辑测试。

本模块是全项目唯一解析「来自数据库的不可信字节」的地方（`chat_room.ext_buffer`），
所以容错用例是这里的重点：畸形输入必须降级成「这个人显示 wxid」，而不是抛异常。
"""
import pytest

from agent_wechat import roster

from helpers import ext_buffer, ld, member, varint


# ---------- parse_members ----------

def test_parse_members_reads_username_and_optional_nick():
    """f2（群昵称）是**可选**的：实测同一个群里有人有、有人没有。"""
    blob = ext_buffer(member("wxid_a", "张三"), member("wxid_b"))
    assert roster.parse_members(blob) == [("wxid_a", "张三"), ("wxid_b", None)]


def test_parse_members_ignores_group_level_fields():
    """f3/f4 是群级字段（实测皆为 10001），不是成员——不能被当成成员收进来。"""
    blob = ext_buffer(member("wxid_a", "张三"))
    assert roster.parse_members(blob) == [("wxid_a", "张三")]


def test_parse_members_handles_username_longer_than_127_bytes():
    """长度字段本身是 varint：名字短不代表协议短，>127 字节必须仍能读对。"""
    long_name = "wxid_" + "x" * 200
    assert roster.parse_members(ext_buffer(member(long_name, "长"))) == [(long_name, "长")]


def test_parse_members_handles_nickname_longer_than_127_bytes():
    long_nick = "名" * 100          # UTF-8 下 300 字节
    assert roster.parse_members(ext_buffer(member("wxid_a", long_nick))) == [("wxid_a", long_nick)]


def test_parse_members_returns_partial_on_truncated_blob():
    """截断返回**已解出的部分**，不抛——一个群的名册坏掉不该让整次查询挂掉。"""
    blob = ext_buffer(member("wxid_a", "张三"), member("wxid_b", "李四"))
    # 切 9 字节 = 先跨过末尾的 f3/f4 群级字段（varint(10001) 各 2 字节，加字段号共 6 字节），
    # 再切进第二个成员条目。只切 3 字节的话两名成员都完好，测不到「截断」。
    assert roster.parse_members(blob[:-9]) == [("wxid_a", "张三")]


@pytest.mark.parametrize("blob", [
    b"",
    None,
    b"\xff\xff\xff\xff",              # 字段号 varint 永远读不完
    b"\x0a",                          # 声明是 length-delimited，但长度字节都没有
    b"\x0a\xff",                      # 长度 varint 截断
    b"\x0a\x50short",                 # 声明的长度超过剩余字节
    b"\x0b" + b"\x00" * 8,            # wire type 3（已废弃的 start group）
    b"\x08" + b"\xff" * 20,           # varint 过长（> 63 位）
])
def test_parse_members_never_raises_on_garbage(blob):
    """**固定样本**而不是 os.urandom——测试必须确定性，随机字节有时能解析成功。"""
    got = roster.parse_members(blob)
    assert isinstance(got, list)
    assert all(isinstance(k, str) for k, _ in got)


def test_parse_members_scales_to_a_500_member_group():
    """实测最大的群是 500 人 / 30,917 字节。"""
    blob = ext_buffer(*[member(f"wxid_{i:04d}", f"名字{i}") for i in range(500)])
    got = roster.parse_members(blob)
    assert len(got) == 500
    assert got[0] == ("wxid_0000", "名字0")
    assert got[-1] == ("wxid_0499", "名字499")


def test_parse_members_skips_entries_without_username():
    """成员条目里没有 f1（username）就跳过——不能产出一个空键的行。"""
    assert roster.parse_members(ext_buffer(ld(1, ld(2, "只有昵称".encode())))) == []


# ---------- build_rows ----------

CONTACTS = {
    "wxid_a": ("张三", "Darius"),
    "wxid_b": ("", "藤壶岛"),
    "wxid_c": ("", ""),
}


def test_build_rows_prefers_group_nickname_over_everything():
    rows = roster.build_rows({"888@chatroom": [("wxid_a", "群里的名字")]}, CONTACTS)
    assert ("888@chatroom", "wxid_a", "群里的名字") in rows


def test_build_rows_falls_back_in_order():
    """群昵称 → 备注 → 昵称 → username。与 reader.contacts() 的 `remark or nick or u` 同序。"""
    rows = dict(((c, k), d) for c, k, d in roster.build_rows(
        {"888@chatroom": [("wxid_b", None), ("wxid_c", None), ("wxid_unknown", None)]},
        CONTACTS))
    assert rows[("888@chatroom", "wxid_b")] == "藤壶岛"           # 无群昵称、无备注 → 昵称
    assert rows[("888@chatroom", "wxid_c")] == "wxid_c"          # 三者皆空 → username
    assert rows[("888@chatroom", "wxid_unknown")] == "wxid_unknown"   # 不在 contact 表里


def test_build_rows_adds_private_chat_rows():
    """私聊行是 (chat_id=对方, member_key=对方)——同一条 join 因此覆盖两种会话。"""
    rows = roster.build_rows({}, CONTACTS)
    assert ("wxid_a", "wxid_a", "张三") in rows
    assert ("wxid_b", "wxid_b", "藤壶岛") in rows


def test_build_rows_private_rows_have_no_group_nickname_layer():
    """同一个人：群里叫群名片，私聊回落备注——私聊没有群昵称那一层。"""
    rows = dict(((c, k), d) for c, k, d in roster.build_rows(
        {"888@chatroom": [("wxid_a", "群里的名字")]}, CONTACTS))
    assert rows[("888@chatroom", "wxid_a")] == "群里的名字"
    assert rows[("wxid_a", "wxid_a")] == "张三"


def test_build_rows_skips_empty_username():
    """空键的行永远 join 不上，还会污染名册——不产出。"""
    rows = roster.build_rows({"888@chatroom": [("", "无主")]}, {"": ("x", "y")})
    assert all(k for _, k, _ in rows)
