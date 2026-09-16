"""ChatView —— HTML 与 Markdown 两个模板**共用**的中间结构。

为什么要有这一层：spec §4.3 要求「HTML 与 Markdown 语义一致」，若两个模板各自从
`messages` 行 + `msg_parts` + `MediaRefState` 拼一遍，这句话就没有任何东西支撑它
——两份互相无法校验的副本。组装只做一次，两个模板只吃 `ChatView`、只吐字符串。

**`build` 是纯函数、零 IO、不碰 SQLite**（`write` 是本模块唯一的 IO：落盘两份产物）。
三份数据全由调用方喂进来：`rows`（`search.messages_in_range` 的普通 dict）、
`parts`（已解析的 `AppMsg`）、`media`（丙的 `MediaRefState`）。渲染端不查库，
模板端不查库——查库只发生在用例层。唯一的例外是 `build` 末尾读一次时钟（`time.time()`）
填 `generated_at`：那是**读时钟不是 IO**，且调用方可以用 `generated_at=` 显式覆盖
（测试要确定性时用得上）。

**必须绕开的一个 import 环**：`ViewMsg.media` / `ChatView.degraded` 的类型分别是
`usecases.MediaRefState` 与 `usecases.Degradation`（丙的产物），而 `usecases` 要
import 本模块——运行期 import 立刻成环。解法：两个类型**只在 `if TYPE_CHECKING:` 下
import**，运行期按结构使用（`media` 只读 `.status` / `.kind` / `.path`，`degraded` 只透传）。
本仓库处处 `from __future__ import annotations`，注解本就是字符串，故这不引入任何运行期依赖。
**不要改成运行期 import。**

**锚点 = `msg_key` 的 12 位 blake2s。** `msg_key` 是身份派生的（`account|chat_md5|shard|local_id`）、
跨导出稳定，所以引用锚点**不会因为重新导出而失效**；取短哈希只是为了让它能安全地进
HTML `id` 与 URL fragment（`msg_key` 里有 `|` 与 `.`）。`anchor_of` 是纯函数：同输入恒同输出。
"""
from __future__ import annotations

import hashlib
import pathlib
import time
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import chat_html, chat_md, errors, messages

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .appmsg import AppMsg
    from .usecases import Degradation, MediaRefState

# blake2s 摘要取 6 字节 → 12 个十六进制字符（spec §4.3 的「12 位」）。
_ANCHOR_DIGEST_SIZE = 6

# 展示用类型名 = `msg_type` → `kind`。`messages.TYPE_NAMES` **刻意不含 49**（见 messages.py
# 的注释：加进去会把 reader 的兜底文案 `[type49]` 变成 `[appmsg]`，而那串文案已冻结在
# index.db 内容与 MCP 返回里），但 spec §4.3 的 `ViewMsg.kind` 明列 `'appmsg'`，
# 模板要按它选渲染分支。故在**本模块**就地补这一条，不动 `messages`。
_KIND_NAMES = {**messages.TYPE_NAMES, messages.TYPE_APPMSG: "appmsg"}


def anchor_of(msg_key: str) -> str:
    """`msg_key` → 12 位十六进制锚点。纯函数（同输入恒同输出），无状态、无 IO。"""
    return hashlib.blake2s(msg_key.encode(), digest_size=_ANCHOR_DIGEST_SIZE).hexdigest()


def _kind_of(msg_type: int) -> str:
    """`msg_type` → `kind`。未知类型走 `str(msg_type)`，**不猜名字**（spec §2.9）。"""
    return _KIND_NAMES.get(msg_type, str(msg_type))


@dataclass(frozen=True)
class ViewMsg:
    """一条消息的渲染视图。字段见 spec §4.3。"""

    msg_key: str
    ts: int
    sender: str
    is_self: bool
    kind: str
    text: str                       # text 的真文本；其余类型是 `content` 里的 hint
    appmsg: AppMsg | None           # `None` = 这条不是 appmsg，或它没能解析出来
    media: MediaRefState | None     # 丙的四态，直接用（`None` = 该 msg_key 没有媒体状态）
    anchor: str                     # msg_key 的 12 位 blake2s
    ref_anchor: str | None          # 引用目标在本次范围内时才非空，否则 None（不是空串）


@dataclass(frozen=True)
class ChatView:
    """一次导出的全量视图。字段见 spec §4.3。"""

    chat_id: str
    title: str
    generated_at: int
    out_dir: str
    msgs: tuple[ViewMsg, ...]
    counts: dict[str, int]          # kind → 条数
    unparsed: int
    rejected: int
    degraded: tuple[Degradation, ...]
    # `search.messages_in_range` 的 `len(rows) == limit` 直通。**写作「可能还有更早的没进来」**：
    # 该公式在「范围内**恰好** limit 条」时是**假阳性**（一条都没被丢却报截断）。公式由 T5 的
    # brief 硬性规定，本模块不重算。**前向风险**：模板若按这个 True 印「还有更早的没进来」，
    # 文案会在这一个边界上说错——要印就印成「可能还有」（或由调用方另行判定）。
    truncated: bool


