"""CLI 参数默认值的漂移门禁（2026-10-30 加）。

**为什么需要它**：`TOOLS.md` 与 `mcp_server.py` 有 `test_tools_doc.py` 逐参数对账，
但 `USAGE.md` 与 `cli.py` 之间此前没有任何门禁。实测变异：把 `search --limit` 的默认
从 20 改成 99，`test_cli.py` 的 119 条**全绿**——而 `USAGE.md` 里写死了 5 处参数默认值，
它们会静默漂移，没有任何东西会报警。（对照：同期把某个 MCP 工具的默认值改掉，
`test_tools_doc.py` 立刻红。）

本文件两道闸：

1. `test_defaults_match_snapshot`：全部子命令（含二级）的 argparse default 存进
   `cli_defaults.json`。任何变动都必须显式更新快照，那份 JSON 的 diff 会进代码审查。
2. `test_usage_documented_defaults`：`USAGE.md` 里写死的 5 处「默认 N」逐条钉住。

**更新快照**（有意改动默认值时）：
    AW_UPDATE_CLI_SNAPSHOT=1 uv run pytest tests/test_cli_doc.py
然后把 `cli_defaults.json` 的 diff 连同 `USAGE.md` 的对应改动一起提交。
"""
import argparse
import json
import os
import pathlib

import pytest

from agent_wechat import cli

HERE = pathlib.Path(__file__).resolve().parent
SNAPSHOT = HERE / "cli_defaults.json"


def _parser(monkeypatch) -> argparse.ArgumentParser:
    """截获 `cli.main()` 内部构建的那份 parser。

    parser 目前内联在 `main()` 里（约 270 行）。抽成 `build_parser()` 要把整段搬移，
    纯位移的改动风险大于收益，所以在这里截获：`main()` 走到 `parse_args` 时把 self
    交出来，再立刻中止解析。
    """
    captured: dict[str, argparse.ArgumentParser] = {}

    def fake_parse_args(self, *args, **kwargs):
        captured["p"] = self
        raise SystemExit(0)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", fake_parse_args)
    with pytest.raises(SystemExit):
        cli.main([])
    return captured["p"]


def _defaults(parser: argparse.ArgumentParser) -> dict[str, dict[str, object]]:
    """→ {"chat export": {"limit": 500, ...}, ...}，含二级子命令。"""
    out: dict[str, dict[str, object]] = {}

    def walk(p: argparse.ArgumentParser, prefix: str) -> None:
        for action in p._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            for name, sub in action.choices.items():
                path = f"{prefix} {name}".strip()
                opts = {a.dest: a.default for a in sub._actions
                        if a.option_strings and a.dest != "help"}
                if opts:
                    out[path] = opts
                walk(sub, path)

    walk(parser, "")
    return out


def test_defaults_match_snapshot(monkeypatch):
    """任何子命令参数的默认值变化都必须显式更新快照（见模块 docstring 的更新方式）。"""
    got = _defaults(_parser(monkeypatch))

    if os.environ.get("AW_UPDATE_CLI_SNAPSHOT") == "1":
        SNAPSHOT.write_text(
            json.dumps(got, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        pytest.skip("已更新快照：%s" % SNAPSHOT.name)

    assert SNAPSHOT.exists(), "快照缺失，先跑 AW_UPDATE_CLI_SNAPSHOT=1"
    want = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    changed = {k: (want.get(k), v) for k, v in got.items() if want.get(k) != v}
    removed = {k: v for k, v in want.items() if k not in got}
    assert not changed and not removed, (
        "CLI 默认值变了（子命令 → {旧, 新}）：%s\n"
        "移除的子命令：%s\n"
        "如果是有意的：更新 USAGE.md 的对应描述，再跑 "
        "`AW_UPDATE_CLI_SNAPSHOT=1 uv run pytest tests/test_cli_doc.py` 并把快照 diff 一起提交。"
        % (changed, removed))


# ------------------------------------------------------------------ USAGE.md 的写死默认值
# 这五处是 USAGE.md 里**直接写给用户看**的参数默认值。代码改了而文档没改，用户照着
# 文档用就会得到不同行为。快照那条只能证明「默认值变了且有人按了头」，这条证明
# 「文档跟着变了」——两条互补，不是重复。
#
# 格式：(子命令路径, 参数 dest, USAGE.md 里印的数字, USAGE.md 行号提示)
USAGE_DOCUMENTED = [
    ("semantic", "n", 10, "USAGE.md §4 参数表：`-n N` 返回条数，默认 10"),
    ("moments export", "limit", 2000, "USAGE.md §? 参数表：`--limit N` 默认 2000"),
    ("media export", "limit", 2000, "USAGE.md §? `--limit` 默认 2000（数的是本次新增）"),
    ("media locate", "limit", 200, "USAGE.md §? 媒体归属：条数上限 1-500，默认 200"),
    ("chat export", "limit", 500, "USAGE.md §? 时间线导出：封顶最近的 N 条，默认 500"),
]


@pytest.mark.parametrize("cmd,dest,documented,where", USAGE_DOCUMENTED,
                         ids=[c.replace(" ", "-") for c, *_ in USAGE_DOCUMENTED])
def test_usage_documented_defaults(monkeypatch, cmd, dest, documented, where):
    """USAGE.md 印出来的默认值必须与 argparse 一致。"""
    defaults = _defaults(_parser(monkeypatch))
    assert cmd in defaults, f"USAGE 写到的子命令 {cmd!r} 在 CLI 里不存在"
    assert dest in defaults[cmd], f"{cmd} 没有参数 {dest!r}"
    assert defaults[cmd][dest] == documented, (
        f"{where}：文档写 {documented}，代码是 {defaults[cmd][dest]}")
