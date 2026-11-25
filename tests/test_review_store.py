"""全量审查（store / sync / tasks / purge 一组）修复的回归测试。只用合成数据。"""
import json
import os
import pathlib
import subprocess
import time

import pytest

from agent_wechat import errors, keys, messages, paths, purge, store, sync, tasks, vector

from test_reader_export_pipeline import ACCT
from test_sync_ops import _env


# ---------------------------------------------------------------- ① sync 重试的计数

def test_sync_retry_counts_rows_committed_by_the_failed_attempt(tmp_path, monkeypatch):
    """第一次尝试已提交了行、随后才抛 SourceUnreadable：重试时水位线已前进、返回 0。
    此前 `exported` 只记最后一次 → status=no_new、exported=0，而索引里实际多了 2 行。"""
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "SOURCE_RETRY_DELAY", 0)
    real = sync.export.run_export
    calls = []

    def commits_then_tears(*a, **k):
        calls.append(1)
        n = real(*a, **k)                    # 真导出：行已提交、水位线已前进
        if len(calls) == 1:
            # 失败的尝试按 `run_export` 的契约经 `on_partial` 报回已提交的行（第二阶段起
            # sync 不再按 max(id) 推算；真管线里的失败见 test_review_phase2）
            k["on_partial"](n, 0)
            raise errors.SourceUnreadable("读取微信库时页校验失败（页无法解密或已损坏）")
        return n
    monkeypatch.setattr(sync.export, "run_export", commits_then_tears)
    res = sync.run_sync(ACCT, skip_vector=True)
    with store.open() as con:
        in_index = con.execute("SELECT count(*) FROM messages").fetchone()[0]
    assert len(calls) == 2 and in_index == 2
    assert (res.status, res.exported) == ("ok", 2)


def test_sync_retry_accumulates_skipped_across_attempts(tmp_path, monkeypatch):
    """`skipped` 同理：各次尝试回调的行数累加，不是最后一次覆盖前一次。"""
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "SOURCE_RETRY_DELAY", 0)
    calls = []

    def skip_then_tear(*a, on_skipped=None, **k):
        calls.append(1)
        assert on_skipped is not None
        on_skipped(1 if len(calls) == 1 else 2)
        if len(calls) == 1:
            raise errors.SourceUnreadable("读取微信库时页校验失败（页无法解密或已损坏）")
        return 0
    monkeypatch.setattr(sync.export, "run_export", skip_then_tear)
    res = sync.run_sync(ACCT, skip_vector=True)
    assert (res.status, res.exported, res.skipped) == ("ok", 0, 3)


# ---------------------------------------------------------------- ② 任务收割：pid 复用

def _status(tid: str) -> str:
    rec = tasks.status(tid)
    assert isinstance(rec, dict)
    return rec["status"]


def test_harvest_reaps_a_running_record_whose_pid_was_reused():
    """MCP 进程崩溃后 pid 被一个**更晚启动**的无关进程复用：心跳还新鲜（24h 兜底管不到），
    此前 `_harvest` 只问 pid 活不活 → 同目标的 `start` 一直返回这条幽灵任务的 id。"""
    now = int(time.time())
    impostor = subprocess.Popen(["sleep", "30"])
    try:
        time.sleep(0.2)
        ghost = {"id": "ghost0000001", "kind": "export", "status": "running", "rows": 0,
                 "started_at": now - 600, "updated_at": now, "error": None,
                 "pid": impostor.pid, "account": ACCT, "chat_id": None, "full": False}
        tasks.TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tasks.TASKS_FILE.write_text(json.dumps({ghost["id"]: ghost}))
        snap = tasks.snapshot()
        assert snap is not None
        assert snap[ghost["id"]]["status"] == "failed"
        assert snap[ghost["id"]]["error"] == tasks.ORPHAN_ERROR
        tid = tasks.start("export", lambda _tid: 0, account=ACCT, chat_id=None, full=False)
        assert tid != ghost["id"], "新的 start 不许被幽灵任务吸收"
        deadline = time.time() + 5                      # 起了线程：等到终态再退出
        while _status(tid) == "running" and time.time() < deadline:
            time.sleep(0.02)
        assert _status(tid) == "done"
    finally:
        impostor.kill()
        impostor.wait()


def test_harvest_keeps_this_process_running_record():
    """反向：本进程自己的 running 记录（心跳新鲜）不许被新判据误收。"""
    now = int(time.time())
    rec = {"id": "mine00000001", "kind": "export", "status": "running", "rows": 0,
           "started_at": now - 600, "updated_at": now, "error": None, "pid": os.getpid()}
    assert tasks._harvest(rec, now)["status"] == "running"


# ---------------------------------------------------------------- ③ purge --chat 清任务记录

VICTIM, KEEPER = "victim", "keeper"


def _row(chat_id: str, n: int) -> messages.MessageRow:
    return messages.make_row(account="a", chat_md5=messages.md5_of(chat_id), chat_id=chat_id,
                             shard="message_0.db", local_id=n, ts=100 + n, msg_type=1,
                             sender_id=None, sender_name=None, is_self=0,
                             content=f"消息 {n}", media_hint=None)


