"""Phase 06：事实记忆的状态机、证据与 purge 生命周期。"""
from __future__ import annotations

import hashlib

import pytest

from agent_wechat import derived, errors, messages, paths, store

ACCT = "wxid_testacct01_ab12"
CHAT = "wxid_friend01"
T0 = 1700000000


def _key(lid: int) -> str:
    return messages.row_key(ACCT, hashlib.md5(CHAT.encode()).hexdigest(),
                            "message_0.db", lid)


def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path / "home")
    with store.open() as con:
        store.upsert_batch(con, [messages.make_row(
            account=ACCT, chat_md5=hashlib.md5(CHAT.encode()).hexdigest(),
            chat_id=CHAT, shard="message_0.db", local_id=1, ts=T0 + 1,
            msg_type=1, sender_id=None, sender_name=None, is_self=0,
            content="周末去爬山", media_hint=None)])


def test_claims_start_as_candidates_and_need_evidence(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    claim = derived.add_claim(ACCT, "周末去爬山", evidence=[_key(1)])
    assert claim["status"] == "candidate", "自动抽取不存在：新增永远是候选"
    assert [e["msg_key"] for e in claim["evidence"]] == [_key(1)]

    with pytest.raises(errors.DerivedInputError):
        derived.add_claim(ACCT, "无证据", evidence=[])
    with pytest.raises(errors.DataNotFound):
        derived.add_claim(ACCT, "证据不存在", evidence=[_key(99)])


def test_status_machine_distinguishes_confirmed_retracted_disputed(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    a = derived.add_claim(ACCT, "事实 A", evidence=[_key(1)])
    b = derived.add_claim(ACCT, "事实 B", evidence=[_key(1)])

    confirmed = derived.set_claim_status(a["claim_id"], "confirmed", now=10)
    assert confirmed["status"] == "confirmed"
    with pytest.raises(errors.DerivedInputError):
        derived.set_claim_status(a["claim_id"], "candidate"), "确认后不能退回候选"

    disputed = derived.set_claim_status(a["claim_id"], "disputed",
                                        conflicts_with=b["claim_id"], now=11)
    assert disputed["status"] == "disputed"
    assert disputed["conflicts_with"] == b["claim_id"]

    retracted = derived.set_claim_status(a["claim_id"], "retracted", now=12)
    assert retracted["status"] == "retracted"

    with pytest.raises(errors.DerivedInputError):
        derived.set_claim_status(a["claim_id"], "disputed",
                                 conflicts_with=a["claim_id"])
    with pytest.raises(errors.DataNotFound):
        derived.set_claim_status(a["claim_id"], "disputed",
                                 conflicts_with="nope")


def test_listing_and_hard_delete(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    a = derived.add_claim(ACCT, "可删除", evidence=[_key(1)])
    assert derived.list_claims(ACCT)["total"] == 1
    assert derived.delete_claim(a["claim_id"]) is True
    assert derived.list_claims(ACCT)["total"] == 0
    with store.open() as con:
        assert con.execute("SELECT count(*) FROM fact_evidence").fetchone()[0] == 0


def test_purge_retracts_a_claim_whose_evidence_is_destroyed(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    with store.open() as con:
        con.execute(
            "INSERT INTO fact_claim(claim_id, account, chat_id, claim, status,"
            " created_at, updated_at) VALUES('c1',? ,NULL,'无会话主张','confirmed',1,1)",
            (ACCT,))
        con.execute(
            "INSERT INTO fact_evidence(claim_id, msg_key, role, created_at)"
            " VALUES('c1', ?, 'support', 1)", (_key(1),))
        con.commit()
        store.delete_chat(con, account=ACCT, chat_id=CHAT)
        con.commit()
    claim = derived.get_claim("c1")
    assert claim["status"] == "retracted", "证据被销毁后主张不得继续成立"
    assert claim["note"] == "evidence_purged"
    assert claim["evidence"] == []
