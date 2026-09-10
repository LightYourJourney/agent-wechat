"""导出管道：流式、幂等、断点续传；附长任务注册表（MCP 后台执行用）。"""
from __future__ import annotations

import json
import threading
import time
import uuid

from . import paths, reader, store

TASKS_FILE = paths.HOME_DIR / "tasks.json"
_tasks: dict[str, dict] = {}
_lock = threading.Lock()


def _persist() -> None:
    paths.HOME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    TASKS_FILE.write_text(json.dumps(_tasks, ensure_ascii=False, indent=1))


def start_task(account: str, chat_id: str | None, full: bool) -> str:
    task_id = uuid.uuid4().hex[:12]
    with _lock:
        _tasks[task_id] = {"id": task_id, "kind": "export", "account": account,
                           "chat_id": chat_id, "status": "running", "rows": 0,
                           "started_at": int(time.time()), "error": None}
        _persist()
    t = threading.Thread(target=_run_task, args=(task_id, account, chat_id, full), daemon=True)
    t.start()
    return task_id


def _run_task(task_id: str, account: str, chat_id: str | None, full: bool) -> None:
    try:
        n = run_export(account, chat_id=chat_id, full=full)
        with _lock:
            _tasks[task_id].update(status="done", rows=n, finished_at=int(time.time()))
            _persist()
    except Exception as e:  # 任务失败不能崩掉 MCP 进程
        with _lock:
            _tasks[task_id].update(status="failed", error=str(e)[:500],
                                   finished_at=int(time.time()))
            _persist()


def task_status(task_id: str | None = None) -> dict | list:
    with _lock:
        if task_id:
            return _tasks.get(task_id) or {"error": f"未知任务 {task_id}"}
        return sorted(_tasks.values(), key=lambda t: t["started_at"], reverse=True)[:10]


def run_export(acct: str, chat_id: str | None = None, full: bool = False,
               progress=None) -> int:
    """全量/增量导出。恒定内存：reader 流式产批 → store 批量幂等 upsert。"""
    r = reader.MessageReader(acct)
    total = 0
    with store.connect() as con:
        if full and not chat_id:
            con.execute("DELETE FROM watermark")
            con.commit()
        resume = {} if full else None
        if resume is None:
            resume = {md5: mx for md5, mx in con.execute(
                "SELECT chat_md5, max(max_local_id) FROM watermark WHERE account=? GROUP BY chat_md5",
                (acct,))}
        for batch in r.iter_all(chat_id=chat_id, resume=resume):
            store.upsert_batch(con, batch)
            total += len(batch)
            if progress and total % 20_000 < store.BATCH:
                progress(total)
    return total
