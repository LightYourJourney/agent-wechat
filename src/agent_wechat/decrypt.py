"""解密层：快照 + SQLCipher 读取。绝不写入微信目录。

流程：
1. 把 db+wal+shm 三元组快照到 ~/.agent-wechat/tmp/（0700）——微信运行在 WAL 模式，
   只拷主文件会缺最新数据；快照后我们拥有副本，后续操作不再触碰微信目录
2. passphrase -> PBKDF2 派生该库 enc_key -> 以 raw key 形式喂给 SQLCipher
   （PRAGMA key = "x'...'"，规避任意字节作为口令的转义问题；raw key 跳过 KDF，
   与我们自己的派生公式闭环，已由对照实验验证）
3. 两个后端（Phase 02）：
   - **进程内**（默认，`open_db`）：sqlcipher3 直接打开**加密快照**，查询只解密触及的页，
     **明文从不落盘**。`recent` 一类只读少量页的路径快一个数量级（Phase 01 实测 1,540 ms →
     12 ms / 237 MB 分片）。
   - **CLI**（`decrypted`）：`sqlcipher_export` 整库导出明文副本、yield 路径。保留给
     `decrypt --out`（它要的就是一份明文库文件），以及进程内后端不可用
     （没装 sqlcipher3 / `AGENT_WECHAT_DECRYPT=cli`）时 `open_db` 的回退。
   两个后端读出的内容逐字节一致（真机 36 个库 / 480 万行核对，见
   docs/audit/phase-01-native-poc-report.md §3.1）。退出时一律清理快照。
"""
from __future__ import annotations

import contextlib
import ctypes
import functools
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from typing import Any, Protocol

from . import errors, keys, paths
from .errors import DecryptError, HomeUnavailable

try:
    import sqlcipher3
    _HAS_SQLCIPHER3 = True
except (ImportError, OSError):    # 没装（如 Python ≥ 3.15 还没有 wheel）/ 动态库载不进来
    _HAS_SQLCIPHER3 = False

# 强制回退到 CLI 后端的开关（排障用：疑似 sqlcipher3 的问题时，用它对照确认）。
BACKEND_ENV = "AGENT_WECHAT_DECRYPT"


def _first_line(text: str) -> str:
    """stderr 的首个非空行（F3：折行回显块里的密钥残段不进消息，见 decrypted 的注释）。"""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


# `tmp/` 下**所有**带 pid 的临时目录（CLI 后端与语音解码会在里面放明文）。此前只认 `awdec-`（众数分片解密），于是
# 语音解码的 `awvoice-`（silk + 中间 pcm，成对明文）崩溃后残留**永不清扫**——同一类
# 残留，一类有兜底一类没有。两种前缀共用清扫判据。
_STALE_TMP_RE = re.compile(r"^(?:awdec|awvoice)-(\d+)-")


def tmp_dir() -> pathlib.Path:
    """解密临时目录（0700）。调用方负责确保存在（见 ensure_home_dirs）。"""
    return paths.HOME_DIR / "tmp"


_swept_stale = False          # 每进程只扫一次（EA-019）


def sweep_stale_tempdirs() -> int:
    """清扫 `tmp/` 下**没有活进程持有**的 `awdec-<pid>-*` / `awvoice-<pid>-*`，返回清掉的目录数（EA-019）。

    「用后即焚」此前只覆盖 `finally` 那条优雅路径：SIGKILL/崩溃时它不执行，MCP server 被
    客户端关闭时长任务在 **daemon 线程**里被解释器丢弃、`finally` 同样不保证执行——于是
    **分片明文**留在 `tmp/`（审计复现实测 8192 字节的 `plain.db`），而全仓没有任何清扫路径。

    **绝不无条件 `rmtree`**：多进程下会误删正在跑的任务的明文（`purge.py` 那句注释点过
    同款坑）。判据是 `os.kill(pid, 0)`：只有「确定已死」才清；判不出来（`PermissionError`
    等）一律留着。不带 pid 的目录名字一律不碰。
    """
    swept = 0
    try:
        entries = list(tmp_dir().iterdir())
    except OSError:
        return 0                       # tmp 读不了：清扫是尽力而为，绝不因此挡住解密
    for d in entries:
        m = _STALE_TMP_RE.match(d.name)
        if not m or paths.pid_alive(int(m.group(1))):
            continue
        try:
            shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue                   # 清扫是尽力而为：删不掉一个陈旧临时目录不该挡住解密
        # 判「真的没了」用 `lexists`：悬空符号链接与自指链接环下 `rmtree` 会拒绝执行、
        # 而 `exists()` 对它们返回 False——用它计数会把「没清掉」记成「清掉了」
        # （复验实测这两种形态各虚增 1）。`lexists` 对它们返回 True。
        if not os.path.lexists(d):
            swept += 1
    return swept


