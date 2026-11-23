"""首次导出的 FTS 批量路径（store.fts_bulk_begin / fts_bulk_finish / _heal_fts_bulk）。

不变量只有一条：**任何时候结束（成功、抛异常、被杀后再打开）FTS 都与消息表一致**，
三条触发器都在、中断标记清空。批量路径只是把「逐行跟随」换成「最后一次 rebuild」。
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from agent_wechat import errors, export, store, sync
from test_reader_export_pipeline import ACCT, env, wechat  # noqa: F401


def _triggers(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}


def _assert_consistent(con) -> None:
    """FTS 与消息表一致：行数相同、FTS5 自检通过、逐行能按正文查回自己。"""
    n = con.execute("SELECT count(*) FROM messages").fetchone()[0]
    assert n > 0
    assert con.execute("SELECT count(*) FROM messages_fts_docsize").fetchone()[0] == n
    con.execute("INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)")
    for mid, content in con.execute("SELECT id, content FROM messages WHERE length(content) >= 3"):
        q = '"' + content[:3].replace('"', '""') + '"'
        hits = {r[0] for r in con.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?", (q,))}
        assert mid in hits
    assert set(store.FTS_TRIGGER_NAMES) <= _triggers(con)
    assert con.execute("SELECT count(*) FROM fts_bulk_pending").fetchone()[0] == 0


def _spy_upserts(monkeypatch) -> list[set[str]]:
    """每次 upsert_batch 时库里有哪些触发器。"""
    seen: list[set[str]] = []
    orig = store.upsert_batch

    def spy(con, rows):
        seen.append(_triggers(con))
        return orig(con, rows)
    monkeypatch.setattr(store, "upsert_batch", spy)
    return seen


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_first_export_fills_without_fts_triggers_then_rebuilds(env, monkeypatch):
    seen = _spy_upserts(monkeypatch)
    assert export.run_export(ACCT) == 8
    assert seen and all(not (set(store.FTS_TRIGGER_NAMES) & t) for t in seen), \
        "首次导出灌库时 FTS 触发器应已拆掉"
    assert all("msg_reembed_au" in t for t in seen), "重嵌触发器不属于 FTS，不许一起拆"
    with store.open() as con:
        _assert_consistent(con)


def test_export_into_a_non_empty_index_keeps_the_triggers(env, monkeypatch):
    export.run_export(ACCT)
    seen = _spy_upserts(monkeypatch)
    export.run_export(ACCT, full=True)                # 已有数据的 --full：逐行跟随（触发器几乎不触发）
    assert seen and all(set(store.FTS_TRIGGER_NAMES) <= t for t in seen)
    with store.open() as con:
        _assert_consistent(con)


def test_a_failure_mid_fill_still_leaves_fts_consistent_with_what_was_committed(env, monkeypatch):
    orig = store.upsert_batch
    calls = {"n": 0}

    def flaky(con, rows):
        calls["n"] += 1
        if calls["n"] == 2:
            raise errors.IndexUnavailable("模拟磁盘满")
        return orig(con, rows)
    monkeypatch.setattr(store, "upsert_batch", flaky)
    with pytest.raises(errors.IndexUnavailable, match="模拟磁盘满"):   # 原异常优先，不被收口盖掉
        export.run_export(ACCT)
    monkeypatch.setattr(store, "upsert_batch", orig)
    with store.open() as con:
        _assert_consistent(con)                       # 第一批已提交，且检索得到


def test_a_killed_fill_is_rebuilt_on_the_next_open(env, monkeypatch):
    """被杀 = finish 从没跑过：触发器没了、FTS 是空的、标记还在。下次 connect 补做。"""
    finish = store.fts_bulk_finish
    monkeypatch.setattr(store, "fts_bulk_finish", lambda con: None)
    export.run_export(ACCT)
    monkeypatch.setattr(store, "fts_bulk_finish", finish)
    with store.open() as con:                          # 本进程还活着：不该抢着修
        assert con.execute("SELECT count(*) FROM messages_fts_docsize").fetchone()[0] == 0
        con.execute("UPDATE fts_bulk_pending SET pid=?", (_dead_pid(),))
    with store.open() as con:                          # 写标记的进程已死：当场 rebuild
        _assert_consistent(con)


def test_bulk_begin_refuses_a_non_empty_index(tmp_path, monkeypatch):
    from agent_wechat import paths
    from test_reader_export_pipeline import FRIEND, mk_row
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path)
    with store.open(tmp_path / "i.db") as con:
        store.upsert_batch(con, [mk_row("a", FRIEND, 1, "一二三", shard="message_0.db")])
        assert store.fts_bulk_begin(con) is False
        assert set(store.FTS_TRIGGER_NAMES) <= _triggers(con)
        assert con.execute("SELECT count(*) FROM fts_bulk_pending").fetchone()[0] == 0


def test_doctor_reports_an_interrupted_fill(env, monkeypatch):
    finish = store.fts_bulk_finish
    monkeypatch.setattr(store, "fts_bulk_finish", lambda con: None)
    export.run_export(ACCT)
    monkeypatch.setattr(store, "fts_bulk_finish", finish)
    with store.open() as con:
        con.execute("UPDATE fts_bulk_pending SET pid=?", (_dead_pid(),))
    info = sync.diagnose(ACCT)                         # 只读：不修，只报
    assert info["fts"]["bulk_pending"] == 1
    assert any("批量重建" in p for p in info["problems"])
