# FAQ — 常见问题

> 面向使用者的快速答疑。深入说明见 [SECURITY.md](SECURITY.md)（安全与合规）、
> [TROUBLESHOOTING.md](TROUBLESHOOTING.md)（故障）、[ARCHITECTURE.md](ARCHITECTURE.md)（原理）。

**Q1：这安全吗？会不会封号？**
工具只读本机数据：lldb 仅在密钥派生瞬间读一次寄存器即脱离，不修改微信行为、不接触
网络协议。公开同类工具经大量真机使用未报告触发风控，但**无官方保证**，且该行为可能
违反微信用户协议。请自行评估（详见 SECURITY.md §2、§5）。

**Q2：支持 Windows / Linux 吗？**
不支持。三个平台的微信数据目录、加密实现均不同；本项目按 macOS 微信 4.1.x 实测实现。

**Q3：支持微信 3.x 或 4.0 吗？**
不支持。4.1+ 与 4.0.x 的密钥内存形态不同（passphrase vs raw key），本项目只实现了
4.1+ 路线。其他 4.1.x 小版本理论同族，**需自行验证**（见 INSTALL.md §4）。

**Q4：多账号如何处理？**
- 数据目录天然按 `wxid` 隔离，互不干扰
- 每个账号的密钥独立：**登录到哪个账号，就在该账号登录状态下跑一次 bootstrap**，
  之后该账号的读取/导出即可用（即使它当前不在登录状态，只要密钥缓存有效）
- 读取/导出默认操作当前登录账号；`--account wxid_xxx_hex4` 显式指定
- `get_account_status` 列出全部账号名与当前（或指定）账号的 `key_ready`；
  逐账号的密钥校验状态用 CLI `agent-wechat status --verify-keys` 查看

**Q5：导出规模有上限吗？内存会爆吗？**
导出是流式批处理，内存占用恒定（与库大小无关）。本机实测 84.1 万条消息 / 772 个
会话 / 约 500MB 加密源库，全量约 1.5-2 分钟。SQLite 单文件在该量级下检索流畅；
千万级以上建议分库（见 ARCHITECTURE.md §6）。

**Q6：会导出图片 / 语音 / 视频吗？**
**本地检索库（`index.db`）里没有媒体本体**：非文本消息以 `[image]`、`[appmsg: 标题]`
等提示占位，保留"此处有一条图片"的事实与类型/时间元数据。
媒体**本体**是按需另行导出的：CLI `agent-wechat media export --chat 小李`
（MCP 对应 `export_chat_media`）把某会话的图片 / 语音 / 视频落到 `~/.agent-wechat/media/`。
能导出的通常远少于会话里的媒体消息数——微信按需下载并定期清理。计数口径见 TOOLS.md §9：
`video.missing` 是常态，而 `image.missing` **恒为 0**（图片按目录枚举，不按消息逐条清点）。
**朋友圈**（`agent-wechat moments export`）的媒体全在远程，默认不下载；要拿到本体需显式加
`--with-media`（MCP 对应 `with_media=True`），这会联网——见 Q7。

**Q7：数据会上传云端吗？**
**默认全程零网络，没有任何数据会被上传**：解密、索引、embedding（本地 ONNX 推理）、
MCP（stdio）均无网络路径，MCP 只在你的 Agent 客户端与本地 Server 之间通信。

只有两个都需要你**主动触发**的例外：

1. 首次构建向量索引时下载约 95MB 的模型文件（fastembed + `bge-small-zh-v1.5`）到
   `~/.agent-wechat/models`；模型就位后嵌入与检索完全离线（见 Q15）。
2. **朋友圈媒体下载**：只有显式指定 `--with-media`（CLI）/ `with_media=True`
   （MCP `export_moments`）时才发起请求，且**只连微信 CDN 两个域**（`*.qpic.cn`、
   `*.video.qq.com`）；不指定则一个网络请求都不发。请求只携带微信自己签发的媒体
   `token` 与固定 `User-Agent`，**不含本机任何聊天 / 索引内容**；**不使用代理**，一律直连。

第 2 条是本项目零网络红线上**本次新增的**显式例外，详见 SECURITY.md §1「零网络例外」。

**Q8：为什么需要 sudo 和重签名？能避免吗？**
lldb attach 需要 `task_for_pid` 权限；Hardened Runtime 会阻止它，所以要 ad-hoc
重签名微信。两者都只在一次性 bootstrap 中出现，之后一切操作零特权。关闭 SIP 可
绕过重签名，但影响整机安全面，不建议（见 SECURITY.md §2）。

**Q9：微信升级后失效了怎么办？**
升级会还原微信签名并可能改变库结构。流程：重签名 → 重新 bootstrap → 跑
`export --full`。若仍异常，按 TROUBLESHOOTING §4 做版本适配排查。

**Q10：修改微信密码后有影响吗？**
有。passphrase 会随密码变更派生路径变化，表现为 HMAC 校验失败/解密失败——
重新 bootstrap 一次即可。

**Q11：读取和导出会让微信变慢或崩溃吗？**
不会修改微信状态。快照读取期间微信正常可用；最坏情况是导出瞬间占用几秒磁盘带宽。

