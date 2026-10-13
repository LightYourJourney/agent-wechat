"""C1 错误契约护栏：core 只抛 AgentWeChatError 子类，绝不让 SystemExit 逃逸。

背景：`SystemExit` 不是 `Exception` 的子类，而 MCP SDK 的兜底是 `except Exception`
（mcp/server/mcpserver/tools/base.py），实测逃出的 SystemExit 会杀死常驻 stdio
server。下面头两条断言就是这条命脉的护栏。
"""
import sqlite3
from pathlib import Path

import pytest

from agent_wechat import account, errors

SRC = Path(__file__).resolve().parents[1] / "src" / "agent_wechat"


def test_resolve_account_explicit_unknown_raises_account_not_found(monkeypatch):
    monkeypatch.setattr(account.paths, "account_dirs", lambda: ["wxid_real_0000"])
    with pytest.raises(errors.AccountNotFound) as ei:
        account.resolve_account("wxid_missing_9999")
    assert isinstance(ei.value, Exception)        # 必须被 except Exception 接得住
    assert not isinstance(ei.value, SystemExit)   # 绝不能是会杀死 server 的 SystemExit
    assert "账号目录不存在" in str(ei.value)


def test_resolve_account_unresolvable_current_raises_account_not_found(monkeypatch):
    st = account.AccountStatus(running=False, current=None, accounts=[], pid=None)
    monkeypatch.setattr(account, "status", lambda: st)
    with pytest.raises(errors.AccountNotFound) as ei:
        account.resolve_account(None)
    assert isinstance(ei.value, Exception) and not isinstance(ei.value, SystemExit)
    assert "微信未运行" in str(ei.value)


def test_all_project_errors_share_one_base():
    """适配层只 except 基类即可覆盖全部项目错误；且都仍是 RuntimeError
    （既有 pytest.raises(RuntimeError) 断言继续成立）。

    EA-006：断言**全集不变量**（子类集合从模块派生），不是 C1 时刻的白名单快照——
    白名单版对「新增一个忘记继承基类的错误类」不会红（实测：C1 之后新增的 8 个子类
    全部漏在名单外）。把任一类临时改成继承 `Exception`，本用例立刻红（正控）。
    """
    derived = {n for n, c in vars(errors).items()
               if isinstance(c, type) and issubclass(c, errors.AgentWeChatError)
               and c is not errors.AgentWeChatError}
    # 双向夹逼：派生集合必须**恰好**等于名册。纯派生（不做这一步）有个洞——
    # 某类改成继承 `Exception` 时它只是从集合里**消失**，断言照样绿（实测踩过）；
    # 名册相等让「丢基类」与「新增未登记」两种漂移都红。
    expected = {
        "AccountNotFound", "CaptureError", "ContactNotFound", "DataNotFound",
        "DecryptError", "DependencyMissing", "HomeUnavailable", "IndexUnavailable",
        "KeyNotFound", "MediaKeyError", "MediaOutputError", "PurgeRefused",
        "SchemaDrift", "SearchInputError", "SnsMediaError", "TaskNotFound",
        "VectorError", "VectorIndexEmpty",
    }
    assert derived == expected, {
        "丢了基类": sorted(expected - derived), "新增未登记": sorted(derived - expected)}
    for name in sorted(derived):
        assert issubclass(getattr(errors, name), RuntimeError), name


def test_to_payload_and_to_text_shapes():
    assert errors.to_payload(errors.VectorError("索引为空")) == {"error": "索引为空"}
    assert errors.to_payload(errors.VectorError("不一致", hint="重建")) == {
        "error": "不一致", "hint": "重建"}
    assert errors.to_text(errors.VectorError("不一致", hint="重建")) == "不一致（重建）"
    assert errors.to_text(errors.VectorError("不一致")) == "不一致"


