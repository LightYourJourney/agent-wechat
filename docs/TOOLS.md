# TOOLS — MCP 工具参考

> 本文档逐个说明 agent-wechat MCP Server 暴露的 8 个工具：用途、参数、返回结构、
> 示例与错误。所有工具经 stdio JSON-RPC 调用；示例为 Agent 视角的调用与实际返回。

通用约定：

- `account` 参数（可选）：目标账号目录名（如 `wxid_xxx_ab12`）。省略时操作**当前
  登录账号**；指定的账号目录不存在时报错。**唯一例外是 `build_vector_index`**
  （省略表示索引全部账号，见 §7）。
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

**用途**：查询长任务进度（`export_full_history` 的 `kind: "export"`、
`build_vector_index` 的 `kind: "vector"`）。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `task_id` | string | 否 | null | 省略则返回最近 ≤10 个任务的列表 |

**返回**：

```json
{ "id": "a1b2c3d4e5f6", "kind": "export", "account": "wxid_xxx_ab12",
  "chat_id": null, "status": "done", "rows": 841584,
  "started_at": 1789044000, "finished_at": 1789044110, "error": null }
```

`status`: `running` / `done` / `failed`；失败时 `error` 含中文原因。向量索引任务
（`kind: "vector"`）额外带 `rebuild`，`rows` 为本轮**已嵌入行数**，`skipped` 为按规则
跳过的行数（非文本/无标题的 appmsg、正文短于 2 字符的消息）。

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

## 7. build_vector_index

**用途**：构建本地向量语义索引（`semantic_search` 的数据源）。**长任务**：立即返回 `task_id`，
用 `get_task_status` 查进度。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `rebuild` | bool | 否 | false | true 时先清空索引再全量重嵌（取得连续上下文） |
| `account` | string | 否 | null | **省略则索引全部账号**（与其它工具相反）；水位线按账号分别记录 |

```json
// 入参
{ "rebuild": false, "account": null }
// 返回
{ "task_id": "a1b2c3d4e5f6", "status": "running" }
```

- 嵌入在本机 CPU 完成（fastembed + `BAAI/bge-small-zh-v1.5`），**聊天内容不外发**。
- 首次调用下载约 95MB 模型文件到 `~/.agent-wechat/models`，之后完全离线可用。
- 全量构建预计 25 分钟–2.4 小时（视消息长度分布）；中断/崩溃后重跑自动从水位线续传。
- 数据源是 `export_full_history` 落库的**本地消息库**（不直接读微信）：先导出，再建向量索引；
  之后新增导出的消息再跑一次本工具（省略 `rebuild`）即增量补齐。
- **增量运行会丢失每个会话首条消息的上下文前缀**：`上文:` 前缀在**同一轮运行内**按会话累积，
  增量运行从水位线开始，因此各会话本轮第一条被嵌入的消息没有前缀（即便它确有前一条消息）。
  需要完整连续的上下文时用 `rebuild: true` 全量重建。
- 进度见 `get_task_status`（`kind: "vector"`，`rows` 为本轮已嵌入行数，`skipped` 为跳过行数）。

---

## 8. semantic_search

**用途**：按**语义**（意思相近）检索聊天记录，适合关键词记不全时使用。

与 `search_history` 的分工：精确关键词、人名、专名用 `search_history`（FTS5 更快更准）；
模糊意图（如「上次说的那个爬山的安排」）用 `semantic_search`。需先运行 `build_vector_index`。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `query` | string | **是** | — | 自然语言检索意图（不是关键词） |
| `contact` | string | 否 | null | 限定会话：username 直接使用；展示名解析出多个联系人时返回 `need_confirm`，未命中返回 `error` |
| `limit` | int | 否 | 10 | 返回上限（1-50，超出按 50 处理） |
| `account` | string | 否 | null | 同通用约定 |

```json
// 入参
{ "query": "上次说的那个爬山的安排", "contact": null, "limit": 10, "account": null }
// 返回
{ "mode": "semantic",
  "model": "fastembed:BAAI/bge-small-zh-v1.5:512",
  "total": 1,
  "hits": [ { "chat_id": "wxid_xxx", "display": "张三", "ts": 1757462400,
              "sender": "张三", "is_self": false,
              "content": "周末爬山的安排定了吗", "media_hint": null,
              "distance": 0.1842 } ] }
```

- `distance` 为 cosine 距离，越小越相近。
- **没有相关性阈值**：KNN 恒返回距离最近的 `limit` 条（无论多不相关）；`total` 是本次
  返回的条数（≤ `limit`），**不代表相关程度**——结果是否可用由调用方按 `distance` 判断，
  不要把 `hits` 直接当作「相关结果」呈现。
- `contact` 给出展示名且命中多个联系人时返回 `need_confirm`，不会静默扩大范围。
- **无法解析的 `chat_id` 静默返回 0 命中，不报错**：会话过滤按 `md5(chat_id)` 计算
  （reader 对无法反查的会话给出的 `md5:<hex>` 退化形式已特判支持），只有 canonical 用户名
  （`wxid_*` / `*@chatroom` / `filehelper`）能匹配。传昵称/备注等展示名、或裸 md5 串时
  算出的 md5 匹配不到任何会话——**返回空结果而不是报错，与「确实没有命中」不可区分**。
  展示名 → 用户名的解析是调用方的工作：MCP 走 `contact` 参数（未命中返回 `error`、
  命中多个返回 `need_confirm`），或先调 `list_contacts` 取 username。
- 索引未构建返回 `{"error": ..., "hint": "请先运行 build_vector_index"}`；
  索引由其他模型构建时返回错误并提示重建（`vector-build --rebuild` 或
  `build_vector_index(rebuild=True)`），不会混用不同模型的向量。
- 单会话检索延迟 P50 < 300ms（实测估计）；不限会话的全局检索约 0.5–1s（暴力扫描，无 ANN）。

---

## 错误汇总

| 现象 | 原因 | 处置 |
|---|---|---|
| `key_ready: false` / KeyNotFound 提示 | 该账号未 bootstrap | 按 INSTALL.md §5 引导 |
| `current_account: null` | 微信未运行或启动中 | 打开微信并登录后重试 |
| `{"error": "找不到联系人…"}` | 名字未命中 | 用 `list_contacts` 查准确名 |
| `{"need_confirm": [...]}` | 名字命中多个联系人 | 让用户选择后用 username 重调 |
| `search_history` 无 hint 但 0 命中 | 该词从未出现 | 换词 / 放宽 contact 过滤 |
| `semantic_search` 0 命中且 `hint` 指向 `build_vector_index` | 向量索引未构建/为空 | 先 `export_full_history`，再 `build_vector_index` |
| `semantic_search` 报「与当前模型不一致」 | 索引由其它模型构建 | `build_vector_index(rebuild=true)` 重建 |
| `semantic_search` 命中却看着不相关 | 无语义相关性阈值，KNN 恒返回最近 k 条 | 按 `distance` 判断，必要时收窄 `contact` |

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

期望输出包含 8 个工具名与 `get_account_status` 的 JSON。
