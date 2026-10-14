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
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import account as acc_mod
from . import (appmsg, chatview, errors, exclusions, export, keys, media, messages,
                msgextra, paths, reader, search, store, tasks, vector)
# 模板模块名与下面的用例函数 `chat_index()` **同名**：这里起别名，否则函数定义会遮蔽
# 模块名（`index_html.render` 那处引用会变成调用函数自身）。
from . import chat_index as index_html

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

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
    # 贴纸名字是**锦上添花**的来源（spec §6.3）：少了它仍有消息 XML 里的 desc，
    # 再兜不住才回落短 ID——所以这句说的是「名字变少」，不是「功能没了」。
    "stickers": "贴纸名称少一部分，取不到名字的会回落短 ID",
}


@dataclass(frozen=True)
class Degradation:
    """一次降级：哪里漂了 + 那意味着什么。"""
    capability: str
    db: str
    table: str
    missing_columns: tuple[str, ...]
    effect: str
    drift: bool = True      # EA-095：False = 非漂移成因（未回填 / 覆盖不足），CLI 不加「结构不符」后缀


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
    # F2（T8 复审）：非漂移成因的源级降级（reader._source_notes）——`drift=False` 让
    # CLI 不加「结构不符」后缀（成因是「读不出来」而不是「结构对不上」）。用 getattr
    # 取：测试里的 reader 桩只建模各自路径用到的那部分状态（真实 reader 在 __init__ 恒有）。
    # F7（T8 复验）：把**哪个库**烘进 effect——信封只带 {capability, effect}，不写进
    # effect 的话两个名字源都坏时 CLI/MCP 各看到两条逐字相同的说明，谁都不知道去修哪个。
    for capability, db in getattr(rdr, "_source_notes", ()):
        key = (capability, db)
        if key not in seen:
            effect = DEGRADE_EFFECT.get(capability, "部分功能降级")
            seen[key] = Degradation(
                capability=capability, db=db, table="", missing_columns=(),
                effect=f"{effect}（{db} 读不出来）" if db else effect, drift=False)
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
    ready = bool(acct and keys.load_cached_hex(paths.bare_wxid(acct)))
    index = {"messages": 0, "chats": 0, "last_ts": None}
    schema_notes: list[str] = []
    # EA-025：**只读诊断不许建库**。`store.open()` 会 executescript(SCHEMA)——HOME 空的
    # 机器上跑一次 `status` 就凭空建出 index.db 与 exclusions.db（实测）。与
    # `purge_chat_targets` 用同一配方：库不在 ≡ 零计数。F9（T7 复审）：`open()` 的建文件
    # 副作用有**两处**（connect 建 index.db、ATTACH 建 exclusions.db），闸必须覆盖两个
    # 文件——只闸 index.db 时，「index 在而排除清单缺」的手工态仍会被补建一个空清单。
    # 排除清单的位置跟着 `path_for(DB_PATH)` 走：那正是 `store.connect` ATTACH 的目标
    # （生产上等于 `exclusions.path()`，测试里两者会分叉——闸必须与 connect 用同一个推导）。
    if store.DB_PATH.exists() and not exclusions.path_for(store.DB_PATH).exists():
        # 索引在、排除清单不在（手工删过它）：闸门覆盖**两个**文件（F9），所以这里不能
        # `store.open()`——那会把空清单建回来。但**全零计数 + 空 schema_notes 读起来就是
        # 「还没导出过」**——正是 EA-025 要消灭的那个假象（review-code 的 Spec 轴 #4）。
        # 不建库的前提不变，这一句必须带上。
        schema_notes = [
            f"排除清单不在（{exclusions.path_for(store.DB_PATH)}）而索引库在——"
            "index 计数按零呈现（只为不凭空建出那个文件）；跑一次导出会把它建回来"]
    elif (store.DB_PATH.exists()
            and exclusions.path_for(store.DB_PATH).exists()):
        try:
            with store.open() as con:
                index = store.counts(con)
                # EA-077：触发器体是「OLD 库 + 新代码」的静默失配面——自检报出来（不改库）。
                schema_notes = store.schema_drift_notes(con)
        except (errors.AgentWeChatError, sqlite3.Error, OSError) as exc:
            # F10（T7 复审）：读不了 ≠ 空索引——把「库坏了」说成「还没导出过」正是本仓
            # 到处在消灭的形态（message_media / chat_export / message_parts / chat_index
            # 四处同立场）。计数仍按零呈现（形状不变），但这条说明必须带上。
            schema_notes = [f"索引库读不出来（{errors.redact(str(exc))}）——"
                            "index 计数按零呈现；删除 index.db 后重跑一次导出可重建"]
    return {"wechat_running": st.running, "current_account": st.current,
            "accounts": st.accounts, "key_ready": ready, "index": index,
            "schema_notes": schema_notes}


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

    def _run(tid: str) -> int:
        # 计数契约的两半（spec §5.4）：返回值只数**实际入库**的行，被排除挡下的另计并以
        # `skipped` 随任务记录带回 MCP 信封（与 vector 任务同名字段——`tasks.status()`
        # 原样透出，入口不需要再翻译）。只修 `rows` 那一半的话，agent 见数少了却不知道
        # 少在哪：「导出成功」与实际发生的事不一致，正是 spec §7 要消灭的形态。
        return export.run_export(plan.account, chat_id=plan.chat_id, full=plan.full,
                                 # EA-082：`rows` 此前恒为 0——progress 没接线（S3-010）。
                                 # progress 的入参正是**已入库行数**，与终值同口径；
                                 # 终值由 tasks._run 用返回值覆盖（既有语义保留）。
                                 progress=lambda n: tasks.update(tid, rows=n),
                                 on_skipped=lambda n: tasks.update(tid, skipped=n))

    # chat_id/full 一并记进任务元数据（tasks.json 已发布字段），与旧 export.start_task 一致；
    # 两者同时是**准入键**（F1，T7 复审）：缺了 full，「增量在跑时要求全量」会被静默吸收、
    # 全量根本没跑，而调用方按 done 轮询会得出错误结论。
    task_id = tasks.start("export", _run, account=plan.account, chat_id=plan.chat_id,
                          full=plan.full)
    return {"task_id": task_id, "status": "running"}


# 「能导出什么」的清单。**贴纸在列**（2026-09-23 恢复导出后收编）：它的字节来源是
# CDN 取件，默认关（`--with-cdn`），故它是可导出 kind、但不是「默认就能导出的 kind」。
MEDIA_KINDS = ("image", "voice", "video", "sticker")

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
                 out_dir=None, limit: int = 500, force: bool = False,
                 account: str | None = None,
                 with_cdn: bool = False) -> MediaExportResult | NeedConfirm:
    """媒体导出用例：会话消歧 + 交 media.export_media。kinds/months 由入口解析成列表。

    chat_ids 恒为单元素——当前调用方一次只导一个会话，重复无从发生，故**不去重**；
    若未来放开多会话，调用方须自行保证无重复（重复 chat_id 会使图片/语音计数翻倍）。

    视频归属要索引库：本层打开 `store` 并把连接交给 `export_media`（按消息的 `media_refs`
    定位本体——视频树本身没有会话维度）。索引打不开时**本次不导视频**并如实降级，绝不退回
    `export_media` 的 `con=None`（账号级枚举）：那会把账号级视频树里的全部文件算到本次请求
    的会话头上——错归属比缺归属更坏。

    `limit` 是**本次最多新增**的文件数：已存在的产物会被跳过（可通过 `force=True` 重建），
    故重跑同一条命令即可继续。

    `with_cdn`（默认 False）：贴纸本机那份是加密的、解不开，故只有显式打开它才从微信 CDN
    取件。**不开则一次网络请求都不发**（`SECURITY.md` 的零网络承诺）。
    """
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
    index_note = None
    kinds_to_export = list(kinds)
    with contextlib.ExitStack() as stack:
        con = None
        if "video" in kinds or "sticker" in kinds:
            # 只有视频与贴纸的**归属**需要索引（图片按目录枚举、语音走 media_*.db）：
            # 不导这两类的调用不该多背一条失败路径。
            try:
                con = stack.enter_context(store.open())
            except (errors.AgentWeChatError, sqlite3.Error, OSError) as exc:
                # 索引打不开 → **不导视频**。绝不能退回 `con=None` 那条账号级枚举：它会把
                # 账号级视频树里的全部文件算到本次请求的会话头上（错归属，比不导出更坏——
                # 本任务修的就是这个缺陷）。其余类型照常导。
                index_note = errors.redact(str(exc))
                kinds_to_export = [k for k in kinds if k not in ("video", "sticker")]
        report = media.export_media(ctx.account, [chat_id], kinds=kinds_to_export, months=months,
                                    out_dir=out_dir,
                                    max_files=clamp_limit(limit),   # 不再硬卡 2000
                                    force=force,
                                    con=con, with_cdn=with_cdn)
    if index_note is not None:
        # 中文名与 kind 名分开查表：拿中文首字去 match 英文 kind 名会**恒不命中**，
        # 印出「未导出任何——」这种半截话（写完当场踩过一次）。
        dropped = "、".join(zh for en, zh in (("video", "视频"), ("sticker", "贴纸"))
                            if en in kinds)
        report.degraded.append(
            f"索引库不可用（{index_note}）：本次未导出任何{dropped}——它们的归属按 media_refs "
            "索引，没有索引时无从判断某条属于哪个会话")
    return MediaExportResult(chat_id=chat_id, out_dir=report.out_dir, report=report)


