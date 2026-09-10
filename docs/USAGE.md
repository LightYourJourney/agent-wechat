# USAGE — 使用指南

> 本文档面向使用者，覆盖典型场景与工作流：读取近期记录、生成回复建议、全量导出、
> 检索历史。工具参数与返回结构的完整说明见 [TOOLS.md](TOOLS.md)。

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
  [1789045400] wxid_abc123def456 wxid_testacct01: 周末爬山的那天记得带水
```

> 检索输出的发送者一列显示 wxid（`is_self=1` 表示本人发送）；`recent` 命令的
> 输出中本人消息显示为 `我:`。

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

## 4. 场景四：LLM 蒸馏准备

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

## 5. 常用辅助命令

```bash
agent-wechat status                 # 登录态/账号/密钥缓存一览
agent-wechat status --verify-keys   # + 对每个库做 HMAC 校验（较慢）
agent-wechat contacts [词]          # 联系人模糊搜索（备注/昵称/微信号）
agent-wechat inspect session/session.db    # 解密并 dump 库结构（排障/适配用）
agent-wechat decrypt message/message_0.db --out m0.db   # 导出明文库（调试）
# ⚠️ --out 产物是持久明文库（~/.agent-wechat/m0.db），用后手动删除
```

## 6. 账号切换

- 读取/导出**默认操作当前登录账号**；`--account <wxid_xxx_hex4>` 可显式指定
- 微信内切换登录后，重新运行 `status` 即可看到 `当前登录` 变化，无需其他配置
- 每个账号首次使用前需**在该账号登录状态下**各跑一次 `bootstrap`
  （passphrase 按账号独立缓存，详见 FAQ）

## 7. 输出约定与错误提示

- 所有失败都会给出中文原因与下一步指引（如密钥缺失时会提示 bootstrap 命令）
- 非文本消息以 `[image]` / `[appmsg: 标题]` / `[voice]` 等提示代替正文，
  XML 内容不会进入索引
- 时间一律为 unix 秒；`is_self=1` 表示本人发送
