"""agent-wechat CLI。MCP Server 是这里同一批核心函数的另一个入口。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from . import account, bootstrap, decrypt, keys, paths


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


def cmd_bootstrap(args: argparse.Namespace) -> int:
    bare = bootstrap.bootstrap(timeout=args.timeout)
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
    from . import reader
    r = reader.MessageReader(account.resolve_account(args.account))
    for c in r.contacts(query=args.query or None):
        print(f"{c['username']:<32} {c['kind']:<9} {c['display']}")
    return 0


def cmd_recent(args: argparse.Namespace) -> int:
    import json as _json
    from . import reader
    r = reader.MessageReader(account.resolve_account(args.account))
    chat_id = args.contact if _is_id(args.contact) else _one(r.resolve_chat(args.contact))
    if not chat_id:
        print(f"无法唯一解析「{args.contact}」；候选: "
              + _json.dumps([{"username": c["username"], "display": c["display"]}
                             for c in r.resolve_chat(args.contact)[:10]], ensure_ascii=False))
        return 1
    for m in r.recent(chat_id, limit=args.n):
        who = "我" if m.is_self else (m.sender_name or m.sender_id or "?")
        body = m.content or m.media_hint or ""
        print(f"[{m.ts}] {who}: {body[:200]}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from . import export, store
    acct = account.resolve_account(args.account)
    chat_id = None
    if args.contact:
        from . import reader
        r = reader.MessageReader(acct)
        chat_id = args.contact if _is_id(args.contact) else _one(r.resolve_chat(args.contact))
        if not chat_id:
            print(f"无法唯一解析「{args.contact}」", file=sys.stderr)
            return 1

    def progress(n: int) -> None:
        print(f"  已写入 {n} 行…", flush=True)

    n = export.run_export(acct, chat_id=chat_id, full=args.full, progress=progress)
    with store.connect() as con:
        print(f"完成：本轮 {n} 行；索引总量 {store.counts(con)}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    import json as _json
    from . import search
    res = search.search(args.query, chat_id=args.contact, limit=args.n)
    print(f"mode={res['mode']} total={res['total']}")
    for h in res["hits"]:
        print(f"  [{h['ts']}] {h['chat_id']} {h['sender_name'] or h['sender_id'] or '?'}: "
              f"{(h['content'] or '')[:120]}")
    return 0


def _is_id(s: str) -> bool:
    return s.endswith("@chatroom") or s.startswith(("wxid_", "gh_", "filehelper"))


def _one(cands) -> str | None:
    return cands[0]["username"] if len(cands) == 1 else None


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
    sp.add_argument("-n", type=int, default=50)
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

    args = p.parse_args(argv)
    raise SystemExit(args.fn(args))
