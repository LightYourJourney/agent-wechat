"""编排层：合成 sns.db + 注入假 fetch，全程离线。"""
import contextlib
import json
import pathlib
import re
import sqlite3

import pytest

from agent_wechat import errors, keys, media, moments, paths, sns_media, usecases
from moments_fixtures import ACCT, _passthrough, env  # noqa: F401  (EA-117 共享夹具)


XML = """<SnsDataItem><TimelineObject>
 <id>P1</id><username>wxid_friend</username><createTime>1735689600</createTime>
 <contentDesc>正文</contentDesc>
 <ContentObject><type>2</type><mediaList><media>
   <id>m1</id><type>2</type>
   <url type="1" md5="aabbccddeeff00112233445566778899" key="3" enc_idx="1" token="TK">http://shmmsns.qpic.cn/sns/u/0</url>
 </media></mediaList></ContentObject>
</TimelineObject><LocalExtraInfo><nickname>老王</nickname></LocalExtraInfo></SnsDataItem>"""

# 第二条动态（无媒体），仅供 truncation 测试自建对照组——共享夹具仍只插一条。
SECOND_XML = """<SnsDataItem><TimelineObject>
 <id>P2</id><username>wxid_friend</username><createTime>1735776000</createTime>
 <contentDesc>第二条</contentDesc>
</TimelineObject><LocalExtraInfo><nickname>老王</nickname></LocalExtraInfo></SnsDataItem>"""

# 白名单外的宿主：build_url 返回 None，「不去取」，一个请求都不发。
NO_TOKEN_XML = """<SnsDataItem><TimelineObject>
 <id>P3</id><username>wxid_friend</username><createTime>1735776000</createTime>
 <contentDesc>站外图</contentDesc>
 <ContentObject><type>1</type><contentSubStyle>0</contentSubStyle><mediaList><media>
   <id>m3</id><type>2</type>
   <url type="1" md5="00112233445566778899aabbccddeeff">https://y.music.163.com/x.jpg</url>
 </media></mediaList></ContentObject>
</TimelineObject><LocalExtraInfo><nickname>张三</nickname></LocalExtraInfo></SnsDataItem>"""


def _md5_xml(bad_md5: str) -> str:
    """一条 `url@md5` 可控的动态——该属性来自好友发的动态，属未经消毒的外部输入。"""
    return f"""<SnsDataItem><TimelineObject>
 <id>P9</id><username>wxid_friend</username><createTime>1735776000</createTime>
 <contentDesc>恶意 md5</contentDesc>
 <ContentObject><type>2</type><mediaList><media>
   <id>m9</id><type>2</type>
   <url type="1" md5="{bad_md5}" key="3" enc_idx="1" token="TK">http://shmmsns.qpic.cn/sns/u/9</url>
 </media></mediaList></ContentObject>
</TimelineObject><LocalExtraInfo><nickname>老王</nickname></LocalExtraInfo></SnsDataItem>"""


def _add_post(xml: str, tid: int = 2) -> None:
    """往合成库里补插一条动态。共享 `env` 夹具只插一条，需要对照组的测试自行补。"""
    con = sqlite3.connect(paths.db_storage(ACCT) / "sns" / "sns.db")
    con.execute("INSERT INTO SnsTimeLine(tid, user_name, content) VALUES (?,'u',?)",
                (tid, xml))
    con.commit()
    con.close()


def _jpeg() -> bytes:
    """`fetch` 的契约是返回**已解密明文**；这是最小可嗅探的 JPEG。"""
    return b"\xff\xd8\xff" + bytes(64)


def _files_outside(ancestor: pathlib.Path, out_root: pathlib.Path) -> list[pathlib.Path]:
    """ancestor 下落在 out_root 之外的既有文件（产物根越界的物证）。"""
    root = out_root.resolve()
    return [p for p in ancestor.rglob("*")
            if p.is_file() and not p.resolve().is_relative_to(root)]


