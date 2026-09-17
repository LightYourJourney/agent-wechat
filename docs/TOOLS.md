# TOOLS — MCP 工具参考

> 本文档逐个说明 agent-wechat MCP Server 暴露的 13 个工具：用途、参数、返回结构、
> 示例与错误。所有工具经 stdio JSON-RPC 调用；示例为 Agent 视角的调用与实际返回。

通用约定：

- `account` 参数（可选）：目标账号目录名（如 `wxid_xxx_ab12`）。省略时操作**当前
  登录账号**；指定的账号目录不存在时报错。**唯一例外是 `build_vector_index`**
  （省略表示索引全部账号，见 §7）。
- 时间字段一律 **unix 秒**。
- 非文本消息的 `content` 为 `[image]` / `[appmsg: 标题]` / `[voice]` 等提示。
- 错误一律以 `{"error": "中文说明"[, "hint": "可执行修复动作"]}` 信封返回；工具层统一
  捕获并翻译内部异常，**不会有任何异常逃到传输层**（包括会杀死常驻 stdio server 的
  `SystemExit`）。`hint` 仅在存在可执行修复动作时出现，文案已含指引则不重复。
  未预期的内部错误返回 `{"error": "内部错误: <异常类型>: <说明>"}`。

---

## 1. get_account_status

**用途**：检测微信运行/登录状态、当前账号、密钥就绪情况与本地索引规模。
**任何其他工具之前先调用它**排查链路问题。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `account` | string | 否 | null | 目标账号；省略用当前登录账号 |

**返回**：

```json
{
  "wechat_running": true,
  "current_account": "wxid_xxx_ab12",
  "accounts": ["wxid_xxx_ab12", "wxid_yyy_cd34"],
  "key_ready": true,
  "index": { "messages": 841584, "chats": 772, "last_ts": 1789046525 }
}
```

**异常**：微信未运行时 `wechat_running=false`、`current_account=null`（不报错）；
索引库不存在时 `index` 计数为 0。

---

## 2. list_contacts

**用途**：按备注/昵称/微信号模糊查找联系人，把用户口述的名字解析成 `username`
（后续工具的稳定 id）。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `query` | string | 否 | `""` | 模糊匹配词；空则列出全部 |
| `limit` | int | 否 | 20 | 返回上限 |
| `account` | string | 否 | null | 同通用约定 |

**返回**（数组，`kind`: `direct` 私聊 / `group` 群聊 / `official` 公众号）：

```json
[
  { "username": "wxid_abc123def456", "display": "小李（产品）",
    "remark": "小李（产品）", "nick_name": "Leo", "alias": "leoli_dev",
    "kind": "direct" }
]
```

**说明**：重名会返回多条——调用方应列出让用户确认，不要猜。出错时（如 `account`
不存在）返回 `{"error": ...}` 信封而非数组。本工具与 `get_task_status` 是仅有的两个
可能返回数组的工具（后者省略 `task_id` 时返回任务列表），调用方需按形状区分成功与失败。

---

## 3. get_recent_messages ⭐

**用途**：读取与指定联系人的**最近**聊天记录（时间正序），用于生成回复建议。
实时解密微信库（最新分片优先，够数即停），通常 1-3 秒。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `contact` | string | **是** | — | username（`wxid_*` / `*@chatroom` / `filehelper`）或展示名 |
| `limit` | int | 否 | 50 | 返回条数（1-500） |
| `account` | string | 否 | null | 同通用约定 |

**成功返回**：

```json
{
  "chat_id": "wxid_abc123def456",
  "display": "小李（产品）",
  "messages": [
    { "msg_key": "wxid_testacct01|0123456789abcdef0123456789abcdef|message_4.db|22415",
      "time": 1789045400, "sender": "我", "is_self": true,
      "type": "text", "content": "今晚一起吃饭吗" },
    { "msg_key": "wxid_testacct01|0123456789abcdef0123456789abcdef|message_4.db|22416",
      "time": 1789045521, "sender": "wxid_abc123def456", "is_self": false,
      "type": "text", "content": "好啊，七点老地方？" },
    { "msg_key": "wxid_testacct01|0123456789abcdef0123456789abcdef|message_4.db|22417",
      "time": 1789045600, "sender": "wxid_abc123def456", "is_self": false,
      "type": "image", "content": "[image]" }
  ],
  "degraded": []
}
```

> `msg_key` 是消息的稳定标识（`account|chat_md5|shard|local_id`），
> 跨导出保持不变；后续 `get_message_media` 靠它指认「这条消息的媒体是什么」。

**降级说明**：信封**总是**带 `degraded` 键（数组，正常情况下是空数组），每项是
`{capability, effect}`——例如微信升级改了库结构时
`[{"capability": "roster", "effect": "显示名暂不可用，发送者会回落到原始帐号标识"}]`。
不认识的客户端可以忽略该键。降级**不是错误**：结果仍然可用，只是比平时少了一部分
（`search_history` / `semantic_search` 同样总是带这个键）。

**名字消歧返回**（传入展示名且命中多个联系人时——调用方应让用户选择）：

```json
{ "need_confirm": [
    { "username": "wxid_abc123def456", "display": "小李（产品）", "kind": "direct" },
    { "username": "12345678@chatroom", "display": "产品群", "kind": "group" }
] }
```

**错误返回**：`{"error": "找不到联系人「XXX」，请用 list_contacts 查询"}`。

---

## 4. export_full_history

