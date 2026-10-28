"""一次同步的编排与只读健康诊断（Phase 04）。

**同步是 CLI 专属的运维路径**（与 `purge` 同类，不进 MCP）：它把
「环境检查 → 消息增量同步（含历史回看）→ 向量脏队列 → 健康快照」串成一次可重复执行
的动作。并发用**每账号文件锁**（非阻塞）拒绝第二个同步者——不是靠任务注册表：
`sync` 是同步调用，没有后台线程可登记。

**日志只记计数**：`sync.log` 是 JSONL，每行
`{ts, account, status, exported, vector, duration_ms}`——**不写消息正文、密钥、
媒体路径**。清理用 CLI `agent-wechat logs clean --yes`。

**调度只写文件**：`schedule install` 生成 launchd plist 并落在
`~/Library/LaunchAgents/`，**从不调用 launchctl**；启用/停用命令打印给用户自己执行。
"""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import time

from . import account as account_mod
from . import errors, export, keys, migrations, paths, store, tasks, vector

MIN_FREE_BYTES = 200 * 1024 * 1024        # 同步前至少留 200 MB
LOCK_NAME = "sync.lock"
LOG_NAME = "sync.log"
LABEL = "com.agent-wechat.sync"
LAUNCH_AGENTS = pathlib.Path.home() / "Library" / "LaunchAgents"

@dataclasses.dataclass
class SyncResult:
    account: str
    status: str
    started_at: int
    finished_at: int = 0
    exported: int = 0
    vector: dict | None = None
    detail: str | None = None
    hint: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "no_new")

    def to_dict(self) -> dict:
        out = {"account": self.account, "status": self.status,
               "started_at": self.started_at, "finished_at": self.finished_at,
               "exported": self.exported, "ok": self.ok}
        if self.vector is not None:
            out["vector"] = self.vector
        if self.detail:
            out["detail"] = self.detail
        if self.hint:
            out["hint"] = self.hint
        return out


class _Abort(Exception):
    """内部分流：带状态与指引，由 `run_sync` 统一收成 SyncResult。"""

    def __init__(self, status: str, detail: str, hint: str | None = None):
        super().__init__(detail)
        self.status, self.detail, self.hint = status, detail, hint


@contextlib.contextmanager
def _sync_lock():
    """每账号非阻塞排他锁。被占用时抛 `SyncBusy`（不是故障，是并发）。"""
    paths.ensure_home_dirs()
    lock = paths.HOME_DIR / LOCK_NAME
    try:
        f = open(lock, "a")
    except OSError as exc:
        raise errors.HomeUnavailable(
            f"无法建立同步锁：{exc}",
            hint="检查 AGENT_WECHAT_HOME 的权限与磁盘空间") from exc
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise errors.SyncBusy(
                "另一个同步正在运行",
                hint="等它结束后重试；终态可用 `agent-wechat doctor` 查看") from exc
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


def _free_bytes() -> int:
    try:
        return shutil.disk_usage(paths.HOME_DIR).free
    except OSError:
        return 0


def _vector_ready(con: sqlite3.Connection) -> bool:
    """向量索引是否已存在（存在才有「处理脏队列」这一步；不存在不算故障）。"""
    if not store._has_table(con, "vector_meta"):
        return False
    row = con.execute("SELECT embedded FROM vector_meta WHERE model_id=?",
                      (vector.MODEL_ID,)).fetchone()
    return bool(row and int(row[0]) > 0)


def _append_log(rec: dict) -> None:
    """JSONL 一行，只含计数与状态。文件 0600（含账号标识，不含内容）。"""
    path = paths.HOME_DIR / LOG_NAME
    try:
        new = not path.exists()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        if new:
            os.chmod(path, 0o600)
    except OSError:
        pass          # 日志写不进去不该让同步失败


def _record(result: SyncResult, stats: dict | None = None) -> None:
    """把终态写进 `sync_state`（失败也不影响结果返回——诊断面不是数据面）。"""
    if not store.DB_PATH.exists():
        return          # 没有索引库就不为一条失败记录去建一个（只读诊断同一条规矩）
    try:
        with store.open() as con:
            store.record_sync_state(
                con, result.account, started_at=result.started_at,
                finished_at=result.finished_at, status=result.status,
                error=result.detail,
                stats=json.dumps(stats or {}, ensure_ascii=False, sort_keys=True))
            con.commit()
    except (sqlite3.Error, OSError, errors.AgentWeChatError):
        pass