def build(chat_id: str, *, title: str, rows: Sequence[dict], parts: Mapping[str, AppMsg],
          media: Mapping[str, MediaRefState] | None = None, out_dir: str, truncated: bool,
          unparsed: int, rejected: int, degraded: tuple[Degradation, ...],
          refs: Mapping[str, str] | None = None,
          generated_at: int | None = None) -> ChatView:
    """把三份**已经在手上**的数据拼成 `ChatView`。不查库、不读文件、不联网。

    - `rows`：`search.messages_in_range` 的普通 dict（时间正序）。
    - `parts`：`msg_key → AppMsg`，**已解析**（调用方负责 `zlib.decompress` + `appmsg.parse`）。
      查不到的 key 不在表里——「不是 appmsg」与「有 appmsg 但解析失败」在上游就分开了
      （后者计入 `unparsed` / `rejected`，`content` 里剩 `[appmsg: 标题]`）。
    - `media`：`msg_key → MediaRefState`（丙的四态）。缺省空表。
    - `refs`：`msg_key → 引用目标的 msg_key`，来自 `store.msg_parts_by_keys` 的第三项
      （`None` 的不放进表）。**只有目标也落在本次 `rows` 里，`ref_anchor` 才非空**；
      否则是 `None` —— `None` 让模板退化成「内联显示原文 + 一句『原消息不在本次范围内』」，
      而空串会渲染成一个指向自己的死锚点。原文一直在 `AppMsg.quote.content` 里，
      **渲染从不依赖回指成功**。
    - `generated_at`：缺省读一次时钟；测试要确定性时显式传值。
    """
    # 先物化：下面要遍历两遍（先收 in_range 再逐行建 ViewMsg）。上游是 list，这一步不是
    # 为了接受生成器，而是把「一次性迭代器会让 msgs 静默变空、而 in_range 照旧不空」这个
    # 失败模式挡在门外——它不会报错，只会静默产出半份时间线。
    rows = list(rows)
    in_range = {row["msg_key"] for row in rows}
    refs = refs or {}
    media = media or {}

    out: list[ViewMsg] = []
    for row in rows:
        key = row["msg_key"]
        target = refs.get(key)
        out.append(ViewMsg(
            msg_key=key,
            ts=row["ts"],
            sender=row["sender_name"] or "",
            is_self=bool(row["is_self"]),
            kind=_kind_of(row["msg_type"]),
            text=row["content"] or "",
            appmsg=parts.get(key),
            media=media.get(key),
            anchor=anchor_of(key),
            ref_anchor=anchor_of(target) if target in in_range else None,
        ))
    msgs = tuple(out)
    return ChatView(
        chat_id=chat_id, title=title,
        generated_at=int(time.time()) if generated_at is None else generated_at,
        out_dir=out_dir, msgs=msgs,
        counts=dict(Counter(m.kind for m in msgs)),
        unparsed=unparsed, rejected=rejected, degraded=degraded, truncated=truncated,
    )


# ---------- 落盘 ----------

# 格式 → 渲染器。**两张模板只在这一处配对**：多一处配对就多一处会分叉的真相
# （与 `chat_text` 收拢措辞同一条理由）。
FORMATS: tuple[str, ...] = ("html", "md")
_RENDERERS = {"html": chat_html.render, "md": chat_md.render}


def write(view: ChatView, *, formats: Sequence[str] = FORMATS) -> list[pathlib.Path]:
    """把 `ChatView` 落盘到 `{view.out_dir}/chat/index.{html,md}`（目录逐级 0700、文件 0600）。

    **参数里刻意没有「输出根」**：产物目录完全由 `view.out_dir` 推出（控制器采纳的改法，
    见 brief Step 5 注 2）。此前设想的是「调用方给 `out_root`、`write` 自己拼
    `{out_root}/{chat_md5}/chat/`」——那与模板用的 `view.out_dir` 构成**两套路径推导**，
    而两者的分歧恰好让所有媒体引用**多一级**：`<img>` 照常渲染、**打不开**（静默 404，
    浏览器与测试都不报错）。只留一份推导，这个坑由构造消失。

    文件名固定 `index.*`，媒体用相对路径 `../image/…` 引用**同级**的既有媒体树
    —— 因此调用方的 `--out` 必须与 `media export` 用同一个（spec 裁定 13）。

    未知格式抛 `SearchInputError`，**不静默跳过**：少写一个文件与「格式名写错了」在
    「产物列表短了一截」上长得一模一样。单文件写失败（磁盘满/权限/只读挂载）返回 None
    并 continue，不进返回列表——由调用方按自己的口径计数（沿用 `media._save` 的形状，
    裸 `OSError` 不逃出 core）。
    """
    from . import media            # 惰性：media 顶层拉起 cryptography（同 usecases 的取舍）
    root = media.ensure_tree(pathlib.Path(view.out_dir), "chat")
    out: list[pathlib.Path] = []
    for fmt in formats:
        render = _RENDERERS.get(fmt)
        if render is None:
            raise errors.SearchInputError(
                f"未知的导出格式：{fmt}（可用: {', '.join(FORMATS)}）")
        p = root / f"index.{fmt}"
        try:
            p.write_text(render(view), encoding="utf-8")
            p.chmod(0o600)                 # 产物是聊天原文：除了属主谁也读不到
        except OSError:
            continue
        out.append(p)
    return out
