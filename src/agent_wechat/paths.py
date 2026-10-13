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

from .errors import HomeUnavailable

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
# EA-054：备份排除这条控制项的**状态**（None = 尚未尝试 / True 成功 / False 失败）。
# 此前「置位在尝试之前」+ 静默 except：成功与失败在进程内不可区分，也没有任何命令报它
# ——同为安全控制的「提交拦截」有 `scan.py --check-hooks` 的自检，这条却是全静默的。
_tm_exclusion_ok: bool | None = None


def ensure_home_dirs() -> None:
    """确保数据目录存在（0700）并排除出 Time Machine（幂等，失败静默）。"""
    global _tm_exclusion_checked, _tm_exclusion_ok
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
        if shutil.which("tmutil") is None:
            _tm_exclusion_ok = False       # 便宜路径：连二进制都没有，不必起子进程
        else:
            try:
                subprocess.run(["tmutil", "addexclusion", str(HOME_DIR)],
                               capture_output=True, timeout=10, check=True)
                _tm_exclusion_ok = True
            except Exception:                  # noqa: BLE001 — 备份排除失败不该挡住任何命令
                # 失败**可观测**（`time_machine_excluded()` → status 印一行），但**不重试**：
                # 本机实测 tmutil 超时就是 10s 级，每次 ensure_home_dirs 重试会把常用命令
                # 拖成秒级（EA-054 的「失败可重试」按此实测取舍，理由记在 ⑤）。
                _tm_exclusion_ok = False


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
    """账号下全部 .db 文件（用于密钥校验覆盖统计）。"""
    root = db_storage(account)
    if _listdir_or_project_error(root, what=f"账号数据目录（{account}）") is None:
        return []
    return sorted(root.glob("*/*.db"))


def time_machine_excluded() -> bool | None:
    """备份排除的状态（EA-054）：True 已排除 / False 尝试过但失败（请手动 tmutil
    addexclusion）/ None 尚未尝试过（进程还没建过 HOME_DIR）。"""
    return _tm_exclusion_ok
