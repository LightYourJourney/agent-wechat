"""向量层：纯函数、索引管道、KNN 查询。默认用确定性假 embedder，不下载模型。"""
import argparse
import sqlite3

import pytest

from agent_wechat import paths, store, vector

from helpers import mk_row


def vec(*xs):
    """构造 512 维测试向量（前几维可控，其余为 0）。"""
    v = [0.0] * vector.DIM
    for i, x in enumerate(xs):
        v[i] = x
    return v


def test_store_vector_schema_dim_matches_model_dim():
    assert vector.DIM == store.VECTOR_DIM          # 维度真值只在 store，vector 是别名
    assert f"float[{store.VECTOR_DIM}]" in store.VECTOR_SCHEMA


def test_connect_creates_vector_tables(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path)   # 否则 store.connect 会对真实目录 mkdir + tmutil
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


def test_connect_wraps_extension_load_failure_in_vector_error(tmp_path, monkeypatch):
    """扩展加载失败必须是 VectorError（带可执行的修复指引），且不留半配置连接。"""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path)
    opened = []
    real_connect = store.connect

    def recording_connect(*args, **kwargs):
        con = real_connect(*args, **kwargs)
        opened.append(con)
        return con

    monkeypatch.setattr(store, "connect", recording_connect)

    def boom(con):
        raise sqlite3.OperationalError("boom")

    monkeypatch.setattr("sqlite_vec.load", boom)
    with pytest.raises(vector.VectorError, match="sqlite-vec"):
        vector.connect()
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")     # 失败路径必须关掉连接，不把半配置连接漏给调用方


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


def test_embedder_prefers_offline_loading_when_model_cached(monkeypatch):
    """模型已在本地时，默认 embedder 必须走 local_files_only=True（运行期零网络请求）。"""
    calls = []

    class FakeTE:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def embed(self, texts, batch_size=256):
            return [[0.0] * vector.DIM for _ in texts]

    monkeypatch.setattr("fastembed.TextEmbedding", FakeTE)
    vector.FastEmbedEmbedder().embed(["x"])
    assert len(calls) == 1 and calls[0]["local_files_only"] is True


def test_embedder_falls_back_to_download_only_when_cache_missing(monkeypatch):
    """缓存缺失（离线加载抛错）时才允许下载；显式 local_files_only 调用必须保持失败语义。"""
    calls = []

    class FakeTE:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            if kwargs.get("local_files_only"):
                raise ValueError("model not cached")

        def embed(self, texts, batch_size=256):
            return [[0.0] * vector.DIM for _ in texts]

    monkeypatch.setattr("fastembed.TextEmbedding", FakeTE)
    vector.FastEmbedEmbedder().embed(["x"])
    assert [c["local_files_only"] for c in calls] == [True, False]

    calls.clear()
    with pytest.raises(ValueError, match="not cached"):
        vector.FastEmbedEmbedder(local_files_only=True).embed(["x"])
    assert [c["local_files_only"] for c in calls] == [True]


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
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path)   # 隔离真实 ~/.agent-wechat
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
    assert res == {"embedded_this_run": 2, "skipped_this_run": 2}
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


def test_build_index_progress_is_monotonic_across_accounts(env):
    """多账号构建时进度必须单调递增，且终点等于总嵌入数。

    缺陷背景：`_build_account` 的计数是每账号局部的（各自从 0 起），`build_index`
    循环时未累加却把同一个 progress 回调透传下去，于是切换到第二个账号时 `rows`
    从大跳回小。MCP 默认 `account=None`（全部账号）正好踩中。
    """
    store.upsert_batch(env, [
        mk_row("acctA", "chatA", i, f"账号A的第{i}条消息") for i in (1, 2, 3)
    ] + [
        mk_row("acctB", "chatB", i, f"账号B的第{i}条消息") for i in (1, 2, 3)
    ])
    seen: list[int] = []
    res = vector.build_index(embedder=FakeEmbedder(), progress=seen.append)

    assert seen == sorted(seen), f"进度倒退: {seen}"
    assert seen[-1] == res["embedded_this_run"] == 6, f"进度终点不等于总嵌入数: {seen}"