def test_export_offline_writes_html_without_any_network(env, monkeypatch):
    """核心边界：不带 with_media 时，一个网络调用都不许发生。"""
    def boom(*a, **k):
        raise AssertionError("离线导出不应发起任何网络请求")
    monkeypatch.setattr(sns_media, "fetch", boom)

    res = usecases.moments_export(account=ACCT)
    assert not isinstance(res, usecases.NeedConfirm)
    html_path = pathlib.Path(res.out_dir) / "index.html"
    assert html_path.exists()
    doc = html_path.read_text(encoding="utf-8")
    assert "缓存窗口" in doc and "远程，未下载" in doc
    assert res.report.media_not_downloaded == 1
    assert res.report.media_downloaded == 0
    assert res.report.posts == 1


def test_export_refuses_a_relative_home(env, tmp_path, monkeypatch):
    """F1 的同族入口（review-T9 复验条件 3）：`moments export` 的默认产物根是
    `HOME_DIR/moments`，而 usecases 入口此前不经过任何闸——相对 `AGENT_WECHAT_HOME` 下
    它会经 `media.ensure_tree` 在 CWD 里建出整棵树（EA-003 的原始后果换了个入口）。

    删掉 `paths.ensure_home_dirs()` 那行本测试必红。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "HOME_DIR", pathlib.Path("relhome"))

    with pytest.raises(errors.HomeUnavailable):
        usecases.moments_export(account=ACCT)
    assert not (tmp_path / "relhome").exists(), "判据必须在 mkdir 之前拦住"


def test_export_creates_locked_down_tree(env):
    res = usecases.moments_export(account=ACCT)
    p = pathlib.Path(res.out_dir)
    assert p.stat().st_mode & 0o777 == 0o700
    assert (p / "index.html").stat().st_mode & 0o777 == 0o600


def test_export_with_media_downloads_and_links(env):
    plain = b"\xff\xd8\xff" + bytes(64)      # `fetch` 的契约是**已解密明文**，直接喂它

    def fake_fetch(m, **kw):
        return plain

    res = usecases.moments_export(account=ACCT, with_media=True, fetch=fake_fetch)
    assert res.report.media_downloaded == 1
    doc = (pathlib.Path(res.out_dir) / "index.html").read_text(encoding="utf-8")
    assert 'src="media/image/' in doc
    # 落盘的是明文（不再二次解密）
    saved = next(pathlib.Path(res.out_dir).glob("media/image/*/*"))
    assert saved.read_bytes() == plain
    assert saved.stat().st_mode & 0o777 == 0o600


def test_export_media_failure_is_isolated(env):
    """单张失败不中断整批。"""
    def bad_fetch(m, **kw):
        raise sns_media.SnsMediaError("下载失败（重试 3 次）：TimeoutError")

    res = usecases.moments_export(account=ACCT, with_media=True, fetch=bad_fetch)
    assert res.report.media_failed == 1
    assert res.report.posts == 1                       # 动态仍然导出
    assert (pathlib.Path(res.out_dir) / "index.html").exists()


def test_export_limit_keeps_the_newest(env):
    """`--limit` 留的必须是**最新**的那些：夹具第二条（tid 更大）的 createTime **更晚**，
    于是扫描序与时间序**故意相反**——修复前按扫描序截断，留下的是**最早**那条，产物
    （按时间倒序渲染）于是只显示时间线最旧的一头，window 也照错报。

    变异：删掉 `iter_posts` 的 `out.sort(...)` → 留下 P1（最旧）→ 红。
    """
    _add_post(SECOND_XML)                                  # tid 更大、时间更晚
    res = usecases.moments_export(account=ACCT, limit=1)
    assert res.report.truncated is True
    assert res.report.window == (1735776000, 1735776000)   # 留下的是**新**那条（P2）
    doc = (pathlib.Path(res.out_dir) / "index.html").read_text(encoding="utf-8")
    assert "第二条" in doc and "正文" not in doc            # 产物里是新的那条，不是旧的


def test_export_parse_failures_are_visible(env):
    """最坏情况：微信改了 XML 形状 → 每条都解析不了。**不许**是一次静默的空成功：
    计数得进报告、降级说明得有一条、产物页首要说明白。

    变异：删掉 usecases 里 `if report.failed:` 那段 → degraded 为空 → 红。
    """
    con = sqlite3.connect(paths.db_storage(ACCT) / "sns" / "sns.db")
    con.execute("DELETE FROM SnsTimeLine")                 # 库里**一条可解析的都没有**
    con.commit()
    con.close()
    for tid, xml in ((1, "<SnsDataItem><TimelineObject>"),  # 畸形
                     (3, "<Other><Nothing/></Other>"),       # 不是朋友圈结构
                     (4, "<FutureShape><Whatever/></FutureShape>")):   # 将来换了形状
        _add_post(xml, tid=tid)
    res = usecases.moments_export(account=ACCT, limit=1)   # limit 也拦不住解析失败
    assert res.report.posts == 0
    assert res.report.failed == 3
    assert any("解析失败" in d for d in res.report.degraded)
    doc = (pathlib.Path(res.out_dir) / "index.html").read_text(encoding="utf-8")
    assert "解析失败 3 条" in doc


def test_export_limit_sets_truncated(env):
    """截断标志必须**如实**置位：恒真或恒假都等于没测，故自建两组的对照。

    共享夹具只插一条（另有测试断言 posts == 1 / window），这里直接补插第二条
    （tid 不同、createTime 更晚），再看 limit=1 与不限量两种口径。
    """
    _add_post(SECOND_XML)

    capped = usecases.moments_export(account=ACCT, limit=1)
    assert capped.report.truncated is True
    assert capped.report.posts == 1

    full = usecases.moments_export(account=ACCT)       # 对照组：不限量 → 不截断
    assert full.report.truncated is False
    assert full.report.posts == 2


def test_export_reports_remaining_after_limit(env):
    """截断要带条数：「被 limit 截断」不带数量，用户无从判断差多少。

    夹具**故意多于两条**：3 条 + limit=1 → 丢 2 条。只有这个数字能把 `+= 1` 与 `= 1`
    分开——「丢 1 条」的夹具对报恒定 1 的实现同样是绿的，而那正是本计划要消灭的那类
    会撒谎的数字（media 侧 `test_export_media_reports_remaining_after_cap` 同此口径）。

    变异：把 `report.remaining += 1` 删掉 → 0 → 红；改成 `report.remaining = 1`
    → 丢的 2 条只报 1 条 → 红。
    """
    _add_post(SECOND_XML)
    _add_post(NO_TOKEN_XML, tid=3)               # 第三条只为凑出「丢 2 条」
    capped = usecases.moments_export(account=ACCT, limit=1)
    assert capped.report.truncated is True
    assert capped.report.remaining == 2          # 夹具共 3 条，留下 1 条


def test_export_capped_posts_are_counted_not_fetched(env):
    """撞上限后只计数、**不得**再调 `_resolve_media`：那个调用在 with_media=True 时会联网。

    变异：把 cap 分支里的 `continue` 挪到 `_resolve_media` 之后 → 被丢下的那条也下载
    （calls 由 1 变 2）→ 红。注意本条在改动**之前**就绿（旧代码是 `break`，同样不抓）：
    它守的是 `continue` 的位置，不是「本次改动做没做」。
    """
    _add_post(_md5_xml("00112233445566778899aabbccddeeff"))   # 更新的那条，媒体可下载
    calls = []

    def fetch(m, **kw):
        calls.append(m)
        return _jpeg()

    res = usecases.moments_export(account=ACCT, with_media=True, fetch=fetch, limit=1)
    assert res.report.truncated is True
    assert res.report.remaining == 1
    assert len(calls) == 1                        # 只有被留下的那条抓了媒体
    assert res.report.media_downloaded == 1


def test_moments_export_limit_is_not_capped(env, monkeypatch):
    """`--limit 5000` 不被压成 2000（硬顶已放开）。

    变异：把 `clamp_limit(limit)` 改回 `clamp_limit(limit, (1, 2000))` → 捕获到的返回值
    为 2000 → 红。断言的是 `clamp_limit` 的**实参**与**返回值**两样，缺一样都拦不住。
    """
    caps = []
    real = usecases.clamp_limit

    def spy(n, bounds=usecases.LIMIT_UNBOUNDED):
        got = real(n, bounds)
        caps.append((n, bounds, got))
        return got

    monkeypatch.setattr(usecases, "clamp_limit", spy)
    usecases.moments_export(account=ACCT, limit=5000)
    assert caps == [(5000, usecases.LIMIT_UNBOUNDED, 5000)]


def test_export_rejects_bad_month(env):
    with pytest.raises(errors.SearchInputError):
        usecases.moments_export(account=ACCT, months=["2025-13"])


def test_export_window_is_reported(env):
    res = usecases.moments_export(account=ACCT)
    assert res.report.window == (1735689600, 1735689600)


# ---------- 媒体名消毒：`url@md5` 是不可信输入，绝不能当路径组件 ----------

def _assert_confined(res, home: pathlib.Path) -> None:
    """产物全在 out_root 内、文件名是 32 位 hex（而非穿越串）。"""
    out_root = pathlib.Path(res.out_dir)
    # 先查越界（这条排在最前：越界时失败信息直接点出那个跑出去的文件）
    assert [p for p in _files_outside(home, out_root) if p.name != "passphrase.json"] == []
    saved = sorted(out_root.rglob("*.jpg"))
    assert len(saved) == 2                              # 夹具那条 + 恶意 md5 那条
    for p in saved:
        assert p.resolve().is_relative_to(out_root.resolve())
        assert re.fullmatch(r"[0-9a-f]{32}\.jpg", p.name), p.name


def test_export_relative_md5_traversal_is_confined(env):
    """`md5="../../../../escape"`：修复前实测写出 `<home>/escape.jpg`（产物根之外），
    且计数是 downloaded=1 / failed=0——静默越界，没有任何信号。"""
    home = env
    _add_post(_md5_xml("../../../../escape"))
    res = usecases.moments_export(account=ACCT, with_media=True,
                                  fetch=lambda m, **k: _jpeg())
    assert not (home / "escape.jpg").exists()           # 越界落点必须为空
    _assert_confined(res, home)


def test_export_absolute_md5_traversal_is_confined(env, tmp_path):
    """`md5="<绝对路径>/escaped"`：pathlib 的 `d / "/x"` 丢弃左操作数，直接落到绝对路径。
    修复前实测既写出 `<tmp>/escaped.jpg`，又让 `relative_to` 抛 ValueError 穿透 core 层。"""
    home = env
    _add_post(_md5_xml(f"{tmp_path}/escaped"))
    res = usecases.moments_export(account=ACCT, with_media=True,
                                  fetch=lambda m, **k: _jpeg())
    assert not (tmp_path / "escaped.jpg").exists()
    _assert_confined(res, home)


# ---------- F25：畸形 createTime 不得让整次导出崩掉（离线也崩） ----------

BAD_TS_XML = """<SnsDataItem><TimelineObject>
 <id>P8</id><username>wxid_friend</username><createTime>99999999999999999999</createTime>
 <contentDesc>畸形时间戳</contentDesc>
