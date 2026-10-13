# CHANGELOG

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格，
版本号语义为 0.x 阶段（API 可能变动）。

### 工程质量审计修复期（进行中）

对全仓 123 条审计发现（`内部审计记录`）的修复。用户可见的行为变更按批
在此累积，修复期结束时合并为正式条目：

- **长任务注册表**（`export_full_history` / `build_vector_index` 共用）：同目标的重复请求
  复用既有 `task_id`（不再叠加第二个同目标任务）；任务记录新增 `pid` 与 `full` 字段；
  `running` 记录在下一次被读到且「pid 已消失**或**已超过 24 小时」时改判 `failed`；
  注册表每次操作重读磁盘、写入按记录合并——两个 MCP server 共享同一 `AGENT_WECHAT_HOME`
  时互相可见、且不再互相抹掉记录（此前长驻进程对另一进程的活任务答「未知任务」，写盘会
  把对方记录整表覆写）
- **检索**（`search_history` / CLI `search`）：空/全空白 query 被拒（此前退化成全表匹配）；
  `%` / `_` 按字面量处理；FTS 索引失同步时空结果会带说明与重建路径（不再静默 0 命中）；
  `get_account_status` 新增 `schema_notes` 自检说明（触发器体不一致 / 索引库读不出来，
  只报不改库）
- **只读诊断不再建库**：`get_account_status` / CLI `status` 在索引库或其排除清单缺失时
  不再凭空创建它们（此前跑一次 status 就会把 `purge --index` 删掉的索引建回来）
- **MCP `export_chat_media` / `export_moments` 补上 `out_dir` 参数**：描述里一直承诺、
  签名里一直没有（SDK 静默丢弃未知实参，「照描述传」会静默落到默认根——与 `export_chat`
  的自定义根组合即全死链）
- **媒体与 CDN**（批 B5）：
  - **崩溃后残留的分片明文会被自动清扫**（本批唯一的**破坏性**行为）：临时目录名现在带
    pid，每次运行（每进程第一次解密之前）清扫 `tmp/` 下**确定没有活进程持有**的
    `awdec-<pid>-*` 目录。判据是 `os.kill(pid, 0)`：**判不出来一律留着**（别人的进程、
    探测失败），非本工具形态的目录一律不碰。⚠️ **旧版本留下的 `awdec-*`（无 pid 段）不会被
    清**——那种形态判不出归属，需要手动 `rm -rf`；本机实测当前 `tmp/` 为空
  - **4xx 不再重试**：`404/410` 等确定性失败直接报「服务端已清理（HTTP 4xx）」，不再
    白等两次退避、也不再被包装成「下载失败（重试 3 次）」；解密失败同样不再重试（它与网络
    无关），根因不再被改写成网络错误。取舍：CDN 边缘的**暂态** 404 会丢掉一次可恢复的下载
  - **贴纸计数口径位移**：没有凭据 / 定位不了那一行 / 承载它的分片读不出来 → 从 `failed`
    改计 `missing`（`failed` 只留真正联网失败的）；分片级失败另在 `degraded` 里说明
    （与「本机没有凭据」区分）
  - 响应头说「加密」而 XML 没给密钥时不再静默返回密文（与请求前那条同族早退对称）
  - `counts[*].skipped` 的口径补上「**本轮内重复**」（同一个贴纸 md5 只取一次件）
  - 文档订正：SECURITY §1 例外 3 的目标域补全三个后缀与实测占比 + 端口既知代价
- **密钥与解密链**（批 B4）：
  - 引导的**诊断分叉**：`bootstrap` 在「一个库都没能读到」时改说权限问题（给「完全磁盘
    访问权限」指引），不再把它混进「捕获值未通过 HMAC 校验，请重试」——此前用户会一遍遍
    重跑 sudo 引导，而真问题是读不到库
  - `status --verify-keys` 的 `HMAC N/N` 后补「（另有 N 个库读不到）」；`status` 新增一行
    备份排除的**真实状态**（只读查询 `tmutil isexcluded`，与 purge 的核查同源；查不出来时
    不印——「判不出来」与「没排除」是两件事）——这条安全控制此前完全不可观测
  - 引导结束时的**非致命警告**（sudo 下属主未归还等）改为 `⚠️` 一行，不再把整次引导判负
  - `AGENT_WECHAT_HOME` 为空时回落默认目录（`Path("")` == `.` 会让密钥缓存落进当前工作
    目录）；相对路径直接拒绝并给 hint
  - 微信目录**不可搜索**（无完全磁盘访问权限）与「目录不存在」分开报错——此前一律报
    「没找到账号」
  - **图片密钥验证门修好一半**：样本优先取 V2 dat（V0/V1 样本的解密输出与 aesKey 无关，
    会让验证门空转、放行错钥）；kvcomm code 排序，去掉「哪把钥匙先被试」的文件系统依赖；
    一个 V2 样本都没有时回落现行为，但把 aesKey 标为**未验证**并在 `degraded` 里说明
  - `sqlcipher` 命令改走 stdin：派生密钥不再出现在进程命令行（`ps` 可见），错误文案相应
    从「Parse error in 3rd command line argument」变为「Parse error near line N」；
    解密失败的 stderr 不再预先截断（脱敏需要完整 64 位密钥形态才认得出）
  - 密钥缓存的**形状**损坏（合法 JSON 但根/条目不是对象）与 hex 长度不符一律按「缓存损坏」
    处理并指向重新 bootstrap——此前在前者上裸抛 `AttributeError`
  - `pgrep` 补超时（卡顿时不再挂住）；lldb 捕获的读循环不再被**半行**输出钉住；收尾只向
    本次捕获的进程组发信号，不再按名字 `pkill -9 -x lldb`（那会连坐用户自己的 lldb 会话）

- **`semantic_search` 不再因缺少密钥缓存整条失败**：展示名富化读不到名册时回落裸
  `chat_id`（与 `search_history` 同一条降级），此前 `purge --keys` 之后该工具 100% 报
  `KeyNotFound` 并把用户引向需要 sudo 的 bootstrap

### 架构深化重构（C1–C8 + 3 个缺陷修复）

内部重构为主。用户可见的变更**只有下列 10 处**：

- CLI `recent -n` 的上界与 MCP 对齐 **1-500**（原为无上限）
- `export_full_history` 的 `contact` 歧义从「误报成找不到」改为返回 `need_confirm`（候选含 kind）
- `list_contacts` / `search_history` 的 `limit` 补下界 1（原为原样透传，`-1` 会变成 SQLite 不限量）
- 错误路径不再杀死 MCP server：`SystemExit` 曾逃出工具处理器（微信未运行时调 5 个工具中的
  任意一个必现），现统一返回 `{"error"[, "hint"]}` 信封；`docs/TOOLS.md` 那句
  「工具不抛异常到传输层」由此由假变真
  （订正，EA-027：那次修复覆盖的是 `Exception` 面；`SystemExit` 由**另一条不变量**兜住
  ——core 任何模块都不抛它，`tests/test_errors.py::test_core_never_raises_systemexit`
  按 AST 钉住。TOOLS 的表述已同步收窄）
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
  （09-29 补上 `--media` 与 `--moments`，见下一节）
- 默认 dry-run，`--yes` 才执行；删完给出残留核查（计划内是否清空、按范围保留了哪些、
  仍有进程持有哪些文件、`$TMPDIR` 遗留的 `awcap_*`、Time Machine 排除状态、本地快照）
