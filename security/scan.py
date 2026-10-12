#!/usr/bin/env python3
"""扫描本仓库，找出「形状像本机真标识」的字符串。

设计见 内部设计记录。
三条硬约束：
  1. 纯标准库，且兼容 Python 3.9（钩子可能落到 /usr/bin/python3 = 3.9.6）
  2. **默认绝不打印命中值本身**——打印了，它自己就成了出口（spec §4.4）
  3. fail-closed：任何内部错误都非 0 退出，且与「发现泄漏」用不同退出码（spec §4.5）

**类型注解齐全**（docs/CONTRIBUTING.md §6），但注解**一律不得在运行时求值**：本文件要能在
3.9.6 上 import（上面第 1 条），而 `X | None` 这类写法在 3.9 的求值位置会抛 `TypeError`。
`from __future__ import annotations`（3.7+、纯标准库）把注解推迟成字符串，正好满足两边。
"""
from __future__ import annotations

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
#
# 路径形（spec §4.2 订正，2026-10-12）取的是**第一段**、不是整条路径——值稳定，指纹才稳。
# 两段结构性排除：`you`（仓库钦定的占位符，CONTRIBUTING §4；`daf9067` 实测有 28 处合法
# 命中——数字带树，别写成「今天」）与
# `Shared`（macOS 系统目录，不是用户名）。排除只认**整段相等**：`(?![A-Za-z0-9_-])`
# 那层管「后面不再接成分字符」，所以 `yours` / `you2` 照抓。
# **那两层 lookahead 是一对，别简化成一层**：末位不能是点之后（见下），末尾那个点已
# 不属于成分——`you` / `Shared` 后接句末点时，成分就是它们本身（占位符 + 标点），必须
# 排除；可**只留一层**就会把「`you` + 点 + 更多成分字符」也放过，而那是**另一个用户名**
# （成分形如 `you.x`，末位 `x`，合法）。第二层 `(?!\.[A-Za-z0-9._-])` 正是「点后面还有
# 成分字符 ⇒ 那不是句末标点」这半边；去掉它等于在 `you.*` / `Shared.*` 整个命名空间上失明。
# 字符集**刻意是 ASCII**：放宽成「非斜杠任意字符」会让文档描述脱敏规则用的记号
# `/Users/<用户名>` 自匹配，而它没有登记这条退路（只能去改写历史记录）。
# **不做大小写折叠**：折叠会拿 `/users/<id>` 这类 URL 片段换假阳性，同样没有退路。
# 末位**不能是点**（所以是 `*` + 一个不含点的收尾字符，而不是 `+`）：`/Users/<用户名>.`
# 这种句末标点若被吃进值，同一个用户名就会产出两个指纹（带点 / 不带点），与「命中的是
# 第一段」冲突。**不是「至少两个字符」**——1 个字符的用户名仍要命中。
PATTERNS = (
    ("wxid", re.compile(r"wxid_[A-Za-z0-9_-]{4,}")),
    ("chatroom", re.compile(r"[0-9]{6,}@chatroom")),
    ("phone", re.compile(r"1[3-9][0-9]{9}")),
    ("path", re.compile(
        r"/Users/(?!(?:you|Shared)(?![A-Za-z0-9_-])(?!\.[A-Za-z0-9._-]))"
        r"[A-Za-z0-9._-]*[A-Za-z0-9_-]")),
)

SHAPE_LABEL = {"wxid": "wxid 形", "chatroom": "chatroom 形", "phone": "手机号形",
               "path": "本机路径形"}

# CONTRIBUTING §4 明文豁免的两个**功能性**本机配置：它们写的就是运行这台机器所需的
# 真实路径，且 JSON/TOML 不展开 `~`。豁免**按 (文件, 形状)**、不按文件——其余三种形状
# 在这两个文件里照扫（别让它们成为整份盲区）。也**不能**改用 allowlist.txt 登记：那等于
# 把真实用户名写进一个会被提交的文件，正是这条红线要拦的事。
PATH_EXEMPT_FILES = frozenset({".mcp.json", ".codex/config.toml"})

# 生成的依赖锁：内容是第三方包元数据，没有一处是本项目的。实测 `uv.lock` 单独贡献
# 47 个手机号形假阳性（52 个手机号值里的 47 个）。跳过它不是网开一面——那里面
# 本来就不可能有本项目流出的标识。
SKIP_NAMES = frozenset({"uv.lock"})

