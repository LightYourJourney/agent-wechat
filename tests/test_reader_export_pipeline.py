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

from agent_wechat import (errors, export, keys, messages, paths, reader, search, store,
                          usecases, vector)

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
    """msg_tables: {chat_username: [(local_id, local_type, sender_rowid, ts, content, ct)]}。

    真实 `Msg_` 表还有 packed_info_data（docs/schema-4.1.13.md）——T5 把它加进 messages
    能力声明（Ruling 24）后，夹具必须跟着有这一列，否则健康夹具全体误报漂移。
    既有 6 元组行补 None；图片/视频消息直接传第 7 个元素（packed 的 32-hex bytes）。
    `server_id` 同在声明里（`reader.iter_parts_input` 读它）——夹具补上这一列，既有行补 None。
    """
    tables = {"Name2Id": ("CREATE TABLE Name2Id (user_name TEXT)", [(u,) for u in users])}
    for chat, rows in msg_tables.items():
        tables[f"Msg_{md5(chat)}"] = (
            f"CREATE TABLE Msg_{md5(chat)} (local_id INTEGER, local_type INTEGER, "
            "real_sender_id INTEGER, create_time INTEGER, message_content, "
            "WCDB_CT_message_content INTEGER, packed_info_data BLOB, server_id INTEGER)",
            [tuple(r) + (None,) * (8 - len(r)) for r in rows])
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
        # 首行与 appmsg 那行给到第 8 列（server_id）：`iter_parts_input` 读它，引用回指
        # 全靠它。其余行留 6 元组 → `build_shard` 补 None（也就是真机里 NULL 的形态）。
        (1, 1, 1, T0 + 10, "你好，最近怎么样", 0, None, 9001),
        (2, 1, 2, T0 + 11, "在吗", 0),                        # sender=Name2Id rowid 2 → 本人
        (3, 0x100000001, 1, T0 + 12, "高比特位类型掩码", 0),    # & 0xffffffff == 1 → 文本
        (4, 1, 1, T0 + 13, zstandard.compress("压缩消息内容".encode()), 4),
        (5, 49, 1, T0 + 14, "<msg><title>分享标题</title></msg>", 0, None, 9005),
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


def test_store_media_refs_upsert_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path)
    # **必须**隔离 DB_PATH：store.DB_PATH 在 import 期绑定真实 HOME_DIR，
    # 只 patch paths.HOME_DIR 拦不住它——会把假数据写进真实 index.db
    monkeypatch.setattr(store, "DB_PATH", tmp_path / f"index-{uuid.uuid4().hex[:8]}.db")
    with store.open() as con:
        store.upsert_media_refs(con, [("k1", "image", "aaaa", "bbbb")])
        store.upsert_media_refs(con, [("k1", "image", "cccc", "bbbb")])   # 幂等更新
        row = con.execute("SELECT media_kind, file_key, xml_md5 FROM media_refs WHERE msg_key='k1'").fetchone()
        assert con.execute("SELECT count(*) FROM media_refs").fetchone()[0] == 1
        store.upsert_media_refs(con, [])                                  # 空列表是 no-op
    assert tuple(row) == ("image", "cccc", "bbbb")


def test_store_upsert_media_refs_does_not_own_transaction(tmp_path, monkeypatch):
    """media_refs 与 messages 同事务（spec §4.3）：调用方回滚时 refs 必须一起消失。

    判别性：`upsert_media_refs` 若自己 commit 一下，回滚后这行仍在——同事务的保证就没了
    （T5 的 `run_export` 正是靠 upsert_batch 的 commit 把 refs 和 messages 收进一个事务）。
    """
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "i.db")
    with pytest.raises(RuntimeError, match="boom"):
        with store.open() as con:
            store.upsert_media_refs(con, [("k1", "image", "aaaa", None)])
            assert con.execute("SELECT count(*) FROM media_refs").fetchone()[0] == 1
            raise RuntimeError("boom")
    with store.open() as con:
        assert con.execute("SELECT count(*) FROM media_refs").fetchone()[0] == 0


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


def test_recent_orders_same_second_messages_by_local_id(wechat, env):
    """EA-016：`recent` 的排序键此前只有 `m.ts`——**同一秒**的消息顺序于是取决于扫描顺序，
    而扫描是 `ORDER BY local_id DESC`（:439 的 `order_desc=True`）+ 稳定排序 ⇒ 同秒的两条
    保持**降序**到达序，「时间正序」这条契约在同秒上不成立（与索引侧 `ORDER BY ts` 的顺序
    也不一致）。键加上 `local_id` 后确定：**先发的在前**。

    把 `key=lambda m: (m.ts, m.local_id)` 换回 `key=lambda m: m.ts` 本测试必红。
    """
    t = T0 + 100                       # 比既有行都新，稳稳落进 limit 窗口
    shard = wechat.wx / ACCT / "db_storage" / "message" / "message_2.db"
    build_shard(wechat.wx, ACCT, "message_2.db",
                {FRIEND: [(1, 1, 1, t, "先发的", 0), (2, 1, 1, t, "后发的", 0)]}, [FRIEND, SELF])
    try:
        msgs = reader.MessageReader(ACCT).recent(FRIEND, limit=2)
        assert [m.content for m in msgs] == ["先发的", "后发的"]
    finally:
        # `wechat` 是 **module 级**共享夹具：新加的分片必须清掉，否则同模块后跑的用例
        # （`test_recent_cross_shard_decode` 断言 7 条）会看到它——实测踩过。
        for p in shard.parent.glob("message_2.db*"):
            p.unlink()


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


# ---------- R9：群前缀在文本上是「发送者」，在非文本上只是残留 ----------
#
# 实测（本机 index.db）：群聊里带 `<X>:\n` 形状的非文本行共 1,953 条——1,952 条 head 以
# `@chatroom` 结尾（全是 type=10000，前缀该清），1 条是 appmsg 标题自带 `:\n`（假阳性，不能碰）。
# 前者正文多为原始 `<sysmsg …>` XML，其中 1 条含真实 `<aeskey>`——那就是 R9 本体。

ROOM = "12345678901@chatroom"          # 合成群 id；真机是同形态的 <digits>@chatroom
XML_SECRET = "1f2e3d4c5b6a7988"        # 造一条「正文带凭据」的夹具；不是真凭据
SYSMSG_XML = ('<?xml version="1.0"?><sysmsg type="sysmsgtemplate"><sysmsgtemplate>'
              '<content_template type="tmpl_type_profile">'
              f'<link aeskey="{XML_SECRET}"/>'
              "</content_template></sysmsgtemplate></sysmsg>")
# **真机的多数派形态**：根就是 `<sysmsg>`、**没有 `<?xml` 声明**（实测群 `sysmsg` 里 1,960 行
# 是这样、带声明的只有 1 行）。夹具必须两种都编，否则「回落判据认得出来」只对被编出来的那种成立。
SYSMSG_XML_NO_PROLOG = ('<sysmsg type="mmchatroomtopmsg"><mmchatroomtopmsg>'
                        f'<chatroomname><![CDATA[{ROOM}]]></chatroomname>'
                        "</mmchatroomtopmsg></sysmsg>")


@pytest.fixture()
def room_env(env, tmp_path, monkeypatch):
    """只装本组用例的合成树；用独立 root，不动模块级 `wechat` 夹具的行数。"""
    root = tmp_path / "wx-sysmsg"
    build_shard(root, ACCT, "message_0.db", {
        ROOM: [(1, 10000, 0, T0 + 100, f"{ROOM}:\n{SYSMSG_XML}", 0),       # 前缀 + XML（R9）
               (2, 10000, 0, T0 + 101, f"{ROOM}:\n你撤回了一条消息", 0),    # 前缀 + 普通文案
               (3, 10000, 0, T0 + 102, SYSMSG_XML, 0),                    # **不**带前缀（对照）
               (4, 1, 1, T0 + 103, f"{FRIEND}:\n开会啦", 0),               # 群文本（回归）
               # 假阳性：appmsg 标题自带 ":\n"。head 是 XML 而不是群 id，不该被剥。
               (5, 49, 1, T0 + 104,
                "<msg><appmsg><title>alpha:\n  beta: v1</title></appmsg></msg>", 0),
               # 真机多数派形态：无 `<?xml` 声明（见下面那条用例）
               (6, 10000, 0, T0 + 105, f"{ROOM}:\n{SYSMSG_XML_NO_PROLOG}", 0)],
        # 单聊：正文**形状像群前缀**才会真的检验那道 `@chatroom` 门——
        # 用不含 ":\n" 的文案（如「对方已通过…」）写这条断言是**假护栏**，它不可能红。
        FRIEND: [(1, 1, 1, T0 + 110, "注意:\n今晚不吃饭", 0),
                 (2, 10000, 1, T0 + 111, "对方已通过你的好友请求", 0)],
    }, [FRIEND, SELF])
    # `recent` → `_apply_roster` 会读 contact.db；表留空 = 名册解析不出任何名字，
    # 断言就只依赖解码本身（否则 sender_name 会被备注名替换，测的就不是 R9 了）。
    enc_db(root / ACCT / "db_storage" / "contact" / "contact.db", {
        "name2id": ("CREATE TABLE name2id (username TEXT)", []),
        "contact": ("CREATE TABLE contact (username TEXT, alias TEXT, remark TEXT, "
                    "nick_name TEXT, delete_flag INTEGER)", []),
        "chat_room": ("CREATE TABLE chat_room (id INTEGER PRIMARY KEY, username TEXT, "
                      "owner TEXT, ext_buffer BLOB)", []),
    })
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    return root