- 拒绝执行的三种情形：有进程持有待删产物；待删路径是符号链接——**文件型具名产物（`--keys`/
  `--index` 的目标）本身是链接时无条件拒绝**（`unlink` 只删链接、目标必留存，核查会报「已清空」）；
  **目录型具名产物（`--tmp` 的目标）与 `--all` 走 `rmtree`，还会扫各自那棵树，只拒绝解析目标
  落于 `~/.agent-wechat` 之外（或判不出来）的链接**——树内的随整树一并删除，huggingface_hub 的
  `models/` blob/snapshot 布局就是这种；`AGENT_WECHAT_HOME` 指向不像数据目录的位置（空值会被
  解析成当前目录，或指向 `/`、`~` 及其祖先）时，dry-run 与 `--yes` 两处都拒绝
- 只做 CLI，MCP 工具面不变

### purge：补上 `--media` / `--moments` 两个具名范围（**行为变更**）

09-13 的 purge 只认识三个范围，而 `media/` 与 `moments/` 是更晚落地的两类产物（09-15 /
09-16）。实测后果：`media/` 有 **10.22 GB**、489 个会话目录，内容是**解密后的**图片/视频/
语音（32,093 个文件）外加 5 份**用浏览器双击就能打开的**会话 HTML/Markdown——而它在计划表里
印成「（不属于任何具名范围）」，整行**没有敏感度、没有重建代价**，比同目录里 5.59 GB 的
`index.db` 还不可见。用户可见的变更：

- 新增 `--media` / `--moments` 两个开关，与既有三个可任意组合；`--all` 语义不变（超集）
- **目录型产物改显整树体积**（原为 inode 自身大小，渲染器只好一律印「—」）——修在源头
  `_item()`，于是计划表与 `--yes` 汇总行是同一个数字；代价是最大的一项不再看起来无事发生
- `models/` 与 `tasks.json` 从 loose 段移入 items 段，因此也拿到「敏感 / 重建代价」两列
  （此前只有一句「已知产物：…」）
- 真正无主的条目，敏感度列由**留白**改为 **`未知`**——留白读起来是「已检查过、不要紧」，
  而真实含义是「没人认识它」
- 请求一个不存在的范围改为**响亮拒绝**（`PurgeRefused`，退 1）。此前是静默空操作：印一张
  空表、报「已删除 0 项，释放 0 B」并退 0
- 内部：`Artifact.label` 字段删除（最后一个读者随 loose 分支一起消失，留着就是无人核对的
  第二份来源）；范围名此前在三处枚举，后两处（`cmd_purge` 的 scopes 元组与 `_build_parser`
  里手写的范围校验）已改为从产物表演绎；`media/` 与 `moments/` 的路径同理收敛到各模块的
  `export_root()`——它们此前在 5 处内联拼写

### 会话级销毁（`purge --chat` / `--undo` / `--excluded`）

销毁的**第二个轴**：按会话删，且删除必须**永久**。既有的五个具名范围只回答「删哪一类文件」，
`export --full` 一跑被删的就原样回来——那在「回收空间」的语义下正确，在**隐私**语义下不成立
（设计见 `内部设计记录`）。

- 新增 `agent-wechat purge --chat <会话> [--yes]`：删该会话在本工具里的副本——`messages`
  （FTS 由既有触发器跟随）、向量、`msg_parts`、`media_refs`、`display_names`，以及
  `media/<会话md5>/` 整份导出物；与具名范围 / `--all` **互斥**（退 2），默认 dry-run
- 新增排除清单 `~/.agent-wechat/exclusions.db`（新模块 `exclusions.py`）：墓碑**逐条**刻在被删
  那批 `msg_key` 上，由 `store.connect()` ATTACH 进索引连接，四个写入端（`upsert_batch` /
  `upsert_msg_parts` / `upsert_media_refs` / `replace_roster`）在**同一个写事务里**按它拦——
  于是 `export --full` / `appmsg rebuild` / `media rebuild-refs` / 名册重建（15 分钟一次自动
  发生）四条复活路径全部堵住，而删除之后新来的消息照常进索引
- 新增 `purge --chat <会话> --undo`：**只删墓碑，不恢复数据**——被删内容不会因此回来，只有
  将来的新消息能重新入库；要找回须重跑 `export --full`（那时它们不再被排除）。索引已被
  `purge --index` 删掉时照样可用
- 新增 `purge --excluded`：列出排除清单的规模，按会话 md5 分组（不查索引，故 `purge --index`
  之后也能跑）
- `exclusions.db` 是**唯一不可重建**的产物（索引 ≈2.5–3 小时、密钥要重 bootstrap、`media/`
  要联网重抓，它不能）：`purge --index` **不动它**，`purge --all` 会连它一起清——两种情况 CLI
  各印一句，后者明说「被删内容下次导出会回来」
- 两条顺序约束落在 `purge.execute_chat`：`PRAGMA secure_delete=ON` 在任何 DELETE 之前；墓碑
  **先落**（独立事务提交，成文理由是跨库事务在 WAL 下不保证崩溃中途的原子性）再删，且删除事务
  里从同一谓词重派生一次（`INSERT OR IGNORE`）
- rowid 复用陷阱（`messages.id` 无 AUTOINCREMENT）：删掉持有最大 id 的行后，新消息会拿到
  ≤ 向量水位线的 id、静默不被嵌入；`purge --chat` 在同一事务里把
  `vector_watermark.last_rowid` 降到 `min(水位线, 该账号 max(id))`
- **保证的边界**：是「索引里没有存活行 + 它们的 `msg_key` 进了排除清单」，**不是**「字节层面
  不存在」——`secure_delete` 只清 SQLite 自己释放的页，而 vec0 的 `preview` 列装的是相邻消息的
  正文，chunk 没整块清空时仍可能留在活跃页里。要字节级清除只能 `purge --index` 后重跑 `export`
- 边界：**朋友圈不在范围内**（`moments/` 是一份 HTML 装所有人，不按会话分目录），计划表会把这句
  原样印出来；只删本工具的副本，微信容器一个字节都不动
- 计划表另写明**「执行期间正在跑的导出任务会因写锁而失败」**——本命令先取写锁再读，另一侧拦不住
  （purge 是独立进程，`tasks` 注册表一读就把 running 误判成 failed），只能先说
- **会话一条消息都没有时照样走完整流程**：计划表各项 0 条、**不写墓碑**（边界是「只清当时已有的」），
  但 `media/<会话md5>/` 那一行是真的——它存不存在与索引里有没有行无关（典型是 `purge --index` 之后
  索引被逐会话重建、这个会话还没被重新导出），`--yes` 会把它删掉
- 计数诚实（`on_skipped` 契约）：导出 / 回填的返回值只数**实际入库**的行，被排除挡下的以一条
  降级（stderr）报出「跳过 N 行（已排除）……」——量词是**行**不是「条」：`media rebuild-refs`
  与 `appmsg rebuild` 一条 `messages` 都不写，回调拿不到消息条数
- **`rows` 的语义随之变了**（CLI 与 MCP 同源，有意为之，spec §5.4）：`export_full_history`
  完成后的任务 `rows` 读的就是上面那个返回值，于是从「**读到**多少行」变成「**写入**多少行」；
  CLI 侧同源的还有 `appmsg rebuild`——`parts` 报的是**落库**条数，被墓碑挡下的不再计进去。
  **MCP 面的承诺补在 `docs/TOOLS.md` §5**（两个 kind 的 `rows` 分开写清，并记上被排除挡下
  时随任务记录带回的 `skipped`），agent 若据 `rows` 判断导入了多少，读到的就是新口径
