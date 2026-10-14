"""`scripts/check_vulture.py` 的测试。

这个文件的存在理由：这条判据在 2026-10-14 一天里错了两次，两次都只在「工具在场/缺席」
变化时才现形——工具正常时的输出与「这一步根本没跑」在屏幕上长得一模一样。

  ① 取不到 vulture 时输出为空，旧判据当成「没有候选」，印了一个 ✅ 并真放行过一次 push；
  ② 修完 ① 之后，又把 vulture 的 **rc=3（＝找到死代码，它的正常语义）**当成「工具坏了」，
     于是门禁在工具完全正常时恒红。

两次都不是引号或 `comm` 的用法问题，是**判据本身**没人验——而它当时写在 shell 里，
没法被 pytest 钉住。所以它按 ① 的规格做成了脚本，这里钉住它的三条判据：

  · 工具的**退出码契约**（0/3 正常，其余＝这步没跑起来）；
  · **行号不进身份**、归一化后**去重**（否则同文件里的重复候选会被集合差当成「新增」）；
  · 新增＝失败且点名，消失＝只是提示。

**真路径用例真的去跑 vulture**（它已是 `pyproject.toml` 的 dev 依赖），不桩掉那次调用：
被桩掉的那次调用恰好就是两次事故所在的地方。
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GATE_PATH = ROOT / "scripts" / "check_vulture.py"


def _load():
    spec = importlib.util.spec_from_file_location("vulture_gate_under_test", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def gate():
    return _load()


def _has_vulture() -> bool:
    """vulture 是 dev 依赖；缺了要**说清楚**，不能静默 skip 掉整份判据。

    这正是被测的那类错误在测试里的翻版：把「没跑」呈现成「通过」。
    所以这个 skip 只挂在那几条**真的要跑 vulture** 的用例上——纯判据（归一化、退出码契约、
    集合差）不依赖它，vulture 缺了也该照跑。
    """
    return importlib.util.find_spec("vulture") is not None


needs_vulture = pytest.mark.skipif(
    not _has_vulture(),
    reason="vulture 不在解释器里（dev 依赖未安装）——跑一次 `uv sync`",
)


# 探针模块的**真实形态**（2026-10-14 用真 vulture 实测过，不是推想的）：同一文件里对
# 同一属性赋两次值，vulture 会逐节点报两条 `unused attribute 'x'`，归一化后逐字相同。
PROBE = '''\
class C:
    def __init__(self):
        self.x = 1
        self.x = 2
'''


@pytest.fixture()
def probe_src(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "m.py").write_text(PROBE)
    return src


def test_normalize_drops_the_line_number_so_a_moved_line_is_the_same_entry(gate):
    a = gate.normalize("src/a.py:12: unused function 'f' (60% confidence)")
    b = gate.normalize("src/a.py:1408: unused function 'f' (60% confidence)")
    assert a == b == "src/a.py:: unused function 'f' (60% confidence)"


def test_normalize_drops_the_repo_root_prefix_so_cwd_does_not_change_the_entry(gate):
    """vulture 印的是**相对 CWD** 的路径（实测）：同一棵树，从仓根跑印 `src/agent_wechat/x.py`，
    从别处跑印绝对路径。仓根前缀不进身份——否则换个目录调用，整份基线都成了「新增」。
    """
    absolute = gate.normalize(f"{gate.ROOT}/src/a.py:12: unused function 'f' (60% confidence)")
    assert absolute == "src/a.py:: unused function 'f' (60% confidence)"


@needs_vulture
def test_the_same_candidate_twice_in_one_file_is_one_entry(gate, probe_src):
    rc, raw, err = gate.run_vulture(probe_src)
    assert rc == 3, err
    assert sum("unused attribute 'x'" in line for line in raw) == 2, (
        "探针形态变了：vulture 不再对同名两次赋值报两条，这条用例的『去重』断言会变成空转"
    )
    now = gate.entries(raw)
    assert sum("unused attribute 'x'" in e for e in now) == 1


@needs_vulture
def test_dead_code_found_is_vultures_normal_exit_code_not_a_tool_failure(gate, probe_src, capsys):
    rc, raw, _ = gate.run_vulture(probe_src)
    assert rc == 3
    assert gate.report(rc, gate.entries(raw), base=set()) in (0, 1), (
        "rc=3 被当成了「这步没跑」——门禁会在工具完全正常时恒红"
    )
    assert "跑不起来" not in capsys.readouterr().err


def test_a_candidate_outside_the_baseline_is_named_and_fails(gate, capsys):
    now = {"src/a.py:: unused function 'f' (60% confidence)",
           "src/b.py:: unused variable 'v' (60% confidence)"}
    base = {"src/a.py:: unused function 'f' (60% confidence)"}
    assert gate.report(3, now, base) == 1
    err = capsys.readouterr().err
    assert "src/b.py:: unused variable 'v' (60% confidence)" in err
    assert "src/a.py::" not in err          # 存量条目不该被当成新增点名


def test_a_clean_run_against_a_matching_baseline_passes(gate, capsys):
    base = {"src/a.py:: unused function 'f' (60% confidence)"}
    assert gate.report(3, set(base), base) == 0
    out = capsys.readouterr().out
    assert "✅ 没有基线外的候选" in out


def test_a_baseline_entry_that_vanished_is_a_hint_not_a_failure(gate, capsys):
    base = {"src/a.py:: unused function 'f' (60% confidence)"}
    assert gate.report(0, set(), base) == 0
    out = capsys.readouterr().out
    assert "src/a.py:: unused function 'f' (60% confidence)" in out


@pytest.mark.parametrize("rc", [1, 2, 127])
def test_an_exit_code_outside_vultures_contract_means_the_step_did_not_run(gate, capsys, rc):
    """vulture 自己的 README：0 无死代码 / 1 输入有问题 / 2 参数错 / 3 找到死代码。

    只有 0 与 3 是「它真的跑完了」。其余一律判「这步没跑起来」（rc=2），
    **且不许印 ✅**——空输出曾经就是靠一个 ✅ 蒙混过去的。
    """
    code = gate.report(rc, {"src/a.py:: unused function 'f' (60% confidence)"}, set())
    assert code == 2
    captured = capsys.readouterr()
    assert "✅" not in captured.out
    assert f"退出码 {rc}" in captured.err


@needs_vulture
def test_the_shipped_baseline_matches_the_shipped_tree(gate, capsys):
    """零漂移：仓里的 `src/` 与仓里的基线，用装好的这把 vulture 量出来必须一致。

    这条同时是**去重的变异杀手**：去掉归一化后的去重，`media.py` 那条重复候选
    （1162/1490 两处 `text_factory`）就会以「新增」出现，这里立刻红。
    """
    assert gate.main() == 0
    out = capsys.readouterr().out
    assert "✅ 没有基线外的候选" in out


@needs_vulture
def test_a_run_from_another_directory_is_the_same_judgment(gate, tmp_path, monkeypatch, capsys):
    """从**别的目录**跑这个脚本，结论必须一样。

    这是真实会发生的用法（`python /path/to/scripts/check_vulture.py`、编辑器任务、别的钩子），
    而 vulture 的路径是相对 CWD 印的——不剥仓根前缀的话，这里会冒出 28 条幻影「新增」。
    """
    monkeypatch.chdir(tmp_path)
    assert gate.main() == 0
    assert "✅ 没有基线外的候选" in capsys.readouterr().out
