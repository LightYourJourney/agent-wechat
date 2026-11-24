"""schema v10：msg_key 追加 create_time、水位线锚点识别表重建、旧 4 段键与旧墓碑兼容。

场景是「微信删掉再建 `Msg_<md5>`」：local_id 从 1 重来。全部用合成库，不碰真机数据。
"""
from __future__ import annotations

import sqlite3
import uuid

import pytest

from agent_wechat import errors, exclusions, export, keys, messages, paths, retrieval, store
from helpers import mk_row
from test_reader_export_pipeline import (FRIEND, PW, SELF, T0, build_shard, enc_db,
                                         md5)

ACCT = "wxid_t_a1b2"


@pytest.fixture()
def wx(tmp_path, monkeypatch):
    """可在用例中途重建分片的合成微信目录（模块级的 `wechat` 夹具是共享的、不能改）。"""
    root = tmp_path / "xwechat_files"
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    ds = root / ACCT / "db_storage"
    enc_db(ds / "session" / "session.db", {
        "SessionTable": ("CREATE TABLE SessionTable (username TEXT)", [(FRIEND,), (SELF,)])})
    enc_db(ds / "contact" / "contact.db", {
        "name2id": ("CREATE TABLE name2id (username TEXT)", [(FRIEND,)]),
        "contact": ("CREATE TABLE contact (username TEXT, alias TEXT, remark TEXT, "
                    "nick_name TEXT, delete_flag INTEGER)", [(FRIEND, None, "张三", "zc", None)]),
        "chat_room": ("CREATE TABLE chat_room (id INTEGER PRIMARY KEY, username TEXT, "
                      "owner TEXT, ext_buffer BLOB)", []),
    })
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    monkeypatch.setattr(store, "DB_PATH", home / f"index-{uuid.uuid4().hex[:8]}.db")
    keys.save_cached_hex(SELF, PW.encode().hex())

    def shard(rows):
        """rows: [(local_id, create_time, 正文)]，整张表重建（= 微信删表再建）。"""
        build_shard(root, ACCT, "message_0.db",
                    {FRIEND: [(lid, 1, 1, ts, text, 0) for lid, ts, text in rows]},
                    [FRIEND, SELF])
    return shard


def _contents() -> list[tuple[int, str]]:
    with store.open() as con:
        return [(r[0], r[1]) for r in con.execute(
            "SELECT ts, content FROM messages ORDER BY ts")]


def test_a_recreated_table_keeps_the_old_generation_and_exports_the_new_one(wx):
    """修前：重建后的 local_id 1..2 低于旧水位线 3，前向扫描一条都读不到；回看把它们写到
    旧键上，覆盖第一代的正文。修后：两代并存、各自完整。"""
    wx([(1, T0 + 1, "一代一"), (2, T0 + 2, "一代二"), (3, T0 + 3, "一代三")])
    assert export.run_export(ACCT) == 3
    wx([(1, T0 + 101, "二代一"), (2, T0 + 102, "二代二")])
    assert export.run_export(ACCT) == 2
    assert _contents() == [(T0 + 1, "一代一"), (T0 + 2, "一代二"), (T0 + 3, "一代三"),
                           (T0 + 101, "二代一"), (T0 + 102, "二代二")]


def test_a_recreated_table_that_already_grew_past_the_old_watermark(wx):
    """新表在两次导出之间已经长过旧水位线：只比 max(local_id) 发现不了，锚点能。"""
    wx([(1, T0 + 1, "一代一"), (2, T0 + 2, "一代二")])
    export.run_export(ACCT)
    wx([(i, T0 + 100 + i, f"二代{i}") for i in range(1, 5)])
    assert export.run_export(ACCT) == 4
    assert len(_contents()) == 6


def test_the_watermark_is_reset_once_then_incremental_again(wx, monkeypatch):
    wx([(1, T0 + 1, "一代一"), (2, T0 + 2, "一代二")])
    export.run_export(ACCT)
    wx([(1, T0 + 101, "二代一")])
    export.run_export(ACCT)
    resets = []
    real = store.reset_export_watermark
    monkeypatch.setattr(store, "reset_export_watermark",
                        lambda *a: (resets.append(a), real(*a)))
    assert export.run_export(ACCT) == 0
    assert resets == [], "重建只该被识别一次：水位线归零后锚点换成了新表的那一行"
    with store.open() as con:
        assert tuple(con.execute("SELECT max_local_id, anchor_ts FROM watermark").fetchone()) \
            == (1, T0 + 101)