**用途**：[长任务] 把聊天记录全量导出到本地检索库（流式落库、幂等去重、断点续传）。
`search_history` 的数据源。**立即返回 task_id**，不阻塞。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `contact` | string | 否 | null | 只导出该会话（username 或展示名）；省略导出全部。展示名命中多个联系人时返回 `need_confirm`（形状见 §3，含 `kind`），未命中返回 `{"error": "找不到联系人「XXX」，请用 list_contacts 查询"}`——不会静默扩大到全部会话 |
| `full` | bool | 否 | false | true 时忽略水位线全量重扫（幂等） |
| `account` | string | 否 | null | 同通用约定 |

**返回**：`{ "task_id": "a1b2c3d4e5f6", "status": "running" }`

`contact` 歧义时返回 `{"need_confirm": [...]}`（不创建任务，也不会猜）；未命中时返回
`error` 信封。两者都不会误报成「找不到」——候选会一并列出。

随后用 `get_task_status` 轮询（建议间隔 10-30 秒）。中断（Ctrl-C / 进程退出）安全：
重跑自动从断点继续。

---

## 5. get_task_status

**用途**：查询长任务进度（`export_full_history` 的 `kind: "export"`、
`build_vector_index` 的 `kind: "vector"`）。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `task_id` | string | 否 | null | 省略则返回最近 ≤10 个任务的列表 |

**返回**：

```json
{ "id": "a1b2c3d4e5f6", "kind": "export", "account": "wxid_xxx_ab12",
  "chat_id": null, "status": "done", "rows": 841584,
  "started_at": 1789044000, "finished_at": 1789044110, "error": null }
```

`status`: `running` / `done` / `failed`；失败时 `error` 含中文原因。向量索引任务
（`kind: "vector"`）额外带 `rebuild`，`rows` 为本轮**已嵌入行数**，`skipped` 为**本轮**按
规则跳过的行数（非文本/无标题的 appmsg、正文短于 2 字符的消息）。

---

## 6. search_history ⭐

**用途**：在本地检索库中做全文检索（需先 `export_full_history`）。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `query` | string | **是** | — | 检索词（≥3 字符走 FTS5 trigram；1-2 字自动 LIKE） |
| `contact` | string | 否 | null | 限定会话：username 直接使用；展示名解析出多个联系人时返回 `need_confirm`，未命中返回 `error`——**不会静默扩大检索范围** |
| `limit` | int | 否 | 20 | 返回上限 |
| `account` | string | 否 | null | 同通用约定（检索严格限定该账号的索引数据） |

**返回**：

```json
{
  "mode": "fts",
  "total": 2,
  "hits": [
    { "msg_key": "wxid_testacct01|0123456789abcdef0123456789abcdef|message_4.db|22415",
      "id": 735662, "chat_id": "wxid_abc123def456",
      "shard": "message_4.db", "local_id": 22415, "ts": 1789045400,
      "msg_type": 1, "sender_id": "wxid_testacct01",
      "sender_name": "wxid_testacct01", "is_self": 1,
      "content": "周末爬山的那天记得带水", "media_hint": null,
      "snip": "…【周末爬山】那天记得带水…" }
  ]
}
```

**说明**：
- `mode`: `fts`（trigram 命中，含高亮 `snip`）| `like`（1-2 字兜底，`snip` 为 null）
- 1-2 字查询**建议传 `contact`**，否则全表扫描（秒级）
- 索引为空时返回 `{"hits": [], "hint": "本地索引为空：请先运行 export_full_history", ...}`
- `msg_key` 是消息的稳定标识（`account|chat_md5|shard|local_id`），跨导出保持不变；
  与 `hits` 里既有的 `id`（`messages.id`，DB rowid）不是一回事——rowid 随重建变化，
  `msg_key` 由消息的身份/位置字段派生（不含内容，编辑消息不会改它），
  可用于 `get_message_media` 指认消息。
- `degraded` 键（§3）在显示名降级时非空：那时 `sender_name` 会回落成 wxid，
  **不要**当成真实姓名呈现

---

## 7. build_vector_index

**用途**：构建本地向量语义索引（`semantic_search` 的数据源）。**长任务**：立即返回 `task_id`，
用 `get_task_status` 查进度。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `rebuild` | bool | 否 | false | true 时先清空索引再全量重嵌（取得连续上下文） |
| `account` | string | 否 | null | **省略则索引全部账号**（与其它工具相反）；水位线按账号分别记录 |

```json
// 入参
{ "rebuild": false, "account": null }
// 返回
{ "task_id": "a1b2c3d4e5f6", "status": "running" }
```

- 嵌入在本机 CPU 完成（fastembed + `BAAI/bge-small-zh-v1.5`），**聊天内容不外发**。
- 模型仅首次下载（约 95MB，存于 `~/.agent-wechat/models`），之后一律从本地缓存加载：
  **运行期不发起任何网络请求**。若想让缓存缺失时直接报错而不是回退下载，可设 `HF_HUB_OFFLINE=1`。
- 全量构建实测约 2.5–3 小时（84 万条消息 → 68 万向量；2026-09-11 真机，含多次中断续跑）——
  插入成本随索引增大而上升，后期显著慢于前期。中断/崩溃后重跑自动从水位线续传，随时可停。
- 数据源是 `export_full_history` 落库的**本地消息库**（不直接读微信）：先导出，再建向量索引；
  之后新增导出的消息再跑一次本工具（省略 `rebuild`）即增量补齐。
- **增量运行会丢失每个会话首条消息的上下文前缀**：`上文:` 前缀在**同一轮运行内**按会话累积，
  增量运行从水位线开始，因此各会话本轮第一条被嵌入的消息没有前缀（即便它确有前一条消息）。
  需要完整连续的上下文时用 `rebuild: true` 全量重建。
- 进度见 `get_task_status`（`kind: "vector"`，`rows` 为本轮已嵌入行数，`skipped` 为本轮跳过行数）。

