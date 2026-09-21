"""朋友圈解析层测试：合成 XML + 合成 sns.db，全部离线。"""
import contextlib
import json
import sqlite3

import pytest

from agent_wechat import keys, media, moments, moments_html, paths, sns_media

ACCT = "wxid_testacct01_ab12"

POST_XML = """<SnsDataItem>
  <TimelineObject>
    <id>P1</id><username>wxid_friend</username><createTime>1735689600</createTime>
    <contentDesc>今天的晚饭</contentDesc>
    <ContentObject><type>2</type><mediaList>
      <media>
        <id>m1</id><type>2</type><sub_type>0</sub_type>
        <thumb type="1" token="TOK_THUMB">http://shmmsns.qpic.cn/sns/t/150</thumb>
        <url type="1" md5="ab" key="12345678901234567890" enc_idx="1" token="TOK_URL">http://shmmsns.qpic.cn/sns/u/0</url>
        <size width="100" height="200"/>
      </media>
    </mediaList></ContentObject>
  </TimelineObject>
  <LocalExtraInfo><nickname>老王</nickname>
    <comment_user_list>
      <user_comment>
        <comment_id>1</comment_id><type>2</type><nickname>小明</nickname>
        <username>wxid_xm</username><create_time>1735689700</create_time>
        <content>好吃</content>
      </user_comment>
    </comment_user_list>
    <like_user_list>
      <user_comment>
        <comment_id>2</comment_id><type>1</type><nickname>小红</nickname>
        <create_time>1735689800</create_time>
      </user_comment>
    </like_user_list>
  </LocalExtraInfo>
</SnsDataItem>"""


def test_parse_post_fields():
    p = moments.parse_post("T1", POST_XML)
    assert p is not None
    assert (p.tid, p.post_id, p.username, p.nickname) == ("T1", "P1", "wxid_friend", "老王")
    assert p.create_time == 1735689600
    assert p.text == "今天的晚饭"
    assert p.is_video_post is False
    assert len(p.media) == 1


def test_token_and_key_come_from_attributes():
    """token/key 是 url 元素的属性——这个坑踩过一次，锁死它。"""
    m = moments.parse_post("T1", POST_XML).media[0]
    assert m.kind == moments.KIND_IMAGE
    assert m.token == "TOK_URL"
    assert m.key == "12345678901234567890"
    assert m.enc_idx == "1"
    assert m.thumb == "http://shmmsns.qpic.cn/sns/t/150"
    assert m.video_duration is None


def test_md5_read_from_url_attribute():
    """`url@md5` 也是**属性**（同 token/key 的坑）；缺省即 None，不拿别的字段顶替。"""
    m = moments.parse_post("T1", POST_XML).media[0]
    assert m.md5 == "ab"
    assert m.local is None                            # 解析层不碰产物路径
    live = moments.parse_post("T3", VIDEO_XML).media[1]
    assert live.md5 is None                           # 该 <url> 没有 md5 属性 → None
    assert moments.parse_post("T3", VIDEO_XML).media[0].md5 == "cd"


def test_comments_split_like_vs_comment():
    p = moments.parse_post("T1", POST_XML)
    kinds = sorted(c.kind for c in p.comments)
    assert kinds == [moments.CMT_COMMENT, moments.CMT_LIKE]
    cmt = next(c for c in p.comments if c.kind == moments.CMT_COMMENT)
    assert (cmt.nickname, cmt.content, cmt.create_time) == ("小明", "好吃", 1735689700)
    like = next(c for c in p.comments if c.kind == moments.CMT_LIKE)
    assert like.content is None


def test_repr_hides_credentials():
    """token/key 不得经 repr 泄漏（与 MediaKeys 同规则）。"""
    m = moments.parse_post("T1", POST_XML).media[0]
    r = repr(m)
    assert "TOK_URL" not in r and "12345678901234567890" not in r


def test_malformed_xml_returns_none():
    assert moments.parse_post("T1", "<SnsDataItem><TimelineObject>") is None
    assert moments.parse_post("T1", "") is None
    assert moments.parse_post("T1", "<Other/>") is None


