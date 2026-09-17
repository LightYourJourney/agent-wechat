"""MCP 适配层错误契约：任何工具都不得把异常抛到传输层。

关键护栏：core 若抛 SystemExit（非 Exception 子类），SDK 的 `except Exception` 兜底
接不住，会杀死常驻 stdio server——所以这里逐个工具**直接调用函数**并断言
「返回含 error 的 dict 且不抛任何 BaseException」。
"""
import asyncio
import inspect
import json
import pathlib
import re
import sqlite3
import time
import zlib
from types import SimpleNamespace

import pytest

from agent_wechat import (account, errors, export, media, messages, mcp_server, paths,
                          reader, store, tasks, usecases)
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
    ("export_chat", {"contact": "张三"}),
    ("get_message_parts", {"contact": "张三", "keys": "acct|deadbeef|message_0.db|1"}),
])
def test_tools_with_missing_account_return_error_not_raise(name, kwargs):
    """经 `usecases` 账号解析的这 10 个工具：account 不存在时必须返回 error 信封，零异常逃逸。"""
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
    ("export_chat", {"contact": "张三"}),
    ("get_message_parts", {"contact": "张三", "keys": "acct|deadbeef|message_0.db|1"}),
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
                 "export_moments", "get_message_media",
                 "export_chat", "get_message_parts"):
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
                # truncated 只在本会导出的条目**真被上限挡下**时才置位，故夹具必须给一个
                # 真实的 remaining（`truncated=True` + `remaining=0` 在生产里不可能出现）；
                # skipped 也给非 0，否则新键只被断言了「存在」而没被断言「真的透出来」。
                image=media.Counts(ok=3, failed=1, missing=2, skipped=3, remaining=4),
                voice=media.Counts(ok=4, raw_only=1, remaining=2),
                video=media.Counts(ok=1, thumb=2, missing=5),
                degraded=["pilk 不可用：语音导出为原始 .slk"], truncated=True))

    monkeypatch.setattr(usecases, "media_export", fake_media_export)
    res = _call(mcp_server.export_chat_media, contact="张三", kinds="image,voice",
                months="2026-08, 2026-09", limit=10, account="acct")
    assert res == {"chat_id": "wxid_target", "out_dir": "/tmp/out/media",
                   "counts": {"image": {"ok": 3, "failed": 1, "missing": 2,
                                        "skipped": 3, "remaining": 4},
                              "voice": {"ok": 4, "failed": 0, "raw_only": 1,
                                        "skipped": 0, "remaining": 2},
                              "video": {"ok": 1, "thumb": 2, "missing": 5,
                                        "skipped": 0, "remaining": 0}},
                   "degraded": ["pilk 不可用：语音导出为原始 .slk"], "truncated": True}
    assert seen == {"contact": "张三", "kinds": ["image", "voice"],
                    "months": ["2026-08", "2026-09"], "limit": 10, "force": False,
                    "account": "acct"}


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
                    "months": None, "limit": 500, "force": False, "account": None}
    assert res["counts"] == {"image": {"ok": 0, "failed": 0, "missing": 0,
                                       "skipped": 0, "remaining": 0},
                             "voice": {"ok": 0, "failed": 0, "raw_only": 0,
                                       "skipped": 0, "remaining": 0},
                             "video": {"ok": 0, "thumb": 0, "missing": 0,
                                       "skipped": 0, "remaining": 0}}
    assert res["degraded"] == [] and res["truncated"] is False


def test_export_chat_media_force_is_forwarded(monkeypatch):
    """force=true 必须透到用例层（同 CLI 那条：写死 force=False 时既有测试全绿）。"""
    seen: dict = {}

    def fake_media_export(contact, **kw):
        seen.update(kw, contact=contact)
        return usecases.MediaExportResult(chat_id="wxid_target", out_dir="/tmp/out/media",
                                          report=media.MediaReport())

    monkeypatch.setattr(usecases, "media_export", fake_media_export)
    _call(mcp_server.export_chat_media, contact="张三", force=True)
    assert seen["force"] is True


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

