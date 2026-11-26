"""MCP 全量审查修复：翻页不漏同秒消息、同一秒超预算不打转、上下文的归属校验按 chat_md5、
AGENT_WECHAT_DENY 覆盖所有读会话内容的 MCP 工具、before_ts 值域、get_message_context 的 type、
export_moments 拒绝 out_dir 时的默认根、semantic_search 命中省略空值。全部用合成数据。"""
import asyncio
import json

import pytest

from agent_wechat import export, mcp_server, messages, store, usecases, vector
from helpers import mk_row
from test_msg_key_v10 import ACCT, FRIEND, T0, md5, wx  # noqa: F401  （夹具）


def _call(name, args):
    r = asyncio.run(mcp_server.server.call_tool(name, {**args, "account": ACCT}))
    return json.loads(r.content[0].text)


def _pages(name, args, key, max_pages=8):
    """跟着 next_before_ts 一路往前翻，返回每页的结果；翻不完（原地打转）就失败。"""
    out, before = [], None
    for _ in range(max_pages):
        res = _call(name, {**args, "before_ts": before})
        assert "error" not in res, res
        out.append(res)
        if "next_before_ts" not in res:
            return out
        assert res["next_before_ts"] != before, "下一页与这一页相同：原地打转"
        before = res["next_before_ts"]
    pytest.fail(f"{max_pages} 页还没翻完：{[[m.get(key) for m in p[key]] for p in out]}")


def _index(rows):
    with store.open() as c:
        store.upsert_batch(c, rows)
        c.commit()


# ---------- 1. 条数到 limit 时也给 next_before_ts，同秒的不漏 ----------

def test_limit_cut_in_the_middle_of_a_second_does_not_lose_the_rest(wx):
    """3 条同秒 + 1 条较新、limit=2：修前第一页不给 next_before_ts，照「末段」翻页读不到同秒较早的。"""
    wx([(1, T0, "甲"), (2, T0, "乙"), (3, T0, "丙"), (4, T0 + 1, "丁")])
    pages = _pages("get_recent_messages", {"contact": "张三", "limit": 2}, "messages")
    seen = {m["content"] for p in pages for m in p["messages"]}
    assert seen == {"甲", "乙", "丙", "丁"}
    assert all(p["truncated"] is False for p in pages)      # truncated 仍只表示「因体积预算丢了」


def test_limit_pages_join_up_across_distinct_seconds(wx):
    wx([(i, T0 + i * 60, f"第{i}条") for i in range(1, 8)])
    pages = _pages("get_recent_messages", {"contact": "张三", "limit": 3}, "messages")
    assert "next_before_ts" in pages[0]
    seen = [m["content"] for p in reversed(pages) for m in p["messages"]]
    assert sorted(set(seen), key=lambda s: int(s[1:-1])) == [f"第{i}条" for i in range(1, 8)]


def test_pages_partition_the_history_exactly(wx):
    """多页翻下来每条消息恰好出现一次：同秒的边界整秒给出，页与页既不重复也不漏。"""
    wx([(1, T0, "a"), (2, T0, "b"), (3, T0, "c"),
        (4, T0 + 1, "d"), (5, T0 + 1, "e"),
        (6, T0 + 5, "f"), (7, T0 + 5, "g"), (8, T0 + 5, "h"),
        (9, T0 + 9, "i")])
    pages = _pages("get_recent_messages", {"contact": "张三", "limit": 2}, "messages")
    keys = [m["msg_key"] for p in pages for m in p["messages"]]
    assert len(keys) == len(set(keys)) == 9
    assert {m["content"] for p in pages for m in p["messages"]} == set("abcdefghi")


# ---------- 2. 同一秒的消息多到超预算：整秒给出，不原地打转 ----------

def test_recent_over_budget_within_one_second_does_not_loop(wx, monkeypatch):
    wx([(i, T0, "长" * 400) for i in range(1, 11)])
    monkeypatch.setattr(mcp_server, "OUTPUT_TOKEN_BUDGET", 1500)
    pages = _pages("get_recent_messages", {"contact": "张三"}, "messages")
    keys = {m["msg_key"] for p in pages for m in p["messages"]}
    assert len(keys) == 10


