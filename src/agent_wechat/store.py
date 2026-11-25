"""本地检索库：流式落库、幂等去重、导出水位线、FTS5。

设计约束（全量导出面向超大规模数据）：
- 写入恒定内存：调用方按批喂 upsert_batch，绝不 fetchall
- 幂等：主键 msg_key = (account, chat_md5, shard, local_id, create_time) + ON CONFLICT DO UPDATE——
  重复导出不产生重复行，且能吸收撤回/编辑引起的内容变化
- 增量：导出水位线 watermark 表按 (account, shard, chat_md5) 记录已导到的 max(local_id)
  读写入口是本模块的 load_export_watermarks / clear_export_watermarks（导出侧不写裸 SQL）
"""
from __future__ import annotations

import contextlib
import os
import pathlib
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence

from . import errors, exclusions, messages, migrations, paths

DB_PATH = paths.HOME_DIR / "index.db"


def db_paths() -> tuple[pathlib.Path, ...]:
    """index 库的文件族：基库 + WAL + SHM。

    为什么需要它：本模块的 DDL 设了 `PRAGMA journal_mode=WAL`，所以基库被删除时，
    若进程仍在运行或曾异常崩溃，`-wal` 会独立存在且**含明文页**——只删基库会留残留。
    「index 由哪些文件构成」是只有本模块有资格定义的知识，让调用方自己拼串就是复制一份。
    """
    return (DB_PATH, pathlib.Path(f"{DB_PATH}-wal"), pathlib.Path(f"{DB_PATH}-shm"))


BATCH = 2000

# `IN (?,…)` 的分批大小。**批大小与 SQLite 的变量上限无关**：旧注释写的「上限 999」
# 早已过时（`SQLITE_MAX_VARIABLE_NUMBER` 自 3.32（2020）起默认 32766）。留着的理由
# 是别的：单条语句的参数个数与 SQL 文本长度都有实际代价，而这类列表是调用方给的
# （`msg_key` 的规模可以到几千），不该让一次调用生成一条几千占位符的语句。
KEYS_BATCH = 500

# Phase 02 的两张表：DDL 只有这一份，`SCHEMA` 与迁移 v3 都引用它。
AUDIT_CURSOR_DDL = (
    "CREATE TABLE IF NOT EXISTS audit_cursor ("
    " account TEXT NOT NULL, shard TEXT NOT NULL, chat_md5 TEXT NOT NULL,"
    " audited_local_id INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL,"
    " PRIMARY KEY (account, shard, chat_md5)) WITHOUT ROWID")
PENDING_REEMBED_DDL = (
    "CREATE TABLE IF NOT EXISTS pending_reembed ("
    " msg_key TEXT PRIMARY KEY,"
    " changed_at INTEGER NOT NULL,"
    " reason TEXT NOT NULL DEFAULT 'content') WITHOUT ROWID")
# Phase 03：重嵌失败账本。队列行本身（`pending_reembed`）保持「待办集合」语义——
# 成功才删；失败只在这里累计尝试次数与最近原因，队列行留在原处等下一轮。
REEMBED_FAILURES_DDL = (
    "CREATE TABLE IF NOT EXISTS reembed_failures ("
    " msg_key TEXT PRIMARY KEY,"
    " attempts INTEGER NOT NULL,"
    " last_error TEXT,"
    " last_attempt_at INTEGER NOT NULL) WITHOUT ROWID")
# Phase 04：一次同步的终态（`sync` 自己写；`doctor` 只读）。只记状态与计数，
# **不记**消息正文、密钥、媒体路径——这张表是运维诊断面，不是数据面。
SYNC_STATE_DDL = (
    "CREATE TABLE IF NOT EXISTS sync_state ("
    " account TEXT PRIMARY KEY,"
    " last_started_at INTEGER,"
    " last_finished_at INTEGER,"
    " last_status TEXT,"
    " last_error TEXT,"
    " stats_json TEXT) WITHOUT ROWID")
# Phase 06：派生文本（OCR/ASR）。**不覆盖原始消息**：正文只写在这里，按 msg_key 关联。
# `status` 三态：auto（引擎产出）/edited（用户改过）/deleted（用户删过，正文已清空）。
DERIVED_TEXT_DDL = """CREATE TABLE IF NOT EXISTS derived_text (
    derivation_id  TEXT PRIMARY KEY,          -- f"{msg_key}|{kind}"（确定性）
    msg_key        TEXT NOT NULL,
    account        TEXT NOT NULL,
    chat_id        TEXT NOT NULL,
    kind           TEXT NOT NULL,             -- ocr | asr
    text           TEXT NOT NULL DEFAULT '',
    engine         TEXT NOT NULL,
    engine_version TEXT,
    source         TEXT,                      -- 本机媒体路径（敏感，只存本地库）
    quality        REAL,
    status         TEXT NOT NULL DEFAULT 'auto',
    created_at     INTEGER NOT NULL,
    edited_at      INTEGER
);"""
DERIVED_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_derived_msg ON derived_text(msg_key)",
    "CREATE INDEX IF NOT EXISTS idx_derived_chat ON derived_text(account, chat_id)",
)

# 删除过的派生文本留墓碑：重扫**不得复活**（与 purge 的排除清单同一立场，但按 (msg_key, kind)）。
DERIVED_TOMBSTONE_DDL = """CREATE TABLE IF NOT EXISTS derived_tombstone (
    msg_key   TEXT NOT NULL,
    kind      TEXT NOT NULL,
    deleted_at INTEGER NOT NULL,
    PRIMARY KEY (msg_key, kind)
) WITHOUT ROWID;"""

DERIVED_FTS_DDL = """CREATE VIRTUAL TABLE IF NOT EXISTS derived_fts
    USING fts5(text, tokenize='trigram');"""

DERIVED_TRIGGERS: tuple[str, ...] = (
    """CREATE TRIGGER IF NOT EXISTS derived_fts_ai AFTER INSERT ON derived_text BEGIN
    INSERT INTO derived_fts(rowid, text) VALUES (new.rowid, new.text);
END;""",
    """CREATE TRIGGER IF NOT EXISTS derived_fts_au AFTER UPDATE OF text ON derived_text BEGIN
    UPDATE derived_fts SET text = new.text WHERE rowid = old.rowid;
END;""",
    """CREATE TRIGGER IF NOT EXISTS derived_fts_ad AFTER DELETE ON derived_text BEGIN
    DELETE FROM derived_fts WHERE rowid = old.rowid;
END;""",
)

# Phase 06：事实记忆。`status` 的四种语义是本阶段的核心区分：
# candidate=模型/Agent 提出、未经用户确认；confirmed=用户确认；retracted=用户撤回；
# disputed=有冲突（`conflicts_with` 指向另一条 claim）。
FACT_CLAIM_DDL = """CREATE TABLE IF NOT EXISTS fact_claim (
    claim_id      TEXT PRIMARY KEY,
    account       TEXT NOT NULL,
    chat_id       TEXT,
    claim         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'candidate'
                  CHECK (status IN ('candidate','confirmed','retracted','disputed')),
    conflicts_with TEXT,
    note          TEXT,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);"""
FACT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_claim_account ON fact_claim(account, chat_id)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_msg ON fact_evidence(msg_key)",
)

FACT_EVIDENCE_DDL = """CREATE TABLE IF NOT EXISTS fact_evidence (
    claim_id  TEXT NOT NULL,
    msg_key   TEXT NOT NULL,
    role      TEXT NOT NULL DEFAULT 'support',
    created_at INTEGER NOT NULL,
    PRIMARY KEY (claim_id, msg_key)
) WITHOUT ROWID;"""

AUDIT_TABLES = (AUDIT_CURSOR_DDL, PENDING_REEMBED_DDL, REEMBED_FAILURES_DDL)

# 事实记忆的状态词表：`derived.CLAIM_STATUSES` 从这里取，purge 也写这两个值——
# 词表只该有一处定义（store 被 derived 依赖，反向 import 会成环）。
CLAIM_RETRACTED = "retracted"
EVIDENCE_PURGED = "evidence_purged"

_SCHEMA_BODY = f"""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    msg_key     TEXT NOT NULL UNIQUE,      -- account|chat_md5|shard|local_id|create_time（v10）
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
    content     TEXT,                      -- 展示用正文：文本是真文本，appmsg 是 "[appmsg: 标题]"（原始 XML 在 msg_parts.raw_xml）
    media_hint  TEXT,                      -- 非文本类型的简述
    exported_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_chat_ts ON messages(account, chat_id, ts);
CREATE INDEX IF NOT EXISTS idx_msg_ts      ON messages(account, ts);
-- EA-076：**预留、零消费者**（照 `server_id` 的先例标注）。删除它需要动既有库（IF NOT EXISTS
-- 只挡创建）；在「按发送者筛」真被实现前先留着，别让一处 DDL 与一处注释各说各话。
CREATE INDEX IF NOT EXISTS idx_msg_sender  ON messages(account, sender_id);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, chat_id,
    content='messages', content_rowid='id',
    tokenize='trigram'
);

-- FTS 的三条触发器**不在这里**：它们被抽成 `FTS_TRIGGERS` 单一来源，既拼进本 SCHEMA，
-- 也被迁移 v2 用来对齐老库的触发器体（`CREATE TRIGGER IF NOT EXISTS` 不会升级已存在的体）。

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
    media_kind TEXT NOT NULL,             -- 'image' | 'video' | 'sticker'
    file_key   TEXT,                      -- 图片/视频：packed_info_data 的 32-hex（dat/mp4 文件名权威）；贴纸：emoji XML 的 md5
    xml_md5    TEXT                       -- XML md5（兜底留证，与文件名无关；贴纸为 NULL）
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS msg_parts (
    msg_key     TEXT PRIMARY KEY,      -- = messages.msg_key
    appmsg_type INTEGER NOT NULL,      -- <appmsg><type>
    ref_svrid   INTEGER,               -- 引用类（57）的 <refermsg><svrid>
    ref_msg_key TEXT,                  -- 解析出的目标消息；解不到为 NULL
    raw_xml     BLOB NOT NULL          -- 规范化后的 appmsg XML，zlib 压缩
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_parts_ref ON msg_parts(ref_msg_key)
    WHERE ref_msg_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS msg_extras (
    msg_key  TEXT PRIMARY KEY,      -- = messages.msg_key
    msg_type INTEGER NOT NULL,      -- 48 位置 / 42 名片（**原样记**，不猜语义）
    payload  BLOB NOT NULL          -- 规范化后的 XML，zlib 压缩（与 msg_parts.raw_xml 同款）
) WITHOUT ROWID;

-- Phase 02：历史消息**审计游标**。导出水位线只向前扫（local_id > max），看不见旧行被改写；
-- 这张表记录「每个 (账号,分片,会话) 已经回看到哪」。每轮导出只回看一小段，游标到头绕回 0，
-- 于是整段历史会在若干轮内被完整重读一遍。恒定内存：一轮只取 AUDIT_ROWS 行。
{AUDIT_CURSOR_DDL};

-- Phase 02：内容被改写、等待重嵌的消息。由 `msg_reembed_au` 触发器维护——**与更新同一
-- 事务**写入，所以「SQLite 已改、FTS 已跟随（既有触发器）、重嵌标记已落」三者不会分叉。
-- Phase 03 消费它；本阶段只保证标记正确产生。
{PENDING_REEMBED_DDL};

-- Phase 03：重嵌失败账本（与队列分表：队列是待办集合，这里记重试信息）
{REEMBED_FAILURES_DDL};

-- Phase 04：最近一次同步的终态（无正文/密钥/媒体路径）
{SYNC_STATE_DDL};

-- Phase 06：派生文本、删除墓碑、检索索引与事实记忆（原始消息表不动）
{DERIVED_TEXT_DDL};
{DERIVED_TOMBSTONE_DDL};
{DERIVED_FTS_DDL};
{FACT_CLAIM_DDL};
{FACT_EVIDENCE_DDL};
{"; ".join(DERIVED_INDEXES)};
{"; ".join(FACT_INDEXES)};
"""

# FTS 触发器：**单一来源**。`SCHEMA` 用它拼出全量建库脚本；迁移 v2 用同一批字符串
# 重建触发器（EA-077 的旧体漂移）。名字单独列出，是为了 DROP 时不必解析 SQL。
FTS_TRIGGER_NAMES: tuple[str, ...] = ("msg_fts_ai", "msg_fts_ad", "msg_fts_au")

FTS_TRIGGERS: tuple[str, ...] = (
    """CREATE TRIGGER IF NOT EXISTS msg_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content, chat_id) VALUES (new.id, new.content, new.chat_id);
END;""",
    """CREATE TRIGGER IF NOT EXISTS msg_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, chat_id)
        VALUES ('delete', old.id, old.content, old.chat_id);
END;""",
    """CREATE TRIGGER IF NOT EXISTS msg_fts_au AFTER UPDATE OF content, chat_id ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, chat_id)
        VALUES ('delete', old.id, old.content, old.chat_id);
    INSERT INTO messages_fts(rowid, content, chat_id) VALUES (new.id, new.content, new.chat_id);
END;""",
)

