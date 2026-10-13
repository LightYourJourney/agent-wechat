"""朋友圈 HTML 时间线渲染。纯函数：动态 + 报告 → 单文件 HTML 字符串。

不写文件、不联网。产物是**单文件自包含**（内联 CSS），媒体以相对路径引用同级
`media/` 目录——不 base64 内联，避免产物膨胀到上百 MB。
"""
from __future__ import annotations

import datetime
import html
import re
from typing import Iterable

from . import chat_text, moments

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
.likes{color:#576b95}.empty{color:#999}
.link{margin:8px 0;padding:8px;background:#f2f6ff;border-radius:6px;font-size:14px}
.link a{color:#576b95;text-decoration:none}
.link .host{color:#999;font-size:12px;margin-left:8px}
.finder{margin:8px 0;color:#576b95;font-size:13px;font-weight:600}
.fdesc{color:#666;font-size:13px;margin:2px 0 8px}"""


def _esc(v: str | None) -> str:
    return html.escape(v or "", quote=True)


def _when(ts: int) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""


def _media_block(m: moments.SnsMedia) -> str:
    if m.local:
        if m.kind == moments.KIND_VIDEO:
            block = f'<div class="media"><video src="{_esc(m.local)}" controls></video></div>'
            if m.thumb_local:
                block += (f'<div class="media"><img src="{_esc(m.thumb_local)}" '
                          f'loading="lazy" alt=""></div>')
            return block
        return f'<div class="media"><img src="{_esc(m.local)}" loading="lazy" alt=""></div>'
    why = "远程，未下载" if m.kind != moments.KIND_OTHER else "外部链接媒体"
    return f'<div class="ph">[{why}：{_esc(m.kind)}]</div>'


# WHATWG 把「末段是数字」的 host 当 IPv4 试解析（纯十进制、`0x…` 十六进制、前导零按
# 八进制）；Python 的 `urlsplit` 只管切字符串。于是 `https://2852039166/promo` 徽章上
# 写着 `2852039166`，浏览器实际去 **169.254.169.254**（云 metadata / link-local）。
_NUMERIC_LABEL = re.compile(r"^(?:[0-9]+|0[xX][0-9a-fA-F]*)\Z")


def _whatwg_ipv4(host: str) -> bool:
    """这个 host 会被 WHATWG 当 IPv4 解析吗（而徽章会把它原样显示出来）。

    判据是 WHATWG「ends in a number」那一步的**超集**：末段是纯数字或 `0x`/`0X`
    开头的十六进制即为真——前导零的八进制（`017700000001` → 127.0.0.1）也是纯数字，
    裸 IPv4（`192.168.1.1`）同样命中；真机上外链都是域名，一并降级不损失什么。
    先去掉末尾的点：`192.168.1.1.` 是绝对域名形式，浏览器照样落到 192.168.1.1。
    二进制 `0b101` 不在判据内——实测 node 的 `new URL("https://0b101/x").hostname`
    就是 `0b101`，WHATWG 不认它。
    """
    label = host.rstrip(".").rsplit(".", 1)[-1]
    return bool(_NUMERIC_LABEL.match(label))


def _link_block(link: moments.SnsLink) -> str:
    """外链块——本模块唯一有真实安全面的渲染。

    `html.escape` 挡不住 scheme（`javascript:alert(1)` 转义后仍是可执行链接），
    也挡不住**反斜杠或非 ASCII 主机**：Python 的 `urlsplit` 不把 `\\` 当分隔符，
    浏览器按 WHATWG 会——`https://evil.com\\.mp.weixin.qq.com/x` 显示成
    `evil.com\\.mp.weixin.qq.com`，点下去却落到 `evil.com`。「让用户看得见去哪」
    这道防线必须建立在两套 URL 语义一致的输入上，否则它本身就是骗人的。

    反斜杠**必须查 `netloc` 而不是 `hostname`**：`hostname` 已经把 userinfo 丢掉，
    而 WHATWG 先按 `\\` 切一刀——`https://evil.com\\@mp.weixin.qq.com/x` 的 `hostname`
    是干干净净的 `mp.weixin.qq.com`，徽章显示它，浏览器却落到 `evil.com`。

    同一族里还有第三类：**WHATWG 会当 IPv4 解析的 host**（`_whatwg_ipv4`）与
    **含 `%` 的 host**。`urlsplit` 不解百分号编码，WHATWG 解——
    `https://2852039166%2e/promo` 的 `hostname` 是 `2852039166%2e`（看着不像地址），
    浏览器解码成 `2852039166.` 后照样落到 169.254.169.254。**只加数字判据挡不住它**，
    故 `%` 一并拒掉（合法主机名里没有 `%`，正常域名/IPv6 字面量都不含）。
    """
    # EA-034：判据已搬到 `chat_text.url_is_safe`（两个模板共用一处）——本函数只留呈现。
    if not chat_text.url_is_safe(link.url):
        return f'<div class="ph">[链接：{_esc(link.url)}]</div>'
    s = moments.split_url(link.url)
    host = (s.hostname or "").lower() if s is not None else ""   # 徽章显示用（判据已过）
    title = _esc(link.title) or _esc(link.url)
    return (f'<div class="link"><a href="{_esc(link.url)}" target="_blank" '
            f'rel="noopener noreferrer nofollow">{title}</a>'
            f'<span class="host">{_esc(host)}</span></div>')


def _finder_block(f: moments.SnsFinder) -> str:
    """视频号块：封面已由 `_media_block` 渲染，这里只补元数据。"""
    out = [f'<div class="finder">视频号 · {_esc(f.nickname)}</div>']
    if f.desc:
        out.append(f'<div class="fdesc">{_esc(f.desc)}</div>')
    return "".join(out)


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
        (f"媒体：已下载 {report.media_downloaded} · "
         f"未下载 {report.media_not_downloaded} · 失败 {report.media_failed}。"),
        f"互动：评论 {report.comments} · 点赞 {report.likes}。",
    ]
    if report.failed:
        # 「0 条动态」与「这个账号没有朋友圈」必须能分辨：解析失败是不出声的那一类，
        # 不写在这里，整页看起来就是一次成功的空导出。
        rows.append(f"<b>解析失败 {report.failed} 条</b>（XML 结构不符，已跳过）。")
    if report.truncated:
        rows.append(f"<b>本次导出被 limit 截断</b>，还有 {report.remaining} 条更早的动态"
                    "没进来 —— 调大 --limit 重跑。")
    for d in report.degraded:
        rows.append(f"降级：{_esc(d)}")
    return '<div class="notice">' + "<br>".join(rows) + "</div>"


def render(posts: Iterable[moments.SnsPost], report: moments.MomentsReport) -> str:
    items = sorted(posts, key=lambda p: p.create_time, reverse=True)
    body = []
    for p in items:
        media = "".join(_media_block(m) for m in p.media)
        text = f'<div class="text">{_esc(p.text)}</div>' if p.text else ""
        link = _link_block(p.link) if p.link else ""
        finder = _finder_block(p.finder) if p.finder else ""
        body.append(
            f'<div class="post"><div><span class="who">{_esc(p.nickname or p.username)}</span>'
            f'<span class="when">{_when(p.create_time)}</span></div>'
            f"{text}{media}{link}{finder}{_comments_block(p.comments)}</div>")
    if not items:
        body.append('<div class="post empty">没有匹配的动态。</div>')
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>朋友圈导出</title><style>{_CSS}</style></head><body>"
            f"{_notice(report, len(items))}{''.join(body)}</body></html>")
