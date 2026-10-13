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


def test_transfer_envelope_renders_as_a_readable_line():
    """MD 侧的转账夹具 —— **此前一条都没有**。

    实测的漏法：把 `chat_md._transfer_line` 整个改成 `return ""`（MD 产物里转账块全没
    了），695 条用例**全绿**。字段清单与标签原本在两个模板里逐字两写，而那条变异正好
    落在没有夹具的那一半上（丙 R21 的复发形状）。清单现已收进 `chat_text.transfer_fields`，
    本条钉住 MD 这一侧真的去取了它。

    `paysubtype` 必须**原样**呈现：spec §2.9 明令不猜它的中文语义。
    `payer` 为 None（实测 61% 为空）→ 那一行**不印**，也不写「（空）」。
    """
    t = appmsg.Transfer(paysubtype="3", amount_desc="¥100.00", memo="还款", payer=None,
                        receiver="wxid_testacct02", transaction_id="TXN1",
                        transfer_id="TR1", begin_time=None, effective_date=None,
                        invalid_time=None, is_red_envelope=False)
    a = appmsg.AppMsg(kind=appmsg.KIND_TRANSFER, title="转账", fields=(), quote=None,
                      link=None, file=None, transfer=t)
    out = chat_md.render(chatview.build(
        "c", title="t", rows=[{"msg_key": "k1", "ts": 1, "msg_type": 49, "content": "",
                               "sender_name": "张三", "is_self": False}],
        parts={"k1": a}, media={}, out_dir="/tmp", truncated=False,
        unparsed=0, rejected=0, degraded=()))
    assert "¥100.00" in out and "还款" in out and "TXN1" in out
    assert "paysubtype：3（语义未确认，原样呈现）" in out
    assert "付款方" not in out, "payer 为空却被印了一行"
    assert "金额：¥100.00 · 备注：还款" in out, "字段顺序或分隔符与 HTML 侧分叉了"


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
    """Markdown 的四处固有损失之一：音视频没有原生语法。"""
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


def test_both_templates_take_their_wording_from_chat_text(monkeypatch):
    """F51：两个模板的措辞**同源**（`chat_text`）—— spec §4.3 的「HTML 与 Markdown 语义
    一致」不能靠两份互相无法校验的副本支撑（丙 R21 的教训）。

    此前两个模板各有一份 `GONE` / `EXPORTABLE` / 顶部三句的副本，实测让它们分叉
    **19 条用例全绿**，而症状是用户读 `index.html` 与 `index.md` 对同一条消息的媒体
    可得性得到互相矛盾的说法。

    两段断言各守一半：
    1. 上面那轮 `in html_out and in md_out`：**拿共享常量的实际值**去比两个产物 ——
       任一侧留一份自己的副本并分叉（硬编码字符串），那边立刻红。
    2. 下面那轮 monkeypatch：改 `chat_text.GONE` 后**产物里的措辞必须跟着变**。
       这条守的是 F53 —— 两个模板此前写 `from .chat_text import GONE`，那是**导入期绑定**：
       运行期 monkeypatch 对产物**完全无效**（实测 HTML=False MD=False），产物停在旧值上，
       而那正是 `purge.py:10` 明令禁止的形状（「测试 monkeypatch 会失效」）。
       改回 `from .chat_text import GONE` **或**任一侧自己绑局部名 → 第 2 轮红。
       （顺带订正：本用例初稿把机制写反了 —— 当时那两条断言能红，恰恰是因为
       常量变了而产物**没**变；订正前那种形态下「两个产物同时变」根本不成立。）
    """
    from agent_wechat import chat_html, chat_text
    from agent_wechat.usecases import MediaRefState
    def _row(k):
        return {"msg_key": k, "ts": 1, "msg_type": 3, "content": "",
                "sender_name": "我", "is_self": True}

    view = chatview.build(
        "c", title="t", rows=[_row("k1"), _row("k2"), _row("k3")], parts={},
        media={"k1": MediaRefState(msg_key="k1", ts=1, kind="image", status="gone",
                                   path=None),
               # EA-108：这两个形态的措辞此前是两个模板各一份副本
               "k2": MediaRefState(msg_key="k2", ts=1, kind="image", status="exported",
                                   path=None),
               "k3": MediaRefState(msg_key="k3", ts=1, kind="image", status="weird",
                                   path=None)},
        out_dir="/tmp", truncated=True, unparsed=2, rejected=1, degraded=())
    html_out, md_out = chat_html.render(view), chat_md.render(view)
    for shared in (chat_text.GONE, chat_text.INCONSISTENT_EXPORTED,
                   chat_text.unknown_status_note('weird'),
                   chat_text.TRUNCATED_NOTE[0],
                   chat_text.unparsed_note(2)[0], chat_text.rejected_note(1)[0]):
        assert shared in html_out and shared in md_out, f"两个模板的措辞分叉了：{shared}"

    monkeypatch.setattr(chat_text, "GONE", "改过的文案XYZ")
    assert "改过的文案XYZ" in chat_html.render(view), "HTML 读的是导入期的旧值（F53）"
    assert "改过的文案XYZ" in chat_md.render(view), "Markdown 读的是导入期的旧值（F53）"

    # EA-108 的两条新共享措辞也要过这一轮：任一侧写回**硬编码字面量**（而不是引用
    # chat_text），上面那轮「两个产物都含共享常量值」抓不到（值相同），这一轮能——
    # 改坏任一侧，产物就不跟着 monkeypatch 的新值变（复审 F6 的判别力缺口）。
    monkeypatch.setattr(chat_text, "INCONSISTENT_EXPORTED", "改过的导出不一致XYZ")
    monkeypatch.setattr(chat_text, "unknown_status_note", lambda s: f"改过的未知状态XYZ({s})")
    html2, md2 = chat_html.render(view), chat_md.render(view)
    for out in (html2, md2):
        assert "改过的导出不一致XYZ" in out, "该侧没读 chat_text（EA-108 的硬编码缺口）"
        assert "改过的未知状态XYZ(weird)" in out, "该侧没读 chat_text（EA-108 的硬编码缺口）"


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
    out = chat_md._media_block(st, "..", "/out/c")
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
