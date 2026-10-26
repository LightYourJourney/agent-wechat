"""Phase 01：版本化 schema 迁移。

这里的**旧库一律手工建**（只跑 `store.SCHEMA`、不写 `schema_version`），才能复现
「上一个版本留下的库」这一状态——用 `store.connect()` 建出来的库已经是新版。

六类必测（任务书 §6）：合成旧库升级 / 事务失败 / 锁冲突 / 磁盘异常 / 重复升级 / 内容保全。
磁盘异常那条用「不可写的目录」而不是伪造 EIO：前者在本机可稳定复现，后者要靠桩。
"""
from __future__ import annotations

import pathlib
import re
import sqlite3

import pytest

from agent_wechat import errors, exclusions, migrations, store, vector


def _legacy_db(db: pathlib.Path) -> sqlite3.Connection:
    """旧版路径：整套 DDL 跑一遍，但**不 stamp 版本**。"""
    con = sqlite3.connect(db)
    con.executescript(store.SCHEMA)
    return con


def _trigger_sql(con: sqlite3.Connection, name: str) -> str | None:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
    return " ".join(row[0].split()) if row else None


def _expected_triggers() -> dict[str, str]:
    """按 `schema_drift_notes` 的同一口径归一：sqlite_master 里存的是
    `CREATE TRIGGER <name> …END`——**没有** `IF NOT EXISTS`，也**不带**结尾分号。"""
    out = {}
    for sql in store.FTS_TRIGGERS:
        m = re.search(r"CREATE TRIGGER IF NOT EXISTS (\w+)(.*?\bEND);", sql, re.S)
        assert m, sql
        out[m.group(1)] = " ".join(("CREATE TRIGGER " + m.group(1) + m.group(2)).split())
    return out


def _insert_message(con: sqlite3.Connection, *, local_id: int = 1,
                    content: str = "周末爬山的安排定了吗") -> str:
    key = f"acct|0123456789abcdef0123456789abcdef|message_0.db|{local_id}"
    con.execute(
        "INSERT INTO messages (msg_key, account, chat_md5, chat_id, shard, local_id,"
        " ts, msg_type, sender_id, sender_name, is_self, content, media_hint, exported_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, "acct", "0123456789abcdef0123456789abcdef", "wxid_abc123def456",
         "message_0.db", local_id, 1700000000 + local_id, 1, "wxid_abc123def456",
         "张三", 0, content, None, 1700000000))
    return key


# ---------------------------------------------------------------- 新库 / 重复升级

def test_fresh_database_is_stamped_and_reopen_changes_nothing(tmp_path):
    db = tmp_path / "i.db"
    con = store.connect(db)
    assert migrations.stored_version(con, migrations.SCOPE_MAIN) == store.SCHEMA_VERSION
    first = con.execute("SELECT applied_at FROM schema_version WHERE scope='main'").fetchone()[0]
    con.close()

    again = store.connect(db)
    assert migrations.stored_version(again, migrations.SCOPE_MAIN) == store.SCHEMA_VERSION
    assert again.execute(
        "SELECT applied_at FROM schema_version WHERE scope='main'").fetchone()[0] == first
    again.close()


def test_repeated_apply_is_a_noop(tmp_path):
    db = tmp_path / "i.db"
    con = _legacy_db(db)
    con.close()

    store.connect(db).close()
    con = store.connect(db)
    before = con.execute("SELECT version, applied_at FROM schema_version WHERE scope='main'").fetchone()
    assert migrations.apply(con, scope=migrations.SCOPE_MAIN,
                            migrations=store.MIGRATIONS) == store.SCHEMA_VERSION
    after = con.execute("SELECT version, applied_at FROM schema_version WHERE scope='main'").fetchone()
    assert tuple(before) == tuple(after)
    con.close()


# ---------------------------------------------------------------- 旧库升级 + 内容保全

