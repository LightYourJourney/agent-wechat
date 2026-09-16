# agent-wechat（WeChat History MCP）

> 一个运行在 macOS 上的 **MCP 服务器**：读取本机微信的登录态与聊天记录，供
> Claude Code / Codex 等 Agent 调用——按联系人读取近期记录生成回复建议、
> 全量导出聊天记录到本地检索库用于分析与 LLM 蒸馏，或把朋友圈导出成
> 可离线阅读的 HTML 时间线。

**仅限本机、本人已登录的微信账号、纯本地处理。** 解密自己的聊天数据库涉及合规争议
（同类开源项目 chatlog 于 2025-10 被微信官方函件后删库），**请勿公开分发本仓库**。
详见 [SECURITY.md](docs/SECURITY.md)。

## 核心特性

| 特性 | 说明 |
|---|---|
| 🔐 登录态检测 | 检测微信是否运行/登录，识别当前登录账号（无侵入：`pgrep` + `lsof`） |
| 👥 多账号支持 | 账号数据目录按 `wxid` 隔离；passphrase 缓存按账号分键；支持账号切换 |
| 💬 近期记录读取 | 按联系人实时解密读取最近 N 条（秒级），供 Agent 生成回复建议 |
| 📦 历史全量导出 | 流式落库、幂等去重、断点续传；本机实测 84 万条消息约 1.5-2 分钟 |
| 🔍 本地全文检索 | SQLite + FTS5 trigram（≥3 字精确子串）+ LIKE 兜底（1-2 字） |
| 🧠 向量语义检索 | 本地嵌入（fastembed + bge-small-zh-v1.5，聊天内容零外发）+ sqlite-vec KNN；关键词记不全时按语义找（`semantic_search`） |
| 🖼️ 媒体导出 | 按会话导出图片（dat 解密）/ 语音（SILK → WAV）/ 视频（定位明文）；产物在 `~/.agent-wechat/media/`（0700） |
| 🧩 媒体与消息关联 | 按消息回答「有没有媒体、什么类型、本机在不在、在哪」：MCP `get_message_media` / CLI `media locate` 同一套四态（`none` / `exported` / `exportable` / `gone`），检索命中用稳定标识 `msg_key` 指名 |
| 📱 朋友圈导出 | 读取本账号可见的动态 / 评论 / 点赞，渲染成单文件 HTML 时间线；**默认零网络**，`--with-media` 才下载远程媒体本体 |
| 📄 会话时间线导出 | 把某会话导成可读的**时间线**（HTML / Markdown 双格式，可搬走）：appmsg（引用 / 链接 / 文件 / 转账）渲染成结构化块、引用可跳转到原消息（HTML 侧）、已导出的媒体以相对路径在位；appmsg 原始载荷由 `appmsg rebuild` 一次性回填（`chat export` / `chat index`，MCP `export_chat` / `get_message_parts`） |
| 🤖 Agent 集成 | MCP Server（13 个工具）+ Claude Code Skill + 同源 CLI |

## 适用场景

- **回复建议**：把「帮我回复 XXX」交给 Agent，它读近期上下文后给出候选回复，
  替代手动截图聊天记录。
- **历史检索**：「我和 XXX 聊过 XX 吗」——本地毫秒级全文检索。
- **LLM 蒸馏**：全量导出为结构化数据，供本地分析对话风格、构建个人知识库。

## 架构概览

