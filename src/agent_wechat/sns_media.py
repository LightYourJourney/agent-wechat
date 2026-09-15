"""朋友圈远程媒体：URL 构造 / 下载 / WX-ISAAC64 解密。

**本模块是「零网络」红线上唯一的显式例外**（spec §6）：只有 `fetch()` 会发请求，
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
    """只允许微信 CDN 两个域——防止恶意 XML 把请求引向任意主机。"""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return any(host.endswith(sfx) for sfx in ALLOWED_SUFFIXES)


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
