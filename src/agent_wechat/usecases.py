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

import contextlib
import dataclasses
import hashlib
import pathlib
import re
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import account as acc_mod
from . import errors, export, keys, messages, paths, reader, search, store, tasks, vector

if TYPE_CHECKING:
    from collections.abc import Sequence

    # 仅类型检查期：media 顶层拉起 cryptography，运行时在 media_export 内惰性 import
    from . import media

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


def to_accounts(account: str | None) -> list[str] | None:
    """wire 层的单数 `account` → core 层的复数 `accounts`（None = 全部，[] = 一个都不索引）。

    两个入口的执行模型不同（CLI 的 `vector-build` 同步跑、MCP 的 `build_vector_index`
    登记长任务），但「一个字符串还是一个列表」的翻译必须是同一份——否则又是一处两写，
    正是 C2/C4 要消的那类重复。
    """
    return [account] if account else None


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
    degraded: tuple[Degradation, ...] = ()


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
            # 文案保持中立于入口：CLI 该提示 `contacts`、MCP 该提示 `list_contacts`，
            # 后缀由各自的适配层追加——本层不产生展示文案。
            raise errors.ContactNotFound(f"找不到联系人「{contact}」")
        return NeedConfirm([Candidate(c["username"], c["display"], c["kind"])
                            for c in cands[:MAX_CANDIDATES]])

    def resolve_optional(self, contact: str | None) -> str | None | NeedConfirm:
        """可选 contact：空 → None（不限会话）；否则同 resolve（歧义给候选、未命中抛错）。

        三个「可选 contact」的用例共用，省得同一段判断在每处重写。
        """
        return self.resolve(contact) if contact else None

    def degradations(self) -> list[dict]:
        """本次调用见过的降级，信封形状（search/semantic 的出口，Ruling 13）。

        **只报已经构造出来的 reader 上的漂移**：没构造过 reader 说明这次调用没碰微信的库
        （检索走的是本地索引），也就没有「静默变少」可言。反过来，为了报降级去构造一个
        reader 是有代价的——它会读 passphrase 缓存，缓存没了就抛（`purge --keys` 之后
        检索仍然可用），把一条本来能用的检索打翻。
        """
        if self._reader is None:
            return []
        return [{"capability": d.capability, "effect": d.effect}
                for d in collect_degradations(self._reader)]


# 能力 → 降级后用户**少了什么**（人话）。用户不需要知道表名（spec §7）：
# `SchemaDrift` 只说「哪里漂了」，策略层才知道「那意味着什么」。
DEGRADE_EFFECT: dict[str, str] = {
    "username_pool": "部分会话的显示名无法反查（其余功能不受影响）",
    "roster": "显示名暂不可用，发送者会回落到原始帐号标识",
    "contacts": "联系人查询不可用",
    "messages": "消息读取不可用",
}


@dataclass(frozen=True)
class Degradation:
    """一次降级：哪里漂了 + 那意味着什么。"""
    capability: str
    db: str
    table: str
    missing_columns: tuple[str, ...]
    effect: str


def collect_degradations(rdr: reader.MessageReader) -> tuple[Degradation, ...]:
    """从 reader 实例收集降级，按 `(capability, table)` 去重（spec §7）。

    `reader._record_drift`（T4）已经用同一把键去过重，所以在**今天的** reader 上这里
    是兜底：`_drift` 的类型是 list，「进来就是去重的」不是它对外保证的东西——将来多一个
    来源（或某条直接 append 的路径）带进重复，用户就会看到几十行一模一样的提示。
    """
    seen: dict[tuple[str, str], Degradation] = {}
    for d in rdr._drift:
        key = (d.capability, d.table)
        if key not in seen:
            seen[key] = Degradation(
                capability=d.capability, db=d.db, table=d.table,
                missing_columns=d.missing_columns,
                effect=DEGRADE_EFFECT.get(d.capability, "部分功能降级"))
    return tuple(seen.values())


# 名册刷新间隔。刷新 = 解密一次 contact.db（秒级），且落在查询路径上会阻塞用户：
# 太短则频繁解密，太长则改完备注要等。900 秒是 spec §7 定的取舍。
ROSTER_TTL = 900


