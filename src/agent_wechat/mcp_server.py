"""MCP Server（stdio）。薄适配层：只做参数编组与渲染，入口策略全在 usecases 用例层。"""
from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import MCPServer

from . import errors, usecases

if TYPE_CHECKING:
    # 仅类型检查期：reader 已由 usecases 间接载入，这里只为让注解可解析
    from . import reader

server = MCPServer(
    name="agent-wechat",
    instructions="读取本机已登录微信账号的聊天记录（只读）。"
                 "先 get_account_status 确认账号；用 list_contacts 把人名解析成 username；"
                 "get_recent_messages 取近期上下文；search_history 检索历史。"
                 "仅处理本人已登录账号的数据，不要把聊天内容发往任何外部服务。",
)


def _guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    """把 core 抛出的项目异常翻成 {"error"[, "hint"]} 信封。

    必须包住每个工具：MCP SDK 的兜底是 `except Exception`，SystemExit（不是
    Exception 子类）会直接杀死常驻 stdio server；且 SDK 会把任何异常改写成语焉
    不详的 "Error executing tool <name>"，core 写好的中文修复指引到不了 agent。
    对外信封与旧的手写 dict 返回逐字节一致（errors.to_payload）。
    """
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except errors.ContactNotFound as exc:
            # 未命中的下一步指引是**入口专属**的：MCP 侧该提示 list_contacts、
            # CLI 侧该提示 contacts。core 只给中立文案，后缀在各自适配层追加
            # （TOOLS.md:105/268 文档化的就是这一条，逐字保持）。
            return {"error": errors.redact(f"{exc}，请用 list_contacts 查询")}
        except errors.AgentWeChatError as exc:
            return errors.to_payload(exc)
        except Exception as exc:      # 第三方裸异常（fastembed/sqlite3…）兜底
            return errors.generic_payload(exc)
    return wrapper


def _msg_dict(m: reader.Message) -> dict:
    return {"msg_key": m.msg_key,
            "time": m.ts,
            "sender": "我" if m.is_self else (m.sender_name or m.sender_id or "?"),
            "is_self": m.is_self, "type": m.type_name,
            "content": m.content or m.media_hint or ""}


def _need_confirm(nc: usecases.NeedConfirm) -> dict:
    """消歧是正常结果而非错误：翻成与 TOOLS.md 一致的 need_confirm 信封。"""
    return {"need_confirm": [c.to_dict() for c in nc.candidates]}


@server.tool()
@_guard
def get_account_status(account: str | None = None) -> dict:
    """检测微信登录状态、当前账号、密钥就绪情况与本地索引规模。

    读取任何消息前应先调用本工具。返回 wechat_running / current_account /
    accounts / key_ready / index(消息数与会话数)。
    """
    return usecases.account_status(account)


@server.tool()
@_guard
def list_contacts(query: str = "", limit: int = 20,
                  account: str | None = None) -> list[dict] | dict:
    """按备注/昵称/微信号模糊查找联系人，返回 username（稳定 id）与展示名。

    用于把用户口述的「XXX」解析成后续工具的 contact 参数。返回 kind:
    direct=私聊 / group=群聊 / official=公众号。重名会返回多条，请让用户确认。
    """
    return usecases.contacts(query=query, limit=limit, account=account)


@server.tool()
@_guard
def get_recent_messages(contact: str, limit: int = 50,
                        account: str | None = None) -> dict:
    """读取与指定联系人的最近聊天记录（时间正序），用于生成回复建议。

    contact 可以是 username（wxid / xxx@chatroom）或展示名（备注/昵称）。
    若按名字解析出多个联系人，返回 {"need_confirm": [...]} 让用户消歧，不要猜。
    每条消息含 time(unix秒)/sender/is_self/type/content，以及稳定标识 msg_key
    （后续按它指名某条消息，如查该消息的媒体文件）。
    微信库结构变更导致能力降级时，另带 degraded: [{capability, effect}]（未降级时为空）。
    """
    res = usecases.recent_messages(contact, limit=limit, account=account)
    if isinstance(res, usecases.NeedConfirm):
        return _need_confirm(res)
    return {"chat_id": res.chat_id, "display": res.display,
            "messages": [_msg_dict(m) for m in res.messages],
            "degraded": [{"capability": d.capability, "effect": d.effect}
                         for d in res.degraded]}


@server.tool()
@_guard
def export_full_history(contact: str | None = None, full: bool = False,
                        account: str | None = None) -> dict:
    """[长任务] 全量导出聊天记录到本地检索库（流式落库、幂等、断点续传）。

    contact 省略则导出全部会话。首次使用 search_history 前必须运行一次。
    立即返回 task_id，用 get_task_status 查询进度；完成后本索引是
    get_recent_messages/search_history 的本地数据源。
    """
    res = usecases.export_history(contact=contact, full=full, account=account)
    if isinstance(res, usecases.NeedConfirm):    # 歧义不是「找不到」：列出候选
        return _need_confirm(res)
    return res


