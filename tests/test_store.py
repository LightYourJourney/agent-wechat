"""store 的读路径。**注意**：store.connect() 会调 paths.ensure_home_dirs()，
不隔离 HOME 就会在真实的 ~/.agent-wechat 下建目录 —— 夹具必须挡住这一手。
"""
from __future__ import annotations

import pytest

from agent_wechat import paths, store


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path / "home")
    c = store.connect(tmp_path / "index.db")
    yield c
    c.close()


def test_media_refs_by_keys_returns_only_present(con):
    """缺失的 key 不进结果 —— 上层据此区分「没有媒体」与「有媒体但无 ref」。"""
    store.upsert_media_refs(con, [("k1", "image", "aa" * 16, None),
                                  ("k2", "video", "bb" * 16, None)])
    assert store.media_refs_by_keys(con, ["k1", "k2", "nope"]) == {
        "k1": ("image", "aa" * 16), "k2": ("video", "bb" * 16)}


def test_media_refs_by_keys_empty_input_is_empty_dict(con):
    """空输入不产生 `IN ()` 语法错，也不去查库。"""
    assert store.media_refs_by_keys(con, []) == {}


def test_media_refs_by_keys_keeps_null_file_key(con):
    """file_key 可以为 NULL（packed 与 xml md5 都没命中）—— 要如实带出 None，不能丢行。"""
    store.upsert_media_refs(con, [("k9", "image", None, "cc" * 16)])
    assert store.media_refs_by_keys(con, ["k9"]) == {"k9": ("image", None)}


def test_media_refs_by_keys_spans_batches_and_tolerates_duplicate_keys(con):
    """跨批次不丢行 + 重复输入 key 不出乱子。

    承重的是前半句：900 个 key 按批大小 500 切成两批，批次边界（第 500/501 个）丢一行
    就会红 —— 变异成 `uniq[i:i + batch - 1]` 时确实 red（len(got) == 899）。
    后半句只保证「重复 key 不会把结果搞坏」：`dict.fromkeys` 那步去重是纯优化，
    去掉它返回的字典逐字节相同（结果按 msg_key 键控），从公开契约上不可观测，
    故**故意不为其设断言**——断言查询次数就是在测实现细节。
    """
    store.upsert_media_refs(con, [(f"k{i}", "image", "dd" * 16, None) for i in range(900)])
    keys = [f"k{i}" for i in range(900)] + ["k0"]        # 重复的 key 只是输入噪声：结果按 msg_key 键控，重复不会导致重复计数
    got = store.media_refs_by_keys(con, keys)
    assert len(got) == 900 and got["k899"] == ("image", "dd" * 16)

def test_count_media_refs_scopes_by_chat_ids_and_kind(con):
    """EA-078①：`count_media_refs` 的 `chat_ids=` 与 `kind=` 两个过滤面此前零用例。

    `chat_ids` 经 md5 派生前缀限流（`messages.md5_of`），`kind` 只数该类型——
    counting 不 join messages（rebuild-refs 只写 refs），两个过滤都必须独立成立。
    """
    from agent_wechat import messages

    a_md5 = messages.md5_of("wxid_abc123def456")
    b_md5 = messages.md5_of("wxid_testacct01_ab12")
    store.upsert_media_refs(con, [
        (f"acct|{a_md5}|message_0.db|1", "image", "aa" * 16, None),
        (f"acct|{a_md5}|message_0.db|2", "video", "bb" * 16, None),
        (f"acct|{b_md5}|message_0.db|1", "video", "cc" * 16, None),
    ])
    assert store.count_media_refs(con, "acct") == 3
    assert store.count_media_refs(con, "acct", kind="video") == 2
    assert store.count_media_refs(con, "acct", chat_ids=["wxid_abc123def456"]) == 2
    assert store.count_media_refs(con, "acct", chat_ids=["wxid_abc123def456"],
                                  kind="video") == 1
    assert store.count_media_refs(con, "acct", chat_ids=["wxid_x01"]) == 0   # 未种入的会话


