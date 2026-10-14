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


def _is_structural(msg: str) -> bool:
    """sqlite 报的是「索引库结构不完整」（可降级），还是别的失败（要翻译/重抛）。"""
    return "no such table" in msg or "no such column" in msg


def _structural_degrade(notes: list[dict], msg: str) -> None:
    """结构不完整 → 追加降级说明；同一能力只记一条（FTS 支路记过之后 LIKE 又撞同一堵墙）。"""
    if any(n["capability"] == "search_fts" for n in notes):
        return
    notes.append({"capability": "search_fts",
                  "effect": f"索引库结构不完整（{msg}）——已退回 LIKE 兜底；"
                            "删除 index.db 后重跑一次导出即可重建"})


def _runtime_failure(msg: str) -> errors.IndexUnavailable:
    """非结构类的 sqlite 失败 = **库侧**运行时问题（写锁/损坏/内存）。

    F6（T6 复审）：此前一律塞进 `SearchInputError`（语义是入参不合法），
    「等写者结束再试 / 重建库」这条可执行动作到不了用户。
    """
    return errors.IndexUnavailable(
        f"检索失败：{errors.redact(msg)}",
        hint="索引库可能被占用或损坏：等导出/构建结束后重试；持续失败则删除 index.db 后重跑一次导出")


def search(query: str, chat_id: str | None = None, limit: int = 20,
           account: str | None = None) -> dict:
    """返回 {"hits": [...], "mode": "fts"|"like", "total": n, "degraded": [...]}。

    account 必传：检索严格限定该账号的数据（多账号索引隔离）。
    """
    if not account:
        raise SearchInputError("search 需要显式 account（多账号索引隔离）")
    if not query.strip():
        # EA-073：空/全空白 query 曾退化成 LIKE '%%'（全表匹配）——在入口直接拒绝，
        # 别让一次手滑变成全表扫。
        raise SearchInputError("检索串不能为空（或全空白）")
    if "\x00" in query:
        # F6：NUL 是**入参**问题（FTS 的 MATCH 会拒它）——按入参拒，不混进「库侧失败」。
        raise SearchInputError("检索串不能包含 NUL 字符")
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
            if _is_structural(msg):
                rows = []
                mode = "like"            # 降级要说出来：不再假报 mode=fts
                _structural_degrade(notes, msg)
            else:
                raise _runtime_failure(msg) from exc

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
            try:
                rows = con.execute(sql, args2).fetchall()
            except sqlite3.OperationalError as exc:
                # F2（T6 复审）：这条 execute 此前在守卫**之外**——裸 sqlite3 穿出 core 层。
                # 两条 SQL 共用同一组列清单（_HIT_COLUMNS）：FTS 结构不完整时这里必然也撞
                # 同一堵墙，按同一套降级收口（空结果 + 说明），非结构类仍翻成项目异常。
                msg = str(exc)
                if _is_structural(msg):
                    rows = []
                    _structural_degrade(notes, msg)
                else:
                    raise _runtime_failure(msg) from exc

        if not rows and mode == "fts":
            # F1（T6 复审）：`connect()` 的 `CREATE VIRTUAL TABLE IF NOT EXISTS` 会把**被删掉的**
            # messages_fts 静默重建为**空索引**（不回填）——此后 MATCH 恒 0 行，与「真的没搜到」
            # 逐字节相同。两个 O(1) 探针把这两种「空」分开：影子表 `messages_fts_docsize` 有行
            # ⟺ 索引里有行（直接扫 messages_fts 不行——外部内容表会去读内容表，空索引照样"有"行）。
            if (con.execute("SELECT 1 FROM messages_fts_docsize LIMIT 1").fetchone() is None
                    and con.execute("SELECT 1 FROM messages LIMIT 1").fetchone() is not None):
                notes.append({"capability": "search_fts",
                              # 措辞**中立**：这条 effect 随 MCP 信封出门，写死 CLI 的命令名就是
                              # 让 agent 读到一个它敲不了的东西（EA-096 的裁定：core 中立 +
                              # 入口补后缀——同一批已把 4 处同类文案改成「重跑一次导出即可重建」）。
                              "effect": "FTS 索引失同步：索引里一行都没有而消息表非空"
                                        "（搜索会漏掉全部结果）——重跑一次全量导出即可重建"})

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