def test_build_index_second_run_is_noop(env):
    store.upsert_batch(env, [mk_row("acct", "chatA", 1, "第一条消息")])
    assert vector.build_index(embedder=FakeEmbedder())["embedded_this_run"] == 1
    assert vector.build_index(embedder=FakeEmbedder())["embedded_this_run"] == 0   # 水位线生效


def test_build_index_resumes_after_new_rows(env):
    store.upsert_batch(env, [mk_row("acct", "chatA", 1, "第一条消息")])
    vector.build_index(embedder=FakeEmbedder())
    store.upsert_batch(env, [mk_row("acct", "chatA", 2, "第二条消息")])
    assert vector.build_index(embedder=FakeEmbedder())["embedded_this_run"] == 1   # 只补新增
    assert env.execute("SELECT count(*) FROM vec_messages").fetchone()[0] == 2


def test_build_index_rebuild_drops_and_reembeds(env):
    store.upsert_batch(env, [mk_row("acct", "chatA", 1, "第一条消息")])
    vector.build_index(embedder=FakeEmbedder())
    res = vector.build_index(rebuild=True, embedder=FakeEmbedder())
    assert res["embedded_this_run"] == 1
    assert env.execute("SELECT count(*) FROM vec_messages").fetchone()[0] == 1   # 未翻倍


def test_build_index_account_scoped(env):
    store.upsert_batch(env, [mk_row("acct1", "chatA", 1, "账号一的消息"),
                             mk_row("acct2", "chatA", 2, "账号二的消息")])
    assert vector.build_index(accounts=["acct1"], embedder=FakeEmbedder())["embedded_this_run"] == 1
    wm = {(r["account"], r["last_rowid"]) for r in
          env.execute("SELECT account, last_rowid FROM vector_watermark")}
    assert len(wm) == 1 and wm.pop()[0] == "acct1"


def test_build_index_accounts_list_selects_exactly_those_accounts(env):
    store.upsert_batch(env, [mk_row("acct1", "chatA", 1, "账号一的消息"),
                             mk_row("acct2", "chatA", 2, "账号二的消息")])
    res = vector.build_index(accounts=["acct1", "acct2"], embedder=FakeEmbedder())
    assert res == {"embedded_this_run": 2, "skipped_this_run": 0}
    got = {r[0] for r in env.execute("SELECT DISTINCT account FROM vec_messages")}
    assert got == {"acct1", "acct2"}


def test_build_index_empty_accounts_indexes_nothing(env):
    """accounts=[] 是「一个都不索引」，不能退化成「全部账号」——这正是复数参数名的价值。"""
    store.upsert_batch(env, [mk_row("acct", "chatA", 1, "不该被索引的消息")])
    res = vector.build_index(accounts=[], embedder=FakeEmbedder())
    assert res == {"embedded_this_run": 0, "skipped_this_run": 0}
    assert env.execute("SELECT count(*) FROM vec_messages").fetchone()[0] == 0


def test_build_index_return_counts_are_this_run_not_cumulative(env):
    """返回键是「本轮」计数：第二次运行水位线已到顶 → 本轮 0，而 vector_meta 累计值不变。"""
    store.upsert_batch(env, [mk_row("acct", "chatA", 1, "第一条消息"),
                             mk_row("acct", "chatA", 2, "嗯")])     # 1 嵌入 + 1 跳过
    assert vector.build_index(embedder=FakeEmbedder()) == {
        "embedded_this_run": 1, "skipped_this_run": 1}
    meta = env.execute("SELECT embedded, skipped FROM vector_meta").fetchone()
    assert (meta["embedded"], meta["skipped"]) == (1, 1)
    assert vector.build_index(embedder=FakeEmbedder()) == {
        "embedded_this_run": 0, "skipped_this_run": 0}
    meta2 = env.execute("SELECT embedded, skipped FROM vector_meta").fetchone()
    assert (meta2["embedded"], meta2["skipped"]) == (1, 1)   # 累计列不受本轮 0 影响