def _diagnose(acct: str | None) -> dict:
    """只读诊断的主体（调用方保证不建库）。"""
    out: dict = {
        "account": acct,
        "wechat": None,
        "key": {"cached": keys.CACHE_FILE.exists()},
        "index": {"exists": store.DB_PATH.exists()},
        "schema": {"latest": store.SCHEMA_VERSION, "version": None, "pending": None},
        "fts": {"messages": None, "indexed": None, "triggers_ok": None, "desync": None},
        "watermarks": {"rows": None, "accounts": None},
        "audit": {"cursors": None},
        "dirty": {"pending": None, "failed": None, "max_attempts": None,
                  "by_reason": {}},
        "vector": {"indexed": False, "model": vector.MODEL_ID, "embedded": 0,
                   "model_matches": None, "model_cache": vector.MODEL_CACHE_DIR.exists()},
        "tasks": {"running": 0, "failed": 0},
        "sync_state": None,
        "permissions": {"home_mode": None, "private": None},
        "disk": {"free_bytes": None},
        "source": {"container": None, "accounts": None, "shards": None},
        "problems": [],
    }
    st = account_mod.status()
    out["wechat"] = {"running": st.running, "current": st.current,
                     "accounts": len(st.accounts)}
    try:
        out["source"]["accounts"] = len(paths.account_dirs())
        if acct:
            out["source"]["shards"] = len(paths.message_shards(acct))
    except errors.AgentWeChatError as exc:
        out["problems"].append(f"源目录读不了：{exc}")
    out["source"]["container"] = paths.XWECHAT_FILES.exists()

    try:
        stat = os.stat(paths.HOME_DIR)
        mode = stat.st_mode & 0o777
        out["permissions"] = {"home_mode": oct(mode), "private": mode & 0o077 == 0}
        if not out["permissions"]["private"]:
            out["problems"].append("数据目录权限不是 0700（其它用户可读）")
    except OSError:
        out["problems"].append("数据目录不存在或不可读")

    free = _free_bytes()
    out["disk"] = {"free_bytes": free}
    if free and free < MIN_FREE_BYTES:
        out["problems"].append("磁盘可用空间低于 200 MB")

    try:
        lst = tasks.status(None) or []
        out["tasks"] = {"running": sum(1 for t in lst if t.get("status") == "running"),
                        "failed": sum(1 for t in lst if t.get("status") == "failed")}
    except (OSError, ValueError):
        out["problems"].append("任务注册表读不出来")

    if not store.DB_PATH.exists():
        return out

    try:
        con = sqlite3.connect(f"file:{store.DB_PATH}?mode=ro", uri=True)
    except (sqlite3.Error, OSError) as exc:
        out["problems"].append(f"索引库打不开：{exc}")
        return out
    try:
        def has(name: str) -> bool:
            return con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,)).fetchone() is not None

        out["index"]["exists"] = True
        if has(migrations.VERSION_TABLE):
            row = con.execute(
                f"SELECT version FROM {migrations.VERSION_TABLE} WHERE scope=?",
                (migrations.SCOPE_MAIN,)).fetchone()
            v = int(row[0]) if row else None
            out["schema"].update(version=v, pending=(v is not None and v < store.SCHEMA_VERSION))
            if v is not None and v > store.SCHEMA_VERSION:
                out["problems"].append("索引库版本高于本程序（需升级程序或恢复备份）")
        if has("messages"):
            out["fts"]["messages"] = con.execute(
                "SELECT count(*) FROM messages").fetchone()[0]
        if has("messages_fts_docsize") and out["fts"]["messages"] is not None:
            out["fts"]["indexed"] = con.execute(
                "SELECT count(*) FROM messages_fts_docsize").fetchone()[0]
            out["fts"]["desync"] = (out["fts"]["indexed"] != out["fts"]["messages"])
            if out["fts"]["desync"]:
                out["problems"].append("FTS 索引与消息表行数不一致（重跑一次导出即可重建）")
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")}
        required = {*store.FTS_TRIGGER_NAMES, "msg_reembed_au"}
        out["fts"]["triggers_ok"] = required <= names
        if not out["fts"]["triggers_ok"]:
            out["problems"].append("FTS/重嵌触发器缺失")
        if has("watermark"):
            row = con.execute(
                "SELECT count(*), count(DISTINCT account) FROM watermark").fetchone()
            out["watermarks"] = {"rows": row[0], "accounts": row[1]}
        if has("audit_cursor"):
            out["audit"]["cursors"] = con.execute(
                "SELECT count(*) FROM audit_cursor").fetchone()[0]
        out["dirty"] = store.reembed_queue_stats(con, acct)
        if (out["dirty"].get("pending") or 0) > 0:
            out["problems"].append(
                f"重嵌队列有 {out['dirty']['pending']} 条积压（跑一次 vector-build 或 sync）")
        if (out["dirty"].get("failed") or 0) > 0:
            out["problems"].append(
                f"{out['dirty']['failed']} 条重嵌失败（见 attempts / last_error）")
        if has("vector_meta"):
            row = con.execute(
                "SELECT embedded FROM vector_meta WHERE model_id=?",
                (vector.MODEL_ID,)).fetchone()
            foreign = con.execute(
                "SELECT 1 FROM vector_meta WHERE model_id!=? LIMIT 1",
                (vector.MODEL_ID,)).fetchone()
            out["vector"].update(indexed=bool(row and int(row[0]) > 0),
                                 embedded=int(row[0]) if row else 0,
                                 model_matches=foreign is None)
            if foreign is not None:
                out["problems"].append("向量索引由其它模型构建（需 rebuild）")
        if has("sync_state") and acct:
            out["sync_state"] = store.load_sync_state(con, acct)
    except (sqlite3.Error, OSError) as exc:
        out["problems"].append(f"索引库读取出错：{exc}")
    finally:
        con.close()
    return out


