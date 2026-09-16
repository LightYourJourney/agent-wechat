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


def test_media_refs_by_keys_dedups_and_spans_batches(con):
    """去重 + 跨批次都覆盖：900 个 key 会分成两批（批大小 500），批次边界不能丢行。"""
    store.upsert_media_refs(con, [(f"k{i}", "image", "dd" * 16, None) for i in range(900)])
    keys = [f"k{i}" for i in range(900)] + ["k0"]        # 重复 key 只算一次
    got = store.media_refs_by_keys(con, keys)
    assert len(got) == 900 and got["k899"] == ("image", "dd" * 16)
