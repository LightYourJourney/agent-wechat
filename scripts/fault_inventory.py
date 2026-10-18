"""A1：独立 AST 枚举写/执行边界，生成并校验冻结表（spec §4.1）。"""
from __future__ import annotations
import argparse, ast, collections, csv, io, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "agent_wechat"
Site = collections.namedtuple("Site", "rel line api kind qualname")

# 全局故障类（spec §4.3）；boundary kind → 适用集合
FAULTS = ["ENOSPC", "EACCES", "EIO", "EINTR", "SHORT_WRITE", "REPLACE_FAIL",
          "SQLITE_CORRUPT", "SQLITE_LOCKED", "SUBPROCESS_START_FAIL",
          "SUBPROCESS_NONZERO", "SUBPROCESS_TIMEOUT",
          "NETWORK_CONNECT_FAIL", "NETWORK_TIMEOUT", "NETWORK_TRUNCATED",
          "GATE_FAULT", "ELOOP", "SIGKILL"]
APPLICABLE = {
    "file_write": {"ENOSPC", "EACCES", "EIO", "EINTR", "SHORT_WRITE", "SIGKILL"},
    "meta": {"ENOSPC", "EACCES", "EIO", "SIGKILL"},
    "replace": {"EACCES", "EIO", "ENOSPC", "REPLACE_FAIL", "SIGKILL"},
    "copy": {"ENOSPC", "EACCES", "EIO", "SIGKILL"},
    "sqlite": {"SQLITE_CORRUPT", "SQLITE_LOCKED", "ENOSPC", "EACCES", "SIGKILL"},
    "subprocess": {"SUBPROCESS_START_FAIL", "SUBPROCESS_NONZERO",
                   "SUBPROCESS_TIMEOUT", "SIGKILL"},
    "network": {"NETWORK_CONNECT_FAIL", "NETWORK_TIMEOUT", "NETWORK_TRUNCATED"},
    "gate": {"GATE_FAULT", "EACCES", "EIO", "ELOOP", "SIGKILL"},
}
KEYWORDS = {"ENOSPC": "空间|No space left", "EACCES": "权限|Permission denied",
            "EIO": "I/O|Input/output", "EINTR": "中断|Interrupted",
            "SHORT_WRITE": "空间|No space left", "REPLACE_FAIL": "权限|Permission denied",
            "SQLITE_CORRUPT": "损坏|malformed", "SQLITE_LOCKED": "锁定|locked",
            "SUBPROCESS_START_FAIL": "命令|No such file",
            "SUBPROCESS_NONZERO": "命令|failed",
            "SUBPROCESS_TIMEOUT": "超时|timeout",
            "NETWORK_CONNECT_FAIL": "网络|network",
            "NETWORK_TIMEOUT": "超时|timeout",
            "NETWORK_TRUNCATED": "截断|truncated",
            "GATE_FAULT": "拒绝|权限|Permission denied",
            "ELOOP": "拒绝|loop|符号链接", "SIGKILL": ""}

_FS = {"write_text": "file_write", "write_bytes": "file_write",
       "mkdir": "meta", "chmod": "meta", "unlink": "meta"}
_SHUTIL = {"rmtree": "meta", "copy2": "copy"}
_OS = {"replace": "replace", "unlink": "meta", "mkdir": "meta", "chmod": "meta"}

class _V(ast.NodeVisitor):
    def __init__(self, rel): self.rel, self.stack, self.out = rel, [], []
    def visit_FunctionDef(self, n):
        self.stack.append(n.name)
        if n.name == "fetch" and self.rel == "sns_media.py":
            self.out.append(Site(self.rel, n.lineno, "fetch", "network", "fetch"))
        self.generic_visit(n)
        self.stack.pop()
    visit_AsyncFunctionDef = visit_FunctionDef
    def _add(self, n, api, kind):
        self.out.append(Site(self.rel, n.lineno, api, kind, ".".join(self.stack) or "<module>"))
    def visit_Call(self, n):
        f = n.func
        if isinstance(f, ast.Attribute):
            api = f.attr
            if isinstance(f.value, ast.Name) and f.value.id == "os" and api in _OS:
                self._add(n, "os." + api, _OS[api])
            elif isinstance(f.value, ast.Name) and f.value.id == "subprocess" \
                    and api in ("run", "Popen", "call", "check_output"):
                self._add(n, "subprocess." + api, "subprocess")
            elif isinstance(f.value, ast.Name) and f.value.id == "sqlite3" \
                    and api == "connect":
                self._add(n, "sqlite3.connect", "sqlite")
            elif api in ("commit", "executescript"):
                self._add(n, api, "sqlite")
            elif api in ("resolve", "samefile", "exists") \
                    and "in_wechat_container" in self.stack:
                self._add(n, api, "gate")
            elif api in _FS:
                self._add(n, api, _FS[api])
            elif api in _SHUTIL:
                self._add(n, "shutil." + api, _SHUTIL[api])
            if any(isinstance(x, ast.Constant) and isinstance(x.value, str)
                   and "ATTACH DATABASE" in x.value for x in n.args):
                self._add(n, "attach", "sqlite")
        if isinstance(f, ast.Name) and f.id == "urlopen":
            self._add(n, "urlopen", "network")
        self.generic_visit(n)

def iter_sites(root: pathlib.Path = SRC) -> list[Site]:
    sites = []
    for p in sorted(root.glob("*.py")):
        v = _V(p.name)
        v.visit(ast.parse(p.read_text(encoding="utf-8")))
        sites += v.out
    return sites

def expand(sites) -> list[dict]:
    rows = []
    for s in sites:
        for fc in FAULTS:
            ok = fc in APPLICABLE[s.kind]
            rows.append({"flow": "", "boundary_id": f"{s.rel}:{s.line}:{s.api}",
                         "file": s.rel, "line": s.line, "fault_class": fc,
                         "applicability": "applicable" if ok else "n/a",
                         "reason": "" if ok else f"{s.kind} 边界不执行 {fc}",
                         "expected_keyword": KEYWORDS[fc] if ok else "",
                         "case_id": ""})
    return rows

def check(path: pathlib.Path, root: pathlib.Path = SRC) -> int:
    with path.open(newline="", encoding="utf-8") as f:
        table = list(csv.DictReader(f))
    enum = expand(iter_sites(root))
    a = {(r["boundary_id"], r["fault_class"]) for r in enum}
    b = {(r["boundary_id"], r["fault_class"]) for r in table}
    missing, extra = a - b, b - a
    if missing or extra:
        print(f"❌ 冻结表与枚举不一致：缺 {len(missing)} 条 / 多 {len(extra)} 条", file=sys.stderr)
        for x in sorted(missing)[:10]: print("  缺:", x, file=sys.stderr)
        for x in sorted(extra)[:10]: print("  多:", x, file=sys.stderr)
        return 2
    print(f"✅ 边界表一致：枚举 {len(a)} / 表 {len(b)} / 差集 {len(missing) + len(extra)}")
    return 0

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit", action="store_true")
    ap.add_argument("--write", type=pathlib.Path)
    ap.add_argument("--check", type=pathlib.Path)
    a = ap.parse_args(argv)
    if a.check: return check(a.check)
    rows = expand(iter_sites())
    buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    if a.write: a.write.write_text(buf.getvalue(), encoding="utf-8")
    else: print(buf.getvalue(), end="")
    return 0

if __name__ == "__main__":
    sys.exit(main())