def test_legacy_database_is_upgraded_and_keeps_every_row(tmp_path):
    db = tmp_path / "i.db"
    con = _legacy_db(db)
    key = _insert_message(con)
    con.execute("INSERT INTO watermark VALUES ('acct','message_0.db',"
                "'0123456789abcdef0123456789abcdef', 7, 1700000000)")
    con.execute("INSERT INTO msg_parts VALUES (?,?,?,?,?)",
                (key, 5, None, None, b"payload"))
    con.execute("INSERT INTO media_refs VALUES (?,?,?,?)",
                (key, "image", "f" * 32, None))
    con.execute("INSERT INTO display_names VALUES ('acct','wxid_abc123def456',"
                "'wxid_abc123def456','张三')")
    # 老库的触发器体漂移（EA-077 的形态）：IF NOT EXISTS 不会升级它
    con.execute("DROP TRIGGER msg_fts_au")
    con.execute("CREATE TRIGGER msg_fts_au AFTER UPDATE OF content ON messages "
                "BEGIN SELECT 1; END")
    con.commit()
    old_body = _trigger_sql(con, "msg_fts_au")
    con.close()

    con = store.connect(db)
    try:
        assert migrations.stored_version(con, migrations.SCOPE_MAIN) == store.SCHEMA_VERSION
        # 触发器体的漂移被 v2 修好；旧/新确实不同（否则这条断言是空转）
        assert old_body != _expected_triggers()["msg_fts_au"]
        for name, want in _expected_triggers().items():
            assert _trigger_sql(con, name) == want, name
        # 内容零丢失：五张表各一条，FTS 也能按正文命中
        assert con.execute("SELECT content FROM messages WHERE msg_key=?",
                           (key,)).fetchone()[0] == "周末爬山的安排定了吗"
        assert con.execute("SELECT count(*) FROM msg_parts").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM media_refs").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM display_names").fetchone()[0] == 1
        assert con.execute("SELECT max_local_id FROM watermark").fetchone()[0] == 7
        assert con.execute(
            "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH '\"周末爬山\"'"
        ).fetchone()[0] == 1
    finally:
        con.close()


# ---------------------------------------------------------------- 失败与恢复

def test_failed_migration_rolls_back_and_keeps_old_version(tmp_path):
    db = tmp_path / "i.db"
    con = _legacy_db(db)
    migrations.ensure_table(con)
    con.execute("INSERT INTO schema_version VALUES ('main', 1, 1)")
    con.commit()

    bad = (migrations.Migration(2, "坏迁移",
                                "CREATE TABLE half_done (x INTEGER);\n"
                                "INSERT INTO no_such_table VALUES (1);"),)
    with pytest.raises(errors.SchemaMigrationError) as ei:
        migrations.apply(con, scope="main", migrations=bad)
    assert "v2" in str(ei.value)
    # 整条回滚：版本不动，前半句建的表也不该在
    assert migrations.stored_version(con, "main") == 1
    assert con.execute(
        "SELECT count(*) FROM sqlite_master WHERE name='half_done'").fetchone()[0] == 0
    assert not con.in_transaction, "失败后必须把事务收掉"

    # 修好后重跑：从 2 继续，且只跑一次
    good = (migrations.Migration(2, "好迁移", "CREATE TABLE half_done (x INTEGER);"),)
    assert migrations.apply(con, scope="main", migrations=good) == 2
    assert con.execute(
        "SELECT count(*) FROM sqlite_master WHERE name='half_done'").fetchone()[0] == 1
    con.close()


def test_crash_without_commit_leaves_no_partial_schema(tmp_path):
    db = tmp_path / "i.db"
    con = _legacy_db(db)
    con.execute("BEGIN IMMEDIATE")
    con.execute("CREATE TABLE crash_probe (x INTEGER)")
    con.close()                                   # 未 COMMIT = 进程崩溃的等价形态

    con = store.connect(db)
    try:
        assert con.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='crash_probe'").fetchone()[0] == 0
        assert migrations.stored_version(con, migrations.SCOPE_MAIN) == store.SCHEMA_VERSION
    finally:
        con.close()


def test_migration_refuses_to_run_inside_an_open_transaction(tmp_path):
    db = tmp_path / "i.db"
    con = _legacy_db(db)
    migrations.ensure_table(con)
    con.commit()
    con.execute("BEGIN IMMEDIATE")
    with pytest.raises(errors.SchemaMigrationError, match="自动提交态"):
        migrations.apply(con, scope="main",
                         migrations=(migrations.Migration(2, "x", "SELECT 1;"),))
    con.rollback()
    con.close()


def test_write_lock_is_reported_as_a_migration_error(tmp_path):
    db = tmp_path / "i.db"
    con1 = _legacy_db(db)
    migrations.ensure_table(con1)
    con1.commit()
    con1.execute("BEGIN IMMEDIATE")               # 持写锁

    con2 = sqlite3.connect(db, timeout=0.05)
    try:
        with pytest.raises(errors.SchemaMigrationError, match="locked"):
            migrations.apply(con2, scope="main",
                             migrations=(migrations.Migration(2, "x", "SELECT 1;"),))
    finally:
        con2.close()
        con1.rollback()
        con1.close()


