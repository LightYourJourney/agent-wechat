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

import datetime
import hashlib
import pathlib
import re
import sqlite3
import stat
import subprocess
import tempfile
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
    月份串须已是 `YYYY-MM`（调用方校验格式后），本函数不做校验。
    """
    out: list[tuple[int, int]] = []
    for m in months:
        y, mo = int(m[:4]), int(m[5:7])
        out.append((int(datetime.datetime(y, mo, 1).timestamp()),
                    int(datetime.datetime(y + mo // 12, mo % 12 + 1, 1).timestamp())))
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


# ---------- 导出 ----------

@dataclass
class Counts:
    ok: int = 0
    failed: int = 0
    missing: int = 0
    raw_only: int = 0      # voice 专属：pilk 缺失时导出原始 .slk 的数量
    thumb: int = 0         # video 专属：回退缩略图数量


@dataclass
class MediaReport:
    image: Counts = field(default_factory=Counts)
    voice: Counts = field(default_factory=Counts)
    video: Counts = field(default_factory=Counts)
    degraded: list[str] = field(default_factory=list)
    truncated: bool = False
    out_dir: str = ""      # 实际使用的输出根（export_media 赋值；CLI/MCP 信封从这取，不重算）

    def to_dict(self) -> dict:
        return {"image": {"ok": self.image.ok, "failed": self.image.failed,
                          "missing": self.image.missing},
                "voice": {"ok": self.voice.ok, "failed": self.voice.failed,
                          "raw_only": self.voice.raw_only},
                "video": {"ok": self.video.ok, "thumb": self.video.thumb,
                          "missing": self.video.missing},
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


def _dest(out_root: pathlib.Path, cm: str, kind: str, mon: str,
          stem: str, ext: str) -> pathlib.Path:
    """导出终路径（`out_root/cm/kind/mon` 逐级 0700，含已存在的 out_root）。
    与 _save 共用：wxgf 转码要先拿路径才能直写。"""
    d = ensure_tree(out_root, cm, kind, mon)
    return d / f"{stem}.{ext}"


def _save(out_root: pathlib.Path, cm: str, kind: str, mon: str,
          stem: str, ext: str, data: bytes) -> pathlib.Path | None:
    """写一个导出文件（0600）。落盘失败（磁盘满/权限/只读挂载）返回 None（Ruling 20）——
    调用方按自己的口径计数（图片 failed / 视频 missing / 语音 failed），裸 OSError 不逃出 core。"""
    p = _dest(out_root, cm, kind, mon, stem, ext)
    try:
        p.write_bytes(data)
        p.chmod(0o600)                         # 导出的是明文媒体：除了属主谁也读不到
    except OSError:
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

    参数不全（image 无 file_key / voice 无 local_id）→ None。
    """
    if kind == "voice":
        if local_id is None:
            return None
        months = (month_of(ts),)          # 语音写盘用的就是 month_of(create_time)，无需 ±1
        cands = [(f"{ts}_{local_id}", "wav"), (f"{ts}_{local_id}", "slk")]
    elif kind == "image":
        if file_key is None:
            return None
        months = _month_window(ts)
        # 与 resolve_image 的后缀优先级一致：写盘用的是**源文件**的 stem（`_save(..., dat.stem, ...)`），
        # 而源文件可能是 K_hd.dat —— 只探裸 K 会漏掉真正存在的那份产物。
        cands = [(f"{file_key}{suf}", ext)
                 for suf in _SUFFIX_PRIORITY for ext in _EXPORT_EXTS]
    elif kind == "video":
        if file_key is None:
            return None
        months = _month_window(ts)
        # 与 resolve_video / 视频导出分支一致：mp4 优先，_raw 变体次之，
        # 最后才是同一目录里的 _thumb.jpg（无缝体时导出写下的就是它）。
        cands = [(file_key, "mp4"), (f"{file_key}_raw", "mp4"),
                 (f"{file_key}_thumb", "jpg")]
    else:
        return None
    for mon in months:                     # 月份外层、候选内层 —— 与 resolve_* 的嵌套顺序一致
        d = out_root / chat_md5 / kind / mon
        for stem, ext in cands:
            p = d / f"{stem}.{ext}"
            if p.exists():
                return p
    return None


def export_media(account: str, chat_ids: list[str], *, kinds: list[str],
                 months: list[str] | None = None, out_dir: pathlib.Path | None = None,
                 max_files: int = 2000, progress=None,
                 con: sqlite3.Connection | None = None) -> MediaReport:
    """按会话导出媒体。图片解密（MediaKeyError → 能力级降级不抛）；语音见 Task 3；视频明文复制
    （refs 驱动：给 `con` 时按 media_refs 归属，任意会话数都成立；`con=None` 时退回账号级枚举，
    只有恰好一个会话时可归属）。

    **调用方必须传 `con`**：默认的 `None` 会静默选中账号级枚举那条老路——多会话下它一条都不归属，
    单会话下它会把账号级视频树里的全部文件都算到该会话头上（错归属，不只是缺归属）。生产入口
    `usecases.media_export` 自己开索引库并传进来；`None` 只留给不走索引的直接调用方（回归网）。
    """
    report = MediaReport()
    out_root = ensure_tree(pathlib.Path(out_dir) if out_dir else paths.HOME_DIR / "media")
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
                    if full():
                        break
                    mon = month_dir.name
                    if want is not None and mon not in want:
                        continue
                    img_dir = month_dir / "Img"
                    if not img_dir.is_dir():
                        continue
                    wxgf_warned = False
                    for dat in _safe_iter(img_dir, "*.dat"):
                        if full():
                            report.truncated = True
                            break
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
                for ts, file_key in refs_by_chat.get(chat, ()):
                    if want is not None and month_of(ts) not in want:
                        continue                     # 月份外的条目本来就不导出，不算「因上限收手」
                    # 上限检查排在月份过滤之后（与语音同一个口径）：只有**本会导出**的条目
                    # 被上限挡下，truncated 才成立；反过来的话 `--limit` 恰好用尽时会把一条
                    # 本来就跳过的条目当成「已提前收手」，那是假话。
                    if full():
                        report.truncated = True
                        break
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
                    mp4_stems = set()
                    for f in _safe_iter(month_dir, "*.mp4"):
                        if full():
                            report.truncated = True
                            break
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
                        # 顺序有意义：先判「本来就不导出」再判上限。反过来的话，`--limit`
                        # 恰好用尽时会把一条**本来就跳过**的缩略图当成「因上限跳过」，
                        # truncated 就成了假话。
                        if full():
                            report.truncated = True
                            break
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
        _export_voices(account, chat_ids, out_root, report, want, full, bump, progress)
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


def _export_voices(account, chat_ids, out_root, report, want, is_full, bump, progress) -> None:
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
                                        want, is_full, bump, progress)
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
                        progress) -> None:
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
            # 上限检查排在这两条之后：只有**本会导出**的条目被上限挡下，truncated 才成立。
            if is_full():
                report.truncated = True
                return
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
