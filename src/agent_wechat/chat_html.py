"""会话时间线的 HTML 渲染。纯函数：`ChatView` → 单文件 HTML 字符串。

形状镜像 `moments_html.py`：模块级 `_CSS`、`_esc()` 走 `html.escape(v, quote=True)`、
`render()` 返回完整文档字符串。**不写文件、不联网、不查库**——落盘是 T9 的
`chatview.write` 的事，模板这一层只吃 `ChatView`、只吐字符串（spec §4.3）。

**单文件自包含**：CSS 内联在 `<style>` 里，不引任何外部资源（无 CDN、无字体、无脚本）。
媒体以**相对路径**引用同级的既有媒体树，**不 base64 内联**——乙的实测理由：产物体积会爆。

**相对路径的契约（T9 必须照此接线）**：`view.out_dir` 是**本会话的媒体树根**
`{--out}/{chat_md5}`，其下并列 `image/` / `voice/` / `video/` 与甲新增的 `chat/`；
产物写在 `chat/` 子目录里，于是默认 `media_prefix=".."` 恰好从 `chat/` 回到那个根，
得到 `../image/2026-09/x.jpg`。**传错层级不会报错，只会让所有媒体引用静默 404**，
故 `test_media_src_is_relative_to_the_session_media_root` 把这个字符串钉住。

**引用（`refermsg`）**：原文一直在 `AppMsg.quote.content` 里，渲染**从不依赖回指成功**
（spec §2.6）。`ref_anchor` 非空 → 整块包成 `<a href="#…">` 可点跳；为空 → 显示原文
加一句「原消息不在本次范围内」。**两种情况下原文都照常出现。**

**所有文本一律走 `_esc`**：消息正文、昵称、字段值都是对手可控的（任何人能给你发一条
appmsg），`<script>` 不许原样进产物。

**与 `chat_md` 共用的措辞/纯函数全在 `chat_text`**（时间格式化、媒体相对路径、四态文案、
顶部提示的三句措辞）。spec §4.3 的「语义一致」靠的是**单一来源**，不是两份互校的副本
（丙 R21 的教训）——改文案只改 `chat_text` 一处，两个产物同时变。

**不 import `usecases`**：`appmsg` / `chat_text` 之外只 import 标准库；`MediaRefState` /
`Degradation` 只在 `if TYPE_CHECKING:` 下取类型、运行期按结构读属性
（`.status` / `.kind` / `.path`）。理由与 `chatview` 对同一类型的处理相同：模板层不能把
usecases 拉进运行期依赖。
"""
from __future__ import annotations

import html
from typing import TYPE_CHECKING

from . import appmsg, chat_text

if TYPE_CHECKING:
    from .appmsg import AppMsg
    from .chatview import ChatView, ViewMsg
    from .usecases import MediaRefState

_CSS = """body{font-family:-apple-system,"PingFang SC",sans-serif;max-width:820px;
margin:0 auto;padding:16px;background:#f7f7f7;color:#1a1a1a;line-height:1.6}
.notice{background:#fff8e1;border:1px solid #ffe082;border-radius:8px;padding:12px;
margin-bottom:16px;font-size:13px}
.msg{background:#fff;border-radius:10px;padding:12px;margin-bottom:10px;max-width:80%}
.msg.self{margin-left:auto;border-left:3px solid #07c160}
.msg.other{margin-right:auto;border-left:3px solid #cfcfcf}
.meta{font-size:12px;color:#888}
.who{font-weight:600;color:#333;margin-right:8px}
.kind{background:#eee;border-radius:4px;padding:0 4px}
.text{margin:6px 0;white-space:pre-wrap}
.media img{max-width:100%;border-radius:6px}
.media audio,.media video{max-width:100%}
.ph{background:#eee;border-radius:6px;padding:8px;color:#666;font-size:12px;margin:4px 0}
.app{background:#fafafa;border:1px solid #eee;border-radius:6px;padding:8px;
margin:6px 0;font-size:13px}
.app .ttl{font-weight:600;margin-bottom:4px}
.app table{border-collapse:collapse}
.app td{padding:1px 10px 1px 0;vertical-align:top}
.q{border-left:3px solid #ddd;padding-left:8px;color:#555;margin:4px 0}
.q .src{color:#999;font-size:12px}
.empty{color:#999}"""


