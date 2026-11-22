"""检索与长任务用例：关键词/统一检索、上下文、向量构建与任务查询。"""
from __future__ import annotations

from .. import errors, retrieval, search, store, tasks, vector
from ._core import (DEGRADE_EFFECT, LIMIT_SEMANTIC, NeedConfirm, _display_or_id,
                    _refresh_roster, pkg_clamp_limit, pkg_context, to_accounts)

def search_history(query: str, contact: str | None = None, limit: int = 20,
                   account: str | None = None) -> dict | NeedConfirm:
    """hits 的 `sender_name` 来自名册（`search.py:13`），而 `_refresh_roster` 吞掉漂移——
    所以降级说明必须随结果带出去（Ruling 13），否则显示名**静默**回落成 wxid。"""
    ctx = pkg_context(account)
    chat_id = ctx.resolve_optional(contact)
    if isinstance(chat_id, NeedConfirm):
        return chat_id
    _refresh_roster(ctx)
    res = search.search(query, chat_id=chat_id, limit=pkg_clamp_limit(limit),
                        account=ctx.account)
    res["degraded"] = [*res.get("degraded", []), *ctx.degradations()]   # EA-013：合并 FTS 降级说明
    return res


LIMIT_RETRIEVAL = (1, retrieval.MAX_LIMIT)


def search_messages(query: str, *, contact: str | None = None,
                    account: str | None = None,
                    after_ts: int | None = None, before_ts: int | None = None,
                    limit: int = 20, mode: str = "hybrid") -> dict | NeedConfirm:
    """Phase 05 统一检索（关键词 / 语义 / RRF 融合）。返回**片段 + msg_key**。"""
    ctx = pkg_context(account)
    chat_id = ctx.resolve_optional(contact)
    if isinstance(chat_id, NeedConfirm):
        return chat_id
    _refresh_roster(ctx)
    res = retrieval.search_messages(
        query, account=ctx.account, chat_id=chat_id,
        after_ts=after_ts, before_ts=before_ts,
        limit=pkg_clamp_limit(limit, LIMIT_RETRIEVAL), mode=mode)
    res["degraded"] = [*res.get("degraded", []), *ctx.degradations()]
    return res


def message_context(msg_key: str, *, contact: str | None = None,
                    before: int = 2, after: int = 2,
                    account: str | None = None) -> dict | NeedConfirm:
    """Phase 05 第二阶段：按 `msg_key` 取完整原文与同会话上下文。"""
    ctx = pkg_context(account)
    chat_id = ctx.resolve_optional(contact)
    if isinstance(chat_id, NeedConfirm):
        return chat_id
    res = retrieval.get_context(msg_key, account=ctx.account,
                                before=before, after=after)
    if chat_id and res["chat_id"] != chat_id:
        raise errors.DataNotFound(
            "msg_key 不属于该联系人",
            hint="只传 msg_key、省略 contact，或核对两者是否同属一个会话")
    return res


def semantic_search(query: str, contact: str | None = None, limit: int = 10,
                    account: str | None = None) -> dict | NeedConfirm:
    """同 search_history：hits 的 `display` 也由名册富化（Ruling 13）。"""
    ctx = pkg_context(account)
    chat_id = ctx.resolve_optional(contact)
    if isinstance(chat_id, NeedConfirm):
        return chat_id
    _refresh_roster(ctx)
    res = vector.semantic_search(query, chat_id=chat_id,
                                 limit=pkg_clamp_limit(limit, LIMIT_SEMANTIC),
                                 account=ctx.account)
    fell_back = False
    for h in res.get("hits", []):
        try:
            h["display"] = _display_or_id(ctx, h["chat_id"])   # 缓存命中，只解密一次
        except errors.AgentWeChatError:
            # EA-024：富化是**锦上添花**——语义检索的数据源是本地索引 + 本地模型，不需要
            # 微信密钥；富化要读 contact.db，才需要。`purge --keys` 之后、或换机只拷了
            # 索引的机器上，`_display_or_id` 只接 SchemaDrift 会让整个工具 100% 失败
            # （错误文案还把用户引向唯一需要 sudo 的 bootstrap），而兄弟工具
            # `search_history` 同一时刻照常出结果。读不到就不富化：`display` 回落裸
            # chat_id——回落要**自证**（F1，T8 复审）：no-key 时 reader 在解密前就抛了，
            # `ctx.degradations()` 里不会有 roster 那条（条目原注释此处被复验证伪）。
            h["display"] = h["chat_id"]
            fell_back = True
    res["degraded"] = ctx.degradations()
    if fell_back and not any(d["capability"] == "roster" for d in res["degraded"]):
        res["degraded"].append(
            {"capability": "roster", "effect": DEGRADE_EFFECT["roster"]})
    return res


def start_vector_build(accounts: list[str] | None, rebuild: bool) -> str:
    """登记向量索引长任务。accounts=None 表示全部账号（core 复数语义）。

    进度口径：`rows` = 本轮已嵌入行数（vector.build_index 的 progress 回调），
    结束时补 `skipped`（本轮按规则跳过的行数）并用返回值覆盖 `rows` 为终值。
    """
    def _run(tid: str) -> int:
        res = vector.build_index(accounts=accounts, rebuild=rebuild,
                                 progress=lambda n: tasks.update(tid, rows=n))
        tasks.update(tid, skipped=res["skipped_this_run"])
        return res["embedded_this_run"]

    # 任务元数据沿用单数 account 字段（tasks.json 已发布格式）：单账号写该账号、
    # 全部账号写 None。多账号列表不是 wire 层能产生的形态（本层只翻译出这两种）。
    single_account = accounts[0] if accounts and len(accounts) == 1 else None
    return tasks.start("vector", _run, account=single_account, rebuild=rebuild)


def build_vector_index(account: str | None = None, rebuild: bool = False) -> dict:
    """注意 account 语义与其它工具相反：省略表示索引**全部**账号（见 TOOLS.md §7）。

    翻译边界：wire 层的 `account`（单数，None = 全部）经 `to_accounts` 翻成 core 的
    `accounts`（复数列表，None = 全部，[] = 一个都不索引）。wire 语义一字不动。

    **经包查 `start_vector_build`**（而不是用本模块的局部名）：它是这一层对外的接缝，
    测试与将来的替换都打在 `usecases.start_vector_build` 上；直接引用局部名会让那次
    替换对这里无效——拆包前两者是同一个名字，拆包后不是了。
    """
    from . import start_vector_build  # 包级查名，见 docstring
    return {"task_id": start_vector_build(to_accounts(account), rebuild),
            "status": "running"}


def task_status(task_id: str | None = None) -> dict | list:
    return tasks.status(task_id)


def index_ready() -> bool:
    """本地检索库是否已有消息。空索引的提示文案是 MCP 专属渲染，故不放进用例。"""
    try:
        with store.open() as con:
            return store.counts(con)["messages"] > 0
    except Exception:
        return False
