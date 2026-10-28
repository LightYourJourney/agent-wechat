# TROUBLESHOOTING — 故障排查

> 本文档按"症状 → 原因 → 处置"组织。先跑 `agent-wechat status` 与
> `get_account_status`（MCP）看链路卡在哪一层，再对照本文。

## 1. 引导（bootstrap）相关

### 1.1 `未找到微信进程`

微信未启动。`open /Applications/WeChat.app` 并登录后重试。

### 1.2 `未捕获到 passphrase`

按顺序检查：

1. **微信是否重签名**——签名被还原（微信自动升级后必然发生）时 lldb 无法 attach：

   ```bash
   codesign -dv --verbose=2 /Applications/WeChat.app 2>&1 | grep flags
   # 含 (runtime) 即 Hardened Runtime 仍开启 → 重跑：
   killall WeChat && sudo codesign --force --deep --sign - /Applications/WeChat.app
   open /Applications/WeChat.app && 重新登录后再 bootstrap
   ```

2. **是否在等待窗口内重新登录了**——passphrase 只在登录时派生一次，必须在
   bootstrap 的 5 分钟窗口内执行微信内「退出登录 → 重新登录」
3. lldb 是否可用：`which lldb`（无则 `xcode-select --install`）

### 1.3 `捕获值未通过任何库的 HMAC 校验（N 个库），拒绝落缓存`

捕获到了值但它是错误时刻的派生（如捕获到了别的 PBKDF2 调用）。直接重跑 bootstrap
即可；连续失败说明该微信版本的派生路径与预期不符——**属"因版本而异"情形**，可用
`agent-wechat inspect <库>` 确认库本身可解（对照 [schema-4.1.13.md](schema-4.1.13.md)），
并到 `spike/probe_keyinfo.py` 复核验证公式。

### 1.3b `一个库都没能读到（N 个不可读）`

捕获本身成功了，但**一个库文件都没读到**——这与 1.3 是两回事（1.3 是读到了库、值不对）。
通常是权限：给终端「完全磁盘访问权限」（[INSTALL.md](INSTALL.md) §3.3）后重试，不要反复
重跑引导。若权限正常仍报此错，看权限位：`ls -l` 微信容器下的 `db_storage/*/*.db`。

### 1.4 `PermissionError: ~/.agent-wechat/passphrase.json`

bootstrap 以 sudo 运行导致缓存文件属主为 root（旧版本代码的问题，新代码会自动归还
属主）。修复：

```bash
sudo chown -R "$USER" ~/.agent-wechat
```

### 1.5 bootstrap 成功，但 `status` 显示 `缓存✗`

`~/.agent-wechat/` 被 `AGENT_WECHAT_HOME` 环境变量改过位置——确认 MCP 配置与终端里
该变量指向同一目录。

## 2. 读取 / 导出相关

### 2.1 `KeyNotFound：账号 xxx 尚无 passphrase 缓存`

该账号未 bootstrap。**切换登录到该账号后**重跑一次 bootstrap（每个账号各自一次）。

### 2.2 `sqlcipher 解密失败`

1. 密钥失效：**修改微信密码**会使 passphrase 变化——重新 bootstrap
2. 单库损坏：`agent-wechat inspect <该库>` 复现。注意：**单个库解密失败会中止本轮
   导出**（此前批次已落库，重跑从水位线继续，但会在同一库再次失败）——先按第 1/3 点
   排除密钥与版本问题
3. sqlcipher 版本过旧（< 4.x 默认参数不匹配）：`sqlcipher -version`，`brew upgrade sqlcipher`

### 2.3 导出中断 / 进程被杀

直接重跑 `agent-wechat export`——水位线与数据同事务提交，自动从断点续传；
已写入的行因幂等主键不会重复。若怀疑索引损坏，`export --full` 全量重扫（幂等）。

### 2.4 导出结果比预期少

- 微信可能未把历史消息**漫游/同步**到本机（手机端更久远的数据本机可能没有——
  这是微信客户端行为，非本工具问题；在微信设置中检查"聊天记录迁移"）
- 某些会话从未在本机打开过：其表存在但无数据
- 用 `agent-wechat inspect message/message_N.db --sample 0 | grep -c '^--- Msg_'`
  对比源库表数量与 `index.db` 的 `watermark` 行数（仅有数据的表会计入）

