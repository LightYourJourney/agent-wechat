# INSTALL — 安装与配置指南

> 本文档说明环境要求、依赖安装、macOS 权限配置、微信版本适配，以及接入
> Claude Code / Codex 的 MCP 配置方法。故障排查见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。

## 1. 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| 操作系统 | macOS 11+ | Apple Silicon 与 Intel 均可（本项目在 Apple Silicon 实测） |
| 微信 | **macOS 4.1.x**（实测版本 4.1.13.63） | 4.1+ 是硬性要求：该版本起密钥只以 passphrase 形式存在内存中，本项目按此路线实现。**其他 4.x 小版本路径/结构可能不同，需自行验证**（见 §4） |
| Python | ≥ 3.12（经 `uv` 自动管理） | 无需手动装 Python |
| uv | 最新版 | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| sqlcipher CLI | 4.x（`brew install sqlcipher`） | 用于数据库解密导出 |
| Xcode Command Line Tools | 提供 lldb | `xcode-select --install` |
| FileVault | **建议开启** | 本地索引库为明文，依赖全盘加密保护；代码不校验也不阻断，建议用 `fdesetup status` 确认（见 SECURITY.md） |

> 💡 检查环境：`csrutil status`（SIP 保持 enabled 即可，**不需要关闭 SIP**）；
> `fdesetup status`（FileVault）；`sqlcipher -version`。

## 2. 安装步骤

```bash
# 1) 获取代码（本仓库为本地仓库，勿从公网拉取不明副本）
cd ~/agent-wechat

# 2) 安装依赖（自动创建 .venv）
uv sync

# 3) 验证 CLI 可用
uv run agent-wechat status
```

依赖 6 个：`mcp`（MCP SDK）、`zstandard`（WCDB 压缩解码）、`fastembed` + `sqlite-vec`（本地向量检索，首次下载模型）、`cryptography`（图片 dat 的 AES 解密）、`pilk`（语音 SILK→WAV）；其余全为标准库。语音 / 向量为按需能力：缺 `pilk` 只降级、缺 `sqlite-vec` 则向量功能不可用。

### 2.1 全局命令（可选，推荐）

默认情况下 `agent-wechat` 只存在于项目 `.venv/bin/`，需经 `uv run` 前缀调用（见
[USAGE.md](USAGE.md) §0）。若想在**任何目录**裸敲 `agent-wechat`，做一次性全局安装：

```bash
uv tool install --editable ~/agent-wechat
# 生成 ~/.local/bin/agent-wechat 与 agent-wechat-mcp；--editable 改代码即时生效
```

> ⚠️ sudo 场景（bootstrap）写全路径：`sudo ~/.local/bin/agent-wechat bootstrap`
> （`~/.local/bin` 不在 sudo 的 PATH 中）。卸载：`uv tool uninstall agent-wechat`。

## 3. macOS 权限配置

本项目**不需要**辅助功能（Accessibility）权限，**不需要**关闭 SIP。涉及的权限只有两项：

### 3.1 一次性 sudo（bootstrap 期间）

`bootstrap` 需要 sudo 运行（lldb attach 微信进程需要 `task_for_pid` 权限）。
这是整个项目**唯一**的特权环节，完成后所有日常操作（MCP、导出、检索）均为普通用户权限。

### 3.2 微信重签名（bootstrap 的前置步骤）

macOS 微信自带 Hardened Runtime，会阻止 lldb attach。需要做一次 ad-hoc 重签名去掉它：

```bash
killall WeChat
sudo codesign --force --deep --sign - /Applications/WeChat.app
open /Applications/WeChat.app   # 重启微信并登录
```

> ⚠️ 影响说明：重签名只改本机微信包的代码签名，不改动数据与聊天记录；微信每次
> **自动升级后签名会被还原**，届时 bootstrap 会失败，需重跑上面两行命令。

### 3.3 可能需要：给终端「完全磁盘访问权限」

微信容器目录（`~/Library/Containers/com.tencent.xinWeChat/...`）通常普通终端可直接读取。
若 `ls` 该目录报 `Operation not permitted`，在 系统设置 → 隐私与安全性 →
完全磁盘访问权限 中，为你运行本工具的终端 App（Terminal / iTerm / VS Code）开启权限。

