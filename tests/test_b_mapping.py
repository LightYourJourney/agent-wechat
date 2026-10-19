"""B0 的验收：冻结的映射表必须**真的**执行到它声称的模块（Task 13）。

两条断言各钉一头：
- 清单对等：`MAPPING` 的键 == 变异器的 `DEFAULT_MODULES`（加模块不许忘记加映射）；
- 行探针：每个映射用例都执行到该模块的代码行。

第三条是**负对照**——没有它，前两条即使探针整个坏掉（永远回 HIT）也会全绿。
"""
from b_mapping import MAPPING, verify_mapping
from scripts.mutation_sweep import DEFAULT_MODULES


def test_mapping_covers_every_frozen_module():
    assert set(MAPPING) == set(DEFAULT_MODULES), "模块清单与映射表不同步"
    assert all(v for v in MAPPING.values()), "空映射等于没映射"


def test_mapping_reaches_every_module():
    bad = {m: verify_mapping(m) for m in MAPPING}
    bad = {m: v for m, v in bad.items() if v}
    assert not bad, f"这些用例没执行到对应模块的代码行：{bad}"


def test_probe_can_report_a_miss():
    """负对照：把 decrypt 映射到一个不碰它的用例，探针必须报「没命中」。

    探针若退化成恒真（比如 `settrace` 没装上、或路径比较永远不等/永远相等而
    提前 return 0），上面那条 `test_mapping_reaches_every_module` 就变成空转——
    本仓对「假护栏」已有 12 次记档，这里先把它钉死。
    """
    assert verify_mapping("decrypt", {"decrypt": ["tests/test_errors.py"]}) == [
        "tests/test_errors.py"]
