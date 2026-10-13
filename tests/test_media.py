"""media 核心的单元测试。全部夹具测试内构造（已知 key 加密假明文），无真实用户数据。"""
import datetime
import hashlib
import os
import pathlib
import stat

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from agent_wechat import errors, media, messages, paths, store


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


def test_month_bounds_covers_the_local_month():
    """月份 → [lo, hi)：区间内每个 ts 的 month_of 都是该月，区间外一个都不是。

    月份串由 ts 现算（不硬编码月对）——本函数与 month_of 必须是同一套本地时区规则：
    下界用 UTC 算会在月初/月末错一天，而那正是两个函数唯一要对齐的地方。
    """
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    mon = media.month_of(ts)
    (lo, hi), = media.month_bounds([mon])
    assert lo <= ts < hi
    assert media.month_of(lo) == mon                # 下界含：本地当月 1 日 00:00
    assert media.month_of(hi - 1) == mon            # 上界不含
    assert media.month_of(lo - 1) != mon            # 差一秒就落到上一个月
    assert media.month_of(hi) != mon
    (nlo, nhi), = media.month_bounds([media._shift_month(mon, 1)])
    assert (nlo, nhi) == (hi, nhi) and nhi > nlo    # 相邻月首尾相接，无缝隙无重叠


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


def test_export_media_report_carries_actual_out_dir(tmp_path, monkeypatch):
    """MediaReport.out_dir = **实际使用的**输出根（export_media 赋值）：CLI/MCP 的信封
    从这取，不再各自重算默认回退（T6 裁定：给 MediaReport 加字段，不加小工具函数）。"""
    root, account = make_tree(tmp_path, monkeypatch)
    out = root / "somewhere" / "out"
    rep = media.export_media(account, ["c@chatroom"], kinds=["video"], out_dir=out)
    assert rep.out_dir == str(out)
    rep = media.export_media(account, ["c@chatroom"], kinds=["video"])   # 省略 --out
    assert rep.out_dir == str(root / "home" / "media")                   # = HOME_DIR/media


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


def test_export_media_single_video_at_limit_is_not_truncated(tmp_path, monkeypatch):
    """truncated 只在**真因上限跳过条目**时置位：单视频 + 它自己的缩略图 + max_files=1。

    改前缩略图循环的 `if full()` 排在 mp4_stems 判重之前——那条本来就不导出的缩略图
    （本体已导出）也被算成「因上限跳过」，于是 `--limit 1` 且只有一个视频时照样提示
    「达到上限，已提前收手」，是假话。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    mon = _video_fixture(account, int(datetime.datetime(2026, 9, 15, 12, 0).timestamp()))
    vdir = paths.XWECHAT_FILES / account / "msg/video" / mon
    (vdir / "abcd_thumb.jpg").write_bytes(b"t" * 8)   # 本体的缩略图：本体导出后它不再导出
    rep = media.export_media(account, ["c@chatroom"], kinds=["video"],
                             months=[mon], out_dir=root / "home" / "media", max_files=1)
    assert rep.video.ok == 1 and rep.video.thumb == 0
    assert rep.truncated is False                     # 没有任何条目因上限被跳过


def test_report_to_dict_envelope_shape():
    rep = media.MediaReport(image=media.Counts(ok=3, failed=1), voice=media.Counts(raw_only=2),
                            video=media.Counts(ok=1, thumb=4), degraded=["x"])
    d = rep.to_dict()
    assert d["image"] == {"ok": 3, "failed": 1, "missing": 0, "skipped": 0, "remaining": 0}
    assert d["voice"] == {"ok": 0, "failed": 0, "raw_only": 2, "skipped": 0, "remaining": 0}
    assert d["video"] == {"ok": 1, "thumb": 4, "missing": 0, "skipped": 0, "remaining": 0}
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

    只对 `con=None` 这条**账号级枚举**路成立（生产入口只在索引打不开时走到它）；给了 `con`
    时视频按 `media_refs` 逐条归属、任意会话数都能导（见下方 test_video_export_* 系列）。
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


# ---------- 视频的 refs 驱动归属（spec §4.7） ----------

def _video_index(tmp_path, account, rows):
    """建一个小索引。rows = [(chat_id, local_id, ts, file_key), …]。

    走 make_tree 隔离后的 HOME_DIR：`store.connect()` 会经 `paths.ensure_home_dirs()`
    建目录，不隔离就会在真实 `~/.agent-wechat` 下动土。
    """
    con = store.connect(tmp_path / "index.db")
    store.upsert_batch(con, [
        {"msg_key": messages.row_key(account, messages.md5_of(c), "message_0.db", lid),
         "account": account, "chat_md5": messages.md5_of(c), "chat_id": c,
         "shard": "message_0.db", "local_id": lid, "server_id": None, "ts": ts,
         "msg_type": messages.TYPE_VIDEO, "sender_id": c, "sender_name": None,
         "is_self": 0, "content": None, "media_hint": "[video]"}
        for c, lid, ts, _ in rows])
    store.upsert_media_refs(con, [
        (messages.row_key(account, messages.md5_of(c), "message_0.db", lid), "video", fk, None)
        for c, lid, ts, fk in rows])
    con.commit()
    return con


def test_video_export_attributes_across_two_chats(tmp_path, monkeypatch):
    """多会话也能归属——本任务修的就是这个：改前 ok=0 且 degraded 里有归属说明。"""
    root, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    fk = "aa" * 16
    vdir = paths.XWECHAT_FILES / account / "msg/video" / media.month_of(ts)
    vdir.mkdir(parents=True)
    (vdir / f"{fk}.mp4").write_bytes(b"video-bytes")
    con = _video_index(tmp_path, account, [("wxid_a", 1, ts, fk)])
    rep = media.export_media(account, ["wxid_a", "wxid_b"], kinds=["video"], con=con)
    assert rep.video.ok == 1                 # 不再因为「多会话」而跳过
    assert not any("归属" in d for d in rep.degraded)
    assert (pathlib.Path(rep.out_dir) / messages.md5_of("wxid_a") / "video"
            / media.month_of(ts) / f"{fk}.mp4").read_bytes() == b"video-bytes"


def test_video_export_refs_rerun_is_skipped(tmp_path, monkeypatch):
    """**生产路径**（带 `con`）重跑必须 ok=0/skipped=1：`usecases.media_export` 恒传 `con`，
    真机上的「重跑即推进」只由 refs 循环里那一处 skip 判定兜着。

    与 `con=None` 那条（`test_export_media_video_thumb_only_rerun_is_skipped`）是两条不同的路：
    账号级枚举的覆盖碰不到这里——refs 路径先跑，`con is None` 那两个 continue 把枚举整个挡住。

    变异：删掉 refs 循环里 `already(...)` 那三行 → 第二次 `ok=1 skipped=0`（无条件重写）→ 红。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    fk = "ab" * 16
    vdir = paths.XWECHAT_FILES / account / "msg/video" / media.month_of(ts)
    vdir.mkdir(parents=True)
    (vdir / f"{fk}.mp4").write_bytes(b"video-bytes")
    con = _video_index(tmp_path, account, [("wxid_a", 1, ts, fk)])
    out = root / "home" / "media"

    first = media.export_media(account, ["wxid_a"], kinds=["video"], out_dir=out, con=con)
    assert (first.video.ok, first.video.skipped) == (1, 0)

    second = media.export_media(account, ["wxid_a"], kinds=["video"], out_dir=out, con=con)
    assert (second.video.ok, second.video.skipped, second.video.remaining) == (0, 1, 0)


def test_video_export_degrades_when_no_refs(tmp_path, monkeypatch):
    """索引为空 → 明确降级，不静默产出 0 条（否则「没建索引」与「本机没有视频」不可区分）。"""
    root, account = make_tree(tmp_path, monkeypatch)
    con = store.connect(tmp_path / "empty.db")
    rep = media.export_media(account, ["wxid_a"], kinds=["video"], con=con)
    assert rep.video.ok == 0
    assert any("rebuild-refs" in d for d in rep.degraded), rep.degraded


def test_video_export_distinguishes_empty_index_from_chat_without_videos(tmp_path, monkeypatch):
    """「索引里一条视频引用都没有」与「这个会话没有视频引用」是两回事，修法也不同。

    第二种**没有可执行的修复动作**（该账号其它会话有引用，说明索引本身可用），所以连
    `rebuild-refs` 都不该提——那会把人支去跑一个改不了任何事的命令。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    con = _video_index(tmp_path, account, [("wxid_a", 1, ts, "aa" * 16)])
    rep = media.export_media(account, ["wxid_b"], kinds=["video"], con=con)
    assert rep.video.ok == 0
    assert any("本次请求的会话" in d for d in rep.degraded), rep.degraded
    assert not any("一条视频引用都没有" in d for d in rep.degraded), rep.degraded
    assert not any("rebuild-refs" in d for d in rep.degraded), rep.degraded


def test_video_export_points_at_history_export_when_messages_are_missing(tmp_path, monkeypatch):
    """第三种成因：refs 在、**消息**不在 → 提示「导出这些会话的历史」，且不许提 `rebuild-refs`。

    `media rebuild-refs` **只写 refs、不写 messages**（它自己不依赖 messages），所以这种索引
    永远 join 不出行——唯一能让 refs 路径跑起来的动作是走导出管道（它同时写 messages 与 refs）。
    这条用例同时钉住「空判定不能 join messages」：用 join 计数的话这里会被报成
    「一条视频引用都没有」（而那条提示给的正是无效的修法）。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    fk = "ee" * 16
    con = store.connect(tmp_path / "refs-only.db")
    store.upsert_media_refs(con, [
        (messages.row_key(account, messages.md5_of("wxid_a"), "message_0.db", 1),
         "video", fk, None)])
    con.commit()
    rep = media.export_media(account, ["wxid_a"], kinds=["video"], con=con)
    assert rep.video.ok == 0
    note, = rep.degraded                      # 恰好一条：不是「一条引用都没有」那条
    assert "导出这些会话的历史" in note and "export_full_history" in note
    assert "rebuild-refs" not in note         # 它只写引用，永远修不好这一种


def test_sticker_export_degrades_with_the_same_three_way_note(tmp_path, monkeypatch):
    """EA-089：贴纸侧的「三种空」此前**零测试**。与视频共用 `_refs_absence_note` 之后，
    两种可区分的成因必须逐条成立——尤其「refs 在、消息不在」那一种不许提 `rebuild-refs`
    （它只写 refs，永远修不好这一种）。把 `_refs_absence_note` 换回硬编码文案或删掉
    贴纸侧的调用，本用例必红。
    """
    root, account = make_tree(tmp_path, monkeypatch)

    con = store.connect(tmp_path / "empty.db")
    rep = media.export_media(account, ["wxid_a"], kinds=["sticker"], con=con)
    assert any("一条贴纸引用都没有" in d and "rebuild-refs" in d for d in rep.degraded), rep.degraded

    fk = "ff" * 16
    con2 = store.connect(tmp_path / "refs-only.db")
    store.upsert_media_refs(con2, [
        (messages.row_key(account, messages.md5_of("wxid_a"), "message_0.db", 1),
         "sticker", fk, None)])
    con2.commit()
    rep2 = media.export_media(account, ["wxid_a"], kinds=["sticker"], con=con2)
    note, = rep2.degraded                     # 恰好一条：不是「一条引用都没有」那条
    assert "导出这些会话的历史" in note
    assert "rebuild-refs" not in note

    # 情形 2：引用在**别处**（账号其它会话有贴纸引用 → 索引可用，无需修复）；
    # 这种连 rebuild-refs 都不该提——那会把人支去跑一个改不了任何事的命令。
    con3 = store.connect(tmp_path / "elsewhere.db")
    store.upsert_media_refs(con3, [
        (messages.row_key(account, messages.md5_of("wxid_b"), "message_0.db", 1),
         "sticker", "aa" * 16, None)])
    con3.commit()
    rep3 = media.export_media(account, ["wxid_a"], kinds=["sticker"], con=con3)
    note3, = rep3.degraded
    assert "没有贴纸引用" in note3 and "索引可用" in note3
    assert "rebuild-refs" not in note3


