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
