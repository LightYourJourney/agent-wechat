"""媒体层：图片 dat 解密、语音 SILK、视频定位（spec: 2026-09-14-media-decrypt-design）。

算法事实全部来自本机实测（spec §2）与 WeFlow/WeFlowPP 的格式研究（Python 独立实现，
未复制任何上游代码）。密钥纯磁盘推导：kvcomm 文件名 code + md5(code+裸wxid)[:16]（ASCII）
+ dat 尾部统计 xorKey；**推导后必须用样本验证，不验证不交付**。

红线：code/aesKey/xorKey 是推导材料，视同密钥——不进日志、错误消息、repr。
依赖方向（单向，不得反向）：**media 可以 import store；store 不得 import media**；本模块不
import reader（reader 可以 import 本模块）。唯一的 store 调用点是视频导出的 refs 驱动归属
（`store.media_refs_by_chat` / `count_media_refs`）——视频树是账号级的，没有索引就无从知道
某条视频属于哪个会话（spec §4.7）。
"""
from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import pathlib
import re
import sqlite3
import stat
import subprocess
import tempfile
import urllib.error
from collections.abc import Sequence
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import decrypt, errors, messages, paths, store
from . import keys as keys_mod

# 可选依赖：缺 pilk 只意味着语音出原始 .slk（能力级降级），不是导入失败。
# 顶层 import（而非函数内）——`media.decrypt` 同理（Ruling 17）：测试要在调用前
# 就能 monkeypatch 掉它们，函数内 import 会让属性在调用前不存在。
try:
    import pilk
    _PILK = pilk
except ImportError:  # pragma: no cover
    _PILK = None

# ---------- 图片容器 ----------

V1_SIG = b"\x07\x08\x56\x31\x08\x07"
V2_SIG = b"\x07\x08\x56\x32\x08\x07"

MAGICS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "jpg"), (b"\x89PNG", "png"), (b"GIF8", "gif"),
    (b"RIFF", "webp"), (b"wxgf", "wxgf"),
)


def detect_ext(buf: bytes) -> str | None:
    for sig, ext in MAGICS:
        if buf.startswith(sig):
            return ext
    return None


def _pkcs7_strip(plain: bytes) -> bytes:
    """严格 PKCS#7：pad 越界或尾字节不全等都是密钥不对——抛 DecryptError，绝不返回乱明文。"""
    if not plain:
        raise errors.DecryptError("AES 段解密结果为空")
    pad = plain[-1]
    if not (0 < pad <= 16) or plain[-pad:] != bytes([pad]) * pad:
        raise errors.DecryptError("PKCS#7 校验失败（图片密钥很可能不匹配）")
    return plain[:-pad]


def decrypt_dat(data: bytes, keys: MediaKeys) -> bytes:
    """dat 解密：V2 = 15B 头 + aligned(aesSize) AES-128-ECB 段 + 中段直通 + 末 xorSize XOR；
    V1 = 剥 6B 明文魔数后余下单字节 XOR；V0 = 全文件单字节 XOR。布局与恒等式见 spec §2.1。"""
    if data[:6] != V2_SIG:
        if data[:6] == V1_SIG:
            data = data[6:]        # V1 魔数明文存储：剥掉再 XOR，输出才是图片本体（乱前缀过不了 detect_ext）
        return bytes(b ^ keys.xor_key for b in data)
    if len(data) < 15:
        raise errors.DecryptError("dat 文件不足 15 字节头")
    aes_size = int.from_bytes(data[6:10], "little")
    xor_size = int.from_bytes(data[10:14], "little")
    aligned = aes_size + (16 - aes_size % 16)      # 已对齐也 +16（整块 PKCS#7）
    payload = data[15:]
    if xor_size < 0 or aligned > len(payload) or xor_size > len(payload) - aligned:
        raise errors.DecryptError("dat 长度字段与文件大小不符（截断或损坏）")
    dec = Cipher(algorithms.AES(keys.aes_key_ascii), modes.ECB()).decryptor()
    plain = _pkcs7_strip(dec.update(payload[:aligned]) + dec.finalize())
    remaining = payload[aligned:]
    raw_len = len(remaining) - xor_size
    tail = bytes(b ^ keys.xor_key for b in remaining[raw_len:])
    return plain + remaining[:raw_len] + tail


# ---------- 密钥推导 ----------

@dataclass(frozen=True, repr=False)
class MediaKeys:
    xor_key: int
    aes_key_ascii: bytes               # md5 hex 前 16 字符的 ASCII 字节（不是 hex 解码！）
    code: str

    def __repr__(self) -> str:         # 显式遮蔽：密钥与推导材料不进日志（全局红线）
        return "MediaKeys(***)"


_KVCOMM_SUBDIRS = ("app_data/net/kvcomm", "app_data/ilink/kvcomm")
_CODE_RE = re.compile(r"^key_(\d+)_")


def _kvcomm_codes() -> list[str]:
    """kvcomm 密钥码候选（Documents 与 Data 两级都试；去重保序）。

    目录读不了（权限/坏符号链接）只是**这一个**目录不可用：跳过它继续找别的目录——
    让裸 OSError 逃出去会击穿「core 只抛项目异常」的契约，而它想表达的降级语义本来
    就是能力级的（拿不到 code 会在验证阶段归到 MediaKeyError）。
    """
    codes: list[str] = []
    for base in (paths.CONTAINER, paths.CONTAINER.parent):
        for sub in _KVCOMM_SUBDIRS:
            d = base / sub
            if not d.is_dir():
                continue
            try:
                entries = list(d.iterdir())      # 先落地：生成器迭代中途也可能抛 OSError
            except OSError:
                continue
            for f in entries:
                m = _CODE_RE.match(f.name)
                if m and m.group(1) not in codes:
                    codes.append(m.group(1))
    return codes


def _attach_dats(account: str) -> list[pathlib.Path]:
    """attach 树里的样本 dat（供 xorKey 统计与密钥验证；Ruling 16）。

    glob 是惰性的，**必须**经 `_safe_iter` 立即物化：否则目录读不了时，异常会从
    `_derive_xor_key` 的 for 或 `derive_keys` 的 `next(iter(...))` 里裸奔出 core 层。
    读不了 = 没有样本，落回 derive_keys 既有的「无样本」MediaKeyError。
    """
    return _safe_iter(paths.XWECHAT_FILES / account / "msg/attach", "*/*/Img/*.dat")


def _derive_xor_key(account: str, limit: int = 200) -> int | None:
    """扫样本 dat 尾部：b[-2]^0xFF == b[-1]^0xD9（JPEG EOI）取众数；≥4 个一致才认可。"""
    counts: dict[int, int] = {}
    scanned = 0
    for p in _attach_dats(account):
        if scanned >= limit:
            break
        try:
            b = p.read_bytes()
        except OSError:
            continue
        if len(b) < 8:
            continue
        k = b[-2] ^ 0xFF
        if k == b[-1] ^ 0xD9:
            counts[k] = counts.get(k, 0) + 1
        scanned += 1
    if not counts:
        return None
    k, n = max(counts.items(), key=lambda kv: kv[1])
    return k if n >= 4 else None


def derive_keys(account: str) -> MediaKeys:
    """推导并**验证**图片密钥。无 code / 无样本 / 验证不过 → MediaKeyError（能力级降级）。"""
    codes = _kvcomm_codes()
    xor_key = _derive_xor_key(account)
    sample = next(iter(_attach_dats(account)), None)
    if sample is None:
        raise errors.MediaKeyError("msg/attach 下没有样本 dat，图片密钥无从验证")
    if xor_key is None:
        raise errors.MediaKeyError("样本 dat 尾部统计不出一致的 xorKey")
    bare = paths.bare_wxid(account)
    for code in codes:
        keys = MediaKeys(
            xor_key=xor_key,
            aes_key_ascii=hashlib.md5((code + bare).encode()).hexdigest()[:16].encode("ascii"),
            code=code)
        try:
            out = decrypt_dat(sample.read_bytes(), keys)
        except OSError as exc:
            # 样本是**唯一**的验证手段（设计上只取一个）：读不了就没法验证，只能能力级降级。
            # 裸 OSError 逃出去会击穿 core 的错误契约（适配层翻译不了它）。
            raise errors.MediaKeyError(
                "验证用的样本 dat 读不出来，图片密钥无从验证",
                hint=f"{exc}；图片解密不可用，语音/视频不受影响") from exc
        except errors.DecryptError:
            continue
        if detect_ext(out):
            return keys
    raise errors.MediaKeyError(
        "kvcomm 密钥码与账号不匹配（或样本 dat 损坏）",
        hint="图片解密不可用；语音/视频不受影响。若微信刚更新过可重试，或检查 kvcomm 目录")


# ---------- 定位 ----------

def month_of(ts: int) -> str:
    """时间戳 → 本地时区 YYYY-MM。微信按**本机本地月**建目录，用 UTC 会在月初/月末错位。"""
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m")


def _shift_month(mon: str, delta: int) -> str:
    y, m = int(mon[:4]), int(mon[5:7])
    i = y * 12 + (m - 1) + delta
    return f"{i // 12:04d}-{i % 12 + 1:02d}"