def test_build_index_counts_accumulate_across_accounts(env):
    """跨账号累加：两个账号各自的 embedded/skipped 汇总成本轮总数。"""
    store.upsert_batch(env, [
        mk_row("acctA", "chatA", 1, "账号A的消息"),
        mk_row("acctA", "chatA", 2, "嗯"),                                   # <2 字跳过
        mk_row("acctB", "chatB", 3, "账号B的消息"),
        mk_row("acctB", "chatB", 4, "[image]", msg_type=3, media_hint="[image]"),
    ])
    assert vector.build_index(embedder=FakeEmbedder()) == {
        "embedded_this_run": 2, "skipped_this_run": 2}


def test_build_index_large_batch_crosses_vec_chunk_boundary(env):
    """spec §9 的合成用例：vec0 内部按 1024 行分块（sqlite-vec issue #245 的回归点），
    跨过该边界时构建必须跑完，且一行不漏。"""
    n = 1100
    store.upsert_batch(env, [mk_row("acct", "chatA", i, f"第 {i} 条消息") for i in range(1, n + 1)])

    res = vector.build_index(embedder=FakeEmbedder())

    assert res["embedded_this_run"] == n
    assert env.execute("SELECT count(*) FROM vec_messages").fetchone()[0] == n
    max_id = env.execute("SELECT max(id) FROM messages").fetchone()[0]
    wm = env.execute("SELECT last_rowid FROM vector_watermark WHERE account='acct'").fetchone()
    assert wm["last_rowid"] == max_id


def test_build_index_mid_run_commit_keeps_rows_and_watermark_in_sync(env):
    """中途提交路径：小 batch/commit_rows 经接口注入（不再 monkeypatch 模块常量），
    逼出提交分支；行数/水位线/返回值三者一致。"""
    store.upsert_batch(env, [mk_row("acct", "chatA", i, f"第 {i} 条需要嵌入的消息")
                             for i in range(1, 6)])

    res = vector.build_index(accounts=["acct"], embedder=FakeEmbedder(),
                             batch=2, commit_rows=2)

    assert res == {"embedded_this_run": 5, "skipped_this_run": 0}
    assert env.execute("SELECT count(*) FROM vec_messages").fetchone()[0] == 5
    wm = env.execute("SELECT last_rowid FROM vector_watermark WHERE account='acct'").fetchone()
    assert wm["last_rowid"] == 5                   # 第 2、4 行各提交过一次，末批补齐第 5 行


def test_build_index_custom_batch_controls_embed_batch_size(env):
    """batch 经接口注入后同时决定分页步长与单次嵌入的行数上限（默认 256 时会是单批 5 行）。"""
    batches: list[int] = []

    class Counting(FakeEmbedder):
        def embed(self, texts):
            batches.append(len(texts))
            return super().embed(texts)

    store.upsert_batch(env, [mk_row("acct", "chatA", i, f"第 {i} 条需要嵌入的消息")
                             for i in range(1, 6)])
    res = vector.build_index(accounts=["acct"], embedder=Counting(), batch=2, commit_rows=4)

    assert batches == [2, 2, 1]
    assert res == {"embedded_this_run": 5, "skipped_this_run": 0}


def test_build_index_custom_commit_rows_sets_crash_boundary(env):
    """commit_rows 经接口注入后决定中途提交点：batch=2、commit_rows=4、第 5 行嵌失败时，
    已落盘的行与水位线都停在第 4 行（默认 commit_rows=5120 时这里会是 0 行）。"""

    class FailOnThirdBatch(FakeEmbedder):
        def __init__(self):
            super().__init__()
            self.embed_calls = 0

        def embed(self, texts):
            self.embed_calls += 1
            if self.embed_calls == 3:
                raise RuntimeError("boom")
            return super().embed(texts)

    store.upsert_batch(env, [mk_row("acct", "chatA", i, f"第 {i} 条需要嵌入的消息")
                             for i in range(1, 6)])
    with pytest.raises(RuntimeError, match="boom"):
        vector.build_index(accounts=["acct"], embedder=FailOnThirdBatch(),
                           batch=2, commit_rows=4)

    assert env.execute("SELECT count(*) FROM vec_messages").fetchone()[0] == 4
    wm = env.execute("SELECT last_rowid FROM vector_watermark WHERE account='acct'").fetchone()
    assert wm["last_rowid"] == 4


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


