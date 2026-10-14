# USAGE — 使用指南

> 本文档面向使用者，覆盖典型场景与工作流：读取近期记录、生成回复建议、全量导出、
> 检索历史（关键词与语义）。工具参数与返回结构的完整说明见 [TOOLS.md](TOOLS.md)。

## 0. 两种使用方式

| 方式 | 适合 | 入口 |
|---|---|---|
| **Agent 对话** | 日常「帮我回复 XXX」 | Claude Code / Codex 中的 MCP 工具 |
| **命令行** | 脚本、探索、排障 | `uv run agent-wechat <子命令>` |

两者共享同一核心逻辑，结果一致。以下命令均省略前缀
`uv run --directory ~/agent-wechat`；若已做全局安装
（[INSTALL.md](INSTALL.md) §2.1，`uv tool install --editable`），则任何目录直接裸敲
`agent-wechat <子命令>` 即可。

## 1. 场景一：为某联系人生成回复建议（最常用）

**Agent 方式**（已装 `wechat-reply` Skill 时）——直接在 Claude Code 里说：

```
帮我回复 XXX
```

Agent 会自动执行：`get_account_status` 确认账号 → `list_contacts("XXX")` 消歧 →
`get_recent_messages` 读近期 50 条 → 生成 2-3 条不同风格的候选回复。
若「XXX」匹配多个联系人，Agent 会列出让**你**确认，不会猜。

**CLI 方式**：

```bash
# 1) 把口述名字解析成 username
agent-wechat contacts "小李"
# wxid_abc123def456    direct    小李（产品）

# 2) 实时读取最近 30 条（时间正序，"我"表示本人发送）
agent-wechat recent wxid_abc123def456 -n 30
# [1789045400] 我: 今晚一起吃饭吗
# [1789045521] 小李: 好啊，七点老地方？
```

> `recent -n` 的范围是 **1-500**（与 MCP 的 `get_recent_messages` 同一口径，超出按
> 500 处理；非正值按 1 处理）。`-n 1000` 只会返回最多 500 条。

把输出交给任何 LLM 即可生成回复建议；Skill 工作流中这一步由 Agent 自行完成。

## 2. 场景二：历史检索

> 前提：已完成一次全量导出（见场景三），检索查的是本地索引库。

```bash
agent-wechat search "周末爬山"                  # 全库检索
agent-wechat search "合同" --contact wxid_abc123def456   # 限定会话
```

返回（`snippet` 中 `【】` 为命中高亮）：

```
mode=fts total=2
  [1789045400] wxid_abc123def456 小李: 周末爬山的那天记得带水
```

> 发送者一列显示人名（群昵称优先，其次备注、昵称）；`is_self=1` 表示本人发送。
> 名册来自索引库的 `display_names` 表——改完备注或群名片后**不需要重新导出**，
> 查询前会自动刷新（默认 900 秒一次）。
> `recent` 命令的输出中本人消息显示为 `我:`。

> 微信升级改了库结构时，受影响的能力会降级并在 stderr 给出一行说明（如「显示名暂不可用」）；
> 需要人工对照结构时跑 `agent-wechat inspect`。

> **已知限制**：名册有 900 秒 TTL。若某次刷新恰好撞上 `contact.db` 的结构漂移，那份
> **不完整**的名册会被连同新的时间戳一起写进索引库——**触发刷新那一次**仍会给出降级
> 说明，但此后 900 秒内的每次 `search` 都走早退分支、不再给（`degraded` 为空、stderr
> 也没有那一行），而显示名**确实**已经降级成 wxid。只要漂移还在，**每个 900 秒窗口都
> 如此重复**：窗口到点后再刷新，那一次又报一遍、之后又静默。`recent` 不受影响：它不走
> 索引库，每次调用都重新探测。

**分词规则**：≥3 个字符走 FTS5 trigram（精确子串，快）；1-2 个字符自动降级为
LIKE（`mode=like`）。1-2 字查询若不限定 `--contact` 会做全表扫描（秒级），建议尽量带上。

**Agent 方式**：「我和 XXX 聊过 XX 吗」→ Agent 调 `search_history(query, contact=...)`。

## 3. 场景三：全量导出（首次使用必做）

```bash
# 全部会话
agent-wechat export
# 完成后输出：完成：本轮 841584 行；索引总量 {'messages': 841584, 'chats': 772, ...}

# 仅导出某个会话
agent-wechat export --contact wxid_abc123def456

# 微信升级 / 索引疑似损坏后：忽略水位线全量重扫（幂等，不会产生重复行）
agent-wechat export --full
```

**特性**：

- **流式落库**：逐分片解密（用后即焚），内存占用恒定，与库大小无关
- **幂等**：主键去重，重复导出不产生重复行
- **断点续传**：中断后直接重跑 `export`，从水位线继续
- **增量**：再次运行只导出新消息（秒级）

**规模参考（本机实测）**：约 500MB 加密源库、84.1 万条消息 / 772 个会话，
全量约 1.5-2 分钟；微信在线时的增量更新秒级完成。

## 4. 场景四：语义检索（关键词记不全时）

> 前提：先做一次全量导出（见场景三），再建一次向量索引。索引读的是本地已导出的消息库，
> 不是实时微信数据——新消息要重新 `export`，再跑一次 `vector-build`（增量）才能检索到。

```bash
# 1) 构建本地向量索引（长任务，本机 CPU 推理；首次会下载约 95MB 模型文件）
agent-wechat vector-build
#   已嵌入 5120 行…
# 完成：本轮嵌入 812345 行，跳过 29239 行

# 2) 语义检索：按「意思」找，不要求原话一致
agent-wechat semantic "上次说的那个爬山的安排"
# mode=semantic total=3
#   [1789045400] wxid_abc123def456 小李: 周末爬山的安排定了吗  distance=0.1842

# 限定单个会话 / 调整返回条数（默认 10）
agent-wechat semantic "合同什么时候签" --contact 小李 -n 5
```