def test_video_export_thumb_keeps_source_stem(tmp_path, monkeypatch):
    """缩略图回退时产物名与账号级枚举那条路逐字一致（`{K}_thumb.jpg`）。

    名字取自**源文件**的 stem：改成 `{K}.jpg` 会让 `media.exported_path`（它按
    `K.mp4` / `K_raw.mp4` / `K_thumb.jpg` 探测）找不到这份产物，机器上既有的缩略图
    也会被误报成「未导出」——与图片那条线上刚修掉的是同一类缺陷。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    fk = "bb" * 16
    vdir = paths.XWECHAT_FILES / account / "msg/video" / media.month_of(ts)
    vdir.mkdir(parents=True)
    (vdir / f"{fk}_thumb.jpg").write_bytes(b"thumb-bytes")   # 本体不在，只剩缩略图
    con = _video_index(tmp_path, account, [("wxid_a", 1, ts, fk)])
    rep = media.export_media(account, ["wxid_a"], kinds=["video"], con=con)
    assert rep.video.thumb == 1 and rep.video.ok == 0
    out = pathlib.Path(rep.out_dir)
    cm = messages.md5_of("wxid_a")
    assert (out / cm / "video" / media.month_of(ts)
            / f"{fk}_thumb.jpg").read_bytes() == b"thumb-bytes"
    assert media.exported_path(out, chat_md5=cm, kind="video", ts=ts,
                               file_key=fk) is not None


def test_video_export_refs_at_limit_do_not_fake_truncation(tmp_path, monkeypatch):
    """refs 路径的 `truncated` 同样不许撒谎：`--limit` 用尽后，落在**月份之外**的引用不算
    「因上限跳过」（与缩略图循环那条既有裁决同一口径）。

    先判上限、后判月份就会红：那条 9 月的引用本来就不导出，却会把 truncated 置成 True。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    aug = int(datetime.datetime(2026, 8, 15, 12, 0).timestamp())     # 命中月份（且更早）
    sep = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())     # 月份之外
    fk_a, fk_b = "cc" * 16, "dd" * 16
    vdir = paths.XWECHAT_FILES / account / "msg/video" / media.month_of(aug)
    vdir.mkdir(parents=True)
    (vdir / f"{fk_a}.mp4").write_bytes(b"m" * 8)      # 9 月那条不必落盘：它在过滤阶段就出局
    con = _video_index(tmp_path, account, [("wxid_a", 1, aug, fk_a), ("wxid_a", 2, sep, fk_b)])
    rep = media.export_media(account, ["wxid_a"], kinds=["video"], months=[media.month_of(aug)],
                             max_files=1, con=con)
    assert rep.video.ok == 1 and rep.truncated is False


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


def _voice_fixture(account: str, rows: list[tuple[int, int, int, bytes]],
                   shard: int = 0) -> None:
    """合成**明文** media_{shard}.db；rows = [(chat_name_id, create_time, local_id, voice_data)]。

    Name2Id 只放一行 'chatA@chatroom'（rowid=1）；明文库是因为测试把 decrypt.decrypted
    换成了直通假上下文——真解密管线（sqlcipher）不在本测试的范围内。
    """
    import sqlite3
    db = paths.XWECHAT_FILES / account / "db_storage/message" / f"media_{shard}.db"
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
    assert rep.degraded == []                         # 成功路径不得出现「语音分片打不开」
    cm = hashlib.md5("chatA@chatroom".encode()).hexdigest()
    wavs = list((out / cm / "voice" / mon).iterdir())
    assert len(wavs) == 1 and wavs[0].name == f"{ts}_7.wav"
    assert wavs[0].read_bytes()[:4] == b"RIFF"
    # 头是导出侧自己封的（Ruling 19）：wave 模块能解析 = 结构合法，且参数确为单声道 16-bit 24kHz
    import wave
    with wave.open(str(wavs[0])) as f:
        assert f.getnchannels() == 1 and f.getsampwidth() == 2 and f.getframerate() == 24000
        assert f.getnframes() == 4800                 # 0.2s × 24kHz
    assert _mode(wavs[0]) == 0o600                    # 明文媒体只有属主可读（经 save_artifact）
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


# ---------- Ruling 18：max_files 必须也管住语音 ----------

def test_export_media_voice_respects_max_files(tmp_path, monkeypatch):
    """Ruling 18：语音计入与图片/视频同一个 produced——两条语音 + max_files=1 → 只出 1 条。

    改前语音从不递增 produced，一个有几千条语音的会话会无视 `--limit` 全量落盘。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    mon = media.month_of(ts)
    _voice_fixture(account, [(1, ts, 7, _silk_blob()), (1, ts, 8, _silk_blob())])
    _passthrough_decrypted(monkeypatch)

    out = root / "home" / "media"
    rep = media.export_media(account, ["chatA@chatroom"], kinds=["voice"],
                             months=[mon], out_dir=out, max_files=1)
    assert rep.voice.ok == 1 and rep.truncated is True
    cm = hashlib.md5("chatA@chatroom".encode()).hexdigest()
    assert [p.name for p in (out / cm / "voice" / mon).iterdir()] == [f"{ts}_7.wav"]


# ---------- 撞上限后继续扫描：remaining 计数 ----------

def test_export_media_reports_remaining_after_cap(tmp_path, monkeypatch):
    """撞顶后要如实报出「还剩几条」，否则「已提前收手」是一句没有数量的空话。

    变异：把 `if is_full()` 分支里的 `report.voice.remaining += 1` 行删掉 → remaining=0 → 红。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    mon = media.month_of(ts)
    _voice_fixture(account, [(1, ts, i, _silk_blob()) for i in (7, 8, 9)])
    _passthrough_decrypted(monkeypatch)

    rep = media.export_media(account, ["chatA@chatroom"], kinds=["voice"],
                             months=[mon], out_dir=root / "home" / "media", max_files=1)
    assert (rep.voice.ok, rep.voice.remaining, rep.truncated) == (1, 2, True)


def test_export_media_skip_does_not_consume_limit(tmp_path, monkeypatch):
    """skip 不吃预算：已存在 2 条 + 余下 3 条 + max_files=3 → ok=3、skipped=2、remaining=0。

    变异：让 skip 分支也走 `bump()` → ok 掉到 1、remaining=2 → 红。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    mon = media.month_of(ts)
    _voice_fixture(account, [(1, ts, i, _silk_blob()) for i in (5, 6, 7, 8, 9)])
    _passthrough_decrypted(monkeypatch)
    out = root / "home" / "media"

    media.export_media(account, ["chatA@chatroom"], kinds=["voice"],
                       months=[mon], out_dir=out, max_files=2)      # 先落 5、6
    rep = media.export_media(account, ["chatA@chatroom"], kinds=["voice"],
                             months=[mon], out_dir=out, max_files=3)
    assert (rep.voice.ok, rep.voice.skipped, rep.voice.remaining) == (3, 2, 0)
    assert rep.truncated is False


def test_export_media_image_remaining_counts_every_month(tmp_path, monkeypatch):
    """`image.remaining` 必须把**所有**月份数完：月份层留一处 `if full(): break`，
    撞顶后月份循环立刻退出，第一个月之后再也不访问 —— remaining 少报，而其余测试全绿。

    变异：在 `for month_dir in _safe_iter(base):` 里把它下面的 `if full(): break` 加回去
    → remaining 从 5 掉到 2（只数到第一个月）→ 红。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    code = "999999"
    (paths.CONTAINER / "app_data/net/kvcomm/key_999999_1_2_3_4_5_input.statistic").write_bytes(b"")
    cm = hashlib.md5("chatA@chatroom".encode()).hexdigest()
    aes = hashlib.md5((code + paths.bare_wxid(account)).encode()).hexdigest()[:16].encode()
    # 取样目录（月份不在 want 里，不参与导出），只为 derive_keys 的 xorKey 统计与 aesKey 验证
    sdir = paths.XWECHAT_FILES / account / "msg/attach" / cm / "2026-03" / "Img"
    sdir.mkdir(parents=True)
    samples = [sdir / f"sam{i}.dat" for i in range(4)]
    for p in samples:
        p.write_bytes(build_v2_dat(jpeg_like(64), b"\x33" * 8 + b"\xff\xd9", aes, FAKE_XOR))
    monkeypatch.setattr(media, "_attach_dats", lambda acct: iter(samples))
    months = ("2026-08", "2026-09")
    for mon in months:
        img = paths.XWECHAT_FILES / account / "msg/attach" / cm / mon / "Img"
        img.mkdir(parents=True)
        for i in range(3):
            (img / f"im{i}.dat").write_bytes(
                build_v2_dat(jpeg_like(256), b"\x11" * 8 + b"\xff\xd9", aes, FAKE_XOR))

    rep = media.export_media(account, ["chatA@chatroom"], kinds=["image"],
                             months=list(months), out_dir=root / "home" / "media", max_files=1)
    # 6 张图、上限 1：出 1 张，剩 5 张 —— 两个月份的 3+3 都要数到
    assert (rep.image.ok, rep.image.remaining, rep.truncated) == (1, 5, True)


# ---------- Ruling 19：中间明文不得落系统 TMPDIR ----------

def test_export_media_voice_plaintext_stays_in_home_tmp(tmp_path, monkeypatch):
    """Ruling 19：silk/pcm 中间文件只落 HOME/tmp（0700）的临时目录，导出后一片不留。

    `pilk.silk_to_wav` 会在**系统 TMPDIR** 里留一份从不清理的明文 .pcm——改后用
    `pilk.decode` + 自己的 44 字节 RIFF 头，中间产物全在可 purge 的目录里。
    """
    import tempfile
    from agent_wechat import decrypt
    root, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    mon = media.month_of(ts)
    _voice_fixture(account, [(1, ts, 7, _silk_blob())])
    _passthrough_decrypted(monkeypatch)

    sys_tmp = pathlib.Path(tempfile.gettempdir())
    before = sorted(p.name for p in (*sys_tmp.glob("*.pcm"), *sys_tmp.glob("*.silk")))
    rep = media.export_media(account, ["chatA@chatroom"], kinds=["voice"],
                             months=[mon], out_dir=root / "home" / "media")
    assert rep.voice.ok == 1
    after = sorted(p.name for p in (*sys_tmp.glob("*.pcm"), *sys_tmp.glob("*.silk")))
    assert after == before                            # 系统临时目录零新增
    assert list(decrypt.tmp_dir().iterdir()) == []    # HOME/tmp 自己也收干净了
    assert _mode(decrypt.tmp_dir()) == 0o700          # 中间明文的落点是 0700


# ---------- Ruling 20：落盘失败的归口 ----------

def test_export_media_image_write_error_counts_failed(tmp_path, monkeypatch):
    """Ruling 20：图片 write_bytes 抛 OSError（磁盘满/只读）→ image.failed，不逃出 core。"""
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
    real_write = pathlib.Path.write_bytes

    def fake(self, data):
        if self.name == "beef.jpg":
            raise OSError(28, "No space left on device", str(self))
        return real_write(self, data)

    monkeypatch.setattr(pathlib.Path, "write_bytes", fake)
    rep = media.export_media(account, ["chatA@chatroom"], kinds=["image"],
                             months=["2026-09"], out_dir=root / "home" / "media")
    assert rep.image.ok == 0 and rep.image.failed == 1


