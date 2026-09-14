"""测试夹具：一行 messages（经 messages.make_row 构造，与行契约同形）。

此前 tests/test_vector.py 与 tests/test_reader_export_pipeline.py 各有一份 mk_row，
字段默认值还不一致（sender_id 一份是 chat、一份是 "wxid_a"）。现收敛到本处：
参数序以调用点更多的 test_vector 那版为准 (acct, chat, lid, content, *)，
原 reader 那版的第 3 个位置参数 shard 改为关键字参数。
"""
import hashlib

from agent_wechat import messages

T0 = 1700000000


def mk_row(acct, chat, lid, content, *, shard="message_0.db", ts=T0, msg_type=1,
           is_self=0, sender_id=None, sender_name=None, media_hint=None):
    return messages.make_row(
        account=acct, chat_md5=hashlib.md5(chat.encode()).hexdigest(), chat_id=chat,
        shard=shard, local_id=lid, ts=ts, msg_type=msg_type, sender_id=sender_id,
        sender_name=sender_name, is_self=is_self, content=content,
        media_hint=media_hint)


# ---------- chat_room.ext_buffer 的 protobuf 构造 ----------
# 字段号来自实测（spec §2.2）：f1 repeated = 成员；成员内 f1 = username，f2 = 群昵称（可选）。

def varint(n: int) -> bytes:
    """protobuf varint 编码。名字短不代表协议短——长度字段本身也是 varint。"""
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def ld(field: int, payload: bytes) -> bytes:
    """length-delimited 字段（wire type 2）。"""
    return varint(field << 3 | 2) + varint(len(payload)) + payload


def member(username: str, nick: str | None = None) -> bytes:
    """ext_buffer 里的一个成员条目。nick 为 None 表示没设群名片（真实数据里很常见）。"""
    inner = ld(1, username.encode())
    if nick is not None:
        inner += ld(2, nick.encode())
    return ld(1, inner)


def ext_buffer(*members: bytes) -> bytes:
    """把一个群的成员拼成 ext_buffer，末尾附上实测见到的两个群级字段（f3/f4）。"""
    return b"".join(members) + varint(3 << 3 | 0) + varint(10001) \
        + varint(4 << 3 | 0) + varint(10001)
