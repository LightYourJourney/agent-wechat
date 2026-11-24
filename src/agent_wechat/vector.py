"""向量语义检索：本地 embedding（fastembed/ONNX）+ sqlite-vec KNN。

隔离带：全部 fastembed 与 sqlite-vec 调用都收在本模块（上游 sqlite-vec 为
pre-v1，存在已知崩溃/内存泄漏 bug），便于回归测试与将来整体替换。
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import threading
import time
from collections.abc import Collection
from typing import Any, Callable, Mapping, Sequence

from . import errors, exclusions, messages, migrations, paths, store
from .errors import SearchInputError, VectorError, VectorIndexEmpty, redact


def _sqlite_vec() -> Any:
    """惰性载入 sqlite_vec：它会顺带载入 numpy，约占 MCP server 冷启动的一成，而只有
    语义检索与向量索引用得到它。"""
    import sqlite_vec
    return sqlite_vec

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
DIM = store.VECTOR_DIM                      # 单一真值在 store；本模块只做短别名
MODEL_ID = f"fastembed:{MODEL_NAME}:{DIM}"
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："   # BGE 官方 s2p 指令，仅查询侧加
MODEL_CACHE_DIR = paths.HOME_DIR / "models"   # 固定目录：不用 $TMPDIR（重启即清）
BATCH = 256                                  # 一次 flush（分页 + 排序窗口）的行数
# 送进 fastembed 的**推理**批大小，与 BATCH 分开（Phase 02）。此前整组 256 条作一个推理批：
# 组内按长度排了序，却仍被 padding 到组里最长的那条，注意力张量随之膨胀——真实数据均匀抽样
# 实测 14 条/s、峰值 RSS 6.0 GB；切成 8 条一批后 86–94 条/s、0.4–0.5 GB，向量逐条余弦 ≥ 0.9999998
# （数值等价，**已有索引不需要 rebuild**）。见 docs/audit/phase-02-integration-report.md §2.3。
EMBED_BATCH = 8
COMMIT_ROWS = 5120                           # 每 ~20 批一个事务：频繁 commit 会让插入掉 ~43%
K_MAX = 4096                                 # sqlite-vec 硬上限
DIRTY_BATCH = 64                             # 一轮最多消费多少条脏行
DIRTY_PREV_SCAN = 500                        # 回查「上一条可嵌入消息」的扫描上限
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
        _sqlite_vec().load(con)
    except Exception as e:
        con.close()
        raise VectorError(
            "无法加载 sqlite-vec 扩展（需要解释器支持 enable_load_extension）："
            f"{e}. uv 管理的 Python 3.12 已验证可用；换 python.org/conda 发行版需重新验证。"
        ) from e
    con.enable_load_extension(False)
    try:
        con.execute("SELECT vec_version()").fetchone()  # 探活：真正验证扩展可用
        # 先判版本、再写（与 store.connect 同一条纪律）：版本过新的库在任何 ensure 之前停。
        migrations.refuse_if_newer(con, scope=migrations.SCOPE_VECTOR, latest=1)
        # ensure：向量表每次连接都建（vec0 需要扩展在场，所以归属本模块而不是 store）。
        con.executescript(store.VECTOR_SCHEMA)
        # migrate：向量 schema 与主库**分开版本化**——没装扩展的机器上 main 迁移照常，
        # 这条只在真的加载了 sqlite-vec 时才有版本行。
        migrations.apply(con, scope=migrations.SCOPE_VECTOR, migrations=())
    except errors.SchemaMigrationError:
        con.close()
        raise
    except (sqlite3.Error, OSError) as exc:
        # A 矩阵：这两步此前裸穿（磁盘满 / 权限 / 库损坏都实测到过）。core 的契约是
        # 只抛项目异常——向量库进不去就没有降级可言，必须让调用方看见原因。
        con.close()
        raise VectorError(
            f"向量索引库不可用：{redact(str(exc))}",
            hint="确认数据目录可写、且有足够空间；仍失败可删除 index.db 后重建向量索引"
        ) from exc
    return con


def load(con: sqlite3.Connection) -> bool:
    """尽力把 sqlite-vec 装进一条**别人开的**连接。装上了 True，装不上 False，**不抛**。

    与 connect() 的分工：那个是 owner（开连接、建表、装不上就 fail-fast），本函数只做
    「把扩展注册到这条连接上」这一件事。存在的理由是 purge：`store.connect()` 从不加载扩展，
    而 purge 的 plan/execute/verify 要碰 `vec_messages`（装了索引的机器上，第一条
    `chat_row_counts` 就会被 `store._vec_exec` 翻成 PurgeRefused）。

    **best-effort 是刻意的**：轻装安装上根本没有向量表，扩展装不装无关紧要
    （`store.has_table` 自会跳过那几处）；装了索引而扩展真坏掉时，仍由 `store._vec_exec`
    响亮抛 PurgeRefused——那才是该报错的地方，本函数抢着报只会把「表在、扩展不在」变成
    「所有安装一律崩」。`enable_load_extension` 用完立刻关回去（finally），
    不让这条连接一直带着「可加载任意动态库」的权限。
    """
    try:
        con.enable_load_extension(True)
        try:
            _sqlite_vec().load(con)
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


def cache_dir_blocks_loading() -> bool:
    """模型缓存目录**存在但读不了**（权限 / 只读卷 / 坏挂载）——这种态不该回退下载。

    为什么需要它（EA-060，复审 F1 实测）：真 fastembed 把「缺缓存」与「缓存坏了」在内部压成
    同一个 `ValueError`，异常类型里没有可分辨的信息；唯一还看得到的是**目录本身**。

    目录不存在 → `False`（真缺失，回退下载是对的）；存在但列举失败 → `True`。
    """
    try:
        os.listdir(MODEL_CACHE_DIR)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return False


SEMANTIC_MISSING = "未安装语义检索组件（fastembed）"
SEMANTIC_HINT = ("语义检索是可选安装项：在项目目录跑 `uv sync --extra semantic`"
                 "（或 `pip install 'agent-wechat[semantic]'`）；不装时关键词检索照常可用")


def semantic_installed() -> bool:
    """fastembed 装没装（只查元数据，不导入——导入它要 ~1 s 且会拉起 onnxruntime）。"""
    return importlib.util.find_spec("fastembed") is not None


class FastEmbedEmbedder:
    """fastembed 懒加载封装：模型只在首次使用时载入（首次会下载 ~95MB 公共资产）。

    加锁：MCP 里建索引跑在后台线程、查询跑在主线程，共享一个实例。
    """

    def __init__(self, local_files_only: bool = False) -> None:
        self._local_only = local_files_only
        self._model = None
        self._lock = threading.Lock()

    def _ensure(self) -> Any:
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:
                # 语义检索是可选安装项（pyproject `[semantic]`）：没装就明确报「没装」，
                # 而不是让裸 ImportError 穿出去（检索侧会据此降级成只有关键词）。
                raise VectorError(SEMANTIC_MISSING, hint=SEMANTIC_HINT) from exc
            # **本模块是 `HOME_DIR/models` 的 owner，闸门就得在这里**（EA-003 的收口项）：
            # 此前它只靠 `store.open()` **间接**过关——相对 `AGENT_WECHAT_HOME` 那条确实会被
            # 拦住，但那是传递性保证：换一条不碰索引的入口（或将来把模型缓存挪到别处）就没了，
            # 而这里一 `mkdir` 就会把模型落到进程的当前工作目录。
            paths.ensure_home_dirs()
            try:
                MODEL_CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as exc:
                raise VectorError(
                    f"模型缓存目录不可用：{MODEL_CACHE_DIR}",
                    hint=f"{exc}；请检查 AGENT_WECHAT_HOME 指向的位置是否可写") from exc
            kwargs: dict[str, Any] = dict(model_name=MODEL_NAME, cache_dir=str(MODEL_CACHE_DIR))
            if not self._local_only:
                try:
                    # 默认先纯离线加载：模型已在本地时绝不发起网络请求（Global Constraint #4：
                    # 运行期不得发起任何网络请求）。只有首次（缓存缺失）才允许下载。
                    self._model = TextEmbedding(local_files_only=True, **kwargs)
                    return self._model
                except (ValueError, FileNotFoundError) as exc:
                    # EA-060（复审 F1 定稿）：**异常类型分辨不出「缺失」与「损坏」**——
                    # 真 fastembed 在 `model_management.download_model` 里两级
                    # `except Exception` 把「缺缓存 / 权限错 / 半份快照 / 坏文件」**统一压成
                    # 同一个 `ValueError`**（实测五类形态里四类如此），所以「按异常类型收窄」
                    # 在生产路径上等价于原来的宽接。判据换成**自己看缓存目录**：
                    if cache_dir_blocks_loading():
                        raise VectorError(
                            f"模型缓存目录读不了：{MODEL_CACHE_DIR}",
                            hint="这不是「缓存缺失」——先修权限/挂载，再重跑；"
                                 "回退下载会在同一个不可读目录上以更晦涩的错误失败",
                        ) from exc
                    pass                       # 目录不存在或读得了 → 按「缓存缺失」回退下载
            # 显式 local_files_only=True 的调用方要求「失败即报错」，不做下载回退。
            # 兜住 fastembed 的裸 ValueError：同步的向量阶段要把它翻成「向量不可用」，
            # 而不是一个看不出是模型问题的 ValueError。
            try:
                self._model = TextEmbedding(local_files_only=self._local_only, **kwargs)
            except Exception as exc:                    # noqa: BLE001 — 第三方异常统一翻译
                raise VectorError(
                    f"嵌入模型不可用：{exc}",
                    hint=f"模型缓存缺失或损坏；联网环境可先跑一次 vector-build 下载模型，"
                         f"离线环境请准备 {paths.HOME_DIR / 'models'}") from exc
        return self._model

    def embed(self, texts: list[str]) -> list[Sequence[float]]:
        with self._lock:
            return list(self._ensure().embed(texts, batch_size=EMBED_BATCH))

    def embed_query(self, text: str) -> Sequence[float]:
        with self._lock:
            return next(iter(self._ensure().query_embed([text])))


_default_embedder: FastEmbedEmbedder | None = None
_default_lock = threading.Lock()


def default_embedder() -> FastEmbedEmbedder:
    """模块级单例：MCP 常驻进程里避免每次查询重载模型。

    建实例要加锁：MCP SDK 把同步工具放进线程池执行，两个并发的首次调用会各建一个实例、
    各载一份模型（实例内的 `_lock` 只管得住同一个实例）。
    """
    global _default_embedder
    with _default_lock:
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
    try:
        con.commit()
    except (sqlite3.Error, OSError) as exc:
        raise store.index_unavailable(exc) from exc


def _write_meta(con: sqlite3.Connection, *, embedded: int, skipped: int) -> None:
    con.execute("UPDATE vector_meta SET embedded=?, skipped=?, built_at=? WHERE model_id=?",
                (embedded, skipped, int(time.time()), MODEL_ID))
    try:
        con.commit()
    except (sqlite3.Error, OSError) as exc:
        raise store.index_unavailable(exc) from exc


def _vector_watermark(con: sqlite3.Connection, account: str) -> int:
    row = con.execute("SELECT last_rowid FROM vector_watermark WHERE model_id=? AND account=?",
                      (MODEL_ID, account)).fetchone()
    return row[0] if row else 0


def _set_vector_watermark(con: sqlite3.Connection, account: str, rowid: int) -> None:
    # 写入值封顶在**该账号**的 `max(messages.id)`：扫描期间 `purge --chat` 删掉了最高的那几行
    # 时，扫到的 last_id 会高过库里现存的最大 id，而 messages.id 不是 AUTOINCREMENT——新消息
    # 复用这段 id、全部落在水位线之下，从此静默不被嵌入。必须按账号而不是全局：全局 max
    # 可能是别的账号的行，那个账号之后被销毁时 `store.delete_chat` 只回压它自己的水位线，
    # 本账号就停在一段会被复用的 id 上。扫描谓词按账号过滤，封顶到本账号 max 不会重嵌任何行。
    con.execute(
        """INSERT INTO vector_watermark(model_id, account, last_rowid, updated_at)
           VALUES (?,?, min(?, (SELECT COALESCE(max(id), 0) FROM messages WHERE account=?)), ?)
           ON CONFLICT(model_id, account) DO UPDATE SET
             last_rowid = max(last_rowid, excluded.last_rowid),
             updated_at = excluded.updated_at""",
        (MODEL_ID, account, rowid, account, int(time.time())))


# **一页一条完整查询**（keyset 分页），不是一个贯穿整轮的游标：活跃的读语句会把连接钉在
# 它开始时的快照上，此后别的连接（导出 / sync）只要提交过一次，本连接的写事务就拿不到锁
# （SQLITE_BUSY_SNAPSHOT，报 `database is locked`，且不等 busy timeout）——写入时对
# messages 的存活复查也会读到那个旧快照。`NOT INDEXED` 让它走 rowid 区间扫描、按 id 序
# 取满 LIMIT 即停；不加时规划器选 idx_msg_ts 再整段临时排序，每页都要 O(账号行数)。
_SELECT_PAGE = (
    "SELECT " + messages.column_list(
        "id", "msg_key", "account", "chat_md5", "chat_id", "ts", "local_id", "msg_type",
        "sender_id", "sender_name", "is_self", "content", "media_hint")
    + " FROM messages NOT INDEXED WHERE account = ? AND id > ? ORDER BY id LIMIT ?")

_INSERT_VEC = ("INSERT INTO vec_messages(msg_rowid, embedding, acct_chat, account, ts, preview) "
               "VALUES (?,?,?,?,?,?)")


def _alive_ids(con: sqlite3.Connection, pairs: list[tuple[int, str]]) -> set[int]:
    """`pairs` 里 (id, msg_key) **仍在 messages 里**的那些 id。分块查，避开变量数上限。"""
    alive: set[int] = set()
    want = dict(pairs)
    ids = list(want)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        alive.update(r[0] for r in con.execute(
            f"SELECT id, msg_key FROM messages WHERE id IN ({','.join('?' * len(chunk))})",
            chunk) if want.get(r[0]) == r[1])
    return alive


def _build_account(con: sqlite3.Connection, acct: str, embedder: FastEmbedEmbedder,
                   progress: Callable[[int], None] | None, *,
                   batch: int, commit_rows: int) -> tuple[int, int]:
    """单账号增量嵌入。返回 (embedded, skipped)。

    **嵌入在任何事务之外**：每批向量先攒在内存里（`commit_rows` 行约 10 MB），攒够了再开
    一个短写事务——复查存活、写向量、推水位线、提交，四步同一事务（崩溃后不会重复嵌入）。
    此前事务从第一次 INSERT 开到 `commit_rows` 行之后才提交，中间约 20 批 CPU 推理（按
    「68 万行 2.5–3 小时」折算每段约 70s）全程持写锁，并发的导出 / sync 等满 5s 就报
    `database is locked`。
    batch 同时是分页步长与单次嵌入的行数上限（默认取模块常量，由 build_index 注入）。
    """
    last_id = _vector_watermark(con, acct)
    # 续跑（水位线 > 0）时，每个会话本轮第一条的上文在水位线之下、不在内存里：回查一次，
    # 否则增量嵌入的那一条永远不带前缀，与全量重建的向量不一致。首次构建不查（之前没有东西）。
    resumed = last_id > 0
    prev_by_chat: dict[str, str | None] = {}
    buf_texts: list[str] = []
    buf_rows: list[tuple] = []
    pending: list[tuple] = []          # (msg_rowid, blob, acct_chat, ts, preview, msg_key)
    scanned: list[tuple[int, str]] = []   # 上次 write 之后扫过的**全部**行（含跳过的）：(id, msg_key)
    embedded = skipped = 0

    def flush() -> None:
        if not buf_rows:
            return
        # 长度排序后成批嵌入，压掉混合长度批次的 padding 浪费（spec §7；
        # 实测混长吞吐仅为等长的 1/5）。结果向量按原扫描顺序配对回去——
        # 单事务内行序无影响。
        order = sorted(range(len(buf_texts)), key=lambda i: len(buf_texts[i]))
        vecs = embedder.embed([buf_texts[i] for i in order])
        # EA-123：**维度自检**并在这里——嵌入模型与 `store.VECTOR_DIM`（DDL 用它建 vec0 表）
        # 是两个必须同步的常量，此前没有任何校验：换模型时失败面是循环中途的裸
        # `sqlite3.OperationalError`（vec0 表 512 维、插 768 维的行），且已经嵌了几批。
        # 放在 flush（所有路径的必经点、且在本批落盘**之前**）⇒ 第一批落盘前就抛
        # 项目异常。条目原本建议「起手单嵌一条常量串」——**不采纳**：那会给每个 batch 多出
        # 一次嵌入调用，污染所有按调用计数/顺序的假 embedder（实测 5 条既有用例同时红）。
        if vecs and len(vecs[0]) != store.VECTOR_DIM:
            raise VectorError(
                f"嵌入模型维度 {len(vecs[0])} 与索引库的 {store.VECTOR_DIM} 不一致："
                "换模型后必须重建索引",
                hint="换回建索引时的模型，或用 rebuild=True 重建（DDL 会按新维度建表）",
            )
        if len(vecs) != len(order):
            # EA-059：`zip(order, vecs)` 会**静默截断**——少返的向量对应的行不进索引，而
            # 账目照记（账目漂移 + 那些消息永远搜不到）。真实 fastembed 恒等长，这条防的是
            # 嵌入层被换/出错的形态。抛出即整轮终止（水位线停在上一次 commit 处，重跑可续）。
            # 文案（复审 F3）：**重跑**才是对的修法——抛在这里时水位线还没推进，不带 rebuild
            # 重跑会从上次提交处续跑；rebuild 会把已经嵌好的部分全部作废（2.5–3 小时）。
            raise VectorError(
                f"嵌入返回的向量数不足（{len(vecs)} != {len(order)}）：索引不完整。"
                "重跑一次即可从上次提交处续跑（不带 rebuild）；反复出现再用 rebuild 重建")
        # serialize_float32 注解成 List，实际只做 `pack(*v)`：numpy 数组照收
        pending.extend((buf_rows[i][0], _sqlite_vec().serialize_float32(v),  # pyright: ignore[reportArgumentType]
                        *buf_rows[i][1:]) for i, v in zip(order, vecs))
        buf_texts.clear()
        buf_rows.clear()

    def write(upto: int) -> None:
        nonlocal embedded
        con.execute("BEGIN IMMEDIATE")
        # **写入时复查消息还在**（同一个写事务）：本函数不登记任务，`purge --chat` 的闸门
        # 看不见它——嵌入期间被销毁的会话，其向量（preview 列是真正文）否则会被写回来。
        # 按 (id, msg_key) 对：messages.id 不是 AUTOINCREMENT，被删的 id 会被新消息复用。
        # 一次查完本段扫过的全部行（pending ⊆ scanned，同一对 (id, msg_key)）
        seen_alive = _alive_ids(con, scanned)
        alive = {r[0] for r in pending if r[0] in seen_alive}
        con.executemany(_INSERT_VEC, [(r[0], r[1], r[2], acct, r[3], r[4])
                                      for r in pending if r[0] in alive])
        # 扫过之后被删、id 又被新消息**复用**的行：水位线马上要推过这些 id，前向扫描再也看不见
        # 那条新消息（典型：嵌入期间 `purge --chat` 删掉了最高的 id，随后的导出复用了它们——
        # 封顶只看 max(id)，而复用之后 max(id) 又回来了）。投进重嵌队列，由 `apply_dirty` 按
        # 当前内容嵌（先删后插，不撞主键）。
        dead = [i for i, _ in scanned if i not in seen_alive]
        for i in range(0, len(dead), 500):
            chunk = dead[i:i + 500]
            for (key,) in con.execute(
                    f"SELECT msg_key FROM messages WHERE id IN ({','.join('?' * len(chunk))})",
                    chunk).fetchall():
                store.enqueue_reembed(con, key, reason="reused")
        scanned.clear()
        _set_vector_watermark(con, acct, upto)
        # 总行数与向量同一笔提交：`vector_meta.embedded` 此前只在整轮结束时写，首次构建中途
        # 被打断就一直是 0——`sync` 据此判「没建过索引」、从此跳过向量阶段，`doctor` 也只报「未建」，
        # 而那几小时已经嵌好的向量与水位线都在库里。
        con.execute("UPDATE vector_meta SET embedded = embedded + ? WHERE model_id = ?",
                    (len(alive), MODEL_ID))
        try:
            con.commit()
        except (sqlite3.Error, OSError) as exc:
            raise store.index_unavailable(exc) from exc
        embedded += len(alive)
        pending.clear()
        if progress:
            progress(embedded)

    while True:
        page = con.execute(_SELECT_PAGE, (acct, last_id, batch)).fetchall()
        if not page:
            break
        for row in page:
            last_id = row["id"]
            scanned.append((row["id"], row["msg_key"]))
            chat_key = messages.acct_chat(row["account"], row["chat_md5"])
            if resumed and chat_key not in prev_by_chat and should_embed(row):
                prev_by_chat[chat_key] = _prev_text(con, row)
            prev = prev_by_chat.get(chat_key)
            if should_embed(row):
                buf_texts.append(doc_text(row, prev))
                buf_rows.append((row["id"], chat_key, row["ts"], (prev or "")[:80],
                                 row["msg_key"]))
                # 只有「有意义的消息」才作为后一条的上文：否则媒体占位（[image] 之类）
                # 会污染前缀，让所有短消息都带上「上文: [image]…」的噪声
                prev_by_chat[chat_key] = display_text(row)
                if len(buf_rows) >= batch:
                    flush()
                    if len(pending) >= commit_rows:
                        write(last_id)       # 此刻 ≤ last_id 的行不是在 pending 里就是已跳过
            else:
                skipped += 1
    flush()
    write(last_id)
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

    `rebuild=True` 会清空索引**并重置本模型的水位线**（EA-043：重置是显式的一步，
    不靠 `VECTOR_DROP` 顺手做）——中断后重跑同一条 rebuild 命令要从头；续跑走不带 rebuild 的那条。
    """
    embedder = embedder or default_embedder()
    if isinstance(embedder, FastEmbedEmbedder) and not semantic_installed():
        # 先于 rebuild 的清表：没装组件时 `--rebuild` 会先把现有索引删光、再在第一次嵌入时失败
        raise VectorError(SEMANTIC_MISSING, hint=SEMANTIC_HINT)
    con = connect()
    try:
        if rebuild:
            # EA-043：进度指针由这里**显式**重置（`VECTOR_DROP` 不再顺手删它，见那里的注释）；
            # 只重置**本模型**的（水位线按 `(model_id, account)` 查，别的模型的留着无害）。
            # 复审 F2：清表与重置进度**必须同一笔事务**——否则「数据表已空、指针还停在旧
            # 进度」这一态可被 SIGKILL 命中并持久化，用户按新文档「不带 rebuild 重跑」会得到
            # 永远 0 进展的空索引。**实测**（本机 sqlite3）：`executescript` 逐条提交、
            # **不原子**（脚本中段出错时前面的 DROP 已落库；「放进同一个脚本」并不能解决），
            # 只有显式 `BEGIN; … COMMIT;` 才是一笔。`MODEL_ID` 是模块常量
            # （`fastembed:<模型>:<维度>`，无引号）⇒ 拼进脚本无注入面。
            try:
                con.executescript(
                    "BEGIN;\n"
                    + store.VECTOR_DROP
                    + f"\nDELETE FROM vector_watermark WHERE model_id='{MODEL_ID}';\n"
                    # 整表重建 = 回到未初始化态：版本行必须一起清掉，否则下一次
                    # `apply` 会认为 vector scope 已就绪、将来的迁移被跳过。
                    + f"\nDELETE FROM {migrations.VERSION_TABLE}"
                      f" WHERE scope='{migrations.SCOPE_VECTOR}';\n"
                    # 整表重建 = 所有消息都按当前内容重嵌一遍；旧队列与失败账本
                    # 随之作废（留着只会让下一轮重复嵌入）。
                    + "\nDELETE FROM pending_reembed;"
                      "\nDELETE FROM reembed_failures;\n"
                    + "COMMIT;")
                con.executescript(store.VECTOR_SCHEMA)
                migrations.apply(con, scope=migrations.SCOPE_VECTOR, migrations=())
                con.commit()
            except (sqlite3.Error, OSError) as exc:
                # 重建段是 DROP + 建表 + 提交三连：任一失败都必须以项目异常浮出，
                # 否则 CLI 直接 traceback（A 矩阵在 393/398/399 三处都实测到裸异常）。
                raise VectorError(
                    f"重建向量索引失败：{redact(str(exc))}",
                    hint="确认数据目录可写、磁盘有空间，且没有别的向量构建在跑"
                ) from exc
        # 换模型必须重建。不重建的两种真实失败形态（实测）：
        #   1) 模型常量变了 → 新模型的水位线从 0 起 → 重嵌 → 撞 vec_messages 主键（裸 OperationalError）
        #   2) 只改了 meta 行 → 水位线仍在 → 静默返回 {"embedded_this_run": 0}，索引停摆
        # 二者都不是「静默混用向量」，但都必须由用户显式 rebuild 才能继续。
        _ensure_model_ok(con)
        _ensure_meta(con)
        targets = (list(accounts) if accounts is not None else
                   [r[0] for r in con.execute("SELECT DISTINCT account FROM messages")])
        # 前向扫描之前的水位线：drain 用它判断「这一轮刚被前向嵌过」，避免重复嵌入。
        wm_before = {acct: _vector_watermark(con, acct) for acct in targets}
        # 同一时刻的队列快照：前向扫描**之后**才进队列（或又被改了一次）的行，扫描读到的可能是
        # 改动之前的正文——它们不算「本轮已按当前正文嵌过」（见 apply_dirty 的 `queued_before`）。
        queued_before = dict(con.execute("SELECT msg_key, changed_at FROM pending_reembed"))
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
        # 消费 Phase 02 投递的脏队列：历史改写只重嵌**改动的那几条**（外加后继一条
        # 因前缀依赖而过时的）。有界：一轮最多 4 批，剩下的留给下一次构建。
        if targets:
            for _ in range(4):
                dirty = apply_dirty(con, accounts=targets, embedder=embedder,
                                    covered_above=wm_before, queued_before=queued_before)
                embedded += dirty["embedded"]
                if dirty["embedded"] == 0 and dirty["dropped"] == 0 \
                        and dirty["deferred"] == 0:
                    break          # 队列已空（或只剩失败，留给下一轮/诊断）
        meta = _meta(con)       # 恒非空（开头 `_ensure_meta` 写过）；None 分支只为类型收窄
        prev_skipped = meta["skipped"] if meta is not None else 0
        total = con.execute("SELECT count(*) FROM vec_messages").fetchone()[0]
        _write_meta(con, embedded=total, skipped=prev_skipped + skipped)
        return {"embedded_this_run": embedded, "skipped_this_run": skipped}
    except sqlite3.Error as exc:
        # 增量段（建 meta、逐批写向量、推水位线）的裸 sqlite3 异常此前直接逃出 core：
        # 两条构建并发时后者拿到的是英文 `database is locked`，而 `sync._sync_vector`
        # 只接项目异常——整次同步被记成 failed 而不是 vector_unavailable。
        raise VectorError(
            f"向量索引写入失败：{redact(str(exc))}",
            hint="多半是另一条向量构建或同步正持有写锁——等它结束后重跑（不带 rebuild 会从"
                 "水位线续跑）；若没有别的任务在跑，确认数据目录可写、磁盘有空间") from exc
    finally:
        con.close()