---

## 8. semantic_search

**用途**：按**语义**（意思相近）检索聊天记录，适合关键词记不全时使用。

与 `search_history` 的分工：精确关键词、人名、专名用 `search_history`（FTS5 更快更准）；
模糊意图（如「上次说的那个爬山的安排」）用 `semantic_search`。需先运行 `build_vector_index`。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `query` | string | **是** | — | 自然语言检索意图（不是关键词） |
| `contact` | string | 否 | null | 限定会话：username 直接使用；展示名解析出多个联系人时返回 `need_confirm`，未命中返回 `error` |
| `limit` | int | 否 | 10 | 返回上限（1-50，超出按 50 处理） |
| `account` | string | 否 | null | 同通用约定 |

```json
// 入参
{ "query": "上次说的那个爬山的安排", "contact": null, "limit": 10, "account": null }
// 返回
{ "mode": "semantic",
  "model": "fastembed:BAAI/bge-small-zh-v1.5:512",
  "total": 1,
  "hits": [ { "msg_key": "wxid_testacct01|0123456789abcdef0123456789abcdef|message_4.db|22415",
              "chat_id": "wxid_xxx", "display": "张三", "ts": 1757462400,
              "sender": "张三", "is_self": false,
              "content": "周末爬山的安排定了吗", "media_hint": null,
              "distance": 0.1842 } ] }
```

- `distance` 为 cosine 距离，越小越相近。
- `msg_key` 是消息的稳定标识（同 `search_history`），可用于 `get_message_media`。
- `degraded` 键（§3）在显示名降级时非空——那时 `display` / `sender` 会回落成 wxid。
- **没有相关性阈值**：KNN 恒返回距离最近的 `limit` 条（无论多不相关）；`total` 是本次
  返回的条数（≤ `limit`），**不代表相关程度**——结果是否可用由调用方按 `distance` 判断，
  不要把 `hits` 直接当作「相关结果」呈现。
- **展示名必须经 `contact` 参数解析**（解析发生在算 md5 之前）：未命中返回 `error`、
  命中多个返回 `need_confirm`——**失败是显式的**，不会静默返回空，也不会静默扩大范围。
  CLI 的 `semantic --contact` 同理（无法唯一解析时非零退出）。
- **会话过滤按 `md5(chat_id)` 匹配**：只有 canonical 用户名（`wxid_*` / `*@chatroom` /
  `gh_*` / `filehelper`；reader 对无法反查的会话给出的 `md5:<hex>` 退化形式也特判支持）
  能命中。一个能解析、但**没有任何已入索引消息**的会话（该会话从未导出，或消息全被跳过）
  返回 `{"total": 0, "hits": []}` 且**不带 hint**——与「确实没有命中」不可区分。
- 索引未构建返回 `{"error": ..., "hint": "请先运行 build_vector_index"}`；
  索引由其他模型构建时返回错误并提示重建（`vector-build --rebuild` 或
  `build_vector_index(rebuild=True)`），不会混用不同模型的向量。
- 检索延迟（2026-09-11 真机实测，68 万向量）：单会话 ~76ms；全局热查询 ~0.6s，
  冷查询（首次触盘）~2.6s（暴力扫描，无 ANN）。

---

## 9. export_chat_media

**用途**：把某会话的媒体文件导出到本机（图片解密 / 语音转 WAV / 视频明文复制），
返回各类计数与输出目录。图片依赖磁盘密钥推导；语音转 WAV 依赖 pilk（缺失时导出
原始 `.slk` 并记 `degraded`）；视频直接复制明文。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `contact` | string | **是** | — | username（`wxid_*` / `*@chatroom` / `filehelper`）或展示名（歧义返回 `need_confirm`，形状见 §3；未命中返回 `{"error": "找不到联系人「XXX」，请用 list_contacts 查询"}`） |
| `kinds` | string | 否 | `"image,voice,video"` | 逗号分隔的媒体类型（`image` / `voice` / `video`）；含未知值报错 |
| `months` | string | 否 | null | 逗号分隔的月份（`YYYY-MM`），按消息时间过滤；省略不限 |
| `limit` | int | 否 | 500 | 本次最多**新增**的文件数（下界 1，无上界；产物已存在的不计） |
| `force` | bool | 否 | `false` | `true` = 忽略已存在的产物全部重做（默认跳过已存在的） |
| `account` | string | 否 | null | 同通用约定 |

**成功返回**：

```json
{
  "chat_id": "wxid_abc123def456",
  "out_dir": "/Users/you/.agent-wechat/media",
  "counts": {
    "image": { "ok": 12, "failed": 1, "missing": 0, "skipped": 0, "remaining": 0 },
    "voice": { "ok": 8, "failed": 0, "raw_only": 2, "skipped": 0, "remaining": 0 },
    "video": { "ok": 3, "thumb": 1, "missing": 0, "skipped": 0, "remaining": 0 }
  },
  "degraded": [],
  "truncated": false
}
```

**说明**：
- 产物按 `out_dir/<会话md5>/<类型>/<月份>/` 落盘，目录 0700、文件 0600。信封**不回传
  文件清单**（大结果膨胀上下文），明细用文件系统工具在 `out_dir` 下自行查看。
- **导出数量远小于会话里的媒体消息数是常态**：微信按需下载并定期清理本地媒体，
  历史图片/视频大概率已不在磁盘上——能导出的只有磁盘还保留的部分，`missing` 大
  不是错误。
