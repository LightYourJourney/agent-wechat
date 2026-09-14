"""display_names 的建表、名册读写、join 与 EXPLAIN。

本文件的夹具是**轻量**的：只造一个临时索引库，名册用手填，不碰加密库。
需要真实 `contact.db` 的用例（`reader.roster()`、刷新失败降级）在
`tests/test_reader_export_pipeline.py`——那里有现成的 `enc_db` / `env` 夹具。
"""
import pytest

from agent_wechat import paths, store


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """一个建好表的临时索引库路径。

    隔离 `HOME_DIR` 是硬要求：`store.connect` 会调 `paths.ensure_home_dirs()`，
    那会对真实 `~/.agent-wechat` 做 mkdir 并调一次 `tmutil addexclusion`。
    返回路径而不是连接——测试自己用 `store.open()`，那会在退出时提交，
    后续 `search.search` 开的新连接才看得到数据。
    """
    home = tmp_path / "home"
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")
    with store.open():
        pass
    return store.DB_PATH


def test_schema_declares_the_two_tables(db):
    with store.open() as con:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"display_names", "roster_meta"} <= names


def test_roster_is_stale_when_never_refreshed(db):
    with store.open() as con:
        assert store.roster_is_stale(con, "acct", ttl=900) is True


def test_roster_is_stale_respects_ttl(db):
    with store.open() as con:
        store.replace_roster(con, "acct", [("888@chatroom", "wxid_a", "张三")])
        assert store.roster_is_stale(con, "acct", ttl=900) is False
        assert store.roster_is_stale(con, "acct", ttl=0) is True     # ttl=0：永远过期


def test_replace_roster_replaces_wholesale_and_isolates_accounts(db):
    with store.open() as con:
        store.replace_roster(con, "a1", [("888@chatroom", "wxid_x", "旧")])
        store.replace_roster(con, "a2", [("888@chatroom", "wxid_x", "别的账号")])
        store.replace_roster(con, "a1", [("888@chatroom", "wxid_y", "新")])
        got = {(a, c, k): d for a, c, k, d in con.execute(
            "SELECT account, chat_id, member_key, display_name FROM display_names")}
    assert got[("a1", "888@chatroom", "wxid_y")] == "新"
    assert ("a1", "888@chatroom", "wxid_x") not in got          # 旧的被整体换掉
    assert got[("a2", "888@chatroom", "wxid_x")] == "别的账号"    # 别的账号不受影响