### 2.5 导出时微信卡顿

快照过程是顺序读盘（约 500MB），可能占用数秒磁盘带宽；导出期间正常使用微信即可。
正常情况下快照是自洽的；微信重度写入的瞬间偶发不一致时，导出会直接报错——
重跑即可（详见 ARCHITECTURE.md §7 已知边界）。

## 3. 检索相关

### 3.1 `search_history` 提示索引为空

先跑 `agent-wechat export`（见 USAGE.md 场景三）。

### 3.2 1-2 个字的查询很慢

trigram 分词无法匹配 <3 字符，自动降级为 LIKE 全表扫描。**带上 `--contact`** 限定
会话即可（毫秒级）。

### 3.3 搜不到刚收到的消息

索引是导出时刻的快照。重跑 `agent-wechat export`（增量，秒级）后再检索。

## 4. 版本适配（微信升级后）

**症状**：bootstrap HMAC 全失败 / `inspect` 报错 / 导出内容异常。

**背景**：本项目按微信 4.1.13.63 实测；微信升级后库结构、加密参数甚至密钥派生路径
都可能变化——**属"因版本而异"范畴，不要假设兼容**。

**处置**：

1. 重签名微信（升级必然还原签名）→ 重新 bootstrap
2. `agent-wechat inspect session/session.db` 能否正常解密并列出表结构
3. 对比 [schema-4.1.13.md](schema-4.1.13.md) 找差异（表名/列名/压缩标记变化）
4. 仍失败 → 该版本可能改变了密钥机制，参考 `spike/probe_keyinfo.py` 的验证公式
   重新确认派生算法后再用

## 5. 环境与权限

### 5.1 `ls 微信容器目录` 报 `Operation not permitted`

给运行本工具的终端 App 开「完全磁盘访问权限」（系统设置 → 隐私与安全性）。

### 5.2 `status` 运行 30 秒才返回

旧版实现曾因 lsof DNS 解析卡 30 秒（已修复为 `lsof -n -P`）。若仍出现，检查是否
有第三方 shell 集成改写了 `lsof` 的调用方式。

### 5.3 MCP 工具在 Agent 中不可见

- Claude Code：确认 `.mcp.json` 中 `--directory` 指向实际仓库路径；重启会话
- Codex：确认 `~/.codex/config.toml` 的 `[mcp_servers.agent-wechat]` 段无重复
- 手动验证 Server 本身：见 TOOLS.md 底部的 stdio 测试方法

## 6. 自检命令速查

```bash
uv run agent-wechat status --verify-keys      # 链路总检查（密钥级）
uv run pytest tests/ -q                       # 核心逻辑回归（不依赖真机）
agent-wechat inspect session/session.db       # 解密层 + schema 探测
agent-wechat recent filehelper -n 3           # 端到端读取
```

## 7. 本地索引库 schema 迁移（`SchemaMigrationError`）

本地库带版本表（`schema_version`），首次连接旧库时会自动升级一次（当前为 `main v2`：
对齐 FTS 触发器体）。迁移在**同一个库内的一笔事务**里完成，失败即整条回滚、版本停在上一版。

| 现象 | 原因 | 处置 |
|---|---|---|
| `迁移失败：main v2（…）：database is locked` | 另一个进程正持写锁（导出 / 向量构建 / 另一个 MCP server） | 等它结束再重试；`status` 可看长任务 |
| `迁移失败：…：disk I/O error` / `No space left` | 磁盘满、只读卷、权限 | 修好磁盘/权限后重跑同一命令即可续 |
| `索引库版本过新：main 已到 vN，本程序只认到 vM` | 你降级运行了旧版程序 | 升级 `agent-wechat` 到匹配版本；或从备份恢复旧库。**不要**用旧版继续写 |
| 升级后 `status` 的 `schema_notes` 提示触发器不一致 | 迁移之后再被手工改动过 | 只报不改库；重跑一次 `export` 或手工重建触发器 |

**备份**：迁移前想留退路就拷 `index.db`（+`-wal`/`-shm`）与 `exclusions.db`。
本阶段的迁移**不删列、不改表结构**，所以通常不需要回退；最坏情况是
`purge --index` 后重跑 `export`（2.5–3 h，含向量）。


