"""MCP 适配层错误契约：任何工具都不得把异常抛到传输层。

关键护栏：core 若抛 SystemExit（非 Exception 子类），SDK 的 `except Exception` 兜底
接不住，会杀死常驻 stdio server——所以这里逐个工具**直接调用函数**并断言
「返回含 error 的 dict 且不抛任何 BaseException」。
"""
import asyncio
import inspect
import json
import time
from types import SimpleNamespace

import pytest

from agent_wechat import (account, errors, export, mcp_server, paths, reader, tasks,
                          usecases)

BOGUS = "wxid_does_not_exist_0000"


@pytest.fixture(autouse=True)
def _no_real_accounts(monkeypatch):
    """不依赖开发机上的真实微信账号目录，保证「不存在的 account」确定不存在。"""
    monkeypatch.setattr(paths, "account_dirs", lambda: [])


def _call(fn, **kwargs):
    """调用工具函数；任何 BaseException（含 SystemExit）都判失败。"""
    try:
        return fn(**kwargs)
    except BaseException as e:  # noqa: BLE001 — 逃逸的异常正是被测对象
        pytest.fail(f"{fn.__name__} 抛出了 {type(e).__name__}: {e}")


@pytest.mark.parametrize("name,kwargs", [
    ("list_contacts", {}),
    ("get_recent_messages", {"contact": "张三"}),
    ("export_full_history", {}),
    ("search_history", {"query": "会议"}),
    ("semantic_search", {"query": "爬山"}),
])
def test_tools_with_missing_account_return_error_not_raise(name, kwargs):
    """经 _acct 的 5 个工具：account 不存在时必须返回 error 信封，零异常逃逸。"""
    res = _call(getattr(mcp_server, name), account=BOGUS, **kwargs)
    assert isinstance(res, dict) and res.get("error"), res
    assert "账号目录不存在" in res["error"]


@pytest.mark.parametrize("name,args", [
    ("list_contacts", {}),
    ("get_recent_messages", {"contact": "张三"}),
    ("export_full_history", {}),
    ("search_history", {"query": "会议"}),
    ("semantic_search", {"query": "爬山"}),
])
def test_errors_reach_the_agent_through_call_tool(name, args):
    """端到端：经 SDK 的 `call_tool` 也必须拿到中文信封、is_error=False。

    这条守卫「返回类型注解与错误信封不兼容」：SDK 会按 return annotation 校验工具
    返回值，形状不符会退化成 UnexpectedToolError——中文文案丢失、agent 只看到
    "Error executing tool <name>"。直接调用函数测不出这一层（list_contacts 即踩过）。
    """
    r = asyncio.run(mcp_server.server.call_tool(name, {**args, "account": BOGUS}))
    assert r.is_error is False
    payload = json.loads(r.content[0].text)
    assert "账号目录不存在" in payload["error"]


def test_contact_miss_surfaces_as_documented_envelope(monkeypatch):
    """未命中经 @_guard 翻回信封——对外形状与旧的手写 dict 返回一字不差。

    消歧/未命中的策略本身在 usecases 层（见 tests/test_usecases.py），这里只钉
    MCP 出口的信封形状。
    """
    class _StubReader:
        def __init__(self, acct):
            pass

        def resolve_one(self, contact):
            return None, []

    monkeypatch.setattr(account, "resolve_account", lambda a: "acct")
    monkeypatch.setattr(reader, "MessageReader", _StubReader)
    res = _call(mcp_server.get_recent_messages, contact="张三")
    assert res == {"error": "找不到联系人「张三」，请用 list_contacts 查询"}


def test_guard_translates_project_error_with_hint():
    @mcp_server._guard
    def f():
        raise errors.KeyNotFound("无 passphrase 缓存", hint="先运行 bootstrap")
    assert f() == {"error": "无 passphrase 缓存", "hint": "先运行 bootstrap"}


def test_guard_translates_project_error_without_hint():
    @mcp_server._guard
    def f():
        raise errors.DataNotFound("/x 不存在")          # 无 hint 的项目错误
    assert f() == {"error": "/x 不存在"}


def test_guard_appends_entry_specific_suffix_for_contact_miss():
    """未命中的下一步指引是入口专属的：core 只给中立文案，MCP 出口补 list_contacts。

    与 CLI 出口补 `contacts` 成对——同一个 core 异常，两个入口各自的下一步不同。
    """
    @mcp_server._guard
    def f():
        raise errors.ContactNotFound("找不到联系人「张三」")
    assert f() == {"error": "找不到联系人「张三」，请用 list_contacts 查询"}


