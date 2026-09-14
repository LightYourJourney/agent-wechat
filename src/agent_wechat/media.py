"""媒体层：图片 dat 解密、语音 SILK、视频定位（spec: 2026-09-14-media-decrypt-design）。

算法事实全部来自本机实测（spec §2）与 WeFlow/WeFlowPP 的格式研究（Python 独立实现，
未复制任何上游代码）。密钥纯磁盘推导：kvcomm 文件名 code + md5(code+裸wxid)[:16]（ASCII）
+ dat 尾部统计 xorKey；**推导后必须用样本验证，不验证不交付**。

红线：code/aesKey/xorKey 是推导材料，视同密钥——不进日志、错误消息、repr。
依赖方向：本模块不 import store/reader（reader 可以 import 本模块）。
"""
from __future__ import annotations

import datetime
import hashlib
import pathlib
import re
import subprocess
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import errors, messages, paths

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


def _attach_dats(account: str):
    return (paths.XWECHAT_FILES / account / "msg/attach").glob("*/*/Img/*.dat")


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
    if r is None or r.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)       # ffmpeg 万一建了半成品：清掉，不留坏文件
        return False
    dest.chmod(0o600)                      # ffmpeg 落盘的权限跟 umask，这里收口（同 _save）
    return True


def _ensure_tree(root: pathlib.Path, *parts: str) -> pathlib.Path:
    """逐级建目录并**逐级 chmod 0700**，返回最终路径（Ruling 11）。

    `mkdir(parents=True, mode=…)` 只给最后一级套 mode，中间级按 umask 落成 0755；而
    `exist_ok=True` 对**已存在**的目录完全不动权限。本函数的调用点要的是「这条输出路径
    每一级都只有我能进」，所以只能逐级 mkdir + chmod（chmod 才是对已存在目录生效的那一下）。

    **root 本身（out_root）也在收紧范围内**，即使它已存在（`--out` 指向既有目录是 spec §6
    允许的用法）：它是本次导出的输出根，明文照片/视频全在里面，spec 要求 0700——这不是
    误伤用户目录，是导出数据的安全边界。只 chmod 权限，不动既有的任何文件。
    """
    p = root
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    p.chmod(0o700)
    for part in parts:
        p = p / part
        p.mkdir(mode=0o700, exist_ok=True)
        p.chmod(0o700)
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
    d = _ensure_tree(out_root, cm, kind, mon)
    return d / f"{stem}.{ext}"


def _save(out_root: pathlib.Path, cm: str, kind: str, mon: str,
          stem: str, ext: str, data: bytes) -> pathlib.Path:
    p = _dest(out_root, cm, kind, mon, stem, ext)
    p.write_bytes(data)
    p.chmod(0o600)                             # 导出的是明文媒体：除了属主谁也读不到
    return p


def export_media(account: str, chat_ids: list[str], *, kinds: list[str],
                 months: list[str] | None = None, out_dir: pathlib.Path | None = None,
                 max_files: int = 2000, progress=None) -> MediaReport:
    """按会话导出媒体。图片解密（MediaKeyError → 能力级降级不抛）；语音见 Task 3；视频明文复制
    （账号级枚举：只有恰好一个会话时可归属，多会话跳过并记 degraded）。"""
    report = MediaReport()
    out_root = _ensure_tree(pathlib.Path(out_dir) if out_dir else paths.HOME_DIR / "media")
    want = set(months) if months else None
    produced = 0

    def full() -> bool:
        return produced >= max_files

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
                            _save(out_root, cm, "image", mon, dat.stem, ext, buf)
                        report.image.ok += 1
                        produced += 1
                        if progress:
                            progress(produced)
    if "video" in kinds:
        # 视频树是**账号级**的（`msg/video/{月}`，没有会话维度）：整个调用只枚举一次。
        # 唯一会话时才能归属（输出仍落在 out_root/{chat_md5}/video/{月}/）；多会话无从归属
        # ——按会话循环会把同一批文件重复写进 N 个目录、计数翻 N 倍。按消息级归属需要
        # media_refs（后续任务能力），本期不做。
        if len(chat_ids) == 1:
            cm = messages.md5_of(chat_ids[0])
            vbase = paths.XWECHAT_FILES / account / "msg/video"
            if full():
                report.truncated = True
            elif vbase.is_dir():
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
                        _save(out_root, cm, "video", mon, f.stem, "mp4", buf)
                        mp4_stems.add(f.stem.removesuffix("_raw"))
                        report.video.ok += 1
                        produced += 1
                        if progress:
                            progress(produced)
                    for f in _safe_iter(month_dir, "*_thumb.jpg"):
                        if full():
                            report.truncated = True
                            break
                        if f.stem.removesuffix("_thumb") in mp4_stems:
                            continue               # 有本体的不再存缩略图
                        try:
                            buf = f.read_bytes()
                        except OSError:
                            report.video.missing += 1    # 缩略图也拿不到：同样的归口
                            continue
                        _save(out_root, cm, "video", mon, f.stem, "jpg", buf)
                        report.video.thumb += 1
                        produced += 1
        elif len(chat_ids) > 1:
            report.degraded.append("视频为账号级枚举，无法归属具体会话；请逐个会话导出")
    if "voice" in kinds:
        report.voice.raw_only += 0             # Task 3 接入；此占位保证 kinds 校验完整
        report.degraded.append("语音导出尚未接入（下个版本）")
    return report
