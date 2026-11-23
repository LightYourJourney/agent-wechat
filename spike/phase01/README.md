# spike/phase01：Rust(PyO3) 对照实验（独立实验，不进 `src/`、不进发布包）

报告见 [docs/audit/phase-01-native-poc-report.md](../../docs/audit/phase-01-native-poc-report.md) §2.4 / §3.6，
Phase 03 复测见 [phase-03-acceptance-report.md](../../docs/audit/phase-03-acceptance-report.md)。

Phase 01 的其余原型（进程内 SQLCipher、tmutil、嵌入批、FTS 批量）已在 Phase 02 / 03 正式集成进
`src/`，原型脚本随之删除、未入库——它们猴补丁的接口集成后已经变了，留着只会给出错误的对照。
对应的正式实现：`decrypt.open_db`、`paths.ensure_home_dirs`、`vector.EMBED_BATCH`、
`store.fts_bulk_begin` / `fts_bulk_finish`。

| 文件 | 内容 |
|---|---|
| `native_decode/` | Rust 扩展：`decode_one` / `decode_rows`（zstd 第一帧 + UTF-8 + 群前缀），`noop_*` 量 FFI 地板 |
| `bench_native.py` | 全部消息分片逐行解码：Python 参考实现 vs Rust 两种调用粒度，外加 20 万例边界模糊测试 |
| `results/*.json` | Phase 01 本机一次运行的原始结果（`native.json` 之外的几份对应已删除的脚本，只作数据留档） |

```bash
# 需要 rustc；maturin 由 uvx 临时拉起。需要本机已登录微信 + 已 bootstrap。只输出计数、耗时与摘要哈希。
uvx maturin build --release -i python3.12 -m spike/phase01/native_decode/Cargo.toml \
    -o spike/phase01/native_decode/dist
uv run --with spike/phase01/native_decode/dist/*.whl python spike/phase01/bench_native.py
```
