"""Phase 04：同步编排、健康诊断与调度文件。

合成加密树 + 假账号状态（不跑 pgrep/lsof）；时间用 `now=` 注入。所有产物在 tmp 里。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_wechat import account as account_mod
from agent_wechat import errors, keys, messages, paths, store, sync, tasks, vector

from test_reader_export_pipeline import ACCT, FRIEND, PW, SELF, T0, build_shard, enc_db
from test_vector import FakeEmbedder

ACCT2 = "wxid_testcontact02_cd34"          # allowlist 里的合成账号目录
SELF2 = ACCT2.rsplit("_", 1)[0]             # 裸 wxid 由目录名派生，不另写一个字面量


def _build_account(wx, acct: str, texts: tuple[str, ...]) -> None:
    ds = wx / acct / "db_storage"
    enc_db(ds / "session" / "session.db", {
        "SessionTable": ("CREATE TABLE SessionTable (username TEXT)", [(FRIEND,)])})
    enc_db(ds / "contact" / "contact.db", {
        "name2id": ("CREATE TABLE name2id (username TEXT)", [(FRIEND,)]),
        "contact": ("CREATE TABLE contact (username TEXT, alias TEXT, remark TEXT, "
                    "nick_name TEXT, delete_flag INTEGER)",
                    [(FRIEND, None, "张三", "zc", None)]),
    })
    build_shard(wx, acct, "message_0.db",
                {FRIEND: [(i, 1, 1, T0 + i, t, 0) for i, t in enumerate(texts, 1)]},
                [FRIEND])


def _env(tmp_path, monkeypatch, texts=("甲一", "乙二")):
    root = tmp_path / "wx"
    wx, home = root / "xwechat_files", root / "home"
    home.mkdir(parents=True, mode=0o700)
    _build_account(wx, ACCT, texts)
    monkeypatch.setattr(paths, "XWECHAT_FILES", wx)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")
    monkeypatch.setattr(tasks, "TASKS_FILE", home / "tasks.json")
    monkeypatch.setattr(vector, "MODEL_CACHE_DIR", home / "models")
    monkeypatch.setattr(paths, "launch_agents_dir", lambda: home / "LaunchAgents")
    keys.save_cached_hex(SELF, PW.encode().hex())
    fake = account_mod.AccountStatus(running=True, current=ACCT, accounts=[ACCT], pid=1)
    monkeypatch.setattr(sync.account_mod, "status", lambda: fake)
    return SimpleNamespace(wx=wx, home=home)


def _count(table: str) -> int:
    with store.open() as con:
        return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


# ---------------------------------------------------------------- 正常路径

def test_sync_exports_records_state_and_writes_a_safe_log(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    res = sync.run_sync(ACCT, now=1000)
    assert res.status == "ok" and res.exported == 2
    assert _count("messages") == 2

    with store.open() as con:
        st = store.load_sync_state(con, ACCT)
        assert st["status"] == "ok"
        assert st["started_at"] == st["finished_at"] == 1000

    log = (paths.HOME_DIR / "sync.log").read_text()
    rec = json.loads(log.splitlines()[0])
    assert rec["exported"] == 2 and rec["account"] == ACCT
    assert "甲一" not in log, "日志不许出现消息正文"
    assert str(env.wx) not in log, "日志不许出现源库路径"


def test_a_second_sync_with_no_new_messages_reports_no_new(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    assert sync.run_sync(ACCT).status == "ok"
    res = sync.run_sync(ACCT)
    assert res.status == "no_new" and res.exported == 0


# ---------------------------------------------------------------- 可区分的失败

def test_wechat_not_running_is_distinct_and_creates_no_index(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(sync.account_mod, "status",
                        lambda: account_mod.AccountStatus(False, None, [], None))
    res = sync.run_sync(None)
    assert res.status == "wechat_not_running"
    assert not store.DB_PATH.exists(), "失败的前置检查不许建库"


def test_missing_key_is_distinct(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    keys.CACHE_FILE.unlink()
    res = sync.run_sync(ACCT)
    assert res.status == "key_missing"
    assert "bootstrap" in (res.detail or ""), "详情要给出可执行的引导命令"


def test_source_unavailable_is_distinct(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise errors.DataNotFound("message/message_0.db 不存在")

    monkeypatch.setattr(sync.export, "run_export", boom)
    res = sync.run_sync(ACCT)
    assert res.status == "source_unavailable"


def test_disk_low_stops_before_writing(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "_free_bytes", lambda: 1)
    res = sync.run_sync(ACCT)
    assert res.status == "disk_low"
    assert not store.DB_PATH.exists()


def test_a_second_sync_process_is_refused(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    paths.ensure_home_dirs()
    holder = open(paths.HOME_DIR / sync.LOCK_NAME, "a")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        res = sync.run_sync(ACCT)
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
    assert res.status == "concurrent"
    assert not store.DB_PATH.exists()


def test_a_crash_is_recorded_and_the_lock_is_released(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    real = sync.export.run_export

    def boom(*a, **k):
        raise RuntimeError("模拟崩溃")

    monkeypatch.setattr(sync.export, "run_export", boom)
    res = sync.run_sync(ACCT)
    assert res.status == "failed" and "RuntimeError" in (res.detail or "")

    monkeypatch.setattr(sync.export, "run_export", real)
    assert sync.run_sync(ACCT).status == "ok", "锁必须已释放，下一次能正常跑"


def test_vector_failure_is_partial_not_total(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    assert sync.run_sync(ACCT).ok, "先把消息导出，索引里才有可嵌入的行"
    con = vector.connect()
    try:
        vector.build_index(accounts=[ACCT], embedder=FakeEmbedder())
    finally:
        con.close()
    # 制造一条脏行：否则「没有新行要嵌」时模型根本不会被加载，测不到向量阶段失败
    with store.open() as con:
        store.upsert_batch(con, [messages.make_row(
            account=ACCT, chat_md5=hashlib.md5(FRIEND.encode()).hexdigest(),
            chat_id=FRIEND, shard="message_0.db", local_id=1, ts=T0 + 1,
            msg_type=1, sender_id=None, sender_name=None, is_self=0,
            content="改过的正文", media_hint=None)])

    res = sync.run_sync(ACCT)       # 模型缓存为空 → 向量阶段失败，消息仍同步
    assert res.status == "vector_unavailable"
    assert "error" in (res.vector or {})
    assert _count("messages") == 2, "消息阶段已完成"
    with store.open() as con:
        assert store.load_sync_state(con, ACCT)["status"] == "vector_unavailable"


# ---------------------------------------------------------------- 诊断

def test_doctor_is_read_only_when_there_is_no_index(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    info = sync.diagnose(ACCT)
    assert info["index"]["exists"] is False
    assert not store.DB_PATH.exists(), "只读诊断不许建库"


def test_doctor_reports_schema_fts_watermarks_and_sync_state(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    sync.run_sync(ACCT)
    info = sync.diagnose(ACCT)
    assert info["schema"]["version"] == store.SCHEMA_VERSION
    assert info["schema"]["pending"] is False
    assert info["fts"]["messages"] == info["fts"]["indexed"] == 2
    assert info["fts"]["desync"] is False and info["fts"]["triggers_ok"] is True
    assert info["watermarks"]["accounts"] == 1
    assert info["dirty"]["pending"] == 0
    assert info["sync_state"]["status"] == "ok"
    assert info["permissions"]["private"] is True


def test_doctor_flags_an_fts_desync(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    sync.run_sync(ACCT)
    with store.open() as con:
        con.execute("INSERT INTO messages_fts(messages_fts) VALUES('delete-all')")
        con.commit()
    info = sync.diagnose(ACCT)
    assert info["fts"]["desync"] is True
    assert any("FTS" in p for p in info["problems"])


def test_multi_account_state_is_isolated(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    _build_account(env.wx, ACCT2, ("丙三",))
    keys.save_cached_hex(SELF2, PW.encode().hex())
    assert sync.run_sync(ACCT).ok
    info = sync.diagnose(ACCT2)
    assert info["sync_state"] is None, "另一个账号不该继承同步终态"
    with store.open() as con:
        assert store.load_sync_state(con, ACCT)["status"] == "ok"
        assert store.load_sync_state(con, ACCT2) is None


# ---------------------------------------------------------------- 日志清理 / 调度

def test_logs_clean_previews_then_removes_all_three(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    sync.run_sync(ACCT)
    for name in (sync.LOG_NAME, *sync.LAUNCHD_LOGS):
        (paths.HOME_DIR / name).write_text("{}")
    preview = sync.clean_logs(yes=False)
    assert preview["would_remove"] == 3
    assert sync.log_path().exists(), "预览不许删"
    assert sync.clean_logs(yes=True)["removed"] == 3
    assert not any((paths.HOME_DIR / n).exists() for n in (sync.LOG_NAME, *sync.LAUNCHD_LOGS))


def test_schedule_generates_installs_and_removes_without_launchctl(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)          # LAUNCH_AGENTS 已指到 tmp
    monkeypatch.setattr(sync.shutil, "which", lambda _n: "/opt/bin/agent-wechat")
    text = sync.launchd_plist(account=ACCT, interval_minutes=60)
    assert sync.LABEL in text and "<integer>3600</integer>" in text
    assert "RunAtLoad" in text and "<false/>" in text, "默认不随登录启动"

    dest = sync.schedule_install(account=ACCT, interval_minutes=60)
    assert dest.exists() and "StartInterval" in dest.read_text()
    cmds = sync.schedule_commands(ACCT)
    assert "bootstrap" in cmds["enable"] and "bootout" in cmds["disable"]

    assert sync.schedule_uninstall() is True
    assert not dest.exists()
    assert sync.schedule_uninstall() is False


# ---------------------------------------------------------------- 复审补充

def test_key_invalid_and_source_unreadable_are_distinguished(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)

    def bad_key(*a, **k):
        raise errors.DecryptError("sqlcipher 解密失败: bad key")

    monkeypatch.setattr(sync.export, "run_export", bad_key)
    assert sync.run_sync(ACCT).status == "key_invalid"

    def bad_source(*a, **k):
        raise errors.DecryptError("无法快照微信数据库（只读复制失败）：message_0.db")

    monkeypatch.setattr(sync.export, "run_export", bad_source)
    assert sync.run_sync(ACCT).status == "source_unavailable"


def test_an_explicit_unknown_account_is_not_reported_as_wechat_not_running(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = sync.run_sync("wxid_other")
    assert res.status == "account_not_found"


def test_a_too_small_interval_is_a_project_error():
    with pytest.raises(errors.SyncInputError):
        sync.launchd_plist(interval_minutes=1)


def test_a_real_sigkill_releases_the_lock(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    paths.ensure_home_dirs()
    lock = paths.HOME_DIR / sync.LOCK_NAME
    code = ("import fcntl,sys,time;"
            "f=open(sys.argv[1],'a');fcntl.flock(f,fcntl.LOCK_EX);"
            "print('ready',flush=True);time.sleep(60)")
    child = subprocess.Popen([sys.executable, "-c", code, str(lock)],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        assert sync.run_sync(ACCT).status == "concurrent"
    finally:
        child.kill()
        child.wait()
    assert sync.run_sync(ACCT).ok, "SIGKILL 后锁必须随进程释放"


def test_degradations_reach_the_result_without_paths(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)

    class _Drift:
        pass

    class _Reader:
        _drift = (errors.SchemaDrift("roster", "contact/contact.db", "chat_room"),)
        _source_notes = ()

    def fake(acct, **kw):
        kw["on_reader"](_Reader())
        return 0

    monkeypatch.setattr(sync.export, "run_export", fake)
    res = sync.run_sync(ACCT)
    assert res.degraded and res.degraded[0]["capability"] == "roster"
    assert all("contact.db" not in json.dumps(d) for d in res.degraded), "降级不带路径"


def test_concurrent_run_does_not_overwrite_the_last_real_state(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    assert sync.run_sync(ACCT).ok
    paths.ensure_home_dirs()
    holder = open(paths.HOME_DIR / sync.LOCK_NAME, "a")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert sync.run_sync(ACCT).status == "concurrent"
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
    with store.open() as con:
        assert store.load_sync_state(con, ACCT)["status"] == "ok"


def test_sync_records_a_health_snapshot(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = sync.run_sync(ACCT)
    assert res.health is not None
    assert res.health["schema_version"] == store.SCHEMA_VERSION
    assert res.health["fts_desync"] is False
    with store.open() as con:
        stats = json.loads(store.load_sync_state(con, ACCT)["stats"])
        assert stats["health"]["dirty_pending"] == 0


def test_an_empty_model_cache_directory_is_not_reported_ready(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    vector.MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)     # 空目录
    assert sync.diagnose(ACCT)["vector"]["model_cache"] is False
