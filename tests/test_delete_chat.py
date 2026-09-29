"""delete_chat 的单元测试。红线：本文件每个测试都不得指向真实 ~/.agent-wechat。"""
import sqlite3

import pytest

from agent_wechat import exclusions, messages, paths, store


@pytest.fixture(autouse=True)
def _temp_index(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path / "home")
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    con = store.connect(tmp_path / "index.db")
    _seed(con)
    yield con
    con.close()


def _seed(con):
    """两个账号各一个会话，让反向断言有东西可查。"""
    def row(mk, acct, cm, cid, lid, ts, content):
        return {"msg_key": mk, "account": acct, "chat_md5": cm, "chat_id": cid,
                "shard": "message_0.db", "local_id": lid, "server_id": lid,
                "ts": ts, "msg_type": 1, "sender_id": None, "sender_name": None,
                "is_self": 0, "content": content, "media_hint": None}
    # 顺序有含义：`id` 是裸 rowid、按插入顺序自增。keeper **必须先插**——test_rowid_reuse_regression
    # 要的是「删掉 victim 后新的 max(id) == 1」（id 1 归 keeper），而被删的 victim 持有最大的
    # 两个 id（2、3），正是那条测试 docstring 说的「删掉持有最大 id 的行、那个 id 会被复用」。
    # （计划的这一行原本把 victim 写在前面，于是 keeper 拿到 id 3、水位线只能降 3——
    # 与它自己断言的 1 自相矛盾。见 task-3 报告。）
    store.upsert_batch(con, [
        row("a|bbbb|message_0.db|1", "a", "bbbb", "keeper", 1, 300, "留下的"),
        row("a|aaaa|message_0.db|1", "a", "aaaa", "victim", 1, 100, "要删的 1"),
        row("a|aaaa|message_0.db|2", "a", "aaaa", "victim", 2, 200, "要删的 2"),
    ])
    store.upsert_msg_parts(con, [("a|aaaa|message_0.db|1", 57, None, None, b"<x/>")])
    store.upsert_media_refs(con, [("a|aaaa|message_0.db|1", "image", "f" * 32, None)])
    con.execute("INSERT INTO display_names VALUES ('a','victim','u1','受害人')")
    con.execute("INSERT INTO display_names VALUES ('a','keeper','u2','留下的人')")
    con.commit()


def test_delete_chat_removes_all_five_places(_temp_index):
    con = _temp_index
    got = store.delete_chat(con, account="a", chat_id="victim", chat_md5="aaaa")
    assert got == {"messages": 2, "vectors": 0, "parts": 1, "refs": 1, "display_names": 1}
    assert store.chat_row_counts(con, "a", "victim") == {
        "messages": 0, "vectors": 0, "parts": 0, "refs": 0, "display_names": 0}
    # 上面那条**挡不住 parts/refs 一条没删**：chat_row_counts 里这两项数的是
    # `msg_key IN (SELECT msg_key FROM messages …)`，messages 一删子查询就空、恒为 0。
    # 实测把 `DELETE FROM messages` 提到 parts/refs 之前，上面那条断言照样全绿，而两张表里
    # 各留着一行孤儿（raw_xml 就是 purge 要销毁的那份载荷）。所以这里直接数全表——
    # 夹具里这两张表只有 victim 的行，全表为 0 就等于「这个会话的载荷真没了」。
    assert con.execute("SELECT count(*) FROM msg_parts").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM media_refs").fetchone()[0] == 0


def test_delete_chat_leaves_the_other_chat_untouched(_temp_index):
    """**反向断言**：范围泄漏在这门命令上不可逆，比正断言重要。"""
    con = _temp_index
    store.delete_chat(con, account="a", chat_id="victim", chat_md5="aaaa")
    assert store.chat_row_counts(con, "a", "keeper")["messages"] == 1
    assert con.execute("SELECT count(*) FROM display_names WHERE chat_id='keeper'"
                       ).fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM messages_fts").fetchone()[0] == 1, \
        "FTS 必须由触发器跟随——少删一条就是搜索里还搜得到"


def test_rowid_reuse_regression(_temp_index):
    """**本轮最贵的一条**（spec §2.2）：`messages.id` 是裸 INTEGER PRIMARY KEY，
    删掉持有最大 id 的行之后那个 id 会被复用，而 vector_watermark 的语义是「id ≤ 它的都已嵌入」。

    没有那行 `min(last_rowid, max(id))` 修法时，新消息会拿到 ≤ 旧水位线的 id、永远不被嵌入。
    """
    con = _temp_index
    # 这两张表**不在 store.SCHEMA 里**——它们属于 VECTOR_SCHEMA（owner 是 vector.py），
    # 没建过向量索引的机器上不存在（store.py:83 的注释就是写这件事的）。本测试要验的是
    # 「有它们时水位线怎么修」，所以手工建；`vec_messages` 是虚表、需要 sqlite_vec，
    # 这里不建——正好顺带覆盖「没有向量表时 delete_chat 照常工作」。
    con.executescript(
        "CREATE TABLE IF NOT EXISTS vector_watermark ("
        "  model_id TEXT NOT NULL, account TEXT NOT NULL,"
        "  last_rowid INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL,"
        "  PRIMARY KEY (model_id, account));"
        "CREATE TABLE IF NOT EXISTS vector_meta ("
        "  model_id TEXT PRIMARY KEY, dim INTEGER NOT NULL, built_at INTEGER NOT NULL,"
        "  embedded INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0);")
    con.execute("INSERT INTO vector_watermark VALUES ('m', 'a', 999999, 0)")
    con.commit()
    store.delete_chat(con, account="a", chat_id="victim", chat_md5="aaaa")
    wm = con.execute("SELECT last_rowid FROM vector_watermark WHERE account='a'").fetchone()[0]
    assert wm == 1, f"水位线必须降到新的 max(id)=1，实际 {wm}"
    # 注意这条测试**不区分**「全局 max」与「该账号自己的 max」——本夹具只有一个账号，两者相同。
    # 也不必区分：2026-09-30 终审推翻了「必须全局」那条约束（扫描按账号过滤，危害不存在；
    # 执行计划也不用 id>? 做范围，性能也无差别）。别为了让它们可区分而往夹具里加第二个账号。


def test_delete_chat_without_any_vector_tables(_temp_index):
    """**轻装安装的判据**：这三张表属于 VECTOR_SCHEMA，没建过向量索引时一张都没有——
    而无条件查它们会让 `purge --chat` 直接崩在 `no such table`（spec §2.3 的警示框）。

    `_temp_index` 这个 fixture 本身就不建向量表，所以上面每个测试都在跑这条路径；
    这里显式钉一次，免得日后有人顺手在 fixture 里补上建表。
    """
    con = _temp_index
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "vec_messages" not in names and "vector_watermark" not in names, \
        "前提：这个 fixture 刻意不建向量表"
    got = store.delete_chat(con, account="a", chat_id="victim", chat_md5="aaaa")
    assert got["vectors"] == 0
    assert store.chat_row_counts(con, "a", "victim")["vectors"] == 0
