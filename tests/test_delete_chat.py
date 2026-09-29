"""delete_chat 的单元测试。红线：本文件每个测试都不得指向真实 ~/.agent-wechat。"""
import sqlite3

import pytest
import sqlite_vec

from agent_wechat import errors, exclusions, messages, paths, store, vector

# 会话 id 的两种形态都要过一遍（`messages.chat_md5` 就这两条分支）：
# - victim 用**退化形态** `md5:<hex>`：它是 `chat_md5` 与 `md5_of` 唯一分道扬镳的地方
#   （`md5_of` 会把 `md5:<hex>` 整个串再哈希一次、得到错的 md5）。宽谓词按
#   `account|chat_md5|` 前缀定位，md5 取错就是**静默地一条都不删**，而核查还报 ✅。
# - keeper 用普通会话名，把非退化那条分支也走一遍。
VICTIM = messages.degraded_chat_id("aaaa")          # "md5:aaaa"，id 里就藏着 md5
VICTIM_MD5 = "aaaa"                                 # 独立字面量，不从被测函数反推
KEEPER = "keeper"
KEEPER_MD5 = messages.md5_of(KEEPER)


@pytest.fixture(autouse=True)
def _temp_index(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path / "home")
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    con = store.connect(tmp_path / "index.db")
    _seed(con)
    yield con
    con.close()


def _seed(con):
    """两个账号各一个会话，让反向断言有东西可查。"""
    def row(mk, acct, cm, cid, lid, ts, content):
        return {"msg_key": mk, "account": acct, "chat_md5": cm, "chat_id": cid,
                "shard": "message_0.db", "local_id": lid, "server_id": lid,
                "ts": ts, "msg_type": 1, "sender_id": None, "sender_name": None,
                "is_self": 0, "content": content, "media_hint": None}
    # 顺序有含义：`id` 是裸 rowid、按插入顺序自增。keeper **必须先插**——test_rowid_reuse_regression
    # 要的是「删掉 victim 后新的 max(id) == 1」（id 1 归 keeper），而被删的 victim 持有最大的
    # 两个 id（2、3），正是那条测试 docstring 说的「删掉持有最大 id 的行、那个 id 会被复用」。
    # （计划的这一行原本把 victim 写在前面，于是 keeper 拿到 id 3、水位线只能降 3——
    # 与它自己断言的 1 自相矛盾。见 task-3 报告。）
    store.upsert_batch(con, [
        row(f"a|{KEEPER_MD5}|message_0.db|1", "a", KEEPER_MD5, KEEPER, 1, 300, "留下的"),
        row(f"a|{VICTIM_MD5}|message_0.db|1", "a", VICTIM_MD5, VICTIM, 1, 100, "要删的 1"),
        row(f"a|{VICTIM_MD5}|message_0.db|2", "a", VICTIM_MD5, VICTIM, 2, 200, "要删的 2"),
    ])
    store.upsert_msg_parts(con, [(f"a|{VICTIM_MD5}|message_0.db|1", 57, None, None, b"<x/>")])
    store.upsert_media_refs(con, [(f"a|{VICTIM_MD5}|message_0.db|1", "image", "f" * 32, None)])
    store.upsert_msg_parts(con, [(f"a|{KEEPER_MD5}|message_0.db|1", 57, None, None, b"<k/>")])
    store.upsert_media_refs(con, [(f"a|{KEEPER_MD5}|message_0.db|1", "video", "d" * 32, None)])
    # **孤儿行**：msg_key 属于 victim，但 messages 里没有对应的行。生产上真会出现——
    # `appmsg rebuild` / `media rebuild-refs` 只写 parts/refs、不写 messages。它带着完整的
    # appmsg 载荷，而「经 messages 子查询」那版**删除与计数都看不见它**（核查会对着残留正文报 ✅）。
    # 这两行是本文件最重要的回归：按 msg_key 前缀的宽谓词必须把它们扫掉。
    store.upsert_msg_parts(con, [(f"a|{VICTIM_MD5}|message_0.db|90", 57, None, None, b"<orphan/>")])
    store.upsert_media_refs(con, [(f"a|{VICTIM_MD5}|message_0.db|91", "image", "e" * 32, None)])
    con.execute("INSERT INTO display_names VALUES ('a',?,'u1','受害人')", (VICTIM,))
    con.execute("INSERT INTO display_names VALUES ('a',?,'u2','留下的人')", (KEEPER,))
    con.commit()