def test_text_only_post_has_no_media():
    xml = ("""<SnsDataItem><TimelineObject><id>P2</id><username>u</username>"""
           """<createTime>1735689600</createTime><contentDesc>只有文字</contentDesc>"""
           """<ContentObject><type>2</type><mediaList/></ContentObject>"""
           """</TimelineObject><LocalExtraInfo><nickname>n</nickname></LocalExtraInfo></SnsDataItem>""")
    p = moments.parse_post("T2", xml)
    assert p.media == () and p.comments == () and p.text == "只有文字"


# ---------- 分支覆盖：视频 / 实况 / 回复 / 富文本 ----------
# spec §7.1 要求样本含视频、回复、表情评论。T2 的 build_url 会按 kind == KIND_VIDEO
# 分支，这条 "6" → video 的映射一旦回归，视频就会静默拼出图片形式的 URL 且无人报错。

VIDEO_XML = """<SnsDataItem>
  <TimelineObject>
    <id>V1</id><username>wxid_friend</username><createTime>1735689600</createTime>
    <contentDesc>视频加实况</contentDesc>
    <ContentObject><type>15</type><mediaList>
      <media>
        <id>mv</id><type>6</type><sub_type>0</sub_type>
        <videoDuration>12</videoDuration>
        <thumb type="1" token="TOK_VTHUMB">http://shmmsns.qpic.cn/sns/vt/150</thumb>
        <url type="1" md5="cd" key="VKEY" enc_idx="0" token="TOK_VURL">http://shmmsns.qpic.cn/sns/v/0</url>
      </media>
      <media>
        <id>ml</id><type>2</type>
        <url type="1" key="IKEY" token="TOK_IURL">http://shmmsns.qpic.cn/sns/i/0</url>
        <LivePhoto><liveMedia>
          <url type="1">http://snsvideo.qpic.cn/live/0</url>
          <enc key="LIVEKEY" ver="1"/>
        </liveMedia></LivePhoto>
      </media>
    </mediaList></ContentObject>
  </TimelineObject>
  <LocalExtraInfo><nickname>老王</nickname></LocalExtraInfo>
</SnsDataItem>"""

INTERACT_XML = """<SnsDataItem>
  <TimelineObject>
    <id>P9</id><username>wxid_friend</username><createTime>1735689600</createTime>
    <contentDesc>互动</contentDesc>
    <ContentObject><type>2</type><mediaList/></ContentObject>
  </TimelineObject>
  <LocalExtraInfo><nickname>老王</nickname>
    <comment_user_list>
      <user_comment>
        <comment_id>1</comment_id><type>2</type><nickname>小明</nickname>
        <username>wxid_xm</username>
        <ref_username>wxid_xh</ref_username><create_time>1735689700</create_time>
        <content>回复你</content><is_rich_text>1</is_rich_text>
      </user_comment>
    </comment_user_list>
    <like_user_list>
      <user_comment>
        <comment_id>2</comment_id><type>1</type><nickname>小红</nickname>
        <username>wxid_xh</username>
        <create_time>1735689800</create_time>
      </user_comment>
    </like_user_list>
  </LocalExtraInfo>
</SnsDataItem>"""


def test_video_media_kind_duration_and_live_photo():
    p = moments.parse_post("T3", VIDEO_XML)
    assert p.is_video_post is True                       # ContentObject/type == 15
    vid, live = p.media
    assert vid.kind == moments.KIND_VIDEO                # media/type == 6
    assert vid.video_duration == 12
    # videoDuration 缺省 / 非数字 → None（不是 0、不抛）
    assert moments.parse_post("T3", VIDEO_XML.replace(
        "<videoDuration>12</videoDuration>", "")).media[0].video_duration is None
    assert moments.parse_post("T3", VIDEO_XML.replace(
        "<videoDuration>12</videoDuration>",
        "<videoDuration>abc</videoDuration>")).media[0].video_duration is None
    # 实况照片：url 取元素文本、key 取 **属性**（与 url@key 同一个坑）
    assert live.kind == moments.KIND_IMAGE
    assert live.live_url == "http://snsvideo.qpic.cn/live/0"
    assert live.live_key == "LIVEKEY"


