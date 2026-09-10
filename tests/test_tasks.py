"""通用长任务注册表：登记/进度/持久化/失败不崩进程。"""
import json
import time

from agent_wechat import tasks


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
    assert tasks.status("nope") == {"error": "未知任务 nope"}
    tasks.start("vector", lambda tid: 0)
    time.sleep(0.05)
    listed = tasks.status()
    assert isinstance(listed, list) and listed and "kind" in listed[0]