- 只做 CLI，MCP 工具面零变化

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
- **视频**：聊天视频本身是明文 mp4，直接复制（本体不在时回退缩略图）。会话归属见下文
  「视频导出改为按索引归属」——视频树 `msg/video/{月份}/` 本身没有会话维度
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

### 媒体与消息关联（`get_message_media` / `media locate`）

- 新增第 11 个 MCP 工具 `get_message_media` 与 CLI `agent-wechat media locate`：按消息回答
  「有没有媒体、什么类型、本机在不在、在哪」。`export_chat_media` 按会话批量导出且信封不回传
  文件清单，这里由调用方用 `keys` 收窄范围，所以逐条回传明细不会膨胀上下文
- **三处消息信封新增稳定标识 `msg_key`**：`get_recent_messages` / `search_history` /
  `semantic_search` 返回的消息统一带 `account|chat_md5|shard|local_id`（由消息的身份/位置
  字段派生，跨导出不变），用来把检索命中交给 `get_message_media` 指认消息。它与
  `search_history` 里既有的 `id`（`messages.id`，DB rowid，随重建变化）**不是一件事**——
  字段名之所以不叫 `id`，也因为那个名字在检索命中里已被占用
- 四态 `status`：`none`（不属于本功能覆盖的媒体类型——文件、位置、appmsg 等都归这里，
  **不是**「这条消息没有媒体」；**贴纸不在此列**，见下文「贴纸可读标识」；
  **只在给了 `keys` 时才可能非零**——省略 `keys` 时它恒为 0）/ `exported`
  （产物已在本机，`path` 是绝对路径）/ `exportable`（有媒体、本体还在微信里，未导出；
  **贴纸永不产出这一态**）/ `gone`（有媒体但本机找不到；**贴纸的 `gone` 说的是
  「取不出来」**，见下）。判定顺序严格按此序；信封另给 `counts` 汇总与 `by_kind` 拆分
  ——各类的本机可得率相差几个数量级，池化会把「语音几乎全可导出」与「图片视频与贴纸
  几乎全 gone」加成一个数字
- 索引为空（从未跑过 `export_full_history`）时，MCP 信封带 `hint` 说明——否则那份
  「`counts` 全 0 + `messages: []`」与「这个会话没有媒体消息」逐字节相同
- **`gone` 是常态、不是错误，且不是「微信把老媒体清理干净了」那种一次性事实**：微信只为
  **近期（尚未导出）**的消息保留媒体本体，所以对索引内已有的消息，图片本体兑现率约
  **0.005%**（2 / 39,307）、视频 **0**（/ 4,404）——导出更多历史会把那些消息带进索引，
  它们仍在的本体才可见。语音相反：本体存在 `media_*.db` 里、微信不清理，**7,390 条 100% 有
  本体**，因此 **`exportable` 在本机实质上是语音专属状态**
- `exportable` 只保证有可定位的**产物**：视频命中缩略图时也算（本机只有一张 jpg，`mp4`
  本体不在）——逐条明细不区分这一点。语音库读不到时降级，**尚未导出**的语音一律按 `gone`
  呈现（已导出的仍照实报 `exported`），不假装成 `exportable`
- **不写微信目录、不写 `out_dir`、不联网**：判定只做 `pathlib` 探测与索引只读查询——唯一的
  磁盘写入是**语音本体判定**要解密 `media_*.db`，明文只在 `tmp/`（0700）短暂存在、用后即焚
  （既有机制，见 SECURITY.md §4「临时文件」；零网络例外无需新增）

### 视频导出改为按索引归属（**行为变更**）

- **多会话导出视频从此成立**：`msg/video/{月份}/` 这棵树没有会话维度，此前只有「恰好一个
  会话」时才归属，多会话直接跳过并记 `degraded`（文案：视频为账号级枚举，无法归属具体会话）。
  现在按 `media_refs` 里每条视频消息的 `file_key` 逐个定位本体，任意会话数都能归属
- **新增对索引的依赖**：视频导出要查 `media_refs`（用例层打开索引库并交给导出）。**视频一条
  都导不出时不再是静默的 0 条**（此前直接按目录枚举），而是按成因分报三种情形——各自能做
  的动作不一样，混成一句话会把用户支去跑修不好的命令：
  1. 该账号一条视频引用都没有 → 索引建立在 `media_refs` 之前 → 跑 `agent-wechat media rebuild-refs`
  2. 请求的会话在索引里没有视频引用（该账号其它会话有）→ **无需修复**。注意这只说明「没有
     引用」，**不等于**这些会话确实没有视频消息——索引里本就有视频消息拿不到引用
     （实测 222 条），它们的会话会被归到这一条
  3. 请求的会话**有**引用、但它们的**消息**不在索引里 → 只有**导出这些会话的历史**有效
     （`export_full_history` / CLI `export` 会同时写入消息与媒体引用；`media rebuild-refs`
     **只写引用、不写消息**，对这种索引永远修不好）
- **视频导出不再有账号级兜底**：索引库打不开（损坏/不可读）时**本次不导视频**并如实降级——
  绝不退回旧的账号级枚举：那会把账号视频树里的**全部**文件写进本次请求的会话目录（错归属，
  比缺归属更坏）。其余类型（image/voice）照常导出
- **`video.missing` 的含义变了**：此前只数「已在磁盘的文件读不出来/写不下去」（本机通常为 0）；
  现在还会数「有视频消息但本机没有本体也没有缩略图」——微信按需下载并定期清理，本机实测
  4,626 条视频消息里只有 30 个文件还在。**几千的 `missing` 是如实回答，不是故障**；
  真正的故障看 `ok`/`thumb` 是否意外为 0
- 产物命名与落盘布局**不变**（`{out}/{会话md5}/video/{月}/{file_key}.mp4` /
  `{file_key}_raw.mp4` / `{file_key}_thumb.jpg`，目录 0700、文件 0600），既有产物不受影响
- 不导视频的调用（`kinds` 只含 image/voice）**不碰索引库**，行为与失败面完全不变

### 朋友圈读取与导出（moments export）

- 新增 MCP 工具 `export_moments` 与 CLI `agent-wechat moments export`：读取本账号可见的
  朋友圈动态（正文 / 作者 / 时间 / 评论 / 点赞），导出为**单文件自包含的 HTML 时间线**，
  双击即可离线阅读
- 数据源是微信的 `db_storage/sns/sns.db`（复用既有 passphrase 解密，不进 `index.db`）；
  产物落 `~/.agent-wechat/moments/`（目录 0700、文件 0600），微信目录仍只读
- **新增一条显式的「零网络」例外**：朋友圈的媒体全部是远程的，只有显式指定
  `--with-media`（CLI）/ `with_media=True`（MCP `export_moments`）时才联网下载并解密；
  **默认全程零网络，一个请求都不发**。联网时只连微信 CDN——后缀 `*.qpic.cn` /
  `*.video.qq.com`，**本期起另加精确主机 `wxapp.tc.qq.com`（不含子域）**，见下文与
  SECURITY.md §1——**直连不走代理**、只携带微信自己签发的 token
  （详见 SECURITY.md §1「零网络例外」）