# Phase 02：内容/发送者/类型被改写时，**在更新它的同一事务里**投递一条重嵌标记。
# 不在 Python 里比对前后值：upsert 的 WHERE 已经保证「值没变就不更新」，触发器只需要
# 在真的更新发生时记一笔——这样标记、消息行与 FTS（既有三条触发器）三者天然同事务。
# **会话名（chat_id）不在内**（v9 起）：向量文本（`vector.doc_text`）与向量行都不含它（分区键用
# chat_md5），会话名从 `md5:` 自愈成真名时此前把整个会话的每一行都投进队列、白嵌一遍。
REEMBED_TRIGGER = """CREATE TRIGGER IF NOT EXISTS msg_reembed_au
AFTER UPDATE OF content, sender_name, msg_type ON messages
WHEN old.content IS NOT new.content
  OR old.sender_name IS NOT new.sender_name OR old.msg_type IS NOT new.msg_type
BEGIN
    INSERT INTO pending_reembed(msg_key, changed_at, reason)
    VALUES (new.msg_key, CAST(strftime('%s','now') AS INTEGER),
            CASE
                WHEN old.content IS NOT new.content THEN 'content'
                WHEN old.msg_type IS NOT new.msg_type THEN 'msg_type'
                ELSE 'sender'
            END)
    ON CONFLICT(msg_key) DO UPDATE SET
        changed_at=excluded.changed_at, reason=excluded.reason;
END;"""

# 首次导出的 FTS 批量路径（`fts_bulk`）的**持久化中断标记**：一行 = 一个正在（或曾经）以
# 「拆掉 FTS 触发器」方式灌库的进程。行在、灌库已不在进行 = 中途被杀或收口失败，FTS 缺行且不会
# 自己补上——`connect()` 见到就当场重建（`_ensure_fts_triggers`）。
FTS_BULK_DDL = """CREATE TABLE IF NOT EXISTS fts_bulk_pending (
    pid        INTEGER NOT NULL,
    started_at INTEGER NOT NULL
);"""

# `connect()` 每次跑的是**不含 FTS 触发器**的这一份：FTS 触发器改由 `_ensure_fts_triggers`
# 在判过中断标记之后再建——否则首次导出灌库期间，任何一个别的连接（MCP 检索、status）都会把
# 刚拆掉的触发器建回来，灌库自己的 UPDATE（会话名自愈）随即对一行从未进过索引的记录做 FTS
# 'delete'，外部内容 FTS 当场写坏（复现：`database disk image is malformed`）。
_SCHEMA_SANS_FTS_TRIGGERS = (_SCHEMA_BODY + "\n" + REEMBED_TRIGGER
                             + "\n" + "\n".join(DERIVED_TRIGGERS) + "\n" + FTS_BULK_DDL)
# 完整建库脚本（含 FTS 触发器）：触发器体漂移的比对基准、测试手工建旧库都用它。
SCHEMA = _SCHEMA_SANS_FTS_TRIGGERS + "\n" + "\n".join(FTS_TRIGGERS)

# 本程序已知的 main scope 最新版本。迁移只列**IF NOT EXISTS 表达不了**的变更；
# 基线（v1）由 `migrations.apply` 隐式 stamp——那代表「整套 DDL 已由 ensure 建好」。
SCHEMA_VERSION = 11

# FTS5 的 `secure-delete`（SQLite ≥ 3.42）：删除时把被删行的 token 从索引段里**当场**抹掉。
# 不开的话 `purge --chat` 之后，被删正文的 trigram 仍留在 `*_fts_data` 的活跃页里（实测 8 个里
# 有 7 个还在），要等某次段合并才消失。它是表级的持久设置（写进 `*_fts_config`），开一次即可。
# 更老的 SQLite 不认这条命令（报错会让整次迁移回滚、索引库打不开），所以那时 v8 是空迁移——
# 残留边界照旧写在 `purge.execute_chat` 的「保证的边界」里。
FTS_SECURE_DELETE_SUPPORTED = sqlite3.sqlite_version_info >= (3, 42, 0)
FTS_SECURE_DELETE: tuple[str, ...] = (
    "INSERT INTO messages_fts(messages_fts, rank) VALUES('secure-delete', 1)",
    "INSERT INTO derived_fts(derived_fts, rank) VALUES('secure-delete', 1)",
) if FTS_SECURE_DELETE_SUPPORTED else ()

# v10：旧 4 段 msg_key → `…|local_id|create_time`（`messages.row_key`）。
# - **先改旁表、后改 messages**：旁表靠旧键去 messages 取 ts，messages 先改就对不上了。
#   没有消息行的孤儿（回填命令写的、或 purge 前缀删之外的残留）保持 4 段——没有 ts 可补，
#   而 purge 的前缀删与排除判据（认 4 段旧键）照样盖得住它们。其中回填缓存三张表的孤儿
#   由 v11 删掉（见那一版）。
# - 只改**恰好 4 段**的键：新库上迁移也会跑（表是空的），已是 5 段的不会被再追加一次。
# - `messages.id` 不变：向量行（`msg_rowid`）与 FTS（rowid）不受影响；FTS / 重嵌触发器
#   不监听 msg_key，这次 UPDATE 不会触发它们。
# - `watermark.anchor_ts` 只能由迁移加列（`ensure` 在迁移之前跑，`CREATE TABLE IF NOT EXISTS`
#   不会给老表加列；基础 DDL 里也不写它，否则新库上这条 ALTER 会撞「列已存在」）。
_IS_LEGACY_KEY = "(length({c}) - length(replace({c}, '|', ''))) = 3"
_TS_OF = ("(SELECT CAST(COALESCE(m.ts, 0) AS INTEGER) FROM messages m "
          "WHERE m.msg_key = {t}.{c})")


def _v10_rekey(table: str, col: str = "msg_key", extra: str = "") -> str:
    return (f"UPDATE {table} SET {col} = {col} || '|' || {_TS_OF.format(t=table, c=col)}{extra} "
            f"WHERE {_IS_LEGACY_KEY.format(c=col)} "
            f"AND EXISTS (SELECT 1 FROM messages m WHERE m.msg_key = {table}.{col})")


_V10_STATEMENTS: tuple[str, ...] = (
    *(_v10_rekey(t) for t in ("media_refs", "msg_extras", "derived_tombstone",
                              "fact_evidence", "pending_reembed", "reembed_failures")),
    _v10_rekey("msg_parts", "ref_msg_key"),
    _v10_rekey("msg_parts"),
    # derivation_id = `{msg_key}|{kind}`：两列在同一条 UPDATE 里由同一个 ts 派生
    _v10_rekey("derived_text", extra=", derivation_id = msg_key || '|' || "
               + _TS_OF.format(t="derived_text", c="msg_key") + " || '|' || kind"),
    # 锚点按**旧键**走 msg_key 的唯一索引回填，所以排在 messages 改键之前
    "ALTER TABLE watermark ADD COLUMN anchor_ts INTEGER",
    "UPDATE watermark SET anchor_ts = (SELECT CAST(COALESCE(m.ts, 0) AS INTEGER) FROM messages m"
    " WHERE m.msg_key = watermark.account || '|' || watermark.chat_md5 || '|'"
    " || watermark.shard || '|' || watermark.max_local_id)",
    f"UPDATE messages SET msg_key = msg_key || '|' || CAST(COALESCE(ts, 0) AS INTEGER) "
    f"WHERE {_IS_LEGACY_KEY.format(c='msg_key')}",
)

MIGRATIONS: tuple[migrations.Migration, ...] = (
    migrations.Migration(
        version=2,
        name="对齐 FTS 触发器体（EA-077）",
        # 只重建触发器：DROP 后立刻按当前定义创建。数据与 FTS 内容都不动——
        # 触发器体漂移影响的是**后续写入**的镜像，不是存量行；存量失同步由 search 的
        # 探针单独报告（重跑导出可重建）。
        statements=tuple(f"DROP TRIGGER IF EXISTS {n}" for n in FTS_TRIGGER_NAMES)
        + FTS_TRIGGERS,
    ),
    migrations.Migration(
        version=3,
        name="历史消息审计游标与重嵌队列（Phase 02）",
        # 两张表 ensure 也会建（自愈）；这里再写一遍，是为了让「v2 的库升到 v3」这件事
        # 在迁移事务里有一份显式的、可审计的记录。触发器要 DROP 再建：老库上可能没有。
        statements=AUDIT_TABLES + ("DROP TRIGGER IF EXISTS msg_reembed_au", REEMBED_TRIGGER),
    ),
    migrations.Migration(
        version=4,
        name="重嵌失败账本（Phase 03）",
        statements=(REEMBED_FAILURES_DDL,),
    ),
    migrations.Migration(
        version=5,
        name="重嵌队列记录原因（Phase 03）",
        # 表重建而不是 ALTER：`ensure` 在迁移**之前**跑，老库上 `CREATE TABLE IF NOT
        # EXISTS` 不会加列，而新库上列已存在——只有「建新表 → 搬数据 → 换名」这一种
        # 脚本对新旧两种状态都成立（与 v3 的触发器 DROP/CREATE 同一条思路）。
        statements=(
            "CREATE TABLE IF NOT EXISTS pending_reembed_v5 ("
            " msg_key TEXT PRIMARY KEY, changed_at INTEGER NOT NULL,"
            " reason TEXT NOT NULL DEFAULT 'content') WITHOUT ROWID",
            "INSERT OR IGNORE INTO pending_reembed_v5(msg_key, changed_at, reason) "
            "SELECT msg_key, changed_at, 'content' FROM pending_reembed",
            # 触发器引用旧表：**先删触发器再删表**，否则 SQLite 在 DROP TABLE 时
            # 会因为触发器体指向一张不存在的表而拒绝（实测 v5 第一版就栽在这里）。
            "DROP TRIGGER IF EXISTS msg_reembed_au",
            "DROP TABLE pending_reembed",
            "ALTER TABLE pending_reembed_v5 RENAME TO pending_reembed",
            REEMBED_TRIGGER,
        ),
    ),
    migrations.Migration(
        version=6,
        name="同步终态表（Phase 04）",
        statements=(SYNC_STATE_DDL,),
    ),
    migrations.Migration(
        version=7,
        name="派生文本与事实记忆（Phase 06）",
        statements=(DERIVED_TEXT_DDL, DERIVED_TOMBSTONE_DDL, DERIVED_FTS_DDL,
                    FACT_CLAIM_DDL, FACT_EVIDENCE_DDL,
                    *DERIVED_INDEXES, *FACT_INDEXES) + DERIVED_TRIGGERS,
    ),
    migrations.Migration(
        version=8,
        name="FTS 开启 secure-delete（被删正文不留 trigram）",
        statements=FTS_SECURE_DELETE,
    ),
    migrations.Migration(
        version=9,
        name="重嵌触发器不再因会话名自愈触发",
        # 只换触发器体（见 `REEMBED_TRIGGER`）。已在队列里的 `chat_id` 行留着：旧触发器的
        # ON CONFLICT 会用它覆盖更早的 `content`，删掉可能丢一次真正的重嵌。
        statements=("DROP TRIGGER IF EXISTS msg_reembed_au", REEMBED_TRIGGER),
    ),
    migrations.Migration(
        version=10,
        name="msg_key 追加 create_time；水位线记锚点（识别表重建）",
        statements=_V10_STATEMENTS,
    ),
    migrations.Migration(
        version=11,
        name="清掉 v10 留下的 4 段旁表孤儿",
        # v10 改不了没有消息行的旁表行（补不出 create_time）。这三张表的行是回填命令
        # （`appmsg rebuild` / `media rebuild-refs` / `extras rebuild`）按微信库写的缓存：消息
        # 日后导出时拿的是 5 段键，4 段的那行再也对不上，只会把计数垫高。删掉，重跑回填即恢复。
        # 单独成一版而不是并进 v10：已经升到 v10 的库也要清一次。
        statements=tuple(f"DELETE FROM {t} WHERE {_IS_LEGACY_KEY.format(c='msg_key')}"
                         for t in ("msg_parts", "media_refs", "msg_extras")),
    ),
)

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
# EA-043：**不含 `vector_watermark`**。「清表」与「重置进度指针」是两件事——水位线记录
# 「已嵌入到哪」，与表里有没有行无关；把它绑在这个常量上，意味着任何整表重建路径都会顺手
# 抹掉续跑能力。重建进度由调用方**显式**重置（`vector.build_index` 的 rebuild 分支）。
# （F4 订正：初版这句写「purge 重建也读这个常量」——不成立，`VECTOR_DROP` 的生产消费者
#  只有 `vector.py` 一处；`purge --index` 删的是 `index.db` 整文件。）
VECTOR_DROP = """
DROP TABLE IF EXISTS vec_messages;
DROP TABLE IF EXISTS vector_meta;
"""


