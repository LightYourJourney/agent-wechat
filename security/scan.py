#!/usr/bin/env python3
"""扫描本仓库，找出「形状像本机真标识」的字符串。

设计见 内部设计记录。
三条硬约束：
  1. 纯标准库，且兼容 Python 3.9（钩子可能落到 /usr/bin/python3 = 3.9.6）
  2. **默认绝不打印命中值本身**——打印了，它自己就成了出口（spec §4.4）
  3. fail-closed：任何内部错误都非 0 退出，且与「发现泄漏」用不同退出码（spec §4.5）
"""
import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

EXIT_CLEAN = 0
EXIT_HITS = 1
EXIT_ERROR = 2

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = REPO_ROOT / "security" / "allowlist.txt"

# [0-9a-f]{32} 刻意不在列：任何 md5 都长这样，仓库里有 2786 个 distinct（spec §4.2）。
PATTERNS = (
    ("wxid", re.compile(r"wxid_[A-Za-z0-9_-]{4,}")),
    ("chatroom", re.compile(r"[0-9]{6,}@chatroom")),
    ("phone", re.compile(r"1[3-9][0-9]{9}")),
)

SHAPE_LABEL = {"wxid": "wxid 形", "chatroom": "chatroom 形", "phone": "手机号形"}

FIXTURE_KEYWORDS = ("test", "fake", "dummy", "sample", "example", "placeholder",
                    "abc", "123")

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class ScannerError(Exception):
    """扫描器自身出错——与「发现泄漏」是两回事（退出码 2 对 1）。"""


