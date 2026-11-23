"""Phase 03 性能 / 正确性 / 稳定性验收编排器。

    python3 spike/phase03/acceptance.py --base-python <改动前 venv 的 python> --cur-python .venv/bin/python

三个配置：
  base     改动前（97b78dc，`git archive` 出来的独立树 + 自己的 venv）
  cur_cli  改动后，但 AGENT_WECHAT_DECRYPT=cli——只含纯 Python 改动（tmutil、嵌入批），解密仍走 CLI
  cur      改动后默认（进程内 SQLCipher）
cur_cli 与 cur 的差就是**原生扩展本身**的收益；base 与 cur_cli 的差是纯 Python 改动的收益。

所有命令都是独立子进程、套 `/usr/bin/time -l`（墙钟 / user / sys / 峰值 RSS，含子进程）；
另起一个线程每 5 ms 采样 `HOME/tmp` 的占用与其中是否出现明文 `plain.db`。
读的是同一份 APFS 克隆的冻结源。不记录任何会话标识与正文，产物与隔离目录结束即删。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import signal
import sqlite3
import statistics
import subprocess
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
PROBE = HERE / "probe.py"
WORK = pathlib.Path.home() / ".aw-p3" / "work"
REAL_HOME = pathlib.Path.home() / ".agent-wechat"
CFGS = ("base", "cur_cli", "cur")


class TmpWatch(threading.Thread):
    """采样 HOME/tmp：峰值字节数、是否见过明文 plain.db 及其最大体积。"""

    def __init__(self, home: pathlib.Path) -> None:
        super().__init__(daemon=True)
        self.tmp, self.stop, self.peak, self.plain_peak = home / "tmp", False, 0, 0

    def run(self) -> None:
        while not self.stop:
            total = plain = 0
            try:
                for root, _dirs, files in os.walk(self.tmp):
                    for f in files:
                        try:
                            sz = os.lstat(os.path.join(root, f)).st_size
                        except OSError:
                            continue
                        total += sz
                        if f == "plain.db":
                            plain += sz
            except OSError:
                pass
            self.peak, self.plain_peak = max(self.peak, total), max(self.plain_peak, plain)
            time.sleep(0.005)


def _time_l(stderr: str) -> dict:
    m = re.search(r"([\d.]+) real\s+([\d.]+) user\s+([\d.]+) sys", stderr)
    rss = re.search(r"(\d+)\s+maximum resident set size", stderr)
    return {"real": float(m.group(1)), "user": float(m.group(2)), "sys": float(m.group(3)),
            "rss_mb": round(int(rss.group(1)) / 2**20, 1)} if m and rss else {}


def run(py: str, args: list[str], *, cfg: str, home: pathlib.Path, path: str | None = None,
        timeout: float = 3600) -> dict:
    env = {**os.environ, "AGENT_WECHAT_HOME": str(home), "AW_FROZEN": str(WORK / "frozen")}
    env.pop("AGENT_WECHAT_DECRYPT", None)
    if cfg == "cur_cli":
        env["AGENT_WECHAT_DECRYPT"] = "cli"
    if path is not None:
        env["PATH"] = path
    w = TmpWatch(home)
    w.start()
    r = subprocess.run(["/usr/bin/time", "-l", py, str(PROBE), *args], capture_output=True,
                       text=True, env=env, timeout=timeout)
    w.stop = True
    w.join()
    res = {"rc": r.returncode, "stdout_sha": hashlib.sha256(r.stdout.encode()).hexdigest()[:12],
           **_time_l(r.stderr), "tmp_peak_mb": round(w.peak / 2**20, 1),
           "plain_peak_mb": round(w.plain_peak / 2**20, 1)}
    js = [ln for ln in r.stderr.splitlines() if ln.startswith("{")]
    if js:
        res["probe"] = json.loads(js[-1])
    if r.returncode != 0:
        res["err"] = r.stderr.strip().splitlines()[-1][:200] if r.stderr.strip() else ""
    return res


def med(xs: list[float]) -> float:
    return round(statistics.median(xs), 3)


def fresh_home(name: str) -> pathlib.Path:
    h = WORK / name
    shutil.rmtree(h, ignore_errors=True)
    h.mkdir(mode=0o700, parents=True)
    shutil.copy2(REAL_HOME / "passphrase.json", h / "passphrase.json")
    (h / "models").symlink_to(REAL_HOME / "models")
    return h


def index_digest(home: pathlib.Path) -> dict:
    con = sqlite3.connect(home / "index.db")
    h = hashlib.sha256()
    n = 0
    for row in con.execute("SELECT msg_key, chat_id, ts, msg_type, sender_id, sender_name, is_self, "
                           "content, media_hint FROM messages ORDER BY msg_key"):
        h.update(repr(row).encode()); n += 1
    fts = con.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH '\"的时候\"'").fetchone()[0]
    docs = con.execute("SELECT count(*) FROM messages_fts_docsize").fetchone()[0]
    try:
        con.execute("INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)")
        fts_ok = docs == n
    except sqlite3.Error:
        fts_ok = False
    trig = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    con.close()
    return {"rows": n, "digest": h.hexdigest()[:16], "fts_hits": fts, "fts_consistent": fts_ok,
            "fts_triggers": {"msg_fts_ai", "msg_fts_ad", "msg_fts_au"} <= trig,
            "size_mb": round((home / "index.db").stat().st_size / 2**20, 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-python", required=True)
    ap.add_argument("--cur-python", required=True)
    ap.add_argument("--account", required=True, help="只用于 export 的 --account 参数，不落结果")
    ap.add_argument("--vector-mod", type=int, default=200)
    ap.add_argument("--kill-after", type=float, default=12, help="导出开始后多少秒 SIGKILL")
    ap.add_argument("--only", default="", help="逗号分隔的节名（startup,export,recent_cold,recent_warm,"
                    "contacts,search,vector,kill,no_cli）；省略=全部。结果合并进既有 acceptance.json")
    a = ap.parse_args()
    only = {x for x in a.only.split(",") if x}
    want = lambda sec: not only or sec in only                              # noqa: E731
    PY = {"base": a.base_python, "cur_cli": a.cur_python, "cur": a.cur_python}
    R: dict = {}
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, mode=0o700)
    from_dir = subprocess.run([a.cur_python, "-c", "from agent_wechat import paths, account;"
                               "print(paths.db_storage(account.resolve_account(None)))"],
                              capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["cp", "-cR", from_dir, str(WORK / "frozen")], check=True)
    log = lambda *x: print(*x, flush=True)                                  # noqa: E731

    def _clean(cfg: str) -> pathlib.Path:
        home = WORK / f"clean_{cfg}"
        if not (home / "index.db").exists():
            home = fresh_home(f"clean_{cfg}")
            run(PY[cfg], ["cli", "export", "--account", a.account], cfg=cfg, home=home)
        return home
    try:
        # ---- 1. 启动 ----
        if want("startup"):
            R["startup"] = {}
            for cfg in ("base", "cur"):
                for mod in ("agent_wechat.cli", "agent_wechat.mcp_server"):
                    xs = [run(PY[cfg], ["import", mod], cfg=cfg, home=REAL_HOME)["probe"]["import_s"]
                          for _ in range(5)]
                    R["startup"][f"{cfg}:{mod}"] = med(xs)
                xs = [run(PY[cfg], ["cli", "status"], cfg=cfg, home=REAL_HOME)["real"] for _ in range(3)]
                R["startup"][f"{cfg}:status_wall"] = med(xs)
            log("startup", R["startup"])

        # ---- 2. 全量导出（各自的隔离 HOME）----
        if want("export"):
            R["export"] = {}
            for cfg in CFGS:
                home = fresh_home(f"home_{cfg}")
                r = run(PY[cfg], ["cli", "export", "--account", a.account], cfg=cfg, home=home)
                r["index"] = index_digest(home)
                # 已有索引上再跑：增量（日常 sync 的形态）与 --full（批量 FTS 不该介入、不该变慢）
                inc = run(PY[cfg], ["cli", "export", "--account", a.account], cfg=cfg, home=home)
                full = run(PY[cfg], ["cli", "export", "--full", "--account", a.account], cfg=cfg, home=home)
                r["incremental"] = {k: inc.get(k) for k in ("rc", "real", "user", "sys", "rss_mb")}
                r["full_on_populated"] = {k: full.get(k) for k in ("rc", "real", "user", "sys", "rss_mb")}
                r["index_after_full"] = index_digest(home)
                R["export"][cfg] = r
                log("export", cfg, {k: r.get(k) for k in ("rc", "real", "user", "sys", "rss_mb", "tmp_peak_mb",
                                                          "plain_peak_mb", "index", "incremental",
                                                          "full_on_populated")})
            con = sqlite3.connect(WORK / "home_cur" / "index.db")
            chats = [r[0] for r in con.execute(
                "SELECT chat_id FROM messages GROUP BY chat_id ORDER BY count(*) DESC LIMIT 3")]
            con.close()
            (WORK / "chats.json").write_text(json.dumps(chats))

        # ---- 3. 冷进程读取（真实 HOME：已排除 Time Machine，就是用户的日常形态）----
        if want("recent_cold"):
            R["recent_cold"] = {}
            for cfg in CFGS:
                rows, shas, fails = [], [], 0
                for c in chats:
                    for _ in range(3):
                        r = run(PY[cfg], ["cli", "recent", c, "-n", "20"], cfg=cfg, home=REAL_HOME)
                        fails += r["rc"] != 0
                        rows.append(r); shas.append(r["stdout_sha"])
                R["recent_cold"][cfg] = {
                    "wall_med": med([r["real"] for r in rows]), "wall_max": max(r["real"] for r in rows),
                    "cpu_med": med([r["user"] + r["sys"] for r in rows]), "rss_med": med([r["rss_mb"] for r in rows]),
                    "tmp_peak_mb": max(r["tmp_peak_mb"] for r in rows),
                    "plain_peak_mb": max(r["plain_peak_mb"] for r in rows), "fails": fails,
                    "shas": sorted(set(shas))}
                log("recent_cold", cfg, R["recent_cold"][cfg])
            R["recent_cold"]["outputs_identical"] = (
                R["recent_cold"]["base"]["shas"] == R["recent_cold"]["cur"]["shas"] == R["recent_cold"]["cur_cli"]["shas"])

        # ---- 4. 常驻进程（MCP 形态）：顺序 5 轮 + 8 线程并发 ----
        if want("recent_warm"):
            R["recent_warm"] = {}
            for cfg in CFGS:
                seq = run(PY[cfg], ["warm", str(WORK / "chats.json"), "5", "1"], cfg=cfg, home=REAL_HOME)
                par = run(PY[cfg], ["warm", str(WORK / "chats.json"), "3", "8"], cfg=cfg, home=REAL_HOME)
                R["recent_warm"][cfg] = {"seq": seq.get("probe"), "par8": par.get("probe"),
                                         "par_tmp_peak_mb": par["tmp_peak_mb"], "par_plain_peak_mb": par["plain_peak_mb"]}
                log("recent_warm", cfg, R["recent_warm"][cfg])
            left = [p.name for p in (REAL_HOME / "tmp").iterdir()] if (REAL_HOME / "tmp").is_dir() else []
            R["recent_warm"]["tmp_leftovers"] = len(left)

        # ---- 5. 联系人检索（解密 contact.db）----
        if want("contacts"):
            R["contacts"] = {}
            for cfg in CFGS:
                rs = [run(PY[cfg], ["cli", "contacts", "a"], cfg=cfg, home=REAL_HOME) for _ in range(3)]
                R["contacts"][cfg] = {"wall_med": med([r["real"] for r in rs]), "sha": rs[0]["stdout_sha"]}
            log("contacts", R["contacts"])

        # ---- 6. 关键词检索（各自的索引；检索代码本阶段没动，看有无回退）----
        if want("search"):
            R["search"] = {}
            qs = ["的时候", "明天", "好的", "会议", "哈哈哈"]
            for cfg in ("base", "cur"):
                home = WORK / f"home_{cfg}"
                ws, shas = [], []
                for q in qs:
                    for _ in range(3):
                        r = run(PY[cfg], ["cli", "search", q, "-n", "20", "--account", a.account], cfg=cfg, home=home)
                        ws.append(r["real"]); shas.append(r["stdout_sha"])
                R["search"][cfg] = {"wall_med": med(ws), "wall_max": max(ws), "shas": shas}
            R["search"]["outputs_identical"] = R["search"]["base"]["shas"] == R["search"]["cur"]["shas"]
            for cfg in ("base", "cur"):
                R["search"][cfg].pop("shas")
            log("search", R["search"])

        # ---- 7. 向量构建（均匀抽样）----
        if want("vector"):
            R["vector"] = {}
            for cfg in ("cur", "base"):
                r = run(PY[cfg], ["vector", str(a.vector_mod)], cfg=cfg, home=WORK / f"home_{cfg}", timeout=7200)
                R["vector"][cfg] = {**(r.get("probe") or {}), "cpu_s": round(r.get("user", 0) + r.get("sys", 0), 1),
                                    "time_rss_mb": r.get("rss_mb"), "rc": r["rc"], "err": r.get("err")}
                log("vector", cfg, R["vector"][cfg])

        # ---- 8. 稳定性：导出中途 SIGKILL → 残留里有没有明文；重跑后产物是否与一次跑完的一致 ----
        if want("kill"):
            R["kill"] = {}
            for cfg in ("base", "cur"):
                home = fresh_home(f"kill_{cfg}")
                env = {**os.environ, "AGENT_WECHAT_HOME": str(home), "AW_FROZEN": str(WORK / "frozen")}
                env.pop("AGENT_WECHAT_DECRYPT", None)
                if cfg not in R.get("export", {}):            # 单跑本节：自己做一次干净导出当对照
                    R.setdefault("export", {})[cfg] = {"index": index_digest(_clean(cfg))}
                # 新会话 + killpg：整棵进程树一起死（CLI 后端的 sqlcipher 子进程也在内），模拟崩溃
                p = subprocess.Popen([PY[cfg], str(PROBE), "cli", "export", "--account", a.account], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
                time.sleep(a.kill_after)
                os.killpg(p.pid, signal.SIGKILL); p.wait()
                time.sleep(1)
                left = list((home / "tmp").rglob("*")) if (home / "tmp").is_dir() else []
                plain_mb = round(sum(f.stat().st_size for f in left
                                     if f.name == "plain.db" and f.exists()) / 2**20, 1)
                n_left = len([f for f in left if f.is_file()])
                con = sqlite3.connect(home / "index.db")
                at_kill = {"rows": con.execute("SELECT count(*) FROM messages").fetchone()[0],
                           "fts_rows": con.execute("SELECT count(*) FROM messages_fts_docsize").fetchone()[0],
                           "bulk_marker": con.execute("SELECT count(*) FROM sqlite_master WHERE name="
                                                      "'fts_bulk_pending'").fetchone()[0]
                           and con.execute("SELECT count(*) FROM fts_bulk_pending").fetchone()[0]}
                con.close()
                r2 = run(PY[cfg], ["cli", "export", "--account", a.account], cfg=cfg, home=home)
                left2 = [d for d in (home / "tmp").iterdir()] if (home / "tmp").is_dir() else []
                # 残留在**重跑之前**量：重跑开头的清扫器（EA-019）会把死进程的 tmp 目录删掉
                R["kill"][cfg] = {"at_kill": at_kill, "leftover_files": n_left,
                                  "leftover_plaintext_mb": plain_mb,
                                  "resume_rc": r2["rc"], "resume_index": index_digest(home),
                                  "same_as_clean_export": index_digest(home)["digest"]
                                  == R["export"][cfg]["index"]["digest"],
                                  "tmp_dirs_after_resume": len(left2)}
                log("kill", cfg, R["kill"][cfg])

        # ---- 9. 没有 sqlcipher 命令的 PATH ----
        if want("no_cli"):
            if "chats" not in locals():
                con = sqlite3.connect(_clean("cur") / "index.db")
                chats = [r[0] for r in con.execute(
                    "SELECT chat_id FROM messages GROUP BY chat_id ORDER BY count(*) DESC LIMIT 1")]
                con.close()
            R["no_cli"] = {}
            bare = "/usr/bin:/bin:/usr/sbin:/sbin"
            for cfg in ("base", "cur"):
                r = run(PY[cfg], ["cli", "recent", chats[0], "-n", "5"], cfg=cfg, home=REAL_HOME, path=bare)
                R["no_cli"][cfg] = {"rc": r["rc"], "err": r.get("err", "")[:80]}
            log("no_cli", R["no_cli"])
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    for cfg in CFGS:
        R.get("recent_cold", {}).get(cfg, {}).pop("shas", None)
    out = HERE / "results" / "acceptance.json"
    out.parent.mkdir(exist_ok=True)
    merged = json.loads(out.read_text()) if out.exists() else {}
    if only and "export" not in only:
        R.pop("export", None)                       # 单跑时的对照导出不覆盖完整那份
    merged.update(R)
    out.write_text(json.dumps(merged, indent=1, ensure_ascii=False))
    log("written", out)


if __name__ == "__main__":
    main()
