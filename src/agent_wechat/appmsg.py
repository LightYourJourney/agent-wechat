"""appmsg 载荷的规范化与解析。**纯函数、零 IO、不联网、不 import 项目模块之外的东西。**

三条规范化规则全部来自真机实测（spec §2.5），顺序不可换：

1. 剥群前缀 —— 判据是「首行非标签文本 + `:` + 紧跟 `<`」，**不是**按 `wxid_` 前缀。
   实测发送者有 `wxid_*` / 自定义微信号 / `@openim` 三类。
   **刻意不复用 `reader._decode_row` 的规则**：那条要求 `chat_id.endswith("@chatroom")`，
   会漏掉 md5 反查不到会话名的那几条群消息（实测 4 条）。两者的适用面不同——
   `reader` 那条服务于**通用消息内容**（不能假设载荷是 XML），本函数服务于**已知是 XML 的
   appmsg 载荷**，故可用更强的判据。**不要「顺手统一」这两处。**
2. 剥掉**全部** `<?xml … ?>` 声明 —— 实测 8,980 条无声明、11 条声明位置非法（嵌在 `<msg>`
   内部）。声明的唯一功能是编码，而载荷到这里已是 `str`（`reader._decode_content` 已完成）。
3. 按 XML 1.0 的合法码位剔除非法的数字字符引用。实测剩最后 1 条失败即此。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

# 群前缀：首行是不含 `<`/`>` 的短文本 + ":" + 换行，且**紧跟的必须是 `<?xml` 或 `<msg`**。
# 第三个条件是安全阀，**不能放宽成裸 `(?=<)`**：那样 `http://x:\n<msg/>` 这类载荷会被误剥。
# 收紧到这两种形态是实测许可的——244 种真实前缀全部落在它们之后，零漏剥。
# 失配是响亮的：真机可解析率会从 0.9999 崩到 0.33，由 test_appmsg_real.py 兜住。
_GROUP_PREFIX = re.compile(r"^[^\n<>]{1,64}:\n(?=<\?xml|<msg)")
_XML_DECL = re.compile(r"<\?xml[^>]*\?>")
# 非法的数字字符引用。**用数值判定，不要手写字符类**：手写版极易把合法的
# `&#10;`(LF) / `&#13;`(CR) 一起删掉（本计划初稿正是如此，而当时的断言恒真、抓不到），
# 或者随写法变化悄悄漏掉某个区间。XML 1.0 的合法码位是：
#   #x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] | [#x10000-#x10FFFF]
# 落在区间外的一律剔除。实测全库只有 1 条命中此规则。
#
# 两个分组各自是一种**合法形状**，不要写成 `(x?)([0-9a-fA-F]+)`：那种写法配 `re.I` 会让
# `&#abc;` 匹配成「无 x 前缀 → 十进制」，`int('abc', 10)` 抛**裸 ValueError**。消息内容是对手
# 可控的（见 `is_dangerous` 的注释），任何人发一条就能让导出崩掉。形状不合法的引用
# （`&#abc;` / `&#;` / `&#xg;`）**既不匹配也不删除**——它们不是数值引用，删它们是越权，
# 留给 ET 判成 `malformed` 更诚实。
_CHARREF = re.compile(r"&#(?:[xX]([0-9a-fA-F]+)|([0-9]+));")


def _keep_charref(m: re.Match) -> str:
    cp = int(m.group(1), 16) if m.group(1) else int(m.group(2), 10)
    ok = (cp in (9, 10, 13) or 0x20 <= cp <= 0xD7FF
          or 0xE000 <= cp <= 0xFFFD or 0x10000 <= cp <= 0x10FFFF)
    return m.group(0) if ok else ""


def normalize(raw: str) -> str:
    """把微信原文规范成可交给 ET 解析的 `<msg>` 片段。幂等。"""
    s = _GROUP_PREFIX.sub("", raw.lstrip())
    s = _XML_DECL.sub("", s)
    return _CHARREF.sub(_keep_charref, s).strip()


def is_dangerous(raw: str) -> bool:
    """是否含 DTD / 实体声明。

    **为什么必须挡**：消息内容是对手可控的（任何人可以给你发一条 appmsg），而
    `ET.fromstring` 会展开内部实体（billion laughs 可打挂进程）。
    实测 66,655 条真机 appmsg 里 `<!DOCTYPE` / `<!ENTITY` 出现 **0 次**，故零误伤。
    """
    return "<!DOCTYPE" in raw or "<!ENTITY" in raw


# ---------------------------------------------------------------- 解析层
#
# 输出形状：**不为每种类型建一个 dataclass**。4 类具名建模（引用 57 / 链接 5 / 文件 6 /
# 转账 2000），其余走 `title` + 点分路径字段表 —— 「每种都有专门呈现」是用户已认可的解释，
# 不是每种一个 bespoke 结构。
#
# **数字口径（订正）**：spec §2.2 那张表里的「27 种可辨识类型」数的是
# 「`<type>` 在**整篇文档**里出现过」，含 `<refermsg>` 里的**嵌套** `<type>`。
# **真机顶层只有 22 种**（`index.db` 的 `msg_parts.appmsg_type`，2026-09-17 实测 66,663 行、
# `count(distinct appmsg_type) == 22`）。同一个口径差也让 spec 的「57 = 53,472」与本机的
# 53,663 对不上。
# **实质成立、数字不成立**：「每种都有块」这一条照旧（那 22 种全都有 `title` + 字段表），
# 但**别再引用 27** —— 要么写 22，要么写明是哪个口径。
KIND_QUOTE = 57
KIND_LINK = 5
KIND_FILE = 6
KIND_TRANSFER = 2000

# 含义已确认的类型名。**含义未确认的（4 / 62 / 87 / 36 / 2001…）不入表**，
# 由 `kind_name` 兜底成 `appmsg:{kind}` —— spec §10.1 明令不许猜。
KIND_NAMES = {
    KIND_QUOTE: "引用",
    KIND_LINK: "链接",
    KIND_FILE: "文件",
    19: "合并转发",
    33: "小程序",
    KIND_TRANSFER: "转账",
    51: "视频号",
    53: "接龙",
    8: "表情",
}

# 已具名建模的子树。**不跳过会让同一个值在具名块与字段表里各出现一遍**（tests 里有断言守）。
# `refermsg` 不在表里也要跳 —— 它是引用的载荷，在**所有** kind 下都不该进字段表。
_NAMED_SUBTREES = {
    KIND_QUOTE: ("refermsg",),
    KIND_FILE: ("appattach",),
    KIND_TRANSFER: ("wcpayinfo",),
}

# **内层 XML 载荷**：值是**一整段 XML 文本**，不是叶子标量，任何 kind 下都不进字段表。
#
# `refermsg`：引用的载荷，已由 `Quote` 具名建模（见上表）。
# `recorditem`：合并转发（19，以及带转发卡的 57 / 6 / 5 / 51 / 24 / 33 / 4 / 76 / 8）的
# **内层聊天记录**。实测它长得像 `<recordinfo><title>…</title><desc>…</desc>
# <datalist count="9">…`，是**转义后**嵌在外层 XML 里的第二份 XML（`&#x0A;` 这类实体
# 原样留着）。
#
# 为什么必须跳（spec §10.3 那条"实现期必须先按 §4.2 三步重测"的探针，**已重测、已裁定**）：
#   · 重测结果（本机 index.db，2026-09-17）：1,023 个 `recorditem`，**原样 `ET.fromstring`
#     就 100% 可解析**，套完 §4.2 三步仍是 1,023/1,023 —— spec §10.3 转述的「611 可解析 /
#     813 不可解析」在这份语料上**没有复现**，那 813 条不是规范化问题（假设被推翻）。
#   · 所以它该按 `refermsg` 的先例处理：**是载荷，不是叶子**，不当叶子也不在本期解析成结构
#     （那是新能力，spec 也没要求）。据实处理的结论就是这一条规则。
#   · 不跳的后果是**用户可见**的：字段值是一段原始 XML，实测中位 ~9.5 KB、最长 224,800
#     字符，含 `&#x0A;` 实体 —— 它同时进 HTML/Markdown 的字段表与 `get_message_parts` 的
#     MCP 信封。`test_recorditem_is_not_a_leaf_field` 钉住这条。
_INNER_XML_PAYLOADS = ("refermsg", "recorditem")
# `type` / `title` 自身已在 `AppMsg` 的具名字段里，不再重复进字段表。只跳**顶层**
# 这两个名字：更深处的同名叶子（如某个未具名子树里的 `<type>`）是该子树自己的数据。
_SELF_NAMES = ("type", "title")

# 已具名建模的**兄弟叶子**。跳子树挡不住它们：`url` / `des` 是 `appmsg` 的顶层叶子，
# 不在任何具名子树里，于是 kind=5 时 `Link(url=…, des=…)` 与 `fields` 里的
# `('url', …)` / `('des', …)` **逐字重复**（实测）。另三个具名块的字段都在各自子树内
# （`refermsg` / `appattach` / `wcpayinfo`），故只有 KIND_LINK 需要这张表。
_NAMED_LEAVES = {KIND_LINK: ("url", "des")}

# 实测 type 5 的 `aeskey` 91%、type 6 的 `aeskey` 100% 与 `fileuploadtoken` 90% —— 密钥与上传凭据。
# `fields` 会进 get_message_parts 的 MCP 信封，而 spec §7 与 Global Constraints 都明令
# token / key 不进信封。名字过滤之外再加一道**值**过滤：凭据也可能挂在名字干净的字段上
# （如 `iconurl` 带 `?token=`）。实测 27 类的字段名里未见误伤。
#
# 名字过滤**逐段**判，不只看末段：`<aeskey><value>…</value></aeskey>` 的末段是 `value`，
# 只锚末段会让**密钥值原样进字段表**（审阅发现、控制器复现，实测泄漏）。
_SECRET_SEG = re.compile(r"(aeskey|passphrase|token|key)$", re.I)
# 分隔符集含 `;` 与 `,`：实测 `http://c/x;token=…` 在只有 `?`/`&` 时不被丢。
#
# **参数名复用 `_SECRET_SEG`，不另写一张备选表**：此前写的是
# `[?&;,](?:token|key|aeskey|passphrase)=`，要求参数名**紧跟分隔符**，于是
# `&filekey=` 里 `key=` 前面是 `file`，正则从 `f` 处断开而**整体放行**（实测：
# `?key=`/`&aeskey=`/`?token=` 拦得住，`&filekey=`/`?filekey=`/`&fileuploadtoken=`
# 全部漏）。把 `filekey` 补进备选表是打地鼠——下一类复合名照漏。
# 判「什么叫像密钥的名字」的规则全项目只应有**一份**，就是 `_SECRET_SEG`。
_SECRET_PARAM = re.compile(r"[?&;,]([A-Za-z0-9_]+)=")


def _has_credential_param(value: str) -> bool:
    """值里是否带着凭据查询参数（`?filekey=…` / `;token=…`）。"""
    return any(_SECRET_SEG.search(m.group(1)) for m in _SECRET_PARAM.finditer(value))


def _is_secret(path: str, value: str) -> bool:
    """该字段是否为密材 —— 名字**逐段**命中，或值里带凭据参数。"""
    return (any(_SECRET_SEG.search(seg) for seg in path.split("."))
            or _has_credential_param(value))


@dataclass(frozen=True)
class Quote:
    """引用（type 57）的载荷 `refermsg`。"""

    svrid: int | None
    displayname: str | None
    content: str | None
    create_time: int | None
    from_usr: str | None


@dataclass(frozen=True)
class Link:
    """链接（type 5）。"""

    url: str | None
    title: str | None
    des: str | None


@dataclass(frozen=True)
class FileRef:
    """文件（type 6）。"""

    title: str | None
    fileext: str | None
    totallen: int | None
    md5: str | None


@dataclass(frozen=True)
class Transfer:
    """转账 / 红包（type 2000）。字段名见 spec §2.9 的实测路径树（全部在 `/wcpayinfo` 之下）。

    `transaction_id` 对应源里的 `<transcationid>` —— **原库拼写少一个 `s`**。
    解析端读那个拼错的标签、对外用正确拼写，不要「顺手修正」源侧标签名。

    `paysubtype` 的中文语义**不猜**（实测无权威映射），原样呈现数字。
    """

    paysubtype: str | None
    amount_desc: str | None       # <feedesc>
    memo: str | None              # <pay_memo>，实测 93% 有
    payer: str | None             # <payer_username>，实测 61% 为空 —— 空是常态，不是异常
    receiver: str | None          # <receiver_username>
    transaction_id: str | None    # <transcationid>（原库拼写）
    transfer_id: str | None       # <transferid>
    begin_time: int | None        # <begintransfertime>
    effective_date: int | None    # <effectivedate>
    invalid_time: int | None      # <invalidtime>
    is_red_envelope: bool         # 由是否出现 <redenvelopetype> 判定（实测 19%）


@dataclass(frozen=True)
class AppMsg:
    kind: int
    title: str | None
    fields: tuple[tuple[str, str], ...]      # 未具名建模的叶子字段，(点分路径, 值)，保序
    quote: Quote | None                      # 仅 57
    link: Link | None                        # 仅 5
    file: FileRef | None                     # 仅 6
    transfer: Transfer | None                # 仅 2000


@dataclass(frozen=True)
class ParseResult:
    """`reason`：`None`=成功 / `'rejected'`=命中危险载荷守卫 / `'malformed'`=规范化后仍解不开。

    **两种失败必须分得开**：`None` 返回（`AppMsg | None`）无法区分「我们主动拒绝」与
    「解不开」，而 §5 要求分开计数（`rejected` / `unparsed`）。带上 `reason`，
    调用方的计数就不可能写错。
    """

    appmsg: AppMsg | None
    reason: str | None
    # 交给 `ET` 的那份规范化文本（成功时必然有，`rejected` 时没算过、为 None）。
    # 存在的理由：别让调用方再 `normalize` 一遍——回填要把正文存盘，重算一次既多跑一遍，
    # 又把「存盘形态」耦合到「`normalize` 幂等」这个隐含前提上。
    normalized: str | None = None


def kind_name(kind: int) -> str:
    """类型的中文名。未确认的兜底成 `appmsg:{kind}`（镜像 `reader._type_name` 的形态）。"""
    return KIND_NAMES.get(kind, f"appmsg:{kind}")


def _to_int(value: str | None) -> int | None:
    """数字字段解析失败给 `None`，**不抛**。

    载荷对手可控：`<svrid>abc</svrid>` 不该让整批导出崩掉（与 T1 那条 `&#abc;` 的裸
    `ValueError` 同一个形状）。空是常态不是异常 —— 实测 `payer_username` 61% 为空。
    """
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _text(el: ET.Element | None, tag: str) -> str | None:
    return None if el is None else el.findtext(tag)


def _int(el: ET.Element | None, tag: str) -> int | None:
    return _to_int(_text(el, tag))


def _leaf_fields(appmsg_el: ET.Element, kind: int) -> list[tuple[str, str]]:
    """未具名建模的叶子字段，(点分路径, 值)，文档序。

    **只收叶子**（无子元素）：非叶子的 `text` 只是标签间的空白，收进来是噪声。
    「只收叶子」**不够**——`recorditem` 本身就是叶子（它没有子元素，整段内层 XML 在
    `text` 里），所以还要显式跳过 `_INNER_XML_PAYLOADS`。
    密材过滤不在这里做 —— 它还要看**值**，由 `parse` 统一过一道。
    """
    skip = (set(_NAMED_SUBTREES.get(kind, ())) | set(_INNER_XML_PAYLOADS)
            | set(_NAMED_LEAVES.get(kind, ())))
    out: list[tuple[str, str]] = []
    # 显式栈而不是递归：载荷对手可控，`<a><a><a>…` 一万层会把递归爆成 RecursionError，
    # 而 `parse` 的契约是「对任意 str 全定义、不抛」。push 时反序，pop 出来就是文档序。
    stack = [(c, c.tag) for c in reversed(list(appmsg_el))
             if c.tag not in skip and c.tag not in _SELF_NAMES]
    while stack:
        el, path = stack.pop()
        kids = list(el)
        if kids:
            stack.extend((c, f"{path}.{c.tag}") for c in reversed(kids))
        else:
            out.append((path, el.text or ""))
    return out


def _quote(appmsg_el: ET.Element) -> Quote:
    ref = appmsg_el.find("refermsg")
    return Quote(
        svrid=_int(ref, "svrid"),
        displayname=_text(ref, "displayname"),
        content=_text(ref, "content"),
        create_time=_int(ref, "createtime"),
        from_usr=_text(ref, "fromusr"),
    )


def _link(appmsg_el: ET.Element) -> Link:
    """链接（type 5）。

    `url` 要过**值**过滤：同一个凭据走 `fields` 被丢掉、走 `Link` 却进信封（spec §4.5 的信封
    含 `link`），这种不一致本身就是缺陷。**只有 `url` 需要这道过滤**——`Quote` 的
    `content`/`displayname`/`from_usr` 是被引用的**用户正文**，对它做值过滤会连用户内容一起丢；
    `FileRef.md5` 是内容指纹不是凭据；`Transfer` 没有凭据字段。**不要顺手扩大。**
    """
    url = appmsg_el.findtext("url")
    # 与 `fields` 的值过滤**同源**：同一个凭据走 fields 被丢、走 Link 却进信封，
    # 这种不一致本身就是缺陷（本函数 docstring 已言明，此处只是把函数换掉）。
    if url is not None and _has_credential_param(url):
        url = None
    return Link(url=url,
                title=appmsg_el.findtext("title"),
                des=appmsg_el.findtext("des"))


def _file(appmsg_el: ET.Element) -> FileRef:
    att = appmsg_el.find("appattach")
    return FileRef(
        title=appmsg_el.findtext("title"),
        fileext=_text(att, "fileext"),
        totallen=_int(att, "totallen"),
        md5=_text(att, "md5"),
    )


def _transfer(appmsg_el: ET.Element) -> Transfer:
    pay = appmsg_el.find("wcpayinfo")
    return Transfer(
        paysubtype=_text(pay, "paysubtype"),
        amount_desc=_text(pay, "feedesc"),
        memo=_text(pay, "pay_memo"),
        payer=_text(pay, "payer_username"),
        receiver=_text(pay, "receiver_username"),
        transaction_id=_text(pay, "transcationid"),      # 源侧拼写：少一个 s
        transfer_id=_text(pay, "transferid"),
        begin_time=_int(pay, "begintransfertime"),
        effective_date=_int(pay, "effectivedate"),
        invalid_time=_int(pay, "invalidtime"),
        is_red_envelope=pay is not None and pay.find("redenvelopetype") is not None,
    )


def parse(raw: str) -> ParseResult:
    """把一条 appmsg 载荷解析成结构化数据。**对任意 `str` 全定义、不抛。**

    三步顺序**不可换**：`is_dangerous` 看的是**原文**（规范化不会删掉 DTD 声明，但不该依赖
    这一点）→ `normalize` → `ET.fromstring`。
    """
    if is_dangerous(raw):
        return ParseResult(None, "rejected")
    try:
        # `normalize` **必须在 try 之内**：它自己会抛 `ValueError`（见下面 `except` 的注释）。
        # 把它挪到 try 外，这个分支就失去它**唯一真正接住的东西**，裸 `ValueError` 直接逃出
        # core 层——`appmsg_rebuild_parts` 对每条载荷调 `parse` 且无外层 try，整次回填会崩。
        norm = normalize(raw)
        root = ET.fromstring(norm)
    except (ET.ParseError, ValueError):
        # `ValueError` 这一支**不是纯防御性的**：它接住的主要是 **`normalize` 自己的
        # `ValueError`** —— `_keep_charref` 的 `int(group(2), 10)` 撞上 Python 3.11+ 的
        # 十进制 int 转换上限（4300 位）。有 repro：
        # `parse("<msg>&#" + "1" * 6000 + ";</msg>")`（`is_dangerous` 不拦它——无 DTD/ENTITY，
        # 而载荷是对手可控的）。这正是上面 `normalize` 不能挪出 try 的原因。
        #
        # **`ET.fromstring` 自己不抛 `ValueError`**：stdlib/expat 对带编码声明的 str 一律正常
        # 解析（实测 3.12/expat 对 `encoding="utf-8"` / `"gbk"` / `"nonsense-99"` 都不抛）；
        # 抛 `ValueError` 是 lxml 的行为，不是这里用的 `xml.etree`。多兜这个类型是失败方向上
        # 的取舍——「裸异常逃出 core 层」本仓库明令禁止。
        return ParseResult(None, "malformed")
    appmsg_el = root.find("appmsg")
    if appmsg_el is None:
        return ParseResult(None, "malformed")

    # 缺失或非数字一律 -1（`kind_name` 兜底成 `appmsg:-1`），不猜、不抛。
    kind = _to_int(appmsg_el.findtext("type"))
    if kind is None:
        kind = -1
    fields = tuple((p, v) for p, v in _leaf_fields(appmsg_el, kind)
                   if not _is_secret(p, v))
    return ParseResult(
        AppMsg(
            kind=kind,
            title=appmsg_el.findtext("title"),
            fields=fields,
            quote=_quote(appmsg_el) if kind == KIND_QUOTE else None,
            link=_link(appmsg_el) if kind == KIND_LINK else None,
            file=_file(appmsg_el) if kind == KIND_FILE else None,
            transfer=_transfer(appmsg_el) if kind == KIND_TRANSFER else None,
        ),
        None,
        norm,
    )
