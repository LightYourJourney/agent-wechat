"""Phase 03：向量索引一致性与重嵌队列。

真实 sqlite-vec + 假 embedder（`test_vector.FakeEmbedder`），全部在 tmp 里；不下载模型、
不碰真实用户库。要验证「改写后局部重嵌」，就必须能改 `messages`——所以每条用例自己种数据。
"""
from __future__ import annotations

import hashlib

import pytest
import sqlite_vec

from agent_wechat import errors, exclusions, messages, paths, purge, store, vector

from test_vector import FakeEmbedder, vec

T0 = 1700000000
ACCT = "acct_a"
ACCT_B = "acct_b"
CHAT = "wxid_friend01"
CHAT_MD5 = hashlib.md5(CHAT.encode()).hexdigest()


def _row(acct: str, chat: str, lid: int, content: str, *, msg_type: int = 1,
         ts: int | None = None):
    return messages.make_row(
        account=acct, chat_md5=hashlib.md5(chat.encode()).hexdigest(), chat_id=chat,
        shard="message_0.db", local_id=lid, ts=T0 + lid if ts is None else ts,
        msg_type=msg_type, sender_id=None, sender_name=None, is_self=0,
        content=content, media_hint=None)


def _key(lid: int, acct: str = ACCT, chat: str = CHAT) -> str:
    return messages.row_key(acct, hashlib.md5(chat.encode()).hexdigest(),
                            "message_0.db", lid)


@pytest.fixture()
def con(tmp_path, monkeypatch):
    """真实 sqlite-vec 连接（行工厂 sqlite3.Row），每例独立 index.db。"""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path)
    c = vector.connect()
    yield c
    c.close()


def _seed(c, texts, acct=ACCT, chat=CHAT):
    rows = [_row(acct, chat, i, t) for i, t in enumerate(texts, 1)]
    store.upsert_batch(c, rows)
    return rows


def _texts(fake) -> list[str]:
    return [t for batch in fake.calls for t in batch]


def _vec_count(c, acct=ACCT, chat=CHAT) -> int:
    return c.execute("SELECT count(*) FROM vec_messages WHERE acct_chat=?",
                     (f"{acct}|{hashlib.md5(chat.encode()).hexdigest()}",)).fetchone()[0]


# ---------------------------------------------------------------- sqlite-vec 能力探针

def test_vec0_supports_delete_insert_and_rollback(con):
    """任务书 §2：钉住我们**实际依赖**的 vec0 行为，别假设 UPDATE 语义。"""
    c = con
    c.execute("INSERT INTO vec_messages(msg_rowid, embedding, acct_chat, account, ts, preview)"
              " VALUES (1, ?, 'a|c', 'a', 1, '')",
              (sqlite_vec.serialize_float32(vec(1.0)),))
    c.commit()
    assert c.execute("SELECT count(*) FROM vec_messages WHERE msg_rowid=1").fetchone()[0] == 1

    # 事务内删除 → 回滚 → 行还在（我们依赖「向量写与队列删在同一事务」）
    c.execute("BEGIN IMMEDIATE")
    c.execute("DELETE FROM vec_messages WHERE msg_rowid=1")
    c.rollback()
    assert c.execute("SELECT count(*) FROM vec_messages WHERE msg_rowid=1").fetchone()[0] == 1

    # 先删后插（本模块采用的更新路径）
    c.execute("DELETE FROM vec_messages WHERE msg_rowid=1")
    c.execute("INSERT INTO vec_messages(msg_rowid, embedding, acct_chat, account, ts, preview)"
              " VALUES (1, ?, 'a|c', 'a', 1, 'new')",
              (sqlite_vec.serialize_float32(vec(0.5)),))
    c.commit()
    assert c.execute("SELECT preview FROM vec_messages WHERE msg_rowid=1").fetchone()[0] == "new"


# ---------------------------------------------------------------- 局部重嵌 vs 全量

