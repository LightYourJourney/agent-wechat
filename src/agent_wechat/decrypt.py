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
from .errors import DecryptError


def ensure_home_dirs() -> None:
    paths.ensure_home_dirs()
    (paths.HOME_DIR / "tmp").mkdir(mode=0o700, parents=True, exist_ok=True)


def snapshot(db: pathlib.Path, td: pathlib.Path) -> pathlib.Path:
    """db+wal+shm 三元组快照到私有目录。微信目录零写入（只读打开+复制）。"""
    dst = td / db.name
    for suf in ("", "-wal", "-shm"):
        src = pathlib.Path(str(db) + suf)
        if src.exists():
            shutil.copy2(src, pathlib.Path(str(dst) + suf))
    return dst


@contextlib.contextmanager
def decrypted(db: pathlib.Path, passphrase: bytes, tmpdir: pathlib.Path | None = None):
    """上下文管理器：yield 解密后的明文库路径，退出时自动清理临时文件。"""
    ensure_home_dirs()
    td = pathlib.Path(tempfile.mkdtemp(prefix="awdec-", dir=tmpdir or (paths.HOME_DIR / "tmp")))
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
        r = subprocess.run(["sqlcipher", str(snap), *cmds], capture_output=True, text=True)
        if r.returncode != 0 or not out.exists():
            raise DecryptError(f"sqlcipher 解密失败: {r.stderr.strip()[:300]}")
        yield out
    finally:
        shutil.rmtree(td, ignore_errors=True)


def open_plain(plain: pathlib.Path) -> sqlite3.Connection:
    """打开明文副本（只读模式）。"""
    return sqlite3.connect(f"file:{plain}?mode=ro", uri=True)
