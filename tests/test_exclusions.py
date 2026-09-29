"""exclusions 模块的单元测试。**不得指向真实 ~/.agent-wechat**（红线同 test_purge.py）。"""
import pathlib

import pytest

from agent_wechat import exclusions, paths


@pytest.fixture(autouse=True)
def _never_touch_real_home(monkeypatch, tmp_path):
    """红线（同 `tests/test_purge.py` 的 autouse fixture）：**每一个**测试都不得指向真实数据。

    `store.DB_PATH` 必须单独 patch：它是 `paths.HOME_DIR / "index.db"`，在**导入时**就绑定了，
    只 patch `paths.HOME_DIR` 对它无效——而 `store.open()` 走的就是它。本文件里
    `run_export` / `store.open()` 这类调用会因此落到真实的 `~/.agent-wechat/index.db` 上。
    """
    from agent_wechat import store
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")
    # 断言查的是**结果**，不是「patch 了哪些常量」——漏 patch 的那一刻就要当场红
    assert store.DB_PATH.is_relative_to(tmp_path)
    assert exclusions.path().is_relative_to(tmp_path)
    yield


def test_path_is_call_time_and_follows_home_dir():
    """重定向 HOME_DIR 之后路径必须跟着走——import 时绑定值的常量会让红线 fixture 静默失效。"""
    before = exclusions.path()
    assert before.parent == paths.HOME_DIR
    assert exclusions.path().name == "exclusions.db"


def test_add_is_idempotent_and_count_matches(tmp_path):
    import sqlite3
    con = sqlite3.connect(str(tmp_path / "x.db"))
    con.execute("ATTACH DATABASE ? AS exclusions", (str(tmp_path / "ex.db"),))
    con.executescript(exclusions.SCHEMA)
    rows = [("a|m1|message_0.db|1", "a", "m1"), ("a|m1|message_0.db|2", "a", "m1")]
    assert exclusions.add(con, rows) == 2
    assert exclusions.add(con, rows) == 2, "重复写入必须幂等（INSERT OR IGNORE）"
    assert exclusions.count(con) == 2
    assert exclusions.by_chat(con) == [("m1", 2)]


def test_derived_columns_match_msg_key(tmp_path):
    """account / chat_md5 两列是 msg_key 的派生副本，存在的唯一凭据是它们不失真（spec §4）。"""
    import sqlite3
    con = sqlite3.connect(str(tmp_path / "x.db"))
    con.execute("ATTACH DATABASE ? AS exclusions", (str(tmp_path / "ex.db"),))
    con.executescript(exclusions.SCHEMA)
    exclusions.add(con, [("acct|deadbeef|message_3.db|77", "acct", "deadbeef")])
    key, acct, md5 = con.execute(
        "SELECT msg_key, account, chat_md5 FROM exclusions.excluded").fetchone()
    parts = key.split("|")
    assert acct == parts[0] and md5 == parts[1]


def test_remove_chat_is_exact_match_not_like(tmp_path):
    """删得准，靠的是等值而不是 LIKE——`_` 与 `%` 都是 LIKE 的通配符，而这里用等值更直接。"""
    import sqlite3
    con = sqlite3.connect(str(tmp_path / "x.db"))
    con.execute("ATTACH DATABASE ? AS exclusions", (str(tmp_path / "ex.db"),))
    con.executescript(exclusions.SCHEMA)
    exclusions.add(con, [("a_c|m1|message_0.db|1", "a_c", "m1"),
                         ("abc|m1|message_0.db|1", "abc", "m1")])
    assert exclusions.remove_chat(con, "a_c", "m1") == 1
    left = [r[0] for r in con.execute("SELECT account FROM exclusions.excluded")]
    assert left == ["abc"], f"`a_c` 的前缀误伤了别的账号：{left}"


def test_connect_attaches_and_creates_the_table(tmp_path):
    """`store.connect()` 之后，排除表必须在同一个连接上可见、且已建好。"""
    from agent_wechat import store
    con = store.connect(tmp_path / "index.db")
    try:
        assert con.execute(
            "SELECT count(*) FROM exclusions.excluded").fetchone()[0] == 0
        assert (tmp_path / "exclusions.db").exists(), "ATTACH 应当建出同目录的排除清单"
        assert con.execute("SELECT count(*) FROM main.sqlite_master "
                           "WHERE name IN ('excluded','idx_excluded_chat')").fetchone()[0] == 0
    finally:
        con.close()
