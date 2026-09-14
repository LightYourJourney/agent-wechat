"""media 核心的单元测试。全部夹具测试内构造（已知 key 加密假明文），无真实用户数据。"""
import hashlib
import pathlib

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from agent_wechat import errors, media, paths


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


# ---------- 定位与导出 ----------

def make_tree(tmp_path, monkeypatch):
    """合成微信树：返回 (root, account)。模式与 test_reader_export_pipeline.env 一致。

    同时隔离密钥缓存（keys.CACHE_FILE）并写入假 passphrase——T3 的语音导出会经
    get_passphrase 取钥，不隔离就会读到**真实机器的** passphrase.json。
    """
    import json as _json
    import uuid
    from agent_wechat import keys as _keys
    root = tmp_path / f"wx-{uuid.uuid4().hex[:6]}"
    account = "wxid_testacct01_ab12"
    docs = root / "Documents"
    (docs / "app_data/net/kvcomm").mkdir(parents=True)
    (docs / "xwechat_files" / account / "msg/attach").mkdir(parents=True)
    (docs / "xwechat_files" / account / "msg/video").mkdir(parents=True)
    (docs / "xwechat_files" / account / "db_storage/message").mkdir(parents=True)
    home = root / "home"
    home.mkdir()
    (home / "passphrase.json").write_text(_json.dumps(
        {"accounts": {"wxid_testacct01": {"passphrase_hex": "aa" * 32, "captured_at": 0}}}))
    monkeypatch.setattr(paths, "CONTAINER", docs)
    monkeypatch.setattr(paths, "XWECHAT_FILES", docs / "xwechat_files")
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(_keys, "CACHE_FILE", home / "passphrase.json")
    return root, account


def test_month_of_uses_local_timezone(monkeypatch):
    # 1700000000 = 2023-11-15 06:13 +08:00 / 2023-11-14 22:13 UTC——断言只查格式不查具体月
    m = media.month_of(1700000000)
    assert len(m) == 7 and m[4] == "-" and m.replace("-", "").isdigit()


def test_resolve_image_month_and_suffix_priority(tmp_path, monkeypatch):
    root, account = make_tree(tmp_path, monkeypatch)
    cm = "a" * 32
    img = paths.XWECHAT_FILES / account / "msg/attach" / cm / "2026-09" / "Img"
    img.mkdir(parents=True)
    (img / "beef_hd.dat").write_bytes(b"x")     # _hd 优先于 _h 与裸名
    (img / "beef_h.dat").write_bytes(b"x")
    (img / "beef.dat").write_bytes(b"x")
    ts = 1789000000                              # 2026-09 内的某刻
    got = media.resolve_image(account, cm, ts, "beef")
    assert got is not None and got.name == "beef_hd.dat"


def test_resolve_image_adjacent_month_fallback(tmp_path, monkeypatch):
    root, account = make_tree(tmp_path, monkeypatch)
    cm = "a" * 32
    img = paths.XWECHAT_FILES / account / "msg/attach" / cm / "2026-08" / "Img"
    img.mkdir(parents=True)
    (img / "cafe.dat").write_bytes(b"x")
    ts = 1789000000                              # 2026-09——目录在相邻的 2026-08，靠 -1 月回退命中
    assert media.resolve_image(account, cm, ts, "cafe") is not None


def test_resolve_video_mp4_then_thumb(tmp_path, monkeypatch):
    root, account = make_tree(tmp_path, monkeypatch)
    vdir = paths.XWECHAT_FILES / account / "msg/video" / "2026-09"
    vdir.mkdir(parents=True)
    (vdir / "abcd.mp4").write_bytes(b"x")
    (vdir / "ef01_thumb.jpg").write_bytes(b"x")
    ts = 1789000000
    assert media.resolve_video(account, ts, "abcd") == (vdir / "abcd.mp4", False)
    assert media.resolve_video(account, ts, "ef01") == (vdir / "ef01_thumb.jpg", True)
    assert media.resolve_video(account, ts, "nope") is None


