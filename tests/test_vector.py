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


def test_should_embed_rules():
    assert vector.should_embed(mk_row("a", "c", 1, "明天开会吗")) is True
    assert vector.should_embed(mk_row("a", "c", 1, "好的")) is True          # 2 字保留
    assert vector.should_embed(mk_row("a", "c", 1, "嗯")) is False           # <2 字跳过
    assert vector.should_embed(mk_row("a", "c", 1, "  ")) is False
    assert vector.should_embed(mk_row("a", "c", 1, None)) is False
    # appmsg：带标题才嵌入
    assert vector.should_embed(mk_row("a", "c", 1, "[appmsg: 分享标题]", msg_type=49)) is True
    assert vector.should_embed(mk_row("a", "c", 1, "[appmsg]", msg_type=49)) is False
    # 其余类型（图片/语音/视频/表情/位置/系统/撤回）一律跳过
    for t in (3, 34, 43, 47, 48, 51, 10000, 10002):
        assert vector.should_embed(mk_row("a", "c", 1, "[image]", msg_type=t,
                                          media_hint="[image]")) is False


def test_doc_text_format_with_and_without_prev():
    row = mk_row("a", "c", 1, "明天上午十点开评审会", is_self=0, sender_name="张三")
    row["ts"] = 1757462400                      # 2025-09-10 00:00 UTC
    text = vector.doc_text(row, None)
    # 用同一 helper 生成期望日期：断言格式，不依赖测试机时区
    assert text.startswith(f"[{vector._date(1757462400)} 张三]")
    assert text.endswith("| 明天上午十点开评审会")
    assert "上文" not in text

    with_prev = vector.doc_text(row, "上周说的那个方案定了吗" * 5)
    assert "上文: " in with_prev
    head = with_prev.split("上文: ", 1)[1].split("…", 1)[0]
    assert len(head) == 30                       # 前一条截断 30 字
    assert "\n" not in head


def test_doc_text_self_and_unknown_sender():
    me = mk_row("a", "c", 1, "我来安排", is_self=1)
    assert " 我]" in vector.doc_text(me, None)
    anon = mk_row("a", "c", 1, "你好", sender_id=None, sender_name=None)
    assert " ?]" in vector.doc_text(anon, None)


def test_query_text_adds_instruction():
    assert vector.query_text(" 爬山安排 ") == vector.QUERY_INSTRUCTION + "爬山安排"
