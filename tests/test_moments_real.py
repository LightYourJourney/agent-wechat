"""真机朋友圈冒烟（默认不跑：`uv run pytest tests/test_moments_real.py -q -m real`）。

只读：解析真 sns.db；**第三条**用例（`test_real_media_download_decrypts_one_image`）会向
微信 CDN 直调 `sns_media.fetch` 发起**一次**真实 HTTP 请求（本功能本次新增的联网例外，
已并入 `SECURITY.md` §1 的例外条目——EA-046；默认 deselect，`-m real` 显式启用），
不写回微信目录，产物不落盘。
"""
import sqlite3

import pytest

from agent_wechat import account, errors, moments, sns_media, usecases

# slow：默认 deselect 由 addopts 的 -m 'not slow' 负责（与 test_media_real 同一机制）；
# real：真机冒烟的统一入口标记，`-m real` 只跑真实数据路径的用例。
pytestmark = [pytest.mark.slow, pytest.mark.real]

# 本机数据不可用一律 skip 而不是 fail——与 `test_media_real.py` 的语音探针同一个口径。
# **`sqlite3.Error` 不再列入**（EA-105，2026-10-14 复审）：`iter_posts` 已把
# `sqlite3.Error` 收口成 `errors.SchemaDrift`，所以它不再从这里逃出来；留在集合里会让
# 「真机的 sns.db 损坏」被 skip 而不是 fail——那正是这类漂移唯一该被看见的时候。
_UNAVAILABLE = (errors.AgentWeChatError,)


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


def test_real_reply_target_is_a_nickname_not_a_wxid():
    """真机上回复目标必须是**昵称**：`<ref_username>` 是 wxid（本机 183/183 全是
    `wxid_…`），修复前它被直接当昵称渲染，产物里每条回复都印成「小明 回复 wxid_abc123」。

    两条断言缺一不可：少了第一条，「反查整个失效 → 全部 None」会因为 `replies` 为空而
    静默通过；少了第二条，回退成 `ref_username` 就没人拦。
    """
    try:
        acc = account.resolve_account(None)
        posts = list(moments.iter_posts(acc))
    except _UNAVAILABLE as e:
        pytest.skip(f"本机不可用：{type(e).__name__}")
    if not posts:
        pytest.skip("本机朋友圈缓存为空")
    replies = [c for p in posts for c in p.comments if c.ref_nickname]
    # 本机实测 183 条回复全部能反查到昵称；一条都没有 = 反查链路整个失效
    assert replies, "本机有 183 条带 ref_username 的回复，一条都反查不出来说明反查已失效"
    assert not any(c.ref_nickname.startswith("wxid_") for c in replies), \
        "回复目标是 wxid —— `<ref_username>` 又被当成昵称用了"


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
        # 只挑真的走解密的老路径媒体。改动后 `build_url` 对 AUTH_READY 也返回非 None，
        # 候选集会混进「下载一张明文图再嗅探」的输入——那样这条本项目唯一的端到端
        # 解密断言会**永远绿**，`decrypt_stream` 全坏了也不红。
        cands = [m for p in moments.iter_posts(acc) for m in p.media
                 if m.kind == moments.KIND_IMAGE
                 and m.auth == moments.AUTH_SNS
                 and sns_media.build_url(m)]
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
