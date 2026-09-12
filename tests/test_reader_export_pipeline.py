"""合成库集成测试：store 幂等/水位线、reader 解码、search 双模式、export 全量/增量。

不依赖真实微信数据：用 sqlcipher 把合成明文库加密成「微信式」库
（与 test_decrypt_pipeline 同法），再 monkeypatch 路径常量让
reader/export/search 走真实全链路（真实 PBKDF2 派生 + 真实解密）。
"""
import hashlib
import pathlib
import sqlite3
import subprocess
import uuid
from types import SimpleNamespace

import pytest
import zstandard

from agent_wechat import export, keys, paths, reader, search, store

from helpers import mk_row

PW = "0123456789abcdef0123456789abcdef"   # 文本 passphrase；sqlcipher 默认 KDF 与 keys.derive_key 一致
ACCT = "wxid_t_a1b2"
SELF = "wxid_t"                            # bare_wxid(ACCT)
FRIEND = "wxid_friend01"
GROUP = "888@chatroom"
T0 = 1700000000


def md5(name: str) -> str:
    return hashlib.md5(name.encode()).hexdigest()


def enc_db(dest, tables: dict) -> None:
    """tables: {表名: (DDL, [行元组])}。建明文库 → sqlcipher 导出加密 → 删明文。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    for suf in ("", "-wal", "-shm"):          # 支持重建：先清掉旧目标（sqlcipher 不覆盖非空库）
        pathlib.Path(str(dest) + suf).unlink(missing_ok=True)
    plain = dest.parent / (dest.name + ".plain")
    con = sqlite3.connect(plain)
    for name, (ddl, rows) in tables.items():
        con.execute(ddl)
        if rows:
            ph = ",".join("?" * len(rows[0]))
            con.executemany(f"INSERT INTO {name} VALUES ({ph})", rows)
    con.commit()
    con.close()
    r = subprocess.run(["sqlcipher", str(plain),
                        f"ATTACH DATABASE '{dest}' AS e KEY '{PW}';",
                        "SELECT sqlcipher_export('e');", "DETACH DATABASE e;"],
                       capture_output=True, text=True)
    plain.unlink()
    assert r.returncode == 0 and dest.exists(), r.stderr


def build_shard(wx, acct: str, shard: str, msg_tables: dict, users: list[str]) -> None:
    """msg_tables: {chat_username: [(local_id, local_type, sender_rowid, ts, content, ct)]}。"""
    tables = {"Name2Id": ("CREATE TABLE Name2Id (user_name TEXT)", [(u,) for u in users])}
    for chat, rows in msg_tables.items():
        tables[f"Msg_{md5(chat)}"] = (
            f"CREATE TABLE Msg_{md5(chat)} (local_id INTEGER, local_type INTEGER, "
            "real_sender_id INTEGER, create_time INTEGER, message_content, "
            "WCDB_CT_message_content INTEGER)", rows)
    enc_db(wx / acct / "db_storage" / "message" / shard, tables)


@pytest.fixture(scope="module")
def wechat(tmp_path_factory):
    """一套完整的合成微信目录：session/contact/message_0/message_1（均加密）。"""
    root = tmp_path_factory.mktemp("wxenv")
    wx = root / "xwechat_files"
    home = root / "home"
    home.mkdir(mode=0o700)
    ds = wx / ACCT / "db_storage"
    enc_db(ds / "session" / "session.db", {
        "SessionTable": ("CREATE TABLE SessionTable (username TEXT)",
                         [(FRIEND,), (GROUP,), (SELF,)]),
    })
    enc_db(ds / "contact" / "contact.db", {
        "name2id": ("CREATE TABLE name2id (username TEXT)",
                    [(FRIEND,), ("888@chatroom",)]),
        "contact": ("CREATE TABLE contact (username TEXT, alias TEXT, remark TEXT, "
                    "nick_name TEXT, delete_flag INTEGER)",
                    [(FRIEND, "f_alias", "张三", "zc", None),
                     ("wxid_friend02", None, "李四", "ls", None),
                     ("888@chatroom", None, "项目群", "pm", None),
                     ("wxid_blocked", None, "已删除", "bl", 1)]),
    })
    # shard0：friend 5 条（文本/本人/高比特位类型/zstd/appmsg）+ group 1 条（群前缀）
    friend_rows0 = [
        (1, 1, 1, T0 + 10, "你好，最近怎么样", 0),
        (2, 1, 2, T0 + 11, "在吗", 0),                        # sender=Name2Id rowid 2 → 本人
        (3, 0x100000001, 1, T0 + 12, "高比特位类型掩码", 0),    # & 0xffffffff == 1 → 文本
        (4, 1, 1, T0 + 13, zstandard.compress("压缩消息内容".encode()), 4),
        (5, 49, 1, T0 + 14, "<msg><title>分享标题</title></msg>", 0),
    ]
    group_rows0 = [(1, 1, 1, T0 + 15, f"{FRIEND}:\n开会啦", 0)]
    build_shard(wx, ACCT, "message_0.db",
                {FRIEND: friend_rows0, "888@chatroom": group_rows0}, [FRIEND, SELF])
    # shard1：friend 2 条更早的（跨分片合并）
    friend_rows1 = [(1, 1, 1, T0 - 20, "很久之前的话一", 0),
                    (2, 1, 2, T0 - 19, "很久之前的话二", 0)]
    build_shard(wx, ACCT, "message_1.db", {FRIEND: friend_rows1}, [FRIEND, SELF])
    return SimpleNamespace(wx=wx, home=home)


@pytest.fixture()
def env(wechat, monkeypatch):
    """把路径常量指到合成目录，并落 passphrase 缓存。每个测试独立 index.db。"""
    monkeypatch.setattr(paths, "XWECHAT_FILES", wechat.wx)
    monkeypatch.setattr(paths, "HOME_DIR", wechat.home)
    monkeypatch.setattr(keys, "CACHE_FILE", wechat.home / "passphrase.json")
    monkeypatch.setattr(store, "DB_PATH", wechat.home / f"index-{uuid.uuid4().hex[:8]}.db")
    keys.save_cached_hex(SELF, PW.encode().hex())
    return wechat


# ---------- store ----------

def test_store_upsert_idempotent_and_watermark(tmp_path, monkeypatch):
    # 必须隔离 HOME_DIR：store.connect → paths.ensure_home_dirs() 会对真实
    # ~/.agent-wechat 做 mkdir 并调用一次 tmutil addexclusion
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path)
    with store.connect(tmp_path / "i.db") as con:
        batch = [mk_row("a", FRIEND, 1, "一", shard="message_0.db"),
                 mk_row("a", FRIEND, 2, "二", shard="message_0.db")]
        store.upsert_batch(con, batch)
        store.upsert_batch(con, batch)                       # 重复写不产生重复行
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
        store.upsert_batch(con, [mk_row("a", FRIEND, 2, "二（已编辑）", shard="message_0.db")])
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
        assert con.execute("SELECT content FROM messages WHERE local_id=2").fetchone()[0] == "二（已编辑）"
        wm = {(s, m): v for s, m, v in con.execute(
            "SELECT shard, chat_md5, max_local_id FROM watermark")}
        assert wm[("message_0.db", md5(FRIEND))] == 2
        store.upsert_batch(con, [mk_row("a", FRIEND, 1, "一", shard="message_0.db")])  # 旧行不回退水位线
        wm = {(s, m): v for s, m, v in con.execute(
            "SELECT shard, chat_md5, max_local_id FROM watermark")}
        assert wm[("message_0.db", md5(FRIEND))] == 2


def test_store_open_really_closes_on_exit(tmp_path, monkeypatch):
    """store.open 是连接生命周期的 owner：退出后连接必须真的关闭。

    反证 `with sqlite3.connect(...)` 那种写法只是 commit/rollback、不 close——
    退出后再执行任何语句必须抛 ProgrammingError。
    """
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "i.db")
    with store.open() as con:
        assert con.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError):
        con.execute("SELECT 1")


def test_store_open_commits_on_success(tmp_path, monkeypatch):
    """成功路径退出即提交：写入的行在连接关闭后仍读得到。"""
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "i.db")
    with store.open() as con:
        store.upsert_batch(con, [mk_row("a", FRIEND, 1, "落盘", shard="message_0.db")])
    with store.open() as con:
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_store_open_rolls_back_on_error(tmp_path, monkeypatch):
    """异常路径退出即回滚：未提交的写入不得落盘。"""
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "i.db")
    with pytest.raises(RuntimeError, match="boom"):
        with store.open() as con:
            con.execute("INSERT INTO watermark(account, shard, chat_md5, max_local_id, updated_at)"
                        " VALUES (?,?,?,?,?)", ("a", "message_0.db", "deadbeef", 1, 0))
            raise RuntimeError("boom")
    with store.open() as con:
        assert con.execute("SELECT count(*) FROM watermark").fetchone()[0] == 0


def test_store_connect_rows_are_rows_and_positional_access_still_works(tmp_path, monkeypatch):
    """connect 默认给 sqlite3.Row：列名与位置取值都要能用（既有消费者按位置取值）。"""
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path)
    with store.open(tmp_path / "i.db") as con:
        assert con.row_factory is sqlite3.Row
        store.upsert_batch(con, [mk_row("a", FRIEND, 1, "一", shard="message_0.db")])
        r = con.execute("SELECT account, content FROM messages").fetchone()
        assert r["account"] == "a" and r["content"] == "一"      # 按列名
        assert tuple(r) == ("a", "一")                            # 按位置（Row 是 tuple 子类）
        assert r[0] == "a" and r[1] == "一"
        assert store.counts(con)["messages"] == 1                 # 既有 r[0] 消费者
        assert store.get_watermark(con, "a", "message_0.db", md5(FRIEND)) == 1


# ---------- reader ----------

def test_contacts_and_resolve(env):
    r = reader.MessageReader(ACCT)
    cs = r.contacts(query="张三")
    assert [c["username"] for c in cs] == [FRIEND]
    assert cs[0]["kind"] == "direct"
    assert all(c["username"] != "wxid_blocked" for c in r.contacts())   # delete_flag=1 被过滤
    assert [c["username"] for c in r.contacts() if c["kind"] == "group"] == ["888@chatroom"]
    assert r.resolve_chat("项目群")[0]["username"] == "888@chatroom"
    assert len(r.resolve_chat("friend")) == 2               # friend01/friend02 → 歧义
    assert r.display_name("888@chatroom") == "项目群"


def test_contacts_cached_per_reader(env):
    r = reader.MessageReader(ACCT)
    calls = 0
    orig = r.contacts

    def counting(*a, **k):
        nonlocal calls
        calls += 1
        return orig(*a, **k)

    r.contacts = counting
    r.resolve_chat("项目群")
    r.display_name("888@chatroom")
    r.resolve_chat("friend")
    assert calls == 1                        # 同一 reader 内只解密一次 contact.db


def test_resolve_one(env):
    r = reader.MessageReader(ACCT)
    assert r.resolve_one("888@chatroom") == ("888@chatroom", [])   # id 形态直通
    assert r.resolve_one("gh_official") == ("gh_official", [])
    chat_id, cands = r.resolve_one("项目群")
    assert chat_id == "888@chatroom" and len(cands) == 1
    chat_id, cands = r.resolve_one("friend")
    assert chat_id is None and len(cands) == 2                   # 歧义返回候选
    chat_id, cands = r.resolve_one("不存在的名字")
    assert chat_id is None and cands == []


def test_recent_cross_shard_decode(env):
    r = reader.MessageReader(ACCT)
    msgs = r.recent(FRIEND, limit=50)
    assert len(msgs) == 7                                   # 两分片合并
    assert [m.ts for m in msgs] == sorted(m.ts for m in msgs)
    assert msgs[0].content == "很久之前的话一"                # 最早
    by_lid = {(m.shard, m.local_id): m for m in msgs}
    self_msg = by_lid[("message_0.db", 2)]
    assert self_msg.is_self and self_msg.sender_id == SELF
    hi = by_lid[("message_0.db", 3)]
    assert hi.msg_type == 1 and hi.content == "高比特位类型掩码"   # 类型掩码
    zs = by_lid[("message_0.db", 4)]
    assert zs.content == "压缩消息内容"                       # zstd 解压
    app = by_lid[("message_0.db", 5)]
    assert app.content == "[appmsg: 分享标题]" and app.media_hint == app.content
    g = r.recent("888@chatroom", limit=10)
    assert len(g) == 1
    assert g[0].sender_id == FRIEND and g[0].sender_name == FRIEND  # 群前缀解析
    assert g[0].content == "开会啦"


# ---------- export ----------

def test_export_full_idempotent(env):
    assert export.run_export(ACCT, full=True) == 8          # friend 5+2 + group 1
    with store.connect() as con:
        c = store.counts(con)
        assert c["messages"] == 8 and c["chats"] == 2
        wm = {(s, m): v for s, m, v in con.execute(
            "SELECT shard, chat_md5, max_local_id FROM watermark WHERE account=?", (ACCT,))}
    assert wm[("message_0.db", md5(FRIEND))] == 5
    assert wm[("message_1.db", md5(FRIEND))] == 2
    assert wm[("message_0.db", md5("888@chatroom"))] == 1
    assert export.run_export(ACCT, full=True) == 8
    with store.connect() as con:
        assert store.counts(con)["messages"] == 8            # 幂等
    assert export.run_export(ACCT) == 0                      # 增量无新数据


def test_full_export_leaves_other_accounts_watermark_alone(env):
    """全量导出只重导本账号，不该清掉其它账号的水位线。

    缺陷背景：`DELETE FROM watermark` 没有 account 过滤，对账号 A 做 --full 会连带
    清掉账号 B 的水位线，使 B 的下一次增量导出白跑一次全量（upsert 幂等，不丢数据，
    但纯属浪费）。本机有 2 个账号目录，所以这不是纸面问题。
    """
    foreign = "wxid_other_account"
    with store.connect() as con:
        con.execute("INSERT INTO watermark(account, shard, chat_md5, max_local_id, updated_at)"
                    " VALUES (?,?,?,?,?)", (foreign, "message_0.db", "deadbeef", 7, 0))
        con.commit()

    export.run_export(ACCT, full=True)

    with store.connect() as con:
        wm = dict(con.execute("SELECT account, max_local_id FROM watermark").fetchall())
    assert wm.get(foreign) == 7, "别的账号的水位线被全量导出清掉了"
    assert ACCT in wm, "本账号的水位线应被重置后重新推进"


def test_search_modes_and_isolation(env):
    export.run_export(ACCT, full=True)
    res = search.search("压缩消息", account=ACCT)            # 4 字 → fts
    assert res["mode"] == "fts" and res["total"] == 1
    assert res["hits"][0]["chat_id"] == FRIEND
    assert "压缩" in res["hits"][0]["snip"]
    res2 = search.search("压缩", account=ACCT)               # 2 字 → LIKE 兜底
    assert res2["mode"] == "like" and res2["total"] == 1
    assert search.search("开会", chat_id="888@chatroom", account=ACCT)["total"] == 1
    assert search.search("开会", chat_id=FRIEND, account=ACCT)["total"] == 0
    with store.connect() as con:                             # 多账号隔离
        store.upsert_batch(con, [mk_row("other", FRIEND, 99, "别的账号的压缩消息", shard="message_0.db")])
    assert search.search("别的账号", account=ACCT)["total"] == 0
    assert search.search("别的账号", account="other")["total"] == 1


# ---------- export 增量正确性（双分片） ----------

def test_incremental_export_covers_non_latest_shard(tmp_path, monkeypatch):
    wx = tmp_path / "xwechat_files"
    home = tmp_path / "home"
    home.mkdir()
    x = "wxid_x01"
    rows0 = {x: [(1, 1, 1, T0 + 1, "分片零一", 0), (2, 1, 1, T0 + 2, "分片零二", 0),
                 (3, 1, 1, T0 + 3, "分片零三", 0)]}
    rows1 = {x: [(i, 1, 1, T0 - 100 + i, f"分片壹{i}", 0) for i in range(1, 51)]}
    build_shard(wx, ACCT, "message_0.db", rows0, [x, SELF])
    build_shard(wx, ACCT, "message_1.db", rows1, [x, SELF])
    monkeypatch.setattr(paths, "XWECHAT_FILES", wx)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")
    keys.save_cached_hex(SELF, PW.encode().hex())

    assert export.run_export(ACCT, full=True) == 53
    rows0[x].append((4, 1, 1, T0 + 4, "分片零四（新增）", 0))
    build_shard(wx, ACCT, "message_0.db", rows0, [x, SELF])   # 重建分片0（新盐无妨）
    assert export.run_export(ACCT) == 1                        # 增量应只补这 1 条
