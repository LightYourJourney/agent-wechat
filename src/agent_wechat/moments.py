"""朋友圈解析：sns.db 的 TimelineObject XML → 结构化动态。

纯读取：不联网、不 import store/reader。解密复用既有 decrypt/keys/paths。
"""
from __future__ import annotations

import dataclasses
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterator

from . import decrypt, errors, keys, media, paths

TYPE_VIDEO_POST = 15
KIND_IMAGE, KIND_VIDEO, KIND_OTHER = "image", "video", "other"
CMT_LIKE, CMT_COMMENT = "like", "comment"


@dataclass(frozen=True)
class SnsMedia:
    """一条媒体引用。`token`/`key` 是鉴权材料——repr 恒隐藏（同 MediaKeys 规则）。"""
    kind: str
    url: str | None = None
    token: str | None = None
    key: str | None = None
    enc_idx: str | None = None
    thumb: str | None = None
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


def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    v = el.findtext(path)
    return v.strip() if v and v.strip() else None


def _int(v: str | None) -> int:
    return int(v) if v and v.isdigit() else 0


def _media_of(m: ET.Element) -> SnsMedia:
    t = _text(m, "type")
    kind = KIND_VIDEO if t == "6" else KIND_IMAGE if t == "2" else KIND_OTHER
    u, th = m.find("url"), m.find("thumb")
    lmu = m.find("LivePhoto/liveMedia/url")
    lmk = m.find("LivePhoto/liveMedia/enc")
    return SnsMedia(
        kind=kind,
        url=(u.text or "").strip() or None if u is not None else None,
        # token/key/enc_idx 是**属性**不是子元素——实测踩过的坑
        token=u.get("token") if u is not None else None,
        key=u.get("key") if u is not None else None,
        enc_idx=u.get("enc_idx") if u is not None else None,
        md5=u.get("md5") if u is not None else None,
        thumb=(th.text or "").strip() or None if th is not None else None,
        video_duration=_int(_text(m, "videoDuration")) or None,
        # 实况照片：url 无 token → 本机不可下载（spec §8）；密钥在 liveMedia/enc@key
        live_url=(lmu.text or "").strip() or None if lmu is not None else None,
        live_key=lmk.get("key") if lmk is not None else None,
    )


def _comments_of(root: ET.Element) -> tuple[SnsComment, ...]:
    """`<user_comment>` 同时出现在评论列表与 like_user_list 内，按 type 区分：
    1=点赞、2=评论、其余（4）按评论处理。"""
    out = []
    for c in root.iter("user_comment"):
        ctype = _text(c, "type")
        out.append(SnsComment(
            kind=CMT_LIKE if ctype == "1" else CMT_COMMENT,
            nickname=_text(c, "nickname") or "",
            content=_text(c, "content"),
            ref_nickname=_text(c, "ref_username"),
            create_time=_int(_text(c, "create_time")),
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
    return SnsPost(
        tid=tid,
        post_id=(tlo.findtext("id") or "").strip(),
        username=(tlo.findtext("username") or "").strip(),
        nickname=_text(root, "LocalExtraInfo/nickname") or "",
        create_time=_int(_text(tlo, "createTime")),
        text=_text(tlo, "contentDesc"),
        is_video_post=(ctype == str(TYPE_VIDEO_POST)),
        media=tuple(_media_of(m) for m in tlo.findall("ContentObject/mediaList/media")),
        comments=_comments_of(tlo),
    )


def iter_posts(account: str, *, chat_filter: str | None = None,
               months: list[str] | None = None) -> Iterator[SnsPost]:
    """流式产出动态。`chat_filter` 匹配作者 wxid；`months` 为本地月份（`media.month_of`）。"""
    db = paths.db_storage(account) / "sns" / "sns.db"
    if not db.exists():
        raise errors.DataNotFound(f"{db} 不存在（该微信版本可能没有朋友圈库）")
    want = set(months) if months else None
    with decrypt.decrypted(db, keys.get_passphrase(account)) as plain:
        con = sqlite3.connect(f"file:{plain}?mode=ro", uri=True)
        con.text_factory = bytes          # 库内含非 UTF-8 字节，统一按 bytes 取
        try:
            for tid, content in con.execute("SELECT tid, content FROM SnsTimeLine"):
                p = parse_post(str(tid), _dec(content))
                if p is None:
                    continue
                if chat_filter and p.username != chat_filter:
                    continue
                if want and media.month_of(p.create_time) not in want:
                    continue
                yield p
        finally:
            con.close()


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
    media_local: int = 0              # 使用了本地缓存图
    media_downloaded: int = 0         # 本轮联网下载成功
    media_failed: int = 0             # 下载或解密失败
    media_not_downloaded: int = 0     # 未开启联网，或该媒体不可下载
    window: tuple[int, int] | None = None      # (最早, 最新) 时间戳
    degraded: list[str] = dataclasses.field(default_factory=list)
    truncated: bool = False
    out_dir: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)
