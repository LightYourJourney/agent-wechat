"""朋友圈远程媒体：URL 构造 / 下载 / WX-ISAAC64 解密。

**本模块是零网络红线上本次新增的显式例外**（spec §6）：只有 `fetch()` 会发请求，
且只连微信 CDN 两个域；调用方默认不启用（`--with-media` / `with_media=True`）。
"""
from __future__ import annotations

import re
import time
import urllib.parse
import urllib.request

from . import errors, moments

MASK64 = (1 << 64) - 1
# 微信的黄金比例常量是 …7C13。公开实现（含 WeFlow 的 TS 移植）多用 …7C15，
# 一个字符之差会静默解出乱码而不是报错 —— 见 spec §2.8。
GOLDEN = 0x9E3779B97F4A7C13
VIDEO_HEAD = 128 * 1024
USER_AGENT = "MicroMessenger Client"
ALLOWED_SUFFIXES = (".qpic.cn", ".video.qq.com")
_SIZE_SEG = re.compile(r"/(?:150|200|480)$")


# 类型本体在 errors.py（spec §4.2：「加入 errors.py，契约与既有一致」），这里只做模块级别名：
# 两处各定义一份会产生两个互不相干的类，`except errors.SnsMediaError` 将抓不到本模块抛的异常。
SnsMediaError = errors.SnsMediaError


class WxIsaac64:
    """微信的 ISAAC-64 变体。除常量外与经典 ISAAC-64 一致：

    两轮 randinit（第二轮加 mm）、`i & 3` 四种移位、**降序取池**、64 位字按**大端**输出。
    种子是密钥材料，`__repr__` 恒隐藏。
    """

    __slots__ = ("_mm", "_aa", "_bb", "_cc", "_rsl", "_cnt")

    def __init__(self, seed: int):
        self._mm = [0] * 256
        self._aa = self._bb = self._cc = 0
        self._rsl = [0] * 256
        self._rsl[0] = seed & MASK64
        self._init()
        self._cnt = 256

    def __repr__(self) -> str:
        return "WxIsaac64(***)"

    @staticmethod
    def _mix(s: list[int]) -> list[int]:
        a, b, c, d, e, f, g, h = s
        a = (a - e) & MASK64; f ^= (h >> 9);            h = (h + a) & MASK64
        b = (b - f) & MASK64; g ^= ((a << 9) & MASK64); a = (a + b) & MASK64
        c = (c - g) & MASK64; h ^= (b >> 23);           b = (b + c) & MASK64
        d = (d - h) & MASK64; a ^= ((c << 15) & MASK64); c = (c + d) & MASK64
        e = (e - a) & MASK64; b ^= (d >> 14);           d = (d + e) & MASK64
        f = (f - b) & MASK64; c ^= ((e << 20) & MASK64); e = (e + f) & MASK64
        g = (g - c) & MASK64; d ^= (f >> 17);           f = (f + g) & MASK64
        h = (h - d) & MASK64; e ^= ((g << 14) & MASK64); g = (g + h) & MASK64
        return [a, b, c, d, e, f, g, h]

    def _init(self) -> None:
        s = [GOLDEN] * 8
        for _ in range(4):
            s = self._mix(s)
        for i in range(0, 256, 8):
            s = [(s[j] + self._rsl[i + j]) & MASK64 for j in range(8)]
            s = self._mix(s)
            self._mm[i:i + 8] = s
        for i in range(0, 256, 8):
            s = [(s[j] + self._mm[i + j]) & MASK64 for j in range(8)]
            s = self._mix(s)
            self._mm[i:i + 8] = s
        self._gen()

    def _gen(self) -> None:
        mm, rsl = self._mm, self._rsl
        self._cc = (self._cc + 1) & MASK64
        self._bb = (self._bb + self._cc) & MASK64
        for i in range(256):
            x = mm[i]
            k = i & 3
            if k == 0:
                self._aa = ((self._aa ^ ((self._aa << 21) & MASK64)) ^ MASK64) & MASK64
            elif k == 1:
                self._aa ^= (self._aa >> 5)
            elif k == 2:
                self._aa ^= ((self._aa << 12) & MASK64)
            else:
                self._aa ^= (self._aa >> 33)
            self._aa = (mm[(i + 128) & 255] + self._aa) & MASK64
            y = (mm[(x >> 3) & 255] + self._aa + self._bb) & MASK64
            mm[i] = y
            self._bb = (mm[(y >> 11) & 255] + x) & MASK64
            rsl[i] = self._bb

    def keystream(self, size: int) -> bytes:
        out = bytearray()
        while len(out) < size:
            if self._cnt == 0:
                self._gen()
                self._cnt = 256
            self._cnt -= 1
            out += self._rsl[self._cnt].to_bytes(8, "big")
        return bytes(out[:size])


def build_url(m: moments.SnsMedia) -> str | None:
    """构造可下载 URL：http→https、图片末段尺寸标识改 `/0`、拼接 token。

    无 `url` 或无 `token` → None（**不可下载**，调用方按此标注，不要猜）。
    视频的 token 必须放在参数**最前**（实测视频 URL 自带 10 个查询参数，
    位置错了服务端不认）。
    """
    if not m.url or not m.token:
        return None
    if m.kind not in (moments.KIND_IMAGE, moments.KIND_VIDEO):
        # 白名单而非黑名单：KIND_OTHER（type 3/4/0）语义未实测，图片分支（尺寸段改 /0）
        # 与视频分支（token 前置）对它都是猜测；将来新增的 kind 也会落到这里，
        # 而不是静默走图片分支构造出看似可下载的 URL。宁可标为不可下载，也不猜形态。
        return None
    url = m.url.replace("http://", "https://")
    tok = urllib.parse.quote(m.token, safe="")
    if m.kind == moments.KIND_VIDEO:
        head, sep, query = url.partition("?")
        extra = f"&{query}" if sep else ""
        return f"{head}?token={tok}&idx=1{extra}"
    head, sep, query = url.partition("?")
    url = _SIZE_SEG.sub("/0", head) + (sep + query if sep else "")
    return f"{url}{'&' if '?' in url else '?'}token={tok}&idx=1"


