"""朋友圈解析：sns.db 的 TimelineObject XML → 结构化动态。

纯读取：不联网、不 import store/reader。解密复用既有 decrypt/keys/paths。
"""
from __future__ import annotations

import dataclasses
import math
import sqlite3
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterator

from . import decrypt, errors, keys, media, paths

TYPE_VIDEO_POST = 15
KIND_IMAGE, KIND_VIDEO, KIND_OTHER = "image", "video", "other"
CMT_LIKE, CMT_COMMENT = "like", "comment"

# URL 的「取法」。差异做成**数据**而不是代码分支：新增一种 CDN 形态 = 加一个枚举值，
# 而不是在 build_url 里加一条分支（分支多了，白名单校验就会有人忘记做）。
AUTH_SNS = "sns"        # 老路径：token/key 在 url 元素属性；URL 需拼 idx=1、尺寸段归一
AUTH_READY = "ready"    # URL 已完整可用（自带签名或无签名公开）；原样 GET，仅 http→https


def split_url(url: str | None) -> urllib.parse.SplitResult | None:
    """安全的 `urllib.parse.urlsplit`：`None` 或畸形输入返回 `None`，**不抛**。

    `urlsplit("https://[::1/x")` → `ValueError: Invalid IPv6 URL`（实测）。URL 全部来自
    好友可控的 XML，裸 `ValueError` 不是 `AgentWeChatError`，会一路穿透到 CLI 打
    traceback（契约见 errors.py 开篇）。同 `_ts` / `_duration` 已收口的那一类。

    `None`（调用方根本没拿到 URL）→ `None`；`""` 是**合法输入** → 空的 `SplitResult`。
    **不要写成 `urlsplit(url or "")`**：那会把 `None` 并进空串，`split_url(None)` 就
    返回 `SplitResult` 而不是 `None`（`test_split_url_normal_cases` 会红）。
    """
    if url is None:
        return None
    try:
        return urllib.parse.urlsplit(url)
    except ValueError:
        return None


def safe_host(url: str | None) -> str | None:
    """URL → 小写主机名；畸形输入或没有主机时返回 `None`。"""
    s = split_url(url)
    if s is None:
        return None
    return (s.hostname or "").lower() or None


@dataclass(frozen=True)
class SnsMedia:
    """一条媒体引用。`token`/`key` 是鉴权材料——repr 恒隐藏（同 MediaKeys 规则）。"""
    kind: str
    auth: str = AUTH_SNS            # 「怎么取 URL」；默认值 = 老路径
    url: str | None = None
    token: str | None = None
    key: str | None = None          # 解密种子：AUTH_SNS 取自 url@key；AUTH_READY 取自 enc@key
    enc_idx: str | None = None
    thumb: str | None = None
    thumb_local: str | None = None  # 封面落盘后的相对路径（由导出编排赋值；解析层恒 None）
    encrypted: bool = False         # XML <enc> 文本 == '1'
    video_duration: int | None = None
    live_url: str | None = None
    live_key: str | None = None
    md5: str | None = None        # 取自 url 元素的 md5 属性（媒体文件名哈希）
    local: str | None = None      # 产物内的相对路径（由导出编排赋值；解析层恒 None）

    def __repr__(self) -> str:            # pragma: no cover - 简单表达式
        return f"SnsMedia(kind={self.kind!r}, video_duration={self.video_duration!r}, live={self.live_url is not None})"


@dataclass(frozen=True)
class SnsComment:
    kind: str                             # 'comment' | 'like'
    nickname: str
    content: str | None
    ref_nickname: str | None
    create_time: int
    is_rich: bool


@dataclass(frozen=True)
class SnsLink:
    """外链卡片的元数据。`url` 是**第三方网页地址**，本项目永不请求它。"""
    url: str
    title: str | None


@dataclass(frozen=True)
class SnsFinder:
    """视频号卡片（`ContentObject/finderFeed`）。

    封面作为一条 `AUTH_READY` 图片出现在 `media` 里——复用既有渲染/落盘/降级链路。
    `objectNonceId`/`authIconType`/`megaVideo`/`liveId` 刻意不取（YAGNI：本期不做跳转
    与认证徽标）。
    """
    nickname: str
    desc: str | None
    duration: int | None


@dataclass(frozen=True)
class SnsPost:
    tid: str
    post_id: str
    username: str
    nickname: str
    create_time: int
    text: str | None
    is_video_post: bool
    media: tuple[SnsMedia, ...]
    comments: tuple[SnsComment, ...]
    link: SnsLink | None = None
    finder: SnsFinder | None = None


def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    v = el.findtext(path)
    return v.strip() if v and v.strip() else None


