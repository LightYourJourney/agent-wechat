"""MCP 第三轮：当前账号判定的短时缓存、contact.db 读取的跨调用复用与按会话取名册、语义模型
后台预热，以及消息条目省掉可推导字段。全部用合成数据（docs/MCP_EFFICIENCY.md §8）。"""
import asyncio
import json
import os
import shutil
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from agent_wechat import (account, decrypt, export, mcp_server, paths, reader, roster, store,
                          usecases, vector)
from test_msg_key_v10 import wx  # noqa: F401  （夹具）
from test_reader_export_pipeline import (ACCT, FRIEND, GROUP, SELF, T0, build_shard,  # noqa: F401
                                         drift_tree, env, wechat)


# ---------- 当前账号判定 ----------

@pytest.fixture()
def probes(monkeypatch):
    clock = SimpleNamespace(now=1000.0, pid=42, cur=ACCT, pgrep=0, lsof=0)

    def pgrep():
        clock.pgrep += 1
        return clock.pid

    def lsof(pid, known=None):
        clock.lsof += 1
        return clock.cur

    monkeypatch.setattr(account, "wechat_pid", pgrep)
    monkeypatch.setattr(account, "current_account", lsof)
    monkeypatch.setattr(account.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(paths, "account_dirs", lambda: [ACCT])
    monkeypatch.setattr(paths, "unrecognized_dirs", lambda: [])
    return clock


def test_account_status_reuses_pgrep_and_lsof_for_a_short_while(probes):
    assert account.status().current == ACCT
    probes.now += 1
    assert account.status().current == ACCT
    assert (probes.pgrep, probes.lsof) == (1, 1)
    probes.now += account.PID_TTL                       # pgrep 过期；pid 没变就不重跑 lsof
    account.status()
    assert (probes.pgrep, probes.lsof) == (2, 1)
    probes.now += account.ACCOUNT_TTL                   # lsof 结果也过期
    account.status()
    assert probes.lsof == 2


def test_a_new_wechat_pid_re_runs_lsof(probes):
    account.status()
    probes.now += account.PID_TTL
    probes.pid, probes.cur = 43, "wxid_otheracct"
    assert account.status().current == "wxid_otheracct"
    assert probes.lsof == 2


def test_an_undetermined_account_is_not_cached(probes):
    probes.cur = None
    assert account.status().current is None
    probes.cur = ACCT
    assert account.status().current == ACCT            # 判不出来的那次不缓存
    assert probes.lsof == 2


# ---------- contact.db：按会话取名册、跨调用复用 ----------

def _contact_opens(monkeypatch) -> list:
    opened: list = []
    real = decrypt.open_db

    def counting(db, pw):
        if db.name == "contact.db":
            opened.append(db)
        return real(db, pw)

    monkeypatch.setattr(decrypt, "open_db", counting)
    return opened


def test_roster_for_is_the_full_roster_restricted_to_that_chat(env):
    full = {(c, k): d for c, k, d in reader.MessageReader(ACCT).roster()}
    for chat in (GROUP, FRIEND):
        part = {(c, k): d for c, k, d in reader.MessageReader(ACCT).roster_for(chat)}
        mine = {ck: d for ck, d in full.items() if ck[0] == chat}
        assert mine and {ck: d for ck, d in part.items() if ck[0] == chat} == mine


def test_roster_for_a_private_chat_parses_no_group(env, monkeypatch):
    parsed: list = []
    real = roster.parse_members
    monkeypatch.setattr(roster, "parse_members", lambda b: parsed.append(1) or real(b))
    reader.MessageReader(ACCT).roster_for(FRIEND)
    assert parsed == []
    reader.MessageReader(ACCT).roster_for(GROUP)
    assert parsed == [1]


def test_recent_uses_the_per_chat_roster_not_the_full_one(env, monkeypatch):
    def boom(self):
        raise AssertionError("recent 不该解析全部群的名册")
    monkeypatch.setattr(reader.MessageReader, "roster", boom)
    msgs = reader.MessageReader(ACCT).recent(GROUP)
    assert [m.sender_name for m in msgs if not m.is_self] == ["群里的张三"]


def test_contact_reads_are_reused_until_contact_db_changes(env, tmp_path, monkeypatch):
    wx_copy = tmp_path / "wx"
    shutil.copytree(env.wx, wx_copy)
    monkeypatch.setattr(paths, "XWECHAT_FILES", wx_copy)
    opened = _contact_opens(monkeypatch)
    for _ in range(2):                                  # 第二轮全部命中缓存（新 reader 也一样）
        r = reader.MessageReader(ACCT)
        assert r.display_name(GROUP) == "项目群"
        got = r.contacts(query="张三")
        assert [c["username"] for c in got] == [FRIEND]
        got[0]["display"] = "被调用方改掉"               # 返回的是副本，不污染缓存
        r.roster_for(GROUP)
    assert len(opened) == 3
    assert reader.MessageReader(ACCT).contacts(query="张三")[0]["display"] == "张三"
    db = wx_copy / ACCT / "db_storage" / "contact" / "contact.db"
    st = db.stat()
    os.utime(db, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))   # 微信写了库
    assert reader.MessageReader(ACCT).display_name(GROUP) == "项目群"
    assert len(opened) == 4


def test_display_name_reads_one_contact_instead_of_the_whole_table(env, monkeypatch):
    monkeypatch.setattr(reader.MessageReader, "_contacts_uncached",
                        lambda self, q, limit: pytest.fail("不该为一个名字读全表"))
    assert reader.MessageReader(ACCT).display_name(FRIEND) == "张三"
    assert reader.MessageReader(ACCT).display_name("wxid_otherchat") == "wxid_otherchat"


