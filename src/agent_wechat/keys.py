"""密钥层：passphrase 缓存、PBKDF2 派生、HMAC 自验证。

背景（WeChat 4.1+，经 spike 与参考实现双重验证）：
- 密钥不在磁盘明文存在（key_info.db 的 blob 是被包裹的材料，spike 已排除全部低成本假设）
- passphrase（32 字节随机）只在登录时经 CCKeyDerivationPBKDF 计算一次，常驻内存
- 因此：bootstrap（lldb 断点捕获，需 sudo，一次性）→ 缓存到本文件 → 之后所有操作无特权

每库密钥派生与验证公式（已用对照实验验证真假两向）：
    salt    = db 文件前 16 字节
    enc_key = PBKDF2-HMAC-SHA512(passphrase, salt, 256000 轮, 32B)
    mac_salt= salt ⊕ 0x3A
    mac_key = PBKDF2-HMAC-SHA512(enc_key, mac_salt, 2 轮, 32B)
    期望 HMAC = HMAC-SHA512(mac_key, page1[16:4032] + 页号(小端uint32))
    实际 HMAC 存于 page1[4032:4096]
    页面布局: [salt 16][密文 4000][IV 16][HMAC 64]
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import os
import pathlib
import struct
import time

from . import paths
from .errors import HomeUnavailable, KeyNotFound

KDF_ITER = 256_000
FAST_KDF_ITER = 2
KEY_SZ = 32
PAGE_SZ = 4096
SALT_SZ = 16
RESERVE = 80          # IV(16) + HMAC-SHA512(64)
HMAC_SZ = 64

CACHE_FILE = paths.HOME_DIR / "passphrase.json"


# ---------- 缓存 ----------

def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except FileNotFoundError:
        return {"accounts": {}}          # 真的没有：首次使用
    except json.JSONDecodeError:
        return {"accounts": {}}          # 缓存损坏：与「没有」等价，重新 bootstrap 即可
    except OSError as exc:
        # 读不到 ≠ 不存在。当成「没有缓存」会让 status 印「缓存 ✗」，而真相是根本读不了
        # ——用户会照着去重新 bootstrap（那也会失败）。core 层只抛项目异常（errors.py 开篇）。
        raise HomeUnavailable(
            f"无法读取密钥缓存：{CACHE_FILE}",
            hint=f"{exc}；请检查 AGENT_WECHAT_HOME 指向的位置是否可用") from exc


def load_cached_hex(bare: str) -> str | None:
    entry = _load_cache().get("accounts", {}).get(bare)
    return entry.get("passphrase_hex") if entry else None


def _deprivilege(path: pathlib.Path) -> None:
    """sudo 环境下把文件/目录属主归还给真实用户，否则缓存普通用户读不了。"""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid and gid:
        os.chown(path, int(uid), int(gid))


def save_cached_hex(bare: str, passphrase_hex: str) -> None:
    cache = _load_cache()
    cache.setdefault("accounts", {})[bare] = {
        "passphrase_hex": passphrase_hex, "captured_at": int(time.time()),
    }
    paths.HOME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2))
    os.chmod(CACHE_FILE, 0o600)
    _deprivilege(CACHE_FILE)
    _deprivilege(paths.HOME_DIR)


# ---------- 派生与验证 ----------

def derive_key(passphrase: bytes, salt: bytes) -> bytes:
    """passphrase(32B) + 库 salt(16B) -> 该库专属 AES-256 密钥。"""
    return hashlib.pbkdf2_hmac("sha512", passphrase, salt, KDF_ITER, KEY_SZ)


def page1_hmac_ok(enc_key: bytes, page1: bytes, pgno: int = 1) -> bool:
    """用 enc_key 校验某库第 1 页的 HMAC。整个项目的真值判据。"""
    salt = page1[:SALT_SZ]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, FAST_KDF_ITER, KEY_SZ)
    hm = hmac_mod.new(mac_key, page1[SALT_SZ:PAGE_SZ - RESERVE + SALT_SZ], hashlib.sha512)
    hm.update(struct.pack("<I", pgno))
    return hmac_mod.compare_digest(hm.digest(), page1[PAGE_SZ - HMAC_SZ:])


def read_page1(db: pathlib.Path) -> bytes | None:
    """直接读库文件前 4096 字节。纯文件读，不经 sqlite，绝不触发 -shm 写入。"""
    try:
        with open(db, "rb") as f:
            return f.read(PAGE_SZ)
    except OSError:
        return None


def verify_passphrase(account: str, passphrase: bytes, *,
                      dbs: list[pathlib.Path] | None = None) -> list[tuple[str, bool]]:
    """对账号的库逐个校验 passphrase。返回 [(库名, 是否通过)]。"""
    targets = dbs if dbs is not None else paths.all_dbs(account)
    out = []
    for db in targets:
        p1 = read_page1(db)
        if not p1 or len(p1) < PAGE_SZ:
            continue
        ok = page1_hmac_ok(derive_key(passphrase, p1[:SALT_SZ]), p1)
        out.append((f"{db.parent.name}/{db.name}", ok))
    return out


def get_passphrase(account: str) -> bytes:
    """取账号的 passphrase（裸 wxid 键）。无缓存则抛 KeyNotFound 并给出指引。"""
    bare = paths.bare_wxid(account)
    hexstr = load_cached_hex(bare)
    if not hexstr:
        raise KeyNotFound(
            f"账号 {bare} 尚无 passphrase 缓存。请运行一次性引导：\n"
            f"  1. killall WeChat && sudo codesign --force --deep --sign - /Applications/WeChat.app\n"
            f"  2. 重启微信并登录 {bare}\n"
            f"  3. sudo uv run agent-wechat bootstrap   （期间在微信内退出登录再重新登录一次）")
    return bytes.fromhex(hexstr)


def get_passphrase_hex(account: str) -> str:
    """`get_passphrase` 的十六进制视图（EA-048：文案只留一份，不再各写各的错误消息）。"""
    return get_passphrase(account).hex()
