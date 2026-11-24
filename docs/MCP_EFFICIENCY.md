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

以下是第一轮的情况；第二轮改了 msg_key 的显示形态、加了字段和参数，见 §7。

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
以内（现在约 9.5k）。给工具补内容是允许的，但请有意识地调高这个预算，并在本文记一笔，
不要悄悄把每个会话的固定开销涨回去。

## 7. 第二轮：短 msg_key、可读时间、翻页与按需加载

第一轮留下的前四个问题，第二轮按你的选择处理了（2026-11-25）：

| 问题 | 处理 | 兼容性 |
|---|---|---|
| msg_key 太长（约 80 字符） | MCP 返回的 `msg_key` / `ref_msg_key` 改成短形态 `<chat_md5 前 8 位>:<local_id>:<create_time>`，约 22 字符（`messages.short_key`）。account 由 account 参数补出，shard 靠 create_time 区分。收到短键时 `store.resolve_msg_key` 先按 `(account, ts)` 查 messages，查不到再查 `msg_parts` / `msg_extras` / 排除清单，候选逐段核对，多条报错不猜 | 字段名不变；完整 5 段键、v10 前的 4 段旧键照收；存储、墓碑、CLI 输出、导出产物都还是完整键。按 `\|` 拆 msg_key 的外部代码会受影响（仓库内没有） |
| 只有 unix 秒 | 每条消息在 `time` / `ts` 后补 `when`（本机时间，`YYYY-MM-DD HH:MM`），时区写进 instructions | 只加字段 |
| 200 条接近客户端 25k 上限，500 条一定被拒收 | `get_recent_messages` 按粗估 token 设预算（13k 粗估，约合 17–18k 实际）。超出时丢掉最早的部分，返回 `truncated: true` 与 `next_before_ts`；新增可选参数 `before_ts` 往前翻页 | 默认 50、上限 500 不变；只加参数和字段；没超预算时与原来一样，多一个 `truncated: false` |
| 冷启动 | `agent_wechat/__init__` 不再在 import 时载入 CLI；`vector` 的 `sqlite_vec`（带 numpy）改成用到时才载入 | 行为不变 |

### 7.1 对比（同一份合成库，Claude 实测 token：探针第 2 次请求与第 1 次请求的输入之差）

| 调用 | 第一轮后 | 第二轮后 |
|---|---:|---:|
| get_recent_messages（50 条） | 5,530（9,011 字符） | **4,437（−20%，7,879 字符）** |
| get_recent_messages（200 条） | 21,700（35,856 字符，离 25k 上限只剩 3k） | **17,307（−20%）**，200 条全在、未截断 |
| get_recent_messages（500 条） | 约 54k，客户端拒收 | 截到最新的约 200 条（约 17k），`truncated: true` + `next_before_ts` |
| search_history（20 条） | 3,651 | **3,286（−10%）** |
| search_messages（keyword，20 条） | 3,350 | **2,985（−11%）** |

两次都加上了 `when`，所以表里的节省是短键省下的部分减去 `when` 增加的部分之后的净值。

固定开销（`/context`）：工具定义从 6.1k 增加到 6.2k（`get_recent_messages` 多了 `before_ts` 参数和翻页说明），
instructions 从 588 增加到 676（短键与 `when` 的说明）。合计每个会话多约 0.2k，读一次 50 条消息就省回来了。

冷启动：MCP server 进程载入的模块从 731 个降到 641 个（不再载入 CLI、`sqlite_vec`、numpy）。
测量时机器负载很高（load average 约 50），墙钟时间的波动比差值还大，没有给出可信的毫秒数；同一时段交错测
20 次，import 的 CPU 时间中位数少约 5%。按负载较低时的 `-X importtime` 拆分，这几个模块约占本项目 import
时间的六到七成、server 冷启动的一成左右。

Agent 端到端：见 §7.2。

### 7.2 Agent 端到端（Sonnet，6 个任务 × 2 次）

| 指标 | 第一轮后 | 第二轮后 |
|---|---:|---:|
| 平均工具调用次数 | 1.42 | 1.33 |
| 输入 token 合计（含缓存读写） | 386,208 | 386,306 |
| 期望工具命中率 / 答案关键词校验 | 100% / 4/4 | 100% / 4/4 |

这组任务里大结果很少（最大的是 50 条最近消息），每次请求多出的约 0.2k 固定开销和结果里省下的部分基本抵消，
合计持平。短键的正确性在这里得到了验证：「找机票并给上下文」两次都是 `search_history → get_message_context`
一次成功，模型把短键原样交回，没有出错重试。收益集中在读长历史的场景（200 条少约 4.4k token，500 条从被拒收
变成可用），这组任务没有覆盖。

## 8. 剩余问题

- **短键依赖本地索引**：短键要靠索引（或 `msg_parts` / 排除清单）换回完整键。`get_recent_messages` 直接读微信库，
  返回的消息可能还没导出；这时把它的短键交给 `get_message_context` 会答「找不到」——不过完整键在这种情况下
  本来也找不到（这个工具读的是索引），`get_message_media` 也一样。只有 `get_message_parts` 例外：
  它原来能凭完整键查到「已回填、未导出」的消息，现在凭短键也能查到（走 `msg_parts` 那一支），没有退步。
- **`next_before_ts` 在同一秒边界上可能与本页重叠一两条**：`before_ts` 不含上界，而被丢掉的消息里可能有和本页
  第一条同一秒的，这时往后挪一秒，宁可重叠也不漏。要做到严格不重叠需要再加一个 local_id 游标，跨分片处理较复杂，
  这次没做。
- **体积预算是粗估**：CJK 按 1 个 token、其它按 3 个字符折 1 个，比 Claude 实际计数偏低约三成五（合成库）。
  正文特别密（大段英文、长链接、大量 emoji）的会话里偏差可能更大；13k 的预算在合成库上约合 17–18k，留了余量，
  但不是硬保证。
- `get_account_status` 的 `index` 计数仍是三次全表扫描（30 万条约 24ms）。这个工具确实需要这些数，
  而且不在热路径上。
- server 冷启动的大头仍是 MCP SDK 自身的 import（约八成），本项目这一侧能省的已经省了。
