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