LIMIT_MEDIA = (1, 500)

_VOICE_TYPE = messages.TYPE_VOICE
# `message_media` 答的是「这些 msg_type 的消息算媒体」——贴纸在列（它有类型、有身份）。
# **与 `MEDIA_KINDS` 是两回事**：那个是「能导出什么」的清单（贴纸现在**在**其中，
# 但它的字节来源默认关）。漏掉下面这一项，贴纸消息会被 SQL 整条滤掉，
# `_KIND_OF_TYPE[47]` 就成了静默 no-op。
_MEDIA_TYPES = (messages.TYPE_IMAGE, messages.TYPE_VIDEO, messages.TYPE_VOICE,
                messages.TYPE_STICKER)


def _voice_keys_for(account: str) -> frozenset[tuple[str, int]] | None:
    """语音本体存在集合。**测试接缝**：替换它以免测试真去开 media_*.db。

    写成函数而不是 `_voice_keys_for = media.voice_body_keys`：模块级取属性会把**测试接缝**
    钉死在 import 期（替换模块属性不再生效）。生产路径上就是 `media.voice_body_keys`
    （media 已顶层 import，见 EA-090）。
    """
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
                 messages.TYPE_VOICE: "voice",
                 messages.TYPE_STICKER: "sticker"}


def _state_of(row: dict, ref: tuple[str, str | None] | None, *,
              out_root: pathlib.Path, account: str,
              voice_keys: frozenset | None) -> MediaRefState:
    """单条消息的四态判定。顺序严格按 spec §4.1：none → exported → exportable → gone。"""
    # none 只由 msg_type 判（索引内实测有 259 条媒体消息拿不到 ref）：ref 只提供 file_key
    kind = _KIND_OF_TYPE.get(row["msg_type"])
    if kind is None:
        return MediaRefState(row["msg_key"], row["ts"], None, "none", None)
    file_key = ref[1] if ref else None
    local_id = row["local_id"]
    p = media.exported_path(out_root, chat_md5=row["chat_md5"], kind=kind,
                            ts=row["ts"], file_key=file_key, local_id=local_id)
    if p is not None and not media.is_thumbnail(p):
        # EA-036：命中的可能是**视频缩略图**（`{stem}_thumb.jpg`）——它不是本体，报
        # `exported` 会让渲染端产出指向 jpg 的 `<video>`（坏播放器）。四态契约**不加第五态**：
        # 缩略图命中时按「本体不在」继续往下判（`exportable` / `gone`）。
        return MediaRefState(row["msg_key"], row["ts"], kind, "exported", str(p))
    if kind == "voice":
        # 语音库读不到（None）时不能当成空集合：那会把「读不到」谎报成「本机没有」
        present = voice_keys is not None and (row["chat_md5"], local_id) in voice_keys
    elif kind == "image":
        present = file_key is not None and media.resolve_image(
            account, row["chat_md5"], row["ts"], file_key) is not None
    elif kind == "sticker":
        # **恒不进 exportable，只可能是 gone**（`exported` 由上面那一步判，与 kind 无关）。
        #
        # 本地那份是**解不开的密文**（两轮 spike 均未破，spec 附录 C/E.4），且实测是**缩略档**；
        # 唯一的字节来源是 CDN 取件，而它**默认关**、单条成功率也不可预知（服务端可能删件）。
        # `exportable` 的文档语义是「有产物可拿、你还没导出」——对贴纸产出它就是谎报。
        #
        # **这不是「本机没有」**：本机有多少条贴纸真的有文件，实测口径不同差异极大
        # （38.3% vs 按 md5 去重 8.6%），所以本状态没有可依据的存在性信号；措辞只能描述
        # 微信侧的存储格式——渲染端为此单列了 `chat_text.STICKER_GONE`。
        #
        # **不要把它「修」成 `media.resolve_sticker(...)`**：那个定位函数只回答「本机有没有
        # 那份**加密副本**」，而那不是可拿的产物——判出 `exportable` 会让用户以为有东西可拿。
        # （它现在的用途只有一个：`_export_stickers` 判 degraded 文案时说得准。）
        present = False
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

    **调用方仍必须先校验格式**（`message_media` 里的 `bad_months`）：要的是那句点名的
    「月份格式应为 YYYY-MM：`2026-13`」——用户看得见是**哪个**字符串写错了。
    `media.month_bounds` 对**形状**仍不校验，但它已经不再把 `datetime` 的 `ValueError`
    漏出去（越界的年份会翻成 `SearchInputError`，见该函数）；本函数不再背
    「穿透 core 层」这个理由，只背文案。
    """
    if not months:
        return "", []
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

    keys=None → 该会话**所有媒体类型的消息**（msg_type ∈ {3,43,34,47}），受 limit 约束
    ——按 msg_type 而非按 ref 筛选：索引内实测有 259 条媒体消息拿不到 ref，
    按 ref 筛会把它们谎报成「没有媒体」。给了 keys → 只答这些，查不到的静默略过。
    months 在 SQL 里筛（不是取回后再滤）：limit 卡的必须是**命中月份**的条数。
    truncated=True 表示 limit 截断了答案，别把它当成「那个月没有媒体」。
    """
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
    out_root = (pathlib.Path(out_dir) if out_dir else media.export_root()).resolve()
    lim = clamp_limit(limit, LIMIT_MEDIA)
    month_sql, month_args = _month_clause(months)
    degraded: list[Degradation] = []

    with contextlib.ExitStack() as stack:
        try:
            con = _con if _con is not None else stack.enter_context(store.open())
            if keys is not None:
                # 分批查（与 store.media_refs_by_keys 同一批大小）：`keys` 是调用方给的，
                # 一条 `IN (?,…)` 塞下几千个 key 时抛的 OperationalError 会以裸 sqlite3
                # 异常逃出 core 层（错误契约见 errors.py 开篇）。批内先去重——同一个 key
                # 落在两个块里会答出两行，而单条语句的 `IN` 天然只答一行。
                # `chat_md5` 一滤不能少：keys 可能来自别的会话——不加就会把别人的媒体
                # 冠上本次解析出的 chat_id 报出去。
                uniq = list(dict.fromkeys(keys))
                rows = []
                for i in range(0, len(uniq), store.KEYS_BATCH):
                    chunk = uniq[i:i + store.KEYS_BATCH]
                    placeholders = ",".join("?" * len(chunk))
                    sql = (f"SELECT {messages.column_list(*messages.COLUMNS, prefix='m.')} "
                           f"FROM messages m WHERE m.msg_key IN ({placeholders}) "
                           f"AND m.chat_md5 = ?{month_sql} ORDER BY m.ts DESC")
                    rows += [dict(zip(messages.COLUMNS, r))
                             for r in con.execute(sql, [*chunk, cm, *month_args])]
                # 各块各自有序，拼起来不是全局有序：这里补一次排序，与单条语句的
                # `ORDER BY m.ts DESC` 等价（limit 截断取的必须是**最新**的那些）。
                rows.sort(key=lambda r: r["ts"], reverse=True)
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
        except (sqlite3.Error, OSError) as exc:
            # 索引库打不开/读不了（损坏、半拷贝的 WAL、权限）：**不照抄 media_export 的降级**
            # ——那条路降级成「本次不导视频」还有意义，这里没有索引就什么都答不了，一个零
            # 只会把「库坏了」谎报成「这个会话没有媒体」。重新抛成项目异常，让入口层按既有
            # 的翻译路径给出单行中文（CLI）/ 错误信封（MCP），而不是一整个 traceback。
            raise errors.IndexUnavailable(
                f"本地检索库不可用：{errors.redact(str(exc))}",
                hint="索引库可能损坏；删除 index.db 后重跑一次导出即可重建"
            ) from exc

    # 语音本体集合**按需**取：这个会话一条语音都没有时，不值得去开 media_*.db
    voice_keys = None
    if any(r["msg_type"] == _VOICE_TYPE for r in rows):
        voice_keys = _voice_keys_for(ctx.account)
        if voice_keys is None:
            degraded.append(Degradation(
                capability="msg_media", db="message/media_*.db", table="VoiceInfo",
                missing_columns=(), drift=False,     # F7：「读不到」不是「结构不符」
                # 措辞必须限定在「尚未导出」的那些：导出判定排在语音判定**之前**，已经
                # 落盘的语音仍会照实报 exported——写「一律」会被同一份结果当场反驳。
                effect="语音库读不到：尚未导出的语音消息一律按「本体不在本机」呈现"
                       "（已导出的不受影响）"))

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


