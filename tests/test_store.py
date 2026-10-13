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
    assert store.count_media_refs(con, "acct", chat_ids=["wxid_testacct01_ab12"]) == 0


def test_media_refs_by_chat_filters_kind_and_spans_batches(con, monkeypatch):
    """EA-078②：`media_refs_by_chat` 的 kind 过滤 + 跨批累积。

    夹具把 `KEYS_BATCH` 压到 2：把 `out`/累积写进循环（只留最后一批）会红；
    kind 过滤去掉会把 image 混进 sticker 结果。
    """
    from agent_wechat import messages

    md5a = messages.md5_of("wxid_abc123def456")
    rows = [(f"acct|{md5a}|message_0.db|{i}", "sticker", "ab" * 16, None) for i in range(1, 6)]
    rows.append((f"acct|{md5a}|message_0.db|9", "image", "aa" * 16, None))
    # refs 的读取 join `messages` 取 ts/local_id 排序——没有消息行就一条都取不到
    store.upsert_batch(con, [messages.make_row(
        account="acct", chat_md5=md5a, chat_id="wxid_abc123def456", shard="message_0.db",
        local_id=i, ts=1700000000 + i, msg_type=49, sender_id="wxid_abc123def456",
        sender_name=None, is_self=0, content=f"[sticker] {i}", media_hint=None)
        for i in list(range(1, 6)) + [9]])
    store.upsert_media_refs(con, rows)
    monkeypatch.setattr(store, "KEYS_BATCH", 2)
    assert store.KEYS_BATCH == 2          # 前提自检：patch 真的生效
    got = store.media_refs_by_chat(con, "acct", ["wxid_abc123def456"], kind="sticker")
    got_rows = got["wxid_abc123def456"]
    assert len(got_rows) == 5, "跨批累积丢了行"
    assert all(k.count(
        "|") == 3 for k, _ts, _fk in got_rows)
