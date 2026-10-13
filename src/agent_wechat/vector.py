"""向量语义检索：本地 embedding（fastembed/ONNX）+ sqlite-vec KNN。

隔离带：全部 fastembed 与 sqlite-vec 调用都收在本模块（上游 sqlite-vec 为
pre-v1，存在已知崩溃/内存泄漏 bug），便于回归测试与将来整体替换。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Callable, Sequence

import sqlite_vec

from . import messages, paths, store
from .errors import SearchInputError, VectorError, VectorIndexEmpty

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
DIM = store.VECTOR_DIM                      # 单一真值在 store；本模块只做短别名
MODEL_ID = f"fastembed:{MODEL_NAME}:{DIM}"
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："   # BGE 官方 s2p 指令，仅查询侧加
MODEL_CACHE_DIR = paths.HOME_DIR / "models"   # 固定目录：不用 $TMPDIR（重启即清）
BATCH = 256                                  # fastembed 实测最优批大小
COMMIT_ROWS = 5120                           # 每 ~20 批一个事务：频繁 commit 会让插入掉 ~43%
K_MAX = 4096                                 # sqlite-vec 硬上限
# 哪些类型值得嵌入是**本模块的策略**（不是行的事实），故常量留在这里；
# 类型本身仍从 messages 的词汇表取，不出现裸数字。
EMBED_TYPES = (messages.TYPE_TEXT, messages.TYPE_APPMSG)   # appmsg 提示形如 "[appmsg: 标题]"


def connect() -> sqlite3.Connection:
    """store 连接 + 加载 sqlite-vec 扩展 + 建向量表（扩展不可用则 fail-fast）。

    返回裸 Connection，关闭由 build_index / semantic_search 的 try/finally 负责
    （sqlite-vec 的知识只留在本模块，不委派给 store 的连接 owner）。
    """
    con = store.connect()
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


def load(con: sqlite3.Connection) -> bool:
    """尽力把 sqlite-vec 装进一条**别人开的**连接。装上了 True，装不上 False，**不抛**。

    与 connect() 的分工：那个是 owner（开连接、建表、装不上就 fail-fast），本函数只做
    「把扩展注册到这条连接上」这一件事。存在的理由是 purge：`store.connect()` 从不加载扩展，
    而 purge 的 plan/execute/verify 要碰 `vec_messages`（装了索引的机器上，第一条
    `chat_row_counts` 就会被 `store._vec_exec` 翻成 PurgeRefused）。

    **best-effort 是刻意的**：轻装安装上根本没有向量表，扩展装不装无关紧要
    （`store._has_table` 自会跳过那几处）；装了索引而扩展真坏掉时，仍由 `store._vec_exec`
    响亮抛 PurgeRefused——那才是该报错的地方，本函数抢着报只会把「表在、扩展不在」变成
    「所有安装一律崩」。`enable_load_extension` 用完立刻关回去（finally），
    不让这条连接一直带着「可加载任意动态库」的权限。
    """
    try:
        con.enable_load_extension(True)
        try:
            sqlite_vec.load(con)
        finally:
            con.enable_load_extension(False)
        return True
    except Exception:
        return False


def should_embed(row: sqlite3.Row | messages.MessageRow) -> bool:
    """文本与带标题的 appmsg 才入向量；纯媒体占位与过短消息跳过（spec §5）。"""
    if row["msg_type"] not in EMBED_TYPES:
        return False
    content = (row["content"] or "").strip()
    if len(content) < 2:
        return False
    if row["msg_type"] == messages.TYPE_APPMSG:
        return messages.has_appmsg_title(content)
    return True


def display_text(row: sqlite3.Row | messages.MessageRow) -> str:
    """一行消息的展示文本（正文；`should_embed` 已保证入选行 content 非空）。"""
    return (row["content"] or "").strip()


def _who(row: sqlite3.Row | messages.MessageRow) -> str:
    if row["is_self"]:
        return "我"
    return row["sender_name"] or row["sender_id"] or "?"


def _date(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def doc_text(row: sqlite3.Row | messages.MessageRow, prev: str | None) -> str:
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


# ---------- meta / 向量水位线（vector_watermark） ----------

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


def _ensure_model_ok(con: sqlite3.Connection) -> None:
    """索引里存在别的模型构建的 meta 行时拒绝继续。

    构建（build_index）与查询（semantic_search）的换模型守卫是**同一条件、同一文案**：
    判据只有 `_foreign_model` 一份，错误构造也只此一处，避免两处文案漂移。
    文案含「不一致」与 `rebuild`，且 hint 给出两条修复路径（与 TOOLS.md 的语义检索一节一致）。
    """
    foreign = _foreign_model(con)
    if foreign:
        raise VectorError(
            f"索引由 {foreign} 构建，与当前模型 {MODEL_ID} 不一致：请改用 rebuild=True 重建",
            hint="运行 vector-build --rebuild 或 build_vector_index(rebuild=True)")


def _ensure_meta(con: sqlite3.Connection) -> None:
    con.execute("INSERT OR IGNORE INTO vector_meta(model_id, dim, built_at, embedded, skipped) "
                "VALUES (?,?,?,0,0)", (MODEL_ID, DIM, int(time.time())))
    con.commit()


def _write_meta(con: sqlite3.Connection, *, embedded: int, skipped: int) -> None:
    con.execute("UPDATE vector_meta SET embedded=?, skipped=?, built_at=? WHERE model_id=?",
                (embedded, skipped, int(time.time()), MODEL_ID))
    con.commit()


def _vector_watermark(con: sqlite3.Connection, account: str) -> int:
    row = con.execute("SELECT last_rowid FROM vector_watermark WHERE model_id=? AND account=?",
                      (MODEL_ID, account)).fetchone()
    return row[0] if row else 0


def _set_vector_watermark(con: sqlite3.Connection, account: str, rowid: int) -> None:
    con.execute(
        """INSERT INTO vector_watermark(model_id, account, last_rowid, updated_at)
           VALUES (?,?,?,?)
           ON CONFLICT(model_id, account) DO UPDATE SET
             last_rowid = max(last_rowid, excluded.last_rowid),
             updated_at = excluded.updated_at""",
        (MODEL_ID, account, rowid, int(time.time())))


_SELECT_ROWS = (
    "SELECT " + messages.column_list(
        "id", "account", "chat_md5", "chat_id", "ts", "msg_type", "sender_id",
        "sender_name", "is_self", "content", "media_hint")
    + " FROM messages WHERE account = ? AND id > ? ORDER BY id")

_INSERT_VEC = ("INSERT INTO vec_messages(msg_rowid, embedding, acct_chat, account, ts, preview) "
               "VALUES (?,?,?,?,?,?)")


def _build_account(con: sqlite3.Connection, acct: str, embedder: FastEmbedEmbedder,
                   progress: Callable[[int], None] | None, *,
                   batch: int, commit_rows: int) -> tuple[int, int]:
    """单账号增量嵌入。返回 (embedded, skipped)。

    事务边界：每 commit_rows 行提交一次（含水位线，二者同事务 → 崩溃后不会重复嵌入）。
    batch 同时是分页步长与单次嵌入的行数上限（默认取模块常量，由 build_index 注入）。
    """
    last_id = start = _vector_watermark(con, acct)
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
        page = cur.fetchmany(batch)
        if not page:
            break
        for row in page:
            last_id = row["id"]
            chat_key = messages.acct_chat(row["account"], row["chat_md5"])
            prev = prev_by_chat.get(chat_key)
            if should_embed(row):
                buf_texts.append(doc_text(row, prev))
                buf_rows.append((row["id"], chat_key, row["ts"], (prev or "")[:80]))
                # 只有「有意义的消息」才作为后一条的上文：否则媒体占位（[image] 之类）
                # 会污染前缀，让所有短消息都带上「上文: [image]…」的噪声
                prev_by_chat[chat_key] = display_text(row)
                if len(buf_rows) >= batch:
                    flush()
                    since_commit += batch
                    if since_commit >= commit_rows:
                        _set_vector_watermark(con, acct, last_id)
                        con.commit()
                        since_commit = 0
                        if progress:
                            progress(embedded)
            else:
                skipped += 1
    flush()
    _set_vector_watermark(con, acct, last_id)
    con.commit()
    if progress:
        progress(embedded)
    return embedded, skipped


def build_index(accounts: Sequence[str] | None = None, rebuild: bool = False,
                embedder: FastEmbedEmbedder | None = None,
                progress: Callable[[int], None] | None = None, *,
                batch: int = BATCH, commit_rows: int = COMMIT_ROWS) -> dict:
    """全量/增量构建向量索引。返回**本轮**进度 {"embedded_this_run": n, "skipped_this_run": n}。

    返回键刻意带 `_this_run` 后缀：`vector_meta.embedded` 是索引内当前总行数（累计），
    而这里的计数只描述本次调用（跨账号累加）；两者语义不同，键名必须能区分。

    accounts=None 表示索引库中出现过的全部账号（水位线按账号分别记录）；显式传入
    列表则只索引列表内的账号（[] = 不索引任何账号，而非「全部」）。

    batch / commit_rows 默认取模块常量，仅供测试经接口注入（生产调用不传）。
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
        #   2) 只改了 meta 行 → 水位线仍在 → 静默返回 {"embedded_this_run": 0}，索引停摆
        # 二者都不是「静默混用向量」，但都必须由用户显式 rebuild 才能继续。
        _ensure_model_ok(con)
        _ensure_meta(con)
        targets = (list(accounts) if accounts is not None else
                   [r[0] for r in con.execute("SELECT DISTINCT account FROM messages")])
        embedded = skipped = 0
        for acct in targets:
            # 进度带偏移：_build_account 的计数是每账号局部的（各自从 0 起），直接
            # 透传会让多账号构建时 rows 在切换账号处从大跳回小。TOOLS.md 把 rows 定
            # 为「本轮已嵌入行数」，所以补上前序账号的累计，保证本轮内单调递增。
            base = embedded
            e, s = _build_account(con, acct, embedder,
                                  (lambda n: progress(base + n)) if progress else None,
                                  batch=batch, commit_rows=commit_rows)
            embedded += e
            skipped += s
        prev_skipped = _meta(con)["skipped"]
        total = con.execute("SELECT count(*) FROM vec_messages").fetchone()[0]
        _write_meta(con, embedded=total, skipped=prev_skipped + skipped)
        return {"embedded_this_run": embedded, "skipped_this_run": skipped}
    finally:
        con.close()