def test_export_media_partial_write_is_unlinked(tmp_path, monkeypatch):
    """写到一半失败（磁盘满）不许留下半成品：否则下一轮的 skip 判定把它当成品，永久坏且报健康。

    `exported_path` 只探测**存在性**：改前 `save_artifact` 的 except 不清盘，残留文件会让之后每一轮都
    `skipped += 1`、`failed=0` —— 唯一出路是 `--force`，而没有任何输出会告诉用户这件事。
    （改前每次无条件覆写，这类残留会自愈；本分支引入 skip 后它变成永久的。）与
    `_wxgf_to_jpeg` 的「不留坏文件」是同一条约定。

    变异：删掉 `save_artifact` except 分支里的 `p.unlink(...)` → 下面三条断言全红。
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
    real_write = pathlib.Path.write_bytes

    def fake(self, data):
        if self.name == "beef.jpg":
            self.open("wb").write(data[: len(data) // 2])   # 半成品先落盘：磁盘满正是这个形态
            raise OSError(28, "No space left on device", str(self))
        return real_write(self, data)

    monkeypatch.setattr(pathlib.Path, "write_bytes", fake)
    out = root / "home" / "media"
    dest = out / cm / "image" / "2026-09" / "beef.jpg"

    assert media.save_artifact(out, cm, "image", "2026-09", "beef", "jpg", b"x" * 8) is None
    assert not dest.exists()                     # 半成品必须已被清掉

    rep = media.export_media(account, ["chatA@chatroom"], kinds=["image"],
                             months=["2026-09"], out_dir=out)
    assert rep.image.ok == 0 and rep.image.failed == 1
    assert not dest.exists()                     # 导出路径同样不留


def test_export_media_video_write_error_counts_missing(tmp_path, monkeypatch):
    """Ruling 20：视频本体/缩略图落盘失败 → video.missing（写不出来 = 拿不到本体）。

    这条打的是 `save_artifact` 里 write 成功之后的 chmod——Ruling 20 点名了 write_bytes 与
    chmod 两条语句，两条都要归口。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    mon = _video_fixture(account, int(datetime.datetime(2026, 9, 15, 12, 0).timestamp()))
    vdir = paths.XWECHAT_FILES / account / "msg/video" / mon
    (vdir / "cccc_thumb.jpg").write_bytes(b"t" * 8)
    (vdir / "bbbb.mp4").write_bytes(b"m" * 8)
    (vdir / "dddd_thumb.jpg").write_bytes(b"t" * 8)
    real_chmod = pathlib.Path.chmod
    # 按**文件名**匹配：chmod 打在导出目标（out_root 下），不是微信树里的源文件——
    # 与读失败用例的 `self in (src, ...)` 写法不同，这里比较源路径永远不会命中。
    blocked = {"bbbb.mp4", "dddd_thumb.jpg"}

    def fake(self, mode, **kw):
        if self.name in blocked:
            raise PermissionError(13, "Operation not permitted", str(self))
        return real_chmod(self, mode, **kw)

    monkeypatch.setattr(pathlib.Path, "chmod", fake)
    rep = media.export_media(account, ["c@chatroom"], kinds=["video"],
                             months=[mon], out_dir=root / "home" / "media")
    assert rep.video.ok == 1 and rep.video.thumb == 1 and rep.video.missing == 2


def test_export_media_voice_write_error_counts_failed(tmp_path, monkeypatch):
    """Ruling 20：语音落盘失败 → voice.failed（不退回 .slk：磁盘满时它一样写不出来）。"""
    root, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    mon = media.month_of(ts)
    _voice_fixture(account, [(1, ts, 7, _silk_blob())])
    _passthrough_decrypted(monkeypatch)
    real_write = pathlib.Path.write_bytes

    def fake(self, data):
        if self.suffix == ".wav":                     # 中间 silk 写在临时目录（.silk）：不受影响
            raise OSError(28, "No space left on device", str(self))
        return real_write(self, data)

    monkeypatch.setattr(pathlib.Path, "write_bytes", fake)
    rep = media.export_media(account, ["chatA@chatroom"], kinds=["voice"],
                             months=[mon], out_dir=root / "home" / "media")
    assert rep.voice.ok == 0 and rep.voice.failed == 1 and rep.voice.raw_only == 0


# ---------- Ruling 15：chmod 尽力而为 + out_root 一次 stat 检查 ----------

def test_chmod_failure_is_best_effort(tmp_path, monkeypatch):
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
    d = media.ensure_tree(out, "cm", "voice", "2026-09")
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


# ---------- Task 4：消息 → 媒体引用（media_refs 的数据源） ----------

def test_extract_media_ref_packed_is_authoritative():
    # 前缀用**非 hex**字节（brief 里是 b"ab"——'a'/'b' 本身就是 hex 字符，最左匹配会
    # 从 'a' 起算，切出 "ab0123…abcd" 这个错窗口；这条测试的断言恰好是这个窗口的判别式）
    packed = b"\x00\x01" + b"zz" + b"0123456789ABCDEF0123456789abcdef" + b"\x00\x02"
    kind, fk, xm = media.extract_media_ref(3, '<msg><img md5="11111111111111111111111111111111"/></msg>', packed)
    assert kind == "image" and fk == "0123456789abcdef0123456789abcdef"
    assert xm == "11111111111111111111111111111111"


def test_extract_media_ref_xml_fallback_only_when_packed_missing():
    kind, fk, xm = media.extract_media_ref(43, '<msg><videomsg md5="22222222222222222222222222222222"/></msg>', None)
    assert kind == "video" and fk == "22222222222222222222222222222222" and xm == fk
    # packed 与 XML 同时在：file_key 用 packed，xml_md5 只记录
    packed = b"33333333333333333333333333333333"
    _, fk2, xm2 = media.extract_media_ref(3, '<img md5="22222222222222222222222222222222"/>', packed)
    assert fk2 == "33333333333333333333333333333333" and xm2 == "22222222222222222222222222222222"


def test_extract_media_ref_non_media_and_empty():
    assert media.extract_media_ref(1, "hi", b"44444444444444444444444444444444") is None
    assert media.extract_media_ref(34, "hi", b"44444444444444444444444444444444") is None   # 语音不入表
    assert media.extract_media_ref(3, None, None) is None
    assert media.extract_media_ref(3, "<img/>", None) is None


def test_extract_media_ref_short_xml_md5_recorded_but_not_a_key():
    """16-hex 的 XML md5 只记录、**不**当文件名兜底（现网 md5 是 32-hex；短形态认了 = 乱猜文件）。

    packed 在 → 三元组保留 xml 的 16-hex；packed 缺 → 没有可信 file_key，整体 None。
    """
    packed = b"\x00\x07" + b"55555555555555555555555555555555"
    kind, fk, xm = media.extract_media_ref(3, '<img md5="AbCdEfAbCdEfAbCd"/>', packed)
    assert (kind, fk, xm) == ("image", "55555555555555555555555555555555", "abcdefabcdefabcd")
    assert media.extract_media_ref(3, '<img md5="AbCdEfAbCdEfAbCd"/>', None) is None


# ---------- Ruling 21：_wxgf_to_jpeg 的收尾 OSError 收口 ----------

def test_wxgf_jpeg_chmod_failure_falls_back_to_container(tmp_path, monkeypatch):
    """Ruling 21：_wxgf_to_jpeg 收尾的 chmod/unlink 抛 OSError → 返回 False，不逃出 core。

    ffmpeg 整段换成假的 subprocess.run（真机装没装 ffmpeg 不该决定这条断言的成败）：
    它把 dest 落盘并返回 0，从而精确打到收尾的 chmod。回退必须干净——宁可存 .wxgf
    容器，也不留一个权限没收紧的明文 jpeg（_save 的替代路径会补 0600）。
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
    wxgf = b"wxgf" + b"\x00\x00\x00\x01" + b"\x00" * 16
    (img / "c0de.dat").write_bytes(build_v2_dat(wxgf, b"\x22" * 20, aes, FAKE_XOR))

    class _Proc:
        returncode = 0

    def fake_run(cmd, **kw):
        pathlib.Path(cmd[-1]).write_bytes(b"\xff\xd8\xff" + b"\x00" * 16)   # 假装 ffmpeg 直写成功
        return _Proc()

    monkeypatch.setattr(media, "_find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(media.subprocess, "run", fake_run)
    real_chmod = pathlib.Path.chmod

    def fake_chmod(self, mode, **kw):
        if self.suffix == ".jpg":                    # 只拒绝 jpeg 落盘那一下，.wxgf 照常
            raise PermissionError(13, "Operation not permitted", str(self))
        return real_chmod(self, mode, **kw)

    monkeypatch.setattr(pathlib.Path, "chmod", fake_chmod)
    out = root / "home" / "media"
    rep = media.export_media(account, ["chatA@chatroom"], kinds=["image"],
                             months=["2026-09"], out_dir=out)
    assert rep.image.ok == 1                         # 回退存 .wxgf 成功
    assert [p.name for p in (out / cm / "image" / "2026-09").iterdir()] == ["c0de.wxgf"]
    assert rep.degraded == ["wxgf 图未转 JPEG（无 ffmpeg 或转码失败），存原容器"]


# ---------- Ruling 22：语音分片不退场 ----------

def test_export_media_voice_present_in_two_shards_exports_both(tmp_path, monkeypatch):
    """Ruling 22：同一会话在**两个**媒体分片里都有语音 → 两条都要导出（ok==2）。

    实测依据：media_0/media_1 的 Name2Id 交集 13 人、其中 12 个会话两分片都有语音。
    改前的「查到就退场」（`remaining_chats -= produced_in_db` + 循环顶 break）在第一个
    分片后就把会话划掉，第二个分片里同一会话的其他语音被静默丢弃（ok 只会是 1）。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    mon = media.month_of(ts)
    _voice_fixture(account, [(1, ts, 7, _silk_blob())], shard=0)
    _voice_fixture(account, [(1, ts, 8, _silk_blob())], shard=1)
    _passthrough_decrypted(monkeypatch)

    out = root / "home" / "media"
    rep = media.export_media(account, ["chatA@chatroom"], kinds=["voice"],
                             months=[mon], out_dir=out)
    assert rep.voice.ok == 2
    cm = hashlib.md5("chatA@chatroom".encode()).hexdigest()
    assert {p.name for p in (out / cm / "voice" / mon).iterdir()} == {f"{ts}_7.wav", f"{ts}_8.wav"}


# ---------- Ruling 23a：语音分片的 OSError 归口 ----------

def test_export_media_voice_unreadable_shard_is_skipped(tmp_path, monkeypatch):
    """Ruling 23a：一个分片打不开（库读不了 / HOME/tmp 建不出来）→ 跳过该分片，
    其余分片照常导出，裸 OSError 不得逃出 core 层。

    在 `decrypt.decrypted` 上定点抛 OSError——真机上抛它的是 `tempfile.mkdtemp(dir=HOME/tmp)`
    与 `snapshot()` 的 copy2/read_bytes，三者都不带包装（decrypt.py）。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    mon = media.month_of(ts)
    _voice_fixture(account, [(1, ts, 7, _silk_blob())], shard=0)
    _voice_fixture(account, [(1, ts, 8, _silk_blob())], shard=1)

    import contextlib
    from agent_wechat import decrypt as dec_mod

    @contextlib.contextmanager
    def flaky(db_path, passphrase, tmpdir=None):
        if db_path.name == "media_0.db":
            raise OSError(28, "No space left on device", str(db_path))
        yield db_path

    # 与 _passthrough_decrypted 同一套 monkeypatch（含 Ruling 17 的模块级属性那条）
    monkeypatch.setattr(dec_mod, "decrypted", flaky)
    monkeypatch.setattr("agent_wechat.media.decrypt", dec_mod)

    out = root / "home" / "media"
    rep = media.export_media(account, ["chatA@chatroom"], kinds=["voice"],
                             months=[mon], out_dir=out)
    assert rep.voice.ok == 1
    cm = hashlib.md5("chatA@chatroom".encode()).hexdigest()
    assert {p.name for p in (out / cm / "voice" / mon).iterdir()} == {f"{ts}_8.wav"}


# ---------- 终审 Fix 1：语音分片打不开必须可见 ----------

def test_export_media_voice_broken_shards_are_degraded(tmp_path, monkeypatch):
    """语音分片全打不开 → voice 计数为 0 **且** degraded 有说明（一次导出一条）。

    改前 `_export_voices` 的 except 只 `continue`：某会话的语音全在打不开/坏钥的分片里时，
    报表是 voice 全 0 + 空 degraded，与「这个会话没有语音消息」逐字节不可区分——本功能里
    其它每一种能力级失败都会公告，语音是唯一的静默者。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    mon = media.month_of(ts)
    _voice_fixture(account, [(1, ts, 7, _silk_blob())], shard=0)
    _voice_fixture(account, [(1, ts, 8, _silk_blob())], shard=1)   # 两个分片都持有该会话

    import contextlib
    from agent_wechat import decrypt as dec_mod

    @contextlib.contextmanager
    def broken(db_path, passphrase, tmpdir=None):
        raise errors.AgentWeChatError("passphrase 缓存缺失，无法解密 media 分片")
        yield                                    # 生成器协议需要（永不执行到）

    monkeypatch.setattr(dec_mod, "decrypted", broken)
    monkeypatch.setattr("agent_wechat.media.decrypt", dec_mod)

    out = root / "home" / "media"
    rep = media.export_media(account, ["chatA@chatroom"], kinds=["voice"],
                             months=[mon], out_dir=out)
    assert rep.voice.ok == 0 and rep.voice.failed == 0
    voice_notes = [d for d in rep.degraded if "语音" in d]
    assert len(voice_notes) == 1                 # 两个分片同因失败：只说一次（wxgf_warned 风格）
    assert "passphrase 缓存缺失" in voice_notes[0]