- 计数口径：`image.failed` = 解密/未知格式/落盘失败数；`voice.raw_only` = pilk 缺失时
  导出的原始 `.slk` 数；`video.thumb` = 本体不在、回退缩略图的数量；
  **`video.missing` 现在还会数「有视频消息、但本机既没有本体也没有缩略图」**（此外仍是
  文件在磁盘上却读不了/写不出来的那些）——微信只为**近期**消息保留本体，本机实测
  4,626 条视频消息只剩 30 个文件，所以**几千的 `video.missing` 是如实回答，不是故障**；
  判断是不是故障，看 `video.ok` / `video.thumb` 是否意外为 0。`image.missing` **当前恒为
  0**——图片导出直接扫磁盘目录，不按消息逐条清点；该键为将来 `media_refs` 驱动的按消息
  导出预留。
- `kinds` 传空串（或全是逗号/空白）表示**不导任何类型**：不报错，成功返回且三类计数全为 0
  （只有 0700 权限等环境类 `degraded` 可能非空）。想导出全部请显式给 `image,voice,video`。
- `degraded` 键**恒存在**（与 §3 同一惯例：降级不是错误），但此处每项是**中文字符串**
  （图片密钥推导失败、pilk 缺失、wxgf 未转 JPEG、输出目录权限未收紧、视频索引相关等），
  不是 `{capability, effect}`。图片密钥问题不影响语音/视频导出。
- **视频是唯一依赖索引的类型，因此多出四种降级说明**（措辞已指明该做什么，别混成一件事）：
  1. 该账号一条视频引用都没有 → 索引建立在 `media_refs` 之前（或建索引时本机没有视频消息）
     → 跑 `media rebuild-refs` 补齐引用；
  2. 请求的会话在索引里没有视频引用（该账号其它会话有，说明索引可用）→ **无需修复**。注意
     这条只说明「没有引用」，**不等于**这些会话确实没有视频消息——索引里本就有视频消息拿不到
     引用（实测 222 条），它们的会话会被归到这一条；
  3. 请求的会话**有**引用、但它们的**消息**不在索引里 → 只有**导出这些会话的历史**有效
     （`export_full_history` / CLI `export` 同时写消息与引用；`media rebuild-refs` **只写引用、
     不写消息**，对这种索引永远修不好）；
  4. 索引库打不开或读不了（如损坏）→ **本次不导视频**，其余类型照常——**不会**退回旧的
     账号级枚举：那会把账号视频树里的**全部**文件写进本次请求的会话目录（错归属比缺归属更坏）。
- `counts[*].skipped` 是本次因产物已存在而跳过的**文件**数（**按文件**计，不是消息条数）；
  它不吃 `limit` 预算。`counts[*].remaining` 是撞上 `limit` 时还有多少个**未导出**（CLI 印的是
  三类之和）。
- `truncated=true` 表示触及 `limit` 提前收手，结果不完整——`remaining` 含续跑时**仍会失败**的
  那些条目（dat 读不出、本体不存在），故它不等于「续跑一定多出这么多文件」。已存在的产物会被
  跳过，**重跑同一条命令即可继续**；要全部重做用 `force=true`——它**不**补缩略图：本体已在产物
  里的视频不会再生成 `{K}_thumb.jpg`。
- 存量历史回填（`media rebuild-refs`）是一次性运维动作，仅提供 CLI（见 USAGE.md），
  MCP 侧无对应工具。

---

## 10. export_moments

**用途**：读取本账号可见的朋友圈动态（正文、作者、时间、评论、点赞），渲染成
**单文件自包含的 HTML 时间线**，返回计数、时间窗口与输出目录。
**默认不联网**——不加 `with_media` 时全程零网络，产物里的媒体一律标「远程，未下载」。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `contact` | string | 否 | null | 按**作者**筛选（username 或展示名）；省略 = 全部好友。歧义返回 `need_confirm`（形状见 §3），未命中返回 `{"error": "找不到联系人「XXX」，请用 list_contacts 查询"}` |
| `months` | string | 否 | null | 逗号分隔的月份（`YYYY-MM`），按动态时间过滤；省略不限 |
| `with_media` | bool | 否 | `false` | **是否联网下载并解密媒体本体**。`false`（默认）= 零网络；`true` = 向微信 CDN 发起请求（见下方「联网边界」） |
| `limit` | int | 否 | 500 | 动态条数上限（下界 1，无上界）。截断留下的是**最新**的那些动态 |
| `account` | string | 否 | null | 同通用约定 |

**成功返回**：

```json
{
  "out_dir": "/Users/you/.agent-wechat/moments",
  "counts": {
    "posts": 500, "comments": 312, "likes": 288, "remaining": 0, "failed": 0,
    "media_downloaded": 0, "media_not_downloaded": 1204, "media_failed": 0
  },
  "window": { "earliest": 1360159626, "latest": 1789046525 },
  "degraded": [],
  "truncated": true
}
```

**说明**：
- 产物是 `out_dir/index.html`（目录 0700、文件 0600），双击即可离线阅读。信封**不回传
  文件清单**，明细用文件系统工具在 `out_dir` 下自行查看。
- **`window` 是微信本地缓存的窗口，不是完整历史**——更早的动态不在本机。本机实测
  1,923 条覆盖 2013-02~2026-09，且密度极不均。产物页首也会如实标注这一点。
- **`with_media=false` 时产物不含任何图片**：朋友圈媒体只有远程 URL，本机的缓存图
  与动态对不上（文件名四种哈希变体全部 0 命中），因此不参与渲染。想要图必须开 `with_media`。
- **联网边界**（本项目零网络红线上**本次新增的**显式例外）：仅当 `with_media=true` 时，
  按动态逐条构造 URL 并**直连**（不使用环境代理，不读 `http_proxy`）微信 CDN 的
  `*.qpic.cn` / `*.video.qq.com`；域白名单**跨重定向也成立**。请求只携带微信自己签发的
  媒体 `token` 与固定 `User-Agent`，不含本机任何聊天 / 索引内容。媒体密钥与 token
  绝不进日志、异常消息或本信封。
