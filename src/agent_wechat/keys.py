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
        raw = json.loads(CACHE_FILE.read_text())
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
    # EA-049：**形状**归约——「损坏」的完整定义不止「JSON 解不开」：根不是对象、`accounts`
     # 不是对象、条目不是对象（合法 JSON 全都过得了 json.loads）此前会在下游 `.get()`
    # 处裸抛 AttributeError（穿透两个适配层）。一律按「损坏 = 空缓存」处理，与既有分支同义。
    accounts = raw.get("accounts") if isinstance(raw, dict) else None
    if not isinstance(accounts, dict):
        return {"accounts": {}}
    return {"accounts": {k: v for k, v in accounts.items() if isinstance(v, dict)}}


def load_cached_hex(bare: str) -> str | None:
    entry = _load_cache()["accounts"].get(bare)
    if not isinstance(entry, dict):
        return None
    hexstr = entry.get("passphrase_hex")
    return hexstr if isinstance(hexstr, str) else None


def _deprivilege(path: pathlib.Path) -> str | None:
    """sudo 环境下把文件/目录属主归还给真实用户，否则缓存普通用户读不了。

    EA-005：失败**不抛**——此刻缓存已经以 0600 落盘（跑引导的这个身份读得了），属主未
    归还只是「sudo 之外读不到」的降级；返回一句给用户看的警告，由引导流程转达。
    """
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if not (uid and gid):
        return None
    try:
        os.chown(path, int(uid), int(gid))
    except OSError as exc:
        return f"属主未归还（{path}）：{exc}——sudo 之外的用户可能读不到密钥缓存"
    return None


def save_cached_hex(bare: str, passphrase_hex: str) -> str | None:
    """写入密钥缓存。返回 None = 一切正常；否则是一句**给用户看的警告**（缓存已落盘）。

    EA-005：落盘的三个 IO 步骤此前全裸抛 OSError——core 只抛项目异常（errors.py 开篇
    那条契约），裸 OSError 会穿透两个适配层（CLI 一整个 traceback、MCP 落
    generic_payload）。文案沿用 `_load_cache` 同一句。
    """
    cache = _load_cache()
    cache.setdefault("accounts", {})[bare] = {
        "passphrase_hex": passphrase_hex, "captured_at": int(time.time()),
    }
    try:
        paths.HOME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache, indent=2))
        os.chmod(CACHE_FILE, 0o600)          # 与 mkdir 的 0700 同属**安全**步骤：失败即不可用
    except OSError as exc:
        raise HomeUnavailable(
            f"无法写入密钥缓存：{CACHE_FILE}",
            hint=f"{exc}；请检查 AGENT_WECHAT_HOME 指向的位置是否可用") from exc
    warns = [w for w in (_deprivilege(CACHE_FILE), _deprivilege(paths.HOME_DIR)) if w]
    return warns[0] if warns else None


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
                      dbs: list[pathlib.Path] | None = None
                      ) -> tuple[list[tuple[str, bool]], int]:
    """对账号的库逐个校验 passphrase。返回 ([(库名, 是否通过)], **读不到的库数**)。

    EA-051：读不到（`read_page1` → None：权限/完全磁盘访问权限/文件被截断）此前被静默
    `continue`——与「HMAC 不过」在结果里完全同形，于是引导在「一个库都没读到」时给出
    **错误诊断**（「重试捕获」而不是「读不到库」）。第二个返回值让调用方分开说。
    """
    targets = dbs if dbs is not None else paths.all_dbs(account)
    out: list[tuple[str, bool]] = []
    unreadable = 0
    for db in targets:
        p1 = read_page1(db)
        if not p1 or len(p1) < PAGE_SZ:
            unreadable += 1
            continue
        ok = page1_hmac_ok(derive_key(passphrase, p1[:SALT_SZ]), p1)
        out.append((f"{db.parent.name}/{db.name}", ok))
    return out, unreadable


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
    try:
        raw = bytes.fromhex(hexstr)
    except ValueError as exc:
        # EA-049：非法 hex（手工编辑、旧版本写过、写坏）时 `bytes.fromhex` 会裸抛
        # ValueError（穿透两个适配层）。与「没有缓存」分开说：重新 bootstrap 能修好它。
        raise KeyNotFound(
            f"账号 {bare} 的 passphrase 缓存损坏（不是合法 hex）。"
            "请重新运行一次性引导（bootstrap）修复") from exc
    if len(raw) != KEY_SZ:
        # EA-049：**长度**判据——`bytes.fromhex("ab")` 是合法的（1 字节），只靠 fromhex
        # 挡不住「短值」；而 passphrase 恒为 32 字节（KEY_SZ）。
        raise KeyNotFound(
            f"账号 {bare} 的 passphrase 缓存损坏（长度 {len(raw)} 字节，应为 {KEY_SZ}）。"
            "请重新运行一次性引导（bootstrap）修复")
    return raw


def get_passphrase_hex(account: str) -> str:
    """`get_passphrase` 的十六进制视图（EA-048：文案只留一份，不再各写各的错误消息）。"""
    return get_passphrase(account).hex()