# ---------- 终审 Fix 3：ensure_tree 的 mkdir 不是裸 OSError ----------

def test_export_media_unwritable_out_dir_raises_media_output_error(tmp_path, monkeypatch):
    """输出根建不出来（`--out` 只读 / 不可写 HOME / 磁盘满）→ MediaOutputError。

    裸 PermissionError/OSError 会穿透 core 层：CLI 只翻 AgentWeChatError（用户见 traceback），
    MCP 落到 generic_payload 兜底——与本模块开篇的错误契约相悖。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    bad = root / "ro" / "media"
    real_mkdir = pathlib.Path.mkdir

    def fake(self, *a, **kw):
        if self == bad:
            raise PermissionError(13, "Operation not permitted", str(self))
        return real_mkdir(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "mkdir", fake)
    with pytest.raises(errors.MediaOutputError) as ei:      # 不是 PermissionError
        media.export_media(account, ["c@chatroom"], kinds=["video"],
                           months=["2026-09"], out_dir=bad)
    assert str(bad) in str(ei.value) and ei.value.hint


# ---------- 已导出产物的路径推导（spec: 2026-09-16-media-message-link §2.3） ----------

def test_exported_path_image_globs_any_ext(tmp_path):
    """image 的扩展名不定（jpg/png/gif/webp/wxgf），必须 glob 而不是写死。"""
    root = tmp_path / "out"
    d = root / ("a" * 32) / "image" / "2026-09"
    d.mkdir(parents=True)
    (d / "beef.png").write_bytes(b"x")
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    got = media.exported_path(root, chat_md5="a" * 32, kind="image", ts=ts,
                              file_key="beef")
    assert got is not None and got.name == "beef.png"


def test_exported_path_image_ignores_unlisted_ext(tmp_path):
    """不能 glob 到什么算什么：非媒体扩展名（如 .dat）不得被认成产物。"""
    root = tmp_path / "out"
    d = root / ("a" * 32) / "image" / "2026-09"
    d.mkdir(parents=True)
    (d / "beef.dat").write_bytes(b"x")
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    assert media.exported_path(root, chat_md5="a" * 32, kind="image", ts=ts,
                               file_key="beef") is None


def test_exported_path_image_suffixed_source_stem(tmp_path):
    """写盘用的是**源 dat 的 stem**（导出不改名，见 `_save(..., dat.stem, ext, ...)`）：
    `K` 的产物可能是 `K_hd.png`，而 `resolve_image` 的优先级又把 `_hd` 排在裸名之前
    —— 两者同时存在时同样以 `_hd` 胜（只探裸 K 会返回错的那份，甚至返回 None）。"""
    root = tmp_path / "out"
    d = root / ("e" * 32) / "image" / "2026-09"
    d.mkdir(parents=True)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    (d / "beef.jpg").write_bytes(b"x")
    (d / "beef_hd.png").write_bytes(b"x")
    assert media.exported_path(root, chat_md5="e" * 32, kind="image", ts=ts,
                               file_key="beef").name == "beef_hd.png"


def test_exported_path_image_prev_month_is_in_window(tmp_path):
    """产物可能落在**上月**：写盘用的是源目录的月名（`mon = month_dir.name`），而源目录
    可能比消息 ts 的月份早一个月 —— 这正是 `resolve_image` 有 ±1 回退的理由。窗口塌成
    单月，这条就红。"""
    root = tmp_path / "out"
    cm = "h" * 32
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    d = root / cm / "image" / media._shift_month(media.month_of(ts), -1)
    d.mkdir(parents=True)
    (d / "beef.jpg").write_bytes(b"x")
    assert media.exported_path(root, chat_md5=cm, kind="image", ts=ts,
                               file_key="beef") == d / "beef.jpg"


def test_exported_path_image_next_month_is_in_window(tmp_path):
    """另一侧同样在窗口里（`resolve_*` 的窗口是 当月→上月→下月，不是单侧）。"""
    root = tmp_path / "out"
    cm = "i" * 32
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    d = root / cm / "image" / media._shift_month(media.month_of(ts), 1)
    d.mkdir(parents=True)
    (d / "beef.jpg").write_bytes(b"x")
    assert media.exported_path(root, chat_md5=cm, kind="image", ts=ts,
                               file_key="beef") == d / "beef.jpg"


def test_exported_path_video_is_mp4_only(tmp_path):
    """video 只认 mp4：同目录里只有 jpg 时必须是 None，不能被当成产物。

    诱饵是必需的 —— 只造 mp4 再断言拿到 mp4，改成分支回退 jpg 也照样绿。
    """
    root = tmp_path / "out"
    d = root / ("b" * 32) / "video" / "2026-09"
    d.mkdir(parents=True)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    (d / "cafe.jpg").write_bytes(b"x")
    assert media.exported_path(root, chat_md5="b" * 32, kind="video", ts=ts,
                               file_key="cafe") is None
    (d / "cafe.mp4").write_bytes(b"x")
    assert media.exported_path(root, chat_md5="b" * 32, kind="video", ts=ts,
                               file_key="cafe").name == "cafe.mp4"


def test_exported_path_video_thumb_is_fallback_and_mp4_wins(tmp_path):
    """缩略图是**最后**的候选：只有它时返回它（无缝体时导出写下的就是它），
    本体与它同在时必须是本体。"""
    root = tmp_path / "out"
    d = root / ("g" * 32) / "video" / "2026-09"
    d.mkdir(parents=True)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    (d / "cafe_thumb.jpg").write_bytes(b"x")
    assert media.exported_path(root, chat_md5="g" * 32, kind="video", ts=ts,
                               file_key="cafe").name == "cafe_thumb.jpg"
    (d / "cafe.mp4").write_bytes(b"x")
    assert media.exported_path(root, chat_md5="g" * 32, kind="video", ts=ts,
                               file_key="cafe").name == "cafe.mp4"


def test_exported_path_video_raw_variant(tmp_path):
    """`_raw.mp4` 是真实语料里的变体（`resolve_video` 也认它）；导出不改名，
    所以它的产物就叫 `K_raw.mp4` —— 只探裸 K 会漏掉真正存在的那份。"""
    root = tmp_path / "out"
    d = root / ("f" * 32) / "video" / "2026-09"
    d.mkdir(parents=True)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    (d / "cafe_raw.mp4").write_bytes(b"x")
    assert media.exported_path(root, chat_md5="f" * 32, kind="video", ts=ts,
                               file_key="cafe").name == "cafe_raw.mp4"


def test_exported_path_voice_wav_wins_over_slk(tmp_path):
    """pilk 缺失时导出的是 .slk；两者都在时 .wav 优先。"""
    root = tmp_path / "out"
    d = root / ("c" * 32) / "voice" / "2026-09"
    d.mkdir(parents=True)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    (d / f"{ts}_7.slk").write_bytes(b"x")
    assert media.exported_path(root, chat_md5="c" * 32, kind="voice", ts=ts,
                               local_id=7).name == f"{ts}_7.slk"
    (d / f"{ts}_7.wav").write_bytes(b"x")
    assert media.exported_path(root, chat_md5="c" * 32, kind="voice", ts=ts,
                               local_id=7).name == f"{ts}_7.wav"


def test_exported_path_voice_has_no_month_window(tmp_path):
    """语音**不**做 ±1：写盘用的就是 `month_of(create_time)`（`_export_voices_from`），
    与消息 ts 同源（实测 400/400 相等）—— 给它加上窗口就成了「认别的月的产物」。"""
    root = tmp_path / "out"
    cm = "j" * 32
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    d = root / cm / "voice" / media._shift_month(media.month_of(ts), -1)
    d.mkdir(parents=True)
    (d / f"{ts}_7.wav").write_bytes(b"x")
    assert media.exported_path(root, chat_md5=cm, kind="voice", ts=ts,
                               local_id=7) is None


def test_exported_path_missing_returns_none(tmp_path):
    """目录不存在、文件不存在 —— 一律 None，不抛。"""
    root = tmp_path / "nowhere"
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    assert media.exported_path(root, chat_md5="d" * 32, kind="image", ts=ts,
                               file_key="dead") is None
    assert media.exported_path(root, chat_md5="d" * 32, kind="voice", ts=ts,
                               local_id=1) is None


def test_exported_path_missing_args_are_guarded_not_lucky(tmp_path):
    """参数不全 → None。**诱饵文件真实存在**，所以这条断言真在测 guard：
    去掉 file_key/local_id 的判空，函数就会去匹配 `None.jpg` / `{ts}_None.wav` 并命中。"""
    root = tmp_path / "out"
    cm = "d" * 32
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    (root / cm / "image" / "2026-09").mkdir(parents=True)
    (root / cm / "voice" / "2026-09").mkdir(parents=True)
    (root / cm / "image" / "2026-09" / "None.jpg").write_bytes(b"x")
    (root / cm / "voice" / "2026-09" / f"{ts}_None.wav").write_bytes(b"x")
    assert media.exported_path(root, chat_md5=cm, kind="image", ts=ts) is None
    assert media.exported_path(root, chat_md5=cm, kind="voice", ts=ts) is None


# ---------- 语音本体存在集合（spec: 2026-09-16-media-message-link §2.3） ----------

def test_voice_body_keys_empty_set_when_no_shards(tmp_path, monkeypatch):
    """一条语音分片都没有 → 空集合（不是 None）：「确定没有」与「读不到」是两回事。

    名字必须与断言同向（原名 `..._none_when_no_shards` 是反的）：这个 `None` 与空集合的
    区分是整个语音设计的立足点，而名字反了之后最省事的「修法」是把断言改成 `is None`。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    got = media.voice_body_keys(account)
    assert got == frozenset()


def test_voice_body_keys_unreadable_shard_is_none(tmp_path, monkeypatch):
    """分片存在但打不开（这里是空文件，不是合法 sqlite）→ None = 读不到，调用方据此降级。"""
    root, account = make_tree(tmp_path, monkeypatch)
    d = paths.db_storage(account) / "message"
    (d / "media_0.db").write_bytes(b"not a database")
    assert media.voice_body_keys(account) is None