def diagnose(account: str | None = None) -> dict:
    """只读健康诊断。**不建库、不加载扩展、不联网。**"""
    return _diagnose(account)


def run_sync(account: str | None = None, *, full: bool = False,
             skip_vector: bool = False, now: int | None = None) -> SyncResult:
    """执行一次同步。任何可区分的前置/阶段失败都折成 `status`，不抛给调用方。"""
    started = int(now if now is not None else time.time())
    res = SyncResult(account=account or "", status="failed", started_at=started)
    stats: dict = {}

    try:
        st = account_mod.status()
        if account is None and not st.running:
            raise _Abort("wechat_not_running",
                         "微信未运行，无法确定当前登录账号",
                         "打开并登录微信，或用 --account 显式指定本机已有账号")
        acct = account_mod.resolve_account(account)
        res.account = acct

        with _sync_lock():
            free = _free_bytes()
            if free and free < MIN_FREE_BYTES:
                raise _Abort("disk_low",
                             f"磁盘可用空间不足（{free // (1024 * 1024)} MB < 200 MB）",
                             "清理磁盘后重试；同步会在写库前停止")
            try:
                keys.get_passphrase(acct)
            except errors.KeyNotFound as exc:
                raise _Abort("key_missing", str(exc),
                             "先完成一次性引导（见 INSTALL.md §5）") from exc

            res.vector = {"skipped": True, "reason": "skip_vector"} if skip_vector else {}
            try:
                res.exported = export.run_export(acct, full=full)
                stats["exported"] = res.exported
            except errors.KeyNotFound as exc:
                raise _Abort("key_missing", str(exc), "重新引导") from exc
            except errors.DecryptError as exc:
                raise _Abort("key_invalid", str(exc),
                             "密钥缓存可能失效：重新 bootstrap 后重试") from exc
            except errors.DataNotFound as exc:
                raise _Abort("source_unavailable", str(exc),
                             "确认微信数据目录存在、且具备完全磁盘访问权限") from exc
            except errors.SchemaMigrationError as exc:
                raise _Abort("index_unavailable", str(exc), exc.hint) from exc
            except errors.IndexUnavailable as exc:
                raise _Abort("index_unavailable", str(exc), exc.hint) from exc
            except errors.HomeUnavailable as exc:
                raise _Abort("disk_error", str(exc), exc.hint) from exc

            if not skip_vector:
                res.vector = _sync_vector(acct)
                stats["vector"] = res.vector

            res.status = ("no_new" if res.exported == 0
                          and (res.vector or {}).get("embedded", 0) == 0
                          else "ok")
            if (res.vector or {}).get("error"):
                res.status = "vector_unavailable"
                res.detail = res.vector["error"]
                res.hint = "消息已同步；向量索引失败见 doctor 的 vector/dirty 段"

    except _Abort as exc:
        res.status, res.detail, res.hint = exc.status, exc.detail, exc.hint
    except errors.SyncBusy as exc:
        res.status, res.detail, res.hint = "concurrent", str(exc), exc.hint
    except errors.AccountNotFound as exc:
        res.status, res.detail = "wechat_not_running", str(exc)
    except errors.AgentWeChatError as exc:
        res.status, res.detail, res.hint = "failed", str(exc), exc.hint
    except OSError as exc:
        res.status, res.detail = "disk_error", f"{type(exc).__name__}: {exc}"
    except Exception as exc:                 # noqa: BLE001 — 编排层兜底：未预期异常也要落终态
        res.status, res.detail = "failed", f"{type(exc).__name__}: {exc}"
    finally:
        res.finished_at = int(now if now is not None else time.time())
        if res.account:
            _record(res, stats)
            vec_counts = {}
            if isinstance(res.vector, dict):
                vec_counts = {k: res.vector[k] for k in ("embedded", "skipped_rows", "skipped")
                              if k in res.vector}
            _append_log({"ts": res.finished_at, "account": res.account,
                         "status": res.status, "exported": res.exported,
                         "vector": vec_counts,
                         "duration_ms": max(0, (res.finished_at - res.started_at) * 1000)})
    return res


