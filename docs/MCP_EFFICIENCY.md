# MCP 使用效率：测量方法、优化与对比

> 本文记录 agent-wechat MCP Server 在 Claude Code 中的真实开销：工具定义占多少上下文、
> 返回值多大、Agent 为完成一件事要来回几轮、各工具多快。数字都可以用下面两个脚本复现，
> 全程使用合成数据，不读真机微信，也不读 `~/.agent-wechat`。

## 1. 怎么测

| 维度 | 工具 | 说明 |
|---|---|---|
| 工具定义的 token | Claude Code `/context` | `claude -p "/context" --strict-mcp-config --mcp-config <cfg>`，读「MCP tools」与「MCP server instructions」两项。这是客户端实际计入上下文的数，不是估算 |
| 定义与返回值的字符数、server 冷启动、单次调用延迟 | `scripts/mcp_bench.py` | 在临时目录里用测试夹具造一份**加密**的合成微信库（默认 20 个会话 × 1500 条，`--chats/--per-chat` 可调），先跑导出建索引，再经 SDK 的 `call_tool` 逐个调用（每个工具调 5 次，取中位数） |
| 返回值实际占多少 token | 单次探针 | `claude -p` 让模型只调一次某个工具，比较第 2 次请求与第 1 次请求的输入 token 之差（差值 = tool_use + 工具结果） |
| 工具选择与多轮成本 | `scripts/mcp_agent_eval.py` | 6 个典型任务，每个跑 2 次，跑在合成库上。只开放 agent-wechat 的工具（`--tools ""`），并关掉本机 hooks（`disableAllHooks`，否则压缩大输出的插件会改写工具结果）。统计调用序列、轮数、输入 token（含缓存读写）与耗时。会调用真实模型、产生费用 |

```bash
uv run --frozen python scripts/mcp_bench.py --json /tmp/bench.json
uv run --frozen python scripts/mcp_agent_eval.py --model sonnet --reps 2 --json /tmp/eval.json
```

## 2. 发现的瓶颈

1. **工具定义是每个会话的固定开销**。只要配置了本 server，即使会话跟微信无关，Claude Code
   也会把 15 个工具的定义全部载入，合计 11.2k token。主要来自三处：
   - **描述**：大段实现史与实测轶事（「实测 0.005%」「用户 2026-09-16 裁定」「spec 裁定 5」等）；
     同一条约定在多个工具里重复（contact 怎么填、out_dir 放行范围、degraded 是什么）；
     docstring 的 4 格缩进也原样进了描述。
   - **inputSchema**：pydantic 给每个参数自动生成 `title`（如 "Contact"、"list_contactsArguments"），
     这些字段在参数名之外不提供任何信息。
   - 单个工具最重的是 `get_message_parts` 与 `get_message_media`，各约 1.6k token。
2. **引导语句造成多余的往返**。server instructions 原文是「先 get_account_status 确认账号；
   用 list_contacts 把人名解析成 username」，`get_account_status` 的描述里也写着「读取任何消息前应先调用
   本工具」。可是每个工具的 `contact` 都能直接接受名字（重名时返回 `need_confirm`），未登录时也会直接报错
   并给出 hint。Sonnet 在 12 次运行中有 9 次先调 `get_account_status`、6 次先调 `list_contacts`，
   平均每个任务 2.42 次工具调用，而理想值约 1.2 次。
3. **返回值是 `indent=2` 的 JSON**。SDK 默认把 dict 结果缩进 2 格再序列化，换行与缩进都要计 token。
   50 条消息的 `get_recent_messages` 约 6.5k token。Claude Code 对有 structuredContent 的工具
   （`list_contacts`、`get_task_status`）用的是紧凑形态，其余工具拿到的都是缩进版。
4. **`get_message_media` 不给 keys 时扫的是整个账号**。查询条件是 `chat_md5`，这一列上没有索引，
   规划器为了 `ORDER BY ts` 选了 `idx_msg_ts (account, ts)`，于是把整个账号的消息倒序扫一遍：
   30 万条约 106ms，并且随索引总量线性增长。
5. **「索引为空」提示的判定做了三次全表扫描**。`index_ready()` 为了得到一个布尔值调用了
   `store.counts`（`count(*)`、`count(DISTINCT)`、`max`），30 万条约 23ms。

