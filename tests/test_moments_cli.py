"""CLI 入口：参数解析 + 输出 + 降级走 stderr。"""
import contextlib
import json
import sqlite3

import pytest

from agent_wechat import cli, keys, paths, usecases
from moments_fixtures import ACCT, _passthrough, env  # noqa: F401  (EA-117 共享夹具)

XML = """<SnsDataItem><TimelineObject><id>P1</id><username>u</username>
<createTime>1735689600</createTime><contentDesc>正文</contentDesc>
<ContentObject><type>2</type><mediaList/></ContentObject></TimelineObject>
<LocalExtraInfo><nickname>老王</nickname></LocalExtraInfo></SnsDataItem>"""

# 畸形 createTime → `moments._ts` 归 0（正确），但 CLI 不许把它印成 1970-01。
BAD_TS_XML = XML.replace("<createTime>1735689600</createTime>",
                         "<createTime>99999999999999999999</createTime>")
# 第二条动态（createTime 更晚）：只为 limit 造对照组，共享夹具仍只插一条。
SECOND_XML = XML.replace("<id>P1</id>", "<id>P2</id>").replace(
    "<createTime>1735689600</createTime>", "<createTime>1735776000</createTime>")


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


def test_truncated_run_tells_how_many_are_left(env, capsys):
    """触顶的 stderr 行必须带条数：只说「本次被截断」用户无从判断差多少。

    文案说「**更早**」是有依据的：`iter_posts` 按 createTime 降序产出、cap 留最新，
    于是被挡下的正是更早的那些。

    变异：把 `{r.remaining}` 写死成 0 → 红。
    """
    con = sqlite3.connect(paths.db_storage(ACCT) / "sns" / "sns.db")
    con.execute("INSERT INTO SnsTimeLine(tid, user_name, content) VALUES (2,'u',?)",
                (SECOND_XML,))
    con.commit()
    con.close()
    with pytest.raises(SystemExit) as ei:
        cli.main(["moments", "export", "--account", ACCT, "--limit", "1"])
    assert ei.value.code == 0
    err = capsys.readouterr().err
    assert "还有 1 条更早的动态没进来" in err


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
