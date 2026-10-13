"""`chat_html` / `chat_md` **共用**的措辞与纯函数。

**为什么单独一层**：spec §4.3 逐字写着「HTML 与 Markdown 语义一致」这条承诺要「由同一个
中间结构支撑，而不是由两份互相无法校验的副本支撑（丙 R21 的教训）」。`ChatView`（T6）把
**数据**那一半做成了单一来源，但**措辞**那一半此前在两个模板里各有一份副本 ——
实测：让 `chat_html` 与 `chat_md` 的 `GONE` 分叉，19 条用例全绿；而后果是用户读
`index.html` 与 `index.md`，对**同一条消息**的媒体可得性得到互相矛盾的说法。
本模块补的就是这半边。

**它不是数据源**：只有文案与纯函数（时间格式化、相对路径、降级文案、**转账的字段清单**）。
两个模板仍然只从 `ChatView` 取数据（spec §4.3「两个模板只吃 `ChatView`」不受影响）：
`transfer_fields` 吃的是 `AppMsg.transfer`，那是 `ChatView` 里已经装好的**同一个对象**，
不是第二份数据。

**改这里的任何一句话，两个产物同时变** —— 这正是目的；分叉从此不可能悄悄发生。
"""

from __future__ import annotations


import datetime
import os
from typing import TYPE_CHECKING

# EA-108：这两句此前在 HTML / MD 各留一份副本（实测分叉零红）。措辞归这里，
# 标记（`[...]` vs `〔…〕`）仍归各模板自己套。
INCONSISTENT_EXPORTED = "已导出，但拿不到产物路径 —— 状态与路径不一致"


def whatwg_ipv4(host: str) -> bool:
    """这个 host 会被 WHATWG 当 IPv4 解析吗（而页面上的徽章会把它原样显示出来）。

    判据是 WHATWG「ends in a number」那一步的**超集**：末段是纯数字或 `0x`/`0X` 开头的
    十六进制即为真——前导零的八进制（`017700000001` → 127.0.0.1）也是纯数字，裸 IPv4
    （`192.168.1.1`）同样命中；真机上外链都是域名，一并降级不损失什么。先去掉末尾的点：
    `192.168.1.1.` 是绝对域名形式，浏览器照样落到 192.168.1.1。二进制 `0b101` 不在判据内
    ——实测 node 的 `new URL("https://0b101/x").hostname` 就是 `0b101`，WHATWG 不认它。
    """
    import re as _re
    label = host.rstrip(".").rsplit(".", 1)[-1]
    # 与 `moments_html` 原实现**逐字一致**（搬移不改行为）：`0[xX][0-9a-fA-F]*` 用 `*` 而非
    # `+`——裸 `0x` 也是命中（WHATWG 认它、原用例 `[0x]` 钉着这一点）。
    return bool(_re.fullmatch(r"(?:[0-9]+|0[xX][0-9a-fA-F]*)", label))


def url_is_safe(url: str) -> bool:
    """URL 能不能安全地做成**可点链接**（EA-034：两个渲染模板共用这一处判据）。

    `html.escape` 挡不住 scheme（`javascript:alert(1)` 转义后仍是可执行链接），也挡不住
    **反斜杠或非 ASCII 主机**：Python 的 `urlsplit` 不把反斜杠当分隔符，浏览器按 WHATWG 会
    ——`https://evil.com` 后面接反斜杠再接 `mp.weixin.qq.com/x` 时，页面显示的是后者，
    点下去却落到 `evil.com`。反斜杠**必须查 `netloc` 而不是 `hostname`**（`hostname` 已
    把 userinfo 丢掉，而 WHATWG 先按反斜杠切一刀）。第三类：WHATWG 会当 IPv4 解析的 host
    与含 `%` 的 host（`urlsplit` 不解百分号编码、WHATWG 解）。

    **从 `moments_html._link_block` 搬来（EA-034）**：那里一直有这套守卫，而 `chat_html`
    的 appmsg 链接块**完全没有**——同一个输入在两个产物里一个被拦、一个成为可点链接。
    搬到这里是因为 `chat_text` 已经是两模板共享的纯函数层。
    """
    from . import moments                      # 复用它的 `split_url`（容错解析）
    s = moments.split_url(url)
    host = (s.hostname or "").lower() if s is not None else ""
    if s is None or s.scheme not in ("http", "https") or not host:
        return False
    if "\\" in s.netloc or not host.isascii() or "%" in host or whatwg_ipv4(host):
        return False
    return True


def unknown_status_note(status: str) -> str:
    """四态之外的取值：照实印出来，不猜也不吞（上游将来加枚举值时两个产物同时变）。"""
    return f"未知媒体状态 {status}"


if TYPE_CHECKING:
    from .appmsg import Transfer
    from .usecases import Degradation

# 媒体类型的中文名。未确认的 kind 兜底成 kind 本身（**不猜**，spec §2.9 的规矩）。
KIND_LABEL = {"image": "图片", "voice": "语音", "video": "视频", "sticker": "贴纸"}

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