def test_build_index_crash_mid_run_leaves_watermark_and_rows_consistent(env):
    """崩溃后「已提交的向量行」必须等于水位线；续跑只补缺口、绝不重复嵌入。"""
    total = 5
    store.upsert_batch(env, [mk_row("acct", "chatA", i, f"第 {i} 条需要嵌入的消息")
                             for i in range(1, total + 1)])

    with pytest.raises(RuntimeError, match="boom"):
        vector.build_index(accounts=["acct"], embedder=_FailingOnSecondBatch(),
                           batch=2, commit_rows=2)

    committed = env.execute("SELECT count(*) FROM vec_messages").fetchone()[0]
    wm = env.execute("SELECT last_rowid FROM vector_watermark WHERE account='acct'").fetchone()
    assert committed > 0, "第一批（2 行）应已在中段提交点落盘，未提交批在 con.close() 时回滚"
    assert wm is not None and wm["last_rowid"] == committed, "水位线必须与其批次同事务提交"

    res = vector.build_index(accounts=["acct"], embedder=FakeEmbedder())
    assert res["embedded_this_run"] == total - committed      # 只补未嵌入的行
    assert env.execute("SELECT count(*) FROM vec_messages").fetchone()[0] == total
    assert env.execute("SELECT count(DISTINCT msg_rowid) FROM vec_messages").fetchone()[0] == total
    wm2 = env.execute("SELECT last_rowid FROM vector_watermark WHERE account='acct'").fetchone()
    assert wm2["last_rowid"] == total


def _seed_search_fixture(env):
    """三条消息、三个可区分向量：问爬山 → A 最相近。"""
    store.upsert_batch(env, [
        mk_row("acct", "chatA", 1, "周末爬山的安排定了吗", ts=1757462400),
        mk_row("acct", "chatB", 2, "明天的会议改到下午三点", ts=1757462500),
        mk_row("acct2", "chatA", 3, "别的账号的爬山消息", ts=1757462600),
    ])
    fake = FakeEmbedder(mapping={
        vector.query_text("爬山"): vec(1.0),
        vector.doc_text(mk_row("acct", "chatA", 1, "周末爬山的安排定了吗", ts=1757462400), None): vec(0.9, 0.1),
        vector.doc_text(mk_row("acct", "chatB", 2, "明天的会议改到下午三点", ts=1757462500), None): vec(0.0, 1.0),
        vector.doc_text(mk_row("acct2", "chatA", 3, "别的账号的爬山消息", ts=1757462600), None): vec(0.95, 0.05),
    }, default=vec(0.0, 1.0))
    vector.build_index(embedder=fake)
    return fake


def test_semantic_search_ranks_and_isolates_account(env):
    fake = _seed_search_fixture(env)
    res = vector.semantic_search("爬山", account="acct", limit=5, embedder=fake)
    assert res["mode"] == "semantic" and res["model"] == vector.MODEL_ID
    # KNN 只保证「最近的 k 条」，设计里没有相关性阈值（spec §6）：acct 下两条都会返回。
    # 刻意不断言条数——那会隐含一个设计里不存在的阈值；要钉的是排序与账号隔离。
    assert [h["chat_id"] for h in res["hits"]] == ["chatA", "chatB"]
    assert res["hits"][0]["distance"] < res["hits"][1]["distance"]
    assert res["hits"][0]["distance"] < 0.5
    assert all("别的账号" not in (h["content"] or "") for h in res["hits"])


def test_semantic_search_chat_filter_and_limit(env):
    fake = _seed_search_fixture(env)
    # chat 过滤真的限定在该会话：chatA 查询只返回 chatA 自己的那条
    a_hits = vector.semantic_search("会议", chat_id="chatA", account="acct", limit=5,
                                    embedder=fake)["hits"]
    assert [h["chat_id"] for h in a_hits] == ["chatA"]
    assert [h["content"] for h in a_hits] == ["周末爬山的安排定了吗"]
    hits = vector.semantic_search("会议", chat_id="chatB", account="acct", limit=5,
                                  embedder=fake)["hits"]
    assert [h["content"] for h in hits] == ["明天的会议改到下午三点"]
    one = vector.semantic_search("爬山", account="acct", limit=1, embedder=fake)
    assert len(one["hits"]) == 1


