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
3. **进入迁移必须是自动提交态**。迁移自己会用 `BEGIN IMMEDIATE` 拿写锁；带着调用方未提交的
   事务进来，要么撞「已在事务里」，要么把别人的改动一起提交。

**一次升级 = 一笔事务**：`BEGIN IMMEDIATE; <所有待执行迁移的语句>; <逐条写版本行>; COMMIT`。
在写锁内**重读版本**，因此两个进程并发 `apply` 不会互相覆盖版本、也不会把同一条迁移跑两遍
（先到者提交后，后到者在锁内读到新版本，直接返回）。任何一句失败 → 整次升级回滚（包括
本次已应用的更早几条）→ 版本停在上次成功的位置 → 抛 `SchemaMigrationError`；下次连接重做。

**拒绝比写入更早**：`refuse_if_newer()` 在 owner 跑 ensure 之前调用，版本过新的库**一个字节
都不写**（否则旧程序会先给新库补建对象、再拒绝，等于它声称不碰却已经改了）。
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

# scope 以字面量拼进 SQL（DDL 与表名都无法参数绑定）。它只来自本模块的常量，
# 但把形状收紧到 `[a-z_]+`，将来若有人从配置读进来也不会形成注入面。
_SCOPE_RE = re.compile(r"^[a-z_]+$")


@dataclasses.dataclass(frozen=True)
class Migration:
    """一条迁移：version 必须≥2（1 是基线，由 `apply` 隐式写入）。

    `statements` 是**逐条** SQL（不是一整段脚本）：`apply` 在同一笔事务里用
    `con.execute` 逐句执行，这样版本行、多条迁移与它们的 DDL 才能共享同一个写锁与
    同一个回滚边界。
    """

    version: int
    name: str
    statements: tuple[str, ...]


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


def version_if_table_exists(con: sqlite3.Connection, scope: str,
                            table: str = VERSION_TABLE) -> int:
    """同 `stored_version`，但**版本表不存在时返回 0 而不是抛**。

    给 `refuse_if_newer()` 用：它在 ensure 之前跑，那时老库还没有 `schema_version`。
    """
    try:
        con.execute(f"SELECT 1 FROM {table} LIMIT 1")
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return 0
        raise
    return stored_version(con, scope, table)


def refuse_if_newer(con: sqlite3.Connection, *, scope: str, latest: int,
                    table: str = VERSION_TABLE) -> None:
    """版本高于本程序已知的最新版 → 拒绝，且**在 owner 跑 ensure 之前**。

    这是「不碰库」的实现点：旧程序遇到新库时，先在这里停下——不补建对象、不改
    journal 模式、不写版本行。仅仅在 `apply()` 里拒绝是不够的：那时 ensure 已经
    把缺失的表补进了新库。
    """
    cur = version_if_table_exists(con, scope, table)
    if cur > latest:
        raise errors.SchemaMigrationError(
            f"索引库版本过新：{scope} 已到 v{cur}，本程序只认到 v{latest}",
            hint="升级 agent-wechat 到匹配的版本；或从备份恢复旧库。"
                 "不要用旧版程序继续写这个库")


def _stamp(con: sqlite3.Connection, scope: str, version: int,
           table: str) -> None:
    """写一条版本行（幂等）。**不 commit**：调用方在同一个事务里收口。"""
    con.execute(
        f"INSERT INTO {table}(scope, version, applied_at) VALUES (?,?,?) "
        "ON CONFLICT(scope) DO UPDATE SET version=excluded.version, "
        "applied_at=excluded.applied_at",
        (scope, version, int(time.time())))


