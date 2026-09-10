# TOOLS — MCP 工具参考

> 本文档逐个说明 agent-wechat MCP Server 暴露的 7 个工具：用途、参数、返回结构、
> 示例与错误。所有工具经 stdio JSON-RPC 调用；示例为 Agent 视角的调用与实际返回。

通用约定：

- `account` 参数（可选）：目标账号目录名（如 `wxid_xxx_ab12`）。省略时操作**当前
  登录账号**；指定的账号目录不存在时报错。
- 时间字段一律 **unix 秒**。
- 非文本消息的 `content` 为 `[image]` / `[appmsg: 标题]` / `[voice]` 等提示。
- 错误返回中文说明与修复指引；工具不抛异常到传输层。

---

## 1. get_account_status

**用途**：检测微信运行/登录状态、当前账号、密钥就绪情况与本地索引规模。
**任何其他工具之前先调用它**排查链路问题。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `account` | string | 否 | null | 目标账号；省略用当前登录账号 |

**返回**：

```json
{
  "wechat_running": true,
  "current_account": "wxid_xxx_ab12",
  "accounts": ["wxid_xxx_ab12", "wxid_yyy_cd34"],
  "key_ready": true,
  "index": { "messages": 841584, "chats": 772, "last_ts": 1789046525 }
}
```

**异常**：微信未运行时 `wechat_running=false`、`current_account=null`（不报错）；
索引库不存在时 `index` 计数为 0。

---

## 2. list_contacts

**用途**：按备注/昵称/微信号模糊查找联系人，把用户口述的名字解析成 `username`
（后续工具的稳定 id）。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `query` | string | 否 | `""` | 模糊匹配词；空则列出全部 |
| `limit` | int | 否 | 20 | 返回上限 |
| `account` | string | 否 | null | 同通用约定 |

**返回**（数组，`kind`: `direct` 私聊 / `group` 群聊 / `official` 公众号）：

```json
[
  { "username": "wxid_abc123def456", "display": "小李（产品）",
    "remark": "小李（产品）", "nick_name": "Leo", "alias": "leoli_dev",
    "kind": "direct" }
]
```

**说明**：重名会返回多条——调用方应列出让用户确认，不要猜。

---

## 3. get_recent_messages ⭐

**用途**：读取与指定联系人的**最近**聊天记录（时间正序），用于生成回复建议。
实时解密微信库（最新分片优先，够数即停），通常 1-3 秒。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `contact` | string | **是** | — | username（`wxid_*` / `*@chatroom` / `filehelper`）或展示名 |
| `limit` | int | 否 | 50 | 返回条数（1-500） |
| `account` | string | 否 | null | 同通用约定 |

**成功返回**：

```json
{
  "chat_id": "wxid_abc123def456",
  "display": "小李（产品）",
  "messages": [
    { "time": 1789045400, "sender": "wxid_testacct01", "is_self": true,
      "type": "text", "content": "今晚一起吃饭吗" },
    { "time": 1789045521, "sender": "wxid_abc123def456", "is_self": false,
      "type": "text", "content": "好啊，七点老地方？" },
    { "time": 1789045600, "sender": "wxid_abc123def456", "is_self": false,
      "type": "image", "content": "[image]" }
  ]
}
```

**名字消歧返回**（传入展示名且命中多个联系人时——调用方应让用户选择）：

```json
{ "need_confirm": [
    { "username": "wxid_abc123def456", "display": "小李（产品）", "kind": "direct" },
    { "username": "12345678@chatroom", "display": "产品群", "kind": "group" }
] }
```

**错误返回**：`{"error": "找不到联系人「XXX」，请用 list_contacts 查询"}`。

---

## 4. export_full_history

**用途**：[长任务] 把聊天记录全量导出到本地检索库（流式落库、幂等去重、断点续传）。
`search_history` 的数据源。**立即返回 task_id**，不阻塞。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `contact` | string | 否 | null | 只导出该会话；省略导出全部 |
| `full` | bool | 否 | false | true 时忽略水位线全量重扫（幂等） |
| `account` | string | 否 | null | 同通用约定 |