def test_semantic_search_errors(env):
    fake = FakeEmbedder()
    # C1 错误契约：core 不再返回 {"error"} dict，而是抛项目异常（hint 走异常属性），
    # 由适配层翻回同样的对外信封。故断言从「dict 字段」改为「异常类型+文案+hint」。
    with pytest.raises(vector.SearchInputError, match="semantic_search 需要"):
        vector.semantic_search("x", account="")
    with pytest.raises(vector.VectorError, match="向量索引为空") as e:
        vector.semantic_search("x", account="acct", embedder=fake)
    assert e.value.hint == "请先运行 build_vector_index"
    # 模型不匹配：手工塞一条别的 model_id 的 meta 行
    vector.build_index(embedder=fake)
    env.execute("UPDATE vector_meta SET model_id='fastembed:other:512'")
    env.commit()
    with pytest.raises(vector.VectorError, match="不一致") as e:
        vector.semantic_search("x", account="acct", embedder=fake)
    assert "rebuild" in e.value.hint


def test_semantic_search_requires_account_arg():
    import inspect
    sig = inspect.signature(vector.semantic_search)
    assert sig.parameters["account"].default == ""


def test_build_index_refuses_model_change_without_rebuild(env):
    """换模型不重建 = 往旧模型的向量表里追加新模型的向量 → 必须报错而非静默混用。"""
    store.upsert_batch(env, [mk_row("acct", "chatA", 1, "第一条消息")])
    vector.build_index(embedder=FakeEmbedder())
    env.execute("UPDATE vector_meta SET model_id='fastembed:other:512'")
    env.commit()
    with pytest.raises(vector.VectorError, match="rebuild"):
        vector.build_index(embedder=FakeEmbedder())
    assert vector.build_index(rebuild=True, embedder=FakeEmbedder())["embedded_this_run"] == 1


def test_semantic_search_accepts_degraded_md5_chat_id(env):
    """reader 对无法反查的会话返回 "md5:<hex>"（reader.py 的退化值）；
    把它当过滤条件时必须仍能命中，否则该会话静默搜不到任何东西。"""
    import hashlib
    row = mk_row("acct", "chatA", 1, "周末爬山的安排定了吗")
    store.upsert_batch(env, [row])
    fake = FakeEmbedder(mapping={
        vector.query_text("爬山"): vec(1.0),
        vector.doc_text(row, None): vec(0.9, 0.1),
    }, default=vec(1.0))
    vector.build_index(embedder=fake)
    degraded = "md5:" + hashlib.md5(b"chatA").hexdigest()
    res = vector.semantic_search("爬山", chat_id=degraded, account="acct", embedder=fake)
    assert res["total"] == 1 and res["hits"][0]["chat_id"] == "chatA"


def test_build_index_refuses_when_any_meta_row_is_foreign(env):
    """多行 meta（旧模型行 + 当前模型行）也必须被拦住——只看「最新一行」会漏，
    因为异模型行可能比当前模型行更旧（built_at 只有秒级分辨率）。"""
    store.upsert_batch(env, [mk_row("acct", "chatA", 1, "第一条消息")])
    vector.build_index(embedder=FakeEmbedder())
    env.execute("INSERT INTO vector_meta(model_id, dim, built_at, embedded, skipped) "
                "VALUES ('fastembed:other:512', 512, 1, 7, 0)")   # 比当前模型行更旧
    env.commit()
    with pytest.raises(vector.VectorError, match="rebuild"):
        vector.build_index(embedder=FakeEmbedder())


