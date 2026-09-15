"""朋友圈解析层测试：合成 XML + 合成 sns.db，全部离线。"""
import contextlib
import json
import sqlite3

import pytest

from agent_wechat import keys, media, moments, moments_html, paths

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
    <user_comment>
      <comment_id>1</comment_id><type>2</type><nickname>小明</nickname>
      <username>wxid_xm</username><create_time>1735689700</create_time>
      <content>好吃</content>
    </user_comment>
    <user_comment>
      <comment_id>2</comment_id><type>1</type><nickname>小红</nickname>
      <create_time>1735689800</create_time>
    </user_comment>
  </TimelineObject>
  <LocalExtraInfo><nickname>老王</nickname></LocalExtraInfo>
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
    <user_comment>
      <comment_id>1</comment_id><type>2</type><nickname>小明</nickname>
      <ref_username>小红</ref_username><create_time>1735689700</create_time>
      <content>回复你</content><is_rich_text>1</is_rich_text>
    </user_comment>
    <user_comment>
      <comment_id>2</comment_id><type>1</type><nickname>小红</nickname>
      <create_time>1735689800</create_time>
    </user_comment>
  </TimelineObject>
  <LocalExtraInfo><nickname>老王</nickname></LocalExtraInfo>
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
    reply, like = moments.parse_post("T4", INTERACT_XML).comments
    assert reply.ref_nickname == "小红"                   # <ref_username>
    assert reply.is_rich is True                         # <is_rich_text>1</is_rich_text>
    assert like.ref_nickname is None                     # 缺省 → None
    assert like.is_rich is False


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


def test_iter_posts_filters_by_month_and_chat(sns):
    # 月中时间戳：任何本地时区（UTC-12..UTC+14）都落在同一个月，期望月用 month_of 现算
    jan_ts, jul_ts = 1736899200, 1752537600
    _add(sns, 1, POST_XML.replace("1735689600", str(jan_ts)))
    _add(sns, 2, POST_XML.replace("1735689600", str(jul_ts)).replace("P1", "P2"))
    assert [p.post_id for p in moments.iter_posts(ACCT, months=[media.month_of(jan_ts)])] == ["P1"]
    assert [p.post_id for p in moments.iter_posts(ACCT, chat_filter="wxid_friend")] == ["P1", "P2"]
    assert [p.post_id for p in moments.iter_posts(ACCT, chat_filter="wxid_other")] == []


def test_iter_posts_missing_db_raises_data_not_found(tmp_path, monkeypatch):
    from agent_wechat import errors
    monkeypatch.setattr(paths, "XWECHAT_FILES", tmp_path / "nope")
    with pytest.raises(errors.DataNotFound):
        list(moments.iter_posts(ACCT))


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
