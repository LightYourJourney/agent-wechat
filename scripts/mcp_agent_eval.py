"""Claude Code 端到端评测：同一组任务在合成数据上跑 `claude -p`，统计工具选择与多轮成本。

server 用 `scripts/mcp_bench.py --serve`（临时目录里的加密合成库，不碰真机数据）；
只开放 agent-wechat 的 MCP 工具（`--tools ""` 关掉全部内置工具）。用法：

    uv run --frozen python scripts/mcp_agent_eval.py --model sonnet --reps 2 --json out.json

每条任务记录：调用的工具序列、轮数、输入 token（含缓存读写）、输出 token、耗时、
是否用到期望的工具、答案里是否含期望的关键词。会调用真实模型、产生费用。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "scripts"))
from mcp_bench import _text  # noqa: E402  合成库的正文生成规则（与 --serve 造的库一致）

# 同事3（第 4 个会话，ci=3）发的最后一条是 local_id 1500（偶数 local_id 是对方发的）
_LAST_FROM_COLLEAGUE3 = _text(1500 * 7 + 3 * 3)

# (任务, 期望用到的工具, 答案里应出现的关键词)
TASKS = [
    ("我和张三最近聊了什么？用两句话概括。", {"get_recent_messages"}, None),
    ("同事3 发给我的最后一条消息说了什么？原文引用。", {"get_recent_messages"},
     _LAST_FROM_COLLEAGUE3.rstrip("。")[:8]),
    ("在聊天记录里找提到「报销」的消息，告诉我其中一条是谁说的。",
     {"search_messages", "search_history"}, None),
    ("找一条提到「机票」的消息，并把它前后各两条上下文给我看。",
     {"get_message_context"}, None),
    ("张三的聊天里有没有可以导出的图片或语音？", {"get_message_media"}, None),
    ("本地检索库里一共索引了多少条消息？只回答数字。", {"get_account_status"}, "30000"),
]


def run_one(prompt: str, cfg: pathlib.Path, model: str) -> dict:
    cmd = ["claude", "-p", prompt, "--model", model, "--strict-mcp-config",
           "--mcp-config", str(cfg), "--tools", "", "--allowedTools", "mcp__agent-wechat",
           "--disable-slash-commands", "--no-session-persistence",
           # 本机 hooks（如压缩大输出的插件）会改写工具结果、干扰对比：评测时全关
           "--settings", json.dumps({"disableAllHooks": True}),
           "--output-format", "stream-json", "--verbose"]
    p = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       timeout=600, cwd=tempfile.gettempdir())
    tools, calls, result = [], [], {}
    for line in p.stdout.splitlines():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("type") == "assistant":
            for c in o["message"].get("content", []):
                if c.get("type") == "tool_use":
                    tools.append(c["name"].removeprefix("mcp__agent-wechat__"))
                    calls.append({"tool": tools[-1], "input": c.get("input")})
        elif o.get("type") == "result":
            result = o
    u = result.get("usage", {})
    return {"tools": tools, "calls": calls, "turns": result.get("num_turns"),
            "input_tokens": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
            + u.get("cache_read_input_tokens", 0),
            "output_tokens": u.get("output_tokens", 0),
            "duration_ms": result.get("duration_ms"), "cost_usd": result.get("total_cost_usd"),
            "answer": (result.get("result") or "")[:300], "ok": not result.get("is_error", True)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--json", type=pathlib.Path)
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="awmcp-eval-") as d:
        _run(pathlib.Path(d) / "mcp.json", args)


def _run(cfg: pathlib.Path, args: argparse.Namespace) -> None:
    cfg.write_text(json.dumps({"mcpServers": {"agent-wechat": {
        "command": sys.executable,
        "args": [str(ROOT / "scripts" / "mcp_bench.py"), "--serve"]}}}))
    rows = []
    for prompt, want, keyword in TASKS:
        for _ in range(args.reps):
            r = run_one(prompt, cfg, args.model)
            r.update(task=prompt, hit=bool(want & set(r["tools"])),
                     keyword_ok=(keyword in r["answer"]) if keyword else None)
            rows.append(r)
            print(json.dumps({k: r[k] for k in ("task", "tools", "turns", "input_tokens",
                                                 "duration_ms", "hit", "keyword_ok")},
                             ensure_ascii=False), flush=True)
    summary = {
        "runs": len(rows),
        "tool_calls_mean": round(statistics.mean(len(r["tools"]) for r in rows), 2),
        "hit_rate": round(sum(r["hit"] for r in rows) / len(rows), 3),
        "keyword_ok": [r["keyword_ok"] for r in rows if r["keyword_ok"] is not None],
        "input_tokens_median": statistics.median(r["input_tokens"] for r in rows),
        "input_tokens_total": sum(r["input_tokens"] for r in rows),
        "duration_ms_median": statistics.median(r["duration_ms"] or 0 for r in rows),
        "cost_usd_total": round(sum(r["cost_usd"] or 0 for r in rows), 4),
    }
    print(json.dumps(summary, ensure_ascii=False))
    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "rows": rows},
                                        ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
