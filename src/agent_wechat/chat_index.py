"""会话索引页的 HTML 渲染。纯函数：条目列表 → 单文件 HTML 字符串。

形状镜像 `moments_html.py` / `chat_html.py`：模块级 `_CSS`、`_esc()` 走
`html.escape(v, quote=True)`、`render()` 返回完整文档字符串。**不写文件、不查库、不读时钟**
——枚举产物目录与查索引库是用例层（`usecases.chat_index`）的事，本层只吃
`ChatIndexEntry`、只吐字符串。

**链接一律相对路径**（`{chat_md5}/chat/index.html`）：索引页与各会话产物同在 `{--out}`
这一棵树下（`{--out}/index.html` 与 `{--out}/{chat_md5}/chat/…`），整棵树拷到别处/别的
机器之后链接照常成立。页面上因此**不出现绝对路径**（也没有 `file://`）。

**所有文本走 `_esc`**：`label` 来自索引库的 `chat_id` 列、`chat_md5` 来自目录名 —— 与
`chat_html` 同一立场（名字也可能是对手可控的），`<script>` 不许原样进产物。

**`ChatIndexEntry.messages is None` = 索引库里查不到这个会话**（导出之后重建过索引 /
`purge --index` 过；那时 `chat_id` 也是空串，但**判据是 `messages`**）。那时条数**未知**，
模板印「条数未知」而**不是 `0 条`**：0 是一条确定的信息（这个会话确实一条都没有），而这里我们
只是不知道——把它存成 0 就是把「不知道」伪装成事实（T11 的 MCP 读 `.messages` 就会这么报）。
该情况下 `label` 是 `md5:{chat_md5}`（`messages.degraded_chat_id` 的形态）。
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

_CSS = """body{font-family:-apple-system,"PingFang SC",sans-serif;max-width:720px;
margin:0 auto;padding:16px;background:#f7f7f7;color:#1a1a1a;line-height:1.6}
.notice{background:#fff8e1;border:1px solid #ffe082;border-radius:8px;
padding:12px;margin-bottom:16px;font-size:13px}
ul.chats{list-style:none;padding:0;margin:0}
li.chat{background:#fff;border-radius:10px;padding:12px;margin-bottom:10px}
a.name{font-weight:600;color:#576b95;text-decoration:none;word-break:break-all}
.n{margin-left:8px;color:#888;font-size:12px}
a.md{margin-left:8px;color:#576b95;font-size:12px}"""


@dataclass(frozen=True)
class ChatIndexEntry:
    """一行：一个**已导出**的会话。`chat_md5` 是产物目录名（`{out}/{chat_md5}/chat/`）。"""

    chat_md5: str
    chat_id: str            # 索引库里的会话 id；`""` = 索引里没有它（`messages` 那时为 None）
    label: str              # 展示名：`chat_id`，查不到时是 `md5:{chat_md5}`
    messages: int | None    # **`None` = 索引里没有这个会话 → 条数未知，不是 `0 条`**
    has_md: bool            # 同级真的存在 `index.md`（写失败过就只有 HTML）
    has_html: bool          # 同级真的存在 `index.html`（EA-035：`--format md` 导出的会话
                            # 只有 md——枚举只有 html 时它们**整批不出现在索引页上**）


def _esc(v: str | None) -> str:
    return html.escape(v or "", quote=True)


def render(entries: Sequence[ChatIndexEntry]) -> str:
    """条目 → 完整 HTML 文档字符串。纯函数、零 IO、确定性（不读时钟）。

    按 `label` 排序：产物目录的枚举顺序由文件系统决定，同一份输入在不同机器上会得到
    不同的页面；索引页是给人看的清单，稳定的顺序比「碰巧的顺序」好。
    """
    rows = []
    for e in sorted(entries, key=lambda e: (e.label, e.chat_md5)):
        base = _esc(e.chat_md5)
        # 查不到索引时条数未知（`None`）：印 0 条是**编**一条信息（那个会话的产物里显然有消息）
        meta = (f"{e.messages} 条" if e.messages is not None
                else "条数未知（索引里没有这个会话）")
        # `index.md` 只在真的写出来了才链：`chatview.write` 单文件失败时目录里只有 HTML，
        # 无条件印就是一条死链（`has_md` 的全部意义）。
        # EA-035：名字链到**存在的那份**（html 优先）；两份都在时另给一个 md 链接，
        # 只有 md 时名字本身就是 md 链接（不再重复印）。
        name_href = (f"{base}/chat/index.html" if e.has_html else f"{base}/chat/index.md")
        md = (f'<a class="md" href="{base}/chat/index.md">Markdown</a>'
              if e.has_md and e.has_html else "")
        rows.append(f'<li class="chat"><a class="name" href="{name_href}">'
                    f'{_esc(e.label)}</a><span class="n">{meta}</span>{md}</li>')
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>会话导出索引</title><style>" + _CSS + "</style></head><body>"
            f'<div class="notice">共 <b>{len(entries)}</b> 个会话；'
            "链接都是相对路径，整棵产物目录可以搬走。</div>"
            f'<ul class="chats">{"".join(rows)}</ul></body></html>')
