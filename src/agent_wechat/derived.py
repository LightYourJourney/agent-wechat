"""本地多模态派生文本与事实记忆（Phase 06）。

**只处理本机已有媒体、只调用本机引擎**：OCR 用 `tesseract`，ASR 用 `whisper-cli` +
用户自己准备的本地模型。**不下载模型、不联网、不上传任何图片/语音/文字**。
引擎缺失时明确报 `engine_unavailable`，不静默跳过、不伪装成功。

派生文本写 `derived_text`（按 `msg_key` 关联），**绝不覆盖 `messages.content`**。
用户在第二阶段看到的是原始消息；`derived_text` 只是可搜索的旁证。

删除语义：`delete` 清空正文并留 `derived_tombstone`；重扫遇到墓碑直接跳过——派生数据
不会因重扫复活。`purge --chat` 会连同派生文本、墓碑、事实证据一起删除，见 `store.delete_chat`。

事实记忆**不做自动抽取**：主张必须由调用方（Agent/用户）显式提交，初始状态恒为
`candidate`；只有用户能把它变成 `confirmed`。这样「模型候选」「用户确认」
「已撤回/有争议」三者在数据上真的分得开，而不是靠提示词约定。
"""
from __future__ import annotations

import contextlib
import fcntl
import os
from typing import Iterator
import pathlib
import shutil
import sqlite3
import subprocess
import time
import uuid

from . import errors, exclusions, media, messages, paths, store

MAX_FILE_BYTES = 20 * 1024 * 1024
MIN_FREE_BYTES = 50 * 1024 * 1024
MAX_AUDIO_SECONDS = 600
MAX_BATCH = 50
ENGINE_TIMEOUT = 60
LOCK_NAME = "derived.lock"
KINDS = ("ocr", "asr")
CLAIM_STATUSES = ("candidate", "confirmed", store.CLAIM_RETRACTED, "disputed")

# msg_type ↔ 派生种类 ↔ 媒体种类：**只有这一份映射**（矩阵/教程/离线路径都引用它）。
_KIND_BY_TYPE = {messages.TYPE_IMAGE: "ocr", messages.TYPE_VOICE: "asr"}
_MEDIA_KIND = {"ocr": "image", "asr": "voice"}

# 允许的状态迁移：candidate（模型/Agent 提出）→ 用户确认/撤回/标记争议；
# 撤回后用户可再次确认；争议最终要落到确认或撤回。任何状态都可以留在原状态。
_TRANSITIONS = {
    "candidate": {"confirmed", "retracted", "disputed"},
    "confirmed": {"retracted", "disputed"},
    "retracted": {"confirmed", "disputed"},
    "disputed": {"confirmed", "retracted"},
}

class TesseractOcr:
    """本机 `tesseract`。语言包按 `--list-langs` 实际有的选，不假设中文可用。"""

    name = "tesseract"

    def __init__(self) -> None:
        self._langs: str | None = None

    def available(self) -> bool:
        return shutil.which("tesseract") is not None

    def version(self) -> str | None:
        return _first_line(["tesseract", "--version"])

    def languages(self) -> str:
        if self._langs is None:
            out = _run(["tesseract", "--list-langs"])
            names = {line.strip() for line in (out or "").splitlines()[1:]}
            self._langs = "chi_sim+eng" if "chi_sim" in names else "eng"
        return self._langs

    def extract(self, path: pathlib.Path) -> tuple[str, float | None]:
        """TSV 输出同时给文字与逐词置信度；quality 是均值（0–1），没有词时为 None。"""
        out = _run(["tesseract", str(path), "stdout", "-l", self.languages(),
                    "tsv"])       # tesseract 的 TSV 是 configfile 参数，不是 --tsv
        if out is None:
            return None, None
        words: list[str] = []
        confs: list[float] = []
        for line in out.splitlines()[1:]:
            cols = line.split("\t")
            if len(cols) < 12:
                continue
            word = cols[11].strip()
            try:
                conf = float(cols[10])
            except ValueError:
                continue
            if word and conf >= 0:
                words.append(word)
                confs.append(conf)
        quality = round(sum(confs) / len(confs) / 100.0, 4) if confs else None
        return " ".join(words).strip(), quality