def _duration(v: str | None) -> int | None:
    """视频时长（秒）。**真机是浮点**（`"30.0009995"` / `"15.033"`）。

    此前走整数谓词（`isdecimal`）→ 87%（158/181）的真实时长被静默判成「没有时长」，
    与「如实导出」相悖（F29）。改 `float()` 后：越界值（`inf`/`nan`）、非正值、非数字
    一律 None（不是 0——0 秒是「时长 0」，语义不同），`round()` 保持 `int | None`。

    不崩是硬要求：`float()` 对畸形输入只抛 `ValueError`，而 `parse_post` 只 catch
    `ET.ParseError`——漏出去就能一路崩到整次导出（F26，与 F25 同源）。`"²"` 与 `"abc"`
    走的是同一条 ValueError 路径。
    """
    if not v:
        return None
    try:
        f = float(v)
    except ValueError:
        return None
    return round(f) if math.isfinite(f) and f > 0 else None


# `datetime.fromtimestamp` 按**本地时区**换算（`media.month_of` 与 `moments_html._when` 都是），
# 所以上界比 UTC 的 9999-12-31 还早：本机 UTC+8 实测，UTC 的 `253402300799` 会落进公元 10000 年
# 并抛 `ValueError`（不是 OverflowError，但同样是裸异常）。取「UTC+14」（现实中的最大偏移）
# 也不越界的那一刻，任何时区都安全。
_MAX_TS = 253402250399        # UTC 9999-12-31T09:59:59 = 本地 9999-12-31T23:59:59 @UTC+14


def _ts(v: str | None) -> int:
    """时间戳：仅接受 `datetime` 能安全表示的正整数，否则 0。

    `createTime` 来自动态 XML（好友可控）。下游 `media.month_of`（含 `iter_posts` 的月份过滤）、
    `moments_html._when` 都会对它做 `datetime.fromtimestamp`，越界即抛 `OverflowError` 或
    `ValueError`——两者**都不是 AgentWeChatError**，会逃出 core 层；而且**离线渲染路径同样会踩**
    （不需要开 `--with-media`）。故在解析边界一次收口，未来新增的消费者自动免疫。

    用 `isdecimal` 而非 `isdigit`：后者对 `"²"` 这类上标字符也为真，随后 `int()` 抛
    `ValueError`（实测），等于把同一个洞换个入口留着。
    """
    if not v or not v.isdecimal():
        return 0
    n = int(v)
    return n if 0 < n <= _MAX_TS else 0


def _nz(v: str | None) -> str | None:
    """`'0'` 是「无密钥」哨兵（真机 312 次出现）。原先它会被当字符串传进
    `decrypt_stream` → `ISAAC64(0)` → 乱码。"""
    return v if v and v != "0" else None


def _media_of(m: ET.Element) -> SnsMedia:
    t = _text(m, "type")
    kind = KIND_VIDEO if t == "6" else KIND_IMAGE if t == "2" else KIND_OTHER
    u, th = m.find("url"), m.find("thumb")
    lmu = m.find("LivePhoto/liveMedia/url")
    lmk = m.find("LivePhoto/liveMedia/enc")
    enc_el = m.find("enc")
    # `<enc>` 文本 '1' ⟺ enc@key 有值 ⟺ 卡片 type=15（真机 5087 个 media 零例外）
    encrypted = enc_el is not None and (enc_el.text or "").strip() == "1"
    return SnsMedia(
        kind=kind,
        # 无 url@token ⟹ URL 已完整（查询串签名 / 无签名公开），不需要拼
        auth=AUTH_SNS if (u is not None and u.get("token")) else AUTH_READY,
        url=(u.text or "").strip() or None if u is not None else None,
        # token/key/enc_idx 是**属性**不是子元素——实测踩过的坑
        token=u.get("token") if u is not None else None,
        key=_nz(enc_el.get("key")) if encrypted else _nz(u.get("key") if u is not None else None),
        enc_idx=u.get("enc_idx") if u is not None else None,
        md5=u.get("md5") if u is not None else None,
        thumb=(th.text or "").strip() or None if th is not None else None,
        encrypted=encrypted,
        video_duration=_duration(_text(m, "videoDuration")),
        # 实况照片：本机不可下载（spec §8 的非目标）；密钥在 liveMedia/enc@key。
        # **理由不是「无 token」**——本期之后无 token 只意味着 `AUTH_READY`（可下）。
        # 真正的原因是这两个字段**从不进 `build_url`/`_resolve_media` 的路径**：
        # 除了 `__repr__`（`live_url is not None`）与 `tests/test_moments.py:174-175`
        # 的解析断言，没有任何消费点。将来若要把实况照片接进下载链路，
        # 得从引入消费点开始，而不是改 token 判定。
        live_url=(lmu.text or "").strip() or None if lmu is not None else None,
        live_key=lmk.get("key") if lmk is not None else None,
    )


