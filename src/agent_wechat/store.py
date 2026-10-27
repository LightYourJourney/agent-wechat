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
import re
import sqlite3
import time
from collections.abc import Iterator, Mapping, Sequence

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

_SCHEMA_BODY = """
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
CREATE TABLE IF NOT EXISTS audit_cursor (
    account         TEXT NOT NULL,
    shard           TEXT NOT NULL,
    chat_md5        TEXT NOT NULL,
    audited_local_id INTEGER NOT NULL DEFAULT 0,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (account, shard, chat_md5)
) WITHOUT ROWID;

-- Phase 02：内容被改写、等待重嵌的消息。由 `msg_reembed_au` 触发器维护——**与更新同一
-- 事务**写入，所以「SQLite 已改、FTS 已跟随（既有触发器）、重嵌标记已落」三者不会分叉。
-- Phase 03 消费它；本阶段只保证标记正确产生。
CREATE TABLE IF NOT EXISTS pending_reembed (
    msg_key   TEXT PRIMARY KEY,
    changed_at INTEGER NOT NULL
) WITHOUT ROWID;
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

# Phase 02：内容/会话名被改写时，**在更新它的同一事务里**投递一条重嵌标记。
# 不在 Python 里比对前后值：upsert 的 WHERE 已经保证「值没变就不更新」，触发器只需要
# 在真的更新发生时记一笔——这样标记、消息行与 FTS（既有三条触发器）三者天然同事务。
REEMBED_TRIGGER = """CREATE TRIGGER IF NOT EXISTS msg_reembed_au
AFTER UPDATE OF content, chat_id ON messages
WHEN old.content IS NOT new.content OR old.chat_id IS NOT new.chat_id
BEGIN
    INSERT INTO pending_reembed(msg_key, changed_at)
    VALUES (new.msg_key, CAST(strftime('%s','now') AS INTEGER))
    ON CONFLICT(msg_key) DO UPDATE SET changed_at=excluded.changed_at;
