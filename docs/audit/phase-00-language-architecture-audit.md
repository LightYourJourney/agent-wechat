# Phase 00：语言与架构审计报告

> 审计日期：2026-11-23 ｜ 基线提交：`97b78dc` ｜ 本阶段**未修改任何业务代码**
>
> 结论先行：**继续使用 Python，不做语言迁移。** 实测的耗时大头分别落在一个 10 秒超时、
> 外部 `sqlcipher` 进程整库解密、嵌入批大小配置和 FTS5 逐行建索引上。它们都能在 Python
> 内修掉，且修复后预期收益（在线读取约 20–30 倍、向量构建 3 倍以上、全量导出约 2 倍）
> 远大于整体改写成 Rust 的理论收益（≤ 10–25%）。

---

## 1. 范围与方法

| 项 | 内容 |
|---|---|
| 机器 | Apple M3（8 核）/ 16 GB / macOS（APFS） |
| 数据 | 本机真实账号：微信库 984 MB（最大分片 237.6 MB），全量导出后 **778,417 条消息 / 780 个会话** |
| 方法 | `cProfile` 剖析 CLI 真实命令；对解密、FTS、sqlite-vec、嵌入、纯 Python 字节运算分别做隔离 benchmark；用临时 venv 验证进程内 SQLCipher（spike） |
| 隔离 | 全量导出类基准在独立的 `AGENT_WECHAT_HOME` 中运行，没有碰主索引；结束后已删除基准目录（含明文索引与密钥副本）和全部脚本 |
| 已知局限 | 基准期间机器负载不稳定（load avg 4–21，另有进程在跑）。**绝对值有噪声，同一轮内的相对比较可靠**；有关结论都标了复测要求 |

## 2. 现状画像

- **代码规模**：`src/` 44 个模块、17.8k 行；`tests/` 31k 行、1,549 个测试函数（全量 1,717 passed / 130 s）。
  门禁（`scripts/check.sh`）包括 import 环检查、vulture、ruff(F)、pyright、pytest、安全扫描与钩子自检。
  另有故障注入、变异扫描、hypothesis 性质测试和差分测试。**测试资产比业务代码还多**，这是语言决策中权重最高的事实。
- **分层**：核心层不依赖 MCP SDK；`usecases/` 只实现一次，`cli.py` 与 `mcp_server.py` 两个入口共用。分层是健康的，没有因语言而生的架构债。
- **运行时依赖**：`mcp`、`zstandard`、`fastembed`（带入 onnxruntime 76 MB、numpy、tokenizers、huggingface_hub、PIL）、
  `sqlite-vec`、`cryptography`、`pilk`。运行时安装面约 170 MB，另需下载 91 MB 模型。
- **外部二进制**：`sqlcipher` CLI（Homebrew，**每次读取都要用**）、`lldb`（Xcode CLT，仅 bootstrap）、
  `codesign`（仅重签名微信）、`tmutil`。
- **平台绑定**：macOS 微信 4.1.x 的 passphrase 路线、lldb 断点、Hardened Runtime 重签名、launchd、Time Machine，
  每一项都绑定 macOS。**这种绑定来自业务本身，与实现语言无关。**

## 3. Profiling 与 Benchmark 结果

### 3.1 在线读取：`recent <群聊> -n 20`（MCP `get_recent_messages` 同路径）

| 阶段 | 耗时 | 说明 |
|---|---|---|
| **总计（现状）** | **12.3 s** | 两次重复稳定在 12.3–12.5 s；user+sys CPU 只有约 2 s |
| `paths.ensure_home_dirs` → `tmutil addexclusion` | **10.0 s** | `paths.py:293`；单独计时 `addexclusion` 11.0 s，`timeout=10` 次次触发 |
| 5 次 `sqlcipher_export` 子进程 | 1.75 s | `decrypt.py:118`，每次把整个库解密成明文副本再查询 |
| PBKDF2（256k 轮 × 5 库） | 0.26 s | `keys.derive_key`，每库每次 51 ms |
| Python 自身（roster/联系人解析等） | ≈ 0.3 s | |

