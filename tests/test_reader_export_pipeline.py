"""合成库集成测试：store 幂等/水位线、reader 解码、search 双模式、export 全量/增量。

不依赖真实微信数据：用 sqlcipher 把合成明文库加密成「微信式」库
（与 test_decrypt_pipeline 同法），再 monkeypatch 路径常量让
reader/export/search 走真实全链路（真实 PBKDF2 派生 + 真实解密）。
"""
import hashlib
import pathlib
import shutil
import sqlite3
import subprocess
import uuid
from types import SimpleNamespace

import pytest
import zstandard

from agent_wechat import errors, export, keys, paths, reader, search, store, usecases

from helpers import ext_buffer, member, mk_row

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


def drift_tree(wx, acct: str, *, drop_table: str | None = None,
               drop_column: str | None = None) -> None:
    """造一棵接触 contact.db 的树，其中某个表或某列被拿掉，模拟微信升级。

    `drop_column` 只在 `contact` 表上支持（那是列级漂移最现实的场景）。
    """
    ds = wx / acct / "db_storage"
    contact_cols = {"username": "TEXT", "alias": "TEXT", "remark": "TEXT",
                    "nick_name": "TEXT", "delete_flag": "INTEGER"}
    contact_vals = {"username": "wxid_a", "alias": None, "remark": "张三",
                    "nick_name": "zc", "delete_flag": None}
    if drop_column:
        contact_cols.pop(drop_column)
    tables = {
        "name2id": ("CREATE TABLE name2id (username TEXT)", [("wxid_a",)]),
        "contact": (f"CREATE TABLE contact "
                    f"({', '.join(f'{c} {t}' for c, t in contact_cols.items())})",
                    [tuple(contact_vals[c] for c in contact_cols)]),
        "chat_room": ("CREATE TABLE chat_room (id INTEGER PRIMARY KEY, username TEXT, "
                      "owner TEXT, ext_buffer BLOB)", []),
    }
    if drop_table:
        tables.pop(drop_table, None)
    enc_db(ds / "contact" / "contact.db", tables)


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
        "chat_room": ("CREATE TABLE chat_room (id INTEGER PRIMARY KEY, username TEXT, "
                      "owner TEXT, ext_buffer BLOB)",
                      [(2, GROUP, FRIEND,
                        ext_buffer(member(FRIEND, "群里的张三"),
                                   member("wxid_friend02"),
                                   member(SELF)))]),
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
    with store.open(tmp_path / "i.db") as con:
        batch = [mk_row("a", FRIEND, 1, "一", shard="message_0.db"),
                 mk_row("a", FRIEND, 2, "二", shard="message_0.db")]
        store.upsert_batch(con, batch)
        store.upsert_batch(con, batch)                       # 重复写不产生重复行
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
        store.upsert_batch(con, [mk_row("a", FRIEND, 2, "二（已编辑）", shard="message_0.db")])
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
        assert con.execute("SELECT content FROM messages WHERE local_id=2").fetchone()[0] == "二（已编辑）"
        wm = store.load_export_watermarks(con, "a")
        assert wm[("message_0.db", md5(FRIEND))] == 2
        store.upsert_batch(con, [mk_row("a", FRIEND, 1, "一", shard="message_0.db")])  # 旧行不回退水位线
        wm = store.load_export_watermarks(con, "a")
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


def test_load_export_watermarks_shape_multi_shard_multi_chat(tmp_path, monkeypatch):
    """批量读取导出水位线：键为 (shard, chat_md5)，值为 max_local_id，且账号隔离。"""
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path)
    with store.open(tmp_path / "i.db") as con:
        store.upsert_batch(con, [
            mk_row("a", FRIEND, 1, "一", shard="message_0.db"),
            mk_row("a", FRIEND, 2, "二", shard="message_0.db"),
            mk_row("a", FRIEND, 1, "另分片", shard="message_1.db"),
            mk_row("a", GROUP, 3, "群", shard="message_0.db"),
            mk_row("b", FRIEND, 9, "别的账号", shard="message_0.db"),
        ])
        assert store.load_export_watermarks(con, "a") == {
            ("message_0.db", md5(FRIEND)): 2,
            ("message_1.db", md5(FRIEND)): 1,
            ("message_0.db", md5(GROUP)): 3,
        }
        assert store.load_export_watermarks(con, "b") == {("message_0.db", md5(FRIEND)): 9}