@pytest.fixture
def _vec_index_db(_temp_index, tmp_path):
    """在夹具那条库上真建向量表、真种一行，返回**库文件路径**。

    建表用的是夹具自己的 `con`（它加载了扩展）；而下面那两条测试要的恰恰是**另一条不加载
    扩展**的连接，所以这里交出路径、不交出连接（同一条连接上扩展的模块注册是不可逆的）。
    """
    con = _temp_index
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.executescript(store.VECTOR_SCHEMA)
    # vec0 的 metadata 列（account / ts）**不允许 NULL**：两者都必须给值，只给 account 会报
    # `Expected integer for INTEGER metadata column ts, received NULL`。
    con.execute(
        "INSERT INTO vec_messages(msg_rowid, embedding, acct_chat, account, ts, preview) "
        "VALUES (?,?,?,?,?,NULL)",
        (2, sqlite_vec.serialize_float32([0.1] * store.VECTOR_DIM),
         vector._acct_chat("a", VICTIM_MD5), "a", 100))
    con.commit()
    return tmp_path / "index.db"


def test_chat_row_counts_refuses_when_vec_extension_not_loaded(_vec_index_db):
    """**装了向量索引的机器上 purge 的第一句就会踩这条**：`store.connect()` 从不加载
    sqlite-vec（加载是 `vector.connect()` 的职责，见 `vector.py:31` 的 docstring），于是
    `SELECT … FROM vec_messages` 抛裸 `sqlite3.OperationalError: no such module: vec0`
    ——违反「core 层只抛 AgentWeChatError 子类」（`errors.py:1`）。

    `_has_table` 只证明「表在」，不证明「这条连接能用它」：守卫要保留（轻装安装是正常路径，
    见 test_delete_chat_without_any_vector_tables），这一格靠翻译补。
    """
    with store.open(_vec_index_db) as plain:        # 不加载扩展的连接——生产 purge 的形态
        with pytest.raises(errors.PurgeRefused) as ei:
            store.chat_row_counts(plain, "a", VICTIM)
    assert isinstance(ei.value.__cause__, sqlite3.OperationalError), \
        "必须 `from e` 保住病因，否则排查时看不到 no such module: vec0"
    assert ei.value.hint and "vector.connect()" in ei.value.hint, \
        "hint 必须给出可执行的修法：改用 vector.connect() 开连接"


def test_delete_chat_refuses_when_vec_extension_not_loaded(_vec_index_db):
    """同一条连接上 `DELETE FROM vec_messages` 也是**响亮失败**（实测 sqlite-vec 0.1.9：
    `execute()` 当场抛 `no such module: vec0`，不是静默空操作）——所以这里**不做** rowcount
    对账，它守卫的是一个不存在的危险。"""
    with store.open(_vec_index_db) as plain:
        with pytest.raises(errors.PurgeRefused) as ei:
            store.delete_chat(plain, account="a", chat_id=VICTIM)
    assert isinstance(ei.value.__cause__, sqlite3.OperationalError)


def test_vec_exec_passes_other_operational_errors_through(_vec_index_db, _temp_index):
    """翻译**只认** `no such module` 一种：其它 OperationalError 原样透出。

    这里要的是「没被翻译」这个否定断言——而它正好是个假护栏的高发区：断言类型本身就分得开
    两条路径（PurgeRefused 是 RuntimeError，不是 sqlite3.Error），所以翻译写宽了、把语法错
    也吞成「没加载扩展」时，这条会红。宽着翻译的代价是**误诊**：库损坏被说成扩展没加载，
    排查会往完全错的方向走。
    """
    with pytest.raises(sqlite3.OperationalError) as ei:
        store._vec_exec(_temp_index, "SELECT count(*) FROM vec_messages WHERE nope = 1")
    assert "no such module" not in str(ei.value)


