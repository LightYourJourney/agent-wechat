"""解密层：快照 + sqlcipher CLI 导出明文。绝不写入微信目录。

流程：
1. 把 db+wal+shm 三元组快照到 ~/.agent-wechat/tmp/（0700）——微信运行在 WAL 模式，
   只拷主文件会缺最新数据；快照后我们拥有副本，后续操作不再触碰微信目录
2. passphrase -> PBKDF2 派生该库 enc_key -> 以 raw key 形式喂给 sqlcipher
   （PRAGMA key = "x'...'"，规避任意字节作为口令的转义问题；raw key 跳过 KDF，
   与我们自己的派生公式闭环，已由对照实验验证）
3. sqlcipher_export 到明文副本，yield 路径；退出时清理
"""
from __future__ import annotations

import contextlib
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import tempfile

from . import keys, paths
from .errors import DecryptError, HomeUnavailable


def _first_line(text: str) -> str:
    """stderr 的首个非空行（F3：折行回显块里的密钥残段不进消息，见 decrypted 的注释）。"""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


_AWDEC_RE = re.compile(r"^awdec-(\d+)-")


def tmp_dir() -> pathlib.Path:
    """解密临时目录（0700）。调用方负责确保存在（见 ensure_home_dirs）。"""
    return paths.HOME_DIR / "tmp"


_swept_stale = False          # 每进程只扫一次（EA-019）


def _pid_alive(pid: int) -> bool:
    """`os.kill(pid, 0)` 判活。`PermissionError` = 别人的进程 = **活着**（保守不删）。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def sweep_stale_tempdirs() -> int:
    """清扫 `tmp/` 下**没有活进程持有**的 `awdec-<pid>-*`，返回清掉的目录数（EA-019）。

    「用后即焚」此前只覆盖 `finally` 那条优雅路径：SIGKILL/崩溃时它不执行，MCP server 被
    客户端关闭时长任务在 **daemon 线程**里被解释器丢弃、`finally` 同样不保证执行——于是
    **分片明文**留在 `tmp/`（审计复现实测 8192 字节的 `plain.db`），而全仓没有任何清扫路径。

    **绝不无条件 `rmtree`**：多进程下会误删正在跑的任务的明文（`purge.py` 那句注释点过
    同款坑）。判据是 `os.kill(pid, 0)`：只有「确定已死」才清；判不出来（`PermissionError`
    等）一律留着。非 `awdec-` 形态的目录一律不碰。
    """
    swept = 0
    try:
        entries = list(tmp_dir().iterdir())
    except OSError:
        return 0                       # tmp 读不了：清扫是尽力而为，绝不因此挡住解密
    for d in entries:
        m = _AWDEC_RE.match(d.name)
        if not m or _pid_alive(int(m.group(1))):
            continue
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            swept += 1
    return swept


def ensure_home_dirs() -> None:
    global _swept_stale
    paths.ensure_home_dirs()
    try:
        tmp_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        # EA-005：同路径那处（paths.ensure_home_dirs）的立场——core 只抛项目异常。
        raise HomeUnavailable(
            f"解密临时目录不可用：{tmp_dir()}",
            hint=f"{exc}；请检查 AGENT_WECHAT_HOME 指向的位置是否可用") from exc
    if not _swept_stale:
        # 挂在「第一次解密之前」这条路径上（`decrypted()` 每次都先调本函数）：
        # 清扫必须发生在**新建**明文之前，否则刚建的目录会被自己扫掉。
        _swept_stale = True
        sweep_stale_tempdirs()


def snapshot(db: pathlib.Path, td: pathlib.Path) -> pathlib.Path:
    """db+wal+shm 三元组快照到私有目录。微信目录零写入（只读打开+复制）。"""
    dst = td / db.name
    for suf in ("", "-wal", "-shm"):
        src = pathlib.Path(str(db) + suf)
        if src.exists():
            shutil.copy2(src, pathlib.Path(str(dst) + suf))
    return dst


@contextlib.contextmanager
def decrypted(db: pathlib.Path, passphrase: bytes):
    """上下文管理器：yield 解密后的明文库路径，退出时自动清理临时文件。"""
    ensure_home_dirs()
    # 目录名带 **pid**（EA-019）：崩溃后的残留靠它判活（`sweep_stale_tempdirs`）。
    td = pathlib.Path(tempfile.mkdtemp(prefix=f"awdec-{os.getpid()}-", dir=tmp_dir()))
    try:
        snap = snapshot(db, td)
        page1 = snap.read_bytes()[:keys.PAGE_SZ]
        if len(page1) < keys.PAGE_SZ:
            raise DecryptError(f"{db.name}: 文件不足一页，无法解密")
        enc_key = keys.derive_key(passphrase, page1[:keys.SALT_SZ])

        out = td / "plain.db"
        cmds = [
            f'PRAGMA key = "x\'{enc_key.hex()}\'";',
            f"ATTACH DATABASE '{out}' AS plain KEY '';",   # 必须绝对路径：ATTACH 相对路径按进程 CWD 解析
            "SELECT sqlcipher_export('plain');",
            "DETACH DATABASE plain;",
        ]
        # EA-053：命令走 **stdin**，密钥不再出现在 argv 里（另一本地用户/进程 `ps` 看得到
        # 的窗口消失）。/tmp 实测（合成库三态）：stdin 与 argv 的 rc/产物一致；错误文案从
        # 「Parse error in 3rd command line argument」变成「Parse error near line N」，
        # 而「回显出错语句 + 插入符」的机制两形态都在——errors.redact 仍是必需的安全网。
        r = subprocess.run(["sqlcipher", str(snap)], input="\n".join(cmds) + "\n",
                           capture_output=True, text=True)
        if r.returncode != 0 or not out.exists():
            # EA-047：**不在这里截断**——`[:300]` 曾把跨过截断点的密钥切成「前半段」，
            # 而 redact 需要完整 64 位才认得出（截断必须发生在脱敏**之后**，由
            # errors.redact 统一做）。
            # F3（T9 复审）：只取**首个非空行**。sqlcipher 报错时会回显出错语句并按 ~80 列
            # **折行**，第二处回显因此留下密钥的前缀（实测 62/64 位）——而 redact 只认「恰好
            # 64 位连续 hex」，残段会一路进对外信封。回显块正是藏残段的地方；首行
            # （`Parse error near line N: near "…"`）已带够定位信息。
            raise DecryptError(f"sqlcipher 解密失败: {_first_line(r.stderr)}")
        yield out
    finally:
        shutil.rmtree(td, ignore_errors=True)


def open_plain(plain: pathlib.Path) -> sqlite3.Connection:
    """打开明文副本（只读模式）。"""
    return sqlite3.connect(f"file:{plain}?mode=ro", uri=True)