def connect(db_path: pathlib.Path | None = None) -> sqlite3.Connection:
    """打开索引库连接（行工厂统一为 sqlite3.Row）。不负责关闭：用 open() 或自行 close()。

    同时 **ATTACH 排除清单**（design spec §5.4）：三处拦截点与 purge 的事务都要在 SQL 里
    直接引用 `exclusions.excluded`，才能做到「判据与写入同事务」且「调用方无法忘记」。
    `executescript` 会隐式提交，所以这两步必须在任何显式事务之前——它本来就在这儿。

    **但这条跨库事务不保证崩溃中途的原子性**——SQLite 官方 lang_attach.html 原文：

    > Transactions involving multiple attached databases are atomic, assuming that the main
    > database is not ":memory:" and the journal_mode is not WAL. If the main database is
    > ":memory:" or if the journal_mode is WAL, then transactions continue to be atomic
    > within each individual database file. But if the host computer crashes in the middle
    > of a COMMIT where two or more database files are updated, some of those files might
    > get the changes where others might not.

    而本库第一句 DDL 就是 `PRAGMA journal_mode=WAL`（见 `SCHEMA`）。所以「墓碑必须先落」靠的
    不是事务本身，而是 purge 侧的两段式顺序：先独立提交一次墓碑，再在删除事务里重派生一次。
    也别给排除库另设 WAL——两个都 WAL 同样不保证跨库原子，只会掩盖真正的收口方式。
    """
    paths.ensure_home_dirs()
    db = db_path or DB_PATH
    con = None
    try:
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        ex_path = exclusions.path_for(pathlib.Path(db))
        ex_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        con.execute("ATTACH DATABASE ? AS exclusions", (str(ex_path),))
        # 先判版本、再写：`ensure` 里的 CREATE 也是写。版本过新的库必须在**任何写入之前**
        # 停下——否则旧程序会先给新库补建对象，再声称「不碰库」。
        migrations.refuse_if_newer(con, scope=migrations.SCOPE_MAIN, latest=SCHEMA_VERSION)
        migrations.refuse_if_newer(con, scope=migrations.SCOPE_EXCLUSIONS, latest=1,
                                   table=migrations.EXCLUSIONS_TABLE)
        # ensure：整套 DDL 每次连接都跑（自愈：缺表/缺索引就补建）。`CREATE … IF NOT EXISTS`
        # 的重建能力是既有行为，不能挪进「只跑一次」的迁移里。FTS 触发器在迁移之后单独判
        # （见 `_SCHEMA_SANS_FTS_TRIGGERS` 与 `_ensure_fts_triggers`）。
        con.executescript(_SCHEMA_SANS_FTS_TRIGGERS)
        con.executescript(exclusions.SCHEMA)
        # migrate：只做 ensure 表达不了的变更。两个 scope 各自事务、各自版本；
        # **不假设跨库原子**（index.db 与 exclusions.db 是两个文件，主库还是 WAL）。
        migrations.apply(con, scope=migrations.SCOPE_MAIN, migrations=MIGRATIONS)
        migrations.apply(con, scope=migrations.SCOPE_EXCLUSIONS, migrations=(),
                         table=migrations.EXCLUSIONS_TABLE)
        con.execute("PRAGMA synchronous=NORMAL")
        _ensure_fts_triggers(con)
    except (errors.SchemaMigrationError, errors.IndexUnavailable):
        # 迁移失败/库太新：**原样抛出**（它的文案已带 scope/version 与可执行动作），
        # 不翻成 IndexUnavailable——「库坏了」与「库太新/迁移没跑完」是两种病。
        # IndexUnavailable 来自 `_heal_fts_bulk`（已是项目异常），同样只关连接、原样抛。
        if con is not None:
            con.close()
        raise
    except (sqlite3.Error, OSError) as exc:
        # EA-074 扩：core 层只抛项目异常。此前只捕 `sqlite3.Error`，于是**建立段的磁盘错误**
        # （ENOSPC/EACCES/EIO，实测由 `mkdir`/`ATTACH`/`connect` 冒出来）与提交段的 sqlite
        # 错误都会裸穿到 CLI（A 矩阵 26 格实测：`PermissionError`/`OSError`/`DatabaseError`）。
        if con is not None:
            con.close()
        raise index_unavailable(exc) from exc
    return con


def index_unavailable(exc: BaseException) -> errors.IndexUnavailable:
    """建立段 / 提交段的裸异常 → 项目异常（同一个出口，三处共用）。

    各模块散落的 `con.commit()` 是本根因类的高发点（A 矩阵实测 export / purge / vector 共 9 处
    ENOSPC / EACCES / EIO / locked / corrupt 裸异常）。**不要把它包成 `store.commit(con)` 之类
    的间接层**：那会把调用点挪进本模块，而冻结表按调用点记账——间接层会让 9 个格全部变成
    「故障未命中」。出口只共享这个构造函数，`con.commit()` 必须留在原地。

    文案里嵌 `str(exc)`：磁盘故障的英文 strerror（`No space left on device` /
    `Permission denied`）与 project 文案一起进信封，用户与门禁都认得出是哪一类。
    """
    msg = str(exc)
    hint = ("索引库可能损坏、数据目录不可写或正被另一个进程写锁住；"
            "确认磁盘空间与目录权限、且没有其它导出在跑，"
            "仍失败则可删除 index.db 后重跑一次导出")
    if is_lock_error(exc):
        hint = "数据库被锁住——稍后重试，或确认没有其它导出 / 向量构建在跑"
    return errors.IndexUnavailable(f"本地检索库不可用：{errors.redact(msg)}", hint=hint)


def is_lock_error(exc: BaseException) -> bool:
    """这次 sqlite 失败是不是「被别的写者挡住」。

    判据只此一份：`index_unavailable` 用它挑 hint，只读诊断用它把「忙」与「坏」分开报
    ——后者此前自己拼 `" database is locked"` / `"database is busy"` 两个字面量。
    """
    return "locked" in str(exc) or "busy" in str(exc)


@contextlib.contextmanager
def open_readonly(db_path: pathlib.Path | None = None) -> Iterator[sqlite3.Connection]:
    """**只读**连接的 owner：`file:…?mode=ro`，退出时真正 close。

    给只读诊断用（CLI/MCP `status` 的自检、`doctor`）：`open()` 会 executescript(SCHEMA)
    并在旧库上跑迁移，把诊断变成写操作；另一个进程持写锁时它还要等满 timeout，然后把
    「忙」报成「库坏了」。这里只读，版本落后由调用方如实提示。
    """
    try:
        con = sqlite3.connect(paths.sqlite_ro_uri(db_path or DB_PATH), uri=True)
    except (sqlite3.Error, OSError) as exc:
        raise index_unavailable(exc) from exc
    try:
        yield con
    finally:
        con.close()


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
        try:
            con.commit()
        except (sqlite3.Error, OSError) as exc:
            # 退出提交是唯一让 `with` 里的写入落盘的时机：它失败必须让调用方看见，
            # 且按 core 的契约只能是项目异常（R2-A-006：此前裸 OSError 穿到 CLI）。
            raise index_unavailable(exc) from exc
    finally:
        con.close()


# INSERT 的列清单直接取自行契约（+ 本模块拥有的 exported_at），不再手抄一份
_INSERT_COLUMNS = (*messages.ROW_COLUMNS, "exported_at")
_INSERT_SQL = (
    f"INSERT INTO messages ({', '.join(_INSERT_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in _INSERT_COLUMNS)}) "
    "ON CONFLICT(msg_key) DO UPDATE SET "
    "content=excluded.content, msg_type=excluded.msg_type, "
    "sender_name=excluded.sender_name, chat_id=excluded.chat_id, "
    # `media_hint` 由 (msg_type, content) 派生，两者变了它必须跟着变：撤回把图片改成系统消息后，
    # 不写它的话库里永远留着「[图片]」。
    "media_hint=excluded.media_hint, "
    "exported_at=excluded.exported_at "
    # EA-012：守卫扩成「content **或 chat_id** 不同」。`chat_id` 是 `chat_md5` 的派生展示
    # 属性（退化形态 `md5:<hex>` 是真名的降级），名字解析回来后必须能自愈；只认 content
    # 时同一条消息会永远停在首次写入的 chat_id 上，同一 chat_md5 的行分裂成两种 id。
    # 退化名（`md5:<hex>`）**只许修好、不许写坏**：回看会重读旧行，而名字源
    # （session/contact）可能临时读不出来——那时 chat_map 缺项、解码结果退回退化 id。
    # 这一轮解出来的**整行**都不可信，不只是 chat_id：群消息的 `<wxid>:\n` 前缀要认得出
    # `@chatroom` 才剥（`reader._decode_row`），退化时 content 带着前缀、sender_name 为空。
    # 此前这道守卫只挡「仅 chat_id 不同」那一支，content 一不同就整行覆盖——好行被写成
    # 「md5:… / 无发送者 / 带前缀的正文」，chat_id 随后自愈，正文却要等下一轮回看。
    # 所以库里已是真名时，退化的来行一列都不写；反向（退化 → 真名）照常更新（会话标识恢复）。
    f"WHERE NOT (excluded.chat_id LIKE '{messages.MD5_PREFIX}%' "
    f"           AND messages.chat_id NOT LIKE '{messages.MD5_PREFIX}%') "
    "  AND (messages.content IS NOT excluded.content "
    # Phase 03：`doc_text` 的发送者前缀与「是否可嵌入」的判定都来自这两列——它们变了
    # 也要落库（触发器才能看到并投递重嵌）。`ts` / `sender_id` 对同一条消息稳定，不比。
    "   OR messages.sender_name IS NOT excluded.sender_name "
    "   OR messages.msg_type IS NOT excluded.msg_type "
    "   OR messages.chat_id IS NOT excluded.chat_id)")


def schema_drift_notes(con: sqlite3.Connection) -> list[str]:
    """库里已存在的**触发器体**与代码里 SCHEMA 文本不一致时的说明（EA-077）。

    `CREATE TRIGGER IF NOT EXISTS` 只挡创建、不挡演进：老库里留着旧触发器体，代码改了 DDL
    也**不会**生效——而 FTS 镜像与 EA-012 的修正集全靠它。

    迁移 v2 会在**升级那一刻**对齐一次（见 `MIGRATIONS`）；此后本函数只**报告**（挂进
    status 自检），不改库：再出现漂移多半是手工改动，自动重写会掩盖它。
    """
    expected: dict[str, str] = {}
    for m in re.finditer(r"CREATE TRIGGER IF NOT EXISTS (\w+)(.*?\bEND);", SCHEMA, re.S):
        expected[m.group(1)] = " ".join(("CREATE TRIGGER " + m.group(1) + m.group(2)).split())
    notes: list[str] = []
    for name, want in sorted(expected.items()):
        row = con.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                          (name,)).fetchone()
        if row is None:
            notes.append(f"触发器 {name} 不存在（索引库不完整）")
        elif " ".join((row[0] or "").split()) != want:
            # F8（T6 复审）：扩面的那一类差异（如 UPDATE OF 多一列）实测**不影响既有行为**
            # （写路径恒设 content，老触发器照样触发）——措辞别让用户为一条无功能影响的
            # 差异去重建 238 MB 索引。
            notes.append(f"触发器 {name} 的库内文本与代码不一致"
                         "（改过 DDL 的老库不会自动升级；只报不改库——此类差异通常不影响"
                         "既有行为，重建索引时自然消除）")
    return notes


def _begin_immediate(con: sqlite3.Connection) -> None:
    """拿写锁。**BEGIN 本身也会失败**（`database is locked`）——此前的四处都裸穿，
    回看把这条路径的触发面放大了（每轮都会写旧行）。与提交段同一个出口。
    """
    try:
        con.execute("BEGIN IMMEDIATE")
    except (sqlite3.Error, OSError) as exc:
        raise index_unavailable(exc) from exc


# 本进程里**正在进行**的批量灌库数（多线程：MCP 后台导出）。中断标记里的 pid 是本进程、而这里
# 是 0 = 本进程的灌库已经结束却没收口（收口失败）——与「写标记的进程已死」同样算中断。
# 没有这一条，常驻的 MCP 进程里一次收口失败就永远修不好（标记的 pid 一直是活的）。
_bulk_active = 0
_bulk_lock = threading.Lock()