def test_a_single_edit_reembeds_only_that_row_and_its_successor(con):
    """一条旧消息改写：只重嵌它 + 因前缀依赖而过时的后继，**不重建整库**。"""
    c = con
    _seed(c, ["第一句正文", "第二句正文", "第三句正文"])
    first = FakeEmbedder()
    assert vector.build_index(accounts=[ACCT], embedder=first)["embedded_this_run"] == 3

    store.upsert_batch(c, [_row(ACCT, CHAT, 2, "改后的第二句")])
    assert store.reembed_queue_stats(c, ACCT)["pending"] == 1

    local = FakeEmbedder()
    assert vector.build_index(accounts=[ACCT], embedder=local)["embedded_this_run"] == 2
    text = _texts(local)
    assert len(text) == 2, f"只该重嵌两条，实际 {text}"
    assert any("改后的第二句" in t for t in text)
    assert any("上文: 改后的第二句" in t for t in text), "后继的上文前缀必须跟着更新"

    # 全量重建做对照：调用次数 = 全部消息数（不编造「快了多少」）
    full = FakeEmbedder()
    vector.build_index(accounts=[ACCT], rebuild=True, embedder=full)
    assert len(_texts(full)) == 3

    assert store.reembed_queue_stats(c, ACCT)["pending"] == 0
    assert _vec_count(c) == 3


def test_repeated_edits_before_consumption_coalesce(con):
    c = con
    _seed(c, ["甲方正文", "乙方正文"])
    vector.build_index(accounts=[ACCT], embedder=FakeEmbedder())
    for i in range(3):
        store.upsert_batch(c, [_row(ACCT, CHAT, 1, f"第{i}次改写")])
    assert store.reembed_queue_stats(c, ACCT)["pending"] == 1, "队列是集合，不是日志"
    fake = FakeEmbedder()
    vector.build_index(accounts=[ACCT], embedder=fake)
    assert any("第2次改写" in t for t in _texts(fake)), "消费的是**当前**内容"


# ---------------------------------------------------------------- 失败与恢复

def test_embed_failure_keeps_the_queue_and_records_attempts(con):
    c = con
    _seed(c, ["一条正文"])
    vector.build_index(accounts=[ACCT], embedder=FakeEmbedder())
    store.upsert_batch(c, [_row(ACCT, CHAT, 1, "改后正文")])

    class Boom(FakeEmbedder):
        def embed(self, texts):
            raise RuntimeError("模型炸了")

    with pytest.raises(errors.VectorError):
        vector.build_index(accounts=[ACCT], embedder=Boom())
    stats = store.reembed_queue_stats(c, ACCT)
    assert stats["pending"] == 1 and stats["failed"] == 1 and stats["max_attempts"] == 1
    # 失败不能丢工作：换一个好 embedder 就收敛
    good = FakeEmbedder()
    vector.build_index(accounts=[ACCT], embedder=good)
    assert store.reembed_queue_stats(c, ACCT) == {
        "pending": 0, "failed": 0, "max_attempts": 0}
    assert any("改后正文" in t for t in _texts(good))


def test_bounded_drain_resumes_after_a_restart(con):
    c = con
    _seed(c, ["甲方", "乙方", "丙方", "丁方"])
    vector.build_index(accounts=[ACCT], embedder=FakeEmbedder())
    for lid in range(1, 5):
        store.upsert_batch(c, [_row(ACCT, CHAT, lid, f"改{lid}")])
    assert store.reembed_queue_stats(c, ACCT)["pending"] == 4

    fake = FakeEmbedder()
    first = vector.apply_dirty(c, accounts=[ACCT], limit=2, embedder=fake)
    assert first["embedded"] == 2
    assert store.reembed_queue_stats(c, ACCT)["pending"] >= 2

    # 「重启」：新连接接着消费，不从头再来
    fresh = vector.connect()
    try:
        rest = vector.apply_dirty(fresh, accounts=[ACCT], limit=10, embedder=fake)
        assert rest["embedded"] >= 2
        assert store.reembed_queue_stats(fresh, ACCT)["pending"] == 0
    finally:
        fresh.close()


# ---------------------------------------------------------------- 陈旧 / 销毁保护

def test_purged_chat_does_not_get_vecs_back(con):
    c = con
    _seed(c, ["甲方正文", "乙方正文"])
    vector.build_index(accounts=[ACCT], embedder=FakeEmbedder())
    store.upsert_batch(c, [_row(ACCT, CHAT, 1, "改后")])
    purge.execute_chat(purge.plan_chat(
        [{"account": ACCT, "chat_id": CHAT, "chat_md5": CHAT_MD5}]))

    assert store.reembed_queue_stats(c, ACCT)["pending"] == 0, "purge 连队列一起清"
    fake = FakeEmbedder()
    vector.build_index(accounts=[ACCT], embedder=fake)
    assert _vec_count(c) == 0
    assert _texts(fake) == [], "被销毁的数据一条都不许重嵌"


