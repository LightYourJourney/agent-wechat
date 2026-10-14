# tests/test_chat_html.py
"""HTML 模板。断言守的都是「产物里必须看得到的东西」。"""
import html as html_mod

from agent_wechat import appmsg, chat_html, chatview


def _view(**kw):
    base = dict(chat_id="c", title="张三", rows=[], parts={}, media={}, out_dir="/tmp",
                truncated=False, unparsed=0, rejected=0, degraded=())
    base.update(kw)
    return chatview.build(base.pop("chat_id"), **base)


def test_truncated_is_printed_at_the_top(monkeypatch):
    """回归：只把 truncated 回终端、产物里不印 —— 删除顶部那行提示这条会红。"""
    out = chat_html.render(_view(truncated=True, rows=[{"msg_key": "k1", "ts": 1,
        "msg_type": 1, "content": "正文在这", "sender_name": "我", "is_self": True}]))
    assert "截断" in out
    assert out.index("截断") < out.index("正文在这"), "截断提示必须在正文之前"


def test_unparsed_and_rejected_are_reported_distinctly():
    """回归：删掉 `unparsed` 或 `rejected` 的整行，这条会红。

    **初稿这条是空断言，值得记下来**：原写 `assert "2" in out and "1" in out` ——
    而产物里**必然有** `"2"` 与 `"1"`（CSS 的 `initial-scale=1`、markup 里的各种数字），
    实测 `unparsed=0, rejected=0` 时两个字符**照样在**，把两整行删掉它也绿。
    （T7 实施者发现，控制器复现。）所以要断言**完整的措辞串**，不是孤立的数字。
    """
    out = chat_html.render(_view(unparsed=2, rejected=1))
    assert "2 条 appmsg 解不开" in out, "解不开那一行没印出来"
    assert "1 条 appmsg 被主动拒绝" in out, "拒绝那一行没印出来"


def test_media_exported_renders_an_element_others_render_distinct_placeholders():
    """回归：exportable 与 gone 的占位必须**印得不一样**（丙 R17：池化会制造误读）。"""
    from agent_wechat.usecases import MediaRefState
    common = dict(chat_id="c", title="t", rows=[{"msg_key": "k1", "ts": 1,
        "msg_type": 3, "content": "", "sender_name": "我", "is_self": True}],
        parts={}, out_dir="/tmp", truncated=False, unparsed=0, rejected=0, degraded=())
    ex = chat_html.render(chatview.build(media={"k1": MediaRefState(
        msg_key="k1", ts=1, kind="image", status="exported", path="/out/a.jpg")}, **common))
    ab = chat_html.render(chatview.build(media={"k1": MediaRefState(
        msg_key="k1", ts=1, kind="image", status="exportable", path=None)}, **common))
    go = chat_html.render(chatview.build(media={"k1": MediaRefState(
        msg_key="k1", ts=1, kind="image", status="gone", path=None)}, **common))
    assert "<img" in ex
    assert "<img" not in ab and "<img" not in go
    assert ab != go


def test_quote_renders_as_a_jump_link_when_target_in_range():
    a = chatview.anchor_of("k1")
    quote = appmsg.AppMsg(kind=57, title="同意", fields=(), link=None, file=None,
                          transfer=None, quote=appmsg.Quote(
                              svrid=1, displayname="张三", content="明天开会", create_time=None,
                              from_usr=None))
    out = chat_html.render(chatview.build(
        "c", title="t", rows=[{"msg_key": "k1", "ts": 1, "msg_type": 1, "content": "x",
                               "sender_name": "张三", "is_self": False},
                              {"msg_key": "k2", "ts": 2, "msg_type": 49, "content": "",
                               "sender_name": "我", "is_self": True}],
        parts={"k2": quote}, refs={"k2": "k1"}, media={}, out_dir="/tmp",
        truncated=False, unparsed=0, rejected=0, degraded=()))
    assert f'href="#{a}"' in out and "明天开会" in out

    # 反向（同一条用例内）：目标**不在**本次范围内时既不能给死锚点，也不能只留一个
    # 没有解释的块 —— brief 要求配一句「原消息不在本次范围内」。
    # 删掉 `_quote_block` 里那句话 → 第 1 条断言红；原文两种情况都照常出现。
    outside = chat_html.render(chatview.build(
        "c", title="t", rows=[{"msg_key": "k2", "ts": 2, "msg_type": 49, "content": "",
                               "sender_name": "我", "is_self": True}],
        parts={"k2": quote}, media={}, out_dir="/tmp",
        truncated=False, unparsed=0, rejected=0, degraded=()))
    assert "原消息不在本次范围内" in outside and "明天开会" in outside
    assert 'href="#' not in outside, "目标不在范围内时不许给锚点（那是死锚点）"