def test_tool_count_is_thirteen():          # 原 test_tool_count_is_eleven 改名
    """工具数变了，README/TOOLS.md 的数法必须跟着变 —— 这条断言守的就是「别忘改文档」。

    两条断言各守一件事：注册表里的工具数（拿掉一个即红）与**活文档**里逐条计数
    （把任一处退回「11 个工具」即红）。只扫活文档：CHANGELOG 的「5 个工具」「7 个工具」
    是发布历史，永远不该被改成 11；TOOLS.md 末尾的「≤10 个任务」是**任务**数，故模式
    要求数字后面紧跟「工具」或「MCP 工具」。
    """
    tools = asyncio.run(mcp_server.server.list_tools())
    assert len(tools) == 13
    assert {"export_chat", "get_message_parts"} <= {t.name for t in tools}
    root = pathlib.Path(__file__).resolve().parent.parent
    for rel in ("README.md", "docs/TOOLS.md", "docs/ARCHITECTURE.md"):
        text = (root / rel).read_text(encoding="utf-8")
        for hit in re.finditer(r"(\d+)\s*(?:个\s*)?(?:MCP\s*)?工具", text):
            assert hit.group(1) == str(len(tools)), \
                f"{rel}：「{hit.group(0)}」应随工具数改成 {len(tools)} 个工具"


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
            by_kind={"image": {"exported": 1}},      # 推导已归用例层：夹具自己给
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
            by_kind={"voice": {"exportable": 2}, "image": {"gone": 1, "exported": 1},
                     "video": {"gone": 1}},          # 推导已归用例层：夹具自己给
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
    # 空 states 会走到 index_ready()（索引为空的 hint 那一步）——不桩就会去开真实
    # ~/.agent-wechat/index.db。这条测的是 degraded 的转换，索引状态与它无关。
    monkeypatch.setattr(usecases, "index_ready", lambda: True)
    monkeypatch.setattr(usecases, "message_media", lambda *a, **k: usecases.MessageMediaResult(
        chat_id="wxid_x", out_dir="/tmp/m", states=[],
        counts={"none": 0, "exported": 0, "exportable": 0, "gone": 0},
        degraded=(usecases.Degradation("msg_media", "message/media_*.db", "VoiceInfo", (),
                                       "语音库读不到：尚未导出的语音消息一律按「本体不在本机」呈现"
                                       "（已导出的不受影响）"),)))
    res = mcp_server.get_message_media(contact="张三")
    assert res["degraded"] == [
        {"capability": "msg_media",
         "effect": "语音库读不到：尚未导出的语音消息一律按「本体不在本机」呈现（已导出的不受影响）"}]


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
    monkeypatch.setattr(usecases, "index_ready", lambda: True)   # 空 states：不桩会开真实索引
    mcp_server.get_message_media(contact="张三", keys="a|b|c|1, a|b|c|2")
    assert seen["keys"] == ["a|b|c|1", "a|b|c|2"]


def test_get_message_media_empty_index_carries_hint(env, monkeypatch):
    """索引为空时信封必须带 hint：否则那份「counts 全 0 + messages: []」与「这个会话没有
    媒体消息」逐字节相同（`store.connect` 还会按需建表，连报错都没有）。

    与 search_history 同一个约定、同一句文案；文案里是工具名 export_full_history，属于
    MCP 专属渲染，所以放在适配层。`env` 把 `store.DB_PATH` 指到一个全新的空索引库：
    这里走**真实**的 `usecases.index_ready()`，不碰真实数据。
    """
    from agent_wechat import usecases
    monkeypatch.setattr(usecases, "message_media", lambda *a, **k: usecases.MessageMediaResult(
        chat_id="wxid_x", out_dir="/tmp/m", states=[],
        counts={"none": 0, "exported": 0, "exportable": 0, "gone": 0}))
    res = mcp_server.get_message_media(contact="张三")
    assert res["hint"] == "本地索引为空：请先运行 export_full_history"
    # 答案本身不变：hint 是附加说明，不是错误信封
    assert res["counts"] == {"none": 0, "exported": 0, "exportable": 0, "gone": 0}
    assert "error" not in res


# ---------- T11：export_chat（第 12 个工具）的信封 ----------

