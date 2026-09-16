# tests/test_chat_md.py
from agent_wechat import appmsg, chat_md, chatview


def _view(**kw):
    base = dict(chat_id="c", title="张三", rows=[], parts={}, media={}, out_dir="/tmp",
                truncated=False, unparsed=0, rejected=0, degraded=())
    base.update(kw)
    return chatview.build(base.pop("chat_id"), **base)


def test_truncated_is_printed_at_the_top():
    out = chat_md.render(_view(truncated=True))
    assert "截断" in out


def test_quote_becomes_blockquote_with_original_text():
    """Markdown 没有可点锚点，但**原文必须照常出现**（spec §2.6）。"""
    quote = appmsg.AppMsg(kind=57, title="同意", fields=(), link=None, file=None,
                          transfer=None, quote=appmsg.Quote(
                              svrid=1, displayname="张三", content="明天开会",
                              create_time=None, from_usr=None))
    out = chat_md.render(chatview.build(
        "c", title="t", rows=[{"msg_key": "k2", "ts": 2, "msg_type": 49, "content": "",
                               "sender_name": "我", "is_self": True}],
        parts={"k2": quote}, media={}, out_dir="/tmp", truncated=False,
        unparsed=0, rejected=0, degraded=()))
    assert "> " in out and "明天开会" in out and "张三" in out


def test_image_uses_markdown_syntax_and_voice_uses_a_link():
    """Markdown 的三处固有损失之一：音视频没有原生语法。"""
    from agent_wechat.usecases import MediaRefState
    st = lambda k, p: MediaRefState(msg_key="k1", ts=1, kind=k, status="exported", path=p)
    img = chat_md.render(chatview.build("c", title="t", rows=[{"msg_key": "k1", "ts": 1,
        "msg_type": 3, "content": "", "sender_name": "我", "is_self": True}],
        parts={}, media={"k1": st("image", "/out/chat/../a.jpg")}, out_dir="/out/chat",
        truncated=False, unparsed=0, rejected=0, degraded=()))
    voc = chat_md.render(chatview.build("c", title="t", rows=[{"msg_key": "k1", "ts": 1,
        "msg_type": 34, "content": "", "sender_name": "我", "is_self": True}],
        parts={}, media={"k1": st("voice", "/out/chat/../v.wav")}, out_dir="/out/chat",
        truncated=False, unparsed=0, rejected=0, degraded=()))
    assert "![" in img
    assert "![" not in voc and "](" in voc


def test_escapes_nothing_but_does_not_emit_raw_html():
    """Markdown 不转义正文（那会破坏可读性），但**不许注入 HTML 标签**。"""
    out = chat_md.render(_view(rows=[{"msg_key": "k1", "ts": 1, "msg_type": 1,
        "content": "<script>alert(1)</script>", "sender_name": "我", "is_self": True}]))
    assert "<script>" not in out


# ---------------- 以下是 brief 之外补的夹具（每条 docstring 都写明怎么红）----------------


def test_self_and_other_are_rendered_differently():
    """回归：`is_self` 的**两侧**都必须在同一个断言里承重（T6 刚栽过：只钉一侧时
    把 `is_self` 写死成 `False` 全量测试无人变红，而整份导出会被判成别人发的）。

    把这个标记删掉（或恒不印）→ 第 1 条红；恒印（不看 is_self）→ 第 2 条红。
    """
    def row(s):
        return [{"msg_key": "k1", "ts": 1, "msg_type": 1, "content": "x",
                 "sender_name": "我" if s else "张三", "is_self": s}]
    # `title="t"`：默认 title 是「张三」，会与「别人的昵称」撞车，把第 2 条断言变成恒真。
    mine = chat_md.render(_view(title="t", rows=row(True)))
    theirs = chat_md.render(_view(title="t", rows=row(False)))
    assert "（自己）" in mine and "（自己）" not in theirs
    assert "张三" in theirs and "张三" not in mine


def test_media_src_is_relative_to_the_session_media_root():
    """媒体引用指向哪里 —— brief 里没有断言读过这个值（`"![" in img` 不读路径）。
    契约与 `chat_html._media_src` **逐字一致**（见两处模块头）：`view.out_dir` 传本会话的
    媒体树根，默认 `media_prefix=".."` 从产物的 `chat/` 子目录回到那个根。
    删掉 `media_prefix` 的拼接 → `../image/…` 变 `image/…`，红。
    """
    from agent_wechat.usecases import MediaRefState
    out = chat_md.render(chatview.build(
        "c", title="t", rows=[{"msg_key": "k1", "ts": 1, "msg_type": 3, "content": "",
                               "sender_name": "我", "is_self": True}],
        parts={}, out_dir="/out/aaaa", media={"k1": MediaRefState(
            msg_key="k1", ts=1, kind="image", status="exported",
            path="/out/aaaa/image/2026-09/x.jpg")},
        truncated=False, unparsed=0, rejected=0, degraded=()))
    assert "../image/2026-09/x.jpg" in out


def test_exportable_and_gone_placeholders_differ():
    """回归：两个状态**都要**印，且**印得不一样**（丙 R17）。brief 只在 HTML 侧钉了这条，
    Markdown 侧此前没有任何夹具 —— 池化成一句「本机没有」照样全绿。
    """
    from agent_wechat.usecases import MediaRefState
    common = dict(chat_id="c", title="t", rows=[{"msg_key": "k1", "ts": 1, "msg_type": 3,
        "content": "", "sender_name": "我", "is_self": True}], parts={}, out_dir="/tmp",
        truncated=False, unparsed=0, rejected=0, degraded=())
    ab = chat_md.render(chatview.build(media={"k1": MediaRefState(
        msg_key="k1", ts=1, kind="image", status="exportable", path=None)}, **common))
    go = chat_md.render(chatview.build(media={"k1": MediaRefState(
        msg_key="k1", ts=1, kind="image", status="gone", path=None)}, **common))
    assert "尚未导出" in ab and "尚未导出" not in go
    assert "本机找不到" in go and "本机找不到" not in ab


def test_quote_without_a_displayname_prints_an_explicit_placeholder():
    """与 HTML 侧同一件事：`displayname` 为空是 0.81% 的常态（spec §2.6）。

    删掉 `or "（未署名）"` → 第 1 条断言红。
    """
    quote = appmsg.AppMsg(kind=57, title="同意", fields=(), link=None, file=None,
                          transfer=None, quote=appmsg.Quote(
                              svrid=1, displayname=None, content="明天开会",
                              create_time=None, from_usr=None))
    out = chat_md.render(_view(rows=[{"msg_key": "k1", "ts": 1, "msg_type": 49,
        "content": "", "sender_name": "张三", "is_self": False}], parts={"k1": quote}))
    assert "（未署名）" in out
    assert "明天开会" in out