_SUPPORT_HOSTS = {"support.weixin.qq.com"}


def _is_external(url: str | None) -> bool:
    """外链判据。必须排除 support 桩——真机上带桩的 `contentUrl` 有 192 条
    （type15 178 + type28 12 + type3 1 + type34 1），不排除就会渲染出一堆
    「微信版本不支持此内容」的无用链接。畸形 URL 当「没有外链」处理，不崩。"""
    s = split_url(url)
    if s is None:
        return False
    return (s.scheme in ("http", "https") and bool(s.hostname)
            and s.hostname not in _SUPPORT_HOSTS)


def _finder_media(feed: ET.Element) -> tuple[SnsMedia, ...]:
    """视频号封面 → `AUTH_READY` 图片。只取 `<thumbUrl>`；`<url>`（视频本体）按
    spec §2 非目标不使用。`<thumbUrl>` 缺失或为空则跳过该条（不产出占位媒体）。"""
    out = []
    for mm in feed.findall("mediaList/media"):
        t = _text(mm, "thumbUrl")
        if t:
            out.append(SnsMedia(kind=KIND_IMAGE, auth=AUTH_READY, url=t))
    return tuple(out)


def _finder_of(feed: ET.Element) -> SnsFinder:
    """`duration` 走 `_duration` 而不是 `int()`：`videoPlayDuration` 是好友可控 XML，
    `int("abc")` 的 `ValueError` 会逃出 core（同 `_ts` / `_duration` 已收口的类）。"""
    med = feed.find("mediaList/media")
    return SnsFinder(
        nickname=_text(feed, "nickname") or "",
        desc=_text(feed, "desc"),
        duration=_duration(_text(med, "videoPlayDuration") if med is not None else None),
    )


def _comments_of(root: ET.Element) -> tuple[SnsComment, ...]:
    """`<user_comment>` 同时出现在评论列表与 like_user_list 内，按 type 区分：
    1=点赞、2=评论、其余（4）按评论处理。

    **`root` 必须是 `SnsDataItem`（整个动态），不是 `TimelineObject`**：实测 2,450 条
    `<user_comment>` 全部落在 `LocalExtraInfo/` 下的 `comment_user_list`(1289) /
    `like_user_list`(1157) / `with_user_list`(4)，`TimelineObject` 内一条都没有。
    传错了不会报错，只会**静默返回 0 条互动**（本机 1,923 条动态全空，直到真机冒烟才暴露）。

    **回复目标要反查昵称**：`<ref_username>` 是 **wxid 不是昵称**（本机 183/183 全是
    `wxid_…`），直接当昵称渲染出来就是「小明 回复 wxid_abc123」。XML 里没有回复方的昵称
    字段（`ref_*` 子元素只有 `ref_comment_id` / `ref_comment_64id` / `ref_username`），但被
    回复的人若在**本条动态**里点过赞或发过评论，他自己的 `<user_comment>` 就带着 `nickname`
    ——按 `username` 建一张表反查即可（本机 183/183 命中）。查不到就留 `None`，由渲染层
    省掉「回复 X」那段，**不编一个昵称**。本模块不 import store/reader，只在本条动态内自洽。
    """
    els = list(root.iter("user_comment"))
    nick_of = {}
    for c in els:
        u, n = _text(c, "username"), _text(c, "nickname")
        if u and n:
            nick_of[u] = n
    out = []
    for c in els:
        ctype = _text(c, "type")
        out.append(SnsComment(
            kind=CMT_LIKE if ctype == "1" else CMT_COMMENT,
            nickname=_text(c, "nickname") or "",
            content=_text(c, "content"),
            ref_nickname=nick_of.get(_text(c, "ref_username") or ""),
            create_time=_ts(_text(c, "create_time")),
            is_rich=(_text(c, "is_rich_text") or "0") == "1",
        ))
    return tuple(out)


def parse_post(tid: str, xml_text: str) -> SnsPost | None:
    """单条动态。XML 畸形或非朋友圈结构 → None（调用方计 failed，不中断整批）。"""
    if not xml_text or not xml_text.strip():
        return None
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return None
    tlo = root.find("TimelineObject")
    if tlo is None or tlo.findtext("id") is None:
        return None
    ctype = _text(tlo, "ContentObject/type")
    feed = tlo.find("ContentObject/finderFeed")
    media = tuple(_media_of(m) for m in tlo.findall("ContentObject/mediaList/media"))
    if feed is not None:
        # 视频号的媒体在 finderFeed 下——真机 27/29 的 ContentObject/mediaList 是空的，
        # 旧解析对它完全不可见（不是「读到了没渲染」，是根本没读）。
        media += _finder_media(feed)
    cu = _text(tlo, "ContentObject/contentUrl")
    return SnsPost(
        tid=tid,
        post_id=(tlo.findtext("id") or "").strip(),
        username=(tlo.findtext("username") or "").strip(),
        nickname=_text(root, "LocalExtraInfo/nickname") or "",
        create_time=_ts(_text(tlo, "createTime")),
        text=_text(tlo, "contentDesc"),
        is_video_post=(ctype == str(TYPE_VIDEO_POST)),
        media=media,
        comments=_comments_of(root),      # 整个 SnsDataItem：互动在 LocalExtraInfo 下，不在 tlo 内
        link=SnsLink(cu, _text(tlo, "ContentObject/title")) if _is_external(cu) else None,
        finder=_finder_of(feed) if feed is not None else None,
    )