def test_a_javascript_link_in_an_appmsg_is_not_rendered_as_a_link():
    """EA-034：appmsg 链接块此前**完全没有** URL 守卫——`javascript:` 直接成为可点链接，
    且锚文本用 `title` 把真实 URL 藏起来；而 `moments_html` 对同一类输入有一整套守卫
    （scheme / 反斜杠主机 / WHATWG-IPv4 / `%`）。判据现已共用 `chat_text.url_is_safe`。

    把 `chat_html` 的守卫去掉（回到无条件 `<a href=…>`）本测试必红。
    """
    link = appmsg.AppMsg(kind=appmsg.KIND_LINK, title="点我", fields=(),
                         link=appmsg.Link(url="javascript:alert(1)", title="点我", des=None),
                         file=None, transfer=None, quote=None)
    out = chat_html.render(chatview.build(
        "c", title="t", rows=[{"msg_key": "k2", "ts": 2, "msg_type": 49, "content": "",
                               "sender_name": "我", "is_self": True}],
        parts={"k2": link}, media={}, out_dir="/tmp",
        truncated=False, unparsed=0, rejected=0, degraded=()))
    # 此前这里还有一句析取：`"javascript:" not in out.split("href=")[0] or 'href="javascript'
    # not in out`——**第一个析取项对两种状态都恒真**（没成链接时 `split("href=")[0]` 就是全文、
    # 而全文里那个 `javascript:` 是我们**故意印出来**的），照这个形状写会给人「多了一重守卫」
    # 的错觉（复审 T12 的 #9 第三条）。真正判别的是下面两条。
    assert "<a href=\"javascript" not in out, "javascript: 成了可点链接"
    assert "链接：" in out, "不安全的 URL 要**显示**出来（用户看得见去哪），只是不可点"

    # 反向守卫：正常 https 链接照常可点（别把守卫做成一刀切）
    ok = appmsg.AppMsg(kind=appmsg.KIND_LINK, title="正常", fields=(),
                       link=appmsg.Link(url="https://mp.weixin.qq.com/s/X", title="正常", des=None),
                       file=None, transfer=None, quote=None)
    out2 = chat_html.render(chatview.build(
        "c", title="t", rows=[{"msg_key": "k2", "ts": 2, "msg_type": 49, "content": "",
                               "sender_name": "我", "is_self": True}],
        parts={"k2": ok}, media={}, out_dir="/tmp",
        truncated=False, unparsed=0, rejected=0, degraded=()))
    assert '<a href="https://mp.weixin.qq.com/s/X"' in out2


def test_escapes_message_content():
    """消息正文是对手可控的 —— 不许原样注入 HTML。"""
    out = chat_html.render(_view(rows=[{"msg_key": "k1", "ts": 1, "msg_type": 1,
        "content": "<script>alert(1)</script>", "sender_name": "我", "is_self": True}]))
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out


def test_is_a_single_self_contained_file():
    """内联 CSS、无外链（沿用 moments_html 的形状）。"""
    out = chat_html.render(_view())
    assert out.startswith("<!DOCTYPE html>") and "<style>" in out
    assert "http://" not in out and "https://" not in out


# ---------------- 以下是 brief 之外补的夹具（每条 docstring 都写明怎么红）----------------


def test_self_and_other_are_rendered_differently():
    """回归：`is_self` 的**两侧**都必须在同一个断言里承重。

    T6 刚栽过：夹具里那条的 `is_self` 恰是 `False`，与「把 is_self 写死成 False」
    不可区分 —— 全量 632 条测试无人变红，而后果是**整份导出被判成别人发的**。
    这里同一个 `msg_key` 各渲一次，把 `is_self=True` 那一支删掉（或恒 False）→ 第 1 条红；
    恒 True（把 `if m.is_self` 写反）→ 第 2 条红。
    """
    def row(s):
        return [{"msg_key": "k1", "ts": 1, "msg_type": 1, "content": "x",
                 "sender_name": "我" if s else "张三", "is_self": s}]
    mine = chat_html.render(_view(rows=row(True)))
    theirs = chat_html.render(_view(rows=row(False)))
    assert 'class="msg self"' in mine and 'class="msg other"' not in mine
    assert 'class="msg other"' in theirs and 'class="msg self"' not in theirs