def test_media_refs_by_chat_filters_kind_and_spans_batches(con, monkeypatch):
    """EA-078②：`media_refs_by_chat` 的 kind 过滤 + 跨批累积。

    夹具把 `KEYS_BATCH` 压到 2：把 `out`/累积写进循环（只留最后一批）会红；
    kind 过滤去掉会把 image 混进 sticker 结果。
    """
    from agent_wechat import messages

    md5a = messages.md5_of("wxid_abc123def456")
    md5b = messages.md5_of("wxid_testacct01_ab12")
    rows = [(f"acct|{md5a}|message_0.db|{i}", "sticker", "ab" * 16, None) for i in range(1, 6)]
    rows.append((f"acct|{md5a}|message_0.db|9", "image", "aa" * 16, None))
    rows += [(f"acct|{md5b}|message_0.db|{i}", "sticker", "cd" * 16, None) for i in range(1, 4)]
    md5c = messages.md5_of("wxid_x01")            # 第三个会话：3 会话 × 批大小 2 = **真跨批**
    rows += [(f"acct|{md5c}|message_0.db|1", "sticker", "ef" * 16, None)]
    # refs 的读取 join `messages` 取 ts/local_id 排序——没有消息行就一条都取不到
    store.upsert_batch(con, [messages.make_row(
        account="acct", chat_md5=md5a, chat_id="wxid_abc123def456", shard="message_0.db",
        local_id=i, ts=1700000000 + i, msg_type=49, sender_id="wxid_abc123def456",
        sender_name=None, is_self=0, content=f"[sticker] {i}", media_hint=None)
        for i in list(range(1, 6)) + [9]]
        + [messages.make_row(
            account="acct", chat_md5=md5b, chat_id="wxid_testacct01_ab12",
            shard="message_0.db", local_id=i, ts=1700001000 + i, msg_type=49,
            sender_id="wxid_testacct01_ab12", sender_name=None, is_self=0,
            content=f"[sticker] b{i}", media_hint=None) for i in range(1, 4)]
        + [messages.make_row(
            account="acct", chat_md5=md5c, chat_id="wxid_x01", shard="message_0.db",
            local_id=1, ts=1700002000, msg_type=49, sender_id="wxid_x01",
            sender_name=None, is_self=0, content="[sticker] c1", media_hint=None)])
    store.upsert_media_refs(con, rows)
    monkeypatch.setattr(store, "KEYS_BATCH", 2)
    assert store.KEYS_BATCH == 2          # 前提自检：patch 真的生效
    # F4：**必须传多个会话**——分批是按 chat_id 数切的（KEYS_BATCH=500），只传一个时
    # `range(0, 1, 2)` 只迭代一次，「跨批累积」形同虚设（复审 M16：把 out 移进循环仍然全绿）。
    got = store.media_refs_by_chat(con, "acct",
                                   ["wxid_abc123def456", "wxid_testacct01_ab12", "wxid_x01"],
                                   kind="sticker")
    rows_a = got["wxid_abc123def456"]
    rows_b = got["wxid_testacct01_ab12"]
    rows_c = got["wxid_x01"]
    # 3 会话 × 批大小 2 → `range(0,3,2)` 两次迭代——**这才真的跨批**（2 会话配 2 仍是一次，
    # 复审 F4 的 M16 就是在这个缝里全绿的）
    assert len(rows_a) == 5 and len(rows_b) == 3 and len(rows_c) == 1, "跨批累积丢了行"
    assert all(k.count("|") == 3 for k, _ts, _fk in rows_a + rows_b + rows_c)
    assert [ts for _k, ts, _fk in rows_a] == sorted(ts for _k, ts, _fk in rows_a), "会话内按 ts 升序"


def test_count_media_refs_takes_a_degraded_chat_id(con):
    """EA-011：`count_media_refs(chat_ids=…)` 必须走 `messages.chat_md5()`——
    退化形态（`md5:<hex>`）经 `md5_of()` 会被**再哈希一次**，与 msg_key 第二段分家、
    计数恒为 0（而 `messages` 侧同族的另三处判据都用 `chat_md5()`）。
    """
    from agent_wechat import messages

    real = "wxid_abc123def456"
    md5v = messages.chat_md5(real)
    deg = messages.degraded_chat_id(md5v)          # "md5:<hex>"
    assert messages.md5_of(deg) != md5v, "前提：md5_of 对退化形态会再哈希一次"
    store.upsert_media_refs(con, [(f"acct|{md5v}|message_0.db|1", "image", "aa" * 16, None)])
    assert store.count_media_refs(con, "acct", chat_ids=[deg]) == 1
    assert store.count_media_refs(con, "acct", chat_ids=[deg], kind="image") == 1
    assert store.count_media_refs(con, "acct", chat_ids=[deg], kind="video") == 0