**与 `search` 的分工**：精确关键词、人名、专名用 `search`（FTS5 更快更准）；
**记不清原话**的模糊意图（「上次说的那个…」）用 `semantic`。两者索引互不影响，可同时使用。

**参数**：

| 参数 | 适用 | 说明 |
|---|---|---|
| `--account <wxid_xxx_hex4>` | 两者 | `vector-build` **省略时索引全部账号**——与其它子命令（省略即当前登录账号）相反；`semantic` 省略即当前登录账号 |
| `--rebuild` | `vector-build` | 先清空索引再全量重嵌：换模型后必须，也是重新生成上下文前缀的办法 |
| `--contact 名字` | `semantic` | 限定单个会话（username 或展示名；重名会提示，不会猜） |
| `-n N` | `semantic` | 返回条数，默认 10 |

**特性**：

- **断点续传**：中断 / 崩溃后直接重跑 `vector-build`（**不带 `--rebuild`**），从水位线继续
  （不重复嵌入）；`--rebuild` 会重置进度，中断后重跑要从头。**内容被改写（撤回/编辑）不会
  触发重嵌**——水位线只按消息 id 推进，需要时用 `--rebuild` 全量重建
- **进度**：每约 5120 行打印一次「已嵌入 N 行…」，结束打印本轮嵌入/跳过行数
- **本机闭环**：嵌入在本机 CPU 完成（fastembed + bge-small-zh-v1.5），聊天内容不外发

**已知限制**（细节见 TOOLS.md §7–§8）：

- 输出中的 `distance` 越小越相近，但**没有相关性阈值**——即使没有合适的结果也会返回
  最近的 N 条，需要自己看 `distance` 判断可用性
- **增量运行**（不带 `--rebuild`）不保留每个会话首条被嵌入消息的「上文」前缀；
  需要完整上下文时用 `--rebuild` 全量重建
- `--contact` 传展示名时**重名/未命中会报错**（不猜，也不会静默返回空）；无法确定时先用
  `contacts` 取 username。能解析但没有任何已入索引消息的会话返回 0 命中且无提示，
  与「确实没有命中」不可区分

**Agent 方式**：Agent 调 MCP `build_vector_index`（长任务，用 `get_task_status` 查进度）与
`semantic_search(query, contact=...)`——与 CLI 同源，参数与返回结构见 TOOLS.md §7–§8。

## 5. 场景五：LLM 蒸馏准备

导出后的数据在 `~/.agent-wechat/index.db`（SQLite），每条消息含
`chat_id / sender_id / is_self / ts / msg_type / content` 结构化字段，可直接 SQL
取用或转 JSONL：

```bash
uv run python - <<'EOF'
import sqlite3, json
con = sqlite3.connect("file:/Users/you/.agent-wechat/index.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute("""SELECT chat_id, sender_id, is_self, ts, content
                      FROM messages WHERE msg_type=1 ORDER BY chat_id, ts""")
with open("distill.jsonl", "w") as f:
    for r in rows:
        f.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
EOF
```

> ⚠️ 蒸馏产物是明文聊天档案：注意存放位置与清理时机（见 SECURITY.md §4）。
> 未来的按对话窗口切片/分块蒸馏格式为规划中功能（见 CHANGELOG）。

## 6. 场景六：朋友圈导出

把本账号可见的朋友圈（动态正文、作者、时间、评论、点赞）导出为**单文件 HTML 时间线**，
双击即可离线阅读。**默认零网络**——不加 `--with-media` 时不发任何网络请求。

```bash
# 默认：全量、离线（媒体默认标「远程，未下载」；kind=other 的标「外部链接媒体」）
agent-wechat moments export
# 已导出 1981 条动态（评论 1349 / 点赞 1197）
# 覆盖：2013-02 ~ 2026-09（微信本地缓存窗口，非完整历史）
# 媒体：已下载 0 / 未下载 5304 / 失败 0
# 产物：/Users/you/.agent-wechat/moments/index.html

# 限定月份（逗号分隔 YYYY-MM）；--chat 按**作者**筛选（名字或 wxid），不是「会话」
agent-wechat moments export --months 2026-01,2026-02 --chat 小李

# 联网下载媒体本体——本项目零网络红线上本次新增的显式例外
agent-wechat moments export --limit 5 --with-media
# 输出形状同上；媒体三桶（已下载 / 未下载 / 失败）的数值随当日缓存与服务端保留策略而变
```

> 上面两段注释里的是**示例输出**（取自 2026-09-22 本机实测）。数字随微信本地缓存的增长
> 而变，**不要拿它当验收基线**——要核对请以自己机器上同一次运行的前后一致性为准。

**参数**：

| 参数 | 说明 |
|---|---|
| `--chat <名字\|wxid>` | 按**作者**筛选；省略 = **不限作者**（含自己的动态）。消歧三态（命中 / 多命中 / 未命中）与其它子命令一致 |
| `--months 2026-01,…` | 逗号分隔的 `YYYY-MM`，按动态时间过滤；省略不限 |
| `--with-media` | **默认关**。开启才向微信 CDN 下载并解密媒体本体；**本机媒体总量约 1.5 GiB，其中本期新增约 0.62 GiB**（小视频 / 视频号 / 视频封面这批内容此前是 0，见下方「产物与代价」） |
| `--out DIR` | 输出目录（默认 `$AGENT_WECHAT_HOME/moments`）；目录 0700、文件 0600 |
| `--limit N` | 动态条数上限（下界 1，无上界；默认 2000）；触顶时 stderr 多一行「注意：触及 --limit，还有 N 条更早的动态没进来」。截断按**时间倒序**留最新的那些 |

**产物与代价**：

