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

from agent_wechat import (account, errors, export, media, mcp_server, paths, reader,
                          tasks, usecases)
# 合成微信目录（真实 sqlcipher 解密链路；路径全部指向 tmp_path，绝不碰真实数据）
from test_reader_export_pipeline import ACCT, drift_tree, env, wechat  # noqa: F401

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
    ("export_chat_media", {"contact": "张三"}),
    ("export_moments", {}),
    ("get_message_media", {"contact": "张三"}),
])
def test_tools_with_missing_account_return_error_not_raise(name, kwargs):
    """经 `usecases` 账号解析的这 8 个工具：account 不存在时必须返回 error 信封，零异常逃逸。"""
    res = _call(getattr(mcp_server, name), account=BOGUS, **kwargs)
    assert isinstance(res, dict) and res.get("error"), res
    assert "账号目录不存在" in res["error"]


@pytest.mark.parametrize("name,args", [
    ("list_contacts", {}),
    ("get_recent_messages", {"contact": "张三"}),
    ("export_full_history", {}),
    ("search_history", {"query": "会议"}),
    ("semantic_search", {"query": "爬山"}),
    ("export_chat_media", {"contact": "张三"}),
    ("export_moments", {}),
    ("get_message_media", {"contact": "张三"}),
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
            self._drift = []         # 真实 MessageReader 的状态：collect_degradations 会读

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
                 "semantic_search", "build_vector_index", "export_chat_media",
                 "export_moments", "get_message_media"):
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
            self._drift = []         # 真实 MessageReader 的状态：collect_degradations 会读

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
                        type_name="文本", content="在", media_hint=None, msg_key="k")
    assert mcp_server._msg_dict(m)["sender"] == "我"


def test_msg_dict_falls_back_to_sender_id():
    m = SimpleNamespace(ts=1, sender_name=None, sender_id="wxid_x", is_self=False,
                        type_name="文本", content="在", media_hint=None, msg_key="k")
    assert mcp_server._msg_dict(m)["sender"] == "wxid_x"


# ---------- T6：降级的信封出口（spec §7） ----------

def test_get_recent_messages_envelope_carries_degraded_key(monkeypatch):
    """MCP 的信封加一个顶层键——加键向后兼容（不认识的 agent 会忽略它），
    但要同步 TOOLS.md（T7）。"""
    from agent_wechat import usecases
    monkeypatch.setattr(usecases, "collect_degradations",
                        lambda r: (usecases.Degradation(
                            "roster", "contact/contact.db", "chat_room", (),
                            "显示名暂不可用"),))
    monkeypatch.setattr(usecases, "recent_messages",
                        lambda *a, **k: usecases.RecentMessages(
                            chat_id="x", display="d", messages=[],
                            degraded=usecases.collect_degradations(None)))
    res = mcp_server.get_recent_messages.__wrapped__(contact="x")
    assert res["degraded"] and res["degraded"][0]["effect"] == "显示名暂不可用"


def test_search_history_envelope_carries_degraded_key(env, tmp_path, monkeypatch):
    """Ruling 13：`_refresh_roster` 吞掉漂移，于是 search 的 `sender_name` **静默**回落成
    wxid——信封必须把这条降级带出去（与 get_recent_messages 同一个键、同一个形状），
    否则「显示名变少了」对 agent 不可见。走真实链路（漂移的 contact.db）。"""
    root = tmp_path / "wx"
    drift_tree(root, ACCT, drop_table="chat_room")
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    # 本模块的 autouse 夹具把 account_dirs 钉成空（「不存在的账号确定不存在」），
    # 这条要解析合成账号，故覆盖回真实扫描（后打的 patch 生效在前，teardown 逆序）。
    monkeypatch.setattr(paths, "account_dirs", lambda: [ACCT])
    res = mcp_server.search_history(query="会议记录", account=ACCT)
    assert "degraded" in res, res
    assert [d["capability"] for d in res["degraded"]] == ["roster"]
    assert "显示名" in res["degraded"][0]["effect"]


# ---------- T7：export_chat_media 的信封（spec §4.6） ----------

