"""sns_media 纯函数层：ISAAC64 向量 / URL 构造 / 解密。不发任何网络请求。"""
import pytest

from agent_wechat import errors, moments, sns_media


# 已知向量：由本机参考实现（微信客户端的 wasm）在 seed=1 下产出，2026-09-15 实测
# 注意——这是**独立来源**的向量，不是我们自己实现算出来的，能真正锁死算法。
VEC_SEED1 = bytes.fromhex(
    "e19ed5d2ca98af2da7a18d07cab39b52a0ab0232d180af1472b36dcf1af5e46f")


def test_isaac64_matches_known_vector():
    assert sns_media.WxIsaac64(1).keystream(32) == VEC_SEED1


def test_isaac64_differs_from_common_golden_constant():
    """防回归：微信用 0x…7C13，公开实现多用 0x…7C15。
    有人「顺手改回常见值」会让全部解密静默产出乱码——这条测试必须红。"""
    class Common(sns_media.WxIsaac64):
        pass
    saved = sns_media.GOLDEN
    try:
        sns_media.GOLDEN = 0x9E3779B97F4A7C15
        wrong = sns_media.WxIsaac64(1).keystream(32)
    finally:
        sns_media.GOLDEN = saved
    assert wrong != VEC_SEED1


def test_isaac64_repr_hides_seed():
    assert "12345" not in repr(sns_media.WxIsaac64(12345))


def test_keystream_is_prefix_consistent():
    """流密码性质：取更长前缀与取短的一致。"""
    a = sns_media.WxIsaac64(42).keystream(64)
    b = sns_media.WxIsaac64(42).keystream(16)
    assert a[:16] == b


def _m(**kw):
    base = dict(kind=moments.KIND_IMAGE, url="http://shmmsns.qpic.cn/sns/xxx/150",
                token="TOK", key="12345678901234567890", enc_idx="1")
    base.update(kw)
    return moments.SnsMedia(**base)


def test_build_url_image():
    u = sns_media.build_url(_m())
    assert u == "https://shmmsns.qpic.cn/sns/xxx/0?token=TOK&idx=1"


def test_build_url_video_puts_token_first():
    u = sns_media.build_url(_m(kind=moments.KIND_VIDEO,
                               url="http://shzjwxsns.video.qq.com/1/2/v.f10002.mp4?encfilekey=K&bizid=1",
                               token="TOK"))
    assert u == ("https://shzjwxsns.video.qq.com/1/2/v.f10002.mp4"
                 "?token=TOK&idx=1&encfilekey=K&bizid=1")


def test_build_url_none_without_token():
    assert sns_media.build_url(_m(token=None)) is None
    assert sns_media.build_url(_m(url=None)) is None


def test_host_allowed():
    assert sns_media.host_allowed("https://shmmsns.qpic.cn/x")
    assert sns_media.host_allowed("https://shzjwxsns.video.qq.com/x")
    assert not sns_media.host_allowed("https://evil.example.com/x")
    assert not sns_media.host_allowed("https://qpic.cn.evil.com/x")


def test_decrypt_stream_roundtrip():
    """测试内自造密文：明文 XOR keystream → decrypt_stream 应还原。"""
    plain = bytes(range(256)) * 4
    ks = sns_media.WxIsaac64(7).keystream(len(plain))
    enc = bytes(a ^ b for a, b in zip(plain, ks))
    assert sns_media.decrypt_stream(enc, "7") == plain


def test_decrypt_stream_video_only_first_128k():
    """视频只加密前 128KB：其后必须是原样返回。"""
    n = sns_media.VIDEO_HEAD + 100
    body = bytes(n)
    ks = sns_media.WxIsaac64(9).keystream(sns_media.VIDEO_HEAD)
    enc = bytes(a ^ b for a, b in zip(body[:sns_media.VIDEO_HEAD], ks)) + body[sns_media.VIDEO_HEAD:]
    out = sns_media.decrypt_stream(enc, "9", video=True)
    assert out == body


def test_decrypt_stream_bad_key_raises_without_echoing_key():
    with pytest.raises(sns_media.SnsMediaError) as ei:
        sns_media.decrypt_stream(b"data", "not-a-number")
    assert "not-a-number" not in str(ei.value)
    assert isinstance(ei.value, errors.AgentWeChatError)
