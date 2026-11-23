# Phase 02：集成报告

> 日期：2026-11-23 ｜ 基线提交：`97b78dc` ｜ 前置：[Phase 00 审计](phase-00-language-architecture-audit.md)、
> [Phase 01 原型](phase-01-native-poc-report.md)

## 0. 结论

| 原型项 | 决定 | 理由 |
|---|---|---|
| Rust/PyO3 逐行解码 | **放弃** | 导出只快 <1%，换来平台相关构建与语义对齐的长期成本（Phase 01 §3.6） |
| 进程内 SQLCipher（原生扩展 `sqlcipher3-wheels`） | **集成**，CLI 保留为回退 | 在线读取的主瓶颈；明文不再落盘；去掉 brew 依赖 |
| tmutil 先查后排 | **集成** | 每个进程 10 s 的超时；改动十几行 |
| 嵌入推理批 256 → 8 | **集成** | 吞吐与内存都显著改善，向量等价、不需要重建索引 |
| FTS 批量重建 | **本阶段不做** | 只惠及首次 / `--full` 导出；需要持久化中断标记与故障注入测试才能保证完整性，且与「原生扩展集成」无关。建议单独立项 |

本阶段引入的唯一原生组件是 **SQLCipher 的 C 绑定**（第三方 wheel），没有自写原生代码。

## 1. 集成设计

### 1.1 解密：`decrypt.open_db(db, passphrase)`

```
open_db ─┬─ backend()=="inproc"（默认）
         │    _keyed_snapshot：tmp/awdec-<pid>-*（0700）快照 → 读 salt → PBKDF2（lru 缓存）
         │    sqlcipher3 只读打开加密快照 → 立即读 sqlite_master 验钥 → yield 代理连接
         └─ backend()=="cli"（AGENT_WECHAT_DECRYPT=cli，或 sqlcipher3 不可用）
              decrypted()（原实现，整库 sqlcipher_export）→ open_plain → yield sqlite3 连接
退出：关连接、删快照目录（调用方体内抛错也一样）
```

- **接口兼容**：`decrypted()` / `open_plain()` 保持原签名与行为（`decrypt` / `inspect` 两个调试命令仍用它们）。
  读数据的 4 处调用点（`reader._open`、`media` 两条语音路径、`moments.iter_posts`）改用 `open_db`，
  调用方不再自己 close。对上层（用例、CLI、MCP）**零接口变化**。
- **异常口径**：连接 / 游标代理把 `sqlcipher3.*Error` 翻成同名 `sqlite3` 异常（原有的 `except sqlite3.*` 不受影响）；
  恰为 `DatabaseError` 的（页 HMAC 失败 = 快照撕裂）翻成 `DecryptError`——CLI 后端在整库导出时就以它失败，
  进程内只是把同一个失败推迟到查询中途。密钥错误在打开处就报 `DecryptError`，且 `from None` 截断异常链（`PRAGMA key` 语句含密钥）。
- **不做运行期自动回退**：密钥不对时两个后端一样失败，静默重试只会多一次整库导出、藏住真问题。
- **依赖声明**：`sqlcipher3-wheels>=0.5.7,<0.6 ; python_full_version < '3.15'`。上游只发到 cp314；
  3.15+ 不装它，`backend()` 自动回退 CLI，而不是让安装去编 sdist 失败。
- **可观测**：`agent-wechat doctor` 新增 `decrypt: {backend, sqlcipher_cli}`；回退到 CLI 而找不到 `sqlcipher` 命令时进 `problems`。

### 1.2 tmutil

`ensure_home_dirs` 每进程一次：先 `time_machine_excluded()`（只读 `tmutil isexcluded`，已有函数）；
已 `[Excluded]` 就跳过，其余情况（未排除 / 判不出来）照旧 `addexclusion`——宁可慢，不漏排除。

### 1.3 嵌入

新常量 `vector.EMBED_BATCH = 8` 只作用于 fastembed 推理批；`BATCH = 256` 仍是 flush / 分页 / 排序窗口。

## 2. 验证

### 2.1 测试

| 范围 | 结果 |
|---|---|
| 全量门禁 `scripts/check.sh`（import 环 / vulture / ruff / pyright / pytest / 安全扫描） | 通过；pytest **1,731 passed** |
| 同一套 pytest 在 `AGENT_WECHAT_DECRYPT=cli` 下（回退路径） | **1,731 passed** |
| 新增 `tests/test_decrypt_open_db.py`（11 例） | 两后端同结果；无 -shm 的 WAL 快照可读；错钥 → DecryptError 且不回显密钥；SQL 错误 → 同名 sqlite3 异常；只读；页 HMAC 失败 → DecryptError；明文不落盘；体内抛错时连接与快照都释放；快照目录可被清扫器认领；密钥缓存与换钥；跨线程 close 的异常翻译 |
| 变异检验 | 去掉 DatabaseError→DecryptError 翻译、去掉只读约束，对应用例各自变红 |
| 新增 `tests/test_decrypt_real.py`（`-m real`） | 真机全部库：两个后端逐表摘要一致，通过 |
| 改动的既有用例 | `test_paths`（tmutil 先查后排 + 已排除不再排）、`test_vector`（推理批 ≠ flush）、`test_sync_ops`（doctor 报后端）；媒体 / 朋友圈的直通桩改为桩 `open_db` |