</TimelineObject><LocalExtraInfo><nickname>老王</nickname></LocalExtraInfo></SnsDataItem>"""


def test_export_survives_malformed_create_time_offline(env, monkeypatch):
    """畸形 `createTime` 曾让**离线**导出整次抛裸 OverflowError 逃出 core 层——
    不需要开 --with-media。这里顺带把 fetch 打成必炸，确认修复后仍是零网络。"""
    def boom(*a, **k):
        raise AssertionError("离线导出不应发起任何网络请求")
    monkeypatch.setattr(sns_media, "fetch", boom)
    _add_post(BAD_TS_XML)

    res = usecases.moments_export(account=ACCT)
    assert res.report.posts == 2                        # 畸形那条仍在（create_time 归 0）
    assert res.report.media_not_downloaded == 1
    assert (pathlib.Path(res.out_dir) / "index.html").exists()


# ---------- 嗅探与落盘失败：`fetch` 的文档把「自行嗅探校验」移交给了本层 ----------

def test_export_garbage_media_is_counted_and_never_written(env):
    """密钥错时 `decrypt_stream` 静默产出乱码而不抛错（见 sns_media.fetch 文档），
    故本层必须嗅探。乱码**绝不落盘**——把 _sniff_ext 写死成 "jpg" 会让这条红。"""
    res = usecases.moments_export(account=ACCT, with_media=True,
                                  fetch=lambda m, **k: b"\x00" * 64)
    assert res.report.media_failed == 1
    assert res.report.media_downloaded == 0
    assert res.report.degraded                          # 有一条降级说明
    assert res.report.media_garbage == 1
    # 文件系统级证据（本测试名字里的 never_written 指的就是这两条）：
    # 只断计数的话，「写了盘但不计 media_downloaded」的变异会漏网。
    out_root = pathlib.Path(res.out_dir)
    assert not (out_root / "media").exists()            # 连目录都没建
    assert [f for f in out_root.rglob("*")
            if f.is_file() and f.name != "index.html"] == []


def test_export_repeated_media_failures_share_one_degraded_line(env):
    """同类降级只写**一条**、且带条数：50 条乱码此前会写 50 行一模一样的话（stderr 刷屏、
    信封里 50 个重复项）。仓库既有先例是能力级问题用 once-flag（media.py 的
    `wxgf_warned` / 语音分片那两条），TOOLS.md/USAGE.md 也已经写的是「写入一项」。

    变异：把 `report.media_garbage += 1` 换回就地 `degraded.append(...)` → 3 条 → 红。
    """
    for tid in (2, 3):
        _add_post(_md5_xml("00112233445566778899aabbccddeeff"
                           if tid == 2 else "ffeeddccbbaa99887766554433221100"), tid=tid)
    res = usecases.moments_export(account=ACCT, with_media=True,
                                  fetch=lambda m, **k: b"\x00" * 64)
    assert res.report.media_failed == 3
    assert len(res.report.degraded) == 1
    assert "3 条" in res.report.degraded[0]


def test_export_unparseable_window_is_not_1970(env):
    """畸形 `createTime` 归 0（`_ts` 的消毒），但产物不许把它印成 1970 年——
    那是凭空造出来的日期（CLI 侧另有一条用例盯 stdout）。"""
    _add_post(BAD_TS_XML, tid=2)
    res = usecases.moments_export(account=ACCT)
    assert res.report.window == (0, 1735689600)          # 0 是「没解析出来」
    doc = (pathlib.Path(res.out_dir) / "index.html").read_text(encoding="utf-8")
    assert "1970" not in doc
    # 注：原先此处还有两条「产物树里没有媒体文件」的断言。它们恒真——本用例离线
    # （不带 with_media），根本不会抓取媒体。已搬回 test_export_garbage_media_…，
    # 那里 fetch 真的返回了非图片字节，断言才有意义。


def test_export_save_failure_is_counted_and_isolated(env, monkeypatch):
    """落盘失败（磁盘满/权限）只计数，不中断整批（media._save 返回 None 的路径）。"""
    monkeypatch.setattr(media, "save_artifact", lambda *a, **k: None)
    res = usecases.moments_export(account=ACCT, with_media=True,
                                  fetch=lambda m, **k: _jpeg())
    assert res.report.media_failed == 1
    assert res.report.media_downloaded == 0
    assert res.report.posts == 1                        # 动态仍然导出
    assert (pathlib.Path(res.out_dir) / "index.html").exists()


def test_export_undownloadable_media_is_not_fetched(env):
    """`with_media=True` 不等于「见媒体就下载」：**白名单外**的媒体不去取，
    不该白发一次必然失败的请求（且要如实计入 not_downloaded）。

    `calls` 里会有**一条基线**动态的请求（`env` 夹具顶部的 `XML`，宿主
    `shmmsns.qpic.cn` 在白名单内），所以按 URL 过滤，**不能**断言 `calls == []`。
    """
    _add_post(NO_TOKEN_XML)
    calls = []
    res = usecases.moments_export(account=ACCT, with_media=True,
                                  fetch=lambda m, **kw: (calls.append(m), _jpeg())[1])
    assert res.report.media_not_downloaded == 1
    assert res.report.media_downloaded == 1                  # 基线那条
    assert [m for m in calls if "y.music.163.com" in (m.url or "")] == []


def test_export_downloads_image_without_token_when_host_allowed(env):
    """**本期推翻的旧语义**：无 `token` ≠ 不可下载。

    `type=7` 的 61 条就是「无 token 但在微信 CDN 上、公开可下」。旧测试的注释
    「无 token 的媒体联网时也不该白发请求」正是本期要改掉的那条假设。
    计数同样含基线那条。
    """
    _add_post(NO_TOKEN_XML.replace("https://y.music.163.com/x.jpg",
                                   "http://szmmsns.qpic.cn/mmsns/XYZ/0"))
    calls = []
    res = usecases.moments_export(account=ACCT, with_media=True,
                                  fetch=lambda m, **kw: (calls.append(m), _jpeg())[1])
    assert len(calls) == 2                       # 基线 + 新增那条
    assert res.report.media_downloaded == 2      # 同上


# spec §8.3 夹具 ②（副本；逐字与 tests/test_moments.py 的同名夹具一致）
# 封面宿主特意取可达的 shzjwxsns.video.qq.com——取占多数的 vweixinthumb.tc.qq.com
# 会让 build_url 返回 None，下面两条封面断言就永远走不到分支（假护栏）。
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

# 上面那条的变体：**封面宿主不在白名单**（真机 158/181 的视频封面就在
# `vweixinthumb.tc.qq.com`），本体仍在白名单内（`shzjwxsns.video.qq.com`）。
# 由 `VIDEO15_XML` 派生而不是就地复制一份：`<thumb>` 的 URL 一旦改动，这里的
# replace 会**失配**——不报错，但 `test_video_cover_outside_allowlist_is_skipped_silently`
# 立刻变红（那时封面会被当成白名单内的封面下载、落出一张 `*_thumb.jpg`），不会静默绿。
VIDEO15_THUMB_OFFLIST_XML = VIDEO15_XML.replace(
    "http://shzjwxsns.video.qq.com/150/20250/snsvideodownload",
    "http://vweixinthumb.tc.qq.com/150/20250/snsvideodownload")

# spec §8.3 夹具 ④（副本）
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


def _mp4_plain() -> bytes:
    return b"\x00\x00\x00\x20ftypisom" + b"\x00" * 4088


def _isaac_wrap(plain: bytes, seed: str) -> bytes:
    """合成密文：用项目的 `WxIsaac64` 加密（流密码自反）。不碰任何真机材料。"""
    ks = sns_media.WxIsaac64(int(seed)).keystream(len(plain))
    return bytes(a ^ b for a, b in zip(plain, ks))


def _url_opener(body_for):
    """按 URL 分派响应体的假 opener——真实的 `fetch` 只调 `op.open(req)`。"""
    class _Resp:
        # `__enter__`/`__exit__` **必须定义在类上**：Python 的 `with` 只在**类型**上查
        # 这两个协议方法，赋在实例上无效——会抛 TypeError，被 `fetch` 的
        # `except Exception` 吞掉、重试 3 次、最终抛 `SnsMediaError`，
        # 症状是 `media_downloaded == 0` 且 `media_failed == 2`（已实测踩过）。
        def __init__(self, body: bytes):
            self._body, self.headers = body, {}
        def read(self) -> bytes:
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _Op:
        def open(self, req, timeout=None):
            return _Resp(body_for(req.full_url))
    return _Op()


def test_video_cover_is_downloaded_and_decrypted(env, monkeypatch):
    """**真实（非注入）的解密链路**——spec §8.1 #8 的硬要求。

    spec 原话：「首稿的 #8 用**注入的假 fetch**，它绕过真解密——于是『封面被误解密』
    这个 bug **单测全绿**。守着一个函数的输入输出，不等于守着它的语义。」

    所以这里**不传** `fetch=`，改把 `sns_media._default_opener` 换成假 opener：
    真实的 `sns_media.fetch` 与 `decrypt_stream` 全程运行，**只有网络是假的**。
    断言「落盘字节 == 明文原文」——把 `key`/`encrypted` 传丢、或把解密判据改坏，都会红。

    封面与本体共用**同一个 `enc@key`**（真机 szzjwxsns 实测 `d1a867dd` → `ffd8ffe0`）。

    **计数断言要把 `env` 的基线动态算进去**：`env` 夹具自己插了一条（见顶部 `XML`），
    它带一个可下载的 `AUTH_SNS` 媒体（`token="TK"`、宿主 `shmmsns.qpic.cn` 在白名单内），
    且无 `<enc>`、响应也无 `x-enc` 头 → **不解密**，故它的响应体给明文。
    """
    _add_post(VIDEO15_XML)
    plain_v, plain_c = _mp4_plain(), _jpeg()
    seed = "20260918"                       # 与 VIDEO15_XML 的 <enc key="…"> 一致

    def body_for(url: str) -> bytes:
        if "150/20250" in url:              # 小视频封面：密文
            return _isaac_wrap(plain_c, seed)
        if "shzjwxsns.video.qq.com" in url:  # 小视频本体：密文
            return _isaac_wrap(plain_v, seed)
        return plain_c                      # env 基线的 AUTH_SNS 图：明文

    monkeypatch.setattr(sns_media, "_default_opener", lambda: _url_opener(body_for))

    res = usecases.moments_export(account=ACCT, with_media=True)
    assert res.report.media_downloaded == 2     # 基线的图 + 小视频本体
    assert res.report.media_failed == 0
    thumbs = list(pathlib.Path(res.out_dir).rglob("*_thumb.jpg"))
    assert len(thumbs) == 1
    assert thumbs[0].read_bytes() == plain_c    # **解出来的等于原文**（真解密）
    # 路径是 {out}/media/{kind}/{月}/{stem}.{ext}（media._out_path）——「与本体同目录」
    # 要退**两级**：parent 是月份目录，parent.parent 才是 kind 目录。
    assert thumbs[0].parent.parent.name == "video"


def test_cover_failure_counts_failed_but_not_losing_the_body(env):
    """本体成功、封面失败 → `media_failed +1` **且**本体仍计 downloaded。

    封面失败被静默吞掉正是「产物有缺陷而报表健康」那一类。
    """
    _add_post(VIDEO15_XML)
    def fetch(m, **kw):
        if "shzjwxsns.video.qq.com" not in m.url:
            return _jpeg()                       # env 基线动态
        if "150/20250" in m.url:
            raise sns_media.SnsMediaError("封面挂了")
        return _mp4_plain()                      # 本体：明文（同 _jpeg 的契约）
    res = usecases.moments_export(account=ACCT, with_media=True, fetch=fetch)
    assert res.report.media_downloaded == 2     # 基线 + 本体（本体仍算成功）
    assert res.report.media_failed == 1         # 封面失败不静默
    assert not list(pathlib.Path(res.out_dir).rglob("*_thumb.jpg"))


def test_video_cover_outside_allowlist_is_skipped_silently(env):
    """封面宿主不在白名单 → **静默跳过**：本体照常下载、封面不落盘，
    `media_failed` 与 `media_not_downloaded` **都不因该封面增加**。

    守的是 `_resolve_media` 里那行 `if sns_media.build_url(tcover) is not None:`。
    它是 A4 恒等式（`media_downloaded + media_failed + media_not_downloaded` 对上
    `p.media` 的成员数）唯一的支点：封面若被送进任何一条计数分支，账目就对不上；
    真机 158/181 的视频封面宿主是 `vweixinthumb.tc.qq.com`（证书名不匹配、不在白名单），
    所以这条不是理论情形。

    变异实测：把这行守卫改成 `if True:`（或删掉 if，直接无条件试取）→ 封面被 fetch
    并落盘 → 本条**两条**断言同时红（多一次请求 + 多一张 `*_thumb.jpg`）。
    """
    _add_post(VIDEO15_THUMB_OFFLIST_XML)
    calls = []
    res = usecases.moments_export(account=ACCT, with_media=True,
                                  fetch=lambda m, **kw: (calls.append(m), _jpeg())[1])
    assert res.report.media_downloaded == 2          # 基线的图 + 小视频本体
    assert res.report.media_failed == 0
    assert res.report.media_not_downloaded == 0      # 跳过≠「不去取」，封面不该进这个桶
    assert [m for m in calls if "vweixinthumb" in (m.url or "")] == []   # 一个请求都不发
    assert not list(pathlib.Path(res.out_dir).rglob("*_thumb.jpg"))


def test_media_outside_allowlist_is_not_downloaded_not_failed(env):
    """白名单外的媒体计 `not_downloaded`——「不去取」，不是「取失败」。"""
    _add_post(LINK_XML.replace("http://shmmsns.qpic.cn/mmsns/D/0",
                               "https://y.music.163.com/x.jpg"))
    calls = []
    res = usecases.moments_export(account=ACCT, with_media=True,
                                  fetch=lambda m, **kw: (calls.append(m), _jpeg())[1])
    assert res.report.media_failed == 0
    assert [m for m in calls if "y.music.163.com" in m.url] == []   # 站外那条一个请求都不发
    assert res.report.media_not_downloaded == 1


# ---------- §5.5：小视频封面的三个计数 ----------

def test_cover_with_non_jpeg_magic_is_failed_but_not_garbage(env):
    """封面解出来不是 JPEG（这里是 PNG）：计 `media_failed`，但**不**计 `media_garbage`
    ——后者专指「嗅探不出格式」，两者对用户的含义不同。"""
    _add_post(VIDEO15_XML)

    def fetch(m, **kw):
        if "150/20250" in m.url:
            return b"\x89PNG\r\n\x1a\n" + b"x" * 8
        return _jpeg()

    res = usecases.moments_export(account=ACCT, with_media=True, fetch=fetch)
    assert res.report.media_failed == 1, res.report
    assert res.report.media_garbage == 0, res.report
    assert not list(pathlib.Path(res.out_dir).rglob("*_thumb.jpg"))


def test_cover_garbage_counts_both_failed_and_garbage(env):
    """封面是乱码（`_sniff_ext` 返回 None）：failed 与 garbage 各 +1。"""
    _add_post(VIDEO15_XML)

    def fetch(m, **kw):
        if "150/20250" in m.url:
            return b"\x00" * 64
        return _jpeg()

    res = usecases.moments_export(account=ACCT, with_media=True, fetch=fetch)
    assert res.report.media_failed == 1 and res.report.media_garbage == 1, res.report
    assert not list(pathlib.Path(res.out_dir).rglob("*_thumb.jpg"))


def test_cover_save_failure_counts_failed(env, monkeypatch):
    """封面格式对、但落盘失败：计 `media_failed`，本体不受影响。"""
    _add_post(VIDEO15_XML)
    real = media.save_artifact
    monkeypatch.setattr(
        media, "save_artifact",
        lambda out, cm, kind, mon, stem, ext, data: (
            None if stem.endswith("_thumb") else real(out, cm, kind, mon, stem, ext, data)))
    res = usecases.moments_export(account=ACCT, with_media=True,
                                  fetch=lambda m, **k: _jpeg())
    assert res.report.media_failed == 1, res.report
    assert res.report.media_downloaded == 2      # 基线 + 本体
    assert not list(pathlib.Path(res.out_dir).rglob("*_thumb.jpg"))
