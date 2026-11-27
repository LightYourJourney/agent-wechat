"""登录态检测与账号识别。只读，绝不修改微信任何状态。

识别原理（本机已实测复现）：
1. 主进程：pgrep -x WeChat（精确进程名，天然排除 WeChatAppEx Helper 等子进程）
2. 当前登录账号：lsof -p <pid> 打开的 xwechat_files/<微信号>_<hex4> 目录——
   实测只出现当前登录账号（230 个 fd），未登录的历史账号不出现。
"""
from __future__ import annotations

import dataclasses
import subprocess
import time
from collections.abc import Collection

from . import paths
from .errors import AccountNotFound

# 模块级可替换点：测试注入桩进程输出的唯一手段（同 keys.CACHE_FILE / paths.XWECHAT_FILES）。
_run = subprocess.run


@dataclasses.dataclass
class AccountStatus:
    running: bool
    current: str | None        # 当前登录账号目录名（<微信号>_<hex4>），不确定时为 None
    accounts: list[str]        # 本机全部账号目录名
    pid: int | None
    # 排障诊断（issue #1）：数据目录在不在、里面哪些子目录没认成账号（只给形状，见 paths）
    data_dir_exists: bool = True
    unrecognized: list[str] = dataclasses.field(default_factory=list)

    @property
    def logged_in(self) -> bool:
        return self.current is not None


def _parse_pids(out: str) -> list[int]:
    """pgrep 输出 → pid 列表；非数字 token 忽略。"""
    return [int(x) for x in out.split() if x.strip().isdigit()]


def wechat_pid() -> int | None:
    """微信主进程 PID；未运行返回 None。

    EA-052：pgrep 也要有超时——此前它没有 `timeout=`（系统卡顿时会一直挂住），
    与同文件的 lsof 调用（15s）不成体系。超时与「pgrep 报错」同义：判不出来就是 None。
    """
    try:
        r = _run(["pgrep", "-x", "WeChat"], capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        # A 矩阵：起不了子进程（ENOSPC/EACCES）与超时同义——判不出来就是 None，
        # 这是只读探测，不该让「微信在不在跑」这个问题把命令打挂。
        return None
    pids = _parse_pids(r.stdout)
    return pids[0] if pids else None


def _parse_account_from_lsof(out: str, known: Collection[str] | None = None) -> str | None:
    """lsof 打开文件列表 → 唯一命中的账号目录名（<微信号>_<hex4>）；无/多命中返回 None。

    给了 `known`（本机账号目录）就只认其中的名字：形似账号的非账号目录不该造成多命中。
    """
    hits: set[str] = set()
    for line in out.splitlines():
        if "xwechat_files/" not in line:
            continue
        tail = line.split("xwechat_files/", 1)[1]
        name = tail.split("/", 1)[0]
        if paths.ACCOUNT_RE.match(name) and (known is None or name in known):
            hits.add(name)
    return next(iter(hits)) if len(hits) == 1 else None


def current_account(pid: int, known: Collection[str] | None = None) -> str | None:
    """lsof 判定当前登录账号。非唯一命中（如启动瞬间）视为不确定，不猜。"""
    try:
        r = _run(["lsof", "-n", "-P", "-p", str(pid)],
                 capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return None                    # 同上：探测失败=不确定，不是错误
    return _parse_account_from_lsof(r.stdout, known)


# 常驻的 MCP server 每次工具调用都要判一次当前账号（pgrep + lsof 约 0.1–0.2 s，检索类调用
# 的大头）。进程内短时缓存：PID_TTL 秒内连 pgrep 都不跑；pid 没变且不超过 ACCOUNT_TTL 秒就
# 不重跑 lsof（微信重启后 pid 会变；同一进程里切账号的情况由 ACCOUNT_TTL 兜住）。
# 只缓存确定的结果——判不出（None）下次照常重判。
PID_TTL = 5.0
ACCOUNT_TTL = 60.0
_pid_cache: tuple[float, int | None] | None = None
_account_cache: tuple[float, int, tuple[str, ...], str] | None = None


def _cached_pid() -> int | None:
    global _pid_cache
    now = time.monotonic()
    if _pid_cache is not None and now - _pid_cache[0] < PID_TTL:
        return _pid_cache[1]
    pid = wechat_pid()
    # 只缓存确定的 PID——判不出（None）下次照常重判（与 `_cached_current` 同一契约）
    _pid_cache = (now, pid) if pid is not None else None
    return pid


def _cached_current(pid: int, accounts: list[str]) -> str | None:
    global _account_cache
    now, known = time.monotonic(), tuple(accounts)
    c = _account_cache
    if c is not None and c[1] == pid and c[2] == known and now - c[0] < ACCOUNT_TTL:
        return c[3]
    cur = current_account(pid, accounts)
    _account_cache = (now, pid, known, cur) if cur else None
    return cur


def status() -> AccountStatus:
    pid = _cached_pid()
    accounts = paths.account_dirs()
    cur = _cached_current(pid, accounts) if pid else None
    unrecognized = paths.unrecognized_dirs()
    return AccountStatus(running=pid is not None, current=cur, accounts=accounts, pid=pid,
                         data_dir_exists=unrecognized is not None,
                         unrecognized=unrecognized or [])


def resolve_account(account: str | None) -> str:
    """把 None 解析成当前登录账号；显式指定但目录不存在时报错。"""
    if account:
        if account not in paths.account_dirs():
            raise AccountNotFound(f"账号目录不存在: {account}（已知: {paths.account_dirs()}）")
        return account
    st = status()
    if not st.logged_in:
        raise AccountNotFound("微信未运行或无法确定当前登录账号；请先登录，或用 --account 显式指定")
    return st.current  # type: ignore[return-value]