def test_voice_body_keys_reads_the_query_body(tmp_path, monkeypatch):
    """**这条才碰到查询主体**：前两条都停在退化出口（零分片 / 打不开的分片），
    把 `with decrypt.decrypted(...)` 整段换成 `pass` 它们照样绿 —— 集合的内容无人守护。

    断言同时钉住三件事：Name2Id 的方向（rid → user_name，反了就对不上）、
    md5 的输入（会话 username）、以及 local_id 被原样带出。
    """
    import hashlib
    root, account = make_tree(tmp_path, monkeypatch)
    _voice_fixture(account, [(1, 1700000000, 7, b"silky"),
                             (1, 1700000001, 8, b"silky")])
    _passthrough_decrypted(monkeypatch)
    want = hashlib.md5(b"chatA@chatroom").hexdigest()
    assert media.voice_body_keys(account) == frozenset({(want, 7), (want, 8)})


def test_voice_body_keys_skips_rows_without_voice_data(tmp_path, monkeypatch):
    """`voice_data IS NULL` 的行不计入 —— 这一条守的是 WHERE 里唯一的那个谓词。"""
    import hashlib
    root, account = make_tree(tmp_path, monkeypatch)
    _voice_fixture(account, [(1, 1700000000, 7, b"silky"),
                             (1, 1700000001, 8, None)])
    _passthrough_decrypted(monkeypatch)
    want = hashlib.md5(b"chatA@chatroom").hexdigest()
    assert media.voice_body_keys(account) == frozenset({(want, 7)})


# ---------- 续跑：已导出即跳过 ----------

def test_export_media_skips_already_exported(tmp_path, monkeypatch):
    """重跑必须推进：第二次跑同一条命令，已导出的被跳过、且**不吃** --limit 预算。

    改前 `save_artifact` 无条件覆写、`full()` 只认 produced —— 第二次跑会走**同一顺序**
    （`_safe_iter` 用 sorted、语音 `ORDER BY create_time, local_id`、执行序固定
    image→video→voice），在同一处撞顶，一条都不多。真机症状：2239 条语音跑了 1923
    条后再跑还是 1923 条。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    mon = media.month_of(ts)
    _voice_fixture(account, [(1, ts, i, _silk_blob()) for i in (7, 8, 9)])
    _passthrough_decrypted(monkeypatch)
    out = root / "home" / "media"

    first = media.export_media(account, ["chatA@chatroom"], kinds=["voice"],
                               months=[mon], out_dir=out, max_files=2)
    assert (first.voice.ok, first.voice.skipped) == (2, 0)

    second = media.export_media(account, ["chatA@chatroom"], kinds=["voice"],
                                months=[mon], out_dir=out, max_files=2)
    assert (second.voice.ok, second.voice.skipped) == (1, 2)


def test_export_media_force_ignores_existing(tmp_path, monkeypatch):
    """`--force` 忽略已存在：跑两次，第二次 skipped 仍为 0、ok 仍是满额。

    变异：`already()` 里删掉 `if force: return False` → 第二次 skipped=2 → 红。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    mon = media.month_of(ts)
    _voice_fixture(account, [(1, ts, i, _silk_blob()) for i in (7, 8)])
    _passthrough_decrypted(monkeypatch)
    out = root / "home" / "media"

    media.export_media(account, ["chatA@chatroom"], kinds=["voice"],
                       months=[mon], out_dir=out, max_files=5)
    again = media.export_media(account, ["chatA@chatroom"], kinds=["voice"],
                               months=[mon], out_dir=out, max_files=5, force=True)
    assert (again.voice.ok, again.voice.skipped) == (2, 0)


def test_export_media_video_thumb_only_rerun_is_skipped(tmp_path, monkeypatch):
    """账号级枚举路径（`con=None`）下，已导出的**缩略图**重跑必须跳过、不许再吃 --limit。

    视频树里只有 `{K}_thumb.jpg`（无缝体：本体不在磁盘）时，缩略图循环是唯一会写盘的入口。
    基准名必须是**裸 file_key** `{K}`——`_out_candidates("video", base)` 的候选集是围绕它
    设计的（`{K}.mp4` / `{K}_raw.mp4` / `{K}_thumb.jpg`）；传 `{K}_thumb` 只会得到
    `{K}_thumb_thumb.jpg` 这种永不存在的名字，判定于是永不命中，缩略图每轮重写（Task 3
    落地后还会让 remaining 恒 >0，CLI 永远印「还有 N 个未导出」）。

    变异：把本处基准名改回 `f.stem` → 第二次 `thumb=1 skipped=0` → 红。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    mon = media.month_of(ts)
    vdir = paths.XWECHAT_FILES / account / "msg/video" / mon
    vdir.mkdir(parents=True)
    (vdir / "abcd_thumb.jpg").write_bytes(b"t" * 8)   # 只有缩略图：本体不在磁盘
    out = root / "home" / "media"

    first = media.export_media(account, ["c@chatroom"], kinds=["video"],
                               months=[mon], out_dir=out)
    assert (first.video.thumb, first.video.skipped) == (1, 0)

    second = media.export_media(account, ["c@chatroom"], kinds=["video"],
                                months=[mon], out_dir=out)
    assert (second.video.thumb, second.video.skipped) == (0, 1)


def test_export_media_video_remaining_equals_the_files_a_resume_adds(tmp_path, monkeypatch):
    """`remaining` 不是「上限分支走了几次」，是**还差几个文件**：续跑新增的文件数必须等于它。

    两个视频各带本体 + 缩略图、`max_files=1`。撞顶那轮把第二个视频的本体计进 remaining 后，
    若**不**把它记进 `mp4_stems`，它的缩略图会再走一遍 `full()` 又被计一次 —— 于是报「还剩 2」
    而实际上只差 1 个文件（缩略图在「本体已存在」时从来不会写：见缩略图循环开头那道 continue）。

    断言取最强的形态「续跑新增文件数 == 上一轮的 remaining」，而不是「remaining == 1」——
    后者换了夹具也能以错误的理由通过。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    mon = "2026-09"
    vdir = paths.XWECHAT_FILES / account / "msg/video" / mon
    vdir.mkdir(parents=True)
    for stem in ("aaaa", "bbbb"):
        (vdir / f"{stem}.mp4").write_bytes(b"m" * 8)
        (vdir / f"{stem}_thumb.jpg").write_bytes(b"t" * 8)
    out = root / "home" / "media"
    chat_dir = out / hashlib.md5("c@chatroom".encode()).hexdigest() / "video" / mon

    def files_on_disk() -> int:
        return sum(1 for p in chat_dir.rglob("*") if p.is_file()) if chat_dir.is_dir() else 0

    capped = media.export_media(account, ["c@chatroom"], kinds=["video"],
                                months=[mon], out_dir=out, max_files=1)
    before = files_on_disk()
    assert (capped.video.ok, capped.video.thumb, capped.truncated) == (1, 0, True)

    resumed = media.export_media(account, ["c@chatroom"], kinds=["video"],
                                 months=[mon], out_dir=out, max_files=50)
    assert files_on_disk() - before == capped.video.remaining   # 报几，就真差几个
    assert resumed.video.remaining == 0                         # 续跑把账结清
    assert (resumed.video.ok, resumed.video.thumb) == (1, 0)    # 缩略图不该被冗余写一次


def test_export_media_image_rerun_skips_before_decrypting(tmp_path, monkeypatch):
    """image 路径的重跑：跳过已导出的，**且不再解密**。

    两条断言各守一件事：
    - `ok == 0 and skipped == N`：跳过确实生效（image 是三种媒体之一，此前零覆盖）
    - `decrypt_dat` 调用 **0 次**：「在解密之前跳过」是本任务的核心价值 —— 把 `already()`
      挪到 `decrypt_dat` 之后，上面那条计数断言**依然通过**，而收益归零。

    变异：把 image 的 `already()` 挪到 `decrypt_dat` 调用之后 → 解密计数 > 0 → 红。
    """
    root, account = make_tree(tmp_path, monkeypatch)
    code = "999999"
    (paths.CONTAINER / "app_data/net/kvcomm/key_999999_1_2_3_4_5_input.statistic").write_bytes(b"")
    cm = hashlib.md5("chatA@chatroom".encode()).hexdigest()
    aes = hashlib.md5((code + paths.bare_wxid(account)).encode()).hexdigest()[:16].encode()
    # 取样目录：derive_keys 要靠它统计 xorKey 并验证 aesKey（月份不在 want 里，不参与导出）
    sdir = paths.XWECHAT_FILES / account / "msg/attach" / cm / "2026-03" / "Img"
    sdir.mkdir(parents=True)
    samples = [sdir / f"sam{i}.dat" for i in range(4)]
    for p in samples:
        p.write_bytes(build_v2_dat(jpeg_like(64), b"\x33" * 8 + b"\xff\xd9", aes, FAKE_XOR))
    monkeypatch.setattr(media, "_attach_dats", lambda acct: iter(samples))
    img = paths.XWECHAT_FILES / account / "msg/attach" / cm / "2026-09" / "Img"
    img.mkdir(parents=True)
    for stem in ("aaaa", "bbbb", "cccc"):
        (img / f"{stem}.dat").write_bytes(
            build_v2_dat(jpeg_like(256), b"\x11" * 8 + b"\xff\xd9", aes, FAKE_XOR))
    out = root / "home" / "media"

    real = media.decrypt_dat
    sample_blobs = {p.read_bytes() for p in samples}
    decrypted: list[bytes] = []                      # 只记「解密图片」，样本验证那一次不算

    def counting(data, keys):
        if data not in sample_blobs:
            decrypted.append(data)
        return real(data, keys)

    monkeypatch.setattr(media, "decrypt_dat", counting)
    first = media.export_media(account, ["chatA@chatroom"], kinds=["image"],
                               months=["2026-09"], out_dir=out)
    assert (first.image.ok, first.image.skipped) == (3, 0)
    assert len(decrypted) == 3                       # 首轮三张图都真解了

    decrypted.clear()
    second = media.export_media(account, ["chatA@chatroom"], kinds=["image"],
                                months=["2026-09"], out_dir=out)
    assert (second.image.ok, second.image.skipped) == (0, 3)
    assert decrypted == []                           # 跳过发生在解密**之前**


# 真机样本（2026-09-22，已抹去 fromusername/tousername 与凭据）
_REAL_STICKER_XML = (
    '<msg><emoji fromusername="wxid_abc123def456" tousername="12345678@chatroom" '
    'type="2" idbuffer="media:0_0" md5="d0f46ed3ce8bac33df5f7055656d8bd9" len="52492" '
    'productid="com.tencent.xin.emoticon.person.stiker_15375600770c986d2d929ebfa8" '
    'cdnurl="http://wxapp.tc.qq.com/262/20304/stodownload?m=d0f46ed3&amp;filekey=SECRET" '
    'aeskey="deadbeefdeadbeefdeadbeefdeadbeef" width="240" height="240" '
    'emojiattr="CgbosKLosKI=" '
    'desc="Cg8KBXpoX2NuEgbosKLosKIKCQoFemhfdHcSAAoLCgdkZWZhdWx0EgA="></emoji>'
    '<gameext type="0" content="0"></gameext></msg>'
)


def test_extract_sticker_ref_reads_md5_name_and_pack():
    md5, name, pack = media.extract_sticker_ref(messages.TYPE_STICKER, _REAL_STICKER_XML)
    assert md5 == "d0f46ed3ce8bac33df5f7055656d8bd9"
    assert name == "谢谢"          # desc 的 zh_cn
    assert pack == "com.tencent.xin.emoticon.person.stiker_15375600770c986d2d929ebfa8"


def test_extract_sticker_ref_lowercases_md5():
    """md5 大小写不敏感 → 一律折成小写（与 extract_media_ref 的 xml_md5 同口径）。

    真机 md5 是小写，所以只有**大写**夹具钉得住 `.lower()`——全小写的夹具下删掉它
    照样全绿（自审变异实测）。
    """
    xml = '<msg><emoji md5="' + "AB12" * 8 + '"></emoji></msg>'
    assert media.extract_sticker_ref(messages.TYPE_STICKER, xml)[0] == "ab12" * 8


