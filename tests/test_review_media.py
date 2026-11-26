"""全量审查（媒体 / 导出页 / 工作台）修复的回归测试。合成数据、全程离线、不碰真机。"""
from __future__ import annotations

import datetime
import hashlib
import http.client
import io
import random
import socket
import threading

import pytest

import test_chatview as tcv
import test_media as tm
import test_sns_media as tsns
import test_webui as tw
from agent_wechat import (appmsg, chat_html, chat_index, chat_md, chat_text, chatview,
                          media, paths, sns_media, webui)
from agent_wechat.usecases import MediaRefState


# ---------------------------------------------------------------- 1. 工作台访问令牌

def _raw(port: int, path: str, headers: dict | None = None, host: str = "127.0.0.1"):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    res = conn.getresponse()
    body = res.read()
    conn.close()
    return res, body


def test_webui_requires_the_access_token_everywhere(srv):
    """首页曾把 CSRF 令牌嵌进 HTML、谁 GET `/` 都给：本机任何账户/进程都能读全部聊天。"""
    for path in ("/", "/app.js", "/style.css", "/?token=wrong"):
        res, body = _raw(srv.port, path)
        assert res.status == 403, path
        assert srv.csrf.encode() not in body, path
    res, body = _raw(srv.port, "/")
    assert "终端打印的链接" in body.decode()
    # 拿到 CSRF 也不够：没有访问令牌照样 403
    res, _ = _raw(srv.port, "/api/status", {"X-AW-CSRF": srv.csrf})
    assert res.status == 403
    res, _ = _raw(srv.port, "/api/status", {"X-AW-CSRF": srv.csrf,
                                             "Cookie": f"aw_token_{srv.port}=wrong"})
    assert res.status == 403


def test_webui_token_link_sets_cookie_and_redirects(srv):
    res, _ = _raw(srv.port, f"/?token={srv.token}")
    assert res.status == 302 and res.getheader("Location") == "/"
    cookie = res.getheader("Set-Cookie") or ""
    assert cookie.startswith(f"aw_token_{srv.port}={srv.token};")
    for attr in ("HttpOnly", "SameSite=Strict", "Path=/"):
        assert attr in cookie, attr
    jar = {"Cookie": cookie.split(";")[0]}
    res, body = _raw(srv.port, "/", jar)
    assert res.status == 200 and srv.csrf.encode() in body
    assert _raw(srv.port, "/app.js", jar)[0].status == 200
    res, _ = _raw(srv.port, "/api/status", {**jar, "X-AW-CSRF": srv.csrf})
    assert res.status == 200
    # 有 cookie 仍要 CSRF 头（防跨站）
    assert _raw(srv.port, "/api/status", jar)[0].status == 403


def test_webui_post_requires_the_access_token(srv):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    conn.request("POST", "/api/derived/delete", body=b'{"derivation_id": "x"}',
                 headers={"Origin": f"http://127.0.0.1:{srv.port}", "X-AW-CSRF": srv.csrf,
                          "Content-Type": "application/json"})
    res = conn.getresponse()
    res.read()
    conn.close()
    assert res.status == 403


