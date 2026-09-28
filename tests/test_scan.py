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
