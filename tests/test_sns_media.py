"""sns_media 纯函数层：ISAAC64 向量 / URL 构造 / 解密 / 下载。不发任何网络请求。"""
import urllib.error
import urllib.request

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


# ---------- fetch：注入假传输层，全程离线 ----------

class _Resp:
    def __init__(self, body, headers=None):
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Opener:
    """按序返回预置结果（bytes 或 Exception），并记录收到的请求。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def open(self, req, timeout=None):
        self.calls.append(req)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, tuple):
            return _Resp(r[0], r[1])
        return _Resp(r, {"x-enc": "1"})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(sns_media.time, "sleep", lambda _s: None)


def _enc(plain: bytes, seed: str) -> bytes:
    ks = sns_media.WxIsaac64(int(seed)).keystream(len(plain))
    return bytes(a ^ b for a, b in zip(plain, ks))


def test_fetch_decrypts_when_x_enc_is_1():
    plain = b"\xff\xd8\xff" + bytes(200)
    op = _Opener([_enc(plain, "3")])
    assert sns_media.fetch(_m(key="3"), opener=op) == plain


def test_fetch_returns_plaintext_when_not_encrypted():
    """x-enc 缺失 → 原样返回（不是所有响应都加密）。"""
    body = b"\xff\xd8\xffplain"
    op = _Opener([(body, {})])
    assert sns_media.fetch(_m(key="3"), opener=op) == body


def test_fetch_retries_then_succeeds():
    plain = b"\xff\xd8\xff" + bytes(50)
    op = _Opener([TimeoutError("x"), TimeoutError("y"), _enc(plain, "3")])
    assert sns_media.fetch(_m(key="3"), opener=op) == plain
    assert len(op.calls) == 3


def test_fetch_exhausts_attempts_raises():
    op = _Opener([TimeoutError()] * 3)
    with pytest.raises(sns_media.SnsMediaError):
        sns_media.fetch(_m(key="3"), opener=op)


def test_fetch_error_never_echoes_token():
    """假异常的文本里**真的带 URL/token**，断言才有牙。

    `str(TimeoutError())` 是空串：只锁异常类型的话，把消息退化成 `f"…：{last}"`
    （最现实的那种「改进」）依然会绿——已实证。
    """
    m = _m(token="SECRETTOKEN")
    url = sns_media.build_url(m)
    assert url and "SECRETTOKEN" in url           # 前提：URL 里确实有 token
    op = _Opener([urllib.error.URLError(f"<urlopen error {url}>")] * 3)
    with pytest.raises(sns_media.SnsMediaError) as ei:
        sns_media.fetch(m, opener=op)
    assert "SECRETTOKEN" not in str(ei.value)


def test_http_404_is_not_retried_and_names_the_real_cause():
    """EA-007：4xx 是**确定性失败**——重试三次既白等，又把根因改写成「下载失败（重试 3 次）」，
    用户会去查网络而不是去理解「这条媒体已经被服务端清理了」。条目：4xx（尤其 404/410）
    直接抛，文案与「网络失败」分开；硬约束「消息只含类型名」仍在（URL/token 不出现）。

    把 4xx 分支删掉（回到宽 except）本测试的前两条断言必红。
    """
    m = _m(token="SECRETTOKEN")
    url = sns_media.build_url(m)
    assert url and "SECRETTOKEN" in url           # 前提：URL 里确实有 token
    # 夹具照 `test_fetch_error_never_echoes_token` 的写法：异常的 url **就是**带 token 的请求
    # URL（真机形态）。此前夹具用的是不含 token 的假 URL，于是「回显 URL」的改法照样绿
    # （复审 N2 实测：把文案改成带上 `exc.url` 时全绿）——那条断言等于没牙。
    err = urllib.error.HTTPError(url, 404, "Not Found", None, None)
    op = _Opener([err, err, err])
    with pytest.raises(sns_media.SnsMediaError) as ei:
        sns_media.fetch(m, opener=op)
    assert len(op.calls) == 1, "4xx 不许重试"
    msg = str(ei.value)
    assert "404" in msg and "清理" in msg, msg
    assert "下载失败" not in msg, "确定性失败不许被说成网络失败"
    assert "SECRETTOKEN" not in msg and url not in msg, "凭据与 URL 都不许回显"


def test_a_decrypt_failure_is_not_retried_or_rewritten():
    """EA-007 的另一半：`decrypt_stream` 此前在重试的 `try` 内——解密失败（如 `key` 不是
    数字）被宽 `except` 吞掉重试 3 次，最后只剩「下载失败（重试 3 次）」，真正的原因
    （密钥形态）反而丢了。解密只依赖响应体与 XML，与网络无关，移出重试面。

    把 `decrypt_stream` 挪回 `try` 内本测试必红（请求数 3 + 文案变成「下载失败」）。
    """
    plain = b"\xff\xd8\xff" + bytes(200)
    op = _Opener([_enc(plain, "3")] * 3)
    with pytest.raises(sns_media.SnsMediaError) as ei:
        sns_media.fetch(_m(key="not-a-number"), opener=op)
    assert len(op.calls) == 1, "解密失败与网络无关，不许重试"
    msg = str(ei.value)
    assert "不是数字" in msg, msg
    assert "下载失败" not in msg, "根因不许被改写成网络失败"


def test_x_enc_header_without_any_key_is_not_silently_plaintext():
    """EA-055：响应头说「加密」（`x-enc: 1`）而 XML 既无 `key` 也无 `<enc>` 时，`fetch`
    经 `if m.key and (...)` 短路**把密文当明文返回**——与 `:251`「XML 说加密而无密钥」
    那条早退（请求**前**）不对称：同一族信号只有一半出声。

    把 `enc == "1" and not m.key` 那条分支删掉本测试必红（会静默返回密文）。
    """
    op = _Opener([(b"\x00" * 64, {"x-enc": "1"})])
    with pytest.raises(sns_media.SnsMediaError) as ei:
        sns_media.fetch(_m(key=None, encrypted=False), opener=op)
    # 钉**具体文案**：只断言「含『密钥』」不够——把分支短路后 `decrypt_stream(raw, None)`
    # 也会抛一条含「密钥」的消息（「媒体密钥不是数字」），断言就假绿了（变异 S3 实测）。
    assert "未给出密钥" in str(ei.value) and "enc@key" in str(ei.value), ei.value
    assert len(op.calls) == 1


def test_fetch_wires_video_so_only_the_head_is_decrypted():
    """EA-062：`fetch` 的 `video=` 接线零覆盖——没有任何用例让 `fetch` 拿到 KIND_VIDEO 且
    响应体 > 128KB，于是 `video=(m.kind == KIND_VIDEO)` 这个实参改错（或不改）都测不出来。

    夹具是「只有前 128KB 是密文、其后本就是明文」的真实视频形态（`decrypt_stream` 的
    docstring 写的就是这个）：`kind=KIND_VIDEO` → 只解头段 → 返回**原文**；同一响应体按
    `kind=KIND_IMAGE` 走（video=False，全份都当密文解）→ 尾部被 XOR 成乱码 → 不等于原文。
    两个方向都可分辨，`video=` 的任一取值错都必红。
    """
    head = sns_media.VIDEO_HEAD
    plain = _jpeg_plain() + bytes((i * 7 + 3) % 256 for i in range(300_000 - len(_jpeg_plain())))
    body = _enc(plain[:head], "7") + plain[head:]        # 只有头段加密

    op_video = _Opener([(body, {"x-enc": "1"})])
    assert sns_media.fetch(_m(kind=moments.KIND_VIDEO, key="7"), opener=op_video) == plain

    op_image = _Opener([(body, {"x-enc": "1"})])
    out = sns_media.fetch(_m(kind=moments.KIND_IMAGE, key="7"), opener=op_image)
    assert out != plain and out[:head] == plain[:head], "非视频形态全份都当密文解（尾部必然不同）"


def test_fetch_rejects_foreign_host_before_any_request():
    """恶意 XML 可能把 URL 指向任意主机——白名单必须在发请求之前拦住。

    Task 3 起「宿主不在白名单」在 `build_url` 就判为「不去取」（返回 `None`），
    于是 `fetch` 走的是**更早**的早退分支，消息里不再有域名。断言**两个真正的守卫**
    （`build_url` 返回 `None`、一个请求都没发），而不是消息文本——那是实现细节。
    """
    m = _m(url="http://evil.example.com/x.jpg", token="T")
    op = _Opener([])
    assert sns_media.build_url(m) is None
    with pytest.raises(sns_media.SnsMediaError):
        sns_media.fetch(m, opener=op)
    assert op.calls == []                      # 一个请求都没发


def test_fetch_without_token_raises_without_request():
    """断言的是**早退身份**，不是「反正都会抛 SnsMediaError」。

    删掉 `build_url(m) is None` 早退分支后，`url=None` 会一路走到域检查
    （`host_allowed(None)` 为假）而**照样不发请求、照样抛 SnsMediaError**——
    只断言异常类型 + `op.calls == []` 对这个变异是假绿（已实证）。
    """
    op = _Opener([])
    with pytest.raises(sns_media.SnsMediaError) as ei:
        sns_media.fetch(_m(token=None), opener=op)
    assert "无法下载" in str(ei.value)      # 早退那条消息，而非重试耗尽那条
    assert op.calls == []


def test_fetch_rejects_plaintext_url_without_request():
    """第一跳也必须是 https，`build_url` 挡不住这一路：它只替换**小写**的 `http://`，
    而 `HTTP://…` 原样过关、urllib 又把 scheme 归一成小写——域名白名单对此完全无感，
    于是 token 跟着明文出去。

    变异：删掉 `fetch` 里那段 scheme 检查 → 请求被发出（`op.calls` 非空）→ 红。
    """
    m = _m(url="HTTP://shmmsns.qpic.cn/sns/u/150")
    assert sns_media.build_url(m).startswith("HTTP://")   # 前提：确实没被归一化
    op = _Opener([])
    with pytest.raises(sns_media.SnsMediaError) as ei:
        sns_media.fetch(m, opener=op)
    assert "https" in str(ei.value)
    assert op.calls == []                                 # 一个请求都没发


def test_fetch_rejects_encrypted_without_key_before_request():
    """`<enc>` 文本 '1' 却没有 `enc@key`：XML 自相矛盾，必须**在发请求之前**出声。

    不能放行——放行会靠 `if m.key and (...)` 短路成「返回密文」，落盘后 `_sniff_ext`
    判不出来，最终报成「解密后不是已知图片格式」，与「密钥错」混为一谈。

    断言的是**早退身份**（那条具体消息）而非仅异常类型：删掉这条早退后，
    `m.key` 为 None 会让 `if m.key and (...)` 短路、**照样返回密文且不抛**，
    只写 `pytest.raises(SnsMediaError)` 对这个变异是假绿。
    """
    m = _m(key=None, encrypted=True)
    op = _Opener([b"ciphertext"])
    with pytest.raises(sns_media.SnsMediaError) as ei:
        sns_media.fetch(m, opener=op)
    assert "enc@key" in str(ei.value)                     # 早退那条消息，而非重试耗尽那条
    assert op.calls == []                                 # 判据只看 XML，请求不必发


def test_https_allowed_requires_both():
    assert sns_media.https_allowed("https://shmmsns.qpic.cn/x")
    assert not sns_media.https_allowed("http://shmmsns.qpic.cn/x")
    assert not sns_media.https_allowed("https://evil.example.com/x")


def test_default_opener_ignores_proxies(monkeypatch):
    """实测走本机代理反而超时，设计取直连。`ProxyHandler({})` 不是写错了——
    它因不暴露 *_open 方法会被 OpenerDirector 丢弃，而**传了 handler** 又让
    `build_opener` 跳过安装默认的 `ProxyHandler(getproxies())`；净效果正是「无代理处理」。

    **必须打桩 `getproxies`，不能靠运行环境**：
    - macOS 上 `getproxies()` 只读系统设置、**完全忽略 `http_proxy` 等环境变量**，
      所以设环境变量在 mac 上改不动它；
    - 而一台根本没配代理的机器（CI、同事的机器）上它返回 `{}`，`build_opener()` 装的
      `ProxyHandler({})` 同样被静默丢弃 —— 于是「简化成 `build_opener()`」**也能通过**。
    打桩成非空之后，那个简化才会真的装上一个 ProxyHandler，断言才咬得住（已变异验证）。
    """
    monkeypatch.setattr(urllib.request, "getproxies",
                        lambda: {"http": "http://127.0.0.1:7897",
                                 "https": "http://127.0.0.1:7897"})
    op = sns_media._default_opener()
    assert not any(isinstance(h, urllib.request.ProxyHandler) for h in op.handlers)


def test_default_opener_uses_allowlisted_redirect():
    """必须装 `_AllowlistedRedirect`，且**不得**是默认的 `HTTPRedirectHandler`。

    `build_opener` 见到 `_AllowlistedRedirect`（默认 handler 的子类）会跳过安装默认那个。
    一旦简化成 `build_opener()`，装回来的就是**无条件跟随重定向**的默认 handler——
    而 `_AllowlistedRedirect` 这个类本身还在、它的单测也照样绿，**只是没人把它接上去**，
    于是白名单只管得住第一跳。故这里断言的是「接线」，不是它的行为。
    """
    op = sns_media._default_opener()
    assert any(isinstance(h, sns_media._AllowlistedRedirect) for h in op.handlers)
    # 精确类型比对：`_AllowlistedRedirect` 是 HTTPRedirectHandler 的子类，isinstance 会误判
    assert not any(type(h) is urllib.request.HTTPRedirectHandler for h in op.handlers)


# ---------- 重定向：白名单必须跨跳成立 ----------

class _Buf:
    """假响应：只提供 urllib 重定向链真正会碰到的几个面。"""

    def __init__(self, body=b"", headers=None, code=200, msg="OK"):
        self._body = body
        self.headers = headers or {}
        self.code = code
        self.msg = msg

    def info(self):
        return self.headers

    def read(self):
        return self._body

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Transport(urllib.request.BaseHandler):
    """假传输层：按序发牌并记录**真实发出**的请求（含重定向的第二跳）。"""

    handler_order = 100          # 必须排在 HTTPHandler 的 500 之前，否则会拨真网络

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def _take(self, req):
        self.calls.append(req)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def http_open(self, req):
        return self._take(req)

    def https_open(self, req):
        return self._take(req)


def _redirect_opener(*results):
    """真 `_default_opener()`（真重定向处理链）+ 假传输层。"""
    op = sns_media._default_opener()
    t = _Transport(list(results))
    op.add_handler(t)
    return op, t


def test_redirect_to_http_is_refused():
    """白名单只查 host 不查 scheme：302 到 `http://shmmsns.qpic.cn/…`——**同一个域**、
    host 白名单完全满意——会被照常跟随，第二跳是**明文**，而这条请求带着 token。

    变异：把 `https_allowed` 换回 `host_allowed` → 第二跳被发出（`len(t.calls) == 2`）→ 红。
    """
    op, t = _redirect_opener(
        _Buf(headers={"location": "http://shmmsns.qpic.cn/sns/yyy/0?token=TOK&idx=1"},
             code=302, msg="Found"),
        _Buf(body=b"\xff\xd8\xffok"))
    with pytest.raises(sns_media.SnsMediaError) as ei:
        sns_media.fetch(_m(), opener=op, attempts=1)
    assert len(t.calls) == 1                    # 明文那一跳根本没发出去
    assert "HTTPError" in str(ei.value)         # 拒的是重定向，不是别的岔路报错


def test_redirect_to_foreign_host_is_refused():
    """白名单只查第一跳等于没查：urllib 默认跟随 302 且没人复查 `geturl()`，
    于是 CDN 上一个重定向就能把请求带去任意主机。"""
    first = sns_media.build_url(_m())
    op, t = _redirect_opener(
        _Buf(headers={"location": "https://evil.example.com/steal.jpg"},
             code=302, msg="Found"),
        _Buf(body=b"PWNED"))
    with pytest.raises(sns_media.SnsMediaError) as ei:
        sns_media.fetch(_m(), opener=op, attempts=1)
    assert [r.full_url for r in t.calls] == [first]   # 只有第一跳，第三方一个请求都没发
    assert "HTTPError" in str(ei.value)               # 拒的是重定向，不是别的岔路报错
    assert "evil.example.com" not in str(ei.value)    # 目标主机也不回显


def test_redirect_to_same_host_is_followed():
    """正对照：同主机 302 照常跟随。

    没有它，「跨主机被拒」可能只是链路压根没走到重定向（比如假响应缺了某个属性
    导致 AttributeError）——那种情况下 `t.calls` 同样只有一条。
    """
    op, t = _redirect_opener(
        _Buf(headers={"location": "/sns/yyy/0?token=TOK&idx=1"},
             code=302, msg="Found"),
        _Buf(body=b"\xff\xd8\xffok"))
    assert sns_media.fetch(_m(), opener=op, attempts=1) == b"\xff\xd8\xffok"
    assert len(t.calls) == 2
    assert t.calls[1].full_url == "https://shmmsns.qpic.cn/sns/yyy/0?token=TOK&idx=1"


def test_host_allowed_survives_malformed_url():
    """host_allowed 是既有的裸 urlsplit 调用点——畸形输入不得抛。"""
    assert sns_media.host_allowed("https://[::1/x") is False
    assert sns_media.https_allowed("https://[::1/x") is False


def test_fetch_rejects_malformed_url_without_leaking_valueerror():
    """畸形 URL 走到 fetch 的拒绝分支时，抛的是 SnsMediaError，不是裸 ValueError。

    这道分支的取 host 一度是裸 `urlsplit`——而它执行的充要条件恰恰是
    `host_allowed` 为假，于是畸形输入必然在这里抛 `ValueError: Invalid IPv6 URL`，
    穿透 usecases/cli 打 traceback。

    变异：把 `host = moments.safe_host(url) or "?"` 改回
    `urllib.parse.urlsplit(url).hostname or "?"` → 本条红。

    拒绝早于 opener 构造，故一个请求都不会发。
    """
    op = _Opener([])
    with pytest.raises(sns_media.SnsMediaError):
        sns_media.fetch(_m(url="https://[::1/x", token="T"), opener=op)
    assert op.calls == []


def test_build_url_ready_returns_url_verbatim():
    """AUTH_READY 原样返回，**不拼 idx=1、不做尺寸段归一**。

    逐字符断言：`type=7` 的路径尾段本就是 /0、视频号 thumbUrl 无尺寸段——
    任何加工都是多余的，且会让签名失效。
    """
    m = _m(auth=moments.AUTH_READY, token=None, key=None,
           url="http://szmmsns.qpic.cn/mmsns/XYZ/0")
    assert sns_media.build_url(m) == "https://szmmsns.qpic.cn/mmsns/XYZ/0"


def test_build_url_ready_keeps_query_untouched():
    """查询串里的签名参数一个都不能动、不能重排。"""
    m = _m(auth=moments.AUTH_READY, token=None, key=None,
           url="http://shzjwxsns.video.qq.com/102/20202/snsvideodownload?encfilekey=E&token=T&idx=1")
    assert sns_media.build_url(m) == (
        "https://shzjwxsns.video.qq.com/102/20202/snsvideodownload?encfilekey=E&token=T&idx=1")


def test_build_url_none_when_host_not_allowlisted():
    """白名单外的主机返回 None——「项目不会去取它」，不是「取失败」。

    不这样做，usecases 会把它们计进 media_failed（fetch 的 host_allowed 在重试循环
    之前抛），而真相是「不去取」。真机上有四类会踩到：第三方网页地址 54 条、
    vweixinthumb 封面 158 条、mmsns.c2c.wechat.com 34 条、wx.qlogo.cn 1 条
    （**后两类共 35 条是既有的误计**，不是本期引入的，本期一并修）。
    """
    for bad in ("https://y.music.163.com/a.jpg", "https://vweixinthumb.tc.qq.com/a",
                "https://mmsns.c2c.wechat.com/a", "https://wx.qlogo.cn/a",
                "https://b23.tv/x"):
        m = _m(auth=moments.AUTH_READY, token=None, key=None, url=bad)
        assert sns_media.build_url(m) is None, bad


def test_allowlist_exact_host_not_subtree():
    """wxapp.tc.qq.com 是**精确主机**，不是子树。"""
    assert sns_media.host_allowed("https://wxapp.tc.qq.com/a") is True
    assert sns_media.host_allowed("https://evil.wxapp.tc.qq.com/a") is False
    assert sns_media.host_allowed("https://vweixinthumb.tc.qq.com/a") is False
    assert sns_media.host_allowed("https://tc.qq.com/a") is False
    assert sns_media.host_allowed("https://wxapp.tc.qq.com./a") is False
    assert sns_media.host_allowed("https://wxapp.tc.qq.com.evil.net/a") is False
    # 既有后缀仍然覆盖子域
    assert sns_media.host_allowed("https://finder.video.qq.com/a") is True
    assert sns_media.host_allowed("https://a.b.qpic.cn/a") is True
    # EA-061：后缀判据的反例面——「后缀」必须落在**标签边界**上。三条都是
    # 「白名单域名 + 攻击者后缀」的经典绕过形态，任何把 endswith 放宽成 `in`、
    # 或把后缀写成裸拼接的改法都会在这里红（改 `endswith` 为 `in` 已实测红）。
    assert sns_media.host_allowed("https://x.qpic.cn.evil.com/a") is False
    assert sns_media.host_allowed("https://evil.qpic.cn.attacker.net/a") is False
    assert sns_media.host_allowed("https://x.video.qq.com.attacker.net/a") is False
    assert sns_media.host_allowed("https://qpic.cn/a") is False          # 裸域不是子域
    # 末尾点（SECURITY §1 把它写成承诺「末尾带点（qpic.cn.）被拒」——补断言把它钉住，
    # 否则将来有人先 rstrip(".") 再判，承诺就静默失效）
    assert sns_media.host_allowed("https://shmmsns.qpic.cn./a") is False
    assert sns_media.host_allowed("https://wxapp.tc.qq.com./a") is False


def _jpeg_plain() -> bytes:
    return bytes.fromhex("ffd8ffe000104a46494600010100000100010000") + b"\x00" * 4080


def _isaac_wrap(plain: bytes, seed: str) -> bytes:
    """用项目的 WxIsaac64 **加密**（流密码自反）——三元组完全合成，不碰真机材料。"""
    ks = sns_media.WxIsaac64(int(seed)).keystream(len(plain))
    return bytes(a ^ b for a, b in zip(plain, ks))


class _FakeResp:
    def __init__(self, body: bytes, headers: dict[str, str]):
        self._body, self.headers = body, headers
    def read(self) -> bytes:
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _FakeOpener:
    def __init__(self, body: bytes, headers: dict[str, str]):
        self._r = _FakeResp(body, headers)
    def open(self, req, timeout=None):
        return self._r


def test_fetch_decrypts_when_xml_says_encrypted_even_without_header():
    """**本期最重要的一条**：type=15 的响应**没有 `x-enc` 头**，但内容是密文。

    只看响应头会静默存下 9MB 损坏视频。真机实测：不解密 → `moov atom not found`；
    只解前 128KB → ffmpeg 全解码零错误。
    """
    plain = _jpeg_plain()
    m = _m(auth=moments.AUTH_READY, token=None, key="20260918", encrypted=True,
           url="https://shzjwxsns.video.qq.com/x?token=T")
    got = sns_media.fetch(m, opener=_FakeOpener(_isaac_wrap(plain, "20260918"), {}),
                          attempts=1)
    assert got == plain


def test_fetch_does_not_decrypt_when_xml_says_plain():
    """XML 说没加密、头也没有 → 原样返回（`type=7` 的形态）。"""
    plain = _jpeg_plain()
    m = _m(auth=moments.AUTH_READY, token=None, key=None, encrypted=False,
           url="https://szmmsns.qpic.cn/mmsns/XYZ/0")
    got = sns_media.fetch(m, opener=_FakeOpener(plain, {}), attempts=1)
    assert got == plain


def test_fetch_still_honours_xenc_header():
    """老路径：XML 文本是 '0'，靠响应头决定解密（真机 4775 条是这样）。"""
    plain = _jpeg_plain()
    m = _m(key="1234567890", encrypted=False,
           url="http://shmmsns.qpic.cn/mmsns/ABC/0")
    got = sns_media.fetch(m, opener=_FakeOpener(_isaac_wrap(plain, "1234567890"),
                                                {"x-enc": "1"}), attempts=1)
    assert got == plain
