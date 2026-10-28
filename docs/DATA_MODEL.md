# DATA_MODEL — 数据与存储说明

> 本文档说明本地检索库的表结构、字段含义、索引设计、去重与增量策略，以及面向
> LLM 蒸馏的数据获取方式。微信侧原始库结构见 [schema-4.1.13.md](schema-4.1.13.md)。

## 1. 存储位置

```
~/.agent-wechat/            # 目录 0700（AGENT_WECHAT_HOME 可覆盖）
├── passphrase.json         # 密钥缓存（0600），按账号分键
├── index.db                # 本地检索库（SQLite, WAL 模式，明文 + FileVault）
├── index.db-wal / -shm     # WAL 伴生文件
├── exclusions.db           # 排除清单：被销毁会话的墓碑；**唯一不可重建的产物**（见 §3、§9）
├── tasks.json              # 导出长任务的状态与进度
├── media/                  # 媒体导出产物（图片/语音 WAV/视频，0700，明文）
├── moments/                # 朋友圈导出产物（单文件 HTML + 媒体，0700，明文）
└── tmp/                    # 解密临时目录（单分片明文，用后即焚）
```

## 2. messages 主表

```sql
CREATE TABLE messages (
    id          INTEGER PRIMARY KEY,      -- 本地自增，FTS 虚表的 rowid 引用
    msg_key     TEXT NOT NULL UNIQUE,     -- 去重主键，见 §4
    account     TEXT NOT NULL,            -- 所属账号（账号目录名）
    chat_md5    TEXT NOT NULL,            -- 微信 Msg_<md5> 表的 md5 部分
    chat_id     TEXT NOT NULL,            -- 会话名：wxid / xxx@chatroom / md5:xxx
    shard       TEXT NOT NULL,            -- 来源分片（message_N.db）
    local_id    INTEGER NOT NULL,         -- 分片内消息 id
    server_id   INTEGER,                  -- 预留字段：当前导出版本恒为 NULL
    ts          INTEGER NOT NULL,         -- unix 秒
    msg_type    INTEGER NOT NULL,         -- 微信消息类型（掩码规则见 §5）
    sender_id   TEXT,                     -- 发送者 username（Name2Id 解析）
    sender_name TEXT,                     -- 导出时从内容前缀取的原值（群聊为 <sender> 原文）；
                                          -- 查询时优先用 display_names 名册覆盖，本列是
                                          -- join 不上时的兜底
    is_self     INTEGER NOT NULL,         -- 1 = 本人发送
    content     TEXT,                     -- 文本正文；非文本为 [image] 等提示
                                          -- **贴纸例外**：msg_type=47 时为可读标识
                                          -- [贴纸:名称] / [贴纸#md5前8位]，源 XML 无
                                          -- 合法 md5 时回落 [sticker]（见 §5）
    media_hint  TEXT,                     -- 非文本类型的提示串；**贴纸恒为 [sticker]**
                                          -- （与上面那列分叉是刻意的，见 §5）
    exported_at INTEGER NOT NULL          -- 入库时间
);
```

**msg_type 常见值**（微信原始类型，已按低 32 位掩码）：
`1` 文本 · `3` 图片 · `34` 语音 · `43` 视频 ·
`47` 贴纸（表情）——`content` 是**可读标识** `[贴纸:名称]` 或 `[贴纸#md5前8位]`
（名称来自消息 XML 的 `<emoji>` 属性，取不到时用本机 `emoticon.db` 按 md5 反查，仍取不到
才回落短 ID；源 XML 里没有合法 md5 时 `content` 保持 `[sticker]`），`media_hint` **恒为
`[sticker]`**——两列分叉是刻意的 ·
`48` 位置 ·
`49` appmsg（引用/链接/文件/转账…；正文 XML 规范化后存 `msg_parts.raw_xml`，`content` 只存 `[appmsg: 标题]` 投影）·
`10000` 系统消息 · `10002` 撤回。

## 3. 索引设计

```sql
CREATE INDEX idx_msg_chat_ts ON messages(account, chat_id, ts);  -- 会话内时间序（最常用）
CREATE INDEX idx_msg_ts      ON messages(account, ts);           -- 全局时间线
CREATE INDEX idx_msg_sender  ON messages(account, sender_id);    -- 预留、零消费者（照 server_id 先例）
```

**全文检索**（外部内容模式，不重复存正文）：