def _decode_extra(msg_type: int, payload: bytes) -> "msgextra.Extra | None":
    """解压 + 解析一条 `msg_extras` 载荷（EA-041）。**对任意 bytes 全定义、不抛**——与
    `_decode_part` 同一条理由：`payload` 是别人写进去的字节，裸 `zlib.error` /
    `UnicodeDecodeError` 不得逃出 core 层；而 `msgextra.parse` 自己就对任意 `str` 全定义。

    两者的失败面**合并处置**（返回 None）：这一族没有 `unparsed` / `rejected` 的计数
    （`msg_parts` 那边的分母是 spec §5 定的，48/42 不参与），消费点只问「这条有没有结构化
    载荷」。解不开 = 没有。

    `chat_export` 与 `message_parts` 共用这一份——同一个解码两处实现，迟早漂移成
    「同一张表在两个入口一个显示一个不显示」（`_decode_part` 的 docstring 记着这条）。
    """
    try:
        raw = zlib.decompress(payload).decode("utf-8", "ignore")
    except (zlib.error, UnicodeDecodeError):
        return None
    # 类型由库里那一列给出；这里不信任它是不是 48/42——`parse` 自己会挡（不在族里返回 None）。
    return None if not raw else msgextra.parse(msg_type, raw)


def _decode_part(raw_xml: bytes) -> tuple["appmsg.AppMsg | None", str, int]:
    """解压 + 解析一条 `msg_parts` 载荷 → `(AppMsg | None, 结果, 剔掉的不透明块数)`。

    结果是三者之一：`"ok"` / `"unparsed"`（解压失败或 XML 解不开）/ `"rejected"`（命中危险
    载荷守卫）。**对任意 bytes 全定义、不抛**：`raw_xml` 是**别人写进去**的字节（索引库可被
    手工改动，半拷贝的 WAL 会落下半页），裸 `zlib.error` 不得逃出 core 层；而 `appmsg.parse`
    自己就对任意 `str` 全定义。

    第三项**随 `appmsg.parse` 的计数一起出来**（`ParseResult.dropped_blobs`），不另解一次：
    解不开 / 被拒的那两条路径恒为 0（它们在字段过滤之前就早退了），故与调用方的分支无关。

    **`chat_export` 与 `message_parts` 共用这一份**（丙 R21：同一个解码两处实现，迟早漂移成
    「`unparsed` 与 `rejected` 在两个入口口径不同」）。两处都按同一个「解压失败与解不开合
    计 `unparsed`、主动拒绝单独计 `rejected`」的分工（spec §5）。
    """
    try:
        raw = zlib.decompress(raw_xml).decode("utf-8", "ignore")
    except zlib.error:
        return None, "unparsed", 0
    res = appmsg.parse(raw)
    if res.reason == "rejected":
        return None, "rejected", 0
    if res.reason is not None:            # 'malformed'
        return None, "unparsed", 0
    return res.appmsg, "ok", res.dropped_blobs


@dataclass(frozen=True)
class ChatExportResult:
    """chat_export 的成功返回（CLI 与 MCP 共用）。

    `out_dir` 是**本会话的媒体树根**（`{--out}/{chat_md5}`，也就是 `view.out_dir`）——
    产物在它的 `chat/` 子目录里。刻意不回传输出**根**：那需要在这里（或入口层）重算一遍
    `chat_md5`，正是「两套路径推导」的开端，而两套推导的分歧就是产物里所有媒体引用的
    静默 404（见 `chatview.write` 的模块注释）。
    """

    chat_id: str
    out_dir: str
    files: tuple[str, ...]          # 真正落盘的产物；写失败的不在列表里
    counts: dict[str, int]          # kind → 条数
    truncated: bool
    unparsed: int
    rejected: int
    degraded: tuple[Degradation, ...] = ()