def test_export_media_images_and_videos(tmp_path, monkeypatch):
    import hashlib
    root, account = make_tree(tmp_path, monkeypatch)
    # kvcomm 写一个 code；attach 放一张可解密的图 + 一个 wxgf；video 放 mp4 + 孤 thumb
    code = "999999"
    (paths.CONTAINER / "app_data/net/kvcomm/key_999999_1_2_3_4_5_input.statistic").write_bytes(b"")
    cm = hashlib.md5("chatA@chatroom".encode()).hexdigest()
    img = paths.XWECHAT_FILES / account / "msg/attach" / cm / "2026-09" / "Img"
    img.mkdir(parents=True)
    # dat 必须用 derive_keys 会推出来的那把 aesKey 造：md5(code + 裸wxid) 前 16 字符的 ASCII
    # （spec §2.2）。用 FAKE_KEY 造的话样本验证就过不了，整条导出会退化成「图片解密不可用」。
    aes = hashlib.md5((code + paths.bare_wxid(account)).encode()).hexdigest()[:16].encode()
    # xorKey 要 ≥4 个尾部一致的样本才认可（spec §2.2「≥4 个文件一致」）：除 beef 外补 3 个，
    # 放在不导出的月份里（months 过滤把它们挡在导出之外，只参与推导计数）。
    sdir = paths.XWECHAT_FILES / account / "msg/attach" / cm / "2026-03" / "Img"
    sdir.mkdir(parents=True)
    for i in range(3):
        (sdir / f"sam{i}.dat").write_bytes(
            build_v2_dat(jpeg_like(64), b"\x33" * 8 + b"\xff\xd9", aes, FAKE_XOR))
    (img / "beef.dat").write_bytes(build_v2_dat(jpeg_like(1024), b"\x11" * 50 + b"\xff\xd9", aes, FAKE_XOR))
    # wxgf 是**解密后**的容器（"无 ffmpeg 时存原容器"）：dat 本身按 V0 全文件 XOR 存
    # （spec §2.1）——直接写明文容器的话解密出来是乱码，导出会记 failed 而不是 .wxgf。
    wxgf = b"wxgf" + b"\x00\x00\x00\x01" + b"\x00" * 16
    (img / "c0de.dat").write_bytes(bytes(b ^ FAKE_XOR for b in wxgf))
    (paths.XWECHAT_FILES / account / "msg/video/2026-09").mkdir(parents=True, exist_ok=True)
    vdir = paths.XWECHAT_FILES / account / "msg/video/2026-09"
    (vdir / "abcd.mp4").write_bytes(b"m" * 32)
    (vdir / "ef01_thumb.jpg").write_bytes(b"t" * 16)

    out = root / "home" / "media"
    rep = media.export_media(account, ["chatA@chatroom"], kinds=["image", "video"],
                             months=["2026-09"], out_dir=out)
    assert rep.image.ok == 2 and rep.image.failed == 0
    assert rep.video.ok == 1 and rep.video.thumb == 1
    saved = list((out / cm / "image" / "2026-09").iterdir())
    assert {p.suffix for p in saved} == {".jpg", ".wxgf"}     # 解出 jpg；wxgf 原容器
    assert any(p.suffix == ".jpg" for p in (out / cm / "video" / "2026-09").iterdir())
    assert (out / cm / "video" / "2026-09" / "abcd.mp4").read_bytes() == b"m" * 32


def test_export_media_key_error_degrades_not_raises(tmp_path, monkeypatch):
    """kvcomm 空 → MediaKeyError 在 export_media 内部降级：图片 0，voice/video 照常。"""
    root, account = make_tree(tmp_path, monkeypatch)   # 没有 kvcomm code
    out = root / "home" / "media"
    rep = media.export_media(account, ["c@chatroom"], kinds=["image", "video"],
                             months=["2026-09"], out_dir=out)
    assert rep.image.ok == 0 and rep.image.failed == 0
    assert rep.degraded and "图片" in rep.degraded[0]