def test_extract_sticker_ref_falls_back_to_emojiattr():
    """desc 缺失时取 emojiattr；两者都缺 → name 为 None，**不是空串**。"""
    xml = '<msg><emoji md5="a' + "0" * 31 + '" emojiattr="CgbosKLosKI="></emoji></msg>'
    assert media.extract_sticker_ref(messages.TYPE_STICKER, xml)[1] == "谢谢"
    xml2 = '<msg><emoji md5="a' + "0" * 31 + '"></emoji></msg>'
    assert media.extract_sticker_ref(messages.TYPE_STICKER, xml2)[1] is None


# ── 以下 Base64 全部**程序生成并做过往返验证**，不要手敲 ──────────────────
#
# 生成器（三行，需要加新夹具时用它，不要凭想象拼字节）：
#   def _f(n, p): return bytes([(n << 3) | 2, len(p)]) + p
#   desc  = base64.b64encode(b"".join(_f(1, _f(1, l.encode()) + _f(2, t.encode()))
#                                      for l, t in pairs)).decode()
#   attr  = base64.b64encode(_f(1, text.encode())).decode()
#
# **为什么强调这个**：自审时手敲的两个 Base64 都错了——一个把「謝謝」打成了乱码
# 「空殚」，另一个把「好」编码了两遍。手敲的字节串肉眼看不出来，但夹具一旦编码了
# 现实里不存在的形态，代码与测试就会互相印证着一起错（本仓库最贵的那类缺陷）。
_DESC_ZH_TW_THEN_ZH_CN = "Cg8KBXpoX3R3Egboh7rngaMKDwoFemhfY24SBuiwouiwog=="   # {zh_tw:臺灣}{zh_cn:谢谢}
_DESC_ZH_CN_EMPTY = "CgkKBXpoX2NuEgAKDAoFemhfdHcSA+WlvQ=="                     # {zh_cn:""}{zh_tw:好}
_DESC_NO_CJK = "Cg4KBXpoX2NuEgVzbWlsZQ=="                                     # {zh_cn:"smile"}
_ATTR_JUST_TEXT = "CgbosKLosKI="                                              # emojiattr 形态：无语言


def test_extract_sticker_ref_prefers_zh_cn_over_other_languages():
    """desc 是多语言重复结构，取 zh_cn；**不取顺序上第一个**。

    夹具里 zh_tw 在**前**且取值不同（臺灣 / 谢谢），所以「取第一个中文串」的实现
    会返回「臺灣」而红。**这是本用例唯一的价值**——两个语言取同值的话它恒绿。
    """
    xml = ('<msg><emoji md5="a' + "0" * 31 + '" '
           f'desc="{_DESC_ZH_TW_THEN_ZH_CN}"></emoji></msg>')
    assert media.extract_sticker_ref(messages.TYPE_STICKER, xml)[1] == "谢谢"


def test_extract_sticker_ref_falls_back_to_another_language_when_zh_cn_empty():
    """zh_cn 为空串时，**允许**用整串里能找到的中文（可能是别的语言）。

    这是**有意**的：`{zh_cn: "", zh_tw: "好"}` 里的「好」仍然是个名字，比裸短 ID
    多出信息量，符合 D2 的降级链（有名给名字，无名给短 ID）。

    **本用例钉得住的**：zh_cn 为空时**不会**让整条返回 None（那样会白白丢掉
    一个可用的名字）。
    **本用例钉不住的**：「按块取」与「取 zh_cn 之后第一个中文串」在本夹具下给出
    同一个答案。两者在真实语料里的差别未被观测到，**不要**为它编一个用例——
    编出来的夹具会编码现实里不存在的形态。
    """
    xml = ('<msg><emoji md5="a' + "0" * 31 + '" '
           f'desc="{_DESC_ZH_CN_EMPTY}"></emoji></msg>')
    assert media.extract_sticker_ref(messages.TYPE_STICKER, xml)[1] == "好"


def test_extract_sticker_ref_returns_none_when_no_chinese():
    """有 desc 但里面没有中文（英文名）→ name 为 None，**不是英文名**。

    本设计只取中文名：短 ID 比一个英文标签更稳定（英文名会随语言包变）。
    """
    xml = ('<msg><emoji md5="a' + "0" * 31 + '" '
           f'desc="{_DESC_NO_CJK}"></emoji></msg>')
    assert media.extract_sticker_ref(messages.TYPE_STICKER, xml)[1] is None


@pytest.mark.parametrize("lt,content", [
    (messages.TYPE_TEXT, _REAL_STICKER_XML),      # 非贴纸类型 → None
    (messages.TYPE_STICKER, None),                # 无内容 → None
    (messages.TYPE_STICKER, ""),                  # 空串 → None
    (messages.TYPE_STICKER, "<msg><emoji></emoji></msg>"),   # 无 md5 → None
    (messages.TYPE_STICKER, "<msg><emoji md5=\"short\"></emoji></msg>"),  # md5 不是十六进制 → None
    # 上一条钉的是**字符集**（"short" 里一个 hex 字符都没有）；本条钉的是**长度**——
    # 31 位全是 hex，只有「必须恰好 32 位」才拦得住它。两条互为补集，都要留。
    (messages.TYPE_STICKER, "<msg><emoji md5=\"" + "a" * 31
                            + "\"></emoji></msg>"),   # md5 是 hex 但只有 31 位 → None
    (messages.TYPE_STICKER, "不是 XML"),           # 畸形 → None
    (messages.TYPE_STICKER, "<msg><emoji md5=\"" + "a" * 32
                            + "\" desc=\"!!!不是base64!!!\"></emoji></msg>"),  # 坏 Base64 → 名字 None，md5 仍在
])
def test_extract_sticker_ref_is_total(lt, content):
    """**载荷是对手可控的**：任何输入都不许抛。最后一条还要保住 md5——
    名字解不出只是少了个标签，不该把整条贴纸拖成 None。"""
    r = media.extract_sticker_ref(lt, content)
    if lt == messages.TYPE_STICKER and content and "a" * 32 in content:
        assert r is not None and r[0] == "a" * 32 and r[1] is None
    else:
        assert r is None


def test_resolve_sticker_prefers_persist_then_falls_back_to_thumb(tmp_path, monkeypatch):
    """三支都要有用例：命中 Persist / 只命中 Thumb / 都没有。"""
    md5 = "d0f46ed3ce8bac33df5f7055656d8bd9"
    # **账号那一层不能漏**：真机是 `XWECHAT_FILES/<account>/business/emoticon`。
    # plan 初稿这里写的是 `tmp_path / "business/emoticon"`，少了账号层——
    # 连它自己给的 `resolve_sticker` 实现都跑不过（实测）。
    root = tmp_path / "wxid_testacct01_ab12" / "business/emoticon"
    monkeypatch.setattr(paths, "XWECHAT_FILES", tmp_path)
    (root / "Persist" / md5[:2]).mkdir(parents=True)
    (root / "Thumb" / md5[:2]).mkdir(parents=True)
    (root / "Thumb" / md5[:2] / f"{md5}.thumb").write_bytes(b"x")
    p, thumb = media.resolve_sticker("wxid_testacct01_ab12", md5)
    assert p.name == f"{md5}.thumb" and thumb is True          # 只有 Thumb
    (root / "Persist" / md5[:2] / md5).write_bytes(b"y")
    p, thumb = media.resolve_sticker("wxid_testacct01_ab12", md5)
    assert p.name == md5 and thumb is False                    # Persist 优先
    assert media.resolve_sticker("wxid_testacct01_ab12", "f" * 32) is None


def test_out_candidates_sticker_uses_md5_stem():
    assert media._out_candidates("sticker", "d0f46ed3") == (
        ("d0f46ed3", "jpg"), ("d0f46ed3", "png"), ("d0f46ed3", "gif"),
        ("d0f46ed3", "webp"), ("d0f46ed3", "wxgf"))


def test_exported_path_sticker_uses_month_of_not_window(tmp_path):
    """**月份窗口是本任务最容易埋的雷**：贴纸的源不按月分区，写与探都只用 month_of。

    这条断言的判别法：把实现换成 `_month_window(ts)` 它会红——
    因为 ts 落在 2026-09 时，产物在 `sticker/2026-09/`，而 2026-08 目录不存在。
    """
    ts = 1780000000                                  # 2026-09
    mon = media.month_of(ts)
    md5 = "d0f46ed3ce8bac33df5f7055656d8bd9"
    assert media.exported_path(tmp_path, chat_md5="c" * 32, kind="sticker",
                               ts=ts, file_key=md5, local_id=1) is None
    (tmp_path / ("c" * 32) / "sticker" / mon).mkdir(parents=True)
    (tmp_path / ("c" * 32) / "sticker" / mon / f"{md5}.gif").write_bytes(b"x")
    p = media.exported_path(tmp_path, chat_md5="c" * 32, kind="sticker",
                            ts=ts, file_key=md5, local_id=1)
    assert p is not None and p.name == f"{md5}.gif"

    # **真正能判别的那一条**：只在**相邻月**放同名产物，必须仍然探不到。
    # 上面那条断言判别不了——`_month_window` 把当月排在**第一位**，换成它照样命中。
    # 用窗口的真实失败模式是**假阳性**：去 2026-08 捞到本该属于别的月的文件。
    # 判别法：把实现换成 `_month_window(ts)`，下面这一条立刻变红。
    adj = media._shift_month(mon, -1)
    d2 = tmp_path / ("c" * 32) / "sticker" / adj
    d2.mkdir(parents=True)
    (d2 / f"{md5}.png").write_bytes(b"x")
    (tmp_path / ("c" * 32) / "sticker" / mon / f"{md5}.gif").unlink()
    assert media.exported_path(tmp_path, chat_md5="c" * 32, kind="sticker",
                               ts=ts, file_key=md5, local_id=1) is None


# ---------- 贴纸 CDN 取件（T10） ----------

def _one_sticker_index(ts, md5, chat_id="wxid_x", account="wxid_testacct01_ab12"):
    """造一个只有一条贴纸引用的索引库，返回连接（`store.DB_PATH` 由 conftest 逐例隔离）。"""
    con = store.connect()
    cm = messages.md5_of(chat_id)
    row = messages.make_row(
        account=account, chat_md5=cm, chat_id=chat_id, shard="message_0.db",
        local_id=7, ts=ts, msg_type=messages.TYPE_STICKER, sender_id=chat_id,
        sender_name=None, is_self=0, content=messages.sticker_display(md5, None),
        media_hint="[sticker]")
    store.upsert_batch(con, [row])
    store.upsert_media_refs(con, [(row["msg_key"], "sticker", md5, None)])
    con.commit()
    return con


def test_sticker_export_sends_no_request_without_the_flag(tmp_path, monkeypatch):
    """**默认零网络**：不加 `with_cdn`，导出流程一次 CDN 取件都不调，且如实计 missing。

    与下面那条正向用例是**一对**。**打桩的是取件那一步（`_fetch_one` → `_fetch_cdn_bytes`）而不是
    `_cdn_opener`**：要钉的是「导出流程会不会走到联网那一步」。打更底层只能证明
    「这次没发请求」，证明不了「没走那条分支」——分支被误接通时它照样绿。
    """
    _, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    md5 = "d0f46ed3" + "0" * 24
    called = []
    monkeypatch.setattr(media, "sticker_cdn_urls",
                        lambda *a, **k: called.append("urls") or {})
    monkeypatch.setattr(media, "_fetch_cdn_bytes",
                        lambda *a, **k: called.append("bytes") or None)
    con = _one_sticker_index(ts, md5)
    try:
        rep = media.export_media(account, ["wxid_x"], kinds=["sticker"],
                                 out_dir=tmp_path / "media", con=con)
    finally:
        con.close()
    assert called == [], "没开 with_cdn 却去回源/取件了——默认零网络被破坏"
    assert rep.sticker.ok == 0
    assert rep.sticker.missing == 1