def test_delete_chat_removes_all_five_places(_temp_index):
    con = _temp_index
    got = store.delete_chat(con, account="a", chat_id=VICTIM)
    # parts/refs 各 2：一条挂在存活消息上、一条是孤儿。**子查询版这里只会是 1**——孤儿看不见。
    assert got == {"messages": 2, "vectors": 0, "parts": 2, "refs": 2, "display_names": 1}
    assert store.chat_row_counts(con, "a", VICTIM) == {
        "messages": 0, "vectors": 0, "parts": 0, "refs": 0, "display_names": 0}
    # 这两条是**宽谓词的回归**（也是上面那两条断言的补丁）：子查询版删不到孤儿，两张表各留一行
    # （`msg_parts.raw_xml` 就是 purge 要销毁的那份载荷），而上面照样全绿。
    # 判据在测试里**独立写一遍**（不借 chat_row_counts，也不用全表计数——keeper 在这两张表里
    # 本来就有一行，"全表为 0" 会被它挡住；要问的是「victim 这个分区还剩不剩」）。
    pref = f"a|{VICTIM_MD5}|"
    for table in ("msg_parts", "media_refs"):
        assert con.execute(f"SELECT count(*) FROM {table} WHERE substr(msg_key, 1, ?) = ?",
                           (len(pref), pref)).fetchone()[0] == 0, f"{table} 里还留着 victim 的行"


def test_delete_chat_leaves_the_other_chat_untouched(_temp_index):
    """**反向断言**：范围泄漏在这门命令上不可逆，比正断言重要。"""
    con = _temp_index
    store.delete_chat(con, account="a", chat_id=VICTIM)
    # keeper 的五项一个不少：前缀是 `account|chat_md5|`，**多一个 `|` 少一个 `|`、或者拿账户当前缀
    # 都会串到别的会话**（keeper 在 parts/refs 里各有一行，就是用来钉这件事的——前缀写宽了会把它
    # 一起删掉）。keeper 与 victim **同 account、前缀不同**：这正是「删除范围只能由 account+chat_id
    # 唯一定出」的判据；把 `chat_md5` 换回调用方传参、或让它与 `chat_row_counts` 的派生不一致，
    # 都会在这里或上一条断言的 `got` 上现形。
    assert store.chat_row_counts(con, "a", KEEPER) == {
        "messages": 1, "vectors": 0, "parts": 1, "refs": 1, "display_names": 1}
    assert con.execute("SELECT count(*) FROM display_names WHERE chat_id=?", (KEEPER,)
                       ).fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM messages_fts").fetchone()[0] == 1, \
        "FTS 必须由触发器跟随——少删一条就是搜索里还搜得到"


