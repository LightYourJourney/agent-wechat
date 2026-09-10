"""通用后台长任务注册表（MCP 长任务用）：登记、进度、持久化、失败不崩进程。

export 与向量索引共用；任务记录持久化到 ~/.agent-wechat/tasks.json。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Callable

from . import paths

TASKS_FILE = paths.HOME_DIR / "tasks.json"
_tasks: dict[str, dict] = {}
_lock = threading.Lock()


def _persist() -> None:
    paths.HOME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    TASKS_FILE.write_text(json.dumps(_tasks, ensure_ascii=False, indent=1))


def start(kind: str, runner: Callable[[str], int | None], **fields) -> str:
    """登记任务并在后台线程执行 runner(task_id)。runner 返回的行数写入 rows。"""
    task_id = uuid.uuid4().hex[:12]
    with _lock:
        _tasks[task_id] = {"id": task_id, "kind": kind, "status": "running",
                           "rows": 0, "started_at": int(time.time()),
                           "error": None, **fields}
        _persist()
    threading.Thread(target=_run, args=(task_id, runner), daemon=True).start()
    return task_id


def _run(task_id: str, runner: Callable[[str], int | None]) -> None:
    try:
        rows = runner(task_id)
        with _lock:
            _tasks[task_id].update(status="done", finished_at=int(time.time()))
            if rows is not None:
                _tasks[task_id]["rows"] = rows
            _persist()
    except Exception as e:  # 任务失败不能崩掉 MCP 进程
        with _lock:
            _tasks[task_id].update(status="failed", error=str(e)[:500],
                                   finished_at=int(time.time()))
            _persist()


def update(task_id: str, **fields) -> None:
    """就地更新任务字段（供 runner 上报进度）。未知 id 静默忽略。"""
    with _lock:
        if task_id in _tasks:
            _tasks[task_id].update(fields)
            _persist()


def status(task_id: str | None = None) -> dict | list:
    with _lock:
        if task_id:
            return _tasks.get(task_id) or {"error": f"未知任务 {task_id}"}
        return sorted(_tasks.values(), key=lambda t: t["started_at"], reverse=True)[:10]
