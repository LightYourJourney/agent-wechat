"""一次性引导：lldb 断点捕获 passphrase（唯一需要 sudo 的环节）。

原理（与 wcdb-key-tool 的已验证实现校准一致）：
- WeChat 4.1+ 登录时调用系统 CommonCrypto 的 CCKeyDerivationPBKDF 派生密钥
- 该符号是苹果公开系统函数，无需逆向微信二进制
- 在其上断点（条件：passwordLen == 32），从 password 指针寄存器读 32 字节
- passphrase 只在登录时计算一次 → 捕获期间用户需退出登录再重新登录

风险与代价：
- 需先对微信 ad-hoc 重签名去掉 Hardened Runtime（否则 task_for_pid 被内核拒绝）：
      killall WeChat && sudo codesign --force --deep --sign - /Applications/WeChat.app
  微信每次升级都会还原签名，需重做。
- lldb 只在断点命中瞬间读一次内存即 detach，不修改微信行为、不碰网络。
"""
from __future__ import annotations

import dataclasses
import os
import platform
import re
import select
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Callable

from . import account, keys, paths
from .errors import CaptureError

# lldb 捕获脚本的临时文件前缀：核查端（purge 的 tmp_leaks 探测）从这里取值，别写第二份。
TMP_PREFIX = "awcap_"

DEFAULT_TIMEOUT = 300

_MIN_HEXDUMP_LINE = re.compile(r"\s*0x[0-9a-f]+:\s+((?:0x[0-9a-f]{2}\s*)+)$")


@dataclasses.dataclass(frozen=True)
class BootstrapProgress:
    """bootstrap 的进度事件。

    核心层不产生展示文案：kind + 结构化字段交入口层渲染（同序、逐字见
    `cli.cmd_bootstrap`）。为什么需要回调而不是只返回结果：前两条提示必须发生在
    lldb 捕获（最长 timeout 秒）**之前**，否则用户看不到「退出登录 → 重新登录」的
    指引；「同序」也要求 account 行先于 ✅ 行。
    """

    kind: str                       # "account" | "attach" | "verified" | "warn"
    bare: str | None = None         # kind="account"
    pid: int | None = None          # kind="account"
    ok_libs: int = 0                # kind="verified"
    total_libs: int = 0             # kind="verified"
    warning: str | None = None      # kind="warn"（EA-005：属主未归还等非致命降级）


