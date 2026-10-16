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
            # F7：解码收口到 keys.decode_cached_hex（坏 hex/长度不符 → KeyNotFound 单行中文，
            # 不是裸 ValueError 的 traceback；也不再印「缓存✓ HMAC 0/0」）
            results, unreadable = keys.verify_passphrase(
                acc, keys.decode_cached_hex(bare, cached))
            ok = sum(1 for _, good in results if good)
            line += f"  HMAC {ok}/{len(results)}"
            if unreadable:
                line += f"（另有 {unreadable} 个库读不到）"   # EA-051：读不到 ≠ 不过
        print(line)
    tm = paths.time_machine_excluded()
    if tm is not None:
        # EA-054（F2 修法）：印**只读查询**到的真实状态（与 purge 的核查同源）。查不出来时
        # 不印——「判不出来」与「没排除」是两件事，混成一行正是本仓在消灭的那种假象。
        print(f"备份排除: {tm}")
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
    elif ev.kind == "warn":
        print(f"⚠️  {ev.warning}")


def cmd_bootstrap(args: argparse.Namespace) -> int:
    bare = bootstrap.bootstrap(timeout=args.timeout, progress=_render_bootstrap)
    print(f"账号 {bare} 引导完成。之后所有操作均无需 sudo。")
    return 0


def cmd_decrypt(args: argparse.Namespace) -> int:
    if args.out:
        # C-11：整库明文（SECURITY.md §1 标为极高敏感）**绝不**落进微信自己的容器目录。
        # 判据在 paths（四个 `--out` 入口共用），且先于账号解析与解密——那才是真正贵的两步。
        # 路径与下面写入时**逐字一致**（`HOME_DIR / args.out`：绝对路径时 pathlib 丢弃左操作数）。
        paths.refuse_container_out(paths.HOME_DIR / args.out)
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
            out.chmod(0o600)      # EA-050：整库明文，权限与目录侧承诺对齐
            print(f"已导出明文: {out}")
        else:
            con = decrypt.open_plain(plain)
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
        con = decrypt.open_plain(plain)
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


def _note_skipped(n: int) -> None:
    """「跳过 N 行（已排除）」走 stderr——与 `_render_degradations` 同一条出口：
    导出/回填**成功但有保留**（被 `purge --chat` 删掉的行仍在微信库里、被写入端挡下），
    不该污染 stdout（那是给人看的表，也是管道里机器可读的部分，spec §7）。

    量词是**行**、不是「条」：三个调用点写的是三张不同的表——`export` 写 messages +
    media_refs、`media rebuild-refs` 只写 media_refs、`appmsg rebuild` 只写 msg_parts——
    而后两条**一条 messages 都不写**，所以「只数消息条数」在那里根本无从谈起（回调拿不到）。
    说「条」会被读成消息条数，而它在后两条命令上恒不等于消息条数；说「行」三者都成立：
    就是被排除清单挡下、没写进库的行数。
    """
    _render_degradations([f"跳过 {n} 行（已排除）：已销毁会话的这些行不再写入索引"])


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
                          progress=progress, on_reader=note_reader,
                          on_skipped=_note_skipped)
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
                                out_dir=args.out, limit=args.limit, force=args.force,
                                account=args.account, with_cdn=args.with_cdn)
    if isinstance(res, usecases.NeedConfirm):
        return _need_confirm(args.chat, res)
    rep = res.report.to_dict()
    print(f"会话 {res.chat_id}")
    print(f"图片   ok={rep['image']['ok']}  skipped={rep['image']['skipped']}"
          f"  failed={rep['image']['failed']}  missing={rep['image']['missing']}")
    print(f"语音   ok={rep['voice']['ok']}  skipped={rep['voice']['skipped']}"
          f"  failed={rep['voice']['failed']}  raw_only={rep['voice']['raw_only']}")
    print(f"视频   ok={rep['video']['ok']}  skipped={rep['video']['skipped']}"
          f"  thumb={rep['video']['thumb']}  missing={rep['video']['missing']}")
    print(f"贴纸   ok={rep['sticker']['ok']}  skipped={rep['sticker']['skipped']}"
          f"  failed={rep['sticker']['failed']}  missing={rep['sticker']['missing']}")
    # remaining 是「未导出」而不是「续跑一定新增 N 个」：被上限挡下的条目里，续跑时
    # 仍会失败的（dat 读不出、blob is None）也计入，故只能说「还有 N 个未导出」。
    left = sum(rep[k]["remaining"] for k in ("image", "voice", "video", "sticker"))
    if rep["truncated"]:
        print(f"（达到 --limit 上限，还有 {left} 个未导出 —— 重跑同一条命令即可继续）")
    print(f"输出目录: {res.out_dir}")
    _render_degradations(res.report.degraded)
    return 0


def cmd_media_rebuild_refs(args: argparse.Namespace) -> int:
    from . import usecases

    def progress(n: int) -> None:
        print(f"  已回填 {n} 条媒体引用…", flush=True)

    n = usecases.media_rebuild_refs(account=args.account, progress=progress,
                                    on_skipped=_note_skipped)
    print(f"完成：media_refs 共 {n} 条")
    return 0


def cmd_appmsg_rebuild(args: argparse.Namespace) -> int:
    from . import usecases

    def progress(n: int) -> None:
        print(f"  已回填 {n} 条 appmsg…", flush=True)

    res = usecases.appmsg_rebuild_parts(account=args.account, progress=progress,
                                        on_skipped=_note_skipped)
    # 两组计数分母不同（见 usecases.appmsg_rebuild_parts 的 docstring）：`parts` 是本轮写入，
    # `resolved` / `unresolved` 是该账号整表。印出来时各自标清，免得被当同一个分母读。
    print(f"完成：本轮写入 msg_parts {res['parts']} 条；"
          f"该账号回指解析 {res['resolved']} / 未解析 {res['unresolved']}；"
          f"拒绝 {res['rejected']}；解不开 {res['malformed']}")
    return 0