测过但不是瓶颈的：server 冷启动（initialize 约 330ms，其中 MCP SDK 自身的 import 约 240ms，
本项目的模块约 65ms）、`tools/list` 约 1.7ms，以及其余各工具的单次调用（30 万条下都在 10ms 以内）。
与模型一轮往返所需的数秒相比，这些都可以忽略。

## 3. 改了什么

改动都只涉及传输形态或文案，工具名、参数、默认值、返回键都没有变：

| 改动 | 位置 | 说明 |
|---|---|---|
| 精简工具描述 | `mcp_server.py` 各工具 docstring | 保留选用与调用所需的要点：做什么、什么时候用、与相近工具怎么分工、前置条件、参数语义、返回键、容易误读的口径（如「gone 是常态」「media_failed 计次数」）、网络与隐私警示。删掉实现史与实测轶事，这些在 TOOLS.md 里仍有完整版 |
| 通用约定集中到 instructions | `mcp_server._INSTRUCTIONS` | contact 的写法与 `need_confirm`、不必先调 `get_account_status`/`list_contacts`、account 的默认值与例外、错误与 degraded 信封、msg_key、索引前置、正文不可信、out_dir 放行范围。这些在 instructions 里写一次，客户端每个会话只载入一份 |
| 去掉 schema 里自动生成的 `title` | `_Server.list_tools` | 类型、默认值、必填项一项不少。名叫 `title` 的参数不受影响（有专门的测试） |
| 描述去缩进 | `_Server.list_tools` | `inspect.cleandoc` |
| 返回紧凑 JSON | `_Server.call_tool` | `indent=2` 改为 `separators=(",", ":")`，中文不转义。`json.loads(content[0].text)` 得到的 dict 逐键相同；structuredContent 不动；非 JSON 文本（SDK 的参数校验错误）原样保留 |
| 媒体查询走主键区间 | `usecases.message_media` | 用会话的 `msg_key` 前缀区间走主键索引，加上 `+m.account` 避免规划器选 `idx_msg_ts`；列谓词照留，答案不变 |
| `index_ready` 只问「有没有」 | `usecases.retrieve.index_ready` | 改为 `SELECT EXISTS(SELECT 1 FROM messages)` |
| Skill / 文档里的流程 | `skills/wechat-reply`、USAGE.md、TOOLS.md §1 | 去掉「先 `list_contacts`」这一步，直接用名字调 `get_recent_messages`，重名时再让用户选。补充检索时优先用 `search_messages` 取片段 |

## 4. 前后对比

### 4.1 工具定义（Claude Code `/context`，每个会话固定计入）

| 工具 | 前 | 后 |
|---|---:|---:|
| get_message_parts | 1.6k | 576 |
| get_message_media | 1.6k | 634 |
| export_chat_media | 1.2k | 639 |
| export_chat | 1.1k | 552 |
| export_moments | 1.1k | 612 |
| search_messages | 909 | 545 |
| semantic_search | 602 | 370 |
| search_history | 530 | 355 |
| build_vector_index | 503 | 343 |
| get_message_context | 487 | 314 |
| get_recent_messages | 434 | 252 |
| export_full_history | 368 | 300 |
| list_contacts | 315 | 264 |
| get_account_status | 279 | 199 |
| get_task_status | 165 | 127 |
| **MCP tools 合计** | **11.1k** | **6.1k** |
| MCP server instructions | 124 | 588 |
| **合计** | **≈11.2k** | **≈6.7k（−40%）** |

字符数（`mcp_bench.py`）：描述从 10,469 降到 4,794，inputSchema 从 6,248 降到 4,480。
每个 API 请求的基础输入（探针，不含内置工具）从 18,275 降到 13,679 token，每轮少约 4.6k。

### 4.2 返回值（同一份合成库，同样的参数）

| 调用 | 字符数 前 → 后 | 实际 token（探针差值）前 → 后 |
|---|---|---|
| get_recent_messages（50 条） | 11,931 → 9,011（−24%） | 6,543 → 5,530（−15%） |
| search_history（20 条） | 8,677 → 6,377（−27%） | 4,484 → 3,651（−19%） |
| search_messages（keyword，20 条） | 8,272 → 5,737（−31%） | 4,192 → 3,350（−20%） |
| get_message_context | 1,444 → 1,130（−22%） | — |
| get_account_status | 393 → 314（−20%） | 305 → 约 262 |
| list_contacts | 2,576 → 2,101（content 块） | 1,066 → 1,066（Claude Code 读的是 structuredContent，本来就是紧凑形态） |

### 4.3 Agent 端到端（6 个任务 × 2 次，合成库）