def ensure_home_dirs() -> None:
    global _swept_stale
    paths.ensure_home_dirs()
    try:
        tmp_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        # EA-005：同路径那处（paths.ensure_home_dirs）的立场——core 只抛项目异常。
        raise HomeUnavailable(
            f"解密临时目录不可用：{tmp_dir()}",
            hint=f"{exc}；请检查 AGENT_WECHAT_HOME 指向的位置是否可用") from exc
    if not _swept_stale:
        # 挂在「第一次解密之前」这条路径上（两个后端都经 `_keyed_snapshot` 先调本函数）：
        # 清扫必须发生在**新建**临时目录之前，否则刚建的目录会被自己扫掉。
        # flag 置在**清扫之后**（复审 N1）：清扫万一将来开始抛，本进程下次还会再试，
        # 而不是永久放弃清扫。
        sweep_stale_tempdirs()
        _swept_stale = True


def _load_clonefile() -> Any:
    try:
        fn = ctypes.CDLL(None, use_errno=True).clonefile
    except (OSError, AttributeError):        # 非 macOS / 老系统：没有 clonefile(2)
        return None
    fn.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32)
    fn.restype = ctypes.c_int
    return fn


_clonefile = _load_clonefile()


def _clone_or_copy(src: pathlib.Path, dst: pathlib.Path) -> None:
    """APFS 克隆（`clonefile(2)`，写时复制，与文件大小无关的常数时间），不行再普通复制。

    Phase 03 实测：快照是进程内后端读取的主要耗时——237 MB 分片 `copy2` 要 120–470 ms，
    克隆约 1 ms。克隆是**那一刻**文件内容的原子快照（比边读边拷更一致）；之后微信改它的文件、
    我们删自己的克隆，彼此不影响。克隆不了（跨卷：AGENT_WECHAT_HOME 不在微信所在的 APFS 卷上；
    非 APFS）就回退 `copy2`——源文件本身读不了的错误也由 `copy2` 照常抛出。
    """
    if _clonefile is not None and _clonefile(os.fsencode(src), os.fsencode(dst), 0) == 0:
        return
    shutil.copy2(src, dst)


# 主库在复制期间被改写时重拍的次数上限（见 `snapshot`）。克隆是毫秒级的，撞上 checkpoint
# 本身就罕见，连撞 5 次只会是微信正在持续大量写入（如首次同步聊天记录）。
SNAPSHOT_TRIES = 5


