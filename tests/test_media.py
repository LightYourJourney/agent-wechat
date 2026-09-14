"""media 核心的单元测试。全部夹具测试内构造（已知 key 加密假明文），无真实用户数据。"""
import hashlib

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from agent_wechat import errors, media


FAKE_KEY = b"0123456789abcdef"          # 16 ASCII 字节（aesKey 的形态，非真实密钥）
FAKE_XOR = 0x31


def build_v2_dat(head: bytes, tail: bytes, key_ascii: bytes, xor_key: int) -> bytes:
    """按 spec §2.1 构造 V2 dat。head 是 AES 段明文（须 16 的倍数）。"""
    pad_len = 16 - len(head) % 16
    padded = head + bytes([pad_len]) * pad_len
    enc = Cipher(algorithms.AES(key_ascii), modes.ECB()).encryptor()
    ct = enc.update(padded) + enc.finalize()
    aes_size = len(head)
    xor_size = len(tail)
    header = media.V2_SIG + aes_size.to_bytes(4, "little") + xor_size.to_bytes(4, "little") + b"\x01"
    return header + ct + bytes(b ^ xor_key for b in tail)


def jpeg_like(n: int) -> bytes:
    """FFD8 开头、FFD9 结尾、中间用递增字节填充的 n 字节假 JPEG。"""
    body = bytes((i * 7 + 3) % 256 for i in range(n - 4))
    return b"\xff\xd8\xff" + body[: n - 5] + b"\xff\xd9"


def test_detect_ext_magics():
    assert media.detect_ext(b"\xff\xd8\xffxx") == "jpg"
    assert media.detect_ext(b"\x89PNG\r\n") == "png"
    assert media.detect_ext(b"GIF89a") == "gif"
    assert media.detect_ext(b"RIFF####WEBP") == "webp"
    assert media.detect_ext(b"wxgf0000") == "wxgf"
    assert media.detect_ext(b"\x00\x00\x00\x01") is None


def test_decrypt_dat_v2_roundtrip():
    head = jpeg_like(1024)
    tail = b"\x11" * 300 + b"\xff\xd9"
    keys = media.MediaKeys(xor_key=FAKE_XOR, aes_key_ascii=FAKE_KEY, code="test")
    dat = build_v2_dat(head, tail, FAKE_KEY, FAKE_XOR)
    out = media.decrypt_dat(dat, keys)
    assert out == head + tail                     # 明文逐字节还原
    assert out[:3] == b"\xff\xd8\xff" and out[-2:] == b"\xff\xd9"


def test_decrypt_dat_v2_full_block_padding():
    """aesSize 已对齐（1024）时必须多解一个整块 pad 块；恒等式 size=15+aligned+xorSize。"""
    head = jpeg_like(1024)
    tail = b"\x22" * 100
    keys = media.MediaKeys(xor_key=FAKE_XOR, aes_key_ascii=FAKE_KEY, code="test")
    dat = build_v2_dat(head, tail, FAKE_KEY, FAKE_XOR)
    assert len(dat) == 15 + 1040 + len(tail)
    assert media.decrypt_dat(dat, keys) == head + tail


def test_decrypt_dat_v1_whole_file_xor():
    body = jpeg_like(200)
    keys = media.MediaKeys(xor_key=FAKE_XOR, aes_key_ascii=FAKE_KEY, code="test")
    dat = media.V1_SIG + bytes(b ^ FAKE_XOR for b in body)
    assert media.decrypt_dat(dat, keys) == body


def test_decrypt_dat_v0_no_sig_whole_file_xor():
    body = jpeg_like(100)
    keys = media.MediaKeys(xor_key=FAKE_XOR, aes_key_ascii=FAKE_KEY, code="test")
    assert media.decrypt_dat(bytes(b ^ FAKE_XOR for b in body), keys) == body


def test_decrypt_dat_wrong_key_raises_decrypt_error():
    """PKCS#7 严格校验：错钥必须抛 DecryptError，绝不返回乱明文。"""
    head = jpeg_like(1024)
    dat = build_v2_dat(head, b"x" * 10, FAKE_KEY, FAKE_XOR)
    bad = media.MediaKeys(xor_key=FAKE_XOR, aes_key_ascii=b"ffffffffffffffff", code="test")
    with pytest.raises(errors.DecryptError):
        media.decrypt_dat(dat, bad)


def test_decrypt_dat_truncated_raises():
    keys = media.MediaKeys(xor_key=FAKE_XOR, aes_key_ascii=FAKE_KEY, code="test")
    with pytest.raises(errors.DecryptError):
        media.decrypt_dat(media.V2_SIG + b"\x00" * 8, keys)   # 不足 15 字节头


def test_media_keys_repr_hides_secrets():
    keys = media.MediaKeys(xor_key=FAKE_XOR, aes_key_ascii=FAKE_KEY, code="42")
    assert repr(keys) == "MediaKeys(***)"     # 显式遮蔽（repr=False 会落回含内存地址的默认形式，断言碰运气）