class WhisperCliAsr:
    """本机 `whisper-cli`（whisper.cpp）。**模型必须由用户事先放在本地**。"""

    name = "whisper-cli"

    def __init__(self, model: pathlib.Path | None = None) -> None:
        self.model = model or _default_whisper_model()

    def available(self) -> bool:
        return (shutil.which("whisper-cli") is not None
                and self.model is not None and self.model.exists())

    def version(self) -> str | None:
        return _first_line(["whisper-cli", "--help"])

    def extract(self, path: pathlib.Path) -> tuple[str, float | None]:
        out = _run(["whisper-cli", "-m", str(self.model), "-f", str(path),
                    "-l", "zh", "-nt"])
        # whisper-cli 在没有语音时会打印 `[BLANK_AUDIO]` 之类的标记，保留原样。
        return (out or "").strip(), None


def _first_line(argv: list[str]) -> str | None:
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=5,
                             check=False)
    except (OSError, UnicodeDecodeError, subprocess.SubprocessError):
        return None
    line = (res.stdout or res.stderr or "").strip().splitlines()
    return line[0].strip() if line else None


def _run(argv: list[str]) -> str | None:
    try:
        res = subprocess.run(argv, capture_output=True, text=True,
                             timeout=ENGINE_TIMEOUT, check=True)
        return res.stdout
    except (OSError, UnicodeDecodeError, subprocess.SubprocessError):
        return None


def _default_whisper_model() -> pathlib.Path | None:
    env = os.environ.get("AGENT_WECHAT_WHISPER_MODEL")
    if env:
        return pathlib.Path(env)
    for cand in ((paths.HOME_DIR / "models" / "ggml-base.bin",)
                 + paths.whisper_model_candidates()):
        if cand.exists():
            return cand
    return None


def default_ocr() -> TesseractOcr:
    return TesseractOcr()


def default_asr() -> WhisperCliAsr:
    return WhisperCliAsr()


@contextlib.contextmanager
def _lock() -> Iterator[None]:
    paths.ensure_home_dirs()
    lock = paths.HOME_DIR / LOCK_NAME
    try:
        f = open(lock, "a")
    except OSError as exc:
        raise errors.HomeUnavailable(
            f"无法建立派生锁：{exc}", hint="检查 AGENT_WECHAT_HOME 的权限") from exc
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise errors.DerivedBusy(
                "另一个派生任务正在运行", hint="等它结束再试") from exc
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()


def _free_bytes() -> int:
    try:
        return shutil.disk_usage(paths.HOME_DIR).free
    except OSError:
        return 0


def _commit(con: sqlite3.Connection) -> None:
    """提交派生/记忆写入；把裸 sqlite 错误翻成项目异常（core 契约）。"""
    try:
        con.commit()
    except (sqlite3.Error, OSError) as exc:
        raise errors.IndexUnavailable(
            f"派生数据写不进去：{exc}",
            hint="检查索引库权限与磁盘空间") from exc


