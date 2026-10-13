"""朋友圈远程媒体：URL 构造 / 下载 / WX-ISAAC64 解密。

**本模块是零网络红线上本次新增的显式例外**（spec §6）：只有 `fetch()` 会发请求，
且只连白名单内的微信 CDN 主机（`*.qpic.cn` / `*.video.qq.com` 后缀，外加**精确**主机
`wxapp.tc.qq.com`）；调用方默认不启用（`--with-media` / `with_media=True`）。
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
ALLOWED_HOSTS = ("wxapp.tc.qq.com",)
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
    """构造**可下载**的 URL。返回 `None` == 「本项目不会去取它」（不计失败）。

    返回 `None` 有四个出口：无 `url`、kind 不在白名单内、老路径（`AUTH_SNS`）无 `token`、
    或宿主不在白名单内（最后这条是**两个分支共用的收口**，`AUTH_READY` 也照样落 `None`）。
    调用方按此标注，不要猜。
    老路径（`AUTH_SNS`）才拼 `token`/`idx=1`，并把图片 URL 末段尺寸标识改 `/0`（`_SIZE_SEG`）。
    视频的 token 必须放在参数**最前**（实测视频 URL 自带 10 个查询参数，位置错了服务端不认）。
    """
    if not m.url or m.kind not in (moments.KIND_IMAGE, moments.KIND_VIDEO):
        # 白名单而非黑名单：KIND_OTHER（type 3/4/0）语义未实测，图片分支（尺寸段改 /0）
        # 与视频分支（token 前置）对它都是猜测；将来新增的 kind 也会落到这里，
        # 而不是静默走图片分支构造出看似可下载的 URL。宁可标为不可下载，也不猜形态。
        return None

    if m.auth == moments.AUTH_READY:
        # 原样返回，只做 http→https。**不拼 idx=1、不做尺寸段归一**：实测 type=7 的路径
        # 尾段本就是 /0，视频号 thumbUrl 的路径无尺寸段——加工是多余的，还会让签名失效。
        # 沿用既有的 `.replace`（不是新 helper）以与 AUTH_SNS 分支同形：它只替换**小写**
        # `http://`，`HTTP://…` 原样过关，而 urllib 会把 scheme 归一成小写。这一跳由
        # `fetch` 里的 `https_allowed` 挡住（判据与重定向的每一跳共用）。
        out = m.url.replace("http://", "https://")
    else:
        if not m.token:
            return None
        url = m.url.replace("http://", "https://")
        tok = urllib.parse.quote(m.token, safe="")
        if m.kind == moments.KIND_VIDEO:
            head, sep, query = url.partition("?")
            extra = f"&{query}" if sep else ""
            out = f"{head}?token={tok}&idx=1{extra}"
        else:
            head, sep, query = url.partition("?")
            url = _SIZE_SEG.sub("/0", head) + (sep + query if sep else "")
            out = f"{url}{'&' if '?' in url else '?'}token={tok}&idx=1"

    # 宿主不在白名单 → 「不去取」。不这样收口，usecases 会把它计进 media_failed
    # （fetch 的 host_allowed 在重试循环之前抛），而真相是「不去取」。
    return out if host_allowed(out) else None


def host_allowed(url: str) -> bool:
    """只允许白名单内主机——防止恶意 XML 把请求引向任意主机。

    **只判 host、不判 scheme**：完整判据是 `https_allowed`。两者分开只为让「域不对」与
    「域对但 scheme 不对」在错误文案里分得开。

    后缀匹配（`.qpic.cn` / `.video.qq.com`）覆盖全部子域；`ALLOWED_HOSTS` 是**精确相等**，
    **不含子域**（spike 只验过 `wxapp.tc.qq.com` 本身）。判据用 `moments.safe_host`：
    裸 `urlsplit` 对畸形输入抛 `ValueError`，会逃出 core。
    """
    host = moments.safe_host(url)
    if host is None:
        return False
    return host in ALLOWED_HOSTS or any(host.endswith(sfx) for sfx in ALLOWED_SUFFIXES)


def https_allowed(url: str) -> bool:
    """白名单内主机 **且** https——第一跳与每一跳重定向共用这个判据。

    域对了但 scheme 不对同样是洞：只查 host 时，`*.qpic.cn` 上一个 302 到
    `http://shmmsns.qpic.cn/…`（**同一个域**）会被照常跟随，第二跳是明文，
    而这条请求带着 token。
    """
    s = moments.split_url(url)
    return s is not None and s.scheme == "https" and host_allowed(url)


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

    **已知边界（EA-056，2026-10-14 审计）**：「只解前 128KB」的判据是 `kind`（`video=`），
    而**视频封面复用 `kind=video`**——封面大于 128KB 且整份加密时会被静默截断成坏图。修法
    （给封面那条调用传 `video=False`）**需要一行真机证据**（封面 >128KB 且整份加密）才该动，
    否则是无根据的猜测；本批按条目的「先别改」处置，边界留在这里。

    注意 `x-enc: 1` 只说明「响应体是加密的」，**不代表解出来的是有效媒体**：
    密钥错误或越界时 `decrypt_stream` 会静默产出乱码而非抛错，调用方须自行嗅探校验。

    解密判据取「响应头 `x-enc` **或** XML `<enc>` 文本」——两个信号都不完备：
    `type=15` 的响应没有 `x-enc` 头却确实是密文，而 `type=1/54/3` 的 XML 文本是 `'0'`
    却要靠响应头决定。
    """
    url = build_url(m)
    if url is None:
        raise SnsMediaError("该媒体无法下载：没有 url/token、宿主不在微信 CDN 白名单内，或媒体类型不支持")
    if not host_allowed(url):
        host = moments.safe_host(url) or "?"
        raise SnsMediaError(f"拒绝非微信 CDN 域名：{host}")
    if not https_allowed(url):
        # 域已在上一行确认过，走到这里的唯一可能就是 scheme 不对。第一跳同样卡 scheme：
        # `build_url` 只替换**小写**的 `http://`，`HTTP://shmmsns.qpic.cn/…` 原样过关，
        # 而 urllib 会把 scheme 归一成小写——域名白名单对此无感，于是第一跳走明文、
        # token 跟着明文出去。判据与重定向的每一跳共用同一个 `https_allowed`。
        raise SnsMediaError("拒绝非 https 的媒体地址")
    if m.encrypted and not m.key:
        # XML 自相矛盾：`<enc>` 文本是 '1'（声明已加密）却没有 `enc@key`。真机 5087 个
        # media 零例外，且那 312 个 `'0'` 哨兵全是 `<enc>0</enc>`——这里本不该触发，
        # 一旦触发就是微信改了形态，必须**出声**。
        # 不能放行：原先靠 `if m.key and (...)` 短路成「返回密文」，落盘后 `_sniff_ext`
        # 判不出来，最终报成「解密后不是已知图片格式」——与「密钥错」「密钥缺失」混为
        # 一谈，排查会被引向错方向。
        # 放在重试循环**之前**：判据只看 XML、与响应无关，请求回来也解不开；放进循环
        # 还会被 `except Exception` 吞掉重试，三次后只剩「下载失败（重试 3 次）」，
        # 真正的原因反而丢了。
        raise SnsMediaError("媒体已加密但 XML 未给出密钥（enc@key 缺失）")

    op = opener if opener is not None else _default_opener()
    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            with op.open(req, timeout=timeout) as resp:
                raw = resp.read()
                enc = str(resp.headers.get("x-enc") or "").strip()
        except urllib.error.HTTPError as exc:
            # EA-007：4xx 是**确定性失败**——重试既白等，又把根因改写成「下载失败（重试 N 次）」，
            # 用户会去查网络而不是理解「服务端已清理这条媒体」。`HTTPError` 是 `URLError`
            # 的子类，故本子句必须排在宽 `except` 之前。文案只带状态码（**不带 URL**——
            # 「消息只含类型名」那条硬约束的同一立场，`test_fetch_error_never_echoes_token` 钉住）。
            # 取舍（条目明记）：确实存在「CDN 边缘暂时 404」的形态，代价是丢掉一次可恢复的下载。
            if 400 <= exc.code < 500:
                raise SnsMediaError(
                    f"服务端已清理（HTTP {exc.code}），不再重试：该媒体已不可取") from None
            last = exc
        except Exception as exc:                       # 网络异常种类多，统一重试
            last = exc
        else:
            # 解密放在重试面**之外**（EA-007）：它只依赖响应体与 XML、与网络无关。此前它在
            # `try` 内，任何解密失败（如 `key` 不是数字）都被宽 `except` 吞掉重试三次，最后
            # 只剩「下载失败（重试 3 次）」——真正的原因反而丢了（同 `:251` 那条早退的理由）。
            # 两个加密信号都不完备，**取或才完备**（真机 5087 个 media 零例外）：
            #   · 响应头 `x-enc: 1` —— type=1/54/3（4775 条）靠它
            #   · XML `<enc>` 文本 '1' —— type=15（180 条）**没有** `x-enc` 头，靠它
            # 只信头会静默存下密文；只信 XML 会漏掉老路径。
            if enc == "1" or m.encrypted:
                if not m.key:
                    # EA-055：头说加密而 XML 没给密钥——此前靠 `if m.key and (...)` 短路成
                    # 「返回密文」，落盘是坏图、归因指向「解密后不是已知格式」，与请求**前**
                    # 那条同族早退（`m.encrypted and not m.key`）不对称：同一族信号只有一半出声。
                    raise SnsMediaError(
                        "媒体已加密（x-enc=1）但 XML 未给出密钥（enc@key 缺失）")
                return decrypt_stream(raw, m.key,
                                      video=(m.kind == moments.KIND_VIDEO))
            return raw
        if i < attempts - 1:
            time.sleep(sleep)
    raise SnsMediaError(f"下载失败（重试 {attempts} 次）：{type(last).__name__}")
