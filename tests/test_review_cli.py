"""全量审查（CLI 部分）修掉的几处缺陷，各一条回归用例。

全部走合成库 / 桩：不碰真实微信数据，也不碰 ~/.agent-wechat；评测脚本只测解析与统计，
`subprocess.run` 一律桩掉——**绝不真跑模型**。
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

from agent_wechat import cli, derived, usecases
from test_cli import _isolate, _stub_resolution

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ---------- 1. purge --chat：索引库不在、导出物还在盘上 ----------

def _no_index_with_export(tmp_path, monkeypatch) -> pathlib.Path:
    """排除清单里有墓碑、索引库已被删（模拟 `purge --index`）、`media/<md5>/` 还在。"""
    from agent_wechat import exclusions, messages, store
    home = _isolate(tmp_path, monkeypatch)
    md5 = messages.chat_md5("victim")
    with store.open() as con:
        exclusions.add(con, [(f"a|{md5}|message_0.db|1", "a", md5)])
    (home / "index.db").unlink()
    d = home / "media" / md5 / "chat"
    d.mkdir(parents=True)
    (d / "index.html").write_text("解密出的明文", encoding="utf-8")
    _stub_resolution(monkeypatch, "victim")
    return home / "media" / md5


def test_purge_chat_yes_without_index_fails_when_export_is_left(tmp_path, monkeypatch, capsys):
    """`--yes` 要的是「删掉」：导出物原封不动还在盘上时不许退 0（EA-029：部分失败退 1）。
    这一支删不了它（删除路线要开索引，会把刚删掉的 index.db 建回来），所以明确失败。"""
    left = _no_index_with_export(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--chat", "小李", "--yes"])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "导出物仍在盘上" in err and str(left) in err
    assert "未删除任何文件" in err
    assert left.exists(), "这一支不删导出物：失败就是失败，不许半删"
    assert not (tmp_path / "home" / "index.db").exists(), "不许把刚删掉的索引建回来"


def test_purge_chat_preview_without_index_still_exits_zero(tmp_path, monkeypatch, capsys):
    """不带 `--yes` 是预览：点名残留、退 0（与有索引时的预览同一口径）。"""
    left = _no_index_with_export(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--chat", "小李"])
    assert ei.value.code == 0
    assert str(left) in capsys.readouterr().err


# ---------- 2. chat export：请求的格式没写成 ----------

def _fake_export(files: tuple[str, ...]):
    def fake(contact, **kw):
        return usecases.ChatExportResult(
            chat_id="wxid_otherchat", out_dir="/tmp/x", files=files,
            counts={"text": 1}, truncated=False, unparsed=0, rejected=0)
    return fake


@pytest.mark.parametrize("files", [(), ("/tmp/x/chat/index.html",)])
def test_chat_export_fails_when_a_requested_format_was_not_written(monkeypatch, capsys, files):
    monkeypatch.setattr(usecases, "chat_export", _fake_export(files))
    with pytest.raises(SystemExit) as ei:
        cli.main(["chat", "export", "--chat", "wxid_otherchat", "--format", "html,md"])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "md" in err and "没写成" in err
    if not files:
        assert "html" in err


def test_chat_export_all_written_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(usecases, "chat_export", _fake_export(
        ("/tmp/x/chat/index.html", "/tmp/x/chat/index.md")))
    with pytest.raises(SystemExit) as ei:
        cli.main(["chat", "export", "--chat", "wxid_otherchat", "--format", "html,md"])
    assert ei.value.code == 0
    assert "没写成" not in capsys.readouterr().err


# ---------- 3. memory delete：id 不存在 ----------

def test_memory_delete_unknown_id_is_data_not_found(monkeypatch, capsys):
    """与 show / confirm 一致：找不到 → DataNotFound → 统一出口 exit 1。"""
    monkeypatch.setattr(derived, "delete_claim", lambda claim_id: False)
    with pytest.raises(SystemExit) as ei:
        cli.main(["memory", "delete", "no-such-claim"])
    assert ei.value.code == 1
    cap = capsys.readouterr()
    assert "找不到主张 no-such-claim" in cap.err
    assert "deleted" not in cap.out


def test_memory_delete_existing_id_still_reports_deleted(monkeypatch, capsys):
    monkeypatch.setattr(derived, "delete_claim", lambda claim_id: True)
    with pytest.raises(SystemExit) as ei:
        cli.main(["memory", "delete", "c1"])
    assert ei.value.code == 0
    assert json.loads(capsys.readouterr().out) == {"deleted": True}


# ---------- 4. inspect --sample 负数 ----------

def test_inspect_rejects_negative_sample(monkeypatch):
    """`LIMIT -1` 会把每张表的全部行明文打出来：参数层就拒绝（用法错误 exit 2），
    根本走不到解密。"""
    called = []
    monkeypatch.setattr(cli, "cmd_inspect", lambda args: called.append(args) or 0)
    with pytest.raises(SystemExit) as ei:
        cli.main(["inspect", "session/session.db", "--sample", "-1"])
    assert ei.value.code == 2
    assert not called


def test_inspect_accepts_zero_sample(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "cmd_inspect", lambda args: called.append(args.sample) or 0)
    with pytest.raises(SystemExit) as ei:
        cli.main(["inspect", "session/session.db", "--sample", "0"])
    assert ei.value.code == 0
    assert called == [0]


# ---------- 5. logs clean 的 help ----------

def test_logs_clean_help_names_the_launchd_logs(capsys):
    with pytest.raises(SystemExit):
        cli.main(["logs", "--help"])
    out = capsys.readouterr().out
    assert "launchd" in out


# ---------- 7. 评测脚本：退出码 / 超时 / 空任务 ----------

@pytest.fixture
def evalmod(monkeypatch):
    path = ROOT / "scripts" / "mcp_agent_eval.py"
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("mcp_agent_eval_under_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("mcp_agent_eval_under_test", None)


_OK_STREAM = "\n".join(json.dumps(o) for o in (
    {"type": "assistant", "message": {"id": "m1", "content": [
        {"type": "tool_use", "name": "mcp__agent-wechat__get_recent_messages", "input": {}}]}},
    {"type": "result", "is_error": False, "num_turns": 2, "result": "好",
     "usage": {"input_tokens": 10, "output_tokens": 3}, "duration_ms": 5},
))


def _stub_run(monkeypatch, mod, *, returncode=0, stdout=_OK_STREAM, raise_timeout=False):
    def fake(cmd, **kw):
        if raise_timeout:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"), output=None)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="boom")
    monkeypatch.setattr(mod.subprocess, "run", fake)


def test_eval_run_one_ok(evalmod, monkeypatch, tmp_path):
    _stub_run(monkeypatch, evalmod)
    r = evalmod.run_one("p", tmp_path / "c.json", "sonnet")
    assert r["ok"] is True and r["tools"] == ["get_recent_messages"]


def test_eval_run_one_nonzero_exit_is_a_failed_row(evalmod, monkeypatch, tmp_path):
    _stub_run(monkeypatch, evalmod, returncode=1)
    r = evalmod.run_one("p", tmp_path / "c.json", "sonnet")
    assert r["ok"] is False
    assert "1" in r["error"]


def test_eval_run_one_timeout_is_a_failed_row(evalmod, monkeypatch, tmp_path):
    _stub_run(monkeypatch, evalmod, raise_timeout=True)
    r = evalmod.run_one("p", tmp_path / "c.json", "sonnet")
    assert r["ok"] is False and "超时" in r["error"]
    assert r["tools"] == [] and r["input_tokens"] == 0


def test_eval_unmatched_tasks_exits_early(evalmod, monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(evalmod, "run_one", lambda *a: called.append(a))
    args = evalmod.argparse.Namespace(server_src=None, slow=False, tasks=["不存在的子串"],
                                      reps=1, model="sonnet", json=None)
    with pytest.raises(SystemExit) as ei:
        evalmod._run(tmp_path / "mcp.json", args)
    assert ei.value.code not in (0, None)
    assert not called


def test_eval_summary_of_no_rows_does_not_crash(evalmod):
    s = evalmod._summary([], evalmod.TASKS)
    assert s["runs"] == 0
