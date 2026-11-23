"""Phase 01：Rust(PyO3) 对照实验——导出热路径里的逐行解码。

    uvx maturin build --release -m spike/phase01/native_decode/Cargo.toml -o spike/phase01/native_decode/dist
    uv run --with spike/phase01/native_decode/dist/<wheel> python spike/phase01/bench_native.py

数据：账号全部消息分片里的全部行（`decrypt.open_db`：进程内 SQLCipher 直读，不落明文）。每种实现在**独立子进程**
里跑，峰值内存只算「装载数据之后」的增量。只打印计数 / 耗时 / 内存，不打印正文。

被比较的实现：
  py        项目自己的 `reader._decode_content` + `_decode_row` 群前缀逻辑（逐字照抄的参考实现）
  rs_one    Rust，每行一次跨语言调用（decode_one）+ Python 侧群前缀
  rs_batch  Rust，每张表一次调用（decode_rows），解码与群前缀都在 Rust
  noop_*    FFI 地板：同样的数据交给原生函数、什么都不做
"""
from __future__ import annotations

import json
import os
import pathlib
import random
import resource
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
TYPE_TEXT = 1


def _rss() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20


def load_rows() -> list[tuple[bool, list[tuple]]]:
    from agent_wechat import account, decrypt, keys, paths, reader
    acct = account.resolve_account(None)
    r = reader.MessageReader(acct)
    md5map = r.md5_map()
    pp = keys.get_passphrase(acct)
    out = []
    for shard in paths.message_shards(acct):
        with decrypt.open_db(shard, pp) as con:
            con.text_factory = bytes
            tables = [t[0].decode() for t in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
            for tb in tables:
                chat = md5map.get(tb[4:], "")
                rows = con.execute(f'SELECT local_type, message_content, WCDB_CT_message_content '
                                   f'FROM "{tb}" ORDER BY local_id').fetchall()
                out.append((chat.endswith("@chatroom"), rows))
    return out


def py_kernel(tables):
    from agent_wechat.reader import _decode_content
    res = []
    for is_room, rows in tables:
        part = []
        for ltype, mc, ct in rows:
            content = _decode_content(mc, ct)
            t = ltype & 0xffffffff
            sender = None
            if is_room and content:
                head, sep, rest = content.partition(":\n")
                if sep and head and "\n" not in head:
                    if t == TYPE_TEXT:
                        sender, content = head, rest
                    elif head.endswith("@chatroom"):
                        content = rest
            part.append((content, sender))
        res.append(part)
    return res


def rs_one(tables):
    import native_decode as nd
    res = []
    for is_room, rows in tables:
        part = []
        for ltype, mc, ct in rows:
            content = nd.decode_one(mc, ct)
            t = ltype & 0xffffffff
            sender = None
            if is_room and content:
                head, sep, rest = content.partition(":\n")
                if sep and head and "\n" not in head:
                    if t == TYPE_TEXT:
                        sender, content = head, rest
                    elif head.endswith("@chatroom"):
                        content = rest
            part.append((content, sender))
        res.append(part)
    return res


def rs_batch(tables):
    import native_decode as nd
    return [nd.decode_rows(rows, is_room) for is_room, rows in tables]


def noop_one(tables):
    import native_decode as nd
    return [[nd.noop_one(mc) for _, mc, _ in rows] for _, rows in tables]


def noop_batch(tables):
    import native_decode as nd
    return [nd.noop_rows(rows) for _, rows in tables]


IMPLS = {"py": py_kernel, "rs_one": rs_one, "rs_batch": rs_batch,
         "noop_one": noop_one, "noop_batch": noop_batch}


def child(name: str) -> dict:
    t = time.perf_counter(); tables = load_rows(); t_load = time.perf_counter() - t
    rss0 = _rss()
    n = sum(len(r) for _, r in tables)
    fn = IMPLS[name]
    best = 9e9
    for _ in range(3):
        t = time.perf_counter(); out = fn(tables); best = min(best, time.perf_counter() - t)
    res = {"rows": n, "load_s": round(t_load, 2), "best_s": round(best, 3),
           "ns_per_row": round(best / n * 1e9, 1), "rss_load_mb": round(rss0, 1),
           "rss_extra_mb": round(_rss() - rss0, 1)}
    if name in ("py", "rs_batch", "rs_one"):
        import hashlib
        h = hashlib.sha256()
        for part in out:
            for c, s in part:
                h.update(repr((c, s)).encode())
        res["digest"] = h.hexdigest()[:16]
    return res


def fuzz(n: int = 200_000) -> dict:
    """边界语义：随机字节 / 非法 UTF-8 / 截断与拼接的 zstd 帧，两边必须同值或同样报错。"""
    import zstandard
    import native_decode as nd
    from agent_wechat import errors
    from agent_wechat.reader import _decode_content
    rnd = random.Random(0)
    cctx = zstandard.ZstdCompressor()
    nasty = [b"\xff", b"\xc3", b"\xe4\xb8", b"\xed\xa0\x80", b"\xf0\x9f\x98", b"\xf4\x90\x80\x80", b"\x80"]
    diff_val = diff_err = 0
    kinds = {"utf8": 0, "zstd_ok": 0, "zstd_trunc": 0, "zstd_multi": 0, "zstd_garbage": 0}
    for i in range(n):
        k = rnd.choice(list(kinds))
        kinds[k] += 1
        txt = "".join(rnd.choice("ab中文😀:\n@") for _ in range(rnd.randint(0, 40))).encode()
        txt = b"".join(rnd.choice([txt[:5], txt[5:], rnd.choice(nasty)]) for _ in range(3))
        if k == "utf8":
            raw, ct = txt, 0
        elif k == "zstd_ok":
            raw, ct = cctx.compress(txt), 4
        elif k == "zstd_trunc":
            f = cctx.compress(txt); raw, ct = f[:rnd.randint(1, max(1, len(f) - 1))], 4
        elif k == "zstd_multi":
            raw, ct = cctx.compress(txt) + cctx.compress(b"tail"), 4
        else:
            raw, ct = os.urandom(rnd.randint(1, 64)), 4
        try:
            a, ea = _decode_content(raw, ct), None
        except errors.DecryptError:
            a, ea = None, "err"
        try:
            b, eb = nd.decode_one(raw, ct), None
        except ValueError:
            b, eb = None, "err"
        if ea != eb:
            diff_err += 1
        elif a != b:
            diff_val += 1
    return {"cases": n, "kinds": kinds, "value_mismatch": diff_val, "error_mismatch": diff_err}


def main() -> None:
    if sys.argv[1:2] == ["_child"]:
        print(json.dumps(child(sys.argv[2]))); return
    if sys.argv[1:2] == ["_fuzz"]:
        print(json.dumps(fuzz())); return
    results = {}
    for name in IMPLS:
        r = subprocess.run([sys.executable, __file__, "_child", name], capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"{name} 失败：{r.stderr[-1500:]}")
        results[name] = json.loads(r.stdout.strip().splitlines()[-1])
        x = results[name]
        print(f"  {name:10s} {x['best_s']:7.3f}s  {x['ns_per_row']:7.1f} ns/行  额外峰值内存 "
              f"{x['rss_extra_mb']:6.1f} MB  {x.get('digest', '')}", flush=True)
    rows = results["py"]["rows"]
    same = results["py"]["digest"] == results["rs_batch"]["digest"] == results["rs_one"]["digest"]
    print(f"  {rows:,} 行（装载 {results['py']['load_s']}s，装载后 RSS {results['py']['rss_load_mb']} MB）；"
          f"三种实现输出{'逐条一致' if same else '不一致'}")
    r = subprocess.run([sys.executable, __file__, "_fuzz"], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"fuzz 失败：{r.stderr[-1500:]}")
    fz = json.loads(r.stdout.strip().splitlines()[-1])
    print(f"  边界模糊测试 {fz['cases']:,} 例 {fz['kinds']}：值不一致 {fz['value_mismatch']}，"
          f"报错行为不一致 {fz['error_mismatch']}")
    out = HERE / "results" / "native.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"impls": results, "same_output": same, "fuzz": fz}, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
