"""messages 行契约：本地 index.db 的 messages 一行——列清单、构造器、两条字符串哨兵。

与 docs/schema-4.1.13.md 的分工：那份讲**微信侧**表结构（Msg_<md5> / Name2Id /
WCDB 压缩），本模块讲**本地** messages 行。此前「一行 messages」在生产端
（reader 手写 dict）、消费端（store 的 INSERT）、四处 SELECT 列清单、两份测试夹具里
各编码一份；两条跨模块字符串约定（`md5:` 退化会话 id、`[appmsg: ...]` 媒体提示）
的生产与解析也分居两处。现在它们都以本模块为唯一出处。

依赖约束：**不 import 任何项目模块**。reader 已 import store，而 store/search/vector
都 import 本模块——反向依赖会立即成环。
"""
from __future__ import annotations

import hashlib
from typing import TypedDict

# ---------- 列清单 ----------

# messages 表的全部列，与 store.SCHEMA 的 CREATE TABLE messages 逐列同序一致
# （tests/test_messages.py 防漂移）。SELECT 清单从它派生。
COLUMNS: tuple[str, ...] = (
    "id", "msg_key", "account", "chat_md5", "chat_id", "shard", "local_id",
    "server_id", "ts", "msg_type", "sender_id", "sender_name", "is_self",
    "content", "media_hint", "exported_at",
)

# 行契约的字段：写入侧 reader 产出的 14 键。id 由库分配、exported_at 由 store 记账，
# 两者都不属于「一行 messages」本身。
ROW_COLUMNS: tuple[str, ...] = (
    "msg_key", "account", "chat_md5", "chat_id", "shard", "local_id", "server_id",
    "ts", "msg_type", "sender_id", "sender_name", "is_self", "content", "media_hint",
)


def column_list(*names: str, prefix: str = "") -> str:
    """把列名渲染成 SELECT 清单。列名必须属于 COLUMNS——拼错即 import 期报错，
    而不是等到查询时才抛 OperationalError。"""
    unknown = [n for n in names if n not in COLUMNS]
    if unknown:
        raise ValueError(f"messages 表没有这些列: {unknown}")
    return ", ".join(f"{prefix}{n}" for n in names)


# ---------- 类型词汇表 ----------

# msg_type = local_type & 0xffffffff（掩码由 reader 在解码时完成）。
TYPE_TEXT = 1
TYPE_IMAGE = 3
TYPE_VOICE = 34
TYPE_VIDEO = 43
TYPE_STICKER = 47
TYPE_LOCATION = 48
TYPE_APPMSG = 49
TYPE_STATUS = 51
TYPE_SYSTEM = 10000
TYPE_REVOKE = 10002

# 只收录「有展示名」的类型。TYPE_APPMSG 刻意不入表：reader._type_name(49) 的兜底
# 文案 "[type49]" 已冻结在 index.db 内容与 MCP 返回里，加进来会把它变成 "[appmsg]"。
TYPE_NAMES: dict[int, str] = {
    TYPE_TEXT: "text", TYPE_IMAGE: "image", TYPE_VOICE: "voice", TYPE_VIDEO: "video",
    TYPE_STICKER: "sticker", TYPE_LOCATION: "location", TYPE_STATUS: "status",
    TYPE_SYSTEM: "system", TYPE_REVOKE: "revoke",
}


# ---------- 哨兵一：md5:<hex> 退化会话 id ----------

MD5_PREFIX = "md5:"


def md5_of(chat_id: str) -> str:
    """会话 id → Msg_<md5> 表的 md5 部分。"""
    return hashlib.md5(chat_id.encode()).hexdigest()


def degraded_chat_id(chat_md5: str) -> str:
    """生产端：会话名无法从 md5 反查时的退化 id（内容即 md5 本身）。"""
    return f"{MD5_PREFIX}{chat_md5}"


def chat_md5(chat_id: str) -> str:
    """解析端：会话 id → md5。退化 id 直接取前缀之后，其余按 md5(chat_id) 计算。

    两侧共用本函数，防止「生产写 md5:<hex>、解析又当普通 id 再 md5 一次」的漂移。
    """
    return chat_id[len(MD5_PREFIX):] if chat_id.startswith(MD5_PREFIX) else md5_of(chat_id)


# ---------- 哨兵二：[appmsg: 标题] 媒体提示 ----------

APPMSG_PREFIX = "[appmsg: "
APPMSG_NO_TITLE = "[appmsg]"


def appmsg_hint(title: str | None) -> str:
    """生产端：appmsg 的媒体提示。无标题产出 "[appmsg]"（截 60 字）。"""
    return f"{APPMSG_PREFIX}{title[:60]}]" if title else APPMSG_NO_TITLE


def has_appmsg_title(text: str) -> bool:
    """解析端：是否为**带标题**的 appmsg 提示——只有它值得嵌入。"""
    return text.startswith(APPMSG_PREFIX) and text != APPMSG_NO_TITLE


# ---------- 行 ----------

def row_key(account: str, chat_md5: str, shard: str, local_id: int) -> str:
    """去重主键：四字段缺一不可（local_id 在会话内、分片内均会重复）。"""
    return f"{account}|{chat_md5}|{shard}|{local_id}"


class MessageRow(TypedDict):
    """messages 表一行（写入侧形状；运行时就是普通 dict）。"""

    msg_key: str
    account: str
    chat_md5: str
    chat_id: str
    shard: str
    local_id: int
    server_id: int | None
    ts: int
    msg_type: int
    sender_id: str | None
    sender_name: str | None
    is_self: int
    content: str | None
    media_hint: str | None


def make_row(*, account: str, chat_md5: str, chat_id: str, shard: str, local_id: int,
             ts: int, msg_type: int, sender_id: str | None, sender_name: str | None,
             is_self: int, content: str | None, media_hint: str | None,
             server_id: int | None = None) -> MessageRow:
    """构造一行 messages：键集合恒等于 COLUMNS，msg_key 由 row_key 派生。"""
    return MessageRow(
        msg_key=row_key(account, chat_md5, shard, local_id),
        account=account, chat_md5=chat_md5, chat_id=chat_id, shard=shard,
        local_id=local_id, server_id=server_id, ts=ts, msg_type=msg_type,
        sender_id=sender_id, sender_name=sender_name, is_self=is_self,
        content=content, media_hint=media_hint)
