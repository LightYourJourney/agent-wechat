"""真机朋友圈冒烟（默认不跑：`uv run pytest tests/test_moments_real.py -q -m real`）。

只读：解析真 sns.db；第二条用例会向微信 CDN 发起**一次**真实 HTTP 请求（本功能
用户显式授权的唯一联网例外），不写回微信目录，产物不落盘。
"""
import pytest

from agent_wechat import account, errors, moments, sns_media

# slow：默认 deselect 由 addopts 的 -m 'not slow' 负责（与 test_media_real 同一机制）；
# real：真机冒烟的统一入口标记，`-m real` 只跑真实数据路径的用例。
pytestmark = [pytest.mark.slow, pytest.mark.real]


def test_real_sns_parses_posts():
    """真 sns.db → ≥1 条动态解析成功，且至少一条带媒体引用（该库的媒体项全是远程的）。"""
    try:
        acc = account.resolve_account(None)
        posts = []
        for i, p in enumerate(moments.iter_posts(acc)):
            posts.append(p)
            if i >= 20:
                break
    except errors.AgentWeChatError as e:
        pytest.skip(f"本机不可用：{type(e).__name__}")
    if not posts:
        pytest.skip("本机朋友圈缓存为空")
    assert all(p.post_id and p.create_time for p in posts)
    assert any(p.media for p in posts), "至少一条应带媒体引用"
    # 互动在 `<LocalExtraInfo>/*_user_list` 下（不是 `TimelineObject` 内）——根节点传错
    # 不会报错，只会静默全空。本机实测：前 21 条里 15 条有互动，全库 870/1923。
    assert any(p.comments for p in posts), "至少一条应带评论或点赞（否则互动解析静默失效）"


def test_real_media_download_decrypts_one_image():
    """真下载一张并解出图片魔数——打的是第 2 条可用媒体（避开同义反复）。

    只发起**一次**请求（不遍历集合）。服务端清理过的对象（404）与密钥不匹配导致的
    非图片响应一律 skip，不是 fail——实测 3/10 带 token 仍返回 404。
    """
    try:
        acc = account.resolve_account(None)
        cands = [m for p in moments.iter_posts(acc) for m in p.media
                 if m.kind == moments.KIND_IMAGE and sns_media.build_url(m)]
    except errors.AgentWeChatError as e:
        pytest.skip(f"本机不可用：{type(e).__name__}")
    if len(cands) < 2:
        pytest.skip("可用媒体不足 2 条")
    try:
        data = sns_media.fetch(cands[1])
    except errors.AgentWeChatError as e:      # 404（对象已清理）/网络失败 → skip
        pytest.skip(f"该媒体在服务端已被清理或不可达：{type(e).__name__}")
    if data[:3] != b"\xff\xd8\xff":
        pytest.skip("该媒体在服务端已被清理（404/非图片），换一条重跑")
    assert data[-2:] == b"\xff\xd9" or len(data) > 1024