### 2.2 真实数据

| 场景 | 改动前 | 改动后 | CLI 回退 |
|---|---|---|---|
| `agent-wechat recent <群> -n 20`（3 轮，进程墙钟） | 12.15–12.18 s | **0.60–0.77 s** | 2.16–2.18 s |
| 三轮 stdout | — | 与 CLI 回退逐字节相同 | — |
| 没有 `sqlcipher` 命令的 PATH 下 `recent` | 失败 | 正常 | 报 DependencyMissing（预期） |

向量构建见 §2.3。

### 2.3 向量构建（均匀抽样 A/B）

在隔离数据目录里全量导出（778,546 条），再用真实 `build_index` 只嵌入 **id % 50 == 0 的均匀 2% 抽样**
（12,635 条可嵌入）——Phase 01 用的是「前 8,000 条」，不代表全量分布。顺序 8 → 256 → 8，控制负载漂移：

| 推理批 | 耗时 | 吞吐 | 峰值 RSS |
|---|---|---|---|
| 8 | 134.3 s | 94.1 条/s | 406 MB |
| **256（改动前）** | 893.7 s | **14.1 条/s** | **6,036 MB** |
| 8 | 147.7 s | 85.6 条/s | 466 MB |

**约 6.3 倍吞吐、峰值内存降到约 1/13**。另有一次全量构建跑了 18 分钟（97,280 条，约 88 条/s，RSS 稳定在 442 MB）
后被中止，与抽样结果一致。

**一个需要说清的落差**：TOOLS.md 记录的「全量 2.5–3 小时 ≈ 69 条/s」是 2026-09 的数据；同一台机器、当前依赖下，
改动前的设置只有 14 条/s（全量折算约 13 小时）。也就是说**相对当前代码是约 6 倍，相对那条历史记录只快约 1.3 倍**
（按 ~90 条/s 折算全量约 2 小时）。历史数字为何更快没有复现条件去查（依赖版本已变），文档里的「2.5–3 小时」
作为保守上界保留未改。

### 2.4 安装与分发

| 项 | 结果 |
|---|---|
| `uv build` → wheel / sdist | 正常（纯 Python wheel，`py3-none-any`，不变） |
| 干净 venv 安装 wheel（cp312、cp313） | 自动拉取 `sqlcipher3-wheels 0.5.7`，`backend()=="inproc"` |
| marker 解析 | `--python-version 3.15` 时不解析该依赖；3.14 时解析 |
| CI | 不变（仍 `brew install sqlcipher`：测试夹具用 CLI 造加密库，回退路径也要测） |

## 3. 代码审查

独立审查（只给 diff 与不变式，不给结论）核对了：代理覆盖的接口与所有拿到连接的代码路径（reader 11 处、`schema.probe`、
语音两条、朋友圈）、异常翻译（多处篡改页、截断文件、关闭后使用）、8 种 WAL 快照形态下两后端行数一致、8 线程并发开库、
`sqlcipher3-wheels` 编译为 `TEMP_STORE=2`（排序不会溢出成磁盘明文临时文件）、生成器上下文管理器的三种退出路径、tmutil 判据。
**未发现会破坏行为或违反不变式的问题**；提出一条存疑边界——`_CipherConnection.close()` 未经翻译，跨线程关闭时会冒出裸
sqlcipher3 异常（当前调用方走不到）。**已修复并补用例**。

## 4. 风险与后续

- **供应链**：`sqlcipher3-wheels` 个人维护、上次发布约 10 个月前。`uv.lock` 锁哈希；CLI 回退路径与真机对照用例保留；
  维护停滞时的退路是自建薄绑定（只包装库，不改业务）。
- **Python 3.15**：自动回退 CLI 后端（需要 brew sqlcipher），性能回到「仅 tmutil 修复」的水平。
- **FTS 批量重建**：建议作为独立阶段，带持久化中断标记、`doctor` 一致性检查与故障注入。
- **向量构建后期变慢**（TOOLS.md 早有记录）：本阶段 A/B 未覆盖索引增长效应，原因未定位，不在本阶段范围。
