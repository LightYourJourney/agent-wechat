"""会话类用例：账号状态、联系人、时间线、导出、会话导出与总目录页。"""
from __future__ import annotations

import re
import sqlite3
import zlib
from collections.abc import Sequence
from dataclasses import dataclass

from .. import (appmsg, chatview, errors, exclusions, export, keys, media, messages,
                migrations, msgextra, paths, reader, search, store, tasks, vector)
from .. import account as acc_mod
from .. import chat_index as index_html
from ._core import (ChatContext, Degradation, ExportPlan, NeedConfirm, RecentMessages,
                    _display_or_id, clamp_limit, collect_degradations)
from .media import _decode_extra, _decode_part, _resolve_out
from ._core import (LIMIT_RECENT, MONTH_RE, pkg_call, pkg_clamp_limit,
                    pkg_context)

# ---------- 工具用例（每个工具一个函数） ----------

def index_snapshot() -> tuple[dict, list[str]]:
    """索引规模 + 自检说明（`get_account_status` 与 CLI `status` 共用的那一半）。只读、不建库。"""
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
            # **只读连接，不走 `store.open()`**：那个会 executescript(SCHEMA) 并在旧库上
            # 跑迁移——诊断命令变成写操作，而 `status` 是故障时最先跑的那条；另一个进程
            # 正持写锁时它会等满 timeout，然后把「忙」报成「库坏了」，还附一句「删掉
            # index.db」。这里只读，版本落后只如实提示。
            with store.open_readonly() as con:
                version = migrations.version_if_table_exists(con, scope=migrations.SCOPE_MAIN)
                if 0 < version < store.SCHEMA_VERSION:
                    schema_notes.append(
                        f"索引库版本 v{version}，本程序是 v{store.SCHEMA_VERSION}——"
                        "跑一次导出或 sync 会自动升级（status 只读，不升级）")
                index = store.counts(con)
                # EA-077：触发器体是「OLD 库 + 新代码」的静默失配面——自检报出来（不改库）。
                schema_notes += store.schema_drift_notes(con)
        except (errors.AgentWeChatError, sqlite3.Error, OSError) as exc:
            # F10（T7 复审）：读不了 ≠ 空索引——把「库坏了」说成「还没导出过」正是本仓
            # 到处在消灭的形态（message_media / chat_export / message_parts / chat_index
            # 四处同立场）。计数仍按零呈现（形状不变），但这条说明必须带上。
            stale = store.is_lock_error(exc)
            schema_notes = [
                f"索引库读不出来（{errors.redact(str(exc))}）——index 计数按零呈现；"
                + ("多半是别的进程正持写锁（导出 / 构建 / 另一个 MCP server），等它结束再试"
                   if stale else "删除 index.db 后重跑一次导出可重建")]
    return index, schema_notes


def account_status(account: str | None = None) -> dict:
    """账号/密钥/索引规模快照。account 省略即当前登录账号。

    **显式给了 account 就校验目录**（与其它工具同一条通用约定）：此前不校验，名字打错时
    答 `key_ready: false`，把 agent 引向「重新 bootstrap」——修错了方向。
    """
    st = acc_mod.status()
    acct = acc_mod.resolve_account(account) if account else st.current
    ready = bool(acct and keys.load_cached_hex(paths.bare_wxid(acct)))
    index, schema_notes = index_snapshot()
    return {"wechat_running": st.running, "current_account": st.current,
            "accounts": st.accounts, "key_ready": ready, "index": index,
            "schema_notes": schema_notes}


def contacts(query: str = "", limit: int = 200,
             account: str | None = None) -> list[dict]:
    """联系人模糊搜索。limit 默认 200 是 reader 的默认值：MCP 传 20，CLI 不传。"""
    return pkg_context(account).reader.contacts(query=query or None,
                                                limit=pkg_clamp_limit(limit))


def timeline(chat_id: str, *, account: str | None = None,
             before_ts: int | None = None, before_local_id: int | None = None,
             limit: int = 50) -> dict:
    """工作台时间线：从**索引**分页读，不经微信库、不返回全量。"""
    acct = account or acc_mod.resolve_account(None)
    if not chat_id:
        raise errors.ContactNotFound("会话不能为空")
    with store.open() as con:
        return store.message_page(con, acct, chat_id, before_ts=before_ts,
                                  before_local_id=before_local_id,
                                  limit=pkg_clamp_limit(limit, (1, 100)))


