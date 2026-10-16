"""微信数据路径解析。纯函数 + 常量，不做有副作用的 IO。

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

from .errors import ContainerWriteRefused, HomeUnavailable

CONTAINER = pathlib.Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents"
XWECHAT_FILES = CONTAINER / "xwechat_files"
APP_DATA = CONTAINER / "app_data"

# 账号目录形如 wxid_testacct01_ab12（尾部 _hex4 是实例后缀）
ACCOUNT_RE = re.compile(r"^wxid_.+_[0-9a-f]{4}$")
# 消息分片形如 message_0.db；不含 message_fts/message_resource 等非分片库
SHARD_RE = re.compile(r"^message_(\d+)\.db$")

# 本项目数据目录（0700）：密钥缓存、临时明文、导出库。可用环境变量覆盖。
# EA-003：空值必须走默认——`Path("")` 是 `.`，`AGENT_WECHAT_HOME=` 会把 passphrase 与
# 明文索引落进**当前工作目录**，而闸门只在销毁侧（purge）。故用 `or` 而非 get 的默认值。
HOME_DIR = pathlib.Path(
    os.environ.get("AGENT_WECHAT_HOME") or (pathlib.Path.home() / ".agent-wechat"))

_tm_exclusion_checked = False


def in_wechat_container(p: str | pathlib.Path) -> bool:
    """`p` 解析后是否落在微信容器目录（`CONTAINER`）之内——**含 `CONTAINER` 本身**。

    判据是**解析后**的路径：`Path.resolve(strict=False)` 展开 `..`、跟随符号链接，相对
    路径按**进程 CWD** 展开（`resolve` 的默认语义）。四个 `--out` 入口的一个判据
    （见 `refuse_container_out`），理由是 SECURITY.md §3 的只读宣称。

    **大小写**：macOS 默认 APFS 不区分大小写——`~/library/containers/…` 与
    `~/Library/Containers/…` 实测是同一个 inode，而 `resolve()` 不做大小写规范化
    ⇒ 只比字符串的话，换一种拼写就绕过判据。故字符串判据之后再用 `samefile` 把它与
    **既有祖先**逐个比 inode：只在「真的指同一目录」时为真，在区分大小写的卷上不会
    误伤（那时两种拼写是两个目录，且容器通常根本不存在）。

    **不含 `CONTAINER` 的祖先**（如 `~/Library/Containers`）：那写的是容器的同级/上级，
    不是容器内部——刻意不收（spec 的判据只到「容器本身及其子路径」）。

    判不出来（符号链接环 / 权限不足）返回 False：这种路径在随后的实际写入里会自己失败，
    在这里谎报「在容器里」是错诊断。
    """
    try:
        resolved = pathlib.Path(p).resolve(strict=False)
        container = CONTAINER.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    if resolved == container or resolved.is_relative_to(container):
        return True
    try:
        if not container.exists():
            return False                      # 容器不在 ⇒ 没有可写进去的东西
        return any(anc.exists() and os.path.samefile(anc, container)
                   for anc in (resolved, *resolved.parents))
    except OSError:
        return False


def refuse_container_out(p: str | pathlib.Path) -> None:
    """`p` 在微信容器内 → `ContainerWriteRefused`。四个 `--out` 入口的共享闸门。

    **在写之前叫停**：不静默改写路径、也不「警告后照写」——`--out` 是用户显式指定的
    落点，悄悄换地方会让「产物到底在哪」不可预期（判据见 `in_wechat_container`）。
    message 里带**容器路径本身**：换一种大小写拼写命中的时候，用户看到的那串 `--out`
    并不长成容器的样子，不印出容器就无从理解为什么被拒。
    """
    if in_wechat_container(p):
        raise ContainerWriteRefused(
            f"拒绝把输出写进微信容器目录（{CONTAINER}）：{p}",
            hint="换一个容器之外的目录（CLI 的 --out / MCP 的 out_dir），"
                 "例如 ~/Desktop/wechat-export")


def ensure_home_dirs() -> None:
    """确保数据目录存在（0700）并排除出 Time Machine（幂等，失败静默）。"""
    global _tm_exclusion_checked
    if not HOME_DIR.is_absolute():
        # EA-003：相对路径（`.`、`""`、`foo/bar`）在本仓没有合法用例——它会让密钥缓存与
        # 明文索引落到进程的当前工作目录。判据上移到路径的收口处；purge 自带的
        # `_check_home_is_sane` 保留（它还要挡 `/`、`~` 及其祖先）。
        raise HomeUnavailable(
            f"AGENT_WECHAT_HOME 必须是绝对路径：{HOME_DIR}",
            hint="设为绝对路径，或删掉该变量用默认 ~/.agent-wechat")
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
    if not _tm_exclusion_checked:
        _tm_exclusion_checked = True
        if shutil.which("tmutil") is not None:
            try:
                # **尽力而为**：首次建目录是唯一的排除时机，所以保留这次尝试；但它的成败
                # **不当状态**——F2（T9 复审）实测本机 addexclusion 对已存在目录需 ~11s >
                # timeout=10，拿它的成败当状态会报出假 ✗，而状态只在进程内、CLI 一次一进程，
                # 消费点永远看不到。真实状态由 `time_machine_excluded()` 的只读查询回答。
                subprocess.run(["tmutil", "addexclusion", str(HOME_DIR)],
                               capture_output=True, timeout=10)
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


def account_dirs() -> list[str]:
    """枚举本机所有账号数据目录名（wxid_xxx_hex4）。"""
    entries = _listdir_or_project_error(XWECHAT_FILES, what="微信数据目录")
    if entries is None:
        return []
    return sorted(p.name for p in entries if p.is_dir() and ACCOUNT_RE.match(p.name))


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