```sql
CREATE VIRTUAL TABLE messages_fts USING fts5(
    content, chat_id,
    content='messages', content_rowid='id',
    tokenize='trigram'        -- 中文无需词典的精确子串匹配
);
```

配三个触发器（AI/AD/AU）保持虚表与主表同步。**选 trigram 的理由**：无词典、
天然支持中文子串与专名/人名匹配；代价是无法匹配 <3 字符的查询——由 `search.py`
的 LIKE 路由兜底（1-2 字查询）。见 ARCHITECTURE.md §4。

向量表（`vec_messages` / `vector_meta` / `vector_watermark`）见 ARCHITECTURE.md §3.6。

`display_names` 与 `roster_meta` 两张表由 `store.SCHEMA` 建
（`CREATE TABLE IF NOT EXISTS`，每次连接都会跑），无需迁移。

**媒体映射表 `media_refs`**（同一批 DDL 建，无迁移）：

```sql
CREATE TABLE IF NOT EXISTS media_refs (
    msg_key    TEXT PRIMARY KEY,   -- = messages.msg_key（account/chat_md5/ts 经 join 取回）
    media_kind TEXT NOT NULL,      -- 'image' | 'video' | 'sticker'
    file_key   TEXT,               -- 文件名哈希（dat / mp4 的文件名，不含扩展名）；
                                   -- **sticker 行放的是贴纸 md5**（它才是该 kind 的定位键）
    xml_md5    TEXT                -- XML 里的 md5（兜底留证，与文件名无关）
) WITHOUT ROWID;
```

- **`file_key` 的权威来源是 `Msg_` 表的 `packed_info_data` BLOB**：里面的 32-hex 就是
  attach / video 目录下的文件名。XML 的 `md5` 属性**实测不等于文件名**（0/19,832 命中），
  仅当 packed 缺失且它恰好是 32-hex 时充当兜底，同时留证备查。
- **语音不入此表**：语音文件由 `media_*.db` 的 `VoiceInfo(chat_name_id, create_time, local_id)`
  三元组直接命中，不需要按消息逐条建映射。
- **贴纸也不按上面那条 `file_key` 语义填**：`media_kind='sticker'` 的行把**贴纸 md5** 放在
  `file_key` 位（`xml_md5` 位留 `None`）——它不指向任何文件名，只是这个 kind 的身份键。
  它与图片/视频的 `file_key` 同列不同义，是刻意的。
  **这一类行现在有读路径了**（2026-09-23）：`media export --kinds sticker` 按它判定归属。
  **但 `_state_of` 的贴纸支仍然不看 `ref`**——`kind` 由 `msg_type` 判定，状态只可能是
  `gone` 或 `exported`（`exported` 由 `exported_path` 探测得出，与 `ref` 无关），
  **永不 `exportable`**。别把这张表里的 sticker 行读成「贴纸状态是它算出来的」。
- 填充两条路径：导出管道与消息写入**同事务**顺带 upsert；本次功能之前的存量历史用
  `agent-wechat media rebuild-refs` 全量重扫回填（幂等，不重写 `messages`）。
- **读路径有三条**（`store.media_refs_by_keys` / `media_refs_by_chat` / `count_media_refs`）：
  「按消息定位媒体」（`media locate` / `get_message_media` 的四态判定）按 msg_key 批量查；
  **视频导出的会话归属**按会话查（join `messages` 取 ts 与会话名）——视频树
  `msg/video/{月份}/` 没有会话维度，只有经此表才知道某条视频属于哪个会话（多会话归属由此成立）。
- `count_media_refs` **不 join `messages`**，按 `msg_key` 前缀计数（`account|` 或
  `account|chat_md5|`；`msg_key` 的形态保证前缀不会串到别的账号——wxid 里不可能有 `|`）。
  为什么必须不 join：**`media rebuild-refs` 只写 refs、不写 messages**，所以「一条 refs 都没有」
  与「refs 在、但那些会话的消息没进索引」是两种病（前者该重建索引，后者该**导出历史**），
  join 出来的 0 会把它们混成一种，并把用户支去跑一个修不好后者的命令。
- 图片与语音的导出**不查此表**：图片按 attach 树目录枚举、语音按 `media_*.db` 的
  `VoiceInfo` 三元组直接命中。

**位置 / 名片原始载荷表 `msg_extras`**（EA-041；与 `msg_parts` 同一批 DDL 建，无迁移）：

