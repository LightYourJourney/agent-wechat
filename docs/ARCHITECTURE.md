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
一切产物落在 `~/.agent-wechat/`（0700）。

## 2. 模块划分

```
src/agent_wechat/
├── paths.py        路径常量与账号目录解析（纯函数，可注入 HOME_DIR 测试）
├── account.py      登录态检测：pgrep -x WeChat + lsof -n -P 判定当前账号
├── keys.py         passphrase 缓存、PBKDF2 派生、page1 HMAC 自验证
├── bootstrap.py    lldb 断点捕获 passphrase（唯一 sudo 环节）
├── decrypt.py      db+wal+shm 三元组快照 → sqlcipher raw-key 解密导出
├── reader.py       消息读取：表发现、内容解码、流式产出（唯一认识微信 schema 的模块）
├── export.py       导出管道：流式批处理 + 水位线 + 后台长任务注册表
├── store.py        本地检索库：DDL、幂等 upsert、水位线、FTS 触发器
├── search.py       检索路由：trigram FTS / LIKE 兜底
├── vector.py       向量索引与语义检索（唯一接触 fastembed 与 sqlite-vec 的隔离带）
├── usecases.py     用例层：两个入口共享的入口策略（参数归一化、账号/会话解析、结果形状）
├── mcp_server.py   MCP 薄适配层（8 工具，只做参数编组与渲染）
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
  → 逐分片：快照（db+wal+shm）→ sqlcipher raw-key 解密到 0700 临时目录
  → 逐表（Msg_<md5>）按 local_id > 水位线游标扫描
  → fetchmany(batch) 分批解码 → 批量幂等 upsert + 水位线推进（同一事务）
  → 临时明文删除；进度写 tasks.json；中断后重跑自动续传
```

### 3.5 检索（本地索引）

```
search.search(query)
  → ≥3 字符：FTS5 trigram MATCH（短语子串，snippet 高亮）
  → 1-2 字符：LIKE '%q%' 兜底（建议限定 contact，否则全表扫描）
```

### 3.6 语义检索（本地嵌入，手动触发）

```
vector.build_index(account, rebuild)        # 长任务，读的是已导出的本地 messages 表
  → 按账号水位线（vector_watermark.last_rowid）续扫 messages
  → 逐条构造嵌入文本：`[日期 发送者] 上文: <上一条入索引消息的前 30 字>… | 正文`（确定性前缀）
  → fastembed（ONNX，CPU）批量嵌入 → vec0 虚表 vec_messages（cosine，512 维）
  → 每 COMMIT_ROWS 行提交（数据+水位线同事务）；rebuild 时先 DROP 再重建

vector.semantic_search(query, chat_id, limit, account)
  → 模型一致性检查（存在异模型 meta 行 → 报错，要求 rebuild）
  → query 侧加 BGE s2p 指令 → KNN k≈limit*3（上限 4096；可加 acct_chat partition key 约束）
  → 按 msg_rowid 回查 messages 取正文 → 截断到 limit，返回 distance（cosine）
```

嵌入文本里的「上文」前缀只解决**同一轮运行内**的短消息语义（「好的」「在吗」），
跨运行的连续性由 `rebuild` 保证（见 §7）。

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
| 媒体导出 | 仅元数据提示 | 数据层已保留 `msg_type` 与源路径解析点，按需扩展 |
| 群成员解析 | 群消息 sender 为 wxid/前缀名 | `contact.db` 的 `chatroom_member` 表可映射群昵称 |
| 增量自动同步 | 手动重跑 export（秒级） | export 管道与水位线已就绪，加定时触发即可 |

## 7. 已知边界

- 微信升级可能改变库结构/加密参数 → `inspect` 对比 [schema-4.1.13.md](schema-4.1.13.md)
- 读取期间微信重度使用时快照可能偶发不一致 → 快照失败自动重试逻辑为规划的加固点
  （当前实现直接报错，重跑即可）
- `key_info.db` 明文库的存在说明微信本地留存了被包裹的密钥材料；若未来微信改为
  更强的包裹算法，LLDB 路线不受影响（它读的是内存中已解出的 passphrase）
- **增量嵌入的上下文前缀不连续**：`上文:` 前缀在单次运行内按会话累积，增量运行从水位线
  开始，各会话本轮第一条被嵌入的消息没有前缀（即便确有前一条）；需要连续上下文时用
  `vector-build --rebuild` 全量重建
- **语义检索没有相关性阈值**：KNN 恒返回最近的 `k` 条（无论距离多远），`distance`（cosine，
  越小越近）原样返回，由调用方判断可用性——不要直接把结果当作「相关结果」
- **全局检索无 ANN**：不限会话的 KNN 是暴力扫描（68 万行实测：热 ~0.6s / 冷 ~2.6s）；单会话靠 `acct_chat`
  partition key 收窄扫描面
- **会话过滤按 `md5(chat_id)`**：只有 canonical 用户名（`wxid_*` / `*@chatroom` / `gh_*` /
  `filehelper`）能命中；能解析、但没有任何已入索引消息的会话（未导出 / 消息全被跳过）返回
  0 命中且无提示，与「确实没有命中」不可区分。展示名必须经 `contact` 参数解析
  （未命中 / 重名会显式报错，不会静默返回空）
- 索引由其它模型构建时必须 `rebuild`（不混用不同模型的向量）；换模型不会自动迁移
