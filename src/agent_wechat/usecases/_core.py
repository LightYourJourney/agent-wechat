"""共享底座：参数归一化、结果类型、`ChatContext` 与降级收集。

`usecases` 拆包后这里是**依赖图的根**：它只依赖 core 模块，不依赖任何兄弟子模块。
兄弟之间还有一条向下的边（`chat` / `memory` / `moments` 用 `media` 里的解码与输出路径
助手），方向始终朝下，所以不会成环——`scripts/check_import_cycles.py` 每次门禁都验它。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .. import account as acc_mod
from .. import errors, reader, store

# ---------- 参数归一化 ----------

# limit 一律夹下界 1：非正值会原样进 SQL——`LIMIT -1` 在 SQLite 里表示「不限量」，
# reader.recent 再被 `pieces[-limit:]` 错位切片，于是 `-n -1` 静默丢一条消息、
# `-n 0` 返回空、search 退化成全表扫描。上界只夹文档化了区间的工具
# （TOOLS.md §3 = 1-500、§8 = 1-50）；contacts 与 search_history 文档只写「返回
# 上限」无区间，故不设上界。
# 「能导出什么」的清单。**贴纸在列**（2026-09-23 恢复导出后收编）：它的字节来源是
# CDN 取件，默认关（`--with-cdn`），故它是可导出 kind、但不是「默认就能导出的 kind」。
MEDIA_KINDS = ("image", "voice", "video", "sticker")

# 月份入参格式：与 media.month_of / 磁盘目录名（`%Y-%m`）逐字符同形。月份部分限定 01-12：
# `2026-13` 同样比不中任何目录，一起被拒（只放行 `\d{2}` 会漏掉它）。
MONTH_RE = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])")

LIMIT_RECENT = (1, 500)
LIMIT_SEMANTIC = (1, 50)
LIMIT_UNBOUNDED = (1, None)
# 「不封顶」的实际上限：再大就要在绑定进 SQLite 的 LIMIT 时抛裸 OverflowError（`limit=10**20`
# 此前让 MCP 报「内部错误」）。留在 int32 内，调用方再做 `limit + 1` 之类的算术也不会越界。
_UNBOUNDED_CEIL = (1 << 31) - 1


def clamp_limit(n: int, bounds: tuple[int, int | None] = LIMIT_UNBOUNDED) -> int:
    lo, hi = bounds
    return max(lo, min(n, _UNBOUNDED_CEIL if hi is None else hi))


def pkg_call(name: str, *args, **kwargs):
    """经包查名调用本层的一个函数（理由见 `pkg_clamp_limit` 的 docstring）。

    拆包带来的固有变化：包级 patch（`monkeypatch.setattr(usecases, "message_media", …)`）
    改的是包的属性，而子模块里的局部名是 import 时绑定的那个对象。凡是**同一个包里
    互调、且测试会从包上替换**的函数，调用点走这里。
    """
    import agent_wechat.usecases as _pkg      # 包级查名，见 docstring
    return getattr(_pkg, name)(*args, **kwargs)


def pkg_context(account: str | None = None) -> ChatContext:
    """`ChatContext` 的**包级查名**版本，供兄弟子模块使用（理由同 `pkg_clamp_limit`）。

    测试用 `monkeypatch.setattr(usecases, "ChatContext", …)` 换掉会话解析；子模块里
    直接构造局部名拿到的是 import 时那个类，那次替换对它无效。
    """
    from . import ChatContext as _impl      # 包级查名，见 docstring
    return _impl(account)


def pkg_clamp_limit(n: int, bounds: tuple[int, int | None] = LIMIT_UNBOUNDED) -> int:
    """`clamp_limit` 的**包级查名**版本，供兄弟子模块调用。

    为什么需要它：拆包后，测试（与将来的替换）打在 `usecases.clamp_limit` 上，而子模块
    里直接用局部名 `clamp_limit(...)` 调的是 import 时绑定的那个函数对象——那次替换对
    它无效。拆包前两者是同一个名字，拆包后不是了，所以互调一律走这里。
    """
    from . import clamp_limit as _impl      # 包级查名，见 docstring
    return _impl(n, bounds)


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
    # 只在 whole_second 补全了一整秒时有值：更早是否还有消息（None = 没补全，按条数自己判）
    earlier: bool | None = None


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

    def __init__(self, account: str | None = None) -> None:
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

    def drift(self) -> tuple[Degradation, ...]:
        """本次调用见过的降级（`Degradation` 元组——结果类型里装的是它，转成信封归入口层）。

        没构造过 reader 说明这次调用没碰微信的库（检索走的是本地索引），没有可报的。
        """
        if self._reader is None:
            return ()
        # **经包查名**：测试与将来的替换打在 `usecases.collect_degradations` 上，而本模块里
        # 直接用局部名拿到的是 import 时那个函数对象——拆包前两者是同一个名字，拆包后不是。
        return pkg_call("collect_degradations", self._reader)

    def degradations(self) -> list[dict]:
        """同 `drift`，信封形状（search/semantic 的出口，Ruling 13）。"""
        return [d.envelope() for d in self.drift()]


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
    # reader 逐条兜住 zstd 坏帧（`reader._decode_row`）：只坏那几条，其余照常。
    "message_content": f"部分消息正文解压失败，以 {reader.UNDECODABLE} 占位",
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

    def envelope(self) -> dict[str, str]:
        """对外信封里的形状（MCP / 任务记录 / sync 结果）：只给能力与「少了什么」，不给表名。"""
        return {"capability": self.capability, "effect": self.effect}


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
            if db:
                # `message_content` 的库读得出来、坏的只是其中几条正文：只点出是哪个库，
                # 不说「读不出来」（那会把用户支去查一个好好的库）。
                effect += f"（{db}）" if capability == "message_content" else f"（{db} 读不出来）"
            seen[key] = Degradation(
                capability=capability, db=db, table="", missing_columns=(),
                effect=effect, drift=False)
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