def test_rowid_reuse_regression(_temp_index):
    """**本轮最贵的一条**（spec §2.2）：`messages.id` 是裸 INTEGER PRIMARY KEY，
    删掉持有最大 id 的行之后那个 id 会被复用，而 vector_watermark 的语义是「id ≤ 它的都已嵌入」。

    没有那行 `min(last_rowid, max(id))` 修法时，新消息会拿到 ≤ 旧水位线的 id、永远不被嵌入。
    """
    con = _temp_index
    # 这两张表**不在 store.SCHEMA 里**——它们属于 VECTOR_SCHEMA（owner 是 vector.py），
    # 没建过向量索引的机器上不存在（store.py:83 的注释就是写这件事的）。本测试要验的是
    # 「有它们时水位线怎么修」，所以手工建；`vec_messages` 是虚表、需要 sqlite_vec，
    # 这里不建——正好顺带覆盖「没有向量表时 delete_chat 照常工作」。
    con.executescript(
        "CREATE TABLE IF NOT EXISTS vector_watermark ("
        "  model_id TEXT NOT NULL, account TEXT NOT NULL,"
        "  last_rowid INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL,"
        "  PRIMARY KEY (model_id, account));"
        "CREATE TABLE IF NOT EXISTS vector_meta ("
        "  model_id TEXT PRIMARY KEY, dim INTEGER NOT NULL, built_at INTEGER NOT NULL,"
        "  embedded INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0);")
    con.execute("INSERT INTO vector_watermark VALUES ('m', 'a', 999999, 0)")
    con.commit()
    store.delete_chat(con, account="a", chat_id=VICTIM)
    wm = con.execute("SELECT last_rowid FROM vector_watermark WHERE account='a'").fetchone()[0]
    assert wm == 1, f"水位线必须降到新的 max(id)=1，实际 {wm}"
    # 注意这条测试**不区分**「全局 max」与「该账号自己的 max」——本夹具只有一个账号，两者相同。
    # 也不必区分：2026-09-30 终审推翻了「必须全局」那条约束（扫描按账号过滤，危害不存在；
    # 执行计划也不用 id>? 做范围，性能也无差别）。别为了让它们可区分而往夹具里加第二个账号。
    #
    # 但水位线本身只是**中间量**，docstring 宣称保的是「新消息永远不被嵌入」这个后果。
    # 下面用**纯 SQL**（不经 vector.py）把后果钉住：删除后新插一条消息，它的 id 必须 > 水位线
    # ——那正是扫描谓词 `WHERE account = ? AND id > ?`（`vector.py:213-217` 的 _SELECT_ROWS）
    # 会扫到它的直接证据。少了这条，水位线取错行（比如取成别的账号、或干脆没降）时上面那条
    # 断言仍可能绿，而新消息已经静默不被嵌入了。
    new_key = f"a|{KEEPER_MD5}|message_0.db|2"
    con.execute(
        "INSERT INTO messages (msg_key, account, chat_md5, chat_id, shard, local_id, server_id,"
        " ts, msg_type, sender_id, sender_name, is_self, content, media_hint, exported_at)"
        " VALUES (?, 'a', ?, ?, 'message_0.db', 2, 2, 400, 1, NULL, NULL, 0, '新消息', NULL, 400)",
        (new_key, KEEPER_MD5, KEEPER))
    con.commit()
    new_id = con.execute("SELECT id FROM messages WHERE msg_key=?", (new_key,)).fetchone()[0]
    assert new_id > wm, (
        f"新消息 id={new_id} 必须 > 水位线 {wm}：id ≤ 水位线时扫描谓词 id>水位线 扫不到它，"
        "它会永远不被嵌入，而没有任何地方会报错（只有向量检索悄悄少结果）")


def test_delete_chat_without_any_vector_tables(_temp_index):
    """**轻装安装的判据**：这三张表属于 VECTOR_SCHEMA，没建过向量索引时一张都没有——
    而无条件查它们会让 `purge --chat` 直接崩在 `no such table`（spec §2.3 的警示框）。

    `_temp_index` 这个 fixture 本身就不建向量表，所以上面每个测试都在跑这条路径；
    这里显式钉一次，免得日后有人顺手在 fixture 里补上建表。
    """
    con = _temp_index
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "vec_messages" not in names and "vector_watermark" not in names, \
        "前提：这个 fixture 刻意不建向量表"
    got = store.delete_chat(con, account="a", chat_id=VICTIM)
    assert got["vectors"] == 0
    assert store.chat_row_counts(con, "a", VICTIM)["vectors"] == 0


