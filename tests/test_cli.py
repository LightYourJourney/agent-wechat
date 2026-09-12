"""CLI 入口策略的回归护栏。

缺陷背景：`-n` 的非正值会一路传到 SQL 而不被拦下——`reader.recent` 的
`LIMIT -1` 在 SQLite 里表示「不限量」，取回后再被 `pieces[-limit:]` 错位切片，
于是 `-n -1` **静默丢掉一条**消息、`-n 0` 返回空；`search` 则退化成无上限全表
扫描。归一化现已收进 usecases（见 tests/test_usecases.py），CLI 只负责渲染。
"""
from types import SimpleNamespace

import pytest

from agent_wechat import account, bootstrap, cli, keys, reader, search, vector


@pytest.fixture()
def captured(monkeypatch):
    """替掉 MessageReader 与账号解析，捕获传给核心函数的 limit。"""
    seen: dict = {}

    class _StubReader:
        def __init__(self, acct):
            self.acct = acct

        def resolve_one(self, contact):
            return ("wxid_target", [])

        def recent(self, chat_id, limit=50):
            seen["recent_limit"] = limit
            return []

        def display_name(self, chat_id):
            return "张三"

    monkeypatch.setattr(reader, "MessageReader", _StubReader)
    monkeypatch.setattr(account, "resolve_account", lambda a: "acct")
    return seen


def test_recent_clamps_non_positive_limit(captured):
    for n in (-1, 0):
        cli.cmd_recent(SimpleNamespace(account=None, contact="张三", n=n))
        assert captured["recent_limit"] == 1, f"-n {n} 未被夹到 1"


def test_recent_clamps_large_limit_to_500(captured):
    """C2 起 CLI 也夹上界：`-n 1000` 与 MCP 的 1-500 对齐（有意的行为变更）。"""
    cli.cmd_recent(SimpleNamespace(account=None, contact="张三", n=1000))
    assert captured["recent_limit"] == 500


def test_search_clamps_non_positive_limit(monkeypatch):
    seen: dict = {}

    def fake_search(query, **kwargs):
        seen["limit"] = kwargs["limit"]
        return {"mode": "fts", "total": 0, "hits": []}

    monkeypatch.setattr(account, "resolve_account", lambda a: "acct")
    monkeypatch.setattr(search, "search", fake_search)
    for n in (-1, 0):
        cli.cmd_search(SimpleNamespace(account=None, contact=None, query="会议", n=n))
        assert seen["limit"] == 1, f"-n {n} 未被夹到 1"


# ---------- C1：统一错误契约（core 抛项目异常，CLI 翻转成 stderr + 退出码 1） ----------

def test_main_prints_project_error_to_stderr_and_exits_1(monkeypatch, capsys):
    """错误路径必须是单行中文 + 退出码 1，且**不得**出现 Traceback。"""
    monkeypatch.setattr(account.paths, "account_dirs", lambda: [])
    with pytest.raises(SystemExit) as ei:
        cli.main(["contacts", "--account", "wxid_missing_0000"])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "账号目录不存在" in err and "wxid_missing_0000" in err
    assert "Traceback" not in err


def test_main_keeps_argparse_exit_code_2():
    """退出码维持 0/1；argparse 自己的参数错（SystemExit(2)）必须原样保留。"""
    with pytest.raises(SystemExit) as ei:
        cli.main(["definitely-not-a-subcommand"])
    assert ei.value.code == 2


def test_main_success_still_exits_0(monkeypatch):
    class _StubReader:
        def __init__(self, acct):
            pass

        def contacts(self, query=None, limit=200):
            return []

    monkeypatch.setattr(account, "resolve_account", lambda a: "acct")
    monkeypatch.setattr(reader, "MessageReader", _StubReader)
    with pytest.raises(SystemExit) as ei:
        cli.main(["contacts"])
    assert ei.value.code == 0


def test_semantic_core_error_reaches_stderr_with_hint(monkeypatch, capsys):
    """semantic_search 现在抛异常而非返回 error dict；CLI 的翻转必须含 hint 单行输出。"""
    monkeypatch.setattr(account, "resolve_account", lambda a: "acct")

    def boom(*a, **k):
        raise vector.VectorError("向量索引为空", hint="请先运行 build_vector_index")

    monkeypatch.setattr(vector, "semantic_search", boom)
    with pytest.raises(SystemExit) as ei:
        cli.main(["semantic", "爬山"])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "向量索引为空（请先运行 build_vector_index）" in err
    assert "Traceback" not in err


# ---------- C8：bootstrap 的 print 迁到 CLI，四处输出逐字（同序/同流向 stdout） ----------

def test_cmd_bootstrap_output_is_verbatim(monkeypatch, capsys):
    """跑真实 bootstrap 编排（仅桩掉账号/捕获/校验/落盘），断言 CLI 的完整 stdout。"""
    monkeypatch.setattr(
        bootstrap.account, "status",
        lambda: SimpleNamespace(logged_in=True, current="wxid_testacct01_ab12", pid=999))
    monkeypatch.setattr(bootstrap, "capture_passphrase_lldb", lambda timeout: "ab" * 32)
    monkeypatch.setattr(
        bootstrap.keys, "verify_passphrase",
        lambda acct, pw: [("m/message_0.db", True)] * 3 + [("m/message_1.db", False)] * 2)
    saved: dict = {}
    monkeypatch.setattr(bootstrap.keys, "save_cached_hex",
                        lambda bare, h: saved.update(bare=bare, h=h))

    with pytest.raises(SystemExit) as ei:
        cli.main(["bootstrap", "--timeout", "7"])
    assert ei.value.code == 0

    out = capsys.readouterr()
    assert out.err == ""                      # 全部流向 stdout
    assert out.out == (
        "当前登录账号: wxid_testacct01 (pid=999)\n"
        "正在 attach lldb… 请在微信内【退出登录 → 重新登录】以触发密钥派生。\n"
        f"✅ HMAC 校验通过 3/5 个库，passphrase 已缓存到 {keys.CACHE_FILE}\n"
        "账号 wxid_testacct01 引导完成。之后所有操作均无需 sudo。\n"
    )
    assert saved == {"bare": "wxid_testacct01", "h": "ab" * 32}
