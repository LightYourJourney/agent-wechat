"""会话时间线的 Markdown 渲染。纯函数：`ChatView` → 字符串。

与 `chat_html.render` **逐一对应**：同一份 `ChatView`、同样的媒体相对路径契约、
同样的四个状态、同样的三个计数印在顶部，只是输出语法不同。不写文件、不联网、不查库。

**Markdown 的三处固有损失**（如实记在 `USAGE.md`，**不假装与 HTML 等价**）：
1. **语音 / 视频没有原生语法** —— 图片才有 `![]()`，音视频只能给链接（`[语音](…)`）；
2. **引用没有可点锚点** —— HTML 侧是 `<a href="#…">`，这里降级成 blockquote，
   位置信息丢失（**原文照常出现**，spec §2.6：渲染从不依赖回指成功）；
3. **字段表比 HTML 朴素** —— 平铺成 `- 路径：值` 列表，没有表格的列对齐。

**注入**：正文**不转义**（`&lt;` 之类会毁掉可读性），但 `<` / `>` 一律换成 HTML 实体
（`_t`）。Markdown 渲染器允许内联 HTML，原文里的 `<script>` 一旦原样透出就是一条注入
——消息内容是对手可控的，任何人能给你发一条。

**相对路径的契约（与 HTML 侧逐字一致）**：`view.out_dir` 是**本会话的媒体树根**
`{--out}/{chat_md5}`，产物写在它的 `chat/` 子目录里，于是默认 `media_prefix=".."`
恰好回到那个根。传错层级不会报错，只会让引用静默断掉。

**与 `chat_html` 共用的措辞与纯函数全在 `chat_text`**（时间格式化、媒体相对路径、四态文案、
顶部提示的三句措辞）—— spec §4.3 的「语义一致」靠**单一来源**，不是两份互校的副本
（丙 R21 的教训）：改文案只改 `chat_text` 一处，两个产物同时变，分叉不可能悄悄发生。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from . import appmsg, chat_text

if TYPE_CHECKING:
    from .appmsg import AppMsg
    from .chatview import ChatView, ViewMsg
    from .usecases import MediaRefState


def _b(head: str, tail: str) -> str:
    """`chat_text` 的共享措辞 + **本模板的**加粗标记（措辞共享、标记不共享）。"""
    return f"**{head}**{tail}"


def _t(v: str | None) -> str:
    """正文**不转义**，但 `<` / `>` 换实体 —— 见模块头的注入段。"""
    return (v or "").replace("<", "&lt;").replace(">", "&gt;")


def _media_block(m: MediaRefState, prefix: str, out_dir: str) -> str:
    """`MediaRefState` 四态 → Markdown。`none` 不印任何东西。"""
    kind = m.kind or ""
    label = chat_text.KIND_LABEL.get(kind, kind)
    if m.status == "exported":
        if not m.path:
            return "〔已导出，但拿不到产物路径 —— 状态与路径不一致〕"
        src = chat_text.media_src(m.path, out_dir, prefix)
        if kind == "image":
            return f"![{_t(label)}]({src})"
        # 音视频没有原生语法（模块头的固有损失 1）：给链接。
        return f"[{_t(label) or '媒体'}]({src})"
    if m.status == "exportable":
        return "〔{} · {}〕".format(_t(label), _t(chat_text.EXPORTABLE.get(kind, chat_text.EXPORTABLE_OTHER)))
    if m.status == "gone":
        return "〔{} · {}〕".format(_t(label), _t(chat_text.GONE))
    if m.status == "none":
        return ""
    return f"〔{_t(label)} · 未知媒体状态 {_t(m.status)}〕"


def _transfer_line(t: appmsg.Transfer) -> str:
    """转账 / 红包 → 一行可读字段。

    **字段清单、顺序与标签全在 `chat_text.transfer_fields`**（与 HTML 侧**同一份**，
    不是两份互校的副本）；本模板只加 Markdown 自己的行首 `- ` 与转义。
    """
    return "- " + _t(" · ".join(chat_text.transfer_fields(t)))


def _appmsg_lines(a: AppMsg, ref_anchor: str | None) -> list[str]:
    """appmsg 的结构化呈现（具名块优先，其余叶子进列表）。"""
    head = _t(appmsg.kind_name(a.kind))
    if a.title:
        head += "：" + _t(a.title)
    out = [f"**{head}**"]
    if a.quote is not None:
        # displayname 为空是 0.81% 的常态：补一个**显式的**「未署名」，不猜名字，
        # 也不留空 —— 留空的行看起来像渲染坏了。
        who = _t(a.quote.displayname) or "（未署名）"
        out.append(f"> **{who}**：{_t(a.quote.content)}")
        if not ref_anchor:
            # **不用 `> ` 前缀**：这条是「没有跳转目标」的元说明，不是被引用的内容。
            # 若它也写成 blockquote，用例里的 `"> " in out` 就会被这一行满足，
            # 于是「引用退化成 blockquote」这半个断言永远红不了（实测确认过）。
            out.append("（原消息不在本次范围内，无法跳转 —— 以上是引用自带的原文。）")
    if a.link is not None:
        url, title = a.link.url, a.link.title or a.link.url
        if url:
            out.append(f"[{_t(title)}]({_t(url)})")
        if a.link.des:
            out.append(_t(a.link.des))
    if a.file is not None:
        bits = [b for b in (a.file.title, a.file.fileext) if b]
        if a.file.totallen:
            bits.append(f"{a.file.totallen} 字节")
        if bits:
            out.append("文件名：" + _t(" · ".join(bits)))
    if a.transfer is not None:
        out.append(_transfer_line(a.transfer))
    for k, v in a.fields:
        out.append(f"- {_t(k)}：{_t(v)}")
    return out


def _message_lines(m: ViewMsg, prefix: str, out_dir: str) -> list[str]:
    """单条消息。`is_self` 决定「我（自己）」与「别人」的标题 —— 与 HTML 的
    `self` / `other` 同一件事（Markdown 没有对齐，只能用标记）。"""
    who = "我" if m.is_self else (m.sender or "（未知发送者）")
    tag = "（自己）" if m.is_self else ""
    out = [f"## {_t(who)}{tag} · {chat_text.when(m.ts)} · {_t(m.kind)}", ""]
    # appmsg 的 `text` 是 `[appmsg: 标题]` 形态的提示，解析成功时与下面的结构化块逐字重复。
    if m.text and m.appmsg is None:
        out += [_t(m.text), ""]
    if m.media is not None:
        block = _media_block(m.media, prefix, out_dir)
        if block:
            out += [block, ""]
    if m.appmsg is not None:
        out += _appmsg_lines(m.appmsg, m.ref_anchor) + [""]
    return out


def _notice_lines(view: ChatView) -> list[str]:
    """页首说明区：三个计数与降级都必须落在**产物顶部**（spec §4.5）。

    三个计数的措辞全部来自 `chat_text`（与 `chat_html` 同源）：改一处，两个产物同时变。
    """
    rows = [f"- 共 {len(view.msgs)} 条 · 导出时间 {chat_text.when(view.generated_at)}"]
    if view.counts:
        rows.append("- 类型：" + " · ".join(f"{_t(k)} {v}"
                                         for k, v in sorted(view.counts.items())))
    if view.truncated:
        rows.append("- " + _b(*chat_text.TRUNCATED_NOTE))
    if view.unparsed:
        rows.append("- " + _b(*chat_text.unparsed_note(view.unparsed)))
    if view.rejected:
        # 与「解不开」分开印：那是我们主动拒绝，池化会让用户以为数据坏了（spec §5）。
        rows.append("- " + _b(*chat_text.rejected_note(view.rejected)))
    for d in view.degraded:
        rows.append("- 降级：" + _t(chat_text.degradation_text(d)))
    return rows


def render(view: ChatView, media_prefix: str = "..") -> str:
    """`ChatView` → Markdown 文档字符串。纯函数、零 IO、确定性（不读时钟）。"""
    lines = [f"# 会话：{_t(view.title or view.chat_id)}", ""]
    lines += _notice_lines(view)
    lines.append("")
    if not view.msgs:
        lines += ["（没有匹配的消息。）", ""]
    for m in view.msgs:
        lines += _message_lines(m, media_prefix, view.out_dir)
    return "\n".join(lines).rstrip("\n") + "\n"
