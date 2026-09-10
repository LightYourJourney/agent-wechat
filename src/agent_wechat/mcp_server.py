"""MCP Server（stdio）。薄适配层：只做编排与参数校验，业务在 reader/store/search/export。"""
from __future__ import annotations

import asyncio
import json

from mcp.server.mcpserver import MCPServer

from . import account as acc_mod
from . import export, keys, reader, search, store

server = MCPServer(
    name="agent-wechat",
    instructions="读取本机已登录微信账号的聊天记录（只读）。"
                 "先 get_account_status 确认账号；用 list_contacts 把人名解析成 username；"
                 "get_recent_messages 取近期上下文；search_history 检索历史。"
                 "仅处理本人已登录账号的数据，不要把聊天内容发往任何外部服务。",
)


def _acct(account: str | None) -> str:
    return acc_mod.resolve_account(account)


def _msg_dict(m) -> dict:
    return {"time": m.ts, "sender": m.sender_name or m.sender_id or "?",
            "is_self": m.is_self, "type": m.type_name,
            "content": m.content or m.media_hint or ""}


@server.tool()
def get_account_status(account: str | None = None) -> dict:
    """检测微信登录状态、当前账号、密钥就绪情况与本地索引规模。

    读取任何消息前应先调用本工具。返回 wechat_running / current_account /
    accounts / key_ready / index(消息数与会话数)。
    """
    st = acc_mod.status()
    acct = account or st.current
    ready = bool(acct and keys.load_cached_hex(acc_mod.paths.bare_wxid(acct)))
    index = {}
    try:
        with store.connect() as con:
            index = store.counts(con)
    except Exception:
        index = {"messages": 0, "chats": 0, "last_ts": None}
    return {"wechat_running": st.running, "current_account": st.current,
            "accounts": st.accounts, "key_ready": ready, "index": index}


@server.tool()
def list_contacts(query: str = "", limit: int = 20, account: str | None = None) -> list[dict]:
    """按备注/昵称/微信号模糊查找联系人，返回 username（稳定 id）与展示名。

    用于把用户口述的「XXX」解析成后续工具的 contact 参数。返回 kind:
    direct=私聊 / group=群聊 / official=公众号。重名会返回多条，请让用户确认。
    """
    r = reader.MessageReader(_acct(account))
    return r.contacts(query=query or None, limit=limit)


@server.tool()
def get_recent_messages(contact: str, limit: int = 50,
                        account: str | None = None) -> dict:
    """读取与指定联系人的最近聊天记录（时间正序），用于生成回复建议。

    contact 可以是 username（wxid / xxx@chatroom）或展示名（备注/昵称）。
    若按名字解析出多个联系人，返回 {"need_confirm": [...]} 让用户消歧，不要猜。
    每条消息含 time(unix秒)/sender/is_self/type/content。
    """
    acct = _acct(account)
    r = reader.MessageReader(acct)
    if _looks_like_id(contact):
        chat_id = contact
    else:
        cands = r.resolve_chat(contact)
        if not cands:
            return {"error": f"找不到联系人「{contact}」，请用 list_contacts 查询"}
        if len(cands) > 1:
            return {"need_confirm": [{"username": c["username"], "display": c["display"],
                                      "kind": c["kind"]} for c in cands[:10]]}
        chat_id = cands[0]["username"]
    msgs = r.recent(chat_id, limit=max(1, min(limit, 500)))
    return {"chat_id": chat_id, "display": r.display_name(chat_id),
            "messages": [_msg_dict(m) for m in msgs]}


@server.tool()
def export_full_history(contact: str | None = None, full: bool = False,
                        account: str | None = None) -> dict:
    """[长任务] 全量导出聊天记录到本地检索库（流式落库、幂等、断点续传）。

    contact 省略则导出全部会话。首次使用 search_history 前必须运行一次。
    立即返回 task_id，用 get_task_status 查询进度；完成后本索引是
    get_recent_messages/search_history 的本地数据源。
    """
    acct = _acct(account)
    chat_id = None
    if contact:
        r = reader.MessageReader(acct)
        chat_id = contact if _looks_like_id(contact) else _resolve_one(r, contact)
        if not chat_id:
            return {"error": f"找不到联系人「{contact}」"}
    task_id = export.start_task(acct, chat_id, full)
    return {"task_id": task_id, "status": "running"}


@server.tool()
def get_task_status(task_id: str | None = None) -> dict | list:
    """查询长任务进度。task_id 省略则列出近期任务。"""
    return export.task_status(task_id)


@server.tool()
def search_history(query: str, contact: str | None = None, limit: int = 20,
                   account: str | None = None) -> dict:
    """在本地检索库中做全文检索（trigram 精确子串；1-2 字自动 LIKE 兜底）。

    需先运行 export_full_history。contact 可选限定会话（username 或展示名；
    展示名命中多个联系人时返回 need_confirm，不命中返回 error，不会静默扩大范围）。
    返回 hits: [{chat_id, time, sender, is_self, content, snippet}]。
    """
    acct = _acct(account)
    chat_id = None
    if contact:
        chat_id = _resolve_chat_id(acct, contact)
        if isinstance(chat_id, dict):   # need_confirm / error 原样返回
            return chat_id
    res = search.search(query, chat_id=chat_id, limit=limit, account=acct)
    if res["total"] == 0 and not _index_ready():
        res["hint"] = "本地索引为空：请先运行 export_full_history"
    return res


def _resolve_chat_id(acct: str, contact: str) -> str | dict:
    """把 contact 参数解析为唯一 chat_id；歧义/未命中返回结构化结果而非猜测。"""
    if _looks_like_id(contact):
        return contact
    r = reader.MessageReader(acct)
    cands = r.resolve_chat(contact)
    if not cands:
        return {"error": f"找不到联系人「{contact}」，请用 list_contacts 查询"}
    if len(cands) > 1:
        return {"need_confirm": [{"username": c["username"], "display": c["display"],
                                  "kind": c["kind"]} for c in cands[:10]]}
    return cands[0]["username"]


@server.tool()
def build_vector_index(rebuild: bool = False) -> dict:
    """[预留] 构建向量语义索引。当前版本未启用向量层，调用返回说明。

    全文检索已可用；语义检索属于后续增强（sqlite-vec + 本地 embedding）。
    """
    return {"status": "unavailable",
            "reason": "向量索引尚未启用；FTS5 全文检索已覆盖 search_history 需求",
            "upgrade": "后续版本将提供 sqlite-vec + 本地 bge-small-zh 语义检索"}


def _looks_like_id(s: str) -> bool:
    return s.endswith("@chatroom") or s.startswith(("wxid_", "gh_", "filehelper"))


def _resolve_one(r: reader.MessageReader, name: str) -> str | None:
    cands = r.resolve_chat(name)
    return cands[0]["username"] if len(cands) == 1 else None


def _index_ready() -> bool:
    try:
        with store.connect() as con:
            return store.counts(con)["messages"] > 0
    except Exception:
        return False


def main() -> None:
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
