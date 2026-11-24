"""MCP 开销基准：工具定义体积、server 冷启动、各工具的延迟与返回体积。

全程合成数据（不碰真机微信与 ~/.agent-wechat）：在临时目录里用测试夹具造一份加密的
微信库，跑一遍导出建索引，再经 SDK 的 `call_tool` 逐个调用。用法：

    uv run --frozen python scripts/mcp_bench.py [--json out.json] [--chats 20 --per-chat 1500]

token 数是估算：CJK 字符按 1 个、其余字符按 `ASCII_CHARS_PER_TOKEN` 个折 1 个
（用 Claude Code `/context` 对工具定义的实测值标定过，见 docs/MCP_EFFICIENCY.md）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import statistics
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))          # 复用测试夹具（加密合成库）

ASCII_CHARS_PER_TOKEN = 3.0


def est_tokens(text: str) -> int:
    cjk = sum(1 for c in text if "　" <= c <= "鿿" or "＀" <= c <= "￯")
    return round(cjk + (len(text) - cjk) / ASCII_CHARS_PER_TOKEN)


def _median_ms(samples: list[float]) -> float:
    return round(statistics.median(samples) * 1000, 1)


# ---------- 1. 工具定义 ----------

async def definitions() -> dict:
    from agent_wechat import mcp_server
    tools = await mcp_server.server.list_tools()
    rows = []
    for t in tools:
        d = t.model_dump(mode="json", by_alias=True, exclude_none=True)
        schema = json.dumps(d.get("inputSchema", {}), ensure_ascii=False)
        out = json.dumps(d.get("outputSchema", {}), ensure_ascii=False) if "outputSchema" in d else ""
        desc = d.get("description", "")
        rows.append({"name": t.name, "desc_chars": len(desc), "schema_chars": len(schema),
                     "output_schema_chars": len(out),
                     "est_tokens": est_tokens(t.name + desc + schema)})
    instr = mcp_server.server.instructions or ""
    return {"tools": rows,
            "total_desc_chars": sum(r["desc_chars"] for r in rows),
            "total_schema_chars": sum(r["schema_chars"] for r in rows),
            "total_est_tokens": sum(r["est_tokens"] for r in rows),
            "instructions_est_tokens": est_tokens(instr)}


# ---------- 2. 冷启动 ----------

async def _one_startup(home: pathlib.Path) -> tuple[float, float]:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    exe = pathlib.Path(sys.executable).with_name("agent-wechat-mcp")
    params = StdioServerParameters(command=str(exe), args=[],
                                   env={**os.environ, "AGENT_WECHAT_HOME": str(home)})
    t0 = time.perf_counter()
    async with stdio_client(params, errlog=open(os.devnull, "w")) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            t1 = time.perf_counter()
            await s.list_tools()
            t2 = time.perf_counter()
    return t1 - t0, t2 - t1


async def startup(home: pathlib.Path, runs: int = 5) -> dict:
    init, lst = [], []
    for _ in range(runs):
        a, b = await _one_startup(home)
        init.append(a)
        lst.append(b)
    return {"initialize_ms": _median_ms(init), "list_tools_ms": _median_ms(lst), "runs": runs}


# ---------- 3. 合成数据上的工具调用 ----------

SENTENCES = [
    "明天下午三点开项目进度会议，记得带上周报。", "报销单我已经提交了，麻烦你帮忙审批一下。",
    "周末去爬山吗？天气预报说是晴天。", "客户那边又改需求了，上线时间可能要往后推。",
    "机票订好了，周三早上八点的航班。", "酒店在会场附近，步行十分钟就到。",
    "合同的第三条还要再和法务确认一下。", "测试环境今晚升级，大概停机两个小时。",
    "晚上一起吃饭吧，我知道一家新开的川菜馆。", "预算表我更新过了，你看看有没有问题。",
    "评审会改到下周一了，地点不变。", "刚看完那部电影，结局有点出乎意料。",
    "今天又要加班，估计十点才能走。", "出差的行程单发你邮箱了。",
    "这个需求的优先级能不能先降一降？", "好的，收到，我晚点回复你。",
]


def _text(i: int) -> str:
    return SENTENCES[i % len(SENTENCES)]


def build_env(tmp: pathlib.Path, chats: int, per_chat: int) -> tuple[str, list[str]]:
    from test_reader_export_pipeline import (  # pyright: ignore[reportMissingImports]
        ACCT, PW, SELF, T0, build_shard, enc_db)

    from agent_wechat import account, keys, paths, store
    root, home = tmp / "xwechat_files", pathlib.Path(os.environ["AGENT_WECHAT_HOME"])
    friends = [f"bench_friend{i:02d}" for i in range(chats)]
    ds = root / ACCT / "db_storage"
    enc_db(ds / "session" / "session.db", {
        "SessionTable": ("CREATE TABLE SessionTable (username TEXT)",
                         [(f,) for f in friends] + [(SELF,)])})
    enc_db(ds / "contact" / "contact.db", {
        "name2id": ("CREATE TABLE name2id (username TEXT)", [(f,) for f in friends]),
        "contact": ("CREATE TABLE contact (username TEXT, alias TEXT, remark TEXT, "
                    "nick_name TEXT, delete_flag INTEGER)",
                    [(f, None, ("张三" if i == 0 else f"同事{i}"), f"nick{i}", None)
                     for i, f in enumerate(friends)]),
        "chat_room": ("CREATE TABLE chat_room (id INTEGER PRIMARY KEY, username TEXT, "
                      "owner TEXT, ext_buffer BLOB)", []),
    })
    users = friends + [SELF]
    build_shard(root, ACCT, "message_0.db",
                {f: [(lid, 1, (len(users) if lid % 2 else ci + 1), T0 + lid * 60 + ci,
                      _text(lid * 7 + ci * 3), 0)
                     for lid in range(1, per_chat + 1)]
                 for ci, f in enumerate(friends)}, users)
    paths.XWECHAT_FILES = root
    paths.HOME_DIR = home
    keys.CACHE_FILE = home / "passphrase.json"
    store.DB_PATH = home / "index.db"
    keys.save_cached_hex(SELF, PW.encode().hex())
    account.wechat_pid = lambda: 4242
    account.current_account = lambda pid, known=None: ACCT
    return ACCT, friends


def _result_sizes(r) -> dict:
    text = "".join(getattr(c, "text", "") for c in r.content)
    structured = (json.dumps(r.structured_content, ensure_ascii=False)
                  if r.structured_content is not None else "")
    return {"blocks": len(r.content), "text_chars": len(text), "text_est_tokens": est_tokens(text),
            "structured_chars": len(structured)}


async def calls(tmp: pathlib.Path, chats: int, per_chat: int, runs: int = 5) -> dict:
    from agent_wechat import export, mcp_server
    acct, friends = build_env(tmp, chats, per_chat)
    t0 = time.perf_counter()
    n = export.run_export(acct)
    export_s = time.perf_counter() - t0
    srv = mcp_server.server

    first = json.loads((await srv.call_tool("search_messages", {
        "query": "会议", "mode": "keyword", "limit": 1, "contact": "张三"})).content[0].text)
    key = first["hits"][0]["msg_key"]
    cases = [
        ("get_account_status", {}),
        ("list_contacts", {"query": "同事"}),
        ("get_recent_messages", {"contact": "张三"}),
        ("search_history", {"query": "会议"}),
        ("search_messages", {"query": "会议", "mode": "keyword"}),
        ("get_message_context", {"contact": "张三", "msg_key": key}),
        ("get_message_media", {"contact": "张三"}),
        ("get_message_parts", {"contact": "张三", "keys": key}),
        ("get_task_status", {}),
    ]
    out = {"messages_indexed": n, "export_s": round(export_s, 2), "tools": {}}
    for name, args in cases:
        lat = []
        r = None
        for _ in range(runs):
            t = time.perf_counter()
            r = await srv.call_tool(name, args)
            lat.append(time.perf_counter() - t)
        out["tools"][name] = {"median_ms": _median_ms(lat), **_result_sizes(r)}
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true",
                    help="造好合成库后以 stdio 跑 MCP server（给 Claude Code 端到端实测用）")
    ap.add_argument("--json", type=pathlib.Path)
    ap.add_argument("--chats", type=int, default=20)
    ap.add_argument("--per-chat", type=int, default=1500)
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="awmcp-bench-") as d:
        tmp = pathlib.Path(d)
        if args.serve:
            from agent_wechat import export, mcp_server
            acct, _ = build_env(tmp, args.chats, args.per_chat)
            export.run_export(acct)
            await mcp_server.server.run_stdio_async()
            return
        (tmp / "starthome").mkdir(mode=0o700)
        res = {"definitions": await definitions(),
               "startup": await startup(tmp / "starthome"),
               "calls": await calls(tmp, args.chats, args.per_chat)}
    text = json.dumps(res, ensure_ascii=False, indent=1)
    if args.json:
        args.json.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    # 必须在 import agent_wechat 之前：模块级路径（任务注册表、模型缓存等）按它算
    with tempfile.TemporaryDirectory(prefix="awmcp-bench-home-") as _home:
        os.environ["AGENT_WECHAT_HOME"] = _home
        asyncio.run(main())
