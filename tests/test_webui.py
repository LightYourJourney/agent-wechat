"""Phase 07：本地工作台的接口、分页与安全故障测试（合成索引，不联网）。"""
from __future__ import annotations

import hashlib
import http.client
import json
import threading
import urllib.parse

import pytest

from agent_wechat import account as account_mod
from agent_wechat import derived, errors, media, messages, paths, store, webui

ACCT = "wxid_testacct01_ab12"
CHAT = "wxid_friend01"
T0 = 1700000000


def _key(lid: int) -> str:
    return messages.row_key(ACCT, hashlib.md5(CHAT.encode()).hexdigest(),
                            "message_0.db", lid)


def _env(tmp_path, monkeypatch, *, seed=True):
    wx = tmp_path / "wx"
    (wx / ACCT).mkdir(parents=True)
    monkeypatch.setattr(paths, "XWECHAT_FILES", wx)
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "home" / "index.db")
    monkeypatch.delenv("AGENT_WECHAT_DENY", raising=False)
    monkeypatch.setattr(webui.account_mod, "status", lambda: account_mod.AccountStatus(
        running=False, current=None, accounts=[], pid=None))
    if seed:
        rows = [messages.make_row(
            account=ACCT, chat_md5=hashlib.md5(CHAT.encode()).hexdigest(),
            chat_id=CHAT, shard="message_0.db", local_id=i, ts=T0 + i,
            msg_type=1, sender_id=None, sender_name=None, is_self=0,
            content=f"第{i}条消息", media_hint=None) for i in range(1, 6)]
        with store.open() as con:
            store.upsert_batch(con, rows)
            con.execute("INSERT INTO display_names VALUES (?,?,?,?)",
                        (ACCT, CHAT, CHAT, "张三"))
            con.execute(
                "INSERT INTO derived_text(derivation_id, msg_key, account, chat_id,"
                " kind, text, engine, source, quality, status, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (f"{_key(1)}|ocr", _key(1), ACCT, CHAT, "ocr", "截图里的字",
                 "fake", "/tmp/x.png", 0.8, "auto", 1))
            con.commit()


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    httpd = webui.create_server("127.0.0.1", 0)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield Simple(port=port, csrf=httpd.aw_state.csrf, tmp=tmp_path)
    finally:
        httpd.shutdown()
        httpd.server_close()


class Simple:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _req(s: Simple, path: str, *, method="GET", headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", s.port, timeout=5)
    hdrs = dict(headers or {})
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, urllib.parse.quote(path, safe="/?&=%"), body=payload,
                 headers=hdrs)
    res = conn.getresponse()
    raw = res.read()
    conn.close()
    return res, raw


def _json(res, raw):
    return json.loads(raw)


# ---------------------------------------------------------------- 静态面

def test_index_has_csp_notice_and_csrf(srv):
    res, raw = _req(srv, "/")
    assert res.status == 200
    assert res.getheader("Content-Security-Policy")
    assert res.getheader("Cache-Control") == "no-store"
    text = raw.decode()
    assert "不要截图外传" in text and srv.csrf in text


def test_assets_are_served_without_filesystem_access(srv):
    res, raw = _req(srv, "/app.js")
    assert res.status == 200 and b"textContent" in raw
    assert b"innerHTML" not in raw, "前端不许拼 HTML（XSS）"
    res, _ = _req(srv, "/style.css")
    assert res.status == 200


def test_path_traversal_and_unknown_paths_are_404(srv):
    for path in ("/../index.db", "/index.db", "/app/../../etc/passwd", "/nope"):
        res, _ = _req(srv, path)
        assert res.status == 404, path


# ---------------------------------------------------------------- 安全面

def test_dns_rebinding_is_rejected(srv):
    res, body = _req(srv, "/api/status", headers={"Host": "evil.example:80"})
    assert res.status == 421


def test_post_requires_origin_and_csrf(srv):
    res, _ = _req(srv, "/api/derived/delete", method="POST",
                  body={"derivation_id": f"{_key(1)}|ocr"})
    assert res.status == 403, "缺 Origin/CSRF 必须拒绝"

    res, _ = _req(srv, "/api/derived/delete", method="POST",
                  headers={"Origin": "http://127.0.0.1:%d" % srv.port},
                  body={"derivation_id": f"{_key(1)}|ocr"})
    assert res.status == 403, "缺 CSRF 必须拒绝"

    bad = _req(srv, "/api/derived/delete", method="POST",
               headers={"Origin": f"http://127.0.0.1:{srv.port}",
                        "X-AW-CSRF": "wrong"},
               body={"derivation_id": f"{_key(1)}|ocr"})
    assert bad[0].status == 403


def test_unsupported_methods_are_405(srv):
    res, _ = _req(srv, "/api/status", method="PUT")
    assert res.status == 405