`_tm_exclusion_checked` 是进程级标志，所以 **每个 CLI 进程都要付这 10 s**；MCP 常驻进程
每个会话的**第一次**工具调用也要付一次。只读查询 `tmutil isexcluded` 只要 0.04 s，
本机数据目录已经是 `[Excluded]`。

### 3.2 解密方式对比（单库，同一快照）

| 库 | 大小 | 快照 copy2 | `sqlcipher_export`（现状） | 进程内直接查询 |
|---|---|---|---|---|
| contact.db | 11.9 MB | 5 ms | 61 ms | — |
| message_0.db | 122.9 MB | 70 ms | 939 ms | — |
| **message_4.db** | **237.6 MB** | 78 ms | **1,540 ms** | **11.7 ms**（spike：`sqlcipher3-wheels` 0.5.7 / SQLCipher 4.12，取最近 20 条） |
| media_1.db | 87.8 MB | 51 ms | 213 ms | — |

现状是「复制快照 → 整库解密写出明文 → 查询」，耗时随**库大小**线性增长；进程内打开加密快照
只解密查询实际触及的页，耗时与库大小基本无关。顺带的好处是**明文副本不再落盘**，安全面变小。
（`sqlcipher` CLI 子进程直接查询同样只要 8–15 ms，但正文是 zstd 压缩的 BLOB，走文本管道并不合适。）

### 3.3 全量导出（778,417 条）

| 指标 | 第 1 轮（重负载，cProfile） | 第 2 轮（较低负载） |
|---|---|---|
| 墙钟 | 110 s | **71.6 s**（≈ 10.9k 条/s） |
| 峰值 RSS | 80 MB | 94 MB |

第 1 轮的剖析分解：

| 成本项 | 耗时 | 占比 | 所在层 |
|---|---|---|---|
| `executemany`（写 messages 及触发器维护的 trigram FTS5） | 76.2 s | 69% | SQLite C |
| `commit` | 7.8 s | 7% | SQLite C / IO |
| `sqlcipher_export`（30 次 `_open`） | 9.4 s | 9% | 外部进程 |
| 其中 `username_pool` 为读 `Name2Id` 把所有分片整库解密一遍 | 4.5 s | | `reader.py:256`，之后 `iter_all` 又解一遍 |
| Python 行处理（`_decode_row`、正则、zstd、`make_row` 等） | ≈ 12–15 s | 11–14% | **解释器** |

FTS 归因实验：把这 77.8 万行原样重灌进新库

| 方式 | 耗时 |
|---|---|
| 无 FTS | 7.3 s（107k 行/s） |
| 触发器逐行维护 trigram FTS（现状形态） | 21.7 s（低负载）/ 38.9 s（高负载） |
| **先灌正文，末尾一次 `'rebuild'`** | **2.1 s + 5.1 s = 7.2 s（约 3 倍）** |

### 3.4 向量构建（文档记录：全量 2.5–3 小时，约 69 条/s）

**sqlite-vec 不是瓶颈**：用真实 (id, 会话) 顺序和随机向量插入 20 万行，速率 14.6k 行/s，
随规模没有明显衰减。瓶颈在 ONNX 推理。

用真实文本分布（走项目自己的 `should_embed` / `doc_text`，中位 61 字、p99 159 字）做测试：
每 256 条按长度排序（与 `vector.py:372` 相同），只改 fastembed 内部的 `batch_size`：

| fastembed `batch_size` | 第 1 轮 | 第 2 轮 | 第 3 轮 |
|---|---|---|---|
| **256（现状 `BATCH`）** | 23 | 23 | 16 |
| 32 | 75 | 96 | 105 |
| 16 | 115 | 122 / 96 | 159 / 95 |
| 8 | — | 139 | **191** |

（单位：条/s；斜杠两侧是同一轮的首尾两次，体现负载漂移。）

**根因**：`flush()` 先排序，再把整组 256 条作为**一个**批次送进推理，于是整组仍被 padding 到组内最长的那条。
排序本身没有错，问题在批次没有切小，排序的收益就此丢掉。三轮测试的方向一致：小批次快 4–10 倍。
**等价性已验证**：同一组文本在 bs=256 与 bs=8 下的向量最小余弦为 1.000000、最大绝对差 1.5e-7，
**改批大小不需要 rebuild**。另测了 CoreML EP：只覆盖 6/40 个节点，比 CPU 更慢，不建议采用。