def _by_lid(msgs):
    return {m.local_id: m for m in msgs}


@pytest.fixture()
def msgs(room_env):
    """本组用例共用的两次读取结果（`recent` 每次都会重新解密分片，取一次就够）。"""
    r = reader.MessageReader(ACCT)
    return {c: _by_lid(r.recent(c, limit=10)) for c in (ROOM, FRIEND)}


def test_group_sysmsg_prefix_does_not_leak_raw_xml(msgs):
    """R9 本体：前缀后面是 XML 时，落进 content 的必须是占位符，不是原始 XML。

    只断言最终正文——先前那句 `XML_SECRET not in content` 是**不能失败的断言**：
    上面一条已经钉死了 content 恰好是 `[system]`，它不可能再含凭据。
    """
    assert msgs[ROOM][1].content == "[system]", "凭据随原始 XML 漏进了 content"


def test_group_sysmsg_prefix_is_stripped_but_never_taken_as_sender(msgs):
    """残留前缀要清掉正文；但 head 是**群 id**，绝不能当发送者——那会污染 sender_id。"""
    assert msgs[ROOM][2].content == "你撤回了一条消息"
    for lid in (1, 2, 6):                      # 三条带前缀的 sysmsg 逐一确认
        assert msgs[ROOM][lid].sender_id != ROOM, f"local_id={lid} 把群 id 当成了发送者"
        assert msgs[ROOM][lid].is_self == 0


def test_group_sysmsg_without_prefix_is_unchanged(msgs):
    """对照组：不带前缀的 sysmsg 本来就靠回落判据兜住了。"""
    assert msgs[ROOM][3].content == "[system]"


def test_group_sysmsg_without_xml_prolog_also_becomes_a_placeholder(msgs):
    """根是 `<sysmsg>`、**没有 `<?xml` 声明**时也要换成占位符，不能只认 `<?xml`。

    实测这种形态才是**多数派**：群 `sysmsg` 里带 `<?xml` 声明的只有 1 行（含凭据那条），
    根直接是 `<sysmsg …>` 的有 1,960 行。放宽到 `startswith("<")` 后受影响共 2,221 行，
    **全是 `msg_type=10000`**；以 `<` 开头的**人类文本**（实测 18 行 `type=1`，有人粘了
    HTML、有人写 `<video>就能显示hdr`）走文本那条分支，`hint` 都不查，不受影响。
    """
    assert msgs[ROOM][6].content == "[system]"


def test_group_text_still_extracts_its_sender(msgs):
    """回归：文本那条语义没被动过——head 仍是发送者，仍被剥掉。"""
    m = msgs[ROOM][4]
    assert m.content == "开会啦"
    assert m.sender_id == FRIEND and m.sender_name == FRIEND
    assert m.is_self == 0


def test_single_chat_content_is_untouched(msgs):
    """那道 `@chatroom` 门：单聊里形状**一模一样**的正文也不许剥、不许当发送者。

    这条夹具是刻意选的——`注意:\\n今晚不吃饭` 与群前缀 `wxid_x:\\n正文` 同形。
    先前这里放的是「对方已通过你的好友请求」（不含 `:\\n`），那条断言**不可能失败**：
    删掉 `chat_id.endswith("@chatroom")` 那道门它照样绿，是假护栏。
    """
    m = msgs[FRIEND][1]
    assert m.content == "注意:\n今晚不吃饭"
    assert m.sender_id != "注意" and m.sender_name is None
    assert msgs[FRIEND][2].content == "对方已通过你的好友请求"


def test_appmsg_title_containing_colon_newline_is_untouched(msgs):
    """收窄的界限：群 appmsg 的 head 是 XML 不是群 id，不该被剥。

    放开成「所有非文本一律剥」会把标题从 `alpha:\\n  beta: v1` 削成 `beta: v1`，
    实测本机恰好 1 条这样的行——清 1,952 条不能顺手改坏第 1,953 条。
    """
    assert "alpha" in msgs[ROOM][5].content and "beta" in msgs[ROOM][5].content


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

def test_chat_export_renders_media_blocks_without_stubs(env, tmp_path):
    """EA-026①：**不起桩**的集成——真 `ChatContext` + 真 `message_media`：索引里种一行
    图片消息 ＋ 一条 media_refs → `chat_export` 真跑 → 产物里出现媒体块、counts 记到那一类。

    （此前的集成面被 `chat_env` 的桩整个替换，「怎么调」落进测试网外。）
    """
    from agent_wechat import messages as msg_mod
    from agent_wechat import usecases

    export.run_export(ACCT, full=True)
    with store.open() as con:
        rows = con.execute(
            "SELECT chat_id, chat_md5, shard, local_id, ts FROM messages LIMIT 1").fetchone()
        assert rows, "夹具必须有消息行"
        chat_id, chat_md5, shard, lid, ts = rows
        key = f"{ACCT}|{chat_md5}|{shard}|{lid}"
        con.execute(
            "INSERT OR IGNORE INTO messages (msg_key, account, chat_md5, chat_id, shard,"
            " local_id, ts, msg_type, is_self, content, exported_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"{ACCT}|{chat_md5}|{shard}|9001", ACCT, chat_md5, chat_id, shard, 9001, ts,
             msg_mod.TYPE_IMAGE, 0, "[图片]", 0))
        store.upsert_media_refs(con, [(f"{ACCT}|{chat_md5}|{shard}|9001", "image",
                                       "aa" * 16, "bb" * 16)])
        con.commit()

    out = tmp_path / "out"
    usecases.chat_export(chat_id, out_dir=str(out), account=ACCT)
    html = (out / chat_md5 / "chat" / "index.html").read_text(encoding="utf-8")
    # F1：断言必须**落在媒体块本身**——`"media" in html`/`"ph" in html` 会命中静态 CSS
    # （chat_html.py 的 `.media img{…}` / `.ph{…}` 无条件输出），整段调用删掉也照样绿。
    # 媒体块的可判别形态：`<div class="ph">[图片 · <理由>]</div>`（四态里的某一句）。
    assert 'class="ph">[图片 · ' in html, html[:600]
    # 反向自证：把产物里的媒体块按「桩回退」形态渲染（states 空）则必然没有这段，
    # 也就是上面这条断言不是恒真——M7（删调用）/M7b（换回桩）两种改坏都会让它红。


def test_recent_limit_zero_is_empty_not_everything(env):
    """EA-080：`recent(…, limit=0)` 曾因 `[-0:]`（= 全量切片）返回**全部**消息；
    而 SQL 侧的 `if limit:` 把 0 当「不拼 LIMIT」——两处口径相反。0 必须返回空列表。
    """
    got = reader.MessageReader(ACCT).recent(FRIEND, limit=0)
    assert got == []


def test_iter_all_respects_the_batch_size(env, tmp_path, monkeypatch):
    """EA-111：批次切分（`store.BATCH` 压到 2）：每批 ≤ 批大小、无空批、各批合起来不重不漏。

    批是**按分片（及其表）**刷的，不是全局计数——所以「批数 = ⌈n/批大小⌉」不成立
    （首稿这么写，实测 5 ≠ 4 被本用例自己否掉）；钉住的是可证伪的那三条。
    删掉批次刷新那段的 `rows = []` 初始化，本用例必红。
    """
    # 定制树：**一张表出两批**（4 行、批大小 2），中间夹两行图片——
    # 这样「flush 处的 `refs = []`」漏重置才会表现为「第二批的 refs 重复」
    # （`rows`/`refs` 是按表初始化的；一张表只 flush 一次时删掉那行不可观测，复审 F5）。
    root = tmp_path / "batchwx"
    shutil.copytree(env.wx, root)
    build_shard(root, ACCT, "message_0.db", {FRIEND: [
        (1, 3, 1, T0, zstandard.compress(IMG_XML.encode()), 4, b"E" * 32),
        (2, 1, 1, T0 + 1, "文本一", 0),
        (3, 3, 1, T0 + 2, zstandard.compress(IMG_XML.encode()), 4, b"F" * 32),
        (4, 1, 1, T0 + 3, "文本二", 0),
    ]}, [FRIEND, SELF])
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    monkeypatch.setattr(store, "BATCH", 2)
    batches = list(reader.MessageReader(ACCT).iter_all())
    rows = [r for b, _refs in batches for r in b]
    assert rows, "夹具必须有行"
    assert len(rows) == len({r["msg_key"] for r in rows}), "批次边界上丢行或重复"
    assert all(0 < len(b) <= 2 for b, _ in batches), [len(b) for b, _ in batches]
    # F5：refs 半边此前无护栏（旧夹具没有媒体行，删掉 flush 处的 `refs = []` 全量仍绿）。
    # 现在同一张表出两批：各批 refs 拼起来必须恰好 2 条、不重复（漏重置 → 第二批重复）。
    refs = [ref for _b, rf in batches for ref in rf]
    assert len(refs) == 2, f"两张图片各一条 ref：{refs}"
    assert len(refs) == len({r[0] for r in refs}), "refs 在各批间重复（`refs = []` 未重置）"


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