def test_export_chat_envelope_has_paths_counts_and_no_body(monkeypatch):
    """信封 = {chat_id, out_dir, files, counts, truncated, unparsed, rejected, degraded}，
    **不回传正文**（沿用 export_chat_media 的既定原则：信封不回传大结果——正文在产物里）。
    顺带钉参数编组（months/format 拆列表、limit/out_dir/account 透传）。

    回归：把 `format` 拆列表那一步删掉（原样透传字符串），seen 与形式都红。
    """
    seen: dict = {}

    def fake_chat_export(contact, **kw):
        seen.update(kw, contact=contact)
        return usecases.ChatExportResult(
            chat_id="wxid_target", out_dir="/tmp/c/4f2a",
            files=("/tmp/c/4f2a/chat/index.html", "/tmp/c/4f2a/chat/index.md"),
            counts={"text": 3, "appmsg": 1}, truncated=True, unparsed=1, rejected=2,
            degraded=(usecases.Degradation(
                "msg_parts", "index.db", "msg_parts", (),
                "msg_parts 未回填，appmsg 会显示成 `[appmsg: 标题]`；"
                "运行 `agent-wechat appmsg rebuild` 补齐"),))

    monkeypatch.setattr(usecases, "chat_export", fake_chat_export)
    res = _call(mcp_server.export_chat, contact="张三", months="2026-08, 2026-09",
                limit=10, format="md, html", out_dir="/tmp/out", account="acct")
    assert res == {"chat_id": "wxid_target", "out_dir": "/tmp/c/4f2a",
                   "files": ["/tmp/c/4f2a/chat/index.html", "/tmp/c/4f2a/chat/index.md"],
                   "counts": {"text": 3, "appmsg": 1}, "truncated": True,
                   "unparsed": 1, "rejected": 2,
                   "degraded": [{"capability": "msg_parts",
                                 "effect": "msg_parts 未回填，appmsg 会显示成 `[appmsg: 标题]`；"
                                           "运行 `agent-wechat appmsg rebuild` 补齐"}]}
    assert seen == {"contact": "张三", "months": ["2026-08", "2026-09"], "limit": 10,
                    "formats": ("md", "html"), "out_dir": "/tmp/out", "account": "acct"}


def test_export_chat_defaults_and_wire_params_frozen(monkeypatch):
    """wire 契约冻结：参数名与默认值（`format` 默认 "html,md"、limit 500）。
    months 省略 → None（不得对 None 调 split）；format 拆成元组（chatview 只认元组/序列）；
    degraded 恒存在（空数组）。"""
    seen: dict = {}

    def fake_chat_export(contact, **kw):
        seen.update(kw, contact=contact)
        return usecases.ChatExportResult(chat_id="wxid_target", out_dir="/tmp/c/4f2a",
                                         files=(), counts={}, truncated=False,
                                         unparsed=0, rejected=0)

    monkeypatch.setattr(usecases, "chat_export", fake_chat_export)
    res = _call(mcp_server.export_chat, contact="张三")
    assert seen == {"contact": "张三", "months": None, "limit": 500,
                    "formats": ("html", "md"), "out_dir": None, "account": None}
    assert res["files"] == [] and res["degraded"] == [] and res["truncated"] is False
    sig = inspect.signature(mcp_server.export_chat)
    assert list(sig.parameters) == ["contact", "months", "limit", "format", "out_dir",
                                    "account"]
    assert sig.parameters["limit"].default == 500
    assert sig.parameters["format"].default == "html,md"


def test_export_chat_need_confirm_passthrough(monkeypatch):
    """歧义原样透传成 need_confirm 信封（与 recent/search/export 同形），不猜。"""
    monkeypatch.setattr(usecases, "chat_export", lambda *a, **k: usecases.NeedConfirm(
        [usecases.Candidate("wxid_a", "张三01", "direct"),
         usecases.Candidate("888@chatroom", "张三（群）", "group")]))
    res = _call(mcp_server.export_chat, contact="张三")
    assert res == {"need_confirm": [
        {"username": "wxid_a", "display": "张三01", "kind": "direct"},
        {"username": "888@chatroom", "display": "张三（群）", "kind": "group"}]}


# ---------- T11：get_message_parts（第 13 个工具）的信封 ----------