def chat_export(contact: str, *, months: list[str] | None = None, limit: int = 500,
                formats: Sequence[str] = ("html", "md"), out_dir: str | None = None,
                account: str | None = None) -> ChatExportResult | NeedConfirm:
    """导出会话为可读时间线（HTML / Markdown）。产物落 `{--out}/{chat_md5}/chat/index.*`。

    `months` 复用 `media.month_bounds` 与同一套校验：格式不符、**非升序**、**不连续**
    三者一律抛 `SearchInputError`（既不静默 0 命中，也不静默放大或清空窗口）。
    升序且连续的月份取 `[lo, hi)` 包络，逐月拼起来无缝隙（`month_bounds` 相邻月首尾相接）。
    两条例外各自的理由：非连续月份只能取包络、会把中间的月份也静默带进来；
    而反序会让窗口取反——反序两个月退化成空区间 `[feb_lo, feb_lo)`、三个月以上直接倒置
    （`after > before`），`messages_in_range` 两种情况都返回 **0 行且 `truncated=False`**，
    即一个**没有任何信号的空产物**。
    `limit` 封顶的是范围内**最近的** N 条（spec §4.4）。
    **`--out` 必须与 `media export` 用同一个根**（spec 裁定 13）：媒体是以
    `../image/…` 的相对引用指向**同级**的既有媒体树，换个根就全是死链。

    标题（`view.title`）走既有的 `_display_or_id` —— 与 `recent` / `search` 同一个来源，
    contact 表漂移时回落 `chat_id`（漂移随 degraded 出去）。
    """
    ctx = ChatContext(account)
    chat_id = ctx.resolve(contact)
    if isinstance(chat_id, NeedConfirm):
        return chat_id
    bad_months = [m for m in (months or []) if not MONTH_RE.fullmatch(m)]
    if bad_months:
        raise errors.SearchInputError(f"月份格式应为 YYYY-MM：{', '.join(bad_months)}")
    after_ts = before_ts = None
    if months:
        bounds = media.month_bounds(months)
        # **必须「升序 + 连续」**：`messages_in_range` 只有一对边界，非连续月份只能取包络——
        # `2026-08,2026-10` 会把 9 月也带进来，而 `limit` / `truncated` 还按放大后的窗口算，
        # 产物上完全看不出多导了。静默放大窗口比报错更坏（同「月份格式不符不静默 0 命中」）。
        # 兄弟命令 `media export` / `message_media` 是逐月 OR，非连续月份能被正确表达 ——
        # 那两处**不动**（同一面 flag 语义不同是刻意的：那里是筛选，这里是单一时间窗）。
        # **升序与连续是两个必要条件，缺一不可**——校验与取窗口必须用**同一份** `bounds`。
        # 不能简化成「先排序再判集合是否连续」：窗口取的是 `bounds[0][0]` 与 `bounds[-1][1]`，
        # 排序只会**掩盖反序**，而反序实测（用 fixture 里的 ts 算过）拿到的是退化/倒置的窗口
        # ——`['1970-02','1970-01']` 得到 `[feb_lo, feb_lo)`（空区间）、
        # `['1970-03','1970-01']` 得到 `after > before`——`messages_in_range` 两种情况都返回
        # **0 行且 truncated=False**，用户拿到的是一个没有任何信号的空产物。
        if any(cur[0] != prev[1] for prev, cur in zip(bounds, bounds[1:])):
            raise errors.SearchInputError(
                f"--months 必须是一段**按时间升序**的连续月份（相邻月首尾相接）："
                f"{', '.join(months)}"
                "；非连续月份只能取包络、会把中间的月份也静默带进来，而反序会得到倒置"
                "（或退化成空区间）的窗口——0 行、truncated 还报 False：两者都会静默产出空产物")
        after_ts, before_ts = bounds[0][0], bounds[-1][1]
    # 输出根绝对化：`--out 相对路径` 时 `view.out_dir` 是相对的，而媒体状态的路径是绝对的
    # （`message_media` 自己 resolve 过），`chat_text.media_src` 用 `os.path.relpath` 算
    # 相对引用时按 **cwd** 展开相对的那一侧 —— 产物的 `<img>` 照常渲染、打不开
    # （静默 404）。与 `message_media` 同款处理。
    out_root = (pathlib.Path(out_dir) if out_dir else media.export_root()).resolve()
    try:
        rows, truncated = search.messages_in_range(
            chat_id, account=ctx.account, after_ts=after_ts, before_ts=before_ts,
            limit=clamp_limit(limit))
        keys = [r["msg_key"] for r in rows]
        with store.open() as con:
            got = store.msg_parts_by_keys(con, keys)
            # 「回填过没有」**只能**由表的状态回答：`appmsg rebuild` 只写 msg_parts、不写
            # messages，join 出来的条数会把「没回填」与「这些消息本来就没有可读内容」混成
            # 一种（丙 R23 的教训——两者在产物里长得一模一样）。
            backfilled = store.count_msg_parts(con, ctx.account)
            # EA-041 的位置/名片：**与 parts 同一趟、同一个 keys 集合**读出原始载荷，
            # 解析在库外做（与 parts 的分工一致：store 只管字节，解析归用例层）。
            extras_raw = store.extras_by_keys(con, keys)
    except (sqlite3.Error, OSError) as exc:
        # 裸 sqlite3/OSError 会穿透 core 层（CLI 印 traceback、MCP 落 generic_payload 兜底）。
        # 索引读不了就是什么都答不了：一个空产物只会把「库坏了」谎报成「这个会话没有消息」。
        raise errors.IndexUnavailable(
            f"本地检索库不可用：{errors.redact(str(exc))}",
            hint="索引库可能损坏；删除 index.db 后重跑一次导出即可重建"
        ) from exc

    # 媒体：直接复用丙的 message_media，`states` 就是 `MediaRefState`。`_ctx=ctx` 复用刚解析
    # 好的会话（省一次 contact.db 解密），`contact` 仍按原样传（入口签名不变）。
    # `limit` 跟着 keys 走（message_media 自己封顶 500，所以 keys 超过 500 条时它会只答
    # 最近的那 500 个 key）——没答到的 key 在产物里与「本来就没有媒体」一模一样，
    # 故下面按**答回的条数**公告（判据见 degraded 那一段，`truncated` 在 keys 路径上不适用）。
    mm = message_media(contact, keys=keys, out_dir=str(out_root),
                       limit=len(keys) or 1, _ctx=ctx)
    states = {s.msg_key: s for s in mm.states}

    # 会话根：chat_md5 取**行里那个**（`messages.chat_md5` 列就是 Msg_<md5> 表的 md5 部分，
    # 也是 msg_key 的第二段）；范围内没有消息时回落到 `messages.chat_md5(chat_id)`。
    chat_md5 = rows[0]["chat_md5"] if rows else messages.chat_md5(chat_id)

    # 四元组的第一、二项（`appmsg_type` / `ref_svrid`）**刻意不用**：它们是 raw_xml 的投影，
    # 存在的理由是「不必解压就能筛选 / 重解析」，而这里已经把 XML 解开了 —— 用解析结果更权威。
    parts: dict[str, appmsg.AppMsg] = {}
    refs: dict[str, str] = {}
    unparsed = rejected = 0
    for key, (_appmsg_type, _ref_svrid, ref_key, raw_xml) in got.items():
        if ref_key:
            refs[key] = ref_key       # `None` 的不进表：build 靠「在不在表里」判退化
        # 解压 + 解析是**共用的一份**（`_decode_part`，`message_parts` 用的是同一个）：
        # 两处各写一遍时，「解压失败算哪一种」这种口径迟早分叉。
        # 第三项（剔掉的不透明块数）导出侧**不消费**：本任务的消费点只有 MCP 信封
        # （`MessagePartsResult.dropped_blobs`），产物侧不新开计数（那是另一处入口的渲染决定，
        # 不是「没人读的死字段」——计数在这里照样算得出来，只是本期没人接）。
        parsed, outcome, _dropped = _decode_part(raw_xml)
        if outcome == "rejected":
            rejected += 1             # 我们主动拒绝（DTD / 实体），与「解不开」分开计数
        elif outcome == "unparsed":
            unparsed += 1
        elif parsed is not None:
            parts[key] = parsed

    # 位置/名片：**空表是常态**（老库、没跑过 `extras rebuild` 的机器），两个模板因此一行
    # 都不印——不是印「未知」。**不发降级**：这一族没有 `msg_parts` 那种「回填过没有」的
    # 可判定状态（「表是空的」与「这些消息本来就没有位置/名片」在库里分不开），公告一句
    # 「可能没回填」会在绝大多数会话上是假警（丙 R23：两者在产物里长得一样，那就别猜）。
    extras: dict[str, msgextra.Extra] = {}
    for key, (msg_type, payload) in extras_raw.items():
        x = _decode_extra(msg_type, payload)
        if x is not None:
            extras[key] = x

    degraded: list[Degradation] = []
    if not backfilled:
        # 与「这些消息本来就没有可读内容」**必须印得不一样**：前者可以补救（跑一次回填），
        # 后者是数据本身就长这样（丙 R23）。复用 Degradation 信封，不新造并行类型。
        degraded.append(Degradation(
            capability="msg_parts", db="index.db", table="msg_parts", missing_columns=(),
            effect="msg_parts 未回填，appmsg 会显示成 `[appmsg: 标题]`；"
                   "运行 `agent-wechat appmsg rebuild` 补齐",
            drift=False))
    if len(states) < len(keys):
        # 判据是**答回的条数**，不是 `mm.truncated`：keys 路径下 message_media 是「一个 key
        # 一行」（非媒体消息也在里面、状态为 `none`），于是 `truncated`（= `len(rows) >= lim`）
        # 在「limit 恰好等于 key 数」时恒为真 —— 照它公告就是每次导出都报一次假警
        # （真机实测：`--limit 200` 时 states 与 keys 等长，却报「可能被截断」）。
        # 而「答回得比 keys 少」正是产物里会**静默少一块**的那个条件。
        degraded.append(Degradation(
            capability="msg_media", db="index.db", table="messages", missing_columns=(),
            drift=False,
            effect=f"媒体状态只覆盖了 {len(states)}/{len(keys)} 条消息"
                   "（message_media 的 limit 上限 500）：没覆盖到的消息在产物里不会出现"
                   "媒体块"))

    view = chatview.build(
        chat_id, title=_display_or_id(ctx, chat_id), rows=rows, parts=parts, media=states,
        out_dir=str(out_root / chat_md5), truncated=truncated, unparsed=unparsed,
        rejected=rejected,
        # 富化（取展示名）**先**做，降级**后**收：`_display_or_id` 回落 chat_id 的那次
        # SchemaDrift 记在 `ctx.reader._drift` 上，这个顺序才让它随 degraded 出去
        # （`_display_or_id` 的注释把这条顺序写成约定）。
        degraded=tuple(degraded) + collect_degradations(ctx.reader), refs=refs,
        extras=extras)
    files = chatview.write(view, formats=formats)
    return ChatExportResult(
        chat_id=chat_id, out_dir=view.out_dir, files=tuple(str(p) for p in files),
        counts=view.counts, truncated=truncated, unparsed=unparsed, rejected=rejected,
        degraded=view.degraded)


@dataclass(frozen=True)
class ExtraPart:
    """一条位置/名片的已解析载荷（EA-041）。形状与 `MessagePart` 平行但**不带 `AppMsg`**。"""
    msg_key: str
    extra: msgextra.Extra


@dataclass(frozen=True)
class MessagePart:
    """一条消息的 appmsg 内容：`msg_key` + **已解析**的 `AppMsg` + 回指目标。

    `appmsg` 给已解析的 `AppMsg`（不是 `msg_parts_by_keys` 的原始四元组）：原始形态
    （zlib 压缩的 XML）只该在 `store`/用例层出现，入口拿到的必须是能直接渲染的东西。
    `ref_msg_key` 是回填那一刻的**快照**，解不到为 `None`（不是空串）。
    """

    msg_key: str
    appmsg: appmsg.AppMsg
    ref_msg_key: str | None