def _stamp(p: pathlib.Path) -> tuple[int, int, int] | None:
    try:
        st = p.stat()
    except FileNotFoundError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def snapshot(db: pathlib.Path, td: pathlib.Path) -> pathlib.Path:
    """db+wal+shm 三元组快照到私有目录。微信目录零写入（只读：克隆或复制）。

    三个文件是**逐个**克隆/复制的，不是一次原子快照：主库拷完、-wal 还没拷的那一刻微信若做了
    checkpoint（把 WAL 里的页写回主库，随后重启 WAL），拿到的就是「旧主库 + 新 WAL」。那份快照
    可能读出 malformed，也可能 `integrity_check` 通过、却**中间缺几行而其后的行都在**——增量
    导出随即把水位线推过那段空洞，前向扫描再也看不见它们（合成库复现：
    tests/test_decrypt_open_db.py 的 torn 用例）。checkpoint 必然写主库，所以判据是**主库在整个
    三元组复制的前后没变**（inode / 大小 / mtime）：变了就整组重拍；只有 WAL 被追加（没有
    checkpoint）时拍到的是一致的前缀，不必重拍。
    """
    dst = td / db.name
    try:
        for _ in range(SNAPSHOT_TRIES):
            before = _stamp(db)
            for suf in ("", "-wal", "-shm"):
                src = pathlib.Path(str(db) + suf)
                out = pathlib.Path(str(dst) + suf)
                out.unlink(missing_ok=True)     # 重拍：clonefile 不覆盖既有文件，上一轮的 -wal 也不能留
                if src.exists():
                    _clone_or_copy(src, out)
            if _stamp(db) == before:
                return dst
    except OSError as exc:
        # A 矩阵：快照那一步此前裸穿 PermissionError/ENOSPC（R2-A-007）。快照是解密的
        # 第一步，失败就整条走不通——按 core 契约只能是项目异常。
        raise errors.SourceUnreadable(
            f"无法快照微信数据库（只读复制失败）：{db.name}",
            hint=f"{exc}；请确认数据目录与 AGENT_WECHAT_HOME 都有空间、且可写"
        ) from exc
    raise errors.SourceUnreadable(
        f"无法快照微信数据库：{db.name} 在复制期间被反复改写（{SNAPSHOT_TRIES} 次）",
        hint="微信正在大量写入（如同步聊天记录）：等它空闲后重试")


@functools.lru_cache(maxsize=64)
def _derive_cached(passphrase: bytes, salt: bytes) -> bytes:
    """PBKDF2（256k 轮，每次 ~51 ms）按 (passphrase, salt) 进程内缓存。

    salt 是库的固有属性、passphrase 换了键就换了，所以不会拿旧钥匙开新库。MCP 常驻进程里
    同一个库每次工具调用都要重新派生——`recent` 一次就开 ~5 个库。缓存的只是进程内已有的
    passphrase 的确定性函数值，不新增任何落盘面。
    """
    return keys.derive_key(passphrase, salt)


@contextlib.contextmanager
def _keyed_snapshot(db: pathlib.Path, passphrase: bytes
                    ) -> Iterator[tuple[pathlib.Path, pathlib.Path, bytes]]:
    """两个后端共用的前半段：建私有临时目录 → 快照 → 读 salt → 派生密钥。
    yield (临时目录, 快照路径, enc_key)；退出时整个临时目录删掉。"""
    ensure_home_dirs()
    # 目录名带 **pid**（EA-019）：崩溃后的残留靠它判活（`sweep_stale_tempdirs`）。
    td = pathlib.Path(tempfile.mkdtemp(prefix=f"awdec-{os.getpid()}-", dir=tmp_dir()))
    try:
        snap = snapshot(db, td)
        # 只读第 1 页（盐在里面）：此前 `read_bytes()[:PAGE_SZ]` 把整个分片读进内存再切，
        # 几百 MB 的库就是几百 MB 的峰值（USAGE 宣称内存占用与库大小无关）。
        page1 = keys.read_page1(snap)
        if page1 is None:
            raise DecryptError(f"{db.name}: 快照读不出来，无法解密",
                               hint="确认 AGENT_WECHAT_HOME 下的 tmp/ 可读写后重试")
        if len(page1) < keys.PAGE_SZ:
            raise DecryptError(f"{db.name}: 文件不足一页，无法解密",
                               hint="该库可能正被微信初始化或已损坏；等微信同步完成后重试")
        yield td, snap, _derive_cached(passphrase, page1[:keys.SALT_SZ])
    finally:
        try:
            shutil.rmtree(td, ignore_errors=True)
        except OSError:
            pass                       # 临时目录清不掉不该盖过真正的失败，也不该自己冒出去


_KEY_MISMATCH_HINT = ("最常见的成因是密钥与库不匹配（微信重新登录过）：跑 "
                      "`agent-wechat status --verify-keys` 核对，不通过就重新 `agent-wechat bootstrap`")