@contextlib.contextmanager
def fts_bulk(con: sqlite3.Connection, *, enabled: bool = True) -> Iterator[bool]:
    """首次导出的 FTS 批量路径：消息表为空时拆触发器灌库、退出时一次 `rebuild`。yield 是否进入。

    逐行触发器是首次全量导出的大头（Phase 01 实测 77.8 万行：63.7 s → 灌入 27.6 s + 一次
    `rebuild` 7.3 s）。**只在消息表为空时**进入：已有数据的 `--full` 重扫几乎全是「值没变、
    不更新」，触发器本来就不触发，换成整表 rebuild 只会更慢。

    退出时无论成败都收口（重建 FTS、恢复触发器、清标记）——已提交的批次要能被检索到。收口
    失败时**原异常优先**、标记留着：下一次 `connect()`（本进程或别的进程）会补做。
    """
    global _bulk_active
    if not (enabled and fts_bulk_begin(con)):
        yield False
        return
    with _bulk_lock:
        _bulk_active += 1
    try:
        try:
            yield True
        except BaseException:
            # rollback 抛的是裸 sqlite3.Error：一并吞掉，否则它会顶替原异常穿出 core
            with contextlib.suppress(errors.AgentWeChatError, sqlite3.Error, OSError):
                con.rollback()
                fts_bulk_finish(con)
            raise
        fts_bulk_finish(con)
    finally:
        with _bulk_lock:
            _bulk_active -= 1


def fts_bulk_begin(con: sqlite3.Connection) -> bool:
    """消息表为空时拆掉 3 条 FTS 触发器、落中断标记（同一事务），返回是否进入批量路径。

    ad / au 也必须拆：灌库过程中有 UPDATE（会话名自愈），au 去 'delete' 一行从没进过索引的
    记录会把外部内容 FTS 写坏。窗口里别的连接不会把它们建回来：`connect()` 见到**进行中**的
    标记就不建（`_ensure_fts_triggers`），而判断与建触发器都在写锁下，与本函数互斥。
    """
    _commit_then_begin(con)
    try:
        if con.execute("SELECT 1 FROM messages LIMIT 1").fetchone() is not None:
            con.rollback()
            return False
        con.execute("INSERT INTO fts_bulk_pending(pid, started_at) VALUES (?, ?)",
                    (os.getpid(), int(time.time())))
        for name in FTS_TRIGGER_NAMES:
            con.execute(f"DROP TRIGGER IF EXISTS {name}")
        con.commit()
    except (sqlite3.Error, OSError) as exc:
        con.rollback()
        raise index_unavailable(exc) from exc
    return True


def fts_bulk_finish(con: sqlite3.Connection) -> None:
    """恢复触发器 → 按消息表整体 `rebuild` FTS → 清标记，同一事务。

    清的是**全部**标记行：rebuild 覆盖了此刻所有已提交的行（别的灌库进程的写事务不可能与
    本事务的写锁并存），此后到来的行有触发器跟随。`secure-delete` 配置在 rebuild 后保留。
    """
    _commit_then_begin(con)
    try:
        _finish_in_txn(con)
        con.commit()
    except (sqlite3.Error, OSError) as exc:
        con.rollback()
        raise index_unavailable(exc) from exc


def _finish_in_txn(con: sqlite3.Connection) -> None:
    for ddl in FTS_TRIGGERS:
        con.execute(ddl)
    con.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
    con.execute("DELETE FROM fts_bulk_pending")


def _commit_then_begin(con: sqlite3.Connection) -> None:
    """批量路径的两段都要**自己的**写事务：先把调用方已做完的写提交掉（export 的调用点上只有
    全量导出复位水位线那一笔，它本来就紧跟着显式 commit），再拿写锁。提交失败与 BEGIN 失败
    同一个出口（core 只抛项目异常）。"""
    if con.in_transaction:
        try:
            con.commit()
        except (sqlite3.Error, OSError) as exc:
            raise index_unavailable(exc) from exc
    _begin_immediate(con)


def fts_bulk_interrupted(con: sqlite3.Connection) -> int:
    """被中断的批量重建：返回中断标记行数；灌库还在进行（或判不出来）时返回 0。

    「不在进行」= 每一行的 pid 要么已死，要么是本进程而本进程此刻没有灌库（`_bulk_active`）。
    判不出死活（`pid_alive` 对 EPERM 答「活」）一律当进行中——宁可晚修，不在别人灌库中途
    抢写锁重建。`_ensure_fts_triggers`（修）与 `sync.diagnose`（只报）共用这一个判据。
    """
    rows = con.execute("SELECT pid, started_at FROM fts_bulk_pending").fetchall()
    me = os.getpid()
    # pid 活着还不够：写标记的进程被杀后 pid 可能被别的进程复用（EPERM 也答「活」），
    # 那样标记会一直被当成进行中。进程**晚于标记**启动的，必然不是写标记的那个。
    live = any(_bulk_active > 0 if p == me
               else paths.pid_alive(p) and not paths.pid_started_after(p, t)
               for p, t in rows)
    return 0 if live else len(rows)


def _fts_triggers_present(con: sqlite3.Connection) -> bool:
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    return set(FTS_TRIGGER_NAMES) <= names


def _ensure_fts_triggers(con: sqlite3.Connection) -> None:
    """`connect()` 的最后一步：FTS 触发器该在就建上，被中断的批量重建当场补做，进行中的不碰。

    常态（触发器都在、没有标记）只有两次读，不拿写锁。要动手时先拿写锁、**在锁下重判**：
    与 `fts_bulk_begin` 互斥，所以不会出现「这边判完没有标记、那边刚拆完触发器、这边又建回来」。
    """
    marked = con.execute("SELECT 1 FROM fts_bulk_pending LIMIT 1").fetchone() is not None
    if not marked and _fts_triggers_present(con):
        return
    if marked and not fts_bulk_interrupted(con):
        return                                   # 灌库进行中：触发器就该是拆着的
    _commit_then_begin(con)
    try:
        if con.execute("SELECT 1 FROM fts_bulk_pending LIMIT 1").fetchone() is None:
            for ddl in FTS_TRIGGERS:             # 没有批量路径：缺了就补（既有的自愈行为）
                con.execute(ddl)
        elif fts_bulk_interrupted(con):
            _finish_in_txn(con)
        con.commit()
    except (sqlite3.Error, OSError) as exc:
        con.rollback()
        raise index_unavailable(exc) from exc


def _filter_excluded(con: sqlite3.Connection, keys: Sequence[str]) -> set[str]:
    """本批里被排除的 msg_key。**必须在调用方的事务内查**，与随后的写同事务：
    读在提交前、写在提交后的话，那份陈旧结果会把被删的行原样写回去（spec §5.4）。
    判据本体在 `exclusions.excluded_keys`（含 v10 前的 4 段墓碑），这里只钉批大小。"""
    return exclusions.excluded_keys(con, keys, batch=KEYS_BATCH)


def _heal_degraded_chat_ids(con: sqlite3.Connection, rows: list[messages.MessageRow]) -> None:
    """本批里解析出真名的会话：把它**其余**仍停在 `md5:<hex>` 的行一并改成真名。

    读路径按 `chat_id = ?` 精确筛（检索 / 会话导出 / 时间线 / 上下文），停在退化 id 的
    行对它们不可见。upsert 的 DO UPDATE 只修**本批重扫到的**行，而前向扫描不会再读旧行
    ——所以按会话修，不按行修。退化 id 由 md5 唯一决定，等值匹配走 `idx_msg_chat_ts`；
    方向只有「退化 → 真名」，与 `_INSERT_SQL` 的「只许修好、不许写坏」同一立场。
    `derived_text` 与 `fact_claim` 的 `chat_id` 都抄自 messages（`derived.add_claim` 未显式给
    会话时取证据行的），同批修正——否则那些行停在退化 id 上，按会话筛照样不可见。
    """
    pairs: set[tuple[str, str, str]] = set()
    named: set[tuple[str, str]] = set()
    degraded: set[tuple[str, str]] = set()
    for r in rows:
        key = (r["account"], r["chat_md5"])
        if r["chat_id"].startswith(messages.MD5_PREFIX):
            degraded.add(key)
        else:
            named.add(key)
            pairs.add((r["chat_id"], r["account"], messages.degraded_chat_id(r["chat_md5"])))
    # 反方向：本批里只有退化 id 的会话（名字源这一轮读不出来），而库里该会话**已有**真名行
    # ——按已知真名修。查找走 `msg_key` 唯一索引的前缀区间（`account|md5|…`；`}` 紧跟 `|`
    # 之后），不为它新建索引；只在本批出现退化行时才查，正常导出零开销。
    for acct, md5 in degraded - named:
        prefix = messages.msg_key_prefix(acct, md5)
        hit = con.execute(
            "SELECT chat_id FROM messages WHERE msg_key >= ? AND msg_key < ?"
            " AND chat_id NOT LIKE ? LIMIT 1",
            (prefix, prefix[:-1] + "}", messages.MD5_PREFIX + "%")).fetchone()
        if hit is not None:
            pairs.add((hit[0], acct, messages.degraded_chat_id(md5)))
    if not pairs:
        return
    con.executemany("UPDATE messages SET chat_id=? WHERE account=? AND chat_id=?", pairs)
    con.executemany("UPDATE derived_text SET chat_id=? WHERE account=? AND chat_id=?", pairs)
    con.executemany("UPDATE fact_claim SET chat_id=? WHERE account=? AND chat_id=?", pairs)


def upsert_batch(con: sqlite3.Connection, rows: list[messages.MessageRow]) -> int:
    """批量幂等写入。rows 来自 reader 的解码输出（messages.make_row 构造）。
    返回**被排除而跳过的**行数（`0` = 全写入）；排除清单里的 `msg_key` 一条都不落库。

    排除判据与写入同事务：调用方可能已经开了事务（`export` 先写 refs），那就在那个事务里；
    没开就自己起一个 IMMEDIATE 的——写锁一拿，读到的最新状态在本次写入前不会再变
    （只判据同事务还不够：读在提交前、写在提交后，那份陈旧结果会把被删的行写回去，spec §5.4）。

    **水位线走全量 rows**：跳过写、不跳过水位线——否则该会话的增量导出会卡在被排除的
    那一段反复重扫（spec §5.4 第 1 条）。返回值恒等于 `len(rows) - <实际写入的行数>`，
    调用方据此把「读到的」与「入库的」分开计（计数契约，见 `export.run_export`）。
    """
    if not con.in_transaction:
        _begin_immediate(con)
    now = int(time.time())
    skip = _filter_excluded(con, [r["msg_key"] for r in rows])
    keep = [r for r in rows if r["msg_key"] not in skip]
    if keep:
        con.executemany(_INSERT_SQL, [{**r, "exported_at": now} for r in keep])
        _heal_degraded_chat_ids(con, keep)
    wm: dict[tuple[str, str, str], tuple[int, int]] = {}
    for r in rows:  # 水位线：批内按会话聚合后一条语句推进（单调 max）
        k = (r["account"], r["shard"], r["chat_md5"])
        if r["local_id"] > wm.get(k, (0, 0))[0]:
            wm[k] = (r["local_id"], int(r["ts"] or 0))
    # `anchor_ts` 跟着 max 那一行走（reader 据它识别表重建）；max 没前进就不动。
    # UPSERT 的 SET 里引用的列都是**旧值**，两条赋值互不影响。
    con.executemany(
        """INSERT INTO watermark (account, shard, chat_md5, max_local_id, anchor_ts, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(account, shard, chat_md5) DO UPDATE SET
             anchor_ts=CASE WHEN excluded.max_local_id >= max_local_id
                            THEN excluded.anchor_ts ELSE anchor_ts END,
             max_local_id=max(max_local_id, excluded.max_local_id),
             updated_at=excluded.updated_at""",
        [(a, s, m, mx, ts, now) for (a, s, m), (mx, ts) in wm.items()])
    try:
        con.commit()
    except (sqlite3.Error, OSError) as exc:
        # R2-A-004：提交失败此前裸穿（实测 EIO）。这里与 `open()` 的出口同一口径。
        raise index_unavailable(exc) from exc
    return len(rows) - len(keep)


