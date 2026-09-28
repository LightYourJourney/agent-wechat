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


def main(argv=None):
    ap = argparse.ArgumentParser(description="扫描形状像真机标识的字符串（绝不打印值）")
    ap.add_argument("--all", action="store_true", help="扫全部 tracked 文件（逐处）")
    ap.add_argument("--show-values", action="store_true",
                    help="⚠️ 打印命中值本身——它会被写进你的会话记录，等于又开一个出口")
    args = ap.parse_args(argv)

    try:
        hits = scan_repo()
    except Exception as exc:
        print("scan: 扫描器出错：%s" % exc, file=sys.stderr)
        return EXIT_ERROR

    for rel, lineno, shape, value in hits:
        shown = value if args.show_values else "%d 字符" % len(value)
        print("%s:%d  %s · %s · 未登记" % (rel, lineno, SHAPE_LABEL[shape], shown))
    if args.show_values and hits:
        print("⚠️ 上面打印了命中值本身——它已进入你的会话记录。", file=sys.stderr)
    return EXIT_HITS if hits else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
