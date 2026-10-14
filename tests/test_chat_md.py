# tests/test_chat_md.py
import pytest

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


def test_user_content_is_hard_escaped_against_markdown_injection():
    """EA-037：`_t` 只换 `<`/`>`，Markdown 元字符原样进产物——一条
    `![](http://evil/x.png)` 的消息在产物里成为**远程图片引用**：打开导出就会去打那个 URL
    （泄露「你打开过这份导出」+ IP）；行首 `#` / `>` 变成**假标题 / 假引用块**（看着像我们
    生成的）；`[点我](javascript:…)` 变成可点链接。

    用户内容（正文 / 昵称 / 转账字段）现在走 `_esc_md` 硬转义；**我们的措辞**（`〔…〕`、
    计数、时间）仍走 `_t`。把三处 `_esc_md` 换回 `_t` 本测试必红。
    """
    out = chat_md.render(_view(rows=[
        {"msg_key": "k1", "ts": 1, "msg_type": 1,
         "content": "![](http://evil.example/x.png)\n# 假标题\n[点我](javascript:alert(1))",
         "sender_name": "张*三", "is_self": False}]))

    assert r"\!\[" in out, "远程图片引用没被挡住"
    assert r"\# 假标题" in out, "行首的假标题没被挡住"
    assert r"\[点我\]\(javascript" in out, "可点链接没被挡住"
    assert r"张\*三" in out, "昵称里的 Markdown 元字符没被转义"
    assert "![](" not in out and "](javascript" not in out
    # 反向守卫：**我们的**措辞不转义（时间与标签由程序生成，转义只会让产物难读）
    assert "1970-01-01" in out


# ---------- 修轮 2：目的地、块级结构与「每个用户内容点各有一条能红的用例」 ----------

_ATK = "![](http://evil.example/x.png)"


def _render_appmsg(a: appmsg.AppMsg) -> str:
    """一条 appmsg 走**真路径**（`chatview.build` → `chat_md.render`）后的产物。"""
    return chat_md.render(chatview.build(
        "c", title="t", rows=[{"msg_key": "k1", "ts": 1, "msg_type": 49, "content": "",
                               "sender_name": "我", "is_self": True}],
        parts={"k1": a}, media={}, out_dir="/tmp",
        truncated=False, unparsed=0, rejected=0, degraded=()))


def _empty(kind: int, **kw) -> appmsg.AppMsg:
    base = dict(kind=kind, title=None, fields=(), quote=None, link=None, file=None,
                transfer=None)
    base.update(kw)
    return appmsg.AppMsg(**base)


def _line_with(out: str, needle: str) -> str:
    return next(ln for ln in out.split("\n") if needle in ln)


@pytest.mark.parametrize("point, a", [
    pytest.param("appmsg 标题",
                 _empty(appmsg.KIND_LINK, title=_ATK,
                        link=appmsg.Link(url="https://ok.example/", title="t", des=None)),
                 id="title"),
    pytest.param("字段表的键与值",
                 _empty(appmsg.KIND_LINK, fields=(("a.b", _ATK),),
                        link=appmsg.Link(url="https://ok.example/", title="t", des=None)),
                 id="fields"),
    pytest.param("引用自带的原文",
                 _empty(appmsg.KIND_QUOTE, title="引用",
                        quote=appmsg.Quote(svrid=1, displayname="张*三", content=_ATK,
                                           create_time=1, from_usr=None)),
                 id="quote-content"),
    pytest.param("链接的标题",
                 _empty(appmsg.KIND_LINK,
                        link=appmsg.Link(url="https://ok.example/", title=_ATK, des=None)),
                 id="link-title"),
    pytest.param("链接的描述",
                 _empty(appmsg.KIND_LINK,
                        link=appmsg.Link(url="https://ok.example/", title="t", des=_ATK)),
                 id="link-des"),
    pytest.param("文件的名字",
                 _empty(appmsg.KIND_FILE, title="文件",
                        file=appmsg.FileRef(title=_ATK, fileext="pdf", totallen=2048,
                                            md5=None)),
                 id="file-title"),
    pytest.param("转账的备注",
                 _empty(appmsg.KIND_TRANSFER, title="转账",
                        transfer=appmsg.Transfer(
                            paysubtype="3", amount_desc="¥100.00", memo=_ATK, payer=None,
                            receiver="wxid_testacct02", transaction_id="TXN1",
                            transfer_id="TR1", begin_time=None, effective_date=None,
                            invalid_time=None, is_red_envelope=False)),
                 id="transfer-memo"),
])
def test_every_user_content_point_in_an_appmsg_is_hard_escaped(point, a):
    """修轮 2（复验 ⑥-3）：修轮给 7 个用户内容点补了 `_esc_md`，而**一处覆盖都没有**——
    复验在副本里逐个把它们换回 `_t`，全量 1184 条**一个都不红**。

    这条按点参数化，每一条都能为「它自己那一处」的变异变红（写完之后逐点验过）。
    `_ATK` 是能造出**远程图片引用**的最小载荷：打开导出就会去打那个 URL。
    """
    out = _render_appmsg(a)
    assert _ATK not in out, f"{point}里的载荷原样落进了产物"
    assert r"\!\[" in out, f"{point}没有被转义"


