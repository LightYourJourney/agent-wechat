"""真机朋友圈冒烟（默认不跑：`uv run pytest tests/test_moments_real.py -q -m real`）。

只读：解析真 sns.db；第二条用例会向微信 CDN 发起**一次**真实 HTTP 请求（本功能
用户显式授权的唯一联网例外），不写回微信目录，产物不落盘。
"""
import sqlite3

import pytest

from agent_wechat import account, errors, moments, sns_media, usecases

# slow：默认 deselect 由 addopts 的 -m 'not slow' 负责（与 test_media_real 同一机制）；
# real：真机冒烟的统一入口标记，`-m real` 只跑真实数据路径的用例。
pytestmark = [pytest.mark.slow, pytest.mark.real]

# `sqlite3.Error` 与项目异常同级：sns.db 损坏/半写时 `iter_posts` 抛的是裸
# `sqlite3.DatabaseError`（`parse_post` 只 catch `ET.ParseError`）。本机数据不可用
# 一律 skip 而不是 fail——与 `test_media_real.py` 的语音探针同一个口径。
_UNAVAILABLE = (errors.AgentWeChatError, sqlite3.Error)


def test_real_sns_parses_posts():
    """真 sns.db → ≥1 条动态解析成功，且至少一条带媒体引用（该库的媒体项全是远程的）。"""
    try:
        acc = account.resolve_account(None)
        posts = []
        for i, p in enumerate(moments.iter_posts(acc)):
            posts.append(p)
            if i >= 20:
                break
    except _UNAVAILABLE as e:
        pytest.skip(f"本机不可用：{type(e).__name__}")
    if not posts:
        pytest.skip("本机朋友圈缓存为空")
    assert all(p.post_id and p.create_time for p in posts)
    assert any(p.media for p in posts), "至少一条应带媒体引用"
    # 互动在 `<LocalExtraInfo>/*_user_list` 下（不是 `TimelineObject` 内）——根节点传错
    # 不会报错，只会静默全空。本机实测：前 21 条里 15 条有互动，全库 870/1923。
    assert any(p.comments for p in posts), "至少一条应带评论或点赞（否则互动解析静默失效）"


def test_real_media_download_decrypts_one_image():
    """真下载一张并解出已知媒体魔数——打的是第 2 条可用媒体（避开同义反复）。

    只发起**一次**请求（不遍历集合）。两种失败必须分开对待——这是本功能**唯一的
    端到端解密断言**，必须能为解密回归变红：

    · `fetch` **抛异常** = 服务端对象已被清理（实测约三成带 token 仍返回 404）→ skip；
    · `fetch` **返回了字节**却认不出任何已知魔数 = **解密链路回归** → fail。

    魔数判定复用 `usecases._sniff_ext`（导出编排自己用的那一份，含 jpg/png/gif/webp/mp4），
    不在这里另写一份只看 JPEG 的检查——索引到 PNG/GIF/WebP 时会静默 skip。
    """
    try:
        acc = account.resolve_account(None)
        cands = [m for p in moments.iter_posts(acc) for m in p.media
                 if m.kind == moments.KIND_IMAGE and sns_media.build_url(m)]
    except _UNAVAILABLE as e:
        pytest.skip(f"本机不可用：{type(e).__name__}")
    if len(cands) < 2:
        pytest.skip("可用媒体不足 2 条")
    try:
        data = sns_media.fetch(cands[1])
    except _UNAVAILABLE as e:      # 404（对象已清理）/ 网络失败 → skip
        pytest.skip(f"该媒体在服务端已被清理或不可达：{type(e).__name__}")
    ext = usecases._sniff_ext(data)
    assert ext is not None, (
        f"下载成功（{len(data)} 字节）但魔数不匹配任何已知媒体格式——这是解密链路回归，"
        "不是服务端数据问题")
