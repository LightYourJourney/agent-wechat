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

from . import paths, store, tasks

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


def should_embed(row: sqlite3.Row | dict) -> bool:
    """文本与带标题的 appmsg 才入向量；纯媒体占位与过短消息跳过（spec §5）。"""
    if row["msg_type"] not in EMBED_TYPES:
        return False
    content = (row["content"] or "").strip()
    if len(content) < 2:
        return False
    if row["msg_type"] == 49:
        return content.startswith("[appmsg: ") and content != "[appmsg]"
    return True


def display_text(row: sqlite3.Row | dict) -> str:
    """一行消息的展示文本：正文优先，媒体消息退回 media_hint。"""
    return (row["content"] or row["media_hint"] or "").strip()


def _who(row: sqlite3.Row | dict) -> str:
    if row["is_self"]:
        return "我"
    return row["sender_name"] or row["sender_id"] or "?"


def _date(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def doc_text(row: sqlite3.Row | dict, prev: str | None) -> str:
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


def _chat_md5(chat_id: str) -> str:
    """会话 id → Msg_<md5> 的 md5 部分。

    reader 对无法反查的会话产出 "md5:<hex>" 退化 id，其本身即 md5——
    该约定以本函数为唯一实现点（评审指出此前在 vector 与 reader 两处各有一份）。
    """
    return chat_id[4:] if chat_id.startswith("md5:") else hashlib.md5(chat_id.encode()).hexdigest()


def _acct_chat(account: str, chat_md5: str) -> str:
    """vec0 分区键 "{account}|{chat_md5}"。构建与查询两侧共用，防格式漂移。"""
    return f"{account}|{chat_md5}"


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
            kwargs = dict(model_name=MODEL_NAME, cache_dir=str(MODEL_CACHE_DIR))
            if not self._local_only:
                try:
                    # 默认先纯离线加载：模型已在本地时绝不发起网络请求（Global Constraint #4：
                    # 运行期不得发起任何网络请求）。只有首次（缓存缺失）才允许下载。
                    self._model = TextEmbedding(local_files_only=True, **kwargs)
                    return self._model
                except Exception:
                    pass
            # 显式 local_files_only=True 的调用方要求「失败即报错」，不做下载回退
            self._model = TextEmbedding(local_files_only=self._local_only, **kwargs)
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

def _meta(con: sqlite3.Connection) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM vector_meta WHERE model_id = ?", (MODEL_ID,)).fetchone()


def _foreign_model(con: sqlite3.Connection) -> str | None:
    """索引里是否存在**别的模型**构建的 meta 行；有则返回那个 model_id，否则 None。

    这是「模型不匹配」的唯一判据，刻意**不看「最新一行」**：`built_at` 只有秒级
    分辨率，多行状态下顺序不确定（取哪行是碰运气）；而且只要存在异模型行，
    就既不允许查询、也不允许继续构建——否则新模型的向量会被追加进旧模型的向量表。
    """
    row = con.execute("SELECT model_id FROM vector_meta WHERE model_id != ? LIMIT 1",
                      (MODEL_ID,)).fetchone()
    return row[0] if row else None


def _ensure_meta(con: sqlite3.Connection) -> None:
    con.execute("INSERT OR IGNORE INTO vector_meta(model_id, dim, built_at, embedded, skipped) "
                "VALUES (?,?,?,0,0)", (MODEL_ID, DIM, int(time.time())))
    con.commit()


def _write_meta(con: sqlite3.Connection, *, embedded: int, skipped: int) -> None:
    con.execute("UPDATE vector_meta SET embedded=?, skipped=?, built_at=? WHERE model_id=?",
                (embedded, skipped, int(time.time()), MODEL_ID))
    con.commit()


def _watermark(con: sqlite3.Connection, account: str) -> int:
    row = con.execute("SELECT last_rowid FROM vector_watermark WHERE model_id=? AND account=?",
                      (MODEL_ID, account)).fetchone()
    return row[0] if row else 0


def _set_watermark(con: sqlite3.Connection, account: str, rowid: int) -> None:
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


def _build_account(con: sqlite3.Connection, acct: str, embedder: FastEmbedEmbedder,
                   progress: Callable[[int], None] | None) -> tuple[int, int]:
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
        # 长度排序后成批嵌入，压掉混合长度批次的 padding 浪费（spec §7；
        # 实测混长吞吐仅为等长的 1/5）。结果向量按原扫描顺序配对回去——
        # 单事务内行序无影响。
        order = sorted(range(len(buf_texts)), key=lambda i: len(buf_texts[i]))
        vecs = embedder.embed([buf_texts[i] for i in order])
        con.executemany(_INSERT_VEC,
                        [(buf_rows[i][0], sqlite_vec.serialize_float32(v), buf_rows[i][1],
                          acct, buf_rows[i][2], buf_rows[i][3])
                         for i, v in zip(order, vecs)])
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
            chat_key = _acct_chat(row["account"], row["chat_md5"])
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


def build_index(account: str | None = None, rebuild: bool = False,
                embedder: FastEmbedEmbedder | None = None,
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
        # 换模型必须重建。不重建的两种真实失败形态（实测）：
        #   1) 模型常量变了 → 新模型的水位线从 0 起 → 重嵌 → 撞 vec_messages 主键（裸 OperationalError）
        #   2) 只改了 meta 行 → 水位线仍在 → 静默返回 {"embedded": 0}，索引停摆
        # 二者都不是「静默混用向量」，但都必须由用户显式 rebuild 才能继续。
        foreign = _foreign_model(con)
        if foreign:
            raise VectorError(
                f"索引由 {foreign} 构建，与当前模型 {MODEL_ID} 不一致：请改用 rebuild=True 重建")
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


# ---------- KNN 查询 ----------

_SELECT_MSGS = ("SELECT id, chat_id, shard, local_id, ts, msg_type, sender_id, sender_name, "
                "is_self, content, media_hint FROM messages WHERE id IN ({ph})")


def semantic_search(query: str, chat_id: str | None = None, limit: int = 10,
                    account: str = "", embedder: FastEmbedEmbedder | None = None) -> dict:
    """KNN 语义检索。account 必传（多账号隔离，与 search.search 规则一致）。

    先 KNN 取 rowid 与 distance，再单独查 messages —— 不用 JOIN+LIMIT
    （sqlite-vec issue #96：JOIN 与标准 LIMIT 子句在 KNN 下不兼容）。
    """
    if not account:
        return {"error": "semantic_search 需要显式 account（多账号索引隔离）",
                "hint": "传入目标账号目录名；MCP/CLI 层会先解析当前登录账号"}
    con = connect()
    try:
        if limit < 1:
            return {"error": "limit 必须为正整数", "hint": "limit 应为 ≥1 的整数"}
        # 顺序要紧：先判模型是否匹配，再判是否为空——不匹配的索引一律不得查询。
        # 判据必须模型无关（`_meta()` 按 MODEL_ID 过滤，异模型的行对它不可见，
        # 用它会让本分支永不可达），且必须看「是否存在任何异模型行」而非「最新一行」。
        foreign = _foreign_model(con)
        if foreign:
            return {"error": f"索引由 {foreign} 构建，与当前模型 {MODEL_ID} 不一致",
                    "hint": "运行 vector-build --rebuild 或 build_vector_index(rebuild=True)"}
        meta = _meta(con)
        if meta is None or meta["embedded"] == 0:
            return {"error": "向量索引为空", "hint": "请先运行 build_vector_index"}
        k = min(limit * 3, K_MAX)          # 多取候选，过滤后再截断（limit ≥ 1 时 limit*3 ≥ limit）
        qvec = sqlite_vec.serialize_float32(
            list((embedder or default_embedder()).embed_query(query_text(query))))
        sql = ("SELECT msg_rowid, distance FROM vec_messages "
               "WHERE embedding MATCH ? AND k = ? AND account = ?")
        args: list = [qvec, k, account]
        if chat_id:
            # 特判 reader 的 "md5:<hex>" 退化 id（见 _chat_md5），
            # 否则该会话永远过滤不到任何消息（静默返回空）
            sql += " AND acct_chat = ?"
            args.append(_acct_chat(account, _chat_md5(chat_id)))
        rows = con.execute(sql, args).fetchall()
        if not rows:
            return {"mode": "semantic", "model": MODEL_ID, "total": 0, "hits": []}
        ids = [r["msg_rowid"] for r in rows]
        msgs = {m["id"]: m for m in con.execute(
            _SELECT_MSGS.format(ph=",".join("?" * len(ids))), ids)}
        hits = []
        for r in rows:
            m = msgs.get(r["msg_rowid"])
            if m is None:
                continue
            hits.append({"chat_id": m["chat_id"], "ts": m["ts"],
                         "sender": m["sender_name"] or m["sender_id"] or "?",
                         "is_self": bool(m["is_self"]), "content": m["content"],
                         "media_hint": m["media_hint"], "distance": r["distance"]})
        return {"mode": "semantic", "model": MODEL_ID,
                "total": len(hits[:limit]), "hits": hits[:limit]}
    finally:
        con.close()


def start_build_task(account: str | None, rebuild: bool) -> str:
    """登记向量索引长任务（MCP 用）。account=None 表示全部账号。"""
    def _run(tid: str) -> int:
        res = build_index(account=account, rebuild=rebuild,
                          progress=lambda n: tasks.update(tid, rows=n))
        tasks.update(tid, skipped=res["skipped"])
        return res["embedded"]

    return tasks.start("vector", _run, account=account, rebuild=rebuild)