def test_media_src_is_relative_to_the_session_media_root():
    """媒体引用到底指向哪里 —— brief 里**没有任何断言碰过这个值**
    （`"![" in img` / `"<img" in ex` 都只数元素，不读路径，路径写错它们照样绿）。

    契约（两个模板一致，见两处模块头）：`view.out_dir` 传**本会话的媒体树根**
    `{--out}/{chat_md5}`，产物写在它的 `chat/` 子目录里，于是默认 `media_prefix=".."`
    恰好回到那个根。删掉 `media_prefix` 的拼接 → `../image/…` 变 `image/…`，红；
    把 out_dir 传成 `{--out}` 根（T9 最容易写错的那一种）→ 变 `../aaaa/image/…`，也红。
    """
    from agent_wechat.usecases import MediaRefState
    out = chat_html.render(chatview.build(
        "c", title="t", rows=[{"msg_key": "k1", "ts": 1, "msg_type": 3, "content": "",
                               "sender_name": "我", "is_self": True}],
        parts={}, out_dir="/out/aaaa", media={"k1": MediaRefState(
            msg_key="k1", ts=1, kind="image", status="exported",
            path="/out/aaaa/image/2026-09/x.jpg")},
        truncated=False, unparsed=0, rejected=0, degraded=()))
    assert 'src="../image/2026-09/x.jpg"' in out


def test_video_exportable_does_not_claim_the_mp4_is_there():
    """spec §4.3 明文：video 的 `exportable` **不得**让人以为「mp4 本体还在」
    —— 丙实测 `resolve_video` 命中缩略图时也返回成功。

    把 video 的 exportable 文案池回 image 那一句（「本机有原图，尚未导出」）→
    第 1 条断言红。这条闸在此之前**没有任何夹具**（brief 只钉了 image 的 ab != go）。
    """
    from agent_wechat.usecases import MediaRefState
    out = chat_html.render(chatview.build(
        "c", title="t", rows=[{"msg_key": "k1", "ts": 1, "msg_type": 43, "content": "",
                               "sender_name": "我", "is_self": True}],
        parts={}, out_dir="/tmp", media={"k1": MediaRefState(
            msg_key="k1", ts=1, kind="video", status="exportable", path=None)},
        truncated=False, unparsed=0, rejected=0, degraded=()))
    assert "缩略图" in out
    assert "尚未导出" in out


def test_transfer_envelope_renders_as_a_readable_list():
    """brief Step 3 要求「转账块按 spec §4.5 的信封字段渲染成可读列表」，但没有夹具。

    删掉 `_transfer_block`（或只印 `AppMsg.title`）→ 三条断言全红。
    `paysubtype` 必须**原样**呈现：spec §2.9 明令不猜它的中文语义。
    """
    t = appmsg.Transfer(paysubtype="3", amount_desc="¥100.00", memo="还款", payer=None,
                        receiver="wxid_testacct02", transaction_id="TXN1",
                        transfer_id="TR1", begin_time=None, effective_date=None,
                        invalid_time=None, is_red_envelope=False)
    a = appmsg.AppMsg(kind=appmsg.KIND_TRANSFER, title="转账", fields=(), quote=None,
                      link=None, file=None, transfer=t)
    out = chat_html.render(_view(rows=[{"msg_key": "k1", "ts": 1, "msg_type": 49,
        "content": "", "sender_name": "张三", "is_self": False}], parts={"k1": a}))
    assert "¥100.00" in out and "还款" in out and "TXN1" in out
    assert "paysubtype" in out


def test_quote_without_a_displayname_prints_an_explicit_placeholder():
    """实测 `refermsg.displayname` 有 **0.81% 为空**（spec §2.6）—— 空是常态不是异常。

    照原样印会长成「引用 ：」，看起来像渲染坏了；故补一个**显式的**「未署名」，
    **不猜名字**（与「`payer` 为空不写『（空）』」同一条规矩的反面：这里留空会造成歧义，
    那里留空只是少一行信息）。删掉 `or "（未署名）"` → 第 1 条断言红。
    """
    quote = appmsg.AppMsg(kind=57, title="同意", fields=(), link=None, file=None,
                          transfer=None, quote=appmsg.Quote(
                              svrid=1, displayname=None, content="明天开会",
                              create_time=None, from_usr=None))
    out = chat_html.render(_view(rows=[{"msg_key": "k1", "ts": 1, "msg_type": 49,
        "content": "", "sender_name": "张三", "is_self": False}], parts={"k1": quote}))
    assert "（未署名）" in out and "引用 ：" not in out
    assert "明天开会" in out          # 原文照常出现，不依赖署名


