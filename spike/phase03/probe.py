"""Phase 03 探针：在**被测版本自己的 venv** 里运行（改动前 / 改动后各用各的解释器）。

与版本无关：只用两个版本都有的接口（`paths.db_storage`、`cli.main`、`usecases.recent_messages`、
`vector.build_index` / `_SELECT_PAGE`）。不打印任何正文——stdout 只给编排器算哈希。

    probe.py cli <agent-wechat 参数…>          # 跑一条真实 CLI 命令
    probe.py warm <chats.json> <repeat> <threads>  # 常驻进程里反复调用 recent_messages（MCP 形态）
    probe.py vector <mod>                       # 只嵌入 id % mod == 0 的均匀抽样，rebuild
    probe.py import <module>                    # 导入耗时
"""
from __future__ import annotations

import json
import os
import pathlib
import resource
import sys
import threading
import time


def _freeze() -> None:
    """两个版本读**同一份冻结源**（APFS 克隆）：微信在跑，不冻结就比不了产物。"""
    from agent_wechat import paths
    frozen = os.environ.get("AW_FROZEN")
    if frozen:
        paths.db_storage = lambda account: pathlib.Path(frozen)   # type: ignore[assignment]


def _rss_mb() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 1)


def cmd_cli(argv: list[str]) -> int:
    _freeze()
    from agent_wechat import main
    sys.argv = ["agent-wechat", *argv]
    try:
        main()
    except SystemExit as e:
        return int(e.code or 0)
    return 0


def cmd_warm(chats: list[str], repeat: int, threads: int) -> None:
    _freeze()
    from agent_wechat import usecases
    lat: list[float] = []
    errs: list[str] = []
    lock = threading.Lock()

    def work() -> None:
        for _ in range(repeat):
            for c in chats:
                t = time.perf_counter()
                try:
                    usecases.recent_messages(c, limit=50)
                except Exception as exc:                       # noqa: BLE001 — 稳定性统计
                    with lock:
                        errs.append(type(exc).__name__)
                with lock:
                    lat.append(time.perf_counter() - t)
    t0 = time.perf_counter()
    ts = [threading.Thread(target=work) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    s = sorted(lat)
    print(json.dumps({"calls": len(lat), "errors": errs, "wall_s": round(time.perf_counter() - t0, 2),
                      "first_s": round(lat[0], 3), "p50_s": round(s[len(s) // 2], 3),
                      "p90_s": round(s[int(len(s) * .9)], 3), "max_s": round(s[-1], 3),
                      "rss_mb": _rss_mb()}), file=sys.stderr)


def cmd_vector(mod: int) -> None:
    _freeze()
    from agent_wechat import paths, vector
    paths._tm_exclusion_checked = True
    vector._SELECT_PAGE = vector._SELECT_PAGE.replace("AND id > ?", f"AND id > ? AND id % {mod} = 0")
    t = time.perf_counter()
    r = vector.build_index(rebuild=True)
    dt = time.perf_counter() - t
    n = r["embedded_this_run"]
    print(json.dumps({"embedded": n, "s": round(dt, 1), "rate": round(n / dt, 1), "rss_mb": _rss_mb(),
                      "infer_batch": getattr(vector, "EMBED_BATCH", vector.BATCH)}), file=sys.stderr)


def cmd_import(mod: str) -> None:
    t = time.perf_counter()
    __import__(mod)
    print(json.dumps({"import_s": round(time.perf_counter() - t, 3)}), file=sys.stderr)


def main() -> int:
    sub, rest = sys.argv[1], sys.argv[2:]
    if sub == "cli":
        return cmd_cli(rest)
    if sub == "warm":
        cmd_warm(json.loads(pathlib.Path(rest[0]).read_text()), int(rest[1]), int(rest[2]))
    elif sub == "vector":
        cmd_vector(int(rest[0]))
    elif sub == "import":
        cmd_import(rest[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