def has_table(con: sqlite3.Connection, name: str) -> bool:
    """该表在本连接上存不存在。**向量那三张表在轻装安装上根本不存在**——
    它们在 `VECTOR_SCHEMA` 里，而 `store.connect()` 只跑 `SCHEMA`（`SCHEMA` 里那行注释就是
    写这件事的：「向量表不在本 SCHEMA：sqlite-vec 需运行时加载扩展后建表，owner 是 vector.py」）。
    无条件查它们会让 purge --chat 直接崩在 `no such table`（spec §2.3 的警示框）。
    """
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _vec_exec(con: sqlite3.Connection, sql: str, args: tuple = ()) -> sqlite3.Cursor:
    """在 `vec_messages` 上执行一条语句，把「这条连接没加载 sqlite-vec」翻成项目异常。

    **为什么必须翻**：`store.connect()` 从不加载扩展——加载是 `vector.connect()` 的职责
    （`vector.py:31` 的 docstring 写着「sqlite-vec 的知识只留在本模块」）。于是装了向量索引的
    机器上（表在、扩展没加载），本模块任何一句真碰 `vec_messages` 的 SQL 都抛裸
    `sqlite3.OperationalError: no such module: vec0`，违反「core 层只抛 AgentWeChatError 子类」
    （`errors.py:1`）。`has_table` 只证明「表在」，不证明「这条连接能用它」——两个判据的差
    正是这一格。

    SELECT 与 DELETE **都是响亮失败**（实测 sqlite-vec 0.1.9：`execute()` 当场抛，不是静默
    空操作），所以不需要 rowcount 对账之类的守卫。

    只翻 `no such module` 一种：语法错、库损坏等其它 OperationalError 原样透出——把它们
    一并翻译会掩盖真正的问题。也**不 import sqlite_vec / vector**：store 只认 sqlite3。
    """
    try:
        return con.execute(sql, args)
    except sqlite3.OperationalError as e:
        if "no such module" not in str(e):
            raise
        raise errors.PurgeRefused(
            f"无法访问向量表 vec_messages：{e}",
            hint="这条连接没加载 sqlite-vec；purge 路径应改用 vector.connect() 开连接"
                 "（store.connect() 不加载扩展）") from e


def chat_row_counts(con: sqlite3.Connection, account: str, chat_id: str) -> dict[str, int]:
    """该会话在各张表里的存活条数。核查与测试共用，避免各处各写一套判据。

    **`msg_parts` / `media_refs` 必须按 `msg_key` 前缀数，不能经 `messages` 子查询数。**
    子查询那版有个致命的盲区（Task 3 的实现者用变异证明了）：`messages` 一删，子查询立刻为空，
    于是**这两张表里的孤儿行恒不被计数**——把 `DELETE FROM messages` 提到前面，四条测试全绿，
    而两张表各留一行孤儿（`msg_parts.raw_xml` 里是完整的 appmsg 载荷）。**核查也会继承这个盲区**：
    一门自称「删干净了」的命令对着留了正文的孤儿行报 ✅。孤儿不是臆想：`appmsg rebuild` /
    `media rebuild-refs` 只写 parts/refs、不写 messages，生产上就会留下它们。

    前缀里的 `chat_md5` **在这里就地派生**（`messages.chat_md5(chat_id)`，退化形态安全；
    用 `md5_of` 会对 `md5:<hex>` 再哈希一次、得到错的 md5）。就地派生而不是加一个参数，
    是为了不动这个方法已有的全部调用点。**不能用 `LIKE`**：`account` 里含 `_` 与 `%`。
    """
    def n(sql: str, args: tuple) -> int:
        return con.execute(sql, args).fetchone()[0]
    md5 = messages.chat_md5(chat_id)
    acct_chat = messages.acct_chat(account, md5)
    p = messages.msg_key_prefix(account, md5)
    # **同一个会话在索引里可能有两种 `chat_id`**：真名与退化形态 `md5:<hex>`（见 upsert 的
    # EA-012 注释——名字源临时读不出来时解码结果退回退化 id，之后名字恢复了才自愈）。
    # 按单个 `chat_id` 计数会漏掉另一种形态的整批行，而**核查也会继承这个盲区**：一份
    # 漏删的会话对着「剩下 0 条」报 ✅。两种形态共享同一个 `chat_md5`，所以一次算出来、
    # 所有按 `chat_id` 过滤的表都用 `IN (?, ?)` 覆盖。
    # 谓词是**并集**：`chat_md5` 覆盖「同一会话分裂成两种 chat_id」的全部形态，`chat_id IN`
    # 兜住历史/夹具里 `chat_md5` 与 `chat_id` 不匹配的行（真实数据两者恒一致，但索引是
    # 长期演进的，不能假设）。只用 md5 会在 `chat_md5` 不一致时**静默一条不中**。
    ident = messages.ChatIdentity.of(account, chat_id)
    md5 = ident.md5
    return {
        "messages": n(f"SELECT count(*) FROM messages WHERE account=? AND {ident.messages_where}",
                      ident.messages_args),
        "vectors": (_vec_exec(con, "SELECT count(*) FROM vec_messages WHERE acct_chat=?",
                              (acct_chat,)).fetchone()[0]
                    if has_table(con, "vec_messages") else 0),
        "parts": n("SELECT count(*) FROM msg_parts WHERE substr(msg_key, 1, ?) = ?", (len(p), p)),
        "refs": n("SELECT count(*) FROM media_refs WHERE substr(msg_key, 1, ?) = ?", (len(p), p)),
        "extras": n("SELECT count(*) FROM msg_extras WHERE substr(msg_key, 1, ?) = ?", (len(p), p)),
        "display_names": n(
            f"SELECT count(*) FROM display_names WHERE account=? AND {ident.chat_id_where}",
            ident.chat_id_args),
        # Phase 06：派生文本与事实记忆同属「删了就要核实真的没了」的那类数据。
        "derived": n("SELECT count(*) FROM derived_text WHERE substr(msg_key, 1, ?) = ?",
                     (len(p), p)),
        "claims": n(
            f"SELECT count(*) FROM fact_claim WHERE account=? AND {ident.chat_id_where}",
            ident.chat_id_args),
        "evidence": n("SELECT count(*) FROM fact_evidence WHERE substr(msg_key, 1, ?) = ?",
                      (len(p), p)),
        "derived_tombstones": n(
            "SELECT count(*) FROM derived_tombstone WHERE substr(msg_key, 1, ?) = ?",
            (len(p), p)),
        # 全局不变量：FTS 行没有对应的 derived_text 行 = 触发器失效、正文可能已删而索引还在。
        # 它不是「本会话」的计数（FTS 没有 msg_key 列），但 purge 的核查要求它恒为 0。
        "derived_fts_orphans": n(
            "SELECT count(*) FROM derived_fts_content c"
            " LEFT JOIN derived_text d ON d.rowid = c.id WHERE d.rowid IS NULL", ()),
    }


# 按 `msg_key` 存会话副本的表（`account|chat_md5|…` 前缀）。`messages` 不在其列：它有 account /
# chat_md5 两列，走 `ChatIdentity.messages_where`。
_MSG_KEY_TABLES = ("msg_parts", "media_refs", "msg_extras", "derived_text", "derived_tombstone",
                   "fact_evidence", "pending_reembed", "reembed_failures")


def accounts_holding_chat(con: sqlite3.Connection, chat_id: str) -> list[str]:
    """索引里**任何一张表**存着这个会话副本的账号（升序）。`purge --chat` 据此决定删哪些账号。

    只看 `messages` 不够：`appmsg rebuild` / `media rebuild-refs` / `extras rebuild` / 名册刷新
    扫的是**整个微信库**，不管会话导没导过——于是一个账号可以一条消息都没进索引，却在
    `msg_parts`（完整 appmsg 载荷）、`media_refs`、`display_names` 里有这个会话。只按消息找账号，
    这些行一行都不删，核查（它也只看给定账号）还报 ✅。

    账号取 `msg_key` 的第一段（账号不含 `|`，见 `count_media_refs`）；第二段逐字比 `chat_md5`，
    不用 LIKE（账号里的 `_` / `%` 是通配符）。全表扫——`purge --chat` 是罕见的人工操作。
    """
    ident = messages.ChatIdentity.of("", chat_id)
    key_md5 = ident.md5 + "|"
    parts = [f"SELECT account FROM messages WHERE {ident.messages_where}"]
    args: list[object] = [ident.md5, ident.chat_id, ident.degraded_id]
    for t in _MSG_KEY_TABLES:
        parts.append(f"SELECT substr(msg_key, 1, instr(msg_key, '|') - 1) FROM {t}"
                     " WHERE substr(msg_key, instr(msg_key, '|') + 1, ?) = ?")
        args += [len(key_md5), key_md5]
    for t in ("display_names", "fact_claim"):
        parts.append(f"SELECT account FROM {t} WHERE {ident.chat_id_where}")
        args += [ident.chat_id, ident.degraded_id]
    return [r[0] for r in con.execute(" UNION ".join(parts) + " ORDER BY 1", args)]


def delete_chat(con: sqlite3.Connection, *, account: str, chat_id: str) -> dict[str, int]:
    """删掉一个会话在本工具里的全部副本。**十二张表，一张不漏**（spec §5.1；另有两张 FTS 由触发器跟随、两张向量元数据表就地改写。EA-041 补的 `msg_extras` 与 `msg_parts` / `media_refs` 同属按 `msg_key` 前缀删的宽谓词那一类，孤儿行照样盖得住）。

    **调用方负责**：① 事务边界（自己 `BEGIN IMMEDIATE` / `commit`）；
    ② `PRAGMA secure_delete=ON`——实测默认设置下被删消息的正文仍留在 index.db 的 freelist
    页里、grep 得到（spec §2.2b）。本函数不设它，因为它是连接级设置、该由 purge 那条路径统一设。

    **`chat_md5` 不收参数，和 `chat_row_counts` 一样就地派生**（`messages.chat_md5(chat_id)`）：
    它是三处宽谓词的前缀来源，一旦由调用方传，就可能与本模块核查用的派生值不一致——那时
    「按传入值删 parts/refs、按派生值核查」会**过删别的会话的行**（不可逆，且此前没有断言护着）。
    一个值的唯一来源，调用方就不可能让它与核查不一致。

    顺序上只剩一条硬约束：
    - **墓碑必须先落**（由调用方在本函数之前写，同一事务）：先删后挡的话，崩在中间就是
      「删了但没挡」，那批东西会复活；先挡后删最坏只是「挡住了但没删掉」，核查看得见、重跑能收口。

    另**九**处**按宽谓词删**（`vec_messages` 用 `acct_chat` 分区键，其余八张用 `msg_key` 前缀：
    见 `_MSG_KEY_TABLES`），不是经 `messages` 子查询删——所以它们与 `messages` 的先后不再有约束，
    而且消息行早就不在了的**孤儿行照样盖得住**（子查询版会漏掉孤儿，见 `chat_row_counts`）。
    """
    before = chat_row_counts(con, account, chat_id)
    # `ident` 是全部派生值的唯一来源：`md5` 不再单独算一遍（此前这里算两次）。
    ident = messages.ChatIdentity.of(account, chat_id)
    md5 = ident.md5
    acct_chat = messages.acct_chat(account, md5)
    p = messages.msg_key_prefix(account, md5)
    if has_table(con, "vec_messages"):                 # 轻装安装上不存在——见 has_table
        # 按 **`acct_chat` 分区键**删，而不是按 `msg_rowid IN (SELECT id FROM messages …)`：
        # 后者会漏掉孤儿向量（消息行已经不在了的那些），而孤儿正是隐私删除最该清的东西。
        # 实测（sqlite-vec 0.1.9，两个分区各 100 行）：`DELETE … WHERE acct_chat=?` 成立，
        # 200→100、被删分区残留 0、另一分区完整；`SELECT count(*) … WHERE acct_chat=?` 同样成立。
        _vec_exec(con, "DELETE FROM vec_messages WHERE acct_chat=?", (acct_chat,))
    # `msg_parts` / `media_refs` 按 **`msg_key` 前缀**删，同样为了盖住孤儿行（见 chat_row_counts）。
    # 不能用 LIKE：account 里含 `_` 与 `%`，两个都是通配符。
    con.execute("DELETE FROM msg_parts WHERE substr(msg_key, 1, ?) = ?", (len(p), p))
    con.execute("DELETE FROM media_refs WHERE substr(msg_key, 1, ?) = ?", (len(p), p))
    con.execute("DELETE FROM msg_extras WHERE substr(msg_key, 1, ?) = ?", (len(p), p))
    # Phase 06：派生文本、它的删除墓碑、以及事实记忆的证据/主张。派生 FTS 由触发器跟随。
    con.execute("DELETE FROM derived_text WHERE substr(msg_key, 1, ?) = ?", (len(p), p))
    con.execute("DELETE FROM derived_tombstone WHERE substr(msg_key, 1, ?) = ?", (len(p), p))
    # 本会话主张的**全部证据**都要删：证据可能挂在别的会话的消息上（跨会话引用），
    # 只按 msg_key 前缀删会留下孤儿证据，而主张一删就再也没人能定位它们。
    con.execute("DELETE FROM fact_evidence WHERE claim_id IN"
                f" (SELECT claim_id FROM fact_claim WHERE account=? AND {ident.chat_id_where})",
                ident.chat_id_args)
    con.execute("DELETE FROM fact_evidence WHERE substr(msg_key, 1, ?) = ?", (len(p), p))
    con.execute(f"DELETE FROM fact_claim WHERE account=? AND {ident.chat_id_where}",
                ident.chat_id_args)
    # 证据被销毁后，**没有证据的主张不得继续成立**：标记为 retracted（保留审计痕迹）。
    con.execute(
        f"UPDATE fact_claim SET status='{CLAIM_RETRACTED}',"
        f" note=COALESCE(note,'{EVIDENCE_PURGED}'),"
        " updated_at=CAST(strftime('%s','now') AS INTEGER)"
        " WHERE account=? AND status!='retracted'"
        " AND claim_id NOT IN (SELECT claim_id FROM fact_evidence)", (account,))
    con.execute(f"DELETE FROM messages WHERE account=? AND {ident.messages_where}",
                ident.messages_args)  # FTS 跟随
    con.execute(f"DELETE FROM display_names WHERE account=? AND {ident.chat_id_where}",
                ident.chat_id_args)
    # 重嵌队列与失败账本：它们是**工作清单**，不是用户数据。删掉与删除同一事务——
    # 留着会让 Phase 03 的消费者对着已销毁的 msg_key 空转（消费侧也有排除判据兜底）。
    con.execute("DELETE FROM pending_reembed WHERE substr(msg_key, 1, ?) = ?", (len(p), p))
    con.execute("DELETE FROM reembed_failures WHERE substr(msg_key, 1, ?) = ?", (len(p), p))
    # 向量水位线：min 而非 max。**不要复用 vector._set_vector_watermark**——它的 upsert 写的是
    # max(...)（单调不减），这一行 min 会被它当场吃掉、静默无效（spec §5.2 第 1 条）。
    # 取**该账号自己的** max 即可（与那条 WHERE account=? 天然配对）。
    # 早先这里写的是「max 必须全局」——2026-09-30 终审推翻了它：扫描谓词 `WHERE account=? AND
    # id>?` 是按账号过滤的，降到该账号自己的新 max 时区间里一条存活行都没有，那个危害不存在
    # （`vector._SELECT_PAGE` 走 rowid 区间扫描，降低下界只是多跳过几行别的账号的）。
    # 这几行必须留在删除的**同一事务**里：先提交删除、再单独修水位线，中间崩一次就是
    # 「行没了、水位线还停在旧高位」，此后复用的 id ≤ 水位线 → 静默不被嵌入
    # （没有任何地方会报错，只有向量检索悄悄少结果）。
    if has_table(con, "vector_watermark"):
        con.execute(
            "UPDATE vector_watermark SET last_rowid = min(last_rowid, "
            "  (SELECT COALESCE(max(id), 0) FROM messages WHERE account=?)) "
            "WHERE account=?", (account, account))
    # `embedded`（定义是「索引内当前行数」，见 VECTOR_SCHEMA 的 DDL 注释）**按实际重算**，
    # 不做减法。减法有两处错：
    # ① `vector_meta` 是**每模型一行**，减法要么漏 `WHERE model_id`（把所有行都减了），
    #    要么得挑一行——而「该挑哪一行」在 store 层无法知道：`MODEL_ID` 是 vector.py 的常量，
    #    `_foreign_model` 的 docstring 明说**刻意不看「最新一行」**（`built_at` 秒级分辨率下
    #    取哪行是碰运气）。
    # ② 减数是删除前的本会话分区行数（**含孤儿**），而孤儿从来没被计进 `embedded`。
    # 重算不需要知道是哪一行：`vec_messages` **没有 `model_id` 列**（它是纯 vec0 表），换模型
    # 走 `VECTOR_DROP` 整表重建（`vector_meta` 一起 DROP），所以表里的行恒属于唯一模型，
    # `count(*)` 就是这个定义的直接实现，还顺带自愈历史漂移（例如本次删除前的值就是漂移的）。
    # 代价：一次 O(表行数) 的 count 取代原来的 O(1) 减法，每次 `purge --chat` 一次。
    # **这条 UPDATE 不能删掉**：`embedded == 0` 是空索引判据（`vector.semantic_search` 读 `vector_meta.embedded`）。
    # 守卫要两张表都在——重算的来源是 `vec_messages`（`vector_meta` 存在而它不存在时无从重算，
    # 那种状态只在手工建表的测试夹具里出现）。
    if has_table(con, "vec_messages") and has_table(con, "vector_meta"):
        _vec_exec(con, "UPDATE vector_meta SET embedded = (SELECT count(*) FROM vec_messages)")
    return before


