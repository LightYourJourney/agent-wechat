"""B 变异器的 census / mutate / fingerprint（Task 11）。

判别性要求：每条断言都必须能因为**变异器本身的缺陷**而失败——只断言「有输出」的用例
在变异器把位点找错时照样绿（本仓已被这一类坑过多次）。
"""
import ast
import os
import subprocess
import sys

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


def test_identical_segments_in_one_function_get_distinct_fingerprints():
    """同形的两个位点（两处 `return None`）不许塌缩——实测真仓库 1235 个位点会塌成 1081 个。

    没有这条，kill 矩阵的「集合对等」断言就是在数一个已经合并过的集合。
    """
    src = "def f(a):\n    if a:\n        return None\n    return None\n"
    sites = iter_sites(src)
    fps = [fingerprint("m", s) for s in sites]
    assert len(set(fps)) == len(sites), {
        (s["qualname"], s["op"], s["segment"], fingerprint("m", s)) for s in sites}


def test_real_modules_have_no_fingerprint_collisions():
    """B 阶段的前提：模块清单里每个位点都有唯一指纹。"""
    from scripts.mutation_sweep import DEFAULT_MODULES, _src_root
    for m in DEFAULT_MODULES:
        sites = iter_sites((_src_root() / f"{m}.py").read_text(encoding="utf-8"))
        fps = [fingerprint(m, s) for s in sites]
        assert len(set(fps)) == len(fps), f"{m}: 指纹碰撞 {len(fps) - len(set(fps))} 处"


def test_classify_five_states():
    """五态互斥：超时单列（不并进 killed），收集/导入错误一律 invalid。"""
    from scripts.mutation_sweep import classify
    assert classify(1, "FAILED tests/x.py::t - AssertionError") == "killed"
    assert classify(1, "ERROR tests/x.py - ImportError") == "invalid"
    assert classify(2, "ERROR collecting tests/x.py") == "invalid"
    assert classify(0, "") == "survived"
    assert classify(-9, "") == "timeout"
    # 判据的边界：只提到 ERROR collecting 而非 FAILED 时不得算 killed
    assert classify(1, "ERROR collecting tests/x.py\nFAILED") == "invalid"
    # 第五态：pytest 内部错误（退出码 3）是**基建错误**，与「变异体被拦下」分开
    assert classify(3, "INTERNALERROR>") == "infra"


def test_failure_kind_uses_the_spec_vocabulary():
    """spec §6.3 的词表是封闭的六个值；台账里不许出现自造词（此前实有 `error`/
    `other-error` 两个不在词表里的值，共 263 行）。"""
    from scripts.mutation_sweep import failure_kind
    vocab = {"assert", "body-error", "setup-error", "collection-error",
             "import-error", "timeout", "-"}
    got = {
        failure_kind("killed", "FAILED tests/x.py::t - AssertionError"),
        failure_kind("killed", "FAILED tests/x.py::t - ValueError: boom"),
        failure_kind("invalid", "ERROR collecting tests/x.py"),
        failure_kind("invalid", "ERROR at setup of test_x"),
        failure_kind("invalid", "ModuleNotFoundError: No module named 'x'"),
        failure_kind("timeout", ""),
        failure_kind("survived", ""),
        failure_kind("infra", "INTERNALERROR>"),
    }
    assert got <= vocab, got - vocab
    assert failure_kind("killed", "FAILED t - AssertionError") == "assert"
    assert failure_kind("invalid", "ERROR collecting tests/x.py") == "collection-error"
    assert failure_kind("survived", "") == "-"


def test_runner_end_to_end_kills_and_restores(tmp_path):
    """真跑一次：变异被用例打死 = killed；源文件必须被还原；集合对等断言生效。

    用小模块 + 小用例，不碰真仓库——但走的是 `run_sweep` 的**同一条**路径
    （copy_root / PYTHONPATH / 还原 / 集合断言）。
    """
    from scripts.mutation_sweep import run_sweep

    pkg = tmp_path / "src" / "agent_wechat"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    mod_src = "def f(a):\n    return a + 1\n"
    (pkg / "tiny.py").write_text(mod_src)
    t = tmp_path / "tests"
    t.mkdir()
    (t / "test_tiny.py").write_text(
        "from agent_wechat.tiny import f\n\n"
        "def test_f():\n    assert f(1) == 2\n")
    # 先确认基线绿（否则分类器的判据没有意义）
    env = dict(os.environ, PYTHONPATH=str(tmp_path / "src"))
    base = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "tests"],
                          cwd=tmp_path, env=env, capture_output=True, text=True)
    assert base.returncode == 0, base.stdout + base.stderr

    from scripts.mutation_sweep import fingerprint, iter_sites
    fps = {fingerprint("tiny", s) for s in iter_sites(mod_src)}
    rows = run_sweep("tiny", {"tiny": ["tests"]}, tmp_path, 120, fps)
    assert {r["fp"] for r in rows} == fps, "集合对等"
    assert any(r["status"] == "killed" for r in rows), "把 +1 变成 +2 必须被用例打死"
    assert (pkg / "tiny.py").read_text() == mod_src, "变异源必须被还原"