def _probe_audio_seconds(path: pathlib.Path) -> float | None:
    """音频时长（秒）。没有 ffprobe 就返回 None——**跳过时长检查，不假装知道**。"""
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=10, check=False)
        return float(res.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def engine_status(ocr: TesseractOcr | None = None,
                  asr: WhisperCliAsr | None = None) -> dict:
    ocr, asr = ocr or default_ocr(), asr or default_asr()
    out = {}
    for kind, eng in (("ocr", ocr), ("asr", asr)):
        try:
            out[kind] = {"name": eng.name, "available": bool(eng.available()),
                         "version": eng.version() if eng.available() else None}
        except Exception as exc:                       # noqa: BLE001 — 探测失败按不可用
            out[kind] = {"name": eng.name, "available": False, "error": str(exc)}
    return out


def _candidates(con: sqlite3.Connection, account: str, chat: str | None,
                wants: set[str], limit: int, *,
                force: bool = False) -> list[sqlite3.Row]:
    where = ["m.account = ?"]
    args: list[object] = [account]
    wanted_types = []
    if "ocr" in wants:
        wanted_types.append(messages.TYPE_IMAGE)
    if "asr" in wants:
        wanted_types.append(messages.TYPE_VOICE)
    if not wanted_types:
        return []
    where.append(f"m.msg_type IN ({','.join('?' * len(wanted_types))})")
    args.extend(wanted_types)
    if chat:
        where.append("m.chat_id = ?")
        args.append(chat)
    if not force:
        # 已派生的消息**不再出现在候选里**：既让扫描能推进到更早的媒体，也保证
        # 用户纠正过的文本不会被下一次摄取覆盖（force 仍需逐行跳过 edited）。
        where.append(
            "NOT EXISTS (SELECT 1 FROM derived_text d WHERE d.msg_key = m.msg_key"
            " AND d.kind = CASE WHEN m.msg_type = ? THEN 'ocr' ELSE 'asr' END)")
        args.append(messages.TYPE_IMAGE)
    args.append(limit)
    return con.execute(
        "SELECT m.msg_key, m.account, m.chat_id, m.chat_md5, m.ts, m.local_id,"
        " m.msg_type, r.file_key FROM messages m"
        " LEFT JOIN media_refs r ON r.msg_key = m.msg_key"
        f" WHERE {' AND '.join(where)} ORDER BY m.ts DESC, m.local_id DESC LIMIT ?",
        args).fetchall()


def _kind_of(msg_type: int) -> str | None:
    return _KIND_BY_TYPE.get(msg_type)


def _media_path(con: sqlite3.Connection, row: sqlite3.Row,
                out_root: pathlib.Path) -> pathlib.Path | None:
    kind = _MEDIA_KIND.get(_kind_of(row["msg_type"]) or "", "")
    return media.exported_path(
        out_root, chat_md5=row["chat_md5"], kind=kind, ts=row["ts"],
        file_key=row["file_key"], local_id=row["local_id"])


def _insert(con: sqlite3.Connection, *, row: sqlite3.Row, kind: str, text: str,
            engine: str, version: str | None, source: pathlib.Path,
            quality: float | None, now: int) -> None:
    con.execute(
        "INSERT INTO derived_text(derivation_id, msg_key, account, chat_id, kind,"
        " text, engine, engine_version, source, quality, status, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?, 'auto', ?)"
        " ON CONFLICT(derivation_id) DO UPDATE SET"
        " text=excluded.text, engine=excluded.engine,"
        " engine_version=excluded.engine_version, source=excluded.source,"
        " quality=excluded.quality, status='auto', created_at=excluded.created_at"
        " WHERE derived_text.status != 'deleted'",
        (f"{row['msg_key']}|{kind}", row["msg_key"], row["account"], row["chat_id"],
         kind, text, engine, version, str(source), quality, now))


def ingest(account: str, chat: str | None = None, *,
           kinds: tuple[str, ...] = ("ocr", "asr"), limit: int = MAX_BATCH,
           ocr: TesseractOcr | None = None, asr: WhisperCliAsr | None = None,
           force: bool = False, allow_all: bool = False,
           now: int | None = None,
           out_root: pathlib.Path | None = None) -> dict:
    """对本机**已导出的**媒体跑本地识别；缺什么就如实记什么。"""
    wants = set(kinds)
    if not wants or not wants <= set(KINDS):
        raise errors.DerivedInputError(
            f"kinds 只能是 {KINDS}", hint="用 --kinds ocr,asr 指定")
    limit = max(1, min(int(limit), MAX_BATCH))
    if chat is None and not allow_all:
        raise errors.DerivedInputError(
            "需要显式 --chat（或 --all）",
            hint="按会话处理，避免无意中扫描全部联系人")
    ocr, asr = ocr or default_ocr(), asr or default_asr()
    engines = {"ocr": ocr, "asr": asr}
    out_root = out_root or media.export_root()
    result: dict = {"account": account, "chat": chat, "counts": {}, "engines": {},
                    "degraded": [], "skipped": {}}

    def hit(kind: str) -> None:
        result["counts"][kind] = result["counts"].get(kind, 0) + 1

    def skip(reason: str) -> None:
        result["skipped"][reason] = result["skipped"].get(reason, 0) + 1

    with _lock():
        if _free_bytes() < MIN_FREE_BYTES:
            raise errors.HomeUnavailable(
                "磁盘可用空间不足 50 MB，派生任务已停止",
                hint="清理磁盘后重试（识别结果要写索引库）")
        with store.open() as con:
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='derived_text'"
                           ).fetchone() is None:
                raise errors.DerivedInputError("派生表不存在（索引库版本过低）")
            for kind, eng in engines.items():
                try:
                    available = bool(eng.available())
                    version = eng.version() if available else None
                except Exception:                       # noqa: BLE001 — 探测失败按不可用
                    available, version = False, None
                result["engines"][kind] = {
                    "name": eng.name, "available": available, "version": version}
            rows = _candidates(con, account, chat, wants, limit * 4, force=force)
            for row in rows:
                if sum(result["counts"].values()) >= limit:
                    break
                kind = _kind_of(row["msg_type"])
                if kind is None or kind not in wants:
                    continue
                if con.execute("SELECT 1 FROM derived_tombstone"
                               " WHERE msg_key=? AND kind=?",
                               (row["msg_key"], kind)).fetchone():
                    skip("deleted")
                    continue
                if exclusions.is_excluded(con, row["msg_key"]):
                    # purge 的排除清单同样管派生：被销毁的消息不得再派生（rescan 不复活）
                    skip("excluded")
                    continue
                existing = con.execute(
                    "SELECT status FROM derived_text WHERE derivation_id=?",
                    (f"{row['msg_key']}|{kind}",)).fetchone()
                if existing is not None:
                    reason = {"edited": "edited", "deleted": "deleted"}.get(
                        existing["status"], "exists")
                    skip(reason)
                    continue
                path = _media_path(con, row, out_root)
                if path is None or not path.exists():
                    skip("media_not_exported")
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    skip("media_unreadable")
                    continue
                if size > MAX_FILE_BYTES:
                    skip("too_large")
                    continue
                if kind == "asr":
                    secs = _probe_audio_seconds(path)
                    if secs is not None and secs > MAX_AUDIO_SECONDS:
                        skip("too_long")
                        continue
                eng = engines[kind]
                try:
                    available = eng.available()
                except Exception:                      # noqa: BLE001 — 探测失败按不可用
                    available = False
                if not available:
                    skip("engine_unavailable")
                    continue
                try:
                    text, quality = eng.extract(path)
                except Exception as exc:               # noqa: BLE001 — 引擎失败不打断整批
                    result["degraded"].append(f"{kind} 引擎调用失败：{type(exc).__name__}")
                    skip("engine_failed")
                    continue
                if text is None:
                    skip("engine_failed")
                    continue
                if not text.strip():
                    skip("empty_output")      # 白图/静音：不落空行
                    continue
                _insert(con, row=row, kind=kind, text=text, engine=eng.name,
                        version=eng.version(), source=path, quality=quality,
                        now=int(now if now is not None else time.time()))
                _commit(con)
                hit(kind)

    if result["skipped"].get("engine_unavailable"):
        result["degraded"].append(
            "有媒体因本地识别引擎不可用而被跳过（OCR 需 tesseract；"
            "ASR 需 whisper-cli + 本地模型）")
    return result