@pytest.fixture
def purge_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")
    monkeypatch.setattr(tasks, "TASKS_FILE", home / "tasks.json")
    monkeypatch.setattr(vector, "MODEL_CACHE_DIR", home / "models")
    with store.open() as con:
        store.upsert_batch(con, [_row(VICTIM, 1), _row(VICTIM, 2), _row(KEEPER, 3)])
    return home


def _task(tid: str, chat_id: str | None, status: str = "done") -> dict:
    now = int(time.time())
    return {"id": tid, "kind": "export", "status": status, "rows": 2, "started_at": now - 10,
            "updated_at": now, "finished_at": now, "error": None, "pid": 1,
            "account": "a", "chat_id": chat_id, "full": False}


def _plan():
    return purge.plan_chat([{"account": "a", "chat_id": VICTIM}])


def test_purge_chat_forgets_the_chats_task_records(purge_home):
    """usecases/chat.py 把 chat_id 写进任务记录：purge 后它不许留在 tasks.json 里（最长 30 天）。
    退化形态（`md5:<hex>`）同一个会话，一并清；别的会话与整库任务不动。"""
    recs = {"t_victim0001": _task("t_victim0001", VICTIM),
            "t_degraded01": _task("t_degraded01", messages.MD5_PREFIX + messages.md5_of(VICTIM)),
            "t_keeper0001": _task("t_keeper0001", KEEPER),
            "t_all0000001": _task("t_all0000001", None)}
    tasks.TASKS_FILE.write_text(json.dumps(recs))
    plan = _plan()
    purge.execute_chat(plan)
    left = json.loads(tasks.TASKS_FILE.read_text())
    assert set(left) == {"t_keeper0001", "t_all0000001"}
    assert VICTIM not in tasks.TASKS_FILE.read_text()
    assert purge.verify_chat(plan).leftover == ()


def test_verify_chat_flags_a_task_record_still_naming_the_chat(purge_home):
    """核查口径与 media / index.html 一致：留着会话 id 就算残留。"""
    plan = _plan()
    purge.execute_chat(plan)
    tasks.TASKS_FILE.write_text(json.dumps({"t_victim0001": _task("t_victim0001", VICTIM)}))
    leftover = purge.verify_chat(plan).leftover
    assert any("任务注册表" in x for x in leftover), leftover


def test_verify_chat_flags_an_unreadable_task_registry(purge_home):
    """读不出来 ≠ 核查通过。"""
    plan = _plan()
    purge.execute_chat(plan)
    tasks.TASKS_FILE.write_text("{not json")
    assert any("任务注册表" in x for x in purge.verify_chat(plan).leftover)


# ---------------------------------------------------------------- ④ derived_fts 与 VACUUM

def test_vacuum_keeps_derived_fts_aligned_with_derived_text(tmp_path):
    """审查担心 VACUUM 会重排 derived_text 的隐式 rowid、让 derived_fts 错位。实测不会：
    SQLite 的 VACUUM 只给**没有任何索引**的 rowid 表重新编号（xfer 优化里 `pIndex==0` 那一支），
    derived_text 有 TEXT 主键的自动索引与两条二级索引。本用例钉住这个前提——哪天去掉了索引，
    这里先红，届时再做「显式 INTEGER PRIMARY KEY」那次迁移。"""
    con = store.connect(tmp_path / "v.db")
    try:
        for i in range(30):
            con.execute(
                "INSERT INTO derived_text(derivation_id, msg_key, account, chat_id, kind, text,"
                " engine, created_at) VALUES (?,?,?,?,?,?,?,0)",
                (f"a|c|{i}|ocr", f"a|c|{i}", "a", "c", "ocr", f"派生正文第{i}号", "e"))
        con.execute("DELETE FROM derived_text WHERE CAST(substr(msg_key, 5) AS INTEGER) % 3 = 0")
        con.commit()
        before = con.execute("SELECT rowid, derivation_id FROM derived_text ORDER BY 1").fetchall()
        con.execute("VACUUM")
        after = con.execute("SELECT rowid, derivation_id FROM derived_text ORDER BY 1").fetchall()
        assert [tuple(r) for r in after] == [tuple(r) for r in before]
        hits = con.execute("SELECT d.text FROM derived_fts f JOIN derived_text d"
                           " ON d.rowid = f.rowid WHERE derived_fts MATCH ?", ("第13号",)).fetchall()
        assert [r[0] for r in hits] == ["派生正文第13号"]
        assert store.chat_row_counts(con, "a", "c")["derived_fts_orphans"] == 0
    finally:
        con.close()


# ---------------------------------------------------------------- ⑤ purge 的相对路径文案

def test_relative_home_hint_no_longer_blames_an_empty_value():
    """EA-003 起空值已在 paths 侧回落默认目录；能走到这里的只剩相对路径。"""
    with pytest.raises(errors.PurgeRefused) as ei:
        purge._check_home_is_sane(pathlib.Path("data"))
    assert "空值" not in (ei.value.hint or "")
    assert "相对路径" in (ei.value.hint or "")