### 3.5 其他

| 项 | 结果 |
|---|---|
| 关键词检索（77.8 万条，FTS5 trigram） | 20–280 ms |
| 启动 | CLI 导入 0.06–0.10 s；`mcp_server` 导入 0.57 s（其中 `mcp` SDK 0.24 s；fastembed 惰性加载） |
| 单字节 XOR（`media.py:86,99`，2 MB） | 生成器 47 ms，`bytes.translate` 0.58 ms（80 倍） |
| 朋友圈 ISAAC64（`sns_media.py`，1 MB） | 纯 Python 82 ms（约 12 MB/s）：keystream 53 ms + zip-XOR 34 ms（换成大整数 XOR 只要 1.7 ms）。视频只解前 128 KB，实际影响小 |
| PBKDF2 | 每库每次 51 ms；导出中共 15 次，0.84 s |

## 4. 热点归因：多少时间真正花在 Python 解释器里

| 场景 | 主瓶颈 | 解释器占比 | 能否在 Python 内解决 |
|---|---|---|---|
| 在线读取 | tmutil 超时 + 整库解密 | < 3% | 能（§7 P1/P2） |
| 全量导出 | SQLite FTS5 写入 | 11–14%（修完其他项后估计升到 25–30%） | 主瓶颈能（P3） |
| 向量构建 | ONNX 推理的 padding 浪费 | ≈ 0（推理在 C++） | 能（P1） |
| 检索 / 启动 | 已经足够快 | — | 无需处理 |

**结论：主要热点都在 C/C++ 库里（SQLite、SQLCipher、OpenSSL、ONNX Runtime、zstd）。**
Rust 调用的是同一批库，换语言消不掉这些成本，消不掉的东西也就谈不上加速。

## 5. 语言适用性评估

| 维度 | Python（现状） | Rust | Go |
|---|---|---|---|
| 性能上限（本项目） | 修完 P1–P3 后，在线读取约 0.4 s、导出约 35 s（估） | 只能再省解释器那部分：导出约 8–10 s，在线读取约 0.1 s | 同 Rust，甚至更差 |
| SQLCipher | `sqlcipher3-wheels`（已验证） | `rusqlite` + `bundled-sqlcipher`（成熟） | 必须 cgo，**纯 Go 交叉编译的优势随之消失** |
| 本地嵌入 | fastembed（成熟） | `ort` + `tokenizers`（可行，仍需带 onnxruntime 动态库） | onnxruntime_go 生态薄弱 |
| MCP SDK | 官方参考实现 | `rmcp`（官方，较新） | 官方 go-sdk |
| 分发 | uv / PyPI；无需代码签名 | 单二进制。但从网络下载的二进制会被 Gatekeeper 隔离，**要正经分发就得 Apple 开发者签名 + 公证** | 同 Rust |
| 迁移成本 | 0 | 重写 17.8k 行业务代码，**外加 1,717 个用例积累的边界语义**（EA-xxx 系列修复、故障注入、差分测试） | 同 Rust，且嵌入生态更弱 |
| 平台问题 | 平台绑定来自业务，与语言无关 | 同左 | 同左 |

- **Rust**：真正的收益在分发（单二进制）和启动时间，不在吞吐。但安装链条里最费事的几步（Xcode CLT/lldb、
  `sudo codesign` 重签名微信、在微信里重新登录）**换任何语言都还在**；`brew install sqlcipher` 这一步用 Python
  也能去掉（P2）。用一次高风险重写换这点边际收益，不划算。
- **Go**：SQLCipher 需要 cgo，ONNX 生态弱。对本项目来说 Go 没有任何一项占优，**不推荐**。
- **PyO3 / Rust 扩展（混合）**：目前没有纯 Python 热点能证明它值得做（XOR 换成 `translate` 或大整数运算就能达到 C 速度）。
  **保留为应急选项**，触发条件见 §9。

## 6. 跨平台、构建、安装与分发

