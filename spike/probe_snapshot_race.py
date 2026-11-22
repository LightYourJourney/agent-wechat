#!/usr/bin/env python3
"""spike: 快照三元组在微信写入期间会不会拍到不一致的组合（审查 P2-F 的验证探针）。

假设：`decrypt.snapshot` 依次复制 `db` → `-wal` → `-shm`。若复制期间微信做了 checkpoint
（WAL 回写主库并重置），可能拍到「旧主库 + 重置后的新 WAL」——解密失败，或者更糟：
解得开但中间一段消息不在快照里，导出水位线越过它们之后前向扫描再也补不回来。

本探针反复走**生产同一条路径**（`decrypt.decrypted`），只在 `snapshot` 外面包一层，
记录复制前后源文件（主库与 `-wal` 的 size / mtime_ns，及 WAL 头的 salt）是否变了：

  · changed   复制期间源文件变了——竞态窗口确实被踩中（不一定已造成损坏）
  · decrypt   sqlcipher 解密失败（可能是撕裂的页，也可能是别的原因，看 first_error）
  · check     解得开，但 `PRAGMA quick_check` 不是 ok

**只读微信数据**：快照与明文都在 `$AGENT_WECHAT_HOME/tmp/`（0700），用后即删，与日常导出
同一套路径。输出**只有计数**，不打印任何消息内容、账号或路径之外的信息。

用法（微信在前台、最好正在收消息时跑；需要已完成 bootstrap）：

    uv run python spike/probe_snapshot_race.py --iterations 300 --interval 0.2

判读：`changed` 与 `decrypt`/`check` 都是 0 → 本机实测窗口没被踩中，P2-F 可降级为
「理论风险」；`changed > 0` 而另两项为 0 → 窗口存在但 SQLite 自己扛住了；
`decrypt` 或 `check` > 0 → 需要做「检测到变化就重拍」的缓解。
"""
from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys
import time

from agent_wechat import account, decrypt, errors, keys, paths


def _stat(db: pathlib.Path) -> tuple:
    out = []
    for suf in ("", "-wal"):
        p = pathlib.Path(str(db) + suf)
        try:
            st = p.stat()
            out.append((st.st_size, st.st_mtime_ns))
        except FileNotFoundError:
            out.append(None)
    try:
        with open(str(db) + "-wal", "rb") as f:
            out.append(f.read(32)[16:24])          # WAL 头的 salt-1/salt-2：checkpoint 重置后会变
    except OSError:
        out.append(None)
    return tuple(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--account", help="账号目录名；省略取当前登录账号")
    ap.add_argument("--shard", help="分片文件名（如 message_0.db）；省略取编号最大的那个")
    ap.add_argument("--iterations", type=int, default=200)
    ap.add_argument("--interval", type=float, default=0.5, help="两次快照之间的间隔（秒）")
    a = ap.parse_args()

    acct = a.account or account.status().current
    if not acct:
        print("判不出当前登录账号：用 --account 指定", file=sys.stderr)
        return 2
    shards = paths.message_shards(acct)
    if not shards:
        print("该账号没有消息分片", file=sys.stderr)
        return 2
    db = next((s for s in shards if s.name == a.shard), None) if a.shard else shards[-1]
    if db is None:
        print(f"找不到分片 {a.shard}", file=sys.stderr)
        return 2
    pw = keys.get_passphrase(acct)

    counts = {"changed": 0, "decrypt": 0, "check": 0, "ok": 0}
    first_error: str | None = None
    real_snapshot = decrypt.snapshot

    def snapshot(src: pathlib.Path, td: pathlib.Path) -> pathlib.Path:
        before = _stat(src)
        dst = real_snapshot(src, td)
        if _stat(src) != before:
            counts["changed"] += 1
        return dst

    decrypt.snapshot = snapshot
    t0 = time.time()
    for i in range(a.iterations):
        try:
            with decrypt.decrypted(db, pw) as plain:
                con = decrypt.open_plain(plain)
                try:
                    verdict = con.execute("PRAGMA quick_check").fetchone()[0]
                finally:
                    con.close()
            counts["ok" if verdict == "ok" else "check"] += 1
        except errors.AgentWeChatError as exc:
            counts["decrypt"] += 1
            first_error = first_error or f"{type(exc).__name__}: {exc}"
        except sqlite3.DatabaseError as exc:
            # 撕裂的快照**正是要数的那一类**：sqlcipher_export 过了、明文库却是坏的，
            # `quick_check` / 打开时抛裸 DatabaseError（"malformed" / "not a database"）。
            # 不接住的话探针在最该计数的那一刻 traceback 退出，前面的计数全丢。
            counts["check"] += 1
            first_error = first_error or f"{type(exc).__name__}: {exc}"
        if (i + 1) % 20 == 0:
            print(f"[{i + 1}/{a.iterations}] {counts}", flush=True)
        time.sleep(a.interval)

    print(f"\n分片 {db.name}，{a.iterations} 次，用时 {time.time() - t0:.0f}s")
    print(f"复制期间源文件变化 changed={counts['changed']}  解密失败 decrypt={counts['decrypt']}  "
          f"quick_check 不过 check={counts['check']}  正常 ok={counts['ok']}")
    if first_error:
        print(f"首个错误：{first_error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
