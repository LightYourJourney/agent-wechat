"""编排层：合成 sns.db + 注入假 fetch，全程离线。"""
import contextlib
import json
import pathlib
import sqlite3

import pytest

from agent_wechat import errors, keys, moments, paths, sns_media, usecases

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


def test_export_limit_sets_truncated(env):
    """截断标志必须**如实**置位：恒真或恒假都等于没测，故自建两组的对照。

    共享夹具只插一条（另有测试断言 posts == 1 / window），这里直接补插第二条
    （tid 不同、createTime 更晚），再看 limit=1 与不限量两种口径。
    """
    con = sqlite3.connect(paths.db_storage(ACCT) / "sns" / "sns.db")
    con.execute("INSERT INTO SnsTimeLine(tid, user_name, content) VALUES (2,'u',?)",
                (SECOND_XML,))
    con.commit()
    con.close()

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