class _StubCtx:
    """替掉 `usecases.ChatContext`：一把桩供两处用——`appmsg rebuild` 夹具要的
    `reader.iter_parts_input`，以及工具本身要的 `account` + `resolve`。不碰真实数据。"""

    def __init__(self, *, account="acct", rows=(), chat_id=None, candidates=()):
        self.account = account
        self._rows = list(rows)
        self._chat_id = chat_id
        self._candidates = list(candidates)

    @property
    def reader(self):
        return SimpleNamespace(iter_parts_input=lambda: iter(self._rows))

    def resolve(self, contact):
        if self._candidates:
            return usecases.NeedConfirm([usecases.Candidate(*c) for c in self._candidates])
        return self._chat_id


def _backfill(rows, monkeypatch, *, account="acct", chat_id="wxid_friend",
              candidates=()):
    """种夹具：**走生产路径**（`usecases.appmsg_rebuild_parts`）把载荷写进索引库，
    形态（zlib 压缩的规范化 XML）与真机回填后的表同源——夹具形态不符是上周期最贵的一类
    缺陷。索引库由 conftest 的 autouse 闸指到 tmp_path，`store.open()` 走默认路径即可。
    """
    monkeypatch.setattr(usecases, "ChatContext",
                        lambda acct=None: _StubCtx(account=account, rows=rows,
                                                   chat_id=chat_id, candidates=candidates))
    usecases.appmsg_rebuild_parts(account=account)


QUOTE_XML = ('<msg><appmsg><title>回复</title><type>57</type><refermsg>'
             '<svrid>1001</svrid><displayname>李四</displayname><content>明天开会</content>'
             '<createtime>1789000000</createtime><fromusr>wxid_friend02</fromusr>'
             '</refermsg></appmsg></msg>')
LINK_XML = ('<msg><appmsg><title>一篇文章</title><type>5</type>'
            '<url>https://example.invalid/a</url><des>摘要</des>'
            '<sources><source>合成来源</source></sources></appmsg></msg>')
FILE_XML = ('<msg><appmsg><title>报表.xlsx</title><type>6</type><appattach>'
            '<fileext>xlsx</fileext><totallen>2048</totallen>'
            '<md5>0123456789abcdef0123456789abcdef</md5></appattach></appmsg></msg>')
TRANSFER_XML = ('<msg><appmsg><title>微信转账</title><type>2000</type><wcpayinfo>'
                '<paysubtype>1</paysubtype><feedesc>¥88.00</feedesc><pay_memo>午饭</pay_memo>'
                '<payer_username>wxid_friend02</payer_username>'
                '<receiver_username>wxid_t_a1b2</receiver_username>'
                '<transcationid>10000000000000000000000</transcationid>'
                '<transferid>t1</transferid><begintransfertime>1789000000</begintransfertime>'
                '<effectivedate>1789086400</effectivedate><invalidtime>1789600000</invalidtime>'
                '</wcpayinfo></appmsg></msg>')
MINIPROGRAM_XML = ('<msg><appmsg><title>某小程序</title><type>33</type>'
                   '<appid>wx0123456789abcdef</appid><pagepath>pages/index</pagepath>'
                   '</appmsg></msg>')


def _parts_fixture(monkeypatch):
    """一次种齐五个 kind（四个具名块各一 + 一个未具名建模的），返回 `(key, foreign)`。

    - `key(i)`：本会话（`wxid_friend`）的 msg_key。
    - `foreign`：**另一个会话**里一条**真实存在**的 appmsg 行（真的 `upsert` 进去）。
      **必须真种一行，不能只构造一个「别的会话的 msg_key」**：第一版就是这么写的，而那个 key
      在库里根本不存在 → 工具过滤不过滤都答不出它 →「删掉会话过滤」那条夹具照样全绿。
      **变异复验（M5 第一轮）才对出来**：夹具与现实形态不符时，绿是假的。
    第 1 条是**被引用的原文**（无 appmsg 载荷，只有 server_id），让引用那条能解出回指。
    """
    cm = messages.chat_md5("wxid_friend")
    key = lambda i: messages.row_key("acct", cm, "message_0.db", i)  # noqa: E731
    foreign = messages.row_key("acct", messages.chat_md5("wxid_someone_else"),
                               "message_0.db", 2)
    _backfill([(1001, key(1), None), (None, key(2), QUOTE_XML), (None, key(3), LINK_XML),
               (None, key(4), FILE_XML), (None, key(5), TRANSFER_XML),
               (None, key(6), MINIPROGRAM_XML), (None, foreign, LINK_XML)], monkeypatch)
    return key, foreign