# 贴纸的 `gone` 与其余三类**不是一回事**，所以措辞必须分开：
# 通用 GONE 说的是「本机找不到本体（已被清理，或从未下载到本机）」——
# 对贴纸这是假的：贴纸取不出来是因为微信侧把它存成加密格式、方案未破
# （spike 判定未破，spec 附录 C），与「本机找不找得到」无关。
# 共用那一句会在产物里印出一句**错话**。这是本任务唯一的实质理由。
#
# **措辞必须对每一条贴纸消息都成立**：本机有没有那份加密副本是**未测的**——
# 实测只有 38.3% 的 type-47 消息本机有贴纸文件（spec §2.4：25,971 / 67,833），
# 而收窄后的 T9 把 presence 写死成常量、本地支线（`resolve_sticker`）随 T7 一起挂起，
# 所以渲染这条时**没有任何存在性信号可依据**。故只能说微信侧的**存储格式**，
# 不许断言「本机有」——那句话对约 62% 会印出这一行的消息是假的。
#
# **这句话是照它要落的坑写的**：两个模板都渲染成 `[标签 · 措辞]`，而标签已经是
# `KIND_LABEL["sticker"]` =「贴纸」，所以本句的主语交给标签、自己只用「它」回指
# （曾写作「贴纸取不出来：微信把贴纸存成加密格式…」，印出来是
# `[贴纸 · 贴纸取不出来：微信把贴纸存成加密格式，解密方案未破]`——三处重复）。
# 若将来有人把本常量**脱离那个槽位**单独使用（日志、MCP 信封、独立提示），
# 句中的「它」就没有先行词了，需自行补主语或另写一句，不要直接搬。
STICKER_GONE = "微信把它存成加密格式，解密方案未破"

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
    """`ts == 0` 是「时间戳没解析出来」，不是 1970 年 —— 返回空串，不编一个日期。

    **超范围的时间戳同样返回空串，不抛**。载荷是**对手可控**的（任何人可以给你发一条
    appmsg），而 `<begintransfertime>` 这类字段只要求是数字：`_to_int` 放行
    `99999999999999999999`，`datetime.fromtimestamp` 随即抛 `OverflowError`。
    它在 core 层无人接住 —— 两个模板都直接调本函数，于是 CLI 打 traceback（违反
    USAGE.md §9 的「单行中文」）、MCP 返回「内部错误」。与 `_to_int` 对非数字字段的
    立场一致：**不可表示的时间就印空**，不编日期、也不崩掉整次导出。
    `ValueError` / `OSError` 一并接住：前者是 `fromtimestamp` 在部分平台上对越界值的
    模样，后者是极端负值经 `localtime` 转换时的失败形态（下限一侧）。
    """
    if not ts:
        return ""
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OverflowError, OSError):
        return ""


def transfer_fields(t: Transfer) -> list[str]:
    """转账 / 红包（type 2000）→ `spec §4.5` 信封字段的可读列表。**两个模板共用这一份。**

    为什么它也在这里：11 个字段名与顺序、三个时间标签、`paysubtype` 那句「语义未确认」
    此前在 `chat_html._transfer_block` 与 `chat_md._transfer_line` 里**逐字两写**，
    而两个模块头都宣称「改文案只改 `chat_text` 一处，分叉不可能悄悄发生」——转账这一段
    恰恰不在 `chat_text` 里。实测：把 `chat_md._transfer_line` 整个改成 `return ""`
    （MD 产物里转账块全没了），695 条用例**全绿**。这正是丙 R21 的复发形状。
    搬进来之后，那条变异至少让 MD 侧的新夹具红了。

    **标记仍各留各的**（照本模块既有的「措辞共享、标记不共享」）：分隔符 ` · ` 由两边
    各自接，行首（Markdown 的 `- `）只有 MD 有。

    `paysubtype` **原样印数字**并注明语义未确认（spec §2.9：实测只有取值分布，没有权威
    映射，不许替微信编一套类型名）。`payer` 61% 为空**是常态不是异常**，空着就不印这一
    行，不写「（空）」——那是把缺失包装成信息。
    """
    rows: list[str] = ["红包" if t.is_red_envelope else "转账"]
    if t.amount_desc:
        rows.append(f"金额：{t.amount_desc}")
    if t.memo:
        rows.append(f"备注：{t.memo}")
    if t.payer:
        rows.append(f"付款方：{t.payer}")
    if t.receiver:
        rows.append(f"收款方：{t.receiver}")
    if t.transaction_id:
        rows.append(f"交易号：{t.transaction_id}")
    if t.transfer_id:
        rows.append(f"转账单号：{t.transfer_id}")
    if t.paysubtype:
        rows.append(f"paysubtype：{t.paysubtype}（语义未确认，原样呈现）")
    for label, value in (("发起时间", t.begin_time), ("生效日期", t.effective_date),
                         ("失效时间", t.invalid_time)):
        if value:
            rows.append(f"{label}：{when(value)}")
    return rows


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
