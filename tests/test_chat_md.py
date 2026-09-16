# tests/test_chat_md.py
from agent_wechat import appmsg, chat_md, chatview


def _view(**kw):
    base = dict(chat_id="c", title="张三", rows=[], parts={}, media={}, out_dir="/tmp",
                truncated=False, unparsed=0, rejected=0, degraded=())
    base.update(kw)
    return chatview.build(base.pop("chat_id"), **base)


def test_truncated_is_printed_at_the_top():
    """回归：**整块顶部提示**（截断 / 解不开 / 拒绝）都不许跑到文末去。

    这条此前只断言 `"截断" in out` —— 与 HTML 的孪生用例（还有顺序断言）不对称，
    于是三个变异在 8 条 MD 用例下**全绿**：把提示块挪到文末、把 `rejected` 的措辞池进
    `unparsed`（spec §5 明令禁止的 R17 误读）、整行删掉 `unparsed`。
    现在：挪到文末 → 第 2 条红；池化措辞 → 第 3 或第 4 条红；删掉任一行 → 对应那条红。

    解不开 / 拒绝这两条断言塞在这条既有用例里（控制器要求「别新增用例」），
    措辞与 HTML 侧 `test_unparsed_and_rejected_are_reported_distinctly` 镜像。
    """
    out = chat_md.render(_view(truncated=True, unparsed=2, rejected=1, rows=[
        {"msg_key": "k1", "ts": 1, "msg_type": 1, "content": "正文在这",
         "sender_name": "我", "is_self": True}]))
    assert "截断" in out
    assert out.index("截断") < out.index("正文在这"), "截断提示必须在正文之前"
    assert "2 条 appmsg 解不开" in out, "解不开那一行没印出来"
    assert "1 条 appmsg 被主动拒绝" in out, "拒绝那一行没印出来"


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
    # 「原消息不在本次范围内」这句话此前在两个模板都没有闸（实测：删掉它两边全绿），
    # 而 brief 把它列为 `ref_anchor` 为空时的硬要求 —— 没有它，引用只是个不可点的块。
    assert "原消息不在本次范围内" in out


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
    契约与 HTML 侧**同一个函数**（`chat_text.media_src`，F51 合并后两模板同源）：
    `view.out_dir` 传本会话的媒体树根，默认 `media_prefix=".."` 从产物的 `chat/`
    子目录回到那个根。删掉 `media_prefix` 的拼接 → `../image/…` 变 `image/…`，红。
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


def test_both_templates_take_their_wording_from_chat_text():
    """F51：两个模板的措辞**同源**（`chat_text`）—— spec §4.3 的「HTML 与 Markdown 语义
    一致」不能靠两份互相无法校验的副本支撑（丙 R21 的教训）。

    此前两个模板各有一份 `GONE` / `EXPORTABLE` / 顶部三句的副本，实测让它们分叉
    **19 条用例全绿**，而症状是用户读 `index.html` 与 `index.md` 对同一条消息的媒体
    可得性得到互相矛盾的说法。

    这条断言把两侧都钉在**共享常量本身**上：任一侧留一份自己的副本并分叉
    （改回硬编码字符串）→ 对应那句 `in html_out and in md_out` 立刻红。
    反向确认：只改 `chat_text.GONE` 一处 → 两个模板的输出**同时**变 → 这条也红。
    """
    from agent_wechat import chat_html, chat_text
    from agent_wechat.usecases import MediaRefState
    view = chatview.build(
        "c", title="t", rows=[{"msg_key": "k1", "ts": 1, "msg_type": 3, "content": "",
                               "sender_name": "我", "is_self": True}], parts={},
        media={"k1": MediaRefState(msg_key="k1", ts=1, kind="image", status="gone",
                                   path=None)},
        out_dir="/tmp", truncated=True, unparsed=2, rejected=1, degraded=())
    html_out, md_out = chat_html.render(view), chat_md.render(view)
    for shared in (chat_text.GONE, chat_text.TRUNCATED_NOTE[0],
                   chat_text.unparsed_note(2)[0], chat_text.rejected_note(1)[0]):
        assert shared in html_out and shared in md_out, f"两个模板的措辞分叉了：{shared}"