## 8. 向量索引与重嵌队列

历史消息被改写后，变化先进 `pending_reembed`，由下一次 `vector-build` 消费。
`agent-wechat vector-status` 是唯一需要看的诊断（只读）：

| 现象 | 原因 | 处置 |
|---|---|---|
| `pending` 持续不降 | 没跑 `vector-build`，或积压超过每轮 4 批（256 条） | 再跑几次；`--rebuild` 不必要 |
| `failed` / `max_attempts` 升高 | 嵌入模型加载失败、维度不符 | 先修环境（模型缓存/依赖），再重跑；失败行在队列里等重试，不会丢 |
| 检索结果仍是旧语义 | 队列还没被消费 | 跑 `vector-build`（增量即可），再看 `pending` 归零 |
| `semantic_search` 报「与当前模型不一致」 | 索引由别的模型建的 | `vector-build --rebuild`（会清空队列，全量重嵌） |
| 模型缓存缺失时构建报错 | 首次使用需要本地模型 | `vector-build` 首次会下载约 95MB；离线环境请先准备好缓存 |

## 9. 同步与健康诊断（`sync` / `doctor`）

`agent-wechat sync` 的状态码是**分流**用的，不是笼统的「失败」：

| 状态 | 含义 | 处置 |
|---|---|---|
| `no_new` / `ok` | 没有/有新增 | 无需处理 |
| `wechat_not_running` | 未给 `--account` 且微信未登录 | 登录微信，或显式给本机账号 |
| `key_missing` | 该账号没有 passphrase 缓存 | 完成一次性引导（需要 sudo，必须由人执行） |
| `account_not_found` | 显式 `--account` 指向不存在的目录 | 用 `status` 看已有账号，或省略该参数 |
| `key_invalid` | 解密失败（sqlcipher/密钥） | 重新 bootstrap；确认微信升级过 |
| `source_unavailable` | 微信数据目录/分片读不到（含快照复制失败） | 检查完整磁盘访问权限与容器路径 |
| `index_unavailable` | 索引库打不开 / 迁移失败 | 看 `doctor` 的 schema 段；必要时恢复备份 |
| `vector_unavailable` | 消息已同步，向量阶段失败 | 准备本地模型缓存后重跑 `vector-build` |
| `disk_low` | 可用空间 < 200 MB | 清理磁盘 |
| `concurrent` | 另一个同步在跑（全局锁） | 等它结束；`doctor` 看 `sync_state`（本次不覆盖） |
| `disk_error` | 磁盘错误（EIO/ENOSPC 等） | 修磁盘/权限后重跑 |
| `failed` | 未预期异常（详情带类型） | 看 `detail` 后重跑；持续失败请报 issue |

`agent-wechat doctor` 只读、不建库：先看它的 `problems` 数组，再按上面的表处置。
`problems` 里出现「FTS 行数不一致」时，重跑一次导出即可重建；出现「版本高于本程序」时
必须升级程序，不要用旧版继续写库。


## 10. 本地识别（`derived`）不产出文字

先跑 `agent-wechat derived status` 看引擎：

| 现象 | 原因 | 处置 |
|---|---|---|
| `ocr.available=false` | 没有 `tesseract` | 本机安装 tesseract（需要 sudo，必须由人执行） |
| 中文识别为空/乱码 | 只装了 `eng` 语言包 | 自行安装 `chi_sim.traineddata`；程序不会下载 |
| `asr.available=false` | 没有 `whisper-cli` 或没有模型 | 安装 whisper.cpp，并用 `AGENT_WECHAT_WHISPER_MODEL` 指向本地 `ggml-*.bin` |
| `skipped.media_not_exported` 很多 | 媒体还没导出 | 先 `agent-wechat media export` |
| `too_large` / `too_long` | 超过 20 MB / 600 s | 单条处理，或调大上限（改代码常量，非配置） |
| `DerivedBusy` | 另一个摄取在跑 | 等它结束 |
| 报「需要显式 --chat」 | 没有给会话 | 加 `--chat`，或明确 `--all` 跨会话 |
| 磁盘不足 50 MB | 派生要写索引库 | 清理磁盘后重跑 |
