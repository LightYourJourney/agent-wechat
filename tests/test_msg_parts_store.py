# tests/test_msg_parts_store.py
"""msg_parts 的读写。夹具用临时库（store.connect(db_path)），绝不碰真实 index.db。"""
import sqlite3

import pytest

from agent_wechat import store


@pytest.fixture()
def con(tmp_path):
    c = store.connect(tmp_path / "index.db")
    yield c
    c.close()


def _row(key="acct|deadbeef|message_0.db|1", kind=57, svrid=None, ref=None, blob=b"<msg/>"):
    return (key, kind, svrid, ref, blob)


def test_schema_creates_msg_parts_without_touching_messages(con):
    """回归：新表不许动既有表。删掉 SCHEMA 里的 msg_parts 块这条会红。"""
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "msg_parts" in names and "messages" in names
    cols = [r[1] for r in con.execute("PRAGMA table_info('msg_parts')")]
    assert cols == ["msg_key", "appmsg_type", "ref_svrid", "ref_msg_key", "raw_xml"]


def test_upsert_is_idempotent_and_updates_on_conflict(con):
    store.upsert_msg_parts(con, [_row()])
    store.upsert_msg_parts(con, [_row(kind=5, blob=b"<msg>2</msg>")])
    got = store.msg_parts_by_keys(con, ["acct|deadbeef|message_0.db|1"])
    assert got["acct|deadbeef|message_0.db|1"] == (5, None, None, b"<msg>2</msg>")


def test_by_keys_batches_past_the_in_clause_and_omits_misses(con):
    """`KEYS_BATCH=500`：给 1200 个 key 不许抛，且**查不到的 key 不出现**（与 media_refs 同约定）。"""
    rows = [_row(key=f"acct|deadbeef|message_0.db|{i}") for i in range(1200)]
    store.upsert_msg_parts(con, rows)
    keys = [r[0] for r in rows] + ["acct|deadbeef|message_0.db|99999"]
    got = store.msg_parts_by_keys(con, keys)
    assert len(got) == 1200
    assert "acct|deadbeef|message_0.db|99999" not in got


def test_count_does_not_join_messages_and_uses_prefix_not_like(con):
    """回归：join 出来的「空」有歧义（回填只写 msg_parts）。
    account 里含 `_` —— 用 LIKE 会被 `_` 当通配符，把别的账号算进来。"""
    store.upsert_msg_parts(con, [
        _row(key="wxid_a_b|deadbeef|message_0.db|1"),
        _row(key="wxid_aXb|deadbeef|message_0.db|2"),   # `_` 若被当通配符，这条会被误计
        _row(key="wxid_c|deadbeef|message_0.db|3"),
    ])
    assert store.count_msg_parts(con, "wxid_a_b") == 1
    assert store.count_msg_parts(con, "wxid_c") == 1


def test_count_on_empty_table_is_zero_not_an_error(con):
    """空表 = 未回填，不是「没有 appmsg」——上层靠这个 0 判降级。"""
    assert store.count_msg_parts(con, "wxid_anything") == 0