```sql
CREATE TABLE IF NOT EXISTS msg_extras (
    msg_key  TEXT PRIMARY KEY,      -- = messages.msg_key
    msg_type INTEGER NOT NULL,      -- 48 位置 / 42 名片（**原样记**，不猜语义）
    payload  BLOB NOT NULL          -- 规范化后的 XML，zlib 压缩（与 msg_parts.raw_xml 同款）
) WITHOUT ROWID;
```

- **存的是原始 XML，不是解析结果**（与 `msg_parts` 同一条不变量）：白名单与渲染都可以演进，
  重跑一次回填即可；权威只有微信库里那一份。
- **字段走白名单**（`msgextra._KEEP`）：只留阅读用得上的那几个（位置的 `x`/`y`/`label`/
  `poiname`/`poi*`…、名片的 `username`/`nickname`/`alias`…）。真机同族的 `antispamticket`
  （加好友凭据）、`brandHomeUrl`（最长 2,487 字符）等**不影响存盘**（XML 原样存），
  但**不进产物、不进信封**——认不出来的属性一律不往外走。
- **谁写它**：只有 CLI `agent-wechat extras rebuild`（`usecases.msgextra_rebuild`）。
  全量重扫微信库分片、**只写 `msg_extras`**；幂等。**`export` 不写这张表**——
  所以「回填之后新到微信」的位置/名片要重跑一次才有内容。
- **老库兼容**：它是随 SCHEMA 建的 `CREATE TABLE IF NOT EXISTS`，既有 `index.db` 在**下一次
  `connect()`** 时就有了它；`purge --chat` 那条不可逆路径在老库上实测跑得通（计数、删除、
  核查三处都按表名读，不对表数量做断言）。

**appmsg 原始载荷表 `msg_parts`**（同一批 DDL 建，无迁移）：

```sql
CREATE TABLE IF NOT EXISTS msg_parts (
    msg_key     TEXT PRIMARY KEY,      -- = messages.msg_key
    appmsg_type INTEGER NOT NULL,      -- <appmsg><type>（正文类别的数字投影）
    ref_svrid   INTEGER,               -- 引用类（57）的 <refermsg><svrid>
    ref_msg_key TEXT,                  -- 解析出的目标消息；解不到为 NULL（不是空串）
    raw_xml     BLOB NOT NULL          -- 规范化后的 appmsg XML，zlib 压缩
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_parts_ref ON msg_parts(ref_msg_key)
    WHERE ref_msg_key IS NOT NULL;
```

- **不变量：`raw_xml` 是 appmsg 正文的权威，其余三列都是它的投影。** 它们存在的唯一理由是
  「不必解压就能筛选与重解析」——`appmsg_type` 供按类型筛选，`ref_svrid` / `ref_msg_key`
  供回填时解析与呈现。与「`messages.content` 是消息的检索投影」同构：投影可以再算，权威
  只有一份。渲染与 `get_message_parts` 一律**从 `raw_xml` 解压后解析**，不拿投影当内容。
- `idx_parts_ref` 是建在 `ref_msg_key` 上的**部分索引**（`WHERE ref_msg_key IS NOT NULL`）。
  **当前所有读路径都按 `msg_key`（主键）取，还没有按目标反查的消费者**——索引在 DDL 里，
  但别把它读成「已经有按引用反查的功能」。
- **谁写它**：只有 CLI `agent-wechat appmsg rebuild`（`usecases.appmsg_rebuild_parts`）。
  它全量重扫微信原库分片、**只写 `msg_parts`**——不碰 `messages` / `messages_fts` /
  向量表 / 水位线（本机回填前后逐表指纹相同）。幂等：重跑覆盖同一批 `msg_key`。
  MCP 侧没有对应工具——回填是显式的一次性运维动作。
- **覆盖范围**：`appmsg rebuild` 扫的是**微信库**（全部分片的 `Msg_%` 表，载荷为 NULL 的除外），
  不是索引库；而 **`export` 不写这张表**。所以 `msg_parts` 覆盖的是「回填那一刻微信库里已有的
  appmsg」——回填之后新到微信的消息要重跑回填才有行，**这与它何时被导出无关**。
- **陈旧规则（必须写进文档，不能只活在代码里）**：`ref_msg_key` 是**回填那一刻的快照**。
  被引用的消息若**后来才**导出进索引，`ref_msg_key` 不会自己变——**重跑 `appmsg rebuild`
  即刷新**（幂等；本机实测 37.5–38.7 s）。**渲染从不依赖回指成功**：被引用的原文一直带在
  `raw_xml` 的 `<refermsg><content>` 里，回指只决定「能不能跳到原消息」。
