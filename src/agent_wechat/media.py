"""媒体层：图片 dat 解密、语音 SILK、视频定位（spec: 2026-09-14-media-decrypt-design）。

算法事实全部来自本机实测（spec §2）与 WeFlow/WeFlowPP 的格式研究（Python 独立实现，
未复制任何上游代码）。密钥纯磁盘推导：kvcomm 文件名 code + md5(code+裸wxid)[:16]（ASCII）
+ dat 尾部统计 xorKey；**推导后必须用样本验证，不验证不交付**。

红线：code/aesKey/xorKey 是推导材料，视同密钥——不进日志、错误消息、repr。
依赖方向：本模块不 import store/reader（reader 可以 import 本模块）。
"""
from __future__ import annotations

import datetime
import hashlib
import pathlib
import re
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import errors, messages, paths

# ---------- 图片容器 ----------

V1_SIG = b"\x07\x08\x56\x31\x08\x07"
V2_SIG = b"\x07\x08\x56\x32\x08\x07"

MAGICS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "jpg"), (b"\x89PNG", "png"), (b"GIF8", "gif"),
    (b"RIFF", "webp"), (b"wxgf", "wxgf"),
)


def detect_ext(buf: bytes) -> str | None:
    for sig, ext in MAGICS:
        if buf.startswith(sig):
            return ext
    return None


def _pkcs7_strip(plain: bytes) -> bytes:
    """严格 PKCS#7：pad 越界或尾字节不全等都是密钥不对——抛 DecryptError，绝不返回乱明文。"""
    if not plain:
        raise errors.DecryptError("AES 段解密结果为空")
    pad = plain[-1]
    if not (0 < pad <= 16) or plain[-pad:] != bytes([pad]) * pad:
        raise errors.DecryptError("PKCS#7 校验失败（图片密钥很可能不匹配）")
    return plain[:-pad]


def decrypt_dat(data: bytes, keys: MediaKeys) -> bytes:
    """dat 解密：V2 = 15B 头 + aligned(aesSize) AES-128-ECB 段 + 中段直通 + 末 xorSize XOR；
    V1 = 剥 6B 明文魔数后余下单字节 XOR；V0 = 全文件单字节 XOR。布局与恒等式见 spec §2.1。"""
    if data[:6] != V2_SIG:
        if data[:6] == V1_SIG:
            data = data[6:]        # V1 魔数明文存储：剥掉再 XOR，输出才是图片本体（乱前缀过不了 detect_ext）
        return bytes(b ^ keys.xor_key for b in data)
    if len(data) < 15:
        raise errors.DecryptError("dat 文件不足 15 字节头")
    aes_size = int.from_bytes(data[6:10], "little")
    xor_size = int.from_bytes(data[10:14], "little")
    aligned = aes_size + (16 - aes_size % 16)      # 已对齐也 +16（整块 PKCS#7）
    payload = data[15:]
    if xor_size < 0 or aligned > len(payload) or xor_size > len(payload) - aligned:
        raise errors.DecryptError("dat 长度字段与文件大小不符（截断或损坏）")
    dec = Cipher(algorithms.AES(keys.aes_key_ascii), modes.ECB()).decryptor()
    plain = _pkcs7_strip(dec.update(payload[:aligned]) + dec.finalize())
    remaining = payload[aligned:]
    raw_len = len(remaining) - xor_size
    tail = bytes(b ^ keys.xor_key for b in remaining[raw_len:])
    return plain + remaining[:raw_len] + tail


# ---------- 密钥推导 ----------

@dataclass(frozen=True, repr=False)
class MediaKeys:
    xor_key: int
    aes_key_ascii: bytes               # md5 hex 前 16 字符的 ASCII 字节（不是 hex 解码！）
    code: str

    def __repr__(self) -> str:         # 显式遮蔽：密钥与推导材料不进日志（全局红线）
        return "MediaKeys(***)"


_KVCOMM_SUBDIRS = ("app_data/net/kvcomm", "app_data/ilink/kvcomm")
_CODE_RE = re.compile(r"^key_(\d+)_")


def _kvcomm_codes() -> list[str]:
    """kvcomm 密钥码候选（Documents 与 Data 两级都试；去重保序）。"""
    codes: list[str] = []
    for base in (paths.CONTAINER, paths.CONTAINER.parent):
        for sub in _KVCOMM_SUBDIRS:
            d = base / sub
            if not d.is_dir():
                continue
            for f in d.iterdir():
                m = _CODE_RE.match(f.name)
                if m and m.group(1) not in codes:
                    codes.append(m.group(1))
    return codes


def _attach_dats(account: str):
    return (paths.XWECHAT_FILES / account / "msg/attach").glob("*/*/Img/*.dat")


def _derive_xor_key(account: str, limit: int = 200) -> int | None:
    """扫样本 dat 尾部：b[-2]^0xFF == b[-1]^0xD9（JPEG EOI）取众数；≥4 个一致才认可。"""
    counts: dict[int, int] = {}
    scanned = 0
    for p in _attach_dats(account):
        if scanned >= limit:
            break
        try:
            b = p.read_bytes()
        except OSError:
            continue
        if len(b) < 8:
            continue
        k = b[-2] ^ 0xFF
        if k == b[-1] ^ 0xD9:
            counts[k] = counts.get(k, 0) + 1
        scanned += 1
    if not counts:
        return None
    k, n = max(counts.items(), key=lambda kv: kv[1])
    return k if n >= 4 else None


def derive_keys(account: str) -> MediaKeys:
    """推导并**验证**图片密钥。无 code / 无样本 / 验证不过 → MediaKeyError（能力级降级）。"""
    codes = _kvcomm_codes()
    xor_key = _derive_xor_key(account)
    sample = next(iter(_attach_dats(account)), None)
    if sample is None:
        raise errors.MediaKeyError("msg/attach 下没有样本 dat，图片密钥无从验证")
    if xor_key is None:
        raise errors.MediaKeyError("样本 dat 尾部统计不出一致的 xorKey")
    bare = paths.bare_wxid(account)
    for code in codes:
        keys = MediaKeys(
            xor_key=xor_key,
            aes_key_ascii=hashlib.md5((code + bare).encode()).hexdigest()[:16].encode("ascii"),
            code=code)
        try:
            out = decrypt_dat(sample.read_bytes(), keys)
        except errors.DecryptError:
            continue
        if detect_ext(out):
            return keys
    raise errors.MediaKeyError(
        "kvcomm 密钥码与账号不匹配（或样本 dat 损坏）",
        hint="图片解密不可用；语音/视频不受影响。若微信刚更新过可重试，或检查 kvcomm 目录")