def test_comment_ref_nickname_and_rich_text():
    """回复目标是**昵称**，不是 `<ref_username>` 里的 wxid。

    真机 183/183 条 `ref_username` 全是 `wxid_…`：直接拿它当昵称渲染，产物里每条回复
    都会印成「小明 回复 wxid_abc123」。XML 没有回复方的昵称字段，故按 `username`
    在**本条动态**的 `<user_comment>` 里反查（被回复者点过赞或评论过就查得到）。

    变异：把 `ref_nickname` 改回 `_text(c, "ref_username")` → 本用例红（拿到 `wxid_xh`）。
    """
    reply, like = moments.parse_post("T4", INTERACT_XML).comments
    assert reply.ref_nickname == "小红"                   # wxid_xh → 昵称
    assert reply.is_rich is True                         # <is_rich_text>1</is_rich_text>
    assert like.ref_nickname is None                     # 缺省 → None
    assert like.is_rich is False


def test_unresolvable_reply_target_is_none_not_a_wxid():
    """反查不到（被回复者在本条动态里既没评论也没点赞）时留 None，**不拿 wxid 顶替**——
    渲染层据此省掉「回复 X」，而不是编一个昵称出来。"""
    xml = INTERACT_XML.replace("<ref_username>wxid_xh</ref_username>",
                               "<ref_username>wxid_nobody</ref_username>")
    reply = moments.parse_post("T4", xml).comments[0]
    assert reply.ref_nickname is None


# ---------- iter_posts：合成 sns.db + 假解密 ----------

@contextlib.contextmanager
def _passthrough(db_path, passphrase, tmpdir=None):
    yield db_path


@pytest.fixture()
def sns(tmp_path, monkeypatch):
    """合成 sns.db（明文 SQLite；decrypt 被打桩成直通）。

    同时隔离密钥缓存并写入假 passphrase——`iter_posts` 会经 `keys.get_passphrase`
    取钥，不隔离就会读到**真实机器的** passphrase.json。
    """
    from agent_wechat import decrypt as dec_mod
    wx, home = tmp_path / "wx", tmp_path / "home"
    d = wx / ACCT / "db_storage" / "sns"
    d.mkdir(parents=True)
    home.mkdir()
    (home / "passphrase.json").write_text(json.dumps(
        {"accounts": {"wxid_testacct01": {"passphrase_hex": "aa" * 32, "captured_at": 0}}}))
    monkeypatch.setattr(paths, "XWECHAT_FILES", wx)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    monkeypatch.setattr(dec_mod, "decrypted", _passthrough)
    monkeypatch.setattr("agent_wechat.moments.decrypt", dec_mod)
    con = sqlite3.connect(d / "sns.db")
    con.execute("CREATE TABLE SnsTimeLine(tid INTEGER PRIMARY KEY DESC, user_name TEXT,"
                " content TEXT, pack_info_buf TEXT)")
    con.commit()
    return con


def _add(con, tid, xml):
    con.execute("INSERT INTO SnsTimeLine(tid, user_name, content) VALUES (?,?,?)",
                (tid, "wxid_friend", xml))
    con.commit()


def test_iter_posts_reads_all_and_skips_malformed(sns):
    _add(sns, 1, POST_XML)
    _add(sns, 2, "<SnsDataItem><TimelineObject>")          # 畸形 → 跳过
    _add(sns, 3, POST_XML.replace("P1", "P3"))
    assert [p.post_id for p in moments.iter_posts(ACCT)] == ["P1", "P3"]


def test_iter_posts_is_newest_first(sns):
    """**最新在前**：调用方的 limit 靠 `iter_posts` 的产出序截断，扫描序（rowid 序）
    会把最旧的那端留住。时间与 tid 序**故意相反**（宽时刻在后插入），这样「碰巧对」
    不可能成立。

    变异：删掉 `iter_posts` 里的 `out.sort(...)` → 退化成扫描序 ["旧","新"] → 红。
    """
    old_ts, new_ts = 1735689600, 1789000000
    _add(sns, 1, POST_XML.replace("1735689600", str(new_ts)).replace("P1", "NEW"))
    _add(sns, 2, POST_XML.replace("1735689600", str(old_ts)).replace("P1", "OLD"))
    assert [p.post_id for p in moments.iter_posts(ACCT)] == ["NEW", "OLD"]