- **计数不 join `messages`**：`store.count_msg_parts` 按 `msg_key` 前缀计数，理由与
  `count_media_refs` 完全相同——`appmsg rebuild` 只写 `msg_parts`、不写 `messages`，
  join 出来的 0 会把「没回填」与「这些消息本来就没有可读内容」混成一种。
- **规模（本机实测）**：66,663 行，`index.db` **+232.8 MB**（`raw_xml` 压缩存储；appmsg
  的 XML 一个字节都不进 `messages`）。
- 与 `media_refs` 的分工：那张表回答「媒体本体在不在、在哪」，这张表回答「appmsg 的内容是
  什么」。两者都以 `msg_key` 为主键，存量历史都只由显式的重建动作回填。

**排除清单表 `excluded`**——**它在另一个库文件里**（`exclusions.db`，不是 `index.db`），
DDL 归 `exclusions.py` 的 `exclusions.SCHEMA`，由 `store.connect()` 把它 `ATTACH` 成
`exclusions` 之后与索引同事务读写（`purge` 的删除路径就靠这个 ATTACH 把墓碑与删除放进
两个库）：

```sql
CREATE TABLE IF NOT EXISTS exclusions.excluded (
    msg_key     TEXT PRIMARY KEY,   -- account|chat_md5|shard|local_id，与 messages.msg_key 逐字相同
    account     TEXT NOT NULL,      -- 从 msg_key 派生，存下来只为能建索引
    chat_md5    TEXT NOT NULL,      -- 同上
    excluded_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS exclusions.idx_excluded_chat ON excluded(account, chat_md5);
```

- 一行 = 一条「被用户明确要求销毁过」的消息。**它是本工具唯一删了就不可重建的产物**：
  `index.db` 能重扫微信库重建、`media/` 能联网重抓、密钥能重新 bootstrap，而这张表没了，
  被删的会话会在下一次全量导出时**整批复活**（见 §9）。
- `account` / `chat_md5` 两列是 `msg_key` 前两段的投影，存下来只为能建上面那条索引：
  四条复活路径里的名册重建那条按 **`(account, chat_md5)` 整会话**判（逐条判要解析全表
  `msg_key`）。投影失真会让那条判据错拦或漏拦，所以**删除按前缀谓词**
  （`substr(msg_key, 1, ?) = ?`，与 `store.delete_chat` / `chat_row_counts` 同源），
  不把**删除**的正确性押在这两列忠实上——在删除这条路上它们只服务于查询性能。
  （名册那条判据仍读它们，所以「投影失真」在这里有两处后果：删除侧不再受影响，
  名册侧会**错拦或漏拦**——那是另一个消费者的账，别混起来读。）
- 写入端只有 `purge --chat`，且用 `INSERT … SELECT … FROM messages WHERE account=? AND
  chat_id=?` **从同一谓词派生**（不是先在内存里读一个 `msg_key` 集合再照着写）；
  读出端是那四条复活路径上的拦截判据（模块职责见 ARCHITECTURE.md 的模块清单）
  与 `purge --excluded`。

## 4. 去重主键与增量策略

### msg_key

```
msg_key = "<account>|<chat_md5>|<shard>|<local_id>"
例：wxid_testacct01_ab12|e5be5159…|message_0.db|103
```

四个字段缺一不可——实测发现（[schema-4.1.13.md](schema-4.1.13.md)）：

1. 微信 4.1 的消息**按会话分表**（`Msg_<md5(chat)>`），不同会话的 `local_id` 各自从 1 计数
2. 同一会话跨分片**按时间分区**，`local_id` 在每个分片里重复出现（指向不同消息）

### 水位线（watermark）

```sql
CREATE TABLE watermark (
    account TEXT, shard TEXT, chat_md5 TEXT,
    max_local_id INTEGER,          -- 该 (账号,分片,会话) 已导到的最大 local_id
    updated_at INTEGER,
    PRIMARY KEY (account, shard, chat_md5)
);
```

- **增量**：导出游标为 `WHERE local_id > max_local_id`（按 id 而非时间——同秒消息不会漏）
- **断点续传**：水位线与消息写入在**同一事务**；任意时刻被杀，重跑自动从断点继续，
  最多重复处理一批（被幂等 upsert 吸收），不丢数据
