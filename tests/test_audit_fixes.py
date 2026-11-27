"""全量审查修复项回归测试（P0 安全红线 / P1 核心正确性 / P2 工程门禁）。"""
from __future__ import annotations

import errno
import os
import pathlib
import plistlib
import sqlite3
import time
from types import SimpleNamespace

import pytest

from agent_wechat import (
    account,
    appmsg,
    bootstrap,
    chat_index,
    chat_md,
    chat_text,
    cli,
    derived,
    errors,
    mcp_server,
    messages,
    paths,
    purge,
    reader,
    search,
    store,
    sync,
    tasks,
    usecases,
    vector,
    webui,
)


def _row(acct: str, chat: str, lid: int, content: str, ts: int = 1_700_000_000,
         msg_type: int = 1, shard: str = "message_0.db") -> messages.MessageRow:
    md5 = messages.chat_md5(chat)
    return messages.make_row(
        account=acct, chat_md5=md5, chat_id=chat, shard=shard,
        local_id=lid, ts=ts, msg_type=msg_type,
        sender_id=chat, sender_name=chat, is_self=0,
        content=content, media_hint=None,
    )


# ---------- P0：安全红线与 purge ----------

def test_container_verdict_fail_closed_on_eacces_and_false_on_enametoolong(tmp_path, monkeypatch):
    guard = tmp_path / "com.tencent.xinWeChat"
    guard.mkdir()
    monkeypatch.setattr(paths, "container_bundle", lambda: guard)

    orig_samefile = os.path.samefile

    def boom_eacces(a, b):
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(os.path, "samefile", boom_eacces)
    other = tmp_path / "other"
    other.mkdir()
    assert paths.container_verdict(other) is None

    def boom_nametoolong(a, b):
        raise OSError(errno.ENAMETOOLONG, "too long")

    monkeypatch.setattr(os.path, "samefile", boom_nametoolong)
    assert paths.container_verdict(other) is False
    monkeypatch.setattr(os.path, "samefile", orig_samefile)


def test_purge_refuses_wechat_container_and_system_dirs(tmp_path, monkeypatch):
    guard = tmp_path / "com.tencent.xinWeChat"
    (guard / "Data").mkdir(parents=True)
    monkeypatch.setattr(paths, "container_bundle", lambda: guard)

    with pytest.raises(errors.PurgeRefused, match="微信容器"):
        purge._check_home_is_sane(guard / "Data")

    with pytest.raises(errors.PurgeRefused, match="系统或共享目录"):
        purge._check_home_is_sane(pathlib.Path("/tmp"))


