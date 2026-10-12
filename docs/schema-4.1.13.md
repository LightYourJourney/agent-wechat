# 微信 macOS 4.1.13.63 真实 schema（本机实测）

> 探测时间 2026-09-10，主账号 wxid_testacct01_ab12（33/33 库 HMAC 验证通过后解密探测）。
> 微信升级后若读取失败，重跑 `agent-wechat inspect <db>` 对比本文档。

## 总览

- 每账号：`xwechat_files/<wxid>_<hex4>/db_storage/{session,contact,message,general,...}/`
- 消息分片 `message/message_0.db..N.db`：**按时间分区**（同一会话的表可跨分片出现，
  各分片 create_time 区间不相交；local_id 每片独立从 1 计数）
- 库加密：SQLCipher 4，passphrase 账号级，每库 salt 独立派生（见 keys.py）

## message_N.db（消息分片）

```sql
-- 会话消息表：表名 = "Msg_" + md5(chat_username)，每会话一张
CREATE TABLE Msg_<md5>(
  local_id INTEGER PRIMARY KEY AUTOINCREMENT,
  server_id INTEGER,
  local_type INTEGER,          -- & 0xffffffff 才是真类型（高位是标志位）
  sort_seq INTEGER,            -- ≈ create_time*1000，单调
  real_sender_id INTEGER,      -- → 本库 Name2Id 的 rowid
  create_time INTEGER,         -- unix 秒
  status INTEGER,
  upload_status INTEGER, download_status INTEGER,
  server_seq INTEGER,
  origin_source INTEGER,
  source TEXT,                 -- WCDB_CT_source: 0=明文, 4=zstd
  message_content TEXT,        -- WCDB_CT_message_content: 0=明文, 4=zstd
  compress_content TEXT,       -- 旧字段，实测多为 NULL
  packed_info_data BLOB,
  WCDB_CT_message_content INTEGER DEFAULT NULL,
  WCDB_CT_source INTEGER DEFAULT NULL
);

CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER);  -- rowid=real_sender_id
CREATE TABLE TimeStamp(timestamp INTEGER);
CREATE TABLE MessageGroupTimeInfo(...);   -- 群时间线（按需）
-- 注意：sqlite_master 里 Msg_<md5>_SENDERID 是索引名，不是表
```

实测值分布（message_0 全量统计）：`WCDB_CT_message_content`: 0→118,479 行，4→46,112 行。

**内容编码**：
- CT=0：明文 UTF-8
- CT=4：zstd 压缩（`zstandard.decompress`）
- 群聊：content = `<sender_wxid>:\n<正文>`（前缀即发送者）
- 私聊：content = 纯正文；发送者由 real_sender_id 判定
- local_type：1=文本 3=图片 34=语音 43=视频 47=表情 48=位置 49=appmsg(引用/文件/链接,
  正文为 XML) 10000=系统。高位含标志位，需掩码。

**md5 表名 ↔ 会话名解析**：`md5(username)` 需要用户名池（session.db 全部 username +
contact.db name2id + 各分片 Name2Id 并集）；未命中的表以 `md5:<hash>` 作为会话 id 仍可导出。

## session.db

```sql
CREATE TABLE SessionTable(username TEXT PRIMARY KEY, type INTEGER, unread_count INTEGER,
  unread_first_msg_srv_id INTEGER, is_hidden INTEGER, summary TEXT, draft TEXT, status INTEGER,
  last_timestamp INTEGER, sort_timestamp INTEGER, last_clear_unread_timestamp INTEGER,
  last_msg_locald_id INTEGER, last_msg_type INTEGER, last_msg_sub_type INTEGER,
  last_msg_sender TEXT, last_sender_display_name TEXT, ...);
CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY);
CREATE TABLE SessionDeleteTable(username TEXT PRIMARY KEY, delete_time INTEGER);
```

## contact.db

关键表：`contact`（12,786 行，含 username/nick_name/remark 等）、`name2id`（12,934 行）、
`chat_room`（224 行）、`chatroom_member`（18,664 行，群成员）、`biz_info`（公众号）。
注意 `biz_profile.offset` 等列存在**非 UTF-8 字节**，读 TEXT 时须用 `text_factory=bytes`。

**表结构（2026-09-14 复测，微信 4.1.13.63）**。只读探测（`sqlite_master` +
`PRAGMA table_info`），库文件字节数与 mtime 前后不变。共 18 张表（含 SQLite 内建的
`sqlite_sequence`；`agent-wechat inspect` 会跳过 `sqlite_` 前缀，因此它列 17 张）：`biz_info`、
`biz_profile`、`biz_session_feeds`、`chat_room`、`chat_room_info_detail`、`chatroom_member`、
`contact`、`contact_label`、`encrypt_name2id`、`name2id`、`openim_acct_type`、
`openim_appid`、`openim_wording`、`oplog`、`sqlite_sequence`、`stranger`、
`stranger_ticket_info`、`ticket_info`。