```
┌────────────────────────────────────────────────────────────┐
│ 消费层   Claude Code (MCP+Skill) │ Codex (MCP) │ 终端 CLI  │
└──────────┬───────────────────────┬──────────────┬───────────┘
           │      stdio (JSON-RPC) │              │
┌──────────▼───────────────────────▼──────────────▼───────────┐
│ mcp_server.py —— 13 个工具的薄适配层（无业务逻辑）            │
├──────────────────────────────────────────────────────────────┤
│ 核心层（Python，可脱离 MCP 独立使用）                         │
│  account   登录态/账号发现（pgrep + lsof，只读）              │
│  keys      passphrase 缓存 + PBKDF2 派生 + HMAC 自验证        │
│  bootstrap lldb 断点捕获 passphrase（唯一需要 sudo 的环节）   │
│  decrypt   db+wal+shm 快照 → sqlcipher 解密（用后即焚）       │
│  reader    消息解码（zstd/类型掩码/群前缀）与流式产出          │
│  export    流式导出管道（幂等 upsert + 水位线 + 长任务）      │
│  store     本地检索库（SQLite + FTS5 trigram）                │
│  search    检索路由（trigram / LIKE 兜底）                    │
│  vector    向量索引与语义检索（本地 fastembed + sqlite-vec）  │
│  purge     本地数据销毁（清单→计划→执行→核查，CLI 专属）      │
├──────────────────────────────────────────────────────────────┤
│ 数据层                                                        │
│  微信容器（只读） │ ~/.agent-wechat/（0700：密钥缓存+索引库） │
└──────────────────────────────────────────────────────────────┘
```