def cmd_extras_rebuild(args: argparse.Namespace) -> int:
    from . import usecases

    def progress(n: int) -> None:
        print(f"  已回填 {n} 条位置/名片…", flush=True)

    res = usecases.msgextra_rebuild(account=args.account, progress=progress,
                                    on_skipped=_note_skipped)
    # `empty` **不是** `extras` 的子集（它含被排除清单挡下的行，判据只有一处、在
    # `upsert_extras` 里，见 `msgextra_rebuild` 的 docstring），所以措辞写成并列的两句，
    # **不用「其中」**——那个词会把它读成包含关系。
    print(f"完成：本轮写入 msg_extras {res['extras']} 条；"
          f"扫到的位置/名片里 {res['empty']} 条没有可读字段")
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
    for kind in ("image", "video", "voice", "sticker"):
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
    if res.by_kind.get("sticker", {}).get("gone"):
        # 贴纸的 gone 与上面那句**不是一回事**（`chat_text.STICKER_GONE` 的注释是本条的
        # 依据）：它取不出来是微信侧存成加密格式、方案未破，与「本机有没有」无关——所以上面
        # 那句「导出更多历史比修复有用」对贴纸是句错话，必须单独说清。主语自己给，不搬
        # `STICKER_GONE`（那个常量是照 `[标签 · 措辞]` 槽位写的，单独用就没有先行词）。
        print("提示：贴纸的 gone 是「取不出来」而不是「本机找不到」——微信把它存成加密格式，"
              "解密方案未破，导出更多历史也拿不出来。")
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
        print(f"注意：触及 --limit，还有 {r.remaining} 条更早的动态没进来", file=sys.stderr)
    _render_degradations(r.degraded)
    print(f"产物：{res.out_dir}/index.html")
    return 0


def cmd_chat_export(args: argparse.Namespace) -> int:
    """会话时间线导出（HTML / Markdown）。产物路径逐条印出来 —— **印的是真正落盘的那些**，
    写失败的不会出现在列表里（`chatview.write` 的口径），故「缺少一行」本身就是信号。"""
    from . import usecases
    months = ([m.strip() for m in args.months.split(",") if m.strip()]
              if args.months else None)
    formats = tuple(f.strip() for f in args.format.split(",") if f.strip())
    res = usecases.chat_export(args.chat, months=months, limit=args.limit,
                               formats=formats, out_dir=args.out, account=args.account)
    if isinstance(res, usecases.NeedConfirm):
        return _need_confirm(args.chat, res)
    kinds = "  ".join(f"{k}={v}" for k, v in sorted(res.counts.items()))
    print(f"会话 {res.chat_id}   {sum(res.counts.values())} 条   {kinds}")
    if res.truncated:
        # 终端这一行是**补充**，不是唯一出口：**产物顶部也会印**（`chat_html` / `chat_md`
        # 的 `_notice`，spec §4.5 明令「必须印在产物顶部」，`test_truncated_is_printed_at_the_top`
        # 两个模板各有一条）。这里再回一次终端，是因为用户可能只读了 stdout 里的产物路径、
        # 根本没打开产物。
        # **不要反过来读成「只回终端、产物里不印」**——产物里不印才是「用户会以为拿到的
        # 是全部」那个故障；本行的存在理由恰恰是**不要**发生它。（与 media export 同一口径。）
        print("注意：触及 --limit，只导了范围内最近的这些（可能还有更早的没进来）",
              file=sys.stderr)
    if res.unparsed or res.rejected:
        print(f"appmsg：解不开 {res.unparsed} 条，主动拒绝 {res.rejected} 条"
              "（明细见产物顶部）", file=sys.stderr)
    for f in res.files:
        print(f"产物：{f}")
    _render_degradations(res.degraded)
    return 0


def cmd_chat_index(args: argparse.Namespace) -> int:
    """列出已导出的会话，写 `{--out}/index.html`。

    **一个产物都没有时不写空壳页面、也不是错误**：印一句提示、退出码 0 —— 用户只是还没
    导过任何会话。写一张 0 行的页面会把「还没导过」与「索引页坏了」弄得一模一样。
    """
    from . import usecases
    res = usecases.chat_index(out_dir=args.out, account=args.account)
    if res.file is None:
        print("没有已导出的会话；先运行 `chat export`")
        return 0
    # 措辞刻意不用「已索引」：`chats` 数的是**磁盘上的产物**（含索引里查不到的那些，
    # 页面那一行自己写着「索引里没有这个会话」）——同一屏两句话的「索引」不能是两个意思。
    print(f"已导出的会话：{res.chats} 个")
    print(f"产物：{res.file}")
    _render_degradations(res.degraded)
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


