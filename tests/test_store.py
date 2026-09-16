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
