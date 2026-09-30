"""排除清单：被用户删掉的会话里那批消息的墓碑。**唯一一个删了就不可重建的产物。**

设计取舍与四条复活路径见 内部设计记录。

两条不可违反的约定：
- 路径**调用时求值**（`path()`），绝不做模块常量——purge 的红线 fixture 靠 monkeypatch
  `paths.HOME_DIR` 把路径重定向到临时目录，import 时绑定值会让那条保护静默失效，
  后果是测试去删真实数据。
- 本模块的表在 SQL 里一律写成 **`exclusions.excluded`**（带库名前缀）：它由
  `store.connect()` ATTACH 进来，而 SQLite 的 upsert 伪表就叫 `excluded`，不限定就有歧义。
  唯一的例外是 `SCHEMA` 里 CREATE INDEX 的 ON 子句（SQLite 不接受那里带库名），见其注释。
"""
from __future__ import annotations

import contextlib
import pathlib
import sqlite3
from collections.abc import Iterator

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS exclusions.excluded (
    msg_key     TEXT PRIMARY KEY,   -- account|chat_md5|shard|local_id，与 messages.msg_key 逐字相同
    account     TEXT NOT NULL,      -- 从 msg_key 派生，存下来只为能建索引
    chat_md5    TEXT NOT NULL,      -- 同上
    excluded_at INTEGER NOT NULL
);
-- ON 后面那张表**只能**是不带库名的 `excluded`：SQLite 的 CREATE INDEX 语法不接受
-- `ON 库名.表名`（实测 3.50.4 报 `near ".": syntax error`）。把库名放在**索引名**上
-- 同样足够、而且更严：索引必须与它索引的表同库，于是 SQLite 只在 exclusions 里找 `excluded`
-- （不限定索引名时它会去 main 找，报 `no such table: main.excluded`）。
CREATE INDEX IF NOT EXISTS exclusions.idx_excluded_chat ON excluded(account, chat_md5);
"""


def path() -> pathlib.Path:
    """排除清单文件。**调用时求值**，理由见模块 docstring。"""
    return paths.HOME_DIR / "exclusions.db"


def path_for(db_path: pathlib.Path) -> pathlib.Path:
    """给定索引库路径，返回与它同目录的排除清单。

    `store.connect()` 用它而不是 `path()`：测试普遍传一个 tmp 的 `db_path`，跟着它走
    才能让排除表也落在 tmp 里。生产上两者一致——`store.DB_PATH` 就是 `HOME_DIR/index.db`。
    """
    return db_path.parent / "exclusions.db"


@contextlib.contextmanager
def standalone() -> Iterator[sqlite3.Connection]:
    """只连排除清单本身的连接：`:memory:` + `ATTACH DATABASE ? AS exclusions`。

    给**不能走 `store.open()`** 的场合用——`store.connect()` 会 `ensure_home_dirs()` 并把整个
    索引 schema 建出来，于是「看一眼/改一下清单」会把刚被 `purge --index` 删掉的 index.db
    当场重建。别名与生产一致（`store.connect()` ATTACH 用的也是 `exclusions`，见模块
    docstring），所以本模块与 `purge.py` 里那些带库名前缀的 SQL 原样可用。

    清单文件不存在时给一张**空表**（在内存里建同一份 schema），而不是让 ATTACH 顺手把它
    建出来——一条只读的查询不该在用户磁盘上落新文件。
    """
    p = path()
    con = sqlite3.connect(":memory:")
    try:
        if p.exists():
            con.execute("ATTACH DATABASE ? AS exclusions", (str(p),))
        else:
            con.execute("ATTACH DATABASE ':memory:' AS exclusions")
            con.executescript(SCHEMA)
        yield con
    finally:
        con.close()


def add(con: sqlite3.Connection, rows: list[tuple[str, str, str]]) -> int:
    """批量写墓碑。幂等（`INSERT OR IGNORE`）。不 commit：由调用方收口。"""
    if not rows:
        return 0
    import time
    now = int(time.time())
    con.executemany(
        "INSERT OR IGNORE INTO exclusions.excluded "
        "(msg_key, account, chat_md5, excluded_at) VALUES (?,?,?,?)",
        [(k, a, m, now) for k, a, m in rows])
    return len(rows)


def _chat_prefix(account: str, chat_md5: str) -> tuple[int, str]:
    """`(前缀长度, 前缀)` 二元组，供 `substr(msg_key, 1, ?) = ?` 用。

    `count_chat` 与 `remove_chat` 共用它：两者的谓词必须同源，一旦分道扬镳，
    「预览说 N 条、实际删掉 M 条」这种各说各话就会成立。
    """
    pref = f"{account}|{chat_md5}|"
    return (len(pref), pref)


def count_chat(con: sqlite3.Connection, account: str, chat_md5: str) -> int:
    """该会话在清单里有多少条——**与 `remove_chat` 同一个谓词**（见 `_chat_prefix`）。

    此前这条计数写在入口层（`cli._cmd_purge_chat_undo` 的行内 SQL），与删除分处两个文件：
    预览说 N 条、实际删掉 M 条这种各说各话，正是「两处各写一遍同一个判据」的产物。
    """
    n, pref = _chat_prefix(account, chat_md5)
    return con.execute("SELECT count(*) FROM exclusions.excluded "
                       "WHERE substr(msg_key, 1, ?) = ?", (n, pref)).fetchone()[0]


def remove_chat(con: sqlite3.Connection, account: str, chat_md5: str) -> int:
    """按 `msg_key` 前缀删该会话的全部条目——**不用 LIKE**：`_` 与 `%` 都是通配符。

    **不用 `account=? AND chat_md5=?`**：那两个派生列只有忠实（逐字等于 `msg_key` 的前两段）
    时才与前缀等价，而 design spec §4 只是**要求**它们忠实——判据不该建立在一个「要求」上，何况
    `store.delete_chat` / `store.chat_row_counts` 删/数同一件事时走的就是这条前缀。
    删除端与核查端读同一段 `msg_key`，两处就不可能对同一个会话给出不同的答案。

    谓词与 `_write_tombstone` 的 `INSERT … SELECT` 也同源：那条写的是
    `account|chat_md5|…` 形态的 `msg_key`，前缀删与它天然配对。
    """
    n, pref = _chat_prefix(account, chat_md5)
    cur = con.execute("DELETE FROM exclusions.excluded WHERE substr(msg_key, 1, ?) = ?",
                      (n, pref))
    return cur.rowcount


def count(con: sqlite3.Connection) -> int:
    return con.execute("SELECT count(*) FROM exclusions.excluded").fetchone()[0]


def by_chat(con: sqlite3.Connection) -> list[tuple[str, int]]:
    """`(chat_md5, 条数)`，按条数降序。`--excluded` 的输出用它。"""
    return [(r[0], r[1]) for r in con.execute(
        "SELECT chat_md5, count(*) AS n FROM exclusions.excluded "
        "GROUP BY chat_md5 ORDER BY n DESC, chat_md5")]
