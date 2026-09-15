# USAGE — 使用指南

> 本文档面向使用者，覆盖典型场景与工作流：读取近期记录、生成回复建议、全量导出、
> 检索历史（关键词与语义）。工具参数与返回结构的完整说明见 [TOOLS.md](TOOLS.md)。

## 0. 两种使用方式

| 方式 | 适合 | 入口 |
|---|---|---|
| **Agent 对话** | 日常「帮我回复 XXX」 | Claude Code / Codex 中的 MCP 工具 |
| **命令行** | 脚本、探索、排障 | `uv run agent-wechat <子命令>` |

两者共享同一核心逻辑，结果一致。以下命令均省略前缀
`uv run --directory ~/agent-wechat`；若已做全局安装
（[INSTALL.md](INSTALL.md) §2.1，`uv tool install --editable`），则任何目录直接裸敲
`agent-wechat <子命令>` 即可。

## 1. 场景一：为某联系人生成回复建议（最常用）

**Agent 方式**（已装 `wechat-reply` Skill 时）——直接在 Claude Code 里说：

```
帮我回复 XXX
```

Agent 会自动执行：`get_account_status` 确认账号 → `list_contacts("XXX")` 消歧 →
`get_recent_messages` 读近期 50 条 → 生成 2-3 条不同风格的候选回复。
若「XXX」匹配多个联系人，Agent 会列出让**你**确认，不会猜。

**CLI 方式**：

```bash
# 1) 把口述名字解析成 username
agent-wechat contacts "小李"
# wxid_abc123def456    direct    小李（产品）

# 2) 实时读取最近 30 条（时间正序，"我"表示本人发送）
agent-wechat recent wxid_abc123def456 -n 30
# [1789045400] 我: 今晚一起吃饭吗
# [1789045521] 小李: 好啊，七点老地方？
```

> `recent -n` 的范围是 **1-500**（与 MCP 的 `get_recent_messages` 同一口径，超出按
> 500 处理；非正值按 1 处理）。`-n 1000` 只会返回最多 500 条。

把输出交给任何 LLM 即可生成回复建议；Skill 工作流中这一步由 Agent 自行完成。

## 2. 场景二：历史检索

> 前提：已完成一次全量导出（见场景三），检索查的是本地索引库。

```bash
agent-wechat search "周末爬山"                  # 全库检索
agent-wechat search "合同" --contact wxid_abc123def456   # 限定会话
```

返回（`snippet` 中 `【】` 为命中高亮）：

```
mode=fts total=2
  [1789045400] wxid_abc123def456 小李: 周末爬山的那天记得带水
```

> 发送者一列显示人名（群昵称优先，其次备注、昵称）；`is_self=1` 表示本人发送。
> 名册来自索引库的 `display_names` 表——改完备注或群名片后**不需要重新导出**，
> 查询前会自动刷新（默认 900 秒一次）。
> `recent` 命令的输出中本人消息显示为 `我:`。

> 微信升级改了库结构时，受影响的能力会降级并在 stderr 给出一行说明（如「显示名暂不可用」）；
> 需要人工对照结构时跑 `agent-wechat inspect`。

> **已知限制**：名册有 900 秒 TTL。若某次刷新恰好撞上 `contact.db` 的结构漂移，那份
> **不完整**的名册会被连同新的时间戳一起写进索引库——**触发刷新那一次**仍会给出降级
> 说明，但此后 900 秒内的每次 `search` 都走早退分支、不再给（`degraded` 为空、stderr
> 也没有那一行），而显示名**确实**已经降级成 wxid。只要漂移还在，**每个 900 秒窗口都
> 如此重复**：窗口到点后再刷新，那一次又报一遍、之后又静默。`recent` 不受影响：它不走
> 索引库，每次调用都重新探测。

**分词规则**：≥3 个字符走 FTS5 trigram（精确子串，快）；1-2 个字符自动降级为
LIKE（`mode=like`）。1-2 字查询若不限定 `--contact` 会做全表扫描（秒级），建议尽量带上。

**Agent 方式**：「我和 XXX 聊过 XX 吗」→ Agent 调 `search_history(query, contact=...)`。

## 3. 场景三：全量导出（首次使用必做）

```bash
# 全部会话
agent-wechat export
# 完成后输出：完成：本轮 841584 行；索引总量 {'messages': 841584, 'chats': 772, ...}

# 仅导出某个会话
agent-wechat export --contact wxid_abc123def456

# 微信升级 / 索引疑似损坏后：忽略水位线全量重扫（幂等，不会产生重复行）
agent-wechat export --full
```

**特性**：

- **流式落库**：逐分片解密（用后即焚），内存占用恒定，与库大小无关
- **幂等**：主键去重，重复导出不产生重复行
- **断点续传**：中断后直接重跑 `export`，从水位线继续
- **增量**：再次运行只导出新消息（秒级）

**规模参考（本机实测）**：约 500MB 加密源库、84.1 万条消息 / 772 个会话，
全量约 1.5-2 分钟；微信在线时的增量更新秒级完成。

## 4. 场景四：语义检索（关键词记不全时）