- **`media_failed` 是常态而非错误**：微信服务端会清理老媒体，实测抽样中约三成带 token
  仍返回 404。单条媒体失败只计数，不中断整次导出。
- **`failed` 是「动态 XML 没读懂」的条数**（与媒体无关，两者别混）：解析失败的单条只
  跳过并计数。它与 `posts` 同量级时，多半是微信改了动态的 XML 结构——**不要**把
  `posts: 0` 当成「这个账号没有朋友圈」，`degraded` 里必有一条说明，产物页首也写着
  「解析失败 N 条」。
- `degraded` 键**恒存在**（与 §3 同一惯例：降级不是错误），此处每项是**中文字符串**，
  且**同类问题只写一项**（带条数）。目前两个来源：动态解析失败、下载成功但解密后不是
  已知图片格式。
- 联系方式里的回复（「A 回复 B」）显示的是 **B 的昵称**：XML 里只有 B 的 wxid
  （`ref_username`），昵称按 `username` 在**同一条动态**的互动里反查；查不到时省略
  「回复 B」那段，不拿 wxid 顶替。
- `truncated=true` 表示触及 `limit` 提前收手，此时 `counts.remaining` 报出还有多少条**更早**
  的动态没进来（留下的是最新的那些）。
- `media_not_downloaded` 涵盖两种情形：「未开启 `with_media`」与「该媒体没有 `token`，
  构造不出下载地址」。
- 表情评论只渲染 `[表情评论]` 占位，本体不下载；分享链接自带的第三方缩略图不是
  朋友圈媒体，不抓取。
- 歧义走既有 `{"need_confirm": [...]}`、未命中走 `{"error": …}`、账号不存在走
  `{"error": "账号目录不存在: …"}`，与其它工具同形（见 §3）。

---

## 11. get_message_media

**用途**：查一批消息各自的媒体状态——有没有媒体、什么类型、本机在不在、产物在哪。
是「消息 → 媒体」的按需查询：`export_chat_media`（§9）按会话批量导出且信封不回传文件
清单，这里由调用方自己缩小范围（给 `keys`），所以逐条回传明细不会膨胀上下文。

**前提**：需先运行 `export_full_history`——判定读的是本地检索库（`messages` + `media_refs`），
索引里一条消息都没有时，每个会话的答案都是一份零（`counts` 全 0、`messages: []`），
信封另带 `hint` 说明「本地索引为空」，别把它当成「这个会话没有媒体」。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `contact` | string | **是** | — | username（`wxid_*` / `*@chatroom` / `filehelper`）或展示名（歧义返回 `need_confirm`，形状见 §3；未命中返回 `{"error": "找不到联系人「XXX」，请用 list_contacts 查询"}`） |
| `keys` | string | 否 | null | 逗号分隔的 `msg_key`（取自 §3 / §6 / §8 返回的 `msg_key` 字段）；省略 = 该会话**全部媒体类型的消息**。只答属于该会话的 key：别的会话的 key、以及**索引里根本没有的 key**（消息未导出、或已从索引消失）都**静默略过**——答案里没有某个 key，不等于它不是媒体消息 |
| `months` | string | 否 | null | 逗号分隔的月份（`YYYY-MM`），按消息时间过滤；格式不符返回 `{"error": "月份格式应为 YYYY-MM：…"}`（不静默 0 命中） |
| `limit` | int | 否 | 200 | 消息条数上限（1-500，超出按边界处理）。`months` 在 SQL 里筛，故 `limit` 卡的是**命中月份**的条数 |
| `out_dir` | string | 否 | null | 已导出产物的根目录；省略按 `~/.agent-wechat/media`。**要与导出时用的 `--out` 同一个**，否则已导出的产物会被判成 `exportable` |
| `account` | string | 否 | null | 同通用约定 |

**成功返回**：

```json
{
  "chat_id": "wxid_abc123def456",
  "out_dir": "/Users/you/.agent-wechat/media",
  "counts": { "none": 3, "exported": 1, "exportable": 19, "gone": 30 },
  "by_kind": { "voice": { "exportable": 19 }, "image": { "exported": 1, "gone": 22 },
               "video": { "gone": 8 } },
  "truncated": false,
  "messages": [
    { "msg_key": "wxid_abc123def456|9f2c1a…|message_0.db|81234", "time": 1789046102,
      "kind": "voice", "status": "exportable", "path": null }
  ],
  "degraded": []
}
```

**说明**：
- `status` 四态（判定顺序严格按此序）：
  - `none` —— 不属于本功能覆盖的三类媒体（图片/视频/语音）：类型由消息类型判定（不依赖
    引用表），贴纸、文件、位置、appmsg 等都归这里。**它不是「这条消息没有媒体」**——索引
    自己对贴纸/位置/appmsg 也挂 `[sticker]` / `[location]` / `[appmsg: …]` 之类的提示，
    只是本功能不覆盖这几类；`kind` 为 `null`。要判断这些类型，看消息信封里的 `type` /
    `content`
  - `exported` —— 产物已在 `out_dir` 下，`path` 是产物**绝对路径**
  - `exportable` —— 有媒体且**本体还在微信里**，但还没导出到 `out_dir`
  - `gone` —— 有媒体，但本机找不到
- **`gone` 是常态，不是错误**：微信只为**近期**消息保留媒体本体，而近期消息往往还没导出进本索引，
  所以本机能对上的极少。实测（2026-09-16）对索引内已有的消息：图片本体兑现率约 **0.005%**
  （2 / 39,307），视频 **0**（/ 4,404）；语音相反——本体存在 `media_*.db` 里、微信不清理，
  **7,390 条 100% 有**。所以 `exportable` 在本机**实质上是语音专属状态**。
