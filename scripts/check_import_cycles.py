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
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent / "src" / "agent_wechat"


def _is_type_checking(node: ast.expr) -> bool:
    return ((isinstance(node, ast.Name) and node.id == "TYPE_CHECKING")
            or (isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"))


def _is_package(dir_: pathlib.Path) -> bool:
    return (dir_ / "__init__.py").exists()


def classify(path: pathlib.Path) -> dict[str, set[str]]:
    """一个模块的出边，按 rt / tc / lz 三分类。

    **包内的文件要按相对层级解析**，不能只看 `level == 1`：
    `usecases/chat.py` 里 `from ._core import …`（level 1）指向**包内兄弟**
    `usecases._core`，而 `from .. import errors`（level 2）指向**顶层** `errors`。
    两者混为一谈时，包内的边要么丢失、要么被错记成同名顶层模块（`from .media import …`
    曾因此记成顶层 `media`，而 `usecases.media` 从不出现）。
    """
    buckets: dict[str, set[str]] = {"rt": set(), "tc": set(), "lz": set()}

    # 本文件所在的包（顶层文件没有）。包内的相对 import 以此为基准向上数。
    pkg_dir = path.parent if path.parent != ROOT and _is_package(path.parent) else None
    pkg_name = pkg_dir.name if pkg_dir else None

    def resolve(level: int, module: str | None, names: list[str]) -> list[str]:
        """把一条 `from <level dots><module> import <names>` 解析成模块身份列表。"""
        # `level` 是相对 import 的点数：1 = 当前包，2 = 上一级……
        base = pkg_dir
        up = level - 1
        for _ in range(up):
            base = base.parent if base else None
        if base is not None and base == ROOT:
            base = None                       # 升到了 src/agent_wechat 这一层 = 顶层模块
        if module:
            # `from .pkg import x` / `from .. import y` 里 module 段本身可能就是目标
            if base is not None and pkg_name and level == 1:
                return [f"{pkg_name}.{module.split('.')[0]}"]
            return [module.split(".")[0]]
        # 无 module 段：`from . import a, b` —— 每个 name 各是一个目标
        out = []
        for n in names:
            head = n.split(".")[0]
            if level == 1 and pkg_dir is not None:
                out.append(f"{pkg_name}.{head}" if (pkg_dir / f"{head}.py").exists()
                           else head)
            else:
                out.append(head)
        return out

    def add(ctx: str, names: list[str]) -> None:
        for n in names:
            if "." in n:                       # 已经带包名的身份
                pkg_part, _, rest = n.partition(".")
                if (ROOT / f"{pkg_part}.py").exists() or (
                        (ROOT / pkg_part).is_dir() and _is_package(ROOT / pkg_part)):
                    buckets[ctx].add(n)
                continue
            # 顶层模块：`x.py`
            if (ROOT / f"{n}.py").exists():
                buckets[ctx].add(n)
            # 顶层**包**：`from . import usecases`（`cli.py` 的 16 处）
            elif (ROOT / n).is_dir() and _is_package(ROOT / n):
                buckets[ctx].add(n)

    def walk(stmts: list[ast.stmt], ctx: str) -> None:
        for s in stmts:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # 函数体里的 import 是惰性的（含 async）；类体里的按外层上下文算
                walk(s.body, "lz" if ctx == "rt" else ctx)
            elif isinstance(s, ast.If) and _is_type_checking(s.test):
                for sub in s.body:
                    walk([sub], "tc")
                if s.orelse:
                    walk(s.orelse, ctx)
            else:
                if isinstance(s, ast.ImportFrom):
                    if s.level >= 1:
                        add(ctx, resolve(s.level, s.module, [a.name for a in s.names]))
                    elif s.module == "agent_wechat":
                        add(ctx, [a.name.split(".")[0] for a in s.names])
                    elif s.module and s.module.startswith("agent_wechat."):
                        rel = s.module.removeprefix("agent_wechat.")
                        add(ctx, [rel])
                        if (ROOT / rel).is_dir() and _is_package(ROOT / rel):
                            add(ctx, [f"{rel}.{a.name}" for a in s.names
                                      if (ROOT / rel / f"{a.name}.py").exists()])
                elif isinstance(s, ast.Import):
                    add(ctx, [a.name.removeprefix("agent_wechat.") for a in s.names
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


def _imports_mcp(path: pathlib.Path) -> bool:
    """AST 检查该文件是否导入了 `mcp` 包（覆盖 `import os, mcp` 与所有子包）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "mcp" or a.name.startswith("mcp.") for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "mcp" or node.module.startswith("mcp."):
                return True
    return False


def main() -> int:
    # **包内的文件也要扫**，且 `pkg/__init__.py` 以包名 `pkg` 作为节点身份，使
    # `from . import usecases` 的目标 `"usecases"` 与 `usecases/__init__.py` 的出边连通。
    sources = {p.stem: p for p in sorted(ROOT.glob("*.py"))}
    for pkg_dir in sorted(p for p in ROOT.iterdir() if p.is_dir() and
                          (p / "__init__.py").exists()):
        for f in sorted(pkg_dir.glob("*.py")):
            key = pkg_dir.name if f.stem == "__init__" else f"{pkg_dir.name}.{f.stem}"
            sources[key] = f
    mods = {name: classify(p) for name, p in sources.items()}
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
    # `import mcp` 只许出现在 mcp_server：覆盖顶层与全部子包模块
    leaked = [name for name, p in sorted(sources.items())
              if name != "mcp_server" and _imports_mcp(p)]
    if leaked:
        print(f"❌ 只有 mcp_server.py 可以 import mcp，实际还有：{'、'.join(leaked)}",
              file=sys.stderr)
    if bad or leaked:
        return 1
    print("✅ 运行时无环；`import mcp` 只在 mcp_server.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