> 前提：先做一次全量导出（见场景三），再建一次向量索引。索引读的是本地已导出的消息库，
> 不是实时微信数据——新消息要重新 `export`，再跑一次 `vector-build`（增量）才能检索到。

```bash
# 1) 构建本地向量索引（长任务，本机 CPU 推理；首次会下载约 95MB 模型文件）
agent-wechat vector-build
#   已嵌入 5120 行…
# 完成：本轮嵌入 812345 行，跳过 29239 行

# 2) 语义检索：按「意思」找，不要求原话一致
agent-wechat semantic "上次说的那个爬山的安排"
# mode=semantic total=3
#   [1789045400] wxid_abc123def456 小李: 周末爬山的安排定了吗  distance=0.1842

# 限定单个会话 / 调整返回条数（默认 10）
agent-wechat semantic "合同什么时候签" --contact 小李 -n 5
```

**与 `search` 的分工**：精确关键词、人名、专名用 `search`（FTS5 更快更准）；
**记不清原话**的模糊意图（「上次说的那个…」）用 `semantic`。两者索引互不影响，可同时使用。

**参数**：

| 参数 | 适用 | 说明 |
|---|---|---|
| `--account <wxid_xxx_hex4>` | 两者 | `vector-build` **省略时索引全部账号**——与其它子命令（省略即当前登录账号）相反；`semantic` 省略即当前登录账号 |
| `--rebuild` | `vector-build` | 先清空索引再全量重嵌：换模型后必须，也是重新生成上下文前缀的办法 |
| `--contact 名字` | `semantic` | 限定单个会话（username 或展示名；重名会提示，不会猜） |
| `-n N` | `semantic` | 返回条数，默认 10 |

**特性**：

- **断点续传**：中断 / 崩溃后直接重跑 `vector-build`，从水位线继续（不重复嵌入）
- **进度**：每约 5120 行打印一次「已嵌入 N 行…」，结束打印本轮嵌入/跳过行数
- **本机闭环**：嵌入在本机 CPU 完成（fastembed + bge-small-zh-v1.5），聊天内容不外发

**已知限制**（细节见 TOOLS.md §7–§8）：

- 输出中的 `distance` 越小越相近，但**没有相关性阈值**——即使没有合适的结果也会返回
  最近的 N 条，需要自己看 `distance` 判断可用性
- **增量运行**（不带 `--rebuild`）不保留每个会话首条被嵌入消息的「上文」前缀；
  需要完整上下文时用 `--rebuild` 全量重建
- `--contact` 传展示名时**重名/未命中会报错**（不猜，也不会静默返回空）；无法确定时先用
  `contacts` 取 username。能解析但没有任何已入索引消息的会话返回 0 命中且无提示，
  与「确实没有命中」不可区分

**Agent 方式**：Agent 调 MCP `build_vector_index`（长任务，用 `get_task_status` 查进度）与
`semantic_search(query, contact=...)`——与 CLI 同源，参数与返回结构见 TOOLS.md §7–§8。

## 5. 场景五：LLM 蒸馏准备

导出后的数据在 `~/.agent-wechat/index.db`（SQLite），每条消息含
`chat_id / sender_id / is_self / ts / msg_type / content` 结构化字段，可直接 SQL
取用或转 JSONL：

```bash
uv run python - <<'EOF'
import sqlite3, json
con = sqlite3.connect("file:/Users/you/.agent-wechat/index.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute("""SELECT chat_id, sender_id, is_self, ts, content
                      FROM messages WHERE msg_type=1 ORDER BY chat_id, ts""")
with open("distill.jsonl", "w") as f:
    for r in rows:
        f.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
EOF
```

> ⚠️ 蒸馏产物是明文聊天档案：注意存放位置与清理时机（见 SECURITY.md §4）。
> 未来的按对话窗口切片/分块蒸馏格式为规划中功能（见 CHANGELOG）。

## 6. 常用辅助命令

```bash
agent-wechat status                 # 登录态/账号/密钥缓存一览
agent-wechat status --verify-keys   # + 对每个库做 HMAC 校验（较慢）
agent-wechat contacts [词]          # 联系人模糊搜索（备注/昵称/微信号）
agent-wechat inspect session/session.db    # 解密并 dump 库结构（排障/适配用）
agent-wechat decrypt message/message_0.db --out m0.db   # 导出明文库（调试）
# ⚠️ --out 产物是持久明文库（~/.agent-wechat/m0.db），用后手动删除
```

### media（导出图片 / 语音 / 视频）

```bash
# 导出某会话的媒体（默认三类全导、不限月份，上限 2000 个文件）
agent-wechat media export --chat 小李
# 会话 wxid_abc123def456
# 图片   ok=12  failed=1  missing=0
# 语音   ok=8  failed=0  raw_only=2
# 视频   ok=3  thumb=1  missing=0
# 输出目录: /Users/you/.agent-wechat/media

# 只导图片、限定月份、限制数量
agent-wechat media export --chat filehelper --kinds image --months 2026-09 --limit 50

# 导到别处（--out 是持久明文目录，用完自行清理；微信目录始终只读）
agent-wechat media export --chat 小李 --out /tmp/media-out

# 存量历史回填「消息 → 媒体文件」映射（全量重扫该账号分片，分钟级，幂等）
agent-wechat media rebuild-refs
#   已回填 20000 条媒体引用…
# 完成：media_refs 共 20042 条
```