def _exclusion_count() -> tuple[int | None, str | None]:
    """排除清单（`exclusions.db`）的墓碑数，以及**它到底读没读成**。**绝不抛**。

    返回 `(计数, 读不出来的原因)`，三种组合各有各的含义、**不许合并**（M-6）：
    · `(None, None)` —— 清单文件不在。这是**常态**：`store.connect()` 每次都会把它建出来，
      所以「空清单」与「没清单」对用户是同一件事——没有任何东西失效，不必出声；
    · `(n, None)`    —— 读到 n 条；
    · `(None, 原因)` —— **判不出来**（损坏 / 权限 / 被占）。它与上面那条正相反：用户此刻
      最需要知道的是「**无法确认排除还在不在生效**」——不说，`--index`/`--all` 两条路都会
      静默给出一个「看起来没问题」的输出。

    **刻意不走 `store.open()`**：那条路会 `paths.ensure_home_dirs()` 再把整个索引 schema
    建出来，于是 `purge --index` 刚删掉的 index.db、`purge --all` 刚删掉的整个数据目录都会被
    原样重建——「已删除」当场变成假象，紧接着的 verify 还会把重建出来的 index.db 报成
    「计划内仍未删除」（实测：这两件事都发生了）。

    连接不在这里手搓：`exclusions.standalone()` 做的正是这件事（`:memory:` + 把清单文件
    ATTACH 成 `exclusions`，与生产同一个别名，所以 `exclusions.count` 那句 SQL 原样可用；
    缺失文件给内存空表、不在磁盘上落新文件），另外三处生产调用点是
    `cli._cmd_purge_excluded`（`--excluded`）、`purge.undo_chat`（`--undo`）、
    `usecases.purge_chat_targets`——同一条配方写两遍，迟早会漂成两种。
    （`purge.plan_chat` **不**在其中：它走 `purge._open_index()`，即 `store.open()`。）

    **「文件不在」仍先判一次**：`standalone()` 对缺失文件给的是空表，读出来是 0 条，于是
    `(None, None)` 会塌进 `(0, None)`。两者在 `--index` 那条路上**不同义**——执行末尾
    「排除清单保留：N 条」判的是 `ex_count is not None`，把哨兵丢了就会对着一份不存在的
    清单说「保留 0 条、重建索引后仍生效」。
    """
    from . import exclusions
    try:
        if not exclusions.path().exists():   # 哨兵：缺失不是 0 条（见 docstring 末段）
            return None, None
        with exclusions.standalone() as con:
            return exclusions.count(con), None
    except Exception as exc:
        return None, str(exc)   # 读不出来要报出来，但这句提示不该把销毁本身弄成失败


def _note_exclusion_unreadable(ex_error: str) -> None:
    """「排除清单读不出来」那**一句**（M-6）——计划表与 `--yes` 两条路共用。

    **读不出来与「没有清单」含义正相反**，所以只此一句、且必须单独说：后者是「没有任何
    东西失效」（`store.connect()` 每次都会把清单建出来，空清单是常态），前者是**无法确认
    排除还在不在生效**——它若失效，被删内容下次导出就会回来。合并或静默都会把用户此刻最
    需要知道的那件事丢掉。走 stderr：与 `_note_skipped` / `_render_degradations` 同一条
    「成功但有保留」的出口。
    """
    print(f"⚠️ 排除清单读不出来（{ex_error}）——无法确认它是否还在生效；"
          "它若失效，被删内容下次导出就会回来。", file=sys.stderr)


def cmd_purge(args: argparse.Namespace) -> int:
    """销毁本地产物。**默认 dry-run**：不带 --yes 只打印计划，一个字节不删。

    --all 的语义是「整个 HOME_DIR」（超集），不是具名范围的并集——这样才扫得掉
    `decrypt --out` 落在 HOME_DIR 下的散落明文（SECURITY.md §1 标为极高敏感）。
    """
    # 两个轴各自分派：`--excluded` 是一次全局查询、`--chat` 是「删哪个会话」，两者都不碰
    # 产物范围那份计划（`purge.plan()` 会枚举 HOME_DIR，而它们一个字节都不删）。
    # 分派放在 `from . import purge` 之前只是因为它用不上 purge——**不是**为了省冷启动：
    # `main()` 的 purge 校验分支早就 `from . import purge as _purge` 过了，purge→vector→
    # sqlite_vec 那条链在这个入口无论如何都已经拉起。
    if args.excluded:
        return _cmd_purge_excluded()

    from . import purge

    if args.chat:
        return _cmd_purge_chat(args)

    # 同样从产物表演绎，不手写第二份名单；`--<scope>` 这个约定由 tests/test_cli.py 的
    # test_every_named_scope_has_a_working_flag 逐条钉住。
    scopes = frozenset(s for s in (a.scope for a in purge.artifacts())
                       if s and getattr(args, s))
    plan = purge.plan(scopes, all_=args.all)

    # 排除清单的规模必须在**渲染与删除之前**取：`--all` 会把它连文件一起删掉（它已是产物
    # 之一），删完再读只剩 0 或「没有」——而这两处提示说的都是「失去了多少」（spec §4.1③）。
    # 提前到渲染前是因为 dry-run 也要印它：那是用户**唯一**能先看到后果的地方。
    ex_count = ex_error = None
    if args.index or args.all:
        ex_count, ex_error = _exclusion_count()

    if not args.yes:
        _render_plan(plan, ex_count, ex_error)
        return 0

    if ex_error is not None:
        # 必须印在**删除之前**：`--all` 正要把这份清单连文件一起清掉，那之后再说
        # 「读不出来」就分不清是哪种了（为什么单独一句见 `_note_exclusion_unreadable`）。
        _note_exclusion_unreadable(ex_error)

    report = purge.execute(plan)
    print(f"已删除 {len(report.removed)} 项，释放 {_human_size(report.freed)}。")
    if report.home_removed:
        # 这行是「目录本身也没了」的唯一出口：removed 数的是 HOME_DIR 的顶层条目，目录自身
        # 不在其中，空目录的 --all 会印「已删除 0 项，释放 0 B」；残留核查那边也只按路径判
        # 存活（`✅ 计划内路径已清空` 对「目录还在」与「目录没了」是同一句话）。
        print(f"数据目录本身已一并删除：{paths.HOME_DIR.resolve()}")
    for p, err in report.failed:
        print(f"  ⚠️ 未能删除 {p}：{err}", file=sys.stderr)
    if args.index or args.all:
        # 这两条是刻意响亮的：排除清单要么活过索引重建（--index），要么随全清一起没
        # ——后者意味着被删内容下次导出会回来，用户必须当场知道（spec §4.1③）。
        from . import exclusions as _ex
        if args.all:
            # 两个前提，缺一个这句话就是假的：
            # · 条数不为 0——`store.connect()` 每次都会把清单文件建出来，所以「空清单」是
            #   常态（每个用过本工具的用户都有它），那时没有任何东西失效；
            # · 它**真的**没了——部分失败时清单可能活着，那时说「已随之删除」也是假话
            #   （它活着的话下面的残留核查会点名它）。
            # 计数只能是删之前那份，见 ex_count。
            # exists() 在父目录不可搜索时抛 EACCES 而不是返回 False（purge.py 记过这件事），
            # 判不出来就当它还在：状态提示绝不能把销毁本身弄成失败。
            try:
                gone = not _ex.path().exists()
            except OSError:
                gone = False
            if ex_count and gone:
                print(f"排除清单已随之删除：{ex_count} 条排除失效，被删内容下次导出会回来。")
            elif gone and ex_error is not None:
                # 清单真没了，而条数读不出来（上面那句 ⚠️ 已说过原因）。这一句说**结果**：
                # 条数缺了，但「它没了 ⇒ 被删内容会回来」这条后果照样成立，不能因为数不出
                # 条数就不说（M-6）。
                print("排除清单已随之删除（条数读不出来）——被删内容下次导出会回来。")
        elif ex_count is not None:
            print(f"排除清单保留：{ex_count} 条（exclusions.db），重建索引后仍生效。")
    # EA-029：退出码**读核查**，与会话轴（`_cmd_purge_chat` 的
    # `1 if report.failed or res.not_removed else 0`）同一条规则。此前只读 `report.failed`，
    # 于是「报告说删了 0 项、核查印 ❌、退出码 0」可以在同一份输出里并存——脚本只看退出码
    # 就会把一次什么都没删的销毁读成完成（spec §6 那张表要求「部分失败 → 1」）。
    res = purge.verify(plan)
    _render_residue(res)
    return 1 if (report.failed or res.not_removed) else 0