@dataclass(frozen=True)
class MessagePartsResult:
    """`message_parts` 的结果。

    `unparsed` / `rejected` 的**口径与 `chat_export` 逐字同源**（共用 `_decode_part`）：
    解压失败与 XML 解不开合计 `unparsed`，命中危险载荷守卫计 `rejected`，**两者分开**
    （spec §5：『我们主动拒绝』与『解不开』是两件事）。

    `dropped_blobs` 是**第三个**计数，与上两个**不许并**：它数的是**解析成功**的载荷里被
    形状规则剔掉的长 Base64 块（`appmsg._is_opaque_blob`）。并进 `unparsed` 会让「载荷里
    有个大 blob」看起来像「这条消息读不出来」——两者的严重程度与可操作性都不同。**它与
    `unparsed` / `rejected` 同为 0 时才是「字段表就是全部」**。

    `backfilled` 是「这个账号回填过没有」这个**事实**，由 `count_msg_parts` 回答
    （不 join `messages`）。入口靠它把「没回填」与「这些消息本来就没有可读内容」分开——
    两种「空答案」不许长得一样（丙 R23）。**它只承载事实，不带文案**：具体给用户看什么
    （MCP 的 `hint` 与 `degraded`）是入口层的渲染决定，同 `search_history` 的 hint。

    **刻意没有 `chat_id` 字段**（对照 `MessageMediaResult.chat_id` 有真消费者）：本工具的
    信封按 spec 不含 `chat_id`、CLI 侧也没有命令消费它，加了就是没人读的死字段（审阅 F73）。
    （`dropped_blobs` 与它不同：MCP 信封读它，故不是死字段。）
    """

    parts: tuple[MessagePart, ...]
    unparsed: int
    rejected: int
    dropped_blobs: int
    backfilled: bool
    #: EA-041：位置（48）/ 名片（42）的结构化载荷，按 `keys` 保序。**与 `parts` 分开一个
    #: 字段**——它们的形状不同（没有 quote/link/file/transfer），并进 `parts` 会让每个消费者
    #: 都要判「这个 AppMsg 到底是不是 appmsg」。空表是常态（没跑过 `extras rebuild`）。
    #: `extras` 这一族**没有 `backfilled` 那样的账号级状态**：表空与「这些消息本来就没有
    #: 位置/名片」在库里分不开，故不设对应的降级位（与 `chat_export` 那边同一决定）。
    extras: tuple["ExtraPart", ...] = ()


def message_parts(contact: str, *, keys: Sequence[str],
                  account: str | None = None) -> MessagePartsResult | NeedConfirm:
    """消息 → appmsg 结构化内容（spec §4.5 的 `get_message_parts`）。

    `keys` 只答**属于该会话**的：`msg_key = account|chat_md5|shard|local_id`，第二段对不上
    的一律静默略过——与 `message_media` 的 keys 路径**同一条约定**（那里的注释：`chat_md5`
    一滤不能少，否则别的会话的内容会被冠上本次解析出的 `chat_id` 报出去）。`msg_parts` 里
    没有的 key 也不出现在结果里。返回顺序按 `keys`（去重后、保序）。

    **判据**：`parts` 为空**不等于**「这个会话没有 appmsg」——`backfilled=False` 时全部
    appmsg 都取不到，入口必须把这两件事分开说（丙 R23）。

    索引库读不了抛 `errors.IndexUnavailable`（不是空结果）：一个空 `parts` 只会把「库坏了」
    谎报成「这些消息没有可读内容」——与 `message_media` 同款翻译。
    """
    ctx = ChatContext(account)
    chat_id = ctx.resolve(contact)
    if isinstance(chat_id, NeedConfirm):
        return chat_id
    prefix = messages.msg_key_prefix(ctx.account, messages.chat_md5(chat_id))
    # 去重保序在**这里**做（与 `message_media` 的 `list(dict.fromkeys(keys))` 同位置）：
    # 入口只管把逗号串拆成 list，归属过滤与去重是入口策略。
    wanted = list(dict.fromkeys(k for k in keys if k.startswith(prefix)))
    try:
        with store.open() as con:
            got = store.msg_parts_by_keys(con, wanted)
            # EA-041：与 parts 同一趟、同一个 wanted 集合读出原始载荷，解析在库外做。
            extras_raw = store.extras_by_keys(con, wanted)
            # 「回填过没有」只能由表的状态回答（`count_msg_parts` 不 join messages）：
            # join 出来的条数会把「未回填」与「本来就没有可读内容」混成一种（丙 R23）。
            # 存成**布尔**（不是条数）：入口要的是「回填过没有」这一个判断，条数没人用，
            # 而一个叫 `backfilled` 的 int 字段读起来像布尔、用起来像计数（两边都骗）。
            backfilled = store.count_msg_parts(con, ctx.account) > 0
    except (sqlite3.Error, OSError) as exc:
        raise errors.IndexUnavailable(
            f"本地检索库不可用：{errors.redact(str(exc))}",
            hint="索引库可能损坏；删除 index.db 后重跑一次导出即可重建"
        ) from exc
    out: list[MessagePart] = []
    unparsed = rejected = dropped_blobs = 0
    for key in wanted:
        row = got.get(key)
        if row is None:               # 不是 appmsg / 没导出：两种情形在这里不可分
            continue
        parsed, outcome, blobs = _decode_part(row[3])
        # **无条件累加**：`rejected` / `unparsed` 两条路径的第三项恒为 0（`_decode_part` 的
        # 早退），所以不必按 outcome 分支——分了反而是把「计数口径」和「成功与否」绑在一起。
        dropped_blobs += blobs
        if outcome == "rejected":
            rejected += 1
        elif outcome == "unparsed":
            unparsed += 1
        elif parsed is not None:
            out.append(MessagePart(key, parsed, row[2]))   # 四元组的第三项是 ref_msg_key
    # 位置/名片：**按 `wanted` 保序**（与 `parts` 同一个序），解析失败的静默略过
    # （「解不开」与「没有」在这一族不可分，理由见 `_decode_extra`）。
    extras = tuple(ExtraPart(key, x) for key in wanted
                   if (row2 := extras_raw.get(key)) is not None
                   and (x := _decode_extra(row2[0], row2[1])) is not None)
    return MessagePartsResult(parts=tuple(out), unparsed=unparsed, rejected=rejected,
                              dropped_blobs=dropped_blobs, backfilled=backfilled,
                              extras=extras)


@dataclass(frozen=True)
class ChatIndexResult:
    """chat_index 的返回（当前只有 CLI 入口消费）。

    `file is None` = **一个文件都没写**：`{--out}` 下一个已导出的会话都没有。这不是错误
    （用户还没导过），但**也不写一张空壳页面**——「共 0 个会话」的页面与「索引页坏了」
    在用户眼里一模一样，空产物比没有产物更难分辨。

    `degraded` **今天没有生产者，恒为 `()`**：本用例没有任何降级来源（不构造 reader，索引
    读不了是抛 `IndexUnavailable` 而不是降级；「索引里查不到某个会话」是逐行呈现）。保留它是
    因为它是四个结果 dataclass 的**统一信封形状**——CLI/MCP 对所有结果一视同仁地读它。
    """

    out_dir: str
    file: str | None
    chats: int
    degraded: tuple[Degradation, ...] = ()