def test_iter_posts_filters_by_month_and_chat(sns):
    # 月中时间戳：任何本地时区（UTC-12..UTC+14）都落在同一个月，期望月用 month_of 现算
    jan_ts, jul_ts = 1736899200, 1752537600
    _add(sns, 1, POST_XML.replace("1735689600", str(jan_ts)))
    _add(sns, 2, POST_XML.replace("1735689600", str(jul_ts)).replace("P1", "P2"))
    assert [p.post_id for p in moments.iter_posts(ACCT, months=[media.month_of(jan_ts)])] == ["P1"]
    # 产出是最新在前（7 月那条在前），过滤本身不改变次序
    assert [p.post_id for p in moments.iter_posts(ACCT, chat_filter="wxid_friend")] == ["P2", "P1"]
    assert [p.post_id for p in moments.iter_posts(ACCT, chat_filter="wxid_other")] == []


def test_iter_posts_missing_db_raises_data_not_found(tmp_path, monkeypatch):
    from agent_wechat import errors
    monkeypatch.setattr(paths, "XWECHAT_FILES", tmp_path / "nope")
    with pytest.raises(errors.DataNotFound):
        list(moments.iter_posts(ACCT))


def test_missing_sns_timeline_table_raises_schema_drift(sns):
    """旧版微信的 sns.db 里没有 `SnsTimeLine`：裸 `sqlite3.OperationalError` 会穿透
    `usecases`（CLI 印 traceback、MCP 退化成通用内部错误），违反「core 只抛
    AgentWeChatError 子类」。spec §5 点名的就是这一行。

    变异：删掉 `iter_posts` 里那圈 `except sqlite3.OperationalError` → 抛的是
    OperationalError（不是 SchemaDrift）→ 红。
    """
    from agent_wechat import errors
    sns.execute("DROP TABLE SnsTimeLine")
    sns.commit()
    with pytest.raises(errors.SchemaDrift) as ei:
        list(moments.iter_posts(ACCT))
    assert ei.value.table == "SnsTimeLine"
    assert isinstance(ei.value, errors.AgentWeChatError)     # 适配层能翻译


def test_parse_failures_are_counted(sns):
    """解析失败必须**有计数**：整个账号的 XML 形状变了时，否则就是「退出码 0 + 0 条动态」，
    与「这个账号没有朋友圈」无法区分（spec §5 第 1 行要求计入 failed）。

    变异：删掉 `iter_posts` 里的 `report.failed += 1` → 计数恒 0 → 红。
    """
    _add(sns, 1, POST_XML)
    _add(sns, 2, "<SnsDataItem><TimelineObject>")          # 畸形
    _add(sns, 3, "<Other><Nothing/></Other>")              # 认识的 XML、不是朋友圈结构
    rep = moments.MomentsReport()
    posts = list(moments.iter_posts(ACCT, report=rep))
    assert [p.post_id for p in posts] == ["P1"]
    assert rep.failed == 2
    assert moments.MomentsReport().failed == 0             # 缺省为 0
    assert rep.to_dict()["failed"] == 2                    # 随报告一起出库


# ---------- F25：createTime 消毒（下游 datetime.fromtimestamp 会抛裸异常） ----------

@pytest.mark.parametrize("bad", [
    "99999999999999999999",       # 远超 time_t：OverflowError
    "253402300799",               # UTC 的 9999-12-31——本机 UTC+8 会落进公元 10000 年：ValueError
    "\u00b2",                     # isdigit() 为真、int() 抛 ValueError（故不能用 isdigit）
    "0", "-1", "", "abc",         # 非正 / 非数字
])
def test_malformed_create_time_is_zeroed_and_still_renderable(bad):
    """`createTime` 是**好友可控**的 XML 字段。越界值会让 `media.month_of`
    （含 `iter_posts` 的月份过滤）与 `moments_html._when` 抛 OverflowError / ValueError——
    都不是 AgentWeChatError，会逃出 core 层；而且**离线渲染路径同样会踩**（无需 --with-media）。
    故在解析边界收口为 0，并实测下游两个消费者都不再抛。"""
    xml = POST_XML.replace("<createTime>1735689600</createTime>",
                           f"<createTime>{bad}</createTime>")
    post = moments.parse_post("T1", xml)
    assert post.create_time == 0
    media.month_of(post.create_time)                       # 不抛
    doc = moments_html.render([post], moments.MomentsReport())
    assert isinstance(doc, str)                            # 不抛