# ---------- 脏队列消费（Phase 03） ----------

def _commit(con: sqlite3.Connection) -> None:
    """apply_dirty 的唯一提交出口：提交失败也要翻成项目异常（与 store 的出口同口径）。"""
    try:
        con.commit()
    except (sqlite3.Error, OSError) as exc:
        if con.in_transaction:
            con.rollback()
        raise store.index_unavailable(exc) from exc


def _is_excluded(con: sqlite3.Connection, msg_key: str) -> bool:
    """该消息是否已被 `purge --chat` 立碑（实现收口在 `exclusions.is_excluded`）。"""
    return exclusions.is_excluded(con, msg_key)


def _delete_vec(con: sqlite3.Connection, rowid: int) -> None:
    con.execute("DELETE FROM vec_messages WHERE msg_rowid=?", (rowid,))


def _write_vec(con: sqlite3.Connection, row: Mapping, vec: Sequence[float], *,
               preview: str) -> None:
    """单行向量写入：先删后插（vec0 的 UPDATE 语义不在我们依赖的契约里）。

    `preview` 与全量构建一致——存的是**上文前缀**（`_build_account` 的 `(prev or "")[:80]`），
    不是正文。两处不一致会让调试列给出两种含义。
    """
    _delete_vec(con, row["id"])
    con.execute(_INSERT_VEC, (
        row["id"], _sqlite_vec().serialize_float32(list(vec)),
        messages.acct_chat(row["account"], row["chat_md5"]),
        row["account"], row["ts"], preview[:80]))