def iter_posts(account: str, *, chat_filter: str | None = None,
               months: list[str] | None = None,
               report: MomentsReport | None = None) -> Iterator[SnsPost]:
    """产出动态，**最新在前**（`createTime` 降序，与渲染层同序）。
    `chat_filter` 匹配作者 wxid；`months` 为本地月份（`media.month_of`）。

    为什么不再「边扫边 yield」：`SnsTimeLine` 的列只有
    `(tid, user_name, content, pack_info_buf)`——**没有 createTime 列**，时间在 XML 里，
    SQL 排不了。而调用方的 limit 语义是「留**最新**的 N 条」：靠扫描序截断留住的是**碰运气
    的那一段**（本机实测 limit=2 留在 2024-11-14/11-23，而真正最新的两条是
    2026-09-13/09-14，window 也跟着报成 2024-11）。故先解析整表、再按 `create_time`
    稳定排序后产出；本机 1,923 条的量级下这点内存可忽略。

    解析失败的单条（XML 畸形 / 不是朋友圈结构）计入 `report.failed` 并跳过，不中断整批；
    `report=None` 表示只读遍历（真机探针），不计数。
    """
    db = paths.db_storage(account) / "sns" / "sns.db"
    if not db.exists():
        raise errors.DataNotFound(f"{db} 不存在（该微信版本可能没有朋友圈库）")
    want = set(months) if months else None
    with decrypt.decrypted(db, keys.get_passphrase(account)) as plain:
        con = sqlite3.connect(f"file:{plain}?mode=ro", uri=True)
        con.text_factory = bytes          # 库内含非 UTF-8 字节，统一按 bytes 取
        try:
            try:
                rows = con.execute("SELECT tid, content FROM SnsTimeLine")
            except sqlite3.OperationalError as exc:
                # 旧版微信的 sns.db 没有这张表（或列被改名）。裸 sqlite3 异常会一路穿透到
                # 适配层：CLI 印 traceback、MCP 退化成通用内部错误。「core 只抛
                # AgentWeChatError 子类」这条契约在此收口（spec §5 的原话就是 SchemaDrift）。
                raise errors.SchemaDrift("朋友圈", str(db), "SnsTimeLine") from exc
            out = []
            for tid, content in rows:
                p = parse_post(str(tid), _dec(content))
                if p is None:
                    if report is not None:
                        report.failed += 1
                    continue
                if chat_filter and p.username != chat_filter:
                    continue
                if want and media.month_of(p.create_time) not in want:
                    continue
                out.append(p)
        finally:
            con.close()
    out.sort(key=lambda p: p.create_time, reverse=True)   # 稳定：同一时刻保持扫描序
    yield from out


def _dec(v) -> str:
    if v is None:
        return ""
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)


@dataclass
class MomentsReport:
    """一次导出的统计与降级说明（CLI/MCP 共用，渲染层消费）。"""
    posts: int = 0
    comments: int = 0
    likes: int = 0
    failed: int = 0                   # 解析失败被跳过的动态条数（与 media_failed 无关）
    media_local: int = 0              # 使用了本地缓存图
    media_downloaded: int = 0         # 本轮联网下载成功
    media_failed: int = 0             # 下载或解密失败
    media_garbage: int = 0            # 其中「解密后不是已知图片格式」的条数（降级行带它）
    # 未开启联网，或「不去取」（无 url / 宿主不在白名单）。封面分两种，别混为一谈：
    # **视频封面**（与本体同目录的 `{stem}_thumb.jpg`）由本体那条媒体派生、**不是**
    # `p.media` 的成员，不进任何桶；**视频号封面**由 `_finder_media` 产成一条 `p.media`
    # 成员，照常进桶。
    media_not_downloaded: int = 0
    window: tuple[int, int] | None = None      # (最早, 最新) 时间戳
    degraded: list[str] = dataclasses.field(default_factory=list)
    remaining: int = 0                # 撞上 limit 后本会导出、但没进来的动态条数
    truncated: bool = False
    out_dir: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)