def test_load_export_watermarks_empty_account(tmp_path, monkeypatch):
    """没导出过的账号读出来是空 dict（不是 None/异常）。"""
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path)
    with store.open(tmp_path / "i.db") as con:
        assert store.load_export_watermarks(con, "never_seen") == {}


def test_clear_export_watermarks_only_clears_named_account(tmp_path, monkeypatch):
    """B2 语义护栏：重置水位线只清指定账号，别的账号必须原样保留。"""
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path)
    with store.open(tmp_path / "i.db") as con:
        store.upsert_batch(con, [mk_row("a", FRIEND, 1, "一", shard="message_0.db"),
                                 mk_row("b", FRIEND, 2, "二", shard="message_0.db")])
        store.clear_export_watermarks(con, "a")
        assert store.load_export_watermarks(con, "a") == {}
        assert store.load_export_watermarks(con, "b") == {("message_0.db", md5(FRIEND)): 2}


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
    assert g[0].sender_id == FRIEND and g[0].sender_name == "群里的张三"  # 群前缀解析 + 名册换名
    assert g[0].content == "开会啦"


# ---------- export ----------

def test_export_full_idempotent(env):
    assert export.run_export(ACCT, full=True) == 8          # friend 5+2 + group 1
    with store.open() as con:
        c = store.counts(con)
        assert c["messages"] == 8 and c["chats"] == 2
        wm = store.load_export_watermarks(con, ACCT)
    assert wm[("message_0.db", md5(FRIEND))] == 5
    assert wm[("message_1.db", md5(FRIEND))] == 2
    assert wm[("message_0.db", md5("888@chatroom"))] == 1
    assert export.run_export(ACCT, full=True) == 8
    with store.open() as con:
        assert store.counts(con)["messages"] == 8            # 幂等
    assert export.run_export(ACCT) == 0                      # 增量无新数据


def test_full_export_leaves_other_accounts_watermark_alone(env):
    """全量导出只重导本账号，不该清掉其它账号的水位线。

    缺陷背景：`DELETE FROM watermark` 没有 account 过滤，对账号 A 做 --full 会连带
    清掉账号 B 的水位线，使 B 的下一次增量导出白跑一次全量（upsert 幂等，不丢数据，
    但纯属浪费）。本机有 2 个账号目录，所以这不是纸面问题。
    """
    foreign = "wxid_other_account"
    with store.open() as con:
        con.execute("INSERT INTO watermark(account, shard, chat_md5, max_local_id, updated_at)"
                    " VALUES (?,?,?,?,?)", (foreign, "message_0.db", "deadbeef", 7, 0))
        con.commit()

    export.run_export(ACCT, full=True)

    with store.open() as con:
        foreign_wm = store.load_export_watermarks(con, foreign)
        own_wm = store.load_export_watermarks(con, ACCT)
    assert foreign_wm.get(("message_0.db", "deadbeef")) == 7, "别的账号的水位线被全量导出清掉了"
    assert own_wm, "本账号的水位线应被重置后重新推进"


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
    with store.open() as con:                             # 多账号隔离
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


# ---------- 显示名名册 ----------

def test_roster_reads_group_members_with_optional_nickname(env):
    """群昵称是**可选**的：fixture 里 FRIEND 有群名片、另两人没有（真实数据就是这样）。"""
    r = reader.MessageReader(ACCT)
    got = dict(((c, k), d) for c, k, d in r.roster())
    assert got[(GROUP, FRIEND)] == "群里的张三"        # 群昵称优先于备注「张三」
    assert got[(GROUP, "wxid_friend02")] == "李四"     # 无群昵称 → 备注
    assert got[(GROUP, SELF)] == SELF                  # 不在 contact 表里 → username


