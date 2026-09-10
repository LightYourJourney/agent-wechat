"""本地检索库：流式落库、幂等去重、水位线、FTS5。

设计约束（全量导出面向超大规模数据）：
- 写入恒定内存：调用方按批喂 upsert_batch，绝不 fetchall
- 幂等：主键 (account, chat_md5, shard, local_id) + ON CONFLICT DO UPDATE——
  重复导出不产生重复行，且能吸收撤回/编辑引起的内容变化
- 增量：watermark 按 (account, shard, chat_md5) 记录已导到的 max(local_id)
"""
from __future__ import annotations

import pathlib
import sqlite3
import time

from . import paths

DB_PATH = paths.HOME_DIR / "index.db"
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

-- 预留向量表（sqlite-vec 需运行时加载扩展后建表，见 search.py）
CREATE TABLE IF NOT EXISTS watermark (
    account   TEXT NOT NULL,
    shard     TEXT NOT NULL,
    chat_md5  TEXT NOT NULL,
    max_local_id INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (account, shard, chat_md5)
);
"""

# ---------- 向量索引（sqlite-vec；由 vector.py 在加载扩展后建表） ----------

VECTOR_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS vec_messages USING vec0(
    msg_rowid   INTEGER PRIMARY KEY,          -- = messages.id，查回原表取正文
    embedding   float[512] distance_metric=cosine,
    acct_chat   TEXT partition key,           -- "{account}|{chat_md5}"，扫描面缩到单会话
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
    paths.ensure_home_dirs()
    con = sqlite3.connect(db_path or DB_PATH)
    con.executescript(SCHEMA)
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def upsert_batch(con: sqlite3.Connection, rows: list[dict]) -> None:
    """批量幂等写入。rows 来自 reader 的解码输出。"""
    now = int(time.time())
    con.executemany(
        """INSERT INTO messages (msg_key, account, chat_md5, chat_id, shard, local_id,
                                 server_id, ts, msg_type, sender_id, sender_name,
                                 is_self, content, media_hint, exported_at)
           VALUES (:msg_key, :account, :chat_md5, :chat_id, :shard, :local_id,
                   :server_id, :ts, :msg_type, :sender_id, :sender_name,
                   :is_self, :content, :media_hint, :exported_at)
           ON CONFLICT(msg_key) DO UPDATE SET
             content=excluded.content, msg_type=excluded.msg_type,
             sender_name=excluded.sender_name, exported_at=excluded.exported_at
           WHERE messages.content IS NOT excluded.content""",
        [{**r, "exported_at": now} for r in rows])
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


def get_watermark(con: sqlite3.Connection, account: str, shard: str, chat_md5: str) -> int:
    row = con.execute("SELECT max_local_id FROM watermark WHERE account=? AND shard=? AND chat_md5=?",
                      (account, shard, chat_md5)).fetchone()
    return row[0] if row else 0


def counts(con: sqlite3.Connection) -> dict:
    n = con.execute("SELECT count(*) FROM messages").fetchone()[0]
    chats = con.execute("SELECT count(DISTINCT chat_id) FROM messages").fetchone()[0]
    last = con.execute("SELECT max(ts) FROM messages").fetchone()[0]
    return {"messages": n, "chats": chats, "last_ts": last}