def test_upsert_corrects_a_degraded_chat_id_in_place(con):
    """EA-012 ①：同 key 重扫、`chat_id` 由退化形态变真名 → **就地修正**。

    修前 `chat_id` 不在 DO UPDATE 集里，行永远停在首次写入的退化 id 上
    （`WHERE messages.content IS NOT excluded.content` 也不会被触发）。
    """
    from agent_wechat import messages

    real = "wxid_abc123def456"
    md5v = messages.chat_md5(real)
    deg = messages.degraded_chat_id(md5v)
    key = f"acct|{md5v}|message_0.db|1"
    row = messages.make_row(account="acct", chat_md5=md5v, chat_id=deg, shard="message_0.db",
                            local_id=1, ts=1700000000, msg_type=1, sender_id=None,
                            sender_name=None, is_self=0, content="同一句正文", media_hint=None)
    store.upsert_batch(con, [row])
    assert con.execute("SELECT chat_id FROM messages WHERE msg_key=?", (key,)).fetchone()[0] == deg

    store.upsert_batch(con, [{**row, "chat_id": real}])
    assert con.execute("SELECT chat_id FROM messages WHERE msg_key=?", (key,)).fetchone()[0] == real


def test_upsert_touches_a_row_whose_chat_id_changed_even_if_content_did_not(con):
    """EA-012 ②：content 不变、只有 `chat_id` 变 → 也必须更新（守卫生效的判别面）。

    把守卫改回「只认 content」，本用例必红。
    """
    import time

    from agent_wechat import messages

    md5v = messages.chat_md5("wxid_abc123def456")
    key = f"acct|{md5v}|message_0.db|1"
    base = dict(account="acct", chat_md5=md5v, shard="message_0.db", local_id=1,
                ts=1700000000, msg_type=1, sender_id=None, sender_name=None, is_self=0,
                content="同一句正文", media_hint=None)
    store.upsert_batch(con, [messages.make_row(chat_id="wxid_abc123def456", **base)])
    before = con.execute("SELECT exported_at FROM messages WHERE msg_key=?", (key,)).fetchone()[0]
    time.sleep(1.1)      # exported_at 是秒级：跨 1 秒才能判别「真的写过」
    store.upsert_batch(con, [messages.make_row(chat_id="wxid_testacct01_ab12", **base)])
    after = con.execute("SELECT exported_at, chat_id FROM messages WHERE msg_key=?",
                        (key,)).fetchone()
    assert after[1] == "wxid_testacct01_ab12"
    assert after[0] > before, "守卫没放行这次更新（exported_at 未推进）"


def test_upsert_keeps_chat_row_counts_consistent_after_the_fix(con):
    """EA-012 ③：分裂场景消失后，同一 chat_md5 的 messages 与 parts/refs 计数同数
    （前缀按 md5 数——修复前 `chat_row_counts` 报 messages:1 而 parts/refs:2 的「各说各话」）。"""
    from agent_wechat import messages

    md5v = messages.chat_md5("wxid_abc123def456")
    deg = messages.degraded_chat_id(md5v)
    store.upsert_batch(con, [
        messages.make_row(account="acct", chat_md5=md5v, chat_id=deg, shard="message_0.db",
                          local_id=1, ts=1700000000, msg_type=49, sender_id=None,
                          sender_name=None, is_self=0, content="<x/>", media_hint=None),
        messages.make_row(account="acct", chat_md5=md5v, chat_id="wxid_abc123def456",
                          shard="message_0.db", local_id=2, ts=1700000001, msg_type=49,
                          sender_id=None, sender_name=None, is_self=0, content="<y/>",
                          media_hint=None)])
    store.upsert_msg_parts(con, [
        (messages.row_key("acct", md5v, "message_0.db", i), 5, None, None, b"<x/>")
        for i in (1, 2)])
    con.commit()

    # 名字解析回来后重扫（同 key、同 content）——EA-012 之前，退化那条会**留在原地**
    # （守卫只认 content），于是同一 chat_md5 上 messages 只有 1 条而 parts/refs 有 2 条。
    store.upsert_batch(con, [
        messages.make_row(account="acct", chat_md5=md5v, chat_id="wxid_abc123def456",
                          shard="message_0.db", local_id=i, ts=1700000000 + i, msg_type=49,
                          sender_id=None, sender_name=None, is_self=0, content=c,
                          media_hint=None)
        for i, c in ((1, "<x/>"), (2, "<y/>"))])
    counts = store.chat_row_counts(con, "acct", "wxid_abc123def456")
    assert counts["messages"] == counts["parts"] == 2, counts