@contextlib.contextmanager
def decrypted(db: pathlib.Path, passphrase: bytes) -> Iterator[pathlib.Path]:
    """CLI 后端：yield **整库明文副本**的路径，退出时自动清理临时文件。

    只有真要一个明文库文件的地方才用它（`decrypt --out`）；读数据走 `open_db`。
    """
    with _keyed_snapshot(db, passphrase) as (td, snap, enc_key):
        out = td / "plain.db"
        cmds = [
            f'PRAGMA key = "x\'{enc_key.hex()}\'";',
            f"ATTACH DATABASE '{out}' AS plain KEY '';",   # 必须绝对路径：ATTACH 相对路径按进程 CWD 解析
            "SELECT sqlcipher_export('plain');",
            "DETACH DATABASE plain;",
        ]
        # EA-053：命令走 **stdin**，密钥不再出现在 argv 里（另一本地用户/进程 `ps` 看得到
        # 的窗口消失）。/tmp 实测（合成库三态）：stdin 与 argv 的 rc/产物一致；错误文案从
        # 「Parse error in 3rd command line argument」变成「Parse error near line N」，
        # 而「回显出错语句 + 插入符」的机制两形态都在——errors.redact 仍是必需的安全网。
        try:
            # `timeout=` 必须给：不给的话下面那个 `except TimeoutExpired` **永远不可达**
            # （死分支），而 sqlcipher 一旦卡住（超大库、挂载/IO 卡死）`decrypted()` 会无限
            # 阻塞，连带 reader / 语音 / 贴纸回源全部挂死，用户没有任何退出手段。
            # 300 s 对「几百 MB 的库做一次 sqlcipher_export」有充足余量。
            r = subprocess.run(["sqlcipher", str(snap)], input="\n".join(cmds) + "\n",
                               capture_output=True, text=True, timeout=300)
        except FileNotFoundError as exc:
            raise errors.DependencyMissing(
                "找不到 sqlcipher 命令",
                hint="安装 sqlcipher 后重试（macOS: brew install sqlcipher）") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DecryptError(
                f"调用 sqlcipher 失败：{exc}",
                hint="确认 sqlcipher 可执行、磁盘与内存有空间后重试"
            ) from exc
        if r.returncode != 0 or not out.exists():
            # EA-047：**不在这里截断**——`[:300]` 曾把跨过截断点的密钥切成「前半段」，
            # 而 redact 需要完整 64 位才认得出（截断必须发生在脱敏**之后**，由
            # errors.redact 统一做）。
            # F3（T9 复审）：只取**首个非空行**。sqlcipher 报错时会回显出错语句并按 ~80 列
            # **折行**，第二处回显因此留下密钥的前缀（实测 62/64 位）——而 redact 只认「恰好
            # 64 位连续 hex」，残段会一路进对外信封。回显块正是藏残段的地方；首行
            # （`Parse error near line N: near "…"`）已带够定位信息。
            raise DecryptError(f"sqlcipher 解密失败: {_first_line(r.stderr)}",
                               hint=_KEY_MISMATCH_HINT)
        yield out


def open_plain(plain: pathlib.Path) -> sqlite3.Connection:
    """打开明文副本（只读模式）。"""
    try:
        return sqlite3.connect(f"file:{plain}?mode=ro", uri=True)
    except (sqlite3.Error, OSError) as exc:
        # A 矩阵 145：这处此前裸穿 OperationalError/DatabaseError/OSError。
        raise DecryptError(
            f"明文副本打不开：{plain.name}",
            hint=f"{exc}；解密产物可能不完整，重跑一次解密命令"
        ) from exc


# ---------- 进程内后端（Phase 02） ----------

class Rows(Protocol):
    """`ReadConnection.execute` 的返回：可迭代 + fetchone / fetchall。"""

    def __iter__(self) -> Iterator[Any]: ...
    def fetchone(self) -> Any: ...
    def fetchall(self) -> list[Any]: ...


class ReadConnection(Protocol):
    """`open_db` 给出的连接的**全部**契约（两个后端都满足：`sqlite3.Connection` 与
    `_CipherConnection`）。故意只有读路径用到的这几样——`with con:`、`cursor()`、
    `row_factory = sqlite3.Row`、`fetchmany` 进程内后端都不支持（或不翻译异常），
    写在契约外，pyright 就能在新调用方用到它们时报出来，而不是只在默认后端上运行期才炸。"""

    @property
    def text_factory(self) -> Any: ...
    @text_factory.setter
    def text_factory(self, value: Any) -> None: ...

    def execute(self, sql: str, parameters: Any = ..., /) -> Rows: ...


