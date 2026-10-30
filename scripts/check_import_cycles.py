#!/usr/bin/env python3
"""运行时 import 环检查（spec §7①）。**只用标准库**，退出码 0/1。

把 import 边按下面的三分类语义处理：
把 `src/agent_wechat/*.py` 之间的 import 边分成三类，**只有第一类进环检查**：

  · `rt` —— 运行时**顶层**的 `from . import x` / `from .x import y`（模块导入时就执行）；
  · `tc` —— `if TYPE_CHECKING:` 块里的（只给类型标注用，运行时不执行）；
  · `lz` —— 函数体里的惰性 import（调用时才执行）。

后两类**不是**运行时环：`chatview` 对 `usecases`/`messages` 的依赖正是靠它们断开的
（模块头写着为什么），把它们算进来自造环。三分类一起打出来，是为了让「为什么这个环
不算环」当场可读——只报一个「有环」而不说分类，读的人要么误删惰性 import，要么去改
本来正确的结构。

为什么需要它：模块之间的环在 Python 里**不报错**——`import a` 先注册半成品模块，
`a` 再 `import b`、`b` 回头 `import a` 时拿到的是那个半成品，于是「有时能跑、换个入口
就 AttributeError」。本仓已经有两条靠注释维持的边界（`chatview` 的反向依赖、
`usecases` 对模板的别名导入），没有闸时只能靠人记住。
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent / "src" / "agent_wechat"


def _is_type_checking(node: ast.expr) -> bool:
    return ((isinstance(node, ast.Name) and node.id == "TYPE_CHECKING")
            or (isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"))


def classify(path: pathlib.Path) -> dict[str, set[str]]:
    """一个模块的包内出边，按 rt / tc / lz 三分类。"""
    buckets: dict[str, set[str]] = {"rt": set(), "tc": set(), "lz": set()}

    def add(ctx: str, names: list[str]) -> None:
        buckets[ctx].update(n for n in names if (ROOT / f"{n}.py").exists())

    def walk(stmts: list[ast.stmt], ctx: str) -> None:
        for s in stmts:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # 函数体里的 import 是惰性的（含 async）；类体里的按外层上下文算
                walk(s.body, "lz" if ctx == "rt" else ctx)
            elif isinstance(s, ast.If) and _is_type_checking(s.test):
                for sub in s.body:
                    walk([sub], "tc")
            else:
                if isinstance(s, ast.ImportFrom) and s.level == 1:
                    ns = ([s.module.split(".")[0]] if s.module
                          else [a.name.split(".")[0] for a in s.names])
                    add(ctx, ns)
                elif isinstance(s, ast.Import):
                    add(ctx, [a.name.split(".")[1] for a in s.names
                              if a.name.startswith("agent_wechat.")])
                walk([n for n in ast.iter_child_nodes(s) if isinstance(n, ast.stmt)], ctx)

    walk(ast.parse(path.read_text()).body, "rt")
    return buckets


def runtime_cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan 求强连通分量，返回大小 > 1 的那些（自环另有检查）。"""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = [0]
    found: list[list[str]] = []
    sys.setrecursionlimit(10000)

    def strong(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in edges.get(v, ()):
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                found.append(sorted(comp))

    for v in edges:
        if v not in index:
            strong(v)
    return found


def main() -> int:
    mods = {p.stem: classify(p) for p in sorted(ROOT.glob("*.py"))}
    rt = {m: v["rt"] for m, v in mods.items()}
    cycles = runtime_cycles(rt)
    self_loops = sorted(m for m, deps in rt.items() if m in deps)
    print(f"模块 {len(mods)} 个 | 运行时顶层边 {sum(len(v) for v in rt.values())} 条 | "
          f"TYPE_CHECKING 边 {sum(len(v['tc']) for v in mods.values())} 条 | "
          f"函数内惰性边 {sum(len(v['lz']) for v in mods.values())} 条")
    bad = cycles or self_loops
    if self_loops:
        print(f"❌ 运行时自环：{'、'.join(self_loops)}", file=sys.stderr)
    for comp in cycles:
        print(f"❌ 运行时 import 环：{' → '.join(comp)} → {comp[0]}", file=sys.stderr)
        for m in comp:
            back = sorted(d for d in comp if d in rt.get(m, ()))
            print(f"     {m} → {back}", file=sys.stderr)
    # `import mcp` 只许出现在 mcp_server：无 mcp 依赖也要能跑其余命令（spec §7）
    leaked = [p.stem for p in sorted(ROOT.glob("*.py"))
              if p.stem != "mcp_server"          # 它自己就该有
              and re.search(r"^\s*(from|import)\s+mcp\b", p.read_text(), re.M)]
    if leaked:
        print(f"❌ 只有 mcp_server.py 可以 import mcp，实际还有：{'、'.join(leaked)}",
              file=sys.stderr)
    if bad or leaked:
        return 1
    print("✅ 运行时无环；`import mcp` 只在 mcp_server.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