def list_derived(account: str, *, chat: str | None = None, kind: str | None = None,
                 status: str | None = None, limit: int = 100) -> dict:
    limit = max(1, min(int(limit), 500))
    where = ["account = ?"]
    args: list[object] = [account]
    if chat:
        where.append("chat_id = ?")
        args.append(chat)
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if status:
        where.append("status = ?")
        args.append(status)
    args.append(limit)
    with store.open() as con:
        rows = con.execute(
            "SELECT derivation_id, msg_key, chat_id, kind, text, engine,"
            " engine_version, quality, status, created_at, edited_at"
            f" FROM derived_text WHERE {' AND '.join(where)}"
            " ORDER BY created_at DESC LIMIT ?", args).fetchall()
    return {"total": len(rows), "items": [dict(r) for r in rows]}


def _get(con: sqlite3.Connection, derivation_id: str) -> sqlite3.Row:
    row = con.execute(
        "SELECT * FROM derived_text WHERE derivation_id=?", (derivation_id,)
    ).fetchone()
    if row is None:
        raise errors.DataNotFound(
            f"找不到派生文本 {derivation_id}",
            hint="用 `agent-wechat derived list` 查看现有 derivation_id")
    return row


def get_derived(derivation_id: str) -> dict:
    """单条派生文本（含 source/engine/quality，供 `derived show` 核对来源）。"""
    with store.open() as con:
        return dict(_get(con, derivation_id))