def backend() -> str:
    """`open_db` 实际会用的后端：`"inproc"`（默认）或 `"cli"`（回退）。

    回退只有两种成因：显式 `AGENT_WECHAT_DECRYPT=cli`，或 sqlcipher3 装不上 / 载不进来
    （例如 Python ≥ 3.15 还没有它的 wheel，依赖声明里就没装）。**进程内后端运行期出错不会
    自动改走 CLI**：密钥不对两条路一样失败，静默重试只会把真问题藏起来、白花一次整库导出。
    """
    if os.environ.get(BACKEND_ENV, "").strip().lower() == "cli" or not _HAS_SQLCIPHER3:
        return "cli"
    return "inproc"


def _translate(exc: BaseException) -> BaseException:
    """sqlcipher3 的异常 → 项目在明文时代面对的异常形态。

    `sqlcipher3.dbapi2.*Error` **不是** `sqlite3.Error` 的子类：不翻译的话，仓里所有
    `except sqlite3.Error`（读微信库的路径上就有十几处）会静默失效。规则：
    - 恰好是 `DatabaseError`（SQLITE_CORRUPT / NOTADB：页 HMAC 校验失败、快照撕裂）→
      `DecryptError`。明文时代这种库在 `sqlcipher_export` 那一步就以 DecryptError 失败了，
      进程内是**按页惰性**解密，同一个失败推迟到了查询中途——异常类型保持原来的口径。
    - 其余子类（OperationalError 之类）→ **同名**的 sqlite3 异常，与查明文副本时逐类一致。
    """
    if type(exc) is sqlcipher3.DatabaseError:
        return errors.SourceUnreadable(
            "读取微信库时页校验失败（页无法解密或已损坏）",
            hint="多半是快照那一刻微信正在写入：重试一次即可；反复出现请跑 "
                 "`agent-wechat status --verify-keys` 核对密钥")
    cls = getattr(sqlite3, type(exc).__name__, None)
    if not (isinstance(cls, type) and issubclass(cls, sqlite3.Error)):
        cls = sqlite3.Error
    return cls(*exc.args)


class _CipherCursor:
    """游标代理：迭代 / fetch 过程中的异常同样要翻译（页是在取行时才解密的）。"""

    __slots__ = ("_cur",)

    def __init__(self, cur: Any) -> None:
        self._cur = cur

    def __iter__(self) -> Iterator[Any]:
        it = iter(self._cur)
        while True:
            try:
                row = next(it)
            except StopIteration:
                return
            except sqlcipher3.Error as exc:
                raise _translate(exc) from exc
            yield row

    def _call(self, name: str) -> Any:
        try:
            return getattr(self._cur, name)()
        except sqlcipher3.Error as exc:
            raise _translate(exc) from exc

    def fetchone(self) -> Any:
        return self._call("fetchone")

    def fetchall(self) -> list[Any]:
        return self._call("fetchall")


class _CipherConnection:
    """只读连接代理：**只**暴露 `ReadConnection` 契约里的 `text_factory` / `execute`，外加
    `open_db` 自己用的 `close`，异常一律经 `_translate`。故意没有 `__getattr__` 透传：透传出去的
    方法（`executemany`、`cursor()`……）抛的是裸 sqlcipher3 异常，`except sqlite3.Error` 接不住。
    **不支持 `sqlite3.Row` 作 row_factory**（它只认 sqlite3 自己的游标）。"""

    __slots__ = ("_con",)

    def __init__(self, con: Any) -> None:
        self._con = con

    @property
    def text_factory(self) -> Any:
        return self._con.text_factory

    @text_factory.setter
    def text_factory(self, value: Any) -> None:
        self._con.text_factory = value

    def execute(self, sql: str, parameters: Any = (), /) -> _CipherCursor:
        try:
            return _CipherCursor(self._con.execute(sql, parameters))
        except sqlcipher3.Error as exc:
            raise _translate(exc) from exc

    def close(self) -> None:
        try:
            self._con.close()
        except sqlcipher3.Error as exc:   # 例如别的线程终结了持有它的生成器（跨线程关闭）
            raise _translate(exc) from exc


