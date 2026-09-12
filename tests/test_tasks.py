"""通用长任务注册表：登记/进度/持久化/孤儿收割/失败不崩进程。"""
import json
import time

import pytest

from agent_wechat import errors, tasks


def _simulate_restart(monkeypatch):
    """模拟进程重启：清空内存注册表，并忘掉「已载入的路径」。

    只清空 _tasks 不够——_ensure_loaded 认路径，路径没变它不会重读。两件一起做，
    下一次 status/start/update 才真的走磁盘载入。

    raising=False：C5 之前没有 _loaded_path 这个属性（那时根本没有载入逻辑），
    让这些测试在旧代码上照样能跑起来去撞真正的行为断言，而不是撞 AttributeError。
    """
    monkeypatch.setattr(tasks, "_loaded_path", None, raising=False)
    tasks._tasks.clear()


def _wait_done(tid, timeout=2.0):
    """等后台任务跑完并返回它的记录；超时即失败（别让测试挂死）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = tasks.status(tid)
        if st["status"] != "running":
            return st
        time.sleep(0.01)
    raise AssertionError(f"任务 {tid} 未在 {timeout}s 内结束")


def test_start_runs_runner_and_records_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    done = []

    def runner(tid):
        tasks.update(tid, rows=42)
        done.append(tid)
        return 42

    tid = tasks.start("export", runner, account="acct")
    for _ in range(200):                      # 后台线程，等待完成
        if done:
            break
        time.sleep(0.01)
    assert done == [tid]
    st = tasks.status(tid)
    assert st["kind"] == "export" and st["account"] == "acct"
    assert st["status"] == "done" and st["rows"] == 42
    assert json.loads((tmp_path / "tasks.json").read_text())[tid]["status"] == "done"


def test_runner_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")

    def boom(tid):
        raise RuntimeError("模型下载失败")

    tid = tasks.start("vector", boom)
    for _ in range(200):
        if tasks.status(tid)["status"] != "running":
            break
        time.sleep(0.01)
    st = tasks.status(tid)
    assert st["status"] == "failed" and "模型下载失败" in st["error"]


def test_status_lists_recent_and_unknown_id(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    with pytest.raises(errors.TaskNotFound):      # 未知 id 是错误，不是 dict
        tasks.status("nope")
    tasks.start("vector", lambda tid: 0)
    time.sleep(0.05)
    listed = tasks.status()
    assert isinstance(listed, list) and listed and "kind" in listed[0]


# ---------- 持久化成真：重启后仍答得上来 ----------

def test_task_survives_process_restart(tmp_path, monkeypatch):
    """start() 落盘 → 内存清空 + 重新载入（= 重启）后照样查得到。"""
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    tid = tasks.start("export", lambda _tid: 7, account="acct")
    assert _wait_done(tid)["rows"] == 7

    _simulate_restart(monkeypatch)
    st = tasks.status(tid)                        # 只能来自磁盘
    assert st["status"] == "done" and st["rows"] == 7 and st["account"] == "acct"
    assert [t["id"] for t in tasks.status()] == [tid]


def test_missing_or_corrupt_file_degrades_to_empty(tmp_path, monkeypatch):
    """坏文件不能让注册表不可用：缺失/非法 JSON/根不是对象，一律静默降级为空。"""
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "absent.json")
    assert tasks.status() == []                   # 文件不存在
    tid = tasks.start("vector", lambda _tid: 1)   # 登记照常
    assert _wait_done(tid)["rows"] == 1

    for bad in ("{ 这不是 json", "[1, 2]"):
        path = tmp_path / "broken.json"
        path.write_text(bad)
        monkeypatch.setattr(tasks, "TASKS_FILE", path)
        assert tasks.status() == []
        assert path.read_text() == bad                 # 读路径不写盘，坏文件原样留着


# ---------- 孤儿收割：上次进程留下的 running ----------

def test_orphan_running_record_is_harvested_as_failed(tmp_path, monkeypatch):
    """载入时把遗留 running 标成 failed（枚举只有三值，不新增 interrupted）。"""
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    (tmp_path / "tasks.json").write_text(json.dumps({
        "dead": {"id": "dead", "kind": "export", "status": "running", "rows": 3,
                 "started_at": 1, "error": None},
        "live": {"id": "live", "kind": "vector", "status": "done", "rows": 9,
                 "started_at": 2, "error": None},
    }))
    _simulate_restart(monkeypatch)

    st = tasks.status("dead")
    assert st["status"] == "failed"
    assert st["error"] == tasks.ORPHAN_ERROR
    assert any("一" <= c <= "鿿" for c in st["error"])   # 非空中文
    assert tasks.status("live")["status"] == "done"              # 已完成的不动

    # 载入是读路径，不写盘：磁盘上那条仍是 running，等下一次 _persist 才落下
    assert json.loads((tmp_path / "tasks.json").read_text())["dead"]["status"] == "running"


# ---------- rows 的两个写入方 ----------

def test_rows_progress_then_final_value(tmp_path, monkeypatch):
    """运行中 update(rows=) 写进度（单调递增），跑完被 runner 返回值的终值覆盖。"""
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    seen = []

    def runner(tid):
        for n in (5, 9, 12):
            tasks.update(tid, rows=n)
            seen.append(tasks.status(tid)["rows"])
        return 99                                 # 终值

    st = _wait_done(tasks.start("export", runner))
    assert seen == [5, 9, 12]                     # 进度就写在这里
    assert seen == sorted(seen) and seen[-1] < st["rows"]
    assert st["status"] == "done" and st["rows"] == 99      # 终值覆盖进度值
    assert json.loads((tmp_path / "tasks.json").read_text())[st["id"]]["rows"] == 99


def test_runner_returning_none_keeps_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")

    def runner(tid):
        tasks.update(tid, rows=13)
        return None

    st = _wait_done(tasks.start("vector", runner))
    assert st["status"] == "done" and st["rows"] == 13


# ---------- 失败文案带上 hint ----------

def _fail_with(exc):
    def runner(_tid):
        raise exc
    tid = tasks.start("vector", runner, account=None)
    return _wait_done(tid)


def test_failure_error_carries_hint(tmp_path, monkeypatch):
    """MCP 用户也要看得到修复路径：error 用 errors.to_text 的「消息（hint）」形式。"""
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    st = _fail_with(errors.VectorError("索引与当前模型不一致", hint="请改用 rebuild=True 重建"))
    assert st["status"] == "failed"
    assert st["error"] == "索引与当前模型不一致（请改用 rebuild=True 重建）"


def test_failure_error_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    st = _fail_with(errors.VectorError("超长" * 1000, hint="修"))
    assert st["status"] == "failed"
    assert 0 < len(st["error"]) <= 500