- 产物页首如实标注**「这是微信本地缓存窗口，不是完整历史」**（本机实测 1,923 条覆盖
  2013-02 ~ 2026-09，且密度极不均）
- 三个新模块：`moments.py`（离线解析）、`sns_media.py`（当时唯一直接向微信 CDN 发请求的
  模块：URL 构造 →
  直连 → WX-ISAAC64 流解密，含域白名单与跨重定向校验）、`moments_html.py`（渲染）
- **`media_failed` 是常态而非错误**：微信服务端会清理老媒体，本期 620 条可下载媒体实测
  58 条未落盘（约一成，且 58 是上界）；单条媒体失败只计数，不中断整次导出
- **`--limit` 截断留下的是最新的动态**（此前按库里的扫描序截断，留住的是**碰运气的那一段**
  ——本机实测 `limit=2` 留在 2024-11-14/11-23，而真正最新的两条在 2026-09-13/09-14，
  `window` 也跟着报错）
- **解析失败不再无声**：MCP 信封的 `counts` 新增 `failed`（动态 XML 解析失败的条数，
  与 `media_failed` 无关），`degraded` 与产物页首各有一条说明。此前微信改了动态的 XML
  形状会得到「退出码 0 + 0 条动态 + 空降级」，与「这个账号没有朋友圈」逐字节相同
- 同类降级**只写一条并带条数**（此前每条「解密后不是已知图片格式」的媒体各写一行、且不带
  条数；纯网络失败则完全不写行）
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
- **远程媒体成功率受服务端保留策略影响**：本期 620 条可下载媒体实测 58 条未落盘（约一成，上界）
- **`content` 为空**的评论渲染 `[表情评论]` 占位（表情评论即落此支；判据是「空」而不是
  「是不是表情」），本体不下载；分享链接自带的第三方缩略图不是朋友圈
  媒体，不抓取
- 发布 / 删除动态、小程序卡片内嵌渲染均不做（微信目录恒只读）

### 会话时间线导出（appmsg 富文本，HTML / Markdown）

- 新增两个 MCP 工具：`export_chat`（§12）与 `get_message_parts`（§13）；工具面 11 → 13
- 新增三个 CLI 命令：`agent-wechat chat export`（某会话 → HTML / Markdown 时间线）、
  `chat index`（已导出会话的索引页）、`appmsg rebuild`（回填 appmsg 原始载荷）
- **appmsg 的原始 XML 首次落库**：索引库新增 `msg_parts` 表，`raw_xml` 是正文的**权威**，
  `appmsg_type` / `ref_svrid` / `ref_msg_key` 是它的**投影**（存在的唯一理由是「不必解压就能
  筛选与重解析」）。`messages.content` **一字未动**（仍是 `[appmsg: 标题]` 投影）——改它要
  重建 FTS + 向量两套索引
- **解析形态是本轮最大的一处实测**：群消息的 `<sender>:\n` 前缀**没有被剥掉**（`reader` 只对
  文本消息剥），`<?xml?>` 声明可能缺失。不做规范化，appmsg 的可解析率只有 **32.8%**；三步
  规范化后 **99.9985%**（66,655 / 66,656，**设计期普查的语料**）。**真机回填重扫实测
  `unparsed` = 0**（66,669 条，见 FAQ Q17）—— 两个分母不同时点，别并排当同一个读。若按
  「`content` 就是 XML」写实现，**2/3 的 appmsg 会静默消失**
- **四类具名建模**（引用 57 / 链接 5 / 文件 6 / 转账 2000），其余类型进 `fields`（点分路径）；
  含义未确认的类型回落成 `appmsg:<type>`，**不猜**。凭据类字段（`aeskey` / `token` / `key` /
  `passphrase`，名字或值里出现）**进不了信封**
- **危险载荷守卫**：含 `<!DOCTYPE` / `<!ENTITY` 的载荷在送进 XML 解析器**之前**直接拒绝
  （防 billion laughs）；与「解不开」**分开计数**（`rejected` / `unparsed`）。实测那两个串在
  全量 appmsg 里出现 0 次，故零误伤
- **回填 `msg_parts` 是显式的一次性动作**（`appmsg rebuild`，幂等）：**只写 `msg_parts`**，
  不碰 messages / FTS / 向量 / 水位线（本机回填前后逐表指纹相同）。本机实测 **37.5–38.7 s**、
  `index.db` **+232.8 MB**。MCP 侧**没有**对应工具
- **`ref_msg_key` 是快照**：被引用的消息若后来才导出进索引，重跑 `appmsg rebuild` 即刷新。
  渲染**从不依赖回指成功**——被引用的原文一直带在 `refermsg` 里，回指只决定「能不能跳」
- **两个模板的语义一致由单一来源保证**：`chat_text.py` 收拢措辞与纯函数（时间 / 相对路径 /
  四态文案），`ChatView` 是唯一数据源。Markdown 的**四处固有损失如实记在 USAGE.md**，
  不假装与 HTML 等价
- **`chat export` / `chat index` 与 `media export` 必须用同一个 `--out`**：产物在
  `{--out}/{会话md5}/chat/` 下，媒体是 `../image/…` 形式的相对引用、指向**同级**的既有媒体树；
  用错根时媒体块退回 `exportable` / `gone` 占位（不会出现打不开的 `<img>`；`gone` 读作
  「这个根下没有」，spec 裁定 13）
- **`get_message_parts` 照实返回转账字段**（金额 / 备注 / 双方用户名 / 交易号）：与
  `search_history` 已在信封返回正文一致，**不为转账单开例外**——这是刻意的裁定，记明于
  SECURITY.md §1
- **`chat export --out DIR` 与既有 `media export --out` 同一类例外**：会把会话正文明文写到
  `~/.agent-wechat/` 之外，用后自行清理（SECURITY.md §1 的例外表）
- `--months` 必须是**升序且相邻月首尾相接**的一段窗口：非连续（`2026-08,2026-10`）只能取
  包络、会把中间的月份静默带进来，反序会得到倒置（或退化成空区间）的窗口——两者都**报错**，
  不静默产出空产物
- 新模块 6 个：`appmsg.py`（规范化 + 守卫 + 解析，纯函数）/ `chat_text.py`（两模板共享的
  措辞）/ `chatview.py`（`ChatView` 组装与落盘）/ `chat_html.py` / `chat_md.py`（两个纯模板）/
  `chat_index.py`（索引页）

### 已知限制（会话时间线导出）

- **引用原文仍搜不到**：`messages.content` 一字未动，`search` / `semantic` 检索不到被引用的
  原话。`raw_xml` 已在库里，日后补检索只需加一张独立 FTS 表
- 未回填 `msg_parts` 时 appmsg 只显示 `[appmsg: 标题]`（信封带 `degraded` + `hint` 指出，
  跑 `appmsg rebuild` 补齐）；**回填之后新到微信**的 appmsg 也要重跑回填才有内容——回填扫的是
  微信库、`export` **不写** `msg_parts`
- `get_message_parts` 少一个 key 时**三种空答案里信封只分得出第一种**（未回填，带
  `degraded` + `hint`）：另有「回填跑在它之前」与「这条本来就不是 appmsg」——**这两种彼此
  逐字节相同**且都不带 `degraded`
