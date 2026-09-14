"""会话显示名：把消息里出现的 username 映射成人能读的名字。

三条约定：
- **本模块不 import 任何项目模块**——纯函数，不碰文件系统、不碰数据库。
  IO 在 `reader.MessageReader.roster()`，编排在 `usecases`。
- **`parse_members` 对任何字节都不抛异常**：输入是数据库里的 BLOB，畸形/截断是现实输入，
  一个群的 `ext_buffer` 坏掉不该让整次查询挂掉（实测畸形输入见 tests/test_roster.py）。
- 显示名的优先级规则**只在这里一份**：群昵称 → 备注 → 昵称 → username。
"""
from __future__ import annotations

# chat_room.ext_buffer 的 protobuf 字段号。无 .proto，由实测解出（spec §2.2）：
#   f1 (repeated) 成员
#       f1  username
#       f2  群昵称    ← 可选，没设群名片的人没有这个字段
#       f3, f4       成员级杂项，本模块不关心
#   f3, f4 群级字段（实测皆为 10001）
_MEMBER = 1
_MEMBER_KEY = 1
_MEMBER_NICK = 2

RosterRow = tuple[str, str, str]        # (chat_id, member_key, display_name)


def _varint(buf: bytes, i: int) -> tuple[int, int] | None:
    """读一个 varint，返回 `(值, 新下标)`；截断或过长（> 63 位）返回 None。"""
    value = shift = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7
        if shift > 63:
            return None
    return None


def _fields(buf: bytes) -> list[tuple[int, int, bytes | int]]:
    """遍历 wire format，返回 `[(字段号, wire type, 值)]`。

    **遇到任何畸形就停下，返回已解出的部分**——这是本模块最重要的行为，
    调用方（`parse_members`）靠它实现「名册坏掉只降级不失败」。
    """
    out: list[tuple[int, int, bytes | int]] = []
    i = 0
    while i < len(buf):
        got = _varint(buf, i)
        if got is None:
            return out
        key, i = got
        field, wt = key >> 3, key & 7
        if wt == 0:
            got = _varint(buf, i)
            if got is None:
                return out
            value, i = got
        elif wt == 2:
            got = _varint(buf, i)
            if got is None:
                return out
            n, i = got
            if i + n > len(buf):
                return out                      # 声明的长度超过剩余字节：截断
            value = buf[i:i + n]
            i += n
        elif wt == 5:
            if i + 4 > len(buf):
                return out
            value, i = buf[i:i + 4], i + 4
        elif wt == 1:
            if i + 8 > len(buf):
                return out
            value, i = buf[i:i + 8], i + 8
        else:
            return out                          # wire type 3/4/6/7：不支持，停
        out.append((field, wt, value))
    return out


def _text(raw: bytes | int) -> str | None:
    """length-delimited 字段当 UTF-8 解；不是 bytes 或解不出就 None。"""
    if not isinstance(raw, bytes):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def parse_members(blob: bytes | None) -> list[tuple[str, str | None]]:
    """`ext_buffer` → `[(username, 群昵称 | None), ...]`。

    畸形/截断返回**已解出的部分**，绝不抛——见模块 docstring 第二条。
    """
    if not blob:
        return []
    out: list[tuple[str, str | None]] = []
    for field, wt, raw in _fields(blob):
        if field != _MEMBER or wt != 2 or not isinstance(raw, bytes):
            continue
        key = nick = None
        for f2, wt2, raw2 in _fields(raw):
            if wt2 != 2:
                continue
            if f2 == _MEMBER_KEY:
                key = _text(raw2)
            elif f2 == _MEMBER_NICK:
                nick = _text(raw2)
        if key:
            out.append((key, nick))
    return out


def _display(group_nick: str | None, contact: tuple[str, str] | None, key: str) -> str:
    """群昵称 → 备注 → 昵称 → username。**全项目唯一一份这条规则。**"""
    remark, nick = contact or ("", "")
    return group_nick or remark or nick or key


def build_rows(groups: dict[str, list[tuple[str, str | None]]],
               contacts: dict[str, tuple[str, str]]) -> list[RosterRow]:
    """算出全部 `display_names` 行：群成员 + 私聊。

    `groups`: `{chat_id: parse_members(ext_buffer) 的结果}`
    `contacts`: `{username: (remark, nick_name)}`

    私聊行是 `(chat_id=对方, member_key=对方)`——同一条 join 因此覆盖两种会话。
    **自己发的私聊消息 join 不上**（`sender_id` 是自己 ≠ `chat_id`），那一处由
    `mcp_server` 的 `is_self` 判断解决，不靠填表（spec §6）。
    """
    out: list[RosterRow] = []
    for chat_id, members in groups.items():
        for key, group_nick in members:
            if key:
                out.append((chat_id, key, _display(group_nick, contacts.get(key), key)))
    for username, (remark, nick) in contacts.items():
        if username:
            out.append((username, username, _display(None, (remark, nick), username)))
    return out