def chat_md5_in_index(con: sqlite3.Connection, account: str) -> set[str]:
    """该账号在索引里**仍有存活消息**的 chat_md5 集合。

    一次扫描（不是每个会话一次 NOT EXISTS）：`messages` 上没有 chat_md5 的索引，
    逐个查会退化成 N 次全表扫，而这里是 O(一次扫)。
    """
    return {r[0] for r in con.execute(
        "SELECT DISTINCT chat_md5 FROM messages WHERE account=?", (account,))}


def chats_in_index(con: sqlite3.Connection, account: str, *, query: str = "",
                   limit: int = 50) -> list[dict]:
    """索引里出现过的会话（有界），供工作台的会话选择器使用。**不读微信库、不要密钥。**"""
    limit = max(1, min(int(limit), 100))
    needle = (query.strip().replace("\\", "\\\\")
              .replace("%", "\\%").replace("_", "\\_"))
    rows = con.execute(
        "SELECT m.chat_id AS chat_id,"
        " COALESCE(MAX(dn.display_name), m.chat_id) AS display,"
        " COUNT(*) AS messages, MAX(m.ts) AS last_ts"
        " FROM messages m LEFT JOIN display_names dn"
        "   ON dn.account = m.account AND dn.chat_id = m.chat_id"
        "  AND dn.member_key = m.chat_id"
        " WHERE m.account = ? AND (m.chat_id LIKE ? ESCAPE '\\'"
        "   OR COALESCE(dn.display_name, '') LIKE ? ESCAPE '\\')"
        " GROUP BY m.chat_id ORDER BY last_ts DESC LIMIT ?",
        (account, f"%{needle}%", f"%{needle}%", limit)).fetchall()
    return [dict(r) for r in rows]


def message_page(con: sqlite3.Connection, account: str, chat_id: str, *,
                 before_ts: int | None = None, before_local_id: int | None = None,
                 limit: int = 50) -> dict:
    """按 `(ts, local_id)` 倒序的**有界分页**；`next_cursor` 为 None 表示到底了。

    不读微信库：工作台时间线只回索引里的行（purge 过的会话这里自然为空）。
    """
    limit = max(1, min(int(limit), 100))
    where = ["m.account = ?", "m.chat_id = ?"]
    args: list[object] = [account, chat_id]
    if before_ts is not None and before_local_id is not None:
        where.append("(m.ts < ? OR (m.ts = ? AND m.local_id < ?))")
        args.extend([int(before_ts), int(before_ts), int(before_local_id)])
    args.append(limit + 1)            # 多取一条判断是否还有下一页
    rows = con.execute(
        "SELECT m.msg_key, m.chat_id, m.ts, m.local_id, m.is_self, m.content,"
        " m.media_hint, m.msg_type, " + _SENDER_EXPR + " AS sender"
        " FROM messages m " + _SENDER_JOIN +
        " WHERE " + " AND ".join(where) +
        " ORDER BY m.ts DESC, m.local_id DESC LIMIT ?", args).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    cursor = None
    if more and rows:
        cursor = {"ts": rows[-1]["ts"], "local_id": rows[-1]["local_id"]}
    return {"messages": [dict(r) for r in rows], "next_cursor": cursor}


def upsert_media_refs(con: sqlite3.Connection,
                      rows: list[tuple[str, str, str | None, str | None]]) -> int:
    """批量幂等写 media_refs。返回**被排除而跳过的**行数（`0` = 全写入）。
    不 commit：由 upsert_batch 的 commit（同事务）或 store.open() 收口。

    被排除的 `msg_key` 一条都不落库——`media rebuild-refs` 与 `export` 都是**全量重扫**
    微信库，不拦的话已删消息的媒体引用会重新长回来（spec §5.4 第 3 条），两个调用者
    共用本函数，所以拦在这里一处就够。

    判据与写入同事务，与 `upsert_batch` 同款：没开事务就先 `BEGIN IMMEDIATE`。
    **`store.open()` 的连接上，本函数是每轮重建的第一句写**——没有这句 BEGIN，判据的
    SELECT 会跑在自动提交里，读与写之间就有一个窗口：`purge --chat` 在窗口里提交墓碑并
    删完，本批随即把已删行的 refs 写回去，而此后**没有任何东西会清它**（永久复活）。
    有了 BEGIN IMMEDIATE，本连接先拿到写锁，那个写者进不来（实测：main 与 ATTACH 的
    exclusions 一起锁住，两边都报 `database is locked`）。

    **这句 guard 只认「自动提交」这一种安全形态**：连接若已经在事务里，它不会（也不能）
    再 BEGIN。事务**如果还没写过主库**——典型是只写过 TEMP 表——`con.in_transaction` 为真
    而主库写锁并未拿到，本函数读得到的仍是「随时可能被改」的状态。那种调用方（今天的
    `appmsg_rebuild_parts`）必须自己把它开成 `BEGIN IMMEDIATE`。

    仍**不 commit**（契约不变，提交由调用方收口）；`rows` 为空时提前返回，不开事务。
    """
    if not rows:
        return 0
    if not con.in_transaction:
        _begin_immediate(con)
    skip = _filter_excluded(con, [r[0] for r in rows])
    keep = [r for r in rows if r[0] not in skip]
    if keep:
        con.executemany(
            "INSERT INTO media_refs (msg_key, media_kind, file_key, xml_md5) VALUES (?,?,?,?) "
            "ON CONFLICT(msg_key) DO UPDATE SET media_kind=excluded.media_kind, "
            "file_key=excluded.file_key, xml_md5=excluded.xml_md5", keep)
    return len(rows) - len(keep)


def media_refs_by_keys(con: sqlite3.Connection,
                       keys: Sequence[str]) -> dict[str, tuple[str, str | None]]:
    """按 msg_key 批量取媒体引用：{msg_key: (media_kind, file_key)}。

    查不到的 key **不出现在结果里**——上层靠「在不在字典里」区分「这条消息没有媒体引用」
    与「有引用但没查到」。file_key 可以是 None（packed 与 xml md5 都没命中），照实带出。
    """
    if not keys:
        return {}
    out: dict[str, tuple[str, str | None]] = {}
    batch = KEYS_BATCH
    uniq = list(dict.fromkeys(keys))
    for i in range(0, len(uniq), batch):
        chunk = uniq[i:i + batch]
        sql = ("SELECT msg_key, media_kind, file_key FROM media_refs WHERE msg_key IN "
               f"({','.join('?' * len(chunk))})")
        for k, kind, fk in con.execute(sql, chunk):
            out[k] = (kind, fk)
    return out


def media_refs_by_chat(con: sqlite3.Connection, account: str,
                       chat_ids: Sequence[str], *, kind: str | None = None
                       ) -> dict[str, list[tuple[str, int, str]]]:
    """按会话批量取媒体引用：{chat_id: [(msg_key, ts, file_key), …]}（会话内按 ts、local_id 升序）。

    **`msg_key` 是第三样必需的**：贴纸走 CDN 取件时要回源到微信库，而定位一行所需的
    `shard` / `local_id` **只编码在 msg_key 里**（`account|chat_md5|shard|local_id|create_time`，
    见 `messages.row_key`）。视频用不到它，但数据源只有这一处，两个调用方共用。

    `media_refs` 只有 msg_key（没有 account / chat / ts 维度），所以必须 join `messages`
    取回会话与时间。**只出现在结果里的会话是有引用的那些**——调用方用 `.get(chat, ())` 取，
    据此区分「这个会话没有媒体消息」与「查不到」。file_key 为 NULL 的行跳过：没有文件名
    就无从定位本体，带出去只会让调用方逐条判空。

    **join 出来的「空」不等于「没有引用」**：`media rebuild-refs` 只写 refs、不写 messages，
    所以「有 refs 但消息没进索引」的索引同样在这里返回空。要分清这两种空，用不 join 的
    `count_media_refs`（它按 msg_key 前缀计数）。
    """
    if not chat_ids:
        return {}
    out: dict[str, list[tuple[str, int, str]]] = {}
    # 与 media_refs_by_keys 同款分批（批大小见 KEYS_BATCH 的注释）。按**会话**分批不会
    # 打乱每条会话内部的顺序：一个会话只落在一批里，各批的 ORDER BY 结果可以直接拼接。
    batch = KEYS_BATCH
    for i in range(0, len(chat_ids), batch):
        chunk = chat_ids[i:i + batch]
        sql = ("SELECT m.chat_id, m.msg_key, m.ts, r.file_key FROM media_refs r "
               "JOIN messages m ON m.msg_key = r.msg_key "
               f"WHERE m.account = ? AND m.chat_id IN ({','.join('?' * len(chunk))}) "
               "AND r.file_key IS NOT NULL")
        args: list[str] = [account, *chunk]
        if kind is not None:
            sql += " AND r.media_kind = ?"
            args.append(kind)
        for chat_id, msg_key, ts, file_key in con.execute(sql + " ORDER BY m.ts, m.local_id", args):
            out.setdefault(chat_id, []).append((msg_key, ts, file_key))
    return out