def test_create_time_boundary_is_actually_safe():
    """上界本身必须真能被 `datetime` 表示——否则等于把洞从「超大值」挪到「边界值」。
    `_MAX_TS` 取本地时区最不利（UTC+14）也安全的那一刻，故任何时区都成立。"""
    assert moments._ts(str(moments._MAX_TS)) == moments._MAX_TS
    assert moments._ts(str(moments._MAX_TS + 1)) == 0
    media.month_of(moments._MAX_TS)                        # 边界值不抛
    moments_html._when(moments._MAX_TS)                    # 边界值不抛


def test_create_time_accepts_only_decimal_digits():
    """`isdigit()` 对 "\u00b2" 这类上标为真但 `int()` 会抛——白名单用 isdecimal。"""
    assert moments._ts("1735689600") == 1735689600
    assert moments._ts("\u00b2") == 0
    assert moments._ts(None) == 0


# ---------- videoDuration：真机是浮点（F29）；畸形输入不可崩（F26） ----------

VIDEO_DURATION_XML = """<SnsDataItem><TimelineObject>
  <id>V9</id><username>wxid_friend</username><createTime>1735689600</createTime>
  <ContentObject><type>2</type><mediaList><media>
    <id>m9</id><type>2</type>
    <videoDuration>{dur}</videoDuration>
  </media></mediaList></ContentObject>
</TimelineObject><LocalExtraInfo><nickname>老王</nickname></LocalExtraInfo></SnsDataItem>"""


@pytest.mark.parametrize("bad", ["\u00b2", "abc", "", "-3", "-1.5", "0", "0.0", "inf", "-inf", "nan"])
def test_malformed_video_duration_is_none_and_does_not_raise(bad):
    """F26：`videoDuration` 与 createTime 同源（整数谓词对 `"\u00b2"` 为真、`int()` 却抛），
    而 `parse_post` 只 catch `ET.ParseError`——畸形 XML 能一路崩到整次导出（F25 同源）。

    F29：非正值与**非有限值**（`inf`/`nan`）同样归 None——「时长 0」与「没有时长」
    不是一回事，硬塞一个 0 等于替调用方编数。
    """
    post = moments.parse_post("T1", VIDEO_DURATION_XML.format(dur=bad))
    assert post.media[0].video_duration is None


@pytest.mark.parametrize("raw, want", [("13", 13), ("30.0009995", 30), ("15.033", 15)])
def test_video_duration_accepts_float(raw, want):
    """F29：**真机的 `videoDuration` 是浮点**（实测 `"30.0009995"` / `"15.033"`），
    整数谓词会把 158/181（87%）的真实时长静默丢成 None。

    变异验证：把 `_duration` 换回整数谓词（`v.isdecimal()`），本用例必须**红**。
    `"13"` 是对照组——它在两种实现下都对，用来证明红的是浮点那两条而不是别的东西。
    """
    assert moments.parse_post(
        "T1", VIDEO_DURATION_XML.format(dur=raw)).media[0].video_duration == want


def test_split_url_never_raises_on_hostile_input():
    """畸形 URL 必须返回 None，不得抛。

    `urlsplit("https://[::1/x")` 实测抛 `ValueError: Invalid IPv6 URL`——而 contentUrl
    是好友可控的 XML，裸 ValueError 会逃出 core 层（同 `_ts` / `_duration` 已收口的类）。
    """
    for bad in ("https://[::1/x", "http://[abc/", "https://[", "http://]", "https://[a/b]"):
        assert moments.split_url(bad) is None, bad
        assert moments.safe_host(bad) is None, bad