def test_the_page_title_is_hard_escaped():
    """页面标题（`# 会话：{view.title}`）修轮前完全没转义：会话名里的 `[]()` 能把整页
    标题撕开（这一处比 appmsg 子字段更近——`--chat` 一跑就渲染）。

    行首那个 `#` 是**我们的**结构（不转义），被转义的是标题**内容**。
    """
    out = chat_md.render(_view(title="张三 " + _ATK))
    assert _ATK not in out
    assert "# 会话：张三 \\!\\[" in out


def test_md_side_url_guard_refuses_a_javascript_link():
    """复验 ⑥-3 的 M2：把 MD 侧的 `url_is_safe` 守卫整个拿掉，1184 条**全绿**。

    与 HTML 侧同一类输入（`test_chat_html` 有一条镜像用例）：那边一直拦着，MD 侧在
    EA-034 之前 `[点我](javascript:…)` 是**可点**的。
    """
    out = _render_appmsg(_empty(appmsg.KIND_LINK, title="限时活动",
                                link=appmsg.Link(url="javascript:alert(1)", title="点我",
                                                 des=None)))
    assert "](javascript" not in out, "javascript: 成了可点链接"
    assert "链接：" in out, "不安全的 URL 要**显示**出来（用户看得见去哪），只是不可点"

    ok = _render_appmsg(_empty(appmsg.KIND_LINK, title="正常",
                               link=appmsg.Link(url="https://example.com/docs?a=1",
                                                title="示例", des=None)))
    assert "[示例](https://example.com/docs?a=1)" in ok, "正常链接被一并拒了"


@pytest.mark.parametrize("sep, name", [
    ("\n", "换行"), ("\r", "回车"), ("\t", "制表符"), (" ", "空格"), (")", "右括号"),
])
def test_a_url_that_cannot_be_written_as_a_destination_is_never_made_a_link(sep, name):
    """修轮 2（复验 ①-A1）：目的地用 `_t`（只换 `<>`）拼出来，而 `url_is_safe` 看不见
    `urlsplit` **静默删掉**的控制字符——于是这几种形态在产物里**真的**造出 `<img>`：

        [点我](https://ok.example/promo<换行>![](http://evil.example/x.png))

    pandoc 的 gfm 与 commonmark 两个 reader 实测都会渲染出
    `<img src="http://evil.example/x.png" />`（`)" 与空格那两种是目的地被提前终止）。
    载具是真的：appmsg 的 `<url>` 字段经 `appmsg.parse` 原样带出（凭据过滤不拦它）。

    判据放在 **MD 侧**而不是 `url_is_safe`：`()` 在 URL 里是**合法且常见**的
    （`…/wiki/Foo_(bar)`），把它们加进共用判据会让 HTML 侧也拒绝正常的维基链接。
    这里问的是另一件事——「这个串能不能原样写进 `](…)` 而不改变它在哪结束」。
    """
    bad = f"https://ok.example/promo{sep}![]({_ATK[4:]})"
    out = _render_appmsg(_empty(appmsg.KIND_LINK, title="限时活动",
                                link=appmsg.Link(url=bad, title="点我", des=None)))
    assert "](" not in out, f"{name}把用户内容漏出了链接目的地"
    assert _ATK not in out, f"{name}形态下载荷进了正文"
    assert "ok.example/promo" in out, "退成纯文本后 URL 本身仍要看得见"