def count_media_refs(con: sqlite3.Connection, account: str, *,
                     chat_ids: Sequence[str] | None = None,
                     kind: str | None = None) -> int:
    """media_refs 里该账号（或其中指定会话）的引用条数。**不 join `messages`。**

    为什么必须与 join 无关：`media rebuild-refs` **只写 refs、不写 messages**（它自己不依赖
    messages），所以「账号里一条 refs 都没有」与「refs 在、但那些会话的消息没进索引」是两种
    不同的病，join 出来的 0 把它们混成一种，还会把用户导向错误的修法（重建索引写不出消息）。
    按 msg_key 前缀计数就分得开：`msg_key = account|chat_md5|shard|local_id|create_time`，而 wxid 不可能
    含 `|`，所以账号前缀与 `account|chat_md5|` 会话前缀都不会串到别的账号或会话
    （也**不能**用 LIKE——account 里的 `_`、`%` 是通配符）。
    """
    args: list[str] = []
    # 前缀比对而非 LIKE：substr 是逐字符的，account 里的 `_`/`%` 不会被当通配符。
    where = ["substr(msg_key, 1, ?) = ?"]
    acct_prefix = f"{account}|"
    args += [len(acct_prefix), acct_prefix]
    if chat_ids is not None:
        # EA-011：用 `chat_md5()` 而不是 `md5_of()`——后者对退化形态（`md5:<hex>`）会
        # **再哈希一次**，与 `msg_key` 第二段（本就是 chat_md5）分家，计数恒为 0。
        pats = [f"{account}|{messages.chat_md5(c)}|" for c in dict.fromkeys(chat_ids)]
        if not pats:                            # 空集：一条都不算（IN () / OR 空集是语法错）
            return 0
        where.append("(" + " OR ".join("substr(msg_key, 1, ?) = ?" for _ in pats) + ")")
        for p in pats:
            args += [len(p), p]
    if kind is not None:
        where.append("media_kind = ?")
        args.append(kind)
    return con.execute("SELECT count(*) FROM media_refs WHERE " + " AND ".join(where),
                       args).fetchone()[0]


# ---------- appmsg 原始载荷（msg_parts） ----------
# `raw_xml` 是 appmsg 正文的**权威**；appmsg_type / ref_svrid / ref_msg_key 是它的**投影**，
# 存在的唯一理由是「不必解压就能筛选与重解析」。与「content 是消息的检索投影」同构。


def upsert_extras(con: sqlite3.Connection,
                  rows: list[tuple[str, int, bytes]]) -> int:
    """批量幂等写 msg_extras。返回**被排除而跳过的**行数（`0` = 全写入）。
    不 commit：与 `upsert_msg_parts` / `upsert_media_refs` 同约定，由调用方收口。

    被排除的 `msg_key` 一条都不落库——回填命令直接回微信库重扫，不拦的话 `purge --chat`
    删掉的位置/名片载荷会重新长回来（spec §5.4 第 2 条，与 `upsert_msg_parts` 同一理由）。

    调用方（`usecases.msgextra_rebuild`）**不开事务**：它没有 TEMP 表的缓冲，
    所以这里那句 guard 真的会触发（`upsert_msg_parts` 的 docstring 记着它那边的例外形态）。
    """
    if not rows:
        return 0
    if not con.in_transaction:
        _begin_immediate(con)
    skip = _filter_excluded(con, [r[0] for r in rows])
    keep = [r for r in rows if r[0] not in skip]
    if keep:
        con.executemany(
            "INSERT INTO msg_extras (msg_key, msg_type, payload) VALUES (?,?,?) "
            "ON CONFLICT(msg_key) DO UPDATE SET msg_type=excluded.msg_type, "
            "payload=excluded.payload", keep)
    return len(rows) - len(keep)


def extras_by_keys(con: sqlite3.Connection,
                   keys: Sequence[str]) -> dict[str, tuple[int, bytes]]:
    """按 `msg_key` 取 48/42 的原始载荷：`{msg_key: (msg_type, payload)}`。

    查不到的 key **不出现在结果里**（与 `msg_parts_by_keys` 同约定）——上层靠「在不在」
    区分「这条没有结构化载荷」与「解析成空」。
    """
    if not keys:
        return {}
    out: dict[str, tuple[int, bytes]] = {}
    for i in range(0, len(keys), KEYS_BATCH):
        chunk = keys[i:i + KEYS_BATCH]
        sql = ("SELECT msg_key, msg_type, payload FROM msg_extras WHERE msg_key IN ("
               + ",".join("?" * len(chunk)) + ")")
        out.update({k: (t, p_) for k, t, p_ in con.execute(sql, chunk)})
    return out


def upsert_msg_parts(con: sqlite3.Connection,
                     rows: list[tuple[str, int, int | None, str | None, bytes]]) -> int:
    """批量幂等写 msg_parts。返回**被排除而跳过的**行数（`0` = 全写入）。
    不 commit：与 upsert_media_refs 同约定，由调用方收口。

    被排除的 `msg_key` 一条都不落库——`appmsg rebuild` 直接回微信库重扫，不拦的话
    被删消息的 appmsg 原始载荷（含完整正文）会重新长回来（spec §5.4 第 2 条）。

    判据与写入同事务，与 `upsert_batch` 同款（理由与窗口形状见 `upsert_media_refs`：
    `appmsg rebuild` 的第一批同样是一次运行里与本函数有关的唯一暴露点）。
    **但本函数唯一的调用方 `appmsg_rebuild_parts` 恰是「已开 deferred 事务」的形态**
    （它先写 TEMP 表 sid_map），那句 guard 在那里不触发——所以它在入口自己
    `BEGIN IMMEDIATE`；这个洞在调用方堵，见 `usecases.appmsg_rebuild_parts`。
    仍**不 commit**（契约不变，提交由调用方收口）；`rows` 为空时提前返回，不开事务。
    """
    if not rows:
        return 0
    if not con.in_transaction:
        _begin_immediate(con)
    skip = _filter_excluded(con, [r[0] for r in rows])
    keep = [r for r in rows if r[0] not in skip]
    if keep:
        con.executemany(
            "INSERT INTO msg_parts (msg_key, appmsg_type, ref_svrid, ref_msg_key, raw_xml) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(msg_key) DO UPDATE SET appmsg_type=excluded.appmsg_type, "
            "ref_svrid=excluded.ref_svrid, ref_msg_key=excluded.ref_msg_key, "
            "raw_xml=excluded.raw_xml", keep)
    return len(rows) - len(keep)


def msg_parts_by_keys(con: sqlite3.Connection, keys: Sequence[str]
                      ) -> dict[str, tuple[int, int | None, str | None, bytes]]:
    """按 msg_key 批量取 appmsg 部件：{msg_key: (appmsg_type, ref_svrid, ref_msg_key, raw_xml)}。

    查不到的 key **不出现在结果里**（与 media_refs_by_keys 同约定）——上层靠「在不在
    字典里」区分「这条消息没有 appmsg」与「有 appmsg 但没查到」。ref_svrid / ref_msg_key
    可以是 None（非引用类，或引用目标解不到），照实带出。
    """
    if not keys:
        return {}
    out: dict[str, tuple[int, int | None, str | None, bytes]] = {}
    batch = KEYS_BATCH
    uniq = list(dict.fromkeys(keys))
    for i in range(0, len(uniq), batch):
        chunk = uniq[i:i + batch]
        sql = ("SELECT msg_key, appmsg_type, ref_svrid, ref_msg_key, raw_xml FROM msg_parts "
               f"WHERE msg_key IN ({','.join('?' * len(chunk))})")
        for k, kind, svrid, ref, blob in con.execute(sql, chunk):
            out[k] = (kind, svrid, ref, blob)
    return out


def count_msg_parts(con: sqlite3.Connection, account: str) -> int:
    """msg_parts 里该账号的条目数。**不 join `messages`。**

    为什么必须与 join 无关：`appmsg rebuild` **只写 msg_parts、不写 messages**，所以
    「账号里一条都没有」与「有、但那些消息没进索引」是两种不同的病，join 出来的 0 把
    它们混成一种。空表返回 0——上层靠这个 0 判「未回填」并降级。

    **前缀比对而非 LIKE**：`msg_key = account|chat_md5|shard|local_id|create_time`，而 account（wxid）
    里**含下划线**，LIKE 的 `_` 是单字符通配符，会把 `wxid_aXb` 的条目算进 `wxid_a_b`；
    substr 逐字符比对，没有这个问题。
    """
    prefix = f"{account}|"
    return con.execute("SELECT count(*) FROM msg_parts WHERE substr(msg_key, 1, ?) = ?",
                       (len(prefix), prefix)).fetchone()[0]


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


def load_export_anchors(con: sqlite3.Connection,
                        account: str) -> dict[tuple[str, str], int | None]:
    """{(shard, chat_md5): 水位线那一行的 create_time}（v10 之前留下、回填不到的为 None）。"""
    return {(shard, md5): a for shard, md5, a in con.execute(
        "SELECT shard, chat_md5, anchor_ts FROM watermark WHERE account=?", (account,))}


def reset_export_watermark(con: sqlite3.Connection, account: str, shard: str,
                           chat_md5: str) -> None:
    """单个会话表的水位线归零（reader 发现它被重建过）。立即提交：之后的批次从空水位线写起；
    崩在中间最坏只是下一轮再从 0 扫一遍（键含 create_time，重扫幂等）。"""
    try:
        con.execute("DELETE FROM watermark WHERE account=? AND shard=? AND chat_md5=?",
                    (account, shard, chat_md5))
        con.commit()
    except (sqlite3.Error, OSError) as exc:
        con.rollback()
        raise index_unavailable(exc) from exc


def resolve_msg_key(con: sqlite3.Connection, account: str, msg_key: str) -> str:
    """调用方给的键 → 库里的键。5 段原样返回；MCP 输出的短键（`messages.short_key`）换回完整键；
    v10 之前的 4 段旧键按前缀找：恰好一条就换成
    它的新键，一条都没有原样返回（由调用方走「找不到」），多条（会话表被重建过、两代消息
    同 local_id）报 `SearchInputError`——不猜。"""
    if (short := messages.split_short_key(msg_key)) is not None:
        return _resolve_short_key(con, account, msg_key, *short)
    kp = messages.split_key(msg_key)
    if kp is None or kp.create_time is not None or kp.account != account:
        return msg_key
    p = msg_key + "|"                 # 走 msg_key 唯一索引的范围扫描：'|' 之后是 '}'
    rows = con.execute("SELECT msg_key FROM messages WHERE msg_key > ? AND msg_key < ? LIMIT 2",
                       (p, msg_key + "}")).fetchall()
    if len(rows) > 1:
        raise errors.SearchInputError(
            f"旧格式 msg_key 对应多条消息：{msg_key}",
            hint="该会话的微信消息表被重建过；请重新检索，使用结果里的新 msg_key")
    return rows[0][0] if rows else msg_key


# 短键在 messages 里找不到时（消息还没导出、或已被 purge 删掉）再去这几张表找：
# appmsg 回填扫的是微信库，`msg_parts` 里可以有索引里还没有的消息；墓碑要认得出才能答「已销毁」。
_SHORT_KEY_FALLBACK_TABLES = ("msg_parts", "msg_extras", "exclusions.excluded")


def _resolve_short_key(con: sqlite3.Connection, account: str, key: str,
                       md5_prefix: str, local_id: int, create_time: int) -> str:
    """短键 → 完整键。先按 `(account, ts)` 走 idx_msg_ts 查 messages，查不到再按
    `account|<前缀>` 的 msg_key 区间扫旁表；候选都用 `split_key` 逐段核对。

    恰好一条返回它；没有原样返回（调用方走「找不到」）；多条报 `SearchInputError`——不猜。
    """
    def match(k: str) -> bool:
        kp = messages.split_key(k)
        return (kp is not None and kp.account == account and kp.local_id == local_id
                and kp.create_time == create_time and kp.chat_md5.startswith(md5_prefix))

    # create_time 为 0 = 源表里是 NULL（`row_key` 记 0），messages.ts 那时也是 NULL
    ts_sql = "ts=?" if create_time else "(ts IS NULL OR ts=?)"
    rows = con.execute(f"SELECT msg_key FROM messages WHERE account=? AND {ts_sql} AND local_id=?",
                       (account, create_time, local_id)).fetchall()
    found = {r[0] for r in rows if match(r[0])}
    if not found:
        lo = f"{account}|{md5_prefix}"
        for table in _SHORT_KEY_FALLBACK_TABLES:
            try:
                rows = con.execute(f"SELECT msg_key FROM {table} WHERE msg_key >= ? AND msg_key < ?",
                                   (lo, lo + "~")).fetchall()
            except sqlite3.OperationalError:       # 旁表不在（如 exclusions.db 没挂上）
                continue
            found |= {r[0] for r in rows if match(r[0])}
    if len(found) > 1:
        raise errors.SearchInputError(
            f"短 msg_key 对应多条消息：{key}",
            hint="请改用完整 msg_key（CLI 输出里是完整形态）")
    return found.pop() if found else key


