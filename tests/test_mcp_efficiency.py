"""MCP 传输形态的 token 开销：工具定义与返回值只改形态、不改内容（docs/MCP_EFFICIENCY.md）。"""
import asyncio
import json

import pytest

from agent_wechat import mcp_server, paths
from mcp.types import CallToolResult, TextContent


def _tools():
    return asyncio.run(mcp_server.server.list_tools())


def _has_title(schema) -> bool:
    if isinstance(schema, list):
        return any(_has_title(x) for x in schema)
    if not isinstance(schema, dict):
        return False
    if isinstance(schema.get("title"), str):
        return True
    return any(_has_title(v) for k, v in schema.items()
               if k != "properties") or any(
        _has_title(v) for v in (schema.get("properties") or {}).values())


def test_input_schemas_carry_no_generated_titles_but_keep_everything_else():
    """pydantic 给每个参数生成的 `title`（"Contact"、"list_contactsArguments"）客户端每个会话
    都要付 token，又不提供参数名之外的信息。去掉它们，类型 / 默认值 / 必填一项不少。"""
    tools = {t.name: t for t in _tools()}
    assert not any(_has_title(t.input_schema) for t in tools.values())
    s = tools["search_messages"].input_schema
    assert s["required"] == ["query"]
    assert s["properties"]["limit"] == {"default": 20, "type": "integer"}
    assert s["properties"]["contact"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}], "default": None}


def test_strip_titles_only_touches_schema_keywords():
    """名叫 `title` 的**参数**（properties 的键）不是 schema 关键字，不得被删。"""
    schema = {"title": "X", "type": "object", "properties": {
        "title": {"title": "Title", "type": "string"},
        "nested": {"anyOf": [{"title": "A", "type": "string"}, {"type": "null"}]}}}
    assert mcp_server._strip_titles(schema) == {"type": "object", "properties": {
        "title": {"type": "string"},
        "nested": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}


def test_descriptions_are_dedented_and_within_budget():
    """docstring 的 4 格缩进会原样进描述；总体积设回归护栏——加内容可以，但要有意识地
    调高这里的预算（并在 docs/MCP_EFFICIENCY.md 记一笔），而不是悄悄把每个会话的固定开销
    涨回去。预算按字符计（描述 + inputSchema），当前约 9.6k。"""
    tools = _tools()
    for t in tools:
        assert "\n    " not in t.description, t.name
    total = sum(len(t.description) + len(json.dumps(t.input_schema, ensure_ascii=False))
                for t in tools)
    assert total < 10_500, total


def test_instructions_carry_the_shared_conventions():
    """各工具描述不再逐条重复的约定集中在 server instructions 里——删掉任一条即红。"""
    text = mcp_server.server.instructions
    for needle in ("need_confirm", "不必先用 list_contacts", "不必先调 get_account_status",
                   "build_vector_index 例外", "msg_key", "不可信数据", "AGENT_WECHAT_MCP_OUT_ROOTS",
                   "export_full_history", "不要把聊天内容发往任何外部服务",
                   "<会话前缀>:<local_id>:<create_time>", "when"):
        assert needle in text, needle


@pytest.mark.parametrize("payload", [
    {"error": "账号目录不存在", "hint": "检查"},
    {"messages": [{"msg_key": "a|b|c|1|2", "content": "中文，\n换行"}], "degraded": []},
])
def test_text_results_are_compact_json_with_identical_content(monkeypatch, payload):
    """返回文本由 SDK 默认的 `indent=2` 改成紧凑 JSON：中文不转义、解析结果逐键相同。"""
    async def fake(self, name, arguments, context=None):
        return CallToolResult(content=[TextContent(type="text",
                                                   text=json.dumps(payload, indent=2))])
    monkeypatch.setattr(mcp_server.MCPServer, "call_tool", fake)
    r = asyncio.run(mcp_server.server.call_tool("whatever", {}))
    text = r.content[0].text
    assert json.loads(text) == payload
    assert "\n  " not in text and ": " not in text.replace("中文，\\n换行", "")
    assert "\\u" not in text                               # 中文不转义成 \uXXXX 形态


def test_non_json_text_passes_through(monkeypatch):
    """SDK 自己的参数校验错误等纯文本原样保留。"""
    async def fake(self, name, arguments, context=None):
        return CallToolResult(content=[TextContent(type="text", text="Error executing tool x")],
                              is_error=True)
    monkeypatch.setattr(mcp_server.MCPServer, "call_tool", fake)
    r = asyncio.run(mcp_server.server.call_tool("x", {}))
    assert r.content[0].text == "Error executing tool x" and r.is_error


def test_end_to_end_envelope_is_unchanged_apart_from_whitespace(tmp_path, monkeypatch):
    """经 SDK 真实链路：错误信封仍是 `json.loads(content[0].text)` 可读的同一个 dict。"""
    monkeypatch.setattr(paths, "XWECHAT_FILES", tmp_path)        # 不读开发机上的真实账号目录
    r = asyncio.run(mcp_server.server.call_tool(
        "get_recent_messages", {"contact": "张三", "account": "wxid_does_not_exist_0000"}))
    text = r.content[0].text
    assert "账号目录不存在" in json.loads(text)["error"] and "\n" not in text