def test_roster_includes_private_chat_rows(env):
    """私聊行 (chat_id=对方, member_key=对方)——同一条 join 覆盖两种会话。"""
    r = reader.MessageReader(ACCT)
    got = dict(((c, k), d) for c, k, d in r.roster())
    assert got[(FRIEND, FRIEND)] == "张三"
    assert got[("wxid_friend02", "wxid_friend02")] == "李四"


def test_roster_skips_deleted_contacts(env):
    """`delete_flag=1` 的是已删除联系人——与 reader.contacts() 同一条过滤规则。"""
    r = reader.MessageReader(ACCT)
    keys = {k for _, k, _ in r.roster()}
    assert "wxid_blocked" not in keys


def test_roster_does_not_use_the_search_limit(env, tmp_path, monkeypatch):
    """**这条钉一个实测发现的坑**：`_contacts_all_cached()` 走 `contacts(limit=5000)`，
    而真实 contact 表有 12,782 行——照着它实现会静默漏掉一半人。夹具造的小数据代不了
    这个：必须真的超过 5000 行才测得出 LIMIT 的存在。

    所以这条用一个**本测试自己造的**独立库（不污染 module 级的 wechat fixture）：
    `_open` 拼的是 `paths.db_storage(acct) / rel`，而 `db_storage` 就是
    `XWECHAT_FILES / acct / "db_storage"`——把 `XWECHAT_FILES` 指过去即可。
    """
    n = 5001
    root = tmp_path / "bulkwx"
    enc_db(root / ACCT / "db_storage" / "contact" / "contact.db", {
        "name2id": ("CREATE TABLE name2id (username TEXT)", [(FRIEND,)]),
        "contact": ("CREATE TABLE contact (username TEXT, alias TEXT, remark TEXT, "
                    "nick_name TEXT, delete_flag INTEGER)",
                    [(f"wxid_bulk{i:05d}", None, f"名字{i}", None, None)
                     for i in range(n)]),
        "chat_room": ("CREATE TABLE chat_room (id INTEGER PRIMARY KEY, username TEXT, "
                      "owner TEXT, ext_buffer BLOB)", []),
    })
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    rows = reader.MessageReader(ACCT).roster()
    private = [x for x in rows if x[0] == x[1]]
    assert len(private) == n, f"联系人被截断了：拿到 {len(private)}，应为 {n}"


def test_recent_uses_display_names(env):
    """实时路径（recent）用内存名册换名——它不走索引库，join 覆盖不到（spec §2.4）。"""
    r = reader.MessageReader(ACCT)
    msgs = r.recent(GROUP, limit=10)
    group_msgs = [m for m in msgs if m.sender_id == FRIEND]
    assert group_msgs, "前提：fixture 的群里有 FRIEND 发的消息"
    assert group_msgs[0].sender_name == "群里的张三"


def test_recent_private_messages_use_display_names(env):
    """私聊也要覆盖：名册里有 (chat_id=对方, member_key=对方) 那一行。"""
    r = reader.MessageReader(ACCT)
    msgs = r.recent(FRIEND, limit=10)
    from_friend = [m for m in msgs if not m.is_self]
    assert from_friend, "前提：fixture 里有对方发的消息"
    assert from_friend[0].sender_name == "张三"


