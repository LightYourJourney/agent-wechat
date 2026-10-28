# ARCHITECTURE — 架构设计文档

> 本文档面向开发者：模块划分、数据流、关键设计决策与取舍、扩展性设计。
> 表结构细节见 [DATA_MODEL.md](DATA_MODEL.md)，微信库结构实测见
> [schema-4.1.13.md](schema-4.1.13.md)。

## 1. 总体原则

**一次性特权引导 → 之后完全无特权。**

获取密钥（bootstrap）是唯一需要 sudo 的环节，产出 passphrase 缓存；此后 MCP Server
常驻、全量导出、检索全部是普通用户权限下的**纯文件读取 + 本地计算**。这既满足最小
权限原则，也让 MCP Server 可以安全常驻。

另一条硬约束：**只读微信数据**。代码中不存在任何对微信容器目录的写路径；
默认一切产物落在 `~/.agent-wechat/`（0700）；四个 `--out` 例外见 SECURITY.md §1 与 §6。

## 2. 模块划分

```
src/agent_wechat/
├── paths.py        路径常量与账号目录解析（纯函数，可注入 HOME_DIR 测试）
├── account.py      登录态检测：pgrep -x WeChat + lsof -n -P 判定当前账号
├── keys.py         passphrase 缓存、PBKDF2 派生、page1 HMAC 自验证
├── bootstrap.py    lldb 断点捕获 passphrase（唯一 sudo 环节）
├── decrypt.py      db+wal+shm 三元组快照 → sqlcipher raw-key 解密导出
├── errors.py       统一错误契约：core 只抛项目异常，适配层翻译成各自介质
├── messages.py     messages 行契约：列清单、行构造器、三条哨兵编解码（`md5:` 退化会话 id、
│                   `[appmsg: 标题]` 提示、`[贴纸:名称]` / `[贴纸#短ID]` 显示串）、类型词汇表
├── appmsg.py       appmsg 原始载荷：三步规范化 + 危险载荷守卫 + 解析（纯函数，不碰 IO）
├── roster.py       显示名规则：ext_buffer 的 protobuf 解析 + 群昵称/备注/昵称优先级（纯逻辑，不碰 IO）
├── schema.py       微信库结构声明：能力 × 必需表/列 + 探测（纯数据，不碰 IO）
├── reader.py       消息读取：表发现、内容解码、流式产出（按语义**解码消息内容**的唯一模块——`inspect` 也会读库，但它只 dump 结构与样例行；结构与探测声明在 schema.py）
├── media.py        媒体导出：图片 dat 解密（密钥纯磁盘推导 + 样本验证）、语音 SILK→WAV、视频定位、**贴纸 CDN 取件（本项目唯一为贴纸联网的路径，默认关）**（唯一接触媒体容器与媒体密钥的模块；reader import 它抽 `media_refs`、并借 `extract_sticker_ref` 解析贴纸 XML 的**三个安全字段**，不反向**顶层** import；唯一的反向是 `media` 在贴纸回源一处**函数内惰性** import `reader` 现取原始 XML——为避开模块级环，见 `media.py` 模块头）
├── moments.py      朋友圈解析：sns.db 的 `TimelineObject` XML → 结构化动态（纯读取，不联网）
├── sns_media.py    朋友圈远程媒体：URL 构造 → 下载 → WX-ISAAC64 解密（朋友圈远程媒体：HTTP、默认关；本项目两条外发路径之一——另一条是 `media.py` 的贴纸 CDN 取件。隔离带）
├── moments_html.py 朋友圈渲染：单文件自包含 HTML 时间线（纯函数，不写文件、不联网）
├── chat_text.py    两个会话模板共享的措辞与纯函数（时间格式化 / 相对路径 / 媒体状态与降级文案）
├── chatview.py     会话时间线：`ChatView` 组装 + `write`（唯一决定产物布局的地方）
├── chat_html.py    会话时间线的 HTML 模板（纯函数，单文件自包含，不写文件、不联网）
├── chat_md.py      会话时间线的 Markdown 模板（纯函数；固有损失与**已收口的转义**见 USAGE.md §7）
├── chat_index.py   会话索引页的 HTML 模板（纯函数；枚举产物与查库在用例层）
├── export.py       导出管道：流式批处理 + 水位线续跑（任务登记归 usecases 层）
├── msgextra.py     位置（48）/ 名片（42）载荷的解析（与 appmsg 同一套规范化与守卫）
├── store.py        本地检索库：DDL、幂等 upsert、水位线、FTS 触发器
├── migrations.py   版本化 schema 迁移：版本表、按 scope 顺序执行、未来版本拒绝、失败回滚
│                   （只认 scope 与版本，不认识任何业务表——DDL 仍归各 owner）
├── search.py       检索路由：trigram FTS / LIKE 兜底
├── vector.py       向量索引与语义检索（唯一接触 fastembed 与 sqlite-vec 的隔离带）
├── tasks.py        长任务注册表：后台线程 + 进度落盘 + 中断回收（不认识任何具体任务）
├── usecases.py     用例层：两个入口共享的入口策略（参数归一化、账号/会话解析、结果形状）
├── purge.py        数据销毁：清单 → 计划 → 执行 → 核查（只做 CLI，不进 MCP）
├── exclusions.py   被销毁会话的排除清单（`exclusions.db` 的墓碑；**唯一不可重建的产物**，
│                   由 `store.connect()` ATTACH 进来，写入端的拦截判据与写入同事务）
├── webui.py       本地只读工作台（Phase 07）：stdlib HTTP + loopback/Host/CSRF/CSP；
│                   无文件服务、无访问日志、无遥测；只调用 core/usecases
├── derived.py     本地 OCR/ASR 派生文本与事实记忆（Phase 06）：只调用本机引擎、
│                   只处理已导出媒体；派生文本按 msg_key 关联、删除留墓碑
├── retrieval.py   统一检索与两阶段披露（Phase 05）：keyword/semantic/RRF 融合、
│                   时间窗、片段预算、读取范围与遮盖；全文由 context 二次读取
├── sync.py        同步编排与只读诊断（Phase 04）：环境检查→导出→向量脏队列→健康快照；
│                   全局非阻塞锁；launchd plist 只生成不加载
├── mcp_server.py   MCP 薄适配层（15 工具，只做参数编组与渲染）
└── cli.py          命令行入口（只做参数编组与渲染）
```

分层规则：**核心层不 import MCP SDK**；`mcp_server.py` 与 `cli.py` 是同一批核心函数
的两个入口，保证 Agent 与 CLI 行为一致、测试无需起 MCP。

入口策略只实现一次、放在 `usecases.py`：每个工具一个用例函数，独占 limit 归一化
（下界一律 1，上界按 TOOLS.md 文档化的区间）、contact → chat_id 解析（歧义返回
`NeedConfirm` 候选、未命中抛 `ContactNotFound`）与结果形状；入口只负责参数编组与
渲染（MCP 翻成 `{"error"}/{"need_confirm"}` 信封，CLI 翻成 stdout/stderr + 退出码）。

## 3. 数据流

### 3.1 登录态检测（无侵入）

```
pgrep -x WeChat → 主进程 PID
lsof -n -P -p <PID> → 过滤 xwechat_files/wxid_* 目录 → 唯一命中 = 当前登录账号
```

未唯一命中（多开/启动瞬间）时返回"不确定"而不是猜。

### 3.2 密钥引导（一次性）

```
codesign 重签名微信（去 Hardened Runtime）
  → lldb attach，断点 CCKeyDerivationPBKDF（条件 passwordLen==32）
  → 用户在微信内重新登录触发派生 → 读寄存器得 32B passphrase
  → PBKDF2 派生各库密钥 → page1 HMAC-SHA512 校验（通过才落缓存）
  → ~/.agent-wechat/passphrase.json（0600，按账号分键）
```

为什么走这条路：微信 **4.1+ 不再在内存缓存 raw key**（只保留登录时算一次的
32 字节 passphrase），旧工具的内存模式扫描全部失效；而 macOS 微信的派生调用走
苹果公开系统符号 `CCKeyDerivationPBKDF`——断它不需要逆向微信二进制，是公开资料中
跨版本最稳的路线。我们曾系统排查过磁盘路线（`key_info.db` 明文库内 56 个被包裹的
blob，66,752 次 raw-key + 1,402 次 passphrase 候选全部未通过 HMAC 验证），确认
纯文件解析不可行，spike 脚本留档于 `spike/`。

### 3.3 近期读取（在线路径，秒级）

```
reader.recent(chat_id, limit)
  → md5(chat_id) 定位 Msg_<md5> 表
  → 分片按时间分区：从最新分片起，快照→解密→读表尾 N 条，够数即停
  → 解码（zstd / 类型掩码 / 群前缀）→ 时间正序返回
```

### 3.4 全量导出（离线路径，长任务）

```
export.run_export
  → 逐分片：快照（db+wal+shm）→ sqlcipher raw-key 解密到 0700 临时目录（**一次**）
  → 逐表（Msg_<md5>）两段读：
      ① 前向：local_id > 导出水位线 → 新消息
      ② 回看：local_id > audit_cursor 的一小段旧行（默认 100 行/会话/轮）
  → fetchmany(batch) 分批解码 → 批量幂等 upsert + 水位线推进（同一事务）
  → 数据提交之后才推进 audit_cursor（回看游标；绕圈覆盖整段历史）
  → 临时明文删除；进度写 tasks.json（进程重启后从该文件载入，遗留的 running 记为 failed）；
  中断后重跑自动续传
```

### 3.5 检索（本地索引）

```
search.search(query)
  → ≥3 字符：FTS5 trigram MATCH（短语子串，snippet 高亮）
  → 1-2 字符：LIKE '%q%' 兜底（建议限定 contact，否则全表扫描）
```

### 3.6 语义检索（本地嵌入，手动触发）

```
vector.build_index(accounts, rebuild)       # 长任务，读的是已导出的本地 messages 表
  → 按账号水位线（vector_watermark.last_rowid）续扫 messages
  → 逐条构造嵌入文本：`[日期 发送者] 上文: <上一条入索引消息的前 30 字>… | 正文`（确定性前缀）
  → fastembed（ONNX，CPU）批量嵌入 → vec0 虚表 vec_messages（cosine，512 维）
  → 每 COMMIT_ROWS 行提交（数据+水位线同事务）；rebuild 时先 DROP 再重建
  → 前向扫描之后消费 `pending_reembed` 脏队列（Phase 03）：每条只重嵌改动的那条，
    外加因「上文前缀」依赖而过时的**后继**；向量写与队列删同一事务，失败记账重试

vector.semantic_search(query, chat_id, limit, account)
  → 模型一致性检查（存在异模型 meta 行 → 报错，要求 rebuild）
  → query 侧加 BGE s2p 指令 → KNN k≈limit*3（上限 4096；可加 acct_chat partition key 约束）
  → 按 msg_rowid 回查 messages 取正文 → 截断到 limit，返回 distance（cosine）
```

嵌入文本里的「上文」前缀只解决**同一轮运行内**的短消息语义（「好的」「在吗」），
跨运行的连续性由 `rebuild` 保证（见 §7）。

### 3.7 媒体导出（图片 / 语音 / 视频）

```
media.export_media(account, chat_ids, kinds, months, con)
  → 图片：kvcomm 文件名取 code + md5(code+裸wxid) + 样本 dat 尾部统计 xorKey → 样本 dat 验证
      → 解密 dat（V2 = AES-128-ECB 段 + XOR 尾）→ 明文图片落 media/<会话md5>/image/<月份>/
  → 语音：media_*.db 快照解密（HOME/tmp，用后即焚）→ VoiceInfo.voice_data（明文 SILK）
      → pilk 解 SILK → 自封 44B 头的 24kHz 单声道 WAV
  → 视频：查 media_refs 取该会话的 file_key → 定位 msg/video/{月份}/ 下的明文 mp4
      （本体不在则回退缩略图 {file_key}_thumb.jpg）→ 复制
  → 产物一律落 ~/.agent-wechat/media/（0700、文件 0600）；微信目录始终只读
```

媒体密钥的推导不需要任何特权：`code` 来自 kvcomm 目录的文件名、`xorKey` 由样本 dat 尾部
统计得出，推导结果**必须**用样本 dat 解密验证——验证不过只是图片能力级降级，语音/视频不受影响。
「消息 → 文件名」映射（`media_refs`）由导出管道与消息同事务顺带写入，存量历史用
`media rebuild-refs` 全量回填（见 DATA_MODEL.md §3）。该表既供「按消息定位媒体」
（`media locate` / `get_message_media`）读，也供**视频导出的会话归属**读——视频树
`msg/video/{月份}/` 没有会话维度，多会话归属只能经此表成立；图片与语音的导出仍按磁盘
目录（attach 树 / `media_*.db`）枚举定位。

### 3.8 朋友圈读取与导出（离线默认，联网需显式开启）

```
moments.iter_posts(account, report=…)       # 复用 passphrase 解密 sns/sns.db（只读）
  → SnsTimeLine.content 的 TimelineObject XML → SnsPost（正文/媒体引用/评论/点赞）
  → 按 chat_filter（作者）与 months（本地月份）过滤，产出**最新在前**（createTime 降序）
       ↓ with_media=False（默认，零网络）
  → 未下载的媒体以占位说明标注：图片 / 视频标「远程，未下载」，`kind=other` 标「外部链接媒体」
       ↓ with_media=True（本次新增的联网例外）
  → sns_media.build_url → 直连（不走代理）微信 CDN 白名单（后缀 `*.qpic.cn` / `*.video.qq.com`
     + 精确主机 `wxapp.tc.qq.com`）→ WX-ISAAC64 解密
  → moments_html.render → 单文件 HTML 落 $AGENT_WECHAT_HOME/moments/index.html（`--out` 可改）
```

产物页首固定标注「这是微信本地缓存窗口，不是完整历史」——`createTime` 覆盖的只是
微信滚动缓存的窗口。单条媒体失败只计入**计数行**（解密后不是已知图片格式的那一类另进
降级区），不中断整次导出；单条动态解析失败同理（计入 `failed`），因为「XML 一条都读不懂」
与「这个账号没有朋友圈」必须分得开。

### 3.9 索引库 schema 升级（Phase 01）

`store.connect()` / `vector.connect()` 在**每次连接**上分两步：先跑整套
`CREATE … IF NOT EXISTS`（缺对象就补建的自愈），再按 scope 跑版本化迁移——后者只处理
`IF NOT EXISTS` **表达不了**的变更（改触发器体、加列、重建表），每条只执行一次。

版本表 `schema_version(scope, version, applied_at)`，scope = `main` / `vector` /
`exclusions`。整次升级 = 一笔 `BEGIN IMMEDIATE … COMMIT`（DDL 与版本行同事务）：失败整次
回滚、版本停在升级前、下次连接重做；已是最新时只读版本行、不拿写锁；库的版本**高于**
程序已知最新版时在 ensure 之前拒绝（一个对象都不写）。

**向量单独一个 scope**：`vec_messages` 依赖 sqlite-vec 扩展，没装扩展的机器上 `main` /
`exclusions` 必须照常、基础检索必须可用。三个 scope 各自版本化，**不假设跨库原子**
（`index.db` 与 `exclusions.db` 是两个文件、主库是 WAL）。

细节见 [内部阶段审计记录](内部阶段审计记录) 与
[DATA_MODEL.md](DATA_MODEL.md) §10。

## 4. 关键设计决策与取舍

| 决策 | 备选 | 理由 |
|---|---|---|
| LLDB 断点捕获（sudo 一次性） | 内存模式扫描（4.0 路线）/ 纯文件解析 | 4.1+ 内存无 raw key；文件路线已用 HMAC 验证排除（见 §3.2）；断系统符号跨微信版本最稳 |
| 每库独立 PBKDF2 派生 + raw-key 形式喂 sqlcipher | 把 passphrase 当字符串传给 CLI | passphrase 是任意 32 字节，作字符串有转义风险；raw-key（`PRAGMA key = "x'...'"`）与我们的派生公式闭环 |
| 快照整个 db+wal+shm 三元组 | 直接只读打开源库 | 微信运行在 WAL 模式：不拷 -wal 会漏最新消息；只读打开 WAL 需要写 -shm，违反"零写入" |
| SQLite + FTS5（单文件） | PostgreSQL / 独立向量库 | 个人数据量级（百万条内）下零运维、单文件易备份/销毁；trigram 无需分词词典，对中文子串匹配和人名/专名表现稳 |
| 每会话一张表的全量枚举（不解析表名来源） | 只导出 session 列表中的会话 | Msg_ 表含历史会话；未解析 md5 的会话以 `md5:xxx` 为 id 仍完整导出，不丢数据 |
| 幂等 upsert（ON CONFLICT DO UPDATE） | INSERT OR IGNORE | 撤回/编辑后重扫能修正内容而非跳过 |
| 水位线与数据同事务 | 先写数据后改水位线 | 任意时刻被杀都一致：最多重复扫一批（幂等吸收），绝不丢数据 |
| 导出与向量化解耦 | 导出时同时嵌入 | 嵌入是 CPU 密集长任务，与 I/O 管道耦合会互相拖累。已落地：`vector.py` 独立嵌入管道，手动/按需触发 |

**消息信封契约**：`get_recent_messages` / `search_history` / `semantic_search` 三处返回的消息
统一带 `msg_key` 字段（`account|chat_md5|shard|local_id`，由消息的身份/位置字段派生，
跨导出稳定）。
`msg_key` 与 `search_history` hits 里既有的 `id`（`messages.id`，DB rowid）不是同一件事
——rowid 随重建变化，`msg_key` 是指认消息的稳定句柄，供 `get_message_media` 指认
「这条消息的媒体是什么」。三处消费点同步添加，避免字段名冲突（`id` 已被占用）。

## 5. 安全设计要点

- **自验证门**：任何密钥候选必须通过 page1 HMAC-SHA512 校验才被接受（唯一真值判据）；
  该公式经合成加密库对照实验做了真/假双向验证
- **缓存**：passphrase 文件 0600；目录 0700；不写日志、不进错误信息
- **临时明文**：单分片、0700 目录、用后即焚；`~/.agent-wechat/` 由代码在首次使用时
  自动加入 Time Machine 排除（`tmutil addexclusion`，失败需手动执行）；索引明文依赖
  FileVault
- **MCP 传输**：stdio，无网络端口

详见 [SECURITY.md](SECURITY.md)。

## 6. 扩展性设计

| 方向 | 现状 | 路径 |
|---|---|---|
| 向量语义检索 | 已实现：`vector.py`（fastembed 本地嵌入 + sqlite-vec KNN） | 逐条消息 + 确定性上下文前缀；`vec0` 表按 `account\|chat_md5` partition key 分片、`account` metadata 做账号隔离；水位线按 `messages.id` 续跑；导出与嵌入解耦（手动触发） |
| 1-2 字高频查询 | LIKE 兜底（限 contact 时够用） | 增加 bigram 影子 FTS 表（存储约 +2×，做成配置项） |
| 更大规模（千万级） | 单文件 SQLite（百万级实测流畅） | 分库（按账号/年份）或迁移 PostgreSQL + pgvector |
| 媒体导出 | 已实现：`media.py`（图片 dat 解密 / 语音 SILK→WAV / 视频定位）+ `media_refs` 映射 | 按会话导出与存量回填已就绪；按相册批量导出未做 |
| 媒体与消息关联 | 已实现：CLI `media locate` / MCP `get_message_media` 按 `msg_key` 回答四态（`none` / `exported` / `exportable` / `gone`），信封另给 `counts` 与 `by_kind` 拆分（`image` / `video` / `voice` / `sticker` 四项） | 不写微信目录、不写 `out_dir`、不联网；判定做 `pathlib` 探测与索引只读查询，语音的本体判定要解密 `media_*.db`（明文只在 `tmp/` 短暂存在，见 SECURITY.md）。`exportable` 在本机实质上是语音专属——图片/视频本体只为**近期（尚未导出）**的消息保留；**贴纸是第四类，只可能 `gone` 或 `exported`（永不 `exportable`）**，那个 `gone` 的语义是「取不出来」：微信把它存成加密格式、方案未破（且本机那份是缩略档），与「本机有没有」无关。**贴纸的字节来源是 CDN 取件**（`media export --kinds …sticker --with-cdn`），默认关，见 TOOLS.md §9 / §11 与 SECURITY.md §1「零网络例外」第 3 条 |
| 会话时间线导出（appmsg 富文本） | 已实现：`appmsg.py`（三步规范化 + 解析）+ `msg_parts` 原始载荷表 + `chatview.py`（`ChatView` 组装与落盘）+ `chat_html.py` / `chat_md.py`（两个纯模板，措辞共享在 `chat_text.py`）+ `chat_index.py`（索引页）；CLI `chat export` / `chat index` / `appmsg rebuild`，MCP `export_chat` / `get_message_parts` | appmsg（引用 / 链接 / 文件 / 转账）渲染成结构化块，媒体以相对路径引用**同级**的既有媒体树（`chat export` 的 `--out` 必须与 `media export` 同根）；`appmsg rebuild` / `extras rebuild` 是显式的一次性动作（幂等；后者补位置/名片的结构化载荷）；Markdown 有三处固有损失（第四处「语法不转义」已在 EA-037 收口），**不假装与 HTML 等价**（见 USAGE.md §7） |
| 朋友圈读取与导出 | 已实现：`moments.py`（离线解析 `sns.db`）+ `moments_html.py`（单文件 HTML）+ `sns_media.py`（远程媒体，默认关） | 远程媒体需联网——`--with-media` / `with_media=True` 显式开启，只连微信 CDN——后缀 `*.qpic.cn` / `*.video.qq.com`，加精确主机 `wxapp.tc.qq.com`（**不含子域**）；默认全程零网络；本地缓存图不参与（文件名与动态对不上）；发布 / 删除等写操作不做 |
| 群成员显示名 | 已实现：索引库存 `display_names` 名册，查询时 join；实时路径 `recent` 用内存名册 | 群昵称在 `contact.db` 的 `chat_room.ext_buffer`（protobuf，字段 f2，可选），回落 `contact.remark` / `nick_name` |
| 增量自动同步 | 手动重跑 export（秒级） | export 管道与水位线已就绪，加定时触发即可 |

## 7. 已知边界

- 微信升级可能改变库结构/加密参数 → 运行时按 `schema.py` 的声明清单探测（读 `PRAGMA
  table_info`，实例级缓存），受影响的能力降级并在结果里说明；`inspect` 仍是人工对照工具
- 读取期间微信重度使用时快照可能偶发不一致 → 快照失败自动重试逻辑为规划的加固点
  （当前实现直接报错，重跑即可）
- `key_info.db` 明文库的存在说明微信本地留存了被包裹的密钥材料；若未来微信改为
  更强的包裹算法，LLDB 路线不受影响（它读的是内存中已解出的 passphrase）
- **增量嵌入的上下文前缀不连续**：`上文:` 前缀在单次运行内按会话累积，增量运行从水位线
  开始，各会话本轮第一条被嵌入的消息没有前缀（即便确有前一条）；需要连续上下文时用
  `vector-build --rebuild` 全量重建
- **历史改写会修正 SQLite/FTS，并在下一次 `vector-build` 重嵌**：Phase 02 把改写投递到
  `pending_reembed`，Phase 03 在每次构建里消费它（每轮最多 4 批）。**不是实时**：没跑
  `vector-build` 之前，旧向量仍在（检索可能返回旧语义），`vector-status` 会显示积压。
- **回看是有界、非实时的**：每轮只回看每个会话的一小段（默认 100 行），游标绕圈；
  完整覆盖一轮约 `ceil(行数/100)` 次导出。要立即核对历史用 `export --full`。
- **源库删除不传播**：索引只增改，不把「扫描不到」当成删除（分片消失/缓存淘汰同理）
- **语义检索没有相关性阈值**：KNN 恒返回最近的 `k` 条（无论距离多远），`distance`（cosine，
  越小越近）原样返回，由调用方判断可用性——不要直接把结果当作「相关结果」
- **全局检索无 ANN**：不限会话的 KNN 是暴力扫描（68 万行实测：热 ~0.6s / 冷 ~2.6s）；单会话靠 `acct_chat`
  partition key 收窄扫描面
- **会话过滤按 `md5(chat_id)`**：只有 canonical 用户名（`wxid_*` / `*@chatroom` / `gh_*` /
  `filehelper`）能命中；能解析、但没有任何已入索引消息的会话（未导出 / 消息全被跳过）返回
  0 命中且无提示，与「确实没有命中」不可区分。展示名必须经 `contact` 参数解析
  （未命中 / 重名会显式报错，不会静默返回空）
- 索引由其它模型构建时必须 `rebuild`（不混用不同模型的向量）；换模型不会自动迁移
