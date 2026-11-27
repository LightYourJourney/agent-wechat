# CONTEXT：领域词表

本仓库代码与文档里反复出现的词，在此固定含义。**只解释「这个词指什么」**，不解释系统
怎么工作（那在 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)）。出处给到模块或文档，
便于按词索骥。

## 账号（account）
一个已登录微信账号的目录名（形如 `wxid_xxx_abcd`）。出处：`paths.py` / `account.py`。

## 会话（chat_id）
一段对话的稳定标识：`wxid_*`（单聊）、`*@chatroom`（群）、`gh_*`（公众号）或
`filehelper`。出处：`reader.is_chat_id`。

## 退化会话 id（`md5:<hex>`）
会话名无法从 md5 反查时的占位 chat_id；其 hex 部分**就是** `Msg_<md5>` 的 md5，
故解析时不得再 md5 一次。出处：`messages.degraded_chat_id` / `messages.chat_md5`。

## 分片（shard）
微信侧的 `message/message_N.db`——同一会话的消息按时间分区散落在多个分片里，
每个分片的 `local_id` 独立从 1 计数。出处：`paths.message_shards`、
[docs/schema-4.1.13.md](docs/schema-4.1.13.md)。

## msg_key
一行 messages 的去重主键（schema v10 起为五段）：`"<account>|<chat_md5>|<shard>|<local_id>|<create_time>"`
（追加 `create_time` 防微信删表重建撞键，兼容 v10 前的 4 段旧键与旧墓碑）。MCP 对外输出返回短键
`<chat_md5前8位>:<local_id>:<create_time>`，回传时短键/4 段/5 段均接受。出处：`messages.row_key` /
`messages.short_key`、[docs/DATA_MODEL.md](docs/DATA_MODEL.md) §4。

## 水位线（watermark）
「已处理到哪里」的游标。**本仓库有两个互不相干的东西，符号/表名都带限定词区分：**
- 导出水位线（export）：磁盘表 `watermark`，键 `(account, shard, chat_md5)` →
  `max_local_id`，增量导出按 `local_id > 水位线` 续扫。读写入口是
  `store.load_export_watermarks` / `store.clear_export_watermarks`（`export.py`
  不再手写裸 SQL）。出处：`store.py` / `export.py`。
- 向量水位线（vector）：磁盘表 `vector_watermark`，键 `(model_id, account)` →
  已嵌入的 `max(messages.id)`，增量嵌入按 `id > 水位线` 续跑。读写入口是
  `vector.py` 的 `_vector_watermark` / `_set_vector_watermark`。出处：`vector.py` /
  `store.VECTOR_SCHEMA`。

两张磁盘表的名字是冻结面（`~/.agent-wechat/index.db` 的真实数据），不随符号改名。
`store.get_watermark` 是导出侧的单键读取器（零生产调用，保留）。

## 快照三元组
`db` + `-wal` + `-shm` 三个文件一组的一致快照——WAL 模式下少了 `-wal` 会漏最新消息，
所以解密前必须整组拷贝。出处：`decrypt.py`、[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 行契约（messages row）
本地 `index.db` 里一行 messages 的 14 个字段（不含库分配的 `id` 与 store 记账的
`exported_at`），含两条字符串哨兵（`md5:` 退化 id、`[appmsg: ...]` 提示）的编解码。
出处：`messages.py`。

## 入口用例（usecases）
MCP 与 CLI 两个入口共享的一层：参数归一化、账号/会话解析、结果形状只在此实现一次。
出处：`usecases/__init__.py`（重导出面）。

## 上文前缀
向量入库文本的确定性前缀，形如 `[2025-09-10 张三] 上文: … | 正文`——用前一条消息给
短消息补语义（增量续跑时会自动回查水位线前同会话最近一条可嵌入消息，最多回扫 500 条）。
出处：`vector.doc_text`、`vector._prev_text`、[docs/TOOLS.md](docs/TOOLS.md) §7。

## 分区键（partition key）
`vec_messages` 的 `acct_chat` 列，`"{account}|{chat_md5}"`——把 KNN 扫描面缩到单会话；
构建与查询两侧共用。出处：`messages.acct_chat`、`store.VECTOR_SCHEMA`。

## 媒体提示（appmsg 哨兵）
非文本消息的正文占位；appmsg 带标题时形如 `[appmsg: 标题]`，无标题为 `[appmsg]`。
出处：`messages.appmsg_hint` / `messages.has_appmsg_title`。

## 贴纸显示串
`msg_type=47` 的 `content`：有名 `[贴纸:名称]`，无名 `[贴纸#md5前8位]`（短 ID 让 LLM
分得清「同一贴纸发了 5 次」与「5 个不同贴纸」）；源 XML 无合法 md5 时保持 `[sticker]`。
**`media_hint` 不受影响，仍是 `[sticker]`**——两列语义不同。出处：
`messages.sticker_display`（唯一的**写入端**）、`messages.STICKER_PREFIX` /
`STICKER_ID_PREFIX`（两种形态的前缀，写与读共用）、`media.extract_sticker_ref`。
**没有「解析端」函数**：早先的 `is_sticker_display` / `sticker_name_of` 零生产调用，
2026-11-21 随代码一起删除——要判断一段 `content` 是不是贴纸显示串，直接用前缀常量比。

## 任务注册表（tasks）
长任务与互斥任务的内存记录 + `~/.agent-wechat/tasks.json` 持久化（通过独立锁文件 `tasks.json.lock`
每次操作跨进程重读合并写回，重启后 `get_task_status` 仍答得上来）。`rows` 有**两个写入方**：运行中
`tasks.update(rows=)` 写进度（本轮已处理行数，单调递增），跑完由 runner 返回值覆盖为终值。`_harvest()`
按 PID 存活（`pid_alive`）、进程启动时刻（防 PID 复用）与 24 小时心跳过期把死掉的 `running` 收割为
`failed`（无 `interrupted` 枚举值），不会误杀另一进程正在跑的任务。**任务登记归 usecases / 运维编排**，
core 管道只负责执行。出处：`tasks.py`、`usecases/chat.py`、`usecases/retrieve.py`。
