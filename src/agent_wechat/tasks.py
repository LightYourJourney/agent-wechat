"""通用后台长任务注册表（MCP 长任务用）：登记、进度、持久化、失败不崩进程。

export 与向量索引共用；任务记录持久化到 ~/.agent-wechat/tasks.json——`start` /
`update` / `_run` 写盘，**首次使用时读回**（进程重启后 get_task_status 仍答得上来）。

`rows` 有意保留两个写入方，语义不同、概不合并：
- `update(..., rows=n)` 写的是运行中的**进度**：本轮已处理行数，单调递增
  （export = 已导出行数、vector = 已嵌入行数）；
- runner 的返回值是**终值**：跑完由 `_run` 覆盖进度值（export = 本轮导出行数、
  vector = `embedded_this_run`）。runner 返回 None 则保留进度值。

任务登记归 usecases 层所有，本模块只提供注册表本身。
"""
from __future__ import annotations

import json
import os
import pathlib
import threading
import time
import uuid
from typing import Callable

from . import errors, paths

# 孤儿任务的失败文案：上次进程退出时还在 running，说明它已经死了。不得新增
# `interrupted` 状态值——TOOLS.md §5 已把枚举发布为 running/done/failed 三值。
ORPHAN_ERROR = "进程中断，任务未完成"

TASKS_FILE = paths.HOME_DIR / "tasks.json"
_tasks: dict[str, dict] = {}
_loaded_path: pathlib.Path | None = None
_lock = threading.Lock()


