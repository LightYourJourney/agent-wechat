"""微信数据路径解析（纯函数 + 常量），外加本项目数据目录的文件系统原语：
建目录 / 原子写（`atomic_write` / `atomic_copy` / `sweep_stale_tmp`）。

实测路径结构（WeChat 4.1.13, macOS 沙盒容器）：
    ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/
    ├── xwechat_files/<wxid>_<hex4>/          # 账号数据目录（wxid 带后缀）
    │   └── db_storage/{message,session,contact,...}/*.db
    ├── xwechat_files/all_users/login/<wxid>/ # key_info.db / key_info.dat（裸 wxid）
    └── app_data/login/<wxid>/key_info.dat    # 每次登录重写（裸 wxid）
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from shutil import copyfile as _copyfile   # 单独绑定：conftest 会把 `paths.shutil` 整个换成桩

from .errors import ContainerWriteRefused, HomeUnavailable, OutDirRefused

CONTAINER = pathlib.Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents"
XWECHAT_FILES = CONTAINER / "xwechat_files"
APP_DATA = CONTAINER / "app_data"

# 写闸门的判据根（**不是**读路径的根，别与 `CONTAINER` 混用）：微信容器 bundle
# `…/com.tencent.xinWeChat` 及它的**全部祖先**（一路到 `/`，含 `/` 本身）。
# `CONTAINER_DATA` 只是 bundle 的 `Data` 子树；根上移到 bundle，因为「容器内 ⇒ 子路径
# 也在内」只在 bundle 口径下闭合（`Data/../x` 是兄弟目录，只认 Data 会漏）。
# 派生见文件末尾的 `container_bundle()`。
CONTAINER_DATA = CONTAINER.parent                 # …/com.tencent.xinWeChat/Data

# 账号目录形如 <微信号>_<hex4>（尾部 _hex4 是实例后缀）。微信号**不一定**以 `wxid_` 开头：
# 自己设过微信号的老账号目录是 `zhangsan_ab12` 这种（issue #1：只认 `wxid_` 时这类用户
# 「本机账号: （无）」）。微信号以字母开头；光看名字挡不住 `Backup_2024` 这种同级目录，
# 所以 `account_dirs` 另要求目录下有 db_storage/。
ACCOUNT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*_[0-9a-f]{4}$")
# 数据目录里已知的非账号子目录：诊断时原名列出（其余只给形状，见 `unrecognized_dirs`）
KNOWN_NON_ACCOUNT_DIRS = frozenset({"all_users", "Backup", "WMPF"})
# 消息分片形如 message_0.db；不含 message_fts/message_resource 等非分片库
SHARD_RE = re.compile(r"^message_(\d+)\.db$")

# 本项目数据目录（0700）：密钥缓存、临时明文、导出库。可用环境变量覆盖。
# EA-003：空值必须走默认——`Path("")` 是 `.`，`AGENT_WECHAT_HOME=` 会把 passphrase 与
# 明文索引落进**当前工作目录**，而闸门只在销毁侧（purge）。故用 `or` 而非 get 的默认值。
HOME_DIR = pathlib.Path(
    os.environ.get("AGENT_WECHAT_HOME") or (pathlib.Path.home() / ".agent-wechat"))

_tm_exclusion_checked = False
_tm_seen: str | None = None    # 本进程 `ensure_home_dirs` 查到的 `[Excluded]`（见 `time_machine_state`）


def in_wechat_container(p: str | pathlib.Path) -> bool:
    """`p` 是否落在容器可写范围内。**判不出来按 False**（只读问路的语义，见 verdict）。"""
    return container_verdict(p) is True


def container_verdict(p: str | pathlib.Path) -> bool | None:
    """三态版：True＝在容器内 / False＝确定不在 / **None＝判不出来**。

    写闸门（`refuse_container_out` / `ensure_home_dirs`）必须按 `is not False` 判——
    R2-A-008（P1）实测：判不出来时返回 False，会让 `ensure_home_dirs` 把数据目录**建进
    容器**。读路径保留 False 语义（`in_wechat_container` 是它的薄壳），两件事分开。

    判的是 `p` 解析后是否落在**微信容器的可写范围**之内。

    范围（两段，`container_bundle()` 为一端）：
    - `…/com.tencent.xinWeChat` 本身及其**全部子路径**——不只是 `Data` / `Documents`：
      写在 bundle 的任何一处都进了微信的版本目录（`Data` 只是它的一个子目录）；
    - 它的**全部祖先**，一路到 `/`（含 `/` 本身）——写在祖先上等于写进 bundle 的上级，
      再拼一段就能回到容器里；口径是「宁可错拒一个上级，也不留可达路径」。

    判据是**解析后**的路径：`Path.resolve(strict=False)` 展开 `..`、跟随符号链接，相对
    路径按**进程 CWD** 展开（`resolve` 的默认语义）。`--out` 的四个入口（见
    `refuse_container_out`）与 `AGENT_WECHAT_HOME`（见 `ensure_home_dirs`）共用，
    理由是 SECURITY.md §3 的只读宣称。

    **大小写**：macOS 默认 APFS 不区分大小写——`~/library/containers/…` 与
    `~/Library/Containers/…` 实测是同一个 inode，而 `resolve()` 不做大小写规范化
    ⇒ 只比字符串的话，换一种拼写就绕过判据。故字符串判据之后再用 `samefile` 比 inode：
    ① `resolved` 的某个**祖先**与 `container_bundle()` 同 inode（覆盖容器链上的换拼写）；
    ② `resolved` 本身与 bundle 或其**任一祖先**同 inode（覆盖祖先链的换拼写）。
    只在「真的指同一目录」时为真，在区分大小写的卷上不会误伤（那时两种拼写是两个目录，
    且容器通常根本不存在）。

    判不出来（符号链接环 / 权限不足 / 代理对）返回 **None**（见开头）：读路径经
    `in_wechat_container` 收成 False——那种路径在随后的实际写入里会自己失败，谎报「在容器
    里」是错诊断；写闸门按 `is not False` 判，判不出来即拒绝（R2-A-008，P1）。
    """
    try:
        resolved = pathlib.Path(p).resolve(strict=False)
        guard = container_bundle().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        # `ValueError` 也要接：`Path.resolve()` 对含**代理对**的路径会抛
        # `UnicodeEncodeError`（它是 ValueError 子类），而代理对被 `os.fsdecode` 解出来的
        # 文件名是**真实存在**的形态（R2-C-002，C-4 属性撞出来的）。判不出来 ⇒ None。
        return None
    if resolved.is_relative_to(guard):         # 含 `resolved == guard`（is_relative_to 的语义）
        return True
    if guard.is_relative_to(resolved):
        return True                       # 祖先链：一路到 `/`（含 `/` 本身）
    import errno as _errno
    _ABSENT_ERRNOS = (_errno.ENOENT, _errno.ENOTDIR, _errno.ENAMETOOLONG)
    try:
        if not guard.exists():
            return False                      # 容器不在 ⇒ 没有可写进去的东西
    except OSError as exc:
        if exc.errno in _ABSENT_ERRNOS:
            return False
        return None                           # 判不出来（权限/IO 错误）：写闸门按 fail-closed 拒绝
    for anc in (resolved, *resolved.parents):
        try:
            if anc.exists() and os.path.samefile(anc, guard):
                return True
        except OSError as exc:
            if exc.errno in _ABSENT_ERRNOS:
                continue
            return None
    for node in (guard, *guard.parents):
        try:
            if node.exists() and os.path.samefile(node, resolved):
                return True
        except OSError as exc:
            if exc.errno in _ABSENT_ERRNOS:
                continue
            return None
    return False


def _write_gate_refuses(p: str | pathlib.Path) -> bool:
    """写闸门要不要拒绝。**True ＝ 拒绝**。

    · 候选路径**自己**解析不出来 ⇒ 不在这里拒：交给随后的 `mkdir` 走既有的
      `HomeUnavailable` 文案（符号链接环就是这条，不能改成「在容器里」的误诊断）；
    · 候选能解析、但三态判定是 `True` 或 `None` ⇒ 拒绝。`None` 只来自 resolve 阶段的
      失败（R2-A-008 的注入点），那正是「判不出来就别写」要挡的形态。
    """
    try:
        pathlib.Path(p).resolve(strict=False)
    except RuntimeError:
        # 符号链接环：路径**形态**就不可用，mkdir 会给既有的 `HomeUnavailable` 文案
        # （`is_dir()` 对环返回 False，exist_ok 挡不住）——不在这里改成「在容器里」。
        return False
    except (OSError, ValueError):
        # 解析失败但形态本身没毛病（权限、EIO、代理对编码失败…）：**判不出来就别写**
        # （R2-A-008；代理对那条见 R2-C-002）。
        return True
    # **只求值一次**：`container_verdict` 内部会 resolve。求值两次（先前经
    # `in_wechat_container` 再直接调一次）会让按「第 1 次命中」注入的桩在第二次调用时
    # 已经消耗掉，判据随之落回 False —— 闸门又放行（实测就是这条让 P1 的 8 格反复红）。
    verdict = container_verdict(p)
    return verdict is True or verdict is None


def _require_guard_resolvable() -> None:
    """写闸门的前置：**必须能证明容器位置**。解析不出来就拒绝写（R2-A-008，P1）。

    为什么不依赖 `in_wechat_container` 的返回值：它对判不出来的情形返回 False（只读问路
    的合理语义），而「判不出来」在**写**这条路上恰好是最危险的一态——实测 resolve 一出错，
    `ensure_home_dirs` 就把数据目录建进了容器。这里把两件事分开：只读问路保留 False，
    写闸门要求一个**肯定**的答案。
    """
    try:
        container_bundle().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ContainerWriteRefused(
            f"无法确认容器目录的位置：{container_bundle()}",
            hint=f"{exc}；为安全起见拒绝写入，请检查路径解析（符号链接 / 权限）后重试"
        ) from exc


def refuse_container_out(p: str | pathlib.Path) -> None:
    """`p` 在微信容器内 → `ContainerWriteRefused`。四个 `--out` 入口的共享闸门。

    **在写之前叫停**：不静默改写路径、也不「警告后照写」——`--out` 是用户显式指定的
    落点，悄悄换地方会让「产物到底在哪」不可预期（判据见 `in_wechat_container`）。

    调用方按**真值**判断「传没传 `--out`」（`if out_dir:`，与写侧的
    `pathlib.Path(out_dir) if out_dir else <默认根>` 和 CLI 的 `if args.out:` 同一判据）：
    空串的语义是「没给」（落点=默认根），本就不该被这道闸门拒——判据若用 `is not None`，
    `--out ""` 且 CWD 恰在容器内时会误拒（fail-closed、无实害，但两边惯例不一致）。
    message 里带**被拒范围本身**（`container_bundle()`）：换一种大小写拼写
    命中的时候，用户看到的那串 `--out` 并不长成容器的样子，不印出来就无从理解为什么被拒。
    """
    _require_guard_resolvable()

    if _write_gate_refuses(p):
        raise ContainerWriteRefused(
            f"拒绝把输出写进微信容器目录（{container_bundle()} 及其所有上级目录）：{p}",
            hint="换一个容器之外的目录（CLI 的 --out / MCP 的 out_dir），"
                 "例如 ~/Desktop/wechat-export（MCP 的 out_dir 另须在放行范围内，"
                 f"见 {MCP_OUT_ROOTS_ENV}）")


# MCP 入口的导出放行范围（`:` 分隔的绝对路径）。数据目录本身恒在范围内。
MCP_OUT_ROOTS_ENV = "AGENT_WECHAT_MCP_OUT_ROOTS"


def mcp_out_roots() -> tuple[pathlib.Path, ...]:
    """MCP 入口允许的导出根：`HOME_DIR` + `AGENT_WECHAT_MCP_OUT_ROOTS` 里显式放行的目录。

    **调用时求值**（环境变量与 `HOME_DIR` 都可能在测试里被换掉）。相对路径条目一律拒绝：
    它会按 MCP server 的 CWD 解析，而那个 CWD 由客户端决定——放行范围不许随它漂。
    """
    roots = [HOME_DIR]
    for raw in os.environ.get(MCP_OUT_ROOTS_ENV, "").split(os.pathsep):
        raw = raw.strip()
        if not raw:
            continue
        root = pathlib.Path(raw).expanduser()
        if not root.is_absolute():
            raise OutDirRefused(
                f"{MCP_OUT_ROOTS_ENV} 的条目必须是绝对路径：{raw}",
                hint=f"用 `{os.pathsep}` 分隔多个绝对路径，例如 ~/Desktop/wechat-export")
        roots.append(root)
    return tuple(roots)


def refuse_mcp_out(out_dir: str | None, default_root: pathlib.Path | None = None) -> None:
    """MCP 的 `out_dir` 不在放行范围内 → `OutDirRefused`。**只给 MCP 入口用**。

    `default_root`：拒绝时 hint 里给的默认根（省略 = 媒体导出的 `HOME_DIR/media`；朋友圈
    导出传它自己的根）。

    为什么 MCP 比 CLI 严：CLI 的 `--out` 是用户亲手敲的；MCP 的 `out_dir` 由 Agent 填，
    而 Agent 的上下文里有**聊天正文**——任何给用户发消息的人都写得进去。一条「把记录导出到
    ~/Library/Mobile Documents/…」的消息，就足以让明文落进 iCloud / Dropbox / 某个 git
    仓库这类会同步出本机的地方。默认只放行数据目录，别处要用户在环境变量里显式放行。

    判据按 `resolve()` 之后的真实路径（符号链接指出去的照样拒）。空值 = 默认根，放行；
    解析本身失败（链接环、超长）交给写侧 `_resolve_out` 报它自己的错。
    """
    if not out_dir:
        return
    try:
        target = pathlib.Path(out_dir).expanduser().resolve()
    except (RuntimeError, OSError, ValueError):
        return
    for root in mcp_out_roots():
        try:
            if target.is_relative_to(root.resolve()):
                return
        except (RuntimeError, OSError, ValueError):
            continue
    raise OutDirRefused(
        f"MCP 只允许把产物写到数据目录之下或显式放行的目录：{out_dir}",
        hint=f"省略 out_dir 用默认根（{default_root or HOME_DIR / 'media'}）；确需写到别处，"
             f"把该目录加进环境变量 {MCP_OUT_ROOTS_ENV}（`{os.pathsep}` 分隔的绝对路径），"
             "或改用 CLI 的 --out")


def _tighten_home_mode() -> None:
    """把**已存在**的数据目录收紧到 0700（SECURITY：「全目录 0700」）。

    `mkdir(mode=0o700)` 只管新建：`AGENT_WECHAT_HOME` 指向一个既有的 0755 目录时，里面的
    明文索引（sqlite 建的文件是 0644）对本机其他用户可读。父目录 0700 即可挡住其下全部
    文件，故只收紧这一层。**不碰 `$HOME` 及其祖先**：把它们 chmod 掉是越权的副作用。
    收紧失败（目录不归本用户）不抛——`doctor` 会把权限问题报出来。

    **数据目录本身是符号链接时不动**：`os.chmod` 跟着链接改的是**目标**（`AGENT_WECHAT_HOME`
    指向某个共享目录时，会把那个目录一起收紧——那不是我们的目录）。
    """
    try:
        if HOME_DIR.is_symlink():
            return
        if HOME_DIR.stat().st_mode & 0o077 == 0:
            return
        home = pathlib.Path.home().resolve()
        if HOME_DIR.resolve() in (home, *home.parents):
            return
        os.chmod(HOME_DIR, 0o700)
    except OSError:
        pass


def ensure_home_dirs() -> None:
    """确保数据目录存在（0700）并排除出 Time Machine（幂等，失败静默）。

    `AGENT_WECHAT_HOME` 指进微信容器的可写范围（`in_wechat_container`）时**拒绝**：
    数据目录会整体落在容器里（`index.db`、密钥缓存、`tmp/`、默认产物根）——那是配置
    选择，但不是一次「只读」的操作。判据在 mkdir 之前，被拒时容器里不留任何东西。
    """
    global _tm_exclusion_checked, _tm_seen
    if not HOME_DIR.is_absolute():
        # EA-003：相对路径（`.`、`""`、`foo/bar`）在本仓没有合法用例——它会让密钥缓存与
        # 明文索引落到进程的当前工作目录。判据上移到路径的收口处；purge 自带的
        # `_check_home_is_sane` 保留（它还要挡 `/`、`~` 及其祖先）。
        raise HomeUnavailable(
            f"AGENT_WECHAT_HOME 必须是绝对路径：{HOME_DIR}",
            hint="设为绝对路径，或删掉该变量用默认 ~/.agent-wechat")
    _require_guard_resolvable()
    if _write_gate_refuses(HOME_DIR):
        raise ContainerWriteRefused(
            f"拒绝把数据目录指向微信容器目录（{container_bundle()} 及其所有上级目录）："
            f"{HOME_DIR}",
            hint="把 AGENT_WECHAT_HOME 指到容器之外（例如 ~/.agent-wechat），"
                 "或删掉该变量使用默认目录")
    try:
        HOME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        # exist_ok=True 挡不住符号链接环：CPython 的 mkdir 是
        # `except FileExistsError: if not exist_ok or not self.is_dir(): raise`，
        # 而 is_dir() 对环返回 False，于是 FileExistsError 原样抛出。
        # 建不出来就是不可用——core 层只抛项目异常（errors.py 开篇那条契约）。
        raise HomeUnavailable(
            f"数据目录不可用：{HOME_DIR}",
            hint=f"{exc}；请检查 AGENT_WECHAT_HOME 指向的位置是否可用") from exc
    _tighten_home_mode()
    if not _tm_exclusion_checked:
        _tm_exclusion_checked = True
        if shutil.which("tmutil") is not None:
            try:
                # **尽力而为**：首次建目录是唯一的排除时机，所以保留这次尝试；但它的成败
                # **不当状态**——F2（T9 复审）实测本机 addexclusion 对已存在目录需 ~11s >
                # timeout=10，拿它的成败当状态会报出假 ✗，而状态只在进程内、CLI 一次一进程，
                # 消费点永远看不到。真实状态由 `time_machine_excluded()` 的只读查询回答。
                # **先查再排**（Phase 02）：那 ~11s 此前**每个进程**都要付满 10s 超时——CLI 每次
                # 调用、MCP 每个会话的首次调用都卡 10s（Phase 00 实测占 `recent` 墙钟的 82%）。
                # 只读查询 ~0.05s；已经 `[Excluded]` 就不再 addexclusion。判不出来（None）
                # 照旧尝试排除——宁可多花时间，也不放过一个该排除的目录。
                state = time_machine_excluded()
                if not (state or "").startswith("[Excluded]"):
                    subprocess.run(["tmutil", "addexclusion", str(HOME_DIR)],
                                   capture_output=True, timeout=10)
                else:
                    _tm_seen = state
            except Exception:                  # noqa: BLE001 — 备份排除失败不该挡住任何命令
                pass


def _listdir_or_project_error(root: pathlib.Path, *, what: str) -> list[pathlib.Path] | None:
    """列出 `root` 的子项。**真不在**（或不是目录）→ None；**判不出来**（不可搜索/无权限）
    → 抛 `HomeUnavailable`。

    EA-002：三个探针此前一律先 `is_dir()`——它把 `OSError` 静默吞成 `False`，「不可搜索」
    于是被当成「不存在」返回 `[]`（"目录里明明有账号，却报没装微信"）。判不出来 ≠ 不存在：
    前者是权限问题（hint 指向完全磁盘访问权限），后者是正常形态。
    """
    try:
        return list(root.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise HomeUnavailable(
            f"读不了{what}：{root}",
            hint=f"{exc}；微信容器需要「完全磁盘访问权限」（见 INSTALL.md §3.3）") from exc


def _is_account_dir(p: pathlib.Path) -> bool:
    return bool(ACCOUNT_RE.match(p.name)) and (p / "db_storage").is_dir()


def account_dirs() -> list[str]:
    """枚举本机所有账号数据目录名（<微信号>_<hex4>，且其下有 db_storage/）。"""
    entries = _listdir_or_project_error(XWECHAT_FILES, what="微信数据目录")
    if entries is None:
        return []
    return sorted(p.name for p in entries if p.is_dir() and _is_account_dir(p))


def _name_shape(name: str) -> str:
    """目录名 → 形状：字母→a、数字→0、其余字符原样。排障要的是命名规则，不是名字本身。"""
    return "".join("a" if c.isascii() and c.isalpha() else "0" if c.isdigit() else c
                   for c in name)


def unrecognized_dirs() -> list[str] | None:
    """数据目录下**没认成账号**的子目录（issue #1 排障用）；数据目录不存在返回 None。

    已知的非账号目录（`KNOWN_NON_ACCOUNT_DIRS`）原名列出，其余只给形状——目录名里有微信号。
    """
    entries = _listdir_or_project_error(XWECHAT_FILES, what="微信数据目录")
    if entries is None:
        return None
    return sorted(p.name if p.name in KNOWN_NON_ACCOUNT_DIRS else _name_shape(p.name)
                  for p in entries
                  if p.is_dir() and not p.name.startswith(".") and not _is_account_dir(p))


def bare_wxid(account: str) -> str:
    """wxid_testacct01_ab12 -> wxid_testacct01（登录目录用裸 wxid）。"""
    return account.rsplit("_", 1)[0]


def db_storage(account: str) -> pathlib.Path:
    return XWECHAT_FILES / account / "db_storage"


def message_shards(account: str) -> list[pathlib.Path]:
    """消息分片（message_0.db..N），按编号排序。绝不包含 fts/resource/revoke/biz/media。"""
    d = db_storage(account) / "message"
    entries = _listdir_or_project_error(d, what=f"消息分片目录（{account}）")
    if entries is None:
        return []
    shards = [(int(m.group(1)), d / m.group(0))
              for p in entries if (m := SHARD_RE.match(p.name))]
    return [p for _, p in sorted(shards)]


def key_info_dat(account: str) -> pathlib.Path:
    """app_data/login/<裸wxid>/key_info.dat（每次登录重写的那份）。

    **磁盘解密路线已结项**（见 ARCHITECTURE §3.2）：此函数仅为路径形状留档，无生产调用。
    """
    return APP_DATA / "login" / bare_wxid(account) / "key_info.dat"


def key_info_db(account: str) -> pathlib.Path:
    """xwechat_files/all_users/login/<裸wxid>/key_info.db（明文 SQLite，56 行包裹 blob）。

    **磁盘解密路线已结项**（见 ARCHITECTURE §3.2）：此函数仅为路径形状留档，无生产调用。
    """
    return XWECHAT_FILES / "all_users" / "login" / bare_wxid(account) / "key_info.db"


def all_dbs(account: str) -> list[pathlib.Path]:
    """账号下全部 .db 文件（用于密钥校验覆盖统计）。

    F8（T9 复审）：此前是一句 `root.glob("*/*.db")`——CPython 的 glob 在**某个子目录**
    scandir 失败时**静默跳过**它，于是「整个 message/ 读不出来」被报成「这个账号没有
    message 分片」（少报，还喂坏 EA-051 的 unreadable 计数与 `HMAC N/N` 的分母）。逐层列举、
    每层过同一个 helper：读不到是项目异常，真没有才是空列表（EA-002 的口径）。
    """
    root = db_storage(account)
    subs = _listdir_or_project_error(root, what=f"账号数据目录（{account}）")
    if subs is None:
        return []
    out: list[pathlib.Path] = []
    for sub in subs:
        if not sub.is_dir():
            continue
        files = _listdir_or_project_error(sub, what=f"库目录（{sub.name}）")
        if files is None:
            continue                 # 列举与访问之间消失 = 真没有
        out += [p for p in files if p.name.endswith(".db")]
    return sorted(out)


def launch_agents_dir() -> pathlib.Path:
    """launchd 用户级目录：**真实家目录**下的 `~/Library/LaunchAgents`。

    **刻意不跟随 `AGENT_WECHAT_HOME` / `HOME_DIR`**：launchd 只扫描真实家目录，
    写到别处不会被加载。与 `CONTAINER` / `HOME_DIR` 的默认值、`whisper_model_candidates`、
    `_tighten_home_mode` 一样，是本模块里**确实指向用户主目录**的少数几处——
    本工具自己的产物路径都由 `HOME_DIR` 派生。测试 monkeypatch 本函数来隔离。
    """
    return pathlib.Path.home() / "Library" / "LaunchAgents"


def whisper_model_candidates() -> tuple[pathlib.Path, ...]:
    """whisper.cpp 常见模型位置（**真实家目录**，不跟随 AGENT_WECHAT_HOME）。

    与 `launch_agents_dir` 同一条理由：whisper-cli 只认用户自己放的模型，位置由它
    所在生态决定，不由我们的数据根决定。**只探测，不下载。**
    """
    home = pathlib.Path.home()
    return (home / ".cache" / "whisper-cpp" / "ggml-base.bin",
            home / "Library" / "Application Support" / "whisper-cpp" / "ggml-base.bin")


def pid_alive(pid: object) -> bool:
    """这个 pid 还活着吗。**判不出来一律当活着**（保守：不收割、不清扫），缺 pid / 非正整数
    → 视为不活。

    一份判据，两处用（`tasks._harvest` 的孤儿判据、`decrypt.sweep_stale_tempdirs` 的清扫判据）。
    此前两份**实现已经分叉**：`decrypt` 那份收了 `OverflowError`（T10 复审 N1 抓的），
    `tasks` 那份没接——于是同一个 `tasks.json` 里一个超大 pid 会让 `status` / `start`
    把 `OverflowError` 抛出 core（它是 `ArithmeticError` 子类，**不是** `OSError`），
    而本文件所在层的契约是「core 只抛项目异常」。合并到一处，这类分叉不可能再发生。

    `pid` 收 `object` 而不是 `int`：调用方读的是 JSON 里的值（可能是字符串 / null / 浮点）。
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError):
        # PermissionError = 别人的进程 = 活着；OverflowError = pid 超出 C long（判不出来）；
        # 其余 OSError 同理保守。三种都归「活着」。
        return True
    return True


