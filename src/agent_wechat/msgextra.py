"""位置（48）/ 名片（42）载荷的解析——`msg_parts` 那条旁路的第二个类型族（EA-041）。

**这不是 `appmsg` 的扩展**：两者共用规范化与危险载荷守卫（`appmsg.normalize` /
`appmsg.is_dangerous`），但根元素与字段模型不同，硬塞进 `appmsg.parse` 会让它的
「`<appmsg>` 专用解析器」这条契约变成「看类型分支」。

真机形态（2026-10-14 实测 202 条、50 种结构；**与审计条目里那个自造探针载荷有三处差异**，
照抄夹具就会错）：
  · 48 是 `<msg><location x=… y=… scale=… label=… maptype=… poiname=… …/></msg>`；
  · 42 是 `<msg username=… nickname=… alias=… …/>`——**属性挂在根上，没有子元素**；
  · 两种都可能是「无声明 / 声明在文首」，**都可能带群前缀**（`wxid_xxx:\\n`）；
  · 属性集合变化很大（`poiname` 与 `label` 并存，`fromusername` / `adcode` / `cityname` /
    `infourl` 等可选），故**按属性出字段**，不为每个可选字段建一个 dataclass 属性。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass

from . import appmsg

KIND_LOCATION = 48
KIND_CARD = 42
KINDS = (KIND_LOCATION, KIND_CARD)
KIND_NAMES = {KIND_LOCATION: "位置", KIND_CARD: "名片"}
# 承载属性的元素：48 是 `<msg>` 的子元素，42 就是根自己
_ATTR_OWNER = {KIND_LOCATION: "location", KIND_CARD: "msg"}

# 每个类型**留下**哪些属性——**allow-list，不是黑名单**。
#
# 为什么是白名单：实测 42 的属性里 `antispamticket`（最长 606 字符的加好友凭据）、
# `brandHomeUrl`（最长 2,487）、`brandSubscriptConfigUrl`（578）、`biznamecardinfo`（300）
# 都对阅读没有信息量，第一个还是**每次会话一换的凭据**；而 `appmsg` 那套过滤器抓不住它们
# （真机实测：只有 9 条 `biznamecardinfo` 被判成不透明块，`antispamticket` 一条都没命中）。
# 黑名单是打地鼠（`appmsg` 的注释自己写过这条），而这里的方向正相反：**认不出来的属性一律
# 不进产物**——少一个字段是看得见的缺失，多一个凭据不是。
_KEEP = {
    KIND_LOCATION: ("x", "y", "scale", "label", "poiname", "maptype", "poiid",
                    "buildingId", "floorName", "adcode", "cityname", "fromusername"),
    KIND_CARD: ("username", "nickname", "alias", "sex", "province", "city", "sign",
                "regionCode", "certflag", "certinfo", "imagestatus", "scene"),
}


@dataclass(frozen=True)
class Extra:
    """一条 48/42 的解析结果。`fields` 是 `(属性名, 值)`，按 `_KEEP` 的次序——**不是文档序**：
    XML 属性本身无序，照 ET 给的字典序排会让「x 在 y 前面」这种约定随 Python 版本漂。"""
    kind: int
    fields: tuple[tuple[str, str], ...]


def kind_name(kind: int) -> str:
    """类型的中文名。未确认的类型回落成 `typeNN`（**不猜**，spec §2.9 的规矩）。"""
    return KIND_NAMES.get(kind, f"type{kind}")


def parse(msg_type: int, raw: str) -> Extra | None:
    """解析一条载荷。**对任意 str 全定义、不抛**（与 `appmsg.parse` 同契约）：类型不在此族、
    解不开、缺承载元素、命中危险载荷守卫（`<!DOCTYPE` / `<!ENTITY`）一律返回 None——
    调用方按「这条没有结构化内容」处理，不当异常。

    空值是**常态**而非异常（实测 `infourl` / `certinfoext` 有近一半是空串），故空串不落字段。
    """
    if msg_type not in KINDS or not raw:
        return None
    if appmsg.is_dangerous(raw):
        return None
    try:
        root = ET.fromstring(appmsg.normalize(raw))
    except (ET.ParseError, ValueError):
        # ValueError 是 `normalize` 没滤净的畸形容引（EA-033 那族）；core 层不许裸抛，
        # 而这里的语义就是「这条解不开」——与 `appmsg.parse` 同一处置。
        return None
    owner = _ATTR_OWNER[msg_type]
    el = root if owner == "msg" else root.find(owner)
    if el is None:
        return None
    out: list[tuple[str, str]] = []
    for name in _KEEP[msg_type]:
        value = el.get(name)
        if not value:                      # 缺属性与空串同待：都不落字段
            continue
        if appmsg.is_secret(name, value):  # 与 `fields` 同源的一道凭据闸（防御纵深）
            continue
        out.append((name, value))
    return Extra(msg_type, tuple(out))
