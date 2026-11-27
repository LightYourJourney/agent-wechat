"""媒体用例：导出、按消息查媒体状态、appmsg 部件解码。"""
from __future__ import annotations

import contextlib
import dataclasses
import pathlib
import sqlite3
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .. import appmsg, errors, media, messages, msgextra, paths, retrieval, store, tasks
from ._core import (MEDIA_KINDS, MONTH_RE, ChatContext, Degradation,
                    NeedConfirm, pkg_call, pkg_clamp_limit, pkg_context)

if TYPE_CHECKING:
    from collections.abc import Sequence

@dataclass(frozen=True)
class MediaExportResult:
    """media_export 的成功返回（CLI 与 MCP 共用，T7 消费）。out_dir 取自 report——
    实际使用的输出根（--out 省略时是 HOME_DIR/media），由 export_media 赋值，不重算。"""

    chat_id: str
    out_dir: str
    report: media.MediaReport


def media_export(contact: str, *, kinds: list[str], months: list[str] | None = None,
                 out_dir: str | None = None, limit: int = 500, force: bool = False,
                 account: str | None = None,
                 with_cdn: bool = False,
                 deny: frozenset[str] = frozenset()) -> MediaExportResult | NeedConfirm:
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

    `out_dir` 落在微信容器目录之内时**拒绝**（C-11）——在任何读取之前叫停。
    `AGENT_WECHAT_HOME` 落在同一范围时同样拒绝：本用例的默认产物根是 `HOME_DIR/media`，
    而 `media.export_media` 在语音分支之前就先 `ensure_tree(export_root())`——不在这里
    先过闸，`--kinds image`（默认根）就会把图片写进容器的 `media/`（整支复审实测）。
    `deny`：读取范围（MCP 入口传 `AGENT_WECHAT_DENY`，CLI 不传）。
    """
    if out_dir:
        paths.refuse_container_out(out_dir)
    paths.ensure_home_dirs()
    ctx = pkg_context(account)
    chat_id = ctx.resolve(contact)
    if isinstance(chat_id, NeedConfirm):
        return chat_id
    retrieval.ensure_allowed(chat_id, deny)
    if not kinds:
        raise errors.SearchInputError(
            "请至少指定一种媒体类型", hint=f"可用: {', '.join(MEDIA_KINDS)}")
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
        # 显式 out_dir 走 `_resolve_out` 绝对化（同 chat_export / message_media）：相对路径
        # 会原样回到信封的 out_dir 里，MCP 客户端拿它没用。
        # 整个写盘期间在任务注册表里占一条 running 记录：`purge --chat` 的在飞闸门据此拒绝，
        # 不会在导出中途删掉 `media/<md5>/`、核查报 ✅ 之后又被这里写回明文（见 `tasks.claim`）。
        stack.enter_context(tasks.claim("media_export", account=ctx.account, chat_id=chat_id))
        report = media.export_media(ctx.account, [chat_id], kinds=kinds_to_export, months=months,
                                    out_dir=_resolve_out(out_dir) if out_dir else None,
                                    max_files=pkg_clamp_limit(limit),   # 不再硬卡 2000
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

    **调用点是 `pkg_call("_voice_keys_for", …)`**（拆包后本模块内直接用局部名会让包级
    替换失效），所以 vulture 看不见它对它的引用——它是活的，只是名字走查表。
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
    # 给了 keys 时，**索引里查不到**的那些（原样、去重保序）：别的会话的、未导出的、不在所选月份
    # 的。被 limit 截掉的不在此列（那是 truncated）。
    unanswered: tuple[str, ...] = ()


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


def _resolve_out(out_dir: str | None) -> pathlib.Path:
    """`--out` 的绝对化。**解析期的两类失败都必须在这里接住。**

    - 符号链接环：`Path.resolve()` 在 3.12 抛的是 `RuntimeError`（**不是** `OSError`）；
    - 单段名超过文件系统上限（255 字节）：抛 `OSError` / `ENAMETOOLONG`（errno 63）。

    两种都会一路穿到 CLI，印一整屏 traceback，破坏「项目错误 → 单行中文 + 退出码 1」；
    MCP 那边则落到 `generic_payload`，agent 只拿到「内部错误」。`paths` 对 `HOME_DIR` 侧
    早就有这条翻译（`HomeUnavailable`），`--out` 侧此前一个都没有。
    """
    try:
        root = (pathlib.Path(out_dir) if out_dir else media.export_root()).resolve()
    except RuntimeError as exc:
        raise errors.MediaOutputError(
            f"输出路径解析不了（符号链接环？）：{out_dir}",
            hint="换一个正常的目录重试（CLI 的 --out / MCP 的 out_dir）") from exc
    except OSError as exc:
        # 只用 `strerror`（"File name too long" / "Permission denied"），不回显路径——
        # 超长路径经 `errors.redact` 会被打成一片 `<已脱敏>`，反而读不出原因。
        raise errors.MediaOutputError(
            f"输出路径不可用：{exc.strerror or type(exc).__name__}",
            hint="换一个更短、可写的目录重试（CLI 的 --out / MCP 的 out_dir）") from exc
    # **探一次活**：`resolve()` 本身对超长路径不抛（它只做字符串运算），真正的
    # `OSError: [Errno 63] File name too long` 要到后面的 `glob()` / `mkdir()` 才炸，
    # 而那些调用点散在四个入口里。在这里统一 stat 一次，`FileNotFoundError` 是正常的
    # （输出目录通常还没建），其余 OSError 一律提前翻译成项目异常。
    try:
        root.stat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        # 只用 `strerror`（"File name too long" / "Permission denied"），不回显路径——
        # 超长路径经 `errors.redact` 会被打成一片 `<已脱敏>`，反而读不出原因。
        raise errors.MediaOutputError(
            f"输出路径不可用：{exc.strerror or type(exc).__name__}",
            hint="换一个更短、可写的目录重试（CLI 的 --out / MCP 的 out_dir）") from exc
    return root


def message_media(contact: str, *, keys: Sequence[str] | None = None,
                  months: list[str] | None = None, limit: int = 200,
                  out_dir: str | None = None, account: str | None = None,
                  _ctx: ChatContext | None = None,
                  _con: sqlite3.Connection | None = None,
                  deny: frozenset[str] = frozenset()) -> MessageMediaResult | NeedConfirm:
    """消息 → 媒体状态（spec §4.1 的四态）。

    keys=None → 该会话**所有媒体类型的消息**（msg_type ∈ {3,43,34,47}），受 limit 约束
    ——按 msg_type 而非按 ref 筛选：索引内实测有 259 条媒体消息拿不到 ref，
    按 ref 筛会把它们谎报成「没有媒体」。给了 keys → 只答这些，查不到的列在 `unanswered`。
    months 在 SQL 里筛（不是取回后再滤）：limit 卡的必须是**命中月份**的条数。
    truncated=True 表示 limit 截断了答案，别把它当成「那个月没有媒体」。
    """
    ctx = _ctx or pkg_context(account)
    chat_id = ctx.resolve(contact)
    if isinstance(chat_id, NeedConfirm):
        return chat_id
    retrieval.ensure_allowed(chat_id, deny)      # 读取范围（MCP 入口传入，CLI 不传）
    # 与 media_export / moments_export 同一口径：格式错的月份串永远比不中，不拦就是
    # 静默 0 命中、exit 0（用户看到「这个月没有媒体」而不是「月份写错了」）。
    bad_months = [m for m in (months or []) if not MONTH_RE.fullmatch(m)]
    if bad_months:
        raise errors.SearchInputError(f"月份格式应为 YYYY-MM：{', '.join(bad_months)}")
    cm = messages.chat_md5(chat_id)
    # 绝对化：`--out 相对路径` 会让 states[].path 变成 MCP 客户端用不了的相对路径。
    # `.resolve()` 是纯路径运算，不碰微信数据（红线不受影响）。
    out_root = _resolve_out(out_dir)
    lim = pkg_clamp_limit(limit, LIMIT_MEDIA)
    month_sql, month_args = _month_clause(months)
    degraded: list[Degradation] = []
    unanswered: tuple[str, ...] = ()

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
                # v10 前的 4 段旧键唯一时换成新键（`store.resolve_msg_key`）
                given = list(dict.fromkeys(keys))
                full_of = {k: store.resolve_msg_key(con, ctx.account, k) for k in given}
                uniq = list(dict.fromkeys(full_of.values()))
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
                found = {r["msg_key"] for r in rows}
                unanswered = tuple(k for k in given if full_of[k] not in found)
                # **`>` 而不是 `>=`**：这条路（keys 分支）没有 SQL `LIMIT`，`rows` 就是候选
                # 全量，`rows[:lim]` 只在**超过** lim 时才真的丢东西。用 `>=` 时「key 数恰好
                # 等于 limit」（真机常见：200 个 key + 默认 limit 200）会一条不丢地报
                # `truncated=True`，MCP 于是告诉 agent「counts 只是部分」、CLI 还会印
                # 「已提前收手」。与下面 else 分支的 `>=` 不同——那条路的 SQL 自己截断了，
                # 取回正好 lim 条时确实可能还有更多，只能保守。
                truncated = len(rows) > lim
                rows = rows[:lim]
            else:
                type_ph = ",".join("?" * len(_MEDIA_TYPES))
                # 会话的 msg_key 前缀区间（`account|chat_md5|` 起头）走主键索引，只扫本会话；
                # `+m.account` 让规划器别为了 ORDER BY 选 idx_msg_ts——那会把整个账号的消息
                # 按时间倒序扫一遍（30 万条约 70ms，前缀区间约 1ms）。两个列谓词照留。
                prefix = messages.msg_key_prefix(ctx.account, cm)
                sql = (f"SELECT {messages.column_list(*messages.COLUMNS, prefix='m.')} "
                       f"FROM messages m WHERE +m.account=? AND m.chat_md5=? "
                       f"AND m.msg_key >= ? AND m.msg_key < ? "
                       f"AND m.msg_type IN ({type_ph}){month_sql} "
                       f"ORDER BY m.ts DESC LIMIT ?")
                rows = [dict(zip(messages.COLUMNS, r))
                        for r in con.execute(sql, [ctx.account, cm, prefix, prefix[:-1] + "}",
                                                   *_MEDIA_TYPES, *month_args, lim])]
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
        voice_keys = pkg_call("_voice_keys_for", ctx.account)
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
    # `Degradation` 元组，同 recent_messages 的口径（转换归入口层）。
    return MessageMediaResult(chat_id=chat_id, out_dir=str(out_root),
                              states=states, counts=counts, by_kind=by_kind,
                              truncated=truncated, unanswered=unanswered,
                              degraded=tuple(degraded) + ctx.drift())


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
