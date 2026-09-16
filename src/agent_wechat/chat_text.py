"""`chat_html` / `chat_md` **共用**的措辞与纯函数。

**为什么单独一层**：spec §4.3 逐字写着「HTML 与 Markdown 语义一致」这条承诺要「由同一个
中间结构支撑，而不是由两份互相无法校验的副本支撑（丙 R21 的教训）」。`ChatView`（T6）把
**数据**那一半做成了单一来源，但**措辞**那一半此前在两个模板里各有一份副本 ——
实测：让 `chat_html` 与 `chat_md` 的 `GONE` 分叉，19 条用例全绿；而后果是用户读
`index.html` 与 `index.md`，对**同一条消息**的媒体可得性得到互相矛盾的说法。
本模块补的就是这半边。

**它不是数据源**：只有文案与纯函数（时间格式化、相对路径、降级文案）。两个模板仍然
只从 `ChatView` 取数据（spec §4.3「两个模板只吃 `ChatView`」不受影响）。

**改这里的任何一句话，两个产物同时变** —— 这正是目的；分叉从此不可能悄悄发生。
"""
from __future__ import annotations

import datetime
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .usecases import Degradation

# 媒体类型的中文名。未确认的 kind 兜底成 kind 本身（**不猜**，spec §2.9 的规矩）。
KIND_LABEL = {"image": "图片", "voice": "语音", "video": "视频"}

# `exportable` / `gone` 的占位**分开措辞**：丙 R17 —— 池化这两个状态会制造误读
# （「本机有但你没导出」与「本机找不到」是两件事，前者可挽回、后者不能）。
# video 的那一条不得让人以为 mp4 本体还在：丙实测 `resolve_video` 命中缩略图时也返回成功。
EXPORTABLE = {
    "image": "本机有原图，尚未导出（跑一次 agent-wechat media export 就能看）",
    "voice": "本机有语音原文件，尚未导出（跑一次 agent-wechat media export 就能听）",
    "video": "本机有转码源，尚未导出成 mp4 —— 它可能只是缩略图，不等于 mp4 本体还在",
}
EXPORTABLE_OTHER = "本机有原文件，尚未导出"
GONE = "本机找不到该媒体的本体（已被清理，或从未下载到本机）"

# 顶部提示的三句措辞，形如 `(加粗的前半句, 后半句)`。**措辞共享、标记不共享**：
# HTML 用 `<b>…</b>`、Markdown 用 `**…**`，由各模板自己套（见各自的 `_b`）。

# 「**可能**还有」而不是「还有」：`truncated` 是 `len(rows) == limit` 的直通，
# 在「范围内恰好 limit 条」时是假阳性（`chatview` 的注释已钉这条，模板不重算）。
TRUNCATED_NOTE = ("本次导出被 limit 截断",
                  "：范围内可能还有更早的消息没进来，可调大 limit 重跑。")


def unparsed_note(n: int) -> tuple[str, str]:
    """`unparsed` 那一行：「解不开」——**我们解不了**，与 `rejected_note` 分开印。"""
    return (f"{n} 条 appmsg 解不开",
            "（XML 结构不符，已跳过）——这些条只显示原始提示文字。")


def rejected_note(n: int) -> tuple[str, str]:
    """`rejected` 那一行：「我们主动拒绝」——含 DTD/实体声明的载荷展开会打挂进程。

    与 `unparsed_note` 池成一句会让用户以为数据坏了（spec §5 明令分开）。
    """
    return (f"{n} 条 appmsg 被主动拒绝",
            "（载荷含 DTD / 实体声明，展开有进程被打挂的风险），已跳过。")


def when(ts: int | None) -> str:
    """`ts == 0` 是「时间戳没解析出来」，不是 1970 年 —— 返回空串，不编一个日期。"""
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""


def media_src(path: str, out_dir: str, prefix: str) -> str:
    """媒体绝对路径 → 产物里的相对引用。契约见两个模板的模块头。

    `out_dir` 是**本会话的媒体树根**（`{--out}/{chat_md5}`），产物写在它的 `chat/` 子目录里，
    故缺省 `prefix=".."` 恰好解出 `../image/2026-09/x.jpg`；传成 `--out` 本身会**多一级**，
    `<img>` 照常渲染但打不开（静默 404）。

    裁剪用 `os.path.relpath`（纯词法，**不解析符号链接**：媒体目录可能是软链，解析后算出来的
    `../../..` 会随挂载点变化）。跨盘符（Windows）抛 `ValueError` —— 没有相对路径可算，
    退回原样字符串而不是让异常穿透模板层。
    """
    rel = path
    if out_dir:
        try:
            rel = os.path.relpath(path, out_dir)
        except ValueError:
            rel = path
    rel = rel.replace(os.sep, "/")
    return f"{prefix}/{rel}" if prefix else rel


def degradation_text(d: Degradation) -> str:
    """`usecases.Degradation` 的结构化读取。

    **不在运行期 import `usecases`**：两个模板都只按结构读它的属性，不把 usecases
    拉进运行期依赖（与 `chatview` 对 `MediaRefState` 的处理同一个理由）。
    """
    cap = getattr(d, "capability", "") or ""
    effect = getattr(d, "effect", "") or ""
    return f"{cap}：{effect}" if cap else effect
