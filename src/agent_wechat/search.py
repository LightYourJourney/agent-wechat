"""检索层：FTS5 trigram（≥3 字精确子串）+ LIKE 兜底（1-2 字）。向量检索为后续增强。"""
from __future__ import annotations

import sqlite3

from . import store


def _fts_safe(q: str) -> str:
    """FTS5 字符串转义：包成双引号短语（trigram 支持子串短语匹配）。"""
    return '"' + q.replace('"', '""') + '"'


def search(query: str, chat_id: str | None = None, limit: int = 20,
           after_ts: int | None = None, before_ts: int | None = None,
           account: str | None = None) -> dict:
    """返回 {"hits": [...], "mode": "fts"|"like", "total": n}。

    account 必传：检索严格限定该账号的数据（多账号索引隔离）。
    """
    con = store.connect()
    con.row_factory = sqlite3.Row
    if not account:
        raise ValueError("search 需要显式 account（多账号索引隔离）")
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

    mode = "fts"
    try:
        sql = (f"SELECT m.id, m.chat_id, m.shard, m.local_id, m.ts, m.msg_type, "
               f"m.sender_id, m.sender_name, m.is_self, m.content, m.media_hint, "
               f"snippet(messages_fts, 0, '【', '】', '…', 12) AS snip "
               f"FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
               f"WHERE messages_fts MATCH ? AND {w} "
               f"ORDER BY m.ts DESC LIMIT ?")
        rows = con.execute(sql, [_fts_safe(query), *args, limit]).fetchall()
    except sqlite3.OperationalError:
        rows = []

    if not rows and len(query.strip()) <= 2:
        # trigram 无法匹配 <3 字符 → LIKE 兜底（有 chat 过滤则快，无过滤为全表扫）
        mode = "like"
        like = f"%{query.strip()}%"
        sql = (f"SELECT m.id, m.chat_id, m.shard, m.local_id, m.ts, m.msg_type, "
               f"m.sender_id, m.sender_name, m.is_self, m.content, m.media_hint, "
               f"NULL AS snip FROM messages m WHERE m.content LIKE ? AND {w} "
               f"ORDER BY m.ts DESC LIMIT ?")
        args2 = [like, *args, limit]
        rows = con.execute(sql, args2).fetchall()

    hits = [dict(r) for r in rows]
    con.close()
    return {"hits": hits, "mode": mode, "total": len(hits)}


def recent_from_store(chat_id: str, limit: int = 50) -> list[dict]:
    """本地库读取近期消息（毫秒级；需先导出）。"""
    con = store.connect()
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM messages WHERE chat_id=? ORDER BY ts DESC, local_id DESC LIMIT ?",
        (chat_id, limit)).fetchall()
    con.close()
    return [dict(r) for r in reversed(rows)]  # 时间正序
