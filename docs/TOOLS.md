# TOOLS — MCP 工具参考

> 本文档逐个说明 agent-wechat MCP Server 暴露的 10 个工具：用途、参数、返回结构、
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
    { "msg_key": "wxid_testacct01|msg_md5hex|message_4.db|22415",
      "time": 1789045400, "sender": "我", "is_self": true,
      "type": "text", "content": "今晚一起吃饭吗" },
    { "msg_key": "wxid_testacct01|msg_md5hex|message_4.db|22416",
      "time": 1789045521, "sender": "wxid_abc123def456", "is_self": false,
      "type": "text", "content": "好啊，七点老地方？" },
    { "msg_key": "wxid_testacct01|msg_md5hex|message_4.db|22417",
      "time": 1789045600, "sender": "wxid_abc123def456", "is_self": false,
      "type": "image", "content": "[image]" }
  ],
  "degraded": []
}
```

> `msg_key` 是消息的稳定标识（`account|chat_md5|shard|local_id`，74 字符），
> 跨导出保持不变；后续 `get_message_media`（T7）靠它指认「这条消息的媒体是什么」。

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
    { "msg_key": "wxid_testacct01|msg_md5hex|message_4.db|22415",
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
  与_hits 里既有的 `id`（`messages.id`，DB rowid）不是一回事——rowid 随重建变化，
  `msg_key` 是内容派生的，可用于 `get_message_media`（T7）指认消息。
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
  "hits": [ { "msg_key": "wxid_testacct01|msg_md5hex|message_4.db|22415",
              "chat_id": "wxid_xxx", "display": "张三", "ts": 1757462400,
              "sender": "张三", "is_self": false,
              "content": "周末爬山的安排定了吗", "media_hint": null,
              "distance": 0.1842 } ] }
```

- `distance` 为 cosine 距离，越小越相近。
- `msg_key` 是消息的稳定标识（同 `search_history`），可用于 `get_message_media`（T7）。
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
| `limit` | int | 否 | 500 | 导出文件数上限（1-2000，超出按边界处理） |
| `account` | string | 否 | null | 同通用约定 |

**成功返回**：

```json
{
  "chat_id": "wxid_abc123def456",
  "out_dir": "/Users/you/.agent-wechat/media",
  "counts": {
    "image": { "ok": 12, "failed": 1, "missing": 0 },
    "voice": { "ok": 8, "failed": 0, "raw_only": 2 },
    "video": { "ok": 3, "thumb": 1, "missing": 0 }
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
  `video.missing` = 本体/缩略图在但读不了或写不出来。`image.missing` **当前恒为 0**——
  图片导出直接扫磁盘目录，不按消息逐条清点；该键为将来 `media_refs` 驱动的按消息导出预留。
- `kinds` 传空串（或全是逗号/空白）表示**不导任何类型**：不报错，成功返回且三类计数全为 0
  （只有 0700 权限等环境类 `degraded` 可能非空）。想导出全部请显式给 `image,voice,video`。
- `degraded` 键**恒存在**（与 §3 同一惯例：降级不是错误），但此处每项是**中文字符串**
  （图片密钥推导失败、pilk 缺失、wxgf 未转 JPEG、输出目录权限未收紧等），不是
  `{capability, effect}`。图片密钥问题不影响语音/视频导出。
- `truncated=true` 表示触及 `limit` 提前收手，结果不完整——可按 `months` 分批重跑补齐。
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
| `limit` | int | 否 | 500 | 动态条数上限（1-2000，超出按边界处理）。截断留下的是**最新**的那些动态 |
| `account` | string | 否 | null | 同通用约定 |

**成功返回**：

```json
{
  "out_dir": "/Users/you/.agent-wechat/moments",
  "counts": {
    "posts": 500, "comments": 312, "likes": 288, "failed": 0,
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
- `media_not_downloaded` 涵盖两种情形：「未开启 `with_media`」与「该媒体没有 `token`，
  构造不出下载地址」。
- 表情评论只渲染 `[表情评论]` 占位，本体不下载；分享链接自带的第三方缩略图不是
  朋友圈媒体，不抓取。
- 歧义走既有 `{"need_confirm": [...]}`、未命中走 `{"error": …}`、账号不存在走
  `{"error": "账号目录不存在: …"}`，与其它工具同形（见 §3）。

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

期望输出包含 10 个工具名与 `get_account_status` 的 JSON。