def test_recent_limit_full_of_one_second_does_not_loop(wx):
    """整页（limit 条）都在同一秒：那一秒的其余消息要补全，否则 before_ts=该秒+1 永远是这一页。"""
    wx([(1, T0 - 60, "更早")] + [(i, T0, f"同秒{i}") for i in range(2, 7)])
    pages = _pages("get_recent_messages", {"contact": "张三", "limit": 2}, "messages")
    seen = {m["content"] for p in pages for m in p["messages"]}
    assert seen == {f"同秒{i}" for i in range(2, 7)} | {"更早"}


def test_search_history_over_budget_within_one_second_does_not_loop(wx, monkeypatch):
    wx([(1, T0 - 60, "预算更早")] + [(i, T0, "预算" + "长" * 400) for i in range(2, 12)])
    export.run_export(ACCT)
    monkeypatch.setattr(mcp_server, "OUTPUT_TOKEN_BUDGET", 1500)
    pages = _pages("search_history", {"query": "预算"}, "hits")
    assert len({h["msg_key"] for p in pages for h in p["hits"]}) == 11


# ---------- 3/4. 上下文的归属校验按 chat_md5；错误 hint 与入口无关 ----------

def test_context_accepts_the_real_name_when_the_index_has_a_degraded_chat_id(wx):
    wx([(1, T0, "x")])
    row = messages.make_row(account=ACCT, chat_md5=md5(FRIEND),
                            chat_id=messages.degraded_chat_id(md5(FRIEND)),
                            shard="message_0.db", local_id=7, ts=T0 + 7, msg_type=1,
                            sender_id=None, sender_name=None, is_self=0,
                            content="退化会话名", media_hint=None)
    _index([row])
    res = _call("get_message_context", {"contact": "张三", "msg_key": row["msg_key"]})
    assert "error" not in res, res
    assert [m["content"] for m in res["messages"]] == ["退化会话名"]


def test_context_mismatch_hint_does_not_suggest_omitting_contact(wx):
    wx([(1, T0, "x")])
    other = mk_row(ACCT, "wxid_otherchat", 1, "别的会话", ts=T0)
    _index([other])
    res = _call("get_message_context", {"contact": "张三", "msg_key": other["msg_key"]})
    assert res["error"] == "msg_key 不属于该联系人"
    assert "省略" not in res["hint"] and "contact" in res["hint"]


# ---------- 5. AGENT_WECHAT_DENY 覆盖所有读会话内容的 MCP 工具 ----------

POINT_READS = [
    ("get_recent_messages", {}),
    ("search_history", {"query": "会议"}),
    ("semantic_search", {"query": "会议"}),
    ("get_message_media", {}),
    ("get_message_parts", {"keys": "x"}),
    ("export_chat", {}),
    ("export_chat_media", {}),
]


@pytest.mark.parametrize("deny", [FRIEND, messages.degraded_chat_id(md5(FRIEND))])
@pytest.mark.parametrize(("name", "args"), POINT_READS)
def test_denied_chat_is_refused_by_every_point_read(wx, monkeypatch, name, args, deny):
    wx([(1, T0, "会议")])
    monkeypatch.setenv("AGENT_WECHAT_DENY", deny)
    res = _call(name, {"contact": "张三", **args})
    assert "不在读取范围内" in res.get("error", ""), res


def test_search_history_pushes_the_deny_list_into_sql(wx, monkeypatch):
    """limit=1、被拒会话的命中更新：取回后再滤会答 0 条，下推进 SQL 才填得满。"""
    wx([(1, T0, "x")])
    _index([mk_row(ACCT, FRIEND, 1, "会议纪要", ts=T0 + 100),
            mk_row(ACCT, "wxid_otherchat", 1, "会议安排", ts=T0)])
    monkeypatch.setenv("AGENT_WECHAT_DENY", FRIEND)
    res = _call("search_history", {"query": "会议", "limit": 1})
    assert [h["content"] for h in res["hits"]] == ["会议安排"] and res["total"] == 1


def test_semantic_search_pushes_the_deny_list_into_knn(wx, monkeypatch):
    wx([(1, T0, "x")])
    seen = {}
    monkeypatch.setattr(vector, "semantic_search",
                        lambda *a, **k: seen.update(k) or {"hits": [], "total": 0})
    monkeypatch.setenv("AGENT_WECHAT_DENY", FRIEND)
    _call("semantic_search", {"query": "会议"})
    assert set(seen["exclude_chats"]) == {FRIEND}