- 产物是 `index.html`（单文件自包含，内联 CSS）+ `media/<类型>/<月份>/<md5>.<ext>`。
  视频多一份**封面**，与本体**同目录**：`media/video/<月份>/<md5>_thumb.jpg`（本体下载成功
  且封面也拿到时才落盘；命名与聊天媒体那边的 `{file_key}_thumb.jpg` 同形，落盘点在
  `usecases._resolve_media`）。封面**同样受 CDN 白名单约束**：宿主不在白名单的封面
  （真机 158/181 在 `vweixinthumb.tc.qq.com`）**静默跳过，一个计数器都不进**——既不算
  `media_failed`，也不算 `media_not_downloaded`（各桶只数 `p.media` 的成员，而**视频封面**
  不是——它由本体那条媒体派生，不在 `p.media` 里）。**别把两种封面混为一谈**：
  **视频号封面**由 `_finder_media` 产成一条 `AUTH_READY` 媒体、**是 `p.media` 的成员**，
  照常计进各桶（下载成功算 `media_downloaded`，宿主不在白名单算 `media_not_downloaded`）。
- **耗时与体积（本机实测，2026-09-22）**：1,981 条**离线**导出约 11 秒、HTML 约 0.9 MB
  （含外链 / 视频号块，会随卡片数增长）。`--with-media` 是**逐条顺序**下载（不并发），耗时
  随媒体数线性增长——实测 22 张约 17 秒。**本期起下载量多出约 0.62 GiB**（小视频 622.8 +
  type=7 10.5 MiB，两者均为**卡片声明值**而非实际落盘量；视频封面可达仅 23 张、实测单张
  27.9 KB，合计 <0.01 GiB；此前这批是 0）。**本机媒体总量约 1.5 GiB**。
  **不做增量续跑**：没有 `already()` 跳过，每跑一次 `--with-media` 就要重下这么多。
- **默认产物不含任何图片**：朋友圈媒体只有远程 URL；本机缓存的朋友圈图与动态对不上
  （文件名四种哈希变体全部 0 命中），所以不参与渲染。想要图必须开 `--with-media`。
- **四类卡片**（本期起可读）：**外链**（正文里的链接，渲染成可点标题 + 主机名徽章；**只导出
  卡片自带元数据，不做预览抓取**，故仍是零网络——本机 178 条里混有 4 个垃圾推广域名，
  抓取会把本机 IP 暴露出去）、**视频号**（媒体在 `ContentObject/finderFeed` 下，解析昵称 /
  描述 / 时长并下载封面；**视频本体不下载**）、**小视频**（卡片 `type=15`，token 在 URL
  查询串里，下载视频本体）、**`type=7`**（卡片级 `ContentObject/type`，其媒体是 `type='2'`
  的无签名公开图，可直接下载）。
- **`media_failed` 是常态而非错误**：微信服务端会清理老媒体，本期 **620 条可下载媒体里
  实测 58 条未落盘**（约一成，且 58 是上界）——对象早被清理，换一条也一样。单条失败只计数，
  不中断整次导出。
  **它计的是失败次数而非媒体条数**（一条视频本体成功、封面失败时同时贡献
  `media_downloaded +1` 与 `media_failed +1`）；**白名单外的媒体不计失败**——它们落
  `media_not_downloaded`（「不去取」而非「取失败」）。
- **降级说明同类只写一条**（带条数，走 stderr），目前两个来源：「下载成功、但解密后不是
  已知图片格式」、「动态 XML 解析失败被跳过」。后者在页首另有一行「解析失败 N 条」——
  没有它，「0 条动态」与「这个账号没有朋友圈」在产物里分不出来。
- 回复（「A 回复 B」）里的 B 是**昵称**：微信的 XML 只带 B 的 wxid，昵称按 `username`
  在同一条动态的互动里反查；反查不到就只写「A：…」，不拿 wxid 顶替。
- **`window` 是微信本地缓存窗口，不是完整历史**：更早的动态不在本机。CLI 输出与产物
  页首都会如实标注。
- 生产环境里这一步**只读微信目录**：产物全部落在 `--out`；联网时只连微信 CDN——后缀
  `*.qpic.cn` / `*.video.qq.com`，加精确主机 `wxapp.tc.qq.com`（**不含子域**）；
  直连不走代理、只携带微信自己签发的 token，不含本机任何聊天 / 索引内容
  （见 SECURITY.md §1「零网络例外」）。

Agent 侧对应 MCP 工具 `export_moments`——参数与返回结构见 TOOLS.md §10。

## 7. 常用辅助命令

```bash
agent-wechat status                 # 登录态/账号/密钥缓存一览
agent-wechat status --verify-keys   # + 对每个库做 HMAC 校验（较慢）
agent-wechat contacts [词]          # 联系人模糊搜索（备注/昵称/微信号）
agent-wechat inspect session/session.db    # 解密并 dump 库结构（排障/适配用）
agent-wechat decrypt message/message_0.db --out m0.db   # 导出明文库（调试）
# ⚠️ --out 产物是持久明文库（~/.agent-wechat/m0.db），用后手动删除
```

### chat（会话时间线导出与索引页）

`media export` 搬的是媒体文件；`chat export` 产出的是**可读的会话正文**——appmsg（引用 /
链接 / 文件 / 转账）渲染成结构化块，媒体以相对路径引用**同级**的既有媒体树。

```bash
# 前提：先跑过一次 export（读的是本地索引库）；appmsg 的内容还需先 `appmsg rebuild`（见下）
agent-wechat chat export --chat 小李
# 会话 wxid_abc123def456   58 条   appmsg=12 image=3 system=2 text=41
# 产物：/Users/you/.agent-wechat/media/<会话md5>/chat/index.html
# 产物：/Users/you/.agent-wechat/media/<会话md5>/chat/index.md

# 限定窗口、调条数上限、只出一种格式
agent-wechat chat export --chat 小李 --months 2026-08,2026-09 --limit 200 --format md

# 列出已导出的会话，生成索引页（{--out}/index.html）
agent-wechat chat index
# 已导出的会话：3 个
# 产物：/Users/you/.agent-wechat/media/index.html
```

**参数**：