1. **`sqlcipher` CLI 是唯一的运行期外部二进制依赖**：要 Homebrew，CI 里也得 `brew install`，
   版本还会漂移（本机 CLI 是 4.20，wheel 内置 4.12，两者都能读 SQLCipher 4 格式，已在 message_4 上验证）。
   换成进程内绑定后，这一依赖可以降为「仅调试命令 `decrypt` / `inspect` 可选使用」。
2. **还没有发布到 PyPI**：目前靠 git clone + `uv sync` / `uv tool install --editable`。发布后可以
   `uv tool install agent-wechat` 或 `uvx --from agent-wechat agent-wechat-mcp`，MCP 配置也能随之简化。
3. **安装体积**：fastembed 及其依赖链占运行时安装面的大头（约 130 MB）加 91 MB 模型。语义检索本来就是
   「手动触发」能力，可以考虑拆成可选的 `[semantic]` extra，hybrid 已有无向量时的降级路径。**这会改变产品形态，需要你决定。**
4. **CI**：只在 `macos-15`（arm64）上跑。Intel Mac 宣称支持，但没有 CI 覆盖（`macos-13` 是 Intel runner）。
5. **Windows/Linux**：需要另起一条密钥获取路线（不同的进程注入和内存方案），与语言无关。如果将来要做，Python 依赖都有
   Windows wheel，`sqlcipher3-wheels` 也有；去掉 CLI 依赖本身就是在为跨平台铺路。

## 7. 技术选型建议

| 编号 | 建议 | 依据 | 风险 |
|---|---|---|---|
| **P1** | 去掉每进程一次的 10 s `tmutil addexclusion` 超时（先用 `isexcluded` 只读查询，已排除就跳过，或者只在新建目录时执行） | §3.1 | 低 |
| **P1** | 嵌入时把内部批次切小（如 `embed(..., batch_size=16)`），或加大 flush 缓冲并配小批次 | §3.4，向量等价已验证 | 低；最优值要在空闲机器上复测 |
| **P1** | 单字节 XOR 改用 `bytes.translate`；朋友圈 zip-XOR 改大整数 XOR | §3.5 | 低；改动纯粹是字节运算的等价替换 |
| **P2** | 解密层改为**进程内 SQLCipher 直接查询加密快照**（`sqlcipher3-wheels`），保留 CLI 作为回退和诊断手段；MCP 常驻进程按（库、salt）缓存派生密钥 | §3.2：1,540 ms → 11.7 ms；明文不落盘 | 中，见 §8 |
| **P3** | 首次或 `--full` 导出走「先灌正文 → 一次 FTS `rebuild`」批量路径，增量路径保持触发器不动 | §3.3：FTS 部分约快 3 倍 | 中；中断时的一致性要设计好 |
| **P4** | 发布到 PyPI，安装时不再需要 brew；CI 增加 Intel runner；可选 `[semantic]` extra | §6 | 低到中 |
| 不做 | 整体迁移到 Rust 或 Go | §4、§5 | — |

## 8. 风险评估

| 风险 | 等级 | 说明与缓解 |
|---|---|---|
| `sqlcipher3-wheels` 是**个人维护的 fork**（供应链） | 中 | `uv.lock` 锁定版本与哈希；保留 CLI 回退路径 1–2 个版本；用已有的差分测试框架（`tests/test_differential.py`）做「CLI 导出 vs 进程内查询」逐行等价比对；维护停滞时的退路是自建 wheel（例如很薄的 `rusqlite` PyO3 包装，只在这时才引入 Rust） |
| `sqlcipher3` 的异常类**不是** `sqlite3.Error` 的子类（已实测 `issubclass` 为 False） | 中 | `src/` 中有 73 处引用 `sqlite3.Error/OperationalError/DatabaseError`，其中读微信库的路径（reader/decrypt/moments/media）占 12 处。切换后这些分支会静默失效。需要在适配层统一翻译异常，并用故障注入测试覆盖。**这是 P2 最容易漏掉的一点** |
| `text_factory=bytes`、`Row` 等 DB-API 细节差异 | 低 | 适配层封装，差分测试兜底 |
| 小批次嵌入的最优值随机器和负载变化 | 低 | 取 16 这类保守值；在空闲机器上用 2 万条子集实测后再定 |
| FTS 批量重建中途被打断 | 中 | 落持久化标记（`sync_state`），重跑时检测并补 `rebuild`；`doctor` 加 FTS `integrity-check`；故障注入覆盖中断点；`secure-delete` 配置在重建后要保持开启 |
| tmutil 修复改变了「首次排除」的时机 | 低 | 以 `isexcluded` 的结果为准，与 `time_machine_excluded()` 同源，语义更准确 |
| 不迁移语言的长期风险 | 低 | Python 3.12+ 与依赖链都活跃；用 §9 的触发条件定期复核 |