def test_contact_lookup_does_not_use_the_search_limit(env, tmp_path, monkeypatch):
    """**同一个坑的另一半**：`_contacts_all_cached()` 走 `contacts(limit=5000)`，
    而 `display_name` 与 `resolve_chat` 都扫这个窗口——**窗口外的联系人按真名解析不出来**。

    真机实测（contact 表 12,799 行、窗口 5,000）：抽 40 个窗口外的联系人按真名 `resolve_one`
    **命中 0**。也就是 `recent --contact 张三` / `search --contact 张三` / `chat export --chat 张三`
    这条主路径，对 61% 的联系人是失效的。

    与 `test_roster_does_not_use_the_search_limit` 同源同形：**必须真的超过 5000 行**
    才测得出 LIMIT 的存在，小数据代不了。

    回归：把 `_contacts_all_cached` 改回 `contacts(limit=5000)`，这条会红。
    """
    n = 5001
    root = tmp_path / "bulkwx"
    enc_db(root / ACCT / "db_storage" / "contact" / "contact.db", {
        "name2id": ("CREATE TABLE name2id (username TEXT)", [(FRIEND,)]),
        "contact": ("CREATE TABLE contact (username TEXT, alias TEXT, remark TEXT, "
                    "nick_name TEXT, delete_flag INTEGER)",
                    [(f"wxid_bulk{i:05d}", None, f"名字{i:05d}", None, None)
                     for i in range(n)]),
        "chat_room": ("CREATE TABLE chat_room (id INTEGER PRIMARY KEY, username TEXT, "
                      "owner TEXT, ext_buffer BLOB)", []),
    })
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    r = reader.MessageReader(ACCT)

    # 排序是 (remark, nick_name)，升序；`名字05000` 是第 5001 行 → 窗口**外**
    assert r.display_name("wxid_bulk05000") == "名字05000", "窗口外的联系人拿不到展示名"
    assert r.resolve_one("名字05000")[0] == "wxid_bulk05000", "窗口外的联系人按真名解析不出来"


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



def test_iter_all_resolves_a_chat_whose_name_lives_in_another_shard(tmp_path, monkeypatch):
    """EA-018 的回滚守卫（复审 F1 的构造性反例固化）：某会话的消息在 shard0，而它的用户名
    **只在 shard1 的 Name2Id**（不在 session / contact）。

    名字池必须收**全部来源**——把它收窄成「静态源 + 本分片」（629088f 的那版优化），
    本用例必红（该会话落成 `md5:<hex>`，而它是真实存在的形态：1:1 会话里我发、对方在
    另一分片才回过消息）。"""
    wx = tmp_path / "xwechat_files"
    home = tmp_path / "home"
    home.mkdir()
    lone, other = "wxid_x01", FRIEND       # 两个都是本文件既有、已登记的占位符
    rows0 = {lone: [(1, 1, 1, T0 + 1, "只在分片零的正文", 0)]}
    rows1 = {other: [(1, 1, 1, T0 + 2, "另一会话", 0)]}
    build_shard(wx, ACCT, "message_0.db", rows0, [other, SELF])   # users 里**没有** lone
    build_shard(wx, ACCT, "message_1.db", rows1, [lone, other, SELF])
    monkeypatch.setattr(paths, "XWECHAT_FILES", wx)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")
    keys.save_cached_hex(SELF, PW.encode().hex())

    rows = [r for batch, _refs in reader.MessageReader(ACCT).iter_all() for r in batch]
    cids = {r["chat_id"] for r in rows}
    assert lone in cids, f"名字在别的分片里也必须解出：{sorted(cids)}"
    assert all(not c.startswith("md5:") for c in cids), sorted(cids)


def _tree_with_a_name2id_less_newest_shard(env, tmp_path, monkeypatch):
    """env 的树 + 一个新分片 message_2.db（编号最大 = 最近）：有**别的会话**的 Msg_ 表，
    没有目标会话的表，也**没有 `Name2Id`**——EA-017 的触发形态。"""
    root = tmp_path / "wx_no_n2i"
    shutil.copytree(env.wx, root)
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    cols = ("local_id INTEGER, local_type INTEGER, real_sender_id INTEGER, "
            "create_time INTEGER, message_content, WCDB_CT_message_content INTEGER, "
            "packed_info_data BLOB, server_id INTEGER")
    enc_db(root / ACCT / "db_storage" / "message" / "message_2.db", {
        f"Msg_{md5(GROUP)}": (f"CREATE TABLE Msg_{md5(GROUP)} ({cols})",
                              [(1, 1, 1, T0 + 30, "别的会话的正文", 0, None, None)])})


def test_recent_survives_a_shard_without_name2id_and_without_the_chat(env, tmp_path, monkeypatch):
    """EA-017（recent 侧）：目标会话在**最新分片**里没有表、那个分片还没有 `Name2Id`——
    修复前 `n2i = self._name2id(con)` 在 `if tables:` **之外**无条件执行，裸
    `sqlite3.OperationalError: no such table: Name2Id` 从这里穿出 core 层（守卫白设）。
    修后该分片整体跳过，继续往更早的分片找，照常返回。

    把缩进挪回去（无条件读 Name2Id），本用例必红。
    """
    _tree_with_a_name2id_less_newest_shard(env, tmp_path, monkeypatch)
    msgs = reader.MessageReader(ACCT).recent(FRIEND, limit=10)
    assert len(msgs) == 7, [m.content for m in msgs]     # shard0 的 5 条 + shard1 的 2 条
    assert msgs[-1].ts == T0 + 14


def test_iter_all_survives_a_shard_without_name2id_and_records_the_drift(env, tmp_path,
                                                                        monkeypatch):
    """EA-017（iter_all 侧）：同一棵树上不抛；缺 `Name2Id` 的分片漂移经 `username_pool`
    记进 `_drift`（由 T5 呈现给用户）。两条路径的**可见性不同**——`recent` 不碰名册池，
    没有漂移可记；这正是条目那条「断言抛 SchemaDrift」的验收写岔了的地方。
    """
    _tree_with_a_name2id_less_newest_shard(env, tmp_path, monkeypatch)
    r = reader.MessageReader(ACCT)
    rows = [row for batch, _ in r.iter_all(FRIEND) for row in batch]
    assert len(rows) == 7, [x["content"] for x in rows]
    assert any(d.capability == "username_pool" and d.table == "Name2Id"
               for d in r._drift), [(d.capability, d.table) for d in r._drift]


def test_username_pool_degrades_when_a_name_only_source_is_undecryptable(env, tmp_path,
                                                                        monkeypatch):
    """EA-079：降级面要覆盖「文件在、**解不开**」（`DecryptError`）——`session` / `contact`
    是**纯名字源**，坏了应跳过（池里仍有其余源的名字，解析不出的会话走既有的 `md5:<hex>`
    回落）；**分片源**坏掉仍必须抛：跳过会让时间线静默出现空洞（两条方向都要钉住）。

    把 `username_pool` 里纯名字源的 except 去掉 `DecryptError`，第一条断言必红。
    """
    root = tmp_path / "wx_pool"
    shutil.copytree(env.wx, root)
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    (root / ACCT / "db_storage" / "session" / "session.db").write_bytes(b"\x00" * 8192)

    r = reader.MessageReader(ACCT)
    pool = r.username_pool()                               # 不许抛
    assert FRIEND in pool, "一个名字源解不开 ≠ 什么都查不到（其余源照常供名字）"
    # F2（T8 复审）：跳过一个坏源必须留一条**非漂移**降级记录——否则会话名静默落
    # `md5:<hex>`，用户只知道名字丢了一些、不知道去修哪个库，CLI 连一行 ⚠️ 都没有。
    dets = usecases.collect_degradations(r)
    assert [(d.capability, d.drift) for d in dets] == [("username_pool", False)], dets
    # F7（T8 复验）：坏的是**哪个库**要看得见（信封只带 {capability, effect}，不写进
    # effect 的话两个名字源都坏时只有两条逐字相同的说明）。
    assert "session/session.db" in dets[0].effect, dets[0].effect

    (root / ACCT / "db_storage" / "message" / "message_0.db").write_bytes(b"\x00" * 8192)
    with pytest.raises(errors.DecryptError):
        reader.MessageReader(ACCT).username_pool()         # 分片源：必须响亮地抛


def test_iter_all_chat_id_set_is_unchanged(env):
    """名字池不变式：合成夹具里的会话必须全部解出真名，退化形态（`md5:<hex>`）一条都不许出现。

    （EA-018 的「名字收窄到本分片」优化**已回滚**——它会让「名字只在别的分片 Name2Id 里」
    的会话退化，见下面那条反例用例。）"""
    rows = [r for batch, _refs in reader.MessageReader("wxid_t_a1b2").iter_all()
            for r in batch]
    assert rows
    assert all(not r["chat_id"].startswith("md5:") for r in rows), \
        sorted({r["chat_id"] for r in rows if r["chat_id"].startswith("md5:")})


def test_iter_all_still_emits_raw_usernames(env):
    """**这是「新旧混杂」的守卫。** `iter_all` 是 export 的数据源，它必须继续产出
    wxid——查询时由 join 统一覆盖，存量与新数据才走同一条路。没有这条，
    一次「顺手重构」把 `_apply_roster` 挪进 `_decode_row`，全量测试仍然全绿，
    而索引库从此一半名字一半 wxid。
    """
    r = reader.MessageReader(ACCT)
    raw = [row for rows, _ in r.iter_all(GROUP) for row in rows]
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