def _seed_raw(monkeypatch, rows, *, account="acct", chat_id="wxid_friend"):
    """直接往隔离索引库写 `msg_parts` 行（**不走生产路径**）。

    解不开 / 被主动拒绝 / 字节坏掉的行**只能这样造**：`appmsg_rebuild_parts` 不落库解析失败
    的载荷（它只在成功分支 append）。真机上这类行来自「别人写进去的字节」——索引库被手工
    改动、半拷贝的 WAL 落下半页（`usecases.chat_export` 的注释记着同一条）。
    """
    monkeypatch.setattr(usecases, "ChatContext",
                        lambda acct=None: _StubCtx(account=account, chat_id=chat_id))
    with store.open() as con:                      # store.open 退出时提交
        store.upsert_msg_parts(con, [(k, 49, None, None, blob) for k, blob in rows])


def test_get_message_parts_envelope_shape(monkeypatch):
    """信封 = {parts: [...], degraded}，每条十个键、按 keys 顺序、**四个具名块按 kind 择一非空**。
    夹具五个 kind 各一：引用 57 / 链接 5 / 文件 6 / 转账 2000 / 小程序 33（未具名建模）。

    回归：删掉 `kind_name`、把 `fields` 换成非点分路径、少给任何一个具名块，这条都会红。
    """
    key, _foreign = _parts_fixture(monkeypatch)
    res = _call(mcp_server.get_message_parts, contact="张三",
                keys=",".join(key(i) for i in (2, 3, 4, 5, 6)))
    # 已回填：不带 hint（hint 只在未回填时给）；解不开/被拒两个计数恒存在（这里都是 0）
    assert set(res) == {"parts", "unparsed", "rejected", "degraded"}
    assert (res["unparsed"], res["rejected"]) == (0, 0)
    assert [p["msg_key"] for p in res["parts"]] == [key(i) for i in (2, 3, 4, 5, 6)]
    assert [p["kind"] for p in res["parts"]] == [57, 5, 6, 2000, 33]
    assert [p["kind_name"] for p in res["parts"]] == ["引用", "链接", "文件", "转账", "小程序"]
    for part, filled in zip(res["parts"], ["quote", "link", "file", "transfer", None]):
        assert set(part) == {"msg_key", "kind", "kind_name", "title", "fields", "quote",
                             "link", "file", "transfer", "ref_msg_key"}, part
        blocks = [b for b in ("quote", "link", "file", "transfer") if part[b] is not None]
        assert blocks == ([filled] if filled else []), part
    quote, link, file, transfer, mini = res["parts"]
    assert quote["title"] == "回复" and quote["ref_msg_key"] == key(1)
    # 字段名是 Python 侧的（`from_usr`），不是 XML 标签（`fromusr`）——具名块用 dataclass 带出
    assert quote["quote"] == {"svrid": 1001, "displayname": "李四", "content": "明天开会",
                              "create_time": 1789000000, "from_usr": "wxid_friend02"}
    # 链接的 url/des 是具名块的兄弟叶子：不得在字段表里重复出现（appmsg._NAMED_LEAVES）
    assert link["link"] == {"url": "https://example.invalid/a", "title": "一篇文章",
                            "des": "摘要"}
    assert link["fields"] == [["sources.source", "合成来源"]]
    # 文件的 appattach 是具名子树：同上，字段表里应当是空的
    assert file["file"] == {"title": "报表.xlsx", "fileext": "xlsx", "totallen": 2048,
                            "md5": "0123456789abcdef0123456789abcdef"}
    assert file["fields"] == []
    # 未具名建模的类型：只有字段表，四个具名块全为 null
    assert mini["fields"] == [["appid", "wx0123456789abcdef"], ["pagepath", "pages/index"]]
    assert transfer["fields"] == [] and transfer["ref_msg_key"] is None
    assert res["degraded"] == []                       # 已回填：没有降级


