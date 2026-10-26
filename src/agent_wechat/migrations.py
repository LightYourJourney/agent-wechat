"""版本化 schema 迁移：版本登记、顺序执行、未来版本拒绝、失败回滚。

**分工（本模块只做「版本」这一件事）**：
- **ensure**（各 owner 自己做）：`CREATE … IF NOT EXISTS` 的整套 DDL 仍由 `store.connect()`
  / `vector.connect()` 每次执行——它负责「对象缺失就补建」的自愈，也是既有行为。
- **migrate**（本模块）：处理 `IF NOT EXISTS` **表达不了**的变更（改触发器体、加列、重建表）。
  每条迁移有版本号，只执行一次；版本行与它的 DDL 在**同一个库内的事务**里落盘。

**三条不可假设的事**：
1. **不假设跨库原子**。`index.db` 与 `exclusions.db` 是两个 ATTACH 的文件，主库又是 WAL——
   SQLite 明确不保证跨文件 COMMIT 的崩溃原子性（`store.connect()` 的 docstring 引了原文）。
   所以每个 scope 各自版本化、各自事务、各自幂等；两个 scope 之间只保证「下次连接接着做」，
   不保证「同一次提交里一起成功」。
2. **迁移脚本里不得出现 `PRAGMA journal_mode`**：事务内不能改 journal 模式。该 PRAGMA 由
   `store.connect()` 在事务外执行。
3. **进入迁移必须是自动提交态**。`executescript` 会先 COMMIT 掉调用方的事务；带着未提交的
   事务进来，那些改动会被静默提交，而版本判断是在它们之前读的。故入口显式拒绝。

**失败语义**：每条迁移包成 `BEGIN IMMEDIATE; <script>; <写版本行>; COMMIT;`。任何一句失败
→ 回滚整条 → 版本停在上一版 → 抛 `SchemaMigrationError`（含 scope / version / 原始原因）。
下次连接重试同一条；重复执行已应用的迁移是 no-op（版本判断）。
"""

from __future__ import annotations

import dataclasses
import re
import sqlite3
import time

from . import errors

SCOPE_MAIN = "main"
SCOPE_VECTOR = "vector"
SCOPE_EXCLUSIONS = "exclusions"

VERSION_TABLE = "schema_version"
EXCLUSIONS_TABLE = f"{SCOPE_EXCLUSIONS}.{VERSION_TABLE}"

# scope 会拼进 SQL 字面量（executescript 不接受参数绑定）。它只来自本模块的常量，
# 但把形状收紧到 `[a-z_]+`，将来若有人从配置读进来也不会形成注入面。
_SCOPE_RE = re.compile(r"^[a-z_]+$")


@dataclasses.dataclass(frozen=True)
class Migration:
    """一条迁移：version 必须≥2（1 是基线，由 `apply` 隐式写入）。"""

    version: int
    name: str
    script: str


def ensure_table(con: sqlite3.Connection, table: str = VERSION_TABLE) -> None:
    """建版本表（幂等）。表名只来自本模块常量与 exclusions 的限定名。"""
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {table} ("
        " scope TEXT PRIMARY KEY,"
        " version INTEGER NOT NULL,"
        " applied_at INTEGER NOT NULL)"
        " WITHOUT ROWID")


def stored_version(con: sqlite3.Connection, scope: str,
                   table: str = VERSION_TABLE) -> int:
    """该 scope 的已应用版本。没有记录 = 0（尚未 stamp 过基线的老库）。"""
    row = con.execute(f"SELECT version FROM {table} WHERE scope=?", (scope,)).fetchone()
    return int(row[0]) if row else 0


def _stamp(con: sqlite3.Connection, scope: str, version: int,
           table: str) -> None:
    """写一条版本行（幂等）。用于基线、以及每条迁移的收尾。"""
    con.execute(
        f"INSERT INTO {table}(scope, version, applied_at) VALUES (?,?,?) "
        "ON CONFLICT(scope) DO UPDATE SET version=excluded.version, "
        "applied_at=excluded.applied_at",
        (scope, version, int(time.time())))


