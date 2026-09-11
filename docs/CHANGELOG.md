# CHANGELOG

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格，
版本号语义为 0.x 阶段（API 可能变动）。

## [0.1.0] - 2026-09-10

首个可用版本。**仅限本机自用，勿公开分发**（见 SECURITY.md §5）。

### 新增

- **登录态检测**：`pgrep -x WeChat` + `lsof -n -P` 判定微信运行与当前登录账号（无侵入）
- **密钥引导（bootstrap）**：lldb 断点 `CCKeyDerivationPBKDF` 捕获 32 字节 passphrase；
  PBKDF2-SHA512(256,000 轮) 每库派生 + page1 HMAC-SHA512 自验证（公式经对照实验
  真假双向验证）；缓存按账号分键（0600）；引导后一切操作零特权
- **解密层**：db+wal+shm 三元组快照 → sqlcipher raw-key 导出；临时明文 0700 用后即焚
- **消息读取**：基于微信 4.1.13 实测 schema——`Msg_<md5>` 每会话一表、跨分片时间分区、
  WCDB zstd 压缩解码、`local_type` 掩码、群消息发送者前缀解析；近期读取按最新分片
  优先早停
- **全量导出**：流式批处理（恒定内存）、幂等主键 `(account, chat_md5, shard, local_id)`、
  水位线断点续传、导出与内容修正解耦的 upsert；本机实测 84.1 万条 / 772 会话
  约 1.5-2 分钟
- **本地检索**：SQLite + FTS5 trigram（≥3 字精确子串 + snippet 高亮），
  1-2 字自动 LIKE 兜底
- **MCP Server**：7 个工具（stdio）：`get_account_status` / `list_contacts` /
  `get_recent_messages` / `export_full_history` / `get_task_status` /
  `search_history` / `build_vector_index`（预留）
- **CLI**：status / bootstrap / decrypt / inspect / contacts / recent / export / search
- **集成**：Claude Code 项目级 `.mcp.json`；Codex `config.toml` 配置示例；
  `wechat-reply` Skill（回复建议工作流）
- **隐私控制**：只读微信目录（代码级保证）、Time Machine 排除、passphrase 0600、
  sudo 环境自动归还文件属主

### 已知限制
- 多次中断的向量构建（每次都从中断点续跑）完成后，`vector_meta.skipped` 只统计最后一段的跳过量
  （`embedded` 不受影响，始终是实时准确值）；`--rebuild` 一次性完成时两个计数都准确。

- 向量语义检索未启用（`build_vector_index` 为占位，见 ARCHITECTURE.md §6）
- 群聊发送者显示为 wxid（群昵称映射未实现）
- 导出不含媒体文件本体（仅类型/元数据提示）
- 微信升级后需重签名 + 重新 bootstrap（见 TROUBLESHOOTING §4）

### 技术笔记

- 微信 4.1+ 内存中不再缓存 raw key（仅存登录时派生的 passphrase），旧的内存模式
  扫描路线在本版本上不可行
- `xwechat_files/all_users/login/<wxid>/key_info.db` 为明文 SQLite（56 行被包裹的
  密钥 blob）；经系统化 HMAC 验证排除纯文件解析路线（spike 脚本留档于 `spike/`）
- 同会话消息跨分片按时间分区且 `local_id` 各分片独立计数——去重键必须四元组

## [0.1.1] - 2026-09-10

文档评审（三路独立审查：代码准确性 / 一致性 / 编造与敏感承诺）后的修复。

### 修复

- **Time Machine 排除**：由"文档声称"变为代码保证——`ensure_home_dirs()` 首次调用时
  自动执行 `tmutil addexclusion ~/.agent-wechat`（幂等、失败静默，可 `tmutil isexcluded`
  验证）
- **search_history 账号隔离**：此前检索固定取索引首行账号的数据，多账号机器上会串号；
  现在 `search` 强制显式 `account` 过滤
- **search_history 联系人解析**：展示名现在真正可解析（经联系人消歧）；未命中返回
  `error`、命中多个返回 `need_confirm`，不再静默退化为全库检索
- **Skill 纳入仓库**：`skills/wechat-reply/SKILL.md` 随仓库分发，INSTALL 的安装指引
  现在可执行
- 文档修正：`decrypt --out` 持久明文例外已如实标注；get_account_status 返回结构、
  示例中本人消息的 sender、单库失败的中止行为、快照一致性边界、`server_id` 为预留
  字段、`tasks.json` 存储清单、失效锚点与伪命令等 12 处

## [未发布]

### 向量语义检索（semantic_search）

- 新增 `semantic_search` MCP 工具与 `agent-wechat semantic` CLI：本地语义检索，与 FTS5 关键词检索互补
- `build_vector_index` 由占位 stub 落地为完整实现：后台长任务 + 断点续跑；新增 `agent-wechat vector-build`
- 嵌入全程本机（fastembed + bge-small-zh-v1.5，无 torch），聊天内容零外发
- 新增依赖：fastembed、sqlite-vec（单文件索引内建 vec0 虚表）
- 既有 `search_history` / `search` 行为不变

### 已知限制（向量语义检索）

- 增量运行（省略 `--rebuild`）的上下文前缀不连续：`上文:` 前缀在同一轮运行内按会话累积，
  各会话本轮第一条被嵌入的消息没有前缀；需要完整上下文时用 `--rebuild` 全量重建
- 语义检索没有相关性阈值：KNN 恒返回最近的 `k` 条（无论距离多远），`distance`（cosine，
  越小越近）原样返回，由调用方判断可用性
- 全局（不限会话）检索是暴力扫描、无 ANN；能解析、但没有任何已入索引消息的会话返回
  0 命中且无提示，与「确实没有命中」不可区分（展示名须经 `contact` 解析，未命中 / 重名会报错）
- 索引由其它模型构建时必须 `--rebuild`，不会混用不同模型的向量

### 其他规划

- 蒸馏导出格式（按对话窗口切片的 JSONL）
- 群成员昵称解析（chatroom_member）
- `purge` 一键销毁命令
- 定时增量同步