核心设计：**一次性特权引导 → 之后完全无特权**。`bootstrap` 是唯一需要 sudo 的
环节，产出 passphrase 缓存；此后 MCP Server 常驻运行零特权。详见
[ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 快速开始

```bash
# 1. 安装（macOS + 微信 4.1.x，见 INSTALL.md 的完整前置条件）
cd ~/agent-wechat && uv sync

# 2. 一次性引导（仅首次 / 微信升级后；需 sudo，期间要在微信里重新登录一次）
killall WeChat
sudo codesign --force --deep --sign - /Applications/WeChat.app
open /Applications/WeChat.app        # 然后在微信中登录
sudo uv run --directory ~/agent-wechat agent-wechat bootstrap
#    启动后 5 分钟内：微信内 退出登录 → 重新登录，触发密钥捕获

# 3. 验证
uv run agent-wechat status --verify-keys     # 期望：HMAC 33/33

# 4. 使用
uv run agent-wechat recent filehelper -n 5   # 实时读取
uv run agent-wechat export                   # 全量导出（~2 分钟）
uv run agent-wechat search "关键词"           # 检索

# 5.（可选）向量语义检索：先建索引（首次会下载约 95MB 本地模型，之后完全离线）
uv run agent-wechat vector-build
uv run agent-wechat semantic "上次说的那个爬山的安排"
```

接入 Claude Code / Codex 的 MCP 配置见 [INSTALL.md](docs/INSTALL.md)；
工具清单见 [TOOLS.md](docs/TOOLS.md)。

## 支持的平台与版本

| 项 | 状态 |
|---|---|
| macOS（Apple Silicon / Intel） | ✅ 本机实测 |
| 微信 4.1.13.63 | ✅ 本机实测（2026-09） |
| 微信 4.1.x 其他版本 | ⚠️ 密钥路线与库结构同族，**需自行验证**（见 TROUBLESHOOTING） |
| 微信 4.0.x / 3.x / Windows / Linux | ❌ 不支持（4.0.x 的内存缓存密钥方案在本项目未实现） |

## 目录结构

```
agent-wechat/
├── README.md              ← 本文件
├── CONTEXT.md             领域词表（代码与文档共用的术语）
├── .mcp.json              Claude Code 项目级 MCP 配置
├── .codex/config.toml     Codex 项目级 MCP 配置（与 .mcp.json 同构的 TOML 版）
├── src/agent_wechat/      核心包（模块职责见 ARCHITECTURE.md）
│   ├── paths.py           路径常量与账号目录解析
│   ├── account.py         登录态检测与账号识别
│   ├── keys.py            密钥派生/校验/缓存
│   ├── bootstrap.py       lldb 捕获（sudo）
│   ├── decrypt.py         快照与解密
│   ├── errors.py          统一错误契约
│   ├── messages.py        messages 行契约
│   ├── appmsg.py          appmsg 原始载荷的规范化与解析
│   ├── roster.py          显示名规则（群昵称/备注/昵称优先级）
│   ├── schema.py          微信库结构声明与能力探测
│   ├── reader.py          消息读取与解码
│   ├── media.py           媒体导出（图片解密 / 语音 SILK→WAV / 视频定位）
│   ├── moments.py         朋友圈解析（sns.db，只读不联网）
│   ├── sns_media.py       朋友圈远程媒体下载与解密（唯一联网模块，默认关）
│   ├── moments_html.py    朋友圈渲染（单文件 HTML）
│   ├── chat_text.py       两个会话模板共享的措辞与纯函数
│   ├── chatview.py        会话时间线组装与落盘
│   ├── chat_html.py       会话时间线的 HTML 模板
│   ├── chat_md.py         会话时间线的 Markdown 模板
│   ├── chat_index.py      会话索引页模板
│   ├── export.py          导出管道与水位线续跑
│   ├── store.py           本地检索库
│   ├── search.py          检索路由
│   ├── vector.py          向量索引与语义检索
│   ├── tasks.py           长任务注册表
│   ├── usecases.py        用例层（两个入口共享的入口策略）
│   ├── purge.py           数据销毁（CLI 专属）
│   ├── mcp_server.py      MCP 工具层
│   └── cli.py             命令行入口
├── tests/                 集成测试（合成加密库，不依赖真机）
├── skills/                随仓库分发的 Claude Code Skill（wechat-reply）
├── spike/                 密钥可行性探索脚本（一次性，留档）
└── docs/
    ├── INSTALL.md         安装与配置
    ├── USAGE.md           使用指南
    ├── TOOLS.md           MCP 工具参考
    ├── ARCHITECTURE.md    架构设计
    ├── DATA_MODEL.md      数据与存储
    ├── SECURITY.md        隐私与安全
    ├── TROUBLESHOOTING.md 故障排查
    ├── FAQ.md             常见问题
    ├── schema-4.1.13.md   微信库结构实测记录
    ├── CHANGELOG.md       版本记录
    └── CONTRIBUTING.md    贡献指南
```

## 文档索引

| 文档 | 用途 |
|---|---|
| [INSTALL.md](docs/INSTALL.md) | 环境要求、依赖、权限、微信适配、MCP 配置 |
| [USAGE.md](docs/USAGE.md) | 典型场景、工作流、命令示例 |
| [TOOLS.md](docs/TOOLS.md) | 13 个 MCP 工具的参数/返回/错误 |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | 模块划分、数据流、设计决策 |
| [DATA_MODEL.md](docs/DATA_MODEL.md) | 表结构、去重与增量策略 |
| [SECURITY.md](docs/SECURITY.md) | 隐私、密钥安全、法律风险 |
| [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | 常见故障与修复 |
| [FAQ.md](docs/FAQ.md) | 用户常见疑问 |
| [CONTEXT.md](CONTEXT.md) | 领域词表（账号/会话/分片/水位线/用例…） |
| [CHANGELOG.md](docs/CHANGELOG.md) / [CONTRIBUTING.md](docs/CONTRIBUTING.md) | 版本与贡献 |

## 许可证与免责声明

本项目为**个人自用工具**，未设开源许可证（默认保留所有权利），**不得公开分发**。

> **免责声明**：本项目通过读取本机微信进程内存（仅一次、断点瞬间）获取本人账号的
> 数据库密钥，并解密本人设备上的聊天数据库。此行为可能违反《微信软件许可及服务协议》，
> 且"规避技术保护措施"的法律定性存在争议。使用本项目的一切后果由使用者自行承担，
> 包括但不限于账号受限。开发者不对任何人因使用本项目而遭受的损失负责。请勿用于
> 读取他人账号数据、二次分发或任何商业用途。
