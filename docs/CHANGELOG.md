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
- 拒绝执行的三种情形：有进程持有待删产物；待删路径是符号链接——**文件型具名产物（`--keys`/
  `--index` 的目标）本身是链接时无条件拒绝**（`unlink` 只删链接、目标必留存，核查会报「已清空」）；
  **目录型具名产物（`--tmp` 的目标）与 `--all` 走 `rmtree`，还会扫各自那棵树，只拒绝解析目标
  落于 `~/.agent-wechat` 之外（或判不出来）的链接**——树内的随整树一并删除，huggingface_hub 的
  `models/` blob/snapshot 布局就是这种；`AGENT_WECHAT_HOME` 指向不像数据目录的位置（空值会被
  解析成当前目录，或指向 `/`、`~` 及其祖先）时，dry-run 与 `--yes` 两处都拒绝
- 只做 CLI，MCP 工具面不变

### 群成员显示名

- 群消息的「发送者」不再显示 wxid：索引库新增 `display_names` 名册表，查询时
  `LEFT JOIN` 并在 `COALESCE` 里回落存量值——**存量的 43.9 万行一个字都没改**
- 名册来源是 `contact.db`：群昵称取 `chat_room.ext_buffer` 的 protobuf（字段 f2，
  可选），依次回落备注、昵称、username
- 改完备注或群名片后**不需要重新导出**：查询前按 900 秒 TTL 自动刷新名册，
  刷新失败不阻断检索
- 实时路径（`recent`）走内存名册换名；`iter_all`（导出用）**保持产出 wxid**，
  由查询时 join 统一覆盖——新数据与存量因此不会分道扬镳
- MCP 的发送者渲染补上 `is_self` 判断，与 CLI 对齐

### 微信库结构漂移的处理

- 新增 `schema.py`：声明四个能力（`contacts` / `roster` / `messages` / `username_pool`）
  各自必需的表与列；`MessageReader` 打开库后用 `PRAGMA table_info` 探测（单表 0.017 ms，
  实例级缓存）
- 漂移时按能力处理：`contacts` / `messages` / `export` **报错**（唯一来源，降级会产生
  「没有这个人/没有消息」的假象）；`username_pool` 跳过读不到的源；`roster` **逐源降级**
  （群昵称与备注昵称物理无关，少一个仍有另一个）
- 降级时结果里带说明：CLI 走 stderr（不污染 stdout），MCP 信封加 `degraded` 键
- **不掩盖自家的 SQL bug**：探测只保声明过的列，SQL 引用了未声明的列时照旧崩
- `docs/schema-4.1.13.md` 补齐了 `contact.db` 的表结构（此前只有描述性列举）
- 已知限制：名册刷新若恰好撞上漂移，那份不完整的名册会被连同新的时间戳一起写进索引库，
  **触发刷新那一次**仍报降级，此后 900 秒内 `search` 静默（`degraded` 为空）；漂移不消失
  则每个 900 秒窗口都如此重复（详见 USAGE.md §2）

### 媒体解密与导出

- 新增 MCP 工具 `export_chat_media` 与 CLI `agent-wechat media export`：按会话导出三类媒体，
  产物落 `~/.agent-wechat/media/<会话md5>/<类型>/<月份>/`（目录 0700、文件 0600，微信目录仍只读）
- **图片**：dat 容器解密（V2 = 15B 头 + AES-128-ECB 段 + XOR 尾，V1/V0 见 spec §2.1）。
  密钥**纯磁盘推导**、无需特权：`code` 取自 kvcomm 文件名、`aesKey` = md5(code+裸wxid) 前 16 字符、
  `xorKey` 由样本 dat 尾部统计得出；推导结果**必须**用样本 dat 验证，不验证不交付
- **语音**：`media_*.db` 的 `VoiceInfo.voice_data`（明文 SILK）→ pilk 解码 → 24kHz 单声道 WAV
- **视频**：聊天视频本身是明文 mp4，按月份目录枚举后直接复制（本体不在时回退缩略图；
  视频树是账号级的，多会话导出时无从归属，记 `degraded` 跳过）
- 新增 CLI `media rebuild-refs`：全量重扫分片回填 `media_refs` 索引（幂等 upsert、
  **不重写 messages**），为本次新增之前的存量历史补上「消息 → 媒体文件」映射；
  新导出会与消息同事务顺带写入
- 新增依赖：**pilk**（SILK 解码，**GPL-3.0**——本仓库为个人自用、不公开分发，不受其
  传染性条款约束；若将来分发须重新评估）

### 已知限制（媒体解密与导出）

- **`missing` 是常态、不是错误**：微信按需下载并定期清理本地媒体，磁盘上留下的只是历史的一小部分。
  本机实测：84.1 万条索引里有 39,344 条图片消息 / 4,626 条视频消息，而 attach 树里只有
  198 个 dat、0 个视频本体、30 张视频缩略图