def month_bounds(months: list[str]) -> list[tuple[int, int]]:
    """月份 → [lo, hi) 的 unix 秒区间，用与 month_of 同一套本地时区规则。

    `month_of` 是 `datetime.fromtimestamp(ts).strftime("%Y-%m")`（**本地时区**），
    所以下界必须是本地时间的当月 1 日 00:00 —— 用 UTC 会让跨月边界错一天。
    月份串须已是 `YYYY-MM`（调用方校验格式后），本函数**不再校验形状**。

    **形状合不等于可表示**：调用方的 `MONTH_RE` 是纯形状判据，`9999-12` 与 `0000-01`
    都逐字符合规（四位年 + 合法月），但 `datetime` 的年是 1–9999，而上界还要算
    **下个月**的 1 号（`y + mo // 12`），于是
      · `0000-01` → `ValueError: year 0 is out of range`
      · `9999-12` → `ValueError: year 10000 is out of range`
    **边界是平台/时区相关的，别去预测它**：`.timestamp()` 走本地时区，UTC+ 的机器上
    `0001-01` 也会越界（本地 1 月 1 日 00:00 折回 UTC 就成了 year 0；本机实测如此），
    所以「年份须在 1–9998」只是一句**给用户的实用区间**，不是判据。判据就是这段
    `except`——**能表示与否由 `datetime` 说了算，不由我们猜**。
    裸 `ValueError` 会一路穿透到 CLI（traceback）与 MCP（「内部错误」），而
    `chat_export` / `USAGE.md` / `TOOLS.md` 对 `--months` 的宣称是「格式不符**一律报错**
    （返回 error）」——用户拿到的必须是一句带修复指引的中文，不是一个年份上界的 traceback。

    故**在这一处**把越界翻成 `SearchInputError`（`media` 自己的名字空间里原本就没有
    月份相关的错误类型，而这就是一条「入参不合法」）。四个调用点共用本函数，
    改这里 `media export` / `message_media` / `chat export` 一并变好。
    **不要在调用点各加一层 try**：那是同一段校验的四份副本，正是本分支立 `chat_text.py`
    要消掉的那类分叉（丙 R21）。
    """
    out: list[tuple[int, int]] = []
    for m in months:
        y, mo = int(m[:4]), int(m[5:7])
        try:
            lo = int(datetime.datetime(y, mo, 1).timestamp())
            hi = int(datetime.datetime(y + mo // 12, mo % 12 + 1, 1).timestamp())
        except (ValueError, OverflowError, OSError) as exc:
            raise errors.SearchInputError(
                f"月份 {m} 超出可表示的时间范围（年份须在 1–9998；"
                f"9999-12 的下个月已是 year 10000）") from exc
        out.append((lo, hi))
    return out


_SUFFIX_PRIORITY = ("_hd", "_h", "", "_b", "_t")


def resolve_image(account: str, chat_md5: str, ts: int, file_key: str) -> pathlib.Path | None:
    """消息 → dat 文件。月份按本地时区，miss 自动试相邻月；后缀 _hd/_h > 裸名 > _b/_t。"""
    mon = month_of(ts)
    for m in (mon, _shift_month(mon, -1), _shift_month(mon, 1)):
        d = paths.XWECHAT_FILES / account / "msg/attach" / chat_md5 / m / "Img"
        if not d.is_dir():
            continue
        for suf in _SUFFIX_PRIORITY:
            p = d / f"{file_key}{suf}.dat"
            if p.exists():
                return p
    return None


def resolve_video(account: str, ts: int, file_key: str) -> tuple[pathlib.Path, bool] | None:
    """消息 → 视频本体（明文 mp4）；miss 回退缩略图 → (路径, True)。"""
    mon = month_of(ts)
    for m in (mon, _shift_month(mon, -1), _shift_month(mon, 1)):
        d = paths.XWECHAT_FILES / account / "msg/video" / m
        if not d.is_dir():
            continue
        for name in (f"{file_key}.mp4", f"{file_key}_raw.mp4"):
            p = d / name
            if p.exists():
                return (p, False)
        t = d / f"{file_key}_thumb.jpg"
        if t.exists():
            return (t, True)
    return None


def resolve_sticker(account: str, md5: str) -> tuple[pathlib.Path, bool] | None:
    """贴纸本体 → `(路径, 是否缩略图)`；本机没有 → None。

    目录布局实测（2026-09-22）：`business/emoticon/{Persist,Thumb}/<md5[:2]>/…`，
    **按 md5 内容寻址**——同一个 md5 只有一份。Thumb 的文件带 `.thumb` 后缀，
    Persist 的不带。

    `Persist` 优先：它是本体，`Thumb` 是缩略图（实测中位 16,016 B vs 4,880 B）。
    返回值第二位把这件事挑明，调用方**必须**把它透出去——与 video 命中缩略图时
    算 `exportable` 同一个理由：不能让「只有一张小图」看起来像「本体在」。

    **这里只做定位，不解密**（本机文件的加密方案两轮 spike 均未破，spec 附录 C/E.4）。
    唯一的调用方拿它判「本机有没有那份加密副本」，用于 `degraded` 文案。
    """
    root = paths.XWECHAT_FILES / account / "business/emoticon"
    p = root / "Persist" / md5[:2] / md5
    if p.is_file():
        return (p, False)
    t = root / "Thumb" / md5[:2] / f"{md5}.thumb"
    if t.is_file():
        return (t, True)
    return None


# ---------- 消息 → 媒体引用（media_refs 的数据源） ----------

# 实测（spec §2.6）：dat/mp4 文件名哈希在 packed_info_data 里（32-hex）；
# XML md5 与文件名**无关**（19,832 条 0 命中），只作 packed 缺失时的兜底记录。
_HEX32 = re.compile(r"[0-9a-fA-F]{32}")
_XML_MD5 = re.compile(r'<(?:img|videomsg)[^>]*?\smd5="([0-9a-fA-F]{16,32})"')


def extract_media_ref(local_type: int, content: str | None,
                      packed: bytes | None) -> tuple[str, str, str | None] | None:
    """(kind, file_key, xml_md5)；非图片/视频或两处都没有 → None。纯函数。"""
    if local_type == messages.TYPE_IMAGE:
        kind = "image"
    elif local_type == messages.TYPE_VIDEO:
        kind = "video"
    else:
        return None
    file_key = xml_md5 = None
    if packed:
        m = _HEX32.search(packed.decode("latin-1", "ignore"))
        if m:
            file_key = m.group(0).lower()
    if content:
        m = _XML_MD5.search(content)
        if m:
            xml_md5 = m.group(1).lower()
    if file_key is None and xml_md5 is not None and len(xml_md5) == 32:
        file_key = xml_md5                # 兜底（现网命中率 0，仅防 packed 缺失）
    if file_key is None:
        return None
    return (kind, file_key, xml_md5)


# 贴纸 `<emoji>` 的属性取值。**用正则不用 XML 解析器**：与 extract_media_ref 对
# `<img>`/`<videomsg>` 的处理同一手法，且对畸形载荷更耐操——本函数契约是
# 「对任意 str 全定义、不抛」（载荷对手可控），解析器会抛。
# **属性与 `=` 之间的空白因消息而异，必须容忍**：实测最新 400 条贴纸里 **193 条**
# 把属性写成 `cdnurl = "…"` / `aeskey= "…"`，而且**同一个 XML 内还会混用**
# （`md5="…"` 紧写、`cdnurl = "…"` 带空格）。写死 `attr="` 会**静默漏掉 48%**——
# 2026-09-23 实测：`cdnurl` 与 `desc` 严格匹配 163/400、宽松 356/400。
# 这正是本仓库最贵的那类缺陷：夹具编码了想象中的形态，代码与测试互相印证，全绿而功能为零。
_ATTR_EQ = r'\s*=\s*'


def _emoji_attr_re(name: str, value: str = r'[^"]*') -> re.Pattern[str]:
    """`<emoji>` 上某个属性的取值。**唯一出处**——解析与 CDN 取件都从这里派生。

    `value` 是该属性的**取值形态**：只有 `md5` 需要收紧成 32-hex（`extract_sticker_ref`
    的契约要求非法 md5 一律返回 None，而通用 `[^"]*` 会把 `md5="short"` 也放进来）。
    """
    return re.compile(rf'<emoji\b[^>]*?\s{re.escape(name)}{_ATTR_EQ}"({value})"')


# **不要叫 `_HEX32`**：本模块已有一个同名的**编译后**正则（`extract_media_ref` 在用），
# 复用那个名字会把它遮蔽成一个字符串，静默打断 `extract_media_ref`（写完当场踩过一次）。
_HEX32_PAT = r'[0-9a-fA-F]{32}'

_STICKER_MD5 = _emoji_attr_re("md5", _HEX32_PAT)
_STICKER_ATTR = {a: _emoji_attr_re(a) for a in ("desc", "emojiattr", "productid")}
# 两种载荷形态**不一样**，这是本函数最容易写错的地方（自审实测：写错过一次，
# 三个真实夹具全部返回 None，整个名称特性会静默归零）：
#
#   desc      = `\x0a <len> <lang> \x12 <len> <text>` 的重复
#               例：`\n\x0f` + (`\n\x05zh_cn\x12\x06谢谢`) + (`\n\x05zh_tw\x12\x00`) + …
#   emojiattr = `\x0a <len> <text>`，**没有语言、也没有 \x12 分隔符**
#               例：`\n\x06谢谢`
#
# 所以匹配 `zh_cn` 必须锚在 `\x12` 上（它只在 desc 形态里出现），
# 而 emojiattr 走「整串找中文」的兜底。
_PROTO_ZHCN = re.compile(rb"zh_cn\x12(.)", re.S)
_CJK = re.compile(r"[一-鿿]+")


def _sticker_name(raw_b64: str | None) -> str | None:
    """`desc` / `emojiattr` 的 Base64 protobuf → 中文名。

    **任何一步失败都返回 None，绝不抛**：Base64 非法、protobuf 形状不符、没有中文——
    都只是「这一条拿不到名字」，不该把整条贴纸拖成 None。

    `desc` 取 `zh_cn` 那一块。**长度前缀按字节切，不是按字符**：「谢谢」是 2 字符 6 字节，
    按字符切会多吃 4 个字符。`zh_cn` 存在但为空（实测 zh_tw/default 可为空串）时同样
    落到兜底，**不要**顺手取「zh_cn 之后第一个中文」——那会拿到下一个语言块的文本。
    """
    if not raw_b64:
        return None
    try:
        blob = base64.b64decode(raw_b64 + "=" * (-len(raw_b64) % 4))
    except (binascii.Error, ValueError):
        return None
    m = _PROTO_ZHCN.search(blob)
    if m:
        seg = blob[m.end():m.end() + m.group(1)[0]]
        names = _CJK.findall(seg.decode("utf-8", "replace"))
        if names:
            return names[-1]
    # 兜底：emojiattr（无语言标注）与 zh_cn 为空的情形——取整串第一个中文串。
    names = _CJK.findall(blob.decode("utf-8", "replace"))
    return names[0] if names else None


def extract_sticker_ref(local_type: int, content: str | None
                        ) -> tuple[str, str | None, str | None] | None:
    """`(md5, name, pack_id)`；非 47 或 XML 里没有合法 md5 → None。**纯函数、不抛。**

    与 `extract_media_ref` 的分工：那个只认图片/视频（`TYPE_IMAGE`/`TYPE_VIDEO`），
    本函数只认贴纸（`TYPE_STICKER`）。两者互斥，调用方按 msg_type 择一。

    **只取三样东西，别的一律不要**：`md5`（文件定位与短 ID）、中文名（显示）、
    `pack_id`（包归属）。**注意 `pack_id` 只被返回、不被持久化**：调用方
    （`reader._decode_row`）把它解包成 `_pack` 后即丢弃，`messages` / `media_refs` /
    `msg_parts` 三张表都没有存它的列——别把它读成「已经记下了包归属」。
    XML 里还有 `cdnurl` / `aeskey` / `encrypturl` 等凭据，**它们绝不进入返回值**——
    贴纸 XML 不落库是凭据问题的结构性根治（spec §7.1），一旦把凭据带出去，那道防线就没了。
    """
    if local_type != messages.TYPE_STICKER or not content:
        return None
    m = _STICKER_MD5.search(content)
    if not m:
        return None
    md5 = m.group(1).lower()
    name = None
    for attr in ("desc", "emojiattr"):        # desc 优先：它是多语言的，更可能有 zh_cn
        a = _STICKER_ATTR[attr].search(content)
        if a and (name := _sticker_name(a.group(1))):
            break
    pk = _STICKER_ATTR["productid"].search(content)
    return (md5, name, pk.group(1) if pk else None)


# ---------- 导出 ----------

@dataclass
class Counts:
    ok: int = 0
    failed: int = 0
    missing: int = 0
    raw_only: int = 0      # voice 专属：pilk 缺失时导出原始 .slk 的数量
    thumb: int = 0         # video 专属：回退缩略图数量
    skipped: int = 0       # 本次因产物已存在而跳过的**文件**数（有本体的视频，其缩略图不再另写，故不另计）；不吃 --limit 预算
    remaining: int = 0     # 撞上 --limit 后本会导出的条数（只计数，不落盘）


@dataclass
class MediaReport:
    image: Counts = field(default_factory=Counts)
    voice: Counts = field(default_factory=Counts)
    video: Counts = field(default_factory=Counts)
    sticker: Counts = field(default_factory=Counts)
    degraded: list[str] = field(default_factory=list)
    truncated: bool = False
    out_dir: str = ""      # 实际使用的输出根（export_media 赋值；CLI/MCP 信封从这取，不重算）

    def to_dict(self) -> dict:
        return {"image": {"ok": self.image.ok, "failed": self.image.failed,
                          "missing": self.image.missing,
                          "skipped": self.image.skipped, "remaining": self.image.remaining},
                "voice": {"ok": self.voice.ok, "failed": self.voice.failed,
                          "raw_only": self.voice.raw_only,
                          "skipped": self.voice.skipped, "remaining": self.voice.remaining},
                "video": {"ok": self.video.ok, "thumb": self.video.thumb,
                          "missing": self.video.missing,
                          "skipped": self.video.skipped, "remaining": self.video.remaining},
                "sticker": {"ok": self.sticker.ok, "failed": self.sticker.failed,
                            "missing": self.sticker.missing,
                            "skipped": self.sticker.skipped,
                            "remaining": self.sticker.remaining},
                "degraded": list(self.degraded), "truncated": self.truncated}


FFMPEG_CANDIDATES = ("ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg")
_ffmpeg_cache: str | None | False = False    # False = 未探测


def _find_ffmpeg() -> str | None:
    """探测一次并缓存：wxgf 转码是**可选**能力，找不到就退回存原容器，不算错误。"""
    global _ffmpeg_cache
    if _ffmpeg_cache is False:
        _ffmpeg_cache = None
        for c in FFMPEG_CANDIDATES:
            try:
                subprocess.run([c, "-version"], capture_output=True, timeout=10)
                _ffmpeg_cache = c
                break
            except (OSError, subprocess.TimeoutExpired):
                continue
    return _ffmpeg_cache


def _wxgf_to_jpeg(data: bytes, dest: pathlib.Path) -> bool:
    """wxgf（HEVC 容器）→ 首帧 JPEG，**直写 dest**（必需：微信目录只读）。

    dest 由调用方算成 out_root 下的终路径；HEVC 流走 stdin 交给 ffmpeg，因此全过程
    一个中间文件都不落——**绝不能**把临时文件写到微信数据目录里（那是本次导出的
    前置红线），out_root 里也不需要留半成品。失败（无 ffmpeg / 流是坏的）→ False，
    调用方退回存 .wxgf 原容器。
    """
    ff = _find_ffmpeg()
    if ff is None:
        return False
    i = data.find(b"\x00\x00\x00\x01")
    if i < 0:
        return False
    try:
        r = subprocess.run([ff, "-y", "-loglevel", "error", "-f", "hevc", "-i", "pipe:0",
                            "-frames:v", "1", str(dest)],
                           input=data[i:], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        r = None
    try:
        if r is None or r.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)   # ffmpeg 万一建了半成品：清掉，不留坏文件
            return False
        dest.chmod(0o600)                  # ffmpeg 落盘的权限跟 umask，这里收口（同 _save）
    except OSError:
        # 收尾这几步（stat / unlink / chmod）任一 OSError 都只能算「这次没转成」：裸 OSError
        # 逃出去会击穿 core 的错误契约（Ruling 21，与 Ruling 9/20 同族），而调用方对 False
        # 的处置是完整的（回退存 .wxgf 容器）。可能已落盘的 jpeg 在这里尽力清掉——宁可回退
        # 存原容器，也不留一个权限没收紧的明文图。
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def _chmod_quiet(p: pathlib.Path) -> None:
    """尽力收紧权限（Ruling 15）：失败只意味着这一级没收紧，不是导出失败。

    典型场景：`--out` 指向用户不拥有的既有目录——`mkdir(exist_ok=True)` 对它无害，
    但 `chmod` 会被拒。裸 OSError 逃出 core 层是契约违规，而权限到底有没有收紧由
    `export_media` 在开头的一次 stat 检查如实告知（恰好一条 degraded，含路径）。
    """
    try:
        p.chmod(0o700)
    except OSError:
        pass


def export_root() -> pathlib.Path:
    """导出根：`--out` 省略时全部产物的落点（`HOME_DIR/media`）。

    **调用时求值，不做模块常量**——这不是风格问题：`purge` 的产物清单靠 monkeypatch
    `paths.HOME_DIR` 把路径重定向到临时目录（`tests/test_purge.py` 的红线 fixture），
    而 import 时绑定值的常量会让那条保护静默失效，后果是测试去删真实数据。
    同款先例：`store.db_paths()`、`decrypt.tmp_dir()`。

    存在的第二个理由：路径的唯一来源必须在拥有它的模块里。`purge.py` 与 `usecases.py`
    各拼一遍 `HOME_DIR / "media"` 就是第二份来源——拥有者演进时它不会跟着演进。
    """
    return paths.HOME_DIR / "media"


def ensure_tree(root: pathlib.Path, *parts: str) -> pathlib.Path:
    """逐级建目录并**逐级 chmod 0700**，返回最终路径（Ruling 11）。

    **公开名**（原 `_ensure_tree`）：同一条「逐级 0700」现在也服务于**非媒体产物**
    （`chatview.write` 的会话时间线、`moments_export` 的 HTML），复制一份就是第二份真相。
    名字里的 `MediaOutputError` 因此涵盖非媒体产物——改名会波及既有消费点，不值当（裁定 11）。

    `mkdir(parents=True, mode=…)` 只给最后一级套 mode，中间级按 umask 落成 0755；而
    `exist_ok=True` 对**已存在**的目录完全不动权限。本函数的调用点要的是「这条输出路径
    每一级都只有我能进」，所以只能逐级 mkdir + chmod（chmod 才是对已存在目录生效的那一下）。

    **root 本身（out_root）也在收紧范围内**，即使它已存在（`--out` 指向既有目录是 spec §6
    允许的用法）：它是本次导出的输出根，明文照片/视频全在里面，spec 要求 0700——这不是
    误伤用户目录，是导出数据的安全边界。只 chmod 权限，不动既有的任何文件。

    mkdir 失败（只读目录/磁盘满/不可写 HOME）→ MediaOutputError：输出根建不出来的意思是
    整次导出无从落盘，不是某个文件失败；裸 OSError 会穿透 core 层的错误契约。
    """
    p = root
    try:
        p.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        # 裸 OSError 会穿透 core 层（CLI 印 traceback、MCP 落 generic_payload 兜底）：
        # 建不出来是**整次导出**不可用（磁盘满、只读挂载、不可写 HOME），不是某个文件失败。
        raise errors.MediaOutputError(
            f"输出目录不可用：{p}",
            hint="请检查该路径的权限与磁盘空间，或用 --out 指定一个可写目录") from exc
    _chmod_quiet(p)
    for part in parts:
        p = p / part
        try:
            p.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise errors.MediaOutputError(
                f"输出目录不可用：{p}",
                hint="请检查该路径的权限与磁盘空间，或用 --out 指定一个可写目录") from exc
        _chmod_quiet(p)
    return p


def _safe_iter(d: pathlib.Path, pattern: str | None = None) -> list[pathlib.Path]:
    """枚举目录（或按 pattern glob）并**立即物化**；读不了返回 []（Ruling 12）。

    两个要点：`glob` 是惰性生成器——OSError 在**迭代时**才抛，所以「创建 + 物化」必须都在
    try 里，否则异常会从 for 循环裸奔出 core 层（与 Ruling 6 对 kvcomm 同一规矩）；一个目录
    读不了只意味着**这一个**目录没东西可导，不是整次导出失败，也不该加 degraded。
    """
    try:
        found = d.glob(pattern) if pattern else d.iterdir()
        return sorted(found)
    except OSError:
        return []


def _out_path(out_root: pathlib.Path, cm: str, kind: str, mon: str,
              stem: str, ext: str) -> pathlib.Path:
    """导出终路径的**纯计算**——不建目录、不写盘、不 stat。

    与 `_dest` 是同一条路径的两种用法：`_dest` 先 `ensure_tree` 再拼名（写盘用），
    本函数只拼名（**探测**用）。探测若走 `_dest`，一次 skip 判定就会在 `out_root`
    下凭空造出空目录树。
    """
    return out_root / cm / kind / mon / f"{stem}.{ext}"


def _out_candidates(kind: str, base: str) -> tuple[tuple[str, str], ...]:
    """基准名 → 写盘可能落的 `(stem, ext)`，**顺序即优先级**（首个命中即用）。

    这是「一个基准名能落成哪些文件」的**唯一出处**：写侧（`export_media` 的跳过探测）、
    读侧（`exported_path`）都从它派生。分两份写就会出现「读侧说已导出、写侧说没有」
    那类静默 404。

    `base` 的口径按 kind 不同——就是三处调用点各自的实参：
      - `image`：源 dat 的 stem（`{file_key}{_hd|_h||_b|_t}`）。写盘**保留原 stem**，
        扩展名由**解密后的内容**决定，故扩展名全枚举。
      - `video`：`file_key`。写盘用**源文件**的 stem，真实语料里有 `{K}` / `{K}_raw`
        两个变体，无缝体时落 `{K}_thumb.jpg`。
      - `voice`：`{ts}_{local_id}`。pilk 可用写 `.wav`，缺失回退 `.slk`。
    """
    if kind == "image":
        return tuple((base, e) for e in _EXPORT_EXTS)
    if kind == "video":
        return ((base, "mp4"), (f"{base}_raw", "mp4"), (f"{base}_thumb", "jpg"))
    if kind == "voice":
        return ((base, "wav"), (base, "slk"))
    if kind == "sticker":
        # 源文件**没有扩展名**（`Persist/<md5>`），扩展名由**下载到的内容**决定，
        # 与 image 同一个理由，故扩展名全枚举、顺序照 MAGICS。
        return tuple((base, e) for e in _EXPORT_EXTS)
    raise ValueError(f"未知媒体类型: {kind}")


def _dest(out_root: pathlib.Path, cm: str, kind: str, mon: str,
          stem: str, ext: str) -> pathlib.Path:
    """导出终路径（`out_root/cm/kind/mon` 逐级 0700，含已存在的 out_root）。
    与 _save 共用：wxgf 转码要先拿路径才能直写。
    路径拼法取自 `_out_path`（探测侧的同源对应物）。"""
    ensure_tree(out_root, cm, kind, mon)
    return _out_path(out_root, cm, kind, mon, stem, ext)


def _save(out_root: pathlib.Path, cm: str, kind: str, mon: str,
          stem: str, ext: str, data: bytes) -> pathlib.Path | None:
    """写一个导出文件（0600）。落盘失败（磁盘满/权限/只读挂载）返回 None（Ruling 20）——
    调用方按自己的口径计数（图片 failed / 视频 missing / 语音 failed），裸 OSError 不逃出 core。"""
    p = _dest(out_root, cm, kind, mon, stem, ext)
    try:
        p.write_bytes(data)
        p.chmod(0o600)                         # 导出的是明文媒体：除了属主谁也读不到
    except OSError:
        try:
            p.unlink(missing_ok=True)          # 写了一半的产物比没有更坏：skip 判定会把它当成品
        except OSError:
            pass                               # 清理本身失败不该盖过「这次没写成功」
        return None
    return p


# 导出产物的扩展名白名单，**从 MAGICS 派生**而不是另抄一份——
# 本仓库已经有过 `_sniff_ext` 与 MAGICS 重复的账（终审 Minor），不再添第二份。
_EXPORT_EXTS: tuple[str, ...] = tuple(dict.fromkeys(ext for _, ext in MAGICS))


def _month_window(ts: int) -> tuple[str, str, str]:
    """与 resolve_image / resolve_video 同一窗口、同一顺序（当月 → 上月 → 下月）。

    导出端写的是**源目录**的月名（`mon = month_dir.name`），而源目录可能比消息 ts 的
    月份早或晚一个月 —— 这正是 resolve_* 那两个 ±1 回退存在的理由。
    """
    mon = month_of(ts)
    return (mon, _shift_month(mon, -1), _shift_month(mon, 1))


def exported_path(out_root: pathlib.Path, *, chat_md5: str, kind: str, ts: int,
                  file_key: str | None = None,
                  local_id: int | None = None) -> pathlib.Path | None:
    """已导出产物的路径；没导出过返回 None。**纯探测，不建目录、不写盘。**

    布局与 `_dest` 同源：`{out_root}/{chat_md5}/{kind}/{YYYY-MM}/{stem}.{ext}`，
    而**月份窗口与候选名都按写盘方的原样**枚举（导出从不改名：写盘用的就是源文件的
    stem），月份外层、候选内层，首个命中即返回：
    - image: 月份 `_month_window(ts)`；`file_key` + `_SUFFIX_PRIORITY`（与 `resolve_image` 同序），扩展名逐个试 `_EXPORT_EXTS`
    - video: 月份 `_month_window(ts)`；`{K}.mp4` > `{K}_raw.mp4`（真实语料里的变体）> `{K}_thumb.jpg`（无缝体时落的就是它）
    - voice: 月份恒 `month_of(ts)`（写盘用的就是它，与消息 ts 同源，**无 ±1**）；
      stem=f"{ts}_{local_id}"，.wav 优先（pilk 缺失时落的是 .slk）
    - sticker: 月份恒 `month_of(ts)`（**同 voice，无 ±1**：贴纸的源 `business/emoticon/`
      不按月分区，写入月份就是我们自己的 `month_of(ts)`，套窗口会让探测去相邻月找
      永不可能存在的文件）；stem=`file_key`（就是 md5），扩展名逐个试 `_EXPORT_EXTS`

    参数不全（image/sticker 无 file_key / voice 无 local_id）→ None。
    """
    if kind == "voice":
        if local_id is None:
            return None
        months = (month_of(ts),)          # 语音写盘用的就是 month_of(create_time)，无需 ±1
        cands = _out_candidates(kind, f"{ts}_{local_id}")
    elif kind == "image":
        if file_key is None:
            return None
        months = _month_window(ts)
        # 与 resolve_image 的后缀优先级一致：写盘用的是**源文件**的 stem（`_save(..., dat.stem, ...)`），
        # 而源文件可能是 K_hd.dat —— 只探裸 K 会漏掉真正存在的那份产物。
        cands = [c for suf in _SUFFIX_PRIORITY
                 for c in _out_candidates(kind, f"{file_key}{suf}")]
    elif kind == "video":
        if file_key is None:
            return None
        months = _month_window(ts)
        # 与 resolve_video / 视频导出分支一致：mp4 优先，_raw 变体次之，
        # 最后才是同一目录里的 _thumb.jpg（无缝体时导出写下的就是它）。
        cands = _out_candidates(kind, file_key)
    elif kind == "sticker":
        if file_key is None:                 # file_key 就是 md5，没有它无从定位
            return None
        # **月份只用 `month_of(ts)`，不用 `_month_window`**——见 `_month_window` 的注释：
        # 那个 ±1 回退是为「写盘月份取自按月分区的源目录」的 image/video 而设。
        # 贴纸的源不按月分区，写入月份就是我们自己的 `month_of(ts)`。
        # 套用窗口会让探测去相邻月找永不可能存在的文件。**voice 同此先例。**
        months = (month_of(ts),)
        cands = _out_candidates(kind, file_key)
    else:
        return None
    for mon in months:                     # 月份外层、候选内层 —— 与 resolve_* 的嵌套顺序一致
        for stem, ext in cands:
            p = _out_path(out_root, chat_md5, kind, mon, stem, ext)
            if p.exists():
                return p
    return None


def export_media(account: str, chat_ids: list[str], *, kinds: list[str],
                 months: list[str] | None = None, out_dir: pathlib.Path | None = None,
                 max_files: int = 2000, progress=None, force: bool = False,
                 con: sqlite3.Connection | None = None,
                 with_cdn: bool = False) -> MediaReport:
    """按会话导出媒体。图片解密（MediaKeyError → 能力级降级不抛）；语音见 Task 3；视频明文复制
    （refs 驱动：给 `con` 时按 media_refs 归属，任意会话数都成立；`con=None` 时退回账号级枚举，
    只有恰好一个会话时可归属）。

    **调用方必须传 `con`**：默认的 `None` 会静默选中账号级枚举那条老路——多会话下它一条都不归属，
    单会话下它会把账号级视频树里的全部文件都算到该会话头上（错归属，不只是缺归属）。生产入口
    `usecases.media_export` 自己开索引库并传进来；`None` 只留给不走索引的直接调用方（回归网）。

    `with_cdn`（默认 False）：贴纸**本机那份是加密的**（两轮 spike 均未破，spec 附录 C/E.4），
    故只有显式打开它才从微信 CDN 取件。**不开则贴纸一条都不导、一次网络请求都不发**
    （`SECURITY.md` 的零网络承诺）。
    """
    report = MediaReport()
    out_root = ensure_tree(pathlib.Path(out_dir) if out_dir else export_root())
    report.out_dir = str(out_root)
    # 一次 stat 检查（Ruling 15）：chmod 是尽力而为的（见 _chmod_quiet），收紧失败在这里
    # 如实上报——只报一次、带路径，不按文件刷屏。stat 也失败（目录被换掉/没权限）同样算
    # 没验证到，按未收紧处理。
    try:
        loose = stat.S_IMODE(out_root.stat().st_mode) != 0o700
    except OSError:
        loose = True
    if loose:
        report.degraded.append(f"输出目录权限未能收紧到 0700（明文媒体可能被他人读到）：{out_root}")
    want = set(months) if months else None
    produced = 0

    def already(cm: str, kind: str, mon: str, base: str) -> bool:
        """目标文件已在 → 本轮跳过。**在解密之前判**——省掉的正是真正的开销。

        候选名与读侧 `exported_path` 同源（见 `_out_candidates`）：判定比读侧严格就会
        出现「读侧说已导出、写侧说没有」的漂移。
        """
        if force:
            return False
        return any(_out_path(out_root, cm, kind, mon, s, e).exists()
                   for s, e in _out_candidates(kind, base))

    def full() -> bool:
        return produced >= max_files

    def bump() -> int:
        """计一个已落盘的媒体文件，返回累计数（Ruling 18：语音与图片/视频同一个口径——
        `max_files` 必须也管住语音，否则一个有几千条语音的会话会无视 `--limit`）。"""
        nonlocal produced
        produced += 1
        return produced

    if "image" in kinds:
        try:
            mk = derive_keys(account)
        except errors.MediaKeyError as exc:
            mk = None
            report.degraded.append(f"图片解密不可用：{exc}")
        if mk is not None:
            for chat in chat_ids:
                cm = messages.md5_of(chat)
                base = paths.XWECHAT_FILES / account / "msg/attach" / cm
                if not base.is_dir():
                    continue
                for month_dir in _safe_iter(base):
                    if not month_dir.is_dir():
                        continue
                    mon = month_dir.name
                    if want is not None and mon not in want:
                        continue
                    img_dir = month_dir / "Img"
                    if not img_dir.is_dir():
                        continue
                    wxgf_warned = False
                    for dat in _safe_iter(img_dir, "*.dat"):
                        if already(cm, "image", mon, dat.stem):
                            report.image.skipped += 1
                            continue
                        if full():
                            report.truncated = True
                            report.image.remaining += 1
                            continue
                        try:
                            buf = decrypt_dat(dat.read_bytes(), mk)
                        except (errors.DecryptError, OSError):
                            # 后半是 Ruling 9：文件在但读不了（权限/导出中途被微信重写）算
                            # failed（导出失败），裸 OSError 不得逃出 core 层。
                            report.image.failed += 1
                            continue
                        ext = detect_ext(buf)
                        if ext is None:
                            report.image.failed += 1
                            continue
                        if ext == "wxgf":
                            # 转码成功时 jpeg 已由 ffmpeg 直写 out_root 下的终路径
                            converted = _wxgf_to_jpeg(
                                buf, _dest(out_root, cm, "image", mon, dat.stem, "jpg"))
                        else:
                            converted = False
                        if not converted:
                            if ext == "wxgf" and not wxgf_warned:
                                report.degraded.append(
                                    "wxgf 图未转 JPEG（无 ffmpeg 或转码失败），存原容器")
                                wxgf_warned = True
                            if _save(out_root, cm, "image", mon, dat.stem, ext, buf) is None:
                                report.image.failed += 1     # 落盘失败（Ruling 20）
                                continue
                        report.image.ok += 1
                        produced += 1
                        if progress:
                            progress(produced)
    if "video" in kinds:
        # 两条路（spec §4.7）：
        # - 给了索引连接（`con`）→ **refs 驱动**：按每个会话的 video 消息取其 file_key，再用
        #   resolve_video 逐个定位本体。视频树是账号级的（`msg/video/{月}`，没有会话维度），
        #   只有经索引才知道某条视频属于哪个会话——多会话归属由此成立，产物落
        #   `out_root/{chat_md5}/video/{月}/`。
        # - 没给（`con=None`）→ 退回**账号级枚举**（下面的实现，本任务之前唯一的路）：整个调用
        #   只枚举一次，恰好一个会话时才归属；多会话无从归属——按会话循环会把同一批文件重复
        #   写进 N 个目录、计数翻 N 倍。用例层只在**索引打不开**时走到这里（并带一条 degraded
        #   公告）；tests/test_media.py 的 15 处直接调用是这条路的回归网。
        if con is not None:
            refs_by_chat: dict[str, list[tuple[int, str]]] = {}
            note: str | None = None
            try:
                refs_by_chat = store.media_refs_by_chat(con, account, chat_ids, kind="video")
                if not refs_by_chat:
                    # 「一条都没导出」有三种成因，修法各不相同（spec §4.7），必须分开说：
                    # 计数一律走**不 join messages** 的 count_media_refs——`media rebuild-refs`
                    # 只写 refs 不写 messages，join 出来的空会把后两种混成一种，还会把用户
                    # 导向错误的修法。
                    if store.count_media_refs(con, account, kind="video") == 0:
                        # 情形 1：该账号一条视频引用都没有（索引建在 media_refs 之前，或建时本机没有视频）
                        note = ("视频导出按 media_refs 索引归属：索引里这个账号一条视频引用都没有"
                                "（索引建立在 media_refs 之前，或建索引时本机没有视频消息），"
                                "本次未导出任何视频。请先运行 `media rebuild-refs` 补齐引用。")
                    elif store.count_media_refs(con, account, chat_ids=chat_ids, kind="video"):
                        # 情形 3：这些会话**有**引用，但 join 不出消息——它们的消息没进索引。
                        #    rebuild-refs 写不出消息，唯一有效的动作是导出这些会话的历史
                        #    （导出管道同时写 messages 与 refs）。
                        note = ("视频导出按 media_refs 索引归属：本次请求的会话有视频引用，"
                                "但它们的消息不在索引里，本次未导出任何视频。"
                                "请先导出这些会话的历史（`export_full_history` / CLI `export`）"
                                "——它同时写入消息与媒体引用。")
                    else:
                        # 情形 2：引用在别处——这些会话确实没有视频引用（索引本身可用，无需修复）
                        note = ("视频导出按 media_refs 索引归属：本次请求的会话在索引里没有视频引用，"
                                "本次未导出任何视频（该账号其它会话有引用，说明索引可用）。")
            except (sqlite3.Error, OSError) as exc:
                # 索引读不了 = 归属无从谈起：**如实降级**产出 0 条。裸 sqlite3.Error 逃出 core
                # 层会击穿错误契约，而静默返回 0 会把「索引坏了」说成「本机没有视频」。
                note = (f"视频索引读不了（{errors.redact(str(exc))}），本次未导出任何视频"
                        "（视频按 media_refs 索引归属）")
            if note:
                report.degraded.append(note)
            for chat in chat_ids:
                cm = messages.md5_of(chat)
                for _msg_key, ts, file_key in refs_by_chat.get(chat, ()):
                    if want is not None and month_of(ts) not in want:
                        continue                     # 月份外的条目本来就不导出，不算「因上限收手」
                    if already(cm, "video", month_of(ts), file_key):
                        report.video.skipped += 1
                        continue
                    # 上限检查排在月份过滤之后（与语音同一个口径）：只有**本会导出**的条目
                    # 被上限挡下，truncated 才成立；反过来的话 `--limit` 恰好用尽时会把一条
                    # 本来就跳过的条目当成「已提前收手」，那是假话。
                    if full():
                        report.truncated = True
                        report.video.remaining += 1
                        continue
                    got = resolve_video(account, ts, file_key)
                    if got is None:
                        report.video.missing += 1        # 有 ref 但本体不在磁盘（被清理）
                        continue
                    src, is_thumb = got
                    try:
                        buf = src.read_bytes()
                    except OSError:
                        report.video.missing += 1        # 文件在、拿不到本体（Ruling 9）
                        continue
                    # 名字逐字取自**源文件**（`{K}.mp4` / `{K}_raw.mp4` / `{K}_thumb.jpg`），与
                    # 账号级枚举那条路一致：`media.exported_path` 正是按这三个名字探测的，改成
                    # `{K}.jpg` 会让它找不到缩略图，机器上既有的产物也会被误报成「未导出」。
                    if _save(out_root, cm, "video", month_of(ts),
                             src.stem, src.suffix[1:], buf) is None:
                        report.video.missing += 1        # 落盘失败 → 同归口（Ruling 20）
                        continue
                    if is_thumb:
                        report.video.thumb += 1
                    else:
                        report.video.ok += 1
                    done = bump()
                    if progress:
                        progress(done)
        # 两处的 `con is None` 不可省：refs 路径已经处理过的调用不该再落进账号级枚举。
        if con is None and len(chat_ids) == 1:
            cm = messages.md5_of(chat_ids[0])
            vbase = paths.XWECHAT_FILES / account / "msg/video"
            # 不在入口判 full()：此刻只是「上限已到」，还没看见任何条目——真要有视频被跳过，
            # 下面两个循环里的逐项检查会如实置位 truncated（此处置位会把「一个视频都没剩」
            # 也说成「已提前收手」）。
            if vbase.is_dir():
                for month_dir in _safe_iter(vbase):
                    if not month_dir.is_dir():
                        continue
                    mon = month_dir.name
                    if want is not None and mon not in want:
                        continue
                    # 本轮的「本体有着落」集合：不只是**写进盘的**，已存在的、被上限挡下的也算。
                    # 缩略图的唯一意义是补没有本体的视频；本体有着落还写一份只是冗余文件、白吃
                    # `--limit`。被上限挡下的必须一并记：它的本体已计进 remaining，这里不记的话
                    # 同一视频的缩略图会再计一次，remaining 就比「真正还差的文件数」多报。
                    # 缺失（读不到 / 写不出）**不记**：那时缩略图是唯一的退路，必须照写。
                    mp4_stems = set()
                    for f in _safe_iter(month_dir, "*.mp4"):
                        if already(cm, "video", mon, f.stem):
                            report.video.skipped += 1
                            mp4_stems.add(f.stem.removesuffix("_raw"))
                            continue
                        if full():
                            report.truncated = True
                            report.video.remaining += 1
                            mp4_stems.add(f.stem.removesuffix("_raw"))
                            continue
                        try:
                            buf = f.read_bytes()
                        except OSError:
                            report.video.missing += 1    # 文件在、拿不到本体（Ruling 9）
                            continue
                        if _save(out_root, cm, "video", mon, f.stem, "mp4", buf) is None:
                            report.video.missing += 1    # 落盘失败 → 与读不到本体同归口（Ruling 20）
                            continue
                        mp4_stems.add(f.stem.removesuffix("_raw"))
                        report.video.ok += 1
                        produced += 1
                        if progress:
                            progress(produced)
                    for f in _safe_iter(month_dir, "*_thumb.jpg"):
                        if f.stem.removesuffix("_thumb") in mp4_stems:
                            continue               # 有本体的不再存缩略图
                        # 基准名必须是**裸 file_key**：`_out_candidates` 的候选集是围绕它
                        # 设计的（`{K}.mp4` / `{K}_raw.mp4` / `{K}_thumb.jpg`）。传 `{K}_thumb`
                        # 会得到 `{K}_thumb_thumb.jpg` 这种永不存在的名字 —— 本行于是永不命中，
                        # 缩略图每轮重写并吃 `--limit`（Task 3 落地后还会让 remaining 恒 >0）。
                        if already(cm, "video", mon, f.stem.removesuffix("_thumb")):
                            report.video.skipped += 1
                            continue
                        # 顺序有意义：先判「本来就不导出」再判上限。反过来的话，`--limit`
                        # 恰好用尽时会把一条**本来就跳过**的缩略图当成「因上限跳过」，
                        # truncated 就成了假话。
                        if full():
                            report.truncated = True
                            report.video.remaining += 1
                            continue
                        try:
                            buf = f.read_bytes()
                        except OSError:
                            report.video.missing += 1    # 缩略图也拿不到：同样的归口
                            continue
                        if _save(out_root, cm, "video", mon, f.stem, "jpg", buf) is None:
                            report.video.missing += 1    # 同上：缩略图也 write 不出来
                            continue
                        report.video.thumb += 1
                        produced += 1
        elif con is None and len(chat_ids) > 1:
            report.degraded.append("视频为账号级枚举，无法归属具体会话；请逐个会话导出")
    if "voice" in kinds:
        if _PILK is None:
            report.degraded.append("pilk 不可用：语音导出为原始 .slk（`uv add pilk` 后可转 WAV）")
        _export_voices(account, chat_ids, out_root, report, want, full, bump, progress,
                       already)
    if "sticker" in kinds:
        _export_stickers(account, chat_ids, out_root, report, want, full, bump, progress,
                         already, con, with_cdn=with_cdn)
    return report


def _wav_bytes(pcm: bytes, rate: int = 24000) -> bytes:
    """44 字节 RIFF 头 + 16-bit 单声道 PCM（Ruling 19：自己封头，不用 `silk_to_wav`——
    那个会在系统 TMPDIR 里留一份从不清理的中间明文 .pcm）。"""
    return (b"RIFF" + (36 + len(pcm)).to_bytes(4, "little") + b"WAVE"
            + b"fmt " + (16).to_bytes(4, "little")
            + (1).to_bytes(2, "little")                # PCM
            + (1).to_bytes(2, "little")                # 单声道
            + rate.to_bytes(4, "little")               # 24000
            + (rate * 2).to_bytes(4, "little")         # byte_rate = 48000
            + (2).to_bytes(2, "little")                # block_align = 2
            + (16).to_bytes(2, "little")               # bits_per_sample
            + b"data" + len(pcm).to_bytes(4, "little") + pcm)


def _voice_db_paths(account: str) -> list[pathlib.Path]:
    """语音库分片（`db_storage/message/media_*.db`）。走 `_safe_iter`：目录读不了 = 没有
    分片可导，不能让惰性 glob 的 OSError 逃出 core 层（同 Ruling 12/16 的规矩）。"""
    return _safe_iter(paths.db_storage(account) / "message", "media_*.db")


def voice_body_keys(account: str) -> frozenset[tuple[str, int]] | None:
    """本机**有本体**的语音消息集合：{(chat_md5, local_id)}。

    判定依据是 VoiceInfo 行的存在 + voice_data 非空（实测 7,390 行 voice_data 全部非空）。
    本函数**不解码 SILK**，只读索引列，所以很轻。

    三个返回值的语义必须分清：
    - `frozenset()`：确定没有语音本体
    - 非空集合：这些 (chat, local_id) 有本体
    - `None`：**语音库读不到**（分片损坏/密钥缺失/HOME 建不出来）——调用方必须降级，
      不能把 None 当成空集合，否则「读不到」会被谎报成「本机没有」。

    与 `_export_voices_from` 同样只读 `media_*.db`；不碰微信的其它目录。
    """
    try:
        passphrase = keys_mod.get_passphrase(account)
    except errors.AgentWeChatError:
        return None
    out: set[tuple[str, int]] = set()
    for db in _voice_db_paths(account):
        try:
            with decrypt.decrypted(db, passphrase) as plain:
                con = sqlite3.connect(f"file:{plain}?mode=ro", uri=True)
                con.text_factory = bytes
                try:
                    n2i = {rid: (u.decode("utf-8", "replace")
                                 if isinstance(u, bytes) else u)
                           for rid, u in con.execute("SELECT rowid, user_name FROM Name2Id")}
                    for cid, lid in con.execute(
                            "SELECT chat_name_id, local_id FROM VoiceInfo "
                            "WHERE voice_data IS NOT NULL"):
                        u = n2i.get(cid)
                        if u:
                            out.add((messages.md5_of(u), lid))
                finally:
                    con.close()
        except (errors.AgentWeChatError, sqlite3.Error, OSError):
            # 与 _export_voices 同一口径：任一分片打不开 → 整体判为「读不到」，
            # 不做部分可信（半个集合会被上层当成完整事实）。
            return None
    return frozenset(out)


# ---------- 贴纸 CDN 取件（**本项目唯一为贴纸联网的路径**） ----------

# 贴纸 `cdnurl` 允许的主机。**按后缀收口**，不逐个列举 host——实测 3,683 个 URL 的分布是
# `vweixinf.tc.qq.com` 71% / `wxapp.tc.qq.com` 27% / `mmbiz.qpic.cn` 1.0% /
# `snsvideo.c2c.wechat.com` 0.1%（另有 0.1% 解析不出 host，一律拒）。
#
# **为什么这三个后缀可以放行**：`tc.qq.com` / `qpic.cn` / `wechat.com` 都是腾讯自有的
# CDN 域。本白名单防的是「把请求（带着用户的 IP 与 filekey）发到**攻击者控制**的主机」——
# 而贴纸 XML 是**发送方可影响**的（`cdnurl` 由对方的客户端写入），所以这道门必须有。
# 放行腾讯自有域**不增加攻击者的可达面**：最坏情况是请求打到腾讯的另一个子域，且带的是
# **攻击者自己的** filekey，没有任何用户数据外流。
#
# `qpic.cn` 与 `wxapp.tc.qq.com` 在 `sns_media` 的白名单里早有先例（朋友圈那条路）。
_CDN_ALLOWED = (".tc.qq.com", ".qpic.cn", ".wechat.com")


class _AllowlistedRedirect(urllib.request.HTTPRedirectHandler):
    """重定向目标必须**也在白名单内**——白名单只对第一跳成立等于没有白名单。

    与 `sns_media._AllowlistedRedirect` 同一手法，但**判据不同**，故**不能复用**。两个障碍：
    ① 贴纸 URL 的 scheme 是 **http**，被 `https_allowed` 的 scheme 那半条挡死；
    ② `vweixinf.tc.qq.com` 不在其白名单里（`wxapp.tc.qq.com` 在）。
    **不是「域不对」**——那是把理由说错了。本处的白名单只判 host 后缀、**不管 scheme**；
    scheme 的限制是既知代价（spec R10）：实测 `vweixinf.tc.qq.com` 的证书对主机名无效，
    而另一个 host 的 https 未测，故当前一律用 XML 给的 http。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlsplit(newurl).hostname or ""
        if not host.endswith(_CDN_ALLOWED):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _cdn_opener() -> urllib.request.OpenerDirector:
    """强制直连的 opener。**不要「简化」成 `urllib.request.urlopen`。**

    本机 macOS 系统代理开着（`scutil --proxy` → 127.0.0.1:7897），Python 的 `getproxies()`
    在 macOS 上会读它 ⇒ 裸 urlopen 全部超时。**2026-09-22 那次「CDN 实测不通」的假结论
    就是这么来的**（spec 附录 E.3）。机制与理由见 `sns_media._default_opener` 的注释。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                       _AllowlistedRedirect())


def sticker_cdn_urls(account: str, items: Sequence[tuple[str, str, int]]
                     ) -> dict[tuple[str, str, int], tuple[str, str]]:
    """批量回源：`{(chat_md5, shard, local_id): (url, expect_md5)}`，取不到的键不出现。

    **为什么必须批量**：回源要解密分片，而 `reader._open` 走的是 `decrypt.decrypted`
    ——那是一次**完整分片**的 sqlcipher_export。本机 `message_4.db` 有 **235 MB**，
    逐条回源 = 每条贴纸解密一整个分片：实测 120 条跑过 120 秒仍未完成，导出几十条就不可用。
    **按分片分组、每个分片只解一次**是这条路径可用的前提，不是优化。

    **凭据只在本函数的栈帧里存在**：`cdnurl`（`filekey` 在其查询串里）取出来交给返回值，
    不落盘、不进日志、不进信封（贴纸 XML 不落库是结构性根治，spec §7.1）。
    """
    from .reader import MessageReader, _decode_content      # 惰性：避开 reader↔media 的环
    out: dict[tuple[str, str, int], tuple[str, str]] = {}
    # 按分片分组：同一个 (shard) 只开一次
    by_shard: dict[str, list[tuple[str, int]]] = {}
    for chat_md5, shard, local_id in items:
        by_shard.setdefault(shard, []).append((chat_md5, local_id))
    for shard, wanted in by_shard.items():
        try:
            r = MessageReader(account)
            with r._open(f"message/{shard}") as con:
                rows: dict[tuple[str, int], str | None] = {}
                for chat_md5, local_id in wanted:
                    row = con.execute(
                        f"SELECT message_content, WCDB_CT_message_content FROM 'Msg_{chat_md5}' "
                        f"WHERE local_id = ?", (local_id,)).fetchone()
                    rows[(chat_md5, local_id)] = _decode_content(*row) if row else None
        except Exception:                    # noqa: BLE001 —— 见 fetch_sticker_from_cdn 的说明
            continue                         # 该分片整体取不到：这些键不出现在返回值里
        for (chat_md5, local_id), xml in rows.items():
            if not xml:
                continue
            # **必须与 `extract_sticker_ref` 用同一套正则**（`_emoji_attr_re`）：属性的空白
            # 形态因消息而异，写死 `attr="` 会漏掉 48%（实测：cdnurl 严格 163/400、宽松 356/400）。
            m, h = _emoji_attr_re("cdnurl").search(xml), _STICKER_MD5.search(xml)
            if not (m and h):
                continue                     # 没凭据 → 这个键不出现 → **一次请求都不发**
            out[(chat_md5, shard, local_id)] = (m.group(1).replace("&amp;", "&"),
                                                h.group(1).lower())
    return out


def _fetch_cdn_bytes(url: str, expect_md5: str, *, timeout: float = 15.0) -> bytes | None:
    """取一次并校验，返回**明文图片**字节；任何失败返回 None，**不抛**。

    `expect_md5` 是 XML 的 `md5` 属性——实测它恒等于**明文对象**的 md5（spec 附录 E.1），
    故这是一次零成本的完整性校验：不符即失败、**绝不落盘**。它同时覆盖「下载内容不对」
    与「服务端换了内容」两种失败。**这也意味着 md5 缺失时不会下载**（spec §2.2 记其覆盖 100%）。
    """
    try:
        # **畸形 URL 会让 `urlsplit` 抛 `ValueError`**——放进同一道网里，别让它逃出 core。
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return None
    if not host.endswith(_CDN_ALLOWED):
        return None                      # 白名单外：一次请求都不发（跨跳由 handler 复查）
    try:
        with _cdn_opener().open(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            body = resp.read()
    except Exception:                    # noqa: BLE001 —— 见下
        # **必须宽到 `Exception`**：`http.client.HTTPException`（`BadStatusLine` 等）与
        # `ssl.SSLError` 不是 `OSError` 的子类，按类型列举会漏；漏出去的异常违反
        # 「core 层只抛项目异常」的契约（`sns_media.fetch` 对网络调用同样是宽接）。
        # **不回显任何内容**：错误消息里可能带 `filekey`，连类型名都不带出去。
        return None
    return body if hashlib.md5(body).hexdigest() == expect_md5 else None


def fetch_sticker_from_cdn(account: str, chat_md5: str, shard: str, local_id: int,
                           *, timeout: float = 15.0) -> bytes | None:
    """从 CDN 取**一张**贴纸的明文图片字节。取不到返回 None，**不抛**。

    **唯一会联网的贴纸路径，只在调用方显式 opt-in 时被调用。**

    **为什么回源而不是读索引**：贴纸 XML 不落库是凭据问题的结构性根治（spec §7.1）。
    所以 `cdnurl`（`filekey` 就在它的查询串里）只能在**用的这一刻**从微信库里现取，
    凭据随本函数的栈帧消失——不落盘、不进日志、不进信封。**不要改成从索引读。**

    **走 `cdnurl`，不走 `encrypturl`**：实测 `cdnurl` 返回**明文**图片（spec 附录 E.1），
    故本函数**不碰 `aeskey`**——凭据面因此只有 URL 里那一个 `filekey`。
    （`encrypturl` 是 AES-128-CBC、`key = IV = aeskey` 的那一份，本设计不用它。）

    **当前只能明文 http**（spec R10）：实测 `vweixinf.tc.qq.com` 的证书对主机名无效。
    外发的是 `filekey`——**这是既知且被显式接受的代价，不许在文档或日志里说成加密传输**。
    （**只测过那一个主机名**，别写成「贴纸域一律不支持 https」。）

    **批量场景请用 `sticker_cdn_urls` + `_fetch_cdn_bytes`**：本函数是单条便捷包装，
    每条都会重新解密一次分片（见 `sticker_cdn_urls` 的性能注记）。
    """
    loc = (chat_md5, shard, local_id)
    urls = sticker_cdn_urls(account, [loc])
    if loc not in urls:
        return None
    url, expect_md5 = urls[loc]
    return _fetch_cdn_bytes(url, expect_md5, timeout=timeout)


def _export_stickers(account: str, chat_ids: list[str], out_root: pathlib.Path,
                     report: MediaReport, want: set[str] | None, is_full, bump,
                     progress, already, con: sqlite3.Connection | None,
                     *, with_cdn: bool = False) -> None:
    """贴纸：按 `media_refs` 归属 → **从 CDN 取明文图** → 落盘。

    **字节来源只有 CDN**：本机那份（`business/emoticon/`）是加密的，两轮 spike 均未破
    （spec 附录 C / E.4），且实测是**缩略档**（16KB vs 原图 67KB）⇒ 本机支线不建。
    `resolve_sticker` 只用来判「本机有没有那份加密副本」，供 `degraded` 文案说得准。

    **归属靠 `media_refs`，不靠枚举**（与视频同一条理由）：贴纸文件本身没有会话维度，
    账号级的 `business/emoticon/` 枚举会把别人的贴纸算到本次请求的会话头上。
    `con=None` 时**一条都不导**并如实降级——错归属比缺归属更坏。

    **三趟结构（性能要求，不是风格）**：① 摊平全部待办（归属、月份过滤、跳过判定都在
    这一趟做完）→ ② 按分片**一次性**回源（见 `sticker_cdn_urls`：逐条回源 = 逐条解密
    整个 235 MB 分片，实测逐条 120 条 >120 秒未完成、批量后 150 条 11.8 秒）
    → ③ 逐条取件 + 落盘。
    """
    if con is None:
        report.degraded.append("贴纸导出需要索引库（归属按 media_refs 判定）：本次未导出任何贴纸")
        return
    refs: dict[str, list[tuple[str, int, str]]] = {}
    note: str | None = None
    try:
        refs = store.media_refs_by_chat(con, account, chat_ids, kind="sticker")
        if not refs:
            # 与视频同款三分解（spec §4.7）：计数值走**不 join messages** 的 count_media_refs，
            # 否则「refs 在但消息没进索引」会被混成「一条都没有」，把用户导向错误的修法。
            if store.count_media_refs(con, account, kind="sticker") == 0:
                note = ("贴纸导出按 media_refs 索引归属：索引里这个账号一条贴纸引用都没有"
                        "（索引建立在 media_refs 之前）。请先运行 `media rebuild-refs` 补齐引用。")
            elif store.count_media_refs(con, account, chat_ids=chat_ids, kind="sticker"):
                note = ("贴纸导出按 media_refs 索引归属：本次请求的会话有贴纸引用，"
                        "但它们的消息不在索引里，本次未导出任何贴纸。"
                        "请先导出这些会话的历史（它同时写入消息与媒体引用）。")
            else:
                note = ("贴纸导出按 media_refs 索引归属：本次请求的会话在索引里没有贴纸引用，"
                        "本次未导出任何贴纸（该账号其它会话有引用，说明索引可用）。")
    except (sqlite3.Error, OSError) as exc:
        note = (f"贴纸索引读不了（{errors.redact(str(exc))}），本次未导出任何贴纸"
                "（贴纸按 media_refs 索引归属）")
    if note:
        report.degraded.append(note)
        return

    # ── 第一趟：按 `(cm, 月, md5)` **去重排队**，并保留该 md5 的**全部候选消息** ──
    #
    # **为什么去重**：产物按 md5 内容寻址，同一条贴纸被发 5 次只该取一次件。实测某会话
    # 265 条贴纸只有 **109 个不同 md5（重复 58%）**——不去重就白发 156 次请求，而这条路径的
    # 设计约束恰恰是「少发请求」（默认关、逐条可数）。**与 `already()` 是两回事**：那个查的是
    # 磁盘上已有的产物，管不了同一次运行内的重复。
    #
    # **为什么保留全部候选**：同一个 md5 的不同消息，XML 形态可能不同——实测有 7 个贴纸的
    # **首条消息没有 `cdnurl`**（商店表情包引用形态）而同 md5 的**另一条有**。只认首条会把
    # 可得件数从 104 压到 97。候选一起交给批量回源是**近乎零成本**的：分片已经解密，多的只是
    # 几次行查询。
    # **候选池按 `(会话, md5)` 收，与月份无关**：`cdnurl` 是按**贴纸**（md5）走的，
    # 与它哪个月发过没有关系。若只收同月的消息，某个贴纸「本月那条没有凭据、别月那条有」
    # 就会白白失败——实测某会话有 5 个正是这样（那 5 个的响应体与 md5 都完全正常）。
    all_keys: dict[tuple[str, str], list[str]] = {}
    for chat in chat_ids:
        cm = messages.md5_of(chat)
        for msg_key, _ts, file_key in refs.get(chat, ()):
            if msg_key.count("|") == 3:
                all_keys.setdefault((cm, file_key), []).append(msg_key)

    order: list[tuple[str, str, str]] = []                  # (cm, mon, md5)，保序
    cands: dict[tuple[str, str, str], list[str]] = {}       # -> 候选 msg_key（跨月）
    for chat in chat_ids:
        cm = messages.md5_of(chat)
        for msg_key, ts, file_key in refs.get(chat, ()):
            mon = month_of(ts)               # 与 exported_path 同源，见下方注释
            if want is not None and mon not in want:
                continue
            if already(cm, "sticker", mon, file_key):
                report.sticker.skipped += 1
                continue
            if msg_key.count("|") != 3:
                report.sticker.failed += 1   # msg_key 形状不对：定位不了那一行
                continue
            key = (cm, mon, file_key)
            if key in cands:
                report.sticker.skipped += 1  # 本轮已排过：同一个贴纸，落的是同一个文件
                continue
            cands[key] = all_keys[(cm, file_key)]
            order.append(key)
    if not order:
        return

    def _locs(keys: list[str]) -> list[tuple[str, str, int]]:
        out = []
        for k in keys:
            _a, c, sh, l = k.split("|")
            out.append((c, sh, int(l)))
        return out

    # 去重：候选跨月后同一个 msg_key 会出现在多个月份键里，重复的行查询是白费。
    all_locs = list(dict.fromkeys(loc for key in order for loc in _locs(cands[key])))

    if not with_cdn:
        # **默认零网络**：本机没有可用的明文来源，故如实计 missing。
        # 这里**不置 truncated / remaining**——不开开关时重跑同样导不出，
        # 把它们算进 remaining 会说「重跑即可继续」，那是假话。
        local_encrypted = sum(1 for key in order
                              if resolve_sticker(account, key[2]) is not None)
        report.sticker.missing += len(order)
        report.degraded.append(
            f"{len(order)} 个贴纸未导出：微信把贴纸存成加密格式、解密方案未破，"
            "本机那份是缩略档且取不出明文；加 `--with-cdn` 才从 CDN 取件（默认不联网）"
            + (f"（其中 {local_encrypted} 个本机有加密副本）" if local_encrypted else ""))
        return

    # ── 第二趟：按分片**一次性**回源（候选消息一并交出去） ──
    urls = sticker_cdn_urls(account, all_locs)

    # ── 第三趟：每个不同贴纸取一次件 + 落盘 ──
    wxgf_warned = False          # once-flag：一次导出只说一次（与图片分支同款）
    for cm, mon, md5 in order:
        if is_full():
            report.truncated = True
            report.sticker.remaining += 1
            continue
        got = next((urls[loc] for loc in _locs(cands[(cm, mon, md5)]) if loc in urls), None)
        if got is None:
            report.sticker.failed += 1       # 全部候选都拿不到（行不在分片里 / XML 无凭据）
            continue
        data = _fetch_cdn_bytes(got[0], got[1])
        if data is None:
            report.sticker.failed += 1       # 联网了但没取到，**如实计失败**
            continue
        ext = detect_ext(data)
        if ext is None:
            # 认不出格式就当失败：乱码落盘比失败更坏（会被 already() 的跳过判定永久固化）。
            report.sticker.failed += 1
            continue
        # **wxgf 是容器、不是图片，直接落盘用户打不开**——与图片分支同一手法：
        # 有 ffmpeg 就转 JPEG（直写终路径，不留半成品），没有/转码失败则退回存原容器**并公告**。
        # （这条分支在贴纸侧此前是漏的：全库实测 0 个 wxgf 贴纸，所以从未被走到——
        #   一旦遇到，产物会是 `file` 认不出、用户打不开的 .wxgf 而不报任何异常。）
        converted = False
        if ext == "wxgf":
            converted = _wxgf_to_jpeg(data, _dest(out_root, cm, "sticker", mon, md5, "jpg"))
        if not converted:
            if ext == "wxgf" and not wxgf_warned:
                report.degraded.append("wxgf 贴纸未转 JPEG（无 ffmpeg 或转码失败），存原容器")
                wxgf_warned = True
            if _save(out_root, cm, "sticker", mon, md5, ext, data) is None:
                report.sticker.failed += 1
                continue
        report.sticker.ok += 1
        done = bump()
        if progress:
            progress(done)


def _export_voices(account, chat_ids, out_root, report, want, is_full, bump, progress,
                   already) -> None:
    """语音：Name2Id rowid → VoiceInfo(chat_name_id, create_time) → pilk 解码 24kHz WAV。
    pilk 缺失 → 原始 .slk（微信原始形态，`\\x02` 前缀保留）。单库打不开则跳过该分片、
    在 report.degraded 里公告一次，不中断。

    每个分片都对**全部**请求会话查一遍 Name2Id（不在本分片里的自然跳过）——实测 media_0 与
    media_1 的 Name2Id 交集 13 人、其中 12 个会话**两分片都有语音**，所以「查到就把会话从待办
    里划掉」会在第一个分片后丢掉同一会话在其余分片的语音（Ruling 22）。
    """
    decrypt.ensure_home_dirs()           # 中间明文只落 HOME/tmp（0700、已排除 Time Machine）
    warned = False                       # 一次导出只说一次（wxgf_warned 同款 once-flag）
    for db in _voice_db_paths(account):
        try:
            passphrase = keys_mod.get_passphrase(account)
            with decrypt.decrypted(db, passphrase) as plain:
                con = sqlite3.connect(f"file:{plain}?mode=ro", uri=True)
                con.text_factory = bytes
                try:
                    _export_voices_from(con, chat_ids, out_root, report,
                                        want, is_full, bump, progress, already)
                finally:
                    con.close()
        except (errors.AgentWeChatError, sqlite3.Error, OSError) as exc:
            # 这个库打不开（漂移/密钥缺失/库读不了/HOME 的 tmp 建不出来）：跳过该分片，不中断。
            # OSError 那一支是 Ruling 23a：decrypt.decrypted 里 mkdtemp 与 snapshot 的
            # copy2/read_bytes 都直接抛 OSError（不包装），漏了它就带 traceback 冲出 core。
            # 但**跳过必须可见**：会话的语音全在这种分片里时 voice 计数为 0，没有这条说明
            # 就与「这个会话没有语音消息」不可区分（本功能其它能力级失败都会公告）。
            # 文案经 redact：DecryptError 可能带回 sqlcipher 回显的 `PRAGMA key` 原文。
            if not warned:
                warned = True
                report.degraded.append(
                    f"语音分片打不开（首个原因：{errors.redact(str(exc))}），该分片的语音未导出")
            continue


def _export_voices_from(con, chat_ids, out_root, report, want, is_full, bump,
                        progress, already) -> None:
    name2id = {u.decode("utf-8", "replace") if isinstance(u, bytes) else u: rid
               for rid, u in con.execute("SELECT rowid, user_name FROM Name2Id")}
    for chat in chat_ids:
        rid = name2id.get(chat)
        if rid is None:
            continue                     # 本分片没有它：别的分片可能还有（Ruling 22）
        cm = messages.md5_of(chat)
        for ts, lid, blob in con.execute(
                "SELECT create_time, local_id, voice_data FROM VoiceInfo "
                "WHERE chat_name_id=? ORDER BY create_time, local_id", (rid,)):
            mon = month_of(ts)
            if want is not None and mon not in want:
                continue                     # 月份外的条目本来就不导出，不算「因上限收手」
            if already(cm, "voice", mon, f"{ts}_{lid}"):
                report.voice.skipped += 1
                continue
            # 上限检查排在这两条之后：只有**本会导出**的条目被上限挡下，truncated 才成立。
            if is_full():
                report.truncated = True
                report.voice.remaining += 1
                continue
            if blob is None:
                report.voice.failed += 1
                continue
            wav_bytes = None
            if _PILK is not None:
                try:
                    # 输入 silk 与中间 pcm **都**只落在 HOME/tmp 的 0700 临时目录里，退出即清
                    # （Ruling 19：silk_to_wav 会把中间明文 pcm 留在系统 TMPDIR 且不删）
                    with tempfile.TemporaryDirectory(dir=decrypt.tmp_dir(),
                                                     prefix="awvoice-") as td:
                        tmp_silk = pathlib.Path(td) / "v.silk"
                        tmp_silk.write_bytes(blob)
                        tmp_pcm = pathlib.Path(td) / "v.pcm"
                        _PILK.decode(str(tmp_silk), str(tmp_pcm), 24000)
                        if tmp_pcm.exists():
                            wav_bytes = _wav_bytes(tmp_pcm.read_bytes())
                except Exception:
                    wav_bytes = None  # 解码失败 → 落到 .slk 回退
            if wav_bytes is not None:
                if _save(out_root, cm, "voice", mon, f"{ts}_{lid}", "wav", wav_bytes) is None:
                    report.voice.failed += 1         # 落盘失败（Ruling 20）
                    continue
                report.voice.ok += 1
            else:
                if _save(out_root, cm, "voice", mon, f"{ts}_{lid}", "slk", blob) is None:
                    report.voice.failed += 1
                    continue
                report.voice.raw_only += 1
            done = bump()
            if progress:
                progress(done)