**Q12：如何彻底删除本工具产生的所有数据？**
`agent-wechat purge --all --yes`（`~/.agent-wechat` 目录包含密钥缓存、索引库与临时文件）；
不加 `--yes` 只预览。核查报告会告诉你还有什么没清干净——如 `$TMPDIR` 下 bootstrap 崩溃残留的
`awcap_*` 脚本，**这些只报告、不代删，需手工处理**。
该目录已排除出 Time Machine 备份。

**Q13：为什么有的会话显示为 `md5:xxxx…` 而不是名字？**
微信按 `md5(会话id)` 建消息表；当该会话不在你的联系人/会话列表（如已删除的会话）
时无法反查名字，以 `md5:` 前缀展示——**消息数据完整保留**，不影响检索。

**Q14：能给别人用，或者把代码发出去吗？**
不能。本项目的合规边界是**个人自用、不分发**（同类项目因公开分发被微信官方函件后
删库，见 SECURITY.md §5）。请勿 push 到公开仓库或打包传播。

**Q15：向量语义检索（「意思相近」的搜索）怎么用？**
已实现，全程本机：本地 ONNX 推理（fastembed + `bge-small-zh-v1.5`），**不调用任何外部服务**。
索引先建后查——CLI 用 `agent-wechat vector-build` 构建、`agent-wechat semantic "上次说的那个爬山的安排"`
检索（MCP 对应 `build_vector_index` / `semantic_search`）；首次构建会下载约 95MB 模型文件到
`~/.agent-wechat/models`，之后完全离线。精确关键词/人名仍建议用 `search`（FTS5 更快更准），
`semantic` 用于记不清原话时。用法与前提见 USAGE.md 场景四；**已知限制**（增量运行不保留
每个会话首条消息的「上文」前缀、没有相关性阈值、没有任何已入索引消息的会话返回 0 命中且无提示）
见 TOOLS.md §7–§8 与 ARCHITECTURE.md §7。

**Q16：为什么查到的图片、视频大多是 `gone`？**
微信只为**近期（尚未导出）**的消息保留媒体本体，所以在**索引内已有的消息**上兑现率极低——
实测（2026-09-16）：图片本体兑现率约 **0.005%**（2 / 39,307），视频 **0**（/ 4,404）。
**`gone` 是如实回答，不是故障**，更不等于「微信已经把老媒体清理干净了」：导出更多历史会把
那些消息带进索引，它们仍在的本体会一并变得可见。语音相反——本体存在 `media_*.db` 里、微信
不清理，**7,390 条 100% 有本体**，所以 **`exportable` 在本机实质上是语音专属状态**。
查法：CLI `agent-wechat media locate --chat 小李` / MCP `get_message_media`；四态与计数
口径见 TOOLS.md §11、USAGE.md §7 的 `media locate`。

**Q17：为什么有些 appmsg（引用 / 链接 / 文件 / 转账）读不出来？**
三种成因，处置各不相同：

1. **没回填**（最常见；也包括「回填跑在这条消息到达微信之前」）。appmsg 的原始 XML **不进
   `messages`**，而是存在索引库的 `msg_parts` 表里，由 CLI `agent-wechat appmsg rebuild`
   显式回填（本机实测 37.5–38.7 s，幂等）。没跑过它时，`chat export` 的产物里 appmsg 只显示
   `[appmsg: 标题]`，MCP 的 `get_message_parts` 答不出任何 key——两者的信封都会带 `degraded`
   + `hint` 指出这件事。回填扫的是**微信库**，`export` **不写**这张表，所以**回填之后新到
   微信**的 appmsg 要重跑一次才拿得到内容。
2. **解不开（`unparsed`）**：载荷解压失败（半拷贝的 WAL / 手工改过的索引库），或三步规范化后
   仍不是合法 XML。原库全量普查里，规范化把 appmsg 的可解析率从 32.8% 提到 **99.9985%**
   （66,655 / 66,656），剩下的那条是非法字符引用。
3. **被主动拒绝（`rejected`）**：载荷含 `<!DOCTYPE` / `<!ENTITY` 声明，展开有把进程打挂的风险
   （billion laughs），守卫**直接拒绝**——这是保护，不是数据坏了。实测这两个串在全量 appmsg
   里出现 0 次，所以守卫**零误伤**。

2 与 3 **分开计数**（合成一个数会让人以为数据坏了）：本机那次回填 66,663 条里两者都是 **0**。
明细印在 `chat export` 产物顶部，MCP 信封装 `unparsed` / `rejected`。

另有一条**陈旧规则**：引用的**跳转目标**（`ref_msg_key`）只反映回填那一刻的索引状态——目标
后来才导出进索引时，重跑回填才能跳转（**被引用的原文不受影响**，它一直带在 appmsg 里，见
DATA_MODEL.md §3）。

还有一件事：`get_message_parts` 少了一个 key 时，成因可能是「未回填」（只有这一种信封会
点名——`degraded` + `hint`）、「回填跑在它之前」、「这条本来就不是 appmsg」三者之一。
**后两种彼此逐字节相同**且都不带 `degraded`（`backfilled` 是账号级的，答案是按 key 的）。
用法见 USAGE.md §7 的 `appmsg` 与 `chat` 两节。
