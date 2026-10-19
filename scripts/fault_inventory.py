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
    # EIO（裁决 R2-A/控制器）：SQLite 的提交/连接失败可以以真实磁盘 I/O 错误的形态
    # 冒出来（tests/test_fault_injection.py::test_store_failure_preserves_existing_rows
    # 注入 commit EIO 并证明了这一点），原表把它标 n/a 低估了矩阵。
    "sqlite": {"SQLITE_CORRUPT", "SQLITE_LOCKED", "ENOSPC", "EACCES", "EIO", "SIGKILL"},
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
_OS = {"replace": "replace", "unlink": "meta", "mkdir": "meta", "chmod": "meta",
       # 后续写入发生在 fd 上，AST 看不见；把 fdopen 调用点本身记为 file_write 边界。
       "fdopen": "file_write"}
_SUBPROC = ("run", "Popen", "call", "check_output")

class _V(ast.NodeVisitor):
    def __init__(self, rel):
        self.rel, self.stack, self.out = rel, [], []
        # 模块级别名（如 `_run = subprocess.run`）：调用点按规范 API 名入表。
        self.aliases = {}
    def visit_Module(self, n):
        for node in n.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                t, v = node.targets[0], node.value
                if (isinstance(t, ast.Name) and isinstance(v, ast.Attribute)
                        and isinstance(v.value, ast.Name) and v.value.id == "subprocess"
                        and v.attr in _SUBPROC):
                    self.aliases[t.id] = "subprocess." + v.attr
        self.generic_visit(n)
    def visit_FunctionDef(self, n):
        self.stack.append(n.name)
        if n.name == "fetch" and self.rel == "sns_media.py":
            self.out.append(Site(self.rel, n.lineno, "fetch", "network", "fetch"))
        if n.name == "_fetch_cdn_bytes" and self.rel == "media.py":
            self.out.append(Site(self.rel, n.lineno, "_fetch_cdn_bytes", "network",
                                 "_fetch_cdn_bytes"))
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
                    and api in _SUBPROC:
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
        if isinstance(f, ast.Name) and f.id in self.aliases:
            self._add(n, self.aliases[f.id], "subprocess")
        elif isinstance(f, ast.Name) and f.id == "urlopen":
            self._add(n, "urlopen", "network")
        elif isinstance(f, ast.Name) and f.id == "open":
            mode = n.args[1] if len(n.args) >= 2 else None
            for kw in n.keywords:
                if kw.arg == "mode":
                    mode = kw.value
            if isinstance(mode, ast.Constant) and isinstance(mode.value, str) \
                    and any(c in mode.value for c in "wax"):
                self._add(n, "open", "file_write")
        self.generic_visit(n)

def iter_sites(root: pathlib.Path = SRC) -> list[Site]:
    sites = []
    for p in sorted(root.rglob("*.py")):
        v = _V(p.relative_to(root).as_posix())
        v.visit(ast.parse(p.read_text(encoding="utf-8")))
        sites += v.out
    return sites

def expand(sites) -> list[dict]:
    rows = []
    counts = collections.Counter((s.rel, s.line, s.api) for s in sites)
    seen = collections.Counter()
    for s in sites:
        key = (s.rel, s.line, s.api)
        seen[key] += 1
        base = f"{s.rel}:{s.line}:{s.api}"
        # 同一行同一 API 有多个调用点时按出现顺序加序号，保证 boundary_id 唯一。
        boundary_id = f"{base}#{seen[key]}" if counts[key] > 1 else base
        for fc in FAULTS:
            ok = fc in APPLICABLE[s.kind]
            rows.append({"flow": "", "boundary_id": boundary_id,
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
    if not enum:
        print("❌ 枚举结果为空：检查 SRC 路径与枚举规则", file=sys.stderr)
        return 2
    derived = {(r["boundary_id"], r["fault_class"]): r for r in enum}
    keys = [(r["boundary_id"], r["fault_class"]) for r in table]
    dupes = sorted(k for k, n in collections.Counter(keys).items() if n > 1)
    if dupes:
        print(f"❌ 冻结表有重复键：{len(dupes)} 条", file=sys.stderr)
        for k in dupes[:10]: print("  重:", k, file=sys.stderr)
        return 2
    table_map = dict(zip(keys, table))
    missing, extra = set(derived) - set(table_map), set(table_map) - set(derived)
    if missing or extra:
        print(f"❌ 冻结表与枚举不一致：缺 {len(missing)} 条 / 多 {len(extra)} 条", file=sys.stderr)
        for x in sorted(missing)[:10]: print("  缺:", x, file=sys.stderr)
        for x in sorted(extra)[:10]: print("  多:", x, file=sys.stderr)
        return 2
    bad = []
    for k, er in derived.items():
        tr = table_map[k]
        for field in ("applicability", "expected_keyword"):
            if tr[field] != er[field]:
                bad.append((k, field, er[field], tr[field]))
        if er["applicability"] == "n/a" and tr["reason"] != er["reason"]:
            bad.append((k, "reason", er["reason"], tr["reason"]))
    if bad:
        print(f"❌ 冻结表字段与枚举不一致：{len(bad)} 条", file=sys.stderr)
        for k, field, want, got in bad[:10]:
            print(f"  {k} {field}: 应为 {want!r} / 实际 {got!r}", file=sys.stderr)
        return 2
    empty = sorted({r["boundary_id"] for r in table if not r["flow"]})
    if empty:
        print(f"❌ 冻结表有 {len(empty)} 个边界的 flow 为空（表未完成）", file=sys.stderr)
        for b in empty[:10]: print("  空 flow:", b, file=sys.stderr)
        return 2
    print(f"✅ 边界表一致：枚举 {len(derived)} / 表 {len(table_map)} / 差集 0")
    return 0

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit", action="store_true")
    ap.add_argument("--write", type=pathlib.Path)
    ap.add_argument("--check", type=pathlib.Path)
    a = ap.parse_args(argv)
    if a.check: return check(a.check)
    rows = expand(iter_sites())
    if not rows:
        print("❌ 枚举结果为空：检查 SRC 路径与枚举规则", file=sys.stderr)
        return 2
    buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    if a.write: a.write.write_text(buf.getvalue(), encoding="utf-8")
    else: print(buf.getvalue(), end="")
    return 0

if __name__ == "__main__":
    sys.exit(main())