- **`gone` 也可能是「定位不了」，不是「本机没有」**：媒体消息拿不到引用（没有 `file_key`）时，
  产物探测拒绝猜文件名，于是即使磁盘上真有产物也答 `gone`——这一类的 `gone` 只说明「判不出来」。
- **`by_kind` 是把 `counts` 按 `image` / `video` / `voice` 再拆一次**：三类的本机可得率
  相差几个数量级，池化的 `counts` 会把「语音几乎全可导出」与「图片视频几乎全 `gone`」
  加成一个数字，看起来像「这个会话的媒体大多没了」。`kind` 为 `null` 的 `none` 态没有
  类型，只进 `counts`。
- **`exportable` 不保证本体可用**：视频只定位到缩略图时也算 `exportable`（本机只有一张
  jpg，`mp4` 本体不在）。
- `counts` 四键**恒存在**（含 0）；`degraded` 键**恒存在**（每项是 `{capability, effect}`，
  形状见 §3 的约定）。当前来源是语音库读不到（`message/media_*.db` 漂移）——此时**尚未导出**
  的语音一律按 `gone` 呈现（已导出的语音仍照实报 `exported`），不会假装成 `exportable`。
- `truncated=true` 表示触及 `limit` 提前收手——此时 `counts` / `by_kind` 只是**答完的
  这些**，不是全量；可按 `months` 分批重跑。
- **不写微信目录、不写 `out_dir`、不联网**——判定只做 `pathlib` 探测与索引只读查询。
  唯一的磁盘写入是**语音本体判定**：要解密 `media_*.db`，明文只在 `~/.agent-wechat/tmp/`（0700）
  短暂存在、用后即焚（与解密管道是同一个机制，见 SECURITY.md §4「临时文件」）。
  该会话一条语音消息都没有时，连这一步都不发生。

---

## 12. export_chat

**用途**：把某会话导出成可读的时间线（HTML / Markdown），返回产物路径与计数。产物里 appmsg
（引用 / 链接 / 文件 / 转账…）渲染成结构化块，媒体以相对路径引用**同级**的既有媒体树。
与 §9 的分工：`export_chat_media` 只搬媒体文件，本工具产出的是**可读的会话正文**。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `contact` | string | **是** | — | username（`wxid_*` / `*@chatroom` / `filehelper`）或展示名（歧义返回 `need_confirm`，形状见 §3；未命中返回 `{"error": "找不到联系人「XXX」，请用 list_contacts 查询"}`） |
| `months` | string | 否 | null | 逗号分隔的月份（`YYYY-MM`），限定**一段连续窗口**。**必须按时间升序、相邻月首尾相接**：非连续（`2026-08,2026-10`）只能取包络、会把 9 月静默带进来，反序会得到倒置（或退化成空区间）的窗口——两者都返回 `{"error": "…"}`，**不静默产出空产物**；格式不符同理 |
| `limit` | int | 否 | 500 | 封顶窗口内**最近的** N 条（非正值按 1 处理：`LIMIT -1` 在 SQLite 里是「不限量」） |
| `format` | string | 否 | `"html,md"` | 逗号分隔，`html` / `md`；空串或未知格式返回 `{"error": "…"}`（「少写一个文件」与「格式名写错」长得一样，故不静默跳过） |
| `out_dir` | string | 否 | null | 输出根；省略按 `~/.agent-wechat/media`。**必须与 §9 `export_chat_media` / CLI `media export` 用同一个根**——产物里的媒体是相对引用，换个根就全是死链 |
| `account` | string | 否 | null | 同通用约定 |

**成功返回**：

```json
{
  "chat_id": "wxid_abc123def456",
  "out_dir": "/Users/you/.agent-wechat/media/9f2c1a0b7d3e",
  "files": ["/Users/you/.agent-wechat/media/9f2c1a0b7d3e/chat/index.html",
            "/Users/you/.agent-wechat/media/9f2c1a0b7d3e/chat/index.md"],
  "counts": { "text": 41, "image": 3, "appmsg": 12, "system": 2 },
  "truncated": false,
  "unparsed": 0,
  "rejected": 0,
  "degraded": []
}
```

**说明**：
- 信封**不回传正文与逐条消息的明细**（沿用 §9 的既定原则：大结果膨胀上下文）：只给产物
  **路径**与计数，会话内容在产物里，用文件系统工具在 `out_dir` 下自行打开。`files` 只有
  **真正落盘**的那几个——写失败的不进列表，所以「少了一行」本身就是信号。
- `out_dir` 是**本会话的媒体树根**（`{--out}/{会话md5}`），产物在它的 `chat/` 子目录里
  （`index.html` / `index.md`）。刻意不回传输出**根**：那需要重算一次会话 md5，而「两套路径
  推导」的分歧就是产物里所有媒体引用的静默 404。
- `counts` 是 `kind` → 条数：`text` / `image` / `voice` / `video` / `sticker` / `location` /
  `status` / `system` / `revoke` / `appmsg`；未知类型回落成 `msg_type` 的数字字符串（不猜名字）。
- `unparsed` 与 `rejected` 都是 appmsg 且**分开计数**：前者「解不开」，后者「主动拒绝」
  （含 DTD / 实体声明的载荷，防 billion laughs）。两者非零时部分 appmsg 会回落成
  `[appmsg: 标题]`，明细印在**产物顶部**。
- `truncated=true` 表示触及 `limit`（只导了窗口内最近的这些）——它同时印在**产物顶部**，
  不只回终端：产物会离开这次会话，只回终端的话读产物的人看不到。