- `chat export` 只导**范围里最近的** N 条（`--limit`，默认 500）；`truncated` 的语义是
  「**可能**还有更早的没进来」——范围内恰好 N 条时它也为真
- 媒体状态最多答 500 条：**没覆盖到的消息在产物里没有媒体块**（信封的 `degraded` 给出
  「答了 N/M 条」）
- 合并转发（19）的内层 `<recorditem>` **不做专门建模**，也**不进 `fields`**：它是**内层 XML 载荷**
  （转义后嵌进来的第二份 XML），与 `refermsg` 同类，任何 kind 下都跳过。同层的可读摘录
  （`<des>`）照常留在字段表里。要看完整的子消息列表请读 `msg_parts.raw_xml`
- `msg_parts` **没有** `purge` 具名范围（既有缺口，与 `moments/` 同命）：只能 `purge --all`
  连带删

### media / moments 导出：`--limit` 新口径与 2000 硬顶放开（**行为变更**）

- **`media export --limit` 换了口径**：现在是「本次最多**新增**多少个文件」，产物已存在的会被
  跳过。于是 `ok` 只数**本次新写**的文件——改前每次都是全量重写，`ok` 当时是**本轮写下的
  产物文件数**（读起来像该会话的媒体总数，但不含微信已清理、导不出来的那些），现在只数本次
  新写的。重复调用同一条命令会从断点向前推进，不再每次都从头跑同一批
- **新增 `--force`**：忽略已存在的产物、全部重做（默认行为即上面那条）。它**不**是「修复缩略图」
  的开关：本体已在产物里的视频不会再生成 `{K}_thumb.jpg`
- **两处 2000 硬顶移除**：`media export` 与 `moments export` 的 `--limit` 此前被静默夹到 2000
  （`--limit 99999` 实际只导 2000），现在只保留下界 1；两处 CLI 帮助文案同步订正
- **新增 `skipped` / `remaining` 计数**：`export_chat_media` 的 `counts` 三个类目（`image` / `voice` /
  `video`）恒含两者；`export_moments` 的 `counts` **只带 `remaining`**——朋友圈每次全量重渲染，
  没有「产物已存在即跳过」这回事，故无 `skipped`。CLI 每类印 `skipped`，`remaining` 只在触及
  `--limit` 的那一行印（三类之和）。
  `skipped` 是本次因产物已存在而跳过的**文件**数（**按文件**计，不是消息条数）；`remaining` 是
  撞上 `--limit` 时还有多少个**未导出**——它含续跑时仍会失败的条目（dat 读不出、本体不存在），
  所以不等于「续跑一定多出这么多文件」
- **`moments export` 截断时报出条数**：原先只说「本次被截断」，现在多带「还有 N 条更早的动态
  没进来」
- **`media export --limit` 只封顶写入**：跳过判定排在 `--limit` 判定之前，撞顶后仍会扫完所有
  剩余条目才返回（图片与视频是逐个 `stat` 产物路径；语音还要把每条 `voice_data` 从库里读出来）。
  大语料上命令看起来「还在跑」，那是扫描不是卡住

### 朋友圈四类新卡片：外链 / 视频号 / 小视频 / type=7（**行为变更**）

- **四类此前读不到或取不到的卡片现在都能出**（四类缺口各自计数之和为 448，**去重后 446 张卡**
  ——有 2 张 `type=7` 卡同时带 `finderFeed`，在两类里各计了一次；约为初版立项陈述的
  206 张的 2.2 倍）：
  - **外链卡片**（178 条）：读 `ContentObject/contentUrl` 与同级 `title`，渲染成可读可点的链接。
    **零网络**——只导出卡片自带元数据，不做预览抓取（178 条里有 4 个垃圾推广域名，抓取会把
    本机 IP 暴露出去）；`support.weixin.qq.com` 的「微信版本不支持此内容」桩不算外链
  - **视频号卡片**（29 条）：媒体在 `ContentObject/finderFeed` 下而不在 `mediaList` 里，
    此前**根本没被解析**（不是「读到了没渲染」）。现解析昵称 / 描述 / 时长，并下载封面。
    **视频本体不下载**（29 条里只有 16 条带 token，其余 13 条形态未验）
  - **小视频 `type=15`**（180 条）：此前被「必须有 `url@token`」挡住——它的 token 在
    **URL 查询串**里，不在属性上。现判 `AUTH_READY`（URL 已完整，原样 GET）并下载视频本体
  - **`type=7`**（61 卡片 / 62 媒体）：无签名公开图，同样被 token 前置条件挡掉，现可下载
- **视频封面**：`KIND_VIDEO` 的媒体额外下载封面（落 `{stem}_thumb.jpg`）。封面是**加密**的，
  用的就是同一个 `media` 元素的 `enc@key`。封面**同样受 CDN 白名单约束**：宿主不在白名单的
  封面（真机 158/181 在 `vweixinthumb.tc.qq.com`）**静默跳过，不计任何计数器**——既不算
  `media_failed`，也不算 `media_not_downloaded`（与下面「白名单外的媒体改为『不去取』」那条
  **不同**：那条针对 `p.media` 的成员，而**视频封面**不是 `p.media` 成员——它由本体那条媒体
  派生、与本体同目录。**别推广到视频号封面**：那是 `_finder_media` 产出的一条 `p.media`
  成员，照常计进各桶）
- **`auth` 语义**：`AUTH_READY` = URL 已完整（自带签名或无签名公开），`build_url` 原样返回、
  只做 `http→https`，**不拼 `idx=1`、不做尺寸段归一**；`AUTH_SNS` = 老路径。
  **无 `token` 不再意味着「不可下载」**
- **联网白名单变了**（SECURITY.md §1 同步更新）：不再只判两个后缀，而是**后缀 `*.qpic.cn` /
  `*.video.qq.com`，加精确主机 `wxapp.tc.qq.com`（不含子域）**
- **白名单外的媒体改为「不去取」**：`build_url` 在有请求之前先判 host，不在白名单直接返回
  `None` → 落 `media_not_downloaded`，**不计 `media_failed`**（「不去取」不是「取失败」）。
  这同时修掉一处既有的账目错误：`mmsns.c2c.wechat.com`（34）+ `wx.qlogo.cn`（1）共 **35 条**
  媒体此前被记成「下载失败」，真相是「不去取」
- **`media_failed` 的语义澄清**：它计的是**失败次数**而非媒体条数——一条视频本体成功、
  封面失败时同时贡献 `media_downloaded +1` 与 `media_failed +1`

**已知代价**：

- **`--with-media` 变重了**：本期**新增约 0.62 GiB**（小视频 622.8 + type=7 10.5 MiB，两者
  均为**卡片声明值**而非实际落盘量；视频封面可达仅 23 张、实测单张 27.9 KB，合计 <0.01 GiB）。
  本机媒体**总量约 1.5 GiB**；此前 `--with-media` 对这批内容的下载量是 **0**
- **不做增量续跑**：`_resolve_media` 没有 `already()` 跳过，**每跑一次 `--with-media` 就要
  重下这么多**（0.62 GiB 是新增部分，既有内容照旧重下；用户裁决不在本期解决）
- **视频号的视频本体**与**外链预览抓取**不在本期范围（见上）

### 贴纸可读标识与两处凭据修复（**行为变更**）

**没有实现贴纸导出**——原因见本节末尾，那是本轮的结论而不是省略。