```sql
-- contact：22 列，此处只列本项目读的 5 列；完整列见 agent-wechat inspect contact/contact.db
CREATE TABLE contact(id INTEGER PRIMARY KEY, username TEXT, ..., alias TEXT, ...,
  delete_flag INTEGER, ..., remark TEXT, ..., nick_name TEXT, ...);

-- name2id：会话 username ↔ rowid（chat_room.id → name2id.rowid，实测 224/224 命中）
CREATE TABLE name2id(username TEXT PRIMARY KEY);

-- chat_room：群昵称的载体（ext_buffer 是 protobuf，字段 f2，可选，见 roster.py）
CREATE TABLE chat_room(id INTEGER PRIMARY KEY, username TEXT, owner TEXT, ext_buffer BLOB);

CREATE TABLE chatroom_member(room_id INTEGER, member_id INTEGER,
  CONSTRAINT room_member UNIQUE(room_id, member_id));
```

- 显示名名册（`roster`）读的是 `chat_room.ext_buffer` + `contact.remark` / `nick_name`；
  `chatroom_member` 这张表的成员关系本项目**不读**（群成员名单在 `ext_buffer` 的 protobuf 里）。
- `schema.py` 在 `contact.db` 上的声明：`contact` 的 `username` / `alias` / `remark` /
  `nick_name` / `delete_flag`，`chat_room` 的 `id` / `ext_buffer`，`name2id` 的 `username`。

## emoticon.db

贴纸库：`emoticon/emoticon.db`（SQLCipher，与其他库同一套 `keys.py` 派生的 passphrase）。
7 张表，本项目只读下面这一张——贴纸名字的**账号级**来源（`schema.py` 的 `stickers` 声明）。

```sql
-- 贴纸名：md5 → 中文名。**列名全部带尾下划线**（微信自己的命名，别「顺手」去掉）
CREATE TABLE kStoreEmoticonCaptionsTable(
  package_id_ TEXT, md5_ TEXT, language_ TEXT, caption_ TEXT);
```

实测（2026-09-22 复测，微信 4.1.13.63）：**925 行、925 个不同 md5、47 个表情包、零空
caption**；`language_` **全部是 `'default'`**（925/925）。

- 语言列**不是** XML 里 `desc` 那套多语言结构（那边是 `zh_cn`）。筛的时候按
  `language_='default'` 精确筛；不筛的话，微信将来若给同一 md5 出多语言行，
  同一个贴纸会被重复计入。
- 名字的优先级是 `XML desc/emojiattr` > 这张表 > 回落短 ID（spec §6.3）：XML 是逐条的、
  与消息同源，这张表是账号级的按 md5 反查。
- `kNonStoreEmoticonTable.aes_key` 那把密钥**解不出**本机 `business/emoticon/Persist`
  下的贴纸文件（spec 附录 C.3 有证明）——所以这张表只用来取名字，与贴纸文件解密无关。

**另外两张表（2026-10-12 spike 观测；本项目不读，只记形态以免将来重测）：**

```sql
-- 容器寻址表：把一个贴纸 md5 映到它在两股流里的位置（列名带尾下划线，同上一张表）
CREATE TABLE kStoreEmoticonFilesTable(
  package_id_ TEXT, md5_ TEXT, type_ INTEGER, sort_order_ INTEGER,
  emoticon_size_ INTEGER, emoticon_offset_ INTEGER,
  thumb_size_ INTEGER, thumb_offset_ INTEGER);
```

- 实测（2026-10-12，微信 4.1.13.63）：**82 行 / 5 个包**（= 本机装的商店表情包）。
  `md5_` 是**贴纸自己的 md5**，所以容器切片与 `business/emoticon/` 的散文件**可以直接对号**
  ——不是「两套索引」。
- `kNonStoreEmoticonTable` 的 md5 列叫 **`md5`（无尾下划线）**，与上一张表**不同名**，别混。
  实测 748 行（2026-10-12；2026-09-22 当时 743）——**748 个逐个带 `cdn_url`、逐个有散文件**，
  而 `kStoreEmoticonFilesTable` 那 82 个**一个都没有**：两表**交集为 0**，是两批人。
- 出处与实测口径：`内部设计记录` §8。
  **`schema.py` 的 `stickers` 只声明名字表**——上面这两张表**不在**产品读取范围内。

## 对去重键的直接影响

同会话跨分片 local_id 重复（不同消息）→ 检索库主键必须为
`(account, chat_table_md5, shard, local_id)`，不能省略 chat 或 shard。
