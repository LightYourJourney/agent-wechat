# CHANGELOG

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格，
版本号语义为 0.x 阶段（API 可能变动）。

### 架构深化重构（C1–C8 + 3 个缺陷修复）

内部重构为主。用户可见的变更**只有下列 10 处**：

- CLI `recent -n` 的上界与 MCP 对齐 **1-500**（原为无上限）
- `export_full_history` 的 `contact` 歧义从「误报成找不到」改为返回 `need_confirm`（候选含 kind）
- `list_contacts` / `search_history` 的 `limit` 补下界 1（原为原样透传，`-1` 会变成 SQLite 不限量）
- 错误路径不再杀死 MCP server：`SystemExit` 曾逃出工具处理器（微信未运行时调 5 个工具中的
  任意一个必现），现统一返回 `{"error"[, "hint"]}` 信封；`docs/TOOLS.md` 那句
  「工具不抛异常到传输层」由此由假变真
- **模型不一致**的错误现在带「：请改用 rebuild=True 重建」后缀与 hint——受影响的是
  MCP 的 `semantic_search` / `build_vector_index` 与 CLI 的 `semantic` / `vector-build`
  （原 `semantic_search` 的文案止于「不一致」，无后缀、无 hint）
- **全量导出**（`--full`）此前会清掉**其它账号**的导出水位线，令它们下次白跑一次全量；
  现只清目标账号
- 多账号 `vector-build` 的进度（`rows`）会随账号切换**倒退**；现为全局累计
- MCP `list_contacts` 的返回类型注解由 `list[dict]` 放宽为 `list[dict] | dict`——错误信封
  必须能过 SDK 的返回标注校验（原先只声明数组一种形状，出错即退化成 `UnexpectedToolError`，
  core 写好的中文修复指引到不了 agent）
- CLI 的错误路径统一：`decrypt` / `inspect` / 密钥缺失等此前把 Python traceback 直接抛给
  用户，现为单行中文 stderr + 退出码 1（上面第 4 条只覆盖了 MCP server 一侧）
- 长任务失败文案的长度上限落定为 **300 字**（`errors.redact` 的上限）；此前 `tasks` 侧另有
  一道 500 字切片，因排在 redact 之后执行而恒定不生效，已删除

同时修正：`paths.key_info_db` 此前返回 `.dat` 而非 docstring 所述的 `.db`。

打包面：`pyproject.toml` 未限定 sdist 收录范围，hatchling 默认按 VCS 跟踪面整棵工作树打包
——`uv build --sdist` 实测产出 55 个条目，含 `docs/`(13)、`tests/`(14)、`skills/`、`spike/`、
`.mcp.json`、`.codex/config.toml`。现用 `[tool.hatch.build.targets.sdist] include` 收窄到
`src/agent_wechat` + `pyproject.toml` + `README.md`（21 条），并已验证可从该 sdist 反向构建 wheel。

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
- 「上文」前缀取**上一条入索引消息**（媒体占位等不入索引的消息不计入），
  而非字面上的前一条消息。

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

### 入口策略统一（C2）

- 抽出用例层 `usecases.py`：limit 归一化、contact 解析与结果形状只实现一次，
  `mcp_server.py` / `cli.py` 收缩为参数编组与渲染
- **行为变更**：`export_full_history`（与 CLI `export --contact`）遇到重名不再误报
  「找不到联系人」，而是返回含 `kind` 的 `need_confirm` 候选；未命中统一为
  「找不到联系人「XXX」，请用 list_contacts 查询」
- **行为变更**：CLI `recent -n` 上界对齐 MCP 的 500（此前 `-n 1000` 会返回 1000 条）
- CLI `recent` / `search` / `export` / `semantic` 的消歧输出统一：候选（含 `kind`）到
  stdout、一行说明到 stderr、退出码 1

### purge 命令

- 新增 `agent-wechat purge`，按范围销毁本地产物：`--keys` / `--index` / `--tmp` / `--all`
- 默认 dry-run，`--yes` 才执行；删完给出残留核查（计划内是否清空、按范围保留了哪些、
  仍有进程持有哪些文件、`$TMPDIR` 遗留的 `awcap_*`、Time Machine 排除状态、本地快照）
- 拒绝执行的两种情形：有进程持有待删产物；待删路径是符号链接。后者的判据跟着删除原语走：
  具名产物（`--keys`/`--index`/`--tmp` 的目标）本身是链接时**无条件拒绝**——它们走 unlink，
  只删链接不删目标，核查却会报「已清空」；`--all` 扫整树时只拒绝**解析目标在
  `~/.agent-wechat` 之外**的链接，树内的链接**不拒绝**，它们随整树一起删除——huggingface_hub
  的 `models/` blob/snapshot 布局正是这种
- 只做 CLI，MCP 工具面不变

### 其他规划

- 蒸馏导出格式（按对话窗口切片的 JSONL）
- 群成员昵称解析（chatroom_member）
- 定时增量同步