- **贴纸消息的正文从裸 `[sticker]` 变成可读标识**：`content` 为 `[贴纸:名称]`，名称取不到
  时为 `[贴纸#md5前8位]`。短 ID 让 LLM 分得清「同一贴纸发了 5 次」与「5 个不同贴纸」，
  这是零成本的现成对话信号。**`media_hint` 不变，仍是 `[sticker]`**——两列语义不同是刻意的
  （`content` 是「这条消息说什么」，`media_hint` 是「它是什么类型」）。
  名称来源与优先级：消息 XML 的 `desc` → XML 的 `emojiattr` → 本机
  `emoticon/emoticon.db` 按 md5 反查 → 回落短 ID。**实测名称覆盖率只有约 9%**
  （67,833 条 type-47 里绝大多数来自本机没安装的表情包），所以九成贴纸只显示短 ID，
  那是如实呈现而不是解析失败
- **贴纸不再是 `none`，`by_kind` 从三项变四项**（`image` / `video` / `voice` / `sticker`）：
  贴纸是**有类型**的媒体消息（`msg_type=47` 明确、身份 md5 100% 可得），
  `get_message_media` / `media locate` 现把它归入媒体四态。
  **它只可能答 `gone`，永不产出 `exportable`**：`exportable` 的文档语义是「有产物可拿、
  你还没导出」，而贴纸拿不出来——产出它就是谎报。**贴纸的 `gone` 也不等于「本机找不到」**：
  本机有没有那份文件（实测 38.3% 的消息有）与它取不取得出来无关，CLI 因此**另印一行**
  说明，不让通用的那句「导出更多历史比修复有用」把它盖过去
- 新增数据源 `emoticon/emoticon.db` 的 `kStoreEmoticonCaptionsTable`（贴纸 md5 → 中文名，
  925 行）。库缺失 / 结构漂移 / 解密失败一律**降级为空表**，贴纸只是回落短 ID，不中断导出
- **凭据修复（一）复合参数名**：`get_message_parts` 的 `fields` 此前按「参数名紧跟分隔符」
  匹配凭据，于是 `?key=` / `&aeskey=` 拦得住、而 **`&filekey=` / `&xsec_token=` 这类复合名
  整体放行**（`key=` 前面是 `file`，正则从 `f` 处断开）。现改成参数名**按同一份命名规则**
  判定，复合名一并拦下
- **凭据修复（二）长 Base64 不进字段表**：`emojiinfo` / `cdnthumburl` 这类字段名干净、
  凭据被 Base64 包住，两道名字/值规则都看不见（实测 505 条表情 appmsg 里 393 条携带，
  解码后是明文 CDN 地址）。现按**形状**剔除（≥128 个 Base64 字符的值），并以
  `dropped_blobs` 计数
- **`get_message_parts` 信封新增 `dropped_blobs`**（第三个计数）：它数的是**解析成功**的
  消息里被形状规则剔掉的长 Base64 块。**与 `unparsed` / `rejected` 不许并**——那两个说的是
  「读不出来」，这个是「按规则剔掉了」；字段表少几行时靠它区分「本来就没有」与「被剔掉了」
- **零网络例外没有增加**：本轮不新增任何联网代码，SECURITY.md 的两个例外维持不变

**已知限制（贴纸）**：

> **订正（2026-09-23）**：下面第 2 条「CDN 实测 0 响应」是**假阴性**，已推翻。
> 根因是探针用了裸 `urllib.request.urlopen`，而本机 macOS **系统代理**会被 Python 的
> `getproxies()` 装回来 ⇒ 请求全部超时。同机同 URL 改 `ProxyHandler({})` 直连后
> 0.2–0.3s 全部 HTTP 200。**别再引用「0 响应」这个结论，也别再去找「缺的那个签名参数」。**

- **贴纸导出的两条路，现状是「本机不行、CDN 行」**：
  1. **本机解密**：微信把贴纸存成加密格式，解密方案**未破**（两轮 spike，真机两向对照；
     见 spec 附录 C 与 E.4）。本机那 38.3% 的贴纸文件也是这份解不开的密文——
     而且**它是缩略档**（同一 md5：`Persist` 16,016B vs CDN 原图 67,167B），
     所以即使破了也不是等价替代；
  2. **CDN**：**可行**。`cdnurl` 返回**明文**图片（不需要 `aeskey`），2023 年的老贴纸
     `filekey` 也仍然有效。代价是**只能走明文 HTTP**、`filekey` 明文出网
     （见 SECURITY.md §1「零网络例外」第 3 条）。
  所以 `media export --kinds` 的合法值**现在含 `sticker`**，但**默认不导**：
  要 `kinds` 显式含 `sticker` **且** `with_cdn=true` 才取件。
  **本机有贴纸文件的比例（38.3%）不构成一条「有东西可导」的路**——别把它读成覆盖率
- 名称覆盖率约 9% 是硬上限（贴纸来自 703 个表情包而本机只装了 5 个），不是可修的缺陷
- ~~`media_refs` 里的 sticker 行**只写不读**~~ **（2026-09-23 订正：已有读路径）**：
  `media export --kinds sticker` 现在会消费它（归属按 `media_refs` 判定），
  `get_message_media` 的贴纸态仍然只由 `msg_type` + `exported_path` 判定

### 贴纸导出恢复（CDN 取件）

**用户可见的变更**：

- **贴纸可以导出成图片了**：`media export --chat X --kinds sticker --with-cdn`
  （MCP `export_chat_media(kinds="…,sticker", with_cdn=true)`）。**两个开关都要给**——
  `--kinds` 的默认值不含 `sticker`，`with_cdn` 默认关，两者都显式才发请求
- **新增零网络例外（第 3 条）**：贴纸 CDN 取件。**它走明文 HTTP**，是三个例外里唯一非加密传输的，
  请求 URL 携带该贴纸的 `filekey`（明文可见）。已在 SECURITY.md 详述，含「范围只实测过
  `vweixinf.tc.qq.com` 一个主机名」这条边界
- `media export` 多印一行「贴纸」计数（`ok` / `skipped` / `failed` / `missing`）；
  MCP 信封的 `counts` 多一个 `sticker` 键
- `get_message_media` 的贴纸态由「只可能 `gone`」变为「`gone` 或 `exported`」
  （导过之后就是 `exported`）——**仍然永不为 `exportable`**

**技术笔记**：

- **wxgf 贴纸会转成 JPEG**：全库实测 **0 个** wxgf 贴纸，所以这条分支本轮从未被真实数据走到——
  而它当时在贴纸侧**是漏的**（转码只接在图片分支上），遇到就会静默落一个 `file` 认不出、
  用户打不开的 `.wxgf`。已补成与图片分支同款：有 ffmpeg 转 JPEG，否则退回存原容器**并公告**。
  **它仍是零样本覆盖的路径**，只由合成夹具钉住。
- **载荷格式**：`cdnurl` 返回**明文**图片（4/4 实测：JPEG/PNG/GIF）；`encrypturl` 才是
  AES-128-CBC + PKCS#7、`key = IV = bytes.fromhex(aeskey)` 的那一份，**本设计不用它**
  （多握一把密钥、多一套解密，收益为零）。XML 的 `md5` 属性 = **明文对象**的 md5，
  下载后据此校验，不符即失败且**不落盘**
- **三个字段不可靠，禁止当判据**：`len`、`Content-Type`、`X-encflag` / `X-enclen`
  （后者对加密载荷也返回 0，与朋友圈可用的 `x-enc: 1` 不是一回事）