def test_core_never_raises_systemexit():
    """源码级护栏（F3，T8 复审：改 AST 面）：core 任何模块都不许让 `SystemExit` 逃出。

    旧版是**文本扫描**（数 `raise SystemExit` 字面量），两个洞：`sys.exit(3)` 逃逸
    ——运行时同样是 `SystemExit`，正是 EA-027 触发条件点名的形态；反向，注释/docstring
    里写这个字面量反而误红（批内自测当场踩过）。AST 只认真实语法节点。

    唯一允许的抛出点是 `cli.py` 的退出漏斗。（`exit()`/`quit()` 这对内建不在判据里：
    全仓零使用，且它们是 site 注入的、静态分析不可靠。）
    """
    import ast

    offenders: dict[str, int] = {}
    for p in sorted(SRC.glob("*.py")):
        hits = 0
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Raise):
                exc = node.exc
                target = exc.func if isinstance(exc, ast.Call) else exc
                if isinstance(target, ast.Name) and target.id == "SystemExit":
                    hits += 1
            elif isinstance(node, ast.Call):
                f = node.func
                if (isinstance(f, ast.Attribute) and f.attr == "exit"
                        and isinstance(f.value, ast.Name) and f.value.id == "sys"):
                    hits += 1
        if hits:
            offenders[p.name] = hits
    assert offenders == {"cli.py": 1}, f"SystemExit 只允许出现在 cli.main: {offenders}"


def test_core_does_not_import_mcp_sdk():
    """硬约束：除适配层 mcp_server.py 外，core 不得 import MCP SDK。"""
    bad = []
    for p in sorted(SRC.glob("*.py")):
        if p.name == "mcp_server.py":
            continue
        if any(line.strip().startswith(("import mcp", "from mcp"))
               for line in p.read_text().splitlines()):
            bad.append(p.name)
    assert bad == []


# ---------- 错误文案脱敏（硬约束：密钥不进错误信息）----------

def test_redact_strips_key_shaped_hex():
    """decrypt 把派生密钥拼成 PRAGMA 参数交给 sqlcipher，而 sqlcipher 报语法错时会
    回显参数原文（已实测）——脱敏就是为了堵住这条通往客户端信封的路。"""
    key = "a1b2c3d4" * 8                       # 32 字节密钥的 hex 形态，恰好 64 位
    msg = f"""Parse error in 1st command line argument: PRAGMA key = "x'{key}'";"""
    out = errors.redact(msg)
    assert key not in out
    assert "<已脱敏>" in out


def test_redact_keeps_md5_length_ids():
    """32 位 hex（md5 / chat_md5 / md5: 退化 id）不是密钥，必须原样保留。"""
    md5hex = "e5be5159" * 4
    assert errors.redact(f"chat_md5={md5hex}") == f"chat_md5={md5hex}"


def test_redact_truncates_overlong_text():
    out = errors.redact("z" * 1000)
    assert len(out) == 301 and out.endswith("…")


def test_payload_and_text_are_both_redacted():
    key = "deadbeef" * 8
    exc = errors.DecryptError(f"""sqlcipher 解密失败: PRAGMA key = "x'{key}'";""")
    assert key not in errors.to_payload(exc)["error"]
    assert key not in errors.to_text(exc)


def test_generic_payload_redacted_but_keeps_diagnosis():
    """兜底信封必须带出类型与说明（_guard 之后 SDK 再也看不到它，否则彻底不可观测）。"""
    key = "0123456789abcdef" * 4
    payload = errors.generic_payload(sqlite3.OperationalError(f"boom {key}"))
    assert key not in payload["error"]
    assert "OperationalError" in payload["error"] and "boom" in payload["error"]


def test_purge_refused_is_a_project_error_with_hint():
    """purge 的拒绝要走项目错误契约，否则 PermissionError/OSError 会穿透 CLI 漏斗
    变成 traceback——正是 C1 要消灭的东西（spec §4）。"""
    exc = errors.PurgeRefused("有进程正在使用检索库", hint="等该进程结束后重试")
    assert isinstance(exc, errors.AgentWeChatError)
    assert not isinstance(exc, SystemExit)          # 绝不能是会杀死 server 的 SystemExit
    assert errors.to_text(exc) == "有进程正在使用检索库（等该进程结束后重试）"