**返回**：`{ "task_id": "a1b2c3d4e5f6", "status": "running" }`

随后用 `get_task_status` 轮询（建议间隔 10-30 秒）。中断（Ctrl-C / 进程退出）安全：
重跑自动从断点继续。

---

## 5. get_task_status

**用途**：查询导出长任务进度。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `task_id` | string | 否 | null | 省略则返回最近 ≤10 个任务的列表 |

**返回**：

```json
{ "id": "a1b2c3d4e5f6", "kind": "export", "account": "wxid_xxx_ab12",
  "chat_id": null, "status": "done", "rows": 841584,
  "started_at": 1789044000, "finished_at": 1789044110, "error": null }
```

`status`: `running` / `done` / `failed`；失败时 `error` 含中文原因。

---

## 6. search_history ⭐

**用途**：在本地检索库中做全文检索（需先 `export_full_history`）。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `query` | string | **是** | — | 检索词（≥3 字符走 FTS5 trigram；1-2 字自动 LIKE） |
| `contact` | string | 否 | null | 限定会话：username 直接使用；展示名解析出多个联系人时返回 `need_confirm`，未命中返回 `error`——**不会静默扩大检索范围** |
| `limit` | int | 否 | 20 | 返回上限 |
| `account` | string | 否 | null | 同通用约定（检索严格限定该账号的索引数据） |

**返回**：

```json
{
  "mode": "fts",
  "total": 2,
  "hits": [
    { "id": 735662, "chat_id": "wxid_abc123def456",
      "shard": "message_4.db", "local_id": 22415, "ts": 1789045400,
      "msg_type": 1, "sender_id": "wxid_testacct01",
      "sender_name": null, "is_self": 1,
      "content": "周末爬山的那天记得带水", "media_hint": null,
      "snip": "…【周末爬山】那天记得带水…" }
  ]
}
```

**说明**：
- `mode`: `fts`（trigram 命中，含高亮 `snip`）| `like`（1-2 字兜底，`snip` 为 null）
- 1-2 字查询**建议传 `contact`**，否则全表扫描（秒级）
- 索引为空时返回 `{"hits": [], "hint": "本地索引为空：请先运行 export_full_history", ...}`

---

## 7. build_vector_index（预留）

**用途**：构建向量语义索引。**当前版本未实现**，调用返回说明而不报错：

```json
{ "status": "unavailable",
  "reason": "向量索引尚未启用；FTS5 全文检索已覆盖 search_history 需求",
  "upgrade": "后续版本将提供 sqlite-vec + 本地 bge-small-zh 语义检索" }
```

---

## 错误汇总

| 现象 | 原因 | 处置 |
|---|---|---|
| `key_ready: false` / KeyNotFound 提示 | 该账号未 bootstrap | 按 INSTALL.md §5 引导 |
| `current_account: null` | 微信未运行或启动中 | 打开微信并登录后重试 |
| `{"error": "找不到联系人…"}` | 名字未命中 | 用 `list_contacts` 查准确名 |
| `{"need_confirm": [...]}` | 名字命中多个联系人 | 让用户选择后用 username 重调 |
| `search_history` 无 hint 但 0 命中 | 该词从未出现 | 换词 / 放宽 contact 过滤 |

## 附：Server stdio 手动验证

不依赖 Agent 客户端，直接验证 MCP Server 可用（在仓库根目录执行）：

```python
# uv run python - < 验证脚本（初始化 → 列工具 → 调用 status）
import asyncio
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp import ClientSession

async def main():
    params = StdioServerParameters(
        command="uv", args=["run", "agent-wechat-mcp"])
    async with stdio_client(params) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            tools = await s.list_tools()
            print([t.name for t in tools.tools])
            r = await s.call_tool("get_account_status", {})
            print(r.content[0].text[:200])

asyncio.run(main())
```

期望输出包含 7 个工具名与 `get_account_status` 的 JSON。
