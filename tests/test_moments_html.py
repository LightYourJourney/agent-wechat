"""HTML 渲染：转义、窗口说明、媒体占位、评论回复。纯函数，无 IO。"""
import dataclasses
import datetime
import html.parser

import pytest

from agent_wechat import moments, moments_html


class _P(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []

    def handle_data(self, d):
        self.text.append(d)


def _mk(media=(), comments=(), text="正文", link=None, finder=None):
    m = moments.SnsMedia(kind=moments.KIND_IMAGE, url="http://x", token="T", key="1")
    return moments.SnsPost(tid="T", post_id="P", username="wxid_a", nickname="老王",
                           create_time=1735689600, text=text, is_video_post=False,
                           media=tuple(media), comments=tuple(comments),
                           link=link, finder=finder)


def _rep(**kw):
    base = dict(posts=1, comments=0, likes=0, media_downloaded=0,
                media_failed=0, media_not_downloaded=0, window=(1735689600, 1735689600))
    base.update(kw)
    return moments.MomentsReport(**base)


def _text_of(doc):
    p = _P()
    p.feed(doc)
    return "".join(p.text)


def test_html_is_parseable_and_has_window_notice():
    doc = moments_html.render([_mk()], _rep())
    _P().feed(doc)                                   # 不抛即结构可解析
    t = _text_of(doc)
    assert "缓存窗口" in t and "不是完整历史" in t
    assert "老王" in t and "正文" in t


def test_header_has_no_phantom_local_counter():
    """页首不得出现「媒体：本地 N」。

    这个计数曾经出现、且**恒为 0**：`MomentsReport.media_local` 全仓无写入点，
    而本工具从不读本机缓存图（`cache/*/Sns/Img/` 的文件名与动态对不上）。印出来
    读着像「查过了，本机没有」，实际是「根本没查」——比不印更坏。
    """
    t = _text_of(moments_html.render([_mk()], _rep()))
    assert "媒体：本地" not in t
    assert "媒体：已下载" in t                        # 守住该行其余部分，防止靠删整行通过


def test_text_is_escaped():
    """正文含 HTML 必须转义——朋友圈内容是不可信输入。"""
    doc = moments_html.render([_mk(text="<script>alert(1)</script>")], _rep())
    assert "<script>alert(1)</script>" not in doc
    assert "&lt;script&gt;" in doc


def test_nickname_and_comment_fields_are_escaped():
    """昵称同样不可信：不进 _esc 就能凭 onerror 属性执行脚本。"""
    c = moments.SnsComment(kind=moments.CMT_COMMENT, nickname='<img src=x onerror="alert(1)">',
                           content='<b>同问</b>', ref_nickname="<i>x</i>",
                           create_time=2, is_rich=False)
    post = dataclasses.replace(_mk(comments=[c]), nickname='<b onmouseover="alert(2)">老王</b>')
    doc = moments_html.render([post], _rep(comments=1))
    # 原始标签一律不存活；属性引号也必须转义（`onerror="…"` 里的引号漏转 = 仍可触发）
    assert "<img src=x" not in doc and "<b onmouseover" not in doc
    assert '"alert(1)"' not in doc and '="alert(2)"' not in doc
    assert "<b>同问</b>" not in doc and "<i>x</i>" not in doc
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in doc
    assert "&lt;b onmouseover=&quot;alert(2)&quot;&gt;老王&lt;/b&gt;" in doc
    assert "&lt;b&gt;同问&lt;/b&gt;" in doc and "&lt;i&gt;x&lt;/i&gt;" in doc


def test_undownloaded_media_shows_placeholder():
    m = moments.SnsMedia(kind=moments.KIND_IMAGE, url="http://x", token="T", key="1")
    doc = moments_html.render([_mk(media=[m])], _rep(media_not_downloaded=1))
    t = _text_of(doc)
    assert "远程，未下载" in t
    assert "<img" not in doc                          # 没有本地文件就不放 img


def test_downloaded_media_references_relative_path():
    m = dataclasses.replace(
        moments.SnsMedia(kind=moments.KIND_IMAGE, url="http://x", token="T", key="1"),
        local="media/2025-01/abc.jpg")
    doc = moments_html.render([_mk(media=[m])], _rep(media_downloaded=1))
    assert 'src="media/2025-01/abc.jpg"' in doc


def test_comment_reply_and_likes_render():
    like = moments.SnsComment(kind=moments.CMT_LIKE, nickname="小红", content=None,
                              ref_nickname=None, create_time=1, is_rich=False)
    reply = moments.SnsComment(kind=moments.CMT_COMMENT, nickname="小明", content="同问",
                               ref_nickname="小红", create_time=2, is_rich=False)
    t = _text_of(moments_html.render([_mk(comments=[like, reply])], _rep(likes=1, comments=1)))
    assert "小红" in t and "小明" in t and "同问" in t
    assert "回复" in t                                 # 「小明 回复 小红：同问」


def test_parse_failures_are_disclosed_in_the_notice():
    """解析失败必须出现在页首：否则「0 条动态」与「这个账号没有朋友圈」长得一模一样。

    变异：删掉 `_notice` 里 `if report.failed` 那两行 → 红。
    """
    doc = moments_html.render([], _rep(posts=0, failed=7))
    t = _text_of(doc)
    assert "解析失败 7 条" in t
    assert "没有匹配的动态" in t          # 空列表仍然照说，但不再是唯一解释
    assert "解析失败" not in _text_of(moments_html.render([_mk()], _rep()))  # 0 条时不出现


def test_unparseable_window_timestamp_is_not_a_date():
    """`create_time == 0` 是「时间戳没解析出来」，不是 1970 年。`_when(0)` 返回空串，
    直接套用会把这一行渲染成「覆盖 <b> ~ </b>」——空档看不出是「没有」还是「坏了」。

    变异：去掉 `_notice` 里两个 `if report.window[...]` 守卫 → 渲染成「覆盖 <b> ~ </b>」→ 红。
    """
    doc = moments_html.render([_mk()], _rep(window=(0, 0)))
    assert "覆盖 <b>— ~ —</b>" in doc
    assert "1970" not in doc
    # 一端可解析时只保留那一端，另一端仍是占位符。
    # 年份用与渲染层同一个转换现算：硬编码 "2025" 会在 UTC 负偏移时区假红
    # （1735689600 = 2025-01-01 00:00 UTC，在 UTC-5 是 2024-12-31）。
    ts = 1735689600
    year = datetime.datetime.fromtimestamp(ts).strftime("%Y")
    doc2 = moments_html.render([_mk()], _rep(window=(0, ts)))
    assert f"覆盖 <b>— ~ {year}" in doc2


def test_degraded_section_lists_reasons():
    t = _text_of(moments_html.render([_mk()], _rep(degraded=["3 张媒体下载失败"])))
    assert "3 张媒体下载失败" in t


def test_truncated_is_disclosed():
    """截断必须**带条数**（只说「被截断」用户无从判断差多少）。

    夹具给非 0 的 `remaining`：`truncated=True` + `remaining=0` 在生产里不可能出现
    （`remaining += 1` 就写在置位那两行里），照默认值写等于把「还有 0 条更早的动态没进来」
    这种荒唐话钉成期望输出。

    变异：`_notice` 里把 `{report.remaining}` 写死成 0 → 红。
    """
    t = _text_of(moments_html.render([_mk()], _rep(truncated=True, remaining=3)))
    assert "截断" in t or "truncated" in t.lower()
    assert "还有 3 条更早的动态" in t


def test_video_cover_renders():
    m = moments.SnsMedia(kind=moments.KIND_VIDEO, local="media/video/2025-01/A.mp4",
                         thumb_local="media/video/2025-01/A_thumb.jpg")
    doc = moments_html.render([_mk(media=(m,))], _rep())
    assert 'src="media/video/2025-01/A.mp4"' in doc
    assert 'src="media/video/2025-01/A_thumb.jpg"' in doc


def test_video_without_cover_renders_only_the_video():
    m = moments.SnsMedia(kind=moments.KIND_VIDEO, local="media/video/2025-01/A.mp4")
    doc = moments_html.render([_mk(media=(m,))], _rep())
    assert 'src="media/video/2025-01/A.mp4"' in doc
    assert "_thumb" not in doc


def test_external_link_renders_with_visible_host():
    link = moments.SnsLink(url="https://mp.weixin.qq.com/s/X", title="文章标题")
    doc = moments_html.render([_mk(link=link)], _rep())
    assert 'href="https://mp.weixin.qq.com/s/X"' in doc
    assert "文章标题" in doc
    assert 'class="host">mp.weixin.qq.com<' in doc       # host 必须可见
    assert 'rel="noopener noreferrer nofollow"' in doc
    assert 'target="_blank"' in doc


def test_javascript_scheme_is_not_rendered_as_link():
    """`html.escape` 挡不住 scheme：`javascript:alert(1)` 转义后仍是可执行链接。

    **但要说清是哪一重拦住它的**：`javascript:alert(1)` 没有 authority，
    `urlsplit().hostname` 是 `None`，而 `s.scheme not in ("http", "https")` 同样为真
    ——两条守卫对这个输入**互为冗余**，所以**单独删掉任一条**，本条都照样绿
    （变异实测：删 scheme 行 → 绿；删 `not host` → 绿）。
    真正锁住 scheme 那一行的是下一条（`ftp://`，有主机且非 http(s)）。
    """
    doc = moments_html.render(
        [_mk(link=moments.SnsLink(url="javascript:alert(1)", title="x"))], _rep())
    assert "<a href" not in doc
    assert "javascript:alert(1)" in doc                   # 降级为纯文本，信息不丢


def test_non_web_scheme_with_host_is_not_rendered_as_link():
    """**有主机但 scheme 不是 http(s)** → 必须由 scheme 白名单拦下。

    这是 `s.scheme not in ("http", "https")` 那一行**唯一**的护栏：
    `javascript:` / `data:` 那类没有 authority，被 **scheme 行短路**拦下
    （`not host` 只是 backstop，对该输入验不到它）。变异实测：删掉 scheme 那一行 → 本条红。
    """
    doc = moments_html.render(
        [_mk(link=moments.SnsLink(url="ftp://evil.example.com/x", title="x"))], _rep())
    assert "<a href" not in doc
    assert "ftp://evil.example.com/x" in doc


def test_backslash_host_is_not_rendered_as_link():
    r"""Python 的 urlsplit 不把 `\` 当分隔符，浏览器按 WHATWG 会——显示与落地会不一致。

    实测：`https://evil.com\.mp.weixin.qq.com/x` 在 node 里 host=evil.com，
    在 Python 里 hostname=`evil.com\.mp.weixin.qq.com`。显示哪一个是骗人的。
    """
    doc = moments_html.render(
        [_mk(link=moments.SnsLink(url="https://evil.com\\.mp.weixin.qq.com/x", title="t"))],
        _rep())
    assert "<a href" not in doc


def test_non_ascii_host_is_not_rendered_as_link():
    """IDN 同形字：显示与浏览器实际落地（punycode）可以不是同一个。"""
    doc = moments_html.render(
        [_mk(link=moments.SnsLink(url="https://mp.weıxin.qq.com/x", title="t"))], _rep())
    assert "<a href" not in doc


def test_backslash_in_userinfo_is_not_rendered_as_link():
    r"""反斜杠必须查 `netloc`：`hostname` 已丢掉 userinfo，徽章会骗人。

    `https://evil.com\@mp.weixin.qq.com/x` 实测（Python）`netloc` 是
    `evil.com\@mp.weixin.qq.com`、`username` 是 `evil.com\`、而 `hostname` 是
    **干干净净的** `mp.weixin.qq.com`——只说 `"\\" in host` 会放它过去，
    渲染出徽章 `mp.weixin.qq.com` 而浏览器（WHATWG 先按 `\` 切一刀）落到 `evil.com`。

    变异实测：把 `"\\" in s.netloc` 改回 `"\\" in host` → 本条红。
    """
    doc = moments_html.render(
        [_mk(link=moments.SnsLink(url="https://evil.com\\@mp.weixin.qq.com/x", title="t"))],
        _rep())
    assert "<a href" not in doc


@pytest.mark.parametrize("host", [
    "2852039166",        # 十进制整数 —— node: 169.254.169.254（link-local / 云 metadata）
    "3232235777",        # 十进制整数 —— node: 192.168.1.1
    "0x7f000001",        # 十六进制 —— node: 127.0.0.1
    "0X7F000001",        # 同上，`0X` 大小写不敏感 —— node: 127.0.0.1
    "017700000001",      # 前导零按八进制 —— node: 127.0.0.1
    "0x",                # node: 0.0.0.0（Linux 上连的就是本机）
    "0",                 # 单段数字 —— node: 0.0.0.0
    "1",                 # node: 0.0.0.1
    "1.2",               # 少于四段也照样解析 —— node: 1.0.0.2
    "192.168.1.1",       # 裸 IPv4：真机外链都是域名，保守降级不损失什么
    "192.168.1.1.",      # 末尾点是绝对域名形式，node 照样落到 192.168.1.1
    "2852039166%2e",     # 百分号编码的 `.` —— node: 169.254.169.254
])
def test_host_that_whatwg_resolves_as_ipv4_is_not_rendered_as_link(host):
    """WHATWG 会把这个 host 当 IPv4 解析，Python 的 `urlsplit` 不会——徽章与落地不一致。

    实测（node 的 `new URL(u).hostname`，逐条见参数表）：徽章会显示 `2852039166`，
    浏览器实际去 **169.254.169.254**。这正是 spec §7.4 那条守卫自己的设计理由所禁止的
    ——「两套 URL 语义不一致…它本身就是骗人的」。

    两处判据各自 load-bearing，**分别删都会红**（变异实测）：
      · 删 `_whatwg_ipv4(host)` → 前 11 条红；
      · 删 `"%" in host` → 最后一条（`2852039166%2e`）红——只查数字会被它绕过。

    二进制 `0b101` 不在判据内：node 的 `new URL("https://0b101/x").hostname` 就是
    `0b101`，WHATWG 不认它（已实测，故不必为它降级）。
    """
    url = f"https://{host}/promo"
    doc = moments_html.render([_mk(link=moments.SnsLink(url=url, title="t"))], _rep())
    assert "<a href" not in doc
    assert url in doc                                     # 降级为纯文本，信息不丢


@pytest.mark.parametrize("url,host", [
    ("https://mp.weixin.qq.com/s/X", "mp.weixin.qq.com"),
    ("https://wxapp.tc.qq.com/251/20304/stodownload", "wxapp.tc.qq.com"),
    ("https://shzjwxsns.video.qq.com/150/20250/x", "shzjwxsns.video.qq.com"),
    ("https://szmmsns.qpic.cn/mmsns/XYZ/0", "szmmsns.qpic.cn"),
    ("https://mp.weixin.qq.com:8443/s/X", "mp.weixin.qq.com"),          # 带端口
    ("https://user:pass@mp.weixin.qq.com/s/X", "mp.weixin.qq.com"),     # 正常 userinfo
])
def test_normal_domains_still_render_as_links(url, host):
    """反面对照：数字判据**不得**误伤真机上会出现的外链形态。

    没有这一条，「整类拒掉」可以退化成「全拒」而没人发现——降级分支同样不出
    `<a href`，上面那组只断言「没有 `<a href`」的用例会全绿。
    """
    doc = moments_html.render([_mk(link=moments.SnsLink(url=url, title="t"))], _rep())
    assert f'href="{url}"' in doc
    assert f'class="host">{host}<' in doc


def test_empty_host_is_not_rendered_as_link():
    """`http:///x`：Python `hostname=None`（被 `not host` 拦下），浏览器落到主机 `x`。

    变异实测：删掉 `not host` → 渲染成空徽章 `<span class="host"></span>`，
    浏览器却落到 `x`——本条红。
    """
    doc = moments_html.render(
        [_mk(link=moments.SnsLink(url="http:///x", title="t"))], _rep())
    assert "<a href" not in doc
    assert "http:///x" in doc                             # 降级为纯文本，信息不丢


def test_link_url_and_title_are_escaped():
    """外链块三处 `_esc` 都是 load-bearing：分别改裸插值，全量零红（无护栏）。

    变异实测（三处各删一次，本条都红）。最坏的一处是 href——下面这条低于本条所用
    输入的最坏情形：`url='https://e.com/a" onmouseover="alert(1)'` 裸插值 →
    `<a href="https://e.com/a" onmouseover="alert(1)" target="_blank" …>` ——
    `"` 提前闭合属性，payload 变成 `<a>` 上的**真事件属性**（真 XSS，非仅显示错乱）。
    `url` / `title` 都是好友可控数据，漏一处就出这个。三条路径各锁一处。
    """
    # ① href + title：渲染成链接时
    link = moments.SnsLink(url='https://e.com/a"b&c', title='" onmouseover="alert(1)"')
    doc = moments_html.render([_mk(link=link)], _rep())
    assert 'href="https://e.com/a&quot;b&amp;c"' in doc
    assert 'onmouseover="alert(1)"' not in doc
    # ② title 为空 → 回落用 url 当锚文本，回落那一处同样要转义
    doc2 = moments_html.render(
        [_mk(link=moments.SnsLink(url="https://e.com/a<script>x", title=None))], _rep())
    assert "<script>x" not in doc2 and "&lt;script&gt;x" in doc2
    # ③ 降级成纯文本的分支
    doc3 = moments_html.render(
        [_mk(link=moments.SnsLink(url="javascript:alert(<script>x)", title="t"))], _rep())
    assert "<script>x" not in doc3 and "&lt;script&gt;x" in doc3


def test_finder_block_renders_nickname_and_desc():
    f = moments.SnsFinder(nickname="视频号昵称", desc="#话题#说明", duration=128)
    doc = moments_html.render([_mk(finder=f)], _rep())
    assert "视频号 · 视频号昵称" in doc
    assert "#话题#说明" in doc


def test_finder_block_omits_desc_when_absent():
    doc = moments_html.render(
        [_mk(finder=moments.SnsFinder(nickname="N", desc=None, duration=None))], _rep())
    assert "视频号 · N" in doc
    assert 'class="fdesc"' not in doc


def test_new_css_classes_are_present():
    """四个新 CSS 类必须真的写进产物。

    **本任务自己写了「不补 CSS 产物仍能渲染，只是这几块没样式——『能跑但不像样』
    容易被漏」，却没补断言**（变异实测：把 `_CSS` 里这几条全删掉，一条测试都不红）。
    """
    doc = moments_html.render([_mk()], _rep())
    for sel in (".link{", ".link a{", ".link .host{", ".finder{", ".fdesc{"):
        assert sel in doc, f"CSS 缺 {sel}"