另有一项观察（不属于语言问题）：sqlite-vec 的 `partition key` 会给每个会话分配独立的 chunk，
20 万向量的测试库达到 927 MB（约 4.6 KB/向量，原始向量只有 2 KB）。全量索引的磁盘占用值得在后续单独评估（例如调 `chunk_size`）。

## 9. 后续实施方案

每个阶段独立提交，必须过 `scripts/check.sh` 全门禁。

**Phase 01：快速修复（约 0.5–1 天，低风险）**
1. tmutil 修复 → 验证：`recent` 的 CLI 墙钟 < 3 s，MCP 首次调用不再多出 10 s
2. 嵌入批大小 → 验证：在空闲机器上对 2 万条子集跑 `vector-build`，吞吐 ≥ 3 倍；向量与旧批大小的余弦 ≥ 0.99999
3. XOR 等价替换 → 验证：性质测试（hypothesis）证明逐字节等价

**Phase 02：进程内 SQLCipher（约 2–3 天，中风险）**
1. 在 `decrypt.py` 增加适配层：`open_encrypted(snapshot, key) -> Connection`，统一异常翻译和 `text_factory`
2. `reader._open` 切到新路径；`username_pool` 的重复整库解密随之消失；按（库、salt）缓存派生密钥
3. CLI 路径保留给 `decrypt` / `inspect` 以及回退
4. 验证：差分测试逐行等价；`recent` < 0.6 s；导出里解密开销 < 1 s；`tmp/` 下不再出现明文 `plain.db`（复用 purge 的 tmp_leaks 探测）；同步更新 INSTALL/SECURITY 文档

**Phase 03：导出的 FTS 批量路径（约 2 天，中风险）**
1. 首次或 `--full` 导出：先停用 FTS 触发器灌数据，末尾做一次 `rebuild` 再恢复触发器，全程有持久化标记
2. 验证：全量导出墙钟下降 ≥ 30%；FTS `integrity-check` 通过；在中断点做故障注入后，重跑能自愈

**Phase 04：分发（约 1–2 天）**
1. 发布到 PyPI，支持 `uvx`；CI 增加 Intel runner 和「不装 brew sqlcipher」的安装冒烟测试
2. 是否拆 `[semantic]` extra：**需要你决定**

**Phase 05：复测与语言复核（条件触发）**

修完 P1–P3 后重新剖析。出现以下任一情况时，再评估 PyO3 扩展或局部 Rust：
- 解释器占全量导出墙钟 > 40%，或增量同步成为用户可感知的瓶颈；
- `sqlcipher3-wheels` 停止维护，需要自建绑定；
- 产品形态转向面向非开发者的独立 App（届时单二进制与签名分发的价值会变成主要矛盾）。

## 附：复现要点

- 剖析：在 `cli.main()` 外包一层 `cProfile`，跑 `recent` / `export`，导出用独立的 `AGENT_WECHAT_HOME`。
  基准中把 `paths._tm_exclusion_checked` 置真，用来隔离 tmutil 的影响
- 解密对比：同一快照分别执行 `sqlcipher_export` 与「`PRAGMA key` + 一条 `ORDER BY sort_seq DESC LIMIT 20`」
- 嵌入：从索引取真实文本，按 `doc_text` 构造，每 256 条排序，变化的只有 fastembed 的 `batch_size`
- FTS：把 `messages` 原样重灌，对比「触发器逐行」与「末尾 rebuild」
