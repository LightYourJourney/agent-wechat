"""通用后台长任务注册表（MCP 长任务用）：登记、进度、持久化、失败不崩进程。

export 与向量索引共用；任务记录持久化到 ~/.agent-wechat/tasks.json。**每个操作都从磁盘
重读、只把本操作改动的记录合并回去**（F2，T7 复审）：注册表是多个进程共享的状态——两个
MCP server 指向同一个 HOME 是文档推荐的部署，长驻进程必须看见别的进程刚写的记录，写回时
也不许把别人的记录整表抹掉（2026-10-14 01:33 那次真实覆写事故正是这一族）。文件缺失 =
空注册表；读不动/解不开 = **保留内存视图**并照常落盘（下一次写会把坏文件修好）——
「坏文件不能让注册表整体不可用」是模块承诺。

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
import threading
import time
import uuid
from typing import Callable

from . import errors, paths

# 孤儿任务的失败文案：上次进程退出时还在 running，说明它已经死了。不得新增
# `interrupted` 状态值——TOOLS.md §5 已把枚举发布为 running/done/failed 三值。
ORPHAN_ERROR = "进程中断，任务未完成"
# F6（T7 复审）：pid 复用的时间兜底。`os.kill(pid, 0)` 分不清「原任务」与「复用了该 pid 的
# 无关进程」，而错误的「还活着」会把该账号的新导出全部吸收进一条永不执行的幽灵任务。
# 24h 远大于真实长任务（68 万行向量构建实测 ~3h），且误判可自愈：那条任务下次
# update/收尾会把状态改回来。
STALE_RUNNING_SECONDS = 24 * 3600
# 准入键（F1，T7 复审）：同 kind、且这些区分「目标」的字段全部相同才算同一次工作。
# 此前只看 (kind, account)——要 chatB 的调用会拿到 chatA 的 task_id、`full=True` 被增量
# 任务吸收、`build_vector_index()`（全部账号）被单账号任务吸收，调用方按 done 轮询后
# 会得出**错误**的结论（「李四导出了」而实际没跑）。新增区分字段时把它加进这个元组。
_IDENTITY_KEYS = ("account", "chat_id", "full", "rebuild")

TASKS_FILE = paths.HOME_DIR / "tasks.json"
_tasks: dict[str, dict] = {}       # 本操作开头从磁盘重读的视图（见 _load_locked）
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


def _harvest(rec: dict, now: int) -> dict:
    """载入时把「进程确实死了」或「陈旧到不可信」的 running 收割成 failed。

    EA-015：此前无条件把 running 改判 failed——另一个进程（MCP server）里正在跑的任务
    被这里一读就写死，而 `get_task_status` 会答「失败」，使用者据得出的结论是**错的**。
    F6：只看 pid 不够（pid 会被复用），再加一条时间兜底（STALE_RUNNING_SECONDS）。
    """
    if rec.get("status") != "running":
        return rec
    started = rec.get("started_at")
    stale = isinstance(started, int) and now - started > STALE_RUNNING_SECONDS
    if stale or not _pid_alive(rec.get("pid")):
        return {**rec, "status": "failed", "error": ORPHAN_ERROR}
    return rec


def _read() -> dict[str, dict] | None:
    """读 + 解析 + 收割。文件不存在 → {}（稳定空表）；读不动/解不开/根不是对象 → None。"""
    try:
        raw = json.loads(TASKS_FILE.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    now = int(time.time())
    return {tid: _harvest(rec, now) for tid, rec in raw.items() if isinstance(rec, dict)}


def _load_locked() -> None:
    """把磁盘现状读进 `_tasks`（调用方必须已持 `_lock`）。

    **每次操作都重读**（F2，T7 复审）：旧实现载入一次就早退，长驻进程永远看不见别的
    进程后来写的记录（对它答「未知任务」）。读不动时**保留**现有视图（EA-014 的立场：
    一次半截读不能被当成「已载入（空表）」缓存住，进而把空表写盘）——旧实现在这里清空
    内存表（F4），`start` 刚返回的 id 下一次调用就成「未知任务」，runner 收尾还会 KeyError。
    """
    global _tasks
    disk = _read()
    if disk is not None:
        _tasks = disk


def _persist_locked(changed: dict[str, dict]) -> None:
    """把**本操作改动的记录**合并进磁盘现状后原子落盘（EA-014 + F2；调用方必须已持 `_lock`）。

    合并而非整表覆写：别的进程在我们读盘之后写下的记录必须留着——整表覆写正是那次真实
    覆写事故的机制。直接 `write_text`（O_TRUNC 后逐块写）会让并发读者读到空/半截 JSON
    （V2 两进程探针实测 957 次坏读），故写临时文件再 `os.replace`。tmp 名带 pid（F5）：
    固定名会让两个进程共用同一个临时文件，一方的 `os.replace` 撞上另一方刚搬走它的
    ENOENT 窗口。读不动时以内存视图为准照常写——下一次落盘就把坏文件修好。
    """
    paths.HOME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    disk = _read()
    merged = disk if disk is not None else dict(_tasks)
    merged.update(changed)
    tmp = TASKS_FILE.with_name(f"{TASKS_FILE.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=1))
        os.replace(tmp, TASKS_FILE)      # 读者要么看到旧全量、要么看到新全量
    except OSError:
        tmp.unlink(missing_ok=True)      # 失败不留 tmp 残骸
        raise


def start(kind: str, runner: Callable[[str], int | None], **fields) -> str:
    """登记任务并在后台线程执行 runner(task_id)。runner 的返回值写 rows 终值。

    **准入控制（EA-122）**：同 kind、且 `_IDENTITY_KEYS` 里区分目标的字段全部与调用方
    一致的任务仍在跑（running、pid 活着、未陈旧）→ **返回既有 id**，不再起第二个：定时
    同步落地后「手动 + 定时」并发触发是常态，两个 export 各自全量重跑会把失败面变成英文
    裸 `database is locked`。**不同目标绝不吸收**（F1）。判据、插入与落盘在**同一把锁内**
    完成（F7）：check-then-act 分两次取锁会让并发 start 各起一条。
    """
    with _lock:
        _load_locked()
        for tid, rec in _tasks.items():
            if (rec.get("status") == "running" and rec.get("kind") == kind
                    and all(rec.get(k) == fields.get(k) for k in _IDENTITY_KEYS)
                    and _pid_alive(rec.get("pid"))):
                return tid
        task_id = uuid.uuid4().hex[:12]
        rec = {"id": task_id, "kind": kind, "status": "running",
               "rows": 0, "started_at": int(time.time()),
               "error": None, "pid": os.getpid(), **fields}
        _tasks[task_id] = rec
        try:
            _persist_locked({task_id: rec})
        except OSError:
            _tasks.pop(task_id, None)    # F5：落盘失败不留一条 runner 永不执行的幽灵
            raise
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


def _finish(task_id: str, *, status: str, rows: int | None = None,
            error: str | None = None) -> None:
    """把终态写回注册表（**重读后按记录合并**，F2）。

    id 在重读结果里不存在时**什么都不写**：那条记录属于另一个注册表文件（测试 teardown
    把 TASKS_FILE 还原成真实路径后，残留线程会落在这里——2026-10-14 01:33 的真实覆写
    就是这条路径），或已被别的进程清理。绝不用下标/`setdefault` 把它重新造出来：
    造出来的记录会被写进**别人的**文件（F4），下标取值还会让 KeyError 逃出
    `except Exception`（同一发现的另一半）。
    """
    with _lock:
        _load_locked()
        rec = _tasks.get(task_id)
        if rec is None:
            return
        rec.update(status=status, finished_at=int(time.time()))
        if rows is not None:             # 终值覆盖进度值
            rec["rows"] = rows
        if error is not None:
            rec["error"] = error
        _persist_locked({task_id: rec})


def _run(task_id: str, runner: Callable[[str], int | None]) -> None:
    try:
        rows = runner(task_id)
        _finish(task_id, status="done", rows=rows)
    except Exception as e:  # 任务失败不能崩掉 MCP 进程
        _finish(task_id, status="failed", error=_error_text(e))


def update(task_id: str, **fields) -> None:
    """就地更新任务字段（供 runner 上报**进度**：rows = 本轮已处理行数，单调递增）。

    未知 id 静默忽略：runner 可能在任务被清理后仍在跑（或记录属于别的注册表文件）。
    """
    with _lock:
        _load_locked()
        rec = _tasks.get(task_id)
        if rec is None:
            return
        rec.update(fields)
        _persist_locked({task_id: rec})


def status(task_id: str | None = None) -> dict | list:
    """查询任务；task_id 省略则列出最近 ≤10 条。未知 id 抛 TaskNotFound。

    每次调用都**重读磁盘**（F2）：两个 MCP server 共享一个 HOME 时，「A 起的任务 B 要
    答得上来」是跨进程可靠性的最低要求。core 只抛项目异常，对外形状（MCP 的
    {"error": "未知任务 X"}、CLI 的 stderr + 退出码 1）由适配层翻译。
    """
    with _lock:
        _load_locked()
        if task_id:
            task = _tasks.get(task_id)
            if task is None:
                raise errors.TaskNotFound(f"未知任务 {task_id}")
            return task
        # EA-081：外部写入的记录可能缺 started_at——排序不许裸 KeyError 把整条列表打挂
        # （与「坏文件不能让注册表整体不可用」同一立场：其余记录仍要答得出来）。
        return sorted(_tasks.values(), key=lambda t: t.get("started_at") or 0,
                      reverse=True)[:10]