- **幂等修正**：`ON CONFLICT(msg_key) DO UPDATE ... WHERE content IS NOT excluded.content`
  ——重扫时仅内容变化的行被更新（吸收撤回/编辑），无写放大

## 5. 数据解码规则（微信原始数据 → 本地字段）

| 微信原始形态 | 本地表示 |
|---|---|
| `WCDB_CT_message_content=4`（zstd 压缩） | 解压为 UTF-8 文本 |
| `WCDB_CT_message_content=0`（明文） | 直接 UTF-8 |
| `local_type` 高位含标志位（如 244813135921） | `& 0xffffffff` 取真实类型 |
| 群消息正文 `<sender_wxid>:\n<正文>` | 前缀拆出 `sender_id`/`sender_name`，正文归 content（`sender_name` 是原值，展示时由 `display_names` 名册覆盖） |
| 非文本消息（图片/语音/appmsg XML…） | content = `[image]` / `[appmsg: 标题]` 等提示；**appmsg 的原始 XML 另存 `msg_parts.raw_xml`**（见 §3），其余类型的原始载荷不入库 |
| 贴纸 `msg_type=47` 的 `<emoji>` XML | 解析**只保留两样**：`md5` 与名称——**原始 XML 不入库**（它满是凭据）。`content` = `[贴纸:名称]` / `[贴纸#md5前8位]`；名称先取 XML 的 `desc/emojiattr`，取不到再按 md5 查本机 `emoticon.db`（见 §7）；源 XML 无合法 md5 时不改 `content`，`media_hint` 与 `content` 都保持 `[sticker]`。解析器还会返回 `pack_id`（包归属），但**解析后即丢弃、不落库**——没有任何列存它 |
| `real_sender_id`（整数） | 经同库 `Name2Id` 表解析为 username |
| 表 md5 无法反查会话名 | `chat_id = "md5:<hash>"`（数据不丢，展示降级） |
| 图片 dat（V2：15B 头 + AES-128-ECB 段 + XOR 尾段） | `media export` 解密为明文图片，扩展名按解密后魔数判定（jpg/png/gif/webp/wxgf），落 `media/<会话md5>/image/<月份>/`（按 `msg/attach/<md5(会话)>/<月份>/Img/` 目录枚举定位） |
| 语音 `VoiceInfo.voice_data`（明文 SILK，`\x02#!SILK_V3` 开头） | pilk 解码 + 自封 44B RIFF 头 → 24kHz 单声道 WAV；pilk 缺失时回退存原始 `.slk`（`\x02` 前缀保留） |
| 视频（`msg/video/{YYYY-MM}/{file_key}.mp4`，本身是明文，微信按需下载） | 按 `media_refs` 的 `file_key` 定位后复制为 `.mp4`；本体不在时回退缩略图 `{file_key}_thumb.jpg`（视频树是账号级的，没有索引连接时退回账号级枚举——那条路只有单会话可归属） |

## 6. 朋友圈数据源（sns.db）

朋友圈**不进本地检索库**：`moments export` 直接从微信的 `db_storage/sns/sns.db`
（sqlcipher，复用同一套 passphrase）读取，产出单文件 HTML 时间线（默认档；开 `--with-media`
后另写同级 `media/` 目录），不写 `index.db`。
产物落 `~/.agent-wechat/moments/`（目录 0700、文件 0600）。

源头是一张表：

```sql
SnsTimeLine(tid, user_name, content, pack_info_buf)
```

`content` 是完整的 XML `<SnsDataItem><TimelineObject>…</TimelineObject><LocalExtraInfo>…`。
本机实测 1,923 行，全部可解析。

### XML 字段（`moments.parse_post` 读的就是这些）

