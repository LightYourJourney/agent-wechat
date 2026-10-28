"""Phase 07 验收链：合成数据 sync → 工作台检索 → 证据核对 → 派生删除。

用 Phase 04 的合成加密树跑**真实** export 路径（不读真实微信、不下载模型）。
"""
from __future__ import annotations

import http.client
import json
import threading
import urllib.parse

from agent_wechat import sync, webui

from test_sync_ops import ACCT, FRIEND, _env as _sync_env


def _req(port: int, path: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", urllib.parse.quote(path, safe="/?&=%"))
    res = conn.getresponse()
    raw = res.read()
    conn.close()
    return res, json.loads(raw)


def _post(port: int, csrf: str, path: str, body: dict):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, body=json.dumps(body).encode(),
                 headers={"Content-Type": "application/json",
                          "Origin": f"http://127.0.0.1:{port}",
                          "X-AW-CSRF": csrf})
    res = conn.getresponse()
    raw = res.read()
    conn.close()
    return res, json.loads(raw)


def test_sync_search_evidence_and_derived_delete(tmp_path, monkeypatch):
    _sync_env(tmp_path, monkeypatch)
    res = sync.run_sync(ACCT)
    assert res.status in ("ok", "no_new") and res.exported == 2, res

    httpd = webui.create_server("127.0.0.1", 0)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        status, body = _req(port, "/api/status")
        assert status.status == 200 and ACCT in body["accounts"]

        status, chats = _req(port, f"/api/chats?account={ACCT}")
        assert any(c["chat_id"] == FRIEND for c in chats["chats"])

        status, found = _req(port, f"/api/search?account={ACCT}&q=甲一&mode=hybrid")
        assert status.status == 200 and found["hits"], found
        hit = found["hits"][0]
        assert hit["msg_key"].startswith(ACCT + "|")

        status, ctx = _req(port, f"/api/evidence?account={ACCT}&key={hit['msg_key']}")
        assert status.status == 200
        assert any(m["content"] == "甲一" for m in ctx["messages"])

        # 模拟一次 OCR 派生（引擎不在验收链里，派生行的生命周期才是）
        from agent_wechat import store
        did = f"{hit['msg_key']}|ocr"
        with store.open() as con:
            con.execute(
                "INSERT INTO derived_text(derivation_id, msg_key, account, chat_id,"
                " kind, text, engine, status, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (did, hit["msg_key"], ACCT, FRIEND, "ocr", "证据截图文字",
                 "fake", "auto", 1))
            con.commit()
        status, listed = _req(port, f"/api/derived?account={ACCT}")
        assert any(i["derivation_id"] == did for i in listed["items"])

        status, deleted = _post(port, httpd.aw_state.csrf, "/api/derived/delete",
                                {"derivation_id": did})
        assert status.status == 200 and deleted["status"] == "deleted"

        status, listed = _req(port, f"/api/derived?account={ACCT}")
        assert all(i["derivation_id"] != did or i["status"] == "deleted"
                   for i in listed["items"])
        with store.open() as con:
            assert con.execute("SELECT count(*) FROM derived_tombstone"
                               " WHERE msg_key=?", (hit["msg_key"],)).fetchone()[0] == 1
    finally:
        httpd.shutdown()
        httpd.server_close()