def test_iter_all_still_emits_raw_usernames(env):
    """**这是「新旧混杂」的守卫。** `iter_all` 是 export 的数据源，它必须继续产出
    wxid——查询时由 join 统一覆盖，存量与新数据才走同一条路。没有这条，
    一次「顺手重构」把 `_apply_roster` 挪进 `_decode_row`，全量测试仍然全绿，
    而索引库从此一半名字一半 wxid。
    """
    r = reader.MessageReader(ACCT)
    raw = [row for batch in r.iter_all(GROUP) for row in batch]
    group_rows = [x for x in raw if x.get("sender_id") == FRIEND]
    assert group_rows, "前提：iter_all 里有 FRIEND 的消息"
    assert group_rows[0]["sender_name"] == FRIEND, \
        f"iter_all 不该换名（换名会让新导出与存量分道扬镳）：{group_rows[0]}"


# ---------- usecases：懒刷新 ----------

def test_search_refreshes_roster_when_stale(env):
    """第一次查询时名册为空 → 自动填充。用户不需要任何初始化仪式（spec §7）。"""
    usecases.search_history("开会", contact=GROUP, account=ACCT)
    with store.open() as con:
        n = con.execute("SELECT COUNT(*) FROM display_names").fetchone()[0]
    assert n > 0, "名册没有被填充"


def test_search_does_not_refresh_within_ttl(env, monkeypatch):
    usecases.search_history("开会", contact=GROUP, account=ACCT)
    calls = []
    real = reader.MessageReader.roster
    monkeypatch.setattr(reader.MessageReader, "roster",
                        lambda self: (calls.append(1), real(self))[1])
    usecases.search_history("开会", contact=GROUP, account=ACCT)
    assert calls == [], "TTL 内不该重读 contact.db"


def test_search_still_works_when_roster_refresh_fails(env, monkeypatch):
    """**刷新是尽力而为**：contact.db 读不到不该让检索也查不了——那是两条独立的能力。
    没有这条，`_refresh_roster` 里那个 try/except 被删掉也不会有测试红（spec §9）。"""
    def boom(self):
        raise errors.DataNotFound("模拟：contact.db 不可用")

    monkeypatch.setattr(reader.MessageReader, "roster", boom)
    res = usecases.search_history("开会", contact=GROUP, account=ACCT)
    assert "hits" in res, f"刷新失败不该阻断检索：{res}"


def test_search_still_works_when_roster_hits_sqlite_error(env, tmp_path, monkeypatch):
    """`_refresh_roster` 只吞 `AgentWeChatError`。contact.db 的结构漂移（ARCHITECTURE §7：
    微信升级可能改变库结构）若在 reader 层不翻成项目异常，就会穿透 `_refresh_roster`
    把**索引检索**一起打挂——而索引库是好的，那是两条独立的能力。终审实测复现过：
    没有 `name2id` 表的 contact.db 让 `reader.roster()` 抛
    `sqlite3.OperationalError: no such table: name2id`。

    **必须驱动真的 `roster()`**（自造一棵带漂移的树，同
    `test_roster_does_not_use_the_search_limit` 的隔离法）：把 `roster` monkeypatch 成
    「抛 sqlite3 错误」的桩，等于绕开被修的那一层——那样这条测试在修与不修时**都是红的**，
    因为它考的是「`_refresh_roster` 要不要吞任意裸异常」，而不是本模块的契约。
    """
    root = tmp_path / "driftwx"
    shutil.copytree(env.wx, root)
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    export.run_export(ACCT, full=True)                # 先建好索引库（此时 contact.db 还好）
    enc_db(root / ACCT / "db_storage" / "contact" / "contact.db", {
        "chat_room": ("CREATE TABLE chat_room (id INTEGER PRIMARY KEY, username TEXT, "
                      "owner TEXT, ext_buffer BLOB)", [(1, GROUP, None, None)]),
        "contact": ("CREATE TABLE contact (username TEXT, alias TEXT, remark TEXT, "
                    "nick_name TEXT, delete_flag INTEGER)", [(FRIEND, None, "张三", None, 0)]),
    })                                                # **唯独没有 name2id 表**

    with pytest.raises(errors.DataNotFound) as ei:    # reader 层负责翻成项目异常
        reader.MessageReader(ACCT).roster()
    assert "name2id" in str(ei.value), "原始 sqlite 错误该留在消息里，便于定位"

    res = usecases.search_history("开会", contact=GROUP, account=ACCT)
    assert res["hits"], f"contact.db 结构漂移不该打挂索引检索：{res}"


