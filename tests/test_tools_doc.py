"""TOOLS.md ↔ 13 个 MCP 工具的 wire 签名对账（F5，T8 复审）。

此前这条对账只是一次性脚本：把 §9 的 `limit` 默认列改成 2000、或删掉一整行参数行，
全量 1117 条**全绿**——文档参数表在仓内没有任何门禁。本文件把它钉成测试：

- 每个工具的**每个形参**必须出现在它那一节的参数表里（行序不限）；
- 表里的**默认值列**必须与签名一致（`—` = 必填无默认）；
- 默认值格子解析不出来就**失败**（不许静默跳过——表格式演进时要有人来看一眼）。
"""
import inspect
import pathlib
import re

from agent_wechat import mcp_server

TOOLS_MD = pathlib.Path(__file__).resolve().parents[1] / "docs" / "TOOLS.md"


def _sections() -> dict[str, str]:
    doc = TOOLS_MD.read_text()
    out: dict[str, str] = {}
    for m in re.finditer(r"^## (\d+)\. (\w+).*$", doc, re.M):
        start = m.end()
        nxt = re.search(r"^## \d+\. ", doc[start:], re.M)
        out[m.group(2)] = doc[start:start + (nxt.start() if nxt else len(doc))]
    return out


def _tools() -> dict[str, object]:
    out: dict[str, object] = {}
    for name in dir(mcp_server):
        fn = getattr(mcp_server, name)
        if (callable(fn) and getattr(fn, "__module__", "") == "agent_wechat.mcp_server"
                and hasattr(fn, "__wrapped__")):
            out[name] = fn
    return out


def _param_rows(section: str) -> dict[str, str]:
    """一节里的参数表：`| 参数 | 类型 | …` 表头之后的连续行 → {参数名: 默认值格子}。"""
    m = re.search(r"^\| 参数 \|.*?\n\|[^\n]*\|\n((?:\|[^\n]*\|\n)+)", section, re.M)
    if m is None:
        return {}
    return {r.group(1): r.group(4).strip() for r in re.finditer(
        r"^\| `(\w+)` \|([^|]*)\|([^|]*)\|([^|]*)\|", m.group(1), re.M)}


def _parse_default(cell: str):
    c = cell.strip()
    if c in ("—", "-"):
        return inspect.Parameter.empty
    if c.startswith("`") and c.endswith("`"):
        inner = c[1:-1]
        if inner.startswith('"') and inner.endswith('"'):
            return inner[1:-1]          # 带引号的字符串字面量（如 kinds 的默认值）
        c = inner                       # 反引号里的裸字面量：null/true/false/数字
    if c == "null":
        return None
    if c == "true":
        return True
    if c == "false":
        return False
    if re.fullmatch(r"-?\d+", c):
        return int(c)
    raise AssertionError(f"看不懂的默认值格子 {cell!r}——表格式变了就来更新这个解析器")


def test_every_tool_parameter_and_default_matches_TOOLS_md():
    sections, tools = _sections(), _tools()
    assert len(tools) == 13, sorted(tools)
    problems: list[str] = []
    for name, fn in sorted(tools.items()):
        section = sections.get(name)
        if section is None:
            problems.append(f"{name}: TOOLS.md 里没有对应小节")
            continue
        rows = _param_rows(section)
        if not rows:
            problems.append(f"{name}: 小节里找不到参数表")
            continue
        for pname, param in inspect.signature(fn).parameters.items():
            if pname not in rows:
                problems.append(f"{name}.{pname}: 参数表里没有这一行")
                continue
            want = _parse_default(rows[pname])
            if want is not param.default and want != param.default:
                problems.append(
                    f"{name}.{pname}: 文档默认 {want!r} ≠ 签名默认 {param.default!r}")
    assert problems == [], problems