| 指标 | Sonnet 前 | Sonnet 后 | Haiku 前 | Haiku 后 |
|---|---:|---:|---:|---:|
| 平均工具调用次数 | 2.42 | **1.42** | 2.08 | **1.33** |
| 平均轮数 | 3.42 | 2.42 | 3.08 | 2.33 |
| 输入 token 合计（含缓存读写） | 629,209 | **386,208（−39%）** | 648,087 | **420,043（−35%）** |
| 单任务输入 token 中位数 | 54,705 | 29,390 | 56,616 | 32,910 |
| 单任务耗时中位数 | 6.1s | 5.5s | 7.3s | 6.9s |
| 期望工具命中率 | 100% | 100% | 100% | 100% |
| 答案关键词校验 | 4/4 | 4/4 | 4/4 | 4/4 |

调用序列的变化（Sonnet）：「我和张三聊了什么」从 `get_account_status → list_contacts →
get_recent_messages` 变成 `get_recent_messages` 一步；「找提到报销的消息」从
`get_account_status → search_history` 变成 `search_messages` 一步，返回的是片段，比全文小。

费用这一项（`total_cost_usd`）没有放进表里。它主要由提示缓存的写入决定，而每次评测之前缓存是冷是热
并不受控制，前后两轮的费用因此不能直接比较。

### 4.4 延迟（`mcp_bench.py`，30 万条合成消息）

| 调用 | 前 | 后 |
|---|---:|---:|
| get_message_media（整会话） | 106ms | 4.3ms |
| index_ready（「索引为空」提示的判定） | 23ms | <0.1ms |
| 其余工具 | 1–8ms | 不变 |
| server initialize / tools/list | 330ms / 1.7ms | 不变 |

## 5. 兼容性

- 工具名、工具数（15）、参数名、类型、默认值、必填项都没有变（`tests/test_tools_doc.py`
  逐项对照 TOOLS.md），返回 dict 的键与值也没有变。
- 返回文本仍是合法 JSON，`json.loads` 的结果与之前完全相同；变化的只有空白字符。
  把返回文本当字面串做逐字比较的客户端会看到不同，项目内没有这种用法。
- 依赖 `content` 分块的客户端：返回列表的工具（`list_contacts`、`get_task_status`）仍是
  一个元素对应一个块，structuredContent 不变。
- 不读 server instructions 的客户端会少看到跨工具的通用约定，但工具本身的行为不变：
  名字照样能用，重名时照样返回 `need_confirm`。Claude Code 会载入 instructions（`/context`
  里那 588 token 就是）。
- 测试：`tests/test_mcp_efficiency.py` 覆盖 schema 无 title 且其余项不变、描述的体积预算、
  instructions 里的各条约定、紧凑 JSON 与原 dict 等价、非 JSON 文本原样保留、端到端信封；
  `tests/test_usecases_media_link.py` 覆盖整会话媒体查询的执行计划与会话隔离。

## 6. 回归护栏

`test_descriptions_are_dedented_and_within_budget` 把「描述 + inputSchema」的总字符数限定在 10,500
以内（现在约 9.3k）。给工具补内容是允许的，但请有意识地调高这个预算，并在本文记一笔，
不要悄悄把每个会话的固定开销涨回去。

## 7. 剩余问题

- **msg_key 很长**（约 80 字符，含 32 位 hex），每条消息都带一个。在 50 条的
  `get_recent_messages` 里大约占三分之一的 token。它是对外的稳定标识，改格式会破坏兼容性，
  所以这次没动。
- **limit 的上限偏大**：`get_recent_messages` 允许的 limit（上限 500）远超 Claude Code 单个工具结果的默认上限
  （`MAX_MCP_OUTPUT_TOKENS` = 25k）。200 条在缩进形态下约 26k token，会被客户端拒收，模型只好
  调小 limit 重试；紧凑后约 22k，余量仍然不多。收紧上限属于接口变更，这次没做。
- **`time` 只给 unix 秒**：模型要换算日期时会去找 Bash，评测里出现过一次。加一个可读时间字段属于
  加法式变更，但每条消息会多约 10 token，这次没做。
- `get_account_status` 的 `index` 计数仍是三次全表扫描（30 万条约 24ms）。这个工具确实需要这些数，
  而且不在热路径上。
- server 冷启动约 330ms，大头是 MCP SDK 自身的 import，本项目能省的不到 15%。启动只发生在会话开始时，
  而且与客户端的其它初始化并行。