def _refresh_roster(ctx: ChatContext) -> None:
    """名册过期就重读一次 contact.db。**尽力而为，绝不抛。**

    `contact.db` 读不到（没 bootstrap、权限坏了）不该让检索也查不了——那是两条独立的
    能力（spec §7）。这里吞掉 `AgentWeChatError` 与裸 `sqlite3.Error`：名册旧总比查不了好。

    为什么连裸 sqlite 错误一起吞：`roster()` 自己没有宽 except（spec §5：自家 bug 与库损坏
    都不该被吞成「名册为空」），于是**探测覆盖不到**的失败会原样穿到这里——`name2id` 变成
    `WITHOUT ROWID` 时 `JOIN ... ON n.rowid` 抛 `OperationalError`（`rowid` 是隐式列，
    `PRAGMA table_info` 不列它，声明覆盖不到），半拷贝的 WAL 抛 `DatabaseError`。安全网该
    落在「尽力而为」这句承诺所在的那一层。
    """
    try:
        with store.open() as con:
            if not store.roster_is_stale(con, ctx.account, ROSTER_TTL):
                return
            rows = ctx.reader.roster()
            # 空名册不写（T2 review 的 Minor 1）：replace_roster([]) 会清空名册并盖上
            # refreshed_at，把整个 TTL 锁死成「全是 wxid」而刷新被记成成功。「读成功且
            # 真没东西」与「读出意外的空」在这里不可区分，按「空 = 这次刷新没发生」处理：
            # 下次查询再试。罕见代价（没有群/联系人的账号每次查询重读 contact.db）
            # 由 Ruling 13 接受——正确性无损，性能损失只在罕见账号上。
            if rows:
                store.replace_roster(con, ctx.account, rows)
    except (errors.AgentWeChatError, sqlite3.Error):
        pass


def _display_or_id(ctx: ChatContext, chat_id: str) -> str:
    """显示名富化：`contact` 表漂移时回落 `chat_id`——那正是查不到名字时的无信息版本。

    显示名是**富化**而非唯一来源（spec §6 的判据）：消息在 message 分片里、hits 在索引库里，
    都不受 `contact` 漂移影响。而 `_apply_roster` 已经为**同一个漂移**把发送者降级成 wxid
    了——富化这里跟着抛，等于同一件事在一处降级、在另一处把整个命令杀掉。

    漂移本身记在 `reader._drift` 上，随既有的 `degraded` 通道出去（调用方**先**富化再收集）。
    `resolve` 不走这条回落：没有 chat_id 就没有会话可查，那条按 spec §6 就该抛。
    """
    try:
        return ctx.reader.display_name(chat_id)
    except errors.SchemaDrift:
        return chat_id


# ---------- 工具用例（每个工具一个函数） ----------

def account_status(account: str | None = None) -> dict:
    """账号/密钥/索引规模快照。account 省略即当前登录账号（不校验目录是否存在）。"""
    st = acc_mod.status()
    acct = account or st.current
    ready = bool(acct and keys.load_cached_hex(acc_mod.paths.bare_wxid(acct)))
    try:
        with store.open() as con:
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
    return RecentMessages(chat_id=chat_id, display=_display_or_id(ctx, chat_id),
                          messages=msgs,
                          degraded=collect_degradations(ctx.reader))


def export_plan(contact: str | None = None, full: bool = False,
                account: str | None = None) -> ExportPlan | NeedConfirm:
    """导出目标解析：歧义/未命中和其它工具同一策略（不再自成一派）。"""
    ctx = ChatContext(account)
    chat_id = ctx.resolve_optional(contact)
    if isinstance(chat_id, NeedConfirm):
        return chat_id
    return ExportPlan(account=ctx.account, chat_id=chat_id, full=full)


def export_history(contact: str | None = None, full: bool = False,
                   account: str | None = None) -> dict | NeedConfirm:
    """登记导出长任务，立即返回 {task_id, status}（MCP 走这条；CLI 同步跑 export_plan）。

    任务登记归本层所有：core 模块只提供管道（export.run_export / vector.build_index），
    不带任务注册表。
    """
    plan = export_plan(contact=contact, full=full, account=account)
    if isinstance(plan, NeedConfirm):
        return plan
    # chat_id 一并记进任务元数据（tasks.json 已发布字段），与旧 export.start_task 一致
    task_id = tasks.start(
        "export",
        lambda _tid: export.run_export(plan.account, chat_id=plan.chat_id, full=plan.full),
        account=plan.account, chat_id=plan.chat_id)
    return {"task_id": task_id, "status": "running"}