def _b(head: str, tail: str) -> str:
    """`chat_text` 的共享措辞 + **本模板的**加粗标记（措辞共享、标记不共享）。"""
    return f"<b>{head}</b>{tail}"


def _esc(v: str | None) -> str:
    return html.escape(v or "", quote=True)


def _media_block(m: MediaRefState, prefix: str, out_dir: str) -> str:
    """`MediaRefState` 四态 → HTML。`none` 不印任何东西（那条消息本来就没有媒体）。"""
    kind = m.kind or ""
    label = chat_text.KIND_LABEL.get(kind, kind)
    if m.status == "exported":
        if not m.path:
            # 状态说「导出了」却没有路径：这是上游的不一致，**不能静默成空白**
            # （空白看起来与「这条消息没有媒体」一模一样）。
            return '<div class="ph">[已导出，但拿不到产物路径 —— 状态与路径不一致]</div>'
        src = _esc(chat_text.media_src(m.path, out_dir, prefix))
        if kind == "image":
            return f'<div class="media"><img loading="lazy" src="{src}" alt="{_esc(label)}"></div>'
        if kind == "voice":
            return f'<div class="media"><audio controls src="{src}"></audio></div>'
        if kind == "video":
            return f'<div class="media"><video controls src="{src}"></video></div>'
        return f'<div class="media"><a href="{src}">{_esc(label or "媒体")}</a></div>'
    if m.status == "exportable":
        why = chat_text.EXPORTABLE.get(kind, chat_text.EXPORTABLE_OTHER)
        return f'<div class="ph">[{_esc(label)} · {_esc(why)}]</div>'
    if m.status == "gone":
        return f'<div class="ph">[{_esc(label)} · {_esc(chat_text.GONE)}]</div>'
    if m.status == "none":
        return ""
    # 四态之外的取值（上游将来加枚举值）：照实印出来，不猜也不吞。
    return f'<div class="ph">[{_esc(label)} · 未知媒体状态 {_esc(m.status)}]</div>'


def _quote_block(q: appmsg.Quote, ref_anchor: str | None) -> str:
    """引用：`ref_anchor` 有值整块可点跳，没有则内联显示原文。**原文两种情况都印。**"""
    # displayname 为空是 0.81% 的常态：补一个**显式的**「未署名」—— 不猜名字，也不留空
    # （留空渲染成「引用 ：」，看起来像坏了）。正文为空（0.56%）则照原样留空。
    who = _esc(q.displayname) or "（未署名）"
    body = _esc(q.content)             # 为空是 0.56% 的常态，不编占位文案
    inner = f'<div class="src">引用 {who}：</div>{body}'
    if not ref_anchor:
        inner += ('<div class="src">原消息不在本次范围内，无法跳转 —— '
                  "以上是引用自带的原文。</div>")
        return f'<div class="q">{inner}</div>'
    return f'<a href="#{_esc(ref_anchor)}"><div class="q">{inner}</div></a>'


def _transfer_block(t: appmsg.Transfer) -> str:
    """转账 / 红包（type 2000）→ `spec §4.5` 信封字段的可读列表。

    **字段清单、顺序与标签全在 `chat_text.transfer_fields`**（改一处，两个产物同时变）；
    本模板只套自己的分隔符与转义——「措辞共享、标记不共享」，与上方 `_b` 同一条规矩。
    """
    return " · ".join(_esc(r) for r in chat_text.transfer_fields(t))