def test_an_ordinary_append_moves_the_anchor_and_does_not_rescan(wx, monkeypatch):
    """常态（只是来了新消息）：水位线走 UPDATE 路径，锚点必须跟着新的 max 那行走——
    留在旧行上的话，下一轮会把「源表 local_id=新 max 的 create_time」与旧锚点比对，误判重建。"""
    wx([(1, T0 + 1, "一"), (2, T0 + 2, "二")])
    export.run_export(ACCT)
    wx([(1, T0 + 1, "一"), (2, T0 + 2, "二"), (3, T0 + 3, "三")])
    resets = []
    real = store.reset_export_watermark
    monkeypatch.setattr(store, "reset_export_watermark",
                        lambda *a: (resets.append(a), real(*a)))
    assert export.run_export(ACCT) == 1
    assert export.run_export(ACCT) == 0
    assert resets == []
    with store.open() as con:
        assert tuple(con.execute("SELECT max_local_id, anchor_ts FROM watermark").fetchone()) \
            == (3, T0 + 3)


def test_deleting_the_newest_message_rescans_without_duplicates(wx):
    """锚点那行不在（用户删了最新一条）也按重建处理：从 0 重扫，键含 create_time，幂等。
    （这一轮的 `rows` 计数含重扫读到的行，与 `--full` 同口径。）"""
    wx([(1, T0 + 1, "一"), (2, T0 + 2, "二"), (3, T0 + 3, "三")])
    export.run_export(ACCT)
    wx([(1, T0 + 1, "一"), (2, T0 + 2, "二")])
    export.run_export(ACCT)
    assert _contents() == [(T0 + 1, "一"), (T0 + 2, "二"), (T0 + 3, "三")]
    assert export.run_export(ACCT) == 0, "重扫之后回到增量"


# ---------- 迁移 ----------

def _downgrade_to_v9(db) -> None:
    """把一个 v10 库改回 v9 的样子：键去掉第五段、watermark 去掉锚点列、版本号写回 9。"""
    con = sqlite3.connect(db)
    four = "substr({c}, 1, length({c}) - length(CAST(ts AS TEXT)) - 1)"
    con.execute(f"UPDATE msg_parts SET ref_msg_key = (SELECT {four.format(c='m.msg_key')} "
                "FROM messages m WHERE m.msg_key = msg_parts.ref_msg_key)")
    for t in ("media_refs", "msg_parts", "msg_extras", "derived_tombstone", "pending_reembed"):
        con.execute(f"UPDATE {t} SET msg_key = (SELECT {four.format(c='m.msg_key')} "
                    f"FROM messages m WHERE m.msg_key = {t}.msg_key) "
                    f"WHERE msg_key IN (SELECT msg_key FROM messages)")
    con.execute(f"UPDATE derived_text SET msg_key = (SELECT {four.format(c='m.msg_key')} "
                "FROM messages m WHERE m.msg_key = derived_text.msg_key), "
                "derivation_id = (SELECT " + four.format(c="m.msg_key")
                + " FROM messages m WHERE m.msg_key = derived_text.msg_key) || '|' || kind")
    con.execute(f"UPDATE messages SET msg_key = {four.format(c='msg_key')}")
    con.execute("ALTER TABLE watermark DROP COLUMN anchor_ts")
    con.execute("UPDATE schema_version SET version = 9 WHERE scope = 'main'")
    con.commit()
    con.close()


