"""B 阶段变异器：AST 级 census / dry-run / run。

**位点定位按 (qualname, op, segment) + 行列**——指纹不含行号，于是「同一个逻辑位点」在
源码上下挪几行之后指纹不变（测试 `test_fingerprint_stable_across_line_shift` 钉这条）。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import pathlib
import sys

SKIP = (ast.Import, ast.ImportFrom)


class _V(ast.NodeVisitor):
    def __init__(self):
        self.stack, self.sites = [], []

    def visit_FunctionDef(self, n):
        self.stack.append(n.name)
        self.generic_visit(n)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _q(self):
        return ".".join(self.stack) or "<module>"

    @staticmethod
    def _doc(n):
        return (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str))

    def visit_Compare(self, n):
        if len(n.ops) == 1 and isinstance(n.ops[0], (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
            self.sites.append((self._q(), "cmp_boundary", ast.unparse(n), n.lineno,
                               n.col_offset))
        self.generic_visit(n)

    def visit_BoolOp(self, n):
        self.sites.append((self._q(), "bool_op", type(n.op).__name__, n.lineno,
                           n.col_offset))
        self.generic_visit(n)

    def visit_Constant(self, n):
        if isinstance(n.value, (int, bool)) and not isinstance(n.value, str):
            self.sites.append((self._q(), "const", repr(n.value), n.lineno, n.col_offset))

    def visit_Expr(self, n):
        if not self._doc(n):
            self.sites.append((self._q(), "delete_stmt", ast.unparse(n), n.lineno,
                               n.col_offset))
            self.generic_visit(n)

    def visit_Return(self, n):
        self.sites.append((self._q(), "delete_stmt", ast.unparse(n), n.lineno,
                           n.col_offset))
        self.generic_visit(n)


def iter_sites(src: str) -> list[dict]:
    tree = ast.parse(src)
    v = _V()
    v.visit(tree)
    return [{"index": i, "qualname": q, "op": op, "segment": seg, "line": line,
             "col": col}
            for i, (q, op, seg, line, col) in enumerate(v.sites)]


def fingerprint(module: str, site: dict) -> str:
    """位点指纹。**不含行号/列号**：同一个逻辑位点在源码挪行之后仍是同一个变异体。"""
    raw = "|".join([module, site["qualname"], site["op"], site["segment"]])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def mutate(src: str, site: dict) -> str:
    """按位点产出一个变异体（返回变异后的源码文本）。"""
    tree = ast.parse(src)
    nodes = []

    class C(ast.NodeVisitor):
        def visit_Compare(self, n):
            if len(n.ops) == 1:
                nodes.append(("cmp_boundary", n))
            self.generic_visit(n)

        def visit_BoolOp(self, n):
            nodes.append(("bool_op", n))
            self.generic_visit(n)

        def visit_Constant(self, n):
            if isinstance(n.value, (int, bool)) and not isinstance(n.value, str):
                nodes.append(("const", n))

        def visit_Expr(self, n):
            if not (isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str)):
                nodes.append(("delete_stmt", n))
            self.generic_visit(n)

        def visit_Return(self, n):
            nodes.append(("delete_stmt", n))
            self.generic_visit(n)

    C().visit(tree)

    if site["op"] == "delete_stmt":
        class Del(ast.NodeTransformer):
            def generic_visit(self, node):
                for field, value in ast.iter_fields(node):
                    if (isinstance(value, list) and value
                            and isinstance(value[0], ast.stmt)):
                        new = []
                        for st in value:
                            if (getattr(st, "lineno", 0) == site["line"]
                                    and getattr(st, "col_offset", 0) == site["col"]):
                                new.append(ast.Pass())
                            else:
                                new.append(st)
                        setattr(node, field, new)
                return super().generic_visit(node)

        return ast.unparse(Del().visit(tree))

    target = [n for op, n in nodes if op == site["op"]
              and getattr(n, "lineno", 0) == site["line"]
              and getattr(n, "col_offset", 0) == site["col"]]
    assert len(target) == 1, f"位点定位失败：{site}"
    node = target[0]
    if site["op"] == "cmp_boundary":
        flip = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt}
        node.ops[0] = flip[type(node.ops[0])]()
    elif site["op"] == "bool_op":
        node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
    elif site["op"] == "const":
        node.value = (not node.value) if isinstance(node.value, bool) else node.value + 1
    return ast.unparse(tree)


DEFAULT_MODULES = ["paths", "store", "purge", "usecases", "decrypt", "media",
                   "messages", "search", "tasks"]


def _src_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1] / "src" / "agent_wechat"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--census", action="store_true")
    ap.add_argument("--dry-run", nargs="*")
    a = ap.parse_args(argv)
    root = _src_root()
    mods = a.dry_run or DEFAULT_MODULES
    rows = []
    for m in mods:
        src = (root / f"{m}.py").read_text(encoding="utf-8")
        sites = iter_sites(src)
        rows.append({"module": m, "n": len(sites),
                     "fps": [fingerprint(m, s) for s in sites]})
    if a.census:
        for r in rows:
            print(f"{r['module']}\t{r['n']}")
        return 0
    for r in rows:
        for fp in r["fps"]:
            print(json.dumps({"module": r["module"], "fp": fp}))
    return 0


if __name__ == "__main__":
    if "--run" in sys.argv[1:]:
        print("--run 在 Task 12 实现", file=sys.stderr)
        sys.exit(2)
    sys.exit(main())