def edit_derived(derivation_id: str, text: str, *, now: int | None = None) -> dict:
    if not text or not text.strip():
        raise errors.DerivedInputError(
            "纠正后的文本不能为空", hint="要删除请用 `derived delete`")
    with store.open() as con:
        row = _get(con, derivation_id)
        if row["status"] == "deleted":
            raise errors.DerivedInputError(
                "这条派生文本已被删除，不能编辑",
                hint="重新派生需要先清掉墓碑（`derived restore` 未提供，按设计不做）")
        con.execute(
            "UPDATE derived_text SET text=?, status='edited', edited_at=?"
            " WHERE derivation_id=?",
            (text, int(now if now is not None else time.time()), derivation_id))
        _commit(con)
    return {"derivation_id": derivation_id, "status": "edited"}


def delete_derived(derivation_id: str, *, now: int | None = None) -> dict:
    """清空正文、留墓碑。重扫不得复活。"""
    stamp = int(now if now is not None else time.time())
    with store.open() as con:
        row = _get(con, derivation_id)
        con.execute(
            "UPDATE derived_text SET text='', status='deleted', edited_at=?"
            " WHERE derivation_id=?", (stamp, derivation_id))
        con.execute(
            "INSERT INTO derived_tombstone(msg_key, kind, deleted_at)"
            " VALUES(?,?,?) ON CONFLICT(msg_key, kind) DO UPDATE SET"
            " deleted_at=excluded.deleted_at",
            (row["msg_key"], row["kind"], stamp))
        _commit(con)
    return {"derivation_id": derivation_id, "status": "deleted"}