def test_sticker_export_calls_cdn_only_with_the_flag(tmp_path, monkeypatch):
    """开关**开着**时必须真的去调 CDN——没有这条，上面那条是假护栏。

    把接线整段删掉，上面那条照样绿（`calls == []` 恒成立）。这条一加，删接线立刻红。
    """
    _, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    md5 = "d0f46ed3" + "0" * 24
    asked, fetched = [], []
    cm_ = messages.md5_of("wxid_x")
    monkeypatch.setattr(media, "sticker_cdn_urls",
                        lambda acct, items, **k: (asked.append((acct, list(items)))
                                                  or {(cm_, "message_0.db", 7):
                                                      ("http://wxapp.tc.qq.com/x", md5)}))
    monkeypatch.setattr(media, "_fetch_cdn_bytes",
                        lambda url, expect, **k: fetched.append((url, expect)) or None)
    con = _one_sticker_index(ts, md5)
    try:
        rep = media.export_media(account, ["wxid_x"], kinds=["sticker"],
                                 out_dir=tmp_path / "media", con=con, with_cdn=True)
    finally:
        con.close()
    assert asked, "开了 with_cdn 却没回源——接线断了"
    # 回源要的三分量必须对得上（chat_md5|shard|local_id），否则查不到那一行
    assert asked[0] == (account, [(cm_, "message_0.db", 7)])
    assert fetched == [("http://wxapp.tc.qq.com/x", md5)]
    assert rep.sticker.failed == 1        # 桩返回 None → 如实计失败，不假装成功


def test_fetch_sticker_sends_no_request_without_cdnurl(tmp_path, monkeypatch):
    """XML 里没有 `cdnurl` → 返回 None，**一次请求都不发**。

    **打桩的是 `_cdn_opener`，不是 `urllib.request.urlopen`**：实现走
    `build_opener(...).open()`，那条路**不经过 `urlopen`**——打 `urlopen` 的桩只能证明
    「没调 urlopen」，把实现改成真的去联网它照样绿。
    """
    from agent_wechat import reader as reader_mod

    opened = []

    class _Boom:
        def open(self, *a, **k):
            opened.append(a)
            raise AssertionError("没有凭据却发起了请求")

    class _FakeCon:
        def execute(self, *a, **k): return self
        def fetchone(self): return (b"x", 0)
        def close(self): pass

    class _FakeReader:
        def __init__(self, account): pass
        def _open(self, rel):
            import contextlib

            @contextlib.contextmanager
            def cm():
                yield _FakeCon()
            return cm()

    monkeypatch.setattr(reader_mod, "MessageReader", _FakeReader)
    md5_ = "d0f46ed3" + "0" * 24
    monkeypatch.setattr(reader_mod, "_decode_content",
                        lambda *a: f'<msg><emoji md5="{md5_}" aeskey="{"b" * 32}"/></msg>')
    monkeypatch.setattr(media, "_cdn_opener", lambda: _Boom())
    assert _fetch_one("wxid_testacct01_ab12", "c" * 32, "message_0.db", 1) is None
    assert opened == []


def _fetch_one(account, cm, shard, lid):
    """EA-088：原 `fetch_sticker_from_cdn`（已删的零生产调用包装）的**等价两步**。

    断言面与实现面都不变：URL 构造（白名单 / 两种写法 / 空白容忍）在 `sticker_cdn_urls`，
    取件与异常吞并在 `_fetch_cdn_bytes`——包装删掉后直接打这两处。
    """
    from agent_wechat import media as m

    loc = (cm, shard, lid)
    urls = m.sticker_cdn_urls(account, [loc])
    if loc not in urls:
        return None
    url, expect_md5 = urls[loc]
    return m._fetch_cdn_bytes(url, expect_md5)


def test_fetch_sticker_swallows_any_opener_exception(tmp_path, monkeypatch):
    """opener 抛任何异常（含非 `OSError` 家族）都必须返回 None，**不逃出 core 层**。

    本仓库的硬契约是「core 层只抛项目异常」。按类型列举接不住
    `http.client.HTTPException`（`BadStatusLine` 等）与 `ssl.SSLError` ——
    **这条用例就是拿一个 `RuntimeError` 把它钉住**：把 except 收窄回
    `(HTTPError, URLError, TimeoutError, ConnectionError, OSError)`，它立刻变红。
    """
    from agent_wechat import reader as reader_mod

    class _Boom:
        def open(self, *a, **k):
            raise RuntimeError("模拟 http.client 家族之外的东西")

    class _FakeCon:
        def execute(self, *a, **k): return self
        def fetchone(self): return (b"x", 0)
        def close(self): pass

    class _FakeReader:
        def __init__(self, account): pass
        def _open(self, rel):
            import contextlib

            @contextlib.contextmanager
            def cm():
                yield _FakeCon()
            return cm()

    md5_ = "d0f46ed3" + "0" * 24
    monkeypatch.setattr(reader_mod, "MessageReader", _FakeReader)
    monkeypatch.setattr(reader_mod, "_decode_content",
                        lambda *a: f'<msg><emoji md5="{md5_}" '
                                  f'cdnurl="http://wxapp.tc.qq.com/x?filekey=deadbeef"/></msg>')
    monkeypatch.setattr(media, "_cdn_opener", lambda: _Boom())
    assert _fetch_one("wxid_testacct01_ab12", "c" * 32, "message_0.db", 1) is None


def test_fetch_sticker_refuses_host_outside_allowlist(tmp_path, monkeypatch):
    """白名单外的主机**一次请求都不发**——XML 是对手可控的，凭据不能被引向别处。"""
    from agent_wechat import reader as reader_mod

    opened = []

    class _Rec:
        def open(self, *a, **k):
            opened.append(a)
            raise AssertionError("白名单外的 host 却发起了请求")

    class _FakeCon:
        def execute(self, *a, **k): return self
        def fetchone(self): return (b"x", 0)
        def close(self): pass

    class _FakeReader:
        def __init__(self, account): pass
        def _open(self, rel):
            import contextlib

            @contextlib.contextmanager
            def cm():
                yield _FakeCon()
            return cm()

    md5_ = "d0f46ed3" + "0" * 24
    monkeypatch.setattr(reader_mod, "MessageReader", _FakeReader)
    monkeypatch.setattr(reader_mod, "_decode_content",
                        lambda *a: f'<msg><emoji md5="{md5_}" '
                                  f'cdnurl="http://evil.example.com/x?filekey=deadbeef"/></msg>')
    monkeypatch.setattr(media, "_cdn_opener", lambda: _Rec())
    assert _fetch_one("wxid_testacct01_ab12", "c" * 32, "message_0.db", 1) is None
    assert opened == []


@pytest.mark.parametrize("eq", ['="', ' = "', '= "', ' ="'])
def test_sticker_attrs_tolerate_whitespace_around_eq(eq):
    """属性与 `=` 之间的空白**因消息而异**，四种形态都必须解析出来。

    实测（2026-09-23，最新 400 条贴纸）：**193 条**把属性写成 `cdnurl = "…"`，
    而且**同一个 XML 内会混用**（`md5="…"` 紧写、`cdnurl = "…"` 带空格）。
    写死 `attr="` 会**静默漏掉 48%**（严格 163/400 vs 宽松 356/400）——
    本仓库最贵的那类缺陷：夹具编码了想象中的形态，代码与测试互相印证，全绿而功能为零。

    **判别法**：把 `_emoji_attr_re` 里的 `_ATTR_EQ` 换回 `"="`，本用例立刻变红。
    """
    md5 = "d0f46ed3" + "0" * 24
    # `eq` 已经含**开引号**（`="` / ` = "` / `= "` / ` ="`），值后面才补闭引号。
    xml = (f'<msg><emoji fromusername{eq}wxid_x" md5{eq}{md5}" '
           f'productid{eq}com.tencent.xin.emoticon.person.stiker_ab12"/></msg>')
    ref = media.extract_sticker_ref(messages.TYPE_STICKER, xml)
    assert ref is not None and ref[0] == md5
    assert ref[2] == "com.tencent.xin.emoticon.person.stiker_ab12"


@pytest.mark.parametrize("style", ["tight", "spaced"])
def test_fetch_sticker_reads_cdnurl_in_both_spellings(tmp_path, monkeypatch, style):
    """CDN 取件必须读得出**两种书写形态**的 `cdnurl`——否则近半数贴纸静默取不到件。

    与上面那条是同一件事的两个入口：解析（`extract_sticker_ref`）与取件
    （`sticker_cdn_urls`）。**两处必须共用 `_emoji_attr_re`**，各写一份就是两个真相源。
    """
    from agent_wechat import reader as reader_mod

    seen = []

    class _Rec:
        def open(self, url, **k):
            seen.append(url)

            class _R:
                status = 200
                headers: dict = {}

                def read(self_inner):
                    return b""

                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *a):
                    return False
            return _R()

    class _FakeCon:
        def execute(self, *a, **k): return self
        def fetchone(self): return (b"x", 0)
        def close(self): pass

    class _FakeReader:
        def __init__(self, account): pass
        def _open(self, rel):
            import contextlib

            @contextlib.contextmanager
            def cm():
                yield _FakeCon()
            return cm()

    md5_ = "d0f46ed3" + "0" * 24
    eq = '="' if style == "tight" else ' = "'
    # `eq` 已含开引号，值后面补闭引号（写重了会捕获到空串——本用例第一版就踩了）。
    xml = (f'<msg><emoji md5{eq}{md5_}" '
           f'cdnurl{eq}http://wxapp.tc.qq.com/x?filekey=deadbeef"/></msg>')
    monkeypatch.setattr(reader_mod, "MessageReader", _FakeReader)
    monkeypatch.setattr(reader_mod, "_decode_content", lambda *a: xml)
    monkeypatch.setattr(media, "_cdn_opener", lambda: _Rec())
    _fetch_one("wxid_testacct01_ab12", "c" * 32, "message_0.db", 1)
    assert seen == ["http://wxapp.tc.qq.com/x?filekey=deadbeef"], \
        f"{style} 形态的 cdnurl 没被读出来"


def test_sticker_export_fetches_a_repeated_sticker_only_once(tmp_path, monkeypatch):
    """同一个 md5 被发多次 → **只取一次件**，其余计 `skipped`。

    实测某会话 265 条贴纸只有 **109 个不同 md5（重复 58%）**：不去重就会白发 156 次请求，
    而这条路径的设计约束正是「少发请求」。**判别法**：把第一趟的 `queued` 去重删掉，
    本用例的 `fetched` 会变成 3（而不是 1）。
    """
    _, account = make_tree(tmp_path, monkeypatch)
    ts_ = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    md5 = "d0f46ed3" + "0" * 24

    con = store.connect()
    cm = messages.md5_of("wxid_x")
    rows = []
    for i in range(3):                      # 同一条贴纸发 3 次（local_id 不同）
        rows.append(messages.make_row(
            account=account, chat_md5=cm, chat_id="wxid_x", shard="message_0.db",
            local_id=7 + i, ts=ts_ + i, msg_type=messages.TYPE_STICKER,
            sender_id="wxid_x", sender_name=None, is_self=0,
            content=messages.sticker_display(md5, None), media_hint="[sticker]"))
    store.upsert_batch(con, rows)
    store.upsert_media_refs(con, [(r["msg_key"], "sticker", md5, None) for r in rows])
    con.commit()

    fetched = []
    monkeypatch.setattr(media, "sticker_cdn_urls", lambda acct, items, **k: {
        (cm, "message_0.db", 7 + i): ("http://wxapp.tc.qq.com/x", md5) for i in range(3)})
    monkeypatch.setattr(media, "_fetch_cdn_bytes",
                        lambda url, expect, **k: fetched.append(url) or None)
    try:
        rep = media.export_media(account, ["wxid_x"], kinds=["sticker"],
                                 out_dir=tmp_path / "media", con=con, with_cdn=True)
    finally:
        con.close()
    assert len(fetched) == 1, f"同一条贴纸取了 {len(fetched)} 次件——去重没生效"
    assert rep.sticker.skipped == 2         # 另两条走 skipped，不吃 limit 预算


