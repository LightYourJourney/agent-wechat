"""微信库结构的声明清单：每个能力必需哪些表、哪些列。

**本模块不 import 任何项目模块**——它是纯数据，与 reader 的实现解耦。

为什么单独一个文件而不是塞进 reader.py：这张表是**关于微信的知识**，它的对照物是
`docs/schema-4.1.13.md` 与真机实测；微信升级时要改的是它，而不是 reader 的逻辑。
放在一起会让「哪部分随微信升级而变」变得不明显。

声明粒度是**列**而不是表（spec §3）：表在但少一列（微信加/改列比改表名常见）时，
表级探测会放行、SQL 照样崩。实测列级探测单表 0.017 ms，多花的成本可忽略。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Requirement:
    """一个能力对一个库的一项要求。

    `db` 是**相对路径**（与 reader._open(rel) 同形）。`table` 可以是通配（`Msg_*`）——
    那种表的真名是 md5、运行时才知道，探测时按「该库第一个真实存在的匹配表」抽样。
    `columns` 为空元组表示只要求表存在；本清单里没有这种声明（见 tests）。
    """
    db: str
    table: str
    columns: tuple[str, ...]


# 四个探测能力。注意它们与**用户可见的能力**不是一一对应（spec §6）：
#   contacts 命令 → contacts；recent → messages + contacts；export → messages + username_pool
#   search/semantic → contacts（索引库不受漂移影响）；inspect → 不探测
CAPABILITIES: dict[str, tuple[Requirement, ...]] = {
    "contacts": (
        Requirement("contact/contact.db", "contact",
                    ("username", "alias", "remark", "nick_name", "delete_flag")),
    ),
    "roster": (
        Requirement("contact/contact.db", "chat_room", ("id", "ext_buffer")),
        Requirement("contact/contact.db", "name2id", ("username",)),
        Requirement("contact/contact.db", "contact",
                    ("username", "remark", "nick_name", "delete_flag")),
    ),
    "messages": (
        Requirement("message/*.db", "Name2Id", ("user_name",)),
        Requirement("message/*.db", "Msg_*",
                    ("local_id", "local_type", "real_sender_id", "create_time",
                     "message_content", "WCDB_CT_message_content", "packed_info_data",
                     "server_id")),
    ),
    "username_pool": (
        Requirement("session/session.db", "SessionTable", ("username",)),
        Requirement("contact/contact.db", "name2id", ("username",)),
        Requirement("message/*.db", "Name2Id", ("user_name",)),
    ),
}


@dataclass(frozen=True)
class ProbeResult:
    """探测结果。`ok=False` 且 `missing_columns` 为空 = 表不存在。"""
    ok: bool
    missing_columns: tuple[str, ...] = ()


def _text(v):
    """PRAGMA 的返回值走连接的 `text_factory`：reader 的库连接统一是 bytes
    （库内 TEXT 含非 UTF-8 字节，docs/schema-4.1.13.md:71）。列名是文本，归一化后再比。"""
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else v


def probe(con, req: Requirement, table: str | None = None) -> ProbeResult:
    """该连接下的库是否满足这条声明。

    `table` 用于通配声明（`Msg_*`）：真名由调用方从 sqlite_master 拿到后传进来。

    探针本身**不会失败**（spec §2.2 实测）：表名按**带引号的标识符**插入（内嵌的 `"`
    翻倍转义），所以不存在的表返回空 list、不抛异常，含引号的表名同样返回空、不抛，
    也注入不了。这也是不裸插表名的原因——裸插时 `t' OR 1=1 --` 会抛
    `unrecognized token`。因此这里不需要 try/except——「探不出来」这个状态不存在，
    存在的只有「探出来是空的」。

    **只报缺的列，不报多的列**：微信新增列不是漂移——我们不读它就不会崩。
    """
    name = (table or req.table).replace('"', '""')
    actual = {_text(row[1]) for row in con.execute(f'PRAGMA table_info("{name}")')}
    if not actual:
        return ProbeResult(ok=False)                       # 表不存在
    missing = tuple(c for c in req.columns if c not in actual)
    return ProbeResult(ok=False, missing_columns=missing) if missing else ProbeResult(ok=True)
