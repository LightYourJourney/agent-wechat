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