def test_guard_wraps_unexpected_exception():
    @mcp_server._guard
    def f():
        raise ValueError("fastembed 炸了")
    res = f()
    assert res["error"].startswith("内部错误") and "fastembed 炸了" in res["error"]


def test_every_registered_tool_is_guarded():
    """防止将来新增工具漏加 @_guard（functools.wraps 会留下 __wrapped__）。"""
    for name in ("get_account_status", "list_contacts", "get_recent_messages",
                 "export_full_history", "get_task_status", "search_history",
                 "semantic_search", "build_vector_index"):
        assert getattr(mcp_server, name).__wrapped__ is not None, f"{name} 缺 @_guard"


def test_build_vector_index_wire_params_unchanged():
    """wire 契约冻结：core 侧把 account 改成复数 accounts 后，工具参数名与默认值一字不动。"""
    sig = inspect.signature(mcp_server.build_vector_index)
    assert list(sig.parameters) == ["rebuild", "account"]
    assert sig.parameters["rebuild"].default is False
    assert sig.parameters["account"].default is None


def test_export_full_history_need_confirm_survives_call_tool(monkeypatch):
    """改走用例层的 export_history 后，歧义信封经 SDK 仍原样到达（不创建任务）。"""
    class _StubReader:
        def __init__(self, acct):
            pass

        def resolve_one(self, contact):
            return None, [{"username": "wxid_a", "display": "张三", "kind": "direct"},
                          {"username": "888@chatroom", "display": "张三（群）", "kind": "group"}]

    monkeypatch.setattr(account, "resolve_account", lambda a: "acct")
    monkeypatch.setattr(reader, "MessageReader", _StubReader)
    r = asyncio.run(mcp_server.server.call_tool("export_full_history", {"contact": "张三"}))
    assert r.is_error is False
    assert json.loads(r.content[0].text) == {"need_confirm": [
        {"username": "wxid_a", "display": "张三", "kind": "direct"},
        {"username": "888@chatroom", "display": "张三（群）", "kind": "group"}]}


def test_unknown_task_id_envelope_unchanged(monkeypatch, tmp_path):
    """未知 id 从返回 dict 改成抛 TaskNotFound 后，对外形状仍逐字节相同。"""
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(tasks, "_loaded_path", None)
    assert _call(mcp_server.get_task_status, task_id="nope") == {"error": "未知任务 nope"}


def test_export_full_history_registers_task_with_same_envelope(monkeypatch):
    """删掉 export.start_task 转发后，登记参数与返回信封都不变。"""
    seen = {}
    monkeypatch.setattr(usecases, "export_plan",
                        lambda **kw: usecases.ExportPlan("acct", "chat1", True))
    monkeypatch.setattr(usecases.tasks, "start",
                        lambda kind, runner, **fields: seen.update(
                            kind=kind, fields=fields) or "tid123")

    assert _call(mcp_server.export_full_history, contact="张三",
                 full=True) == {"task_id": "tid123", "status": "running"}
    assert seen == {"kind": "export", "fields": {"account": "acct", "chat_id": "chat1"}}


def test_export_task_is_visible_through_get_task_status(monkeypatch, tmp_path):
    """用例层登记 → tasks 持久化 → MCP 查询：删掉转发后的全链路仍通。"""
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(tasks, "_loaded_path", None)
    monkeypatch.setattr(account, "resolve_account", lambda a: "acct")
    monkeypatch.setattr(export, "run_export", lambda acct, chat_id=None, full=False: 3)

    res = _call(mcp_server.export_full_history)
    for _ in range(200):                      # 后台线程，等待完成
        st = _call(mcp_server.get_task_status, task_id=res["task_id"])
        if st["status"] != "running":
            break
        time.sleep(0.01)
    assert st["status"] == "done" and st["rows"] == 3 and st["kind"] == "export"


def test_msg_dict_marks_self_as_me():
    """`cli.py:149` 有 is_self 判断而 MCP 没有——自己发的**私聊**消息（sender_id 是自己
    ≠ chat_id，名册 join 不上）因此在 MCP 里显示成一串 wxid。"""
    m = SimpleNamespace(ts=1, sender_name=None, sender_id="wxid_me", is_self=True,
                        type_name="文本", content="在", media_hint=None)
    assert mcp_server._msg_dict(m)["sender"] == "我"


def test_msg_dict_falls_back_to_sender_id():
    m = SimpleNamespace(ts=1, sender_name=None, sender_id="wxid_x", is_self=False,
                        type_name="文本", content="在", media_hint=None)
    assert mcp_server._msg_dict(m)["sender"] == "wxid_x"
