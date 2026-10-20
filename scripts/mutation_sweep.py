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

class _Walker(ast.NodeVisitor):
    """**唯一的**位点枚举。`iter_sites` 与 `mutate` 都从这里取位点。

    此前是两份（`_V` 供 census、`mutate` 里再写一个 `C`），已经分叉过：边界算子判据
    只在 `_V` 那侧。分叉的后果不报错、只让两边悄悄看不同的位点集合。
    """

    def __init__(self) -> None:
        self.stack: list[str] = []
        self.sites: list[tuple[str, str, str, ast.AST]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _q(self) -> str:
        return ".".join(self.stack) or "<module>"

    @staticmethod
    def _doc(node: ast.Expr) -> bool:
        return (isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str))

    def visit_Compare(self, node: ast.Compare) -> None:
        if len(node.ops) == 1 and isinstance(node.ops[0],
                                             (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
            self.sites.append((self._q(), "cmp_boundary", ast.unparse(node), node))
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self.sites.append((self._q(), "bool_op", type(node.op).__name__, node))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, (int, bool)) and not isinstance(node.value, str):
            self.sites.append((self._q(), "const", repr(node.value), node))

    def visit_Expr(self, node: ast.Expr) -> None:
        if not self._doc(node):
            self.sites.append((self._q(), "delete_stmt", ast.unparse(node), node))
            self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        self.sites.append((self._q(), "delete_stmt", ast.unparse(node), node))
        self.generic_visit(node)


def _pos(node: ast.AST) -> tuple[int, int]:
    """位点的 (行, 列)。四种算子都落在带位置的节点上，但 `ast.AST` 泛型上没有这两个属性。"""
    return getattr(node, "lineno", 0), getattr(node, "col_offset", 0)


def _walk(src: str) -> tuple[ast.Module, list[tuple[str, str, str, ast.AST]]]:
    tree = ast.parse(src)
    w = _Walker()
    w.visit(tree)
    return tree, w.sites


def iter_sites(src: str) -> list[dict]:
    seen: dict[tuple[str, str, str], int] = {}
    out: list[dict] = []
    for i, (q, op, seg, node) in enumerate(_walk(src)[1]):
        # 同一函数里同形的位点很常见（两处 `return None`、两个 `0`）。没有序号，
        # 它们的指纹会**塌缩成一个**——kill 矩阵的集合对等断言随之失效（实测
        # 1235 个位点会塌成 1081 个）。序号按遍历序编号，与行号无关。
        n = seen.get((q, op, seg), 0)
        seen[(q, op, seg)] = n + 1
        line, col = _pos(node)
        out.append({"index": i, "qualname": q, "op": op, "segment": seg,
                    "ordinal": n, "line": line, "col": col})
    return out


def fingerprint(module: str, site: dict) -> str:
    """位点指纹。**不含行号/列号**：同一个逻辑位点在源码挪行之后仍是同一个变异体。"""
    raw = "|".join([module, site["qualname"], site["op"], site["segment"],
                    str(site["ordinal"])])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _to_pass(tree: ast.Module, target: ast.AST) -> None:
    """把 `target` 这条语句换成 `pass`（**按对象身份**，不按行列再找一遍）。"""

    class _Replace(ast.NodeTransformer):
        def generic_visit(self, node: ast.AST) -> ast.AST:
            for field, value in ast.iter_fields(node):
                if (isinstance(value, list) and value
                        and isinstance(value[0], ast.stmt)):
                    setattr(node, field,
                            [ast.Pass() if st is target else st for st in value])
            return super().generic_visit(node)

    _Replace().visit(tree)


def mutate(src: str, site: dict) -> str:
    """按位点产出一个变异体（返回变异后的源码文本）。"""
    tree, sites = _walk(src)
    target = [n for _q, op, _seg, n in sites
              if op == site["op"] and _pos(n) == (site["line"], site["col"])]
    assert len(target) == 1, f"位点定位失败：{site}"
    node = target[0]

    if site["op"] == "delete_stmt":
        _to_pass(tree, node)
        return ast.unparse(tree)
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


def main(argv: list[str] | None = None) -> int:
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
    """runner 的五态（spec §6.3）：killed / survived / timeout / invalid / infra。

    `timeout` 单列：被杀不算「断言杀死」，否则把超时调短就能伪造全杀。
    `invalid` 覆盖「收集失败 / 导入错误 / 用例体之外的崩溃」——它们不是被断言打死的。
    `infra`（基建错误）是**跑不起来**：pytest 自身退出码 3（内部错误）或进程起不来。
    它与 `invalid` 分开，因为前者说明**工具/环境**坏了，后者说明**变异体**坏了——
    混成一类会让「工具静默失效」看起来像「变异体被拦下」。
    """
    if rc == -9:
        return "timeout"
    if rc == 0:
        return "survived"
    if rc == 3:                              # pytest 内部错误：与变异体无关
        return "infra"
    if "ERROR " in out or rc in (2, 5):      # 收集/夹具错误：不是被断言打死的
        return "invalid"
    if "FAILED" in out:
        return "killed"
    return "invalid"


def failure_kind(status: str, out: str) -> str:
    """spec §6.3 的词表：assert / body-error / setup-error / collection-error /
    import-error / timeout。未杀死（survived）记 `-`。"""
    if status == "survived":
        return "-"
    if status == "timeout":
        return "timeout"
    if status == "infra":
        return "body-error"                  # 进程级失败，不是断言
    if "ERROR collecting" in out:
        return "collection-error"
    if "ImportError" in out or "ModuleNotFoundError" in out:
        return "import-error"
    if "ERROR at setup" in out or "error at setup" in out:
        return "setup-error"
    if status == "killed" and ("AssertionError" in out or "assert " in out):
        return "assert"
    return "body-error"


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
            both = r.stdout + r.stderr
            status = classify(r.returncode, both)
            out_rows.append({"fp": fp, "status": status,
                             "failure_kind": failure_kind(status, both),
                             "out": both[-400:]})
        except subprocess.TimeoutExpired:
            out_rows.append({"fp": fp, "status": "timeout"})
        finally:
            src.write_text(original, encoding="utf-8")
    got = {r["fp"] for r in out_rows}
    assert got == set(manifest), "kill 矩阵键集合与 dry-run 集合不等"
    assert len(out_rows) == len(manifest), "有指纹重复——集合对等断言的判据就不成立了"
    return out_rows


def main_run(argv: list[str]) -> int:
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