def _render_plan(plan: purge.PurgePlan, ex_count: int | None,
                 ex_error: str | None) -> None:
    # ex_count / ex_error **必须由调用方给**（不给默认值）：它们为空时下面①那句会静默不印
    # ——spec §4.1③ 要求计划表把「不可重建：N 条排除随之失效」印出来，而这条表是用户
    # 据以说 --yes 的唯一依据。一个能省掉参数的默认值，就是让那句在该印的时候消失的入口。
    # 与 cmd_purge 同一惰性策略：purge 的 import 链带起 vector→sqlite_vec
    from . import purge
    print("将删除：")
    # 绝对目标必须先印：下面每行的路径都可能与用户以为的那个目录无关（AGENT_WECHAT_HOME
    # 被设成空值时全都相对当前工作目录），而「将删除：」这四个字是这张表唯一的上下文。
    print(f"目标目录：{paths.HOME_DIR.resolve()}")
    for i in plan.items:
        # 目录现在也有整树体积（_item 走 _tree_size），不再印「—」——那张表是用户据以说
        # --yes 的唯一依据，而「—」让最大的一项（media/，10 GB）看起来无事发生。
        size = _human_size(i.size) if i.exists else "—"
        mark = "" if i.exists else "（不存在）"
        # scope 为 None = 有拥有者但没有 CLI 开关（models/、tasks.json），只随 --all 一起删。
        # 印「all」与下面 loose 行第一列同形：那一列的含义本来就是「按哪个范围删得掉它」。
        print(f"  {i.artifact.scope or 'all':<7} {i.artifact.path}  {size:>10}  "
              f"{'敏感' if i.artifact.sensitive else '否':<4} {i.artifact.rebuild}{mark}")
    for e in plan.loose:
        # 走到这里的一定是**没有拥有者**的条目：有拥有者的（含没有开关的 models/、
        # tasks.json）都被 plan() 排进了 items。所以敏感度写「未知」而不是留空——这张表里
        # 空着读起来是「已检查过、不要紧」，而真实含义是「没人认识它」，两者正相反。
        # 体积由 purge 侧算（目录走整树，见 _loose_entry）：CLI 不再自己量一遍，那是第二份来源。
        print(f"  {'all':<7} {e.path}  {_human_size(e.size):>10}  "
              f"{'未知':<4} 不属于任何已知产物")
    # 排除清单是**唯一一个删了就不可重建**的产物，spec §4.1③ 要求计划表为它单列一句。
    # 判据是 `Artifact.unrebuildable`——「不可重建」这条事实的结构化出处
    # （`purge.known_artifacts` 声明它），不是这里的文案、也不是按路径现推：文案是本函数
    # 渲染出来的东西，拿它当判据等于自己给自己出题。含不含天然分开了两条路：`--all`
    # 会清掉它、`--index` 不会（清单必须活过索引重建）。
    if any(i.artifact.unrebuildable for i in plan.items):
        if ex_error is not None:
            # 读不出来**不能**说成「0 条」——那是「一条都没被挡」的意思，与事实相反，而
            # 用户正是据这句话判断重导会不会把已删内容带回来（M-6）。
            _note_exclusion_unreadable(ex_error)
        elif ex_count:
            print(f"  不可重建：{ex_count:,} 条排除随之失效，被删内容下次导出会回来")
        # 条数为 0 或清单不在：不印。`store.connect()` 每次都会把清单文件建出来，空清单是
        # 常态（`cmd_purge` 的 --all 分支在删除后记着同一笔账）——「0 条失效」是噪音。
    print("\n未删除任何文件。加 --yes 执行。")


def _tm_excluded_line(raw: str) -> str:
    """`tmutil isexcluded` 的原文 → **判定句**（EA-032）。

    这两格是残留核查里**唯一**直指「外置面 / 可恢复面」的字段，而 `[Included]` 与
    `[Excluded]` 对用户是同一类中性词——前者的意思是「删除前的明文可能还在备份里」。
    判定成文在这里（渲染层），`ResidueReport` 的字段语义不动。

    **认不出的形态一律原样印**：`_tmutil` 的「无法判定（…）」与「无输出」说的是「判不出来」，
    与「没有」正相反（见 `_tmutil` 的 docstring），不许被判定句吞掉。
    """
    if "[Included]" in raw:
        return f"⚠️ 数据目录在 Time Machine 范围内——删除前的明文可能还在备份里（{raw}）"
    if "[Excluded]" in raw:
        return f"已排除（{raw}）"
    return raw


