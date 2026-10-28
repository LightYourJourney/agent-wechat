"""Phase 05：合成检索评测集（Recall@k / MRR / 证据命中 / 耗时 / 字符成本）。

**合成语料 + 主题假 embedder**：不读微信、不下载模型。评测只声称「在这个合成集上」的
数字；真实语料上的效果要用户自己跑 `agent-wechat retrieve` 才算数。
"""
from __future__ import annotations

import hashlib
import json
import math
import time

import pytest

from agent_wechat import messages, paths, retrieval, store, vector

ACCT = "wxid_testacct01_ab12"
CHAT_A = "wxid_friend01"
CHAT_B = "wxid_otherchat"
T0 = 1700000000
K = 5

TOPICS = {
    "hike": ("爬山", "登山", "徒步"),
    "food": ("火锅", "吃饭", "聚餐"),
    "work": ("评审", "会议", "项目"),
}


class TopicEmbedder:
    def _vec(self, text: str) -> list[float]:
        v = [0.0] * vector.DIM
        for i, words in enumerate(TOPICS.values()):
            if any(w in text for w in words):
                v[i] = 1.0
        if not any(v):
            v[9] = 1.0
        return v

    def embed(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


CORPUS = [
    (CHAT_A, 1, "周末爬山的安排定了吗"),
    (CHAT_A, 2, "那我们就去登山吧"),
    (CHAT_A, 3, "晚上吃火锅"),
    (CHAT_A, 4, "聚餐定在七点"),
    (CHAT_B, 1, "项目评审改到周四"),
    (CHAT_B, 2, "会议改到下午三点"),
    (CHAT_B, 3, "周三前把材料发我"),
]

# (query, 期望命中的 msg_key 位置, 是否属于「改写查询」——关键词无共同子串)
QUERIES = [
    ("爬山", (CHAT_A, 1), False),
    ("徒步", (CHAT_A, 2), True),
    ("火锅", (CHAT_A, 3), False),
    ("吃饭", (CHAT_A, 4), True),
    ("评审", (CHAT_B, 1), False),
    ("会议", (CHAT_B, 2), False),
    ("材料", (CHAT_B, 3), False),
]


def _key(chat: str, lid: int) -> str:
    return messages.row_key(ACCT, hashlib.md5(chat.encode()).hexdigest(),
                            "message_0.db", lid)


def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path)
    monkeypatch.setattr(vector, "MODEL_CACHE_DIR", tmp_path / "models")
    monkeypatch.delenv("AGENT_WECHAT_DENY", raising=False)
    embedder = TopicEmbedder()
    monkeypatch.setattr(vector, "default_embedder", lambda: embedder)
    rows = [messages.make_row(
        account=ACCT, chat_md5=hashlib.md5(chat.encode()).hexdigest(),
        chat_id=chat, shard="message_0.db", local_id=lid, ts=T0 + lid,
        msg_type=1, sender_id=None, sender_name=None, is_self=0,
        content=content, media_hint=None) for chat, lid, content in CORPUS]
    with store.open() as con:
        store.upsert_batch(con, rows)
    con = vector.connect()
    try:
        vector.build_index(accounts=[ACCT], embedder=embedder)
    finally:
        con.close()


def _evaluate(mode: str) -> dict:
    recalls, rrs, hits, chars, started = [], [], 0, 0, time.perf_counter()
    full_chars = 0
    for query, expected_pos, _paraphrase in QUERIES:
        res = retrieval.search_messages(query, account=ACCT, mode=mode, limit=K)
        keys = [h["msg_key"] for h in res["hits"]]
        expected = _key(*expected_pos)
        pos = keys.index(expected) if expected in keys else None
        recalls.append(1.0 if pos is not None else 0.0)
        rrs.append(1.0 / (pos + 1) if pos is not None else 0.0)
        hits += 1 if pos is not None else 0
        chars += len(json.dumps(res, ensure_ascii=False))
        full_chars += sum(
            len(next(c for ch, lid, c in CORPUS if _key(ch, lid) == h["msg_key"]))
            for h in res["hits"])
    elapsed = time.perf_counter() - started
    return {
        "mode": mode,
        "recall@5": round(sum(recalls) / len(recalls), 4),
        "mrr": round(sum(rrs) / len(rrs), 4),
        "evidence_hits": hits,
        "queries": len(QUERIES),
        "mean_ms": round(elapsed * 1000 / len(QUERIES), 2),
        "envelope_chars": chars,
        "full_text_chars": full_chars,
        "approx_tokens": math.ceil(chars / 1.5),   # 粗估：1 token ≈ 1.5 字符（中文）
    }


def test_hybrid_beats_or_matches_each_single_route(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    kw = _evaluate("keyword")
    sem = _evaluate("semantic")
    hy = _evaluate("hybrid")
    print("\nEVAL " + json.dumps([kw, sem, hy], ensure_ascii=False))

    assert hy["recall@5"] == 1.0, hy
    assert hy["recall@5"] >= kw["recall@5"]
    assert hy["recall@5"] >= sem["recall@5"]
    assert hy["mrr"] >= kw["mrr"], "双路命中的证据要排在只命中一路的前面"
    # 改写查询（关键词无共同子串）：关键词 0 命中，融合必须覆盖
    assert kw["recall@5"] < 1.0
    for query, expected_pos, paraphrase in QUERIES:
        if not paraphrase:
            continue
        kw_res = retrieval.search_messages(query, account=ACCT, mode="keyword")
        assert _key(*expected_pos) not in {h["msg_key"] for h in kw_res["hits"]}
        hy_res = retrieval.search_messages(query, account=ACCT, mode="hybrid")
        assert _key(*expected_pos) in {h["msg_key"] for h in hy_res["hits"]}


def test_a_long_message_costs_much_less_in_phase_one(tmp_path, monkeypatch):
    """阶段一披露的成本优势在**长消息**上才显著：片段 ≤200 字符，全文照旧可查。"""
    _env(tmp_path, monkeypatch)
    body = "长文档开头的结论。" + "细节说明。" * 200          # ≥1000 字符
    with store.open() as con:
        store.upsert_batch(con, [messages.make_row(
            account=ACCT, chat_md5=hashlib.md5(CHAT_B.encode()).hexdigest(),
            chat_id=CHAT_B, shard="message_0.db", local_id=9, ts=T0 + 99,
            msg_type=1, sender_id=None, sender_name=None, is_self=0,
            content=body, media_hint=None)])
    res = retrieval.search_messages("长文档开头", account=ACCT, mode="keyword", limit=1)
    hit = res["hits"][0]
    assert len(hit["snippet"]) <= retrieval.SNIPPET_CHARS
    ctx = retrieval.get_context(hit["msg_key"], account=ACCT, before=0, after=0)
    assert ctx["messages"][0]["content"] == body, "阶段二才给全文"
    assert len(body) > 5 * len(hit["snippet"])
