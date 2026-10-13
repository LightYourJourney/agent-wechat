"""display_names 的建表、名册读写、join 与 EXPLAIN。

本文件的夹具是**轻量**的：只造一个临时索引库，名册用手填，不碰加密库。
需要真实 `contact.db` 的用例（`reader.roster()`、刷新失败降级）在
`tests/test_reader_export_pipeline.py`——那里有现成的 `enc_db` / `env` 夹具。
"""
import pytest

from agent_wechat import paths, search, store, vector
from helpers import mk_row


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


def _seed(con, *rows):
    store.upsert_batch(con, list(rows))


def test_fts_hit_uses_display_name(db):
    """FTS 那条路径（查询 >= 3 字走 trigram）。"""
    with store.open() as con:
        _seed(con, mk_row("acct", "888@chatroom", 1, "开会啦",
                          sender_id="wxid_a", sender_name="wxid_a"))
        store.replace_roster(con, "acct", [("888@chatroom", "wxid_a", "张三")])
    hits = search.search("开会啦", account="acct")["hits"]
    assert hits[0]["sender_name"] == "张三", f"FTS 路径没走 join：{hits[0]}"


def test_like_fallback_uses_display_name(db):
    """LIKE 兜底那条路径（查询 <= 2 字，trigram 匹配不到）。"""
    with store.open() as con:
        _seed(con, mk_row("acct", "888@chatroom", 1, "开会啦",
                          sender_id="wxid_a", sender_name="wxid_a"))
        store.replace_roster(con, "acct", [("888@chatroom", "wxid_a", "张三")])
    res = search.search("开会", account="acct")
    assert res["mode"] == "like", f"前提：两个字走 LIKE 兜底，实际 {res['mode']}"
    assert res["hits"][0]["sender_name"] == "张三", f"LIKE 路径没走 join：{res['hits'][0]}"


def test_recent_from_store_uses_display_name(db):
    """`_SENDER_JOIN` 的显示名接线（EA-072：原函数零生产调用且无 account 隔离，已删除；
    断言改打等价入口 `messages_in_range`——它带 account 且走同一个 join）。"""
    with store.open() as con:
        _seed(con, mk_row("acct", "888@chatroom", 1, "开会啦",
                          sender_id="wxid_a", sender_name="wxid_a"))
        store.replace_roster(con, "acct", [("888@chatroom", "wxid_a", "张三")])
    got, _truncated = search.messages_in_range("888@chatroom", account="acct", limit=10)
    assert got[0]["sender_name"] == "张三", f"没走 join：{got[0]}"


def test_semantic_recall_uses_display_name(db):
    """语义检索按 id 回取消息那一步（`vector._SELECT_MSGS`）。

    直接执行那个 SQL 常量——`semantic_search` 里是内联的
    `_SELECT_MSGS.format(ph=",".join("?" * len(ids)))`（`vector.py:383`），
    没有单独的取数函数可以调用。
    """
    with store.open() as con:
        _seed(con, mk_row("acct", "888@chatroom", 1, "开会啦",
                          sender_id="wxid_a", sender_name="wxid_a"))
        store.replace_roster(con, "acct", [("888@chatroom", "wxid_a", "张三")])
        rows = con.execute(vector._SELECT_MSGS.format(ph="?"), [1]).fetchall()
    assert rows[0]["sender_name"] == "张三", f"语义回取没走 join：{dict(rows[0])}"


def test_join_falls_back_to_stored_value_when_not_in_roster(db):
    """**join 不上时逐字回落**：退群的人、不在 contact 表里的人，行为与今天一致。
    这条与上面四条成对——只测「join 上」会让一个恒返回 NULL 的 join 也全绿。"""
    with store.open() as con:
        _seed(con, mk_row("acct", "888@chatroom", 1, "开会啦",
                          sender_id="wxid_gone", sender_name="wxid_gone"))
    hits = search.search("开会啦", account="acct")["hits"]
    assert hits[0]["sender_name"] == "wxid_gone"


def test_join_drives_from_messages_not_the_roster(db):
    """驱动表必须是 messages（43.9 万行）。让 3 万行的名册驱动，意味着每次查询都要
    遍历整个名册。主键与 join 条件吻合、理论上不该如此——但这是要靠 EXPLAIN 确认的，
    不是靠推理的（spec §2.7）。"""
    with store.open() as con:
        plan = [str(r[-1]) for r in con.execute(
            f"EXPLAIN QUERY PLAN SELECT m.id, {store._SENDER_EXPR} "
            f"FROM messages m {store._SENDER_JOIN} WHERE m.account = ?", ("acct",))]
    assert plan, "EXPLAIN QUERY PLAN 必须有输出"
    assert "dn" not in plan[0].split(), f"名册成了驱动表：{plan}"