def chat_index(out_dir: str | None = None, account: str | None = None) -> ChatIndexResult:
    """把 `{--out}` 下**所有已导出的会话**列成一页 `{--out}/index.html`。

    枚举 `{out}/*/chat/index.html`（T9 的落盘布局，`chat_md5` 就是那个目录名），
    会话名与条数**一次查库**取回（`GROUP BY chat_md5`；逐会话一条 SQL 在真机 772 个
    会话上是 772 次往返）。索引里查不到的会话（导出之后重建过索引 / `purge --index`）
    回落成 `messages.degraded_chat_id` 的 `md5:{chat_md5}` 形态——**不自造第三种写法**，
    那个函数存在的理由就是生产端与解析端共用一种形态。

    **枚举先于查库、也先于账号解析**：没有产物就没有要查的名字，`chat index` 在
    「还没导出过任何会话」时必须是零依赖可用的（不碰微信目录、不碰索引库）。

    会话名**只用索引库里的 `chat_id`**，不取展示名：`reader._contacts_all_cached()` 是
    `contacts(limit=5000)` 的 5000 行窗口，窗口外的会话会静默回落裸 id（真机 772 个会话
    只有 18% 在窗口内）——那条已 park，别在这里顺手接上。
    """
    # 与 `chat_export` 同款绝对化：`--out` 给相对路径时，`out_dir`/`file` 才是能直接打开、
    # 也能与 `chat export` 对齐的路径。
    out_root = (pathlib.Path(out_dir) if out_dir else media.export_root()).resolve()
    # EA-035：枚举 `index.*` 后按 `{chat_md5}` **归并**——此前只枚举 `index.html`，
    # 于是用 `--format md` 导出的会话**永远不出现在索引页上**（`has_md` 只覆盖反方向）。
    # 排序让页面顺序不依赖文件系统。
    found: dict[str, set[str]] = {}
    for p in out_root.glob("*/chat/index.*"):
        # 白名单：只认**真的两种产物名**（复审 #3）——`index.txt` / `index.md.bak` 这类
        # 外部文件会让条目带一条**必死链**，而 `has_md` 存在的全部意义就是「不印死链」。
        if p.name in ("index.html", "index.md"):
            found.setdefault(p.parent.parent.name, set()).add(p.name)
    if not found:
        return ChatIndexResult(out_dir=str(out_root), file=None, chats=0)
    ctx = ChatContext(account)
    try:
        with store.open() as con:
            rows = con.execute(
                "SELECT chat_md5, chat_id, count(*) AS n FROM messages WHERE account=?"
                " GROUP BY chat_md5", (ctx.account,)).fetchall()
    except (sqlite3.Error, OSError) as exc:
        # 与 chat_export 同一口径：索引读不了就什么都答不了，且裸异常不得逃出 core 层。
        # 退回「没有已导出的会话」是**谎报**——产物明明在，用户会以为导出白做了。
        raise errors.IndexUnavailable(
            f"本地检索库不可用：{errors.redact(str(exc))}",
            hint="索引库可能损坏；删除 index.db 后重跑一次导出即可重建"
        ) from exc
    indexed = {r["chat_md5"]: r for r in rows}
    entries = []
    for chat_md5, names in sorted(found.items()):   # {out}/{chat_md5}/chat/index.*
        row = indexed.get(chat_md5)
        entries.append(index_html.ChatIndexEntry(
            chat_md5=chat_md5,
            chat_id=row["chat_id"] if row else "",
            label=row["chat_id"] if row else messages.degraded_chat_id(chat_md5),
            messages=row["n"] if row else None,  # 查不到时**未知**：`None`，不是 0 条
            has_md="index.md" in names,
            has_html="index.html" in names))
    target = media.ensure_tree(out_root) / "index.html"
    try:
        target.write_text(index_html.render(entries), encoding="utf-8")
    except OSError as exc:
        # 单个产物写失败**不吞**（与 `chatview.write` 的多产物取舍不同）：这里只有一个文件，
        # 吞掉就是「退出码 0、什么都没有」，而索引页正是用户唯一的入口（同 moments_export）。
        raise errors.MediaOutputError(f"写产物失败：{target}") from exc
    try:
        target.chmod(0o600)                   # 尽力而为：权限没收紧不该让产物作废
    except OSError:
        pass
    return ChatIndexResult(out_dir=str(out_root), file=str(target), chats=len(entries))


def purge_chat_targets(contact: str, account: str | None = None) -> list[dict] | NeedConfirm:
    """会话名 → 每个**索引里（或排除清单里）有它**的账号各一条 `{account, chat_id}`（EA-092：无 `chat_md5`）。

    **空集也回一条**（`account` 为空串，见下），所以调用方拿到的列表恒非空：会话名解析
    不出会抛 `ContactNotFound`、有歧义会回 `NeedConfirm`，两者都不走这条路。

    `chat_md5` 在**函数内部**用它查排除清单（**`messages.chat_md5()`**，不是 `messages.md5_of()`——后者对退化形态
    （`md5:<hex>`，`degraded_chat_id` 的产物）会**再哈希一次**，得到一个错的 md5。
    错法是静默的——`media/<chat_md5>/` 指向不存在的目录、导出物删不掉，而核查会报
    「0 行、目录不存在」一片 ✅（spec §5.1 第 1 步、终审 D1）。返回给调用方的**不含**
    `chat_md5`（按 `purge_chat_targets` 的返回契约——消费者要 md5 自行派生，EA-092）。

    跨账号：隐私不是按账号分的，凡是索引里有这个会话的账号都要删；`chat_md5` 与账号无关，
    所以 `media/<chat_md5>/` 天然是共享的。

    **`exclusions.excluded` 那一支不能省**：`--undo` 的存在理由就是「一次误删之后」
    （spec §5.6），而那时这个会话在 `messages` 里**一条都不剩**——只按 messages 找会话的话，
    撤销会拿到空清单、报一句「没有可删的东西」并退 0，墓碑原样留着，而用户以为已经撤销。
    这一支不会留下删不掉的残留：`--undo` 把这批墓碑删干净之后，这个账号下一次就不在并集里了。

    **`index.db` 不存在时一个字节都不许建**：`store.open()`（→ `store.connect()` →
    `executescript(SCHEMA)`）会把刚被 `purge --index` 删掉的索引连 schema 一起建回来，
    而 `--undo` 恰恰是紧跟销毁之后跑的那条命令——它会**静默撤销那次索引 purge**。
    索引库不在 ≡ 没有活消息（那一支本来就没有行可给），所以跳过它、只查排除清单即可；
    那一支走 `exclusions.standalone()`（`:memory:` + ATTACH），同样不落文件。
    """
    chat_id = ChatContext(account).resolve_optional(contact)
    if isinstance(chat_id, NeedConfirm):
        return chat_id
    if chat_id is None:
        raise errors.ContactNotFound(f"找不到会话「{contact}」")
    md5 = messages.chat_md5(chat_id)
    try:
        if store.DB_PATH.exists():
            with store.open() as con:
                rows = con.execute(
                    "SELECT account FROM messages WHERE chat_id=? "
                    "UNION SELECT account FROM exclusions.excluded WHERE chat_md5=? "
                    "ORDER BY account", (chat_id, md5)).fetchall()
        else:
            with exclusions.standalone() as con:
                rows = con.execute(
                    "SELECT account FROM exclusions.excluded WHERE chat_md5=? "
                    "ORDER BY account", (md5,)).fetchall()
    except (sqlite3.Error, OSError, errors.IndexUnavailable) as exc:
        # 与 plan_chat 同一口径：读不了索引/清单时裸 sqlite3.Error 会一路逃到 CLI 的
        # `except AgentWeChatError` 之外，用户看到的是一整个 traceback 而不是一行拒绝。
        # EA-074 之后 `store.connect()` 会先翻一次（IndexUnavailable）——同样是「读不了」，
        # 在这里统一翻成销毁路径自己的 PurgeRefused，`__cause__` 取最深那层病因。
        raise errors.PurgeRefused(
            f"读不出会话「{contact}」的销毁目标：{exc}",
            hint="确认 index.db 可读、且没有别的进程正在写它（导出任务 / MCP server）；"
                 "库损坏（file is not a database 等）时先 purge --index 删掉再重跑 export"
        ) from (exc.__cause__ or exc)
    if not rows:
        # 空集**不是「无事可做」**，恰好相反：索引里没有活行，不代表 `media/<md5>/` 不在盘上
        # ——`purge --index` 之后逐会话重建的那个窗口里，解密出的图片/语音/HTML 还摊在盘上，
        # 而命令会报一句「没有可删的东西」退 0（spec §5.7 只要求「不写墓碑」，没要求跳过
        # 计划与核查）。所以这里回一条**账号为空**的目标，让计划 → 执行 → 核查照常走一遍：
        # 导出物只取决于 chat_md5、与账号无关，于是计划表印得出那一行、`--yes` 删得掉它。
        # 索引侧各项恒为 0 且不会误伤——`account` 是账号目录名，生产上不可能为空，所以
        # `account=''` / 前缀 `'|md5|'` 一个真实行都命不中；墓碑也因此自然为 0 条。
        return [{"account": "", "chat_id": chat_id}]
    return [{"account": r[0], "chat_id": chat_id} for r in rows]


def media_rebuild_refs(account: str | None = None,
                       progress: Callable[[int], None] | None = None,
                       on_skipped: Callable[[int], None] | None = None) -> int:
    """全量重扫微信库，只为存量历史回填 media_refs（spec §4.5）。幂等 upsert；
    **不写 messages**（消息内容不变，只有 refs 需要补）。分钟级，同步执行。

    **返回值只数未被排除的行**（计数契约，与 `export.run_export` 同款）：它是全量重扫，
    被 `purge --chat` 删掉的会话仍在微信库里，每次都会读到——`store.upsert_media_refs`
    把它们拦在写入端，这里据此把「读到的」与「入库的」分开计。
    `on_skipped`：有行被排除时回调一次，参数是本次跳过的**行数**（「成功但有保留」的出口）。
    """
    ctx = ChatContext(account)
    n = 0
    skipped = 0
    with store.open() as con:
        for _rows, refs in ctx.reader.iter_all():
            k = store.upsert_media_refs(con, refs)
            skipped += k
            n += len(refs) - k            # ← 计数契约：只数未被排除的
            if progress and n and n % 20_000 < store.BATCH:
                progress(n)
    if on_skipped is not None and skipped:
        on_skipped(skipped)
    return n


