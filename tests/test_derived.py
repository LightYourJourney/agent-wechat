"""Phase 06：本地派生文本（OCR/ASR）的摄取、编辑、删除与 purge 生命周期。

引擎用假实现（真实 tesseract 另有一条 skipif 冒烟）；不联网、不下载模型。
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import pathlib

import pytest

from agent_wechat import derived, errors, exclusions, media, messages, paths, retrieval, store

ACCT = "wxid_testacct01_ab12"
CHAT = "wxid_friend01"
T0 = 1700000000


class FakeOcr:
    name = "fake-ocr"

    def __init__(self, text="识别出的文字", quality=0.9):
        self.text, self.quality, self.calls = text, quality, []

    def available(self):
        return True

    def version(self):
        return "1.0"

    def extract(self, path):
        self.calls.append(path)
        return self.text, self.quality


class FakeAsr(FakeOcr):
    name = "fake-asr"

    def __init__(self, text="转写出的语音", quality=None):
        super().__init__(text, quality)


def _row(lid: int, msg_type: int, content: str | None):
    return messages.make_row(
        account=ACCT, chat_md5=hashlib.md5(CHAT.encode()).hexdigest(), chat_id=CHAT,
        shard="message_0.db", local_id=lid, ts=T0 + lid, msg_type=msg_type,
        sender_id="wxid_other", sender_name=None, is_self=0, content=content,
        media_hint=None)


def _env(tmp_path, monkeypatch, *, export_image=True, export_voice=True):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(paths, "launch_agents_dir", lambda: tmp_path / "LaunchAgents")
    out = tmp_path / "media"
    monkeypatch.setattr(media, "export_root", lambda: out)
    with store.open() as con:
        store.upsert_batch(con, [_row(1, messages.TYPE_IMAGE, "[图片]"),
                                 _row(2, messages.TYPE_VOICE, "[语音]")])
        store.upsert_media_refs(con, [
            (_row(1, 3, None)["msg_key"], "image", "f" * 32, None)])
        con.commit()
    md5 = hashlib.md5(CHAT.encode()).hexdigest()
    month = dt.datetime.fromtimestamp(T0 + 1, dt.timezone.utc).strftime("%Y-%m")
    if export_image:
        p = out / md5 / "image" / month / f"{'f' * 32}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    if export_voice:
        p = out / md5 / "voice" / month / f"{T0 + 2}_2.wav"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"RIFF" + b"0" * 64)
    return out


def _keys():
    return _row(1, messages.TYPE_IMAGE, None)["msg_key"], _row(2, messages.TYPE_VOICE, None)["msg_key"]


# ---------------------------------------------------------------- 摄取

def test_ingest_records_engine_provenance_and_leaves_messages_untouched(
        tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = derived.ingest(ACCT, CHAT, ocr=FakeOcr(), asr=FakeAsr(), now=1000)
    assert res["counts"] == {"ocr": 1, "asr": 1}, res
    items = derived.list_derived(ACCT)["items"]
    img = next(i for i in items if i["kind"] == "ocr")
    assert img["engine"] == "fake-ocr" and img["engine_version"] == "1.0"
    assert img["status"] == "auto" and img["quality"] == 0.9
    voice = next(i for i in items if i["kind"] == "asr")
    assert voice["text"] == "转写出的语音"
    with store.open() as con:
        content = con.execute("SELECT content FROM messages WHERE msg_key=?",
                              (_keys()[0],)).fetchone()[0]
    assert content == "[图片]", "派生文本绝不能覆盖原始消息"


def test_unexported_media_and_missing_engines_are_reported_not_faked(
        tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, export_image=False, export_voice=False)

    class NoEngine(FakeOcr):
        def available(self):
            return False

    res = derived.ingest(ACCT, CHAT, ocr=FakeOcr(), asr=FakeAsr())
    assert res["counts"] == {}
    assert res["skipped"].get("media_not_exported") == 2

    # 引擎检查在「文件存在」之后：没有文件时先报 media_not_exported 是对的。
    _env(tmp_path, monkeypatch, export_image=True, export_voice=True)
    res2 = derived.ingest(ACCT, CHAT, ocr=NoEngine(), asr=NoEngine())
    assert res2["skipped"].get("engine_unavailable") == 2
    assert res2["degraded"], "引擎缺失必须带 degraded 说明"


def test_size_duration_and_batch_limits(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    original = derived.MAX_FILE_BYTES
    monkeypatch.setattr(derived, "MAX_FILE_BYTES", 10)
    res = derived.ingest(ACCT, CHAT, ocr=FakeOcr(), asr=FakeAsr())
    assert res["skipped"].get("too_large") == 2

    monkeypatch.setattr(derived, "MAX_FILE_BYTES", original)
    monkeypatch.setattr(derived, "_probe_audio_seconds", lambda p: 10_000.0)
    res2 = derived.ingest(ACCT, CHAT, ocr=FakeOcr(), asr=FakeAsr())
    assert res2["skipped"].get("too_long") == 1 and res2["counts"].get("ocr") == 1

    res3 = derived.ingest(ACCT, CHAT, ocr=FakeOcr(), asr=FakeAsr(), limit=1)
    assert sum(res3["counts"].values()) == 1, "批量上限必须生效"


def test_concurrent_ingest_is_refused(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    paths.ensure_home_dirs()
    holder = open(paths.HOME_DIR / derived.LOCK_NAME, "a")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(errors.DerivedBusy):
            derived.ingest(ACCT, CHAT, ocr=FakeOcr(), asr=FakeAsr())
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()


# ---------------------------------------------------------------- 编辑 / 删除 / 搜索

def test_edit_updates_search_and_delete_leaves_a_tombstone(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    derived.ingest(ACCT, CHAT, ocr=FakeOcr("旧文字"), asr=FakeAsr())
    item = next(i for i in derived.list_derived(ACCT)["items"] if i["kind"] == "ocr")
    derived.edit_derived(item["derivation_id"], "新文字", now=2000)
    assert derived.search_derived("新文字", account=ACCT)["total"] == 1
    assert derived.search_derived("旧文字", account=ACCT)["total"] == 0
    assert derived.list_derived(ACCT)["items"][0]["status"] in ("edited", "auto")

    derived.delete_derived(item["derivation_id"], now=3000)
    assert derived.search_derived("新文字", account=ACCT)["total"] == 0
    again = derived.ingest(ACCT, CHAT, ocr=FakeOcr("旧文字"), asr=FakeAsr())
    assert again["skipped"].get("deleted") == 1, "删除后重扫不得复活"
    row = derived.list_derived(ACCT, status="deleted")["items"][0]
    assert row["text"] == "", "删除必须清空正文"


def test_short_query_falls_back_to_like(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    derived.ingest(ACCT, CHAT, ocr=FakeOcr("爬山"), asr=FakeAsr())
    assert derived.search_derived("爬山", account=ACCT)["total"] == 1


def test_hybrid_retrieval_fuses_derived_evidence(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    derived.ingest(ACCT, CHAT, ocr=FakeOcr("会议纪要截图"), asr=FakeAsr())
    hy = retrieval.search_messages("会议纪要", account=ACCT, mode="hybrid")
    hit = next(h for h in hy["hits"] if h["msg_key"] == _keys()[0])
    assert "derived" in hit["sources"]
    kw = retrieval.search_messages("会议纪要", account=ACCT, mode="keyword")
    assert all("derived" not in h["sources"] for h in kw["hits"]), \
        "keyword 模式保持既有语义（原消息 FTS）"


# ---------------------------------------------------------------- purge / 排除

def test_purge_removes_derived_rows_and_claims(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    derived.ingest(ACCT, CHAT, ocr=FakeOcr(), asr=FakeAsr())
    derived.add_claim(ACCT, "周末去爬山", evidence=[_keys()[0]])
    with store.open() as con:
        before = store.chat_row_counts(con, ACCT, CHAT)
        assert before["derived"] == 2 and before["claims"] == 1
        store.delete_chat(con, account=ACCT, chat_id=CHAT)
        con.commit()
        after = store.chat_row_counts(con, ACCT, CHAT)
    assert after["derived"] == 0 and after["claims"] == 0 and after["evidence"] == 0
    assert derived.list_derived(ACCT)["total"] == 0


def test_excluded_messages_are_never_derived(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    with store.open() as con:
        exclusions.add(con, [(_keys()[0], ACCT, hashlib.md5(CHAT.encode()).hexdigest())])
        con.commit()
    res = derived.ingest(ACCT, CHAT, ocr=FakeOcr(), asr=FakeAsr())
    assert res["skipped"].get("excluded") == 1
    assert res["counts"].get("asr") == 1


# ---------------------------------------------------------------- 真实引擎冒烟

@pytest.mark.skipif(not derived.TesseractOcr().available(),
                    reason="本机没有 tesseract")
def test_real_tesseract_recognises_english_text(tmp_path):
    """本机真实 OCR 冒烟：画一张含英文的图，认出单词（不涉及中文语言包）。"""
    PIL = pytest.importorskip("PIL.Image")
    draw_mod = pytest.importorskip("PIL.ImageDraw")
    img = PIL.new("RGB", (420, 120), "white")
    draw_mod.Draw(img).text((10, 40), "HELLO WECHAT", fill="black")
    p = tmp_path / "shot.png"
    img.save(p)
    text, _quality = derived.TesseractOcr().extract(p)
    assert "HELLO" in text.upper()


def test_whisper_without_a_local_model_is_unavailable():
    eng = derived.WhisperCliAsr(model=pathlib.Path("/nonexistent/ggml-base.bin"))
    assert eng.available() is False