def test_export_chat_media_envelope(monkeypatch):
    """正常路径：counts 三类 + degraded 恒存在 + truncated；顺带钉参数编组
    （kinds/months 拆列表、limit/account 透传）。media_export 打桩：不打真库。"""
    seen: dict = {}

    def fake_media_export(contact, **kw):
        seen.update(kw, contact=contact)
        return usecases.MediaExportResult(
            chat_id="wxid_target", out_dir="/tmp/out/media",
            report=media.MediaReport(
                image=media.Counts(ok=3, failed=1, missing=2),
                voice=media.Counts(ok=4, raw_only=1),
                video=media.Counts(ok=1, thumb=2, missing=5),
                degraded=["pilk 不可用：语音导出为原始 .slk"], truncated=True))

    monkeypatch.setattr(usecases, "media_export", fake_media_export)
    res = _call(mcp_server.export_chat_media, contact="张三", kinds="image,voice",
                months="2026-08, 2026-09", limit=10, account="acct")
    assert res == {"chat_id": "wxid_target", "out_dir": "/tmp/out/media",
                   "counts": {"image": {"ok": 3, "failed": 1, "missing": 2},
                              "voice": {"ok": 4, "failed": 0, "raw_only": 1},
                              "video": {"ok": 1, "thumb": 2, "missing": 5}},
                   "degraded": ["pilk 不可用：语音导出为原始 .slk"], "truncated": True}
    assert seen == {"contact": "张三", "kinds": ["image", "voice"],
                    "months": ["2026-08", "2026-09"], "limit": 10, "account": "acct"}


def test_export_chat_media_defaults_split_cleanly(monkeypatch):
    """months 省略 → None（不得对 None 调 split）；kinds 默认拆三类；wire 默认
    limit=500。未降级/未截断时 degraded 恒存在（空数组）、truncated=false。"""
    seen: dict = {}

    def fake_media_export(contact, **kw):
        seen.update(kw, contact=contact)
        return usecases.MediaExportResult(chat_id="wxid_target", out_dir="/tmp/out/media",
                                          report=media.MediaReport())

    monkeypatch.setattr(usecases, "media_export", fake_media_export)
    res = _call(mcp_server.export_chat_media, contact="张三")
    assert seen == {"contact": "张三", "kinds": ["image", "voice", "video"],
                    "months": None, "limit": 500, "account": None}
    assert res["counts"] == {"image": {"ok": 0, "failed": 0, "missing": 0},
                             "voice": {"ok": 0, "failed": 0, "raw_only": 0},
                             "video": {"ok": 0, "thumb": 0, "missing": 0}}
    assert res["degraded"] == [] and res["truncated"] is False


def test_export_chat_media_need_confirm_passthrough(monkeypatch):
    """歧义原样透传成 need_confirm 信封（与 recent/search/export 同形），不猜。"""
    monkeypatch.setattr(usecases, "media_export", lambda *a, **k: usecases.NeedConfirm(
        [usecases.Candidate("wxid_a", "张三01", "direct"),
         usecases.Candidate("888@chatroom", "张三（群）", "group")]))
    res = _call(mcp_server.export_chat_media, contact="张三")
    assert res == {"need_confirm": [
        {"username": "wxid_a", "display": "张三01", "kind": "direct"},
        {"username": "888@chatroom", "display": "张三（群）", "kind": "group"}]}


# ---------- T6：消息信封加稳定标识 msg_key（spec §4.3） ----------

def test_msg_dict_carries_msg_key():
    """信封必须带稳定标识 —— 没有它，「这条消息的媒体是什么」问不出来。"""
    m = reader.Message(chat_id="wxid_x", shard="message_0.db", local_id=42, ts=1,
                       msg_type=1, type_name="文本", sender_id="wxid_x",
                       sender_name=None, is_self=False, content="hi", media_hint=None,
                       msg_key="acct|deadbeef|message_0.db|42")
    assert mcp_server._msg_dict(m)["msg_key"] == "acct|deadbeef|message_0.db|42"


# ---------- T7：get_message_media 的信封（spec §4.5） ----------

def test_tool_count_is_eleven():
    """工具数变了，README/TOOLS.md 的数法必须跟着变 —— 这条断言守的就是「别忘改文档」。"""
    tools = asyncio.run(mcp_server.server.list_tools())
    assert len(tools) == 11
    assert "get_message_media" in {t.name for t in tools}


