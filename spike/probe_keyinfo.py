#!/usr/bin/env python3
"""spike: 验证 key_info.db 中的 blob 能否直接派生出可用密钥。

假设空间（按成本排序验证）：
  H1 (raw key):  blob 的某个 32 字节窗口本身就是某库的 raw key
                 → mac_key=PBKDF2(key, salt^0x3A, 2轮) → HMAC(page1[16:4032]) 校验
  H2 (passphrase): blob 的某个 32 字节窗口（或其哈希/hex）是账号级 passphrase
                 → enc_key=PBKDF2(pass, salt, 256000轮) → 同上校验
  H3: 整个 blob / protobuf 子段 / 常量区等结构化切片

判据唯一：HMAC-SHA512 校验通过（来自 wcdb-key-tool 已验证实现）。
全部失败 = blob 是被包裹的密钥材料，纯文件解析路线到此为止 → 转 LLDB 路线。

只读微信数据：key_info.db / 目标库均先快照到临时目录，绝不在微信目录内产生写入。
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import itertools
import shutil
import sqlite3
import struct
import sys
import tempfile
import time
import pathlib

HOME = pathlib.Path.home()
DOC = HOME / "Library/Containers/com.tencent.xinWeChat/Data/Documents"
# 账号目录是占位符：跑之前换成 `agent-wechat status` 列出的你自己的账号目录名。
# 仓库内一律不放真实 wxid（见 CONTRIBUTING.md §4 的敏感数据自查）。
KDB = DOC / "xwechat_files/all_users/login/wxid_testacct01/key_info.db"
MAIN = DOC / "xwechat_files/wxid_testacct01_ab12/db_storage"

PAGE_SZ, SALT_SZ, RESERVE, HMAC_SZ, KEY_SZ = 4096, 16, 80, 64, 32
KDF_ITER = 256_000
FAST_KDF_ITER = 2

# 验证用的库集合：不同库 salt 不同，raw key 是按库独立的
TARGET_DBS = [
    MAIN / "session/session.db",
    MAIN / "contact/contact.db",
    MAIN / "general/general.db",
    MAIN / "message/message_0.db",
    MAIN / "message/message_4.db",
    MAIN / "message/media_0.db",
    MAIN / "chatbot/chatbot_message.db",
    MAIN / "favorite/favorite.db",
]


def snapshot(src: pathlib.Path, td: pathlib.Path) -> pathlib.Path:
    """db+wal+shm 三元组快照到私有目录，返回副本路径。"""
    dst = td / src.name
    for suf in ("", "-wal", "-shm"):
        s = pathlib.Path(str(src) + suf)
        if s.exists():
            shutil.copy2(s, pathlib.Path(str(dst) + suf))
    return dst


def load_blobs() -> list[tuple[int, str, bytes]]:
    """读 key_info.db 全部行（副本上以读写模式打开，让 WAL 回放，确保看到全部行）。"""
    td = pathlib.Path(tempfile.mkdtemp(prefix="spike-kdb-"))
    snap = snapshot(KDB, td)
    con = sqlite3.connect(snap)  # 副本，可写，WAL 正常回放
    rows = list(con.execute(
        "SELECT rowid, user_name_md5, key_info_data FROM LoginKeyInfoTable ORDER BY rowid"))
    con.close()
    shutil.rmtree(td, ignore_errors=True)
    return [(r[0], r[1], bytes(r[2])) for r in rows]


def mac_key_for(enc_key: bytes, mac_salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, FAST_KDF_ITER, KEY_SZ)


def _page_hmac_ok(enc_key: bytes, page1: bytes, pgno: int = 1) -> bool:
    """HMAC-SHA512 校验（公式与 wcdb-key-tool 逐字节一致）：
    mac_salt = salt ⊕ 0x3A；mac_key = PBKDF2-SHA512(enc_key, mac_salt, 2轮, 32B)；
    HMAC 输入 = page1[16:4032]（密文+IV）+ 页号（小端 uint32）。
    """
    salt = page1[:SALT_SZ]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mk = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, FAST_KDF_ITER, KEY_SZ)
    data = page1[SALT_SZ:PAGE_SZ - RESERVE + SALT_SZ]
    stored = page1[PAGE_SZ - HMAC_SZ:]
    hm = hmac_mod.new(mk, data, hashlib.sha512)
    hm.update(struct.pack("<I", pgno))
    return hmac_mod.compare_digest(hm.digest(), stored)


def check_raw_key(key: bytes, page1: bytes, salt: bytes, mac_salt: bytes) -> bool:
    """H1：key 直接作为 enc_key。mac_key 派生仅 2 轮，成本可忽略。"""
    return _page_hmac_ok(key, page1)


def check_passphrase(passphrase: bytes, page1: bytes, salt: bytes, mac_salt: bytes) -> bool:
    """H2：passphrase 经 256k 轮 PBKDF2 派生 enc_key。"""
    enc_key = hashlib.pbkdf2_hmac("sha512", passphrase, salt, KDF_ITER, KEY_SZ)
    return _page_hmac_ok(enc_key, page1)


def window_slices(blob: bytes, n: int = 32) -> list[bytes]:
    return [blob[i:i + n] for i in range(len(blob) - n + 1)]


def main() -> int:
    t0 = time.time()
    blobs = load_blobs()
    users = {u for _, u, _ in blobs}
    print(f"key_info.db: {len(blobs)} 行, user_name_md5 种类: {len(users)}")

    # 预载各目标库 page1 与 salt（快照只读）
    td = pathlib.Path(tempfile.mkdtemp(prefix="spike-dbs-"))
    pages: list[tuple[str, bytes, bytes, bytes]] = []
    for db in TARGET_DBS:
        if not db.exists():
            continue
        snap = snapshot(db, td)
        p1 = snap.read_bytes()[:PAGE_SZ]
        pages.append((db.parent.name + "/" + db.name, p1, p1[:SALT_SZ],
                      bytes(b ^ 0x3A for b in p1[:SALT_SZ])))
    print(f"验证目标: {[name for name, *_ in pages]}\n")

    # ---- H1: raw key —— 全部 32 字节窗口 × 全部库 ----
    tried = hits = 0
    for rowid, _user, blob in blobs:
        for cand in window_slices(blob):
            for name, p1, salt, mac_salt in pages:
                tried += 1
                if check_raw_key(cand, p1, salt, mac_salt):
                    print(f"✅ H1 raw key 命中: row={rowid} offset={blob.find(cand)} db={name}")
                    hits += 1
    print(f"H1 raw key: {tried} 次尝试, {hits} 命中  [{time.time()-t0:.1f}s]")

    # ---- H2/H3: passphrase —— 结构化候选（限单库 session.db 控制时长）----
    name0, p10, salt0, ms0 = pages[0]
    cands: dict[str, bytes] = {}
    for rowid, _user, blob in blobs:
        for off in (0, 3, 16, 17, 19, 31, 35, 36, 51, 67, 71, 72, 87, 103, 119, 135, 138, 148):
            cands[f"row{rowid}@{off}"] = blob[off:off + 32]
        cands[f"row{rowid}@67:171"] = blob[67:171]
        cands[f"row{rowid}@17:31"] = blob[17:31]
        cands[f"row{rowid}:sha256[67:171]"] = hashlib.sha256(blob[67:171]).digest()
        cands[f"row{rowid}:sha256(blob)"] = hashlib.sha256(blob).digest()
        cands[f"row{rowid}:md5hex[67:171]"] = hashlib.md5(blob[67:171]).hexdigest().encode()
        cands[f"row{rowid}:hex[67:99]"] = blob[67:99].hex().encode()
        cands[f"row{rowid}:whole"] = blob
    # 当前登录用的 key_info.dat 本体
    dat = (DOC / "app_data/login/wxid_testacct01/key_info.dat").read_bytes()
    cands["dat@67:99"], cands["dat:whole"] = dat[67:99], dat

    hits2 = 0
    for label, cand in cands.items():
        if len(cand) < 8:
            continue
        try:
            if check_passphrase(cand, p10, salt0, ms0):
                print(f"✅ H2 passphrase 命中: {label}")
                hits2 += 1
        except Exception:
            pass
    print(f"H2 passphrase: {len(cands)} 候选 × 1 库, {hits2} 命中  [{time.time()-t0:.1f}s]")

    shutil.rmtree(td, ignore_errors=True)
    if hits == 0 and hits2 == 0:
        print("\n❌ 全部未命中 — key_info blob 是被包裹的密钥材料，"
              "纯文件解析路线无更多低成本假设。结论：转 LLDB 路线（bootstrap）。")
        return 1
    print(f"\n总耗时 {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