def test_refresh_does_not_write_when_roster_is_empty(env, monkeypatch):
    """**空名册不得写库**。`replace_roster([])` 会清空该账号名册**并盖上 refreshed_at**——
    roster_is_stale 从此返回 False，整个 TTL 内不再重试：一次意外的空名册（contact.db
    半损坏但没抛）会把名册锁死成「全是 wxid」，而刷新被记成了**成功**（T2 review 的
    Minor 1）。`_refresh_roster` 里「读成功」与「真没东西」不可区分，所以按「空 → 这次
    刷新没发生」处理：不 replace_roster、不记时间戳，下次查询再试。

    代价（已裁定接受，见 ledger Ruling 13）：没有群也没有联系人的账号每次查询都会
    重读一次 contact.db——正确性无损，纯性能，且该场景罕见。"""
    calls = []
    real = store.replace_roster

    def spy(con, account, rows):
        calls.append(rows)
        return real(con, account, rows)

    monkeypatch.setattr(store, "replace_roster", spy)
    monkeypatch.setattr(reader.MessageReader, "roster", lambda self: [])
    usecases.search_history("开会", contact=GROUP, account=ACCT)
    assert calls == [], f"空名册不该触发写库：{calls}"


# ---------- 结构漂移 ----------
# 这几条自造漂移树，所以要把 XWECHAT_FILES 指过去；`env` 负责另一半环境——
# passphrase 缓存（`MessageReader.__init__` 就要用，缺了连构造都过不去）。
# env 先指到合成目录，测试体再指到漂移树，后者生效。

def test_contacts_raises_schema_drift_when_table_missing(env, tmp_path, monkeypatch):
    """`contacts` 是唯一来源，漂移时**不降级**——返回空会让用户以为「没有这个人」。"""
    root = tmp_path / "wx"
    drift_tree(root, ACCT, drop_table="contact")
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    with pytest.raises(errors.SchemaDrift) as ei:
        reader.MessageReader(ACCT).contacts()
    assert ei.value.capability == "contacts" and ei.value.table == "contact"
    assert ei.value.missing_columns == ()          # 空 = 表不存在


def test_contacts_raises_schema_drift_and_names_the_missing_column(env, tmp_path, monkeypatch):
    root = tmp_path / "wx"
    drift_tree(root, ACCT, drop_column="nick_name")
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    with pytest.raises(errors.SchemaDrift) as ei:
        reader.MessageReader(ACCT).contacts()
    assert ei.value.missing_columns == ("nick_name",)


def test_recent_raises_schema_drift_when_message_columns_change(env, tmp_path, monkeypatch):
    """`messages` 也是唯一来源——分片是**按时间分区**的，跳过它会让时间线上出现
    用户看不出来的空洞（spec §10 的排除项）。"""
    root = tmp_path / "wx"
    enc_db(root / ACCT / "db_storage" / "message" / "message_0.db", {
        "Name2Id": ("CREATE TABLE Name2Id (user_name TEXT)", [(FRIEND,)]),
        f"Msg_{md5(FRIEND)}": (
            # 故意缺 WCDB_CT_message_content
            f"CREATE TABLE Msg_{md5(FRIEND)} (local_id INTEGER, local_type INTEGER, "
            "real_sender_id INTEGER, create_time INTEGER, message_content BLOB)",
            [(1, 1, 1, T0, "hi")]),
    })
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    with pytest.raises(errors.SchemaDrift) as ei:
        reader.MessageReader(ACCT).recent(FRIEND, limit=5)
    assert "WCDB_CT_message_content" in ei.value.missing_columns