def _prev_text(con: sqlite3.Connection, row: Mapping) -> str | None:
    """当前数据里，该消息之前最近一条**可嵌入**消息的正文（`doc_text` 的上文来源）。

    全量构建在内存里累积 prev；队列消费只有零散几条，必须回查。上限 `DIRTY_PREV_SCAN`：
    只有连续 500 条不可嵌入（媒体/系统）的会话才会取到 None——那时前缀本来也是空的。
    """
    # 按 chat_id 查走 `idx_msg_chat_ts`（chat_md5 上没有索引，按它查是整账号扫描——前向扫描续跑时
    # 每个会话都要查一次）。同一会话的退化名 `md5:` 与真名并存时只看本行那一种：取不到就是没前缀。
    cand = con.execute(
        "SELECT msg_type, content, ts, local_id FROM messages "
        "WHERE account=? AND chat_id=? AND (ts < ? OR (ts = ? AND local_id < ?)) "
        "ORDER BY ts DESC, local_id DESC LIMIT ?",
        (row["account"], row["chat_id"], row["ts"], row["ts"], row["local_id"],
         DIRTY_PREV_SCAN))
    for r in cand:                       # 逐行取：通常第一行就是答案，不必整段 fetchall
        if should_embed(r):
            return display_text(r)
    return None