def test_search_still_works_when_roster_degrades_on_drift(env, tmp_path, monkeypatch):
    """`_refresh_roster` 只吞 `AgentWeChatError`，所以 reader 层给它的必须是认得的形状。
    contact.db 的结构漂移（ARCHITECTURE §7：微信升级可能改变库结构）若以裸
    `sqlite3.OperationalError` 的形态穿过去，就会把**索引检索**一起打挂——而索引库是好的，
    那是两条独立的能力。

    T4 之前这里钉的是「reader 把 sqlite 错误翻成 `DataNotFound`」，构造就是下面这棵树
    （没有 `name2id` 表 ⇒ 裸 `no such table: name2id`）。T4 之后**结构漂移不再抛**：
    `name2id` 是声明过的源，缺了就逐源降级（并记进 `_drift`，由 T5 呈现给用户）。
    所以这条测试钉的是新形状：漂移的名册不抛、其余源照常产出、检索完全不受影响。
    「文件不存在」那一支仍由 `test_search_still_works_when_roster_refresh_fails` 钉着
    （`_open` 抛 `DataNotFound`）。

    **必须驱动真的 `roster()`**（自造一棵带漂移的树，同
    `test_roster_does_not_use_the_search_limit` 的隔离法）：把 `roster` monkeypatch 成
    「抛 sqlite3 错误」的桩，等于绕开被考的那一层——那样这条测试在修与不修时**都是红的**，
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

    r = reader.MessageReader(ACCT)
    rows = r.roster()                                 # 漂移 → 逐源降级，不该抛
    assert (FRIEND, FRIEND, "张三") in rows, "contact 源是好的，私聊行照常产出"
    assert {d.table for d in r._drift if d.capability == "roster"} == {"name2id"}

    res = usecases.search_history("开会", contact=GROUP, account=ACCT)
    assert res["hits"], f"contact.db 结构漂移不该打挂索引检索：{res}"


def test_search_survives_a_bare_sqlite_error_from_the_roster(env, tmp_path, monkeypatch):
    """**探测覆盖不到**的失败也要被「尽力而为」接住（spec §5 最后三行）。

    `name2id` 变成 `WITHOUT ROWID`：声明只保 `username` 这一列，探测于是放行——`rowid` 是
    隐式列，`PRAGMA table_info` 根本不列它，声明覆盖不到它。下面那条
    `JOIN name2id n ON n.rowid = cr.id` 照样崩，抛的是裸 `OperationalError`。

    `roster()` 不接它（spec §5：自家 bug 与库损坏都不该被吞成「名册为空」），所以接网点
    必须在 `_refresh_roster`——那里才是「刷新失败不该让检索一起挂」这句承诺的兑现处。
    没有这条，把 except 收回 `AgentWeChatError` 也不会有测试红，而索引库明明好好的。

    构造必须驱动真的 `roster()`（理由同上面的漂移那条）：猴补一个「抛 sqlite 错误」的桩
    等于绕开被考的那一层，测试在修与不修时都是红的。第一段断言 `roster()` 本身照旧抛，
    钉住「安全网不在 reader 里」——它一旦被挪进去，这条会红。
    """
    root = tmp_path / "norowidwx"
    shutil.copytree(env.wx, root)
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    export.run_export(ACCT, full=True)                # 先建好索引库（此时 contact.db 还好）
    enc_db(root / ACCT / "db_storage" / "contact" / "contact.db", {
        "name2id": ("CREATE TABLE name2id (id INTEGER PRIMARY KEY, username TEXT) WITHOUT ROWID",
                    [(1, FRIEND)]),
        "contact": ("CREATE TABLE contact (username TEXT, alias TEXT, remark TEXT, "
                    "nick_name TEXT, delete_flag INTEGER)", [(FRIEND, None, "张三", None, 0)]),
        "chat_room": ("CREATE TABLE chat_room (id INTEGER PRIMARY KEY, username TEXT, "
                      "owner TEXT, ext_buffer BLOB)", [(1, GROUP, None, None)]),
    })

    with pytest.raises(sqlite3.OperationalError):
        reader.MessageReader(ACCT).roster()           # 探测放行、SQL 崩——网不能在这儿

    res = usecases.search_history("开会", contact=GROUP, account=ACCT)
    assert res["hits"], f"名册刷新撞上裸 sqlite 错误不该打挂索引检索：{res}"


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

def test_db_matches_wildcard_needs_a_real_path_segment():
    """`message/*.db` 只该匹配 `message/` 下的库。切片写成 `[:-5]` 留下的是 `"message"`
    （没有斜杠），于是 `message_fts/message_fts.db` 也会命中——今天所有 `rel` 都是
    `message/<shard>` 或精确路径所以无害，但注释里的算术与代码对不上，早晚要坑人。"""
    assert reader._db_matches("message/*.db", "message/message_0.db")
    assert not reader._db_matches("message/*.db", "message_fts/message_fts.db")
    assert not reader._db_matches("message/*.db", "message")
    assert reader._db_matches("contact/contact.db", "contact/contact.db")
    assert not reader._db_matches("contact/contact.db", "session/session.db")


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
            "real_sender_id INTEGER, create_time INTEGER, message_content BLOB, "
            "server_id INTEGER)",
            [(1, 1, 1, T0, "hi", 9001)]),
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
            "real_sender_id INTEGER, create_time INTEGER, message_content BLOB, "
            "server_id INTEGER)",
            [(1, 1, 1, T0, "hi", 9001)]),
    })
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    with pytest.raises(errors.SchemaDrift) as ei:
        list(reader.MessageReader(ACCT).iter_all())     # 生成器：必须消费才触发
    assert ei.value.capability == "messages"
    assert "WCDB_CT_message_content" in ei.value.missing_columns


def test_iter_all_raises_schema_drift_when_packed_info_data_missing(env, tmp_path, monkeypatch):
    """Ruling 24：`iter_all` 的 SELECT 增了 `packed_info_data`，声明必须同步增——
    否则缺这一列的微信版本会在探测放行后撞上裸 `OperationalError`，而不是
    `SchemaDrift`，声明式探测就白做了（schema.py 的模块注释）。"""
    root = tmp_path / "wx"
    enc_db(root / ACCT / "db_storage" / "message" / "message_0.db", {
        "Name2Id": ("CREATE TABLE Name2Id (user_name TEXT)", [(FRIEND,)]),
        f"Msg_{md5(FRIEND)}": (
            f"CREATE TABLE Msg_{md5(FRIEND)} (local_id INTEGER, local_type INTEGER, "
            "real_sender_id INTEGER, create_time INTEGER, message_content BLOB, "
            "WCDB_CT_message_content INTEGER, server_id INTEGER)",  # 其余声明列齐全，唯独缺 packed_info_data
            [(1, 1, 1, T0, "hi", 0, 9001)]),
    })
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    with pytest.raises(errors.SchemaDrift) as ei:
        list(reader.MessageReader(ACCT).iter_all())
    assert ei.value.capability == "messages"
    assert ei.value.missing_columns == ("packed_info_data",)


def test_username_pool_degrades_when_one_source_drifts(env, tmp_path, monkeypatch):
    """四个源汇集，少一个仍有三个（spec §6）——**不抛**，跳过它继续。

    断言必须钉的是「其余源**照常贡献**」而不只是「不抛」：把 pool 换成
    `{self_wxid, "filehelper"}` 的空转实现也该红。所以这里让 session 源是**活的**，
    只把 name2id 拿掉——它是唯一能证明「跳过坏的、留下好的」的构造。"""
    root = tmp_path / "wx"
    drift_tree(root, ACCT, drop_table="name2id")        # contact 源（name2id）没了
    enc_db(root / ACCT / "db_storage" / "session" / "session.db", {
        "SessionTable": ("CREATE TABLE SessionTable (username TEXT)", [(FRIEND,)]),
    })                                                 # session 源是好的
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    r = reader.MessageReader(ACCT)
    pool = r.username_pool()                            # 不该抛
    assert FRIEND in pool                               # 漂移的源被跳过，其余照常贡献
    assert any(d.capability == "username_pool" for d in r._drift)


def test_drift_names_the_table_that_actually_drifted(env, tmp_path, monkeypatch):
    """漂移点名的必须是**真的缺了的那张表**：`messages` 能力下 `Name2Id` 缺了，若报成
    `Msg_<md5>`（一张存在的表），用户会去查错方向，T5 按 `(capability, table)` 去重也会错位。"""
    root = tmp_path / "wx"
    enc_db(root / ACCT / "db_storage" / "message" / "message_0.db", {
        # **唯独没有 Name2Id 表**
        f"Msg_{md5(FRIEND)}": (
            f"CREATE TABLE Msg_{md5(FRIEND)} (local_id INTEGER, local_type INTEGER, "
            "real_sender_id INTEGER, create_time INTEGER, message_content BLOB, "
            "WCDB_CT_message_content INTEGER, server_id INTEGER)", [(1, 1, 1, T0, "hi", 0, 9001)]),
    })
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    with pytest.raises(errors.SchemaDrift) as ei:
        reader.MessageReader(ACCT).recent(FRIEND, limit=5)
    assert ei.value.table == "Name2Id", "该点名缺了的 Name2Id，而不是那张存在的 Msg_ 表"
    assert ei.value.missing_columns == ()               # 空 = 表不存在


def test_check_only_validates_the_named_table(env, tmp_path, monkeypatch):
    """`only=` 只校验这一张表的声明（T4 的逐源降级靠它）：没有它，一次 `_check` 会走完
    `roster` 的全部三条声明，第一条漂移就抛——后面的源永远记不上 `_drift`。"""
    root = tmp_path / "wx"
    drift_tree(root, ACCT, drop_table="chat_room")       # 群昵称源没了，contact 还在
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    r = reader.MessageReader(ACCT)
    with r._open("contact/contact.db") as con:
        r._check("roster", "contact/contact.db", con, only="contact")   # 不该抛
    assert r._drift == []
    with r._open("contact/contact.db") as con:
        with pytest.raises(errors.SchemaDrift) as ei:
            r._check("roster", "contact/contact.db", con)      # 不带 only：chat_room 缺
    assert ei.value.table == "chat_room"


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


def test_roster_survives_missing_chat_room(env, tmp_path, monkeypatch):
    """`chat_room` 没了 → 没有群昵称，但私聊行（备注/昵称）照常产出。"""
    root = tmp_path / "wx"
    drift_tree(root, ACCT, drop_table="chat_room")
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    rows = reader.MessageReader(ACCT).roster()          # 不该抛
    private = [r for r in rows if r[0] == r[1]]
    assert private and private[0][2] == "张三"


def test_roster_survives_missing_contact(env, tmp_path, monkeypatch):
    """`contact` 没了 → 没有备注/昵称，但群成员行仍以群昵称（无群名片者回落 username）产出。

    **必须造一个真有成员的 `chat_room`**：`drift_tree` 的 chat_room 是空表，用它的话
    `contact` 一走名册就必然是空的——私聊行**只**来自 `contact` 表（`build_rows` 的第二个
    循环），contact 没了就没有「谁是我的私聊」这份名单，那一条断言只能是空转。
    """
    root = tmp_path / "wx"
    enc_db(root / ACCT / "db_storage" / "contact" / "contact.db", {
        "name2id": ("CREATE TABLE name2id (username TEXT)", [(GROUP,)]),
        "chat_room": ("CREATE TABLE chat_room (id INTEGER PRIMARY KEY, username TEXT, "
                      "owner TEXT, ext_buffer BLOB)",
                      [(1, GROUP, None, ext_buffer(member(FRIEND, "群里的张三"),
                                                   member("wxid_friend02")))]),
    })                                          # **唯独没有 contact 表**
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    rows = reader.MessageReader(ACCT).roster()          # 不该抛
    got = {(c, k): d for c, k, d in rows}
    assert got[(GROUP, FRIEND)] == "群里的张三", "群昵称不依赖 contact"
    assert got[(GROUP, "wxid_friend02")] == "wxid_friend02", "无群名片回落 username"


def test_roster_records_both_drifts_when_both_sources_are_gone(env, tmp_path, monkeypatch):
    """两个源都没了 → 名册为空（T5 的空名册守卫会让它不写库），且两条漂移都被记下。"""
    root = tmp_path / "wx"
    enc_db(root / ACCT / "db_storage" / "contact" / "contact.db", {
        "name2id": ("CREATE TABLE name2id (username TEXT)", []),
    })
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    r = reader.MessageReader(ACCT)
    assert r.roster() == []
    assert {d.table for d in r._drift if d.capability == "roster"} == {"chat_room", "contact"}


def test_second_roster_call_does_not_bypass_the_drift_guard(env, tmp_path, monkeypatch):
    """同一个 reader 上连续两次 `roster()`：**第二次也得走降级**。

    `_check` 的结论是实例级缓存的（T3）。若「探过」与「探过且漂了」共用一份状态，第二次
    调用会在缓存命中处早退——既不重抛漂移、也不拦下面那条 SQL，`chat_room` 那段的裸
    `sqlite3.OperationalError` 就从 `roster()` 里逃出去了（生产路径今天不可达：每个
    usecase 入口都新建 reader；但 T5 的 `collect_degradations` 测试正是
    `r.roster(); r.roster()` 这个形状）。**漂移也要按类型重抛**，让调用方看见 `SchemaDrift`
    而不是一个裸 sqlite 错误。
    """
    from agent_wechat import schema                     # 函数内 import，不动文件头
    root = tmp_path / "wx"
    drift_tree(root, ACCT, drop_table="chat_room")
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    r = reader.MessageReader(ACCT)

    first = r.roster()
    assert first, "第一次就该降级产出（contact 源是好的）"
    second = r.roster()                                 # 缓存命中：不该抛裸 sqlite 错误
    assert second == first, "第二次该产出同样的降级结果"

    # 命中时重抛的是**记下来的**结论，不重新探（`_drifted`）：探测虽然便宜，但「探过」
    # 就该是探过。探针被判失败，说明这条分支走的不是缓存。
    monkeypatch.setattr(schema, "probe", lambda *a, **k: pytest.fail("缓存命中不该重探"))
    with r._open("contact/contact.db") as con:          # guard 本身：命中时重抛，不静默放行
        with pytest.raises(errors.SchemaDrift) as ei:
            r._check("roster", "contact/contact.db", con, only="chat_room")
    assert ei.value.table == "chat_room"

    assert [d.table for d in r._drift] == ["chat_room"], "重抛不该让 _drift 长出重复条目"


def test_drift_recorded_once_per_capability_and_table(env, tmp_path, monkeypatch):
    """同一次调用里同一个 `(capability, table)` 只记一条：`username_pool` 逐分片探
    （每片一条 `message/*.db` 声明），两片都缺 `Name2Id` 说的是同一件事——记两条，
    用户就会看到两行一模一样的降级说明（spec §7 的去重，T5 只是兜底）。

    去重**不是**为第二次 `roster()` 准备的：那条重抛路径不会再记一次（钉在
    `test_second_roster_call_does_not_bypass_the_drift_guard`）。真正的重复来自
    `rel` 不同、`(capability, table)` 相同的多分片场景。
    """
    root = tmp_path / "wx"
    for shard in ("message_0.db", "message_1.db"):
        enc_db(root / ACCT / "db_storage" / "message" / shard, {})   # 两片都没有 Name2Id
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    r = reader.MessageReader(ACCT)
    r.username_pool()                                   # 降级：跳过读不到的源
    assert [(d.capability, d.table) for d in r._drift] == [("username_pool", "Name2Id")]


# ---------- usecases：降级编排 ----------

def test_degradations_are_collected_and_deduped(env, tmp_path, monkeypatch):
    """同一 `(capability, table)` 在一次调用里出现多次（iter_all 每个分片探一次），
    去重后只挂一条——否则一次导出会给用户几十条重复提示（spec §7）。"""
    root = tmp_path / "wx"
    drift_tree(root, ACCT, drop_table="chat_room")
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    r = reader.MessageReader(ACCT)
    r.roster()
    r.roster()                                   # 第二次：缓存命中，但 _drift 不再追加
    ds = usecases.collect_degradations(r)
    assert len(ds) == 1 and ds[0].capability == "roster"
    assert "显示名" in ds[0].effect


def test_collect_degradations_dedupes_on_its_own(env):
    """`collect_degradations` **自己**那层去重的钉子。上面那条考不到它：
    `_record_drift`（T4）已经按同一把键去过重，`roster(); roster()` 根本不会产出重复，
    把这里的 `seen` 换成裸 append 也不会有测试红。`_drift` 的类型是 list，去重不是它的
    保证——直接喂一个带重复的 `_drift`，把契约钉在函数上。
    """
    r = reader.MessageReader(ACCT)
    dup = errors.SchemaDrift("roster", "contact/contact.db", "chat_room")
    r._drift.extend([dup, dup,
                     errors.SchemaDrift("username_pool", "message/message_0.db", "Name2Id")])
    assert [(d.capability, d.table) for d in usecases.collect_degradations(r)] == [
        ("roster", "chat_room"), ("username_pool", "Name2Id")]


def test_degradation_effect_is_human_readable_not_the_table_name():
    """用户不需要知道 chat_room 表没了——他需要知道「显示名暂不可用」（spec §7）。"""
    assert "chat_room" not in usecases.DEGRADE_EFFECT["roster"]
    assert "显示名" in usecases.DEGRADE_EFFECT["roster"]


def test_degrade_effect_covers_every_capability():
    """策略表的完整性钉子：`collect_degradations` 走的是
    `DEGRADE_EFFECT.get(capability, "部分功能降级")`——一个键拼错**不会红任何东西**，
    只会让用户把一句没有信息量的话当成全部真相。能力清单的唯一来源是
    `schema.CAPABILITIES` 的键，两边必须一一对上。"""
    from agent_wechat import schema                     # 函数内 import，不动文件头
    assert set(schema.CAPABILITIES) <= set(usecases.DEGRADE_EFFECT)


def test_recent_messages_carries_degradations(env, tmp_path, monkeypatch):
    """降级说明要挂进**返回结构**，而不是靠全局状态。"""
    root = tmp_path / "wx"
    drift_tree(root, ACCT, drop_table="chat_room")     # 显示名降级，但消息可读
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    monkeypatch.setattr(keys, "CACHE_FILE", root / "passphrase.json")
    keys.save_cached_hex(SELF, PW.encode().hex())
    res = usecases.recent_messages(FRIEND, limit=5, account=ACCT)
    assert isinstance(res.degraded, tuple)
    assert [d.table for d in res.degraded] == ["chat_room"], \
        "降级必须真的挂上去：默认 `()` 也是 tuple，光 isinstance 是空转"


def test_recent_survives_contact_table_drift(env, tmp_path, monkeypatch):
    """`contact` 表漂移时 `recent` 仍要返回消息：显示名是**富化**而非唯一来源（spec §6
    的判据）——消息在 message 分片里，与 `contact` 无关；而 `_apply_roster` 已经为**同一个
    漂移**把发送者降级成 wxid 了。富化这里跟着抛，等于同一件事在一处降级、在另一处把整个
    命令杀掉。

    注意 contact 必须是 **chat_id**：按名字解析本来就该抛（spec §6：没有 chat_id 就没有
    会话可查），那不是这条要钉的东西。
    """
    root = tmp_path / "wx"
    drift_tree(root, ACCT, drop_table="contact")
    build_shard(root, ACCT, "message_0.db", {FRIEND: [(1, 1, 1, T0, "你好", 0)]}, [FRIEND])
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)

    res = usecases.recent_messages(FRIEND, limit=5, account=ACCT)
    assert [m.content for m in res.messages] == ["你好"]        # 消息照常可读
    assert res.display == FRIEND                                # 富化回落 chat_id
    assert "contacts" in [d.capability for d in res.degraded]   # 漂移照常报出去


def test_semantic_search_survives_contact_table_drift(env, tmp_path, monkeypatch):
    """同上，semantic 一侧：hits 的 `display` 只是富化——索引库完好、命中照常返回。
    富化跟着 `contact` 漂移一起抛，等于把「显示名暂时拿不到」升级成「什么都搜不到」。"""
    root = tmp_path / "wx"
    drift_tree(root, ACCT, drop_table="contact")
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    # 不打嵌入（要模型）：这条钉的是富化与降级，不是检索本身
    monkeypatch.setattr(vector, "semantic_search", lambda *a, **k: {
        "mode": "semantic", "total": 1,
        "hits": [{"chat_id": FRIEND, "ts": T0, "sender": "wxid_a", "is_self": 0,
                  "content": "你好", "distance": 0.1}]})

    res = usecases.semantic_search("你好", account=ACCT)
    assert res["total"] == 1 and res["hits"][0]["display"] == FRIEND
    assert "contacts" in [d["capability"] for d in res["degraded"]]


# ---------- media_refs 管道（T5） ----------
# 三条共用同一个树：合成目录的副本里，分片 0 换成一条图片消息（local_type=3、
# zstd 压缩的 XML、packed_info_data 带 32-hex 文件名哈希）。分片 1 保留夹具原有的
# 2 条旧文本——refs 与行数两边都有参照。

IMG_MD5 = "0123456789abcdef0123456789abcdef"
IMG_XML = (f'<msg><img aeskey="aes_key_abc" encryver="1" cdnthumburl="cdn" '
           f'md5="{IMG_MD5}"/></msg>')


def _tree_with_image_message(env, tmp_path):
    root = tmp_path / "imgwx"
    shutil.copytree(env.wx, root)
    build_shard(root, ACCT, "message_0.db",
                {FRIEND: [(1, 3, 1, T0, zstandard.compress(IMG_XML.encode()), 4, b"E" * 32)]},
                [FRIEND, SELF])
    return root


def test_iter_all_yields_refs_for_image_message(env, tmp_path, monkeypatch):
    """iter_all 的 yield 变为 (rows, refs)：图片消息的 packed 32-hex 进 refs（文件名权威），
    XML md5 随行佐证。XML 只存在于**原始** content（_decode_row 产出的是 "[image]" 占位），
    且这里是 zstd 压缩的——实现若把占位符或未解压字节当 content 传下去，这条必红。"""
    monkeypatch.setattr(paths, "XWECHAT_FILES", _tree_with_image_message(env, tmp_path))

    batches = list(reader.MessageReader(ACCT).iter_all())
    rows = [r for batch, _ in batches for r in batch]
    refs = [ref for _, rf in batches for ref in rf]
    key = messages.row_key(ACCT, md5(FRIEND), "message_0.db", 1)
    assert refs == [(key, "image", "e" * 32, IMG_MD5)]   # packed 大写 hex 被归一小写
    assert len(rows) == 3                                 # 图片 1 + 分片 1 的旧 2：yield 改形不丢行
    img = next(r for r in rows if r["msg_type"] == 3)
    assert img["content"] == "[image]"                    # 行内容照旧是占位符


def test_run_export_writes_media_refs_same_transaction(env, tmp_path, monkeypatch):
    """run_export 把 refs 与 messages 写进**同一个事务**（spec §4.3）。

    原子性构造：在 upsert_batch 处炸掉——此刻 refs 已先行写入（先写 refs 再 upsert_batch），
    若 upsert_media_refs 自带 commit，refs 会在回滚后幸存。恢复后跑成功路径：refs 落地，
    且 messages 不因 refs 多出行。
    """
    monkeypatch.setattr(paths, "XWECHAT_FILES", _tree_with_image_message(env, tmp_path))

    real_batch = store.upsert_batch

    def boom(con, rows):
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "upsert_batch", boom)
    with pytest.raises(RuntimeError, match="boom"):
        export.run_export(ACCT, full=True)
    with store.open() as con:
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM media_refs").fetchone()[0] == 0

    monkeypatch.setattr(store, "upsert_batch", real_batch)
    assert export.run_export(ACCT, full=True) == 3
    with store.open() as con:
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 3
        assert con.execute("SELECT count(*) FROM media_refs").fetchone()[0] == 1
        row = con.execute("SELECT media_kind, file_key, xml_md5 FROM media_refs").fetchone()
    assert tuple(row) == ("image", "e" * 32, IMG_MD5)


def test_media_rebuild_refs_idempotent_and_does_not_touch_messages(env, tmp_path, monkeypatch):
    """media_rebuild_refs 只回填 media_refs，**不碰 messages**；幂等，且能把既有但
    不一致的 refs 纠正过来。

    「不碰 messages」断言在**空索引**上做：若实现顺手走了 upsert_batch，这里会凭空长出
    3 行消息——先跑过导出的话，幂等 upsert 会让这一违规不可见。"""
    monkeypatch.setattr(paths, "XWECHAT_FILES", _tree_with_image_message(env, tmp_path))
    key = messages.row_key(ACCT, md5(FRIEND), "message_0.db", 1)

    assert usecases.media_rebuild_refs(account=ACCT) == 1
    with store.open() as con:
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        row = con.execute("SELECT media_kind, file_key, xml_md5 FROM media_refs").fetchone()
    assert tuple(row) == ("image", "e" * 32, IMG_MD5)

    # 存量 refs 是别的样子（kind/file_key 全错）→ rebuild 覆盖纠正，不产生第二行
    with store.open() as con:
        store.upsert_media_refs(con, [(key, "video", "0" * 32, None)])
    assert usecases.media_rebuild_refs(account=ACCT) == 1
    with store.open() as con:
        assert con.execute("SELECT count(*) FROM media_refs").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        row = con.execute("SELECT media_kind, file_key, xml_md5 FROM media_refs").fetchone()
    assert tuple(row) == ("image", "e" * 32, IMG_MD5)

    # 再跑一遍：幂等（refs 不增长、messages 仍为 0）
    assert usecases.media_rebuild_refs(account=ACCT) == 1
    with store.open() as con:
        assert con.execute("SELECT count(*) FROM media_refs").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_iter_parts_input_keys_are_the_same_row_keys_messages_uses(env):
    """`reader.iter_parts_input` 产出的 `msg_key` 必须与 `messages.row_key` **逐字符同形**。

    **为什么这条不能省**：它是回填那条链路上**唯一真跑生产扫描器**的地方。两个新测试文件
    都用桩把 `iter_parts_input` 替掉了（`test_chat_export.py` 的 `_FakeReader`、
    `test_mcp_server.py` 的 `_StubCtx`），而唯一真跑它的 `test_appmsg_real.py` 被
    `addopts = -m 'not slow'` **默认 deselect**，且忽略返回的 `_key`（只算比率）。
    于是**失败是静默的**：`row_key` 的 shard / md5 / 账号前缀拼错时，回填会往 `msg_parts`
    写一大堆**没有任何消息对得上**的行，而 `count_msg_parts` 是**账号级**的 →
    `chat export` 不报任何 `degraded`，用户只看到 appmsg 全变 `[appmsg: 标题]`。

    判据是**两条生产路径的集合相等**（形状照 `test_recent_messages_msg_key_populated_by_read_rows`）：
    `messages` 的 `msg_key` 由 `reader._decode_row` 算，回填的由 `iter_parts_input` 算，
    两者必须**逐字符落在同一批字符串上**——**这里比的是两个独立实现，不是实现与自己**。

    **为什么是集合相等，而不是「appmsg 那几条都在」**：夹具里的 appmsg 只有 `message_0.db`
    那一行，于是「分片名写死成 `message_0.db`」这个变异**对它是等价的**（实测：只挑 appmsg
    来比时，那条变异全绿——一条真护栏就这样退化成恒真）。`message_1.db` 的普通文本消息
    不受 msg_type 过滤的影响，才照得出这个变异——**回填扫的是全部分片，一个都不该漏**。

    再钉一条**字面量**（不调 `messages.row_key` 现算）：集合法对「两边一起漂移」是盲的
    ——两边都调同一个函数时，改坏它两边一起变、集合照样相等。
    """
    assert export.run_export(ACCT, full=True) > 0
    with store.open() as con:
        in_index = {r[0] for r in con.execute(
            "SELECT msg_key FROM messages WHERE account = ?", (ACCT,))}
        n_appmsg = con.execute(
            "SELECT count(*) FROM messages WHERE account = ? AND msg_type = ?",
            (ACCT, messages.TYPE_APPMSG)).fetchone()[0]
    assert n_appmsg == 1, "夹具里没有 appmsg 行，下面的载荷断言将恒真"
    assert any("|message_1.db|" in k for k in in_index), "夹具没跨分片，分片那条会退化成恒真"

    scanned: dict[str, tuple] = {key: (sid, raw)
                                 for sid, key, raw in reader.MessageReader(ACCT).iter_parts_input()}
    assert set(scanned) == in_index, (
        "两条路径算出的 msg_key 不同形 —— 回填会写出对不上的行，且不报警")
    sid, raw = scanned[f"{ACCT}|{md5(FRIEND)}|message_0.db|5"]
    assert raw is not None and raw.startswith("<msg>"), "appmsg 载荷没被解出来"
    assert sid == 9005, "server_id 没从微信库带出来（引用回指整条断掉）"
    # 契约写死成字面量：账号 | chat_md5 | 分片名 | local_id，四段缺一不可。
    assert f"{ACCT}|{md5(FRIEND)}|message_0.db|5" in scanned


# ---------- T6：msg_key 在 _read_rows 里真实赋值（不能靠字段默认值空转） ----------

def test_recent_messages_msg_key_populated_by_read_rows(env):
    """`reader.py:362` 的 `m.msg_key = messages.row_key(...)` 是 get_recent_messages
    路径上 msg_key 唯一的真实来源。走 `wechat` 夹具的完整库（真 roster + 真 _read_rows），
    删掉那一行字段仍在（默认空串）但毫无用处——这条断言会红：先验非空，再验等于
    四字段重算值（钉住顺序与组成）。"""
    res = usecases.recent_messages(FRIEND, limit=5, account=ACCT)
    assert res.messages, "夹具没建出消息，下面的断言将恒真"
    assert all(m.msg_key for m in res.messages), "msg_key 不能是空串：字段在但没用"
    for m in res.messages:
        assert m.msg_key == messages.row_key(ACCT, messages.md5_of(m.chat_id),
                                             m.shard, m.local_id)


# ---------- T5：emoticon.db 的名字来源 ----------

def test_sticker_captions_returns_empty_when_db_unreadable(env, monkeypatch):
    """**降级的能力**：读不到就返回空表，绝不抛——贴纸没有名字仍要能导出。

    与 `roster()` 的逐源降级同一条理由：少一个源不该让整个导出失败。
    """
    r = reader.MessageReader(ACCT)
    monkeypatch.setattr(reader.MessageReader, "_open",
                        lambda self, rel: (_ for _ in ()).throw(errors.DataNotFound("x")))
    assert r.sticker_captions() == {}


def test_sticker_captions_maps_md5_to_caption(env, tmp_path, monkeypatch):
    """**正向路径**，也是列名那条静默失败的唯一单测防线。

    夹具的 DDL 用**真机列名**（`md5_` / `language_` / `caption_`，带尾下划线）：
    声明或 SQL 里写掉一个 `_`，`_check` 会判漂移、`sticker_captions()` 吞掉异常返回
    空表——那时下面的断言会红。没有这条，错列名在单测里**完全不可见**（只靠真机冒烟）。

    三条边界同时钉住：非 `'default'` 语言的行**不入表**（不筛会把同一 md5 重复计入）、
    空 caption 跳过、md5 归一化成小写（XML 那边的 md5 大小写不定，合并时按小写对齐）。
    """
    root = tmp_path / "wx"
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    enc_db(root / ACCT / "db_storage" / "emoticon" / "emoticon.db", {
        "kStoreEmoticonCaptionsTable": (
            "CREATE TABLE kStoreEmoticonCaptionsTable "
            "(package_id_ TEXT, md5_ TEXT, language_ TEXT, caption_ TEXT)",
            [("pkg_a", "a" * 32, "default", "抱抱"),
             ("pkg_a", "B" * 32, "default", "大哭"),
             ("pkg_a", "c" * 32, "en_us", "hug"),        # 别的语言：不入表
             ("pkg_a", "d" * 32, "default", ""),         # 空 caption：不入表
             ("pkg_a", "e" * 32, "default", None)]),
    })
    caps = reader.MessageReader(ACCT).sticker_captions()
    assert caps == {"a" * 32: "抱抱", "b" * 32: "大哭"}


# ---------- T6：贴纸显示串接入 ----------

def test_decode_row_renders_sticker_display(env):
    """`content` 变成可读串，`media_hint` **保持 `[sticker]` 不变**（spec D5）。

    两列语义不同：`media_hint` 是给程序判类型的（`vector.py` 据它决定嵌入），
    `content` 是给人/LLM 读的。名字不该污染类型标记——贴纸因此成为三类媒体里
    唯一 content != media_hint 的成员，这是**刻意的**。
    """
    r = reader.MessageReader(ACCT)
    xml = ('<msg><emoji md5="d0f46ed3ce8bac33df5f7055656d8bd9" '
           'desc="Cg8KBXpoX2NuEgbosKLosKIKCQoFemhfdHcSAAoLCgdkZWZhdWx0EgA="></emoji></msg>')
    m = r._decode_row(1, messages.TYPE_STICKER, 1, 1730000000, xml.encode(), 0,
                      "0" * 32, {}, {})
    assert m.content == "[贴纸:谢谢]"
    assert m.media_hint == "[sticker]"
    assert m.sticker_md5 == "d0f46ed3ce8bac33df5f7055656d8bd9"


def test_decode_row_uses_db_caption_when_xml_has_no_name(env):
    """优先级：XML 内嵌名 > emoticon.db caption > 短 ID。**反转优先级必红。**"""
    r = reader.MessageReader(ACCT)
    xml = '<msg><emoji md5="d0f46ed3ce8bac33df5f7055656d8bd9"></emoji></msg>'
    m = r._decode_row(1, messages.TYPE_STICKER, 1, 1730000000, xml.encode(), 0,
                      "0" * 32, {}, {}, sticker_names={"d0f46ed3ce8bac33df5f7055656d8bd9": "滑稽"})
    assert m.content == "[贴纸:滑稽]"
    m2 = r._decode_row(1, messages.TYPE_STICKER, 1, 1730000000, xml.encode(), 0,
                       "0" * 32, {}, {}, sticker_names={})
    assert m2.content == "[贴纸#d0f46ed3]"
    # 两个源**都有名字且不同**时 XML 内嵌名胜出——没有这一条，上面两句对反转优先级是盲的
    # （XML 无名时两个源不冲突，反着写也得到同一个串），docstring 那句「反转必红」就是空话。
    named = ('<msg><emoji md5="d0f46ed3ce8bac33df5f7055656d8bd9" '
             'desc="Cg8KBXpoX2NuEgbosKLosKIKCQoFemhfdHcSAAoLCgdkZWZhdWx0EgA="></emoji></msg>')
    m3 = r._decode_row(1, messages.TYPE_STICKER, 1, 1730000000, named.encode(), 0,
                       "0" * 32, {}, {},
                       sticker_names={"d0f46ed3ce8bac33df5f7055656d8bd9": "库里的名字"})
    assert m3.content == "[贴纸:谢谢]", "冲突时 XML 内嵌名优先于 emoticon.db caption"
    assert m3.media_hint == "[sticker]"


def test_decode_row_sticker_without_md5_falls_back(env):
    """XML 没有 md5 → 回落 `[sticker]`，**不崩也不产出半个标识**。"""
    r = reader.MessageReader(ACCT)
    m = r._decode_row(1, messages.TYPE_STICKER, 1, 1730000000, b"<msg><emoji></emoji></msg>", 0,
                      "0" * 32, {}, {})
    assert m.content == "[sticker]"
    assert m.sticker_md5 is None


def test_iter_all_yields_a_sticker_ref_carrying_the_xml_md5(env, tmp_path, monkeypatch):
    """贴纸也进 `media_refs`：`file_key` 位放 emoji XML 的 md5，`xml_md5` 位留 None。

    **今天没有消费者，不等于可以不写**：这张表是「消息 → 媒体身份」的唯一索引，
    而 md5 在走得到这条分支时是 100% 拿得到的——漏写意味着日后要再全量重导一遍
    （67k 条消息）才补得回来。`media_kind='sticker'` 不需要 DDL 改动（没有 CHECK 约束），
    只有 store 的列注释跟着改。

    `sticker_md5` **必须来自 `_decode_row` 那一趟**：这条构造里 `packed_info_data` 是
    NULL（真机贴纸就是没有 packed），所以从 packed 里找 md5 的写法拿不到任何东西。
    行内容与 ref 是同一次解码的两半——索引里的 content 与实时 `recent` 必须是同一个串。
    """
    sticker_md5 = "d0f46ed3ce8bac33df5f7055656d8bd9"
    root = tmp_path / "stickerwx"
    shutil.copytree(env.wx, root)
    build_shard(root, ACCT, "message_0.db",
                {FRIEND: [(1, 47, 1, T0, f'<msg><emoji md5="{sticker_md5}"></emoji></msg>', 0)]},
                [FRIEND, SELF])
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)

    batches = list(reader.MessageReader(ACCT).iter_all())
    rows = [r for batch, _ in batches for r in batch]
    refs = [ref for _, rf in batches for ref in rf]
    key = messages.row_key(ACCT, md5(FRIEND), "message_0.db", 1)
    assert refs == [(key, "sticker", sticker_md5, None)]
    sticker = next(r for r in rows if r["msg_type"] == 47)
    assert sticker["content"] == "[贴纸#d0f46ed3]"     # 无名 → 短 ID，索引里照写
    assert sticker["media_hint"] == "[sticker]"


def test_recent_and_iter_all_pass_emoticon_captions_down(env, tmp_path, monkeypatch):
    """`emoticon.db` 的名字必须真的走到 `content` 上，且**每次调用只解一次库**。

    两个钉子缺一不可。上面三条测试都直接调 `_decode_row` 并手喂 `sticker_names`，
    所以「`recent` / `iter_all` 把名字传下去」这**两行接线**今天无人守护——删掉它，
    真机上贴纸会静默地全部回落短 ID（名字覆盖率那 9% 一分不剩），而单测全绿。

    调用次数那一条**必须用多于一行**的夹具：只有一行时「循环外取一次」与「每行取一次」
    都是 1，断言会退化成恒真。真机 67k 条消息，写成每行一次就是几万次白白解密。
    """
    sticker_md5 = "d0f46ed3ce8bac33df5f7055656d8bd9"
    root = tmp_path / "captionwx"
    shutil.copytree(env.wx, root)
    build_shard(root, ACCT, "message_0.db",
                {FRIEND: [(i, 47, 1, T0 + i, f'<msg><emoji md5="{sticker_md5}"/></msg>', 0)
                          for i in (1, 2, 3)]},
                [FRIEND, SELF])
    enc_db(root / ACCT / "db_storage" / "emoticon" / "emoticon.db", {
        "kStoreEmoticonCaptionsTable": (
            "CREATE TABLE kStoreEmoticonCaptionsTable "
            "(package_id_ TEXT, md5_ TEXT, language_ TEXT, caption_ TEXT)",
            [("pkg_a", sticker_md5, "default", "滑稽")]),
    })
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)

    calls: list[int] = []
    real = reader.MessageReader.sticker_captions
    monkeypatch.setattr(reader.MessageReader, "sticker_captions",
                        lambda self: (calls.append(1), real(self))[1])
    r = reader.MessageReader(ACCT)

    stickers = [m for m in r.recent(FRIEND, limit=10) if m.msg_type == 47]
    assert [m.content for m in stickers] == ["[贴纸:滑稽]"] * 3
    assert len(calls) == 1, f"recent 该在循环外取一次名字，实际取了 {len(calls)} 次"

    calls.clear()
    rows = [row for batch, _ in r.iter_all(FRIEND) for row in batch]
    assert [x["content"] for x in rows if x["msg_type"] == 47] == ["[贴纸:滑稽]"] * 3
    assert len(calls) == 1, f"iter_all 该在循环外取一次名字，实际取了 {len(calls)} 次"


# ---------- EA-042：增量不重扫「同一 local_id 内容被改写」的行 ----------

def test_rewrites_are_caught_by_the_audit_pass_but_not_by_forward_scan(tmp_path, monkeypatch):
    """Phase 02：**前向扫描看不见原地改写，回看能看见**——两半都钉。

    前半是 EA-042 的现状（水位线只按 `max(local_id)` 推进）：`audit_rows=0` 时同一
    `local_id` 的正文被改写不会被拾取，`--full` 才修正。这条**仍然是契约**——回看是有界
    的、不是实时的。后半是本阶段新增的契约：默认导出会回看每个会话的一小段，改写落库、
    FTS 跟随、`pending_reembed` 投递重嵌标记。

    **自建一棵树**，不用 module 级的 `wechat` 夹具：这条测试要**改写源库**，而那个夹具是
    模块级共享的——T12 记过这条（往共享夹具里加一个分片，同模块 4 条用例当场红）。
    """
    root = tmp_path / "wxenv"
    root.mkdir(parents=True)          # `home.mkdir` 不建父目录（夹具那里是 mktemp 先建的）
    wx = root / "xwechat_files"
    home = root / "home"
    home.mkdir(mode=0o700)
    ds = wx / ACCT / "db_storage"
    enc_db(ds / "session" / "session.db", {
        "SessionTable": ("CREATE TABLE SessionTable (username TEXT)", [(FRIEND,)])})
    enc_db(ds / "contact" / "contact.db", {
        "name2id": ("CREATE TABLE name2id (username TEXT)", [(FRIEND,)]),
        "contact": ("CREATE TABLE contact (username TEXT, alias TEXT, remark TEXT, "
                    "nick_name TEXT, delete_flag INTEGER)",
                    [(FRIEND, None, "张三", "zc", None)]),
    })
    rows_v1 = [(1, 1, 1, T0 + 10, "第一版正文", 0)]
    build_shard(wx, ACCT, "message_0.db", {FRIEND: rows_v1}, [FRIEND])
    monkeypatch.setattr(paths, "XWECHAT_FILES", wx)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")
    keys.save_cached_hex(SELF, PW.encode().hex())
    key = f"{ACCT}|{md5(FRIEND)}|message_0.db|1"

    assert export.run_export(ACCT) == 1
    with store.open() as con:
        assert con.execute("SELECT content FROM messages WHERE msg_key=?",
                           (key,)).fetchone()[0] == "第一版正文"

    # 同一个 local_id、改了正文——**这就是撤回/编辑在库里的形态**
    build_shard(wx, ACCT, "message_0.db", {FRIEND: [(1, 1, 1, T0 + 10, "第二版正文", 0)]},
                [FRIEND])

    # ① 关掉回看 = 旧行为：前向扫描确实看不见已扫过的区间（EA-042 的现状）
    assert export.run_export(ACCT, audit_rows=0) == 0
    with store.open() as con:
        assert con.execute("SELECT content FROM messages WHERE msg_key=?",
                           (key,)).fetchone()[0] == "第一版正文", \
            "前向扫描只读 local_id > 水位线，原地改写必须看不见"
        assert con.execute("SELECT count(*) FROM pending_reembed").fetchone()[0] == 0

    # ② 默认回看：下一轮把改写拾取回来，FTS 跟随，重嵌标记投递
    assert export.run_export(ACCT) == 0
    with store.open() as con:
        assert con.execute("SELECT content FROM messages WHERE msg_key=?",
                           (key,)).fetchone()[0] == "第二版正文"
        assert con.execute(
            "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH '\"第二版\"'"
        ).fetchone()[0] == 1
        assert con.execute(
            "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH '\"第一版\"'"
        ).fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM pending_reembed WHERE msg_key=?",
                           (key,)).fetchone()[0] == 1

    # ③ `--full` 仍是即时核对手段：前向全量重扫会修正新改写（这一轮不做回看）
    build_shard(wx, ACCT, "message_0.db",
                {FRIEND: [(1, 1, 1, T0 + 10, "第三版正文", 0)]}, [FRIEND])
    assert export.run_export(ACCT, full=True) == 1
    with store.open() as con:
        assert con.execute("SELECT content FROM messages WHERE msg_key=?",
                           (key,)).fetchone()[0] == "第三版正文"


def test_chat_export_renders_the_location_block_end_to_end(tmp_path, monkeypatch):
    """**真路径**：合成微信树 → `export` → `extras rebuild` → `usecases.chat_export` → 读产物文件。

    复审 T14 的 N1：本批此前的覆盖只有「绕过 `chat_export`、直调 `chatview.build(extras=…)`」
    的模板级用例——把 `chat_export` 里那行 `extras=extras` 删掉，或把 `iter_extras_input` 的
    `md5hex` 切错，**全仓 1255 条全绿而产物里一个结构化块都没有**（本仓记过档的「全绿而功能
    为零」）。这条用例把那条接线钉住，且**两份产物都读**（HTML 那条链路同样会静默空掉）。
    """
    from agent_wechat import usecases
    root = tmp_path / "wxenv"
    root.mkdir(parents=True)
    wx = root / "xwechat_files"
    home = root / "home"
    home.mkdir(mode=0o700)
    ds = wx / ACCT / "db_storage"
    enc_db(ds / "session" / "session.db", {
        "SessionTable": ("CREATE TABLE SessionTable (username TEXT)", [(FRIEND,)])})
    enc_db(ds / "contact" / "contact.db", {
        "name2id": ("CREATE TABLE name2id (username TEXT)", [(FRIEND,)]),
        "contact": ("CREATE TABLE contact (username TEXT, alias TEXT, remark TEXT, "
                    "nick_name TEXT, delete_flag INTEGER)",
                    [(FRIEND, None, "张三", "zc", None)]),
    })
    loc = ('<msg><location x="39.9" y="116.4" scale="1" label="某地" poiname="P"'
           ' poiCategoryTips="咖啡"/></msg>')
    build_shard(wx, ACCT, "message_0.db",
                {FRIEND: [(1, 48, 1, T0 + 10, loc, 0)]}, [FRIEND])
    monkeypatch.setattr(paths, "XWECHAT_FILES", wx)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")
    keys.save_cached_hex(SELF, PW.encode().hex())

    assert export.run_export(ACCT) == 1
    # **必须显式给 `--account`**：`resolve_account(None)` 走 `lsof` 判本机当前登录的微信账号，
    # 与 `paths.XWECHAT_FILES` 的 patch 无关——不给就解析到**真实的那个账号**，然后
    # `KeyNotFound`（实测踩过；`run_export` 那条路是显式传 account 的，所以没暴露）。
    # **对照组先跑**：没回填时产物里一行都不印（这是契约，不是缺陷）
    before = usecases.chat_export(FRIEND, account=ACCT)
    md_before = pathlib.Path(before.files[0]).read_text()
    assert "某地" not in md_before and "位置" not in md_before

    usecases.msgextra_rebuild(account=ACCT)
    res = usecases.chat_export(FRIEND, account=ACCT)
    html = pathlib.Path(next(f for f in res.files if f.endswith(".html"))).read_text()
    md = pathlib.Path(next(f for f in res.files if f.endswith(".md"))).read_text()
    for out, name in ((md, "md"), (html, "html")):
        assert "某地" in out and "poiCategoryTips" in out, (
            f"{name} 产物里没有位置块：{out[-200:]!r}")
    # 坐标的**形态两边不同**：MD 侧走 `_esc_md`（`.` 被行首那一档转义成 `39\.9`），
    # HTML 侧原样。断言写死一种形态会在另一半上假红（第一次就是这么红的）。
    assert "39\\.9" in md and "39.9" in html
    assert "\\[location\\]" not in md, "结构化块出现时占位行必须收掉"