| 参数 | 说明 |
|---|---|
| `--chat` | 会话（username 或展示名；消歧同其它子命令）**必填** |
| `--months 2026-08,2026-09` | 必须是一段**升序且相邻月首尾相接**的窗口。非连续（`2026-08,2026-10`）只能取包络、会把 9 月静默带进来，反序会得到倒置（或退化成空区间）的窗口——两者都**报错**，不静默产出空产物；格式不符同理 |
| `--limit N` | 封顶窗口内**最近的** N 条，默认 500（触顶时 stderr 多一行提示，产物页首也印） |
| `--format html,md` | 逗号分隔，默认两种；未知格式或空串**报错**（「少写一个文件」与「格式名写错」不许长得一样） |
| `--out DIR` | 输出根，默认 `$AGENT_WECHAT_HOME/media`。**必须与 `media export` 用同一个**（见下） |
| `--account` | 同其它子命令 |

**约束一：`chat export` / `chat index` 的 `--out` 必须与 `media export` 用同一个根。**
产物在 `{--out}/{会话md5}/chat/` 下，媒体是以 `../image/…` 形式的**相对引用**指向**同级**的
`{--out}/{会话md5}/image|voice|video/`（也就是 `media export` 落的那棵树）。换个根，两个产物
照常生成、页面照常打开，**媒体块会退回 `exportable` / `gone` 占位**（不会出现打不开的
`<img>`，spec 裁定 13）——而 `gone` 的措辞读起来像「本机没有」，实际是「**这个根下**没有」。
两条命令必须同根是为了让媒体能**显示出来**，不是为了避免死链。索引页同理：
它用相对路径链到同一棵树下的会话产物，所以整棵产物目录可以整体拷到别处/别的机器。

**约束二：Markdown 与 HTML 不等价——三处固有损失（如实记，不假装一致；第 4 条已于
EA-037 代码收口，见下）：**

1. **语音 / 视频没有原生语法**：图片才有 `![]()`，音视频只能给链接（`[语音](…)`）；
2. **引用没有可点锚点**：HTML 侧是 `<a href="#…">` 可点跳，Markdown 降级成 blockquote，
   位置信息丢失（**被引用的原文照常出现**——它一直带在 appmsg 的 `refermsg` 里，渲染
   从不依赖回指成功）；
3. **字段表更朴素**：平铺成 `- 路径：值` 列表，没有 HTML 那份表格的列对齐；
4. ~~**Markdown 语法不转义**~~ **已收口（EA-037 + EA-034，2026-10-14）**：**所有用户内容**
   ——正文 / 昵称 / 转账字段 / appmsg 的标题、描述、引用正文、文件名、字段表 / 会话标题
   ——都做**硬转义**：行内结构字符（`` ` `` `*` `_` `[` `]` `(` `)` `!` `|` `~`）在任何
   位置转义；`#` `>` `-` `+` `*` `=` 与 `1.` 只在**行首**——「行首」按 CommonMark 的算法算：
   最多 3 个空格（或凑不满 4 列的制表符）之后仍算行首；缩进满 4 列则是缩进代码块，把行首那
   一个空白字符写成实体（`&#32;` / `&#9;`）来打断它（只有空白的行不动——它本来就不是代码块）。
   行结束只认 `\n` / `\r` / `\r\n` 并统一成 `\n`。分档的理由：一律转义会把 `¥100.00` 弄成
   `¥100\.00`（渲染无差异、源却脏）。**我们的措辞**（计数、时间、状态说明）不转义——它们
   由程序生成。
   appmsg 的链接另过**两道**守卫：URL 本身（`javascript:` 之类）与**目的地能不能原样写进
   `](…)`**（含空格 / 换行 / `)` 的 URL 会让目的地在用户内容处提前结束、其后的
   `![](http://…)` 就落回正文变成真的图片引用）。任一道不过就降级成可见的纯文本
   `[链接：…]`——URL 仍看得见，只是不可点。**第二道只在 `.md` 侧生效**（第一道两边共用）：
   `index.html` 没有 Markdown 语法，这类 URL 在那边照常可点。
   此前一条 `![](http://evil/x.png)` 的消息会在产物里变成**远程图片引用**（打开导出就会去
   打那个 URL），现在不是了。

**计数与降级**：

- 与其它命令一样，`--chat` 的会话消歧（以及标题里的人员名）要读 `contact.db`，因此**需要
  passphrase 缓存**（先 bootstrap）；缺缓存时整条命令失败并指向 bootstrap。
- CLI 印一行 `appmsg=12 image=3 …`（`kind=条数`）与每条**真正落盘**的产物路径——写失败的产物不进列表，
  所以「少了一行」本身就是信号。
- `--limit` 触顶（`truncated`）与 appmsg 的 `unparsed`（解不开）/ `rejected`（载荷含 DTD /
  实体声明，我们主动拒绝）**各印两处**：stderr 一次、产物顶部一次——产物会离开这次会话，
  只回终端的话读产物的人看不到。
- 三条降级说明走 stderr（`⚠️` 前缀），并同时印在产物顶部（`降级：…`）：`msg_parts` 未回填
  （appmsg 只显示 `[appmsg: 标题]`，跑 `appmsg rebuild`）、媒体状态覆盖不足（最多答 500 条，
  **没覆盖到的消息在产物里不会出现媒体块**）、显示名降级（标题与发送者回落成原始标识）。
- 索引页只列**磁盘上真有产物**的会话；索引里查不到的会话（导出之后重建过索引 / `purge --index`）
  印「条数未知（索引里没有这个会话）」——**不是「0 条」**；一个产物都没有时不写空壳页面，
  只印一句「没有已导出的会话；先运行 `chat export`」、退出码 0。

Agent 侧对应 MCP 工具 `export_chat`（参数与返回结构见 TOOLS.md §12）。

### appmsg（回填 appmsg 原始载荷）

