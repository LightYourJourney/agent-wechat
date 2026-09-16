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
    out = chat_html.render(_view(unparsed=2, rejected=1))
    assert "2" in out and "1" in out
    assert "拒绝" in out and ("解不开" in out or "解析失败" in out)


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
