"""用例层：MCP 与 CLI 共用的入口策略。

为什么要有这一层：账号解析、会话消歧、limit 归一化此前在 mcp_server.py 与 cli.py
各写一遍，形态还不一致——CLI 的候选列表丢 `kind`、export 的歧义被误报成「找不到
联系人」、同一个 limit 在两个入口得到不同处理。现在每个工具一个用例函数，独占
参数归一化、账号与会话解析与结果形状；两个入口只剩「参数编组」与「渲染」。

约定：
- 成功返回类型化结果（dataclass / 既有 dict 形状），歧义返回 NeedConfirm（不是
  错误），未命中抛 errors.ContactNotFound——本层不产生任何展示文案；
- 不 import MCP SDK（与其它 core 模块一致）。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import account as acc_mod
from . import errors, export, keys, reader, search, store, vector

# ---------- 参数归一化 ----------

# limit 一律夹下界 1：非正值会原样进 SQL——`LIMIT -1` 在 SQLite 里表示「不限量」，
# reader.recent 再被 `pieces[-limit:]` 错位切片，于是 `-n -1` 静默丢一条消息、
# `-n 0` 返回空、search 退化成全表扫描。上界只夹文档化了区间的工具
# （TOOLS.md §3 = 1-500、§8 = 1-50）；contacts 与 search_history 文档只写「返回
# 上限」无区间，故不设上界。
LIMIT_RECENT = (1, 500)
LIMIT_SEMANTIC = (1, 50)
LIMIT_UNBOUNDED = (1, None)


def clamp_limit(n: int, bounds: tuple[int, int | None] = LIMIT_UNBOUNDED) -> int:
    lo, hi = bounds
    return max(lo, n) if hi is None else max(lo, min(n, hi))


# ---------- 结果类型 ----------

MAX_CANDIDATES = 10          # 候选一律截前 10：避免刷屏，两个入口同一口径


@dataclass(frozen=True)
class Candidate:
    """歧义候选。显式三字段（而非 reader 的整行 dict）：它是对外契约的一部分。"""

    username: str
    display: str
    kind: str

    def to_dict(self) -> dict:
        return {"username": self.username, "display": self.display, "kind": self.kind}


@dataclass(frozen=True)
class NeedConfirm:
    """contact 命中多个联系人：不是错误，入口应让用户/agent 消歧，不要猜。"""

    candidates: list[Candidate]


@dataclass(frozen=True)
class RecentMessages:
    chat_id: str
    display: str
    messages: list[reader.Message]


@dataclass(frozen=True)
class ExportPlan:
    """导出目标。同步跑还是登记长任务由入口决定（CLI 同步、MCP 后台）。"""

    account: str
    chat_id: str | None
    full: bool


class ChatContext:
    """一次用例调用内的账号 + 会话上下文。

    reader 惰性构造、实例内复用：会话解析与展示名富化共用同一份联系人缓存
    （contact.db 只解密一次），两个入口也不再各自构造 MessageReader。
    """

    def __init__(self, account: str | None = None):
        self.account = acc_mod.resolve_account(account)
        self._reader: reader.MessageReader | None = None

    @property
    def reader(self) -> reader.MessageReader:
        if self._reader is None:
            self._reader = reader.MessageReader(self.account)
        return self._reader

    def resolve(self, contact: str) -> str | NeedConfirm:
        """contact → 唯一 chat_id；歧义返回候选，未命中抛 ContactNotFound。"""
        chat_id, cands = self.reader.resolve_one(contact)
        if chat_id:
            return chat_id
        if not cands:
            raise errors.ContactNotFound(
                f"找不到联系人「{contact}」，请用 list_contacts 查询")
        return NeedConfirm([Candidate(c["username"], c["display"], c["kind"])
                            for c in cands[:MAX_CANDIDATES]])


# ---------- 工具用例（每个工具一个函数） ----------

def account_status(account: str | None = None) -> dict:
    """账号/密钥/索引规模快照。account 省略即当前登录账号（不校验目录是否存在）。"""
    st = acc_mod.status()
    acct = account or st.current
    ready = bool(acct and keys.load_cached_hex(acc_mod.paths.bare_wxid(acct)))
    try:
        with store.connect() as con:
            index = store.counts(con)
    except Exception:
        index = {"messages": 0, "chats": 0, "last_ts": None}
    return {"wechat_running": st.running, "current_account": st.current,
            "accounts": st.accounts, "key_ready": ready, "index": index}


def contacts(query: str = "", limit: int = 200,
             account: str | None = None) -> list[dict]:
    """联系人模糊搜索。limit 默认 200 是 reader 的默认值：MCP 传 20，CLI 不传。"""
    return ChatContext(account).reader.contacts(query=query or None,
                                                limit=clamp_limit(limit))


def recent_messages(contact: str, limit: int = 50,
                    account: str | None = None) -> RecentMessages | NeedConfirm:
    ctx = ChatContext(account)
    chat_id = ctx.resolve(contact)
    if isinstance(chat_id, NeedConfirm):
        return chat_id
    msgs = ctx.reader.recent(chat_id, limit=clamp_limit(limit, LIMIT_RECENT))
    return RecentMessages(chat_id=chat_id, display=ctx.reader.display_name(chat_id),
                          messages=msgs)


def export_plan(contact: str | None = None, full: bool = False,
                account: str | None = None) -> ExportPlan | NeedConfirm:
    """导出目标解析：歧义/未命中和其它工具同一策略（不再自成一派）。"""
    ctx = ChatContext(account)
    chat_id = None
    if contact:
        got = ctx.resolve(contact)
        if isinstance(got, NeedConfirm):
            return got
        chat_id = got
    return ExportPlan(account=ctx.account, chat_id=chat_id, full=full)


def search_history(query: str, contact: str | None = None, limit: int = 20,
                   account: str | None = None) -> dict | NeedConfirm:
    ctx = ChatContext(account)
    chat_id = None
    if contact:
        got = ctx.resolve(contact)
        if isinstance(got, NeedConfirm):
            return got
        chat_id = got
    return search.search(query, chat_id=chat_id, limit=clamp_limit(limit),
                         account=ctx.account)


def semantic_search(query: str, contact: str | None = None, limit: int = 10,
                    account: str | None = None) -> dict | NeedConfirm:
    ctx = ChatContext(account)
    chat_id = None
    if contact:
        got = ctx.resolve(contact)
        if isinstance(got, NeedConfirm):
            return got
        chat_id = got
    res = vector.semantic_search(query, chat_id=chat_id,
                                 limit=clamp_limit(limit, LIMIT_SEMANTIC),
                                 account=ctx.account)
    for h in res.get("hits", []):
        h["display"] = ctx.reader.display_name(h["chat_id"])   # 缓存命中，只解密一次
    return res


def build_vector_index(account: str | None = None, rebuild: bool = False) -> dict:
    """注意 account 语义与其它工具相反：省略表示索引**全部**账号（见 TOOLS.md §7）。"""
    return {"task_id": vector.start_build_task(account, rebuild), "status": "running"}


def task_status(task_id: str | None = None) -> dict | list:
    return export.task_status(task_id)


def index_ready() -> bool:
    """本地检索库是否已有消息。空索引的提示文案是 MCP 专属渲染，故不放进用例。"""
    try:
        with store.connect() as con:
            return store.counts(con)["messages"] > 0
    except Exception:
        return False