def test_export_media_output_dir_is_0700(tmp_path, monkeypatch):
    import os, stat as st
    root, account = make_tree(tmp_path, monkeypatch)
    (paths.CONTAINER / "app_data/net/kvcomm/key_1_a.statistic").write_bytes(b"")
    out = root / "home" / "media"
    media.export_media(account, ["c@chatroom"], kinds=["video"], out_dir=out)
    assert st.S_IMODE(os.stat(out).st_mode) == 0o700


def test_export_media_max_files_truncates(tmp_path, monkeypatch):
    root, account = make_tree(tmp_path, monkeypatch)
    (paths.CONTAINER / "app_data/net/kvcomm/key_1_a.statistic").write_bytes(b"")
    vdir = paths.XWECHAT_FILES / account / "msg/video/2026-09"
    vdir.mkdir(parents=True)
    for i in range(5):
        (vdir / f"{i:04d}{'0' * 28}.mp4").write_bytes(b"m")
    rep = media.export_media(account, ["c@chatroom"], kinds=["video"],
                             months=["2026-09"], out_dir=root / "home" / "media", max_files=2)
    assert rep.video.ok == 2 and rep.truncated is True


def test_report_to_dict_envelope_shape():
    rep = media.MediaReport(image=media.Counts(ok=3, failed=1), voice=media.Counts(raw_only=2),
                            video=media.Counts(ok=1, thumb=4), degraded=["x"])
    d = rep.to_dict()
    assert d["image"] == {"ok": 3, "failed": 1, "missing": 0}
    assert d["voice"] == {"ok": 0, "failed": 0, "raw_only": 2}
    assert d["video"] == {"ok": 1, "thumb": 4, "missing": 0}
    assert d["truncated"] is False


# ---------- Ruling 6：OSError 不得逃出 derive_keys ----------

def test_kvcomm_codes_skips_unreadable_dir(tmp_path, monkeypatch):
    """Ruling 6：kvcomm 目录读不了（权限/坏链）是**能力级**问题——跳过它，另一个目录
    照样给 code；裸 OSError 逃出 derive_keys 会击穿「core 只抛项目异常」的契约。"""
    root, account = make_tree(tmp_path, monkeypatch)
    alt = paths.CONTAINER / "app_data/ilink/kvcomm"      # 第二个 kvcomm 目录，可读
    alt.mkdir(parents=True)
    (alt / "key_777_x.statistic").write_bytes(b"")
    broken = paths.CONTAINER / "app_data/net/kvcomm"
    real_iterdir = pathlib.Path.iterdir

    def fake(self):
        if self == broken:
            raise PermissionError(13, "Operation not permitted", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(pathlib.Path, "iterdir", fake)
    assert media._kvcomm_codes() == ["777"]


def test_unreadable_sample_raises_media_key_error(tmp_path, monkeypatch):
    """Ruling 6：唯一的验证样本读不出来 → MediaKeyError（带 hint），而不是裸 OSError。

    样本顺序由 `_attach_dats` 的 glob 决定，这里把它换成「第一个是读不了的样本、
    其余 4 个可读」——xorKey 统计照常成立，从而精确打到 derive_keys 里那次
    `sample.read_bytes()`。目录冒充 .dat 是**真** OSError（IsADirectoryError），
    不依赖 chmod 在 root 下的行为。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    (paths.CONTAINER / "app_data/net/kvcomm/key_5_x.statistic").write_bytes(b"")
    img = paths.XWECHAT_FILES / account / "msg/attach" / ("a" * 32) / "2026-09" / "Img"
    img.mkdir(parents=True)
    bad = img / "bad.dat"
    bad.mkdir()
    good = [img / f"g{i}.dat" for i in range(4)]
    for p in good:
        p.write_bytes(build_v2_dat(jpeg_like(64), b"\x44" * 8 + b"\xff\xd9", FAKE_KEY, FAKE_XOR))
    monkeypatch.setattr(media, "_attach_dats", lambda acct: iter([bad, *good]))
    with pytest.raises(errors.MediaKeyError) as ei:
        media.derive_keys(account)
    assert ei.value.hint