@server.tool()
@_guard
def get_task_status(task_id: str | None = None) -> dict | list:
    """查询长任务进度。task_id 省略则列出近期任务。"""
    return usecases.task_status(task_id)


@server.tool()
@_guard
def search_history(query: str, contact: str | None = None, limit: int = 20,
                   account: str | None = None) -> dict:
    """在本地检索库中做全文检索（trigram 精确子串；1-2 字自动 LIKE 兜底）。

    需先运行 export_full_history。contact 可选限定会话（username 或展示名；
    展示名命中多个联系人时返回 need_confirm，不命中返回 error，不会静默扩大范围）。
    返回 hits: [{chat_id, ts, sender_id, sender_name, is_self, content, media_hint, snip}]，
    每条另带稳定标识 msg_key（可用来指名该消息）。
    微信库结构变更导致显示名等能力降级时，另带 degraded: [{capability, effect}]——
    降级会让 sender_name 回落成 wxid，别把它当成真实姓名。
    """
    res = usecases.search_history(query, contact=contact, limit=limit, account=account)
    if isinstance(res, usecases.NeedConfirm):
        return _need_confirm(res)
    if res["total"] == 0 and not usecases.index_ready():
        # MCP 专属提示（文案里是工具名 export_full_history）；CLI 侧不打印，故不放用例层
        res["hint"] = "本地索引为空：请先运行 export_full_history"
    return res


@server.tool()
@_guard
def semantic_search(query: str, contact: str | None = None, limit: int = 10,
                    account: str | None = None) -> dict:
    """按语义（意思相近）检索聊天记录，适合关键词记不清时使用；需先运行 build_vector_index。

    与 search_history 的分工：精确关键词/人名/专名用 search_history（FTS 更快更准），
    模糊意图（「上次说的那个爬山的安排」）用本工具。contact 可选限定会话，
    展示名歧义时返回 need_confirm。返回 hits: [{chat_id, display, ts, sender,
    is_self, content, media_hint, distance}]，distance 越小越相近（cosine 距离）；
    每条另带稳定标识 msg_key（可用来指名该消息）。
    微信库结构变更导致显示名等能力降级时，另带 degraded: [{capability, effect}]。
    """
    res = usecases.semantic_search(query, contact=contact, limit=limit, account=account)
    return _need_confirm(res) if isinstance(res, usecases.NeedConfirm) else res


@server.tool()
@_guard
def build_vector_index(rebuild: bool = False, account: str | None = None) -> dict:
    """[长任务] 构建本地向量语义索引（供 semantic_search 使用）。

    嵌入跑在本机 CPU（fastembed + bge-small-zh-v1.5），聊天内容不外发；
    首次调用会下载约 95MB 模型文件到 ~/.agent-wechat/models，之后完全离线。
    实测全量构建约 2.5–3 小时（68 万向量，含多次中断续跑），中断后重跑自动续传。
    立即返回 task_id，用 get_task_status 查进度；rebuild=True 会先清空索引重新嵌入。
    注意 account 语义与其它工具相反：省略 account 表示索引**全部**账号，而不是当前账号。
    """
    return usecases.build_vector_index(account, rebuild)


@server.tool()
@_guard
def export_chat_media(contact: str, kinds: str = "image,voice,video",
                      months: str | None = None, limit: int = 500,
                      account: str | None = None) -> dict:
    """导出某会话的媒体文件到本机（图片解密 / 语音转 WAV / 视频复制），返回计数与输出目录。

    contact 可以是 username（wxid / xxx@chatroom）或展示名（备注/昵称），歧义返回
    need_confirm 让用户消歧，不要猜。kinds/months 为逗号分隔字符串（kinds:
    image,voice,video；months: YYYY-MM，可多个）。产物落 out_dir 指向的输出根
    （默认 ~/.agent-wechat/media，目录 0700），用文件系统工具自行取用——信封不回传
    文件清单（大结果膨胀上下文）。历史媒体大多未被微信保留，counts 里的 missing
    是常态，不是错误。图片依赖磁盘密钥推导，失败时 degraded 会说明且语音/视频
    不受影响；其余能力级降级也汇总在 degraded（未降级为空数组）。触及 limit 时
    truncated=true，可分月份重跑补齐。
    """
    res = usecases.media_export(
        contact, kinds=[k.strip() for k in kinds.split(",") if k.strip()],
        months=[m.strip() for m in months.split(",") if m.strip()] if months else None,
        limit=limit, account=account)
    if isinstance(res, usecases.NeedConfirm):
        return _need_confirm(res)
    d = res.report.to_dict()
    return {"chat_id": res.chat_id, "out_dir": res.out_dir,
            "counts": {k: d[k] for k in ("image", "voice", "video")},
            "degraded": res.report.degraded, "truncated": res.report.truncated}


