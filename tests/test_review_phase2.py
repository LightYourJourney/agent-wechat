"""全量审查第二阶段修掉的几处缺陷，各一组回归用例。只用合成数据，不碰真实微信数据与 ~/.agent-wechat。

A. purge --chat 与**同步执行**的导出（媒体 / 会话）之间的竞态：`tasks.claim`；
   以及索引库不在时 `purge --chat --yes` 漏查总目录页。
B. sync 源侧重试：失败那次已提交/已跳过的行由 `export.run_export` 的 `on_partial` 报回。
C. `message_content` 源级降级的友好文案。
D. 只读 sqlite URI 的百分号转义（`paths.sqlite_ro_uri`）。
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from agent_wechat import (cli, errors, exclusions, export, media, messages, paths, purge, reader,
                          search, store, sync, tasks, usecases, vector)
from agent_wechat.usecases import _core

from test_cli import _isolate, _stub_resolution
from test_reader_export_pipeline import ACCT, FRIEND, T0
from test_sync_ops import _env

CHAT = "victim@chatroom"


# ---------------------------------------------------------------- A. 同步导出占住在飞闸门

class _Ctx:
    """只换掉会话解析（真 `ChatContext` 会去开微信库）。"""

    def __init__(self, account=None):
        self.account = ACCT

    def resolve(self, contact):
        return CHAT


def _purge_refused() -> bool:
    """`execute_chat` 的在飞闸门此刻拒不拒绝（空计划：闸门之后什么都不删）。"""
    try:
        purge.execute_chat(purge.ChatPlan(()))
    except errors.PurgeRefused as exc:
        assert "长任务正在运行" in str(exc)
        return True
    return False


def test_purge_refused_while_media_export_runs_and_allowed_after(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(usecases, "ChatContext", _Ctx)
    seen = {}

    def fake_export_media(account, chat_ids, **kw):
        seen["running"] = [(t["kind"], t.get("chat_id")) for t in tasks.running()]
        seen["refused"] = _purge_refused()
        return media.MediaReport()
    monkeypatch.setattr(media, "export_media", fake_export_media)
    usecases.media_export("小李", kinds=["image"])
    assert seen == {"running": [("media_export", CHAT)], "refused": True}
    assert tasks.running() == [] and not _purge_refused(), "导出结束后 purge 放行"
    assert tasks.snapshot() == {}, "claim 的记录退出即删，不占已结束记录的名额"


def test_chat_export_claim_is_released_when_export_raises(tmp_path, monkeypatch):
    """会话导出读索引那一步就抛：记录照样释放，purge 随后放行。"""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(usecases, "ChatContext", _Ctx)
    seen = {}

    def boom(chat_id, **kw):
        seen["refused"] = _purge_refused()
        raise errors.IndexUnavailable("合成故障")
    monkeypatch.setattr(search, "messages_in_range", boom)
    with pytest.raises(errors.IndexUnavailable):
        usecases.chat_export("小李")
    assert seen == {"refused": True}
    assert tasks.running() == [] and not _purge_refused()


def test_claim_keeps_long_task_history_and_is_not_absorbed(tmp_path, monkeypatch):
    """大量短 claim 不挤掉已结束的长任务记录（`KEEP_FINISHED` 上限）；同目标的后台导出
    不会被吸收进 claim 那条没有 runner 的记录（kind 不同）。"""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(tasks, "KEEP_FINISHED", 3)
    done = {"id": "longtask0001", "kind": "export", "status": "done", "rows": 9,
            "started_at": T0, "updated_at": T0, "finished_at": 2**31 - 1, "error": None,
            "pid": 1, "account": ACCT, "chat_id": None, "full": False}
    tasks._persist_locked({done["id"]: done})
    for _ in range(5):
        with tasks.claim("media_export", account=ACCT, chat_id=CHAT):
            pass
    assert list(tasks.snapshot() or {}) == ["longtask0001"]
    with tasks.claim("chat_export", account=ACCT, chat_id=CHAT):
        (claimed,) = [t["id"] for t in tasks.running()]
        tid = tasks.start("export", lambda _tid: 0, account=ACCT, chat_id=CHAT, full=False)
    assert tid != claimed
    for _ in range(500):          # 等后台线程到终态（conftest 的隔离闸要求）
        rec = tasks.status(tid)
        assert isinstance(rec, dict)
        if rec["status"] != "running":
            break
        time.sleep(0.01)
    assert rec["status"] == "done" and claimed not in (tasks.snapshot() or {})


def test_purge_chat_yes_without_index_counts_the_index_page(tmp_path, monkeypatch, capsys):
    """索引库不在、`media/<md5>/` 已没了，但总目录页 `media/index.html` 还列着该会话那一行：
    `--yes` 不许退 0（此前这一支只看导出目录，总目录页里的会话 id 与条数被放过）。"""
    home = _isolate(tmp_path, monkeypatch)
    md5 = messages.chat_md5("victim")
    with store.open() as con:
        exclusions.add(con, [(f"a|{md5}|message_0.db|1", "a", md5)])
    (home / "index.db").unlink()
    (home / "media").mkdir()
    page = home / "media" / "index.html"
    page.write_text(f'<a href="{md5}/chat/index.html">victim</a>', encoding="utf-8")
    _stub_resolution(monkeypatch, "victim")
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--chat", "小李", "--yes"])
    assert ei.value.code == 1
    assert str(page) in capsys.readouterr().err
    assert page.exists() and not (home / "index.db").exists()


# ---------------------------------------------------------------- B. sync 重试的计数

def _tear_after_first_batch(monkeypatch):
    """第一次导出交出第一批（随即逐批提交）后抛 SourceUnreadable；之后照常。"""
    orig = reader.MessageReader.iter_all
    calls = []

    def tearing(self, *a, **k):
        calls.append(1)
        it = orig(self, *a, **k)
        if len(calls) > 1:
            yield from it
            return
        yield next(it)
        raise errors.SourceUnreadable("读取微信库时页校验失败（页无法解密或已损坏）")
    monkeypatch.setattr(reader.MessageReader, "iter_all", tearing)
    return calls


def _exclude_first_message() -> None:
    md5 = messages.chat_md5(FRIEND)
    with store.open() as con:
        exclusions.add(con, [(messages.row_key(ACCT, md5, "message_0.db", 1, T0 + 1), ACCT, md5)])


@pytest.mark.parametrize("full", [False, True])
def test_sync_retry_counts_rows_committed_and_skipped_by_the_failed_attempt(
        tmp_path, monkeypatch, full):
    """失败那次已提交 1 行、跳过 1 行（被排除），重试时水位线已前进。增量：两个数都要累加进来
    （此前 skipped 数不到）；全量：重试清水位线重扫整段，失败那次不能再加一遍（此前按 max(id)
    推算会把那 1 行数两次）。"""
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "SOURCE_RETRY_DELAY", 0)
    _exclude_first_message()
    calls = _tear_after_first_batch(monkeypatch)
    res = sync.run_sync(ACCT, full=full, skip_vector=True)
    with store.open() as con:
        in_index = con.execute("SELECT count(*) FROM messages").fetchone()[0]
    assert len(calls) == 2 and in_index == 1
    assert (res.status, res.exported, res.skipped) == ("ok", 1, 1)


def test_run_export_reports_partial_counts_on_failure(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _exclude_first_message()
    _tear_after_first_batch(monkeypatch)
    got = []
    with pytest.raises(errors.SourceUnreadable):
        export.run_export(ACCT, on_partial=lambda n, s: got.append((n, s)))
    assert got == [(1, 1)]


# ---------------------------------------------------------------- C. 解压失败的降级文案

def test_message_content_degradation_has_a_friendly_effect():
    class _R:
        _drift: list = []
        _source_notes = [("message_content", "message/message_0.db")]
    (d,) = _core.collect_degradations(_R())    # type: ignore[arg-type]
    assert d.effect == "部分消息正文解压失败，以 [解压失败] 占位（message/message_0.db）"
    assert d.envelope() == {"capability": "message_content", "effect": d.effect}
    assert not d.drift


# ---------------------------------------------------------------- D. 只读 URI 的转义

@pytest.fixture
def odd_dir(tmp_path):
    d = tmp_path / "a?b#c%41 d"
    d.mkdir()
    return d


def _make_db(p) -> None:
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE t (x)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()


def test_sqlite_ro_uri_opens_the_named_file_read_only(odd_dir, tmp_path):
    db = odd_dir / "index.db"
    _make_db(db)
    con = sqlite3.connect(paths.sqlite_ro_uri(db), uri=True)
    try:
        assert con.execute("SELECT x FROM t").fetchall() == [(1,)]
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            con.execute("INSERT INTO t VALUES (2)")
    finally:
        con.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == [odd_dir.name]


def test_store_open_readonly_with_odd_home(odd_dir, tmp_path, monkeypatch):
    db = odd_dir / "index.db"
    _make_db(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    with store.open_readonly() as con:
        assert con.execute("SELECT x FROM t").fetchall() == [(1,)]
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            con.execute("INSERT INTO t VALUES (2)")
    assert sorted(p.name for p in tmp_path.iterdir()) == [odd_dir.name], "不许另开一个截断路径"


def test_vector_warm_up_reads_the_odd_home(odd_dir, tmp_path, monkeypatch):
    db = odd_dir / "index.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE vector_meta (model_id TEXT, embedded INTEGER)")
    con.execute("INSERT INTO vector_meta VALUES (?, 0)", (vector.MODEL_ID,))
    con.commit()
    con.close()
    models = odd_dir / "models"
    models.mkdir()
    (models / "x").write_text("")
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(vector, "MODEL_CACHE_DIR", models)
    monkeypatch.setattr(vector, "semantic_installed", lambda: True)
    assert vector.warm_up_if_ready() is False          # embedded=0：读到的是这个库
    assert sorted(p.name for p in tmp_path.iterdir()) == [odd_dir.name], "不许另开一个截断路径"
