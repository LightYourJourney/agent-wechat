"""会话时间线的 Markdown 渲染。纯函数：`ChatView` → 字符串。

与 `chat_html.render` **逐一对应**：同一份 `ChatView`、同样的媒体相对路径契约、
同样的四个状态、同样的三个计数印在顶部，只是输出语法不同。不写文件、不联网、不查库。

**Markdown 的三处固有损失**（如实记在 `USAGE.md`，**不假装与 HTML 等价**）：
1. **语音 / 视频没有原生语法** —— 图片才有 `![]()`，音视频只能给链接（`[语音](…)`）；
2. **引用没有可点锚点** —— HTML 侧是 `<a href="#…">`，这里降级成 blockquote，
   位置信息丢失（**原文照常出现**，spec §2.6：渲染从不依赖回指成功）；
3. **字段表比 HTML 朴素** —— 平铺成 `- 路径：值` 列表，没有表格的列对齐。
（原先还有第 4 条「Markdown 语法不转义」——**已在 EA-037/EA-034 收口**，见下。）

**注入（EA-037，2026-10-14 收口）**：**用户内容**（正文 / 昵称 / 转账字段 / appmsg 的
标题、描述、引用正文、文件名、字段表 / 会话标题）一律走 `_esc_md` **硬转义**——行内结构
字符（`` ` `` `*` `_` `[` `]` `(` `)` `!` `|` `~`）在任何位置转义；`#` `>` `-` `+` `*` `=`
与 `1.` **只在行首**；行首 4 空格 / 制表符（缩进代码块）也转义。分档的理由：一律转义会把
`¥100.00` 弄成「`¥100` + 反斜杠 + `00`」（渲染无差异、源却脏）。**我们的措辞**（计数 / 时间 / 状态说明）
仍走 `_t`（只换 `<` / `>` 实体）——它们由程序生成，转义只会让产物难读。
「行首」按 CommonMark 的算法算（最多 3 空格 / 凑不满 4 列的制表符之后仍算行首），行结束只认
`\n` `\r` `\r\n` 并统一成 `\n`——两条都是复审实测出来的漏面，细节在 `_esc_md` 的 docstring。
appmsg 的链接过**两道**判据：`chat_text.url_is_safe`（与 HTML、朋友圈同一处，问 scheme/host
会不会骗人）与 `_md_dest_ok`（本模块独有，问这个串能不能原样写进 `](…)`）；任一道不过就
降级成可见纯文本。此前一条 `![](http://evil/x.png)` 的消息会变成**远程图片引用**，现在不是了
——换行 / 空格 / `)` 这几种形态是修轮 2 才堵上的（见 `_md_dest_ok`）。

**相对路径的契约（与 HTML 侧逐字一致）**：`view.out_dir` 是**本会话的媒体树根**
`{--out}/{chat_md5}`，产物写在它的 `chat/` 子目录里，于是默认 `media_prefix=".."`
恰好回到那个根。传错层级不会报错，只会让引用静默断掉。

**与 `chat_html` 共用的措辞与纯函数全在 `chat_text`**（时间格式化、媒体相对路径、四态文案、
顶部提示的三句措辞）—— spec §4.3 的「语义一致」靠**单一来源**，不是两份互校的副本
（丙 R21 的教训）：改文案只改 `chat_text` 一处，两个产物同时变，分叉不可能悄悄发生。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import re

from . import appmsg, chat_text

if TYPE_CHECKING:
    from .appmsg import AppMsg
    from .chatview import ChatView, ViewMsg
    from .usecases import MediaRefState


def _b(head: str, tail: str) -> str:
    """`chat_text` 的共享措辞 + **本模板的**加粗标记（措辞共享、标记不共享）。"""
    return f"**{head}**{tail}"


def _t(v: str | None) -> str:
    """**我们的措辞**：`<` / `>` 换实体，其余原样（见模块头的注入段）。"""
    return (v or "").replace("<", "&lt;").replace(">", "&gt;")


# Markdown 的**行内**结构字符（在任何位置都有语义）；反斜杠转义是 CommonMark 的正式做法。
# **不含 `.` `-` `+` `#` `>`**：它们只在**行首**才有结构义，一律转义会把 `¥100.00` 弄成
# `¥100\.00`（渲染无差异、源却脏了）——行首那一档在 `_esc_md` 里单独处理。
_MD_INLINE = "\\`*_[]()!|~"
_MD_LINE_START = "#>+-*="          # `=` 是 setext 标题（`标题\n===`）——复审实测的漏挡面


_OL_START = re.compile(r"\d+\.")          # 行首的 `1.` / `12.`（有序列表）

# CommonMark 的行结束：`\n` / `\r` / `\r\n`（**只有这三种**——`str.splitlines()` 还会按
# `\v` `\f` `\x1c` ` ` 切，那是 Python 的口径，不是规范的口径，会把一行内容劈成两行）。
_LINE_BREAK = re.compile(r"\r\n|\r|\n")

# Markdown 的**链接目的地**（`](…)`）在源码层面装不下的字符：ASCII 控制字符（空白与行结束
# 都在其中，`\v` `\f` 也是空白）、DEL、以及 `(` `)` `<` `>`——前两者改变嵌套层级、后两者
# 改变定界。见 `_md_dest_ok`。
_MD_DEST_UNSAFE = frozenset(map(chr, range(0x20))) | frozenset("\x7f()<>")

# 打断缩进代码块用的实体：把行首那**一个**空白字符写成实体（详见 `_esc_md` 的缩进档）。
_WS_ENTITY = {" ": "&#32;", "\t": "&#9;"}


def _md_dest_ok(url: str) -> bool:
    """这个 URL 能**原样**写进 `](…)` 吗——问的不是「它是不是一个安全的 URL」。

    安全与否是 `chat_text.url_is_safe` 的事（与 HTML 侧共用）；这里问的是「把它原样拼进
    目的地之后，**目的地在哪结束**还由我们说了算吗」。两者会分叉，实测三条：
      · `urlsplit` 会**静默删掉** `\\t` `\\r` `\\n` 再解析（CPython 的
        `_UNSAFE_URL_BYTES_TO_REMOVE`），于是 `url_is_safe` 压根看不见它们，而产物里落的是
        **带真换行**的字符串——`[点我](https://ok.example/promo<换行>![](http://evil/x))`，
        pandoc 的 gfm 与 commonmark **都**渲染出 `<img src="http://evil/x" />`（实测）；
      · `)` 直接终止目的地、空格亦然，其后的 `![](...)` 就落回正文（同上，两种 reader 都实测出图）；
      · 载具是真的：appmsg 的 `<url>` 值经 `appmsg.parse` 原样带出（凭据过滤不拦它）。

    **判据不进 `chat_text.url_is_safe`**：那边问的是 scheme/host 会不会骗人，而 `()` 在 URL 里
    合法且常见（`…/wiki/Foo_(bar)`），加进共用判据会让 HTML 侧也拒掉正常的维基链接。
    这里只决定「要不要把它做成**可点**链接」：判否就退成纯文本 `[链接：…]`——URL 仍看得见，
    只是不可点，与 HTML 侧不安全的那些同一个处置。
    """
    return not any(ch in _MD_DEST_UNSAFE for ch in url)


def _indent_width(line: str) -> int:
    """行首缩进的**列数**：制表符按 4 列制表位推进（CommonMark 的算法）。

    数「几个空格」会漏掉 `" \\t"` 这种混排——它凑满 4 列、在 gfm 下就是 `<pre><code>`（复验实测）。
    """
    col = 0
    for ch in line:
        if ch == " ":
            col += 1
        elif ch == "\t":
            col += 4 - col % 4
        else:
            break
    return col


def _esc_md(v: str | None) -> str:
    """**用户内容**的 Markdown 硬转义（EA-037）：正文 / 昵称 / 转账字段里的 Markdown
    元字符一律反斜杠转义。

    不转义的后果（审计实测，四条都会真的发生）：
      · `![](http://evil/x.png)` → 产物里成为**远程图片引用**：打开导出就会去打那个 URL；
      · `[点我](javascript:…)` → 可点链接；
      · 行首 `#` / `>` → **假标题 / 假引用块**（看着像我们生成的）；
      · 行首 `---` → 假分隔线。
     `<`/`>` 沿用实体转义（`_t`）——两者都成立，实体更保险（HTML 侧也安全）。

    分两档：**行内**字符（`` ` `` `*` `_` `[` `]` `(` `)` `!` `|` `~`）在任何位置都转义；
    **块级**标记（`#` `>` `-` `+` `*` `=` 与 `1.`）只在**行首**转义——但「行首」按 CommonMark
    的算法算：**最多 3 个空格**（或凑不满 4 列的制表符）之后仍算行首（复验实测
    `"   # 假标题"` 与 `"标题\\n ==="` 都真的成了结构）；缩进**满 4 列**则是缩进代码块，
    把行首那一个空白字符写成**实体**来打断它（见下面的缩进档）。

    行结束按规范只认 `\\n` / `\\r` / `\\r\\n`，并且**统一成 `\\n`**：只按 `\\n` 切行时
    `"abc\\r# 假标题"` 是一行、行首是 `abc`，而按规范切行的渲染器会看到一个真的 ATX 标题。
    """
    out = []
    for line in _LINE_BREAK.split(_t(v)):
        lead = len(line) - len(line.lstrip(" \t"))     # 缩进=空白前缀（`_indent_width` 再折算列数）
        ol = _OL_START.match(line[lead:])              # `1` 与 `.` 都不在行内集合，转义不位移
        # 缩进代码块：**列数** ≥ 4（制表符按 4 列制表位推进）**且行内有内容**。空白行不算
        # ——CommonMark 的缩进代码块由非空白行组成，动它只会把前后两个段落并成一个（实测）。
        indented = _indent_width(line) >= 4 and bool(line.strip())
        line = "".join(("\\" + ch) if ch in _MD_INLINE else ch for ch in line)
        body = line[lead:]                             # 行内转义只插在缩进之后，前缀仍是原样
        # **块级那一档必须在行内转义之后、且判据落在转义后的行上**：`*` 既在行内集合、又在
        # 块级集合里，行内那一轮已经把它转义成 `\*`，此时 `body[0]` 是 `\`、不再命中块级集合
        # ——判据若落在原行上就会再插一个反斜杠，得到 `\\*`（两个）。`\` 自身在 `_MD_INLINE`
        # 里，这个顺序反过来也会踩到，实测踩过两次。
        if indented:
            # 打断缩进用的是**实体**，不是行首插反斜杠：`\` + 空白**不是** CommonMark 的合法
            # 转义（它只对 ASCII 标点生效），插进去的反斜杠会被原样渲染出来。首字符一旦变成
            # `&`，源级行首的缩进就是 0 列，不可能再是缩进代码块——**上下文无关**，判据不依赖
            # 前一行是什么。
            # 换掉反斜杠的理由是复审 2 用真实语料量出来的：400 条真实 appmsg 抽样里 57 条命中
            # 这一档，其中 **56 条是段落续行**（前一非空行是普通文本）——那个位置本来就不会成
            # 代码块，插反斜杠是**纯代价**；改实体之后这 56 条的渲染与 EA-037 之前**逐字相同**。
            line = _WS_ENTITY[line[0]] + line[1:]
        elif body[:1] and body[0] in _MD_LINE_START:
            line = line[:lead] + "\\" + body            # 转义插在标记**之前**，缩进留着
        elif ol:
            line = line[:lead] + body[:ol.end() - 1] + "\\." + body[ol.end():]
        out.append(line)
    return "\n".join(out)


def _media_block(m: MediaRefState, prefix: str, out_dir: str) -> str:
    """`MediaRefState` 四态 → Markdown。`none` 不印任何东西。"""
    kind = m.kind or ""
    label = chat_text.KIND_LABEL.get(kind, kind)
    if m.status == "exported":
        if not m.path:
            return "〔{}〕".format(_t(chat_text.INCONSISTENT_EXPORTED))
        src = chat_text.media_src(m.path, out_dir, prefix)
        if kind == "image":
            return f"![{_t(label)}]({src})"
        # 音视频没有原生语法（模块头的固有损失 1）：给链接。
        return f"[{_t(label) or '媒体'}]({src})"
    if m.status == "exportable":
        return "〔{} · {}〕".format(_t(label), _t(chat_text.EXPORTABLE.get(kind, chat_text.EXPORTABLE_OTHER)))
    if m.status == "gone":
        why = chat_text.STICKER_GONE if kind == "sticker" else chat_text.GONE
        return "〔{} · {}〕".format(_t(label), _t(why))
    if m.status == "none":
        return ""
    return "〔{} · {}〕".format(_t(label), _t(chat_text.unknown_status_note(m.status)))


def _transfer_line(t: appmsg.Transfer) -> str:
    """转账 / 红包 → 一行可读字段。

    **字段清单、顺序与标签全在 `chat_text.transfer_fields`**（与 HTML 侧**同一份**，
    不是两份互校的副本）；本模板只加 Markdown 自己的行首 `- ` 与转义。
    """
    return "- " + _esc_md(" · ".join(chat_text.transfer_fields(t)))


def _appmsg_lines(a: AppMsg, ref_anchor: str | None) -> list[str]:
    """appmsg 的结构化呈现（具名块优先，其余叶子进列表）。"""
    head = _t(appmsg.kind_name(a.kind))
    if a.title:
        head += "：" + _esc_md(a.title)
    out = [f"**{head}**"]
    if a.quote is not None:
        # displayname 为空是 0.81% 的常态：补一个**显式的**「未署名」，不猜名字，
        # 也不留空 —— 留空的行看起来像渲染坏了。
        who = _esc_md(a.quote.displayname) or "（未署名）"
        out.append(f"> **{who}**：{_esc_md(a.quote.content)}")
        if not ref_anchor:
            # **不用 `> ` 前缀**：这条是「没有跳转目标」的元说明，不是被引用的内容。
            # 若它也写成 blockquote，用例里的 `"> " in out` 就会被这一行满足，
            # 于是「引用退化成 blockquote」这半个断言永远红不了（实测确认过）。
            out.append("（原消息不在本次范围内，无法跳转 —— 以上是引用自带的原文。）")
    if a.link is not None:
        url, title = a.link.url, a.link.title or a.link.url
        if url:
            # EA-034（复审）：MD 侧此前**没有** URL 守卫——`[title](javascript:…)` 与
            # HTML 侧同一类输入，一个被拦一个可点。判据与 HTML/朋友圈共用。
            # 修轮 2：再加一道**目的地**判据（`_md_dest_ok`）——两个读者问的不是同一件事，
            # 复验实测换行/空格/`)` 三种都能从目的地里逃出去变成真的 `<img>`。
            # 目的地原样写 `url`（不加 `_t`）：`_md_dest_ok` 已排除 `<`/`>`，`_t` 在这是空操作，
            # 而它会把 URL 里合法的 `&` 之外的实体写法搅乱——不写更诚实。
            if chat_text.url_is_safe(url) and _md_dest_ok(url):
                out.append(f"[{_esc_md(title)}]({url})")
            else:
                out.append(f"[链接：{_esc_md(url)}]")
        if a.link.des:
            out.append(_esc_md(a.link.des))
    if a.file is not None:
        bits = [b for b in (a.file.title, a.file.fileext) if b]
        if a.file.totallen:
            bits.append(f"{a.file.totallen} 字节")
        if bits:
            out.append("文件名：" + _esc_md(" · ".join(bits)))
    if a.transfer is not None:
        out.append(_transfer_line(a.transfer))
    for k, v in a.fields:
        out.append(f"- {_esc_md(k)}：{_esc_md(v)}")
    return out


def _message_lines(m: ViewMsg, prefix: str, out_dir: str) -> list[str]:
    """单条消息。`is_self` 决定「我（自己）」与「别人」的标题 —— 与 HTML 的
    `self` / `other` 同一件事（Markdown 没有对齐，只能用标记）。"""
    who = "我" if m.is_self else (m.sender or "（未知发送者）")
    tag = "（自己）" if m.is_self else ""
    out = [f"## {_esc_md(who)}{tag} · {chat_text.when(m.ts)} · {_t(m.kind)}", ""]
    # appmsg 的 `text` 是 `[appmsg: 标题]` 形态的提示，解析成功时与下面的结构化块逐字重复。
    if m.text and m.appmsg is None:
        out += [_esc_md(m.text), ""]
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
    lines = [f"# 会话：{_esc_md(view.title or view.chat_id)}", ""]
    lines += _notice_lines(view)
    lines.append("")
    if not view.msgs:
        lines += ["（没有匹配的消息。）", ""]
    for m in view.msgs:
        lines += _message_lines(m, media_prefix, view.out_dir)
    return "\n".join(lines).rstrip("\n") + "\n"
