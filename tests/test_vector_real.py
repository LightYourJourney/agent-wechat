"""真实 fastembed 模型冒烟（默认不跑：uv run pytest -m slow -v）。

首次运行会下载约 95MB 模型到 ~/.agent-wechat/models。
"""
import os

import pytest

from agent_wechat import vector

pytestmark = [pytest.mark.slow, pytest.mark.real]     # EA-115：真机层（与另三个 _real 文件同款）


def test_real_embedder_returns_512_dim_vectors():
    emb = vector.FastEmbedEmbedder()
    vecs = emb.embed(["今天天气不错，适合爬山", "明天的会议改到下午三点"])
    assert len(vecs) == 2
    assert all(len(v) == vector.DIM for v in vecs)
    q = emb.embed_query("爬山")
    assert len(q) == vector.DIM


def test_real_embedder_offline_after_download():
    """模型就位后必须能完全离线加载（隐私约束：运行期零网络）。"""
    if not vector.MODEL_CACHE_DIR.exists():
        pytest.skip("模型尚未下载")
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        emb = vector.FastEmbedEmbedder(local_files_only=True)
        assert len(emb.embed(["离线加载测试"])[0]) == vector.DIM
    finally:
        os.environ.pop("HF_HUB_OFFLINE", None)