## 4. 微信版本适配

本项目按微信 **4.1.13.63** 实测实现。若你的微信是其他 4.1.x 小版本：

1. 库加密参数（SQLCipher 4：AES-256-CBC / PBKDF2-SHA512 / 256,000 轮 / 页 4096）在 4.1 家族
   一致的可能性高，但**需自行验证**；
2. 验证方法：

   ```bash
   # 跑一次 bootstrap；若 HMAC 校验通过 N/N 个库，说明密钥派生与库结构兼容
   sudo uv run agent-wechat bootstrap
   ```

3. 若 HMAC 全部失败或 `inspect` 解密报错，说明库结构/加密参数有变化——
   用 `agent-wechat inspect <库名>` 对比 [schema-4.1.13.md](schema-4.1.13.md)
   找出差异点（schema 文档给出了每个表的完整 DDL）。

微信 4.0.x 及以下：密钥在内存中以 raw key 缓存（与 4.1+ 机制不同），本项目未实现该路线。

## 5. 一次性引导（bootstrap）

```bash
killall WeChat
sudo codesign --force --deep --sign - /Applications/WeChat.app
open /Applications/WeChat.app        # 然后在微信中登录
sudo uv run --directory <仓库路径> agent-wechat bootstrap
# 命令启动后有 5 分钟窗口：在微信内 退出登录 → 重新登录
# 成功标志：✅ HMAC 校验通过 N/N 个库
```

原理与风险说明见 [SECURITY.md](SECURITY.md)（§2 密钥如何获取）；多账号见
[FAQ.md](FAQ.md)（Q4）。

## 6. MCP 配置

### 6.1 Claude Code

**项目级**（推荐）：仓库根目录已带 [.mcp.json](../.mcp.json)——本机实际形态是**直调**
`.venv/bin/agent-wechat-mcp`；下面给的是等价的 `uv run` 写法（两种形态都可用）：

```json
{
  "mcpServers": {
    "agent-wechat": {
      "command": "uv",
      "args": ["--directory", "/Users/you/agent-wechat", "run", "agent-wechat-mcp"],
      "env": { "AGENT_WECHAT_HOME": "/Users/you/.agent-wechat" }
    }
  }
}
```

> 把路径替换为你的实际仓库路径。Claude Code 在项目目录内启动时会自动加载
> 项目 `.mcp.json`；若要全局可用，把同样的块加入 `~/.claude.json` 的 `mcpServers`。

**可选 Skill**：复制 `skills` 目录中的 `wechat-reply` 到 `~/.claude/skills/`，
之后说「帮我回复 XXX」即可触发完整工作流（消歧 → 读记录 → 候选回复）。

### 6.2 Codex

在 `~/.codex/config.toml` 追加：

```toml
[mcp_servers.agent-wechat]
command = "uv"
args = ["--directory", "/Users/you/agent-wechat", "run", "agent-wechat-mcp"]
startup_timeout_sec = 60

[mcp_servers.agent-wechat.env]
AGENT_WECHAT_HOME = "/Users/you/.agent-wechat"
```

### 6.3 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_WECHAT_HOME` | `~/.agent-wechat` | 密钥缓存、索引库、临时文件的根目录（自动 0700） |
| `AGENT_WECHAT_DENY` | 空 | 逗号分隔的 chat_id；**只约束 Phase 05 的 `retrieve` / `context`**，不给既有工具加行为 |
| `AGENT_WECHAT_WHISPER_MODEL` | 自动探测本地模型 | Phase 06 的 ASR 模型路径；**只读本地文件，绝不下载** |

## 7. 安装验证清单

```bash
uv run agent-wechat status --verify-keys
```

- [ ] `微信运行: True`，`当前登录` 显示账号目录名
- [ ] 每个已引导账号显示 `缓存✓ HMAC N/N`（有库读不到时会追加「（另有 N 个库读不到）」）
- [ ] `备份排除: [Excluded] …`（`[Included]` 时手动 `tmutil addexclusion ~/.agent-wechat`；
      该行由只读查询得出，查不出来时不会出现）
- [ ] `uv run agent-wechat recent filehelper -n 3` 返回真实消息
- [ ] MCP 接入后在 Claude Code 里调用 `get_account_status` 返回 JSON

任一步失败 → [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。