| 路径 | 说明 |
|---|---|
| `TimelineObject/id` | 动态 id |
| `TimelineObject/username` | 作者 wxid |
| `LocalExtraInfo/nickname` | 作者昵称——**直接带在动态里，不需要 join 联系人** |
| `TimelineObject/createTime` | 时间戳（unix 秒） |
| `TimelineObject/contentDesc` | 动态正文（可空；本机 1,718/1,923 非空） |
| `TimelineObject/ContentObject/type` | `15` = 视频动态 |
| `ContentObject/mediaList/media` | 媒体引用；`media/type`：`2`=图片、`6`=视频、其余归「未分类」 |
| `media/url@token`、`@key`、`@md5`、`@enc_idx` | **属性，不是子元素**——只看元素文本会漏掉全部鉴权信息（踩过的坑）。**`@token` 不是「能不能下」的判据**：`type=15`（小视频）的 token 在 **URL 查询串**里，`type=7` **根本没有**；两者都判 `AUTH_READY`（URL 已完整，原样 GET） |
| `media/url`（**元素文本**） | URL 本体本身（上面那行是挂在它上面的鉴权属性） |
| `media/thumb`（**元素文本**） | 视频**封面**的 URL——`KIND_VIDEO` 的那条媒体据此另下一张 `{stem}_thumb.jpg`（本期新增，落盘在 `usecases._resolve_media`，同样受白名单约束）。**与下面视频号的 `thumbUrl` 不是一回事**，别混 |
| `media/enc` | 加密标记与密钥：元素文本 `1` ⟺ 有 `enc@key`；`@key` 为 `'0'` 是「无密钥」哨兵（真机 312 次），按无密钥处理 |
| `media/videoDuration` | 视频时长（秒，可缺省） |
| `ContentObject/finderFeed/nickname`、`desc`、`mediaList/media/videoPlayDuration` | 视频号卡片的作者昵称 / 描述 / 时长。这类动态的 `ContentObject/mediaList` 多为空，媒体在 `finderFeed` 下 |
| `finderFeed/mediaList/media/thumbUrl` | 视频号**封面** → 当作一条 `AUTH_READY` 图片进 `media`（`<url>` 是视频本体，不取） |
| `ContentObject/contentUrl`、同级 `ContentObject/title` | 正文里的外链与其标题。`contentUrl` 是 `support.weixin.qq.com` 桩（「微信版本不支持此内容」）时不算外链 |
| `media/LivePhoto/liveMedia/url`、`enc@key` | 实况照片；**`live_url` / `live_key` 不进下载路径**（`build_url` / `_resolve_media` 都不读），故本机不可下载。**理由不是「无 `token`」**——无 `token` 只意味着 `AUTH_READY` |
| `LocalExtraInfo/comment_user_list/user_comment` | 评论（`type=2`；带 `ref_username` 即「A 回复 B」，见下方⚠️） |
| `LocalExtraInfo/like_user_list/user_comment` | 点赞（`type=1`，只有昵称、无正文） |
| `LocalExtraInfo/with_user_list/user_comment` | `type=4`，与评论同归一类 |
| `user_comment/username` | 互动者 wxid——评论与点赞都读它；回复的 `ref_username` 也靠它在**同一条动态**里反查昵称 |
| `user_comment/content` | 评论正文（点赞没有；**为空时渲染成 `[表情评论]` 占位**，判据是「空」而不是「是不是表情」） |
| `user_comment/create_time` | 互动时间（解析了，但当前无渲染消费点） |
| `user_comment/is_rich_text` | 是否富文本（同上，解析了但无消费点） |
| `SnsDataItem`（根） | 解析入口。⚠️ `<user_comment>` 挂在这个根下的 `LocalExtraInfo` 里，**不在** `TimelineObject` 内——取错根节点**不报错**，只会静默得 0 条互动 |

⚠️ **`<user_comment>` 在 `LocalExtraInfo` 下，不在 `TimelineObject` 内**：本机 2,450 条
互动全部落在 `comment_user_list`(1,289) / `like_user_list`(1,157) / `with_user_list`(4)，
`TimelineObject` 里一条都没有。解析时把根节点取错**不会报错**，只会静默得 0 条互动。

⚠️ **`ref_username` 是 wxid，不是昵称**（本机 183/183 条回复全是 `wxid_…`）。XML 里没有
回复方的昵称字段（`ref_*` 子元素只有 `ref_comment_id` / `ref_comment_64id` /
`ref_username`），所以 `moments._comments_of` 按 `username` 在**同一条动态**的
`<user_comment>` 里反查昵称（被回复者点过赞或评论过就查得到，本机 183/183 命中）；
查不到时 `ref_nickname` 为 `None`，渲染层据此省掉「回复 X」那段。

### 缓存窗口（产物必须如实标注的边界）

`createTime` 覆盖的只是微信**滚动缓存**的窗口，不是好友的完整历史：本机实测 1,923 条
覆盖 2013-02 ~ 2026-09，但密度极不均（2013–2023 多数月份仅 1–13 条，2026-01 单月 348 条）。
产物页首固定写明「这是微信本地缓存窗口，不是完整历史」。