def _sync_vector(acct: str) -> dict:
    """向量阶段：没建过索引就跳过；建过则用**只读模型缓存**跑增量（不发网络请求）。"""
    if not store.DB_PATH.exists():
        return          # 没有索引库就不为一条失败记录去建一个（只读诊断同一条规矩）
    try:
        with store.open() as con:
            ready = _vector_ready(con)
    except (sqlite3.Error, OSError) as exc:
        return {"error": f"索引库不可用：{exc}"}
    if not ready:
        return {"skipped": True, "reason": "index_not_built"}
    try:
        res = vector.build_index(
            accounts=[acct],
            embedder=vector.FastEmbedEmbedder(local_files_only=True))
        return {"embedded": res["embedded_this_run"],
                "skipped_rows": res["skipped_this_run"]}
    except errors.VectorError as exc:
        return {"error": str(exc)}


# ---------- 日志清理 ----------

def log_path() -> pathlib.Path:
    return paths.HOME_DIR / LOG_NAME


def clean_logs(*, yes: bool) -> dict:
    """删除 `sync.log`。不加 `--yes` 只回报将要删什么。"""
    p = log_path()
    try:
        size = p.stat().st_size if p.exists() else 0
        if not yes:
            return {"would_remove": p.exists(), "bytes": size}
        removed = False
        if p.exists():
            p.unlink()
            removed = True
        return {"removed": removed, "bytes": size}
    except OSError as exc:
        raise errors.HomeUnavailable(
            f"日志清理失败：{exc}",
            hint="检查 AGENT_WECHAT_HOME 的权限") from exc


# ---------- launchd（只生成/安装文件，从不调用 launchctl） ----------

def _program() -> str:
    found = shutil.which("agent-wechat")
    if found:
        return str(pathlib.Path(found).resolve())
    return str(pathlib.Path(sys.executable).with_name("agent-wechat"))


def launchd_plist(*, account: str | None = None,
                  interval_minutes: int = 360) -> str:
    """用户级 LaunchAgent 的 plist 文本。`RunAtLoad=false`：登录时不自动跑。"""
    if interval_minutes < 5:
        raise ValueError("interval_minutes 至少 5 分钟")
    args = [_program(), "sync", "--json"]
    if account:
        args += ["--account", account]
    arg_xml = "".join(f"\n        <string>{a}</string>" for a in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>{arg_xml}
    </array>
    <key>StartInterval</key>
    <integer>{int(interval_minutes) * 60}</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{paths.HOME_DIR / "sync.launchd.out.log"}</string>
    <key>StandardErrorPath</key>
    <string>{paths.HOME_DIR / "sync.launchd.err.log"}</string>
</dict>
</plist>
"""


def schedule_install(*, account: str | None = None,
                     interval_minutes: int = 360) -> pathlib.Path:
    """写 plist 到 `~/Library/LaunchAgents/`。**不加载**（默认不启用）。"""
    dest = LAUNCH_AGENTS / f"{LABEL}.plist"
    try:
        LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
        dest.write_text(launchd_plist(account=account,
                                      interval_minutes=interval_minutes),
                        encoding="utf-8")
        os.chmod(dest, 0o600)
    except OSError as exc:
        raise errors.HomeUnavailable(
            f"无法写入 LaunchAgent：{exc}",
            hint="检查 ~/Library/LaunchAgents 的权限") from exc
    return dest


def schedule_uninstall() -> bool:
    dest = LAUNCH_AGENTS / f"{LABEL}.plist"
    try:
        if dest.exists():
            dest.unlink()
            return True
    except OSError as exc:
        raise errors.HomeUnavailable(
            f"无法移除 LaunchAgent：{exc}",
            hint="检查 ~/Library/LaunchAgents 的权限") from exc
    return False


def schedule_commands(account: str | None = None) -> dict:
    """启用/停用/查看的具体命令（打印给人执行，程序自己不跑）。"""
    dest = LAUNCH_AGENTS / f"{LABEL}.plist"
    uid = os.getuid()
    return {
        "plist": str(dest),
        "enable": f"launchctl bootstrap gui/{uid} {dest}",
        "kickstart": f"launchctl kickstart -k gui/{uid}/{LABEL}",
        "disable": f"launchctl bootout gui/{uid}/{LABEL}",
        "inspect": "agent-wechat doctor --json",
    }
