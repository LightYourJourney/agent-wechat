"""MCP Server（stdio）。薄适配层：只做参数编组与渲染，入口策略全在 usecases 用例层。"""
from __future__ import annotations

import asyncio
import functools
import json

from mcp.server.mcpserver import MCPServer

from . import errors, export, usecases

server = MCPServer(
    name="agent-wechat",
    instructions="读取本机已登录微信账号的聊天记录（只读）。"
                 "先 get_account_status 确认账号；用 list_contacts 把人名解析成 username；"
                 "get_recent_messages 取近期上下文；search_history 检索历史。"
                 "仅处理本人已登录账号的数据，不要把聊天内容发往任何外部服务。",
)


def _guard(fn):
    """把 core 抛出的项目异常翻成 {"error"[, "hint"]} 信封。

    必须包住每个工具：MCP SDK 的兜底是 `except Exception`，SystemExit（不是
    Exception 子类）会直接杀死常驻 stdio server；且 SDK 会把任何异常改写成语焉
    不详的 "Error executing tool <name>"，core 写好的中文修复指引到不了 agent。
    对外信封与旧的手写 dict 返回逐字节一致（errors.to_payload）。
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except errors.AgentWeChatError as exc:
            return errors.to_payload(exc)
        except Exception as exc:      # 第三方裸异常（fastembed/sqlite3…）兜底
            return errors.generic_payload(exc)
    return wrapper


def _msg_dict(m) -> dict:
    return {"time": m.ts, "sender": m.sender_name or m.sender_id or "?",
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
    每条消息含 time(unix秒)/sender/is_self/type/content。
    """
    res = usecases.recent_messages(contact, limit=limit, account=account)
    if isinstance(res, usecases.NeedConfirm):
        return _need_confirm(res)
    return {"chat_id": res.chat_id, "display": res.display,
            "messages": [_msg_dict(m) for m in res.messages]}


@server.tool()
@_guard
def export_full_history(contact: str | None = None, full: bool = False,
                        account: str | None = None) -> dict:
    """[长任务] 全量导出聊天记录到本地检索库（流式落库、幂等、断点续传）。

    contact 省略则导出全部会话。首次使用 search_history 前必须运行一次。
    立即返回 task_id，用 get_task_status 查询进度；完成后本索引是
    get_recent_messages/search_history 的本地数据源。
    """
    plan = usecases.export_plan(contact=contact, full=full, account=account)
    if isinstance(plan, usecases.NeedConfirm):   # 歧义不是「找不到」：列出候选
        return _need_confirm(plan)
    return {"task_id": export.start_task(plan.account, plan.chat_id, plan.full),
            "status": "running"}


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
    返回 hits: [{chat_id, ts, sender_id, sender_name, is_self, content, media_hint, snip}]。
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
    is_self, content, media_hint, distance}]，distance 越小越相近（cosine 距离）。
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


def main() -> None:
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
