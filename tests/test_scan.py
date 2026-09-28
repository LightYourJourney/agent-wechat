"""`security/scan.py` 的测试。

两条贯穿全篇的判据：
  1. **输出里绝不出现命中值本身**——扫描器一旦打印真标识，输出就进了 Agent 会话
     记录、落进 ~/.claude/projects，堵出口的工具自己成了出口（spec §4.4）。
  2. **fail-closed**——扫描器自身出错必须非 0 退出，且与「发现泄漏」用不同退出码
     （0 干净 / 1 有命中 / 2 出错）。
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCAN_PATH = ROOT / "security" / "scan.py"


def _load():
    spec = importlib.util.spec_from_file_location("scan_under_test", SCAN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def scan():
    return _load()


# 一条**伪造的** wxid。绝不用真标识（spec §7.4）。
FAKE = "wxid_qz7x2m4k9p1v3n8b"


def test_patterns_match_the_three_shapes(scan):
    shapes = {s for _, _, s, _ in scan.hits_in_text(
        "a " + FAKE + " b 12345678901@chatroom c 13800138000 d")}
    assert shapes == {"wxid", "chatroom", "phone"}, "三种形状都要抓到"


def test_wxid_requires_four_chars_after_prefix(scan):
    assert scan.hits_in_text("wxid_abc") == [], "wxid_ 后不足 4 字符不该命中"
    assert [h[2] for h in scan.hits_in_text("wxid_abcd")] == ["wxid"]


def test_chatroom_requires_six_digits(scan):
    assert scan.hits_in_text("12345@chatroom") == [], "不足 6 位不算群 id"
    assert [h[2] for h in scan.hits_in_text("123456@chatroom")] == ["chatroom"]


def test_phone_boundaries(scan):
    assert scan.hits_in_text("1380013800") == [], "10 位不该命中"
    assert [h[2] for h in scan.hits_in_text("13800138000")] == ["phone"]
    assert scan.hits_in_text("12800138000") == [], "第 2 位不在 3-9 不该命中"


def test_hex32_is_not_a_pattern(scan):
    """[0-9a-f]{32} 有 2786 个 distinct，是任何 md5，纯噪声——spec §4.2 明确排除。"""
    assert scan.hits_in_text("d41d8cd98f00b204e9800998ecf8427e") == []


def test_output_never_contains_the_value(scan, tmp_path, monkeypatch, capsys):
    """本设计里最该被钉死的不变量（spec §4.4）。"""
    (tmp_path / "x.txt").write_text("hit " + FAKE + " end\n", encoding="utf-8")
    monkeypatch.setattr(scan, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(scan, "_tracked_files", lambda: ["x.txt"])
    monkeypatch.setattr(scan, "ALLOWLIST_PATH", tmp_path / "allowlist.txt")

    rc = scan.main(["--all"])
    out = capsys.readouterr()

    assert FAKE not in out.out, "命中值出现在 stdout 里了"
    assert FAKE not in out.err, "命中值出现在 stderr 里了"
    assert rc == scan.EXIT_HITS
    assert "x.txt:1" in out.out, "但要给出位置"


def test_missing_git_is_fail_closed(scan, monkeypatch, capsys):
    """Review Focus 5：GitHub Desktop 的 stripped git 环境里可能没有 git。"""
    monkeypatch.setattr(scan.shutil, "which", lambda _name: None)
    rc = scan.main(["--all"])
    assert rc == scan.EXIT_ERROR, "找不到 git 必须退出 2，不能崩栈、也不能放行"
    assert "git" in capsys.readouterr().err


def test_non_scanner_error_is_fail_closed(scan, monkeypatch, capsys):
    """任何**非 `ScannerError`** 的内部错误也必须退出 2（不是 1）。

    只捕获 `ScannerError` 时，其他异常会以**退出码 1** 逃逸——而 1 是契约留给
    「发现命中」的码，于是 0 干净 / 1 命中 / 2 扫描器出错 这个**区分会坍缩**，
    而那正是本任务存在的目的（spec §4.5）。这条测试就是它的护栏。
    """
    def boom(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(scan, "_git", boom)
    rc = scan.main(["--all"])

    assert rc == scan.EXIT_ERROR, "非 ScannerError 的崩溃也必须退出 2"
    assert "扫描器出错" in capsys.readouterr().err, "消息要点明是扫描器出错，不是发现泄漏"


def test_values_view_is_per_value_not_per_occurrence(scan, tmp_path, monkeypatch, capsys):
    """spec §4.6 的归类是逐**值**的（90 个），不是逐处的。"""
    (tmp_path / "a.txt").write_text("x " + FAKE + " y\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text(FAKE + "\n" + FAKE + "\n", encoding="utf-8")
    monkeypatch.setattr(scan, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(scan, "_tracked_files", lambda: ["a.txt", "b.txt"])
    monkeypatch.setattr(scan, "ALLOWLIST_PATH", tmp_path / "allowlist.txt")

    rc = scan.main(["--values"])
    out = capsys.readouterr().out

    assert FAKE not in out, "逐值视图同样不许打印值"
    assert out.count(scan.fingerprint(FAKE)) == 1, "同一个值只该有一行"
    assert "3 次" in out, "要给出出现次数"
    assert rc == scan.EXIT_HITS


def test_repo_scope_comes_from_git(scan, monkeypatch):
    """作用域由 `git ls-files` 决定——`.gitignore` 掉的东西天然不进（spec §4.4 作用域说明）。

    这条钉的是**机制**而不是结果：如果有人哪天把枚举改成自己 os.walk 目录树，
    `.venv/`、`node_modules/` 这些会被扫进来，而这条会红。
    （原先这里写的是「把 _tracked_files 打桩成 []，断言 collect_values() == {}」——
    没有文件当然没有命中，那条断言**永远不可能为它名字宣称的东西而失败**。）
    """
    calls = []
    monkeypatch.setattr(scan, "_git", lambda *a: (calls.append(a), "a.py\0b.py\0")[1])
    assert scan._tracked_files() == ["a.py", "b.py"]
    assert calls == [("ls-files", "-z")], "枚举必须走 git，不许自己去 walk 目录树"
