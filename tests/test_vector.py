"""向量层：纯函数、索引管道、KNN 查询。默认用确定性假 embedder，不下载模型。"""
import sqlite3

import pytest

from agent_wechat import store, vector


def mk_row(acct, chat, lid, content, *, ts=1700000000, msg_type=1, is_self=0,
           sender_id="wxid_a", sender_name=None, media_hint=None, shard="message_0.db"):
    import hashlib
    md5 = hashlib.md5(chat.encode()).hexdigest()
    return {"msg_key": f"{acct}|{md5}|{shard}|{lid}", "account": acct, "chat_md5": md5,
            "chat_id": chat, "shard": shard, "local_id": lid, "server_id": None,
            "ts": ts, "msg_type": msg_type, "sender_id": sender_id,
            "sender_name": sender_name, "is_self": is_self, "content": content,
            "media_hint": media_hint}


def vec(*xs):
    """构造 512 维测试向量（前几维可控，其余为 0）。"""
    v = [0.0] * vector.DIM
    for i, x in enumerate(xs):
        v[i] = x
    return v


def test_store_vector_schema_dim_matches_model_dim():
    assert f"float[{vector.DIM}]" in store.VECTOR_SCHEMA


def test_connect_creates_vector_tables(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "index.db")
    con = vector.connect()
    try:
        ver = con.execute("SELECT vec_version()").fetchone()[0]
        assert ver.startswith("v0.1")
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'vec_%' OR name LIKE 'vector_%'")}
        assert {"vec_messages", "vector_meta", "vector_watermark"} <= names
        assert con.row_factory is sqlite3.Row
    finally:
        con.close()
