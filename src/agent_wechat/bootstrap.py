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
import shutil
import subprocess
import tempfile
import time
from typing import Callable

from . import account, keys, paths
from .errors import CaptureError

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

    kind: str                       # "account" | "attach" | "verified"
    bare: str | None = None         # kind="account"
    pid: int | None = None          # kind="account"
    ok_libs: int = 0                # kind="verified"
    total_libs: int = 0             # kind="verified"


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
    fd, sp = tempfile.mkstemp(suffix=".lldb", prefix="awcap_")
    with os.fdopen(fd, "w") as f:
        f.write(script)

    # cat 脚本后用 sleep 保活 stdin：lldb continue 是异步的，stdin EOF 会提前退出，
    # 等不到用户重新登录触发断点。
    proc = subprocess.Popen(
        ["bash", "-c", f"( cat {sp}; sleep {timeout} ) | TERM=dumb lldb"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    buf = ""
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            r, _, _ = select.select([proc.stdout], [], [], 3.0)
            if r:
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue
                buf += line
            if _parse_passphrase(buf):
                break
    finally:
        try:                      # 先温和终止，超时/失败再强杀
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
        try:
            os.unlink(sp)
        except OSError:
            pass
        subprocess.run(["pkill", "-9", "-x", "lldb"], capture_output=True)

    ph = _parse_passphrase(buf)
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
    results = keys.verify_passphrase(st.current, passphrase)  # type: ignore[arg-type]
    ok = [name for name, good in results if good]
    total = len(results)
    if not ok:
        raise CaptureError(f"捕获值未通过任何库的 HMAC 校验（{total} 个库），拒绝落缓存。"
                           "可能捕获了错误时刻的派生值，请重试。")
    keys.save_cached_hex(bare, hexstr)
    emit(BootstrapProgress("verified", ok_libs=len(ok), total_libs=total))
    return bare