def msgextra_rebuild(account: str | None = None,
                     progress: Callable[[int], None] | None = None,
                     on_skipped: Callable[[int], None] | None = None) -> dict:
    """全量重扫微信库回填 `msg_extras`（EA-041）。幂等；**只写 msg_extras**——
    不碰 messages / FTS / 向量 / 水位线 / msg_parts。

    **为什么是一条独立命令而不是并进 `chat export`**：与 `appmsg rebuild` / `media
    rebuild-refs` 同一个理由（`cli.py` 那张「谁写谁」的表）——导出链每多写一张表，
    就多一个「导出到一半失败留下半张表」的形态；而这三张旁路表都是**可重算**的，
    让它们各自有一条显式的、幂等的回填入口更省事。历史行也借此可补（原始 XML 只在
    微信库里，导出时不解压 48/42）。

    计数的分母（与 `media_rebuild_refs` / `appmsg_rebuild_parts` 同款）：
      · `extras` = **实际落库**的条数（缓冲数 − 被排除清单挡下的）；被挡下的行一条都不落库
        ——本命令直接回微信库重扫，不拦的话 `purge --chat` 删掉的位置/名片载荷会重新长回来
        （spec §5.4 第 2 条）；
      · `empty` = **本轮扫到的** 48/42 里没有一个可读字段的条数——两种成因都算：载荷危险 /
        解不开 / 缺承载元素（`parse` 返回 None），或解得开但一个白名单属性都没命中
        （`fields` 为空元组）。它不是错误。
        **它含被排除清单挡下的那些行**（复审 T14 的 N8）：判据只有一处、在 `upsert_extras`
        里，要在这里把两者分开就得复刻那份判据——而重复的判据正是本模块一直在消的东西。
        所以它**不是 `extras` 的子集**，两个数别并排当包含关系读（`skipped` 单列）；
      · `skipped` = 被排除清单挡下的行数（`on_skipped` 有值时回调一次，「成功但有保留」的出口）。
    """
    ctx = ChatContext(account)
    stored = empty = skipped = 0
    with store.open() as con:
        # 入口就开 IMMEDIATE：本函数是这次写入的事务 owner（commit 在 `store.open()` 出口）。
        # 与 `appmsg_rebuild_parts` 不同，这里**没有 TEMP 表**先开 deferred 事务，
        # 所以 `upsert_extras` 里那句 guard 会正常触发；入口这一句是为了让整次回填
        # （含排除判据的读）落在同一把写锁下。
        con.execute("BEGIN IMMEDIATE")
        buf: list[tuple[str, int, bytes]] = []

        def flush() -> None:
            nonlocal skipped
            if buf:
                skipped += store.upsert_extras(con, buf)
                buf.clear()
                if progress:
                    # 报**实际落库**的数（与另两处同款）：报缓冲数会让进度条上的最后一个数
                    # 比完成行还大，两句话对不上。
                    progress(stored - skipped)

        for key, msg_type, payload in ctx.reader.iter_extras_input():
            # `empty` 的判据是「**没有一个可读字段**」，不是「parse 返回 None」——两者不等价：
            # 载荷解得开、承载元素也在，但一个白名单属性都没命中时 `Extra.fields` 是**空元组**，
            # 那正是「这条没有可读内容」的常态（真机 42 里有只带 `percard` 的名片）。
            # **走共享的那一份解码**（`_decode_extra`）：自己再写一遍 `zlib.decompress(...).decode()`
            # 已经漂过一次——那边是 `decode()`（严格），这边写成 `decode("utf-8", "ignore")`，
            # 同一个坏载荷在两个入口一个抛一个不抛（review-code 的 Standards 轴 #6）。
            extra = _decode_extra(msg_type, payload)
            if extra is None or not extra.fields:
                empty += 1
            buf.append((key, msg_type, payload))
            stored += 1
            if len(buf) >= store.BATCH:
                flush()
        flush()
    if on_skipped is not None and skipped:
        on_skipped(skipped)
    return {"extras": stored - skipped, "empty": empty, "skipped": skipped}


