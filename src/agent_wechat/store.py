"""本地检索库：流式落库、幂等去重、导出水位线、FTS5。

设计约束（全量导出面向超大规模数据）：
- 写入恒定内存：调用方按批喂 upsert_batch，绝不 fetchall
- 幂等：主键 (account, chat_md5, shard, local_id) + ON CONFLICT DO UPDATE——
  重复导出不产生重复行，且能吸收撤回/编辑引起的内容变化
- 增量：导出水位线 watermark 表按 (account, shard, chat_md5) 记录已导到的 max(local_id)
  读写入口是本模块的 load_export_watermarks / clear_export_watermarks（导出侧不写裸 SQL）
"""
from __future__ import annotations

import contextlib
import pathlib
import sqlite3
import time
from collections.abc import Iterator

from . import messages, paths

DB_PATH = paths.HOME_DIR / "index.db"


def db_paths() -> tuple[pathlib.Path, ...]:
    """index 库的文件族：基库 + WAL + SHM。

    为什么需要它：本模块的 DDL 设了 `PRAGMA journal_mode=WAL`，所以基库被删除时，
    若进程仍在运行或曾异常崩溃，`-wal` 会独立存在且**含明文页**——只删基库会留残留。
    「index 由哪些文件构成」是只有本模块有资格定义的知识，让调用方自己拼串就是复制一份。
    """
    return (DB_PATH, pathlib.Path(f"{DB_PATH}-wal"), pathlib.Path(f"{DB_PATH}-shm"))


BATCH = 2000

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    msg_key     TEXT NOT NULL UNIQUE,      -- account|chat_md5|shard|local_id
    account     TEXT NOT NULL,
    chat_md5    TEXT NOT NULL,             -- Msg_<md5> 表的 md5 部分
    chat_id     TEXT NOT NULL,             -- 解析出的会话名（wxid / xxx@chatroom / md5:xxx）
    shard       TEXT NOT NULL,             -- 来源分片 message_N.db
    local_id    INTEGER NOT NULL,
    server_id   INTEGER,
    ts          INTEGER NOT NULL,
    msg_type    INTEGER NOT NULL,          -- local_type & 0xffffffff
    sender_id   TEXT,                      -- 发送者 username（Name2Id 解析）
    sender_name TEXT,                      -- 群聊时从 content 前缀解析的显示名
    is_self     INTEGER NOT NULL DEFAULT 0,
    content     TEXT,                      -- 解压后的正文（appmsg 保留 XML）
    media_hint  TEXT,                      -- 非文本类型的简述
    exported_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_chat_ts ON messages(account, chat_id, ts);
CREATE INDEX IF NOT EXISTS idx_msg_ts      ON messages(account, ts);
CREATE INDEX IF NOT EXISTS idx_msg_sender  ON messages(account, sender_id);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, chat_id,
    content='messages', content_rowid='id',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS msg_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content, chat_id) VALUES (new.id, new.content, new.chat_id);
END;
CREATE TRIGGER IF NOT EXISTS msg_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, chat_id)
        VALUES ('delete', old.id, old.content, old.chat_id);
END;
CREATE TRIGGER IF NOT EXISTS msg_fts_au AFTER UPDATE OF content ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, chat_id)
        VALUES ('delete', old.id, old.content, old.chat_id);
    INSERT INTO messages_fts(rowid, content, chat_id) VALUES (new.id, new.content, new.chat_id);
END;

-- 向量表不在本 SCHEMA：sqlite-vec 需运行时加载扩展后建表，owner 是 vector.py（见 VECTOR_SCHEMA）
CREATE TABLE IF NOT EXISTS watermark (
    account   TEXT NOT NULL,
    shard     TEXT NOT NULL,
    chat_md5  TEXT NOT NULL,
    max_local_id INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (account, shard, chat_md5)
);