@server.tool()
@_guard
def get_message_media(contact: str, keys: str | None = None, months: str | None = None,
                      limit: int = 200, out_dir: str | None = None,
                      account: str | None = None) -> dict:
    """查消息的媒体：有没有、什么类型、在不在本机、在哪。

    keys 为逗号分隔的 msg_key（取自 get_recent_messages / search_history / semantic_search
    返回的 msg_key 字段）；省略则回答该会话全部媒体类型的消息。out_dir 省略时按
    ~/.agent-wechat/media——若导出时用过 --out，这里也要给同一个，否则已导出的产物会被
    判成 exportable 而非 exported。

    status 四态：none(没有媒体) / exported(已导出，path 是产物路径) /
    exportable(有媒体、本体还在微信里，但你还没导出) / gone(有媒体但本机找不到)。
    **本机绝大多数图片与视频的本体已被微信清理，gone 是常态，不是错误。**
    counts 给出四态的汇总，用来一眼判断是不是「全是 gone」；by_kind 再把 counts 按
    image/video/voice 拆开——池化会把「语音几乎全可导出」与「图片视频几乎全 gone」
    混成一个数字，读起来像「这个会话的媒体大多没了」，按 kind 看才是实情。

    **exportable 只保证本机有可定位的产物**：video 命中缩略图时也算 exportable，
    此时本机只有一张 jpg，mp4 本体并不在。

    truncated=true 表示触及 limit 提前收手：counts/by_kind 是**答完的这些**，不是全量。

    语音库读不到时会降级：degraded 非空，且语音一律按 gone 呈现（不会假装成 exportable）。
    """
    res = usecases.message_media(
        contact,
        keys=[k.strip() for k in keys.split(",") if k.strip()] if keys else None,
        months=[m.strip() for m in months.split(",") if m.strip()] if months else None,
        limit=limit, out_dir=out_dir, account=account)
    if isinstance(res, usecases.NeedConfirm):
        return _need_confirm(res)
    # by_kind 由用例层从 states 派生（cli.py 读的是同一份）：两个入口逐字重复过这段推导，
    # 而 TOOLS.md / USAGE.md 承诺两边暴露同一套 by_kind。
    return {"chat_id": res.chat_id, "out_dir": res.out_dir,
            "counts": res.counts, "by_kind": res.by_kind, "truncated": res.truncated,
            "messages": [{"msg_key": s.msg_key, "time": s.ts, "kind": s.kind,
                          "status": s.status, "path": s.path} for s in res.states],
            "degraded": [{"capability": d.capability, "effect": d.effect}
                         for d in res.degraded]}


@server.tool()
@_guard
def export_moments(contact: str | None = None, months: str | None = None,
                   with_media: bool = False, limit: int = 500,
                   account: str | None = None) -> dict:
    """导出朋友圈为可读的 HTML 时间线（读取本账号可见的动态、评论与点赞）。

    contact 可选，按**作者**筛选（username 或展示名），省略则导出全部好友；
    歧义返回 need_confirm，不要猜。months 为逗号分隔的 YYYY-MM。

    **默认不联网**：with_media=False 时全程零网络，产物里的媒体一律标注
    「远程，未下载」。设为 True 才会下载并解密媒体本体——这会向微信 CDN 发起
    请求（本项目零网络红线上本次新增的显式例外），且耗时随媒体数量增长。
    产物落 out_dir 的 index.html（目录 0700），用文件系统工具自行打开；
    信封不回传文件清单。

    counts 里的 media_failed 是常态而非错误：微信服务端会清理老媒体，
    实测抽样中约三成带 token 仍返回 404。counts.failed 是**动态解析失败**被跳过的
    条数（与媒体无关）：非零说明部分动态没读懂，若它接近 posts 的总量，多半是微信
    改了动态的 XML 结构——此时不要把它当成「这个账号没有朋友圈」。window 给出动态的
    时间跨度——**注意这是微信本地缓存的窗口，不是完整历史**（本机实测 1,923 条覆盖
    2013-02~2026-09，密度极不均）。触及 limit 时 truncated=true，留下的是**最新**的
    那些动态。
    """
    res = usecases.moments_export(
        contact,
        months=[m.strip() for m in months.split(",") if m.strip()] if months else None,
        with_media=with_media, limit=limit, account=account)
    if isinstance(res, usecases.NeedConfirm):
        return _need_confirm(res)
    r = res.report
    return {"out_dir": res.out_dir,
            "counts": {"posts": r.posts, "comments": r.comments, "likes": r.likes,
                       "failed": r.failed,
                       "media_downloaded": r.media_downloaded,
                       "media_not_downloaded": r.media_not_downloaded,
                       "media_failed": r.media_failed},
            "window": ({"earliest": r.window[0], "latest": r.window[1]}
                       if r.window else None),
            "degraded": r.degraded, "truncated": r.truncated}


def main() -> None:
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