def test_a_degraded_roster_is_not_cached(tmp_path, monkeypatch, env):
    """漂移的说明挂在 reader 实例上：命中缓存的下一次调用会丢掉它，所以降级了就不缓存。"""
    wx_copy = tmp_path / "wx"
    shutil.copytree(env.wx, wx_copy)
    drift_tree(wx_copy, ACCT, drop_table="chat_room")
    monkeypatch.setattr(paths, "XWECHAT_FILES", wx_copy)
    for _ in range(2):
        r = reader.MessageReader(ACCT)
        r.roster_for("wxid_a")
        assert any(d.table == "chat_room" for d in r._drift)


# ---------- 语义模型后台预热 ----------

def test_warm_up_only_when_the_index_and_the_local_model_exist(tmp_path, monkeypatch):
    warmed: list = []
    monkeypatch.setattr(vector, "semantic_installed", lambda: True)
    monkeypatch.setattr(vector, "MODEL_CACHE_DIR", tmp_path / "models")
    monkeypatch.setattr(vector, "default_embedder",
                        lambda: SimpleNamespace(warm_up=lambda: warmed.append(1)))
    assert vector.warm_up_if_ready() is False                   # 没有索引库
    con = sqlite3.connect(store.DB_PATH)
    con.execute("CREATE TABLE vector_meta (model_id TEXT PRIMARY KEY, embedded INTEGER)")
    con.execute("INSERT INTO vector_meta VALUES (?, 0)", (vector.MODEL_ID,))
    con.commit()
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "x").write_text("")
    assert vector.warm_up_if_ready() is False                   # 索引是空的
    con.execute("UPDATE vector_meta SET embedded=5")
    con.commit()
    con.close()
    (tmp_path / "models" / "x").unlink()
    assert vector.warm_up_if_ready() is False                   # 模型不在本地：不预热（也就不下载）
    (tmp_path / "models" / "x").write_text("")
    assert vector.warm_up_if_ready() is True and warmed == [1]


def test_warm_up_loads_the_model_offline_only(monkeypatch):
    seen: dict = {}

    class FakeModel:
        def __init__(self, **kw):
            seen.update(kw)

        def query_embed(self, texts):
            seen["queried"] = texts
            return iter([[0.0]])

    monkeypatch.setitem(sys.modules, "fastembed", SimpleNamespace(TextEmbedding=FakeModel))
    vector.FastEmbedEmbedder().warm_up()
    assert seen["local_files_only"] is True and seen["queried"]


def test_a_failing_warm_up_is_swallowed(monkeypatch):
    def boom(*_a):
        raise RuntimeError("模型坏了")
    monkeypatch.setattr(vector, "warm_up_if_ready", boom)
    monkeypatch.setattr(usecases, "warm_up", boom)
    mcp_server._warm_up()                                       # 不抛


def test_key_warm_up_failing_does_not_skip_the_model(monkeypatch):
    seen = []
    monkeypatch.setattr(usecases, "warm_up", lambda: 1 / 0)
    monkeypatch.setattr(vector, "warm_up_if_ready", lambda: seen.append(1))
    mcp_server._warm_up()
    assert seen == [1]


# ---------- 消息条目省掉可推导字段 ----------

def _call(name, args):
    r = asyncio.run(mcp_server.server.call_tool(name, {**args, "account": ACCT}))
    return json.loads(r.content[0].text)


def test_message_items_drop_derivable_fields(wx):
    # (local_id, type, sender rowid, create_time, 正文, ...)：sender 2 = 本人，type 3 = 图片
    build_shard(paths.XWECHAT_FILES, ACCT, "message_0.db",
                {FRIEND: [(1, 1, 1, T0 + 60, "对方说的", 0),
                              (2, 1, 2, T0 + 120, "我说的", 0),
                              (3, 3, 1, T0 + 180, None, 0)]},
                [FRIEND, SELF])
    export.run_export(ACCT)
    msgs = _call("get_recent_messages", {"contact": "张三"})["messages"]
    assert [m["sender"] for m in msgs] == ["张三", "我", "张三"]
    assert all("time" not in m and "is_self" not in m for m in msgs)
    assert "type" not in msgs[0] and msgs[2]["type"] != "text"

    ctx = _call("get_message_context", {"contact": "张三", "msg_key": msgs[0]["msg_key"]})
    assert ctx["untrusted"] is True                                # 顶层仍标不可信
    assert ctx["messages"][1]["sender"] == "我"                    # 自己发的：sender 换成「我」
    assert all(set(m) <= {"msg_key", "when", "sender", "content"} for m in ctx["messages"])

    hits = _call("search_history", {"query": "说的"})["hits"]
    assert hits and all(not {"shard", "local_id", "id"} & set(h) for h in hits)
    assert all(v is not None for h in hits for v in h.values())

    res = _call("search_messages", {"query": "说的", "mode": "keyword"})
    assert res["untrusted"] is True and res["hits"]
    assert all(not {"untrusted", "sources", "ranks", "distance"} & set(h) for h in res["hits"])


def test_a_contact_literally_named_me_keeps_is_self_false():
    d = mcp_server._slim_message({"msg_key": "k", "time": T0, "sender": "我", "is_self": False,
                                  "type": "text", "content": "x"})
    assert d == {"msg_key": "k", "when": mcp_server._when(T0), "sender": "我",
                 "content": "x", "is_self": False}