def search_derived(query: str, *, account: str, chat_id: str | None = None,
                   limit: int = 20) -> dict:
    """在派生文本里检索，返回带 `msg_key` 的证据片段（供 retrieve 融合与 CLI 使用）。"""
    if not query or not query.strip():
        raise errors.DerivedInputError("检索串不能为空")
    limit = max(1, min(int(limit), 100))
    base = ("SELECT d.derivation_id, d.msg_key, d.chat_id, d.kind, d.text,"
            " d.engine, m.ts,"
            " COALESCE(dn.display_name, m.sender_name, m.sender_id) AS sender,"
            " m.is_self"
            " FROM derived_text d"
            " JOIN messages m ON m.msg_key = d.msg_key"
            f" {store._SENDER_JOIN}"
            " WHERE d.account = ? AND d.status != 'deleted'")
    args: list[object] = [account]
    if chat_id:
        base += " AND d.chat_id = ?"
        args.append(chat_id)
    with store.open() as con:
        rows: list[sqlite3.Row] = []
        if len(query.strip()) >= 3:
            try:
                rows = con.execute(
                    "SELECT d.derivation_id, d.msg_key, d.chat_id, d.kind, d.text,"
                    " d.engine, m.ts,"
                    " COALESCE(dn.display_name, m.sender_name, m.sender_id) AS sender,"
                    " m.is_self"
                    " FROM derived_fts JOIN derived_text d ON d.rowid = derived_fts.rowid"
                    " JOIN messages m ON m.msg_key = d.msg_key"
                    f" {store._SENDER_JOIN}"
                    " WHERE derived_fts MATCH ? AND d.account = ?"
                    " AND d.status != 'deleted'"
                    + (" AND d.chat_id = ?" if chat_id else "")
                    + " ORDER BY rank LIMIT ?",
                    (query, *args, limit)).fetchall()
            except sqlite3.OperationalError:
                rows = []       # FTS 表缺失/不可用：走 LIKE；其它 sqlite 错误照旧抛
        if not rows:                     # 1-2 字或 FTS 不可用：LIKE 兜底
            # 转义 `%`/`_`：与 search.py 同立场——查询里的通配符不该被当语法。
            needle = (query.strip().replace("\\", "\\\\")
                      .replace("%", "\\%").replace("_", "\\_"))
            try:
                rows = con.execute(
                    base + " AND d.text LIKE ? ESCAPE '\\' ORDER BY m.ts DESC LIMIT ?",
                    (*args, f"%{needle}%", limit)).fetchall()
            except (sqlite3.Error, OSError) as exc:
                raise errors.IndexUnavailable(
                    f"派生文本检索失败：{exc}",
                    hint="检查索引库完整性；必要时重跑一次导出") from exc
    return {"total": len(rows), "hits": [
        {"derivation_id": r["derivation_id"], "msg_key": r["msg_key"],
         "chat_id": r["chat_id"], "ts": r["ts"], "sender": r["sender"] or "?",
         "kind": r["kind"], "text": r["text"], "engine": r["engine"],
         "content": r["text"], "is_self": bool(r["is_self"]), "media_hint": None}
        for r in rows]}


# ---------------------------------------------------------------- 事实记忆

def add_claim(account: str, claim: str, *, evidence: list[str],
              chat_id: str | None = None, note: str | None = None,
              now: int | None = None) -> dict:
    """新增一条**候选**主张（绝不自动变成 confirmed）。证据至少一条。"""
    if not claim or not claim.strip():
        raise errors.DerivedInputError("主张内容不能为空")
    if not evidence:
        raise errors.DerivedInputError(
            "事实记忆必须有原始证据", hint="给至少一个 msg_key（来自检索结果）")
    evidence = list(dict.fromkeys(evidence))      # 重复 key 去重（PK 冲突会逃出项目异常）
    stamp = int(now if now is not None else time.time())
    with store.open() as con:
        rows = con.execute(
            "SELECT msg_key, chat_id FROM messages WHERE msg_key IN ("
            + ",".join("?" * len(evidence)) + ") AND account=?",
            (*evidence, account)).fetchall()
        found = {r["msg_key"] for r in rows}
        missing = [k for k in evidence if k not in found]
        if missing:
            raise errors.DataNotFound(
                f"证据 {missing[0]} 不在本账号的索引里",
                hint="先用 retrieve 找到证据的 msg_key；被 purge 的消息不能作为证据")
        chat_id = chat_id or rows[0]["chat_id"]
        claim_id = uuid.uuid4().hex
        con.execute(
            "INSERT INTO fact_claim(claim_id, account, chat_id, claim, status,"
            " note, created_at, updated_at) VALUES(?,?,?,?, 'candidate',?,?,?)",
            (claim_id, account, chat_id, claim.strip(), note, stamp, stamp))
        con.executemany(
            "INSERT INTO fact_evidence(claim_id, msg_key, role, created_at)"
            " VALUES(?,?, 'support', ?)",
            [(claim_id, k, stamp) for k in evidence])
        _commit(con)
    return get_claim(claim_id)