def appmsg_rebuild_parts(account: str | None = None,
                         progress: Callable[[int], None] | None = None,
                         on_skipped: Callable[[int], None] | None = None) -> dict:
    """全量重扫微信库回填 msg_parts（spec §4.1）。幂等；**只写 msg_parts**——
    不碰 messages / FTS / 向量 / 水位线。

    两阶段，因为引用可以跨分片（实测分片内命中 99.86% 是下界）：
      1. 一边把 server_id→msg_key 灌进 TEMP TABLE，一边把 appmsg 攒批写 msg_parts
      2. 一条 SQL 解析回指

    实测整次 37.5–38.7 s；同轮 `index.db` 增长 +232.8 MB（本机实测）。

    计数口径（**两组不同的分母**，别并排当同一个读）：
      - `parts` / `rejected` / `malformed` = **本轮**的条数（`parts` 是成功解析**且真的
        落库**的条数——解析失败的不计，被排除清单挡下的也不计：缓冲时计数减去 `skipped`
        就是它；所以它**不等于**「扫到的 appmsg 条数」）；
      - `resolved` / `unresolved` = 跑完后**该账号整张 `msg_parts` 表**里 `ref_svrid`
        非空的行中，回指解出来 / 没解出来的行数（同一个谓词 ⇒ 两者之和恒等于该谓词下的
        引用行数）。删改后再跑一次，这组随整表变化，而 `parts` 只反映本轮。

    `skipped` 是新增的键（既有键一个不动）：被排除清单挡下、**没落库**的行数——
    `appmsg rebuild` 直接回微信库重扫，被 `purge --chat` 删掉的会话每次都会读到，
    不拦的话它们的原始载荷（`raw_xml` 里是完整正文）会重新长回来（spec §5.4 第 2 条）。
    它不是第三个分母：`skipped ⊆ 缓冲时计的数`，而 `parts` 报的就是那个差
    （**实际落库**的条数——与 `run_export` / `media_rebuild_refs` 的返回值同款口径：
    三处都只数真正写进库的那些）。`on_skipped`：有行被排除时
    回调一次（与 `media_rebuild_refs` / `run_export` 同款，「成功但有保留」的出口）。
    """
    ctx = ChatContext(account)
    prefix = f"{ctx.account}|"
    parts = rejected = malformed = 0
    skipped = 0
    with store.open() as con:
        # **入口就把事务开成 IMMEDIATE**：本函数是这次写入的事务 owner（commit 在
        # `store.open()` 出口），而 `flush()` 里那句 TEMP 表（sid_map）的 INSERT 会先开出一个
        # **deferred** 事务——`con.in_transaction` 为真、但主库写锁还没拿到
        # （实测：`CREATE TEMP TABLE` 不会，TEMP 表的 INSERT 会；此刻另一个连接可以提交墓碑）。
        # 于是 `upsert_msg_parts` 里那句 `if not con.in_transaction: BEGIN IMMEDIATE` 在
        # **本形态下不触发**，排除判据的「读」与随后的「写」之间就有了窗口：窗口里落下的
        # 墓碑会被本批的写绕过，被删消息的 appmsg 原始载荷永久复活（spec §5.4）。
        con.execute("BEGIN IMMEDIATE")
        con.execute("CREATE TEMP TABLE sid_map("
                    "server_id INTEGER PRIMARY KEY, msg_key TEXT NOT NULL)")
        sid_buf: list[tuple[int, str]] = []
        part_buf: list[tuple[str, int, int | None, str | None, bytes]] = []

        def flush(wrote_parts: bool) -> None:
            """把两批攒下的行落库。**同一批里一起写**：`sid_map` 与 `msg_parts` 都只是
            缓冲，谁先落都不影响结果（阶段 2 在全部扫完之后才跑）。"""
            nonlocal skipped
            if sid_buf:
                # `OR IGNORE`：`server_id` 实测全局唯一（846,785 个、重复 0 次），但那是
                # 上一次测量的结论，而消息还在不断进来。真撞上重复时**静默保留第一条映射**
                # ——即退化成一个错引用（引用解到先出现的那条），而**不是**以裸
                # IntegrityError 崩掉整次回填。这正是它被判 Minor 而非 Important 的原因：
                # 错的是稀有情形下的一个引用，不是进程的存亡。
                con.executemany("INSERT OR IGNORE INTO sid_map VALUES (?,?)", sid_buf)
                sid_buf.clear()
            if part_buf:
                skipped += store.upsert_msg_parts(con, part_buf)
                part_buf.clear()
            if wrote_parts and progress:
                # 进度报的也是**实际落库**的数（与 `run_export` / `media_rebuild_refs` 同款）：
                # 报缓冲数会让进度条上的最后一个数比完成行还大，两句话对不上。
                progress(parts - skipped)

        for sid, key, raw in ctx.reader.iter_parts_input():
            # `if sid` 一次挡掉两种「没有 server_id」：NULL 与 0（实测 15 个 0）。
            # 它们是哨兵而不是可用标识，进 sid_map 会让 `ref_svrid=0` 的引用被错解。
            if sid:
                sid_buf.append((sid, key))
            if raw is not None:
                res = appmsg.parse(raw)
                if res.reason == "rejected":
                    rejected += 1
                elif res.reason == "malformed":
                    malformed += 1
                else:
                    # `quote` 仅 kind=57 才非 None；`svrid` 在 XML 里是字符串，转不出 int
                    # 时 `_to_int` 已经给了 None（**不抛**）。给 None 的那条**照样落库**，
                    # 否则它的正文也跟着没了。
                    svrid = res.appmsg.quote.svrid if res.appmsg.quote else None
                    # 正文用 `parse` 已经算出的规范化文本（`res.normalized`），**不再**
                    # 调一次 `normalize`——那样既白跑一遍，又把存盘形态耦合到「`normalize`
                    # 幂等」这个隐含前提上。
                    part_buf.append((key, res.appmsg.kind, svrid, None,
                                     zlib.compress(res.normalized.encode(), 6)))
                    parts += 1
            if len(sid_buf) >= store.BATCH or len(part_buf) >= store.BATCH:
                flush(bool(part_buf))
        flush(False)

        # 阶段 2：一条 SQL 解析回指。**按账号前缀限定**——sid_map 只装了本账号的
        # server_id，不限定的话别的账号里恰好同号的引用会被接到本账号的消息上。
        # 前缀比对而非 LIKE：wxid 里含 `_`，LIKE 的 `_` 是单字符通配符（同
        # `count_msg_parts`）。
        con.execute(
            "UPDATE msg_parts SET ref_msg_key = (SELECT msg_key FROM sid_map "
            "WHERE server_id = msg_parts.ref_svrid) "
            "WHERE ref_svrid IS NOT NULL AND substr(msg_key, 1, ?) = ?",
            (len(prefix), prefix))
        # 计数从**表的状态**算（而不是 UPDATE 的 changes）：与上面那条 UPDATE 同谓词，
        # 所以 `resolved + unresolved` 恒等于该谓词下 `ref_svrid` 非空的行数（和不变）。
        # **但它是整表的**：上一次回填留下的行照样被算进来——这正是它与 `parts` 分母不同的
        # 地方，别把两者当成同一次扫描的两个视角。
        refs, resolved = (
            con.execute("SELECT count(*), count(ref_msg_key) FROM msg_parts "
                        "WHERE ref_svrid IS NOT NULL AND substr(msg_key, 1, ?) = ?",
                        (len(prefix), prefix)).fetchone())
    if on_skipped is not None and skipped:
        on_skipped(skipped)
    # `parts` 报的是**实际落库**的条数（缓冲数 − 被排除挡下的），与另两处（`run_export` /
    # `media_rebuild_refs` 的返回值）同一口径：CLI 那句「本轮写入 msg_parts N 条」印的就是它，
    # 报缓冲数会让用户被告知写入了 N 条而实际一条没写（终审 I-3）。
    return {"parts": parts - skipped, "resolved": resolved, "unresolved": refs - resolved,
            "rejected": rejected, "malformed": malformed, "skipped": skipped}


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
    res["degraded"] = [*res.get("degraded", []), *ctx.degradations()]   # EA-013：合并 FTS 降级说明
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
            # chat_id——回落要**自证**（F1，T8 复审）：no-key 时 reader 根本没构造出来，
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
                   with_media: bool = False, limit: int = 500, out_dir=None,
                   account: str | None = None, fetch=None) -> "MomentsExportResult | NeedConfirm":
    """朋友圈导出用例：解析 →（可选）下载解密 → 渲染 HTML。

    `with_media=False`（默认）时**全程零网络**——这是零网络红线上本次新增的例外开关，
    默认必须关闭。`fetch` 仅作测试注入，生产调用方不传。
    """
    # F1 的同族入口（review-T9 复验条件 3）：默认产物根在 HOME 下（`HOME_DIR/moments`），
    # 而本入口此前不过任何闸——相对 AGENT_WECHAT_HOME 下会经 `media.ensure_tree` 在 CWD 里
    # 建出整棵树（EA-003 的后果换了个入口）。同 `store.connect` / `keys.save_cached_hex` 的立场。
    paths.ensure_home_dirs()
    from . import moments, moments_html, sns_media          # 惰性：moments_html 只在导出时用（media 已顶层）
    ctx = ChatContext(account)
    author = ctx.resolve_optional(contact)
    if isinstance(author, NeedConfirm):
        return author
    bad_months = [m for m in (months or []) if not MONTH_RE.fullmatch(m)]
    if bad_months:
        raise errors.SearchInputError(f"月份格式应为 YYYY-MM：{', '.join(bad_months)}")
    cap = clamp_limit(limit)          # 不再硬卡 2000
    do_fetch = fetch if fetch is not None else sns_media.fetch

    out_root = media.ensure_tree(
        pathlib.Path(out_dir) if out_dir else moments.export_root())
    report = moments.MomentsReport()
    kept: list[moments.SnsPost] = []
    lo = hi = None
    for p in moments.iter_posts(ctx.account, chat_filter=author, months=months,
                                report=report):
        if len(kept) >= cap:
            report.truncated = True
            report.remaining += 1
            continue                  # 只计数：**不得**再调 _resolve_media（它会联网）
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
    from . import moments, sns_media
    out = []
    for m in p.media:
        if not with_media:
            report.media_not_downloaded += 1
            out.append(m)
            continue
        if sns_media.build_url(m) is None:
            # `build_url` 返回 None == 「本项目不去取」：无 url、kind 不在白名单内，
            # 或**宿主不在 CDN 白名单**。**理由不是「无 token」**——本期之后无 token
            # 只意味着 `AUTH_READY`（URL 已完整，照样可下）；实况照片落在这里是因为
            # `live_url` 不进下载路径。
            report.media_not_downloaded += 1
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
            saved = media.save_artifact(out_root, "media", m.kind,
                                media.month_of(p.create_time), stem, ext, data)
            if saved is None:
                report.media_failed += 1
                out.append(m)
                continue
            report.media_downloaded += 1

            # 封面。只在 KIND_VIDEO 且 AUTH_READY 时下——实测 181 条视频媒体全是 AUTH_READY。
            # 白名单外的封面（真机上 158/181 在 vweixinthumb.tc.qq.com，证书名不匹配）会由
            # `build_url` 拦下——**静默跳过，一个计数器都不加**（`media_not_downloaded` 只对
            # `p.media` 的成员累加，封面不是）。Task 10 的 A4 恒等式正建立在这个认识上。
            thumb_local = None
            if m.kind == moments.KIND_VIDEO and m.thumb and m.auth == moments.AUTH_READY:
                # `key`/`encrypted` **必须一起带过去**：小视频封面是**加密**的，且用的就是
                # 同一个 media 元素的 `enc@key`（真机实测 d1a867dd → ffd8ffe0）。
                # 清掉它们会把能拿到的封面全变成乱码。临时对象让 fetch 的职责不变。
                tcover = dataclasses.replace(m, url=m.thumb)
                if sns_media.build_url(tcover) is not None:
                    try:
                        cdata = do_fetch(tcover)
                        cext = _sniff_ext(cdata)
                        # 扩展名固定 jpg：`media._out_candidates("video", base)` 只声明了
                        # `_thumb.jpg`，判成 png/webp 会与声明不符（将来接 skip 探测时探不到）。
                        if cext != "jpg":
                            report.media_failed += 1
                            if cext is None:
                                report.media_garbage += 1
                        else:
                            csaved = media.save_artifact(out_root, "media", m.kind,
                                                 media.month_of(p.create_time),
                                                 f"{stem}_thumb", cext, cdata)
                            if csaved is None:
                                report.media_failed += 1
                            else:
                                thumb_local = str(csaved.relative_to(out_root))
                    except sns_media.SnsMediaError:
                        report.media_failed += 1
            out.append(dataclasses.replace(m, local=str(saved.relative_to(out_root)),
                                           thumb_local=thumb_local))
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
