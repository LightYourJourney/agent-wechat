"""朋友圈 HTML 时间线渲染。纯函数：动态 + 报告 → 单文件 HTML 字符串。

不写文件、不联网。产物是**单文件自包含**（内联 CSS），媒体以相对路径引用同级
`media/` 目录——不 base64 内联，避免产物膨胀到上百 MB。
"""
from __future__ import annotations

import datetime
import html
from typing import Iterable

from . import moments

_CSS = """body{font-family:-apple-system,"PingFang SC",sans-serif;max-width:720px;
margin:0 auto;padding:16px;background:#f7f7f7;color:#1a1a1a;line-height:1.6}
.notice{background:#fff8e1;border:1px solid #ffe082;border-radius:8px;
padding:12px;margin-bottom:16px;font-size:13px}
.post{background:#fff;border-radius:10px;padding:14px;margin-bottom:14px}
.who{font-weight:600}.when{color:#888;font-size:12px;margin-left:8px}
.text{margin:8px 0;white-space:pre-wrap}
.media img{max-width:100%;border-radius:6px;margin:4px 0}
.ph{background:#eee;border-radius:6px;padding:8px;color:#666;font-size:12px;margin:4px 0}
.likes,.cmts{margin-top:8px;padding-top:8px;border-top:1px solid #eee;font-size:13px}
.likes{color:#576b95}.empty{color:#999}"""


def _esc(v: str | None) -> str:
    return html.escape(v or "", quote=True)


def _when(ts: int) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""


def _media_block(m: moments.SnsMedia) -> str:
    if m.local:
        if m.kind == moments.KIND_VIDEO:
            return f'<div class="media"><video src="{_esc(m.local)}" controls></video></div>'
        return f'<div class="media"><img src="{_esc(m.local)}" loading="lazy" alt=""></div>'
    why = "远程，未下载" if m.kind != moments.KIND_OTHER else "外部链接媒体"
    return f'<div class="ph">[{why}：{_esc(m.kind)}]</div>'


def _comments_block(cs: tuple[moments.SnsComment, ...]) -> str:
    likes = [c for c in cs if c.kind == moments.CMT_LIKE]
    cmts = [c for c in cs if c.kind != moments.CMT_LIKE]
    out = []
    if likes:
        names = "，".join(_esc(c.nickname) for c in likes)
        out.append(f'<div class="likes">♥ {names}</div>')
    if cmts:
        rows = []
        for c in cmts:
            body = _esc(c.content) if c.content else "[表情评论]"
            who = _esc(c.nickname)
            if c.ref_nickname:
                rows.append(f"<div>{who} 回复 {_esc(c.ref_nickname)}：{body}</div>")
            else:
                rows.append(f"<div>{who}：{body}</div>")
        out.append('<div class="cmts">' + "".join(rows) + "</div>")
    return "".join(out)


def _notice(report: moments.MomentsReport, n: int) -> str:
    """页首说明区——spec §2.4 的硬要求：必须写明这是缓存窗口。"""
    lo = hi = "—"
    if report.window:
        # `create_time == 0` 是「时间戳没解析出来」，不是 1970 年（`_ts` 的消毒结果）。
        # `_when(0)` 返回空串，直接用它会把这一行渲染成「覆盖 <b> ~ </b>」——空档看不出
        # 是「没有」还是「坏了」，故保留占位符。
        if report.window[0]:
            lo = _when(report.window[0])[:10]
        if report.window[1]:
            hi = _when(report.window[1])[:10]
    rows = [
        f"本产物覆盖 <b>{lo} ~ {hi}</b>，共 <b>{n}</b> 条动态。",
        "<b>这是微信本地缓存窗口，不是完整历史</b>——更早的动态不在本机。",
        (f"媒体：本地 {report.media_local} · 已下载 {report.media_downloaded} · "
         f"未下载 {report.media_not_downloaded} · 失败 {report.media_failed}。"),
        f"互动：评论 {report.comments} · 点赞 {report.likes}。",
    ]
    if report.failed:
        # 「0 条动态」与「这个账号没有朋友圈」必须能分辨：解析失败是不出声的那一类，
        # 不写在这里，整页看起来就是一次成功的空导出。
        rows.append(f"<b>解析失败 {report.failed} 条</b>（XML 结构不符，已跳过）。")
    if report.truncated:
        rows.append("<b>本次导出被 limit 截断</b>，可调大 --limit 重跑。")
    for d in report.degraded:
        rows.append(f"降级：{_esc(d)}")
    return '<div class="notice">' + "<br>".join(rows) + "</div>"


def render(posts: Iterable[moments.SnsPost], report: moments.MomentsReport) -> str:
    items = sorted(posts, key=lambda p: p.create_time, reverse=True)
    body = []
    for p in items:
        media = "".join(_media_block(m) for m in p.media)
        text = f'<div class="text">{_esc(p.text)}</div>' if p.text else ""
        body.append(
            f'<div class="post"><div><span class="who">{_esc(p.nickname or p.username)}</span>'
            f'<span class="when">{_when(p.create_time)}</span></div>'
            f"{text}{media}{_comments_block(p.comments)}</div>")
    if not items:
        body.append('<div class="post empty">没有匹配的动态。</div>')
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>朋友圈导出</title><style>{_CSS}</style></head><body>"
            f"{_notice(report, len(items))}{''.join(body)}</body></html>")
