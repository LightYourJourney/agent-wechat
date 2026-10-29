# CONTRIBUTING — 贡献指南

> 本仓库已公开，但**不接受外部 Pull Request**——它首先是作者的自用工具，流程按单人开发
> 设计（合规背景见 [SECURITY.md](SECURITY.md) §5）。本文面向"未来的自己"与本地协作者。

## 1. 开发环境

```bash
cd ~/agent-wechat
uv sync                          # 安装运行时依赖
uv add --dev pytest              # 开发依赖按需
uv run pytest tests/ -q          # 运行测试
```

要求：macOS + 微信 4.1.x + sqlcipher + lldb（见 [INSTALL.md](INSTALL.md)）。

## 2. 架构约定（改代码前必读）

- **分层**：核心层不 import MCP SDK；`mcp_server.py`/`cli.py` 只是入口，业务逻辑
  一律放核心模块——保证 Agent 与 CLI 行为一致
- **只读红线**：任何代码不得写入微信容器目录。涉及微信路径的新功能，先自问：
  "这条路径在微信进程打开的文件列表里吗？"
- **单一 schema 认知点**：结构声明只在 `schema.py`（`CAPABILITIES`），读取与探测接入在
  `reader.py` 与 `media.py`（媒体侧直接读 `VoiceInfo`/`Name2Id` 与 kvcomm 文件名）；
  schema 变化改 `schema.py` 与 [schema-4.1.13.md](schema-4.1.13.md)
- **自验证门不可绕过**：任何密钥候选必须过 `keys.page1_hmac_ok`；禁止"看起来像就采用"
- **路径注入**：模块不硬编码 `Path.home()`，通过 `paths.HOME_DIR`（可被环境变量覆盖）
  引用，保证可测试性

## 3. 测试策略

| 层 | 方式 |
|---|---|
| 路径/账号/store/search | 纯单测或合成数据（CI 可跑） |
| 解密/导出管道 | **合成加密库**（`tests/test_decrypt_pipeline.py`：用 sqlcipher 加密已知明文，
  验证快照→派生→解密→校验全链路，含错误密钥拒绝路径） |
| 密钥获取 / 真实 schema / 媒体内容 | **真机人工验证**（无法 mock）：跑 `status --verify-keys`、
  `inspect`、`recent`，按 [TROUBLESHOOTING.md](TROUBLESHOOTING.md) §6 自检 |
| 真机层（`slow` ＋ `real` 标记的用例） | `uv run pytest tests/ -m slow` **或** `uv run pytest tests/ -m real`
  ——两个入口都要能收全（EA-115：此前 `-m real` 漏掉 `test_vector_real.py`，两处口径分叉） |

新增功能时：先写能复现问题的测试，再修；涉及数据写入的功能必须补幂等性测试
（连续跑两次结果一致）。

## 4. 提交规范

- 提交信息用中文祈使句概括变更，正文说明动机与验证方式（参考根提交格式）
- **提交前必须做敏感数据自查**：`git status` + 抽查新增文件，确认无真实聊天内容、
  无联系人样本、无密钥材料。`docs/` 下的 schema 探测原始输出（含真实数据）不得入库，
  只保留手工整理的纯结构文档
- **换 clone / 换机器、或怀疑机制时**，跑 `uv run python security/scan.py --check-hooks`：它报安装状态
  的五态（未装 / 旧机制 / 配置指错 / 本地改动 / 正常）。提交拦截是唯一反复复发过的出口，
  而它的失效可以是完全静默的
- **门禁的单一入口是 `scripts/check.sh`**（五步：运行时 import 环 / vulture 候选集不超基线 /
  `pytest` / `scan --all` / 钩子自检；任一步失败即非 0 并指明是哪一步、哪一条）。
  `pre-commit` 只跑第①步（0.07 s），全量在 `pre-push`（~70 s）。手工跑：`bash scripts/check.sh`
  （加 `--fast` 只做结构检查）。vulture 的基线在 `security/gate-baseline.txt`，
  **只许放存量条目**——新增候选要么修掉、要么走一次显式的基线更新（别在提交里顺手加行）
- **账号标识一律用占位符**：真实 wxid（本机账号目录名、联系人 username）不得入库，
  文档与测试统一用 `wxid_testacct01` / `wxid_abc123def456` 这类合成值；
  示例与测试夹具里的本机绝对路径同理，用 `/Users/you`，不要写真实用户名。
  本仓库不再入库两处**功能性本机配置**：`.mcp.json` 与 `.codex/config.toml`（`~` 在
  JSON/TOML 里不展开，写进去只能是绝对路径，故由 `.gitignore` 挡住，模板是同名
  `.example`）。扫描器对它们的路径形仍保留按 (文件, 形状) 的豁免，以防旧克隆把它们带回来。
  路径这条**有自动层**（2026-10-12 起，**在 macOS 形态上**）：`security/scan.py` 的路径形
  会拦下 `/Users/<第一段>`（第一段不是 `you` 也不是 `Shared`）——`/home/…` 与
  `C:\Users\…` 不在形状里，**不拦**；而且它**不进豁免清单**——`--allow` 拒绝登记路径，
  **手抄进去也不生效**（命中照拦，还会被 `--verify-allowlist` 判成死条目）；登记等于把
  真实用户名写进一个会被提交的文件
- 本仓库已公开，push 前确认提交拦截已装：`git config core.hooksPath security/hooks`
  （见 SECURITY.md §6 出口 #2）

## 5. 版本与文档同步

- 行为变更同步更新 [CHANGELOG.md](CHANGELOG.md) 与相关文档（TOOLS/USAGE/DATA_MODEL）
- 微信 schema 变化：更新 [schema-4.1.13.md](schema-4.1.13.md)（标注实测日期与微信版本），
  必要时在 CHANGELOG 的"技术笔记"记录新发现
- 实测数字（条数/耗时）变化时更新 README 与 USAGE 中的参考值，注明"本机实测"

## 6. 代码风格

- Python 3.12+，类型注解齐全，模块级 docstring 说明职责与关键不变量
- 注释密度向现有代码看齐：解释"为什么"，不复述"是什么"
- 错误信息一律中文并带下一步指引；用户可见输出不经由 print 散落在核心层
  （核心层抛异常/返回数据，展示归 CLI/MCP 层）