MEDIA_KINDS = ("image", "voice", "video")

# 月份入参格式：与 media.month_of / 磁盘目录名（`%Y-%m`）逐字符同形。月份部分限定 01-12：
# `2026-13` 同样比不中任何目录，一起被拒（只放行 `\d{2}` 会漏掉它）。
MONTH_RE = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])")


@dataclass(frozen=True)
class MediaExportResult:
    """media_export 的成功返回（CLI 与 MCP 共用，T7 消费）。out_dir 取自 report——
    实际使用的输出根（--out 省略时是 HOME_DIR/media），由 export_media 赋值，不重算。"""

    chat_id: str
    out_dir: str
    report: media.MediaReport


def media_export(contact: str, *, kinds: list[str], months: list[str] | None = None,
                 out_dir=None, limit: int = 2000,
                 account: str | None = None) -> MediaExportResult | NeedConfirm:
    """媒体导出用例：会话消歧 + 交 media.export_media。kinds/months 由入口解析成列表。

    chat_ids 恒为单元素——当前调用方一次只导一个会话，重复无从发生，故**不去重**；
    若未来放开多会话，调用方须自行保证无重复（重复 chat_id 会使图片/语音计数翻倍）。
    """
    from . import media                       # 惰性：media 顶层拉起 cryptography
    ctx = ChatContext(account)
    chat_id = ctx.resolve(contact)
    if isinstance(chat_id, NeedConfirm):
        return chat_id
    bad = [k for k in kinds if k not in MEDIA_KINDS]
    if bad:
        raise errors.SearchInputError(f"未知的媒体类型: {bad}（可用: {', '.join(MEDIA_KINDS)}）")
    # months 与磁盘目录名逐字符比对：格式错的串永远比不中，不拦就是静默 0 命中、exit 0
    # （用户看到的是「这个月没有媒体」而不是「月份写错了」）。
    bad_months = [m for m in (months or []) if not MONTH_RE.fullmatch(m)]
    if bad_months:
        raise errors.SearchInputError(f"月份格式应为 YYYY-MM：{', '.join(bad_months)}")
    report = media.export_media(ctx.account, [chat_id], kinds=kinds, months=months,
                                out_dir=out_dir, max_files=clamp_limit(limit, (1, 2000)))
    return MediaExportResult(chat_id=chat_id, out_dir=report.out_dir, report=report)


LIMIT_MEDIA = (1, 500)

_VOICE_TYPE = messages.TYPE_VOICE
_MEDIA_TYPES = (messages.TYPE_IMAGE, messages.TYPE_VIDEO, messages.TYPE_VOICE)


def _voice_keys_for(account: str) -> frozenset[tuple[str, int]] | None:
    """语音本体存在集合。**测试接缝**：替换它以免测试真去开 media_*.db。

    写成函数而不是 `_voice_keys_for = media.voice_body_keys`：本模块顶层不 import media
    （顶层会拉起 cryptography，见文件头部的 TYPE_CHECKING 注释），模块级取属性会在
    import 期就 NameError。生产路径上就是 `media.voice_body_keys`。
    """
    from . import media
    return media.voice_body_keys(account)


@dataclass(frozen=True)
class MediaRefState:
    """一条消息的媒体状态。`kind` 为 None 只在 status='none' 时出现。"""
    msg_key: str
    ts: int
    kind: str | None
    status: str                # 'none' | 'exported' | 'exportable' | 'gone'
    path: str | None           # 仅 'exported'：产物绝对路径


@dataclass(frozen=True)
class MessageMediaResult:
    chat_id: str
    out_dir: str
    states: list[MediaRefState]
    counts: dict[str, int]
    # 按 kind 再拆一次 counts。**由本层派生、两个入口共用**：此前 MCP 与 CLI 各派生一份
    # 逐字相同的代码，而两份文档承诺两边暴露同一套 by_kind——改动一侧就会静默分歧。
    # 默认空 dict 只为让不关心拆分的构造点（测试夹具）不必写它；生产路径恒被填。
    by_kind: dict[str, dict[str, int]] = dataclasses.field(default_factory=dict)
    truncated: bool = False    # limit 截断了答案：与 media.MediaReport.truncated 同义
    degraded: tuple[Degradation, ...] = ()