def pid_started_after(pid: int, ts: int) -> bool:
    """`pid` 现在这个进程是否**确定**晚于 `ts`（unix 秒）启动——是则它只是复用了这个 pid。

    取 `ps -o etime=`（已运行时长，`[[dd-]hh:]mm:ss`）倒推启动时刻：不经本地时区解析，
    不受夏令时影响。秒级截断留 2 s 余量。**判不出来一律答 False**（同 `pid_alive` 的保守立场）。
    """
    try:
        out = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        days, _, hms = out.rpartition("-")
        secs = 0
        for part in hms.split(":"):
            secs = secs * 60 + int(part)
        secs += int(days or 0) * 86400
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return time.time() - secs > ts + 2


def sqlite_ro_uri(p: str | pathlib.Path) -> str:
    """只读 sqlite URI（配 `sqlite3.connect(..., uri=True)`）。路径必须百分号转义：直接拼
    `file:{p}` 时，目录名里的 `?` / `#` 会截断路径（`mode=ro` 也随之落进被丢弃的那段，变成
    读写打开**另一个**文件），`%` 被当转义。`AGENT_WECHAT_HOME` 由用户指定，这些字符都可能出现。
    全仓的只读 URI 都走这里（此前 decrypt 有一份 `_ro_uri`，store / vector 各自裸拼）。"""
    return pathlib.Path(p).absolute().as_uri() + "?mode=ro"