def test_purge_escapes_roots_rejects_symlinked_root_to_outside_or_sibling(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / "home"
    kept = home / "kept"
    kept.mkdir(parents=True)
    media_link = home / "media"
    media_link.symlink_to(outside)
    # 根本身是指向外部目录的符号链接：绝不可把它的 resolve 目标加入合法根集合
    assert purge._escapes_roots(media_link, (media_link,)) is True

    # 具名目录内的软链指向同 HOME 下的保留目录：必须判为逃逸
    real_tmp = home / "tmp"
    real_tmp.mkdir()
    cross = real_tmp / "leak"
    cross.symlink_to(kept)
    assert purge._escapes_roots(cross, (real_tmp,)) is True
    # 同一子树内部链接放行
    inner = real_tmp / "sub"
    inner.mkdir()
    ok_link = real_tmp / "ok"
    ok_link.symlink_to(inner)
    assert purge._escapes_roots(ok_link, (real_tmp,)) is False


def test_purge_all_with_tasks_claim_does_not_recreate_home_dir(tmp_path, monkeypatch):
    home = tmp_path / "aw-home"
    home.mkdir(mode=0o700)
    (home / "dummy.txt").write_text("hi", encoding="utf-8")
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(tasks, "TASKS_FILE", home / "tasks.json")

    p = purge.plan(frozenset(), all_=True)
    rep = purge.execute(p)
    assert rep.home_removed is True
    assert not home.exists()
    assert not (home / "tasks.json").exists()
    assert not (home / "tasks.json.lock").exists()
    res = purge.verify(p)
    assert res.not_removed == ()


def test_purge_verify_chat_ignores_fresh_message_arrived_after_tombstone(tmp_path, monkeypatch):
    home = tmp_path / "aw-home"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")
    monkeypatch.setattr(tasks, "TASKS_FILE", home / "tasks.json")

    with store.open() as con:
        store.upsert_batch(con, [_row("acct", "bob", 1, "旧消息", ts=1_700_000_001)])
    p = purge.plan_chat([{"account": "acct", "chat_id": "bob"}])
    purge.execute_chat(p)

    # 模拟删除之后并发同步写入一条新消息（不在墓碑内）
    with store.open() as con:
        store.upsert_batch(con, [_row("acct", "bob", 2, "新到的消息", ts=1_700_000_010)])
    res = purge.verify_chat(p)
    assert res.leftover == ()
    assert res.tombstone_written >= 1


def test_purge_tree_entries_fail_closed_on_unreadable_subdir(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root 不受 chmod 000 限制")
    root = tmp_path / "tree"
    sub = root / "sealed"
    sub.mkdir(parents=True)
    sub.chmod(0o000)
    try:
        with pytest.raises(OSError):
            purge._tree_entries(root)
    finally:
        sub.chmod(0o700)


def test_cli_purge_rejects_account_without_chat():
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--all", "--account", "acct_1234"])
    assert ei.value.code == 2


def test_cli_db_under_storage_blocks_traversal(tmp_path, monkeypatch):
    base = tmp_path / "db_storage"
    base.mkdir()
    monkeypatch.setattr(paths, "db_storage", lambda _acc: base)
    with pytest.raises(errors.SearchInputError, match="越出"):
        cli._db_under_storage("acct", "../../etc/passwd")


# ---------- P1：核心正确性与数据完整性 ----------

def test_degraded_chat_id_case_normalization_and_is_chat_id():
    hex32 = "d0f46ed3" * 4
    assert messages.chat_md5(f"md5:{hex32.upper()}") == hex32
    assert reader.is_chat_id(f"md5:{hex32.upper()}") is True
    assert reader.is_chat_id("md5:not-hex!") is False


def test_chat_md_blocks_space_and_backslash_injection_and_keeps_multiline_quote():
    assert chat_md._md_dest_ok("https://ok.example/x https://evil.example/phish") is False
    assert chat_md._md_dest_ok("https://ok.example/x\\") is False
    assert chat_md._md_dest_ok("https://ok.example/ok?a=1") is True

    a = appmsg.AppMsg(
        kind=57,
        title="回复",
        fields=(),
        quote=appmsg.Quote(svrid=1, displayname="张三\n## 伪造标题", content="第一段\n\n第二段",
                           create_time=None, from_usr=None),
        link=None,
        file=None,
        transfer=None,
    )
    lines = chat_md._appmsg_lines(a, ref_anchor="m1")
    quote_lines = [ln for ln in lines if ln.startswith(">")]
    assert len(quote_lines) == 3
    assert quote_lines[1] == ">"
    assert "第二段" in quote_lines[2]
    assert "\n" not in quote_lines[0]


def test_appmsg_filters_fragment_credentials_and_chat_text_rejects_controls():
    assert appmsg._has_credential_param("https://example.com/cb#access_token=secret123") is True
    assert appmsg._has_credential_param("https://example.com/cb#token=abc") is True
    assert chat_text.url_is_safe("\x01https://ok.example/x") is False
    assert chat_text.url_is_safe("https://ok.example/a b") is False
    assert chat_text.url_is_safe("https://ok.example/a") is True


def test_search_falls_back_to_like_on_broken_fts_for_long_query(tmp_path, monkeypatch):
    home = tmp_path / "aw-home"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")

    with store.open() as con:
        store.upsert_batch(con, [_row("acct", "bob", 1, "明天发会议纪要给你")])
    # 正常 FTS 下首尾空白的查询也应命中
    ok = search.search(" 会议纪要 ", account="acct")
    assert ok["total"] == 1 and ok["mode"] == "fts"

    # 破坏 FTS 表并复用该连接：≥3 字符查询应真正执行 LIKE 兜底并返回命中
    import contextlib
    con = store.connect(home / "index.db")
    try:
        con.execute("DROP TABLE messages_fts")
        con.commit()
        monkeypatch.setattr(store, "open", lambda: contextlib.nullcontext(con))
        res = search.search("会议纪要", account="acct")
        assert res["mode"] == "like"
        assert res["total"] == 1
        assert any(d["capability"] == "search_fts" for d in res["degraded"])
    finally:
        con.close()


def test_derived_insert_respects_edited_and_tombstone_and_claim_blocks_zero_evidence(
    tmp_path, monkeypatch
):
    home = tmp_path / "aw-home"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")

    r1 = _row("acct", "bob", 1, "[image]", msg_type=3)
    r2 = _row("acct", "bob", 2, "[image]", msg_type=3)
    with store.open() as con:
        store.upsert_batch(con, [r1, r2])
        row1 = con.execute("SELECT * FROM messages WHERE msg_key=?", (r1["msg_key"],)).fetchone()
        row2 = con.execute("SELECT * FROM messages WHERE msg_key=?", (r2["msg_key"],)).fetchone()
        assert derived._insert(
            con, row=row1, kind="ocr", text="原始OCR", engine="t", version="1",
            source=pathlib.Path("a.jpg"), quality=None, now=100) is True
        con.commit()

    # 用户编辑后再次 _insert 不得覆盖 edited 状态
    did1 = f"{r1['msg_key']}|ocr"
    derived.edit_derived(did1, "人工订正文本", now=101)
    with store.open() as con:
        row1 = con.execute("SELECT * FROM messages WHERE msg_key=?", (r1["msg_key"],)).fetchone()
        assert derived._insert(
            con, row=row1, kind="ocr", text="引擎再跑一遍", engine="t", version="1",
            source=pathlib.Path("a.jpg"), quality=None, now=102) is False
        got = con.execute("SELECT text, status FROM derived_text WHERE derivation_id=?", (did1,)).fetchone()
        assert (got["text"], got["status"]) == ("人工订正文本", "edited")
        # 有墓碑时纯插入也被拦
        con.execute(
            "INSERT INTO derived_tombstone(msg_key, kind, deleted_at) VALUES (?,?,?)",
            (r2["msg_key"], "ocr", 102))
        assert derived._insert(
            con, row=row2, kind="ocr", text="被墓碑拦住", engine="t", version="1",
            source=pathlib.Path("b.jpg"), quality=None, now=103) is False
        con.commit()

    # 证据被清空后（retracted），不得再次迁回 confirmed
    claim = derived.add_claim("acct", "周五开会", evidence=[r1["msg_key"]], now=110)
    with store.open() as con:
        con.execute("DELETE FROM fact_evidence WHERE claim_id=?", (claim["claim_id"],))
        con.execute("UPDATE fact_claim SET status='retracted' WHERE claim_id=?", (claim["claim_id"],))
        con.commit()
    with pytest.raises(errors.DerivedInputError, match="有效证据"):
        derived.set_claim_status(claim["claim_id"], "confirmed", now=111)


def test_whisper_asr_returns_none_on_engine_failure(tmp_path, monkeypatch):
    model = tmp_path / "ggml.bin"
    model.write_bytes(b"m")
    asr = derived.WhisperCliAsr(model=model)
    monkeypatch.setattr(derived, "_run", lambda argv: None)
    assert asr.extract(tmp_path / "a.wav") == (None, None)


def test_vector_semantic_search_wraps_dim_mismatch_and_sqlite_error(tmp_path, monkeypatch):
    home = tmp_path / "aw-home"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")

    class _BuildEmb:
        def embed(self, texts):
            return [[0.1] * store.VECTOR_DIM for _ in texts]

    class _BadQueryEmb:
        def embed_query(self, text):
            return [0.1] * (store.VECTOR_DIM - 1)  # 故意维度不匹配

    with store.open() as con:
        store.upsert_batch(con, [_row("acct", "bob", 1, "爬山安排定在周末")])
    vector.build_index(accounts=["acct"], embedder=_BuildEmb())  # type: ignore[arg-type]

    with pytest.raises(errors.VectorError, match="维度"):
        vector.semantic_search("爬山", account="acct", embedder=_BadQueryEmb())  # type: ignore[arg-type]


def test_vector_freshness_filters_by_model_id_and_reembed_stats_handles_legacy_schema():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE vector_watermark (model_id TEXT, account TEXT, last_rowid INTEGER)")
    con.execute("INSERT INTO vector_watermark VALUES ('old:model:1', 'acct', 999)")
    con.execute("INSERT INTO vector_watermark VALUES (?, 'acct', 42)", (vector.MODEL_ID,))
    con.execute("CREATE TABLE pending_reembed (msg_key TEXT PRIMARY KEY, changed_at INTEGER)")
    con.execute("INSERT INTO pending_reembed VALUES ('acct|m|s|1|1', 100)")

    stats = store.reembed_queue_stats(con, "acct")
    assert stats == {"pending": 1, "failed": 0, "max_attempts": 0, "by_reason": {}}

    fresh = vector.freshness(con, "acct")
    assert fresh["watermark"] == {"acct": 42}
    con.close()


def test_bootstrap_read_capture_exits_immediately_on_eof():
    r_fd, w_fd = os.pipe()
    os.close(w_fd)
    fake_proc = SimpleNamespace(stdout=os.fdopen(r_fd, "rb"), poll=lambda: None)
    try:
        t0 = time.monotonic()
        assert bootstrap._read_capture(fake_proc, time.time() + 10) == ""  # type: ignore[arg-type]
        assert time.monotonic() - t0 < 1.0
    finally:
        fake_proc.stdout.close()


def test_errors_redact_handles_truncated_key_and_url_tokens():
    s = "PRAGMA key = \"x'0123456789abcdef0123456789\" url=https://a.example/x?token=TOPSECRET&k=1"
    out = errors.redact(s)
    assert "0123456789abcdef" not in out
    assert "TOPSECRET" not in out


def test_account_cached_pid_does_not_cache_none(monkeypatch):
    calls = {"n": 0}

    def fake_pid():
        calls["n"] += 1
        return None if calls["n"] == 1 else 4321

    monkeypatch.setattr(account, "_pid_cache", None)
    monkeypatch.setattr(account, "wechat_pid", fake_pid)
    assert account._cached_pid() is None
    assert account._cached_pid() == 4321
    assert calls["n"] == 2


def test_sync_launchd_plist_escapes_xml_special_chars(tmp_path, monkeypatch):
    odd = tmp_path / "a&b<c>"
    monkeypatch.setattr(paths, "HOME_DIR", odd)
    xml_text = sync.launchd_plist(account="acct&1<2>", interval_minutes=15)
    parsed = plistlib.loads(xml_text.encode("utf-8"))
    assert parsed["ProgramArguments"][-1] == "acct&1<2>"
    assert "a&b<c>" in parsed["StandardOutPath"]


def test_webui_origin_80_and_mcp_annotations_and_media_empty_kinds(tmp_path, monkeypatch):
    assert webui._origin_ok("http://127.0.0.1", 80) is True
    assert webui._origin_ok("http://127.0.0.1", 8765) is False

    assert mcp_server._READ_ONLY.open_world_hint is False
    assert mcp_server._READ_ONLY.destructive_hint is False
    assert mcp_server._IDEMPOTENT.open_world_hint is False
    assert mcp_server._IDEMPOTENT_NET.open_world_hint is True

    home = tmp_path / "aw-home"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(paths, "HOME_DIR", home)

    class _FakeCtx:
        account = "acct"

        def resolve(self, contact):
            return "bob"

    monkeypatch.setattr("agent_wechat.usecases.media.pkg_context", lambda _a=None: _FakeCtx())
    with pytest.raises(errors.SearchInputError, match="媒体类型"):
        usecases.media_export("bob", kinds=[])


def test_chat_index_skips_invalid_dirs_and_empty_formats():
    assert chat_index.is_chat_dir("0123abcd") is True
    assert chat_index.is_chat_dir("backup") is False
    html_out = chat_index.render([
        chat_index.ChatIndexEntry("backup", "x", "x", 1, True, True),
        chat_index.ChatIndexEntry("0123abcd", "y", "y", 1, False, False),
        chat_index.ChatIndexEntry("deadbeef", "z", "z", 2, False, True),
    ])
    assert "共 <b>1</b> 个会话" in html_out
    assert "deadbeef/chat/index.html" in html_out
    assert "0123abcd" not in html_out


# ---------- P2：工程门禁回归 ----------

def test_check_import_cycles_detects_cross_package_cycle_and_subpackage_mcp(tmp_path):
    import importlib.util

    script = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "check_import_cycles.py"
    spec = importlib.util.spec_from_file_location("cic_mod", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 跨包真环：a -> pkg -> pkg.sub -> a（验证 pkg 与 pkg/__init__.py 同一身份）
    rt = {"a": {"pkg"}, "pkg": {"pkg.sub"}, "pkg.sub": {"a"}}
    assert mod.runtime_cycles(rt) == [["a", "pkg", "pkg.sub"]]

    # AST 检查 `import os, mcp` 也能被抓出
    leak = tmp_path / "bad.py"
    leak.write_text("import os, mcp\n", encoding="utf-8")
    assert mod._imports_mcp(leak) is True
    clean = tmp_path / "ok.py"
    clean.write_text('x = "import mcp"\n', encoding="utf-8")
    assert mod._imports_mcp(clean) is False


def test_check_vulture_works_from_src_subdirectory(monkeypatch):
    import importlib.util

    script = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "check_vulture.py"
    spec = importlib.util.spec_from_file_location("cv_mod", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    monkeypatch.chdir(mod.SRC)
    assert mod.main() == 0