媒体**全部是远程的**：每个媒体项只有远程 URL，需联网下载并解密
（默认关闭，见 SECURITY.md §1「零网络例外」）。本机 `cache/*/Sns/Img/` 里的朋友圈缓存图
与聊天图同一把钥匙、可解，但**文件名与动态对不上**（与 `url@md5`、`thumb@md5`、
`media/id` 及 URL 的四种 md5 变体全部 0 命中），因此不参与导出。

## 7. 贴纸名字的数据源（emoticon.db）

贴纸**不进本地检索库的正文**（`content` 只是可读标识，见 §2、§5）。名字的来源有两个——
**逐条的**消息 XML 与**账号级的**本机 `emoticon/emoticon.db`（后者用得着时才读）：

```sql
-- kStoreEmoticonCaptionsTable：md5 → 中文名。**列名全部带尾下划线**（微信自己的命名）
SELECT md5_, caption_ FROM kStoreEmoticonCaptionsTable WHERE language_ = 'default';
```

- **优先级**：消息 XML 的 `desc` → XML 的 `emojiattr` → 本机这张表（按 md5 反查）
  → 回落短 ID `[贴纸#md5前8位]`。实测名称总覆盖率只有**约 9%**：根因是贴纸来自
  703 个不同表情包而本机只装了 5 个——微信只为**你主动添加**的包保留元数据。
  所以九成的贴纸只会显示短 ID，那是**如实呈现**，不是解析失败。
- `language_` 实测**全是 `'default'`**（925/925），与 XML 里 `desc` 的多语言结构
  （`zh_cn`）不是一回事，筛的时候按 `'default'` 精确筛。
- 与 `sns.db` 一样是**微信侧**的库（SQLCipher，同一套 `keys.py` 派生的 passphrase），
  不进 `~/.agent-wechat/`。它只用来取名字，**与贴纸文件解密无关**。
- **降级而非报错**：库缺失 / 结构漂移 / 解密失败一律返回空表，贴纸只是少个名字、
  回落短 ID，绝不因此中断导出。表结构实测另见 [schema-4.1.13.md](schema-4.1.13.md) §emoticon.db。

## 8. 面向 LLM 蒖馏的数据获取

导出后的每条消息已是结构化字段（会话/发送者/是否本人/时间/类型/正文）。
两种取用方式：

**MCP/CLI 即取即用**：`get_recent_messages`（近期上下文）、`search_history`
（按相关性召回，带 snippet 高亮与上下文定位字段 `shard`/`local_id`）。

**批量转 JSONL**（按对话分块喂给 LLM）：

```python
import sqlite3, json, pathlib
db = pathlib.Path.home() / ".agent-wechat/index.db"   # sqlite URI 不展开 ~，须用绝对路径
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
for chat, in con.execute("SELECT DISTINCT chat_id FROM messages"):
    rows = con.execute("""SELECT sender_id, is_self, ts, content FROM messages
                          WHERE chat_id=? AND msg_type=1 ORDER BY ts""", (chat,))
    # 此处依赖具体分块策略（按时间间隔切片等），见 USAGE.md §5 的最小脚本
```

> 📌 按对话窗口自动切片（如 30 分钟静默切段）的蒸馏导出格式为**规划中功能**，
> 当前需按上述方式自行组织。非文本消息在 `messages` 里以提示占位，媒体本体不入 `index.db`
> ——需要本体时走 `agent-wechat media export`（见 USAGE.md §7），产物在 `~/.agent-wechat/media/`；
> appmsg（引用/链接/文件/转账）的结构化正文在 `msg_parts`（见 §3），要读成可读时间线走
> `agent-wechat chat export`（见 USAGE.md §7）。

## 9. 备份与销毁

- 备份：拷贝 `index.db`（+`-wal`/`-shm`）**与 `exclusions.db`** 即可；`~/.agent-wechat/` 已排除出
  Time Machine。**`exclusions.db` 不能漏**：它是唯一不可重建的产物，只恢复 `index.db` 的话，
  被销毁的会话会在下次 `export --full` 时**整批复活**——索引里没有墓碑可拦，而文件系统上看
  不出任何异常
- 销毁：`agent-wechat purge --all --yes`（密钥缓存与全部索引一并清除；微信源数据不受影响）。默认 dry-run，手工 `rm -rf ~/.agent-wechat` 仍是等价后备

## 10. Schema 版本与迁移

