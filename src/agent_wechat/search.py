"""检索层：FTS5 trigram（≥3 字精确子串）+ LIKE 兜底（1-2 字）。向量检索为后续增强。"""
from __future__ import annotations

import sqlite3

from . import messages, store
from .errors import SearchInputError

# 命中行的列清单（fts / like 两条路径共用；两处 SQL 曾各抄一份）
_HIT_COLUMNS = (
    messages.column_list("id", "chat_id", "shard", "local_id", "ts", "msg_type",
                         "sender_id", prefix="m.")
    + f", {store._SENDER_EXPR} AS sender_name, "
    + messages.column_list("is_self", "content", "media_hint", prefix="m."))

_RECENT_COLUMNS = (messages.column_list(*messages.ROW_COLUMNS, prefix="m.")
                   .replace("m.sender_name", store._SENDER_EXPR + " AS sender_name"))


def _fts_safe(q: str) -> str:
    """FTS5 字符串转义：包成双引号短语（trigram 支持子串短语匹配）。"""
    return '"' + q.replace('"', '""') + '"'


def search(query: str, chat_id: str | None = None, limit: int = 20,
           after_ts: int | None = None, before_ts: int | None = None,
           account: str | None = None) -> dict:
    """返回 {"hits": [...], "mode": "fts"|"like", "total": n}。

    account 必传：检索严格限定该账号的数据（多账号索引隔离）。
    """
    if not account:
        raise SearchInputError("search 需要显式 account（多账号索引隔离）")
    where, args = ["m.account = ?"], [account]
    if chat_id:
        where.append("m.chat_id = ?")
        args.append(chat_id)
    if after_ts:
        where.append("m.ts >= ?")
        args.append(after_ts)
    if before_ts:
        where.append("m.ts < ?")
        args.append(before_ts)
    w = " AND ".join(where)

    with store.open() as con:
        mode = "fts"
        try:
            sql = (f"SELECT {_HIT_COLUMNS}, "
                   f"snippet(messages_fts, 0, '【', '】', '…', 12) AS snip "
                   f"FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
                   f"{store._SENDER_JOIN} "
                   f"WHERE messages_fts MATCH ? AND {w} "
                   f"ORDER BY m.ts DESC LIMIT ?")
            rows = con.execute(sql, [_fts_safe(query), *args, limit]).fetchall()
        except sqlite3.OperationalError:
            rows = []

        if not rows and len(query.strip()) <= 2:
            # trigram 无法匹配 <3 字符 → LIKE 兜底（有 chat 过滤则快，无过滤为全表扫）
            mode = "like"
            like = f"%{query.strip()}%"
            sql = (f"SELECT {_HIT_COLUMNS}, "
                   f"NULL AS snip FROM messages m {store._SENDER_JOIN} "
                   f"WHERE m.content LIKE ? AND {w} "
                   f"ORDER BY m.ts DESC LIMIT ?")
            args2 = [like, *args, limit]
            rows = con.execute(sql, args2).fetchall()

    hits = [dict(r) for r in rows]
    return {"hits": hits, "mode": mode, "total": len(hits)}


def recent_from_store(chat_id: str, limit: int = 50) -> list[dict]:
    """本地库读取近期消息（毫秒级；需先导出）。"""
    with store.open() as con:
        rows = con.execute(
            f"SELECT {_RECENT_COLUMNS} FROM messages m {store._SENDER_JOIN} "
            "WHERE m.chat_id=? ORDER BY m.ts DESC, m.local_id DESC LIMIT ?",
            (chat_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]  # 时间正序