def resolve_msg_keys(con: sqlite3.Connection, account: str, keys: Iterable[str]) -> list[str]:
    """批量版 `resolve_msg_key`：逐个换成库里的键，再去重保序（调用方给的键可能重复）。"""
    return list(dict.fromkeys(resolve_msg_key(con, account, k) for k in keys))


def clear_export_watermarks(con: sqlite3.Connection, account: str) -> None:
    """重置指定账号的导出水位线（全量导出用）。只删该 account，别的账号不受影响。"""
    con.execute("DELETE FROM watermark WHERE account = ?", (account,))


# ---------- 历史回看游标（Phase 02） ----------

# 每个 (分片, 会话) 每轮回看的行数上限。它与 `BATCH` 分工不同：BATCH 是「一次 yield 多少
# 行」，这里是「一轮总共回看多少行」——后者决定增量导出的额外成本上限。
# 772 个会话 × 100 行 ≈ 7.7 万行/轮（全量 84 万行的 9%），完整覆盖一轮约 11 次导出。
AUDIT_ROWS = 100


def load_audit_cursors(con: sqlite3.Connection, account: str) -> dict[tuple[str, str], int]:
    """该账号的回看游标：`{(shard, chat_md5): audited_local_id}`。

    与导出水位线**分表分键**：水位线问「新消息扫到哪」，游标问「旧消息回看到哪」。
    两者互不推进——把游标塞进 `watermark` 会让 `local_id > max` 的扫描直接跳过历史。
    """
    return {(shard, md5): int(lid) for shard, md5, lid in con.execute(
        "SELECT shard, chat_md5, audited_local_id FROM audit_cursor WHERE account=?",
        (account,))}


def save_audit_cursors(con: sqlite3.Connection, account: str,
                       marks: Mapping[tuple[str, str], int]) -> None:
    """推进回看游标并提交。

    **必须在数据 upsert 提交之后调用**：游标先走、数据没落，崩在中间就是「这段历史再也
    不会被回看」。反过来（数据已落、游标没走）最坏只是下一轮重看同一段——upsert 幂等，
    没有代价。所以这一步单独提交，不跟 upsert 抢同一个事务。
    """
    if not marks:
        return
    now = int(time.time())
    try:
        con.executemany(
            "INSERT INTO audit_cursor(account, shard, chat_md5, audited_local_id, updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(account, shard, chat_md5) DO UPDATE SET "
            "audited_local_id=excluded.audited_local_id, updated_at=excluded.updated_at",
            [(account, s, m, lid, now) for (s, m), lid in marks.items()])
        con.commit()
    except (sqlite3.Error, OSError) as exc:
        raise index_unavailable(exc) from exc


# ---------- 重嵌队列（Phase 02 写入 / Phase 03 消费） ----------

def enqueue_reembed(con: sqlite3.Connection, msg_key: str,
                    reason: str = "cascade") -> None:
    """把一条消息放进重嵌队列（幂等）。**不 commit**：与调用方的向量写入同事务。

    队列是**集合**不是日志：同一条消息改十次也只是一行，`changed_at` 取最后一次。
    消费时读的是 `messages` 的**当前**内容，所以中间版本不需要保留。
    """
    # 已在队列里的行**不被 `prefix` 降级**：它原来的原因（正文变了）决定消费时要不要级联到
    # 后继（`vector._NO_CASCADE_REASON`）；降成 `prefix` 就再也不级联，后继的前缀永远过时。
    con.execute(
        "INSERT INTO pending_reembed(msg_key, changed_at, reason) VALUES (?, ?, ?) "
        "ON CONFLICT(msg_key) DO UPDATE SET changed_at=excluded.changed_at, "
        "reason=CASE WHEN excluded.reason='prefix' THEN pending_reembed.reason "
        "ELSE excluded.reason END",
        (msg_key, int(time.time()), reason))


def dequeue_reembed(con: sqlite3.Connection, msg_key: str) -> None:
    """消费完成（或判定无需向量）：删队列行与失败账本。不 commit。"""
    con.execute("DELETE FROM pending_reembed WHERE msg_key=?", (msg_key,))
    con.execute("DELETE FROM reembed_failures WHERE msg_key=?", (msg_key,))


def record_reembed_failure(con: sqlite3.Connection, msg_key: str,
                           error: str) -> int:
    """记一次失败：尝试次数 +1，保存最近原因。返回新的次数。不 commit。"""
    con.execute(
        "INSERT INTO reembed_failures(msg_key, attempts, last_error, last_attempt_at) "
        "VALUES (?, 1, ?, ?) ON CONFLICT(msg_key) DO UPDATE SET "
        "attempts=attempts + 1, last_error=excluded.last_error, "
        "last_attempt_at=excluded.last_attempt_at",
        (msg_key, error, int(time.time())))
    return int(con.execute("SELECT attempts FROM reembed_failures WHERE msg_key=?",
                           (msg_key,)).fetchone()[0])


def queued_reembed(con: sqlite3.Connection, account: str | None = None,
                   limit: int = 256) -> list[str]:
    """取一批待重嵌的 `msg_key`（按 `changed_at`，再按 key 稳定排序）。

    `account` 给定时按 `msg_key` 前缀筛（`msg_key` 第一段就是账号，wxid 里不可能有 `|`）。
    队列是小表：它的规模由「改写了多少条」决定，不是全量消息数。
    """
    if account is None:
        rows = con.execute(
            "SELECT msg_key FROM pending_reembed ORDER BY changed_at, msg_key LIMIT ?",
            (int(limit),)).fetchall()
    else:
        p = f"{account}|"
        rows = con.execute(
            "SELECT msg_key FROM pending_reembed WHERE substr(msg_key, 1, ?) = ? "
            "ORDER BY changed_at, msg_key LIMIT ?", (len(p), p, int(limit))).fetchall()
    return [r[0] for r in rows]


def reembed_queue_stats(con: sqlite3.Connection, account: str | None = None) -> dict:
    """队列深度与失败分布（新鲜度诊断用）。表不存在时返回零（轻装安装）。"""
    if not has_table(con, "pending_reembed"):
        return {"pending": 0, "failed": 0, "max_attempts": 0}
    if account is None:
        pending = con.execute("SELECT count(*) FROM pending_reembed").fetchone()[0]
        fails = con.execute(
            "SELECT count(*), COALESCE(max(attempts), 0) FROM reembed_failures").fetchone()
    else:
        p = f"{account}|"
        pending = con.execute(
            "SELECT count(*) FROM pending_reembed WHERE substr(msg_key, 1, ?) = ?",
            (len(p), p)).fetchone()[0]
        fails = con.execute(
            "SELECT count(*), COALESCE(max(attempts), 0) FROM reembed_failures "
            "WHERE substr(msg_key, 1, ?) = ?", (len(p), p)).fetchone()
    by_reason: dict[str, int] = {}
    if pending:
        sql = "SELECT reason, count(*) FROM pending_reembed"
        args: tuple = ()
        if account is not None:
            p = f"{account}|"
            sql += " WHERE substr(msg_key, 1, ?) = ?"
            args = (len(p), p)
        by_reason = dict(con.execute(sql + " GROUP BY reason", args).fetchall())
    return {"pending": pending, "failed": fails[0], "max_attempts": fails[1],
            "by_reason": by_reason}


# ---------- 同步终态（Phase 04） ----------

def record_sync_state(con: sqlite3.Connection, account: str, *,
                      started_at: int, finished_at: int, status: str,
                      error: str | None, stats: str) -> None:
    """写一次同步的终态（幂等 upsert）。不 commit（调用方收口）。

    `stats` 是**已序列化的计数摘要**（JSON 文本）；调用方负责保证里面没有正文、
    密钥或媒体路径——本模块不解析它。
    """
    con.execute(
        "INSERT INTO sync_state(account, last_started_at, last_finished_at,"
        " last_status, last_error, stats_json) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(account) DO UPDATE SET "
        "last_started_at=excluded.last_started_at, "
        "last_finished_at=excluded.last_finished_at, "
        "last_status=excluded.last_status, last_error=excluded.last_error, "
        "stats_json=excluded.stats_json",
        (account, started_at, finished_at, status, error, stats))


def load_sync_state(con: sqlite3.Connection, account: str) -> dict | None:
    row = con.execute(
        "SELECT account, last_started_at, last_finished_at, last_status,"
        " last_error, stats_json FROM sync_state WHERE account=?", (account,)).fetchone()
    if row is None:
        return None
    return {"account": row[0], "started_at": row[1], "finished_at": row[2],
            "status": row[3], "error": row[4], "stats": row[5]}


def clear_audit_cursors(con: sqlite3.Connection, account: str) -> None:
    """重置该账号的回看游标（`--full` 用：全量重扫等于把整段历史都看过了）。"""
    con.execute("DELETE FROM audit_cursor WHERE account = ?", (account,))



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


def roster_skip_chat_md5(con: sqlite3.Connection, account: str) -> set[str]:
    """名册重建时要跳过的会话：**被排除过 且 索引里已无存活消息**。

    第二个条件是必须的——`_SENDER_EXPR` 是 COALESCE(dn.display_name, m.sender_name,
    m.sender_id)，而 `sender_name` 只在群聊有值（DDL 注释：「群聊时从 content 前缀解析」），
    **私聊的 `sender_name` 是 NULL**。只按第一个条件拦，被删的私聊将来收到的新消息会显示成
    裸 wxid，直到 --undo——那等于把「只清当时已有的」偷偷扩成「把这个会话弄瘸」。
    加上第二个条件之后它自愈：一旦有新消息入库，名册就照常重建这个名字。

    **调用点必须在 `replace_roster` 的 DELETE 之后**（那里才拿得到写锁），理由见那里的注释。
    """
    excluded = {r[0] for r in con.execute(
        "SELECT DISTINCT chat_md5 FROM exclusions.excluded WHERE account=?", (account,))}
    if not excluded:
        return set()
    return excluded - chat_md5_in_index(con, account)


def replace_roster(con: sqlite3.Connection, account: str,
                   rows: list[tuple[str, str, str]]) -> None:
    """整体替换该账号的名册，并更新 refreshed_at。

    **一个事务内 DELETE + INSERT**：中途失败若留下半份名册，那半份之外的人会突然
    显示成 wxid——比不刷新更糟（一半有名一半没名，用户无从判断哪个可信）。
    本函数不 commit：由调用方（`store.open()` 或 usecases）决定事务边界。

    **跳过判据的读必须在这句 DELETE 之后**（不是风格问题，是承重的，见下面那句注释）。
    """
    con.execute("DELETE FROM display_names WHERE account=?", (account,))
    # 这一句**必须**留在 DELETE 之后：DELETE 是写语句，它当场把事务开起来并拿到主库的
    # WAL 写锁——**没有匹配行时也拿**（实测：两种情形下 `in_transaction` 都是 True、
    # 第二条连接写同一库都报 `database is locked`），其后的两次读与 INSERT 就全在锁内。
    # 挪到 DELETE 之前，两次读落在自动提交里，与写之间就有窗口：`purge --chat` 在窗口里
    # 提交墓碑并删完，本函数随即把那个会话的显示名原样写回去——purge 在 display_names 上
    # 的那一处删除被撤销，此后没有任何东西会再清它。
    # 判据**不是** `con.in_transaction`：TEMP 表的 INSERT 也会开一个 deferred 事务而主库
    # 写锁并未拿到（Task 4 已实测），那时 `in_transaction` 为真却仍在窗口里。
    skip = roster_skip_chat_md5(con, account)
    rows = [r for r in rows if messages.chat_md5(r[0]) not in skip]
    con.executemany(
        "INSERT OR REPLACE INTO display_names (account, chat_id, member_key, display_name) "
        "VALUES (?,?,?,?)",
        [(account, chat_id, key, display) for chat_id, key, display in rows])
    con.execute(
        "INSERT INTO roster_meta (account, refreshed_at) VALUES (?,?) "
        "ON CONFLICT(account) DO UPDATE SET refreshed_at=excluded.refreshed_at",
        (account, int(time.time())))