本地库带一张版本表（Phase 01 起）：

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    scope      TEXT PRIMARY KEY,   -- 'main' | 'vector' | 'exclusions'
    version    INTEGER NOT NULL,
    applied_at INTEGER NOT NULL
) WITHOUT ROWID;
```

- **三个 scope**：`main`（`index.db` 主表，当前 **5**）、`vector`（vec0 三表，当前 **1**）、
  `exclusions`（在 `exclusions.db` 里，SQL 写作 `exclusions.schema_version`，当前 **1**）。
- **ensure 与 migrate 分工**：`CREATE … IF NOT EXISTS` 每次连接照跑（缺对象就补建的自愈）；
  迁移只处理 `IF NOT EXISTS` **表达不了**的变更（改触发器体、加列、重建表），每条只跑一次。
  版本迁移：`main v2` 把 FTS 三条触发器重建为代码里的最新定义（EA-077 的旧体漂移）；
  `main v3`（Phase 02）建立历史回看游标与重嵌队列（`audit_cursor` / `pending_reembed`）
  并重建重嵌触发器；`main v4`（Phase 03）新增重嵌失败账本 `reembed_failures`；
  `main v5`（Phase 03）给队列加 `reason` 列（表重建）。
- **整次升级 = 一笔事务**：`BEGIN IMMEDIATE; <本次全部待执行迁移的 DDL>; <逐条写版本行>;
  COMMIT`。任一句失败整次回滚（版本停在升级前），抛 `SchemaMigrationError`；下次连接重做。
  已是最新的库走只读快路径，**不拿写锁**；重复执行是 no-op（`applied_at` 都不变）。
- **不假设跨库原子**：`index.db` 与 `exclusions.db` 是两个文件、主库是 WAL，跨文件 COMMIT
  不保证崩溃原子；两个 scope 各自版本化，一个成功一个失败时下次连接接着做。
- **版本过新**：库的版本高于程序已知最新版时**拒绝打开**（不写库），提示升级程序或恢复备份
  ——降级运行新库会把未知结构改坏。
- **备份**：`schema_version` 随 `index.db` 一起备份；`exclusions.db` 里那张随排除清单备份。
- **`purge --index`** 删掉整个 `index.db`，版本表随之消失；重跑 `export` 时按新库初始化。


## 11. 历史回看与重嵌队列（Phase 02）

**`audit_cursor`**——每个 `(账号, 分片, 会话)` 已经**回看**到哪：

```sql
CREATE TABLE IF NOT EXISTS audit_cursor (
    account         TEXT NOT NULL,
    shard           TEXT NOT NULL,
    chat_md5        TEXT NOT NULL,
    audited_local_id INTEGER NOT NULL DEFAULT 0,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (account, shard, chat_md5)
) WITHOUT ROWID;
```

它与导出水位线 `watermark` **分表分键、互不推进**：水位线问「新消息扫到哪」，游标问
「旧消息回看到哪」。每轮导出在每个会话上回看 `store.AUDIT_ROWS`（默认 100）行，游标
走到表尾绕回 0——`ceil(行数 / 100)` 轮覆盖整段历史。游标**只在数据提交之后**才落盘
（先游标后数据会丢历史；反过来最坏只是重看一遍，upsert 幂等）。

**`pending_reembed`**——内容/会话名被改写、等待重嵌的消息：

```sql
CREATE TABLE IF NOT EXISTS pending_reembed (
    msg_key    TEXT PRIMARY KEY,
    changed_at INTEGER NOT NULL
) WITHOUT ROWID;
```

由触发器 `msg_reembed_au` 维护（`AFTER UPDATE OF content, chat_id, sender_name, msg_type`，
`WHEN old IS NOT new`）：
它与消息行、FTS 索引在**同一笔事务**里更新。**它是集合不是日志**：同一条改十次也只有
一行，消费时读 `messages` 的当前内容。

**`reembed_failures`**——消费失败的重试信息（Phase 03）：

```sql
CREATE TABLE IF NOT EXISTS reembed_failures (
    msg_key        TEXT PRIMARY KEY,
    attempts       INTEGER NOT NULL,
    last_error     TEXT,
    last_attempt_at INTEGER NOT NULL
) WITHOUT ROWID;
```

消费成功时与队列行一起删；失败只累计次数与最近原因，队列行留在原处等下一轮。
`purge --chat` 会在删除事务里连这两张表一起清（它们是工作清单，不是用户数据）。
`vector.build_index` 每轮消费最多 4 批（每批 64 条），`rebuild=True` 清空队列。