def _snapshots_line(raw: str) -> str:
    """`tmutil listlocalsnapshots` 的原文 → **判定句**（EA-032）。

    **没有快照时它也会印一行表头**（`Snapshots for disk /:`），所以「非空」不是判据——
    要数的是 `com.apple.TimeMachine.*.local` 那几行。
    """
    snaps = [ln.strip() for ln in raw.splitlines()
             if ln.strip().startswith("com.apple.TimeMachine")]
    if snaps:
        return (f"⚠️ 存在 {len(snaps)} 个本地快照——它们可能含删除前的数据，"
                f"销毁不是不可逆")
    if raw.startswith("无法判定") or raw.startswith("无输出"):
        return raw
    return f"无（{raw.strip()}）" if raw.strip() else "无"


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
    print(f"  Time Machine 排除：{_tm_excluded_line(rep.tm_excluded)}")
    print(f"  本地快照：{_snapshots_line(rep.snapshots)}")
    if not (rep.not_removed or rep.kept or rep.held or rep.tmp_leaks):
        print("  ✅ 计划内路径已清空，未发现残留")


def _render_degradations(degraded) -> None:
    """降级说明走 stderr：成功但有保留，不该污染 stdout（spec §7）。

    三种形状都要收：`recent` 给 `usecases.Degradation`（dataclass；只有 `drift=True`
    （缺省）才追加「结构不符」诊断——EA-095：非漂移成因不许印成漂移），search/semantic 给随信封走的 dict（`{"capability", "effect"}`，与 MCP 同一
    形状——它刻意不带表名，用户不需要知道），media 给 media.py 就地写好的整句 str
    （能力级降级的成文权在 core：无 ffmpeg、密钥不可用这些原因只有它自己知道）。
    """
    for d in degraded:
        if isinstance(d, dict):
            print(f"⚠️  {d['effect']}", file=sys.stderr)
        elif isinstance(d, str):
            print(f"⚠️  {d}", file=sys.stderr)
        else:
            suffix = f"（{d.db} 的 {d.table} 结构不符）" if getattr(d, "drift", True) else ""
            print(f"⚠️  {d.effect}{suffix}", file=sys.stderr)


# ---------- `purge --chat` / `--undo` / `--excluded` ----------

# 计划表与执行报告共用的五张表。顺序固定（不按字典序）：两次运行的输出可以直接 diff。
_CHAT_TABLES = (
    ("messages", "消息", "含 FTS 索引（由既有触发器跟随）"),
    ("vectors", "向量", ""),
    ("parts", "appmsg 部件", ""),
    ("extras", "位置 / 名片载荷", ""),
    ("refs", "媒体引用", ""),
    ("display_names", "群成员显示名", ""),
)


def _chat_export_dirs(plan: purge.ChatPlan) -> dict:
    """导出物目录 → (体积, 计划时是否存在)，**按目录去重**。

    跨账号 purge 里多条 entry 共享同一个 `media/<md5>/`（目录按会话 md5 分，与账号无关），
    逐条累加会把同一个目录算两遍——而这一行是用户据以判断「删了能省多少」的唯一来源。
    去重口径与核查侧的 `ChatResidue.export_dirs` 一致。
    """
    seen: dict = {}
    for e in plan.entries:
        seen.setdefault(e.export_dir, (e.export_size, e.export_exists))
    return seen


def _render_chat_plan(plan: purge.ChatPlan, query: str) -> None:
    # 第一行印**解析真身**：这个项目栽过一次（61% 的会话按真名解析会静默落到裸 id），而销毁
    # 不可逆——用户必须当场看见删的是谁，且一个会话名可能对应多个账号的同一份副本。
    print(f"将删除（会话：{query} → {plan.entries[0].chat_id}）：")
    for e in plan.entries:
        # 账号为空串 = 索引里没有任何账号有这个会话（`purge_chat_targets` 的「空集也回一条」，
        # 为的是这条会话的导出物仍进计划）。此时没有账号块可印，但下面的导出物那一行照常
        # ——它只取决于 chat_md5、与账号无关，而它恰恰是这一趟唯一要删的东西。
        if e.account:
            print(f"  账号 {e.account}")
            for key, label, note in _CHAT_TABLES:
                n = e.counts.get(key, 0)
                print(f"    {label:<12} {n:>7,} 条"
                      f"{'    ' + note if note and n else ''}")
    for d, (size, exists) in _chat_export_dirs(plan).items():
        # 体积为 0 或「不存在」时印 —（与 _render_plan 同款）：0 B 会被读成「空目录」，
        # 而这里的事实是「这个目录根本不在」。
        print(f"  导出物  {d}   {_human_size(size) if exists else '—':>10}   "
              f"解密后的图/视频/语音，及可直接打开的 chat/index.html"
              f"{'' if exists else '（不存在）'}")
    print(f"  随后列入永久排除：{sum(e.tombstone for e in plan.entries):,} 条"
          "（**新消息不受影响**，照常进索引）")
    # 这段警告不是客套：`moments/` 是一份 HTML 装所有人，没有按会话切分的目录结构，删掉
    # 会话后那个人的动态与互动仍可能躺在里面——不说清楚，用户会以为清干净了（spec §3.1）。
    print("\n⚠️ 不在本范围内：朋友圈导出（moments/ 是一份 HTML 装所有人，不按会话分目录）"
          "——\n   那个人的动态与互动**仍可能留在其中**。")
    # 这一句是 spec §5.3 点名要求写进计划表的（原文：「另一侧无法阻止，所以计划表要写明：
    # 执行期间正在跑的导出任务会因写锁而失败」）。它说的是**本命令对别人做了什么**：
    # 读任何东西之前就 `BEGIN IMMEDIATE` 取写锁（SQLite 同时只允许一个写者），于是正在跑的
    # export 的下一批写入会等满 Python sqlite3 的默认 timeout=5 秒，然后抛 `database is locked`。
    # 不查 tasks 注册表做提前拦截：它只覆盖本工具登记过的任务，而这里防的是**文件被持有**
    # 本身（非本工具进程也算）。唯一诚实的做法就是把它印在用户据以说 --yes 的那张表上。
    print("⚠️ 执行期间正在跑的导出任务会因写锁而失败——本命令先取写锁再读，"
          "同一时刻只有一个写者；\n   等导出跑完再执行，或让它下一轮重试。")
    print("删的是本工具的副本，不是微信里的记录（微信容器恒只读）。")