def chats(account: str | None = None, query: str = "", limit: int = 50) -> list[dict]:
    """会话选择器：索引里出现过的会话（有界、不读微信库）。"""
    acct = account or acc_mod.resolve_account(None)
    with store.open() as con:
        return store.chats_in_index(con, acct, query=query,
                                    limit=pkg_clamp_limit(limit, (1, 100)))


def recent_messages(contact: str, limit: int = 50,
                    account: str | None = None) -> RecentMessages | NeedConfirm:
    ctx = pkg_context(account)
    chat_id = ctx.resolve(contact)
    if isinstance(chat_id, NeedConfirm):
        return chat_id
    msgs = ctx.reader.recent(chat_id, limit=pkg_clamp_limit(limit, LIMIT_RECENT))
    return RecentMessages(chat_id=chat_id, display=_display_or_id(ctx, chat_id),
                          messages=msgs,
                          degraded=pkg_call("collect_degradations", ctx.reader))


def export_plan(contact: str | None = None, full: bool = False,
                account: str | None = None) -> ExportPlan | NeedConfirm:
    """导出目标解析：歧义/未命中和其它工具同一策略（不再自成一派）。"""
    ctx = pkg_context(account)
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
    plan = pkg_call("export_plan", contact=contact, full=full, account=account)
    if isinstance(plan, NeedConfirm):
        return plan

    def _run(tid: str) -> int:
        def note_reader(r: reader.MessageReader) -> None:
            # 与 CLI `export` 同一份降级：漂移记在导出用的那个 reader 上（spec §6 的
            # `username_pool`）。此前只有 CLI 接了它，MCP 任务记录里没有这一条。
            found = [d.envelope() for d in pkg_call("collect_degradations", r)]
            if found:
                tasks.update(tid, degraded=found)

        # 计数契约的两半（spec §5.4）：返回值只数**实际入库**的行，被排除挡下的另计并以
        # `skipped` 随任务记录带回 MCP 信封（与 vector 任务同名字段——`tasks.status()`
        # 原样透出，入口不需要再翻译）。只修 `rows` 那一半的话，agent 见数少了却不知道
        # 少在哪：「导出成功」与实际发生的事不一致，正是 spec §7 要消灭的形态。
        return export.run_export(plan.account, chat_id=plan.chat_id, full=plan.full,
                                 # EA-082：`rows` 此前恒为 0——progress 没接线（S3-010）。
                                 # progress 的入参正是**已入库行数**，与终值同口径；
                                 # 终值由 tasks._run 用返回值覆盖（既有语义保留）。
                                 progress=lambda n: tasks.update(tid, rows=n),
                                 on_skipped=lambda n: tasks.update(tid, skipped=n),
                                 on_reader=note_reader)

    # chat_id/full 一并记进任务元数据（tasks.json 已发布字段），与旧 export.start_task 一致；
    # 两者同时是**准入键**（F1，T7 复审）：缺了 full，「增量在跑时要求全量」会被静默吸收、
    # 全量根本没跑，而调用方按 done 轮询会得出错误结论。
    task_id = tasks.start("export", _run, account=plan.account, chat_id=plan.chat_id,
                          full=plan.full)
    return {"task_id": task_id, "status": "running"}


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

    `out_dir` 落在微信容器目录之内时**拒绝**（C-11）——在任何读取之前叫停。
    """
    if out_dir:
        paths.refuse_container_out(out_dir)
    ctx = pkg_context(account)
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
    out_root = _resolve_out(out_dir)
    try:
        rows, truncated = search.messages_in_range(
            chat_id, account=ctx.account, after_ts=after_ts, before_ts=before_ts,
            limit=pkg_clamp_limit(limit))
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
    mm = pkg_call("message_media", contact, keys=keys, out_dir=str(out_root),
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
        degraded=tuple(degraded) + pkg_call("collect_degradations", ctx.reader),
        refs=refs,
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
    ctx = pkg_context(account)
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

    `out_dir` 落在微信容器目录之内时**拒绝**（C-11）——在任何读取之前叫停。
    """
    if out_dir:
        paths.refuse_container_out(out_dir)
    # 与 `chat_export` 同款绝对化：`--out` 给相对路径时，`out_dir`/`file` 才是能直接打开、
    # 也能与 `chat export` 对齐的路径。
    out_root = _resolve_out(out_dir)
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
    ctx = pkg_context(account)
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