def test_a_safe_url_still_becomes_a_clickable_link():
    """反向守卫：判据别做成一刀切（正常链接、带查询串与锚点的都要照常）。"""
    for url in ("https://example.com/docs?a=1&b=2", "https://example.com/a#frag",
                "https://example.com/a-b_c~d"):
        out = _render_appmsg(_empty(appmsg.KIND_LINK, title="正常",
                                    link=appmsg.Link(url=url, title="示例", des=None)))
        assert f"[示例]({url})" in out, f"{url} 不该被拒"


@pytest.mark.parametrize("payload, needle", [
    pytest.param("   # 假标题", "假标题", id="1-3 空格 + ATX 标题"),
    pytest.param("   - 假列表", "假列表", id="1-3 空格 + 无序列表"),
    pytest.param("  1. 假有序", "假有序", id="1-3 空格 + 有序列表"),
    pytest.param("标题行\n ===", "===", id="1 空格 + setext 下划线"),
    pytest.param("标题行\n   ---", "---", id="3 空格 + 分隔线"),
    pytest.param("abc\r# 回车后", "回车后", id="裸 CR 也是行结束（CommonMark 规范）"),
])
def test_block_marker_at_a_line_start_is_escaped(payload, needle):
    """修轮 2（复验 ①-A2）：块级标记那一档只认**第 0 列**，而 CommonMark 允许块级标记前有
    **最多 3 个空格**（ATX 标题 / 列表 / setext 下划线 / 分隔线全都如此）——复验实测
    `"   # 假标题"` 与 `"标题\\n ==="` 都真的成了结构。

    判据：载荷那一行的**行首 5 个字符内**必须出现转义反斜杠（缩进最多 3 列，所以转义要么
    落在第 0 列、要么落在标记的第一个字符之前）。
    """
    out = chat_md.render(_view(rows=[
        {"msg_key": "k1", "ts": 1, "msg_type": 1, "content": payload,
         "sender_name": "我", "is_self": True}]))
    line = _line_with(out, needle)
    assert "\\" in line[:5], f"行首没有被转义：{line!r}"


def _indent_columns(line: str) -> int:
    """行首空白折算成列数（制表符按 4 列制表位）——**独立于被测实现**，抄的是 CommonMark。"""
    col, i = 0, 0
    while i < len(line) and line[i] in " \t":
        col += 1 if line[i] == " " else 4 - col % 4
        i += 1
    return col


@pytest.mark.parametrize("prefix", [
    pytest.param(p, id=f"{p!r}")
    for p in ("    ", "     ", "\t", "\t\t", " \t", "\t ", "  \t", " \t ", "\t  \t",
              "   \t", "\t   ")
])
def test_an_indented_line_cannot_start_a_code_block_and_shows_no_backslash(prefix):
    """缩进满 4 列 = 缩进代码块。打断它的机制**不是**在行首插反斜杠——`\\` + 空白不是
    CommonMark 的合法转义，那个反斜杠会被原样渲染出来。

    这是复审 2 用真实语料量出来的：400 条真实 appmsg 抽样里 57 条命中这一档，其中 **56 条
    是段落续行**（前一非空行是普通文本）——那种位置本来就不会成代码块，插反斜杠是**纯代价**
    （修复前后都没有 `<pre>`，只多一个可见的 `\\`）。改成把行首那一个空白字符写成实体之后，
    这 56 条的渲染与 EA-037 之前逐字相同，而真正在块首的那些照样不成代码块。

    两条判据各自对应一种退化：`_indent_columns` 那条在「整档删掉」时红，`startswith` 那条
    在「改回插反斜杠」时红。
    """
    out = chat_md.render(_view(rows=[
        {"msg_key": "k1", "ts": 1, "msg_type": 1, "content": prefix + "缩进正文",
         "sender_name": "我", "is_self": True}]))
    line = _line_with(out, "缩进正文")
    assert _indent_columns(line) < 4, f"这一行仍是缩进代码块：{line!r}"
    assert not line.startswith("\\"), f"行首插了反斜杠（它会渲染出来）：{line!r}"


def test_a_whitespace_only_line_is_left_alone():
    """只有空白的行**本来就不是**代码块（CommonMark：缩进代码块由非空白行组成），动它会把
    前后两个段落并成一个——实测 `"甲\\n     \\n乙"`（5 空格）插了实体之后从两个段落变成一个。
    """
    out = chat_md.render(_view(rows=[
        {"msg_key": "k1", "ts": 1, "msg_type": 1, "content": "甲\n     \n乙",
         "sender_name": "我", "is_self": True}]))
    assert "&#32;" not in out and "&#9;" not in out
    assert _line_with(out, "甲").strip() == "甲"
    assert _line_with(out, "乙").strip() == "乙"