`chat export` 里的 appmsg 结构化内容、以及 MCP 的 `get_message_parts`，读的都是本地索引库的
`msg_parts` 表（见 DATA_MODEL.md §3）。**这张表不会随 `export` 自动生成**——它由这条命令显式
回填（存量历史如此，将来也是），重跑幂等：

```bash
agent-wechat appmsg rebuild
#   已回填 20000 条 appmsg…
# 完成：本轮写入 msg_parts 66663 条；该账号回指解析 53625 / 未解析 38；拒绝 0；解不开 0
```

- **要解密全部分片**（引用回指需要一张账号级的 `server_id → msg_key` 表，而被引用的原文大多
  是普通文本消息），所以与其它读微信库的命令一样**需要 passphrase 缓存**（先 bootstrap）。
- **只写 `msg_parts`**：不碰 `messages` / FTS / 向量表 / 水位线（本机回填前后逐表指纹相同）。
- **耗时与体积（本机实测）**：37.5–38.7 s、`index.db` **+232.8 MB**。
- **覆盖范围**：它扫的是**微信库**，覆盖「回填那一刻微信库里已有的 appmsg」（载荷为 NULL 的
  除外）；`export` **不写**这张表，所以**回填之后新到微信**的 appmsg 要重跑一次才有内容。
- **回指是快照**：`ref_msg_key` 取的是回填那一刻的答案。被引用的消息若**后来才**导出进索引，
  重跑一次 `appmsg rebuild` 即刷新（幂等）；它只影响「能否跳到原消息」，**被引用的原文不受
  影响**。
- 那一条输出里的两组计数**分母不同**，别并排当同一个读：`本轮写入` 是本轮扫描落库的条数；
  `回指解析 / 未解析` 是跑完后**该账号整张 `msg_parts` 表**里回指解出 / 没解出的条数。
- MCP 侧**没有**对应工具——回填是显式的一次性运维动作；未回填时 `export_chat` /
  `get_message_parts` 的信封会带 `degraded` 与 `hint` 指出这件事（TOOLS.md §12 / §13）。

### media（导出图片 / 语音 / 视频）

```bash
# 导出某会话的媒体（默认三类全导、不限月份；--limit 默认 2000，数的是本次**新增**的文件）
agent-wechat media export --chat 小李
# 会话 wxid_abc123def456
# 图片   ok=12  skipped=0  failed=1  missing=0
# 语音   ok=8  skipped=0  failed=0  raw_only=2
# 视频   ok=3  skipped=0  thumb=1  missing=0
# 输出目录: /Users/you/.agent-wechat/media

# 只导图片、限定月份、限制数量
agent-wechat media export --chat filehelper --kinds image --months 2026-09 --limit 50

# 导到别处（--out 是持久明文目录，用完自行清理；微信目录始终只读）
agent-wechat media export --chat 小李 --out /tmp/media-out

# 存量历史回填「消息 → 媒体文件」映射（全量重扫该账号分片，幂等；本机实测 22.6 秒）
agent-wechat media rebuild-refs
#   已回填 21880 条媒体引用…
# 完成：media_refs 共 43969 条
```

**产物与计数**：

- 产物落 `~/.agent-wechat/media/<会话md5>/<类型>/<月份>/`（目录 0700、文件 0600；
  `--out` 可改根目录）。CLI 只打印计数与「输出目录」，明细自行 `ls`
- **能导出的远少于会话里的媒体消息数**：微信按需下载并定期清理本地媒体，`missing` 大
  是常态而不是错误（本机实测：84.1 万条索引里 39,344 条图片消息，磁盘上只剩 198 个 dat）
- 计数含义：`failed` = 解密/未知格式/落盘失败；`raw_only` = pilk 缺失时导出的原始 `.slk` 数；
  `thumb` = 视频本体不在、回退缩略图的数量；`skipped` = 本次因产物**已存在**、或**本轮内
  重复**（同一个贴纸 md5 只取一次件）而跳过的**文件**数（**按文件**计，不是消息条数）；`remaining` = 撞上 `--limit` 时还有多少个**未导出**
  （CLI 印的是三类之和）。`image.missing` 恒为 0（图片按目录枚举，不按消息清点）
- **`--limit` 只数本次新增**（已存在的产物不计），而且**只封顶写入、不封顶扫描**：撞顶后仍会
  扫完所有剩余条目（图片与视频是逐个 `stat` 产物路径；语音还要把每条 `voice_data` 从库里读出来）
  才返回——大语料上表现为「还在跑」，不是卡住。触及 `--limit` 时会多印
  一行「（达到 --limit 上限，还有 N 个未导出 —— 重跑同一条命令即可继续）」。这个 `N` 含
  续跑时**仍会失败**的那些条目（dat 读不出、本体不存在），故它不等于「续跑一定多出 N 个文件」。
- 重跑同一条命令会跳过已存在的产物、从断点向前推进；要把已存在的产物全部重做用 `--force`
  ——它**不**补缩略图：本体已在产物里的视频不会再生成 `{K}_thumb.jpg`。`--force` 也是**被中断的
  导出**所留残件的唯一出路：落盘失败时半成品会被清掉，但 SIGKILL / Ctrl-C 不抛异常、没有清理
  的机会。
- **依赖的边界要分清**：媒体密钥推导本身不需要特权（图片密钥是纯磁盘推导的）；但与其他
  命令一样，`media export` 需要 passphrase 缓存——会话消歧要读 `contact.db`。缺缓存时
  命令**整体失败**（报错指向 bootstrap），不存在「只退图片、语音照常」的部分可用形态。
  缓存齐备后各能力才独立降级：语音要 pilk（缺失则出原始 `.slk`）、wxgf 图要 ffmpeg
  （缺失则存 `.wxgf` 原容器）。缺哪项只退哪项，降级说明走 stderr
