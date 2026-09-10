# agent-wechat

macOS 微信（4.1+）聊天记录读取 + MCP Server。让 Claude Code / Codex 直接读取本机
已登录微信账号的聊天记录，为指定联系人生成回复建议；支持全量导出到本地检索库。

> ⚠️ **仅限本机本人已登录账号、仅本地使用**。解密自己的聊天数据库涉及合规争议
> （chatlog 等同类项目 2025 年被微信官方函件后删库），**请勿公开分发本仓库**。
> 隐私细节见文末。

## 架构

- **一次性特权引导**：`bootstrap` 用 lldb 断点捕获登录派生的 32 字节 passphrase
  （唯一需要 sudo 的环节），之后一切操作无特权、纯文件读取
- **流式导出**：逐分片解密（用后即焚）→ 解码（WCDB zstd / 类型掩码 / 群前缀）→
  幂等 upsert 到 `~/.agent-wechat/index.db`（SQLite + FTS5 trigram）
- **MCP 薄适配层**：7 个工具，核心逻辑可独立用 CLI

真实 schema 实测文档：[docs/schema-4.1.13.md](docs/schema-4.1.13.md)

## 使用

```bash
# 0. 一次性引导（仅首次 / 微信升级后；详见 bootstrap.py 文档串）
killall WeChat
sudo codesign --force --deep --sign - /Applications/WeChat.app   # 去掉 Hardened Runtime
open /Applications/WeChat.app                                     # 登录
sudo uv run --directory ~/agent-wechat agent-wechat bootstrap
#    启动后 5 分钟内：在微信里 退出登录 → 重新登录，触发密钥捕获

# 1. 状态
uv run agent-wechat status --verify-keys

# 2. 近期消息（实时解密，秒级）
uv run agent-wechat recent filehelper -n 5
uv run agent-wechat contacts <名字>          # 解析口述名字 → username

# 3. 全量导出（84 万行约 2 分钟；增量续传）
uv run agent-wechat export                   # 全部会话
uv run agent-wechat export --contact <username|名字>

# 4. 检索（基于本地索引）
uv run agent-wechat search "好莱坞"          # ≥3 字走 trigram，1-2 字自动 LIKE
```

**MCP 接入**：Claude Code 用仓库里的 [.mcp.json](.mcp.json)（项目级自动加载）；
Codex 已在 `~/.codex/config.toml` 追加 `[mcp_servers.agent-wechat]`。
Claude Code 另有 `~/.claude/skills/wechat-reply/SKILL.md`（说「帮我回复 XXX」触发）。

## MCP 工具

| 工具 | 说明 |
|---|---|
| `get_account_status` | 登录态 / 当前账号 / 密钥就绪 / 索引规模 |
| `list_contacts(query)` | 人名 → username（备注/昵称/微信号模糊匹配） |
| `get_recent_messages(contact, limit)` | 近期记录（实时解密，最新分片优先） |
| `export_full_history(contact?)` | [长任务] 全量导出，返回 task_id |
| `get_task_status(task_id?)` | 任务进度 |
| `search_history(query, contact?)` | 全文检索（本地索引） |
| `build_vector_index` | 预留（语义检索后续增强） |

## 多账号

- 数据目录按 `wxid_<id>_<hex4>` 隔离；当前登录账号由 `lsof` 判定
- 每个账号各自的 passphrase 独立捕获：登录到哪个账号，就 bootstrap 一次
- `--account wxid_xxx_hex4` 显式指定目标账号；省略时操作当前登录账号

## 隐私与合规

- 绝不写入微信容器目录；解密副本在 `~/.agent-wechat/tmp/`（0700）用后即焚
- `~/.agent-wechat/` 已加入 Time Machine 排除（`tmutil addexclusion`）
- passphrase 缓存 0600；索引库明文依赖 FileVault 全盘加密
- embedding/检索全部本地，聊天内容不出本机
- `uv run agent-wechat purge` 未实现前，手动 `rm -rf ~/.agent-wechat` 即可销毁全部数据