def test_disk_failure_surfaces_as_a_migration_error(tmp_path, monkeypatch):
    """磁盘异常：把版本表所在库换成只读连接，写版本行必然失败。"""
    db = tmp_path / "i.db"
    con = _legacy_db(db)
    con.close()

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        with pytest.raises(errors.SchemaMigrationError):
            migrations.apply(con, scope="main",
                             migrations=(migrations.Migration(2, "x", "SELECT 1;"),))
    finally:
        con.close()


# ---------------------------------------------------------------- scope 隔离

def test_future_version_is_refused_without_touching_the_database(tmp_path):
    db = tmp_path / "i.db"
    con = store.connect(db)
    con.execute("UPDATE schema_version SET version=99 WHERE scope='main'")
    con.commit()
    con.close()

    with pytest.raises(errors.SchemaMigrationError) as ei:
        store.connect(db)
    assert "99" in str(ei.value)

    raw = sqlite3.connect(db)
    try:
        assert raw.execute(
            "SELECT version FROM schema_version WHERE scope='main'").fetchone()[0] == 99
    finally:
        raw.close()


def test_exclusions_scope_is_versioned_separately_and_keeps_tombstones(tmp_path):
    db = tmp_path / "i.db"
    # 旧库：主库与排除清单都手工建，均无版本表
    con = _legacy_db(db)
    con.close()
    ex = exclusions.path_for(db)
    # `exclusions.SCHEMA` 里的表名带库名前缀，独立连接必须先把它 ATTACH 成 `exclusions`
    con = sqlite3.connect(":memory:")
    con.execute("ATTACH DATABASE ? AS exclusions", (str(ex),))
    con.executescript(exclusions.SCHEMA)
    con.execute("INSERT INTO exclusions.excluded VALUES "
                "('acct|m|message_0.db|1','acct','m',1)")
    con.commit()
    con.close()

    con = store.connect(db)
    try:
        assert migrations.stored_version(
            con, migrations.SCOPE_EXCLUSIONS, migrations.EXCLUSIONS_TABLE) == 1
        assert con.execute("SELECT count(*) FROM exclusions.excluded").fetchone()[0] == 1
    finally:
        con.close()


def test_main_scope_works_without_the_vector_component(tmp_path, monkeypatch):
    db = tmp_path / "i.db"
    # search 走的是 store.open()（默认 DB_PATH）；conftest 已把它指到 tmp，这里再把
    # 它指到本用例显式建的库，否则「基础检索照常工作」搜的是另一个文件。
    monkeypatch.setattr(store, "DB_PATH", db)
    con = store.connect(db)
    try:
        assert migrations.stored_version(con, migrations.SCOPE_MAIN) == store.SCHEMA_VERSION
        assert migrations.stored_version(con, migrations.SCOPE_VECTOR) == 0
        assert con.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='vec_messages'").fetchone()[0] == 0
        _insert_message(con)
        con.commit()
    finally:
        con.close()
    # 基础检索不依赖向量组件
    from agent_wechat import search
    hits = search.search("周末爬山", account="acct")
    assert hits["total"] == 1 and hits["mode"] == "fts"


def test_vector_scope_is_stamped_only_after_the_extension_loads(tmp_path):
    con = vector.connect()
    try:
        assert migrations.stored_version(con, migrations.SCOPE_VECTOR) == 1
        assert con.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='vec_messages'").fetchone()[0] == 1
    finally:
        con.close()


def test_vector_rebuild_restamps_the_vector_scope(tmp_path):
    """整表重建 = 回到未初始化态：版本行要清掉再 stamp，否则将来的向量迁移会被跳过。"""
    con = vector.connect()
    try:
        # 与 `vector.build_index(rebuild=True)` 的 DROP 脚本同一条语句（生产侧内联在
        # executescript 里，必须与 DROP 同事务，所以不抽成公共函数）
        con.execute("DELETE FROM schema_version WHERE scope='vector'")
        con.commit()
        assert migrations.stored_version(con, migrations.SCOPE_VECTOR) == 0
    finally:
        con.close()

    assert vector.build_index(accounts=[], rebuild=True) == {
        "embedded_this_run": 0, "skipped_this_run": 0}
    con = vector.connect()
    try:
        assert migrations.stored_version(con, migrations.SCOPE_VECTOR) == 1
    finally:
        con.close()
