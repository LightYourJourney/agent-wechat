"""朋友圈导出用例。"""
from __future__ import annotations

import dataclasses
import hashlib
import pathlib
import re
from typing import TYPE_CHECKING

from .. import errors, media, paths
from ._core import MONTH_RE, NeedConfirm, pkg_clamp_limit, pkg_context
from .media import _resolve_out

if TYPE_CHECKING:
    from collections.abc import Callable

    from .. import moments

@dataclasses.dataclass
class MomentsExportResult:
    """moments_export 的成功返回（CLI 与 MCP 共用）。out_dir 由本用例赋值，入口不重算。"""
    out_dir: str
    report: "moments.MomentsReport"


def moments_export(contact: str | None = None, *, months: list[str] | None = None,
                   with_media: bool = False, limit: int = 500, out_dir: str | None = None,
                   account: str | None = None,
                   fetch: Callable[[moments.SnsMedia], bytes] | None = None
                   ) -> "MomentsExportResult | NeedConfirm":
    """朋友圈导出用例：解析 →（可选）下载解密 → 渲染 HTML。

    `with_media=False`（默认）时**全程零网络**——这是零网络红线上本次新增的例外开关，
    默认必须关闭。`fetch` 仅作测试注入，生产调用方不传。

    `out_dir` 落在微信容器目录之内时**拒绝**（C-11）——在任何读取之前叫停。
    """
    if out_dir:
        paths.refuse_container_out(out_dir)
    # F1 的同族入口（review-T9 复验条件 3）：默认产物根在 HOME 下（`HOME_DIR/moments`），
    # 而本入口此前不过任何闸——相对 AGENT_WECHAT_HOME 下会经 `media.ensure_tree` 在 CWD 里
    # 建出整棵树（EA-003 的后果换了个入口）。同 `store.connect` / `keys.save_cached_hex` 的立场。
    paths.ensure_home_dirs()
    from .. import moments, moments_html, sns_media         # 惰性：moments_html 只在导出时用（media 已顶层）
    ctx = pkg_context(account)
    author = ctx.resolve_optional(contact)
    if isinstance(author, NeedConfirm):
        return author
    bad_months = [m for m in (months or []) if not MONTH_RE.fullmatch(m)]
    if bad_months:
        raise errors.SearchInputError(f"月份格式应为 YYYY-MM：{', '.join(bad_months)}")
    cap = pkg_clamp_limit(limit)          # 不再硬卡 2000
    do_fetch = fetch if fetch is not None else sns_media.fetch

    out_root = media.ensure_tree(
        _resolve_out(out_dir) if out_dir else moments.export_root())
    report = moments.MomentsReport()
    kept: list[moments.SnsPost] = []
    lo: int | None = None
    hi: int | None = None
    for p in moments.iter_posts(ctx.account, chat_filter=author, months=months,
                                report=report):
        if len(kept) >= cap:
            report.truncated = True
            report.remaining += 1
            continue                  # 只计数：**不得**再调 _resolve_media（它会联网）
        lo = p.create_time if lo is None else min(lo, p.create_time)
        hi = p.create_time if hi is None else max(hi, p.create_time)
        report.comments += sum(1 for c in p.comments if c.kind == moments.CMT_COMMENT)
        report.likes += sum(1 for c in p.comments if c.kind == moments.CMT_LIKE)
        kept.append(_resolve_media(p, out_root, report, with_media, do_fetch))
    # 解析失败此前是**无声**的：`parse_post` 返回 None、`iter_posts` 直接 continue，既没有
    # 计数也没有说明。最坏情况下微信改了 XML 形状 → 每条动态都被丢掉 → 退出码 0、信封
    # `{"posts": 0, "degraded": []}`、HTML 写着「没有匹配的动态」，与「这个账号没有朋友圈」
    # **完全无法区分**。故这里补一条带条数的降级说明（CLI 走 stderr、MCP 走 degraded、
    # 产物页首的降级区也读它），并把同类问题合并成一条（50 条失败写 50 行同样的话是噪音）。
    if report.failed:
        report.degraded.append(f"{report.failed} 条动态解析失败（XML 结构不符，已跳过）")
    if report.media_garbage:
        report.degraded.append(
            f"{report.media_garbage} 条媒体解密后不是已知图片格式（未落盘）")
    report.posts = len(kept)
    report.window = (lo, hi) if lo is not None and hi is not None else None
    report.out_dir = str(out_root)

    doc = moments_html.render(kept, report)
    index = out_root / "index.html"
    try:
        paths.atomic_write(index, doc.encode("utf-8"))   # 并发/中断不留半截页
    except OSError as exc:
        raise errors.MediaOutputError(f"写产物失败：{index}") from exc
    try:
        # 尽力而为（同 media._chmod_quiet / _save 的立场）：权限没收紧只是这一级没收紧，
        # 不该让「全部媒体都已下载完」之后的一次 chmod 失败把整次导出作废。
        index.chmod(0o600)
    except OSError:
        pass
    return MomentsExportResult(out_dir=str(out_root), report=report)