def test_sticker_gone_says_encrypted_copy_exists():
    """贴纸的 `gone` **不许**用那句通用的 GONE——它说的是「本机找不到本体（已被清理，
    或从未下载到本机）」，而贴纸取不出来是**微信侧把它存成加密格式、方案未破**
    （spec 附录 C），与「本机找不找得到」无关。

    这条钉的是「产物里不许出现一句错话」。把实现改回共用 `chat_text.GONE` 它必红。
    """
    from agent_wechat import chat_text
    from agent_wechat.usecases import MediaRefState
    st = MediaRefState(msg_key="k", ts=1730000000, kind="sticker",
                       status="gone", path=None)
    out = chat_html._media_block(st, "..", "/out/c")
    assert chat_text.STICKER_GONE in out
    assert chat_text.GONE not in out          # **关键**：不是那句通用的
    assert "贴纸" in out


def test_sticker_label_is_chinese():
    """类型标签走 `chat_text.KIND_LABEL`——两个模板共用一份，不各写各的。

    没有它，`gone` 支的占位会印成 `[sticker · …]` 而不是 `[贴纸 · …]`。
    """
    from agent_wechat import chat_text
    assert chat_text.KIND_LABEL["sticker"] == "贴纸"


def test_sticker_gone_makes_no_local_presence_claim():
    """措辞**不许**断言本机有那份加密副本 —— 本机有没有是**未测的**。

    `"本机有" not in …` 是这条规矩的**代名断言（proxy）**：它代理的真规矩是
    「这句话必须对**本机副本可能不存在**的消息也成立」。为什么不能断言：实测只有
    **38.3%** 的 type-47 消息本机有贴纸文件（spec §2.4：25,971 / 67,833），而收窄后的
    T9 把 presence 写死成常量、本地支线 `resolve_sticker` 随 T7 一起挂起 —— 渲染这条时
    **没有任何存在性信号**可依据。写成「本机有加密副本」对约 **62%** 会印出这一行的
    消息是假话（初稿正是这么写的，被评审判掉）。改回那种写法 → 这条红。

    它与上面那条守的**不是同一件事**：上面守「别是那句通用的」，这条守
    「也不许换成另一个假话」。
    """
    from agent_wechat import chat_text
    assert "本机有" not in chat_text.STICKER_GONE


# ---------- EA-041：位置 / 名片的结构化块 ----------

def test_a_location_renders_as_a_structured_block():
    """与 MD 侧同一件事（`test_chat_md.py::test_a_location_renders_its_fields...`）：
    结构化块出现、`[location]` 占位收掉、没有 extras 时一行都不印。"""
    from agent_wechat import msgextra
    x = msgextra.Extra(48, (("x", "39.9"), ("y", "116.4"), ("label", "某地")))
    view = chatview.build(
        "c", title="t", rows=[{"msg_key": "k1", "ts": 1, "msg_type": 48,
                               "content": "[location]", "sender_name": "我", "is_self": True}],
        parts={}, media={}, out_dir="/tmp", truncated=False, unparsed=0, rejected=0,
        degraded=(), extras={"k1": x})
    out = chat_html.render(view)
    assert "位置" in out and "39.9" in out and "某地" in out
    assert "[location]" not in out
    plain = chat_html.render(chatview.build(
        "c", title="t", rows=[{"msg_key": "k1", "ts": 1, "msg_type": 48,
                               "content": "[location]", "sender_name": "我", "is_self": True}],
        parts={}, media={}, out_dir="/tmp", truncated=False, unparsed=0, rejected=0,
        degraded=()))
    assert "[location]" in plain and "位置" not in plain


def test_a_card_without_readable_fields_says_so():
    from agent_wechat import chat_text, msgextra
    view = chatview.build(
        "c", title="t", rows=[{"msg_key": "k1", "ts": 1, "msg_type": 42,
                               "content": "[type42]", "sender_name": "我", "is_self": True}],
        parts={}, media={}, out_dir="/tmp", truncated=False, unparsed=0, rejected=0,
        degraded=(), extras={"k1": msgextra.Extra(42, ())})
    assert chat_text.EXTRA_NO_FIELDS in chat_html.render(view)