def test_schema_drift_notes_detect_a_stale_trigger_body(tmp_path, monkeypatch):
    """EA-077：`CREATE TRIGGER IF NOT EXISTS` 只挡创建、不挡演进——老库留着旧触发器体，
    代码改了 DDL 也不会生效（FTS 镜像与 EA-012 的修正集全靠它）。自检必须**报出来**。

    新库 → 无 note；把触发器体改掉重建 → 恰好报那一条。
    """
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path / "home")
    con = store.connect(tmp_path / "i.db")
    try:
        assert store.schema_drift_notes(con) == [], "新库不该报漂移"
        con.execute("DROP TRIGGER msg_fts_au")
        con.execute("CREATE TRIGGER msg_fts_au AFTER UPDATE OF content ON messages "
                    "BEGIN SELECT 1; END")
        con.commit()
        notes = store.schema_drift_notes(con)
        assert len(notes) == 1 and "msg_fts_au" in notes[0], notes
    finally:
        con.close()


def test_search_reports_a_damaged_index_instead_of_silently_zero(con, monkeypatch):
    """EA-013（= EA-078③ 的 FTS 失败路径）：`messages_fts` 被删后，`search()` 不许再
    静默答「0 命中，mode=fts」——降级为 like 并随 `degraded` 说明「索引不完整 + 重建路径」；
    而**非**索引类的 OperationalError（编程错误）要响亮地抛。

    把 except 收窄回 `except sqlite3.OperationalError: rows = []`，本用例必红。
    """
    import sqlite3

    from agent_wechat import messages, search

    con.execute("DROP TABLE messages_fts")
    con.commit()
    # 注入这条连接：`store.open()` 会按 SCHEMA 把删掉的 FTS 表**重建**回来（IF NOT EXISTS），
    # 直接调它测不到损坏形态。
    monkeypatch.setattr(store, "open", lambda: _Ctx(con))
    res = search.search("爬山", account="acct")
    assert res["mode"] == "like", res
    assert res["degraded"] and "索引库结构不完整" in res["degraded"][0]["effect"], res

    # 编程错误（列名拼错 → no such column）也走「索引损坏」一支是刻意的：
    # 两者都无法降级、都必须让人看见；这里钉住的是「不再静默成 0 命中」这一点。
    assert res["total"] == 0


def test_search_raises_on_an_unclassified_sqlite_error(con, monkeypatch):
    """EA-013：不认识的 OperationalError 一律原样重抛成项目异常（响亮），不吞。"""
    import sqlite3

    from agent_wechat import errors, search

    class _Boom:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("out of memory")

    monkeypatch.setattr(store, "open", lambda: _Ctx(_Boom()))
    import pytest as _pytest
    with _pytest.raises(errors.SearchInputError):
        search.search("爬山", account="acct")


class _Ctx:
    def __init__(self, con):
        self._con = con

    def __enter__(self):
        return self._con

    def __exit__(self, *a):
        return False


def test_search_rejects_blank_query_and_escapes_like_wildcards(con, monkeypatch):
    """EA-073：① 空/全空白 query 在入口拒绝（此前退化成 `LIKE '%%'` 全表匹配）；
    ② LIKE 支路把 `%` / `_` 当**字面量**（FTS 支路是短语匹配，不受影响——只在 LIKE 支路转义）。

    去掉转义（或 `ESCAPE`），第二条断言必红：`a%` 会以通配符命中「abc」。
    """
    import pytest as _pytest

    from agent_wechat import errors, messages, search

    with _pytest.raises(errors.SearchInputError):
        search.search("   ", account="acct")

    store.upsert_batch(con, [
        messages.make_row(account="acct", chat_md5="a" * 32, chat_id="wxid_abc123def456",
                          shard="message_0.db", local_id=1, ts=1700000001, msg_type=1,
                          sender_id=None, sender_name=None, is_self=0, content="abc",
                          media_hint=None),
        messages.make_row(account="acct", chat_md5="a" * 32, chat_id="wxid_abc123def456",
                          shard="message_0.db", local_id=2, ts=1700000002, msg_type=1,
                          sender_id=None, sender_name=None, is_self=0, content="a%b",
                          media_hint=None)])
    con.commit()
    monkeypatch.setattr(store, "open", lambda: _Ctx(con))
    res = search.search("a%", account="acct")
    assert res["mode"] == "like", res
    assert [h["content"] for h in res["hits"]] == ["a%b"], res