- **贴纸要两个开关**：`sticker` 是合法值，但**默认不导**。要
  `--kinds image,voice,video,sticker --with-cdn`（两个都显式）才从腾讯 CDN 取件。
  **本机那份取不出来**——微信把它存成加密格式、解密方案未破（两轮 spike），
  而且实测是**缩略档**（同一 md5：16,016B vs CDN 原图 67,167B），所以即使破了也不等价。
  CDN 那条路**实测可用**（返回明文图片；2023 年的老贴纸凭据也仍然有效），
  代价是该请求**走明文 HTTP、携带 `filekey`**（见 SECURITY.md §1「零网络例外」第 3 条）。
  不给 `--with-cdn` 时**一次网络请求都不发**，贴纸全部计入 `sticker.missing`。
  贴纸的**可读标识**与开关无关，照常有（见 §9 的正文约定）

**`--chat` 消歧**（与 `search` / `recent` 同一套）：命中多个时候选 JSON 打到 stdout、
一行说明打到 stderr、退出码 1——**不会猜**，用 username 重试即可：

```bash
agent-wechat media export --chat 小李
# stdout: [{"username": "wxid_abc123def456", "display": "小李（产品）", "kind": "direct"}, …]
# stderr: 无法唯一解析「小李」；候选见 stdout 列表，请用 username 重试   （退出码 1）
```

`media rebuild-refs` 只回填 `media_refs` 索引（不重写 messages），是给**本次功能之前**
已导出的存量历史补映射用的；新导出会与消息同事务顺带写入，无需手动跑。它要解密全部分片，
因此同样需要 passphrase 缓存（先 bootstrap）。Agent 侧对应 MCP 工具 `export_chat_media`
（见 TOOLS.md §9；rebuild-refs 仅 CLI）。

### media locate（按消息列出媒体归属）

`media export` 是「按会话批量导出」；`media locate` 是反向的按需查询——**某条消息的媒体
在不在本机、在哪**。

**前提**：先跑过一次 `export`（见场景三）——它读的是本地检索库（`messages` + `media_refs`）。
索引里一条消息都没有时，答案是一份零（`汇总：exported=0 exportable=0 gone=0 none=0`），
MCP 侧的信封另带 `hint` 说明「本地索引为空」——别把它当成「这个会话没有媒体」。

它只做路径探测与索引只读查询：**不写微信目录、不写 `--out`、不联网**。唯一的例外是
**语音本体的判定**——要解密 `media_*.db`，明文只在 `~/.agent-wechat/tmp/`（0700）短暂
存在、用后即焚（见 SECURITY.md §4）。

```bash
agent-wechat media locate --chat 小李
# 会话 wxid_abc123def456    产物根 /Users/you/.agent-wechat/media
#   [exported  ] image 1789046525  /Users/you/.agent-wechat/media/<会话md5>/image/2026-09/<md5>.jpg
#   [gone      ] image 1789045401
#   [gone      ] sticker 1789045900
#   [exportable] voice 1789046102
# 汇总：exported=1 exportable=19 gone=34 none=0
#   按类型 image exported=1 exportable=0 gone=22
#   按类型 video exported=0 exportable=0 gone=8
#   按类型 voice exported=0 exportable=19 gone=0
#   按类型 sticker exported=0 exportable=0 gone=4
# 提示：gone 表示消息引用了媒体但本机找不到——微信只为近期（尚未导出）的消息保留本体，所以导出更多历史比「修复」有用。
# 提示：贴纸的 gone 是「取不出来」而不是「本机找不到」——微信把它存成加密格式，解密方案未破，导出更多历史也拿不出来。

# 只看指定几条：--keys 是 msg_key 的逗号分隔列表
# （msg_key 只在 MCP 工具的返回信封里给出，见下）
agent-wechat media locate --chat 小李 --keys 'wxid_abc123def456|9f2c1a…|message_0.db|81234'

# 限定月份，或调高条数上限（1-500，默认 200）
agent-wechat media locate --chat 小李 --months 2026-08,2026-09 --limit 500
```

> **`--keys` 的 msg_key 从哪来**：只来自 MCP 工具的返回信封——`get_recent_messages` /
> `search_history` / `semantic_search` 每条消息都带 `msg_key`。**CLI 自己的 `recent` /
> `search` / `semantic` 不打印它**，所以从命令行拿不到 key；只用 `--chat` 时不需要 key
> （它答该会话全部媒体类型的消息）。

**四态**（判定顺序严格按此序）：

| 状态 | 含义 |
|---|---|
| `none` | 不属于本功能覆盖的媒体类型：文件、位置、appmsg 等都归这里（按消息类型判，不依赖引用表）。**它不是「没有媒体」**。**贴纸不在此列**——它是有类型的媒体消息，见下。**只在显式给 `--keys` 时才可能非零**：不给 `--keys` 时 SQL 只取四类媒体消息，该计数恒为 0（上面示例里的 `none=0` 就是这么来的） |
| `exported` | 产物已在产物根下，行尾给出**绝对路径** |
| `exportable` | 有媒体、本体还在微信里，但还没导出到产物根。**贴纸永不产出这一态**（它没有产物可拿） |
| `gone` | 有媒体，但本机找不到（未导出，或微信已清理本体）。**贴纸的 `gone` 是另一回事：它说的是「取不出来」**——微信把贴纸存成加密格式、解密方案未破，**与本机有没有那份文件无关**，所以下面「导出更多历史比修复有用」那句对贴纸**不成立**，CLI 会另印一行说明 |

- **`gone` 是常态，不是错误**：微信只为**近期**消息保留媒体本体，而近期消息往往还没导出
  进本索引，能对上的极少。实测（2026-09-16）对索引内已有的消息：图片本体兑现率约
  **0.005%**、视频 **0**；语音相反——本体存在 `media_*.db` 里、微信不清理，**7,390 条
  100% 有**。所以 **`exportable` 在本机实质上是语音专属状态**，图片与视频几乎一律 `gone`。
  **贴纸不在这句话的范围里**：它全归 `gone`，但那不是因为微信清理过什么。