_KIND_OF_TYPE = {messages.TYPE_IMAGE: "image",
                 messages.TYPE_VIDEO: "video",
                 messages.TYPE_VOICE: "voice"}


def _state_of(row: dict, ref: tuple[str, str | None] | None, *,
              out_root: pathlib.Path, account: str,
              voice_keys: frozenset | None) -> MediaRefState:
    """单条消息的四态判定。顺序严格按 spec §4.1：none → exported → exportable → gone。"""
    from . import media                       # 惰性：media 顶层拉起 cryptography
    # none 只由 msg_type 判（索引内实测有 259 条媒体消息拿不到 ref）：ref 只提供 file_key
    kind = _KIND_OF_TYPE.get(row["msg_type"])
    if kind is None:
        return MediaRefState(row["msg_key"], row["ts"], None, "none", None)
    file_key = ref[1] if ref else None
    local_id = row["local_id"]
    p = media.exported_path(out_root, chat_md5=row["chat_md5"], kind=kind,
                            ts=row["ts"], file_key=file_key, local_id=local_id)
    if p is not None:
        return MediaRefState(row["msg_key"], row["ts"], kind, "exported", str(p))
    if kind == "voice":
        # 语音库读不到（None）时不能当成空集合：那会把「读不到」谎报成「本机没有」
        present = voice_keys is not None and (row["chat_md5"], local_id) in voice_keys
    elif kind == "image":
        present = file_key is not None and media.resolve_image(
            account, row["chat_md5"], row["ts"], file_key) is not None
    else:
        present = file_key is not None and media.resolve_video(
            account, row["ts"], file_key) is not None
    return MediaRefState(row["msg_key"], row["ts"], kind,
                         "exportable" if present else "gone", None)


def _month_clause(months: list[str] | None) -> tuple[str, list[int]]:
    """months → SQL 片段与绑定参数（本地月区间，见 `media.month_bounds`）。

    **必须在 SQL 里筛**：取回后再滤等于让 limit 卡在筛之前——命中月份之外的新消息会把
    命中月份的消息挤出答案，于是 `counts` 全 0，而「那个月真没有媒体」与「我只看了最新
    的 N 条」完全无法区分。空 months 返回空片段，SQL 一字不多。

    **调用方必须先校验格式**（`message_media` 里的 `bad_months`）：`media.month_bounds`
    是裸 `datetime` 运算，`2026-13` 会让它抛 ValueError 穿透 core 层。
    """
    if not months:
        return "", []
    from . import media                       # 惰性：media 顶层拉起 cryptography
    parts: list[str] = []
    args: list[int] = []
    for lo, hi in media.month_bounds(months):
        parts.append("(m.ts >= ? AND m.ts < ?)")
        args += [lo, hi]
    return " AND (" + " OR ".join(parts) + ")", args