def test_the_cli_path_is_not_scoped_by_the_deny_list(wx, monkeypatch):
    """usecases 被 CLI 共用：读取范围只由 MCP 入口传入，CLI 的 recent / search 照旧。"""
    wx([(1, T0, "会议")])
    export.run_export(ACCT)
    monkeypatch.setenv("AGENT_WECHAT_DENY", FRIEND)
    assert [m.content for m in usecases.recent_messages("张三", account=ACCT).messages] == ["会议"]
    assert usecases.search_history("会议", account=ACCT)["total"] == 1


# ---------- 6. semantic_search 的命中也省略空值 ----------

def test_semantic_hits_omit_null_fields(monkeypatch):
    monkeypatch.setattr(usecases, "semantic_search", lambda *a, **k: {
        "hits": [{"msg_key": f"a|b|c|1|{T0}", "ts": T0, "content": "x",
                  "media_hint": None, "distance": 0.1}], "total": 1, "degraded": []})
    hit = _call("semantic_search", {"query": "x"})["hits"][0]
    assert "media_hint" not in hit and hit["content"] == "x"


# ---------- 7. get_recent_messages 的 before_ts 值域 ----------

def test_recent_before_ts_out_of_int64_is_an_input_error(wx):
    wx([(1, T0, "x")])
    res = _call("get_recent_messages", {"contact": "张三", "before_ts": 2 ** 63})
    assert "超出范围" in res["error"] and "内部错误" not in res["error"]


# ---------- 8. get_message_context 的非文本条目带 type ----------

def test_context_entries_carry_type_for_non_text(wx):
    wx([(1, T0, "x")])
    text = mk_row(ACCT, FRIEND, 1, "文字", ts=T0)
    image = mk_row(ACCT, FRIEND, 2, None, ts=T0 + 1, msg_type=messages.TYPE_IMAGE,
                   media_hint="[image]")
    _index([text, image])
    res = _call("get_message_context", {"contact": "张三", "msg_key": text["msg_key"]})
    assert [m.get("type") for m in res["messages"]] == [None, "image"]


# ---------- 9. export_moments 拒绝 out_dir 时给的是朋友圈的默认根 ----------

def test_export_moments_refusal_names_the_moments_root(wx, tmp_path):
    res = _call("export_moments", {"out_dir": str(tmp_path / "elsewhere")})
    assert "moments" in res["hint"] and "media" not in res["hint"]


# ---------- 10. search_messages 的 truncated 两种含义 ----------

def test_search_messages_describes_both_meanings_of_truncated():
    tools = {t.name: t.description for t in asyncio.run(mcp_server.server.list_tools())}
    assert "多于 limit" in tools["search_messages"]


# ---------- search_history：条数到 limit 时也给续搜位置（与 get_recent_messages 同口径） ----------

def test_search_history_at_limit_pages_without_losing_the_same_second(wx):
    from agent_wechat import export
    from test_msg_key_v10 import ACCT
    from test_reader_export_pipeline import T0
    wx([(1, T0 + 10, "续搜"), (2, T0 + 10, "续搜"), (3, T0 + 10, "续搜"), (4, T0 + 99, "续搜")])
    export.run_export(ACCT)
    seen, before = [], None
    for _ in range(6):
        res = _call("search_history", {"query": "续搜", "account": ACCT, "limit": 2, "before_ts": before})
        seen += [h["msg_key"] for h in res["hits"]]
        if "next_before_ts" not in res:
            break
        before = res["next_before_ts"]
    assert len(set(seen)) == 4                                   # 同秒那 3 条一条不漏


def test_search_history_full_page_in_one_second_does_not_loop(wx):
    from agent_wechat import export
    from test_msg_key_v10 import ACCT
    from test_reader_export_pipeline import T0
    wx([(i, T0 + 10, "同秒") for i in range(1, 5)])
    export.run_export(ACCT)
    res = _call("search_history", {"query": "同秒", "account": ACCT, "limit": 2})
    assert len(res["hits"]) == 4 and res["next_before_ts"] == T0 + 10   # 整秒给全，从它之前接着搜
