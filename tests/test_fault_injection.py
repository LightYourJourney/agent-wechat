"""A 快层矩阵（一）：keys / tasks / store / purge 的故障注入用例。

用例本身是判据：`inj.assert_fired()` 证明注入点真的打中；失败的用例先按发现登记
（R2-A-NNN），不在本文件里放松断言。
"""
import json

import pytest

from agent_wechat import errors, keys, paths, purge, store, tasks
from faultkit import Fault, expect_project_error, install

CASES = [
    # (flow, api, fault, keyword)
    ("keys", "write_text", "ENOSPC", "无法写入|No space left"),
    ("keys", "write_text", "EACCES", "无法写入|Permission denied"),
    ("keys", "write_text", "SHORT_WRITE", "无法写入|No space left"),
    ("tasks", "os.replace", "REPLACE_FAIL", "权限|Permission denied"),
    ("tasks", "write_text", "ENOSPC", "空间|No space left"),
    ("store", "sqlite.connect", "SQLITE_LOCKED", "锁定|locked"),
    ("store", "sqlite.connect", "SQLITE_CORRUPT", "损坏|malformed"),
]


def _home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


# id 只取前三列：关键词列含中文，拼进去会被 pytest 转义成 \uXXXX，nodeid 就不可选中了。
@pytest.mark.parametrize("flow,api,fault,keyword", CASES,
                         ids=["-".join(str(v) for v in case[:3]) for case in CASES])
def test_fault_matrix(tmp_path, monkeypatch, flow, api, fault, keyword):
    home = _home(tmp_path)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")

    def op():
        if flow == "keys":
            keys.save_cached_hex("wxid_testacct01_ab12", "00" * 32)
        elif flow == "tasks":
            tasks.start("noop", lambda tid: 0)
        else:
            with store.open() as con:
                con.commit()

    inj = install([Fault(api, fault, nth=1)])
    try:
        expect_project_error(op, keyword=keyword)
        inj.assert_fired()
        if flow == "store":
            # 不变量 2：连接失败不得留下半成品 index.db（否则 touch 式回归会静默通过）。
            assert not store.DB_PATH.exists(), "连接失败不得留下半成品 index.db"
        # 不变量 2：不得留下临时文件；密钥缓存若存在必须可解析（不得半截 JSON）。
        # 扫 tmp_path 而非 home：tasks 的 tmp 落在 TASKS_FILE 同级（conftest 把它钉在
        # tmp_path/tasks.json），只扫 home 的话这条断言对 tasks 流程永远不可能红。
        assert not list(tmp_path.rglob("*.tmp")), "失败后不得留下临时文件"
        if flow == "keys" and (home / "passphrase.json").exists():
            json.loads((home / "passphrase.json").read_text())
    finally:
        inj.undo()


def test_purge_delete_failure_is_reentrant(tmp_path, monkeypatch):
    """不变量 2/4（销毁口径）：删除失败时目标不得消失；故障消失后重试必须删干净。

    purge 的 index 产物来自 `store.db_paths()`（随 `store.DB_PATH`），不是 HOME 里猜的路径。
    """
    home = _home(tmp_path)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    db = store.db_paths()[0]
    db.write_text("seed")
    plan = purge.plan(frozenset({"index"}))
    inj = install([Fault("unlink", "EACCES", path=str(db))])
    try:
        purge.execute(plan)
        inj.assert_fired()
        assert db.exists(), "删除失败时目标不得消失（不得半删）"
        assert db.read_text() == "seed", "删除失败时既有内容不得被截断"
    finally:
        inj.undo()
    purge.execute(purge.plan(frozenset({"index"})))
    assert not db.exists(), "重试必须删干净"


def test_keys_write_failure_preserves_existing_cache(tmp_path, monkeypatch):
    """不变量 4：既有密钥缓存必须在写入失败后原样可解析——不得被删掉或截断。"""
    home = _home(tmp_path)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    seeded = {"accounts": {"old": {"passphrase_hex": "aa" * 32, "captured_at": 0}}}
    (home / "passphrase.json").write_text(json.dumps(seeded))   # 必须在 install 之前种
    inj = install([Fault("write_text", "ENOSPC", nth=1)])
    try:
        expect_project_error(
            lambda: keys.save_cached_hex("wxid_testacct01_ab12", "00" * 32),
            keyword="无法写入|No space left")
        inj.assert_fired()
    finally:
        inj.undo()
    assert (home / "passphrase.json").exists(), "写入失败不得删掉既有缓存"
    data = json.loads((home / "passphrase.json").read_text())
    assert "old" in data["accounts"], "既有缓存条目不得丢失"
    assert data["accounts"]["old"] == seeded["accounts"]["old"], "既有条目必须原样保留"


def test_store_failure_preserves_existing_rows(tmp_path, monkeypatch):
    """不变量 4：注入前种入数据；失败后既有行必须原样还在。"""
    home = _home(tmp_path)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")
    from agent_wechat import messages
    row = messages.make_row(account="wxid_testacct01_ab12", chat_md5="m" * 32,
                            chat_id="c", shard="message_0.db", local_id=1, ts=1,
                            msg_type=1, sender_id="u", sender_name=None, is_self=0,
                            content="seed", media_hint=None)
    with store.open() as con:
        store.upsert_batch(con, [row]); con.commit()
    inj = install([Fault("commit", "EIO", nth=1)])
    try:
        with pytest.raises(errors.AgentWeChatError):
            with store.open() as con:
                store.upsert_batch(con, [messages.make_row(
                    account="wxid_testacct01_ab12", chat_md5="m" * 32, chat_id="c",
                    shard="message_0.db", local_id=2, ts=2, msg_type=1, sender_id="u",
                    sender_name=None, is_self=0, content="two", media_hint=None)])
                con.commit()
    finally:
        inj.undo()
    with store.open() as con:
        n = con.execute("SELECT count(*) FROM messages").fetchone()[0]
    assert n == 1, "失败后既有数据必须原样保留（不得出现第二条，也不得丢第一条）"