def message_media(contact: str, *, keys: Sequence[str] | None = None,
                  months: list[str] | None = None, limit: int = 200,
                  out_dir: str | None = None, account: str | None = None,
                  _ctx=None, _con=None) -> MessageMediaResult | NeedConfirm:
    """消息 → 媒体状态（spec §4.1 的四态）。

    keys=None → 该会话**所有媒体类型的消息**（msg_type ∈ {3,43,34}），受 limit 约束
    ——按 msg_type 而非按 ref 筛选：索引内实测有 259 条媒体消息拿不到 ref，
    按 ref 筛会把它们谎报成「没有媒体」。给了 keys → 只答这些，查不到的静默略过。
    months 在 SQL 里筛（不是取回后再滤）：limit 卡的必须是**命中月份**的条数。
    truncated=True 表示 limit 截断了答案，别把它当成「那个月没有媒体」。
    """
    from . import media                       # 惰性：media 顶层拉起 cryptography
    ctx = _ctx or ChatContext(account)
    chat_id = ctx.resolve(contact)
    if isinstance(chat_id, NeedConfirm):
        return chat_id
    # 与 media_export / moments_export 同一口径：格式错的月份串永远比不中，不拦就是
    # 静默 0 命中、exit 0（用户看到「这个月没有媒体」而不是「月份写错了」）。
    bad_months = [m for m in (months or []) if not MONTH_RE.fullmatch(m)]
    if bad_months:
        raise errors.SearchInputError(f"月份格式应为 YYYY-MM：{', '.join(bad_months)}")
    cm = messages.md5_of(chat_id)
    # 绝对化：`--out 相对路径` 会让 states[].path 变成 MCP 客户端用不了的相对路径。
    # `.resolve()` 是纯路径运算，不碰微信数据（红线不受影响）。
    out_root = (pathlib.Path(out_dir) if out_dir else paths.HOME_DIR / "media").resolve()
    lim = clamp_limit(limit, LIMIT_MEDIA)
    month_sql, month_args = _month_clause(months)
    degraded: list[Degradation] = []

    with contextlib.ExitStack() as stack:
        con = _con if _con is not None else stack.enter_context(store.open())
        if keys is not None:
            placeholders = ",".join("?" * len(keys))
            # `chat_md5` 一滤不能少：keys 是调用方给的，可能来自别的会话——不加就会把
            # 别人的媒体冠上本次解析出的 chat_id 报出去。
            sql = (f"SELECT {messages.column_list(*messages.COLUMNS, prefix='m.')} "
                   f"FROM messages m WHERE m.msg_key IN ({placeholders}) "
                   f"AND m.chat_md5 = ?{month_sql} ORDER BY m.ts DESC")
            rows = [dict(zip(messages.COLUMNS, r))
                    for r in con.execute(sql, [*keys, cm, *month_args])]
            rows = [r for r in rows if r["account"] == ctx.account]
            truncated = len(rows) >= lim
            rows = rows[:lim]
        else:
            type_ph = ",".join("?" * len(_MEDIA_TYPES))
            sql = (f"SELECT {messages.column_list(*messages.COLUMNS, prefix='m.')} "
                   f"FROM messages m WHERE m.account=? AND m.chat_md5=? "
                   f"AND m.msg_type IN ({type_ph}){month_sql} "
                   f"ORDER BY m.ts DESC LIMIT ?")
            rows = [dict(zip(messages.COLUMNS, r))
                    for r in con.execute(sql,
                                         [ctx.account, cm, *_MEDIA_TYPES, *month_args, lim])]
            # 取回条数正好等于 limit ⇒ 库里可能还有更多。这里偏保守（恰好等于总数时也报
            # True），与 media.MediaReport 的 `produced >= max_files` 同一取舍：宁可多报
            # 「可能被截断」，也不能漏报一条真的没答完。
            truncated = len(rows) >= lim
        refs = store.media_refs_by_keys(con, [r["msg_key"] for r in rows])

    # 语音本体集合**按需**取：这个会话一条语音都没有时，不值得去开 media_*.db
    voice_keys = None
    if any(r["msg_type"] == _VOICE_TYPE for r in rows):
        voice_keys = _voice_keys_for(ctx.account)
        if voice_keys is None:
            degraded.append(Degradation(
                capability="msg_media", db="message/media_*.db", table="VoiceInfo",
                missing_columns=(),
                effect="语音库读不到：语音消息一律按「本体不在本机」呈现"))

    states = [_state_of(r, refs.get(r["msg_key"]), out_root=out_root,
                        account=ctx.account, voice_keys=voice_keys) for r in rows]
    counts = {"none": 0, "exported": 0, "exportable": 0, "gone": 0}
    for s in states:
        counts[s.status] += 1
    # 池化的 counts 把本机可得率相差几个数量级的类别加在一起（语音几乎全可导出、图片视频
    # 几乎全 gone），读起来像「这个会话的媒体大多没了」；按 kind 拆开才是实情。`none` 态
    # 没有 kind，只进池化的 counts——**这一份拆分是两个入口唯一的真相**。
    by_kind: dict[str, dict[str, int]] = {}
    for s in states:
        if s.kind is None:
            continue
        bucket = by_kind.setdefault(s.kind, {})
        bucket[s.status] = bucket.get(s.status, 0) + 1
    # 走 `collect_degradations(ctx.reader)` 而不是 `ctx.degradations()`：后者是 search/
    # semantic 的**信封形状**出口（dict），而本结果类型的元素是 `Degradation`（同
    # recent_messages 的口径，转换归入口层）。reader 在这行之前必然已构造——`ctx.resolve`
    # 走的就是它——所以这里不会为报降级白读一次库（那正是 ctx.degradations 的顾虑）。
    return MessageMediaResult(chat_id=chat_id, out_dir=str(out_root),
                              states=states, counts=counts, by_kind=by_kind,
                              truncated=truncated,
                              degraded=tuple(degraded) + collect_degradations(ctx.reader))


