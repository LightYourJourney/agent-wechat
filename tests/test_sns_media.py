"""sns_media 纯函数层：ISAAC64 向量 / URL 构造 / 解密。不发任何网络请求。"""
import pytest

from agent_wechat import errors, moments, sns_media


# 已知向量：由本机参考实现（微信客户端的 wasm）在 seed=1 下产出，2026-09-15 实测
# 注意——这是**独立来源**的向量，不是我们自己实现算出来的，能真正锁死算法。
VEC_SEED1 = bytes.fromhex(
    "e19ed5d2ca98af2da7a18d07cab39b52a0ab0232d180af1472b36dcf1af5e46f")


def test_isaac64_matches_known_vector():
    assert sns_media.WxIsaac64(1).keystream(32) == VEC_SEED1


def test_golden_constant_is_wechat_variant():
    """防回归：微信用的是 0x…7C13，公开实现多用 0x…7C15，一个字符之差解出乱码。

    必须**直锁常量**——「改常量再看输出」那种写法在常量本身被改错时会退化成空操作
    （`saved` 拿到的已是被改错的值，patch 无事发生，输出仍 ≠ VEC 而假绿），是假护栏。
    """
    assert sns_media.GOLDEN == 0x9E3779B97F4A7C13
    # 第二半仍保留：它证明「常量确实驱动输出」（即向量测试的失败模式真实存在），
    # 以及 monkeypatch 通道有效——但判别力来自上面那行，不是这半。
    saved = sns_media.GOLDEN
    try:
        sns_media.GOLDEN = 0x9E3779B97F4A7C15
        assert sns_media.WxIsaac64(1).keystream(32) != VEC_SEED1
    finally:
        sns_media.GOLDEN = saved


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


def test_build_url_none_for_other_kind():
    """未分类类型（type 3/4/0）的语义未实测——不猜 URL 形态，一律标为不可下载。
    url 与 token 都给全，确保这里的 None 出自 kind 判据，而非缺字段的早退分支。"""
    m = _m(kind=moments.KIND_OTHER)
    assert m.url and m.token
    assert sns_media.build_url(m) is None
    # 白名单语义：将来新增的未知 kind 同样被拒，而不是静默落进图片分支
    assert sns_media.build_url(_m(kind="weird")) is None


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
