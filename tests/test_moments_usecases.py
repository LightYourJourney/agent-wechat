"""编排层：合成 sns.db + 注入假 fetch，全程离线。"""
import contextlib
import json
import pathlib
import re
import sqlite3

import pytest

from agent_wechat import errors, keys, media, moments, paths, sns_media, usecases

ACCT = "wxid_testacct01_ab12"

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

# 无 token 的媒体（实况照片形态）：build_url → None，不可下载，联网时也不该白发请求。
NO_TOKEN_XML = """<SnsDataItem><TimelineObject>
 <id>P3</id><username>wxid_friend</username><createTime>1735776000</createTime>
 <contentDesc>实况照片</contentDesc>
 <ContentObject><type>2</type><mediaList><media>
   <id>m3</id><type>2</type>
   <url type="1" md5="00112233445566778899aabbccddeeff">http://shmmsns.qpic.cn/sns/u/3</url>
 </media></mediaList></ContentObject>
</TimelineObject><LocalExtraInfo><nickname>老王</nickname></LocalExtraInfo></SnsDataItem>"""


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


@contextlib.contextmanager
def _passthrough(db_path, passphrase, tmpdir=None):
    yield db_path


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from agent_wechat import decrypt as dec_mod
    wx, home = tmp_path / "wx", tmp_path / "home"
    d = wx / ACCT / "db_storage" / "sns"
    d.mkdir(parents=True)
    home.mkdir()
    # `iter_posts` 经 `keys.get_passphrase` 取钥；不隔离就会读到**真实机器的**
    # passphrase.json（与 test_moments.py 同一口径）。
    (home / "passphrase.json").write_text(json.dumps(
        {"accounts": {"wxid_testacct01": {"passphrase_hex": "aa" * 32, "captured_at": 0}}}))
    monkeypatch.setattr(paths, "XWECHAT_FILES", wx)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    monkeypatch.setattr(dec_mod, "decrypted", _passthrough)
    for mod in ("agent_wechat.moments", "agent_wechat.usecases"):
        monkeypatch.setattr(f"{mod}.decrypt", dec_mod, raising=False)
    con = sqlite3.connect(d / "sns.db")
    con.execute("CREATE TABLE SnsTimeLine(tid INTEGER PRIMARY KEY DESC, user_name TEXT,"
                " content TEXT, pack_info_buf TEXT)")
    con.execute("INSERT INTO SnsTimeLine(tid, user_name, content) VALUES (1,'u',?)", (XML,))
    con.commit()
    con.close()
    return home


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
    _add_post(SECOND_XML)                                  # tid 更大、时间更早
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
    out_root = pathlib.Path(res.out_dir)
    assert not (out_root / "media").exists()            # 连目录都没建
    assert [p for p in out_root.rglob("*")
            if p.is_file() and p.name != "index.html"] == []


def test_export_save_failure_is_counted_and_isolated(env, monkeypatch):
    """落盘失败（磁盘满/权限）只计数，不中断整批（media._save 返回 None 的路径）。"""
    monkeypatch.setattr(media, "_save", lambda *a, **k: None)
    res = usecases.moments_export(account=ACCT, with_media=True,
                                  fetch=lambda m, **k: _jpeg())
    assert res.report.media_failed == 1
    assert res.report.media_downloaded == 0
    assert res.report.posts == 1                        # 动态仍然导出
    assert (pathlib.Path(res.out_dir) / "index.html").exists()


def test_export_undownloadable_media_is_not_fetched(env):
    """`with_media=True` 不等于「见媒体就下载」：无 token 的媒体本地不可下载，
    不该白发一次必然失败的请求（且要如实计入 not_downloaded）。"""
    _add_post(NO_TOKEN_XML)
    calls = []

    def fetch(m, **kw):
        calls.append(m)
        return _jpeg()

    res = usecases.moments_export(account=ACCT, with_media=True, fetch=fetch)
    assert res.report.media_not_downloaded == 1         # 无 token 那条
    assert res.report.media_downloaded == 1             # 有 token 那条
    assert len(calls) == 1                              # 只发了一次请求