def test_export_raises_schema_drift_when_message_columns_change(env, tmp_path, monkeypatch):
    """`export` 是 spec §8 列的第三条「不降」路径（`contacts`/`recent`/`export`），
    但它走的是**另一条代码路径**（`iter_all`）——只钉 `recent` 的话，`iter_all` 里的
    接入被删掉也不会有测试红。分片按时间分区，跳过它导出的时间线会出现看不出来的空洞。"""
    root = tmp_path / "wx"
    enc_db(root / ACCT / "db_storage" / "message" / "message_0.db", {
        "Name2Id": ("CREATE TABLE Name2Id (user_name TEXT)", [(FRIEND,)]),
        f"Msg_{md5(FRIEND)}": (
            # 故意缺 WCDB_CT_message_content
            f"CREATE TABLE Msg_{md5(FRIEND)} (local_id INTEGER, local_type INTEGER, "
            "real_sender_id INTEGER, create_time INTEGER, message_content BLOB)",
            [(1, 1, 1, T0, "hi")]),
    })
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    with pytest.raises(errors.SchemaDrift) as ei:
        list(reader.MessageReader(ACCT).iter_all())     # 生成器：必须消费才触发
    assert ei.value.capability == "messages"
    assert "WCDB_CT_message_content" in ei.value.missing_columns


def test_username_pool_degrades_when_one_source_drifts(env, tmp_path, monkeypatch):
    """四个源汇集，少一个仍有三个（spec §6）——**不抛**，跳过它继续。"""
    root = tmp_path / "wx"
    drift_tree(root, ACCT, drop_table="name2id")        # contact 源没了
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    r = reader.MessageReader(ACCT)
    pool = r.username_pool()                            # 不该抛
    assert FRIEND not in pool                           # 别的源也没造，但至少不炸
    assert any(d.capability == "username_pool" for d in r._drift)


def test_a_bare_sql_column_typo_still_crashes(env, tmp_path, monkeypatch):
    """**本设计最重要的一条测试**（spec §5）：探测只保**声明过的**列。
    把某条 SQL 的列名改成声明里没有的名字，探测会放行、SQL 必须崩——
    自家的 bug 不该被降级掩盖。没有这条，整个设计就退化成「统一 catch 一切」。"""
    root = tmp_path / "wx"
    drift_tree(root, ACCT)                              # 结构完全正常
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)

    def broken_contacts(self, *a, **k):
        with self._open("contact/contact.db") as con:
            self._check("contacts", "contact/contact.db", con)      # 探测通过
            con.execute("SELECT nick_nam FROM contact").fetchall()  # 拼错的列名

    monkeypatch.setattr(reader.MessageReader, "contacts", broken_contacts)
    with pytest.raises(sqlite3.OperationalError):
        reader.MessageReader(ACCT).contacts()           # 探测放行，SQL 崩


def test_inspect_still_works_in_a_drifted_tree(env, tmp_path, monkeypatch):
    """`inspect` 的职责就是面对漂移——它是用户的诊断工具，降级等于把它变成废物。
    它直连解密后的库、不经过 reader 的探测，所以这条测试同时也钉住了「探测接入
    没有顺手把 inspect 也管起来」。"""
    from agent_wechat import cli                     # 函数内 import，不动文件头
    root = tmp_path / "wx"
    drift_tree(root, ACCT, drop_table="contact")     # 结构是坏的
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    monkeypatch.setattr(reader, "MessageReader",           # 若它被间接调用，必须炸
                        lambda *a: pytest.fail("inspect 不该经过 reader"))
    with pytest.raises(SystemExit) as ei:
        # --account 必须显式给：省略时 resolve_account 走 pgrep/lsof 认**真机**的
        # 登录账号，测试就寄生在本机微信状态上（密码缓存由 env fixture 落好）。
        cli.main(["inspect", "contact/contact.db", "--account", ACCT])
    assert ei.value.code == 0, "漂移环境下 inspect 仍该成功"