def test_get_message_media_envelope_shape(monkeypatch):
    """信封形状：chat_id / out_dir / counts / truncated / by_kind / messages / degraded；
    messages 里带 msg_key。

    替换用例层而不是造真数据：本任务要验的是**信封**，四态判定已由 Task 4 单独覆盖。
    """
    from agent_wechat import usecases

    def _fake(*a, **k):
        return usecases.MessageMediaResult(
            chat_id="wxid_x", out_dir="/tmp/m",
            states=[usecases.MediaRefState(msg_key="acct|deadbeef|message_0.db|1",
                                           ts=1, kind="image", status="exported",
                                           path="/tmp/m/x.jpg")],
            counts={"none": 0, "exported": 1, "exportable": 0, "gone": 0},
            degraded=())
    monkeypatch.setattr(usecases, "message_media", _fake)
    res = mcp_server.get_message_media(contact="张三")
    assert set(res) == {"chat_id", "out_dir", "counts", "truncated", "by_kind",
                        "messages", "degraded"}
    assert res["truncated"] is False
    assert res["by_kind"] == {"image": {"exported": 1}}
    assert res["messages"] == [{"msg_key": "acct|deadbeef|message_0.db|1", "time": 1,
                                "kind": "image", "status": "exported",
                                "path": "/tmp/m/x.jpg"}]


def test_get_message_media_by_kind_breakdown(monkeypatch):
    """by_kind 按 kind 再拆一次四态。夹具含**两种以上 kind 且状态不同**——池化的 counts
    把「语音全部可导出」与「图片视频全 gone」算成一个数字，读起来像「这会话媒体大多没了」，
    而按 kind 拆开后正相反。kind=None 的 none 态只进池化 counts，不进任何桶（它没有 kind）。
    """
    from agent_wechat import usecases

    def _state(key, kind, status):
        return usecases.MediaRefState(msg_key=key, ts=1, kind=kind, status=status, path=None)

    def _fake(*a, **k):
        return usecases.MessageMediaResult(
            chat_id="wxid_x", out_dir="/tmp/m",
            states=[_state("k1", "voice", "exportable"), _state("k2", "voice", "exportable"),
                    _state("k3", "image", "gone"), _state("k4", "image", "exported"),
                    _state("k5", "video", "gone"), _state("k6", None, "none")],
            counts={"none": 1, "exported": 1, "exportable": 2, "gone": 2},
            truncated=True, degraded=())
    monkeypatch.setattr(usecases, "message_media", _fake)
    res = mcp_server.get_message_media(contact="张三")
    assert res["by_kind"] == {"voice": {"exportable": 2},
                              "image": {"gone": 1, "exported": 1},
                              "video": {"gone": 1}}
    assert res["counts"] == {"none": 1, "exported": 1, "exportable": 2, "gone": 2}
    assert res["truncated"] is True


def test_get_message_media_degraded_is_converted_to_dict(monkeypatch):
    """`degraded` 的 dict 转换归适配层（用例层给的元素是 `Degradation` 对象而不是 dict），
    对外形状与 get_recent_messages 同一个键、同一个形状。"""
    from agent_wechat import usecases
    monkeypatch.setattr(usecases, "message_media", lambda *a, **k: usecases.MessageMediaResult(
        chat_id="wxid_x", out_dir="/tmp/m", states=[],
        counts={"none": 0, "exported": 0, "exportable": 0, "gone": 0},
        degraded=(usecases.Degradation("msg_media", "message/media_*.db", "VoiceInfo", (),
                                       "语音库读不到：语音消息一律按「本体不在本机」呈现"),)))
    res = mcp_server.get_message_media(contact="张三")
    assert res["degraded"] == [
        {"capability": "msg_media",
         "effect": "语音库读不到：语音消息一律按「本体不在本机」呈现"}]


def test_get_message_media_parses_comma_separated_keys(monkeypatch):
    """keys 是逗号分隔字符串（与 kinds/months 同约定）；msg_key 内的 `|` 与 `,` 不冲突。"""
    from agent_wechat import usecases
    seen = {}

    def _fake(contact, **kw):
        seen.update(kw)
        return usecases.MessageMediaResult(chat_id="wxid_x", out_dir="/tmp/m",
                                           states=[], counts={"none": 0, "exported": 0,
                                                              "exportable": 0, "gone": 0})
    monkeypatch.setattr(usecases, "message_media", _fake)
    mcp_server.get_message_media(contact="张三", keys="a|b|c|1, a|b|c|2")
    assert seen["keys"] == ["a|b|c|1", "a|b|c|2"]