def get_claim(claim_id: str) -> dict:
    with store.open() as con:
        row = con.execute("SELECT * FROM fact_claim WHERE claim_id=?",
                          (claim_id,)).fetchone()
        if row is None:
            raise errors.DataNotFound(f"找不到主张 {claim_id}")
        ev = con.execute(
            "SELECT msg_key, role FROM fact_evidence WHERE claim_id=?"
            " ORDER BY created_at", (claim_id,)).fetchall()
    out = dict(row)
    out["evidence"] = [{"msg_key": e["msg_key"], "role": e["role"]} for e in ev]
    return out


def set_claim_status(claim_id: str, status: str, *, conflicts_with: str | None = None,
                     note: str | None = None, now: int | None = None) -> dict:
    if status not in CLAIM_STATUSES:
        raise errors.DerivedInputError(
            f"未知状态：{status}", hint=f"可用：{', '.join(CLAIM_STATUSES)}")
    stamp = int(now if now is not None else time.time())
    with store.open() as con:
        row = con.execute("SELECT status FROM fact_claim WHERE claim_id=?",
                          (claim_id,)).fetchone()
        if row is None:
            raise errors.DataNotFound(f"找不到主张 {claim_id}")
        current = row["status"]
        if status != current and status not in _TRANSITIONS.get(current, set()):
            raise errors.DerivedInputError(
                f"不能从 {current} 迁到 {status}",
                hint="允许：candidate→confirmed/retracted/disputed；"
                     "confirmed→retracted/disputed；retracted/disputed→confirmed")
        if conflicts_with:
            if conflicts_with == claim_id:
                raise errors.DerivedInputError("主张不能与自己冲突")
            if con.execute("SELECT 1 FROM fact_claim WHERE claim_id=?",
                           (conflicts_with,)).fetchone() is None:
                raise errors.DataNotFound(f"冲突主张 {conflicts_with} 不存在")
        con.execute(
            "UPDATE fact_claim SET status=?, conflicts_with=COALESCE(?, conflicts_with),"
            " note=COALESCE(?, note), updated_at=? WHERE claim_id=?",
            (status, conflicts_with, note, stamp, claim_id))
        _commit(con)
    return get_claim(claim_id)


def list_claims(account: str, *, status: str | None = None,
                limit: int = 50) -> dict:
    limit = max(1, min(int(limit), 200))
    where = ["account = ?"]
    args: list[object] = [account]
    if status:
        if status not in CLAIM_STATUSES:
            raise errors.DerivedInputError(f"未知状态：{status}")
        where.append("status = ?")
        args.append(status)
    args.append(limit)
    with store.open() as con:
        rows = con.execute(
            "SELECT * FROM fact_claim WHERE " + " AND ".join(where)
            + " ORDER BY updated_at DESC LIMIT ?", args).fetchall()
        ids = [r["claim_id"] for r in rows]
        ev: dict[str, list[str]] = {}
        if ids:
            for e in con.execute(
                    "SELECT claim_id, msg_key FROM fact_evidence WHERE claim_id IN ("
                    + ",".join("?" * len(ids)) + ") ORDER BY created_at", ids):
                ev.setdefault(e["claim_id"], []).append(e["msg_key"])
    return {"total": len(rows),
            "items": [dict(r, evidence=ev.get(r["claim_id"], [])) for r in rows]}


def delete_claim(claim_id: str) -> bool:
    with store.open() as con:
        row = con.execute("SELECT 1 FROM fact_claim WHERE claim_id=?",
                          (claim_id,)).fetchone()
        if row is None:
            return False
        con.execute("DELETE FROM fact_evidence WHERE claim_id=?", (claim_id,))
        con.execute("DELETE FROM fact_claim WHERE claim_id=?", (claim_id,))
        _commit(con)
    return True