def test_get_message_parts_returns_transfer_fields_verbatim(monkeypatch):
    """用户 2026-09-16 裁定：**照实返回**转账的金额 / 备注 / 双方用户名 / 交易号，
    不为转账单开例外（与 search_history 已在信封返回正文一致）。

    回归：给 `Transfer` 的任一字段加遮蔽（置 None、打码、丢掉），这条会红——整份 dict 相等。
    """
    key, _foreign = _parts_fixture(monkeypatch)
    res = _call(mcp_server.get_message_parts, contact="张三", keys=key(5))
    assert res["parts"][0]["transfer"] == {
        "paysubtype": "1", "amount_desc": "¥88.00", "memo": "午饭",
        "payer": "wxid_friend02", "receiver": "wxid_t_a1b2",
        "transaction_id": "10000000000000000000000", "transfer_id": "t1",
        "begin_time": 1789000000, "effective_date": 1789086400,
        "invalid_time": 1789600000, "is_red_envelope": False}


def test_get_message_parts_answers_only_this_conversation(monkeypatch):
    """只答**属于该会话**的 key：别的会话的 key 与索引里没有的 key 都静默略过
    （与 §11 get_message_media 同一约定——答案里没有某个 key，不等于它不是 appmsg）。

    夹具里 `foreign` 是**另一个会话里真实存在**的一条 appmsg（不是凭空造的 key）：只有它
    在库里，删掉 `account|chat_md5|` 前缀过滤才会让它冒出来。
    **回归：把前缀过滤删掉，这条会红**（变异 M5 实测：第一版夹具用了一个库里不存在的
    foreign key，删掉过滤仍全绿——夹具与现实形态不符，是控制器复验抓出来的）。
    """
    key, foreign = _parts_fixture(monkeypatch)
    res = _call(mcp_server.get_message_parts, contact="张三",
                keys=f"{key(2)}, {foreign}, acct|deadbeef|message_0.db|99")
    assert [p["msg_key"] for p in res["parts"]] == [key(2)]
    assert res["degraded"] == []
    # 已回填：答案非空且**不该**带 hint（hint 不是无条件的装饰）
    assert "hint" not in res


def test_get_message_parts_empty_index_carries_hint(monkeypatch):
    """未回填时信封必须带 hint（并点名 `appmsg rebuild`）：否则那份「parts: []、
    degraded: []」与「这些消息本来就没有可读内容」**逐字节相同**——丙 R23 同一个坑。

    两个通道**分工、不复读**（控制器 F69.2）：`degraded` 说「少了什么」（结构化记录），
    `hint` 只给**可操作的那一句**。
    这里**不种任何 msg_parts 行**：conftest 的 autouse 闸已把索引库指到 tmp_path 的空库，
    工具走的是真实的 `count_msg_parts`。
    回归（各一次）：删掉 hint 或那条 degraded 记录 → 红；把两者改回印同一句 → 红（末条断言）。
    """
    monkeypatch.setattr(usecases, "ChatContext",
                        lambda acct=None: _StubCtx(chat_id="wxid_friend"))
    res = _call(mcp_server.get_message_parts, contact="张三",
                keys="acct|deadbeef|message_0.db|1")
    assert res["parts"] == []
    assert res["degraded"] == [{"capability": "msg_parts",
                                "effect": "msg_parts 未回填：引用 / 链接 / 文件 / 转账"
                                          "一律取不到内容"}]
    assert "agent-wechat appmsg rebuild" in res["hint"]
    effect = res["degraded"][0]["effect"]
    assert effect not in res["hint"] and res["hint"] not in effect, \
        "hint 与 degraded 印了同一句——两个通道要分工，不是复读"
    assert "error" not in res                          # hint 是附加说明，不是错误信封