def host_allowed(url: str) -> bool:
    """只允许微信 CDN 两个域——防止恶意 XML 把请求引向任意主机。

    **只判 host、不判 scheme**：完整判据是 `https_allowed`。两者分开只为让「域不对」与
    「域对但 scheme 不对」在错误文案里分得开。
    """
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return any(host.endswith(sfx) for sfx in ALLOWED_SUFFIXES)


def https_allowed(url: str) -> bool:
    """微信 CDN 域名 **且** https——第一跳与每一跳重定向共用这个判据。

    域对了但 scheme 不对同样是洞：只查 host 时，`*.qpic.cn` 上一个 302 到
    `http://shmmsns.qpic.cn/…`（**同一个域**）会被照常跟随，第二跳是明文，
    而这条请求带着 token。
    """
    return urllib.parse.urlsplit(url).scheme == "https" and host_allowed(url)


def decrypt_stream(data: bytes, key: str, *, video: bool = False) -> bytes:
    """`x-enc: 1` 的响应体 → 明文。视频只解前 128KB（其后本就是明文）。"""
    try:
        seed = int(key)
    except (TypeError, ValueError):
        raise SnsMediaError("媒体密钥不是数字，无法解密") from None   # 不回显 key
    n = min(len(data), VIDEO_HEAD) if video else len(data)
    if n <= 0:
        return data
    ks = WxIsaac64(seed).keystream(n)
    return bytes(a ^ b for a, b in zip(data[:n], ks)) + data[n:]


class _AllowlistedRedirect(urllib.request.HTTPRedirectHandler):
    """重定向目标必须**也在白名单内**——白名单只对第一跳成立等于没有白名单。

    urllib 默认跟随重定向、且没人复查 `resp.geturl()`：`*.qpic.cn` 上一个 302
    就能把请求（带着 token）带去任意主机。目标不在白名单时返回 `None`，
    urllib 会因此抛 `HTTPError`，落进 `fetch` 既有的重试/包装路径
    （消息只含类型名，不会把攻击者指定的主机回显出去）。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not https_allowed(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_opener() -> urllib.request.OpenerDirector:
    """**强制直连**（不读 `http_proxy` 等环境变量）+ **跨跳域白名单**。

    直连的机制不是「空 ProxyHandler 生效」：`ProxyHandler({})` 因为 `proxies` 为空
    而**不暴露任何 `*_open` 方法**，于是 `OpenerDirector.add_handler` 末尾的
    `if added:` 为假、这个 handler 被**静默丢弃**；与此同时，传了 handler 又让
    `build_opener` **跳过安装默认的 `ProxyHandler(getproxies())`**。
    净效果 = 完全没有代理处理 = 直连。
    所以：**不要「简化」成 `build_opener()`** —— 那会静默把环境代理装回来。
    实测本机 `http_proxy=http://127.0.0.1:7897`：直连 6/6 成功（0.2–0.3s），走代理反复超时。

    第二个 handler 是 `_AllowlistedRedirect`：`build_opener` 见到它会**跳过**安装
    默认的 `HTTPRedirectHandler`（后者会无条件跟随重定向）。域白名单必须**跨跳成立**，
    否则 `host_allowed` 只管得住第一跳。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                       _AllowlistedRedirect())


def fetch(m: moments.SnsMedia, *, timeout: float = 20.0, attempts: int = 3,
          sleep: float = 2.0, opener=None) -> bytes:
    """下载并解密一条媒体，返回明文。失败重试 `attempts` 次后抛 `SnsMediaError`。

    异常消息**只含类型名**，绝不回显 URL / token / key。

    注意 `x-enc: 1` 只说明「响应体是加密的」，**不代表解出来的是有效媒体**：
    密钥错误或越界时 `decrypt_stream` 会静默产出乱码而非抛错，调用方须自行嗅探校验。
    """
    url = build_url(m)
    if url is None:
        raise SnsMediaError("该媒体没有 token 或 url，无法下载")
    if not host_allowed(url):
        host = urllib.parse.urlsplit(url).hostname or "?"
        raise SnsMediaError(f"拒绝非微信 CDN 域名：{host}")
    if not https_allowed(url):
        # 域已在上一行确认过，走到这里的唯一可能就是 scheme 不对。第一跳同样卡 scheme：
        # `build_url` 只替换**小写**的 `http://`，`HTTP://shmmsns.qpic.cn/…` 原样过关，
        # 而 urllib 会把 scheme 归一成小写——域名白名单对此无感，于是第一跳走明文、
        # token 跟着明文出去。判据与重定向的每一跳共用同一个 `https_allowed`。
        raise SnsMediaError("拒绝非 https 的媒体地址")

    op = opener if opener is not None else _default_opener()
    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            with op.open(req, timeout=timeout) as resp:
                raw = resp.read()
                enc = str(resp.headers.get("x-enc") or "").strip()
            if enc == "1" and m.key:
                return decrypt_stream(raw, m.key,
                                      video=(m.kind == moments.KIND_VIDEO))
            return raw
        except Exception as exc:                       # 网络异常种类多，统一重试
            last = exc
            if i < attempts - 1:
                time.sleep(sleep)
    raise SnsMediaError(f"下载失败（重试 {attempts} 次）：{type(last).__name__}")
