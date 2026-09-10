"""向量语义检索：本地 embedding（fastembed/ONNX）+ sqlite-vec KNN。

隔离带：全部 fastembed 与 sqlite-vec 调用都收在本模块（上游 sqlite-vec 为
pre-v1，存在已知崩溃/内存泄漏 bug），便于回归测试与将来整体替换。
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from typing import Callable, Sequence

import sqlite_vec

from . import paths, store

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
DIM = 512                                   # 必须与 store.VECTOR_SCHEMA 的 float[512] 一致
MODEL_ID = f"fastembed:{MODEL_NAME}:{DIM}"
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："   # BGE 官方 s2p 指令，仅查询侧加
MODEL_CACHE_DIR = paths.HOME_DIR / "models"   # 固定目录：不用 $TMPDIR（重启即清）
BATCH = 256                                  # fastembed 实测最优批大小
COMMIT_ROWS = 5120                           # 每 ~20 批一个事务：频繁 commit 会让插入掉 ~43%
K_MAX = 4096                                 # sqlite-vec 硬上限
EMBED_TYPES = (1, 49)                        # 1=文本；49=appmsg（content 形如 "[appmsg: 标题]"）


class VectorError(RuntimeError):
    """向量层不可用（扩展缺失/模型不可用）。消息里带用户可执行的修复指引。"""


def connect() -> sqlite3.Connection:
    """store 连接 + 加载 sqlite-vec 扩展 + 建向量表（扩展不可用则 fail-fast）。"""
    con = store.connect()
    con.row_factory = sqlite3.Row
    try:
        con.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(con)
    except Exception as e:
        con.close()
        raise VectorError(
            "无法加载 sqlite-vec 扩展（需要解释器支持 enable_load_extension）："
            f"{e}. uv 管理的 Python 3.12 已验证可用；换 python.org/conda 发行版需重新验证。"
        ) from e
    con.enable_load_extension(False)
    con.execute("SELECT vec_version()").fetchone()      # 探活：真正验证扩展可用
    con.executescript(store.VECTOR_SCHEMA)
    return con


def should_embed(row) -> bool:
    """文本与带标题的 appmsg 才入向量；纯媒体占位与过短消息跳过（spec §5）。"""
    if row["msg_type"] not in EMBED_TYPES:
        return False
    content = (row["content"] or "").strip()
    if len(content) < 2:
        return False
    if row["msg_type"] == 49:
        return content.startswith("[appmsg: ") and content != "[appmsg]"
    return True


def display_text(row) -> str:
    """一行消息的展示文本：正文优先，媒体消息退回 media_hint。"""
    return (row["content"] or row["media_hint"] or "").strip()


def _who(row) -> str:
    if row["is_self"]:
        return "我"
    return row["sender_name"] or row["sender_id"] or "?"


def _date(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def doc_text(row, prev: str | None) -> str:
    """入库文本 = 确定性上下文前缀 + 正文。

    前缀解决「好的」「在吗」这类短消息单条 embedding 语义不可靠的问题
    （Anthropic Contextual Retrieval 方法学：失败检索率 -49%，见 spec §5）。
    """
    parts = [f"[{_date(row['ts'])} {_who(row)}]"]
    if prev:
        head = prev.replace("\n", " ")[:30]
        if head:
            parts.append(f"上文: {head}…")
    parts.append(f"| {(row['content'] or '').strip()}")
    return " ".join(parts)


def query_text(q: str) -> str:
    """查询侧加 BGE 官方 s2p 指令（文档侧不加；两侧策略固定，见 spec §5）。"""
    return QUERY_INSTRUCTION + q.strip()


class FastEmbedEmbedder:
    """fastembed 懒加载封装：模型只在首次使用时载入（首次会下载 ~95MB 公共资产）。

    加锁：MCP 里建索引跑在后台线程、查询跑在主线程，共享一个实例。
    """

    def __init__(self, local_files_only: bool = False):
        self._local_only = local_files_only
        self._model = None
        self._lock = threading.Lock()

    def _ensure(self):
        if self._model is None:
            from fastembed import TextEmbedding
            MODEL_CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._model = TextEmbedding(model_name=MODEL_NAME,
                                        cache_dir=str(MODEL_CACHE_DIR),
                                        local_files_only=self._local_only)
        return self._model

    def embed(self, texts: list[str]) -> list[Sequence[float]]:
        with self._lock:
            return list(self._ensure().embed(texts, batch_size=BATCH))

    def embed_query(self, text: str) -> Sequence[float]:
        with self._lock:
            return next(iter(self._ensure().query_embed([text])))


_default_embedder: FastEmbedEmbedder | None = None


def default_embedder() -> FastEmbedEmbedder:
    """模块级单例：MCP 常驻进程里避免每次查询重载模型。"""
    global _default_embedder
    if _default_embedder is None:
        _default_embedder = FastEmbedEmbedder()
    return _default_embedder


# ---------- meta / 水位线 ----------

def _meta(con) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM vector_meta WHERE model_id = ?", (MODEL_ID,)).fetchone()


def _ensure_meta(con) -> None:
    con.execute("INSERT OR IGNORE INTO vector_meta(model_id, dim, built_at, embedded, skipped) "
                "VALUES (?,?,?,0,0)", (MODEL_ID, DIM, int(time.time())))
    con.commit()


def _write_meta(con, *, embedded: int, skipped: int) -> None:
    con.execute("UPDATE vector_meta SET embedded=?, skipped=?, built_at=? WHERE model_id=?",
                (embedded, skipped, int(time.time()), MODEL_ID))
    con.commit()


def _watermark(con, account: str) -> int:
    row = con.execute("SELECT last_rowid FROM vector_watermark WHERE model_id=? AND account=?",
                      (MODEL_ID, account)).fetchone()
    return row[0] if row else 0


def _set_watermark(con, account: str, rowid: int) -> None:
    con.execute(
        """INSERT INTO vector_watermark(model_id, account, last_rowid, updated_at)
           VALUES (?,?,?,?)
           ON CONFLICT(model_id, account) DO UPDATE SET
             last_rowid = max(last_rowid, excluded.last_rowid),
             updated_at = excluded.updated_at""",
        (MODEL_ID, account, rowid, int(time.time())))


_SELECT_ROWS = (
    "SELECT id, account, chat_md5, chat_id, ts, msg_type, sender_id, sender_name, "
    "is_self, content, media_hint FROM messages WHERE account = ? AND id > ? ORDER BY id")

_INSERT_VEC = ("INSERT INTO vec_messages(msg_rowid, embedding, acct_chat, account, ts, preview) "
               "VALUES (?,?,?,?,?,?)")


def _build_account(con, acct: str, embedder, progress: Callable[[int], None] | None):
    """单账号增量嵌入。返回 (embedded, skipped)。

    事务边界：每 COMMIT_ROWS 行提交一次（含水位线，二者同事务 → 崩溃后不会重复嵌入）。
    """
    last_id = start = _watermark(con, acct)
    prev_by_chat: dict[str, str] = {}
    buf_texts: list[str] = []
    buf_rows: list[tuple] = []
    embedded = skipped = since_commit = 0

    def flush() -> None:
        nonlocal embedded
        if not buf_rows:
            return
        vecs = embedder.embed(buf_texts)
        con.executemany(_INSERT_VEC,
                        [(rid, sqlite_vec.serialize_float32(v), acct_chat, acct, ts, prev)
                         for (rid, acct_chat, ts, prev), v in zip(buf_rows, vecs)])
        embedded += len(buf_rows)
        buf_texts.clear()
        buf_rows.clear()

    cur = con.execute(_SELECT_ROWS, (acct, start))
    while True:                       # 分页遍历（实测：同连接边读边写边 commit 安全）
        page = cur.fetchmany(BATCH)
        if not page:
            break
        for row in page:
            last_id = row["id"]
            chat_key = f"{row['account']}|{row['chat_md5']}"
            prev = prev_by_chat.get(chat_key)
            if should_embed(row):
                buf_texts.append(doc_text(row, prev))
                buf_rows.append((row["id"], chat_key, row["ts"], (prev or "")[:80]))
                # 只有「有意义的消息」才作为后一条的上文：否则媒体占位（[image] 之类）
                # 会污染前缀，让所有短消息都带上「上文: [image]…」的噪声
                prev_by_chat[chat_key] = display_text(row)
                if len(buf_rows) >= BATCH:
                    flush()
                    since_commit += BATCH
                    if since_commit >= COMMIT_ROWS:
                        _set_watermark(con, acct, last_id)
                        con.commit()
                        since_commit = 0
                        if progress:
                            progress(embedded)
            else:
                skipped += 1
    flush()
    _set_watermark(con, acct, last_id)
    con.commit()
    if progress:
        progress(embedded)
    return embedded, skipped


def build_index(account: str | None = None, rebuild: bool = False, embedder=None,
                progress: Callable[[int], None] | None = None) -> dict:
    """全量/增量构建向量索引。返回本次运行 {"embedded": n, "skipped": n}。

    account=None 表示索引库中出现过的全部账号（水位线按账号分别记录）。
    """
    embedder = embedder or default_embedder()
    con = connect()
    try:
        if rebuild:
            con.executescript(store.VECTOR_DROP)
            con.executescript(store.VECTOR_SCHEMA)
            con.commit()
        _ensure_meta(con)
        accounts = ([account] if account else
                    [r[0] for r in con.execute("SELECT DISTINCT account FROM messages")])
        embedded = skipped = 0
        for acct in accounts:
            e, s = _build_account(con, acct, embedder, progress)
            embedded += e
            skipped += s
        prev_skipped = _meta(con)["skipped"]
        total = con.execute("SELECT count(*) FROM vec_messages").fetchone()[0]
        _write_meta(con, embedded=total, skipped=prev_skipped + skipped)
        return {"embedded": embedded, "skipped": skipped}
    finally:
        con.close()