def test_v10_migration_rekeys_every_table_and_keeps_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path / "home")
    db = tmp_path / "index.db"
    con = store.connect(db)
    rows = [mk_row(ACCT, FRIEND, i, f"正文{i}", ts=T0 + i) for i in (1, 2, 3)]
    store.upsert_batch(con, rows)
    k1 = rows[0]["msg_key"]
    store.upsert_media_refs(con, [(k1, "image", "aa" * 16, None)])
    store.upsert_msg_parts(con, [(rows[1]["msg_key"], 57, 9, k1, b"x")])   # 引用 k1
    con.execute("INSERT INTO derived_text(derivation_id, msg_key, account, chat_id, kind, text,"
                " engine, engine_version, source, quality, status, created_at)"
                " VALUES (?,?,?,?, 'ocr', 'x', 'e', '1', 's', 1.0, 'auto', 0)",
                (f"{k1}|ocr", k1, ACCT, FRIEND))
    orphan = f"{ACCT}|{md5(FRIEND)}|message_0.db|99"            # 没有消息行的旁表孤儿
    store.upsert_media_refs(con, [(orphan, "image", "bb" * 16, None)])
    tomb_orphan = f"{ACCT}|{md5(FRIEND)}|message_0.db|98"       # 派生墓碑的孤儿：要留着
    con.execute("INSERT INTO derived_tombstone(msg_key, kind, deleted_at) VALUES (?, 'ocr', 0)",
                (tomb_orphan,))
    con.execute("INSERT INTO derived_tombstone(msg_key, kind, deleted_at) VALUES (?, 'asr', 0)",
                (rows[2]["msg_key"],))
    con.commit()
    ids = dict(con.execute("SELECT local_id, id FROM messages").fetchall())
    con.close()
    _downgrade_to_v9(db)

    con = store.connect(db)                                   # 触发 v10
    try:
        assert con.execute("SELECT version FROM schema_version WHERE scope='main'"
                           ).fetchone()[0] == store.SCHEMA_VERSION == 11
        got = dict(con.execute("SELECT local_id, msg_key FROM messages").fetchall())
        assert got == {r["local_id"]: r["msg_key"] for r in rows}, "改写结果须与 row_key 同形"
        assert dict(con.execute("SELECT local_id, id FROM messages").fetchall()) == ids
        # 回填缓存里的 4 段孤儿对不上任何日后导出的 5 段键：v11 删掉（重跑回填即恢复）
        assert {r[0] for r in con.execute("SELECT msg_key FROM media_refs")} == {k1}
        assert {r[0] for r in con.execute("SELECT msg_key FROM derived_tombstone")} \
            == {rows[2]["msg_key"], tomb_orphan}
        assert tuple(con.execute("SELECT msg_key, ref_msg_key FROM msg_parts").fetchone()) \
            == (rows[1]["msg_key"], k1)
        assert tuple(con.execute("SELECT derivation_id, msg_key FROM derived_text"
                                 ).fetchone()) == (f"{k1}|ocr", k1)
        assert tuple(con.execute("SELECT max_local_id, anchor_ts FROM watermark").fetchone()) \
            == (3, T0 + 3)
        con.execute("INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)")
    finally:
        con.close()


# ---------- 旧墓碑与旧键 ----------

@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "index.db")
    c = store.connect(tmp_path / "index.db")
    yield c
    c.close()


def test_a_legacy_four_segment_tombstone_still_blocks_the_message(con):
    """exclusions.db 不迁移：v10 前写下的 4 段墓碑必须照样挡住同前四段的 5 段消息。"""
    row = mk_row(ACCT, FRIEND, 1, "被销毁的话", ts=T0 + 1)
    legacy = messages.legacy_key(row["msg_key"])
    exclusions.add(con, [(legacy, ACCT, md5(FRIEND))])
    con.commit()
    assert store.upsert_batch(con, [row]) == 1, "旧墓碑没挡住：被删内容复活"
    assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert exclusions.is_excluded(con, row["msg_key"])


def test_count_unblocked_counts_a_legacy_tombstone_as_blocking(con):
    row = mk_row(ACCT, FRIEND, 1, "x", ts=T0 + 1)
    store.upsert_batch(con, [row])
    exclusions.add(con, [(messages.legacy_key(row["msg_key"]), ACCT, md5(FRIEND))])
    assert exclusions.count_unblocked(con, ACCT, FRIEND) == 0


def test_resolve_msg_key_unique_missing_and_ambiguous(con):
    a = mk_row(ACCT, FRIEND, 1, "一代", ts=T0 + 1)
    store.upsert_batch(con, [a])
    legacy = messages.legacy_key(a["msg_key"])
    assert store.resolve_msg_key(con, ACCT, legacy) == a["msg_key"]
    assert store.resolve_msg_key(con, ACCT, a["msg_key"]) == a["msg_key"]
    gone = f"{ACCT}|{md5(FRIEND)}|message_0.db|42"
    assert store.resolve_msg_key(con, ACCT, gone) == gone
    store.upsert_batch(con, [mk_row(ACCT, FRIEND, 1, "二代", ts=T0 + 500)])
    with pytest.raises(errors.SearchInputError, match="多条"):
        store.resolve_msg_key(con, ACCT, legacy)


def test_get_context_accepts_a_legacy_key(con):
    a = mk_row(ACCT, FRIEND, 1, "旧键也能取到", ts=T0 + 1)
    store.upsert_batch(con, [a])
    ctx = retrieval.get_context(messages.legacy_key(a["msg_key"]), account=ACCT)
    assert ctx["msg_key"] == a["msg_key"]
    assert [m["content"] for m in ctx["messages"]] == ["旧键也能取到"]