def _killpg_quiet(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass                                       # 组里已经没人（或只剩僵尸）：这正是常态


def _terminate_capture(proc: subprocess.Popen) -> None:
    """收尾：**只向本次捕获的 lldb 进程组**发信号，任何路径都不按名字杀（EA-004）。

    组号就是 `proc.pid`：Popen 用 `start_new_session=True` 起，bash 是新会话的首进程
    （`test_capture_starts_its_own_session…` 钉住这一点）。**不用 `getpgid` 现查**——
    bash 一旦被回收（超时路径上 `_read_capture` 的 `poll()` 就会回收它）`getpgid` 必抛
    ESRCH，此前两轮都因此「不可用」，落到按名 `pkill -9 -x lldb`，一次普通的「没在时限内
    重新登录」就以 root 杀掉本机所有 lldb（含用户自己的调试会话）。

    bash 已退出 = lldb 已结束，但喂 stdin 的进程替换（`sleep`）可能还活着，补一刀 SIGKILL
    把它（以及「bash 被外力杀掉、lldb 成孤儿」的情形）一并收掉；还在跑就 SIGTERM → 等 3 s → SIGKILL → 再等 3 s，最后无论如何再对本组
    补一刀 SIGKILL。失败一律吞掉：清理是尽力而为，不因它把引导打挂（A 矩阵 73）。
    """
    pgid = proc.pid
    if proc.poll() is None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            _killpg_quiet(pgid, sig)
            try:
                proc.wait(timeout=3)
                return
            except Exception:                      # noqa: BLE001 — 兜底路径要吞遍所有失败
                continue
    _killpg_quiet(pgid, signal.SIGKILL)


def _read_capture(proc: subprocess.Popen, deadline: float) -> str:
    """读 lldb 的输出直到 deadline 或捕获到 passphrase（EA-052）。

    用 `select` + `os.read` 而不是 `readline`：**半行**输入（lldb 的输出没有换行就停住）
    会让 `readline` 无限阻塞——deadline 只在两次 readline 之间被检查，整个引导因此可能
    远超 `timeout`。`os.read` 有多少读多少，循环每次都回到 deadline 判断。
    （`text=True` 只是给调用方方便；这里绕开 TextIOWrapper 直接读 fd。）

    lldb 提前退出（attach 失败、微信没重签名）时管道等不到 EOF——喂 stdin 的 `sleep` 还占着
    stderr——所以读不到新数据时还要看进程是否已退出，退出了就立刻返回，不等满 deadline。
    """
    buf = ""
    out = proc.stdout
    if out is None:                   # 只有不带 stdout=PIPE 起的进程才会这样：读不到就是没捕获
        return buf
    while time.time() < deadline:
        # 超时不许越过 deadline：固定 3.0 会让「300s 的 timeout」实际超到 303s，
        # 而按秒计的 deadline 承诺在这里就该是准的（EA-052 的测试钉住了这条）。
        r, _, _ = select.select([out], [], [], max(0.0, min(3.0, deadline - time.time())))
        if r:
            chunk = os.read(out.fileno(), 65536)
            if not chunk:
                if proc.poll() is not None:
                    break
                continue
            buf += chunk.decode("utf-8", "replace")
        elif proc.poll() is not None:
            break                     # lldb 已退出且输出已读尽
        if _parse_passphrase(buf):
            break
    return buf


def _parse_passphrase(out: str) -> str | None:
    """从 lldb `memory read --format x` 的 hexdump 中提取 32 字节（64 hex 字符）。"""
    by: list[str] = []
    for line in out.splitlines():
        m = _MIN_HEXDUMP_LINE.match(line)
        if m:
            by += re.findall(r"0x([0-9a-f]{2})", m.group(1))
    return "".join(by[:32]) if len(by) >= 32 else None


def capture_passphrase_lldb(timeout: int = DEFAULT_TIMEOUT) -> str:
    """attach 微信并在 CCKeyDerivationPBKDF 断点等待重新登录触发。返回 64 hex 字符。"""
    if not shutil.which("lldb"):
        raise CaptureError("未检测到 lldb，请先: xcode-select --install")
    pid = account.wechat_pid()
    if not pid:
        raise CaptureError("未找到微信进程，请先启动微信")

    is_arm = platform.machine() in ("arm64", "aarch64")
    pw_reg, len_reg = ("x1", "x2") if is_arm else ("rsi", "rdx")

    script = (
        "settings set target.preload-symbols false\n"
        f"process attach -p {pid}\n"
        f"breakpoint set -n CCKeyDerivationPBKDF -c '${len_reg} == 32'\n"
        "breakpoint command add 1\n"
        f"memory read --size 1 --count 32 --format x ${pw_reg}\n"
        "detach\n"
        "quit\n"
        "DONE\n"
        "process continue\n"
    )
    fd, sp = tempfile.mkstemp(suffix=".lldb", prefix=TMP_PREFIX)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(script)
    except OSError as exc:
        # A 矩阵 134：写 lldb 脚本失败此前裸穿（ENOSPC/EACCES/EINTR）。
        try:
            os.unlink(sp)
        except OSError:
            pass
        raise CaptureError(
            f"无法写入 lldb 脚本：{sp}",
            hint=f"{exc}；请确认临时目录可写、磁盘有空间") from exc

    # cat 脚本后用 sleep 保活 stdin：lldb continue 是异步的，stdin EOF 会提前退出，
    # 等不到用户重新登录触发断点。走**进程替换**而不是管道：管道里 bash 要等 sleep 结束才退，
    # lldb 提前退出（attach 失败）就看不出来；进程替换下 lldb 一退 bash 就退（`_read_capture` 盯它）。
    # EA-004：起**独立进程组**（start_new_session）——收尾靠 killpg 精确命中本次的
    # bash→lldb 进程树，而不是按名字杀（见 _terminate_capture）。
    try:
        proc = subprocess.Popen(
            # 路径要 shell 转义：`TMPDIR` 含空格或 `;` 时，不转义的 cat 读不到脚本。
            ["bash", "-c", f"TERM=dumb lldb < <(cat {shlex.quote(sp)}; sleep {timeout})"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        # A 矩阵 141/155：起不了 lldb 此前裸穿（找不到 bash / ENOSPC / EACCES / 超时）。
        try:
            os.unlink(sp)
        except OSError:
            pass
        raise CaptureError(
            f"无法启动 lldb 捕获进程：{exc}",
            hint="确认已安装 Xcode 命令行工具、且磁盘有空间后重试") from exc
    deadline = time.time() + timeout
    try:
        buf = _read_capture(proc, deadline)
    finally:
        _terminate_capture(proc)
        try:
            os.unlink(sp)
        except OSError:
            pass

    ph = _parse_passphrase(buf)
    if not ph and time.time() < deadline:
        # `_read_capture` 只在解析出 passphrase 或 lldb 已退出时提前返回：这是 lldb 自己退了。
        # 只回显 lldb 的 error 行（attach 失败的原因），hexdump 片段不进消息。
        err = next((ln.strip() for ln in buf.splitlines() if ln.strip().startswith("error:")), "")
        raise CaptureError(f"lldb 在捕获完成前就退出了（多半是 attach 微信失败）{'：' + err if err else ''}。"
                           "请确认：以 sudo 运行；微信已重签名"
                           "（sudo codesign --force --deep --sign - /Applications/WeChat.app）")
    if not ph:
        raise CaptureError("未捕获到 passphrase。请确认：捕获期间已在微信内退出登录并重新登录；"
                           "微信已重签名（sudo codesign --force --deep --sign - /Applications/WeChat.app）")
    return ph


def bootstrap(timeout: int = DEFAULT_TIMEOUT,
              progress: Callable[[BootstrapProgress], None] | None = None) -> str:
    """完整引导：捕获 → 用账号的库做 HMAC 校验 → 通过才落缓存。返回裸 wxid。

    进度经 progress 回调外发；本层不打印（用户可见输出归入口层）。
    """
    emit = progress or (lambda _ev: None)
    st = account.status()
    if not st.logged_in:
        raise CaptureError("无法确定当前登录账号，请先登录微信")
    bare = paths.bare_wxid(st.current)  # type: ignore[arg-type]

    emit(BootstrapProgress("account", bare=bare, pid=st.pid))
    emit(BootstrapProgress("attach"))
    hexstr = capture_passphrase_lldb(timeout)

    passphrase = bytes.fromhex(hexstr)
    results, unreadable = keys.verify_passphrase(st.current, passphrase)  # type: ignore[arg-type]
    ok = [name for name, good in results if good]
    total = len(results)
    if not ok:
        if total == 0 and unreadable == 0:
            # F9（T9 复审）：库在盘上而一个 .db 都没有（账号刚登录还没同步过 / 目录刚迁移）
            # ——这不是权限问题，说成「（0 个不可读）」自相矛盾，还会把用户送去改系统设置。
            raise CaptureError(
                "账号目录下没有可解密的数据库——微信还没同步过？"
                "确认已登录并同步后再重试引导。")
        if total == 0:
            # EA-051：一个库都没读到 ≠ 捕获值不对——此前两者共用「重试捕获」的文案，
            # 用户会一遍遍重跑引导，而真问题是读不到库（权限/完全磁盘访问权限）。
            raise CaptureError(
                f"一个库都没能读到（{unreadable} 个不可读）——这通常是权限问题："
                "请给终端「完全磁盘访问权限」（见 INSTALL.md §3.3）后重试。")
        raise CaptureError(f"捕获值未通过任何库的 HMAC 校验（{total} 个库），拒绝落缓存。"
                           "可能捕获了错误时刻的派生值，请重试。")
    warn = keys.save_cached_hex(bare, hexstr)
    emit(BootstrapProgress("verified", ok_libs=len(ok), total_libs=total))
    if warn:
        # EA-005：属主未归还这类**非致命**降级——缓存已落盘（verified 行照报），
        # 但要让用户知道 sudo 之外可能读不到，而不是把整次引导判负。
        emit(BootstrapProgress("warn", warning=warn))
    return bare