def _render_chat_reports(reports: tuple[purge.ChatReport, ...]) -> None:
    """执行结果**逐条**打出来：哪个账号删掉了哪些行、墓碑新写了多少、导出物删没删掉。"""
    print("\n已执行：")
    for r in reports:
        if r.account:      # 空串 = 索引侧无事可做（同 _render_chat_plan），不印空的账号块
            print(f"  账号 {r.account}")
            for key, label, _ in _CHAT_TABLES:
                print(f"    {label:<12} {r.deleted.get(key, 0):>7,} 行已删除")
        # tombstone 是**本次真正新写进去的**条数（重跑同一个会话时为 0，幂等，spec §5.7）：
        # 「该会话当前被挡住多少条」是核查那一侧的事，这里混着说会让重跑看起来像又挡了一批。
        print(f"    本次新写入排除：{r.tombstone:,} 条")
        # 这一行是「删了但没删掉」的**唯一**出口（文件系统上看不出别的东西）：不说出来，
        # 报告就只剩一片 ✅，而东西还在。退出码也读它（见 _cmd_purge_chat）。
        print("    导出物已删除" if r.export_removed
              else "    ❌ 导出物未能删除（那个名字还在磁盘上）")


def _cmd_purge_excluded() -> int:
    """列出排除清单的规模（按会话分组）。**不打印会话名**——那要查索引，而这条命令可能在
    `purge --index` 之后跑、索引已经不在了（spec §3）。"""
    from . import exclusions
    try:
        with exclusions.standalone() as con:
            rows = exclusions.by_chat(con)
    except (sqlite3.Error, OSError) as exc:
        # 读不出来**不能**报「0 条」：那是「一条都没被挡」的意思，与事实相反，而用户正是
        # 据这句话判断重导会不会把已删内容带回来。
        raise errors.PurgeRefused(
            f"排除清单读不出来：{exc}",
            hint=f"检查 {exclusions.path()} 是否可读、是否损坏"
        ) from exc
    total = sum(n for _, n in rows)
    print(f"排除清单（{exclusions.path().resolve()}）：{total:,} 条")
    for md5, n in rows:
        print(f"  {md5}   {n:,} 条")
    return 0


def _cmd_purge_chat(args: argparse.Namespace) -> int:
    from . import purge, store, usecases
    targets = usecases.purge_chat_targets(args.chat, account=args.account)
    if isinstance(targets, usecases.NeedConfirm):
        return _need_confirm(args.chat, targets)
    if not targets:
        # 契约：`purge_chat_targets` 对「索引里一条消息都没有」的会话也会回**一条**目标
        # （账号为空串；导出物只取决于 chat_md5，与账号无关）。走到这里说明那条契约破了。
        # **不许退回「stderr 一句 + 退 0」**——那正是本轮修掉的缺陷形态：索引已重建、
        # `media/<md5>/` 还在盘上时，命令报「没有可删的东西」，而解密出的图/语音/HTML
        # 原封不动，还没有一个字说破。
        raise errors.PurgeRefused(
            f"解析不出会话「{args.chat}」的销毁目标",
            hint="索引与排除清单里都没有它——这不是预期形态，请报告这个形状")
    if args.undo:
        return _cmd_purge_chat_undo(targets, yes=args.yes)
    if not store.DB_PATH.exists():
        # 索引库不在（`purge --index` 之后）+ 有墓碑 → targets 是「只剩墓碑」的那些账号。
        # 删除这条路会让 `plan_chat` 的 `store.open()` 把刚删掉的索引连 schema 建回来；
        # 而活消息一条都没有，本来就没有东西可删——说清楚，别默默重建。
        print(f"索引库不在（{store.DB_PATH}）——没有活消息可删；"
              "要解除排除请加 --undo。", file=sys.stderr)
        # **导出物与索引库无关**：它落在 `media/<会话md5>/` 下，索引库不在并不会让它消失。
        # 不说这句，用户会把「没有活消息可删」读成「这个会话在本工具里什么都不剩了」——
        # 而那份解密后的图/视频/语音（外加可直接打开的 HTML）还摊在盘上（M-3）。
        # 判据走 purge 的那一个 `name_there`：不跟随符号链接、也不把 EACCES 吞成「不在」
        # （CLI 不另写一套存活判据，同 M-4 的理由）；它抛 `OSError` 时什么都不说——
        # 这句提示不该把命令本身弄成失败。
        # 目录同样走 purge 的那一个 `chat_export_dir`：导出根与会话 md5 的单一来源都在 core
        # （CLI 自己拼一遍就是同一件事的第二个说法，改了一处漏一处的老毛病）。
        left = []
        for t in targets:
            d = purge.chat_export_dir(t["chat_id"])
            try:
                if purge.name_there(d):
                    left.append(d)
            except OSError:
                continue
        if left:
            dirs = "、".join(str(p) for p in dict.fromkeys(left))
            print(f"该会话的导出物仍在盘上：{dirs}——索引库不在，本命令不会动它；"
                  "要删它得先重跑 export 把索引建回来，或手工删除。", file=sys.stderr)
        return 0
    plan = purge.plan_chat(targets)
    _render_chat_plan(plan, args.chat)
    if not args.yes:
        print("\n未删除任何文件。加 --yes 执行。")
        return 0
    reports = purge.execute_chat(plan)
    _render_chat_reports(reports)
    # 汇总。导出物**按目录去重**（见 _chat_export_dirs），且只数真删掉的那些——失败的那些
    # 仍占着磁盘，算进「已释放」就是谎报。索引那句必须点明文件不会变小（spec §5.7）：
    # vec0 只在整块清空时才回收空间，要真回收得 `purge --index` 或重跑 export。
    freed = {e.export_dir: e.export_size
             for e, r in zip(plan.entries, reports) if r.export_removed}
    print(f"\n导出物 {_human_size(sum(freed.values()))} 已释放；"
          f"索引内 {sum(sum(r.deleted.values()) for r in reports):,} 行已删除"
          "（**索引文件不会因此变小**，要回收空间得 purge --index 或重跑 export）")
    res = purge.verify_chat(plan)
    print("\n核查：")
    print(res.render())
    # 判据从「报告」与「核查」两侧各取一份。**今天前两项与后两项互为冗余**（实测：摘掉
    # `not res.export_still_there` 没有任何测试会红），不是四条独立判据——别按「四处缺一不可」
    # 记账。两份都留着是因为它们回答的是不同的问题：`export_removed` 是「这一条报了什么」，
    # `export_still_there` 是「核查看见了什么」，而 memo ③ 要的是「报告说没删掉 ⇒ 不许退 0」
    # 这条通道独立存在（它的判据是注入式的那条测试）。
    ok = (all(r.export_removed for r in reports) and res.tombstone_ok
          and not res.leftover and not res.export_still_there)
    return 0 if ok else 1