def test_split_url_normal_cases():
    assert moments.safe_host("https://Example.COM/p?q=1") == "example.com"
    assert moments.split_url(None) is None
    assert moments.split_url("") is not None          # 空串是合法输入，不是畸形
    assert moments.safe_host("") is None              # 但没有 host


# ---------- Task 2：SnsMedia.auth / encrypted（取法变成数据） ----------

# spec §8.3 夹具 ⑤：普通图片（老路径）。token/key 是 url 的属性，无 contentUrl、无 finderFeed。
AUTH_SNS_XML = """<SnsDataItem><TimelineObject>
 <id>P1</id><username>wxid_friend</username><createTime>1735776000</createTime>
 <contentDesc>普通图</contentDesc>
 <ContentObject><type>1</type><contentSubStyle>0</contentSubStyle><mediaList><media>
  <id>13000000000000000004</id><type>2</type><sub_type>0</sub_type>
  <thumb type="1" token="TOK" key="1234567890" enc_idx="0">http://shmmsns.qpic.cn/mmsns/ABC/150</thumb>
  <url type="1" token="TOK" key="1234567890" enc_idx="0" md5="00112233445566778899aabbccddeeff">http://shmmsns.qpic.cn/mmsns/ABC/0</url>
  <size width="1440" height="1080" totalSize="318204"/>
  <videoDuration>0</videoDuration>
  <enc>0</enc>
  <predownload_percent>0</predownload_percent><predown_net_type>0</predown_net_type>
 </media></mediaList></ContentObject>
</TimelineObject><LocalExtraInfo><nickname>张三</nickname></LocalExtraInfo></SnsDataItem>"""


def test_char_existing_sns_path_build_url_unchanged():
    """characterization：老路径的 build_url 输出**逐字符不变**。

    这是本期唯一能证明「既有行为零变化」的手段——否则「全量测试绿」只能证明
    没人碰过测试。**必须在动 auth 字段之前跑绿**，改完仍要绿。
    """
    m = moments.parse_post("P1", AUTH_SNS_XML).media[0]
    assert sns_media.build_url(m) == (
        "https://shmmsns.qpic.cn/mmsns/ABC/0?token=TOK&idx=1")


# spec §8.3 夹具 ③：type=7，url 与 thumb 同址、零 token、<enc> 文本为 "0"
TYPE7_XML = """<SnsDataItem><TimelineObject>
 <id>P2</id><username>wxid_friend</username><createTime>1735776000</createTime>
 <ContentObject><type>7</type><contentSubStyle>0</contentSubStyle><mediaList><media>
  <id>13000000000000000002</id><type>2</type><sub_type>0</sub_type>
  <thumb type="1" key="0" enc_idx="0">http://szmmsns.qpic.cn/mmsns/XYZ/0</thumb>
  <url   type="1" key="0" enc_idx="0">http://szmmsns.qpic.cn/mmsns/XYZ/0</url>
  <size width="960" height="960" totalSize="124101"/>
  <videoDuration>0</videoDuration>
  <enc>0</enc>
  <predownload_percent>0</predownload_percent><predown_net_type>0</predown_net_type>
 </media></mediaList></ContentObject>
</TimelineObject><LocalExtraInfo><nickname>张三</nickname></LocalExtraInfo></SnsDataItem>"""

# spec §8.3 夹具 ②：小视频。url@key 是哨兵 '0'，真密钥在 enc@key；token 在查询串
VIDEO15_XML = """<SnsDataItem><TimelineObject>
 <id>P3</id><username>wxid_friend</username><createTime>1735776000</createTime>
 <ContentObject><title>微信小视频</title><type>15</type><contentSubStyle>0</contentSubStyle>
  <contentUrl>https://support.weixin.qq.com/cgi-bin/mmsupport-bin/readtemplate?a=1</contentUrl>
  <mediaList><media>
   <id>13000000000000000001</id><type>6</type><sub_type>0</sub_type>
   <title>张三的文案</title><description>张三的文案</description>
   <thumb type="1" key="0" enc_idx="0">http://shzjwxsns.video.qq.com/150/20250/snsvideodownload?encfilekey=E&amp;token=T&amp;idx=1</thumb>
   <url type="1" md5="aabbccddeeff00112233445566778899" key="0" enc_idx="0" videomd5="aabbccddeeff00112233445566778899">http://shzjwxsns.video.qq.com/102/20202/snsvideodownload?encfilekey=E&amp;token=T&amp;idx=1</url>
   <size width="288" height="512" totalSize="12871"/>
   <videoDuration>9.66600037</videoDuration>
   <enc key="20260918">1</enc>
   <predownload_percent>0</predownload_percent><predown_net_type>0</predown_net_type>
  </media></mediaList>
 </ContentObject>
</TimelineObject><LocalExtraInfo><nickname>张三</nickname></LocalExtraInfo></SnsDataItem>"""