def test_model_guard_is_shared_by_build_and_search(env):
    """构建与查询的换模型守卫是同一个 helper：同一条件 → 逐字相同的文案与 hint。"""
    store.upsert_batch(env, [mk_row("acct", "chatA", 1, "第一条消息")])
    vector.build_index(embedder=FakeEmbedder())
    env.execute("UPDATE vector_meta SET model_id='fastembed:other:512'")
    env.commit()

    errs = []
    for call in (lambda: vector.build_index(embedder=FakeEmbedder()),
                 lambda: vector.semantic_search("x", account="acct", embedder=FakeEmbedder())):
        with pytest.raises(vector.VectorError) as e:
            call()
        errs.append(e.value)

    assert str(errs[0]) == str(errs[1])                # 文案同源
    assert "不一致" in str(errs[0]) and "rebuild" in str(errs[0])
    assert errs[0].hint == errs[1].hint and "rebuild" in errs[0].hint


def test_build_index_refuses_real_model_change(env, monkeypatch):
    """守卫的真正场景：模型常量变了（而非手工改 meta 行）。"""
    store.upsert_batch(env, [mk_row("acct", "chatA", 1, "第一条消息")])
    vector.build_index(embedder=FakeEmbedder())
    monkeypatch.setattr(vector, "MODEL_ID", "fastembed:other:512")
    with pytest.raises(vector.VectorError, match="rebuild"):
        vector.build_index(embedder=FakeEmbedder())


def test_semantic_search_model_mismatch_wins_over_empty(env):
    """存在异模型行且当前模型行 embedded=0 时，必须报「不一致」而非「索引为空」
    ——这正是两个检查顺序真正起作用的场景。"""
    store.upsert_batch(env, [mk_row("acct", "chatA", 1, "第一条消息")])
    vector.build_index(embedder=FakeEmbedder())
    env.execute("UPDATE vector_meta SET embedded = 0")
    env.execute("INSERT INTO vector_meta(model_id, dim, built_at, embedded, skipped) "
                "VALUES ('fastembed:other:512', 512, 1, 0, 0)")
    env.commit()
    with pytest.raises(vector.VectorError, match="不一致"):
        vector.semantic_search("x", account="acct", embedder=FakeEmbedder())


def test_semantic_search_rejects_non_positive_limit(env):
    """CLI 的 -n 可为负：不能把 k=-1 透传给 sqlite（会抛裸 OperationalError）。"""
    store.upsert_batch(env, [mk_row("acct", "chatA", 1, "第一条消息")])
    vector.build_index(embedder=FakeEmbedder())
    # C1：错误改为抛 SearchInputError，文案不变（原为返回 {"error": ...}）
    with pytest.raises(vector.SearchInputError, match="limit 必须为正整数"):
        vector.semantic_search("x", account="acct", limit=-1, embedder=FakeEmbedder())


def test_cmd_vector_build_prints_progress(tmp_path, monkeypatch, capsys):
    from agent_wechat import cli
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path)
    con = store.connect()
    store.upsert_batch(con, [mk_row("acct", "chatA", 1, "明天开评审会")])
    con.close()
    monkeypatch.setattr(vector, "default_embedder", lambda: FakeEmbedder())

    args = argparse.Namespace(account="acct", rebuild=False)
    assert cli.cmd_vector_build(args) == 0
    out = capsys.readouterr().out
    assert "完成" in out and "嵌入 1 行" in out


def test_build_index_embeds_length_sorted(env):
    """spec §7：同批文本按长度升序嵌入，压掉混合长度批次的 padding 浪费。"""
    store.upsert_batch(env, [
        mk_row("acct", "chatA", 1, "这是一条相当长的消息内容需要占用不少字符" * 6),
        mk_row("acct", "chatA", 2, "短消息"),
        mk_row("acct", "chatA", 3, "中等长度的消息内容大概这样子"),
    ])
    seen: list[list[str]] = []

    class Capturing(FakeEmbedder):
        def embed(self, texts):
            seen.append(list(texts))
            return [vec(1.0)] * len(texts)

    vector.build_index(embedder=Capturing())
    assert len(seen) == 1                       # 单批
    lens = [len(t) for t in seen[0]]
    assert lens == sorted(lens)                 # 批内按长度升序
