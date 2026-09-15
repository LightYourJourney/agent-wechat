"""MCP 信封形状与「默认不联网」契约。"""
import contextlib
import json
import sqlite3

import pytest

from agent_wechat import keys, mcp_server, paths, sns_media, usecases

ACCT = "wxid_testacct01_ab12"
# 媒体**带 url + token + md5**：这样 with_media=True 时 fetch 才会真的被调用
# （mediaList 为空的话，「默认不联网」那条测试就是空的——把默认改成 True 也照样绿）。
# token 取一个正文里不会自然出现的字面量，供「信封不泄 token」断言盯住。
TOKEN = "TK_SECRET_9f3a1c"
XML = f"""<SnsDataItem><TimelineObject><id>P1</id><username>u</username>
<createTime>1735689600</createTime><contentDesc>正文</contentDesc>
<ContentObject><type>2</type><mediaList><media>
  <id>m1</id><type>2</type>
  <url type="1" md5="aabbccddeeff00112233445566778899" key="3" enc_idx="1"
       token="{TOKEN}">http://shmmsns.qpic.cn/sns/u/0</url>
</media></mediaList></ContentObject></TimelineObject>
<LocalExtraInfo><nickname>老王</nickname></LocalExtraInfo></SnsDataItem>"""


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
    # passphrase.json（与 test_moments_cli.py 同一口径）。
    (home / "passphrase.json").write_text(json.dumps(
        {"accounts": {"wxid_testacct01": {"passphrase_hex": "aa" * 32, "captured_at": 0}}}))
    monkeypatch.setattr(paths, "XWECHAT_FILES", wx)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    monkeypatch.setattr(dec_mod, "decrypted", _passthrough)
    monkeypatch.setattr("agent_wechat.moments.decrypt", dec_mod, raising=False)
    con = sqlite3.connect(d / "sns.db")
    con.execute("CREATE TABLE SnsTimeLine(tid INTEGER PRIMARY KEY DESC, user_name TEXT,"
                " content TEXT, pack_info_buf TEXT)")
    con.execute("INSERT INTO SnsTimeLine(tid, user_name, content) VALUES (1,'u',?)", (XML,))
    con.commit()
    con.close()
    return home


def test_envelope_shape(env):
    d = mcp_server.export_moments(account=ACCT)
    assert set(d) == {"out_dir", "counts", "window", "degraded", "truncated"}
    assert set(d["counts"]) == {"posts", "comments", "likes", "failed", "media_downloaded",
                                "media_not_downloaded", "media_failed"}
    assert d["counts"]["posts"] == 1
    assert d["counts"]["failed"] == 0                   # 解析失败与媒体失败各算各的
    assert d["window"] == {"earliest": 1735689600, "latest": 1735689600}
    assert d["degraded"] == [] and d["truncated"] is False


def test_mcp_defaults_to_no_network(env, monkeypatch):
    """MCP 侧也不许静默联网——与 CLI 同默认。"""
    def boom(*a, **k):
        raise AssertionError("MCP 默认不应发起网络请求")
    monkeypatch.setattr(sns_media, "fetch", boom)
    d = mcp_server.export_moments(account=ACCT)
    assert d["counts"]["media_downloaded"] == 0
    # 该媒体确实解析出来了、且**可下载**（fetch 会被调到）——只是默认路径不去碰它。
    # 少了这条，把 with_media 默认改成 True 时这条测试可能照样绿。
    assert d["counts"]["media_not_downloaded"] == 1


def test_envelope_never_leaks_token(env):
    d = mcp_server.export_moments(account=ACCT)
    assert "token" not in repr(d).lower()
    assert TOKEN not in repr(d)


def test_envelope_reports_parse_failures(env):
    """XML 形状变了（每条都解析不了）时，信封必须**说出**来：否则 `{"posts": 0,
    "degraded": []}` 与「这个账号没有朋友圈」逐字节相同。

    变异：删掉 mcp_server 里的 `"failed": r.failed` → KeyError → 红；删掉 usecases
    里那条降级 → `degraded` 为空 → 红。
    """
    con = sqlite3.connect(paths.db_storage(ACCT) / "sns" / "sns.db")
    con.execute("UPDATE SnsTimeLine SET content = '<FutureShape><Whatever/></FutureShape>'")
    con.commit()
    con.close()
    d = mcp_server.export_moments(account=ACCT)
    assert d["counts"]["posts"] == 0
    assert d["counts"]["failed"] == 1
    assert any("解析失败" in x for x in d["degraded"])


def test_need_confirm_passthrough(env, monkeypatch):
    """作者名歧义走 need_confirm 信封（与其它工具同形），不是 error、也不许猜。"""
    nc = usecases.NeedConfirm(candidates=[
        usecases.Candidate(username="wxid_a", display="老王", kind="direct")])
    monkeypatch.setattr(usecases, "moments_export", lambda *a, **k: nc)
    d = mcp_server.export_moments(contact="老王", account=ACCT)
    assert list(d) == ["need_confirm"]
    assert d["need_confirm"] == [{"username": "wxid_a", "display": "老王",
                                  "kind": "direct"}]