def _appmsg_block(a: AppMsg, ref_anchor: str | None) -> str:
    """appmsg 的结构化呈现：具名块（引用/链接/文件/转账）优先，其余叶子进字段表。"""
    head = _esc(appmsg.kind_name(a.kind))
    if a.title:
        head += "：" + _esc(a.title)
    inner = [f'<div class="ttl">{head}</div>']
    if a.quote is not None:
        inner.append(_quote_block(a.quote, ref_anchor))
    if a.link is not None:
        url, title = a.link.url, a.link.title or a.link.url
        if url:
            inner.append(f'<div><a href="{_esc(url)}">{_esc(title)}</a></div>')
        if a.link.des:
            inner.append(f"<div>{_esc(a.link.des)}</div>")
    if a.file is not None:
        bits = [b for b in (a.file.title, a.file.fileext) if b]
        if a.file.totallen:
            bits.append(f"{a.file.totallen} 字节")
        if bits:
            inner.append("<div>" + _esc(" · ".join(bits)) + "</div>")
    if a.transfer is not None:
        inner.append(f"<div>{_transfer_block(a.transfer)}</div>")
    if a.fields:
        rows = "".join(f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>"
                       for k, v in a.fields)
        inner.append(f'<table>{rows}</table>')
    return '<div class="app">' + "".join(inner) + "</div>"


def _msg_block(m: ViewMsg, prefix: str, out_dir: str) -> str:
    """单条消息。`is_self` 决定 `self` / `other` —— 左右对齐与「谁发的」都由它派生。"""
    who = "我" if m.is_self else (m.sender or "（未知发送者）")
    cls = "self" if m.is_self else "other"
    inner = [f'<div class="meta"><span class="who">{_esc(who)}</span>'
             f'<span class="when">{chat_text.when(m.ts)}</span>'
             f'<span class="kind">{_esc(m.kind)}</span></div>']
    # appmsg 的 `text` 是 `[appmsg: 标题]` 形态的提示（`content` 语义不变，见 spec §2.4），
    # 解析成功时它与下面的结构化块逐字重复，故只在没有结构化块时才印它。
    if m.text and m.appmsg is None:
        inner.append(f'<div class="text">{_esc(m.text)}</div>')
    if m.media is not None:
        inner.append(_media_block(m.media, prefix, out_dir))
    if m.appmsg is not None:
        inner.append(_appmsg_block(m.appmsg, m.ref_anchor))
    return f'<div class="msg {cls}" id="{_esc(m.anchor)}">{"".join(inner)}</div>'


def _notice(view: ChatView) -> str:
    """页首说明区：条数、类型分布、以及**必须印在产物顶部**的三个计数（spec §4.5）。

    三个计数的措辞全部来自 `chat_text`（与 `chat_md` 同源）：改一处，两个产物同时变。
    """
    rows = [f"会话 <b>{_esc(view.title or view.chat_id)}</b> · 共 <b>{len(view.msgs)}</b> 条"
            f" · 导出时间 {chat_text.when(view.generated_at)}"]
    if view.counts:
        rows.append("类型：" + " · ".join(f"{_esc(k)} {v}"
                                        for k, v in sorted(view.counts.items())))
    if view.truncated:
        rows.append(_b(*chat_text.TRUNCATED_NOTE))
    if view.unparsed:
        rows.append(_b(*chat_text.unparsed_note(view.unparsed)))
    if view.rejected:
        # 与「解不开」**分开印**：那是我们主动拒绝，池化会让用户以为数据坏了（spec §5）。
        rows.append(_b(*chat_text.rejected_note(view.rejected)))
    for d in view.degraded:
        rows.append("降级：" + _esc(chat_text.degradation_text(d)))
    return '<div class="notice">' + "<br>".join(rows) + "</div>"


def render(view: ChatView, media_prefix: str = "..") -> str:
    """`ChatView` → 完整 HTML 文档字符串。纯函数、零 IO、确定性（不读时钟）。"""
    body = [_msg_block(m, media_prefix, view.out_dir) for m in view.msgs]
    if not body:
        body.append('<div class="msg empty">没有匹配的消息。</div>')
    return ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{_esc(view.title or '会话导出')}</title><style>{_CSS}</style></head>"
            f"<body>{_notice(view)}{''.join(body)}</body></html>")
