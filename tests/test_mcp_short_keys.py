"""MCP 返回形态的第二轮：短 msg_key、每条消息的 `when`、get_recent_messages 的体积预算与翻页，
以及冷启动不载入 CLI / sqlite_vec。全部用合成数据（docs/MCP_EFFICIENCY.md §7）。"""
import asyncio
import json
import subprocess
import sys
import time
import zlib

import pytest

from agent_wechat import errors, exclusions, export, mcp_server, messages, retrieval, store
from helpers import mk_row
from test_msg_key_v10 import ACCT, FRIEND, T0, con, md5, wx  # noqa: F401  （夹具）

KEY = messages.row_key(ACCT, md5(FRIEND), "message_0.db", 59, T0 + 59)


def _ts(m):
    """消息条目不再带 time：秒级时间取 msg_key 末段。"""
    return int(m["msg_key"].rsplit(":", 1)[1])


def _call(name, args):
    r = asyncio.run(mcp_server.server.call_tool(name, {**args, "account": ACCT}))
    return json.loads(r.content[0].text)


# ---------- 短键的形态 ----------

def test_short_key_round_trip_shape():
    short = messages.short_key(KEY)
    assert short == f"{md5(FRIEND)[:8]}:59:{T0 + 59}"
    assert messages.split_short_key(short) == (md5(FRIEND)[:8], 59, T0 + 59)
    # 不是 5 段完整键的原样返回：旧 4 段键、已是短键、畸形键
    for k in (messages.legacy_key(KEY), short, "a|b|c|1|2", "x"):
        assert messages.short_key(k) == k
    for bad in ("2cb559d:1:2", "2cb559dg:1:2", "2cb559de:1", "2cb559de:-1:2", "2cb559de:1:2:3"):
        assert messages.split_short_key(bad) is None, bad


# ---------- 短键换回完整键 ----------

def test_resolve_a_short_key_from_messages(con):
    a = mk_row(ACCT, FRIEND, 59, "x", ts=T0 + 59)
    other = mk_row(ACCT, "wxid_otherchat", 59, "y", ts=T0 + 59)     # 同号同秒、别的会话
    store.upsert_batch(con, [a, other])
    assert store.resolve_msg_key(con, ACCT, messages.short_key(a["msg_key"])) == a["msg_key"]
    assert store.resolve_msg_keys(con, ACCT, [messages.short_key(a["msg_key"]), a["msg_key"]]) \
        == [a["msg_key"]]
    # 找不到原样返回（调用方走「找不到」）；别的账号的同形短键不会解析到本账号
    assert store.resolve_msg_key(con, ACCT, f"{md5(FRIEND)[:8]}:60:{T0}") == f"{md5(FRIEND)[:8]}:60:{T0}"
    assert store.resolve_msg_key(con, "wxid_otheracct", messages.short_key(a["msg_key"])) \
        == messages.short_key(a["msg_key"])


def test_an_ambiguous_short_key_is_refused_not_guessed(con):
    """同会话两个分片里同 local_id、同一秒（实际上不会出现，但不许猜）。"""
    a = mk_row(ACCT, FRIEND, 59, "x", ts=T0 + 59)
    b = mk_row(ACCT, FRIEND, 59, "y", ts=T0 + 59, shard="message_1.db")
    store.upsert_batch(con, [a, b])
    with pytest.raises(errors.SearchInputError, match="多条消息") as ei:
        store.resolve_msg_key(con, ACCT, messages.short_key(a["msg_key"]))
    assert "完整 msg_key" in (ei.value.hint or "")


def test_a_short_key_resolves_through_msg_parts_when_the_message_is_not_indexed(con):
    """appmsg 回填扫的是微信库：msg_parts 里可以有索引里还没有的消息。"""
    store.upsert_msg_parts(con, [(KEY, 5, None, None, zlib.compress(b"<appmsg/>"))])
    con.commit()
    assert store.resolve_msg_key(con, ACCT, messages.short_key(KEY)) == KEY


def test_a_short_key_of_a_purged_message_is_reported_as_destroyed(con):
    """行已删、只剩墓碑：短键要认出墓碑，答「已被销毁」而不是「找不到」。"""
    exclusions.add(con, [(KEY, ACCT, md5(FRIEND))])
    con.commit()
    with pytest.raises(errors.DataNotFound, match="销毁"):
        retrieval.get_context(messages.short_key(KEY), account=ACCT)


# ---------- 经 MCP 的端到端 ----------