def atomic_write(dest: pathlib.Path, data: str | bytes, *, mode: int = 0o600) -> None:
    """临时文件 + `os.replace`：失败时 `dest` 上既有的文件**原样不动**，也不会出现半截文件。

    共用于密钥缓存、任务注册表、媒体产物与 HTML/Markdown 导出页，此前各写一份、写法已经分叉
    （媒体那份直接截断终路径再写，断电后半截文件被「已导出」判据永久跳过）。
    - 换名前 `fsync` 临时文件：rename 不保证数据先落盘。
    - 临时文件在**同目录**（`os.replace` 只在同一文件系统上原子）、名带 pid（并发进程不共用
      同一个 tmp），以 `mode` **建出**——落到正式文件名之前权限已经收紧；chmod 再收一次
      （同名残留可能是别的权限）。
    - 写入与 chmod 走 `Path` 的方法：故障注入矩阵（`tests/faultkit.py`）按它们记账。
    - **任何**中断（含 KeyboardInterrupt）都抹掉临时文件；异常原样上抛（OSError 由调用方
      翻成各自的项目异常）。崩溃（SIGKILL）留下的 tmp 由 `sweep_stale_tmp` 收。
    - `os.replace` 换的是**名字**：`dest` 是符号链接时替换链接本身，不会写穿到链接目标。
    """
    if isinstance(data, str):
        _atomic(dest, lambda tmp: tmp.write_text(data, encoding="utf-8"), mode)
    else:
        _atomic(dest, lambda tmp: tmp.write_bytes(data), mode)