def _run_one(con: sqlite3.Connection, scope: str, m: Migration,
             table: str) -> None:
    if con.in_transaction:
        raise errors.SchemaMigrationError(
            f"迁移入口要求自动提交态：{scope} v{m.version}（{m.name}）",
            hint="调用方先 commit/rollback；带未提交事务进来会让那些改动被 executescript 静默提交")
    # scope 只来自常量；version 是 int；时间戳是 int。没有用户输入进 SQL。
    body = (
        "BEGIN IMMEDIATE;\n"
        f"{m.script.strip()}\n"
        f"INSERT INTO {table}(scope, version, applied_at) "
        f"VALUES ('{scope}', {int(m.version)}, {int(time.time())}) "
        "ON CONFLICT(scope) DO UPDATE SET version=excluded.version, "
        "applied_at=excluded.applied_at;\n"
        "COMMIT;")
    try:
        con.executescript(body)
    except (sqlite3.Error, OSError) as exc:
        # executescript 在错误处停下；BEGIN 起的事务仍在，必须自己收掉，否则下一条
        # 迁移（或调用方的下一次写入）会撞「cannot start a transaction within a transaction」。
        if con.in_transaction:
            con.rollback()
        raise errors.SchemaMigrationError(
            f"迁移失败：{scope} v{m.version}（{m.name}）：{errors.redact(str(exc))}",
            hint="该库未被改动（版本停在上一版）；确认磁盘空间与写锁后重跑即可续；"
                 "反复失败请先备份该库") from exc


def apply(con: sqlite3.Connection, *, scope: str,
          migrations: tuple[Migration, ...] = (),
          table: str = VERSION_TABLE, baseline: int = 1) -> int:
    """把该 scope 升到本程序已知的最新版，返回最终版本。

    - 无版本行 → 先写 `baseline`（=1：整套 DDL 已由 ensure 建好），再执行已定义的迁移。
    - 版本**高于**本程序已知的最新版 → 拒绝（`SchemaMigrationError`），不碰库：那多半是
      降级运行新库，继续写会把未知结构改坏。
    - 迁移按 version 升序、只跑 `version > 当前` 的；重复调用是 no-op。
    """
    if not _SCOPE_RE.match(scope):
        raise ValueError(f"scope 形状不合法：{scope!r}")
    if con.in_transaction:
        # 同一道守卫在 _run_one 里也有一份；这里是**更早**的一次：基线 stamp 也是一条
        # INSERT，它会开事务——带着调用方未提交的写入进来，下面那句 commit 会把它们
        # 一起提交掉。在入口就拒，语义才一致。
        raise errors.SchemaMigrationError(
            f"迁移入口要求自动提交态：{scope}",
            hint="调用方先 commit/rollback；迁移自己管理事务边界")
    try:
        ensure_table(con, table)
        latest = max([baseline, *(m.version for m in migrations)])
        cur = stored_version(con, scope, table)
        if cur == 0:
            # 基线 stamp 也是一条写：它自己的失败（只读库 / 磁盘满 / 写锁）必须与迁移
            # 同一种出口。commit 必须紧跟着——下一条迁移要求自动提交态。
            _stamp(con, scope, baseline, table)
            con.commit()
            cur = baseline
    except (sqlite3.Error, OSError) as exc:
        if con.in_transaction:
            con.rollback()
        raise errors.SchemaMigrationError(
            f"读取/初始化 schema 版本失败：{scope}：{errors.redact(str(exc))}",
            hint="确认数据目录可写、磁盘有空间、且没有别的进程持写锁") from exc
    if cur > latest:
        raise errors.SchemaMigrationError(
            f"索引库版本过新：{scope} 已到 v{cur}，本程序只认到 v{latest}",
            hint="升级 agent-wechat 到匹配的版本；或从备份恢复旧库。"
                 "不要用旧版程序继续写这个库")
    for m in sorted(migrations, key=lambda x: x.version):
        if m.version <= cur:
            continue
        _run_one(con, scope, m, table)
        cur = m.version
    return cur