def test_serve_prints_and_opens_the_token_link(tmp_path, monkeypatch, capsys):
    tw._env(tmp_path, monkeypatch, seed=False)
    made = []
    real = webui.create_server

    def fake_create(host, port):
        h = real(host, 0)
        made.append(h)

        def stop():
            raise KeyboardInterrupt
        h.serve_forever = stop                     # type: ignore[method-assign]
        return h
    monkeypatch.setattr(webui, "create_server", fake_create)
    import webbrowser
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    webui.serve(0, open_browser=True)
    h = made[0]
    url = f"http://127.0.0.1:{h.server_address[1]}/?token={h.aw_state.token}"  # type: ignore[attr-defined]
    assert url in capsys.readouterr().out
    assert opened == [url]


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    tw._env(tmp_path, monkeypatch)
    httpd = webui.create_server("127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        st = httpd.aw_state                       # type: ignore[attr-defined]
        yield tw.Simple(port=httpd.server_address[1], csrf=st.csrf, token=st.token, tmp=tmp_path)
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------- 2. ::1

def test_webui_can_listen_on_ipv6_loopback(tmp_path, monkeypatch):
    """hint 建议 `::1`，而 ThreadingHTTPServer 默认只 IPv4（bind `::1` 抛 gaierror）。"""
    if not socket.has_ipv6:
        pytest.skip("本机没有 IPv6")
    tw._env(tmp_path, monkeypatch, seed=False)
    httpd = webui.create_server("::1", 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        port, tok = httpd.server_address[1], httpd.aw_state.token  # type: ignore[attr-defined]
        res, _ = _raw(port, "/", {"Cookie": f"aw_token_{port}={tok}"}, host="::1")
        assert res.status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------- 3. 索引页

def _entry(md5: str) -> chat_index.ChatIndexEntry:
    return chat_index.ChatIndexEntry(chat_md5=md5, chat_id="c", label=md5, messages=1,
                                     has_md=True, has_html=True)


def test_chat_index_skips_non_hex_dirs_and_has_csp():
    good = "0123456789abcdef0123456789abcdef"
    page = chat_index.render([_entry("javascript:alert(1)"), _entry("../x"), _entry(good)])
    assert "javascript:" not in page and "../x" not in page
    assert f'href="{good}/chat/index.html"' in page
    assert chat_text.CSP_META in page
    assert "共 <b>1</b> 个会话" in page


# ---------------------------------------------------------------- 4. 贴纸 missing 只计一次

@pytest.mark.parametrize("with_cdn", [False, True])
def test_sticker_bad_msg_key_counts_missing_once(tmp_path, monkeypatch, with_cdn):
    _, account = tm.make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    monkeypatch.setattr(media, "sticker_cdn_urls", lambda *a, **k: ({}, None))
    con = tm._sticker_with_bad_key(ts, "d0f46ed3" + "0" * 24, "acct|cm|message_0.db|notanumber")
    try:
        rep = media.export_media(account, ["wxid_y"], kinds=["sticker"],
                                 out_dir=tmp_path / "media", con=con, with_cdn=with_cdn)
    finally:
        con.close()
    assert rep.sticker.missing == 1, rep.sticker


# ---------------------------------------------------------------- 5. 写失败不删旧产物

def test_chatview_write_failure_keeps_the_previous_artifact(tmp_path, monkeypatch):
    view = tcv._minimal_view(tmp_path)
    (out,) = chatview.write(view, formats=["md"])
    whole = out.read_bytes()

    def boom(dest, data, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(paths, "atomic_write", boom)
    assert chatview.write(view, formats=["md"]) == []      # 没写成就不进返回列表
    assert out.read_bytes() == whole, "上一次导出的完好文件被删了"


# ---------------------------------------------------------------- 6. wxgf 不算已完成

def _wxgf_sticker_env(tmp_path, monkeypatch):
    _, account = tm.make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    md5 = "d0f46ed3" + "0" * 24
    con, cm = tm._one_wxgf_sticker_index(ts, md5)
    monkeypatch.setattr(media, "sticker_cdn_urls",
                        lambda acct, items, **k: ({(cm, "message_0.db", 7):
                                                   ("http://wxapp.tc.qq.com/x", md5)}, None))
    monkeypatch.setattr(media, "_fetch_cdn_bytes", lambda url, expect, **k: (tm.WXGF_BLOB, "ok"))
    return account, con, cm, md5, ts


def _fake_transcode(data, dest):
    paths.atomic_write(dest, b"\xff\xd8\xff" + b"x" * 20)
    return True


def test_wxgf_is_retranscoded_once_ffmpeg_appears(tmp_path, monkeypatch):
    account, con, cm, md5, ts = _wxgf_sticker_env(tmp_path, monkeypatch)
    out = tmp_path / "media"

    def run():
        return media.export_media(account, ["wxid_x"], kinds=["sticker"], out_dir=out,
                                  con=con, with_cdn=True)
    try:
        # 没有 ffmpeg：存原容器（现状），重跑照旧跳过——没有能力就不白取件
        monkeypatch.setattr(media, "_find_ffmpeg", lambda: None)
        monkeypatch.setattr(media, "_wxgf_to_jpeg", lambda data, dest: False)
        assert run().sticker.ok == 1
        assert run().sticker.skipped == 1
        # 有 ffmpeg 但这次还是没转成：不重写、不占名额
        monkeypatch.setattr(media, "_find_ffmpeg", lambda: "ffmpeg")
        rep = run()
        assert (rep.sticker.ok, rep.sticker.skipped) == (0, 1), rep.sticker
        # 转成了：补出 jpg，读侧认 jpg
        monkeypatch.setattr(media, "_wxgf_to_jpeg", _fake_transcode)
        rep = run()
        assert rep.sticker.ok == 1, rep.sticker
        got = media.exported_path(out, chat_md5=cm, kind="sticker", ts=ts, file_key=md5)
        assert got is not None and got.suffix == ".jpg"
        assert run().sticker.skipped == 1
    finally:
        con.close()


# ---------------------------------------------------------------- 7. 贴纸按图片渲染

def _sticker_state(path: str) -> MediaRefState:
    return MediaRefState(msg_key="k", ts=1730000000, kind="sticker", status="exported",
                         path=path)


def test_exported_sticker_renders_as_an_image():
    st = _sticker_state("/o/cm/sticker/2026-09/a.gif")
    h = chat_html._media_block(st, "..", "/o/cm")
    assert '<img loading="lazy" src="../sticker/2026-09/a.gif" alt="贴纸">' in h
    assert chat_md._media_block(st, "..", "/o/cm") == "![贴纸](../sticker/2026-09/a.gif)"
    # 转义照旧
    h = chat_html._media_block(_sticker_state('/o/cm/sticker/2026-09/"x<.png'), "..", "/o/cm")
    assert '"x<' not in h and "&quot;x&lt;" in h


# ---------------------------------------------------------------- 8. 凭据名

_BLOCK_PARAMS = ("sessionid", "sign", "ranksessionid", "signature", "team_sign", "pindansig",
                 "sig", "pass_ticket", "signatures", "password", "Sign", "client_secret")
_BLOCK_PATHS = ("ecsgift.appmsg_sign", "finderLive.enterSessionId", "x.pass_ticket",
                "x.signature", "x.password", "x.app_secret",
                "finderLive.byPassInfoString")   # 视频号直播的进场凭据：名字以 String 结尾，后缀规则看不见
_KEEP_NAMES = ("photoaccountnamecard.sign", "x.auth_icon", "x.auth_icon_url", "x.auth_job",
               "x.authIconType", "x.authIconUrl", "x.authorUsername",
               "designershared.designeruin", "designershared.designername",
               "designershared.designerrediretcturl", "secretmsg.issecretmsg",
               "emoji.designerid", "x.author_share", "x.geoauth")


@pytest.mark.parametrize("name", _BLOCK_PARAMS)
def test_credential_url_params_are_dropped(name):
    assert appmsg._is_secret("x.url", f"https://h.example/p?a=1&{name}=V"), name


@pytest.mark.parametrize("path", _BLOCK_PATHS)
def test_credential_field_paths_are_dropped(path):
    assert appmsg._is_secret(path, "V"), path


@pytest.mark.parametrize("path", _KEEP_NAMES)
def test_non_credential_names_are_kept(path):
    assert not appmsg._is_secret(path, "V"), path
    # 作为 URL 参数名同样不拦（只有整名 `sign` 在参数位上算凭据）
    last = path.rsplit(".", 1)[-1]
    if last != "sign":
        assert not appmsg._is_secret("x.url", f"https://h.example/p?{last}=V"), last


# ---------------------------------------------------------------- 10. 批量 XOR / ISAAC 与下载上限

class _OldIsaac(sns_media.WxIsaac64):
    """修前的逐字取池实现（对拍基准）。"""

    def keystream(self, size: int) -> bytes:
        out = bytearray()
        while len(out) < size:
            if self._cnt == 0:
                self._old_gen()
                self._cnt = 256
            self._cnt -= 1
            out += self._rsl[self._cnt].to_bytes(8, "big")
        return bytes(out[:size])

    def _old_gen(self) -> None:
        mask = sns_media.MASK64
        mm, rsl = self._mm, self._rsl
        self._cc = (self._cc + 1) & mask
        self._bb = (self._bb + self._cc) & mask
        for i in range(256):
            x = mm[i]
            k = i & 3
            if k == 0:
                self._aa = ((self._aa ^ ((self._aa << 21) & mask)) ^ mask) & mask
            elif k == 1:
                self._aa ^= (self._aa >> 5)
            elif k == 2:
                self._aa ^= ((self._aa << 12) & mask)
            else:
                self._aa ^= (self._aa >> 33)
            self._aa = (mm[(i + 128) & 255] + self._aa) & mask
            y = (mm[(x >> 3) & 255] + self._aa + self._bb) & mask
            mm[i] = y
            self._bb = (mm[(y >> 11) & 255] + x) & mask
            rsl[i] = self._bb


def test_isaac_keystream_matches_the_reference_byte_for_byte():
    rng = random.Random(20261126)
    for seed in (0, 1, 9, (1 << 64) - 1, *(rng.getrandbits(64) for _ in range(5))):
        new, old = sns_media.WxIsaac64(seed), _OldIsaac(seed)
        for size in (1, 7, 8, 9, 2047, 2048, 2049, 5000, 0, 3):   # 连续多次取，跨池边界
            assert new.keystream(size) == old.keystream(size), (seed, size)
    data = rng.randbytes(9000)
    ks = _OldIsaac(42).keystream(len(data))
    assert sns_media.decrypt_stream(data, "42") == bytes(a ^ b for a, b in zip(data, ks))
    short = sns_media.VIDEO_HEAD + 5
    big = rng.randbytes(short)
    ks = _OldIsaac(7).keystream(sns_media.VIDEO_HEAD)
    want = bytes(a ^ b for a, b in zip(big, ks)) + big[sns_media.VIDEO_HEAD:]
    assert sns_media.decrypt_stream(big, "7", video=True) == want


def test_dat_xor_matches_the_bytewise_reference():
    rng = random.Random(7)
    keys = media.MediaKeys(xor_key=0x5A, aes_key_ascii=b"0" * 16, code="", aes_verified=False)
    data = rng.randbytes(4096)
    assert media.decrypt_dat(data, keys) == bytes(b ^ 0x5A for b in data)
    v1 = media.V1_SIG + data
    assert media.decrypt_dat(v1, keys) == bytes(b ^ 0x5A for b in data)
    aes = b"0123456789abcdef"
    tk = media.MediaKeys(xor_key=tm.FAKE_XOR, aes_key_ascii=aes, code="", aes_verified=True)
    head, tail = tm.jpeg_like(40), rng.randbytes(33)
    assert media.decrypt_dat(tm.build_v2_dat(head, tail, aes, tm.FAKE_XOR), tk).endswith(tail)


class _BodyResp:
    status = 200

    def __init__(self, body: bytes, headers: dict | None = None):
        self._f = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, amt=None):
        return self._f.read(amt)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_cdn_sticker_download_is_capped(monkeypatch):
    monkeypatch.setattr(media, "CDN_MAX_BYTES", 16)
    body = b"\x89PNG" + b"x" * 13                                   # 17 字节：超上限
    monkeypatch.setattr(media, "_cdn_opener", lambda: type(
        "Op", (), {"open": lambda self, url, timeout=None: _BodyResp(body)})())
    assert media._fetch_cdn_bytes("http://wxapp.tc.qq.com/x",
                                  hashlib.md5(body).hexdigest()) == (None, "too_large")
    ok = body[:16]
    monkeypatch.setattr(media, "_cdn_opener", lambda: type(
        "Op", (), {"open": lambda self, url, timeout=None: _BodyResp(ok)})())
    assert media._fetch_cdn_bytes("http://wxapp.tc.qq.com/x",
                                  hashlib.md5(ok).hexdigest()) == (ok, "ok")


def test_cdn_sticker_too_large_is_reported_and_not_written(tmp_path, monkeypatch):
    _, account = tm.make_tree(tmp_path, monkeypatch)
    ts = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
    md5 = "d0f46ed3" + "0" * 24
    con, cm = tm._one_wxgf_sticker_index(ts, md5)
    monkeypatch.setattr(media, "sticker_cdn_urls",
                        lambda acct, items, **k: ({(cm, "message_0.db", 7):
                                                   ("http://wxapp.tc.qq.com/x", md5)}, None))
    monkeypatch.setattr(media, "_fetch_cdn_bytes", lambda url, expect, **k: (None, "too_large"))
    out = tmp_path / "media"
    try:
        rep = media.export_media(account, ["wxid_x"], kinds=["sticker"], out_dir=out,
                                 con=con, with_cdn=True)
    finally:
        con.close()
    assert rep.sticker.failed == 1
    assert any("上限" in d for d in rep.degraded), rep.degraded
    assert not (out / cm / "sticker").exists() or not any((out / cm / "sticker").rglob("*.*"))


def test_sns_fetch_is_capped_and_not_retried(monkeypatch):
    monkeypatch.setattr(sns_media, "MAX_FETCH_BYTES", 16)
    monkeypatch.setattr(sns_media.time, "sleep", lambda _s: None)
    calls = []

    class Op:
        def __init__(self, body):
            self.body = body

        def open(self, req, timeout=None):
            calls.append(req)
            return _BodyResp(self.body, {"x-enc": "1"})

    with pytest.raises(sns_media.SnsMediaError, match="上限"):
        sns_media.fetch(tsns._m(key="3"), opener=Op(b"\xff" * 17))     # type: ignore[arg-type]
    assert len(calls) == 1, "超上限是确定性失败，不该重试"
    plain = b"\xff\xd8\xff" + b"\x00" * 13
    enc = tsns._enc(plain, "3")
    got = sns_media.fetch(tsns._m(key="3"), opener=Op(enc))            # type: ignore[arg-type]
    assert got == plain