- wxgf 图片（本机约占图片的 11%）需 **ffmpeg** 转首帧 JPEG；无 ffmpeg 时存 `.wxgf` 原容器
  并在 `degraded` 里说明（能力级降级，不影响其余导出）
- 朋友圈**不在本期枚举范围**：本地缓存图（本机 27 张，`cache/*/Sns/Img/`）与聊天图同一把钥匙、
  可解，但导出路径只枚举 `msg/attach/<会话md5>/` 与 `msg/video/`，不碰该目录；
  远程媒体需联网，另立设计
- 微信侧未下载完成的截断图解出来仍是不完整 JPEG——如实导出，不做修补
- `image.missing` 计数当前恒为 0（图片导出直接扫磁盘目录，不按消息逐条清点），
  为将来 `media_refs` 驱动的按消息导出预留

### 朋友圈读取与导出（moments export）

- 新增 MCP 工具 `export_moments` 与 CLI `agent-wechat moments export`：读取本账号可见的
  朋友圈动态（正文 / 作者 / 时间 / 评论 / 点赞），导出为**单文件自包含的 HTML 时间线**，
  双击即可离线阅读
- 数据源是微信的 `db_storage/sns/sns.db`（复用既有 passphrase 解密，不进 `index.db`）；
  产物落 `~/.agent-wechat/moments/`（目录 0700、文件 0600），微信目录仍只读
- **新增一条显式的「零网络」例外**：朋友圈的媒体全部是远程的，只有显式指定
  `--with-media`（CLI）/ `with_media=True`（MCP `export_moments`）时才联网下载并解密；
  **默认全程零网络，一个请求都不发**。联网时只连微信 CDN 两域
  （`*.qpic.cn` / `*.video.qq.com`）、**直连不走代理**、只携带微信自己签发的 token
  （详见 SECURITY.md §1「零网络例外」）
- 产物页首如实标注**「这是微信本地缓存窗口，不是完整历史」**（本机实测 1,923 条覆盖
  2013-02 ~ 2026-09，且密度极不均）
- 三个新模块：`moments.py`（离线解析）、`sns_media.py`（唯一直接向微信 CDN 发请求的
  模块：URL 构造 →
  直连 → WX-ISAAC64 流解密，含域白名单与跨重定向校验）、`moments_html.py`（渲染）
- **`media_failed` 是常态而非错误**：微信服务端会清理老媒体，实测抽样中约三成带 token
  仍返回 404；单条媒体失败只计数，不中断整次导出
- **`--limit` 截断留下的是最新的动态**（此前按库里的扫描序截断，留住的是时间线**最旧**
  的那一端，产物按时间倒序渲染后就只剩最早那几条，`window` 也跟着报错）
- **解析失败不再无声**：MCP 信封的 `counts` 新增 `failed`（动态 XML 解析失败的条数，
  与 `media_failed` 无关），`degraded` 与产物页首各有一条说明。此前微信改了动态的 XML
  形状会得到「退出码 0 + 0 条动态 + 空降级」，与「这个账号没有朋友圈」逐字节相同
- 同类降级**只写一条并带条数**（此前每条失败媒体各写一行，50 条失败就是 50 行）
- **回复显示昵称而不是 wxid**：微信的 `<ref_username>` 是 wxid（本机 183/183），此前被
  直接当昵称渲染成「小明 回复 wxid_abc123」；现按 `username` 在同一条动态的互动里反查
  昵称，反查不到则省略「回复 X」那段
- **联网加固**：媒体下载的第一跳与每一跳重定向都必须是 **https**（白名单此前只查
  host，`http://shmmsns.qpic.cn/…` 这样的同域明文跳转会被照常跟随，而请求带着 token）
- `sns.db` 里没有 `SnsTimeLine` 表（旧版微信）时抛 `SchemaDrift` 而不是裸
  `sqlite3.OperationalError`（此前 CLI 会印一整个 traceback）
- 本机的朋友圈缓存图（`cache/*/Sns/Img/`）与聊天图同一把钥匙、可解，但**文件名与动态
  对不上**（四种 md5 变体全部 0 命中），本周期不参与——默认产物是正文 + 评论 + 点赞 +
  媒体占位

### 已知限制（朋友圈）

- **缓存窗口**：只覆盖微信本地缓存的滚动窗口，更早的动态不在本机
- **远程媒体成功率受服务端保留策略影响**：约三成带 token 仍返回 404
- 表情评论只渲染 `[表情评论]` 占位，本体不下载；分享链接自带的第三方缩略图不是朋友圈
  媒体，不抓取
- 发布 / 删除动态、小程序卡片内嵌渲染均不做（微信目录恒只读）

### 其他规划

- 蒸馏导出格式（按对话窗口切片的 JSONL）
- 定时增量同步