def test_accounts_are_explicit_and_validated(srv):
    res, body = _req(srv, "/api/chats")
    assert res.status == 400 and "account" in _json(res, body)["error"]
    res, body = _req(srv, "/api/chats?account=wxid_other")
    assert res.status == 400 and "账号目录不存在" in _json(res, body)["error"]


def test_read_only_ui_never_creates_an_index(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, seed=False)
    httpd = webui.create_server("127.0.0.1", 0)
    s = Simple(port=httpd.server_address[1], csrf=httpd.aw_state.csrf, tmp=tmp_path)
    t = threading.Thread(target=httpd.serve_forever, daemon=True); t.start()
    try:
        res, body = _req(s, "/api/status")
        assert _json(res, body)["index_exists"] is False
        res, body = _req(s, f"/api/chats?account={ACCT}")
        assert res.status == 400 and "索引库不存在" in _json(res, body)["error"]
        assert not store.DB_PATH.exists(), "只读接口不得建库"
    finally:
        httpd.shutdown(); httpd.server_close()


# ---------------------------------------------------------------- 数据面

def test_chats_and_timeline_are_bounded_and_paginated(srv):
    res, body = _req(srv, f"/api/chats?account={ACCT}")
    data = _json(res, body)
    assert data["chats"][0]["chat_id"] == CHAT and data["chats"][0]["display"] == "张三"

    res, body = _req(srv, f"/api/timeline?account={ACCT}&chat={CHAT}&limit=2")
    page1 = _json(res, body)
    assert len(page1["messages"]) == 2 and page1["next_cursor"]
    assert page1["messages"][0]["ts"] > page1["messages"][1]["ts"]

    c = page1["next_cursor"]
    res, body = _req(srv, f"/api/timeline?account={ACCT}&chat={CHAT}&limit=2"
                            f"&ts={c['ts']}&local_id={c['local_id']}")
    page2 = _json(res, body)
    assert {m["msg_key"] for m in page1["messages"]}.isdisjoint(
        {m["msg_key"] for m in page2["messages"]})


def test_search_is_bounded_and_respects_deny_list(srv, monkeypatch):
    res, body = _req(srv, f"/api/search?account={ACCT}&q=消息&limit=999")
    data = _json(res, body)
    assert data["hits"] and data["budget"]["limit"] == 100

    monkeypatch.setenv("AGENT_WECHAT_DENY", CHAT)
    res, body = _req(srv, f"/api/search?account={ACCT}&q=消息&limit=5")
    assert _json(res, body)["hits"] == []


def test_evidence_returns_full_text_and_refuses_tombstones(srv):
    res, body = _req(srv, f"/api/evidence?account={ACCT}&key={_key(1)}")
    ctx = _json(res, body)
    assert ctx["messages"][0]["content"] == "第1条消息"

    with store.open() as con:
        con.execute("INSERT INTO exclusions.excluded(msg_key, account, chat_md5,"
                    " excluded_at) VALUES(?,?,?,1)", (_key(2), ACCT, "m" * 32))
        con.commit()
    res, body = _req(srv, f"/api/evidence?account={ACCT}&key={_key(2)}")
    assert res.status == 400 and "销毁" in _json(res, body)["error"]


def test_derived_list_and_delete_via_post(srv):
    res, body = _req(srv, f"/api/derived?account={ACCT}")
    items = _json(res, body)["items"]
    assert items and items[0]["text"] == "截图里的字"

    headers = {"Origin": f"http://127.0.0.1:{srv.port}", "X-AW-CSRF": srv.csrf}
    res, body = _req(srv, "/api/derived/delete", method="POST", headers=headers,
                     body={"derivation_id": f"{_key(1)}|ocr"})
    assert _json(res, body)["status"] == "deleted"

    res, body = _req(srv, f"/api/derived?account={ACCT}")
    assert all(i["status"] == "deleted" for i in _json(res, body)["items"])
    with store.open() as con:
        assert con.execute("SELECT count(*) FROM derived_tombstone").fetchone()[0] == 1


def test_media_paths_are_reduced_to_basenames():
    data = {"messages": [{"msg_key": "k",
                      "path": "/" + "Users/example/secret/a.png"}]}
    out = webui._strip_paths(data)
    assert out["messages"][0]["path"] == "a.png"


def test_responses_do_not_leak_absolute_paths(srv):
    for path in (f"/api/chats?account={ACCT}",
                 f"/api/timeline?account={ACCT}&chat={CHAT}",
                 f"/api/search?account={ACCT}&q=消息",
                 f"/api/evidence?account={ACCT}&key={_key(1)}",
                 f"/api/diagnostics?account={ACCT}"):
        _, raw = _req(srv, path)
        assert str(srv.tmp) not in raw.decode(), path


def test_server_refuses_non_loopback_bind():
    with pytest.raises(errors.ScopeDenied):
        webui.create_server("0.0.0.0", 0)
