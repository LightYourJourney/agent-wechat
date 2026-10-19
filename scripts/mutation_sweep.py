"""B 阶段变异器：AST 级 census / dry-run / run。

**位点定位按 (qualname, op, segment) + 行列**——指纹不含行号，于是「同一个逻辑位点」在
源码上下挪几行之后指纹不变（测试 `test_fingerprint_stable_across_line_shift` 钉这条）。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
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


def classify(rc: int, out: str) -> str:
    """runner 的五态（spec §6.3）。

    `timeout` 单列：被杀不算「断言杀死」，否则把超时调短就能伪造全杀。
    `invalid` 覆盖「收集失败 / 导入错误 / 用例体之外的崩溃」——它们不是被断言打死的。
    """
    if rc == -9:
        return "timeout"
    if rc == 0:
        return "survived"
    if "ERROR collecting" in out or "ERROR " in out or rc in (2, 5):
        return "invalid"
    if "FAILED" in out:
        return "killed"
    return "invalid"


def run_sweep(module: str, mapping: dict, copy_root: pathlib.Path, timeout: float,
              manifest: set[str]) -> list[dict]:
    """在 `copy_root`（git archive 解出的副本）里逐个变异并跑映射用例。

    每次变异后**立刻还原源文件**：一个变异体的失败不该污染下一个。
    结束时断言「跑过的指纹集合 == dry-run 集合」——这是「集合自报」那条风险的唯一防线。
    """
    import subprocess

    src = copy_root / "src" / "agent_wechat" / f"{module}.py"
    original = src.read_text(encoding="utf-8")
    tests = mapping[module]
    out_rows = []
    for site in iter_sites(original):
        fp = fingerprint(module, site)
        if fp not in manifest:
            continue
        try:
            mutated = mutate(original, site)
        except AssertionError:
            out_rows.append({"fp": fp, "status": "invalid", "why": "位点定位失败"})
            continue
        src.write_text(mutated, encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(copy_root / "src"),
                   PYTHONPYCACHEPREFIX=str(copy_root / ".pycache"),
                   PYTHONDONTWRITEBYTECODE="1")
        try:
            r = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", *tests],
                               cwd=copy_root, env=env, capture_output=True, text=True,
                               timeout=timeout)
            status = classify(r.returncode, r.stdout + r.stderr)
            out_rows.append({"fp": fp, "status": status,
                             "out": (r.stdout + r.stderr)[-400:]})
        except subprocess.TimeoutExpired:
            out_rows.append({"fp": fp, "status": "timeout"})
        finally:
            src.write_text(original, encoding="utf-8")
    got = {r["fp"] for r in out_rows}
    assert got == set(manifest), "kill 矩阵键集合与 dry-run 集合不等"
    return out_rows


def main_run(argv):
    """--run MODULE --manifest FILE [--only-fps f1,f2] --timeout S"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--only-fps", default="")
    ap.add_argument("--timeout", type=float, default=300)
    a = ap.parse_args(argv)
    manifest = {json.loads(line)["fp"] for line in
                pathlib.Path(a.manifest).read_text().splitlines() if line.strip()}
    if a.only_fps:
        manifest &= set(a.only_fps.split(","))
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tests"))
    from b_mapping import MAPPING
    copy_root = pathlib.Path(os.environ["AW_COPY"])
    rows = run_sweep(a.run, MAPPING, copy_root, a.timeout, manifest)
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if "--run" in sys.argv[1:]:
        sys.exit(main_run(sys.argv[1:]))
    sys.exit(main())
