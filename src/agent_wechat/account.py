"""登录态检测与账号识别。只读，绝不修改微信任何状态。

识别原理（本机已实测复现）：
1. 主进程：pgrep -x WeChat（精确进程名，天然排除 WeChatAppEx Helper 等子进程）
2. 当前登录账号：lsof -p <pid> 打开的 xwechat_files/wxid_* 目录——
   实测只出现当前登录账号（230 个 fd），未登录的历史账号不出现。
"""
from __future__ import annotations

import dataclasses
import subprocess

from . import paths
from .errors import AccountNotFound

# 模块级可替换点：测试注入桩进程输出的唯一手段（同 keys.CACHE_FILE / paths.XWECHAT_FILES）。
_run = subprocess.run


@dataclasses.dataclass
class AccountStatus:
    running: bool
    current: str | None        # 当前登录账号目录名（wxid_xxx_hex4），不确定时为 None
    accounts: list[str]        # 本机全部账号目录名
    pid: int | None

    @property
    def logged_in(self) -> bool:
        return self.current is not None


def _parse_pids(out: str) -> list[int]:
    """pgrep 输出 → pid 列表；非数字 token 忽略。"""
    return [int(x) for x in out.split() if x.strip().isdigit()]


def wechat_pid() -> int | None:
    """微信主进程 PID；未运行返回 None。"""
    r = _run(["pgrep", "-x", "WeChat"], capture_output=True, text=True)
    pids = _parse_pids(r.stdout)
    return pids[0] if pids else None


def _parse_account_from_lsof(out: str) -> str | None:
    """lsof 打开文件列表 → 唯一命中的账号目录名（wxid_xxx_hex4）；无/多命中返回 None。"""
    hits: set[str] = set()
    for line in out.splitlines():
        if "xwechat_files/" not in line:
            continue
        tail = line.split("xwechat_files/", 1)[1]
        name = tail.split("/", 1)[0]
        if paths.ACCOUNT_RE.match(name):
            hits.add(name)
    return next(iter(hits)) if len(hits) == 1 else None


def current_account(pid: int) -> str | None:
    """lsof 判定当前登录账号。非唯一命中（如启动瞬间）视为不确定，不猜。"""
    try:
        r = _run(["lsof", "-n", "-P", "-p", str(pid)],
                 capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return None
    return _parse_account_from_lsof(r.stdout)


def status() -> AccountStatus:
    pid = wechat_pid()
    cur = current_account(pid) if pid else None
    return AccountStatus(running=pid is not None, current=cur,
                         accounts=paths.account_dirs(), pid=pid)


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