def test_media_of_marks_ready_when_no_token_attribute():
    """无 url@token ⟹ AUTH_READY，且 token 必为 None（spec §3.4 的不变式）。"""
    m = moments.parse_post("P2", TYPE7_XML).media[0]
    assert m.auth == moments.AUTH_READY
    assert m.token is None
    assert m.encrypted is False
    assert m.key is None


def test_media_of_takes_key_from_enc_element_when_encrypted():
    """url@key 是哨兵 '0' 时，密钥取自 enc@key（真机 180/180 都是这形态）。"""
    m = moments.parse_post("P3", VIDEO15_XML).media[0]
    assert m.auth == moments.AUTH_READY          # url@token 不存在
    assert m.encrypted is True                   # <enc> 文本 == '1'
    assert m.key == "20260918"                   # 来自 enc@key，不是 '0'
    assert m.kind == moments.KIND_VIDEO
    assert m.thumb is not None                   # 封面地址保留


def test_media_of_keeps_sns_path_when_token_attribute_present():
    """有 url@token ⟹ AUTH_SNS，密钥仍取自 url@key。"""
    m = moments.parse_post("P1", AUTH_SNS_XML).media[0]
    assert m.auth == moments.AUTH_SNS
    assert m.encrypted is False
    assert m.key == "1234567890"


def test_zero_key_sentinel_is_not_used_as_key():
    """`'0'` 是「无密钥」哨兵——原先会被当字符串传给 ISAAC64(0) 解出乱码。

    只换 url 的 key 属性；`<enc>` 文本保持 '0'（真机这 312 条里没有一条同时满足
    `x-enc: 1`，故既有行为零变化，新增的保护是哨兵不再被当密钥）。
    """
    xml = AUTH_SNS_XML.replace('key="1234567890"', 'key="0"')
    m = moments.parse_post("P1", xml).media[0]
    assert m.key is None


# ---------- Task 5：finderFeed 封面与 contentUrl 外链 ----------

# spec §8.3 夹具 ①：视频号。媒体在 finderFeed 下，ContentObject/mediaList 为空
FINDER_XML = """<SnsDataItem><TimelineObject>
 <id>P4</id><username>wxid_friend</username><createTime>1735776000</createTime>
 <contentDesc>转发视频号</contentDesc>
 <ContentObject><type>28</type><contentSubStyle>0</contentSubStyle>
  <contentUrl>https://support.weixin.qq.com/update/?v=1</contentUrl>
  <mediaList/>
  <finderFeed><objectId>12000000000000000001</objectId>
   <objectNonceId>11267274765907987420_0_25_0_0</objectNonceId><feedType>4</feedType>
   <nickname>视频号昵称</nickname><avatar>https://wx.qlogo.cn/finderhead/A/0</avatar>
   <desc>#话题#说明文字</desc><mediaCount>1</mediaCount>
   <username>v2_060000231003b20fB</username><authIconType>2</authIconType>
   <mediaList><media><mediaType>4</mediaType>
    <url>http://wxapp.tc.qq.com/251/20302/stodownload?encfilekey=E&amp;token=T&amp;idx=1</url>
    <thumbUrl>http://finder.video.qq.com/251/20304/stodownload?encfilekey=E&amp;token=T&amp;idx=1</thumbUrl>
    <width>720.0</width><height>544.0</height>
    <coverUrl>http://wxapp.tc.qq.com/251/20304/stodownload?encfilekey=E&amp;token=T&amp;idx=1</coverUrl>
    <videoPlayDuration>128</videoPlayDuration>
   </media></mediaList></finderFeed>
 </ContentObject>
</TimelineObject><LocalExtraInfo><nickname>张三</nickname></LocalExtraInfo></SnsDataItem>"""

