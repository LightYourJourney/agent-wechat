"""检索层：FTS5 trigram（≥3 字精确子串）+ LIKE 兜底（1-2 字）。向量检索为后续增强。"""
from __future__ import annotations

import sqlite3

from . import errors, messages, store
from .errors import SearchInputError

# 命中行的列清单（fts / like 两条路径共用；两处 SQL 曾各抄一份）
_HIT_COLUMNS = (
    messages.column_list("id", "msg_key", "chat_id", "shard", "local_id", "ts",
                         "msg_type", "sender_id", prefix="m.")
    + f", {store._SENDER_EXPR} AS sender_name, "
    + messages.column_list("is_self", "content", "media_hint", prefix="m."))

_RECENT_COLUMNS = (messages.column_list(*messages.ROW_COLUMNS, prefix="m.")
                   .replace("m.sender_name", store._SENDER_EXPR + " AS sender_name"))


def _fts_safe(q: str) -> str:
    """FTS5 字符串转义：包成双引号短语（trigram 支持子串短语匹配）。"""
    return '"' + q.replace('"', '""') + '"'


def search(query: str, chat_id: str | None = None, limit: int = 20,
           account: str | None = None) -> dict:
    """返回 {"hits": [...], "mode": "fts"|"like", "total": n}。

    account 必传：检索严格限定该账号的数据（多账号索引隔离）。
    """
    if not account:
        raise SearchInputError("search 需要显式 account（多账号索引隔离）")
    if not query.strip():
        # EA-073：空/全空白 query 曾退化成 LIKE '%%'（全表匹配）——在入口直接拒绝，
        # 别让一次手滑变成全表扫。
        raise SearchInputError("检索串不能为空（或全空白）")
    where, args = ["m.account = ?"], [account]
    if chat_id:
        where.append("m.chat_id = ?")
        args.append(chat_id)
    w = " AND ".join(where)

    notes: list[dict] = []
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
        except sqlite3.OperationalError as exc:
            # EA-013：只吞「**确认可降级**」的那一种，且把降级说出来（mode 不再是 fts）。
            # 此前吞掉**全部** OperationalError：索引损坏与将来的 SQL 拼装错误都静默成
            # 「0 命中，mode=fts」——真故障看起来和「没搜到」一模一样。
            msg = str(exc)
            if "no such table" in msg or "no such column" in msg:
                rows = []
                mode = "like"            # 降级要说出来：不再假报 mode=fts
                notes.append({"capability": "search_fts",
                              "effect": f"索引库结构不完整（{msg}）——已退回 LIKE 兜底；"
                                        "删除 index.db 后重跑一次导出即可重建"})
            else:
                raise errors.SearchInputError(   # 编程/输入错误的原样重抛（响亮）
                    f"检索失败：{errors.redact(msg)}") from exc

        if not rows and len(query.strip()) <= 2:
            # trigram 无法匹配 <3 字符 → LIKE 兜底（有 chat 过滤则快，无过滤为全表扫）
            mode = "like"
            q = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{q}%"
            sql = (f"SELECT {_HIT_COLUMNS}, "
                   f"NULL AS snip FROM messages m {store._SENDER_JOIN} "
                   f"WHERE m.content LIKE ? ESCAPE '\\' AND {w} "
                   f"ORDER BY m.ts DESC LIMIT ?")
            args2 = [like, *args, limit]
            rows = con.execute(sql, args2).fetchall()

    hits = [dict(r) for r in rows]
    return {"hits": hits, "mode": mode, "total": len(hits), "degraded": notes}


def messages_in_range(chat_id: str, *, account: str,
                      after_ts: int | None = None, before_ts: int | None = None,
                      limit: int = 500) -> tuple[list[dict], bool]:
    """范围内**最近的** limit 条（时间正序返回），以及 truncated。

    `truncated` 是「**可能**还有更早的没进来」——`len(rows) == limit` 只在一个方向成立：
    范围内**恰好** `limit` 条时它同样是 `True`（**假阳性**，其实一条都没被丢）。
    取的是最近的一端，不是最早的一端，
    取向写反会让产物看起来「从很久以前开始」，而用户以为自己拿到了最近的消息（spec §4.4）。
    `after_ts` 含下界、`before_ts` 不含上界（与 `search` / `media.month_bounds` 的 [lo, hi) 一致）。

    本函数**不做区间校验**：调用方负责限幅（`usecases.clamp_limit`）。
    `limit` 直通 SQLite 的 `LIMIT`，所以 `limit=-1` 的语义是**不限量**（不是「一条都不要」），
    此时 `truncated` 仍按 `len(rows) == limit` 算，会是 `False` —— 那是「真的都进来了」而不是错，
    但调用方不该依赖这个巧合。
    """
    where, args = ["m.account = ?", "m.chat_id = ?"], [account, chat_id]
    if after_ts is not None:
        where.append("m.ts >= ?")
        args.append(after_ts)
    if before_ts is not None:
        where.append("m.ts < ?")
        args.append(before_ts)
    with store.open() as con:
        rows = con.execute(
            f"SELECT {_RECENT_COLUMNS} FROM messages m {store._SENDER_JOIN} "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY m.ts DESC, m.local_id DESC LIMIT ?",
            (*args, limit)).fetchall()
    return [dict(r) for r in reversed(rows)], len(rows) == limit  # 时间正序
