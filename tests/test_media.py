"""media 核心的单元测试。全部夹具测试内构造（已知 key 加密假明文），无真实用户数据。"""
import datetime
import hashlib
import os
import pathlib
import stat

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
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())   # 月由 ts 现算（Ruling 13.1）
    img = paths.XWECHAT_FILES / account / "msg/attach" / cm / media.month_of(ts) / "Img"
    img.mkdir(parents=True)
    (img / "beef_hd.dat").write_bytes(b"x")     # _hd 优先于 _h 与裸名
    (img / "beef_h.dat").write_bytes(b"x")
    (img / "beef.dat").write_bytes(b"x")
    got = media.resolve_image(account, cm, ts, "beef")
    assert got is not None and got.name == "beef_hd.dat"


def test_resolve_image_adjacent_month_fallback(tmp_path, monkeypatch):
    """目录落在**上一个月**：月份由 ts 用 datetime 推算，不手写常量对（会随 ts 漂移）。"""
    root, account = make_tree(tmp_path, monkeypatch)
    cm = "a" * 32
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    prev = (datetime.datetime.fromtimestamp(ts).replace(day=1)
            - datetime.timedelta(days=1)).strftime("%Y-%m")
    img = paths.XWECHAT_FILES / account / "msg/attach" / cm / prev / "Img"
    img.mkdir(parents=True)
    (img / "cafe.dat").write_bytes(b"x")
    assert media.resolve_image(account, cm, ts, "cafe") is not None


def test_resolve_video_mp4_then_thumb(tmp_path, monkeypatch):
    root, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    vdir = paths.XWECHAT_FILES / account / "msg/video" / media.month_of(ts)
    vdir.mkdir(parents=True)
    (vdir / "abcd.mp4").write_bytes(b"x")
    (vdir / "ef01_thumb.jpg").write_bytes(b"x")
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
    # wxgf 是**解密后**的容器（"无 ffmpeg 时存原容器"）：夹具按磁盘真实形态包成 V2 容器
    # （Ruling 13.2；spec §2.1：本机 200 样本全为 V2，V0 是 3.x 遗留形态；§2.3：其中 23 个
    # wxgf 解密成功）。
    # 不是 V0——老注释把「V0 全文件 XOR」当成磁盘形态，与 §2.1/§2.3 矛盾。
    wxgf = b"wxgf" + b"\x00\x00\x00\x01" + b"\x00" * 16
    (img / "c0de.dat").write_bytes(build_v2_dat(wxgf, b"\x22" * 20, aes, FAKE_XOR))
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


# ---------- Ruling 8：视频是账号级媒体 ----------

