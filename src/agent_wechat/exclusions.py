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

import pathlib
import sqlite3

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


def remove_chat(con: sqlite3.Connection, account: str, chat_md5: str) -> int:
    """按 `(account, chat_md5)` 精确删——**不用 LIKE**：`_` 与 `%` 都是通配符。"""
    cur = con.execute("DELETE FROM exclusions.excluded WHERE account=? AND chat_md5=?",
                      (account, chat_md5))
    return cur.rowcount


def count(con: sqlite3.Connection) -> int:
    return con.execute("SELECT count(*) FROM exclusions.excluded").fetchone()[0]


def by_chat(con: sqlite3.Connection) -> list[tuple[str, int]]:
    """`(chat_md5, 条数)`，按条数降序。`--excluded` 的输出用它。"""
    return [(r[0], r[1]) for r in con.execute(
        "SELECT chat_md5, count(*) AS n FROM exclusions.excluded "
        "GROUP BY chat_md5 ORDER BY n DESC, chat_md5")]
