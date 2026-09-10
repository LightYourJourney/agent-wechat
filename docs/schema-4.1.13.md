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

关键表：`contact`（12,754 行，含 username/nick_name/remark 等）、`name2id`（12,902 行）、
`chat_room`、`chatroom_member`（18,651 行，群成员）、`biz_info`（公众号）。
注意 `biz_profile.offset` 等列存在**非 UTF-8 字节**，读 TEXT 时须用 `text_factory=bytes`。

## 对去重键的直接影响

同会话跨分片 local_id 重复（不同消息）→ 检索库主键必须为
`(account, chat_table_md5, shard, local_id)`，不能省略 chat 或 shard。