CREATE TABLE IF NOT EXISTS display_names (
    account      TEXT NOT NULL,
    chat_id      TEXT NOT NULL,
    member_key   TEXT NOT NULL,     -- 消息里出现的那个形态（username）
    display_name TEXT NOT NULL,
    PRIMARY KEY (account, chat_id, member_key)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS roster_meta (
    account      TEXT PRIMARY KEY,
    refreshed_at INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS media_refs (
    msg_key    TEXT PRIMARY KEY,          -- = messages.msg_key（account 等字段经 join 取回）
    media_kind TEXT NOT NULL,             -- 'image' | 'video'
    file_key   TEXT,                      -- packed_info_data 的 32-hex（dat/mp4 文件名权威）
    xml_md5    TEXT                       -- XML md5（兜底留证，与文件名无关）
) WITHOUT ROWID;
"""

# ---------- 向量索引（sqlite-vec；由 vector.py 在加载扩展后建表） ----------

# 向量维度的单一真值：本 DDL 的列宽与 vector.DIM 都从这里取，不再各写一个字面量
VECTOR_DIM = 512

VECTOR_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS vec_messages USING vec0(
    msg_rowid   INTEGER PRIMARY KEY,          -- = messages.id，查回原表取正文
    embedding   float[{VECTOR_DIM}] distance_metric=cosine,
    acct_chat   TEXT partition key,           -- "{{account}}|{{chat_md5}}"，扫描面缩到单会话
    account     TEXT,                         -- metadata：全局检索的账号隔离
    ts          INTEGER,                      -- metadata：时间过滤
    +preview    TEXT                          -- 辅助列（不参与过滤），调试用
);
CREATE TABLE IF NOT EXISTS vector_meta (
    model_id   TEXT PRIMARY KEY,              -- 如 fastembed:BAAI/bge-small-zh-v1.5:512
    dim        INTEGER NOT NULL,
    built_at   INTEGER NOT NULL,
    embedded   INTEGER NOT NULL DEFAULT 0,    -- 索引内当前行数
    skipped    INTEGER NOT NULL DEFAULT 0     -- 累计按规则跳过的行数
);
CREATE TABLE IF NOT EXISTS vector_watermark (
    model_id   TEXT NOT NULL,
    account    TEXT NOT NULL,
    last_rowid INTEGER NOT NULL DEFAULT 0,    -- 已嵌入的 max(messages.id)
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (model_id, account)
);
"""

# rebuild 用 DROP 而非 DELETE：vec0 的删除只在整块清空时回收空间，且 DELETE 路径有已知 bug
VECTOR_DROP = """
DROP TABLE IF EXISTS vec_messages;
DROP TABLE IF EXISTS vector_meta;
DROP TABLE IF EXISTS vector_watermark;
"""


def connect(db_path: pathlib.Path | None = None) -> sqlite3.Connection:
    """打开索引库连接（行工厂统一为 sqlite3.Row）。不负责关闭：用 open() 或自行 close()。"""
    paths.ensure_home_dirs()
    con = sqlite3.connect(db_path or DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.execute("PRAGMA synchronous=NORMAL")
    return con


@contextlib.contextmanager
def open(db_path: pathlib.Path | None = None) -> Iterator[sqlite3.Connection]:
    """连接生命周期的 owner：退出时**真正 close**，成功 commit、异常 rollback。

    不要用 `with sqlite3.connect(...)` 代替：那个上下文管理器只管事务，不关连接
    （关闭时机取决于引用计数归零，是实现细节而非契约）。这里三件事一次说清。

    提交是硬要求：export/usecases 的 upsert 正是靠退出提交落盘（export.run_export
    只在 full 分支显式 commit），本 context manager 与旧写法的事务语义完全一致。
    """
    con = connect(db_path)
    try:
        yield con
    except BaseException:
        con.rollback()
        raise
    else:
        con.commit()
    finally:
        con.close()


# INSERT 的列清单直接取自行契约（+ 本模块拥有的 exported_at），不再手抄一份
_INSERT_COLUMNS = (*messages.ROW_COLUMNS, "exported_at")
_INSERT_SQL = (
    f"INSERT INTO messages ({', '.join(_INSERT_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in _INSERT_COLUMNS)}) "
    "ON CONFLICT(msg_key) DO UPDATE SET "
    "content=excluded.content, msg_type=excluded.msg_type, "
    "sender_name=excluded.sender_name, exported_at=excluded.exported_at "
    "WHERE messages.content IS NOT excluded.content")


def upsert_batch(con: sqlite3.Connection, rows: list[messages.MessageRow]) -> None:
    """批量幂等写入。rows 来自 reader 的解码输出（messages.make_row 构造）。"""
    now = int(time.time())
    con.executemany(_INSERT_SQL, [{**r, "exported_at": now} for r in rows])
    wm: dict[tuple[str, str, str], int] = {}
    for r in rows:  # 水位线：批内按会话聚合后一条语句推进（单调 max）
        k = (r["account"], r["shard"], r["chat_md5"])
        if r["local_id"] > wm.get(k, 0):
            wm[k] = r["local_id"]
    con.executemany(
        """INSERT INTO watermark (account, shard, chat_md5, max_local_id, updated_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(account, shard, chat_md5) DO UPDATE SET
             max_local_id=max(max_local_id, excluded.max_local_id),
             updated_at=excluded.updated_at""",
        [(a, s, m, mx, now) for (a, s, m), mx in wm.items()])
    con.commit()


def upsert_media_refs(con: sqlite3.Connection,
                      rows: list[tuple[str, str, str | None, str | None]]) -> None:
    """批量幂等写 media_refs。不 commit：由 upsert_batch 的 commit（同事务）或 store.open() 收口。"""
    if not rows:
        return
    con.executemany(
        "INSERT INTO media_refs (msg_key, media_kind, file_key, xml_md5) VALUES (?,?,?,?) "
        "ON CONFLICT(msg_key) DO UPDATE SET media_kind=excluded.media_kind, "
        "file_key=excluded.file_key, xml_md5=excluded.xml_md5", rows)


def get_watermark(con: sqlite3.Connection, account: str, shard: str, chat_md5: str) -> int:
    row = con.execute("SELECT max_local_id FROM watermark WHERE account=? AND shard=? AND chat_md5=?",
                      (account, shard, chat_md5)).fetchone()
    return row[0] if row else 0


def load_export_watermarks(con: sqlite3.Connection, account: str) -> dict[tuple[str, str], int]:
    """导出水位线的批量读取：{(shard, chat_md5): max_local_id}，只含指定账号。

    键必须含 shard：local_id 是分片内序号，跨分片取 max 会漏导非最新分片的新消息。
    """
    return {(shard, md5): mx for shard, md5, mx in con.execute(
        "SELECT shard, chat_md5, max_local_id FROM watermark WHERE account=?", (account,))}


def clear_export_watermarks(con: sqlite3.Connection, account: str) -> None:
    """重置指定账号的导出水位线（全量导出用）。只删该 account，别的账号不受影响。"""
    con.execute("DELETE FROM watermark WHERE account = ?", (account,))


def counts(con: sqlite3.Connection) -> dict:
    n = con.execute("SELECT count(*) FROM messages").fetchone()[0]
    chats = con.execute("SELECT count(DISTINCT chat_id) FROM messages").fetchone()[0]
    last = con.execute("SELECT max(ts) FROM messages").fetchone()[0]
    return {"messages": n, "chats": chats, "last_ts": last}


# ---------- 显示名名册 ----------

# join 片段与表达式**必须成对使用**：_SENDER_EXPR 引用别名 dn，单独用是一个静默的
# no such column: dn.display_name。别名用 dn 而不是 d，是为了让
# test_join_drives_from_messages_not_the_roster 的 EXPLAIN 断言不会误判。
_SENDER_JOIN = ("LEFT JOIN display_names dn ON dn.account = m.account "
                "AND dn.chat_id = m.chat_id AND dn.member_key = m.sender_id")
_SENDER_EXPR = "COALESCE(dn.display_name, m.sender_name, m.sender_id)"


def roster_is_stale(con: sqlite3.Connection, account: str, ttl: int) -> bool:
    """该账号的名册是否该刷新：没记录过，或记录早于 `now - ttl`。

    查的是 roster_meta（一行），不是 display_names——后者在一次 DELETE + INSERT
    的中间态里会短暂为空，拿它判会得到假过期。
    """
    row = con.execute("SELECT refreshed_at FROM roster_meta WHERE account=?",
                      (account,)).fetchone()
    return row is None or (int(time.time()) - row[0]) >= ttl


def replace_roster(con: sqlite3.Connection, account: str,
                   rows: list[tuple[str, str, str]]) -> None:
    """整体替换该账号的名册，并更新 refreshed_at。

    **一个事务内 DELETE + INSERT**：中途失败若留下半份名册，那半份之外的人会突然
    显示成 wxid——比不刷新更糟（一半有名一半没名，用户无从判断哪个可信）。
    本函数不 commit：由调用方（`store.open()` 或 usecases）决定事务边界。
    """
    con.execute("DELETE FROM display_names WHERE account=?", (account,))
    con.executemany(
        "INSERT OR REPLACE INTO display_names (account, chat_id, member_key, display_name) "
        "VALUES (?,?,?,?)",
        [(account, chat_id, key, display) for chat_id, key, display in rows])
    con.execute(
        "INSERT INTO roster_meta (account, refreshed_at) VALUES (?,?) "
        "ON CONFLICT(account) DO UPDATE SET refreshed_at=excluded.refreshed_at",
        (account, int(time.time())))