def test_sticker_export_tries_other_messages_of_the_same_sticker(tmp_path, monkeypatch):
    """同一个 md5 的**首条**消息拿不到凭据时，必须试它的其它候选消息。

    实测有 7 个贴纸的首条消息是「商店表情包引用」形态（XML 里没有 `cdnurl`），
    而同 md5 的另一条消息**有**——只认首条会把可得件数从 **104 压到 97**。
    去重（按 md5 排一次队）与「多试候选」（不漏件）是两件事，都要成立。

    **判别法**：把第三趟改回只查 `cands[key][0]`，本用例立刻变红（一次都不取）。
    """
    _, account = make_tree(tmp_path, monkeypatch)
    ts_ = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    md5 = "d0f46ed3" + "0" * 24
    con = store.connect()
    cm = messages.md5_of("wxid_x")
    rows = [messages.make_row(
        account=account, chat_md5=cm, chat_id="wxid_x", shard="message_0.db",
        local_id=lid, ts=ts_ + lid, msg_type=messages.TYPE_STICKER, sender_id="wxid_x",
        sender_name=None, is_self=0, content=messages.sticker_display(md5, None),
        media_hint="[sticker]") for lid in (7, 8)]
    store.upsert_batch(con, rows)
    store.upsert_media_refs(con, [(r["msg_key"], "sticker", md5, None) for r in rows])
    con.commit()

    fetched = []
    # **只有第二条候选**（local_id=8）拿得到凭据——模拟首条是商店引用形态
    monkeypatch.setattr(media, "sticker_cdn_urls", lambda acct, items, **k: {
        (cm, "message_0.db", 8): ("http://wxapp.tc.qq.com/x", md5)})
    monkeypatch.setattr(media, "_fetch_cdn_bytes",
                        lambda url, expect, **k: fetched.append(url) or None)
    try:
        rep = media.export_media(account, ["wxid_x"], kinds=["sticker"],
                                 out_dir=tmp_path / "media", con=con, with_cdn=True)
    finally:
        con.close()
    assert len(fetched) == 1, "首条候选没凭据就放弃了，没有试同 md5 的其它消息"
    assert rep.sticker.ok == 0              # 桩返回 None → 如实计失败
    assert rep.sticker.failed == 1          # 仍然是**一个贴纸**失败，不是两个


def test_sticker_export_sources_credentials_from_another_month(tmp_path, monkeypatch):
    """凭据可以从**同会话、别的月份**的同 md5 消息取。

    `cdnurl` 是按**贴纸**（md5）走的，与它哪个月发过无关。实测某会话有 **5 个**贴纸栽在
    这里——它们本月那条消息的 XML 没有凭据、别月那条有，于是白丢 5 个（而那 5 个的响应体
    与 md5 都完全正常，手动取件 HTTP 200 且 md5 相符）。

    **判别法**：把候选池改回「只收同月」，下面的 `seen_locs` 里就不会出现 8 月那条的 loc，
    断言立刻变红。
    """
    _, account = make_tree(tmp_path, monkeypatch)
    md5 = "d0f46ed3" + "0" * 24
    sep = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    aug = int(datetime.datetime(2026, 8, 15, 12, 0).timestamp())

    con = store.connect()
    cm = messages.md5_of("wxid_x")
    rows = [
        messages.make_row(account=account, chat_md5=cm, chat_id="wxid_x",
                          shard="message_0.db", local_id=7, ts=sep,
                          msg_type=messages.TYPE_STICKER, sender_id="wxid_x",
                          sender_name=None, is_self=0,
                          content=messages.sticker_display(md5, None), media_hint="[sticker]"),
        messages.make_row(account=account, chat_md5=cm, chat_id="wxid_x",
                          shard="message_0.db", local_id=8, ts=aug,
                          msg_type=messages.TYPE_STICKER, sender_id="wxid_x",
                          sender_name=None, is_self=0,
                          content=messages.sticker_display(md5, None), media_hint="[sticker]"),
    ]
    store.upsert_batch(con, rows)
    store.upsert_media_refs(con, [(r["msg_key"], "sticker", md5, None) for r in rows])
    con.commit()

    seen_locs, fetched = [], []
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40      # detect_ext 认得的最小假 PNG

    def _fake_urls(acct, items, **k):
        seen_locs.extend(items)
        # **只有 8 月那条**（local_id=8）拿得到凭据
        return {(cm, "message_0.db", 8): ("http://wxapp.tc.qq.com/x", md5)}

    monkeypatch.setattr(media, "sticker_cdn_urls", _fake_urls)
    monkeypatch.setattr(media, "_fetch_cdn_bytes",
                        lambda url, expect, **k: fetched.append(url) or png)
    out = tmp_path / "media"
    try:
        rep = media.export_media(account, ["wxid_x"], kinds=["sticker"],
                                 out_dir=out, con=con, with_cdn=True)
    finally:
        con.close()
    assert (cm, "message_0.db", 8) in seen_locs, \
        "候选池只收了同月的消息——别月那条带凭据的消息没被交出去"
    # **同一个贴纸跨两个月 = 两个产物、两次取件**，这是 D4 明确接受的按会话/月去重；
    # 本条用例要钉的是「**9 月那一条也拿到了凭据**」，不是「只取一次」。
    assert len(fetched) == 2, f"应两个月各取一次件，实为 {len(fetched)}"
    assert rep.sticker.ok == 2, "9 月那条没有靠 8 月的候选拿到凭据"
    for mon in ("2026-09", "2026-08"):
        assert (out / cm / "sticker" / mon / f"{md5}.png").exists(), f"{mon} 没有产物"


@pytest.mark.parametrize("host", ["vweixinf.tc.qq.com", "wxapp.tc.qq.com",
                                  "mmbiz.qpic.cn", "snsvideo.c2c.wechat.com"])
def test_fetch_sticker_allows_every_observed_cdn_host(monkeypatch, host):
    """**实测出现的四种 CDN 主机都要放行**。

    白名单最初只按两个样本收成 `*.tc.qq.com`，真机于是静默挡掉 1.2%——
    `mmbiz.qpic.cn` 1.0% + `snsvideo.c2c.wechat.com` 0.1%。**白名单不能靠样本拍**。
    判别法：把 `_CDN_ALLOWED` 缩回 `(".tc.qq.com",)`，后两个参数立刻变红。
    """
    seen = []

    class _Rec:
        def open(self, url, **k):
            seen.append(url)

            class _R:
                status = 200
                headers: dict = {}

                def read(self_inner): return b""
                def __enter__(self_inner): return self_inner
                def __exit__(self_inner, *a): return False
            return _R()

    monkeypatch.setattr(media, "_cdn_opener", lambda: _Rec())
    media._fetch_cdn_bytes(f"http://{host}/x?filekey=deadbeef", "0" * 32)
    assert seen == [f"http://{host}/x?filekey=deadbeef"], f"{host} 被白名单挡了"


def _one_wxgf_sticker_index(ts, md5, chat_id="wxid_x", account="wxid_testacct01_ab12"):
    con = store.connect()
    cm = messages.md5_of(chat_id)
    row = messages.make_row(
        account=account, chat_md5=cm, chat_id=chat_id, shard="message_0.db",
        local_id=7, ts=ts, msg_type=messages.TYPE_STICKER, sender_id=chat_id,
        sender_name=None, is_self=0, content=messages.sticker_display(md5, None),
        media_hint="[sticker]")
    store.upsert_batch(con, [row])
    store.upsert_media_refs(con, [(row["msg_key"], "sticker", md5, None)])
    con.commit()
    return con, cm


WXGF_BLOB = b"wxgf" + b"\x00\x00\x00\x01" + b"\x00" * 16


def test_sticker_wxgf_transcodes_to_jpeg(tmp_path, monkeypatch):
    """CDN 返回 wxgf 贴纸时**必须转成 JPEG** —— wxgf 是容器，原样落盘用户打不开。

    这条分支在贴纸侧此前是**漏的**（`_wxgf_to_jpeg` 只接在图片分支上）：全库实测 0 个
    wxgf 贴纸所以从未被走到，一旦遇到会落一个 `file` 认不出、用户打不开的 .wxgf
    **且不报任何异常**。这里把成功的转码分支钉住（否则只有回退那条被覆盖）。

    **判别法**：删掉 `if ext == "wxgf": converted = _wxgf_to_jpeg(...)` 两行，本用例变红
    （产物会变成 .wxgf 而不是 .jpg）。
    """
    _, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    md5 = "d0f46ed3" + "0" * 24
    con, cm = _one_wxgf_sticker_index(ts, md5)
    monkeypatch.setattr(media, "sticker_cdn_urls",
                        lambda acct, items, **k: {(cm, "message_0.db", 7):
                                                  ("http://wxapp.tc.qq.com/x", md5)})
    monkeypatch.setattr(media, "_fetch_cdn_bytes",
                        lambda url, expect, **k: WXGF_BLOB)

    def _fake_transcode(data, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\xff\xd8\xff" + b"x" * 20)   # 假装 ffmpeg 落了 jpeg
        return True

    monkeypatch.setattr(media, "_wxgf_to_jpeg", _fake_transcode)
    out = tmp_path / "media"
    try:
        rep = media.export_media(account, ["wxid_x"], kinds=["sticker"],
                                 out_dir=out, con=con, with_cdn=True)
    finally:
        con.close()
    names = sorted(p.name for p in (out / cm / "sticker" / "2026-09").iterdir())
    assert names == [f"{md5}.jpg"], f"wxgf 没转成 jpeg：{names}"
    assert rep.sticker.ok == 1
    assert rep.degraded == []          # 转成功了就不该公告


def test_sticker_wxgf_without_transcode_saves_container_and_says_so(tmp_path, monkeypatch):
    """转码失败 → 存 .wxgf 原容器 + **公告一次**（与图片分支同款 once-flag），仍计 ok。

    **宁可留可恢复的原容器，也不丢数据**；但必须让用户知道这个文件打不开——
    静默落一个 .wxgf 就是「全绿而功能为零」。
    """
    _, account = make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    md5 = "d0f46ed3" + "0" * 24
    con, cm = _one_wxgf_sticker_index(ts, md5)
    monkeypatch.setattr(media, "sticker_cdn_urls",
                        lambda acct, items, **k: {(cm, "message_0.db", 7):
                                                  ("http://wxapp.tc.qq.com/x", md5)})
    monkeypatch.setattr(media, "_fetch_cdn_bytes",
                        lambda url, expect, **k: WXGF_BLOB)
    monkeypatch.setattr(media, "_wxgf_to_jpeg", lambda data, dest: False)   # 无 ffmpeg / 转码失败
    out = tmp_path / "media"
    try:
        rep = media.export_media(account, ["wxid_x"], kinds=["sticker"],
                                 out_dir=out, con=con, with_cdn=True)
    finally:
        con.close()
    names = sorted(p.name for p in (out / cm / "sticker" / "2026-09").iterdir())
    assert names == [f"{md5}.wxgf"]
    assert rep.sticker.ok == 1                       # 原容器保留了，算导出来了
    assert rep.degraded == ["wxgf 贴纸未转 JPEG（无 ffmpeg 或转码失败），存原容器"]

def test_derive_xor_key_reads_only_the_tail(tmp_path, monkeypatch):
    """EA-086：样本扫描只读每份 dat 的**末 2 字节**（此前是 `read_bytes()` 整文件读）。

    判别法：把 `Path.read_bytes` 换成会抛的桩——回退成整文件读，本用例必红。
    """
    import pathlib as _p

    from agent_wechat import media as m

    dats = []
    for i in range(5):
        p = tmp_path / f"{i}.dat"
        p.write_bytes(b"\x00" * 4096 + bytes([0x5A ^ 0xFF, 0x5A ^ 0xD9]))
        dats.append(p)

    def _boom(self):
        raise AssertionError("整文件读：EA-086 要的是尾部两字节")

    monkeypatch.setattr(_p.Path, "read_bytes", _boom)
    assert m._derive_xor_key(dats) == 0x5A
