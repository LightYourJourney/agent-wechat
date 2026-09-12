"""MCP 适配层错误契约：任何工具都不得把异常抛到传输层。

关键护栏：core 若抛 SystemExit（非 Exception 子类），SDK 的 `except Exception` 兜底
接不住，会杀死常驻 stdio server——所以这里逐个工具**直接调用函数**并断言
「返回含 error 的 dict 且不抛任何 BaseException」。
"""
import asyncio
import inspect
import json

import pytest

from agent_wechat import account, errors, mcp_server, paths, reader

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
        raise errors.ContactNotFound("找不到联系人")
    assert f() == {"error": "找不到联系人"}


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