def atomic_copy(src: pathlib.Path, dest: pathlib.Path, *, mode: int = 0o600) -> None:
    """同 `atomic_write`，内容来自文件 `src`（流式复制，不整读进内存——视频可达数百 MB）。
    `shutil.copyfile` 只拷数据、不拷权限：临时文件建出时的 `mode` 留着。"""
    _atomic(dest, lambda tmp: _copyfile(src, tmp), mode)


def _atomic(dest: pathlib.Path, fill: Callable[[pathlib.Path], object], mode: int) -> None:
    # 名字里带线程号：同一进程的两个线程写同一个目标（MCP 的同步工具跑在工作线程里，两次并发的
    # 媒体导出会撞上同一个文件）时，各写各的 tmp；只带 pid 的话一方的 `O_TRUNC` / `replace` /
    # 失败清理会踩掉另一方的，成功的写被报成失败。`sweep_stale_tmp` 只看第一段的 pid。
    tmp = dest.with_name(f"{dest.name}.{os.getpid()}.{threading.get_native_id()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
        try:
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        fill(tmp)
        tmp.chmod(mode)
        # 先落盘再换名：否则断电后可能是「新名字、空内容」——rename 先于数据持久化，
        # 而空文件会被「已存在」判据当成已导出永久跳过（密钥缓存则是读出空串）。
        fd = os.open(tmp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, dest)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass                       # 抹不掉也不掩盖原始错误
        raise


def sweep_stale_tmp(dest: pathlib.Path) -> None:
    """清掉崩溃遗留的 `<dest>.<pid>.<tid>.tmp`（`atomic_write` 的产物）。**只清死 pid 的**——活进程的
    tmp 可能正在被写，绝不碰（与 `decrypt.sweep_stale_tempdirs` 同一立场）。尽力而为。"""
    prefix = dest.name + "."
    try:
        entries = list(dest.parent.iterdir())
    except OSError:
        return
    for p in entries:
        n = p.name
        if not n.startswith(prefix) or not n.endswith(".tmp"):
            continue
        pid = n[len(prefix):-len(".tmp")].split(".", 1)[0]   # `<pid>` 或 `<pid>.<tid>`
        if not pid.isdigit() or pid_alive(int(pid)):
            continue
        try:
            p.unlink()
        except OSError:
            pass


def time_machine_state() -> str | None:
    """同 `time_machine_excluded`，但本进程的 `ensure_home_dirs` 刚查到过 `[Excluded]`（且没去改它）
    时直接用那一次的结果：`status` 此前对同一个目录查两遍 `tmutil isexcluded`。"""
    return _tm_seen if _tm_seen is not None else time_machine_excluded()


def time_machine_excluded() -> str | None:
    """备份排除的**真实状态**（只读查询 `tmutil isexcluded`）：`[Excluded]` / `[Included]`；
    判不出来（无 tmutil / 超时 / 非零退出 / HOME_DIR 还不存在）→ None。

    F2（T9 复审，P2）：EA-054 的原修法记的是「上次 `addexclusion` 尝试的成败」——**进程内**
    状态，而 CLI 一次调用一个进程，唯一消费点 `status` 因此恒拿到 None（那行永不打印）；
    即便拿到，本机形态也是假 ✗。改成查真实状态：回答的是「现在排没排除」，与 purge 的
    `tmutil isexcluded` 核查同源同口径。**判不出来与没排除是两件事**，前者返回 None。
    """
    if not HOME_DIR.is_dir() or shutil.which("tmutil") is None:
        return None                    # 没建过目录谈不上排除；无 tmutil 就判不出来
    try:
        r = subprocess.run(["tmutil", "isexcluded", str(HOME_DIR)],
                           capture_output=True, text=True, timeout=10)
    except Exception:                  # noqa: BLE001 — 探测失败只表示「判不出来」
        return None
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip() or None          # tmutil 原文（含路径），与 purge 同口径


def container_bundle() -> pathlib.Path:
    """写闸门的判据根：`…/com.tencent.xinWeChat`（整个容器 bundle）。

    **调用时由 `CONTAINER_DATA.parent` 派生，不做模块常量**：测试会 monkeypatch
    `paths.CONTAINER_DATA` 把判据重定向到 tmp（`test_fault_injection` /
    故障矩阵用例），import 期绑定的副本会让那条重定向静默失效。

    定义在文件末尾是**刻意的**：故障矩阵按**行号**冻结（`paths.py:65:resolve` 这类），
    在文件前部插函数会把所有行号推移，整批格子随之「故障未命中」。追加在末尾，
    行号零位移。
    """
    return CONTAINER_DATA.parent
