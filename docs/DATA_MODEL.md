# DATA_MODEL — 数据与存储说明

> 本文档说明本地检索库的表结构、字段含义、索引设计、去重与增量策略，以及面向
> LLM 蒸馏的数据获取方式。微信侧原始库结构见 [schema-4.1.13.md](schema-4.1.13.md)。

## 1. 存储位置

```
~/.agent-wechat/            # 目录 0700（AGENT_WECHAT_HOME 可覆盖）
├── passphrase.json         # 密钥缓存（0600），按账号分键
├── index.db                # 本地检索库（SQLite, WAL 模式，明文 + FileVault）
├── index.db-wal / -shm     # WAL 伴生文件
├── tasks.json              # 导出长任务的状态与进度
├── media/                  # 媒体导出产物（图片/语音 WAV/视频，0700，明文）
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
    msg_type    INTEGER NOT NULL,         -- 微信消息类型（已掩码，见 §3）
    sender_id   TEXT,                     -- 发送者 username（Name2Id 解析）
    sender_name TEXT,                     -- 导出时从内容前缀取的原值（群聊为 <sender> 原文）；
                                          -- 查询时优先用 display_names 名册覆盖，本列是
                                          -- join 不上时的兜底
    is_self     INTEGER NOT NULL,         -- 1 = 本人发送
    content     TEXT,                     -- 文本正文；非文本为 [image] 等提示
    media_hint  TEXT,                     -- 非文本类型的提示串
    exported_at INTEGER NOT NULL          -- 入库时间
);
```

**msg_type 常见值**（微信原始类型，已按低 32 位掩码）：
`1` 文本 · `3` 图片 · `34` 语音 · `43` 视频 · `47` 表情 · `48` 位置 ·
`49` appmsg（链接/文件/引用，正文为 XML，入库时转为 `[appmsg: 标题]`）·
`10000` 系统消息 · `10002` 撤回。

## 3. 索引设计

```sql
CREATE INDEX idx_msg_chat_ts ON messages(account, chat_id, ts);  -- 会话内时间序（最常用）
CREATE INDEX idx_msg_ts      ON messages(account, ts);           -- 全局时间线
CREATE INDEX idx_msg_sender  ON messages(account, sender_id);    -- 按发送者
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
    media_kind TEXT NOT NULL,      -- 'image' | 'video'
    file_key   TEXT,               -- 文件名哈希（dat / mp4 的文件名，不含扩展名）
    xml_md5    TEXT                -- XML 里的 md5（兜底留证，与文件名无关）
) WITHOUT ROWID;
```

- **`file_key` 的权威来源是 `Msg_` 表的 `packed_info_data` BLOB**：里面的 32-hex 就是
  attach / video 目录下的文件名。XML 的 `md5` 属性**实测不等于文件名**（0/19,832 命中），
  仅当 packed 缺失且它恰好是 32-hex 时充当兜底，同时留证备查。
- **语音不入此表**：语音文件由 `media_*.db` 的 `VoiceInfo(chat_name_id, create_time, local_id)`
  三元组直接命中，不需要按消息逐条建映射。
- 填充两条路径：导出管道与消息写入**同事务**顺带 upsert；本次功能之前的存量历史用
  `agent-wechat media rebuild-refs` 全量重扫回填（幂等，不重写 `messages`）。
- **当前只写不读**：`media export` 仍按磁盘目录（attach 树 / `msg/video`）枚举定位文件，
  不查此表；这张表是为「按消息定位媒体」的后续能力（含视频的会话归属）预留的索引。

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
| 非文本消息（图片/语音/appmsg XML…） | content = `[image]` / `[appmsg: 标题]` 等提示，原始 XML 不入库 |
| `real_sender_id`（整数） | 经同库 `Name2Id` 表解析为 username |
| 表 md5 无法反查会话名 | `chat_id = "md5:<hash>"`（数据不丢，展示降级） |
| 图片 dat（V2：15B 头 + AES-128-ECB 段 + XOR 尾段） | `media export` 解密为明文图片，扩展名按解密后魔数判定（jpg/png/gif/webp/wxgf），落 `media/<会话md5>/image/<月份>/`（按 `msg/attach/<md5(会话)>/<月份>/Img/` 目录枚举定位） |
| 语音 `VoiceInfo.voice_data`（明文 SILK，`\x02#!SILK_V3` 开头） | pilk 解码 + 自封 44B RIFF 头 → 24kHz 单声道 WAV；pilk 缺失时回退存原始 `.slk`（`\x02` 前缀保留） |
| 视频（`msg/video/{YYYY-MM}/{file_key}.mp4`，本身是明文，微信按需下载） | 账号级枚举 `msg/video/{月份}/` 后直接复制为 `.mp4`；本体不在时回退缩略图 `{file_key}_thumb.jpg`（多会话导出时无从归属，记 `degraded` 跳过） |

## 6. 面向 LLM 蒖馏的数据获取

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
> ——需要本体时走 `agent-wechat media export`（见 USAGE.md §6），产物在 `~/.agent-wechat/media/`。

## 7. 备份与销毁

- 备份：拷贝 `index.db`（+`-wal`/`-shm`）即可；`~/.agent-wechat/` 已排除出 Time Machine
- 销毁：`agent-wechat purge --all --yes`（密钥缓存与全部索引一并清除；微信源数据不受影响）。默认 dry-run，手工 `rm -rf ~/.agent-wechat` 仍是等价后备