def _next_embed_key(con: sqlite3.Connection, row: Mapping) -> str | None:
    """同一会话里**紧随其后**的第一条可嵌入消息。

    它的 `doc_text` 前缀来自这一条——所以本条被改写时，下一条的向量也过时了。
    级联只在消费时做一次（由下一条自己的队列行继续推进），不做递归。
    """
    cand = con.execute(
        "SELECT msg_key, msg_type, content, ts, local_id FROM messages "
        "WHERE account=? AND chat_md5=? AND (ts > ? OR (ts = ? AND local_id > ?)) "
        "ORDER BY ts, local_id LIMIT ?",
        (row["account"], row["chat_md5"], row["ts"], row["ts"], row["local_id"],
         DIRTY_PREV_SCAN)).fetchall()
    for r in cand:
        if should_embed(r):
            return r["msg_key"]
    return None


def _queued(con: sqlite3.Connection, key: str) -> tuple[int | None, str | None]:
    """队列行的 `(changed_at, reason)`；不在队列里时两者都是 None。"""
    row = con.execute("SELECT changed_at, reason FROM pending_reembed WHERE msg_key=?",
                      (key,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


# 只因**上一条**变了而进队列（`prefix`）的行，自己的正文没变——后继的上文前缀（`doc_text` 只取
# 上一条的正文）不受影响，不再级联。此前一律级联：改一条就把整段会话之后的每一条都重嵌一遍
# （每轮 4 批地推进，队列长期挂着一条）。其余原因照旧级联（保守：触发器的 ON CONFLICT 会用后来
# 的原因覆盖先前的，`sender` 可能盖住了 `content`）。
_NO_CASCADE_REASON = "prefix"


def _queued_keys(con: sqlite3.Connection, accounts: Sequence[str] | None,
                 limit: int) -> list[str]:
    if accounts is None:
        return store.queued_reembed(con, None, limit)
    keys: list[str] = []
    for acct in accounts:
        keys.extend(store.queued_reembed(con, acct, limit))
    return keys[:limit]


def apply_dirty(con: sqlite3.Connection, *, accounts: Sequence[str] | None = None,
                limit: int | None = None,
                embedder: FastEmbedEmbedder | None = None,
                covered_above: Mapping[str, int] | None = None,
                queued_before: Mapping[str, int] | None = None) -> dict:
    """消费最多 `limit` 条重嵌队列，返回 `{embedded, dropped, deferred, failed}`。

    判定顺序（每一支都决定「这条队列行还要不要」）：

    1. **消息不在索引里** → 队列行作废（`dropped`）。没有 rowid 可删——purge 已按
       `acct_chat` 删过向量。
    2. **在排除清单里** → 删向量、作废队列行（`dropped`）。被销毁的数据**不会**复活。
    3. **`id` 高于向量水位线** → 还没被嵌入过，下一次前向扫描会按当前正文嵌入；
       只作废队列行（`deferred`），不重复嵌入。
    4. **不再可嵌入**（类型变成图片、正文过短）→ 删向量、作废（`dropped`）。
    5. 其余：按**当前**内容重算 `doc_text`（含回查的上文前缀），批量嵌入一次，
       逐条「先删后插」并在**同一事务**里完成队列行删除；同时把**后继消息**入队
       （它的前缀依赖这一条）。

    `covered_above`：调用方（`build_index`）在**前向扫描之前**记下的水位线。给了它时，
    `id` 高于该值的行算「本轮前向扫描已经按当前正文嵌过」——只作废队列行、不再嵌一次。
    不给则按当前水位线判（独立调用/测试的语义）。`queued_before`：同一时刻的队列快照
    `{msg_key: changed_at}`；给了它时，扫描开始之后才进队列（或 `changed_at` 变了）的行
    不算嵌过，照常重嵌。

    嵌入失败时：整批记失败账本、保留队列行、提交失败记录，然后抛 `VectorError`——
    下一次运行会重试同一批，直到收敛。**不静默吞掉**。
    """
    limit = DIRTY_BATCH if limit is None else limit
    embedder = embedder or default_embedder()
    stats = {"embedded": 0, "dropped": 0, "deferred": 0, "failed": 0}
    todo: list[tuple[str, Mapping, str, str]] = []     # (key, row, text, preview)
    reasons: dict[str, str | None] = {}

    try:
        keys = _queued_keys(con, accounts, limit)
    except (sqlite3.Error, OSError) as exc:
        raise store.index_unavailable(exc) from exc

    try:
        for key in keys:
            row = con.execute(
                "SELECT id, msg_key, account, chat_md5, chat_id, ts, local_id, msg_type,"
                " sender_id, sender_name, is_self, content, media_hint"
                " FROM messages WHERE msg_key=?", (key,)).fetchone()
            if row is None or _is_excluded(con, key):
                if row is not None:
                    _delete_vec(con, row["id"])
                store.dequeue_reembed(con, key)
                stats["dropped"] += 1
                continue
            # 「这一行的向量是否已经按当前正文写过」：
            # ① 高于**当前**水位线 = 还没嵌过，下一次前向扫描按当前正文嵌；
            # ② 高于**前向之前**的水位线 = 本轮前向扫描刚嵌过——但只有改动早于扫描开始时才是
            #    当前正文：扫描途中进队列（或又被改了一次）的行，扫描读到的可能是旧正文，此前在这里
            #    被一并作废，那个向量就永远停在旧正文上（并发导出 + 向量构建即可触发）。
            cur_wm = _vector_watermark(con, row["account"])
            queued_at, reasons[key] = _queued(con, key)
            fresh = (covered_above is not None and row["account"] in covered_above
                     and row["id"] > covered_above[row["account"]]
                     and (queued_before is None or queued_before.get(key) == queued_at))
            if row["id"] > cur_wm or fresh:
                store.dequeue_reembed(con, key)
                stats["deferred"] += 1
                continue
            if not should_embed(row):
                _delete_vec(con, row["id"])
                store.dequeue_reembed(con, key)
                stats["dropped"] += 1
                continue
            prev = _prev_text(con, row)
            todo.append((key, row, doc_text(row, prev), prev or ""))
    except (sqlite3.Error, OSError) as exc:
        if con.in_transaction:
            con.rollback()
        raise store.index_unavailable(exc) from exc

    if todo:
        # 嵌入（CPU 推理）在任何事务之外：上面的作废/延后是写，会隐式开着一个写事务——带着它去
        # 推理，并发的导出 / purge 就要等满 busy timeout（同 `_build_account` 的「嵌入在任何事务之外」）。
        if con.in_transaction:
            _commit(con)
        try:
            vecs = embedder.embed([t for _, _, t, _ in todo])
        except Exception as exc:                       # noqa: BLE001 — 出口统一翻译
            reason = redact(str(exc))[:300]
            for key, _, _, _ in todo:
                store.record_reembed_failure(con, key, reason)
            _commit(con)
            stats["failed"] = len(todo)
            raise VectorError(
                f"重嵌失败（{len(todo)} 条已记录，队列保留）：{reason}",
                hint="确认模型可加载后重跑；失败次数见 CLI `agent-wechat vector-status`"
            ) from exc
        if len(vecs) != len(todo):
            for key, _, _, _ in todo:
                store.record_reembed_failure(con, key, "嵌入返回的向量数不足")
            _commit(con)
            raise VectorError(
                f"嵌入返回的向量数不足（{len(vecs)} != {len(todo)}）：本批未写入",
                hint="重跑一次即可（队列行还在）；反复出现请检查 embedder")
        if vecs and len(vecs[0]) != store.VECTOR_DIM:
            raise VectorError(
                f"嵌入模型维度 {len(vecs[0])} 与索引库的 {store.VECTOR_DIM} 不一致："
                "换模型后必须重建索引",
                hint="用 rebuild=True 重建（DDL 会按新维度建表）")
        try:
            # **写之前在写锁下复查**（与 `_build_account.write()` 同一判据）：上面那些读不在任何
            # 锁里，嵌入又要几秒——`purge --chat` 在这段窗口里删完的话，不复查就把被删会话的
            # 向量（`preview` 列是正文）写回去，此后没有任何东西会清它。按 (id, msg_key) 对判：
            # 消息没了的 id 可能已被新消息复用，所以只作废队列行、不碰那个 id 的向量。
            con.execute("BEGIN IMMEDIATE")
            alive = _alive_ids(con, [(row["id"], key) for key, row, _, _ in todo])
            live: list[tuple[tuple[str, Mapping, str, str], Sequence[float]]] = []
            for item, v in zip(todo, vecs):
                key, row = item[0], item[1]
                if row["id"] not in alive or _is_excluded(con, key):
                    if row["id"] in alive:
                        _delete_vec(con, row["id"])
                    store.dequeue_reembed(con, key)
                    stats["dropped"] += 1
                else:
                    live.append((item, v))
            for (key, row, _text, preview), v in live:
                _write_vec(con, row, v, preview=preview)
                store.dequeue_reembed(con, key)
                nxt = (_next_embed_key(con, row)
                       if reasons.get(key) != _NO_CASCADE_REASON else None)
                if nxt:
                    # 原因与触发器写的那几种并列：`prefix` = 自己的正文没变、
                    # 是前一条变了导致上文前缀过时。
                    store.enqueue_reembed(con, nxt, reason="prefix")
            _commit(con)
        except (sqlite3.Error, OSError) as exc:
            if con.in_transaction:
                con.rollback()
            raise store.index_unavailable(exc) from exc
        except BaseException:
            # 非 sqlite 的意外（将来某处抛 RuntimeError…）同样不许留半截事务。
            if con.in_transaction:
                con.rollback()
            raise
        stats["embedded"] = len(live)
    else:
        # 只有作废/延后时也要提交（dequeue 是写）
        _commit(con)
    return stats


def freshness(con: sqlite3.Connection, account: str | None = None) -> dict:
    """索引新鲜度：模型、水位线、队列深度、失败次数。**只读**（不建表、不加载扩展）。"""
    out: dict = {"model": MODEL_ID, "embedded": 0, "skipped": 0,
                 "watermark": {}, **store.reembed_queue_stats(con, account)}
    if store.has_table(con, "vector_meta"):
        row = con.execute(
            "SELECT embedded, skipped FROM vector_meta WHERE model_id=?",
            (MODEL_ID,)).fetchone()
        if row:
            out["embedded"], out["skipped"] = int(row[0]), int(row[1])
    if store.has_table(con, "vector_watermark"):
        sql = "SELECT account, last_rowid FROM vector_watermark"
        args: tuple = ()
        if account:
            sql += " WHERE account=?"
            args = (account,)
        out["watermark"] = dict(con.execute(sql, args).fetchall())
    return out


# ---------- KNN 查询 ----------

_SELECT_MSGS = ("SELECT " + (
    messages.column_list("id", "msg_key", "chat_id", "shard", "local_id", "ts",
                         "msg_type", "sender_id", prefix="m.")
    + f", {store._SENDER_EXPR} AS sender_name, "
    + messages.column_list("is_self", "content", "media_hint", prefix="m."))
    + f" FROM messages m {store._SENDER_JOIN} WHERE m.id IN ({{ph}})")


def semantic_search(query: str, chat_id: str | None = None, limit: int = 10,
                    account: str = "", embedder: FastEmbedEmbedder | None = None, *,
                    after_ts: int | None = None,
                    before_ts: int | None = None,
                    exclude_chats: Collection[str] = ()) -> dict:
    """KNN 语义检索。account 必传（多账号隔离，与 search.search 规则一致）。

    `exclude_chats`（`AGENT_WECHAT_DENY`）下推进 KNN：每个会话一条 `acct_chat != ?`。
    **不能写成 `NOT IN`**——实测 sqlite-vec 对分区键的 `NOT IN` 静默返回空集，`!=` 才对。

    先 KNN 取 rowid 与 distance，再单独查 messages —— 不用 JOIN+LIMIT
    （sqlite-vec issue #96：JOIN 与标准 LIMIT 子句在 KNN 下不兼容）。

    `after_ts` 含下界、`before_ts` 不含上界（Phase 05）：时间过滤在**取回候选之后**做
    （vec0 的 metadata 范围过滤不在我们依赖的契约里），被时间窗筛掉的不会补位，所以
    时间窗越窄、命中可能越少——调用方要更多结果就自己调大 `limit`。
    """
    if not account:
        raise SearchInputError(
            "semantic_search 需要显式 account（多账号索引隔离）",
            hint="传入目标账号目录名；MCP/CLI 层会先解析当前登录账号")
    # EA-073 的同源判据：`search` / `search_messages` 都拒空串，唯独这里没有——空的 query
    # 经 `query_text` 只剩 BGE 指令前缀，照样进 KNN，于是**有索引时**返回 k 条「最近邻」
    # 噪声、`total > 0`，用户以为搜到了东西。无索引时才碰巧返回空。
    if not query.strip():
        raise SearchInputError("检索串不能为空（或全空白）")
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
        qvec = _sqlite_vec().serialize_float32(
            list((embedder or default_embedder()).embed_query(query_text(query))))
        sql = ("SELECT msg_rowid, distance FROM vec_messages "
               "WHERE embedding MATCH ? AND k = ? AND account = ?")
        args: list = [qvec, k, account]
        if chat_id:
            # 特判 reader 的 "md5:<hex>" 退化 id（见 messages.chat_md5），
            # 否则该会话永远过滤不到任何消息（静默返回空）
            sql += " AND acct_chat = ?"
            args.append(messages.acct_chat(account, messages.chat_md5(chat_id)))
        for denied in exclude_chats:
            sql += " AND acct_chat != ?"
            args.append(messages.acct_chat(account, messages.chat_md5(denied)))
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
        if after_ts is not None:
            hits = [h for h in hits if h["ts"] >= after_ts]
        if before_ts is not None:
            hits = [h for h in hits if h["ts"] < before_ts]
        return {"mode": "semantic", "model": MODEL_ID,
                "total": len(hits[:limit]), "hits": hits[:limit]}
    finally:
        con.close()
