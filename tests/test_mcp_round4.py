"""MCP 第四轮：工具注解、get_task_status 的 wait（长轮询）、完整正文类工具共用的体积预算与
search_history 的 before_ts 续搜。全部用合成数据（docs/MCP_EFFICIENCY.md §9）。"""
import asyncio
import json
import time

from agent_wechat import export, mcp_server, usecases
from test_msg_key_v10 import ACCT, wx  # noqa: F401  （夹具）
from test_reader_export_pipeline import T0


def _call(name, args):
    r = asyncio.run(mcp_server.server.call_tool(name, args))
    return json.loads(r.content[0].text)


# ---------- 注解 ----------

def test_every_tool_says_whether_it_is_read_only():
    """Claude Code 把 readOnlyHint 的工具与其它只读调用并行执行；写本地文件的导出类标 idempotentHint。"""
    tools = {t.name: t.annotations for t in asyncio.run(mcp_server.server.list_tools())}
    writers = {"export_full_history", "build_vector_index", "export_chat_media",
               "export_moments", "export_chat"}
    assert all(a is not None for a in tools.values())
    assert {n for n, a in tools.items() if a.read_only_hint} == set(tools) - writers
    assert all(tools[n].read_only_hint is False and tools[n].idempotent_hint for n in writers)


# ---------- get_task_status(wait) ----------

def _fake_task(monkeypatch, finish_after: float):
    t0 = time.monotonic()
    calls = []

    def status(task_id=None):
        calls.append(1)
        done = time.monotonic() - t0 >= finish_after
        return {"id": task_id, "status": "done" if done else "running", "rows": len(calls)}

    monkeypatch.setattr(usecases, "task_status", status)
    monkeypatch.setattr(mcp_server, "TASK_POLL_INTERVAL", 0.05)
    return calls


def test_wait_returns_as_soon_as_the_task_finishes(monkeypatch):
    _fake_task(monkeypatch, finish_after=0.3)
    t = time.monotonic()
    res = _call("get_task_status", {"task_id": "t1", "wait": 30})
    assert res["status"] == "done" and time.monotonic() - t < 5


def test_wait_gives_up_at_the_deadline_with_the_current_progress(monkeypatch):
    _fake_task(monkeypatch, finish_after=999)
    monkeypatch.setattr(mcp_server, "TASK_WAIT_MAX", 0.3)       # wait 被夹到上限
    t = time.monotonic()
    res = _call("get_task_status", {"task_id": "t1", "wait": 60})
    assert res["status"] == "running" and 0.25 < time.monotonic() - t < 5


def test_no_wait_and_no_task_id_do_not_poll(monkeypatch):
    calls = _fake_task(monkeypatch, finish_after=999)
    _call("get_task_status", {"task_id": "t1"})
    assert len(calls) == 1                                      # 默认 wait=0：老行为
    monkeypatch.setattr(usecases, "task_status", lambda task_id=None: [])
    assert asyncio.run(mcp_server.server.call_tool("get_task_status", {"wait": 5})).content == []


def test_a_waiting_call_does_not_block_other_tools(monkeypatch):
    _fake_task(monkeypatch, finish_after=1.0)

    async def both():
        t = time.monotonic()
        slow = asyncio.create_task(mcp_server.server.call_tool(
            "get_task_status", {"task_id": "t1", "wait": 30}))
        await asyncio.sleep(0.05)
        await mcp_server.server.call_tool("get_task_status", {"task_id": "t2"})
        quick = time.monotonic() - t
        await slow
        return quick

    assert asyncio.run(both()) < 0.8


def test_wait_against_a_real_export_task(wx):
    """真实的导出任务：一次 wait 等到 done，不必轮询。"""
    wx([(i, T0 + i * 60, f"第{i}条") for i in range(1, 30)])
    tid = _call("export_full_history", {"account": ACCT})["task_id"]
    res = _call("get_task_status", {"task_id": tid, "wait": 30})
    assert res["status"] == "done" and res["rows"] == 29


# ---------- 体积预算：search_history / semantic_search ----------

def test_search_history_over_budget_keeps_the_newest_hits_and_pages_with_before_ts(wx, monkeypatch):
    wx([(i, T0 + i * 60, "预算" + "长" * 400) for i in range(1, 11)])
    export.run_export(ACCT)
    monkeypatch.setattr(mcp_server, "OUTPUT_TOKEN_BUDGET", 1500)
    seen, before, pages = [], None, 0
    for _ in range(10):
        pages += 1
        res = _call("search_history", {"query": "预算", "account": ACCT, "before_ts": before})
        seen += [h["ts"] for h in res["hits"]]
        assert res["total"] == len(res["hits"])
        if not res["truncated"]:
            assert "next_before_ts" not in res and "hint" not in res
            break
        assert 0 < len(res["hits"]) < 10 and "before_ts=next_before_ts" in res["hint"]
        before = res["next_before_ts"]
    assert pages > 1                                            # 确实截断过、续搜过
    assert seen == [T0 + i * 60 for i in range(10, 0, -1)]      # 从新到旧，不重不漏


def test_search_history_before_ts_alone_filters(wx):
    wx([(i, T0 + i * 60, f"过滤{i}") for i in range(1, 6)])
    export.run_export(ACCT)
    res = _call("search_history", {"query": "过滤", "account": ACCT, "before_ts": T0 + 180})
    assert [h["ts"] for h in res["hits"]] == [T0 + 120, T0 + 60] and res["truncated"] is False