- **必须强制直连**（`ProxyHandler({})`）：裸 `urlopen` 会被 macOS 系统代理接管并超时
- **宿主白名单逐跳复查**：`*.tc.qq.com`（**订正，EA-023，2026-10-14**：事后按实测扩到
  三个后缀 `*.tc.qq.com` / `*.qpic.cn` / `*.wechat.com`，本条正文未动）。`sns_media` 那套不能复用——两个障碍是
  「贴纸 URL 是 http」与「`vweixinf` 不在其白名单里」，**不是**「域不对」
- **本机支线不建**：那份密文解不开、且是缩略档

### 群 sysmsg 的前缀残留与 XML 泄漏（**行为变更**）

**这是既有缺陷，不是本轮引入的**，但本轮把它修了——它是「凭据进信封」那一类里唯一还活着的一条。

- **带真实 `<aeskey>` 的那条系统消息不再落进 `messages.content`**：`reader._decode_row` 原本只在
  `msg_type == 1` 时剥群前缀 `"<X>:\n"`，而群里的 `sysmsg`（`msg_type=10000`）同样带这层前缀，
  于是兜底的 `content.startswith("<?xml")` 判据失配，整段 XML 原样入库。实测本机索引里
  **恰好 1 行**（一条群里的「邀请你加入了群聊」系统消息，正文含 **4 处 `aeskey`**）——
  `messages.content` 是 `get_recent_messages` / `search_history` 直接交给 LLM 的字段，
  还进 FTS，所以这是**凭据进了信封**，和 `raw_xml` 那条（从不进信封）不是一回事。该行现显示 `[system]`
- **顺带清掉群 sysmsg 正文里的群 id 前缀**：实测 **1,952 行**（全部 `msg_type=10000`）的
  `content` 是 `<群 id>@chatroom:\n你撤回了一条消息` 这种形态。现在前缀被剥掉。
  **爆炸半径实测是 1,952，不是 spec 原先写的 12,681**——12,681 是 `msg_type=10000` 的总数，
  真带前缀的只有 1,952
- **剥前缀的判据对非文本收窄到「head 以 `@chatroom` 结尾」，且绝不把它当发送者**：
  实测那 1,952 条的 head 都是 `<digits>@chatroom`，但**代码只判后缀**（不判数字形态）——
  这样将来出现别的群 id 形态也照样清得掉。之所以要收窄：本机有 1 条群 appmsg 的**标题自带**
  `":\n"`（一段带冒号换行的配置片段），放成「非文本一律剥」会把它削坏；而 head 是**群 id**，
  当发送者会污染 `sender_id`。文本那条路径一字未动
- **非文本整段 XML 一律换成类型占位符**：回落判据从 `startswith("<?xml")` 放宽到
  `startswith("<")`。原因——真机群 `sysmsg` 里带 `<?xml` 声明的只有 **1 行**，而根直接是
  `<sysmsg …>` 的有 **1,960 行**：只认声明会把多数派放过去，原始 XML 照样喂给上层。
  放宽后受影响共 **2,221 行**（全部 `msg_type=10000`）。这条判据只到得了 `hint is None`
  的类型（`10000` / `10002`），**人类文本走另一条分支**——实测 18 行以 `<` 开头的 `type=1`
  （有人粘了 HTML、有人写 `<video>就能显示hdr`）**原样保留**
- **两次重导的账要分清**：第一轮（16:31）写 5,640 行 = 988 新 + 4,652 改写，改写里
  **1,952** 是本条的前缀剥离、**2,700** 是贴纸名解析（`[贴纸#md5]` → `[贴纸:名字]`，
  由本分支更早的属性正则放宽带来，与本条无关）。第二轮（放宽判据后）再改 **2,221** 行。
  **两轮里 `msg_type=1` 都是一行未动**，与「文本路径没改」一致
- **已知代价**：向量索引是**水位线**式的（`messages.id > last_rowid`），改 `content` 不移动 `id`，
  所以这些行的 embedding **不会自动重算**。判为可接受（系统消息的语义检索价值低）；
  要抹平得跑 **`vector-build --rebuild`**（MCP `build_vector_index(rebuild=true)`）——
  不带 `--rebuild` 只增量追加水位线之后的行，对已存在的行**是 no-op**

### 标识外流拦截：扫描器与两个钩子、备份排除、`/tmp` 清理

本仓库的敏感数据（真机 wxid / 群 id / 聊天正文）**已经三次被提交进 git**：2026-09-13、
09-17、09-27（其中 09-17 那次随自动推送到达了远端；远端为私有仓库）。每次的处置都是
「事后清运 + 重写历史」，**下一轮又复发**——唯一反复复发的那条出口此前没有任何机制。
本轮补上自动层，并把「出口有哪些、各自有没有机制」写进 `SECURITY.md`（新增 §6「出口清单」，
原免责声明顺延为 §7）。

**用户可见的变更**：

- **新增 `security/scan.py`**：扫本仓库里形状像真机标识的字符串，起初只认三种**形状**
  ——`wxid_*`、`*@chatroom`、手机号（2026-10-12 补上第四种「本机路径」，见本文件末尾
  那一节）。默认逐处打印 `文件:行号 · 形状 · N 字符 · 未登记`，
  **绝不打印命中值本身**（打印了，它自己就成了新的出口）；要看值得显式加 `--show-values`，
  且会附带一句「它已进入你的会话记录」的警告。退出码三态：`0` 干净 / `1` 有未登记命中 /
  `2` 扫描器自身出错——**任何内部错误都非 0**（fail-closed，且与「发现泄漏」分开，
  免得扫描器坏掉被读成「没问题」）
- **两个 git 钩子，命中即拦**：`pre-commit` 走 `scan.py --staged`（只扫暂存区的**新增行**）、
  `commit-msg` 走 `scan.py --text <文件>`（**连提交信息一起扫**——2026-09-27 那次真标识就躺在
  提交信息里，只装 `pre-commit` 会漏掉这条已实际发生过的路径）。
  ~~**两个钩子都不入库**：装在该检出的**共享 hooks 目录**（`git rev-parse --git-common-dir`
  下的 `hooks/`，主检出与它的所有 worktree 共用），换 clone 或换机器要重装~~
  **（2026-10-13 订正：已改为「源入库 + `core.hooksPath`」——钩子源随仓库入库、`pull` 后
  立即生效、不需要重装；见本文件末尾新增的那一节）**；装法与理由见 `security/README.md`
- **豁免清单 `security/allowlist.txt`**：只许放**假值**（夹具里那类）。加条目走
  `scan.py --allow <指纹>`，**不要手抄**——手抄意味着你先把值读了出来；
  `--verify-allowlist` 检查死条目（在仓库里一次都没命中的条目）。`--values` 是逐值汇总视图
  （一次性审查归类用，值仍以指纹与「N 字符」呈现）
- **七项备份排除**：`~/.claude/projects`、`~/.claude/file-history`、`~/.claude/plans`、
  `~/.claude/history.jsonl`、`~/.claude/bash-commands.log`、`~/.claude/cost-tracker.log`、
  `~/.codex/sessions` 已 `tmutil addexclusion`。**这是「移出备份」，不是「清除」**——
  转录本身仍在盘上。`~/.claude/plugins`、`~/.claude/skills`、`~/.codex/plugins` 仍是
  `[Included]`：粒度判据是「承载对话内容的目录出备份，配置与插件照常备份」