def apply(con: sqlite3.Connection, *, scope: str,
          migrations: tuple[Migration, ...] = (),
          table: str = VERSION_TABLE, baseline: int = 1) -> int:
    """把该 scope 升到本程序已知的最新版，返回最终版本。

    - 无版本行 → 先写 `baseline`（=1：整套 DDL 已由 ensure 建好），再执行已定义的迁移。
    - 版本**高于**本程序已知的最新版 → 拒绝（`SchemaMigrationError`），不碰库。
    - 迁移按 version 升序、只跑 `version > 当前` 的；重复调用是 no-op。
    - 整次升级是一笔 `BEGIN IMMEDIATE … COMMIT`：版本在写锁内**重读**，并发 apply 不会
      互相覆盖，也不会把一条迁移跑两遍。
    """
    if not _SCOPE_RE.match(scope):
        raise errors.SchemaMigrationError(
            f"scope 形状不合法：{scope!r}",
            hint="scope 只能是小写字母与下划线；它由本模块的常量提供，不接受外部输入")
    if con.in_transaction:
        # 迁移自己管事务边界；带着调用方的未提交事务进来，要么撞「已在事务里」，
        # 要么把别人的改动一起提交掉。入口就拒。
        raise errors.SchemaMigrationError(
            f"迁移入口要求自动提交态：{scope}",
            hint="调用方先 commit/rollback；迁移自己管理事务边界")
    try:
        ensure_table(con, table)
    except (sqlite3.Error, OSError) as exc:
        raise errors.SchemaMigrationError(
            f"无法建立版本表：{scope}：{errors.redact(str(exc))}",
            hint="确认数据目录可写、磁盘有空间、且没有别的进程持写锁") from exc

    latest = max([baseline, *(m.version for m in migrations)])
    pending = sorted(migrations, key=lambda m: m.version)

    # **快路径**：已经是最新版就只读一次版本行，**不拿写锁**。每次 `store.connect()` 都会
    # 走到这里——若无条件 `BEGIN IMMEDIATE`，任何只读操作（status / search）都会被一个
    # 正在写库的进程挡住，且 `purge` 那把「先探排除清单」的 fail-closed 闸会提前失败、
    # 报出比它自己的诊断更含糊的错（实测：purge 的锁冲突用例因此红）。
    try:
        cur = stored_version(con, scope, table)
    except (sqlite3.Error, OSError) as exc:
        raise errors.SchemaMigrationError(
            f"读取 schema 版本失败：{scope}：{errors.redact(str(exc))}",
            hint="确认索引库可读、且没有别的进程正在写它") from exc
    if cur > latest:
        raise errors.SchemaMigrationError(
            f"索引库版本过新：{scope} 已到 v{cur}，本程序只认到 v{latest}",
            hint="升级 agent-wechat 到匹配的版本；或从备份恢复旧库")
    if cur == latest and cur != 0:
        return cur

    try:
        con.execute("BEGIN IMMEDIATE")
        cur = stored_version(con, scope, table)      # 写锁内重读：并发安全
        if cur > latest:
            raise errors.SchemaMigrationError(
                f"索引库版本过新：{scope} 已到 v{cur}，本程序只认到 v{latest}",
                hint="升级 agent-wechat 到匹配的版本；或从备份恢复旧库")
        if cur == 0:
            _stamp(con, scope, baseline, table)
            cur = baseline
        for m in pending:
            if m.version <= cur:
                continue
            try:
                for stmt in m.statements:
                    con.execute(stmt)
            except (sqlite3.Error, OSError) as exc:
                raise errors.SchemaMigrationError(
                    f"迁移失败：{scope} v{m.version}（{m.name}）：{errors.redact(str(exc))}",
                    hint="整次升级已回滚（版本停在上一版）；确认磁盘空间与写锁后重跑即可；"
                         "反复失败请先备份该库") from exc
            _stamp(con, scope, m.version, table)
            cur = m.version
        con.commit()
        return cur
    except errors.SchemaMigrationError:
        if con.in_transaction:
            con.rollback()
        raise
    except (sqlite3.Error, OSError) as exc:
        if con.in_transaction:
            con.rollback()
        raise errors.SchemaMigrationError(
            f"schema 升级失败：{scope}：{errors.redact(str(exc))}",
            hint="整次升级已回滚；确认磁盘空间与写锁后重跑即可续") from exc
