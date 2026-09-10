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


class FakeEmbedder:
    """确定性假 embedder：按文本查表出向量，不下载模型（spec §9 的依赖注入点）。"""

    def __init__(self, mapping=None, default=None):
        self.mapping = mapping or {}
        self.default = default or vec(1.0)
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [self.mapping.get(t, self.default) for t in texts]

    def embed_query(self, text):
        return self.mapping.get(text, self.default)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "index.db")
    con = vector.connect()          # 需 sqlite-vec 与 Row：断言直接查 vec_messages、按列名取值
    yield con
    con.close()


def test_build_index_embeds_text_and_skips_others(env):
    store.upsert_batch(env, [
        mk_row("acct", "chatA", 1, "明天上午十点开评审会", ts=1757462400),
        mk_row("acct", "chatA", 2, "嗯", ts=1757462460),                       # <2 字跳过
        mk_row("acct", "chatA", 3, "[image]", ts=1757462520, msg_type=3,
               media_hint="[image]"),                                          # 媒体跳过
        mk_row("acct", "chatA", 4, "[appmsg: 分享标题]", ts=1757462580, msg_type=49,
               media_hint="[appmsg: 分享标题]"),                                # appmsg 保留
    ])
    fake = FakeEmbedder()
    res = vector.build_index(embedder=fake)
    assert res == {"embedded": 2, "skipped": 2}
    meta = env.execute("SELECT embedded, skipped, model_id FROM vector_meta").fetchone()
    assert (meta["embedded"], meta["skipped"], meta["model_id"]) == (2, 2, vector.MODEL_ID)
    # 前缀文本确实进了 embedder（第 2 条带上了第 1 条的「上文」）
    flat = [t for call in fake.calls for t in call]
    assert any(f"[{vector._date(1757462400)}" in t and "明天上午十点开评审会" in t for t in flat)
    assert any("上文: " in t for t in flat)
    # acct_chat 分区键 = "{account}|{md5(chat_id)}"
    import hashlib
    parts = {r[0] for r in env.execute("SELECT DISTINCT acct_chat FROM vec_messages")}
    assert parts == {f"acct|{hashlib.md5('chatA'.encode()).hexdigest()}"}


def test_build_index_second_run_is_noop(env):
    store.upsert_batch(env, [mk_row("acct", "chatA", 1, "第一条消息")])
    assert vector.build_index(embedder=FakeEmbedder())["embedded"] == 1
    assert vector.build_index(embedder=FakeEmbedder())["embedded"] == 0      # 水位线生效


def test_build_index_resumes_after_new_rows(env):
    store.upsert_batch(env, [mk_row("acct", "chatA", 1, "第一条消息")])
    vector.build_index(embedder=FakeEmbedder())
    store.upsert_batch(env, [mk_row("acct", "chatA", 2, "第二条消息")])
    assert vector.build_index(embedder=FakeEmbedder())["embedded"] == 1      # 只补新增
    assert env.execute("SELECT count(*) FROM vec_messages").fetchone()[0] == 2


def test_build_index_rebuild_drops_and_reembeds(env):
    store.upsert_batch(env, [mk_row("acct", "chatA", 1, "第一条消息")])
    vector.build_index(embedder=FakeEmbedder())
    res = vector.build_index(rebuild=True, embedder=FakeEmbedder())
    assert res["embedded"] == 1
    assert env.execute("SELECT count(*) FROM vec_messages").fetchone()[0] == 1   # 未翻倍


def test_build_index_account_scoped(env):
    store.upsert_batch(env, [mk_row("acct1", "chatA", 1, "账号一的消息"),
                             mk_row("acct2", "chatA", 2, "账号二的消息")])
    assert vector.build_index(account="acct1", embedder=FakeEmbedder())["embedded"] == 1
    wm = {(r["account"], r["last_rowid"]) for r in
          env.execute("SELECT account, last_rowid FROM vector_watermark")}
    assert len(wm) == 1 and wm.pop()[0] == "acct1"


def test_build_index_mid_run_commit_keeps_rows_and_watermark_in_sync(env, monkeypatch):
    """中途提交路径：把阈值缩到 2，逼出 COMMIT_ROWS 分支；行数/水位线/返回值三者一致。"""
    monkeypatch.setattr(vector, "BATCH", 2)
    monkeypatch.setattr(vector, "COMMIT_ROWS", 2)
    store.upsert_batch(env, [mk_row("acct", "chatA", i, f"第 {i} 条需要嵌入的消息")
                             for i in range(1, 6)])

    res = vector.build_index(account="acct", embedder=FakeEmbedder())

    assert res == {"embedded": 5, "skipped": 0}
    assert env.execute("SELECT count(*) FROM vec_messages").fetchone()[0] == 5
    wm = env.execute("SELECT last_rowid FROM vector_watermark WHERE account='acct'").fetchone()
    assert wm["last_rowid"] == 5                   # 第 2、4 行各提交过一次，末批补齐第 5 行


class _FailingOnSecondBatch(FakeEmbedder):
    """第二次 embed 抛错：模拟两次提交之间进程被杀（此时新批还没写进 vec_messages）。"""

    def __init__(self):
        super().__init__()
        self.embed_calls = 0

    def embed(self, texts):
        self.embed_calls += 1
        if self.embed_calls == 2:
            raise RuntimeError("boom")
        return super().embed(texts)


def test_build_index_crash_mid_run_leaves_watermark_and_rows_consistent(env, monkeypatch):
    """崩溃后「已提交的向量行」必须等于水位线；续跑只补缺口、绝不重复嵌入。"""
    monkeypatch.setattr(vector, "BATCH", 2)
    monkeypatch.setattr(vector, "COMMIT_ROWS", 2)
    total = 5
    store.upsert_batch(env, [mk_row("acct", "chatA", i, f"第 {i} 条需要嵌入的消息")
                             for i in range(1, total + 1)])

    with pytest.raises(RuntimeError, match="boom"):
        vector.build_index(account="acct", embedder=_FailingOnSecondBatch())

    committed = env.execute("SELECT count(*) FROM vec_messages").fetchone()[0]
    wm = env.execute("SELECT last_rowid FROM vector_watermark WHERE account='acct'").fetchone()
    assert committed > 0, "第一批（2 行）应已在中段提交点落盘，未提交批在 con.close() 时回滚"
    assert wm is not None and wm["last_rowid"] == committed, "水位线必须与其批次同事务提交"

    res = vector.build_index(account="acct", embedder=FakeEmbedder())
    assert res["embedded"] == total - committed               # 只补未嵌入的行
    assert env.execute("SELECT count(*) FROM vec_messages").fetchone()[0] == total
    assert env.execute("SELECT count(DISTINCT msg_rowid) FROM vec_messages").fetchone()[0] == total
    wm2 = env.execute("SELECT last_rowid FROM vector_watermark WHERE account='acct'").fetchone()
    assert wm2["last_rowid"] == total