- **`/tmp` 一次性清理**：删掉 6 条命中（5 条**别的项目**的 `osr_*` 快照 + 1 条本仓库会话的
  `tasks/*.output`），`wxid_` 与 `@chatroom` 两种形状复扫再无所获。**不改 `/private/tmp`
  的备份配置**（系统重启也会清）。注意 `grep` 在这棵树上以退出码 `2` 结束（有不可读的子
  路径），所以**结果是下界、不是穷尽**
- **文档**：`SECURITY.md` §1 补上此前未被文档化的那条出口——**MCP 信封的落点是磁盘上的
  会话转录**（`~/.claude/projects/*.jsonl`、`~/.codex/sessions/`），即同一条消息在本机
  至少有两份明文，且**已经发生、不可撤回**（删本机副本不撤回任何东西）；§4 存储控制表
  补「会话转录」一行；§5 边界补第 4 条（从本项目取数的仓库应装同一套提交拦截）

**边界：自动层只覆盖标识符形状，别把它当内容层**

- `scan.py` **扫不到**自定义微信号（alias）、群名、备注名、聊天正文片段、导出的时间线与
  媒体明文。**这不是实现遗漏，是形状扫描的原理边界**——能枚举的只有形状，枚举不了的是
  意义；内容那一层只能靠人工审查与位置控制
- ⚠️ **二进制 / 非 UTF-8 文件根本没有被检查**——与上一条是**两条不同的边界**：`--all`
  读不出 UTF-8 就跳过，`--staged` 对二进制文件一行都不扫（git 不为二进制给新增行），
  只在 stderr 点名警告（文件名，说明它没被检查）而**不改退出码**。上一条说的是「读了也
  未必看得懂」，这一条说的是**压根没读**——别把自动层读成「二进制也扫过一遍、干净」。
  同理，生成的依赖锁 `uv.lock` **整份跳过**（`--all` 与 `--staged` 同一条规则）：里面是
  第三方包元数据，实测单独贡献 47 处手机号形假阳性
- 同理，**验收判据不能写「扫描为 0」**：复查用的 `grep` 与 `/tmp` 的验收都只用 `wxid_*`
  与 `*@chatroom` 两种形状、**刻意不含手机号**——执行期实测，含手机号的那版在
  `/private/tmp` 单目录命中 **1000 个以上文件**（绝大多数落在别的项目 Rust 构建树的 UUID 段、
  浮点字面量、整数常量中间），而 `wxid_` 精确命中 6。**无条件的「扫描为 0」宣称的是一件
  照原样不可达的事**

### 扫描器补第四种形状：本机路径

CONTRIBUTING §4 那条「本机绝对路径不得入库」（示例与夹具用 `/Users/you`）此前**没有自动
层**——判据只写在人读的文档里。2026-10-12 一次全仓 grep 就查出 6 处 `/Users/<用户名>`。
现在它是第四种形状。

- **形状**：`/Users/<第一段>`，第一段不是 `you`（仓库钦定占位符，`daf9067` 实测仓库里有
  28 处合法命中）也不是 `Shared`（macOS 系统目录）。命中的是**第一段**（用户名）而不是
  整条路径——值稳定，指纹才稳
- **排除只认整段相等**：`yours` / `you2` / `Shared2` 照抓
- **字符集刻意是 ASCII**：文档描述脱敏规则用的记号 `/Users/<用户名>` 不自匹配——它出现在
  `内部记录/` 的验收记录里，若判成命中，它没有登记这条退路，只能去改写
  历史记录。把字符类放宽成「非斜杠的任意字符」，测试会红
- **已知漏面（刻意留的，已写进 SECURITY.md 覆盖边界）**：非 ASCII 用户名不拦；
  也不做大小写折叠（小写的 `/users/…` 不拦）——折叠会拿 `/users/<id>` 这类 URL 片段换
  假阳性，而本形状**没有豁免清单这条退路**，假阳性只能靠改写文字消化
- **豁免走结构、不走清单**：`.mcp.json` 与 `.codex/config.toml`（CONTRIBUTING §4 的那两个
  功能性本机配置，写的就是真实路径）**按 (文件, 形状)** 豁免，且**只豁免路径形**——
  其余三种形状在那两个文件里照扫。**不能改用 `allowlist.txt` 登记**：清单的规矩是「只许放
  假值」，而这里要放的是真路径，登记等于把真实用户名写进一个会被提交的文件
- **`--allow` 对路径形直接拒绝（退出 2）**：登记一个真路径**不会**被 §4.3 的不变式 1 抓到
  （那个值仍在原文里命中，不算死条目），所以这层拒绝是唯一的自动守卫——缺了它，
  「钩子拦下 → 顺手 `--allow`」会把泄漏合法化且没有任何一层会报出来
- **口径与复算**（数字要能自己数出来，别只记结论）：候选模式 = `/Users/` 后接一段，
  `git grep -c -E '/Users/[A-Za-z0-9._-]+' <树>` 逐文件计数（全仓 tracked 文件，求和即候选
  行数）；候选行数按 **`you` 行 + `Shared` 行 + 真路径行**三段互斥拆开，**三段之和必须等于
  总数**。
  **改动前（`daf9067`）实测：32 行 = 28 行 `/Users/you` + 4 行真路径**（`Shared` 0 行），
  那 4 行真路径**全在那两个被豁免的文件里**——**最终形状不匹配前 28 行**（这正是要排除
  `you` 的原因）。该提交自己新增的文档与夹具又添了 9 行（`you` 段 28→36、`Shared` 段 0→1
  ——测试夹具里的那一行、真路径 4 行没变，32+9=41），故它落地后同口径复测 **41 行
  = 36 + 1 + 4** ⇒ `--all` 仍是 0 未登记命中，**豁免清单的条目一条没加**（本次只动了它的
  头部注释）
- 验收：七个变异各能红一条测试（两处豁免各漏一处、`--allow` 不拒、排除退化成前缀判据、
  加大小写折叠、字符类放宽、豁免改成按文件整份跳过）

### 提交钩子改为「源入库 + core.hooksPath」

- **钩子源随仓库入库**（`security/hooks/`，带执行位）：换 clone / 换机器不再需要照抄两个文件，
  跑一次 `git config core.hooksPath security/hooks` 即可；`git pull` 后**立即生效、不需要重装**
- **「装了但没执行位」在安装路径上消失**：执行位由 git 跟踪，旧机制那条「拷进去忘了 `chmod +x`、
  git 静默跳过」在拷贝这一步不复存在（**订正**：实测 git 2.56 会打一句 advice 提示；但**本地**
  `chmod -x` 仍会让 git 跳过它——自检会报出来，见 `security/README.md`）
- **新增 `security/scan.py --check-hooks`**：报安装状态的五态（未装 / 旧机制 / 配置指错 /
  本地改动 / 正常），外加一句「共享 hooks 目录里还有已被旁路的同名文件」的警告（不改退出码）
- **`.git/hooks/` 被整体旁路**：以后别往那儿放钩子（不会跑）
- 已知边界：自检是**拉取式**的（不主动跑就不说话）；Windows 上 `core.filemode=false` 会丢
  执行位，本项目 macOS-only

### 其他规划

- 蒸馏导出格式（按对话窗口切片的 JSONL）
- 定时增量同步
