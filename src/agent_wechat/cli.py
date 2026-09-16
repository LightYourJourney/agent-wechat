"""agent-wechat CLI。MCP Server 是这里同一批核心函数的另一个入口。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import TYPE_CHECKING

from . import account, bootstrap, decrypt, errors, keys, paths

if TYPE_CHECKING:
    # 仅类型检查期：usecases 会连带拉起 vector→fastembed，不能进 CLI 的启动路径
    # （本文件其余地方也一律在函数内惰性 import usecases）。
    from . import usecases
    # purge 同理（它的 import 链带起 vector→sqlite_vec）：此处只为 _render_* 的签名取类型，
    # 真身仍在 cmd_purge 里惰性 import，冷启动不为它买单。
    from . import purge


def _need_confirm(contact: str, nc: usecases.NeedConfirm) -> int:
    """消歧输出（recent/search/export/semantic 四处一致，策略见 usecases）：

    候选列表（含 kind，与 MCP 的 need_confirm 同形）打到 stdout，一行说明打到
    stderr，退出码 1。stdout 保持可被脚本直接 json 解析。
    """
    print(json.dumps([c.to_dict() for c in nc.candidates], ensure_ascii=False))
    print(f"无法唯一解析「{contact}」；候选见 stdout 列表，请用 username 重试",
          file=sys.stderr)
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    st = account.status()
    print(f"微信运行: {st.running}  pid: {st.pid}")
    print(f"当前登录: {st.current or '未知（未运行或启动中）'}")
    print(f"本机账号: {', '.join(st.accounts) or '（无）'}")
    for acc in st.accounts:
        bare = paths.bare_wxid(acc)
        cached = keys.load_cached_hex(bare)
        line = f"  {acc}: 缓存{'✓' if cached else '✗'}"
        if cached and args.verify_keys:
            results = keys.verify_passphrase(acc, bytes.fromhex(cached))
            ok = sum(1 for _, good in results if good)
            line += f"  HMAC {ok}/{len(results)}"
        print(line)
    return 0


def _render_bootstrap(ev: bootstrap.BootstrapProgress) -> None:
    """bootstrap 进度事件的渲染（核心层只发结构化事件，文案在此）。"""
    if ev.kind == "account":
        print(f"当前登录账号: {ev.bare} (pid={ev.pid})")
    elif ev.kind == "attach":
        print("正在 attach lldb… 请在微信内【退出登录 → 重新登录】以触发密钥派生。")
    elif ev.kind == "verified":
        print(f"✅ HMAC 校验通过 {ev.ok_libs}/{ev.total_libs} 个库，"
              f"passphrase 已缓存到 {keys.CACHE_FILE}")


def cmd_bootstrap(args: argparse.Namespace) -> int:
    bare = bootstrap.bootstrap(timeout=args.timeout, progress=_render_bootstrap)
    print(f"账号 {bare} 引导完成。之后所有操作均无需 sudo。")
    return 0


def cmd_decrypt(args: argparse.Namespace) -> int:
    acc = account.resolve_account(args.account)
    db = paths.db_storage(acc) / args.db
    if not db.exists():
        print(f"库不存在: {db}", file=sys.stderr)
        return 1
    passphrase = keys.get_passphrase(acc)
    with decrypt.decrypted(db, passphrase) as plain:
        if args.out:
            out = paths.HOME_DIR / args.out
            out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            out.write_bytes(plain.read_bytes())
            print(f"已导出明文: {out}")
        else:
            con = sqlite3.connect(f"file:{plain}?mode=ro", uri=True)
            ok = con.execute("PRAGMA quick_check").fetchone()[0]
            print(f"quick_check: {ok}")
            con.close()
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """schema 探测：解密指定库，dump 全部表结构与样本行。微信升级后的第一站。"""
    acc = account.resolve_account(args.account)
    db = paths.db_storage(acc) / args.db
    if not db.exists():
        print(f"库不存在: {db}", file=sys.stderr)
        return 1
    passphrase = keys.get_passphrase(acc)
    with decrypt.decrypted(db, passphrase) as plain:
        con = sqlite3.connect(f"file:{plain}?mode=ro", uri=True)
        # 库里有非 UTF-8 的 TEXT 列，默认解码会直接抛异常；统一按 bytes 取，展示时再防御解码
        con.text_factory = bytes
        print(f"== {args.db} ==")
        for (name, sql) in con.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"):
            name = _decode(name)
            sql = _decode(sql)
            if name.startswith("sqlite_"):
                continue
            n = con.execute(f"SELECT count(*) FROM '{name}'").fetchone()[0]
            print(f"\n--- {name}  ({n} 行) ---\n{sql}")
            if args.sample and n:
                cols = [_decode(c[1]) for c in con.execute(f"PRAGMA table_info('{name}')")]
                rows = con.execute(f"SELECT * FROM '{name}' LIMIT {args.sample}").fetchall()
                for row in rows:
                    sample = {c: _shorten(_decode(v)) for c, v in zip(cols, row)}
                    print("  " + json.dumps(sample, ensure_ascii=False, default=str))
        con.close()
    return 0


def _decode(v):
    """bytes 尽力解码为 str，失败保留 repr 摘要。"""
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return v
    return v


def _shorten(v, limit: int = 80):
    if isinstance(v, bytes):
        return f"<{len(v)}B:{v[:16].hex()}...>" if len(v) > 16 else f"<{len(v)}B:{v.hex()}>"
    s = str(v)
    return s[:limit] + "…" if len(s) > limit else s


def cmd_contacts(args: argparse.Namespace) -> int:
    from . import usecases
    for c in usecases.contacts(query=args.query, account=args.account):
        print(f"{c['username']:<32} {c['kind']:<9} {c['display']}")
    return 0


def cmd_recent(args: argparse.Namespace) -> int:
    from . import usecases
    res = usecases.recent_messages(args.contact, limit=args.n, account=args.account)
    if isinstance(res, usecases.NeedConfirm):
        return _need_confirm(args.contact, res)
    for m in res.messages:
        who = "我" if m.is_self else (m.sender_name or m.sender_id or "?")
        body = m.content or m.media_hint or ""
        print(f"[{m.ts}] {who}: {body[:200]}")
    _render_degradations(res.degraded)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from . import export, store, usecases
    plan = usecases.export_plan(contact=args.contact, full=args.full, account=args.account)
    if isinstance(plan, usecases.NeedConfirm):
        return _need_confirm(args.contact, plan)

    def progress(n: int) -> None:
        print(f"  已写入 {n} 行…", flush=True)

    degraded: list = []

    def note_reader(r) -> None:
        """导出只返回行数，而漂移记在它用的那份 reader 上（spec §6 的 `username_pool`）。"""
        degraded.extend(usecases.collect_degradations(r))

    n = export.run_export(plan.account, chat_id=plan.chat_id, full=plan.full,
                          progress=progress, on_reader=note_reader)
    with store.open() as con:
        print(f"完成：本轮 {n} 行；索引总量 {store.counts(con)}")
    _render_degradations(degraded)      # 降级走 stderr：成功但有保留（spec §7）
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from . import usecases
    res = usecases.search_history(args.query, contact=args.contact, limit=args.n,
                                  account=args.account)
    if isinstance(res, usecases.NeedConfirm):
        return _need_confirm(args.contact, res)
    print(f"mode={res['mode']} total={res['total']}")
    for h in res["hits"]:
        who = "我" if h["is_self"] else (h["sender_name"] or h["sender_id"] or "?")
        print(f"  [{h['ts']}] {h['chat_id']} {who}: {(h['content'] or '')[:120]}")
    _render_degradations(res["degraded"])
    return 0


def cmd_vector_build(args: argparse.Namespace) -> int:
    """构建向量索引。故意不解析当前账号：省略 --account 表示「全部账号」，
    与其它子命令（省略即当前登录账号）语义相反，别照抄 resolve_account。"""
    from . import usecases, vector

    def progress(n: int) -> None:
        print(f"  已嵌入 {n} 行…", flush=True)

    res = vector.build_index(accounts=usecases.to_accounts(args.account),
                             rebuild=args.rebuild, progress=progress)
    print(f"完成：本轮嵌入 {res['embedded_this_run']} 行，跳过 {res['skipped_this_run']} 行")
    return 0


def cmd_semantic(args: argparse.Namespace) -> int:
    """语义检索。chat_id 原样打印（与 search 子命令一致），展示名只用于 MCP 的返回结构。"""
    from . import usecases
    res = usecases.semantic_search(args.query, contact=args.contact, limit=args.n,
                                   account=args.account)
    if isinstance(res, usecases.NeedConfirm):
        return _need_confirm(args.contact, res)
    print(f"mode={res['mode']} total={res['total']}")
    for h in res["hits"]:
        who = "我" if h["is_self"] else h["sender"]
        print(f"  [{h['ts']}] {h['chat_id']} {who}: {(h['content'] or '')[:120]}"
              f"  distance={h['distance']:.4f}")
    _render_degradations(res["degraded"])
    return 0


def cmd_media_export(args: argparse.Namespace) -> int:
    from . import usecases
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    months = ([m.strip() for m in args.months.split(",") if m.strip()]
              if args.months else None)
    res = usecases.media_export(args.chat, kinds=kinds, months=months,
                                out_dir=args.out, limit=args.limit, account=args.account)
    if isinstance(res, usecases.NeedConfirm):
        return _need_confirm(args.chat, res)
    rep = res.report.to_dict()
    print(f"会话 {res.chat_id}")
    print(f"图片   ok={rep['image']['ok']}  failed={rep['image']['failed']}"
          f"  missing={rep['image']['missing']}")
    print(f"语音   ok={rep['voice']['ok']}  failed={rep['voice']['failed']}"
          f"  raw_only={rep['voice']['raw_only']}")
    print(f"视频   ok={rep['video']['ok']}  thumb={rep['video']['thumb']}"
          f"  missing={rep['video']['missing']}")
    if rep["truncated"]:
        print("（达到 --limit 上限，已提前收手）")
    print(f"输出目录: {res.out_dir}")
    _render_degradations(res.report.degraded)
    return 0


def cmd_media_rebuild_refs(args: argparse.Namespace) -> int:
    from . import usecases

    def progress(n: int) -> None:
        print(f"  已回填 {n} 条媒体引用…", flush=True)

    n = usecases.media_rebuild_refs(account=args.account, progress=progress)
    print(f"完成：media_refs 共 {n} 条")
    return 0


def cmd_media_locate(args: argparse.Namespace) -> int:
    """按消息列出媒体归属。与 MCP get_message_media 同一个用例，只是渲染成人类可读。

    `--out` 只参与 `exported` 的判定（要与导出时用的那个一致）。**不写微信目录、不写
    `--out`、不联网**；但语音本体的判定要解密 `media_*.db`，明文只在 `~/.agent-wechat/tmp/`
    短暂存在（用后即焚，见 SECURITY.md）。
    """
    from . import usecases
    keys = [s.strip() for s in args.keys.split(",") if s.strip()] if args.keys else None
    months = ([m.strip() for m in args.months.split(",") if m.strip()]
              if args.months else None)
    res = usecases.message_media(args.chat, keys=keys, months=months, limit=args.limit,
                                 out_dir=args.out, account=args.account)
    if isinstance(res, usecases.NeedConfirm):
        return _need_confirm(args.chat, res)
    print(f"会话 {res.chat_id}    产物根 {res.out_dir}")
    for s in res.states:
        where = s.path or ""
        print(f"  [{s.status:10s}] {s.kind or '-':5s} {s.ts}  {where}")
    c = res.counts
    print(f"汇总：exported={c['exported']} exportable={c['exportable']} "
          f"gone={c['gone']} none={c['none']}")
    # 池化的 counts 把本机可得率相差几个数量级的类别加在一起，看起来像「这个会话的媒体
    # 大多没了」；按 kind 拆开才看得出语音几乎全可导出、图片视频几乎全 gone。拆分由用例层
    # 派生（MCP 信封的 by_kind 读的就是同一份），`none` 态没有 kind，只进上面那行汇总。
    # 固定顺序（不是消息顺序）：两次运行、两次导出的输出可直接 diff
    for kind in ("image", "video", "voice"):
        bucket = res.by_kind.get(kind)
        if not bucket:
            continue
        stats = " ".join(f"{st}={bucket.get(st, 0)}"
                         for st in ("exported", "exportable", "gone"))
        print(f"  按类型 {kind:5s} {stats}")
    if res.truncated:
        # 没有这行，「就这些」与「我在 --limit 处停了」分不出来：明细与计数只是答完的这些
        print("（达到 --limit 上限，已提前收手；以上计数只是答完的这些）")
    if c["gone"]:
        # 主因先写：微信只为**近期（尚未导出）**的消息保留本体（MCP 工具描述与 TOOLS.md §11
        # 是同一套措辞）。先写次因（「微信已清理本体」）会把用户支去「修复」而不是去导历史。
        print("提示：gone 表示消息引用了媒体但本机找不到——微信只为近期（尚未导出）的消息"
              "保留本体，所以导出更多历史比「修复」有用。")
    _render_degradations(res.degraded)       # stderr：成功但有保留，不污染 stdout（spec §7）
    return 0


def cmd_moments_export(args: argparse.Namespace) -> int:
    """朋友圈导出：默认零网络；--with-media 才下载远程媒体本体。"""
    from . import media, usecases
    months = ([m.strip() for m in args.months.split(",") if m.strip()]
              if args.months else None)
    res = usecases.moments_export(
        args.chat, months=months, with_media=args.with_media,
        limit=args.limit, out_dir=args.out, account=args.account)
    if isinstance(res, usecases.NeedConfirm):
        return _need_confirm(args.chat, res)
    r = res.report
    print(f"已导出 {r.posts} 条动态（评论 {r.comments} / 点赞 {r.likes}）")
    if r.window:
        # 时间戳 0 = 「没解析出来」（`moments._ts` 的消毒结果），不是 1970 年。照直印
        # `month_of(0)` 会得到「覆盖：1970-01 ~ 1970-01」——一个凭空造出来的日期。
        lo = media.month_of(r.window[0]) if r.window[0] else "?"
        hi = media.month_of(r.window[1]) if r.window[1] else "?"
        print(f"覆盖：{lo} ~ {hi}（微信本地缓存窗口，非完整历史）")
    print(f"媒体：已下载 {r.media_downloaded} / 未下载 {r.media_not_downloaded}"
          f" / 失败 {r.media_failed}")
    if r.truncated:
        print("注意：触及 --limit，本次被截断", file=sys.stderr)
    _render_degradations(r.degraded)
    print(f"产物：{res.out_dir}/index.html")
    return 0


def _human_size(n: int) -> str:
    """十进制单位。spec §3 的表样就是十进制的：5.30 GB / 12 MB / 182 B。

    **不要用 1024 基底却标 GB**——那会印出 `4.94 GB`，与同屏 help 里的 5.3 GB 打架；
    而 `if i.size` 那种写法会让 182 B 走 MiB 分支印成 `0.0 MiB`，读起来像空文件。
    同一个文件在一次会话里出现三个不同数字，对一个靠数字帮用户判断该不该删的命令是实打实的伤害。

    下面两条规则都是为了「单位与数字相符」：
    · **舍入推进下一档时升档**——999,999 字节按 KB 档格式化是「1000 KB」，用户按单位读会
      以为它比 1 MB 小；999,999,999 印「1000.0 MB」，而真实值差 1 字节就到 1 GB。
    · **整数不印「.0」**——spec §3 的样例是 `12 MB` / `95 MB` / `182 B`，而 `5.30 GB`
      保留两位小数（它不整）。这是唯一能同时命中那五个样例的规则。
    """
    if n < 1000:
        return f"{n} B"
    for div, unit, prec in ((1000, "KB", 0), (1000**2, "MB", 1),
                            (1000**3, "GB", 2), (1000**4, "TB", 2)):
        if n < div * 1000 and round(n / div, prec) < 1000:
            scaled = n / div
            if scaled == int(scaled):
                return f"{int(scaled)} {unit}"
            return f"{scaled:.{prec}f} {unit}"
    return f"{n / 1000**4:.2f} TB"      # ≥999.995 TB：再往上的单位没有实际用途


def cmd_purge(args: argparse.Namespace) -> int:
    """销毁本地产物。**默认 dry-run**：不带 --yes 只打印计划，一个字节不删。

    --all 的语义是「整个 HOME_DIR」（超集），不是三个具名范围的并集——这样才扫得掉
    `decrypt --out` 落在 HOME_DIR 下的散落明文（SECURITY.md:13 标为极高敏感）。
    """
    from . import purge

    scopes = frozenset(s for s, on in
                       (("keys", args.keys), ("index", args.index), ("tmp", args.tmp)) if on)
    plan = purge.plan(scopes, all_=args.all)

    if not args.yes:
        _render_plan(plan)
        return 0

    report = purge.execute(plan)
    print(f"已删除 {len(report.removed)} 项，释放 {_human_size(report.freed)}。")
    if report.home_removed:
        # 这行是「目录本身也没了」的唯一出口：removed 数的是 HOME_DIR 的顶层条目，目录自身
        # 不在其中，空目录的 --all 会印「已删除 0 项，释放 0 B」；残留核查那边也只按路径判
        # 存活（`✅ 计划内路径已清空` 对「目录还在」与「目录没了」是同一句话）。
        print(f"数据目录本身已一并删除：{paths.HOME_DIR.resolve()}")
    for p, err in report.failed:
        print(f"  ⚠️ 未能删除 {p}：{err}", file=sys.stderr)
    _render_residue(purge.verify(plan))
    return 1 if report.failed else 0


def _render_plan(plan: purge.PurgePlan) -> None:
    from . import purge      # 与 cmd_purge 同一惰性策略：purge 的 import 链带起 vector→sqlite_vec
    print("将删除：")
    # 绝对目标必须先印：下面每行的路径都可能与用户以为的那个目录无关（AGENT_WECHAT_HOME
    # 被设成空值时全都相对当前工作目录），而「将删除：」这四个字是这张表唯一的上下文。
    print(f"目标目录：{paths.HOME_DIR.resolve()}")
    for i in plan.items:
        # 目录的 i.size 是 inode 自身大小（几十到几百字节），**不是整树体积**——
        # 而 execute 的汇总行用的是 _tree_size 量出的整树体积。同一个 tmp/ 会在计划表里
        # 显示 96 B、在汇总行显示几百 MB，差好几个数量级。spec §3 的表样对那一行标的正是
        # 「—」，故目录一律不显体积。（T4 修过 freed 上的同一个错误，这是它在渲染器里的残留。）
        size = _human_size(i.size) if (i.exists and i.artifact.kind != "dir") else "—"
        mark = "" if i.exists else "（不存在）"
        print(f"  {i.artifact.scope:<7} {i.artifact.path}  {size:>10}  "
              f"{'敏感' if i.artifact.sensitive else '否':<4} {i.artifact.rebuild}{mark}")
    for e in plan.loose:
        # spec §3：每行点名它属于哪个已知产物，或标「散落文件」。models/ 与 tasks.json 是
        # vector/tasks 的已知产物，只是没有对应的 CLI 开关——一律印「不属于任何具名范围」
        # 字面没错，读起来却是「没人认识的文件」。
        # 体积由 purge 侧算（目录走整树，见 _loose_entry）：CLI 不再自己量一遍，那是第二份来源。
        known = purge.known_artifact_label(e.path)
        mark = f"已知产物：{known}" if known else "不属于任何具名范围"
        print(f"  {'all':<7} {e.path}  {_human_size(e.size):>10}  （{mark}）")
    print("\n未删除任何文件。加 --yes 执行。")


def _render_residue(rep: purge.ResidueReport) -> None:
    print("\n残留核查：")
    for p in rep.not_removed:
        print(f"  ❌ 计划内仍未删除：{p}")
    for p in rep.kept:
        print(f"  · 按范围保留 / 散落：{p}")
    for name, pids in rep.held.items():
        print(f"  ⚠️ {name} 仍被 pid {','.join(map(str, pids))} 持有"
              f"（空间要等该进程退出才释放）")
    for x in rep.tmp_leaks:
        print(f"  ⚠️ $TMPDIR 遗留：{x}")
    print(f"  Time Machine 排除：{rep.tm_excluded}")
    print(f"  本地快照：{rep.snapshots}")
    if not (rep.not_removed or rep.kept or rep.held or rep.tmp_leaks):
        print("  ✅ 计划内路径已清空，未发现残留")


def _render_degradations(degraded) -> None:
    """降级说明走 stderr：成功但有保留，不该污染 stdout（spec §7）。

    三种形状都要收：`recent` 给 `usecases.Degradation`（dataclass，带 db/table，故能印出
    那句诊断），search/semantic 给随信封走的 dict（`{"capability", "effect"}`，与 MCP 同一
    形状——它刻意不带表名，用户不需要知道），media 给 media.py 就地写好的整句 str
    （能力级降级的成文权在 core：无 ffmpeg、密钥不可用这些原因只有它自己知道）。
    """
    for d in degraded:
        if isinstance(d, dict):
            print(f"⚠️  {d['effect']}", file=sys.stderr)
        elif isinstance(d, str):
            print(f"⚠️  {d}", file=sys.stderr)
        else:
            print(f"⚠️  {d.effect}（{d.db} 的 {d.table} 结构不符）", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="agent-wechat",
                                description="macOS 微信聊天记录读取 + MCP Server（仅限本人已登录账号）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status", help="查看登录态/账号/密钥缓存")
    sp.add_argument("--verify-keys", action="store_true", help="对每个库做 HMAC 校验（较慢）")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("bootstrap", help="[需 sudo] 一次性引导：lldb 捕获 passphrase")
    sp.add_argument("--timeout", type=int, default=bootstrap.DEFAULT_TIMEOUT)
    sp.set_defaults(fn=cmd_bootstrap)

    sp = sub.add_parser("decrypt", help="解密指定库（调试用）")
    sp.add_argument("db", help="相对 db_storage 的路径，如 session/session.db")
    sp.add_argument("--out", help="输出文件名（存于 ~/.agent-wechat/ 下）；省略则做 quick_check")
    sp.add_argument("--account", help="目标账号目录名；省略用当前登录账号")
    sp.set_defaults(fn=cmd_decrypt)

    sp = sub.add_parser("inspect", help="解密并 dump 表结构（schema 探测）")
    sp.add_argument("db", help="相对 db_storage 的路径")
    sp.add_argument("--sample", type=int, default=2, help="每表样本行数（默认 2）")
    sp.add_argument("--account", help="目标账号目录名；省略用当前登录账号")
    sp.set_defaults(fn=cmd_inspect)

    sp = sub.add_parser("contacts", help="搜索联系人（解析口述名字）")
    sp.add_argument("query", nargs="?", default="")
    sp.add_argument("--account")
    sp.set_defaults(fn=cmd_contacts)

    sp = sub.add_parser("recent", help="读取某会话最近 N 条（实时解密）")
    sp.add_argument("contact")
    sp.add_argument("-n", type=int, default=50, help="返回条数（1-500，默认 50）")
    sp.add_argument("--account")
    sp.set_defaults(fn=cmd_recent)

    sp = sub.add_parser("export", help="全量/增量导出到本地检索库")
    sp.add_argument("--contact", help="仅导出该会话；省略则全量")
    sp.add_argument("--full", action="store_true", help="忽略水位线全量重扫")
    sp.add_argument("--account")
    sp.set_defaults(fn=cmd_export)

    sp = sub.add_parser("search", help="在本地检索库中全文检索")
    sp.add_argument("query")
    sp.add_argument("--contact")
    sp.add_argument("-n", type=int, default=20)
    sp.add_argument("--account")
    sp.set_defaults(fn=cmd_search)

    sp = sub.add_parser("vector-build", help="构建向量语义索引（本地嵌入，长任务）")
    sp.add_argument("--rebuild", action="store_true", help="先清空索引再全量重建")
    sp.add_argument("--account", help="仅索引该账号；省略则索引全部账号")
    sp.set_defaults(fn=cmd_vector_build)

    sp = sub.add_parser("semantic", help="语义检索（需先 vector-build）")
    sp.add_argument("query")
    sp.add_argument("--contact", help="仅检索该会话（username 或展示名）")
    sp.add_argument("-n", type=int, default=10)
    sp.add_argument("--account")
    sp.set_defaults(fn=cmd_semantic)

    sp = sub.add_parser("purge", help="销毁本地产物（默认 dry-run，加 --yes 才真删）")
    sp.add_argument("--keys", action="store_true", help="密钥缓存 passphrase.json")
    sp.add_argument("--index", action="store_true", help="本地检索库 index.db（含向量索引）")
    sp.add_argument("--tmp", action="store_true", help="解密临时目录")
    sp.add_argument("--all", action="store_true",
                    help="整个 ~/.agent-wechat（含 models/ 与散落文件），与具名范围互斥")
    sp.add_argument("--yes", action="store_true", help="真正执行删除")
    sp.set_defaults(fn=cmd_purge)

    sp = sub.add_parser("media", help="媒体导出（图片/语音/视频）")
    media_sub = sp.add_subparsers(dest="media_cmd", required=True)
    msp = media_sub.add_parser("export", help="导出某会话的媒体到本地目录")
    msp.add_argument("--chat", required=True, help="会话（名字或 username）")
    msp.add_argument("--kinds", default="image,voice,video",
                     help="逗号分隔：image,voice,video（默认全部）")
    msp.add_argument("--months", default=None, help="逗号分隔的 YYYY-MM（默认不限）")
    msp.add_argument("--out", default=None, help="输出目录（默认 ~/.agent-wechat/media/）")
    msp.add_argument("--limit", type=int, default=2000, help="最多导出文件数（1-2000）")
    msp.add_argument("--account")
    msp.set_defaults(fn=cmd_media_export)
    msp = media_sub.add_parser("locate", help="按消息列出媒体归属（不写微信目录、不写 --out、不联网）")
    msp.add_argument("--chat", required=True, help="会话（名字或 username）")
    msp.add_argument("--keys", default=None, help="逗号分隔的 msg_key（默认整个会话）")
    msp.add_argument("--months", default=None, help="逗号分隔的 YYYY-MM")
    msp.add_argument("--out", default=None, help="产物根（默认 ~/.agent-wechat/media/）")
    msp.add_argument("--limit", type=int, default=200, help="最多回答几条（1-500）")
    msp.add_argument("--account")
    msp.set_defaults(fn=cmd_media_locate)
    msp = media_sub.add_parser("rebuild-refs", help="全量重扫微信库回填 media_refs 索引")
    msp.add_argument("--account")
    msp.set_defaults(fn=cmd_media_rebuild_refs)

    sp = sub.add_parser("moments", help="朋友圈读取与导出")
    msp = sp.add_subparsers(dest="moments_cmd", required=True)
    esp = msp.add_parser("export", help="导出朋友圈为 HTML 时间线")
    esp.add_argument("--chat", help="按作者筛选（名字或 wxid；省略=全部好友）")
    esp.add_argument("--months", help="逗号分隔的 YYYY-MM，限定月份")
    esp.add_argument("--with-media", action="store_true",
                     help="联网下载远程媒体本体（默认关闭，不联网）")
    esp.add_argument("--out", help="输出目录（默认 $AGENT_WECHAT_HOME/moments）")
    esp.add_argument("--limit", type=int, default=2000, help="最多导出动态数（1-2000）")
    esp.add_argument("--account", help="账号目录名")
    esp.set_defaults(fn=cmd_moments_export)

    args = p.parse_args(argv)
    if args.cmd == "purge":
        named = args.keys or args.index or args.tmp
        if args.all and named:
            p.error("--all 不能与 --keys/--index/--tmp 同时使用")
        if not args.all and not named:
            p.error("必须指定至少一个范围：--keys / --index / --tmp / --all")
    try:
        code = args.fn(args)
    except errors.ContactNotFound as exc:
        # 入口专属后缀：CLI 用户该用 `contacts`，而不是 MCP 的工具名 list_contacts
        # （USAGE.md §9「输出约定与错误提示」文档化的就是这一条）。
        print(f"{exc}，请用 contacts 查询", file=sys.stderr)
        code = 1
    except errors.AgentWeChatError as exc:
        # 项目错误 → 单行中文（含 hint）打到 stderr，退出码 1。argparse 自己的
        # SystemExit(2) 在 parse_args 处先抛出，不经过这里，故参数错仍是 2。
        print(errors.to_text(exc), file=sys.stderr)
        code = 1
    raise SystemExit(code)