def test_export_media_multi_chat_skips_video(tmp_path, monkeypatch):
    """Ruling 8：`msg/video/{月}` 没有会话维度——多会话调用无从归属，跳过 + degraded。

    按会话循环会把同一批文件重复写进 N 个目录、`video.ok`/`produced`（进而 max_files 口径）
    翻 N 倍；单会话路径由 test_export_media_images_and_videos 的 ok==1/thumb==1 守住。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    vdir = paths.XWECHAT_FILES / account / "msg/video/2026-09"
    vdir.mkdir(parents=True)
    (vdir / "abcd.mp4").write_bytes(b"m" * 8)
    (vdir / "ef01_thumb.jpg").write_bytes(b"t" * 8)
    out = root / "home" / "media"
    rep = media.export_media(account, ["chatA@chatroom", "chatB@chatroom"], kinds=["video"],
                             months=["2026-09"], out_dir=out)
    assert rep.video.ok == 0 and rep.video.thumb == 0
    assert "视频为账号级枚举，无法归属具体会话；请逐个会话导出" in rep.degraded
    assert list(out.iterdir()) == []              # 一个会话目录都不建（更别说两份视频）


# ---------- Ruling 9：export_media 内的 OSError 归口 ----------

def test_export_media_image_read_error_counts_failed(tmp_path, monkeypatch):
    """Ruling 9：dat 在但读不了（权限/被微信重写）→ image.failed，不逃出 core 层。

    `read_bytes` 用 monkeypatch 定点抛 OSError（不用 chmod，跨环境稳定）；`_attach_dats`
    换成 4 个可读样本，免得 `derive_keys` 的 `next(iter(...))` 命中坏文件而整段能力降级。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    code = "999999"
    (paths.CONTAINER / "app_data/net/kvcomm/key_999999_1_2_3_4_5_input.statistic").write_bytes(b"")
    cm = hashlib.md5("chatA@chatroom".encode()).hexdigest()
    aes = hashlib.md5((code + paths.bare_wxid(account)).encode()).hexdigest()[:16].encode()
    sdir = paths.XWECHAT_FILES / account / "msg/attach" / cm / "2026-03" / "Img"
    sdir.mkdir(parents=True)
    samples = [sdir / f"sam{i}.dat" for i in range(4)]
    for p in samples:
        p.write_bytes(build_v2_dat(jpeg_like(64), b"\x33" * 8 + b"\xff\xd9", aes, FAKE_XOR))
    monkeypatch.setattr(media, "_attach_dats", lambda acct: iter(samples))
    img = paths.XWECHAT_FILES / account / "msg/attach" / cm / "2026-09" / "Img"
    img.mkdir(parents=True)
    (img / "beef.dat").write_bytes(
        build_v2_dat(jpeg_like(1024), b"\x11" * 50 + b"\xff\xd9", aes, FAKE_XOR))
    bad = img / "dead.dat"
    # 载荷用**正确密钥**的真 V2 容器（Ruling 13.3）：能读出来就会被正常导出。于是 failed==1
    # 只能来自那次读失败——若 monkeypatch 未生效会变成 ok==2，断言自足。（用错钥构造的话，
    # monkeypatch 失效时照样计 failed，断言的失败原因就分不出来了。）
    bad.write_bytes(build_v2_dat(jpeg_like(1024), b"\x33" * 50 + b"\xff\xd9", aes, FAKE_XOR))
    real_read = pathlib.Path.read_bytes

    def fake(self):
        if self == bad:
            raise PermissionError(13, "Operation not permitted", str(self))
        return real_read(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", fake)
    rep = media.export_media(account, ["chatA@chatroom"], kinds=["image"],
                             months=["2026-09"], out_dir=root / "home" / "media")
    assert rep.image.ok == 1 and rep.image.failed == 1 and rep.degraded == []


def test_export_media_video_read_error_counts_missing(tmp_path, monkeypatch):
    """Ruling 9：视频本体/缩略图在但读不了 → video.missing（拿不到本体），不逃出 core 层。"""
    root, account = make_tree(tmp_path, monkeypatch)
    vdir = paths.XWECHAT_FILES / account / "msg/video/2026-09"
    vdir.mkdir(parents=True)
    (vdir / "aaaa.mp4").write_bytes(b"m" * 8)
    (vdir / "cccc_thumb.jpg").write_bytes(b"t" * 8)
    bad_mp4, bad_thumb = vdir / "bbbb.mp4", vdir / "dddd_thumb.jpg"
    bad_mp4.write_bytes(b"m" * 8)
    bad_thumb.write_bytes(b"t" * 8)              # 两个读不了的文件：本体与缩略图各占一处
    real_read = pathlib.Path.read_bytes

    def fake(self):
        if self in (bad_mp4, bad_thumb):
            raise PermissionError(13, "Operation not permitted", str(self))
        return real_read(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", fake)
    rep = media.export_media(account, ["c@chatroom"], kinds=["video"],
                             months=["2026-09"], out_dir=root / "home" / "media")
    assert rep.video.ok == 1 and rep.video.thumb == 1 and rep.video.missing == 2


# ---------- Ruling 11：0700 覆盖输出树全链路 ----------

def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def _video_fixture(account, ts: int) -> str:
    """在 ts 所属月放一个 mp4，返回该月（月份由 ts 现算，不手写常量）。"""
    mon = media.month_of(ts)
    vdir = paths.XWECHAT_FILES / account / "msg/video" / mon
    vdir.mkdir(parents=True)
    (vdir / "abcd.mp4").write_bytes(b"m" * 8)
    return mon


def test_export_media_tree_is_0700_all_the_way(tmp_path, monkeypatch):
    """Ruling 11：默认 out_root 下，输出树**每一级**都是 0700，落盘文件 0600。

    只断言 out_root 会给出假保证：`mkdir(mode=0o700, parents=True)` 只给最后一级套 mode，
    中间两级（{chat_md5}/{kind}）按 umask 落 0755，文件按 umask 落 0644。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    mon = _video_fixture(account, int(datetime.datetime(2026, 9, 15, 12, 0).timestamp()))
    media.export_media(account, ["c@chatroom"], kinds=["video"], months=[mon])   # 默认 out_root
    out = paths.HOME_DIR / "media"
    cm = hashlib.md5("c@chatroom".encode()).hexdigest()
    for p in (out, out / cm, out / cm / "video", out / cm / "video" / mon):
        assert _mode(p) == 0o700, p
    assert _mode(out / cm / "video" / mon / "abcd.mp4") == 0o600


def test_export_media_tightens_existing_out_root(tmp_path, monkeypatch):
    """Ruling 11：`--out` 指向**已存在**目录（spec §6 允许）时同样收紧到 0700。

    审阅者点名的暴露场景：`mkdir(exist_ok=True)` 对已存在目录不动权限——改前整棵树没有
    任何一级是 0700，明文视频世界可读。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    out = root / "preexisting"
    out.mkdir()
    os.chmod(out, 0o755)                        # 用户既有目录：默认权限
    (out / "keep.txt").write_text("x")
    mon = _video_fixture(account, int(datetime.datetime(2026, 9, 15, 12, 0).timestamp()))
    media.export_media(account, ["c@chatroom"], kinds=["video"], months=[mon], out_dir=out)
    cm = hashlib.md5("c@chatroom".encode()).hexdigest()
    for p in (out, out / cm, out / cm / "video", out / cm / "video" / mon):
        assert _mode(p) == 0o700, p
    assert _mode(out / cm / "video" / mon / "abcd.mp4") == 0o600
    assert (out / "keep.txt").read_text() == "x"    # 只收紧权限，不动既有文件


# ---------- Ruling 12：目录枚举的 OSError 也要归口 ----------

def test_export_media_unreadable_dir_is_skipped(tmp_path, monkeypatch):
    """Ruling 12：目录枚举抛 OSError（权限/竞态删目录）不逃出 core 层——该目录跳过，
    其余照常，且**不**误记 degraded（与 Ruling 6 对 kvcomm 同一规矩）。

    四处枚举（`base.iterdir` / `img_dir.glob` / `vbase.iterdir` / `month_dir.glob`）都走
    同一个 `_safe_iter`，故这里聚焦打到最外层那处：会话的图片目录整个读不了。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    code = "999999"
    (paths.CONTAINER / "app_data/net/kvcomm/key_999999_1_2_3_4_5_input.statistic").write_bytes(b"")
    cm = hashlib.md5("chatA@chatroom".encode()).hexdigest()
    aes = hashlib.md5((code + paths.bare_wxid(account)).encode()).hexdigest()[:16].encode()
    sdir = paths.XWECHAT_FILES / account / "msg/attach" / cm / "2026-03" / "Img"
    sdir.mkdir(parents=True)
    samples = [sdir / f"sam{i}.dat" for i in range(4)]
    for p in samples:
        p.write_bytes(build_v2_dat(jpeg_like(64), b"\x33" * 8 + b"\xff\xd9", aes, FAKE_XOR))
    monkeypatch.setattr(media, "_attach_dats", lambda acct: iter(samples))
    mon = _video_fixture(account, int(datetime.datetime(2026, 9, 15, 12, 0).timestamp()))
    blocked = paths.XWECHAT_FILES / account / "msg/attach" / cm
    real_iterdir = pathlib.Path.iterdir

    def fake(self):
        if self == blocked:
            raise PermissionError(13, "Operation not permitted", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(pathlib.Path, "iterdir", fake)
    rep = media.export_media(account, ["chatA@chatroom"], kinds=["image", "video"],
                             months=[mon], out_dir=root / "home" / "media")
    assert rep.image.ok == 0 and rep.image.failed == 0    # 该会话没东西可导 ≠ 导出失败
    assert rep.video.ok == 1                              # 视频照常导出
    assert rep.degraded == []                             # 一个目录读不了不是能力级降级


# ---------- Task 3：语音导出（pilk SILK→WAV，.slk 回退） ----------

def _silk_blob(seconds: float = 0.2, rate: int = 24000) -> bytes:
    """用 pilk 自身 encode 造一段真 SILK（`\\x02` 前缀，与微信 VoiceInfo.voice_data 同形）。"""
    import tempfile
    import pilk
    pcm = b"\x00\x01" * int(rate * seconds)
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td)
        (p / "a.pcm").write_bytes(pcm)
        pilk.encode(str(p / "a.pcm"), str(p / "a.silk"), rate, tencent=True)
        return (p / "a.silk").read_bytes()


def _voice_fixture(account: str, rows: list[tuple[int, int, int, bytes]]) -> None:
    """合成**明文** media_0.db；rows = [(chat_name_id, create_time, local_id, voice_data)]。

    Name2Id 只放一行 'chatA@chatroom'（rowid=1）；明文库是因为测试把 decrypt.decrypted
    换成了直通假上下文——真解密管线（sqlcipher）不在本测试的范围内。
    """
    import sqlite3
    db = paths.XWECHAT_FILES / account / "db_storage/message/media_0.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE Name2Id (user_name TEXT);"
        "CREATE TABLE VoiceInfo (chat_name_id INTEGER, create_time INTEGER,"
        " local_id INTEGER, svr_id INTEGER, voice_data BLOB, data_index TEXT);"
        "INSERT INTO Name2Id (user_name) VALUES ('chatA@chatroom');")
    con.executemany("INSERT INTO VoiceInfo VALUES (?, ?, ?, 0, ?, 'x')", rows)
    con.commit()
    con.close()


def _passthrough_decrypted(monkeypatch) -> None:
    """把 `decrypt.decrypted` 换成「直接 yield 原库」的假上下文（合成库是明文）。

    第二条 setattr 要求 `media.decrypt` 是**模块级**属性（Ruling 17）——函数内 import
    的话它在调用前不存在，这里会直接 AttributeError。
    """
    import contextlib
    from agent_wechat import decrypt as dec_mod

    @contextlib.contextmanager
    def fake_decrypted(db_path, passphrase, tmpdir=None):
        yield db_path
    monkeypatch.setattr(dec_mod, "decrypted", fake_decrypted)
    monkeypatch.setattr("agent_wechat.media.decrypt", dec_mod)


def test_export_media_voice_silk_to_wav(tmp_path, monkeypatch):
    """VoiceInfo 命中 → pilk 解码 24kHz WAV；目录 0700、文件 0600。

    夹具不用 kvcomm code：语音走 VoiceInfo，不依赖图片密钥——没有 code 也必须能导出。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())   # 月由 ts 现算
    mon = media.month_of(ts)
    _voice_fixture(account, [(1, ts, 7, _silk_blob())])
    _passthrough_decrypted(monkeypatch)

    out = root / "home" / "media"
    rep = media.export_media(account, ["chatA@chatroom"], kinds=["voice"],
                             months=[mon], out_dir=out)
    assert rep.voice.ok == 1 and rep.voice.raw_only == 0
    cm = hashlib.md5("chatA@chatroom".encode()).hexdigest()
    wavs = list((out / cm / "voice" / mon).iterdir())
    assert len(wavs) == 1 and wavs[0].name == f"{ts}_7.wav"
    assert wavs[0].read_bytes()[:4] == b"RIFF"
    assert _mode(wavs[0]) == 0o600                    # pilk 落盘权限跟 umask，导出侧收口
    for p in (out, out / cm, out / cm / "voice", out / cm / "voice" / mon):
        assert _mode(p) == 0o700, p


def test_export_media_voice_month_filter(tmp_path, monkeypatch):
    """months 过滤按 create_time 的**本地月**：另一个月那条不导出。

    上个月由 ts 用 datetime 现算（曾经手写的常量对是 2025-08，注释却写 2026-08）。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    mon = media.month_of(ts)
    prev = datetime.datetime.fromtimestamp(ts).replace(day=1) - datetime.timedelta(days=1)
    ts_prev = int(prev.timestamp())
    assert media.month_of(ts_prev) != mon             # 夹具自证：两条确实落在不同月
    _voice_fixture(account, [(1, ts, 7, _silk_blob()), (1, ts_prev, 8, _silk_blob())])
    _passthrough_decrypted(monkeypatch)

    rep = media.export_media(account, ["chatA@chatroom"], kinds=["voice"],
                             months=[mon], out_dir=root / "home" / "media")
    assert rep.voice.ok == 1                          # 上个月那条被月份过滤掉
    cm = hashlib.md5("chatA@chatroom".encode()).hexdigest()
    got = {p.name for p in (root / "home" / "media" / cm / "voice" / mon).iterdir()}
    assert got == {f"{ts}_7.wav"}


def test_export_media_voice_slk_fallback(tmp_path, monkeypatch):
    """pilk 不可用 → 原始 .slk（字节原样）+ raw_only 计数 + degraded，绝不整批失败。"""
    root, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    mon = media.month_of(ts)
    blob = b"\x02#!SILK_V3" + b"\x00" * 32
    _voice_fixture(account, [(1, ts, 7, blob)])
    _passthrough_decrypted(monkeypatch)
    monkeypatch.setattr(media, "_PILK", None)

    out = root / "home" / "media"
    rep = media.export_media(account, ["chatA@chatroom"], kinds=["voice"],
                             months=[mon], out_dir=out)
    assert rep.voice.raw_only == 1 and rep.voice.ok == 0
    assert len(rep.degraded) == 1                     # 一次导出一条，不按文件刷屏
    assert any("slk" in d or "语音" in d for d in rep.degraded)
    cm = hashlib.md5("chatA@chatroom".encode()).hexdigest()
    slks = list((out / cm / "voice" / mon).iterdir())
    assert len(slks) == 1 and slks[0].suffix == ".slk"
    assert slks[0].read_bytes()[:10] == blob[:10]     # 原始形态（\x02 前缀保留）
    assert _mode(slks[0]) == 0o600


# ---------- Ruling 15：chmod 尽力而为 + out_root 一次 stat 检查 ----------

def test_ensure_tree_chmod_failure_is_best_effort(tmp_path, monkeypatch):
    """Ruling 15：chmod 抛 OSError（`--out` 指向不归自己的既有目录）不得逃出 core。

    mkdir 对已存在目录是 no-op，能被拒的只有 chmod——权限没收紧只该降级提示，
    不该让整次导出崩在裸 OSError 上；自己建的下级目录照样收紧。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    out = root / "notmine"
    out.mkdir()
    os.chmod(out, 0o755)
    real_chmod = pathlib.Path.chmod

    def fake(self, mode, **kw):
        if self == out:
            raise PermissionError(13, "Operation not permitted", str(self))
        return real_chmod(self, mode, **kw)

    monkeypatch.setattr(pathlib.Path, "chmod", fake)
    d = media._ensure_tree(out, "cm", "voice", "2026-09")
    assert d.is_dir() and _mode(d) == 0o700          # 自己能建的照常收紧
    assert _mode(out) != 0o700                       # 收不紧的那级保持原样


def test_export_media_unverifiable_out_root_mode_degrades_once(tmp_path, monkeypatch):
    """Ruling 15：out_root 权限没收紧 → **一条** degraded（含路径），不是每个文件一条。

    既有的 0755 目录 + chmod 被拒，同时打到「chmod 尽力而为」与「一次 stat 检查」两处。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    out = root / "preexisting"
    out.mkdir()
    os.chmod(out, 0o755)
    real_chmod = pathlib.Path.chmod

    def fake(self, mode, **kw):
        if self == out:
            raise PermissionError(13, "Operation not permitted", str(self))
        return real_chmod(self, mode, **kw)

    monkeypatch.setattr(pathlib.Path, "chmod", fake)
    mon = _video_fixture(account, int(datetime.datetime(2026, 9, 15, 12, 0).timestamp()))
    vdir = paths.XWECHAT_FILES / account / "msg/video" / mon
    for i in range(2):
        (vdir / f"e{i:03d}.mp4").write_bytes(b"m" * 8)
    rep = media.export_media(account, ["c@chatroom"], kinds=["video"],
                             months=[mon], out_dir=out)
    assert rep.video.ok == 3                          # 权限没收紧不影响导出本身
    assert _mode(out) != 0o700                        # 自证：确实没收紧（否则这条断言碰运气）
    assert len(rep.degraded) == 1 and str(out) in rep.degraded[0]


# ---------- Ruling 16：_attach_dats 的惰性 glob 归口 ----------

def test_unreadable_attach_tree_raises_media_key_error(tmp_path, monkeypatch):
    """Ruling 16：attach 树读不了 → 既有的「没有样本」MediaKeyError，不是裸 OSError。

    `glob` 是惰性生成器：不物化的话异常从 `_derive_xor_key` 的 for 或 `next(iter(...))`
    里裸奔出 core 层。走 `_safe_iter` 后空样本集落到 derive_keys 既有的能力级降级上。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    (paths.CONTAINER / "app_data/net/kvcomm/key_5_x.statistic").write_bytes(b"")
    attach = paths.XWECHAT_FILES / account / "msg/attach"
    real_glob = pathlib.Path.glob

    def fake(self, pattern, **kw):
        if self == attach:
            raise PermissionError(13, "Operation not permitted", str(self))
        return real_glob(self, pattern, **kw)

    monkeypatch.setattr(pathlib.Path, "glob", fake)
    with pytest.raises(errors.MediaKeyError) as ei:
        media.derive_keys(account)                    # 不抛 PermissionError
    assert "样本" in str(ei.value)