def test_delete_chat_removes_vectors_of_that_chat_only(_temp_index):
    """**七处里唯一没有直接覆盖的一处**：`vec_messages` 是虚表，前面几条测试都只跑到
    `_has_table` 为假的分支（轻装安装）。这条真加载 sqlite_vec、真建表、真种向量、真删。

    删的是 **`acct_chat` 分区键**（`vector._acct_chat`，生产写入与查询共用同一个函数——
    用它而不是在测试里手拼，正是为了钉住 store 内联的 `f"{account}|{chat_md5}"` 与它同格式），
    不是 `msg_rowid IN (SELECT id FROM messages …)`：后者会漏掉**孤儿向量**（消息行早就不在了的
    那些），而孤儿带着正文的嵌入、正是隐私删除最该清掉的东西。
    """
    con = _temp_index
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.executescript(store.VECTOR_SCHEMA)      # 真 DDL（含 acct_chat 分区键），不手抄一份
    emb = sqlite_vec.serialize_float32([0.1] * store.VECTOR_DIM)
    victim_key = vector._acct_chat("a", VICTIM_MD5)
    keeper_key = vector._acct_chat("a", KEEPER_MD5)
    # msg_rowid 与夹具里 messages.id 对齐：keeper=1、victim=2,3；999 是孤儿（messages 里没有它）。
    con.executemany(
        "INSERT INTO vec_messages(msg_rowid, embedding, acct_chat, account, ts, preview) "
        "VALUES (?,?,?,?,?,NULL)",
        [(2, emb, victim_key, "a", 100), (3, emb, victim_key, "a", 200),
         (999, emb, victim_key, "a", 900), (1, emb, keeper_key, "a", 300)])
    con.commit()
    assert store.chat_row_counts(con, "a", VICTIM)["vectors"] == 3
    assert store.chat_row_counts(con, "a", KEEPER)["vectors"] == 1
    got = store.delete_chat(con, account="a", chat_id=VICTIM)
    assert got["vectors"] == 3, "孤儿向量也算数——它带着正文的嵌入"
    assert store.chat_row_counts(con, "a", VICTIM)["vectors"] == 0
    # keeper **逐条完好**：一条不多、一条不少，且 `acct_chat` 没被改
    assert [tuple(r) for r in con.execute(
        "SELECT msg_rowid, acct_chat FROM vec_messages ORDER BY msg_rowid")] == [(1, keeper_key)]
    assert store.chat_row_counts(con, "a", KEEPER)["vectors"] == 1


def test_delete_chat_recomputes_embedded_from_actual_rows(_temp_index):
    """`vector_meta.embedded` 必须按**实际剩余行数**重算，不能按 `before["vectors"]` 做减法。

    减法的两处错：① 没有 `WHERE model_id`（`vector_meta` 是每模型一行，所有行都会被减）；
    ② 减数是本会话分区的行数、**含孤儿**，而孤儿从来没被计进 `embedded`
    （`embedded` 的定义是「索引内当前行数」——重算才是这个定义的直接实现，还自愈历史漂移）。

    这里把 `embedded` 故意写成漂移值 99：它与真实行数 3 对不上。减法版会得到 99-2=97
    （减数是 **victim 分区**的 2 行，不含 keeper，也不含孤儿——这正是它错的地方），
    重算版必须得到删除后 `vec_messages` 的真实剩余行数 1。
    """
    con = _temp_index
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.executescript(store.VECTOR_SCHEMA)
    emb = sqlite_vec.serialize_float32([0.1] * store.VECTOR_DIM)
    victim_key = vector._acct_chat("a", VICTIM_MD5)
    con.executemany(
        "INSERT INTO vec_messages(msg_rowid, embedding, acct_chat, account, ts, preview) "
        "VALUES (?,?,?,?,?,NULL)",
        [(2, emb, victim_key, "a", 100), (999, emb, victim_key, "a", 900),  # 999 是孤儿
         (1, emb, vector._acct_chat("a", KEEPER_MD5), "a", 300)])
    con.execute("INSERT INTO vector_meta VALUES ('m', ?, 0, 99, 0)", (store.VECTOR_DIM,))
    con.commit()
    store.delete_chat(con, account="a", chat_id=VICTIM)
    left = con.execute("SELECT count(*) FROM vec_messages").fetchone()[0]
    got = con.execute("SELECT embedded FROM vector_meta WHERE model_id='m'").fetchone()[0]
    assert left == 1, "前提：只该剩下 keeper 那一行"
    assert got == left, f"embedded 必须等于删除后的真实行数 {left}（重算），实际 {got}"
