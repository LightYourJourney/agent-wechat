"""CLI 入口：参数解析 + 输出 + 降级走 stderr。"""
import contextlib
import json
import sqlite3

import pytest

from agent_wechat import cli, keys, paths, usecases

ACCT = "wxid_testacct01_ab12"
XML = """<SnsDataItem><TimelineObject><id>P1</id><username>u</username>
<createTime>1735689600</createTime><contentDesc>正文</contentDesc>
<ContentObject><type>2</type><mediaList/></ContentObject></TimelineObject>
<LocalExtraInfo><nickname>老王</nickname></LocalExtraInfo></SnsDataItem>"""

# 畸形 createTime → `moments._ts` 归 0（正确），但 CLI 不许把它印成 1970-01。
BAD_TS_XML = XML.replace("<createTime>1735689600</createTime>",
                         "<createTime>99999999999999999999</createTime>")


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
    # passphrase.json（与 test_moments_usecases.py 同一口径）。
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


def test_moments_export_parses_and_reports(env, capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(["moments", "export", "--account", ACCT])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    # 产物路径必须打出来（用户照着打开），且默认落在 $AGENT_WECHAT_HOME/moments。
    assert f"产物：{env}/moments/index.html" in out
    assert "已导出 1 条动态" in out


def test_moments_export_rejects_bad_month(env, capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(["moments", "export", "--account", ACCT, "--months", "2025-13"])
    assert ei.value.code != 0
    assert "YYYY-MM" in capsys.readouterr().err


def test_missing_timeline_table_is_a_one_line_error_not_a_traceback(env, capsys):
    """旧版微信没有 `SnsTimeLine`：修复前裸 `sqlite3.OperationalError` 穿透 `main` 的
    `except errors.AgentWeChatError`，命令行上是一个 Python traceback。现在是一行中文
    + 退出码 1，stdout 干净（模块 docstring 的流纪律）。

    变异：删掉 `iter_posts` 里那圈 `except sqlite3.OperationalError` → 抛的是
    OperationalError、`main` 不认 → 本用例拿不到 SystemExit → 红。
    """
    con = sqlite3.connect(paths.db_storage(ACCT) / "sns" / "sns.db")
    con.execute("DROP TABLE SnsTimeLine")
    con.commit()
    con.close()
    with pytest.raises(SystemExit) as ei:
        cli.main(["moments", "export", "--account", ACCT])
    assert ei.value.code == 1
    cap = capsys.readouterr()
    assert cap.out == ""                       # 不许有半成品输出
    assert "结构与预期不符" in cap.err and "SnsTimeLine" in cap.err
    assert "Traceback" not in cap.err


def _sole_row(xml: str) -> None:
    """把合成库换成只有这一条动态（夹具默认已插一条）。"""
    con = sqlite3.connect(paths.db_storage(ACCT) / "sns" / "sns.db")
    con.execute("DELETE FROM SnsTimeLine")
    con.execute("INSERT INTO SnsTimeLine(tid, user_name, content) VALUES (1,'u',?)", (xml,))
    con.commit()
    con.close()


def test_unparseable_create_time_is_not_printed_as_1970(env, capsys):
    """`createTime` 畸形时 `_ts` 归 0，CLI 此前印「覆盖：1970-01 ~ 1970-01」——一个凭空
    造出来的日期。0 的含义是「没解析出来」，故空出来而不是编一个。

    变异：把 `lo`/`hi` 换回无条件的 `media.month_of(...)` → 印出 1970-01 → 红。
    """
    _sole_row(BAD_TS_XML)
    with pytest.raises(SystemExit) as ei:
        cli.main(["moments", "export", "--account", ACCT])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "1970-01" not in out
    assert "覆盖：? ~ ?" in out


def test_with_media_flag_defaults_off(env, monkeypatch):
    """默认关：CLI 解析出的 with_media 必须是 False。"""
    seen = {}
    real = usecases.moments_export

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(usecases, "moments_export", spy)
    with pytest.raises(SystemExit):
        cli.main(["moments", "export", "--account", ACCT])
    assert seen.get("with_media") is False