def media_rebuild_refs(account: str | None = None, progress=None) -> int:
    """全量重扫微信库，只为存量历史回填 media_refs（spec §4.5）。幂等 upsert；
    **不写 messages**（消息内容不变，只有 refs 需要补）。分钟级，同步执行。"""
    ctx = ChatContext(account)
    n = 0
    with store.open() as con:
        for _rows, refs in ctx.reader.iter_all():
            store.upsert_media_refs(con, refs)
            n += len(refs)
            if progress and n and n % 20_000 < store.BATCH:
                progress(n)
    return n


def search_history(query: str, contact: str | None = None, limit: int = 20,
                   account: str | None = None) -> dict | NeedConfirm:
    """hits 的 `sender_name` 来自名册（`search.py:13`），而 `_refresh_roster` 吞掉漂移——
    所以降级说明必须随结果带出去（Ruling 13），否则显示名**静默**回落成 wxid。"""
    ctx = ChatContext(account)
    chat_id = ctx.resolve_optional(contact)
    if isinstance(chat_id, NeedConfirm):
        return chat_id
    _refresh_roster(ctx)
    res = search.search(query, chat_id=chat_id, limit=clamp_limit(limit),
                        account=ctx.account)
    res["degraded"] = ctx.degradations()
    return res


def semantic_search(query: str, contact: str | None = None, limit: int = 10,
                    account: str | None = None) -> dict | NeedConfirm:
    """同 search_history：hits 的 `display` 也由名册富化（Ruling 13）。"""
    ctx = ChatContext(account)
    chat_id = ctx.resolve_optional(contact)
    if isinstance(chat_id, NeedConfirm):
        return chat_id
    _refresh_roster(ctx)
    res = vector.semantic_search(query, chat_id=chat_id,
                                 limit=clamp_limit(limit, LIMIT_SEMANTIC),
                                 account=ctx.account)
    for h in res.get("hits", []):
        h["display"] = _display_or_id(ctx, h["chat_id"])   # 缓存命中，只解密一次
    res["degraded"] = ctx.degradations()
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
    """
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


@dataclasses.dataclass
class MomentsExportResult:
    """moments_export 的成功返回（CLI 与 MCP 共用）。out_dir 由本用例赋值，入口不重算。"""
    out_dir: str
    report: "moments.MomentsReport"


def moments_export(contact: str | None = None, *, months: list[str] | None = None,
                   with_media: bool = False, limit: int = 2000, out_dir=None,
                   account: str | None = None, fetch=None) -> "MomentsExportResult | NeedConfirm":
    """朋友圈导出用例：解析 →（可选）下载解密 → 渲染 HTML。

    `with_media=False`（默认）时**全程零网络**——这是零网络红线上本次新增的例外开关，
    默认必须关闭。`fetch` 仅作测试注入，生产调用方不传。
    """
    from . import media, moments, moments_html, sns_media   # 惰性：moments_html 只在导出时用
    ctx = ChatContext(account)
    author = ctx.resolve_optional(contact)
    if isinstance(author, NeedConfirm):
        return author
    bad_months = [m for m in (months or []) if not MONTH_RE.fullmatch(m)]
    if bad_months:
        raise errors.SearchInputError(f"月份格式应为 YYYY-MM：{', '.join(bad_months)}")
    cap = clamp_limit(limit, (1, 2000))
    do_fetch = fetch if fetch is not None else sns_media.fetch

    out_root = media._ensure_tree(
        pathlib.Path(out_dir) if out_dir else paths.HOME_DIR / "moments")
    report = moments.MomentsReport()
    kept: list[moments.SnsPost] = []
    lo = hi = None
    for p in moments.iter_posts(ctx.account, chat_filter=author, months=months,
                                report=report):
        if len(kept) >= cap:
            report.truncated = True
            break
        lo = p.create_time if lo is None else min(lo, p.create_time)
        hi = p.create_time if hi is None else max(hi, p.create_time)
        report.comments += sum(1 for c in p.comments if c.kind == moments.CMT_COMMENT)
        report.likes += sum(1 for c in p.comments if c.kind == moments.CMT_LIKE)
        kept.append(_resolve_media(p, out_root, report, with_media, do_fetch))
    # 解析失败此前是**无声**的：`parse_post` 返回 None、`iter_posts` 直接 continue，既没有
    # 计数也没有说明。最坏情况下微信改了 XML 形状 → 每条动态都被丢掉 → 退出码 0、信封
    # `{"posts": 0, "degraded": []}`、HTML 写着「没有匹配的动态」，与「这个账号没有朋友圈」
    # **完全无法区分**。故这里补一条带条数的降级说明（CLI 走 stderr、MCP 走 degraded、
    # 产物页首的降级区也读它），并把同类问题合并成一条（50 条失败写 50 行同样的话是噪音）。
    if report.failed:
        report.degraded.append(f"{report.failed} 条动态解析失败（XML 结构不符，已跳过）")
    if report.media_garbage:
        report.degraded.append(
            f"{report.media_garbage} 条媒体解密后不是已知图片格式（未落盘）")
    report.posts = len(kept)
    report.window = (lo, hi) if lo is not None else None
    report.out_dir = str(out_root)

    doc = moments_html.render(kept, report)
    index = out_root / "index.html"
    try:
        index.write_text(doc, encoding="utf-8")
    except OSError as exc:
        raise errors.MediaOutputError(f"写产物失败：{index}") from exc
    try:
        # 尽力而为（同 media._chmod_quiet / _save 的立场）：权限没收紧只是这一级没收紧，
        # 不该让「全部媒体都已下载完」之后的一次 chmod 失败把整次导出作废。
        index.chmod(0o600)
    except OSError:
        pass
    return MomentsExportResult(out_dir=str(out_root), report=report)


# `url@md5` 是**未经消毒的 XML 属性**（好友发的动态就能携带它），而它会被直接当作产物文件名。
# `media._dest` 只做 `d / f"{stem}.{ext}"`，不做任何消毒——`md5="../../../../x"` 会把明文写出
# 产物根之外，`md5="/tmp/x"` 更会因 pathlib 丢弃左操作数而直接落在绝对路径上（已实测复现；
# 后者还会让 `relative_to` 抛 ValueError 穿透 core 层）。故走白名单：只有 32 位 hex 才认
# （真实数据 4899/5076 命中），其余一律回落到 URL 的 md5。
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}\Z")


def _resolve_media(p: "moments.SnsPost", out_root, report, with_media: bool,
                   do_fetch) -> "moments.SnsPost":
    """逐条媒体决定「下载并落盘」还是「标注未下载」。单条失败只计数，不外抛。"""
    from . import media, moments, sns_media
    out = []
    for m in p.media:
        if not with_media:
            report.media_not_downloaded += 1
            out.append(m)
            continue
        if sns_media.build_url(m) is None:
            report.media_not_downloaded += 1          # 无 token（如实况照片）→ 不可下载
            out.append(m)
            continue
        try:
            data = do_fetch(m)
            ext = _sniff_ext(data)
            if ext is None:
                report.media_failed += 1
                report.media_garbage += 1          # 降级说明在收尾处统一写一条
                out.append(m)
                continue
            raw = m.md5 or ""
            stem = (raw if _HEX32.match(raw)
                    else hashlib.md5((m.url or "").encode()).hexdigest())
            saved = media._save(out_root, "media", m.kind,
                                media.month_of(p.create_time), stem, ext, data)
            if saved is None:
                report.media_failed += 1
                out.append(m)
                continue
            report.media_downloaded += 1
            out.append(dataclasses.replace(
                m, local=str(saved.relative_to(out_root))))
        except sns_media.SnsMediaError:
            report.media_failed += 1
            out.append(m)
    return dataclasses.replace(p, media=tuple(out))


def _sniff_ext(data: bytes) -> str | None:
    """按魔数判扩展名——解密成功但内容非图片时返回 None（spec §5）。"""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp":
        return "mp4"
    return None