def _pid_alive(pid: object) -> bool:
    """记录里的 pid 是否还活着（EA-015 的孤儿判据）。缺 pid / 非正整数 → 视为不活。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                     # 存在但无权限发信号：活着
    return True


def _persist() -> None:
    """整文件覆写，但**原子**（EA-014）：写临时文件再 `os.replace`。

    此前直接 `write_text`（O_TRUNC 后逐块写）：并发的读者会读到空文件或半截 JSON，
    解析失败又走「静默降级为空」→ 注册表被读空，随后任一次 `_persist` 把这空快照
    落盘、**放大**成真丢失（V2 用两进程探针实测：`{'empty': 78, 'partial': 2, …}`）。
    """
    if _loaded_path != TASKS_FILE:
        # F3（T6 复审）：表不是从**当前**路径载入的（载入后 TASKS_FILE 被改指；测试
        # teardown 会这样还原真实路径）——写回去等于拿一份属于别的文件的快照整表覆写
        # 别人的注册表。真实事故：~/.agent-wechat/tasks.json 被残留线程的三条合成记录
        # 替换。不写；等下一次 `_ensure_loaded` 把表与路径重新对齐。
        return
    paths.HOME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = TASKS_FILE.with_name(TASKS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(_tasks, ensure_ascii=False, indent=1))
    os.replace(tmp, TASKS_FILE)          # 读者要么看到旧全量、要么看到新全量


def _ensure_loaded() -> None:
    """首次使用（或 TASKS_FILE 被改指）时把磁盘记录载入内存。

    记住上次载入的路径：TASKS_FILE 是模块级常量，测试会 monkeypatch 到临时目录，
    路径一变必须重新载入，否则测试之间会互相污染。

    文件缺失/损坏一律静默降级为空——注册表不能因为一个坏文件而整体不可用。
    载入是读路径，**不写盘**：被收割的孤儿任务等下一次 _persist() 自然落下。
    """
    global _loaded_path, _tasks
    with _lock:
        if _loaded_path == TASKS_FILE:
            return
        _tasks = {}
        try:
            raw = json.loads(TASKS_FILE.read_text())
        except FileNotFoundError:
            _loaded_path = TASKS_FILE   # 「文件不存在」= 空注册表，这是稳定状态
            return
        except (OSError, ValueError):
            # EA-014：读/解析失败**不记 loaded**，下次调用重试。此前在 try 之前就置位
            # `_loaded_path`——一次半截读会被当成「已载入（空表）」缓存住，后面的 `_persist`
            # 再把空表写盘，静默清空别人的任务记录。
            return
        if not isinstance(raw, dict):
            return
        _loaded_path = TASKS_FILE
        for tid, rec in raw.items():
            if not isinstance(rec, dict):
                continue
            if rec.get("status") == "running" and not _pid_alive(rec.get("pid")):
                # EA-015：只收割「进程确实死了」的孤儿。此前无条件把 running 改判 failed
                # ——另一个进程（MCP server）里正在跑的任务，被这里一读就写死，而
                # `get_task_status` 会答「失败」，使用者据得出的结论是**错的**。
                rec = {**rec, "status": "failed", "error": ORPHAN_ERROR}
            _tasks[tid] = rec


def start(kind: str, runner: Callable[[str], int | None], **fields) -> str:
    """登记任务并在后台线程执行 runner(task_id)。runner 的返回值写 rows 终值。

    **准入控制（EA-122）**：同 `kind` 且同 `account`（给定时）的任务仍在跑（记录 running
    且 pid 活着）→ **返回既有 id**，不再起第二个。定时同步落地后「手动 + 定时」并发触发是
    常态；两个 export 各自全量重跑会把失败面变成英文裸 `database is locked`。
    `account` 缺省（向量构建是多账号）时不设限。
    """
    _ensure_loaded()
    acct = fields.get("account")
    with _lock:
        for tid, rec in _tasks.items():
            if (rec.get("status") == "running" and rec.get("kind") == kind
                    and (acct is None or rec.get("account") == acct)
                    and _pid_alive(rec.get("pid"))):
                return tid
    task_id = uuid.uuid4().hex[:12]
    with _lock:
        _tasks[task_id] = {"id": task_id, "kind": kind, "status": "running",
                           "rows": 0, "started_at": int(time.time()),
                           "error": None, "pid": os.getpid(), **fields}
        _persist()
    threading.Thread(target=_run, args=(task_id, runner), daemon=True).start()
    return task_id


def _error_text(exc: BaseException) -> str:
    """失败文案：项目异常带上 hint（与 CLI 同一条修复路径），脱敏。

    `str(exc)` 拿不到 `AgentWeChatError.hint`——同一个模型不一致错误，CLI 用户经
    `errors.to_text` 看得到修复路径，MCP 用户看不到，就是这里丢的。非项目异常也过
    `errors.redact`：这条 error 是作为**成功结果**返回给 agent 的，不经过 `_guard`。

    长度上限由 `errors.redact` 自己的 300 字负责（此处曾有一道 `[:_MAX_ERROR]`
    切片，但它在 redact 之后执行、恒定不生效，已删除）。
    """
    text = errors.to_text(exc) if isinstance(exc, errors.AgentWeChatError) else str(exc)
    return errors.redact(text)


def _run(task_id: str, runner: Callable[[str], int | None]) -> None:
    try:
        rows = runner(task_id)
        with _lock:
            _tasks[task_id].update(status="done", finished_at=int(time.time()))
            if rows is not None:            # 终值覆盖进度值
                _tasks[task_id]["rows"] = rows
            _persist()
    except Exception as e:  # 任务失败不能崩掉 MCP 进程
        with _lock:
            _tasks[task_id].update(status="failed", error=_error_text(e),
                                   finished_at=int(time.time()))
            _persist()


def update(task_id: str, **fields) -> None:
    """就地更新任务字段（供 runner 上报**进度**：rows = 本轮已处理行数，单调递增）。

    未知 id 静默忽略：runner 可能在任务被清理后仍在跑。
    """
    _ensure_loaded()
    with _lock:
        if task_id in _tasks:
            _tasks[task_id].update(fields)
            _persist()


def status(task_id: str | None = None) -> dict | list:
    """查询任务；task_id 省略则列出最近 ≤10 条。未知 id 抛 TaskNotFound。

    core 只抛项目异常，对外形状（MCP 的 {"error": "未知任务 X"}、CLI 的 stderr +
    退出码 1）由适配层翻译。
    """
    _ensure_loaded()
    with _lock:
        if task_id:
            task = _tasks.get(task_id)
            if task is None:
                raise errors.TaskNotFound(f"未知任务 {task_id}")
            return task
        return sorted(_tasks.values(), key=lambda t: t["started_at"], reverse=True)[:10]
