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


def test_partial_index_on_ref_msg_key_exists(con):
    """回归：`idx_parts_ref` 是**部分索引**（`WHERE ref_msg_key IS NOT NULL`），
    「谁引用了这条消息」靠它。**原先没有任何夹具断言它存在**（T3 实施者发现）——
    从 DDL 里删掉那一行，原来的 5 条用例全绿。"""
    rows = con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='msg_parts'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "idx_parts_ref" in names, f"缺 idx_parts_ref，实际有 {names}"
    sql = next(r[1] for r in rows if r[0] == "idx_parts_ref")
    assert "ref_msg_key" in sql and "NOT NULL" in sql, f"应当是部分索引，实际 {sql!r}"


def test_upsert_is_idempotent_and_updates_on_conflict(con):
    store.upsert_msg_parts(con, [_row()])
    store.upsert_msg_parts(con, [_row(kind=5, blob=b"<msg>2</msg>")])
    got = store.msg_parts_by_keys(con, ["acct|deadbeef|message_0.db|1"])
    assert got["acct|deadbeef|message_0.db|1"] == (5, None, None, b"<msg>2</msg>")


def test_by_keys_returns_the_union_of_all_batches(con, monkeypatch):
    """回归：分批取回的结果必须**跨批累积**。

    夹具把 `KEYS_BATCH` 压到 2 再给 5 个 key —— 这样「把 `out = {}` 写进循环里」
    （只返回最后一批）会立刻红。

    **原稿不是这么写的**：它给 1200 个 key 断言「不许抛 + 一条不少」，那条**钉不住分批**
    ——SQLite 的变量上限自 3.32 起默认 32766（`store.py:33-38` 的注释自己就写着），
    1201 远未触顶，把分批整个去掉它依然全绿（T3 实施者用变异实测发现）。
    名字与夹具现按**实际可证伪的性质**对齐。
    """
    monkeypatch.setattr(store, "KEYS_BATCH", 2)
    rows = [_row(key=f"acct|deadbeef|message_0.db|{i}") for i in range(5)]
    store.upsert_msg_parts(con, rows)
    assert store.KEYS_BATCH == 2          # 前提自检：patch 真的生效
    got = store.msg_parts_by_keys(con, [r[0] for r in rows])
    assert len(got) == 5                  # 只返回最后一批会得到 1


def test_by_keys_omits_misses(con):
    """查不到的 key **不出现在结果里**（与 `media_refs_by_keys` 同约定）——
    上层靠「在不在字典里」区分「没有引用」与「有引用但没查到」。"""
    store.upsert_msg_parts(con, [_row()])
    got = store.msg_parts_by_keys(
        con, ["acct|deadbeef|message_0.db|1", "acct|deadbeef|message_0.db|99999"])
    assert len(got) == 1
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