- **`exportable` 不保证视频本体可用**：视频只定位到缩略图时也算 `exportable`——本机只有
  一张 jpg，`mp4` 本体不在。逐条明细**不区分这一点**（缩略图标志不外露）。
- **「按类型」那几行要连着「汇总」一起看**：各类的本机可得率相差几个数量级，池化的
  「汇总」会把「语音几乎全可导出」与「图片视频与贴纸几乎全 `gone`」加成一个数字，读起来
  像「这个会话的媒体大多没了」；按 `image`/`video`/`voice`/`sticker` 拆开才是实情。
  `none` 态没有类型，只进「汇总」；某个类型一条都没有时**那一行根本不会印**
  （所以行数随会话而变，最多四行）。
- `--out` 要与导出时用的那个一致（默认 `~/.agent-wechat/media`），否则已导出的产物会被
  判成 `exportable` 而不是 `exported`。
- `--months` 在 SQL 里筛，所以 `--limit` 卡的是**命中月份**的条数；触顶时多印一行
  「（达到 --limit 上限，已提前收手；以上计数只是答完的这些）」——此时明细与计数都不是
  全量，可按 `--months` 分批重跑。
- 与其它 `media` 子命令一样需要 passphrase 缓存（会话消歧要读 `contact.db`）；`--chat`
  消歧同形（多命中时候选 JSON 打到 stdout、一行说明打到 stderr、退出码 1）。降级说明
  （如语音库读不到，此时**尚未导出**的语音一律按 `gone` 呈现）走 stderr。

Agent 侧对应 MCP 工具 `get_message_media`——同一个用例、同一套四态与 `by_kind`，
信封见 TOOLS.md §11。

### purge（销毁本地产物）

```bash
agent-wechat purge --index            # 预览：将删除哪些路径、体积、重建代价
agent-wechat purge --index --yes      # 执行
agent-wechat purge --keys --tmp --yes # 具名范围可任意组合
agent-wechat purge --media --yes      # 只删导出物（见下：它通常是这里最大的一项）
agent-wechat purge --all --yes        # 整个 ~/.agent-wechat（含 models/、tasks.json、散落文件）
```

五个具名范围：`--keys`（密钥缓存）、`--index`（检索库）、**`--media`（媒体与聊天导出）**、
**`--moments`（朋友圈导出）**、`--tmp`（解密临时目录）。

**默认 dry-run**，不加 `--yes` 一个字节都不删。`--all` 与具名范围互斥。

**表里那两列是判断依据，不是装饰**：`敏感` 与**重建代价**。`--media` 那一行通常最大——
它是解密后的图片/视频/语音，外加**在 Finder 里双击就能打开的**会话 HTML/Markdown；而
`--index` 要经本工具才读得懂。两者都标「敏感」，但重建代价差一个量级（前者要联网重抓 +
重解微信本体，后者 ≈2.5–3 小时）。

没有拥有者的条目（例如手工落在数据目录里的脚本）敏感度列写 **`未知`**——它的含义是
「没人认识它」，不是「已检查过、不要紧」，两者正相反。

**拒绝执行**的四种情形（**只有第一条是具名范围那条路独有的**：**`purge --chat` 不跑
`lsof`**——导出目录被别的进程持有时，它删得掉而空间要等那个进程退出才释放。**第二条两条路
共用**，判据、扫的范围与拒绝的时机都相同）：
- 有进程正在使用待删产物（否则 unlink 会「成功但空间不释放」）；
- 待删路径是符号链接。**文件型产物（`--keys`/`--index` 的目标）本身是链接时无条件拒绝**
  ——unlink 只删链接、目标必留存，核查会报「已清空」；**目录型产物（`--media`/`--moments`/
  `--tmp` 的目标）、`--all`，以及 `purge --chat` 的 `media/<会话md5>/` 走 rmtree，还会扫各自
  那棵树，只拒绝解析目标落于 `~/.agent-wechat` 之外（或判不出来）的链接**，树内的随整树一并
  删除；
  （huggingface 的模型缓存里 `snapshots/*` 全是树内链接，故 `models/` 照常可删；判据是
  「目标在删完之后还在不在」，不是「见到链接就拒」。）
  **两种路径都有一处不提前拒绝的形态**：**目录本身**是符号链接、而它的目标解析在
  `~/.agent-wechat` 之内时（树内目标会随 rmtree 一并删掉，没有 false-clean），两条路都走到
  `rmtree` 才失败——具名范围报「未能删除」进 `report.failed`，`--chat` 则在删除与墓碑
  **提交之后**报「导出物未能删除（那个名字还在磁盘上）」并退 1。**别按「拒绝 vs 不拒绝」
  读**这一条。
- **请求了不存在的范围**（例如开关名与实现漂移）——响亮的拒绝，绝不静默空操作：一门用来
  销毁数据的命令报「已删除 0 项」并退 0 是最坏的一种假象；
- `AGENT_WECHAT_HOME` 指向不像数据目录的位置（空值会被解析成当前目录，或指向 `/`、`~` 及其祖先）
  ——dry-run 与 `--yes` 两处都拦，绝不在这种路径上执行销毁。

#### 按会话销毁（`purge --chat`）

销毁的**第二个轴**，与具名范围/`--all` **互斥**（后者问「删哪一类文件」，它问「删哪个会话」；
设计见 `内部设计记录` §3）：

```bash
agent-wechat purge --chat 小李             # 预览：将删哪些行、哪份导出物
agent-wechat purge --chat 小李 --yes       # 执行：删 + 写入永久排除
agent-wechat purge --chat 小李 --undo      # 预览：从排除清单里移出（**不恢复数据**）
agent-wechat purge --chat 小李 --undo --yes
agent-wechat purge --excluded              # 列出排除清单的规模（按会话 md5 分组）
```

- 删的是**本工具的副本**——该会话在 `messages` / 向量 / `msg_parts` / `media_refs` /
  `display_names` 里的行，以及 `media/<会话md5>/` 整份导出物；**不是微信里的记录**
  （微信容器恒只读）。