- `degraded` 键**恒存在**（形状同 §3 的 `{capability, effect}`），当前三个来源：
  1. `msg_parts` 未回填 → appmsg 会显示成 `[appmsg: 标题]`，跑 CLI `agent-wechat appmsg
     rebuild` 补齐（本机实测 37.5–38.7 s、幂等）；
  2. `msg_media` 媒体状态覆盖不足（媒体状态最多答 500 条）→ **没覆盖到的消息在产物里不会
     出现媒体块**（`effect` 里给了「答了 N/M 条」）；
  3. 显示名降级（`roster`，如 `contact.db` 漂移）→ 标题与发送者回落成原始标识。
- **不联网、不写微信目录**：只往 `out_dir` 下写产物（目录 0700、文件 0600 的聊天原文）。

---

## 13. get_message_parts

**用途**：取一批消息的 appmsg 结构化内容——引用 / 链接 / 文件 / 转账 / 其余类型。
是 §11 的姊妹工具：那个答「有没有媒体、什么类型、在哪」，这个答「内容是什么」。由调用方给
`keys` 自己缩小范围，故逐条回传明细不会膨胀上下文。

**前提**：需先运行 **CLI `agent-wechat appmsg rebuild`**——内容读的是本地检索库的 `msg_parts`
表。**未回填时每个 key 都答不出东西**，此时信封另带 `hint` 与 `degraded` 说明。

**空答案有三种成因，信封只分得出其中两种**（别宣称「靠这几个键就能分清」）：

1. **未回填**（账号级）→ `degraded` + `hint` 指出，重跑 `appmsg rebuild` 可补救；
2. 这条消息本来就不是 appmsg；
3. **回填之后才到微信的消息**（或那条 appmsg 的载荷本身为空——两者都不产生行）——
   它的行不在 `msg_parts` 里，于是 `parts` 空、
   `unparsed` / `rejected` 为 0、`degraded` 为空，**与第 2 种逐字节相同**（`backfilled` 是
   **账号级**的，答案是**按 key** 的）。出路同样是重跑 `agent-wechat appmsg rebuild`（幂等）：
   回填扫的是**微信库**（全部分片的全部 `Msg_%` 表，载荷为 NULL 的除外），不是索引库
   ——所以「行在不在」取决于**消息何时到达微信**，与它何时导出无关（`export` **不写**
   `msg_parts`）。

第 2/3 种只能靠**调用方手上别的信号**自己分：`get_recent_messages`（§3）的 `type` / `content`，
或 `search_history`（§6）命中里的 `msg_type` / `content`（appmsg 的 `content` 是
`[appmsg: 标题]`）——**信封本身不给这个信号**。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `contact` | string | **是** | — | username（`wxid_*` / `*@chatroom` / `filehelper`）或展示名（歧义返回 `need_confirm`，形状见 §3；未命中返回 `{"error": "找不到联系人「XXX」，请用 list_contacts 查询"}`）。**`keys` 只答属于该会话的**：别的会话的 key 静默略过 |
| `keys` | string | **是** | — | 逗号分隔的 `msg_key`（取自 §3 / §6 / §8 返回的 `msg_key` 字段）。msg_parts 里没有的 key（回填跑在它之前、不是 appmsg）**静默略过**——答案里没有某个 key，不等于它不是 appmsg。**载荷解不开 / 被主动拒绝的不在此列**：它们进下面的 `unparsed` / `rejected` 计数。空串 → `parts: []` |
| `account` | string | 否 | null | 同通用约定 |

**成功返回**：

```json
{
  "parts": [
    { "msg_key": "wxid_abc123def456|9f2c1a0b7d3e|message_0.db|81234",
      "kind": 57, "kind_name": "引用", "title": "回复",
      "fields": [],
      "quote": { "svrid": 81230, "displayname": "李四", "content": "明天开会",
                 "create_time": 1789000000, "from_usr": "wxid_friend02" },
      "link": null, "file": null, "transfer": null,
      "ref_msg_key": "wxid_abc123def456|9f2c1a0b7d3e|message_0.db|81230" },
    { "msg_key": "wxid_abc123def456|9f2c1a0b7d3e|message_0.db|81240",
      "kind": 2000, "kind_name": "转账", "title": "微信转账",
      "fields": [], "quote": null, "link": null, "file": null,
      "transfer": { "paysubtype": "1", "amount_desc": "¥88.00", "memo": "午饭",
                    "payer": "wxid_friend02", "receiver": "wxid_abc123def456",
                    "transaction_id": "10000000000000000000000", "transfer_id": "t1",
                    "begin_time": 1789000000, "effective_date": 1789086400,
                    "invalid_time": 1789600000, "is_red_envelope": false },
      "ref_msg_key": null }
  ],
  "unparsed": 0,
  "rejected": 0,
  "degraded": []
}
```

**说明**：
- 每条恒有十个键：`msg_key` / `kind` / `kind_name` / `title` / `fields` / `quote` / `link` /
  `file` / `transfer` / `ref_msg_key`。顺序按入参 `keys`（去重后、保序）。
- `kind` 是 appmsg 的数字 `type`；`kind_name` 是中文名（引用 57 / 链接 5 / 文件 6 / 转账 2000 /
  合并转发 19 / 小程序 33 / 视频号 51 / 接龙 53 / 表情 8）。**含义未经确认的类型回落成
  `appmsg:<type>`——不猜**（如 4 / 62 / 87 / 36 / 2001）。
- 四个具名块 **`quote` / `link` / `file` / `transfer` 按 `kind` 择一非空**，其余为 `null`：
  57 → `quote`，5 → `link`，6 → `file`，2000 → `transfer`，其它类型四者全 `null`（内容在
  `fields` 里）。