END;"""

SCHEMA = _SCHEMA_BODY + "\n" + "\n".join(FTS_TRIGGERS) + "\n" + REEMBED_TRIGGER

# 本程序已知的 main scope 最新版本。迁移只列**IF NOT EXISTS 表达不了**的变更；
# 基线（v1）由 `migrations.apply` 隐式 stamp——那代表「整套 DDL 已由 ensure 建好」。
SCHEMA_VERSION = 3

_AUDIT_TABLES = (
    "CREATE TABLE IF NOT EXISTS audit_cursor ("
    " account TEXT NOT NULL, shard TEXT NOT NULL, chat_md5 TEXT NOT NULL,"
    " audited_local_id INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL,"
    " PRIMARY KEY (account, shard, chat_md5)) WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS pending_reembed ("
    " msg_key TEXT PRIMARY KEY, changed_at INTEGER NOT NULL) WITHOUT ROWID",
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
        statements=_AUDIT_TABLES + ("DROP TRIGGER IF EXISTS msg_reembed_au", REEMBED_TRIGGER),
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
        # 的重建能力是既有行为，不能挪进「只跑一次」的迁移里。
        con.executescript(SCHEMA)
        con.executescript(exclusions.SCHEMA)
        # migrate：只做 ensure 表达不了的变更。两个 scope 各自事务、各自版本；
        # **不假设跨库原子**（index.db 与 exclusions.db 是两个文件，主库还是 WAL）。
        migrations.apply(con, scope=migrations.SCOPE_MAIN, migrations=MIGRATIONS)
        migrations.apply(con, scope=migrations.SCOPE_EXCLUSIONS, migrations=(),
                         table=migrations.EXCLUSIONS_TABLE)
        con.execute("PRAGMA synchronous=NORMAL")
    except errors.SchemaMigrationError:
        # 迁移失败/库太新：**原样抛出**（它的文案已带 scope/version 与可执行动作），
        # 不翻成 IndexUnavailable——「库坏了」与「库太新/迁移没跑完」是两种病。
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
    if isinstance(exc, sqlite3.OperationalError) and "locked" in msg:
        hint = "数据库被锁住——稍后重试，或确认没有其它导出 / 向量构建在跑"
    return errors.IndexUnavailable(f"本地检索库不可用：{errors.redact(msg)}", hint=hint)


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
    "exported_at=excluded.exported_at "
    # EA-012：守卫扩成「content **或 chat_id** 不同」。`chat_id` 是 `chat_md5` 的派生展示
    # 属性（退化形态 `md5:<hex>` 是真名的降级），名字解析回来后必须能自愈；只认 content
    # 时同一条消息会永远停在首次写入的 chat_id 上，同一 chat_md5 的行分裂成两种 id。
    "WHERE messages.content IS NOT excluded.content "
    "   OR messages.chat_id IS NOT excluded.chat_id")


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


def _filter_excluded(con: sqlite3.Connection, keys: Sequence[str]) -> set[str]:
    """本批里被排除的 msg_key。**必须在调用方的事务内查**，与随后的写同事务：
    读在提交前、写在提交后的话，那份陈旧结果会把被删的行原样写回去（spec §5.4）。

    走 `exclusions.excluded` 的主键，按 KEYS_BATCH 分批——与 `media_refs_by_keys` 同一写法。
    """
    if not keys:
        return set()
    uniq = list(dict.fromkeys(keys))
    out: set[str] = set()
    for i in range(0, len(uniq), KEYS_BATCH):
        chunk = uniq[i:i + KEYS_BATCH]
        sql = ("SELECT msg_key FROM exclusions.excluded "
               f"WHERE msg_key IN ({','.join('?' * len(chunk))})")
        out.update(r[0] for r in con.execute(sql, chunk))
    return out


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
        con.execute("BEGIN IMMEDIATE")
    now = int(time.time())
    skip = _filter_excluded(con, [r["msg_key"] for r in rows])
    keep = [r for r in rows if r["msg_key"] not in skip]
    if keep:
        con.executemany(_INSERT_SQL, [{**r, "exported_at": now} for r in keep])
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
    try:
        con.commit()
    except (sqlite3.Error, OSError) as exc:
        # R2-A-004：提交失败此前裸穿（实测 EIO）。这里与 `open()` 的出口同一口径。
        raise index_unavailable(exc) from exc
    return len(rows) - len(keep)


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    """该表在本连接上存不存在。**向量那三张表在轻装安装上根本不存在**——
    它们在 `VECTOR_SCHEMA` 里，而 `store.connect()` 只跑 `SCHEMA`（`store.py:83` 的注释就是
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
    （`errors.py:1`）。`_has_table` 只证明「表在」，不证明「这条连接能用它」——两个判据的差
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
    """该会话在五张表里的存活条数。核查与测试共用，避免各处各写一套判据。

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
    a = (account, chat_id)
    return {
        "messages": n("SELECT count(*) FROM messages WHERE account=? AND chat_id=?", a),
        "vectors": (_vec_exec(con, "SELECT count(*) FROM vec_messages WHERE acct_chat=?",
                              (acct_chat,)).fetchone()[0]
                    if _has_table(con, "vec_messages") else 0),
        "parts": n("SELECT count(*) FROM msg_parts WHERE substr(msg_key, 1, ?) = ?", (len(p), p)),
        "refs": n("SELECT count(*) FROM media_refs WHERE substr(msg_key, 1, ?) = ?", (len(p), p)),
        "extras": n("SELECT count(*) FROM msg_extras WHERE substr(msg_key, 1, ?) = ?", (len(p), p)),
        "display_names": n("SELECT count(*) FROM display_names WHERE account=? AND chat_id=?", a),
    }


def delete_chat(con: sqlite3.Connection, *, account: str, chat_id: str) -> dict[str, int]:
    """删掉一个会话在本工具里的全部副本。**八处，一处不漏**（spec §5.1；第八处是 EA-041 的 `msg_extras`——它与 `msg_parts` / `media_refs` 同属按 `msg_key` 前缀删的宽谓词那一类，孤儿行照样盖得住）。

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

    另**四**处**按宽谓词删**（`vec_messages` 用 `acct_chat` 分区键，`msg_parts`/`media_refs` 用
    `msg_key` 前缀），不是经 `messages` 子查询删——所以它们与 `messages` 的先后不再有约束，
    而且消息行早就不在了的**孤儿行照样盖得住**（子查询版会漏掉孤儿，见 `chat_row_counts`）。
    """
    before = chat_row_counts(con, account, chat_id)
    a = (account, chat_id)
    md5 = messages.chat_md5(chat_id)
    acct_chat = messages.acct_chat(account, md5)
    p = messages.msg_key_prefix(account, md5)
    if _has_table(con, "vec_messages"):                 # 轻装安装上不存在——见 _has_table
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
    con.execute("DELETE FROM messages WHERE account=? AND chat_id=?", a)   # FTS 由触发器跟随
    con.execute("DELETE FROM display_names WHERE account=? AND chat_id=?", a)
    # 向量水位线：min 而非 max。**不要复用 vector._set_vector_watermark**——它的 upsert 写的是
    # max(...)（单调不减），这一行 min 会被它当场吃掉、静默无效（spec §5.2 第 1 条）。
    # 取**该账号自己的** max 即可（与那条 WHERE account=? 天然配对）。
    # 早先这里写的是「max 必须全局」——2026-09-30 终审推翻了它：扫描谓词 `WHERE account=? AND
    # id>?` 是按账号过滤的，降到该账号自己的新 max 时区间里一条存活行都没有，那个危害不存在；
    # 而且实测 `_SELECT_ROWS` 的执行计划是 COVERING INDEX idx_msg_ts(account=?) + TEMP B-TREE
    # FOR ORDER BY——它压根不用 id>? 做范围，连性能差别都没有。
    # 这几行必须留在删除的**同一事务**里：先提交删除、再单独修水位线，中间崩一次就是
    # 「行没了、水位线还停在旧高位」，此后复用的 id ≤ 水位线 → 静默不被嵌入
    # （没有任何地方会报错，只有向量检索悄悄少结果）。
    if _has_table(con, "vector_watermark"):
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
    if _has_table(con, "vec_messages") and _has_table(con, "vector_meta"):
        _vec_exec(con, "UPDATE vector_meta SET embedded = (SELECT count(*) FROM vec_messages)")
    return before


def chat_md5_in_index(con: sqlite3.Connection, account: str) -> set[str]:
    """该账号在索引里**仍有存活消息**的 chat_md5 集合。

    一次扫描（不是每个会话一次 NOT EXISTS）：`messages` 上没有 chat_md5 的索引，
    逐个查会退化成 N 次全表扫，而这里是 O(一次扫)。
    """
    return {r[0] for r in con.execute(
        "SELECT DISTINCT chat_md5 FROM messages WHERE account=?", (account,))}


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
        con.execute("BEGIN IMMEDIATE")
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
    `shard` / `local_id` **只编码在 msg_key 里**（`account|chat_md5|shard|local_id`，
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
    按 msg_key 前缀计数就分得开：`msg_key = account|chat_md5|shard|local_id`，而 wxid 不可能
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
        con.execute("BEGIN IMMEDIATE")
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
        con.execute("BEGIN IMMEDIATE")
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

    **前缀比对而非 LIKE**：`msg_key = account|chat_md5|shard|local_id`，而 account（wxid）
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
