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
import pathlib
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


def tmp_dir() -> pathlib.Path:
    """解密临时目录（0700）。调用方负责确保存在（见 ensure_home_dirs）。"""
    return paths.HOME_DIR / "tmp"


def ensure_home_dirs() -> None:
    paths.ensure_home_dirs()
    try:
        tmp_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        # EA-005：同路径那处（paths.ensure_home_dirs）的立场——core 只抛项目异常。
        raise HomeUnavailable(
            f"解密临时目录不可用：{tmp_dir()}",
            hint=f"{exc}；请检查 AGENT_WECHAT_HOME 指向的位置是否可用") from exc


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
    td = pathlib.Path(tempfile.mkdtemp(prefix="awdec-", dir=tmp_dir()))
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