def test_split_and_legacy_key_shapes():
    k = messages.row_key("a", "m", "s", 3, 1700000000)
    assert k == "a|m|s|3|1700000000"
    assert messages.legacy_key(k) == "a|m|s|3" == messages.legacy_key("a|m|s|3")
    assert messages.split_key(k).create_time == 1700000000
    assert messages.split_key("a|m|s|3").create_time is None
    assert messages.split_key("a|m|s|x|1") is None and messages.split_key("a|m") is None


def test_derived_and_claims_accept_legacy_keys(con):
    """`derivation_id` 是 `{msg_key}|{kind}`：用户手里 v10 前的 id 唯一时照样能取到；
    事实证据给旧键时落库的是新键。"""
    from agent_wechat import derived
    a = mk_row(ACCT, FRIEND, 1, "图", ts=T0 + 1)
    store.upsert_batch(con, [a])
    con.execute("INSERT INTO derived_text(derivation_id, msg_key, account, chat_id, kind, text,"
                " engine, engine_version, source, quality, status, created_at)"
                " VALUES (?,?,?,?, 'ocr', 'x', 'e', '1', 's', 1.0, 'auto', 0)",
                (f"{a['msg_key']}|ocr", a["msg_key"], ACCT, FRIEND))
    con.commit()
    legacy = messages.legacy_key(a["msg_key"])
    assert derived.get_derived(f"{legacy}|ocr")["derivation_id"] == f"{a['msg_key']}|ocr"
    assert derived.delete_derived(f"{legacy}|ocr")["derivation_id"] == f"{a['msg_key']}|ocr"
    claim = derived.add_claim(ACCT, "某个事实", evidence=[legacy])
    assert [r[0] for r in con.execute("SELECT msg_key FROM fact_evidence WHERE claim_id=?",
                                      (claim["claim_id"],))] == [a["msg_key"]]


def test_a_legacy_key_of_a_purged_message_is_reported_as_destroyed(con):
    """消息已被 purge（行不在、墓碑是 5 段）：调用方手里的 4 段旧键换不成新键，判据也要认出
    它对应的 5 段墓碑——否则答的是「找不到」而不是「已被销毁」。"""
    row = mk_row(ACCT, FRIEND, 1, "x", ts=T0 + 1)
    exclusions.add(con, [(row["msg_key"], ACCT, md5(FRIEND))])
    con.commit()
    legacy = messages.legacy_key(row["msg_key"])
    assert exclusions.is_excluded(con, legacy)
    with pytest.raises(errors.DataNotFound, match="销毁"):
        retrieval.get_context(legacy, account=ACCT)


def test_resolve_msg_keys_dedupes_after_resolving(con):
    a = mk_row(ACCT, FRIEND, 1, "x", ts=T0 + 1)
    store.upsert_batch(con, [a])
    assert store.resolve_msg_keys(con, ACCT, [messages.legacy_key(a["msg_key"]), a["msg_key"]]) \
        == [a["msg_key"]]


def test_a_null_anchor_skips_the_rebuild_check(wx, monkeypatch):
    """v10 迁移回填不到锚点（水位线那行不在索引里）时为 NULL：不判，照常增量。"""
    wx([(1, T0 + 1, "一"), (2, T0 + 2, "二")])
    export.run_export(ACCT)
    with store.open() as c:
        c.execute("UPDATE watermark SET anchor_ts = NULL")
    wx([(1, T0 + 101, "别的一代"), (2, T0 + 102, "别的二"), (3, T0 + 103, "三")])
    resets = []
    monkeypatch.setattr(store, "reset_export_watermark", lambda *a: resets.append(a))
    assert export.run_export(ACCT, audit_rows=0) == 1
    assert resets == []


def test_a_rebuild_restarts_the_look_back_cursor_too(wx):
    """回看游标是上一代表的编号：重建后不归零，就从旧位置往后看、漏掉新表开头。"""
    wx([(i, T0 + i, f"一代{i}") for i in range(1, 4)])
    export.run_export(ACCT)
    export.run_export(ACCT, audit_rows=1)
    export.run_export(ACCT, audit_rows=1)              # 游标走到 2
    wx([(i, T0 + 100 + i, f"二代{i}") for i in range(1, 4)])
    export.run_export(ACCT, audit_rows=1)
    with store.open() as c:
        assert list(store.load_audit_cursors(c, ACCT).values()) == [1]