def test_mcp_returns_short_keys_and_when_and_takes_them_back(wx):
    wx([(i, T0 + i * 60, f"第{i}条") for i in range(1, 6)])
    export.run_export(ACCT)
    res = _call("get_recent_messages", {"contact": "张三", "limit": 3})
    # 条数到 limit：可能还有更早的，给 next_before_ts——本页最早那一秒是完整的，从它之前续读
    assert res["truncated"] is False and res["next_before_ts"] == T0 + 180
    first = res["messages"][0]
    assert first["msg_key"] == f"{md5(FRIEND)[:8]}:3:{T0 + 180}"
    assert first["when"] == time.strftime("%Y-%m-%d %H:%M", time.localtime(T0 + 180))
    assert list(first) == ["msg_key", "when", "sender", "content"]   # time/is_self/type=text 省略

    ctx = _call("get_message_context", {"contact": "张三", "msg_key": first["msg_key"]})
    assert ctx["msg_key"] == first["msg_key"]
    assert [m["content"] for m in ctx["messages"]] == ["第1条", "第2条", "第3条", "第4条", "第5条"]
    assert all("when" in m for m in ctx["messages"])

    hits = _call("search_messages", {"query": "第4条", "mode": "keyword"})["hits"]
    assert hits[0]["msg_key"] == f"{md5(FRIEND)[:8]}:4:{T0 + 240}" and "when" in hits[0]
    media = _call("get_message_media", {"contact": "张三", "keys": first["msg_key"]})
    assert media["counts"] == {"none": 1, "exported": 0, "exportable": 0, "gone": 0}
    assert media["messages"][0]["msg_key"] == first["msg_key"]
    # 完整键照收
    full = messages.row_key(ACCT, md5(FRIEND), "message_0.db", 3, T0 + 180)
    # get_message_parts：短键要先换回完整键再按会话前缀过滤（先过滤会把短键全滤掉）
    with store.open() as c:
        store.upsert_msg_parts(c, [(full, 5, None, None, zlib.compress(b"not xml"))])
        c.commit()
    assert _call("get_message_parts", {"contact": "张三", "keys": first["msg_key"]})["unparsed"] == 1
    assert _call("get_message_context", {"contact": "张三", "msg_key": full})["msg_key"] \
        == first["msg_key"]


def test_recent_messages_pages_with_before_ts(wx):
    wx([(i, T0 + i * 60, f"第{i}条") for i in range(1, 8)])
    res = _call("get_recent_messages", {"contact": "张三", "limit": 3, "before_ts": T0 + 5 * 60})
    assert [m["content"] for m in res["messages"]] == ["第2条", "第3条", "第4条"]


def test_recent_messages_over_budget_keeps_the_newest_and_says_where_to_continue(wx, monkeypatch):
    wx([(i, T0 + i * 60, "长" * 400) for i in range(1, 11)])
    monkeypatch.setattr(mcp_server, "OUTPUT_TOKEN_BUDGET", 1500)
    res = _call("get_recent_messages", {"contact": "张三", "limit": 10})
    got = [_ts(m) for m in res["messages"]]
    assert res["truncated"] is True and 0 < len(got) < 10
    assert got == [T0 + i * 60 for i in range(11 - len(got), 11)]      # 留下的是最新的
    assert res["next_before_ts"] == got[0]
    more = _call("get_recent_messages", {"contact": "张三", "limit": 10,
                                         "before_ts": res["next_before_ts"]})
    assert _ts(more["messages"][-1]) == got[0] - 60                 # 接得上，不重不漏


def test_next_before_ts_stops_on_a_whole_second_boundary(wx, monkeypatch):
    """预算把页尾切在同一秒上：那一秒整块留下（超预算也给），续读位置从它之前开始——
    下一页既不重复也不漏（以前只能给「该秒 + 1」，这一页的同秒消息会在下一页重复）。"""
    wx([(1, T0, "长" * 400), (2, T0 + 60, "长" * 400), (3, T0 + 60, "长" * 400),
        (4, T0 + 120, "长" * 400)])
    monkeypatch.setattr(mcp_server, "OUTPUT_TOKEN_BUDGET", 1100)
    res = _call("get_recent_messages", {"contact": "张三", "limit": 4})
    assert [_ts(m) for m in res["messages"]] == [T0 + 60, T0 + 60, T0 + 120]
    assert res["next_before_ts"] == T0 + 60
    more = _call("get_recent_messages", {"contact": "张三", "limit": 4,
                                         "before_ts": res["next_before_ts"]})
    assert [_ts(m) for m in more["messages"]] == [T0]
    assert "next_before_ts" not in more


def test_pages_never_repeat_when_the_boundary_is_a_whole_second(wx):
    """不同秒的消息按 limit 翻页：下一页从上一页最早那一秒之前开始，两页没有重复的 msg_key。"""
    wx([(i, T0 + i * 60, f"第{i}条") for i in range(1, 6)])
    first = _call("get_recent_messages", {"contact": "张三", "limit": 2})
    second = _call("get_recent_messages", {"contact": "张三", "limit": 2,
                                           "before_ts": first["next_before_ts"]})
    a = {m["msg_key"] for m in first["messages"]}
    b = {m["msg_key"] for m in second["messages"]}
    assert a and b and not (a & b)


def test_a_single_message_larger_than_the_budget_is_still_returned(wx, monkeypatch):
    wx([(1, T0, "长" * 400)])
    monkeypatch.setattr(mcp_server, "OUTPUT_TOKEN_BUDGET", 10)
    res = _call("get_recent_messages", {"contact": "张三"})
    assert len(res["messages"]) == 1 and res["truncated"] is False


# ---------- 冷启动 ----------

def test_the_mcp_server_does_not_import_the_cli_or_sqlite_vec(tmp_path):
    """CLI 与 sqlite_vec（带 numpy）只有 CLI 入口与语义检索用得到：MCP server 启动时不载入。"""
    code = ("import sys, agent_wechat.mcp_server; "
            "print(sorted(m for m in ('agent_wechat.cli', 'sqlite_vec', 'numpy') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env={"AGENT_WECHAT_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}).stdout
    assert out.strip() == "[]"