def test_crlf_and_cr_are_treated_as_line_endings():
    """`\\r\\n` / 裸 `\\r` 与 `\\n` 同义（CommonMark 把它们都列为行结束）。

    只按 `\\n` 切行时，`abc\\r# 假标题` 是**一行**，行首是 `abc` → 什么都不转义，
    而按规范切行的渲染器会看到一个真的 ATX 标题。
    """
    out = chat_md.render(_view(rows=[
        {"msg_key": "k1", "ts": 1, "msg_type": 1, "content": "abc\r\n# 假标题\r\nxyz",
         "sender_name": "我", "is_self": True}]))
    assert "\r" not in out, "产物里还留着 CR（渲染器按规范切行时它仍是行结束）"
    assert _line_with(out, "假标题").startswith("\\#")


# ---------- EA-041：位置 / 名片的结构化块 ----------

def _extra_view(**kw):
    return chatview.build(
        "c", title="t", rows=[{"msg_key": "k1", "ts": 1, "msg_type": kw.pop("t", 48),
                               "content": kw.pop("text", "[location]"),
                               "sender_name": "我", "is_self": True}],
        parts={}, media={}, out_dir="/tmp", truncated=False, unparsed=0, rejected=0,
        degraded=(), **kw)


def test_a_location_renders_its_fields_and_drops_the_placeholder():
    """EA-041 的验收：位置消息在产物里出现 **x / y / label 结构化块**。

    `text` 那一行（`[location]` 占位）必须**消失**——它与结构化块逐字重复，这正是 appmsg
    那条既有的处置（`content` 的契约不变，只是产物里不再重复印一遍）。
    """
    from agent_wechat import msgextra
    x = msgextra.Extra(48, (("x", "39.9"), ("y", "116.4"), ("label", "某地")))
    out = chat_md.render(_extra_view(extras={"k1": x}))
    assert "**位置**" in out
    # 坐标里的 `.` 会被行首那一档的「有序列表」判据（`\d+\.`）转义成 `39\.9`——**既有行为**，
    # 不是本批引入：`_esc_md` 逐值调用，判不出这个值最终落在行中还是行首，于是按行首处理。
    # 渲染结果相同（pandoc 两边都出 `39.9`），只是源码多一个反斜杠；已在台账记为观察项。
    assert "- x：39\.9" in out and "- y：116\.4" in out and "- label：某地" in out
    assert "[location]" not in out, "占位行与结构化块重复，必须收掉"
    # 没有 extras 时**一行都不印**，且占位行照旧（老库 / 没跑过 extras rebuild 的机器）
    plain = chat_md.render(_extra_view())
    # 占位行本身是**用户内容**吗？不是——但它是**我们**从 `content` 直接印出来的一句，走
    # `_esc_md`（`[` `]` 会被转义）：所以判据写成转义后的形态。
    assert "位置" not in plain and r"\[location\]" in plain


def test_a_card_renders_its_identity_fields():
    from agent_wechat import msgextra
    x = msgextra.Extra(42, (("username", "wxid_a"), ("nickname", "甲"), ("alias", "A")))
    out = chat_md.render(_extra_view(t=42, text="[type42]", extras={"k1": x}))
    assert "**名片**" in out and "- nickname：甲" in out
    assert r"\[type42\]" not in out


def test_an_extra_without_readable_fields_says_so():
    """没命中任何白名单属性时**印共享的那句占位**，不印一个空块——空块读起来像渲染坏了。
    与 HTML 侧共用 `chat_text.EXTRA_NO_FIELDS`（改一处两个产物同时变）。"""
    from agent_wechat import chat_text, msgextra
    out = chat_md.render(_extra_view(t=42, text="[type42]",
                                     extras={"k1": msgextra.Extra(42, ())}))
    assert chat_text.EXTRA_NO_FIELDS in out
    assert "**名片**" in out


def test_extra_field_values_are_hard_escaped():
    """字段值是**用户内容**：`label` 是地点的名字、`nickname` 是别人的昵称。"""
    from agent_wechat import msgextra
    x = msgextra.Extra(48, (("label", _ATK),))
    out = chat_md.render(_extra_view(extras={"k1": x}))
    assert _ATK not in out and r"\!\[" in out