def test_get_message_parts_counts_unparsed_and_rejected_separately(monkeypatch):
    """「解不开」与「被主动拒绝」**分开计**（形状照 `ChatExportResult` 的 `unparsed` /
    `rejected`，不新造并行类型），两者也都**不进 `parts`**——不然「解不开」与「本来就没有
    可读内容」又长成一个样（丙 R23，控制器 F69.3）。

    三条夹具行都是**别人写进去的字节**（见 `_seed_raw`）：XML 解不开、含 DTD（危险载荷守卫）、
    以及根本不是 zlib 的字节。
    回归（验收点）：把两个计数并成一个（或让 `except zlib.error` 那一支算进 rejected），
    这条会红。
    """
    key, _foreign = _parts_fixture(monkeypatch)
    cm = messages.chat_md5("wxid_friend")
    k = lambda i: messages.row_key("acct", cm, "message_0.db", i)   # noqa: E731
    _seed_raw(monkeypatch, [
        (k(7), zlib.compress(b"<msg><unclosed></msg>", 6)),              # XML 解不开
        (k(8), zlib.compress(b"<!DOCTYPE x [<!ENTITY a 'b'>]><msg/>", 6)),  # 危险载荷
        (k(9), b"\x00\x01 not-zlib"),                                    # 解压就失败
    ])
    res = _call(mcp_server.get_message_parts, contact="张三",
                keys=f"{key(2)}, {k(7)}, {k(8)}, {k(9)}")
    assert res["unparsed"] == 2        # 解压失败 + XML 解不开，合计
    assert res["rejected"] == 1        # 主动拒绝单独计
    assert [p["msg_key"] for p in res["parts"]] == [key(2)]   # 三条都没进 parts
    assert res["degraded"] == []       # 回填过（夹具种了行）：不报「未回填」


def test_message_parts_usecase_returns_typed_result(monkeypatch):
    """用例层 `usecases.message_parts` 的结果是**类型化**的：`parts` 里是 `MessagePart`
    （msg_key + **已解析**的 `AppMsg` + `ref_msg_key`），不是 `msg_parts_by_keys` 的原始四元组；
    `backfilled` 是「回填过没有」这个**事实**本身，文案与信封渲染全在适配层（F67）。

    放在本文件是因为**夹具在这里**（`_parts_fixture` 走生产路径种库）；它测的是用例层。
    回归：把 `parts` 换回原始四元组（不解压不解析），这条会红。
    **`backfilled` 写成常量 `True` 这条杀不掉**（本夹具种了行，常量 True 与真相同色）——
    钉它的是 `test_get_message_parts_empty_index_carries_hint`。**别照前半句的形状去验后半句**
    （初稿把两者写成同一句「都会红」，审阅者照着变异得到绿，白怀疑一次自己的方法）。
    """
    key, _foreign = _parts_fixture(monkeypatch)
    res = usecases.message_parts("张三", keys=[key(2), key(5)])
    assert isinstance(res, usecases.MessagePartsResult)
    assert [p.msg_key for p in res.parts] == [key(2), key(5)]
    assert res.parts[0].appmsg.kind == 57 and res.parts[0].ref_msg_key == key(1)
    assert res.parts[1].appmsg.transfer.amount_desc == "¥88.00"
    assert (res.unparsed, res.rejected, res.backfilled) == (0, 0, True)


def test_get_message_parts_need_confirm_passthrough(monkeypatch):
    """歧义原样透传成 need_confirm 信封（与其余 contact 类工具同形），不猜。"""
    _backfill((), monkeypatch,
              candidates=[("wxid_a", "张三01", "direct"),
                          ("888@chatroom", "张三（群）", "group")])
    res = _call(mcp_server.get_message_parts, contact="张三", keys="x")
    assert res == {"need_confirm": [
        {"username": "wxid_a", "display": "张三01", "kind": "direct"},
        {"username": "888@chatroom", "display": "张三（群）", "kind": "group"}]}


def test_get_message_parts_index_unavailable_is_error_envelope(monkeypatch):
    """索引库读不了是**错误**（有可执行的修复动作），不是「这些消息没有内容」的空答案。

    回归：把 `except (sqlite3.Error, OSError)` 收窄成 `except OSError`，这条会红——不是
    因为异常逃出（`@_guard` 的 `except Exception` 兜底还在，`_call` 也接得住），而是它退化成
    `{"error": "内部错误: OperationalError: …"}`：**修复指引（hint）丢了**，且「库坏了」与
    「我们的 bug」在信封里长得一样。实测如此（变异 M10）。
    """
    monkeypatch.setattr(usecases, "ChatContext",
                        lambda acct=None: _StubCtx(chat_id="wxid_friend"))

    def boom(*a, **k):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(store, "open", boom)
    res = _call(mcp_server.get_message_parts, contact="张三", keys="x")
    assert "本地检索库不可用" in res["error"]