def _cmd_purge_chat_undo(targets: list[dict], *, yes: bool) -> int:
    """撤销：从排除清单里移出该会话。**只删墓碑，不恢复数据**——这句话必须印在输出里。

    没有它，`--undo` 会被读成「恢复」：用户撤销后重跑 `export --full` 发现消息没回来，
    会以为工具坏了（而它按设计工作：撤销只是放开拦截，spec §5.6）。

    业务规则（逐账号数、逐账号删、commit）在 core 的 `purge.undo_chat`：它此前整个住在这里，
    而 plan/execute/verify 都由 `purge` 拥有——同一族的第四件事放在入口层，别的入口就复用它
    不到，且撤销是**唯一会写排除清单**的动作，更该与清单的读写端（`exclusions`）挨着。
    本函数只剩渲染与退出码。
    """
    from . import purge
    per = purge.undo_chat(targets, apply=yes)
    n = sum(e.count for e in per)
    print(f"将解除排除：{n} 条")
    for e in per:
        if e.account:      # 空串 = 索引里没有它、清单里也没有（同 _render_chat_plan）
            print(f"  {e.account}  {e.chat_md5}  {e.count} 条")
    print("⚠️ 这**不会**把数据找回来——被删的消息要重跑 `export --full` 才回来"
          "（那时它们不再被排除）。")
    if not yes:
        print("\n未做任何改动。加 --yes 执行。")
        return 0
    print(f"已解除 {n} 条排除。")
    return 0


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
    sp.add_argument("--out", help="输出路径（相对路径存于 ~/.agent-wechat/ 下；绝对路径按原样写入）；文件权限 0600；省略则做 quick_check")
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
    sp.add_argument("--media", action="store_true",
                    help="媒体与聊天导出（HOME_DIR/media：解密后的图片/视频/语音，及 HTML/MD）")
    sp.add_argument("--moments", action="store_true", help="朋友圈导出（HOME_DIR/moments）")
    sp.add_argument("--tmp", action="store_true", help="解密临时目录")
    sp.add_argument("--all", action="store_true",
                    help="整个 ~/.agent-wechat（含 models/ 与散落文件），与具名范围互斥")
    sp.add_argument("--chat", metavar="会话",
                    help="按会话销毁（username 或展示名），与具名范围互斥")
    sp.add_argument("--undo", action="store_true",
                    help="从排除清单里移出该会话（**不恢复数据**，要重跑 export --full）")
    sp.add_argument("--excluded", action="store_true", help="列出排除清单的规模")
    sp.add_argument("--account", help="解析会话名用的账号（省略＝当前登录的那个）")
    sp.add_argument("--yes", action="store_true", help="真正执行删除")
    sp.set_defaults(fn=cmd_purge)

    sp = sub.add_parser("media", help="媒体导出（图片/语音/视频）")
    media_sub = sp.add_subparsers(dest="media_cmd", required=True)
    msp = media_sub.add_parser("export", help="导出某会话的媒体到本地目录")
    msp.add_argument("--chat", required=True, help="会话（名字或 username）")
    msp.add_argument("--kinds", default="image,voice,video",
                     help="逗号分隔：image,voice,video,sticker"
                          "（默认前三项；sticker 需配 --with-cdn 才取得到本体）")
    msp.add_argument("--months", default=None, help="逗号分隔的 YYYY-MM（默认不限）")
    msp.add_argument("--out", default=None, help="输出目录（默认 ~/.agent-wechat/media/）")
    msp.add_argument("--limit", type=int, default=2000, help="本次最多新增文件数（已存在的不计）")
    msp.add_argument("--force", action="store_true",
                     help="忽略已存在的产物，全部重做（默认跳过）")
    msp.add_argument("--with-cdn", action="store_true",
                     help="贴纸改从微信 CDN 取件（**默认关**：不加则一次网络请求都不发）。"
                          "**会把 filekey 以明文 http 发往腾讯 CDN**，见 SECURITY.md")
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
    esp.add_argument("--chat", help="按作者筛选（名字或 wxid；省略=不限作者，含自己的动态）")
    esp.add_argument("--months", help="逗号分隔的 YYYY-MM，限定月份")
    esp.add_argument("--with-media", action="store_true",
                     help="联网下载远程媒体本体（默认关闭，不联网；开启后本机媒体总量约 1.5 GiB，"
                          "其中本期新增约 0.62 GiB）")
    esp.add_argument("--out", help="输出目录（默认 $AGENT_WECHAT_HOME/moments）")
    esp.add_argument("--limit", type=int, default=2000, help="最多导出动态数（下界 1，无上界）")
    esp.add_argument("--account", help="账号目录名")
    esp.set_defaults(fn=cmd_moments_export)

    sp = sub.add_parser("chat", help="会话时间线导出（HTML / Markdown）")
    csp = sp.add_subparsers(dest="chat_cmd", required=True)
    esp = csp.add_parser("export", help="把某会话导成可读时间线（含媒体与引用）")
    esp.add_argument("--chat", required=True, help="会话（名字或 username）")
    esp.add_argument("--months", default=None, help="逗号分隔的 YYYY-MM（默认不限）")
    esp.add_argument("--limit", type=int, default=500,
                     help="最多导出范围内最近 N 条（默认 500）")
    esp.add_argument("--format", default="html,md", help="逗号分隔，html / md（默认两者）")
    # --out 必须与 media export 同根：产物在 {--out}/{chat_md5}/chat/ 下，媒体以相对路径
    # 引用它**同级**的既有媒体树（{--out}/{chat_md5}/image|voice|video/…）。
    esp.add_argument("--out", default=None,
                     help="输出根（默认 $AGENT_WECHAT_HOME/media，与 media export 同根）")
    esp.add_argument("--account")
    esp.set_defaults(fn=cmd_chat_export)

    isp = csp.add_parser("index", help="列出已导出的会话，生成 {--out}/index.html 索引页")
    # 与 `chat export` 同根：索引页链到的是**同一个 --out** 下的会话产物。
    isp.add_argument("--out", default=None,
                     help="输出根（默认 $AGENT_WECHAT_HOME/media，与 chat/media export 同根）")
    isp.add_argument("--account")
    isp.set_defaults(fn=cmd_chat_index)

    sp = sub.add_parser("appmsg", help="appmsg 原始载荷（引用/链接/文件/转账…）")
    asp = sp.add_subparsers(dest="appmsg_cmd", required=True)
    rsp = asp.add_parser("rebuild", help="全量重扫微信库回填 msg_parts")
    rsp.add_argument("--account", default=None)
    rsp.set_defaults(fn=cmd_appmsg_rebuild)

    sp = sub.add_parser("extras", help="位置（48）/ 名片（42）的原始载荷")
    esp = sp.add_subparsers(dest="extras_cmd", required=True)
    xsp = esp.add_parser("rebuild", help="全量重扫微信库回填 msg_extras")
    xsp.add_argument("--account", default=None)
    xsp.set_defaults(fn=cmd_extras_rebuild)

    args = p.parse_args(argv)
    if args.cmd == "purge":
        # 名单**从产物表演绎**，不再手写第二遍：这里原是一份三元素的手写清单，加上
        # --media / --moments 之后它会判「没指定范围」——参数明明给了却报用法错。
        # 「范围名」在本模块此前有三处枚举（purge 那张表、cmd_purge 的 scopes 元组、这里），
        # 三份要手工同步的东西就是漂移的温床；现在只剩那张表一处。
        from . import purge as _purge
        names = [a.scope for a in _purge.artifacts() if a.scope]
        flags = " / ".join(f"--{s}" for s in names)
        named = any(getattr(args, s) for s in names)     # 开关缺失就是 AttributeError，响亮
        # 三条轴各自独立：`--excluded` 是全局查询、`--chat` 问「删哪个会话」、具名范围与
        # `--all` 问「删哪一类文件」。混着给一律退 2——`purge --chat X --media` 若被接受，
        # 用户以为只删了那个会话，实际把整棵 media/ 清空了（不可逆）。
        if args.excluded:
            # `--yes` 也是「其它 flag」（spec §3 的字面要求：与其它 flag 同给时退 2）：
            # 这条命令一次查询、什么都不改，带 `--yes` 只说明用户以为它会动手——那种误解
            # 必须当场停住，而不是安静地忽略一个「执行」开关（M-2）。
            if args.chat or args.undo or named or args.all or args.yes:
                p.error("--excluded 是全局查询，不能与其它参数同用")
        elif args.undo and not args.chat:
            # `--undo` 只对会话有意义。少了这一条，`purge --undo --media --yes` 会被当成
            # 一条普通的范围删除跑掉——`--undo` 毫无效果，而整棵 media/ 没了（不可逆）。
            p.error("--undo 只与 --chat 同用：它问的是「解除哪个会话的排除」，不是「删哪一类文件」")
        elif args.chat:
            if named or args.all:
                p.error("--chat 与具名范围/--all 互斥：它问的是「删哪个会话」，不是「删哪一类文件」")
        elif args.all and named:
            p.error(f"--all 不能与具名范围同时使用：{flags}")
        elif not args.all and not named:
            p.error(f"必须指定至少一个范围：{flags} / --all")
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
        # EA-096：core 的 hint 只给中立动作；入口专属的命令名在此按**异常类型**追加
        # （与 ContactNotFound 的后缀同一处约定），保证印出来的命令在本入口可直接执行。
        text = errors.to_text(exc)
        if isinstance(exc, errors.IndexUnavailable):
            text += "（CLI：`agent-wechat export`）"
        elif isinstance(exc, errors.VectorIndexEmpty):
            text += "（CLI：`agent-wechat vector-build`）"
        print(text, file=sys.stderr)
        code = 1
    raise SystemExit(code)