# ---------- KNN 查询 ----------

_SELECT_MSGS = ("SELECT " + (
    messages.column_list("id", "msg_key", "chat_id", "shard", "local_id", "ts",
                         "msg_type", "sender_id", prefix="m.")
    + f", {store._SENDER_EXPR} AS sender_name, "
    + messages.column_list("is_self", "content", "media_hint", prefix="m."))
    + f" FROM messages m {store._SENDER_JOIN} WHERE m.id IN ({{ph}})")


def semantic_search(query: str, chat_id: str | None = None, limit: int = 10,
                    account: str = "", embedder: FastEmbedEmbedder | None = None) -> dict:
    """KNN 语义检索。account 必传（多账号隔离，与 search.search 规则一致）。

    先 KNN 取 rowid 与 distance，再单独查 messages —— 不用 JOIN+LIMIT
    （sqlite-vec issue #96：JOIN 与标准 LIMIT 子句在 KNN 下不兼容）。
    """
    if not account:
        raise SearchInputError(
            "semantic_search 需要显式 account（多账号索引隔离）",
            hint="传入目标账号目录名；MCP/CLI 层会先解析当前登录账号")
    con = connect()
    try:
        if limit < 1:
            raise SearchInputError("limit 必须为正整数", hint="limit 应为 ≥1 的整数")
        # 顺序要紧：先判模型是否匹配，再判是否为空——不匹配的索引一律不得查询。
        # 判据必须模型无关（`_meta()` 按 MODEL_ID 过滤，异模型的行对它不可见，
        # 用它会让本分支永不可达），且必须看「是否存在任何异模型行」而非「最新一行」；
        # 该判据与错误文案由 _ensure_model_ok 独占（与 build_index 同源）。
        _ensure_model_ok(con)
        meta = _meta(con)
        if meta is None or meta["embedded"] == 0:
            raise VectorIndexEmpty("向量索引为空", hint="请先构建向量语义索引")
        k = min(limit * 3, K_MAX)          # 多取候选，过滤后再截断（limit ≥ 1 时 limit*3 ≥ limit）
        qvec = sqlite_vec.serialize_float32(
            list((embedder or default_embedder()).embed_query(query_text(query))))
        sql = ("SELECT msg_rowid, distance FROM vec_messages "
               "WHERE embedding MATCH ? AND k = ? AND account = ?")
        args: list = [qvec, k, account]
        if chat_id:
            # 特判 reader 的 "md5:<hex>" 退化 id（见 messages.chat_md5），
            # 否则该会话永远过滤不到任何消息（静默返回空）
            sql += " AND acct_chat = ?"
            args.append(messages.acct_chat(account, messages.chat_md5(chat_id)))
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
                         "media_hint": m["media_hint"], "msg_key": m["msg_key"],
                         "distance": r["distance"]})
        return {"mode": "semantic", "model": MODEL_ID,
                "total": len(hits[:limit]), "hits": hits[:limit]}
    finally:
        con.close()
