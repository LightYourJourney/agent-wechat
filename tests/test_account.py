"""account 的接口测试：解析纯函数 + 进程输出的注入边界。

C8 目标：登录态判定（lsof 自由文本解析）此前与 `subprocess.run` 缠在一起，
只能靠真机验证；现把两处解析提成纯函数，进程调用收到模块级可替换点 `account._run`。

红线：本文件所有"有进程"路径一律走桩 `_run`，绝不真的起 pgrep/lsof。
"""
import subprocess
from types import SimpleNamespace

import pytest

from agent_wechat import account

ACC = "wxid_testacct01_ab12"


def _lsof(path: str) -> str:
    """一行形如真实 lsof 输出的记录；NAME 列是打开文件的绝对路径。"""
    return f"WeChat  1234 user  12r  REG  1,2  0  12345  {path}"


def _stdout(text: str) -> SimpleNamespace:
    return SimpleNamespace(stdout=text)


# ---------- _parse_account_from_lsof ----------

def test_parse_lsof_unique_hit():
    out = "\n".join([
        _lsof("/Applications/WeChat.app/Contents/MacOS/WeChat"),
        _lsof(f"/Users/you/Library/Containers/com.tencent.xinWeChat/Data/Documents/"
              f"xwechat_files/{ACC}/db_storage/message/message_0.db"),
        _lsof("/usr/lib/dyld"),
    ])
    assert account._parse_account_from_lsof(out) == ACC


def test_parse_lsof_same_account_many_lines_is_still_unique():
    """同一账号的 230 个 fd 全部命中同一目录名，仍是唯一命中（真实常态）。"""
    out = "\n".join(_lsof(f".../xwechat_files/{ACC}/db_storage/message/message_{i}.db")
                    for i in range(5))
    assert account._parse_account_from_lsof(out) == ACC


def test_parse_lsof_multiple_accounts_returns_none():
    out = "\n".join([
        _lsof(f".../xwechat_files/{ACC}/db_storage/a.db"),
        _lsof(".../xwechat_files/wxid_other2222_ab12/db_storage/a.db"),
    ])
    assert account._parse_account_from_lsof(out) is None


def test_parse_lsof_no_hit_returns_none():
    assert account._parse_account_from_lsof("") is None
    assert account._parse_account_from_lsof(_lsof("/tmp/x.db")) is None


def test_parse_lsof_ignores_non_account_dirs_under_xwechat_files():
    """xwechat_files/all_users/login/<裸wxid> 这类路径首段不是账号目录，不得误判。"""
    out = "\n".join([
        _lsof(".../xwechat_files/all_users/login/wxid_testacct01/key_info.dat"),
        _lsof(".../xwechat_files/all_users/login/wxid_other2222/key_info.db"),
    ])
    assert account._parse_account_from_lsof(out) is None


def test_parse_lsof_account_name_without_trailing_slash():
    """NAME 列恰好停在账号目录处（无后续 /）也要能取到。"""
    out = _lsof(f".../xwechat_files/{ACC}")
    assert account._parse_account_from_lsof(out) == ACC


def test_parse_lsof_rejects_uppercase_instance_suffix():
    """ACCOUNT_RE 只认小写 hex4 后缀；大写是既有契约，不在此放宽。"""
    out = _lsof(".../xwechat_files/wxid_testacct01_AB12/db_storage/a.db")
    assert account._parse_account_from_lsof(out) is None


# ---------- _parse_pids ----------

def test_parse_pids_multiple_and_junk():
    assert account._parse_pids("1234\n5678\n") == [1234, 5678]
    assert account._parse_pids("abc 42 x\n") == [42]
    assert account._parse_pids("") == []
    assert account._parse_pids("\n  ") == []


# ---------- wechat_pid：注入 _run ----------

def test_wechat_pid_uses_module_run_and_returns_first(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _stdout("1234\n5678\n")

    monkeypatch.setattr(account, "_run", fake_run)
    assert account.wechat_pid() == 1234
    assert seen["argv"] == ["pgrep", "-x", "WeChat"]


def test_wechat_pid_none_when_not_running(monkeypatch):
    monkeypatch.setattr(account, "_run", lambda *a, **k: _stdout(""))
    assert account.wechat_pid() is None


def test_wechat_pid_ignores_abnormal_output(monkeypatch):
    """pgrep 报错文字落到 stdout 时，非数字 token 一律忽略。"""
    monkeypatch.setattr(account, "_run", lambda *a, **k: _stdout("pgrep: no matching processes\n"))
    assert account.wechat_pid() is None


def test_wechat_pid_passes_a_timeout_and_degrades_to_none(monkeypatch):
    """EA-052：pgrep 此前没有 `timeout=`（同文件的 lsof 是 15s）——系统卡顿时会一直挂住。

    超时与「pgrep 报错」同义：判不出来的 pid 就是 None。`TimeoutExpired` 不加处理会穿透
    两个适配层（CLI 一整个 traceback / MCP 落 generic_payload）。删掉 except 那条分支
    第二条断言必红；删掉 `timeout=` 第一条必红。
    """
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return _stdout("1234\n")

    monkeypatch.setattr(account, "_run", fake_run)
    assert account.wechat_pid() == 1234
    assert seen["timeout"] == 15

    def timed_out(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(account, "_run", timed_out)
    assert account.wechat_pid() is None


# ---------- current_account：注入 _run ----------

def test_current_account_parses_stdout(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["timeout"] = kwargs.get("timeout")
        return _stdout(_lsof(f".../xwechat_files/{ACC}/db_storage/a.db"))

    monkeypatch.setattr(account, "_run", fake_run)
    assert account.current_account(4321) == ACC
    assert seen["argv"] == ["lsof", "-n", "-P", "-p", "4321"]
    assert seen["timeout"] == 15


def test_current_account_timeout_returns_none(monkeypatch):
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="lsof", timeout=15)

    monkeypatch.setattr(account, "_run", fake_run)
    assert account.current_account(4321) is None
