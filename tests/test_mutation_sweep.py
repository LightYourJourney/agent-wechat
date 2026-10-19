"""B 变异器的 census / mutate / fingerprint（Task 11）。

判别性要求：每条断言都必须能因为**变异器本身的缺陷**而失败——只断言「有输出」的用例
在变异器把位点找错时照样绿（本仓已被这一类坑过多次）。
"""
import ast

from scripts.mutation_sweep import fingerprint, iter_sites, mutate

SRC = '''
def f(a, b):
    if a < b and b:
        return a + 1
    return 0
'''


def test_census_covers_expression_level_sites():
    ops = {s["op"] for s in iter_sites(SRC)}
    assert {"cmp_boundary", "bool_op", "const", "delete_stmt"} <= ops


def test_mutation_changes_behavior():
    s = next(x for x in iter_sites(SRC) if x["op"] == "cmp_boundary")
    out = mutate(SRC, s)
    assert out != SRC
    ast.parse(out)
    # 不只是「变了」：**行为**必须变。f(1, 1) 在 `<` 下是 0，在 `<=` 下是 2。
    ns_src, ns_mut = {}, {}
    exec(SRC, ns_src)          # noqa: S102 — 测试语料是本文件里的常量字符串
    exec(out, ns_mut)          # noqa: S102
    assert ns_src["f"](1, 1) != ns_mut["f"](1, 1)


def test_bool_op_mutation_flips_operator():
    s = next(x for x in iter_sites(SRC) if x["op"] == "bool_op")
    out = mutate(SRC, s)
    ns = {}
    exec(out, ns)              # noqa: S102
    # `and` → `or`：f(-5, 0) 里 `a < b` 为真而 `b` 为假 —— `and` 得 0，`or` 得 -4。
    # （取 a<b 且 b 假的那个角落：否则两种算子的结果一样，这条用例就空转了。）
    assert ns["f"](-5, 0) == -4


def test_delete_stmt_mutation_removes_the_return():
    sites = [x for x in iter_sites(SRC) if x["op"] == "delete_stmt"]
    assert len(sites) == 2, "两处 return 都该被枚举"
    out = mutate(SRC, sites[0])          # 第一个 return → pass
    ns = {}
    exec(out, ns)              # noqa: S102
    assert ns["f"](1, 2) == 0, "被删掉的那个分支必须落空（而不是照样返回）"


def test_fingerprint_stable_across_line_shift():
    a = fingerprint("m", next(iter(iter_sites(SRC))))
    b = fingerprint("m", next(iter(iter_sites("\n\n" + SRC))))
    assert a == b


def test_fingerprint_differs_for_different_sites():
    sites = iter_sites(SRC)
    fps = {fingerprint("m", s) for s in sites}
    assert len(fps) == len(sites), "不同位点的指纹必须不同（否则 dry-run 集合会塌缩）"