def fingerprint(value):
    """值的稳定不透明标识。用它代替值本身出现在输出里（spec §4.4）。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def hits_in_text(text):
    """→ [(start, end, shape, value)]，按出现位置排序。"""
    found = []
    for shape, pattern in PATTERNS:
        for m in pattern.finditer(text):
            found.append((m.start(), m.end(), shape, m.group(0)))
    found.sort()
    return found


def _git(*args):
    exe = shutil.which("git")
    if exe is None:
        raise ScannerError("PATH 上找不到 git；本扫描器需要 git 来枚举文件与读取 diff")
    r = subprocess.run([exe] + list(args), cwd=str(REPO_ROOT),
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise ScannerError("git %s 退出码 %d：%s"
                           % (args[0], r.returncode, (r.stderr or "").strip()[:200]))
    return r.stdout


def _tracked_files():
    """tracked 文件——正是「可能被提交」的那一面，.gitignore 天然生效。"""
    return [p for p in _git("ls-files", "-z").split("\0") if p]


def _read(path):
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None          # 二进制 / 不可读：跳过，不算错误


def scan_repo():
    """→ [(relpath, lineno, shape, value)]。

    **只跳过 security/allowlist.txt**：它是「已批准的占位符」的注册表，扫它会让
    §4.3 的不变式 1（清单项必须在仓库里出现）**永远为真**——那是假护栏。

    **不跳过 scan.py 自身**：实测它不自匹配（`wxid_[A-Za-z0-9_-]{4,}` 这串字面量里
    `[` 不在字符类内，匹配不上；跑出来 0 命中）。多一条跳过规则等于白开一个洞——
    真标识粘进这个文件就没人管了。
    """
    skip = {ALLOWLIST_PATH.resolve()}
    out = []
    for rel in _tracked_files():
        path = REPO_ROOT / rel
        if path.resolve() in skip:
            continue
        text = _read(path)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for _, _, shape, value in hits_in_text(line):
                out.append((rel, lineno, shape, value))
    return out


def collect_values():
    """逐值汇总（spec §4.4 的 --values 视图）。

    返回的 value 只在内存里流转——任何出口都必须先过 fingerprint()。
    """
    agg = {}
    for rel, _lineno, shape, value in scan_repo():
        key = fingerprint(value)
        entry = agg.get(key)
        if entry is None:
            entry = {"value": value, "shape": shape, "dirs": set(), "count": 0}
            agg[key] = entry
        entry["dirs"].add(rel.split("/", 1)[0] if "/" in rel else ".")
        entry["count"] += 1
    return agg


def load_allowlist(path=None):
    """→ set(值)。`#` 起注释；第一个空白之前的 token 是值，其后是说明。

    文件不存在时返回空集：效果是「全部按未登记处理」，方向是 fail-closed（spec §4.3）。
    """
    path = Path(path) if path is not None else ALLOWLIST_PATH
    if not path.exists():
        return set()
    values = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            values.add(line.split()[0])
    return values


def allow_value(key):
    """把 fingerprint == key 的值写进豁免清单。返回写入的行（**调用方不得打印它**）。

    **为什么编号是 sha256[:8] 指纹，而不是递增序号**：序号会**随命中集合漂移**——
    你看了 `#7`，中间任何人改了任何文件（或者你刚 `--allow` 掉一条），转头
    `--allow 7` 指的可能已经是**另一个值**。指纹是值的函数、与集合无关，所以稳定。
    这正是 Review Focus 第 1 条要防的误用。（spec §4.4 里的 `--allow 7` 只是**示意**；
    它要求的是「不透明编号」，指纹满足。指纹本身不必抗暴力——豁免清单里本来就
    躺着值，它只是个界面把手。）
    """
    table = collect_values()
    if key not in table:
        raise ScannerError("没有编号 %s 的命中——先跑 --values 看有哪些" % key)
    entry = table[key]
    line = "%s  %s · %d 字符 · --allow %s 登记" % (
        entry["value"], entry["shape"], len(entry["value"]), key)
    with ALLOWLIST_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return line


def verify_allowlist():
    """spec §4.3 不变式 1 → 死条目的指纹列表。

    判据是 scan_repo() 的产物；它**刻意跳过 allowlist.txt 自身**，否则清单里每一项
    都会「出现在仓库里」，这条不变式就成了永真断言。
    """
    seen = {value for _, _, _, value in scan_repo()}
    return sorted(fingerprint(v) for v in load_allowlist() - seen)


def staged_added_lines(diff_text=None):
    """→ [(relpath, lineno, text)]，**只含新增行**（spec §4.4：不必为文件旧账负责）。

    `+++ b/路径` 只有在**紧跟在 `--- ` 行之后**才算文件头——否则一个内容行
    `+++ b/x` 会被误认成路径。路径解析不出来时**抛 ScannerError**（fail-closed），
    不静默跳过：静默跳过会让「钩子装了但从没拦过」变得不可见。
    """
    diff = _git("diff", "--cached", "--unified=0", "--no-color") if diff_text is None else diff_text
    out = []
    path = None
    lineno = 0
    prev_was_minus = False
    for line in diff.splitlines():
        if line.startswith("--- "):
            prev_was_minus = True
            continue
        if line.startswith("+++ ") and prev_was_minus:
            prev_was_minus = False
            target = line[4:].strip()
            path = None if target == "/dev/null" else target[2:]   # 去掉 "b/"
            if path is not None and not (REPO_ROOT / path).exists():
                raise ScannerError("diff 里的路径解析不出来：%r" % path)
            continue
        prev_was_minus = False
        if line.startswith("@@"):
            m = _HUNK.match(line)
            if m is None:
                raise ScannerError("看不懂的 hunk 头：%r" % line)
            lineno = int(m.group(1))
            continue
        if path is None:
            continue
        if line.startswith("+"):
            out.append((path, lineno, line[1:]))
            lineno += 1
        elif line.startswith(" "):
            lineno += 1
    return out


def text_hits(path):
    """扫一个给定文件（用于 commit-msg 的提交信息）。→ [(relpath, lineno, shape, value)]"""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ScannerError("%s 不是合法 UTF-8——拒绝放行（fail-closed）" % p)
    except OSError as exc:
        raise ScannerError("读不了 %s：%s" % (p, exc))
    return [(str(p), n, shape, value)
            for n, line in enumerate(text.splitlines(), 1)
            for _, _, shape, value in hits_in_text(line)]


def _build_parser():
    ap = argparse.ArgumentParser(description="扫描形状像真机标识的字符串（绝不打印值）")
    ap.add_argument("--all", action="store_true", help="扫全部 tracked 文件（逐处）")
    ap.add_argument("--values", action="store_true", help="逐值汇总（审查用，spec §4.6）")
    ap.add_argument("--show-values", action="store_true",
                    help="⚠️ 打印命中值本身——它会被写进你的会话记录，等于又开一个出口")
    ap.add_argument("--allow", metavar="指纹", help="按指纹把该值写进豁免清单（不打印值）")
    ap.add_argument("--verify-allowlist", action="store_true",
                    help="检查豁免清单里的死条目（spec §4.3 不变式 1）")
    ap.add_argument("--staged", action="store_true", help="只扫暂存区的新增行（pre-commit 用）")
    ap.add_argument("--text", metavar="文件", help="扫一个给定文件（commit-msg 用）")
    return ap


def _run(args):
    if args.verify_allowlist:
        try:
            dead = verify_allowlist()
        except Exception as exc:
            print("scan: 扫描器出错：%s" % exc, file=sys.stderr)
            return EXIT_ERROR
        if dead:
            for key in dead:
                print("死条目 #%s——它在仓库里一次都没命中，请从豁免清单删掉" % key)
            return EXIT_HITS
        return EXIT_CLEAN

    if args.allow:
        try:
            allow_value(args.allow)
        except Exception as exc:
            print("scan: 扫描器出错：%s" % exc, file=sys.stderr)
            return EXIT_ERROR
        print("已登记 #%s 到 security/allowlist.txt" % args.allow)
        return EXIT_CLEAN

    if args.values:
        try:
            table = collect_values()
            allow = load_allowlist()      # 读文件，必须在边界之内（同上）
        except Exception as exc:
            print("scan: 扫描器出错：%s" % exc, file=sys.stderr)
            return EXIT_ERROR
        for key in sorted(table, key=lambda k: (table[k]["shape"], k)):
            e = table[key]
            low = e["value"].lower()
            fixture = "是" if any(w in low for w in FIXTURE_KEYWORDS) else "否"
            state = "已登记" if e["value"] in allow else "未登记"
            print("#%s  %s · %d 字符 · 出现于 %s · %d 次 · 夹具关键词：%s · %s"
                  % (key, SHAPE_LABEL[e["shape"]], len(e["value"]),
                     "、".join(sorted(e["dirs"])), e["count"], fixture, state))
        return EXIT_HITS if table else EXIT_CLEAN

    if args.staged or args.text:
        try:
            if args.text:
                hits = text_hits(args.text)
            else:
                added = staged_added_lines()          # 只取一次：两次调用可能看到不同状态
                if any(rel == "security/allowlist.txt" for rel, _n, _t in added):
                    print("scan: ⚠️ 你在修改豁免清单——请确认新增的是**占位符**而不是真值",
                          file=sys.stderr)
                hits = [(rel, n, shape, value)
                        for rel, n, text in added
                        for _, _, shape, value in hits_in_text(text)]
            allow = load_allowlist()  # 读文件，必须在边界之内（同前两处）
        except Exception as exc:
            print("scan: 扫描器出错：%s" % exc, file=sys.stderr)
            return EXIT_ERROR
        unregistered = [h for h in hits if h[3] not in allow]
        for rel, lineno, shape, value in unregistered:
            shown = value if args.show_values else "%d 字符" % len(value)
            print("%s:%d  %s · %s · 未登记" % (rel, lineno, SHAPE_LABEL[shape], shown))
        return EXIT_HITS if unregistered else EXIT_CLEAN

    try:
        hits = scan_repo()
        allow = load_allowlist()          # 读文件，必须在边界之内
    except Exception as exc:
        print("scan: 扫描器出错：%s" % exc, file=sys.stderr)
        return EXIT_ERROR

    unregistered = [h for h in hits if h[3] not in allow]
    for rel, lineno, shape, value in unregistered:
        shown = value if args.show_values else "%d 字符" % len(value)
        print("%s:%d  %s · %s · 未登记" % (rel, lineno, SHAPE_LABEL[shape], shown))
    if args.show_values and unregistered:
        print("⚠️ 上面打印了命中值本身——它已进入你的会话记录。", file=sys.stderr)
    return EXIT_HITS if unregistered else EXIT_CLEAN


def main(argv=None):
    """薄壳：**任何**逃出 `_run` 的异常都以 `EXIT_ERROR` 退出。

    各分支内部仍保留自己的 `except Exception`（消息更具体）；这一层只作兜底，
    防的是「新加一个分支时忘了把读文件/调外部命令的那步放进边界」——
    那个坑在本计划里已经出现过三次。

    `parse_args` 故意留在 `try` **之外**：参数错误应当照常以 argparse 自己的
    退出码结束，不该被误报成「扫描器出错」。（`SystemExit` 不是 `Exception`
    的子类，即使放进去也穿透，但放外面读起来更清楚。）
    """
    args = _build_parser().parse_args(argv)
    try:
        return _run(args)
    except Exception as exc:      # 顶层 fail-closed 边界正是宽泛捕获的位置
        print("scan: 扫描器出错：%s" % exc, file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