def test_excluded_message_is_dropped_not_embedded(con):
    c = con
    key = _key(1)
    exclusions.add(c, [(key, ACCT, CHAT_MD5)])
    c.commit()
    store.enqueue_reembed(c, key)
    c.commit()
    fake = FakeEmbedder()
    stats = vector.apply_dirty(c, accounts=[ACCT], embedder=fake)
    assert stats["dropped"] == 1 and stats["embedded"] == 0
    assert _texts(fake) == []
    assert store.reembed_queue_stats(c, ACCT)["pending"] == 0


def test_type_change_removes_the_old_vector(con):
    c = con
    _seed(c, ["会被改成图片"])
    vector.build_index(accounts=[ACCT], embedder=FakeEmbedder())
    assert _vec_count(c) == 1
    store.upsert_batch(c, [_row(ACCT, CHAT, 1, "[image]", msg_type=messages.TYPE_IMAGE)])
    fake = FakeEmbedder()
    stats = vector.apply_dirty(c, accounts=[ACCT], embedder=fake)
    assert stats["dropped"] == 1 and _vec_count(c) == 0 and _texts(fake) == []


def test_message_gone_from_index_is_dequeued(con):
    """消息行没了 → 队列作废、不嵌入；搜索也查不到（vec 行与 messages 的 join 过滤）。"""
    c = con
    _seed(c, ["会消失"])
    vector.build_index(accounts=[ACCT], embedder=FakeEmbedder())
    key = _key(1)
    store.enqueue_reembed(c, key)
    c.execute("DELETE FROM messages WHERE msg_key=?", (key,))
    c.commit()
    fake = FakeEmbedder()
    stats = vector.apply_dirty(c, accounts=[ACCT], embedder=fake)
    assert stats["dropped"] == 1 and _texts(fake) == []
    assert store.reembed_queue_stats(c, ACCT)["pending"] == 0


# ---------------------------------------------------------------- 多账号 / 模型

def test_multi_account_drain_is_scoped(con):
    c = con
    _seed(c, ["A 的正文"], acct=ACCT, chat="wxid_a")
    _seed(c, ["B 的正文"], acct=ACCT_B, chat="wxid_b")
    vector.build_index(accounts=[ACCT, ACCT_B], embedder=FakeEmbedder())
    store.upsert_batch(c, [_row(ACCT, "wxid_a", 1, "A 改后")])
    store.upsert_batch(c, [_row(ACCT_B, "wxid_b", 1, "B 改后")])
    fake = FakeEmbedder()
    vector.apply_dirty(c, accounts=[ACCT], limit=10, embedder=fake)
    assert any("A 改后" in t for t in _texts(fake))
    assert not any("B 改后" in t for t in _texts(fake))
    assert store.reembed_queue_stats(c, ACCT)["pending"] == 0
    assert store.reembed_queue_stats(c, ACCT_B)["pending"] == 1


def test_model_mismatch_refuses_before_touching_the_queue(con):
    c = con
    _seed(c, ["正文"])
    vector.build_index(accounts=[ACCT], embedder=FakeEmbedder())
    store.upsert_batch(c, [_row(ACCT, CHAT, 1, "改后")])
    c.execute("INSERT INTO vector_meta VALUES ('other-model', 512, 0, 0, 0)")
    c.commit()
    with pytest.raises(errors.VectorError, match="不一致"):
        vector.build_index(accounts=[ACCT], embedder=FakeEmbedder())
    assert store.reembed_queue_stats(c, ACCT)["pending"] == 1, "队列不许被动"


def test_rebuild_clears_the_queue(con):
    c = con
    _seed(c, ["甲方正文", "乙方正文"])
    vector.build_index(accounts=[ACCT], embedder=FakeEmbedder())
    store.upsert_batch(c, [_row(ACCT, CHAT, 1, "改后")])
    assert store.reembed_queue_stats(c, ACCT)["pending"] == 1
    fake = FakeEmbedder()
    res = vector.build_index(accounts=[ACCT], rebuild=True, embedder=fake)
    assert len(_texts(fake)) == 2, "全量重建会按当前内容重嵌所有行"
    assert res["embedded_this_run"] == 2
    assert store.reembed_queue_stats(c, ACCT)["pending"] == 0


# ---------------------------------------------------------------- 新鲜度诊断

def test_freshness_reports_queue_and_watermark(con):
    c = con
    _seed(c, ["甲方正文"])
    vector.build_index(accounts=[ACCT], embedder=FakeEmbedder())
    store.upsert_batch(c, [_row(ACCT, CHAT, 1, "改后")])
    info = vector.freshness(c, ACCT)
    assert info["model"] == vector.MODEL_ID
    assert info["pending"] == 1 and info["failed"] == 0
    assert info["embedded"] == 1
    assert info["watermark"] == {ACCT: 1}