- `fields` 是未具名建模的叶子字段，`[[点分路径, 值], …]`，文档序（如 `[["sources.source",
  "weibo"]]`）。**密钥 / 凭据已剔除**：名字逐段命中 `aeskey|passphrase|token|key`，或值里
  带 `?token=` / `&key=` 一类的凭据参数（链接的 `url` 同样过滤）。已具名建模的子树
  （`refermsg` / `appattach` / `wcpayinfo`）与它们的兄弟叶子（`kind=5` 的 `url` / `des`）
  **不在字段表里重复出现**——那些值在具名块里。
  **内层 XML 载荷也不在**：`recorditem`（合并转发 19 的内层聊天记录，另一个 `refermsg`
  同族的载荷）**不是叶子值**——它是一整段**转义后嵌进来**的 XML 文本，实测中位 ~9.5 KB、
  最长 224,800 字符。同层的可读摘录（如 `<des>`）照常留在 `fields` 里；要看完整的子消息
  列表请读 `msg_parts.raw_xml`。
- **转账（type 2000）照实返回**：金额 `amount_desc`、备注 `memo`、双方用户名 `payer` /
  `receiver`、交易号 `transaction_id`，另加 `paysubtype`（中文语义无权威映射，原样给数字）、
  `transfer_id`、三个时间戳与 `is_red_envelope`。`payer` 为空是常态（实测 61%），不是异常。
  **这些内容会离开本机**（信封进 agent / LLM 上下文）——与 `search_history` 已在信封返回消息
  正文一致，不为转账单开例外，见 SECURITY.md §1「MCP 信封里的消息正文」。
- `ref_msg_key` 是引用（`kind=57`）的目标，**取的是回填那一刻的快照**：目标消息若后来才导出进
  索引，重跑 `appmsg rebuild`（幂等）即刷新。解不到时是 `null`（**不是空串**）——「解不到」与
  「没有引用」在渲染上同义，被引用的原文一直在 `quote.content` 里。
- `unparsed` / `rejected` **恒存在**，口径与 §12 `export_chat` 逐字同源（同一份解码）：
  前者是「解压失败（半拷贝的 WAL / 手工改过的索引库）或 XML 解不开」，后者是「命中危险载荷
  守卫被主动拒绝（DTD / 实体声明）」。两者**分开计**且都不进 `parts`——`parts` 里少一条时，
  看这两个数才知道是「解不开」还是「本来就没有可读内容」。
- 未回填时信封给**两条**、且**分工不复读**：`degraded` 给结构化记录
  （`capability: "msg_parts"`，`effect` 说清少了什么：「msg_parts 未回填：引用 / 链接 /
  文件 / 转账一律取不到内容」），`hint` 只给**可操作的那一句**「请先运行 CLI：
  `agent-wechat appmsg rebuild`」。空答案必须自证是哪一种空（与 §11 的「本地索引为空」
  同一约定）。
- 索引库打不开 / 读不了（损坏、半拷贝的 WAL、权限）→ `{"error": "本地检索库不可用：…",
  "hint": "索引库可能损坏；删除 index.db 后重跑 export_full_history 可重建"}`，**不是**一个空
  `parts`——空答案会把「库坏了」谎报成「没有可读内容」。
- **不联网、不写微信目录、不写任何产物**：只读本地检索库；`contact` 的解析要读一次
  `contact.db`（只读，同其它 contact 类工具）。


---

## 错误汇总

| 现象 | 原因 | 处置 |
|---|---|---|
| `{"error": "账号 … 无 passphrase 缓存，请先运行 bootstrap"}` | 该账号未 bootstrap | 按 INSTALL.md §5 引导 |
| `{"error": "账号目录不存在: …"}` / `{"error": "微信未运行或无法确定当前登录账号…"}` | `account` 传错或微信未登录 | 用 `get_account_status` 看已知账号，或先登录微信 |
| `{"error": "内部错误: …"}` | 未预期的第三方异常（fastembed/sqlite3 等） | 多为环境问题；查看服务端日志 |
| `current_account: null` | 微信未运行或启动中 | 打开微信并登录后重试 |
| `{"error": "找不到联系人…"}` | 名字未命中 | 用 `list_contacts` 查准确名 |
| `{"need_confirm": [...]}` | 名字命中多个联系人 | 让用户选择后用 username 重调 |
| `search_history` 无 hint 但 0 命中 | 该词从未出现 | 换词 / 放宽 contact 过滤 |
| `semantic_search` 0 命中且 `hint` 指向 `build_vector_index` | 向量索引未构建/为空 | 先 `export_full_history`，再 `build_vector_index` |
| `semantic_search` 报「与当前模型不一致」 | 索引由其它模型构建 | `build_vector_index(rebuild=true)` 重建 |
| `semantic_search` 命中却看着不相关 | 无语义相关性阈值，KNN 恒返回最近 k 条 | 按 `distance` 判断，必要时收窄 `contact` |

## 附：Server stdio 手动验证

不依赖 Agent 客户端，直接验证 MCP Server 可用（在仓库根目录执行）：

```python
# uv run python - < 验证脚本（初始化 → 列工具 → 调用 status）
import asyncio
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp import ClientSession

async def main():
    params = StdioServerParameters(
        command="uv", args=["run", "agent-wechat-mcp"])
    async with stdio_client(params) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            tools = await s.list_tools()
            print([t.name for t in tools.tools])
            r = await s.call_tool("get_account_status", {})
            print(r.content[0].text[:200])

asyncio.run(main())
```

期望输出包含 13 个工具名与 `get_account_status` 的 JSON。