def _connect_encrypted(snap: pathlib.Path, enc_key: bytes) -> _CipherConnection:
    """打开**加密快照**。打开即校验：`PRAGMA key` 本身不验证密钥，第一次读页才会失败，
    所以这里立刻读一次 `sqlite_master`——密钥不对就在打开处抛 DecryptError，与 CLI 后端
    「sqlcipher 解密失败」同一个时点、同一个口径。

    只读打开（与 CLI 后端的 `open_plain` 同口径）：快照里还没 checkpoint 的 WAL 帧照常可见，
    快照缺 -shm 时也能读（用例覆盖）；任何写语句都被拒成 `sqlite3.OperationalError`。
    """
    try:
        raw = sqlcipher3.connect(f"file:{snap}?mode=ro", uri=True)
    except sqlcipher3.Error as exc:
        raise DecryptError(f"{snap.name}: 快照打不开（{type(exc).__name__}）",
                           hint="确认 AGENT_WECHAT_HOME 下的 tmp/ 可读写后重试") from None
    try:
        raw.execute(f"PRAGMA key = \"x'{enc_key.hex()}'\"")
        raw.execute("PRAGMA cipher_compatibility = 4")
        raw.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlcipher3.Error as exc:
        raw.close()
        # `from None`：异常链里不留 sqlcipher 的原文——PRAGMA key 那条语句带着密钥。
        raise DecryptError(f"{snap.name}: 解密失败（{type(exc).__name__}）",
                           hint=_KEY_MISMATCH_HINT) from None
    return _CipherConnection(raw)


# 同时持有微信库快照的**线程**上限。MCP 的同步工具跑在工作线程里：8 路并发时实测峰值 RSS
# 234 MB、tmp 里 1.9 GB 快照（Phase 03 验收 §2.3），而推理争的是同几颗 CPU，再多也不更快。
# 按线程计、同一线程内的嵌套打开不再占名额——否则 4 个线程各持一个、都在等第二个就死锁。
OPEN_DB_CONCURRENCY = 4
_open_db_slots = threading.BoundedSemaphore(OPEN_DB_CONCURRENCY)
_open_db_depth = threading.local()


@contextlib.contextmanager
def _open_db_slot() -> Iterator[None]:
    depth = getattr(_open_db_depth, "n", 0)
    if depth == 0:
        _open_db_slots.acquire()
    _open_db_depth.n = depth + 1
    try:
        yield
    finally:
        _open_db_depth.n = depth
        if depth == 0:
            _open_db_slots.release()


@contextlib.contextmanager
def open_db(db: pathlib.Path, passphrase: bytes) -> Iterator[ReadConnection]:
    """读微信库的统一入口：yield 一个只读连接，退出时关连接、删快照。

    默认进程内后端（明文不落盘、只解密触及的页）；不可用时回退 CLI 后端（整库导出明文副本，
    与 Phase 02 之前的行为逐字节相同）。两条路径上：快照失败 / 密钥不对 → DecryptError；
    查询中的 SQL 错误 → 对应的 sqlite3 异常。调用方按需设 `text_factory`，不必自己 close。
    同时打开的线程数受 `OPEN_DB_CONCURRENCY` 限制，超出的排队。
    """
    with _open_db_slot():
        with _open_db_unbounded(db, passphrase) as con:
            yield con


@contextlib.contextmanager
def _open_db_unbounded(db: pathlib.Path, passphrase: bytes) -> Iterator[ReadConnection]:
    if backend() == "cli":
        with decrypted(db, passphrase) as plain:
            con = open_plain(plain)
            try:
                yield con
            finally:
                con.close()
        return
    with _keyed_snapshot(db, passphrase) as (_td, snap, enc_key):
        con = _connect_encrypted(snap, enc_key)
        try:
            yield con
        finally:
            con.close()