# spec §8.3 夹具 ④：外链。contentUrl 是第三方网页；<url> 带 token（微信 CDN 配图）
LINK_XML = """<SnsDataItem><TimelineObject>
 <id>P5</id><username>wxid_friend</username><createTime>1735776000</createTime>
 <ContentObject><title>文章标题</title>
  <contentUrl>https://mp.weixin.qq.com/s/AbCdEf</contentUrl>
  <type>3</type><contentSubStyle>0</contentSubStyle>
  <mediaList><media>
   <id>13000000000000000003</id><type>2</type><sub_type>0</sub_type>
   <thumb type="1" token="TOK" key="1234567890" enc_idx="0">http://shmmsns.qpic.cn/mmsns/D/150</thumb>
   <url type="1" token="TOK" key="1234567890" enc_idx="0" md5="aabbccddeeff00112233445566778899">http://shmmsns.qpic.cn/mmsns/D/0</url>
   <size width="1080" height="1440" totalSize="203118"/>
   <videoDuration>0</videoDuration>
   <enc>0</enc>
   <predownload_percent>0</predownload_percent><predown_net_type>0</predown_net_type>
  </media></mediaList></ContentObject>
</TimelineObject><LocalExtraInfo><nickname>张三</nickname></LocalExtraInfo></SnsDataItem>"""


def test_parse_post_reads_finder_feed_cover():
    """视频号：ContentObject/mediaList 为空，媒体全在 finderFeed 下（真机 27/29 如此）。

    **取的是 `<thumbUrl>`，不是 `<coverUrl>`**：夹具特意让两者不同（真机 30 条
    finderFeed media 里 7 条如此，其中一条连主机都不同——thumbUrl 在
    `finder.video.qq.com`、coverUrl 在 `wxapp.tc.qq.com`）。两份 URL 写成同一个值时，
    `_finder_media` 改读 `coverUrl` 也全绿——那是**夹具编码了本不存在的形态**。
    变异实测：把 `_text(mm, "thumbUrl")` 改成 `coverUrl` → 本条红。
    """
    p = moments.parse_post("P4", FINDER_XML)
    assert len(p.media) == 1
    m = p.media[0]
    assert m.kind == moments.KIND_IMAGE
    assert m.auth == moments.AUTH_READY
    assert m.token is None
    assert m.url == ("http://finder.video.qq.com/251/20304/stodownload?"
                     "encfilekey=E&token=T&idx=1")
    assert p.finder is not None
    assert p.finder.nickname == "视频号昵称"
    assert p.finder.desc == "#话题#说明文字"
    assert p.finder.duration == 128


def test_parse_post_support_stub_is_not_a_link():
    """support 桩必须排除——真机带桩的卡片有 192 条，不排除会渲染出 192 个无用链接。"""
    assert moments.parse_post("P4", FINDER_XML).link is None
    assert moments.parse_post("P3", VIDEO15_XML).link is None


def test_parse_post_reads_external_link():
    p = moments.parse_post("P5", LINK_XML)
    assert p.link is not None
    assert p.link.url == "https://mp.weixin.qq.com/s/AbCdEf"
    assert p.link.title == "文章标题"
    # 配图仍是普通媒体，走老路径
    assert len(p.media) == 1 and p.media[0].auth == moments.AUTH_SNS


def test_parse_post_survives_malformed_content_url():
    """畸形 contentUrl 不得让整次解析崩——裸 ValueError 会穿到 CLI 打 traceback。"""
    xml = LINK_XML.replace("https://mp.weixin.qq.com/s/AbCdEf", "https://[::1/x")
    p = moments.parse_post("P5", xml)
    assert p is not None
    assert p.link is None


def test_finder_without_nickname_does_not_crash():
    xml = FINDER_XML.replace("<nickname>视频号昵称</nickname>", "")
    p = moments.parse_post("P4", xml)
    assert p is not None and p.finder is not None
    assert p.finder.nickname == ""