- **朋友圈不在范围内**（spec §3.1 / §9）：`moments/` 是一份 HTML 装所有人，不按会话分目录，
  那个人的动态与互动**仍可能留在其中**——计划表会把这句原样印出来。
- **只清当时已有的**（spec §4.1②）：墓碑逐条记在那批 `msg_key` 上，删除之后新来的消息
  照常进索引。
- **执行期间正在跑的导出任务会因写锁而失败**（spec §5.3）：本命令在读任何东西**之前**就
  `BEGIN IMMEDIATE` 取写锁（SQLite 同时只允许一个写者），于是后台那一次 `export` 的下一批
  写入会等满 Python `sqlite3` 的默认 5 秒，然后抛 `database is locked`。**另一侧拦不住**
  ——purge 是独立进程，`tasks` 注册表一读就把磁盘上的 running 误判成 failed，没有可靠的
  提前探测手段——所以这句写在计划表里，让你先知道再决定；等导出跑完再执行即可。反过来，
  **purge 自己拿不到写锁时**会以 `PurgeRefused` 退 1，不许接住异常继续删（spec §6）。
- **会话一条消息都没有时照样走完整流程**（spec §5.7）：计划表照印（各项 0 条、不写墓碑），
  但**导出物那一行是真的**——`media/<会话md5>/` 里解密出的图/语音/HTML 与索引里有没有行
  无关（典型是 `purge --index` 之后索引被逐会话重建、这个会话还没被重新导出）。`--yes`
  会把它删掉。
- 计划表第一行印**解析结果的真身**（`将删除（会话：小李 → wxid_testacct01_ab12）：`）——
  删除不可逆，必须让你当场看见删的是谁；跨账号时逐账号列出。
- **保证的边界要说准**：这条命令保证的是「**索引里没有存活行 + 它们的 `msg_key` 进了排除
  清单**」，**不是「字节层面不存在」**。`secure_delete=ON` 只清 SQLite 自己释放的页，而 vec0
  的 `preview` 列装的是相邻消息的正文——chunk 没整块清空时它仍可能留在**活跃页**里：把
  `index.db` 连同 `-wal` 读成字节去 grep，实测 1200 行形态下开了 pragma 也还能命中 1–3 次
  （**不是**本工具的检索还能搜到它——索引查询里它已经没有了）。要字节级清除只能 `purge --index`
  后重跑 `export`。

**`exclusions.db`（排除清单）是唯一不可重建的产物**（spec §4.1③）：索引可重建（≈2.5–3 小时）、
密钥可重新 bootstrap、`media/` 可联网重抓，而它不能——它没了，被删的内容下次 `export --full`
就全回来。所以：

- `purge --index` **不会**动它——重建索引后排除仍生效，CLI 会报「排除清单保留：N 条」；
- `purge --all` 会**连它一起清掉**，CLI 当场报「排除清单已随之删除：N 条排除失效，被删内容
  下次导出会回来」；
- `--undo` **只删墓碑、不恢复数据**：被删的内容不会因为 `--undo` 回来，只有**将来**的新消息
  能重新入库；要找回那批得重跑 `export --full`（那时它们不再被排除）。它在索引已被
  `purge --index` 删掉时**照样能用**——走排除清单那一支，不会把刚删掉的索引重建回来。

**dry-run 不改索引内容，但别读成「磁盘零变化」**：要读索引的命令（`search` / `export` 等，
以及**索引库存在时**的 `purge --chat` 与 `--undo`）都会经 `store.connect()` 把
`exclusions.db` ATTACH 进来——本机还没有它的，会因此**建出一个空文件**。（`recent` 不在此列：
它不读索引库，也就不会建出这个文件。）只有 `purge --excluded`（以及索引库不在时的 `purge --chat` /
`--undo`）走独立连接（`exclusions.standalone()`，不经 `store.connect()`），**不会建出 `index.db`
或 `exclusions.db`**——注意它**不是只读连接**：`--undo --yes` 正是经它把墓碑删掉的。但后两条
仍要解析会话名（`reader` 要解密一份 `contact.db` 快照），明文会在 `tmp/` 下短暂出现、用后即焚——
「不建索引与清单」不等于「磁盘零活动」。

## 8. 账号切换

- 读取/导出/`semantic` **默认操作当前登录账号**；`--account <wxid_xxx_hex4>` 可显式指定
- **例外**：`vector-build` 省略 `--account` 表示索引**全部账号**（见场景四）
- 微信内切换登录后，重新运行 `status` 即可看到 `当前登录` 变化，无需其他配置
- 每个账号首次使用前需**在该账号登录状态下**各跑一次 `bootstrap`
  （passphrase 按账号独立缓存，详见 FAQ）

## 9. 输出约定与错误提示

- 所有失败都会给出中文原因与下一步指引（如密钥缺失时会提示 bootstrap 命令）
- 名字消歧在 `recent` / `search` / `export` / `semantic` 四个子命令上行为一致：
  候选列表（JSON，含 `kind`；与 MCP 的 `need_confirm` 同形）打到 **stdout**，
  一行「无法唯一解析…」打到 **stderr**，退出码 **1**——歧义不会被当成「找不到」，
  也不会替你猜。用候选里的 `username` 重试即可
- `contact` 传展示名且未命中任何联系人时，报错并提示用 `contacts`（MCP 侧为
  `list_contacts`）查询，不会静默扩大到全部会话
- 非文本消息以 `[image]` / `[appmsg: 标题]` / `[voice]` 等提示代替正文，
  XML 内容不会进入索引。**贴纸例外**：它的正文是可读标识 `[贴纸:名称]` /
  `[贴纸#md5前8位]`（源 XML 里没有合法 md5 时回落 `[sticker]`），而 `media_hint`
  仍是 `[sticker]`
- 时间一律为 unix 秒；`is_self=1` 表示本人发送