# `url@md5` 是**未经消毒的 XML 属性**（好友发的动态就能携带它），而它会被直接当作产物文件名。
# `media._dest` 只做 `d / f"{stem}.{ext}"`，不做任何消毒——`md5="../../../../x"` 会把明文写出
# 产物根之外，`md5="/tmp/x"` 更会因 pathlib 丢弃左操作数而直接落在绝对路径上（已实测复现；
# 后者还会让 `relative_to` 抛 ValueError 穿透 core 层）。故走白名单：只有 32 位 hex 才认
# （真实数据 4899/5076 命中），其余一律回落到 URL 的 md5。
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}\Z")


def _resolve_media(p: "moments.SnsPost", out_root: pathlib.Path,
                   report: "moments.MomentsReport", with_media: bool,
                   do_fetch: Callable[[moments.SnsMedia], bytes]) -> "moments.SnsPost":
    """逐条媒体决定「下载并落盘」还是「标注未下载」。单条失败只计数，不外抛。"""
    from .. import moments, sns_media
    out = []
    for m in p.media:
        if not with_media:
            report.media_not_downloaded += 1
            out.append(m)
            continue
        if sns_media.build_url(m) is None:
            # `build_url` 返回 None == 「本项目不去取」：无 url、kind 不在白名单内，
            # 或**宿主不在 CDN 白名单**。**理由不是「无 token」**——本期之后无 token
            # 只意味着 `AUTH_READY`（URL 已完整，照样可下）；实况照片落在这里是因为
            # `live_url` 不进下载路径。
            report.media_not_downloaded += 1
            out.append(m)
            continue
        try:
            data = do_fetch(m)
            ext = _sniff_ext(data)
            if ext is None:
                report.media_failed += 1
                report.media_garbage += 1          # 降级说明在收尾处统一写一条
                out.append(m)
                continue
            raw = m.md5 or ""
            stem = (raw if _HEX32.match(raw)
                    else hashlib.md5((m.url or "").encode()).hexdigest())
            saved = media.save_artifact(out_root, "media", m.kind,
                                media.month_of(p.create_time), stem, ext, data)
            if saved is None:
                report.media_failed += 1
                out.append(m)
                continue
            report.media_downloaded += 1

            # 封面。只在 KIND_VIDEO 且 AUTH_READY 时下——实测 181 条视频媒体全是 AUTH_READY。
            # 白名单外的封面（真机上 158/181 在 vweixinthumb.tc.qq.com，证书名不匹配）会由
            # `build_url` 拦下——**静默跳过，一个计数器都不加**（`media_not_downloaded` 只对
            # `p.media` 的成员累加，封面不是）。Task 10 的 A4 恒等式正建立在这个认识上。
            thumb_local = None
            if m.kind == moments.KIND_VIDEO and m.thumb and m.auth == moments.AUTH_READY:
                # `key`/`encrypted` **必须一起带过去**：小视频封面是**加密**的，且用的就是
                # 同一个 media 元素的 `enc@key`（真机实测 d1a867dd → ffd8ffe0）。
                # 清掉它们会把能拿到的封面全变成乱码。临时对象让 fetch 的职责不变。
                tcover = dataclasses.replace(m, url=m.thumb)
                if sns_media.build_url(tcover) is not None:
                    try:
                        cdata = do_fetch(tcover)
                        cext = _sniff_ext(cdata)
                        # 扩展名固定 jpg：`media._out_candidates("video", base)` 只声明了
                        # `_thumb.jpg`，判成 png/webp 会与声明不符（将来接 skip 探测时探不到）。
                        if cext != "jpg":
                            report.media_failed += 1
                            if cext is None:
                                report.media_garbage += 1
                        else:
                            csaved = media.save_artifact(out_root, "media", m.kind,
                                                 media.month_of(p.create_time),
                                                 f"{stem}_thumb", cext, cdata)
                            if csaved is None:
                                report.media_failed += 1
                            else:
                                thumb_local = str(csaved.relative_to(out_root))
                    except sns_media.SnsMediaError:
                        report.media_failed += 1
            out.append(dataclasses.replace(m, local=str(saved.relative_to(out_root)),
                                           thumb_local=thumb_local))
        except sns_media.SnsMediaError:
            report.media_failed += 1
            out.append(m)
    return dataclasses.replace(p, media=tuple(out))


def _sniff_ext(data: bytes) -> str | None:
    """按魔数判扩展名——解密成功但内容非图片时返回 None（spec §5）。"""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp":
        return "mp4"
    return None