def test_semantic_search_over_budget_says_how_many_were_left_out(monkeypatch):
    hits = [{"msg_key": f"a|b|c|{i}|{T0}", "ts": T0, "content": "长" * 400, "distance": i / 10}
            for i in range(8)]
    monkeypatch.setattr(usecases, "semantic_search",
                        lambda *a, **k: {"hits": [dict(h) for h in hits], "total": 8, "degraded": []})
    monkeypatch.setattr(mcp_server, "OUTPUT_TOKEN_BUDGET", 1000)
    res = _call("semantic_search", {"query": "x"})
    kept = len(res["hits"])
    assert res["truncated"] is True and 0 < kept < 8 and res["omitted"] == 8 - kept
    assert [h["distance"] for h in res["hits"]] == [i / 10 for i in range(kept)]   # 最相近的留下
    assert res["next_offset"] == kept and res["total"] == kept and "offset=next_offset" in res["hint"]
    assert _call("semantic_search", {"query": "x", "offset": 30})["next_offset"] == 30 + kept


def test_semantic_offset_is_passed_through_and_bounded(monkeypatch):
    seen, real = {}, usecases.semantic_search
    monkeypatch.setattr(usecases, "semantic_search",
                        lambda *a, **k: seen.update(k) or {"hits": [], "total": 0, "degraded": []})
    _call("semantic_search", {"query": "x", "offset": 20})
    assert seen["offset"] == 20
    monkeypatch.setattr(usecases, "semantic_search", real)     # 别用 undo()：会连 conftest 的隔离一起撤掉
    res = _call("semantic_search", {"query": "x", "offset": 5000})
    assert "offset" in res["error"]                             # 超出 KNN 能取到的范围：明说，不静默夹取


def test_full_text_tools_mark_their_results_untrusted(wx, monkeypatch):
    """返回完整正文的三个工具与 search_messages / get_message_context 一样在顶层标 untrusted。"""
    wx([(1, T0 + 60, "标记")])
    export.run_export(ACCT)
    assert _call("get_recent_messages", {"contact": "张三", "account": ACCT})["untrusted"] is True
    assert _call("search_history", {"query": "标记", "account": ACCT})["untrusted"] is True
    monkeypatch.setattr(usecases, "semantic_search",
                        lambda *a, **k: {"hits": [], "total": 0, "degraded": []})
    assert _call("semantic_search", {"query": "x"})["untrusted"] is True


def test_wait_on_an_unknown_task_errors_at_once():
    t = time.monotonic()
    res = _call("get_task_status", {"task_id": "nope", "wait": 30})
    assert "未知任务" in res["error"] and time.monotonic() - t < 5


def test_get_message_media_lists_keys_not_in_the_index(wx):
    wx([(1, T0 + 60, "一"), (2, T0 + 120, "二")])
    export.run_export(ACCT)
    first = _call("get_recent_messages", {"contact": "张三", "account": ACCT})["messages"][0]
    bogus = first["msg_key"].split(":")[0] + f":999:{T0}"
    res = _call("get_message_media", {"contact": "张三", "account": ACCT,
                                      "keys": f"{first['msg_key']},{bogus},{bogus}"})
    assert [m["msg_key"] for m in res["messages"]] == [first["msg_key"]]
    assert res["unanswered"] == [bogus]
    assert "unanswered" not in _call("get_message_media", {"contact": "张三", "account": ACCT})


def test_token_estimate_weights_by_character_class():
    """标定见 MCP_EFFICIENCY.md §9.4：中文 ≈1.1、英文 ≈0.4、数字 0.75、emoji 2.6 token/字符。"""
    est = mcp_server._est_tokens
    assert est("") == 0 and est("a") == 1                     # 不足 1 个也按 1 个（向上取整）
    assert est("会议" * 100) == 220
    assert est("ab" * 100) == 80 and est("12" * 100) == 150
    assert est("\U0001F600" * 10) == 26                        # BMP 以外按一个字符计，不按两个代理项


def test_budget_also_caps_characters(monkeypatch):
    """纯 ASCII 正文按 token 算还没到预算，字符数先到：照样截断（Claude Code 另有 5 万字符上限）。"""
    items = [{"content": "a" * 1000} for _ in range(10)]
    monkeypatch.setattr(mcp_server, "OUTPUT_TOKEN_BUDGET", 10**6)
    monkeypatch.setattr(mcp_server, "OUTPUT_CHAR_BUDGET", 3500)
    assert mcp_server._fit_budget(items, newest_first=True) == 3


def test_key_warm_up_leaves_nothing_to_derive_on_the_first_call(wx, monkeypatch):
    """预派生之后，首个读微信库的调用不再跑 PBKDF2；只读 salt，不快照（tmp 下不留东西）。"""
    from agent_wechat import decrypt, keys
    wx([(1, T0 + 60, "预热")])
    decrypt._derive_cached.cache_clear()
    usecases.warm_up(ACCT)
    derived = []
    real = keys.derive_key
    monkeypatch.setattr(keys, "derive_key", lambda *a: derived.append(1) or real(*a))
    assert _call("get_recent_messages", {"contact": "张三", "account": ACCT})["messages"]
    assert derived == []