**产物与计数**：

- 产物落 `~/.agent-wechat/media/<会话md5>/<类型>/<月份>/`（目录 0700、文件 0600；
  `--out` 可改根目录）。CLI 只打印计数与「输出目录」，明细自行 `ls`
- **能导出的远少于会话里的媒体消息数**：微信按需下载并定期清理本地媒体，`missing` 大
  是常态而不是错误（本机实测：84.1 万条索引里 39,344 条图片消息，磁盘上只剩 198 个 dat）
- 计数含义：`failed` = 解密/未知格式/落盘失败；`raw_only` = pilk 缺失时导出的原始 `.slk` 数；
  `thumb` = 视频本体不在、回退缩略图的数量；`truncated`（打印「达到 --limit 上限」）= 还有
  没导完的，可按 `--months` 分批重跑补齐。`image.missing` 恒为 0（图片按目录枚举，不按消息清点）
- **依赖是能力级的**：图片解密的密钥纯磁盘推导、不需要 bootstrap；语音要读 `media_*.db`，
  因此需要 passphrase 缓存（即已跑过 bootstrap）与 pilk（缺失则出原始 `.slk`）；
  wxgf 图要 ffmpeg（缺失则存 `.wxgf` 原容器）。缺哪项就只退哪项，降级说明走 stderr

**`--chat` 消歧**（与 `search` / `recent` 同一套）：命中多个时候选 JSON 打到 stdout、
一行说明打到 stderr、退出码 1——**不会猜**，用 username 重试即可：

```bash
agent-wechat media export --chat 小李
# stdout: [{"username": "wxid_abc123def456", "display": "小李（产品）", "kind": "direct"}, …]
# stderr: 无法唯一解析「小李」；候选见 stdout 列表，请用 username 重试   （退出码 1）
```

`media rebuild-refs` 只回填 `media_refs` 索引（不重写 messages），是给**本次功能之前**
已导出的存量历史补映射用的；新导出会与消息同事务顺带写入，无需手动跑。Agent 侧对应
MCP 工具 `export_chat_media`（见 TOOLS.md §9；rebuild-refs 仅 CLI）。

### purge（销毁本地产物）

```bash
agent-wechat purge --index            # 预览：将删除哪些路径、体积、重建代价
agent-wechat purge --index --yes      # 执行
agent-wechat purge --keys --tmp --yes # 三个具名范围可任意组合
agent-wechat purge --all --yes        # 整个 ~/.agent-wechat（含 models/、散落文件）
```

**默认 dry-run**，不加 `--yes` 一个字节都不删。`--all` 与具名范围互斥。

**拒绝执行**的三种情形：
- 有进程正在使用待删产物（否则 unlink 会「成功但空间不释放」）；
- 待删路径是符号链接。**文件型具名产物（`--keys`/`--index` 的目标）本身是链接时无条件拒绝**
  ——unlink 只删链接、目标必留存，核查会报「已清空」；**目录型具名产物（`--tmp` 的目标）与
  `--all` 走 rmtree，还会扫各自那棵树，只拒绝解析目标落于 `~/.agent-wechat` 之外（或判不出来）
  的链接**，树内的随整树一并删除；
- `AGENT_WECHAT_HOME` 指向不像数据目录的位置（空值会被解析成当前目录，或指向 `/`、`~` 及其祖先）
  ——dry-run 与 `--yes` 两处都拦，绝不在这种路径上执行销毁。

## 7. 账号切换

- 读取/导出/`semantic` **默认操作当前登录账号**；`--account <wxid_xxx_hex4>` 可显式指定
- **例外**：`vector-build` 省略 `--account` 表示索引**全部账号**（见场景四）
- 微信内切换登录后，重新运行 `status` 即可看到 `当前登录` 变化，无需其他配置
- 每个账号首次使用前需**在该账号登录状态下**各跑一次 `bootstrap`
  （passphrase 按账号独立缓存，详见 FAQ）

## 8. 输出约定与错误提示

- 所有失败都会给出中文原因与下一步指引（如密钥缺失时会提示 bootstrap 命令）
- 名字消歧在 `recent` / `search` / `export` / `semantic` 四个子命令上行为一致：
  候选列表（JSON，含 `kind`；与 MCP 的 `need_confirm` 同形）打到 **stdout**，
  一行「无法唯一解析…」打到 **stderr**，退出码 **1**——歧义不会被当成「找不到」，
  也不会替你猜。用候选里的 `username` 重试即可
- `contact` 传展示名且未命中任何联系人时，报错并提示用 `contacts`（MCP 侧为
  `list_contacts`）查询，不会静默扩大到全部会话
- 非文本消息以 `[image]` / `[appmsg: 标题]` / `[voice]` 等提示代替正文，
  XML 内容不会进入索引
- 时间一律为 unix 秒；`is_self=1` 表示本人发送