FIXTURE_KEYWORDS = ("test", "fake", "dummy", "sample", "example", "placeholder",
                    "abc", "123")

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_DIFF_HEADER = re.compile(r"^diff --git ")
# 二进制文件在 diff 里**没有 `---`/`+++` 头、也没有 hunk**（实测 git 2.55：新文件只给
# `Binary files /dev/null and b/<路径> differ`），所以它既进不了新增行、也不会被
# 那条「路径解析不出来」的存在性检查碰到——是一条**静默**路径。git 的这句是**人类消息**
# （非结构化 token），理论上随 locale 变；本机 git 无 gettext 目录，实测 zh_CN 下仍是英文。
_BINARY = re.compile(r"^Binary files .* and (.*) differ$")


class ScannerError(Exception):
    """扫描器自身出错——与「发现泄漏」是两回事（退出码 2 对 1）。"""


def fingerprint(value: str) -> str:
    """值的稳定不透明标识。用它代替值本身出现在输出里（spec §4.4）。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def hits_in_text(text: str) -> list[tuple[int, int, str, str]]:
    """→ [(start, end, shape, value)]，按出现位置排序。"""
    found = []
    for shape, pattern in PATTERNS:
        for m in pattern.finditer(text):
            found.append((m.start(), m.end(), shape, m.group(0)))
    found.sort()
    return found


def path_exempt(rel: str, shape: str) -> bool:
    """这一处 (文件, 形状) 是否豁免。**两个调用点都必须过它**（`scan_repo` / `--staged`）。

    2026-09-29 的 Critical 1 就是跳过规则只落在一处：`--all` 说干净、`--staged` 说
    47 个未登记。这条判据的测试也按那个教训写——两处任一漏掉都会红。
    """
    return shape == "path" and rel in PATH_EXEMPT_FILES


def allowlistable(shape: str) -> bool:
    """这个形状的值能不能进豁免清单。只有路径形不能——清单的规矩是「只许放假值」，
    而路径形的值只能是真路径；登记它等于把真实用户名写进一个会被提交的文件。

    **这一条判据要卡住两侧**（只卡一侧等于没卡）：`allow_value` 卡「写」（前门），
    `unregistered_hits` 与 `verify_allowlist` 卡「读」（后门）。手抄一行进清单时，
    前门根本不在路上——后门那两侧才是「永不登记」这句话真正成立的地方。
    """
    return shape != "path"


def unregistered_hits(hits: list[tuple[str, int, str, str]],
                      allow: set[str]) -> list[tuple[str, int, str, str]]:
    """会被拦下的命中。路径形**不看清单**（见 allowlistable）——于是手抄进去的条目
    既不生效、又会被 --verify-allowlist 当死条目揪出来。

    **两个 `_run` 分支共用它**（`--staged`/`--text` 与默认）：规则只写一处，就不会再出现
    「默认视图看清单、`--staged` 不看」那种两条路各说各话——2026-09-29 的 Critical 1
    （跳过规则只落在 `scan_repo()` 一处）正是这个形状。
    """
    return [h for h in hits if not allowlistable(h[2]) or h[3] not in allow]


def _git(*args: str) -> str:
    exe = shutil.which("git")
    if exe is None:
        raise ScannerError("PATH 上找不到 git；本扫描器需要 git 来枚举文件与读取 diff")
    r = subprocess.run([exe] + list(args), cwd=str(REPO_ROOT),
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise ScannerError("git %s 退出码 %d：%s"
                           % (args[0], r.returncode, (r.stderr or "").strip()[:200]))
    return r.stdout


def _tracked_files() -> list[str]:
    """tracked 文件——正是「可能被提交」的那一面，.gitignore 天然生效。"""
    return [p for p in _git("ls-files", "-z").split("\0") if p]


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None          # 二进制 / 不可读：跳过，不算错误


def scan_repo() -> list[tuple[str, int, str, str]]:
    """→ [(relpath, lineno, shape, value)]。

    **只跳过 `security/allowlist.txt` 与 `SKIP_NAMES` 里的生成文件**：前者是
    「已批准的占位符」的注册表，扫它会让
    §4.3 的不变式 1（清单项必须在仓库里出现）**永远为真**——那是假护栏。

    另有一条**按 (文件, 形状)** 的豁免（`path_exempt`）：CONTRIBUTING §4 那两个
    功能性本机配置的路径形不报——它们的真实路径是运行这台机器所需，且没有
    「登记成假值」这条路（登记等于把真实用户名写进一个会被提交的文件）。
    注意它**不是**整份跳过：其余三种形状在那两个文件里照扫。

    **不跳过 scan.py 自身**：实测它不自匹配（`wxid_[A-Za-z0-9_-]{4,}` 这串字面量里
    `[` 不在字符类内，匹配不上；跑出来 0 命中）。多一条跳过规则等于白开一个洞——
    真标识粘进这个文件就没人管了。
    """
    skip = {ALLOWLIST_PATH.resolve()}
    out = []
    for rel in _tracked_files():
        if Path(rel).name in SKIP_NAMES:
            continue
        path = REPO_ROOT / rel
        if path.resolve() in skip:
            continue
        text = _read(path)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for _, _, shape, value in hits_in_text(line):
                if path_exempt(rel, shape):
                    continue
                out.append((rel, lineno, shape, value))
    return out


def collect_values() -> dict[str, dict]:
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


def load_allowlist(path: str | Path | None = None) -> set[str]:
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


def allow_value(key: str) -> str:
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
        # **不回显 key**：这是唯一可能拿到任意用户串的入口。有人把**值本身**粘进来时
        # （而不是指纹），回显就等于扫描器亲手把值打印到了 stdout/stderr（spec §4.4）。
        raise ScannerError("这个编号不在命中表里——编号是值的 sha256 前 8 位小写十六进制"
                           "（先跑 --values 看有哪些），**不要把值本身粘进来**")
    entry = table[key]
    if not allowlistable(entry["shape"]):
        # 前门这一处必须拦：登记一个真路径**不会**被 §4.3 的不变式 1 抓到——那个值仍在
        # 原文里命中，不算死条目。于是「钩子拦下 → 顺手 --allow」会把真路径合法化进一个
        # 会被提交的文件，正是这条红线要拦的事。
        # **但它只是前门**：手抄那一侧由 `unregistered_hits` 与 `verify_allowlist` 关
        # （见 allowlistable）——只关前门时，手抄一行照样豁免，而文档会宣称「永不登记」。
        raise ScannerError(
            "本机路径形不能登记进豁免清单——登记它等于把真实用户名写进一个会被提交的"
            "文件，且 §4.3 的不变式 1 查不出来。请改写那处文字：占位符一律用 "
            "/Users/you；CONTRIBUTING §4 的那两个功能性配置文件由扫描器按 (文件, 形状)"
            "豁免，不需要登记")
    line = "%s  %s · %d 字符 · --allow %s 登记" % (
        entry["value"], entry["shape"], len(entry["value"]), key)
    with ALLOWLIST_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return line


def verify_allowlist() -> list[str]:
    """spec §4.3 不变式 1 → 死条目的指纹列表。

    判据是 scan_repo() 的产物；它**刻意跳过 allowlist.txt 自身**，否则清单里每一项
    都会「出现在仓库里」，这条不变式就成了永真断言。

    **清单文件不存在是错误（退出 2），不是「干净」**：`load_allowlist` 对缺失文件返回
    空集，那是「全部按未登记处理」的正确方向；但拿它来**校验**就变成了「一个条目都没
    校验过」却报 verified——删掉 / 改名清单会让这条不变式静默失效。
    """
    if not ALLOWLIST_PATH.exists():
        raise ScannerError("豁免清单 %s 不存在——「没有清单」不是「清单干净」"
                           % ALLOWLIST_PATH)
    # **只收可登记的形状**：路径形的值不算「在仓库里命中过」。否则手抄一行真路径进清单
    # 会被算成有效条目（不是死条目），而它对路径形本来就不生效（见 allowlistable）——
    # 于是 `--allow` 拒绝写、手抄又不报死条目，「永不登记」就只剩一句宣示。
    seen = {value for _, _, shape, value in scan_repo() if allowlistable(shape)}
    return sorted(fingerprint(v) for v in load_allowlist() - seen)


def _b_side_path(token: str) -> str | None:
    """二进制行里的 `b/<路径>` → `<路径>`；删除那一侧是 `/dev/null` → None。"""
    token = token.strip()
    if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
        token = token[1:-1]          # 含空格 / 非 ASCII 时 git 会加引号（不展开转义）
    if token == "/dev/null":
        return None
    return token[2:] if token.startswith("b/") else token


def staged_added_lines(diff_text: str | None = None) -> list[tuple[str, int, str]]:
    """→ [(relpath, lineno, text)]，**只含新增行**（spec §4.4：不必为文件旧账负责）。

    **`+++ ` 只在「本文件的第一个 hunk 之前」才算文件头。**

    订正（2026-09-29，Task 5 审阅）：原先只判「上一行是 `--- `」，**不够**——hunk 体内
    也允许出现恰好长得像头的内容行：旧侧内容 `-- x` 渲染成 `--- x`，新侧内容
    `++ /dev/null` 渲染成 `+++ /dev/null`，那一对会被误认成「文件被删」，于是**该文件
    后面所有行被静默跳过**而进程照样退 0。**这是 fail-open，且与本节「不静默跳过」的
    承诺直接矛盾。** 审阅者实测仓库今天没有这种相邻对（扫了 129 个文件、0 处命中），
    所以是**潜伏**而非现患——但一个「只在没有伪装输入时才对」的解析器不算 fail-closed。

    纯新增安全，但**理由不是「不匹配头」**（复审订正）：内容 `-- x` 渲染成 `+-- x`，
    以 `+` 开头、确实不匹配头；内容 `++ y` 渲染成 `+++ y`，它**确实匹配**
    `startswith("+++ ")`——只是那时已经在 hunk 体**之内**，`seen_hunk` 会把它挡在
    文件头分支外。**是新判据救了它，不是文本模式。**

    改判据：`--- `/`+++ ` 只可能出现在**文件头块**里，而文件头块一定在**第一个 `@@` 之前**。
    所以用 `seen_hunk`（每个 `diff --git` 重置），而不是「上一行长什么样」。

    **`SKIP_NAMES` 在这里也生效**（2026-09-29 终审 Critical 1）：跳过规则原先只在
    `scan_repo()` 里，于是 `--staged` 会拿 `uv.lock` 里 47 个 URL 十六进制摘要中的
    手机号形假阳性**拦下一次提交**，而文档给的解法**根本解不开**——`--allow` 走
    `collect_values()` → `scan_repo()`，那个文件在那儿被跳过，指纹进不了表，
    `--allow` 只会退出 2；手抄又会让 `--verify-allowlist` 永远红。工具自相矛盾：
    `--all` 说干净、`--staged` 说 47 个未登记。放在这里，所有调用方一并继承。

    **二进制文件会点名警告**（终审 Important 2）：git 对二进制不给 `---`/`+++`、也不给
    hunk，只给一行 `Binary files … differ`，于是它既没有可扫的行、也无从触发上面那条
    存在性检查——**不警告就是静默退 0**，与「装了但从没拦过」不可区分。只警告，
    **不改退出码**：二进制文件不构成泄漏证据。
    """
    diff = _git("diff", "--cached", "--unified=0", "--no-color") if diff_text is None else diff_text
    out = []
    path = None
    lineno = 0
    seen_hunk = False
    for line in diff.splitlines():
        if _DIFF_HEADER.match(line):
            path = None
            seen_hunk = False
            continue
        if not seen_hunk and line.startswith("+++ "):
            target = line[4:].strip()
            path = None if target == "/dev/null" else target[2:]   # 去掉 "b/"
            if path is not None:
                if Path(path).name in SKIP_NAMES:
                    path = None       # 与 scan_repo() 同一条规则，别让两条路各说各话
                elif not (REPO_ROOT / path).exists():
                    raise ScannerError("diff 里的路径解析不出来：%r" % path)
            continue
        if line.startswith("@@"):
            m = _HUNK.match(line)
            if m is None:
                raise ScannerError("看不懂的 hunk 头：%r" % line)
            seen_hunk = True
            lineno = int(m.group(1))
            continue
        m = _BINARY.match(line)
        if m is not None:
            bpath = _b_side_path(m.group(1))
            if bpath is not None:
                print("scan: ⚠️ %s 是二进制文件——它**一行都没被检查**"
                      "（形状扫描只读得懂 UTF-8 文本），退出码不因它改变" % bpath,
                      file=sys.stderr)
            continue
        if path is None:
            continue
        if line.startswith("+"):
            out.append((path, lineno, line[1:]))
            lineno += 1
        elif line.startswith(" "):
            lineno += 1
    return out


def text_hits(path: str | Path) -> list[tuple[str, int, str, str]]:
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


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="扫描形状像真机标识的字符串（绝不打印值）")
    ap.add_argument("--all", action="store_true",
                    help="扫全部 tracked 文件（逐处）——这是**默认行为**，本开关保留为显式别名")
    ap.add_argument("--values", action="store_true", help="逐值汇总（审查用，spec §4.6）")
    ap.add_argument("--show-values", action="store_true",
                    help="⚠️ 打印命中值本身——它会被写进你的会话记录，等于又开一个出口")
    ap.add_argument("--allow", metavar="指纹", help="按指纹把该值写进豁免清单（不打印值）")
    ap.add_argument("--verify-allowlist", action="store_true",
                    help="检查豁免清单里的死条目（spec §4.3 不变式 1）")
    ap.add_argument("--staged", action="store_true", help="只扫暂存区的新增行（pre-commit 用）")
    ap.add_argument("--text", metavar="文件", help="扫一个给定文件（commit-msg 用）")
    return ap


def _run(args: argparse.Namespace) -> int:
    """各模式的分派。**整个函数体就是那条 fail-closed 边界**（`main` 那层只作兜底）。

    边界只此一处，所以新加分支**不可能**漏掉它——不必再靠「每次记得抄三行 except」。
    """
    try:
        if args.verify_allowlist:
            dead = verify_allowlist()
            if dead:
                for key in dead:
                    print("死条目 #%s——它在仓库里一次都没命中，请从豁免清单删掉" % key)
                return EXIT_HITS
            return EXIT_CLEAN

        if args.allow:
            allow_value(args.allow)
            print("已登记 #%s 到 security/allowlist.txt" % args.allow)
            return EXIT_CLEAN

        if args.values:
            table = collect_values()
            allow = load_allowlist()      # 读文件，在边界之内（本函数整个函数体就是边界）
            for key in sorted(table, key=lambda k: (table[k]["shape"], k)):
                e = table[key]
                low = e["value"].lower()
                fixture = "是" if any(w in low for w in FIXTURE_KEYWORDS) else "否"
                # 不可登记的形状**不看清单**（见 allowlistable）：显示「已登记」而命中
                # 照样被拦是假话，显示「未登记」则在暗示「登记一下就好了」——同样做不到。
                if allowlistable(e["shape"]):
                    state = "已登记" if e["value"] in allow else "未登记"
                else:
                    state = "不可登记"
                print("#%s  %s · %d 字符 · 出现于 %s · %d 次 · 夹具关键词：%s · %s"
                      % (key, SHAPE_LABEL[e["shape"]], len(e["value"]),
                         "、".join(sorted(e["dirs"])), e["count"], fixture, state))
            return EXIT_HITS if table else EXIT_CLEAN

        if args.staged or args.text:
            if args.text:
                hits = text_hits(args.text)
            else:
                added = staged_added_lines()          # 只取一次：两次调用可能看到不同状态
                if any(rel == "security/allowlist.txt" for rel, _n, _t in added):
                    print("scan: ⚠️ 你在修改豁免清单——请确认新增的是**占位符**而不是真值",
                          file=sys.stderr)
                hits = [(rel, n, shape, value)
                        for rel, n, text in added
                        for _, _, shape, value in hits_in_text(text)
                        if not path_exempt(rel, shape)]
            allow = load_allowlist()      # 读文件，在边界之内（同上）
            unregistered = unregistered_hits(hits, allow)
            for rel, lineno, shape, value in unregistered:
                shown = value if args.show_values else "%d 字符" % len(value)
                print("%s:%d  %s · %s · 未登记" % (rel, lineno, SHAPE_LABEL[shape], shown))
            if args.show_values and unregistered:
                print("⚠️ 上面打印了命中值本身——它已进入你的会话记录。", file=sys.stderr)
            return EXIT_HITS if unregistered else EXIT_CLEAN

        hits = scan_repo()
        allow = load_allowlist()          # 读文件，在边界之内
        unregistered = unregistered_hits(hits, allow)
        for rel, lineno, shape, value in unregistered:
            shown = value if args.show_values else "%d 字符" % len(value)
            print("%s:%d  %s · %s · 未登记" % (rel, lineno, SHAPE_LABEL[shape], shown))
        if args.show_values and unregistered:
            print("⚠️ 上面打印了命中值本身——它已进入你的会话记录。", file=sys.stderr)
        return EXIT_HITS if unregistered else EXIT_CLEAN
    except Exception as exc:      # 唯一的函数级边界：任何分支的异常都从这里退 2
        print("scan: 扫描器出错：%s" % exc, file=sys.stderr)
        return EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    """薄壳：**任何**逃出 `_run` 的异常都以 `EXIT_ERROR` 退出。

    **边界分两层**（订正，2026-10-12，终局评审）：`_run` 那层包住它**整个函数体**，
    是主边界——新加分支**不可能忘**带边界，这比「每个分支各写一个 `except`」更强；
    这里只作兜底。此前这层的 docstring 说各分支保留自己的 `except` 是因为「消息更
    具体」，实测那六处消息**逐字相同**（现只剩 `_run` 与 `main` 各一处：**并成两处，
    六处里没有一句是不同的**），那条理由在代码里不成立。

    兜底防的仍是「新加一个分支时忘了把读文件/调外部命令的那步放进边界」——
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
