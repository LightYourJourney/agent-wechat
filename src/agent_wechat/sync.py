"""一次同步的编排与只读健康诊断（Phase 04）。

**同步是 CLI 专属的运维路径**（与 `purge` 同类，不进 MCP）：它把
「环境检查 → 消息增量同步（含历史回看）→ 向量脏队列 → 健康快照」串成一次可重复执行
的动作。并发用**全局文件锁**（非阻塞）：所有账号共用同一个 `index.db`，让两个账号并行
写库既没有收益又制造锁竞争，所以同一时刻只允许一个同步。

**日志只记计数**：`sync.log` 是 JSONL，每行
`{ts, account, status, exported, skipped, vector, duration_ms}`——**不写消息正文、
密钥、媒体路径**。清理用 CLI `agent-wechat logs clean --yes`（连 launchd 的 out/err 一起）。

**调度只写文件**：`schedule install` 生成 launchd plist 并落在真实家目录的
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
LAUNCHD_LOGS = ("sync.launchd.out.log", "sync.launchd.err.log")
LABEL = "com.agent-wechat.sync"

# 状态词汇表：**单一来源**（CLI/文档/测试都引用这些常量，避免字符串各写一遍）。
OK = "ok"
NO_NEW = "no_new"
WECHAT_NOT_RUNNING = "wechat_not_running"
ACCOUNT_NOT_FOUND = "account_not_found"
KEY_MISSING = "key_missing"
KEY_INVALID = "key_invalid"
SOURCE_UNAVAILABLE = "source_unavailable"
INDEX_UNAVAILABLE = "index_unavailable"
VECTOR_UNAVAILABLE = "vector_unavailable"
DISK_LOW = "disk_low"
DISK_ERROR = "disk_error"
CONCURRENT = "concurrent"
FAILED = "failed"
_OK_STATUSES = frozenset({OK, NO_NEW})


@dataclasses.dataclass
class SyncResult:
    account: str
    status: str
    started_at: int
    finished_at: int = 0
    exported: int = 0
    skipped: int = 0
    vector: dict | None = None
    health: dict | None = None
    degraded: list | None = None
    detail: str | None = None
    hint: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in _OK_STATUSES

    def to_dict(self) -> dict:
        out = {"account": self.account, "status": self.status,
               "started_at": self.started_at, "finished_at": self.finished_at,
               "exported": self.exported, "skipped": self.skipped, "ok": self.ok}
        if self.vector is not None:
            out["vector"] = self.vector
        if self.health is not None:
            out["health"] = self.health
        if self.degraded:
            out["degraded"] = self.degraded
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
    """**全局**非阻塞排他锁。被占用时抛 `SyncBusy`（不是故障，是并发）。"""
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


def _model_cache_ready() -> bool:
    """模型缓存里**真的有东西**（空目录不算可用）。

    `FastEmbedEmbedder._ensure()` 在失败路径上也会建目录，所以「目录存在」不能回答
    「模型可用」——这里看目录里有没有条目。
    """
    try:
        return any(vector.MODEL_CACHE_DIR.iterdir())
    except OSError:
        return False


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
    """把终态写进 `sync_state`。

    - **索引库不存在时不建库**（只读诊断同一条规矩）；
    - `concurrent` 不写：那次运行没做任何事，不该覆盖该账号上一次真实终态。
    """
    if result.status == CONCURRENT or not result.account:
        return
    if not store.DB_PATH.exists():
        return
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


def diagnose(account: str | None = None) -> dict:
    """只读健康诊断。**不建库、不加载扩展、不联网。**"""
    out: dict = {
        "account": account,
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
                   "model_matches": None, "model_cache": _model_cache_ready()},
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
        if account:
            out["source"]["shards"] = len(paths.message_shards(account))
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

    # **直接读注册表文件**，不走 `tasks.status()`：后者会顺手清扫陈旧 `.tmp`（写操作），
    # 而 doctor 承诺只读。这里只在内存里统计 running/failed。
    try:
        raw = json.loads(tasks.TASKS_FILE.read_text())
        recs = [r for r in raw.values() if isinstance(r, dict)]
        out["tasks"] = {
            "running": sum(1 for r in recs if r.get("status") == "running"
                           and paths.pid_alive(r.get("pid"))),
            "failed": sum(1 for r in recs if r.get("status") == "failed"),
        }
    except FileNotFoundError:
        pass
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
        try:
            v = migrations.version_if_table_exists(con, migrations.SCOPE_MAIN)
        except sqlite3.Error:
            v = None
        if v:
            out["schema"].update(version=v, pending=v < store.SCHEMA_VERSION)
            if v > store.SCHEMA_VERSION:
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
        out["dirty"] = store.reembed_queue_stats(con, account)
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
        if has("sync_state") and account:
            out["sync_state"] = store.load_sync_state(con, account)
    except (sqlite3.Error, OSError) as exc:
        out["problems"].append(f"索引库读取出错：{exc}")
    finally:
        con.close()
    return out


def _decrypt_status(exc: errors.DecryptError) -> str:
    """解密失败再分一层：**快照读不动**是源的问题，**解不开**才像密钥的问题。"""
    return (SOURCE_UNAVAILABLE if str(exc).startswith("无法快照微信数据库")
            else KEY_INVALID)


def _health_summary(info: dict) -> dict:
    """同步终态里保留的**计数摘要**（不带路径、不带错误正文）。"""
    return {
        "schema_version": info["schema"]["version"],
        "schema_pending": info["schema"]["pending"],
        "fts_desync": info["fts"]["desync"],
        "dirty_pending": info["dirty"]["pending"],
        "vector_indexed": info["vector"]["indexed"],
        "problems": len(info["problems"]),
    }


def _sync_vector(acct: str) -> dict:
    """向量阶段：没建过索引就跳过；建过则用**只读模型缓存**跑增量（不发网络请求）。"""
    try:
        with store.open() as con:
            ready = _vector_ready(con)
        if not ready:
            return {"skipped": True, "reason": "index_not_built"}
        res = vector.build_index(
            accounts=[acct],
            embedder=vector.FastEmbedEmbedder(local_files_only=True))
        return {"embedded": res["embedded_this_run"],
                "skipped_rows": res["skipped_this_run"]}
    except errors.AgentWeChatError as exc:
        # 索引库打不开 / 迁移失败 / 模型不可用：都算**向量阶段**失败——消息已经同步完了，
        # 归类成 vector_unavailable（部分成功），而不是把整次同步说成失败。
        return {"error": str(exc)}


def run_sync(account: str | None = None, *, full: bool = False,
             skip_vector: bool = False, now: int | None = None) -> SyncResult:
    """执行一次同步。任何可区分的前置/阶段失败都折成 `status`，不抛给调用方。"""
    started = int(now if now is not None else time.time())
    res = SyncResult(account=account or "", status=FAILED, started_at=started)
    stats: dict = {}
    res.degraded = []
    skipped_rows = {"n": 0}

    try:
        st = account_mod.status()
        if account is None and not st.running:
            raise _Abort(WECHAT_NOT_RUNNING,
                         "微信未运行，无法确定当前登录账号",
                         "打开并登录微信，或用 --account 显式指定本机已有账号")
        try:
            acct = account_mod.resolve_account(account)
        except errors.AccountNotFound as exc:
            raise _Abort(ACCOUNT_NOT_FOUND if account else WECHAT_NOT_RUNNING,
                         str(exc), "用 --account 指定本机已有账号，或先登录微信") from exc
        res.account = acct

        with _sync_lock():
            free = _free_bytes()
            if free and free < MIN_FREE_BYTES:
                raise _Abort(DISK_LOW,
                             f"磁盘可用空间不足（{free // (1024 * 1024)} MB < 200 MB）",
                             "清理磁盘后重试；同步会在写库前停止")
            try:
                keys.get_passphrase(acct)
            except errors.KeyNotFound as exc:
                raise _Abort(KEY_MISSING, str(exc),
                             "先完成一次性引导（见 INSTALL.md §5）") from exc

            # 降级与「挡下行数」不丢：导出只返回行数，漂移/来源不可读记在 reader 上。
            from . import usecases                 # 函数内导入：core 不静态依赖入口层

            try:
                res.exported = export.run_export(
                    acct, full=full,
                    on_reader=lambda r: res.degraded.extend(
                        {"capability": d.capability, "effect": d.effect}
                        for d in usecases.collect_degradations(r)),
                    on_skipped=lambda n: skipped_rows.__setitem__("n", n))
                res.skipped = skipped_rows["n"]
                stats.update(exported=res.exported, skipped=res.skipped)
            except errors.KeyNotFound as exc:
                raise _Abort(KEY_MISSING, str(exc), "重新引导") from exc
            except errors.DecryptError as exc:
                raise _Abort(_decrypt_status(exc), str(exc),
                             "若是密钥失效：重新 bootstrap 后重试；"
                             "若是源不可读：检查完全磁盘访问权限与容器路径") from exc
            except errors.DataNotFound as exc:
                raise _Abort(SOURCE_UNAVAILABLE, str(exc),
                             "确认微信数据目录存在、且具备完全磁盘访问权限") from exc
            except errors.SchemaMigrationError as exc:
                raise _Abort(INDEX_UNAVAILABLE, str(exc), exc.hint) from exc
            except errors.IndexUnavailable as exc:
                raise _Abort(INDEX_UNAVAILABLE, str(exc), exc.hint) from exc
            except errors.HomeUnavailable as exc:
                raise _Abort(DISK_ERROR, str(exc), exc.hint) from exc

            if not skip_vector:
                res.vector = _sync_vector(acct)
            else:
                res.vector = {"skipped": True, "reason": "skip_vector"}
            stats["vector"] = {k: res.vector[k] for k in
                               ("embedded", "skipped_rows", "skipped")
                               if k in res.vector}

            # 任务书 §2 的最后一步：健康快照（与 doctor 同源，只保留计数摘要）。
            health = diagnose(acct)
            res.health = _health_summary(health)
            stats["health"] = res.health

            res.status = (NO_NEW if res.exported == 0 and res.skipped == 0
                          and (res.vector or {}).get("embedded", 0) == 0
                          else OK)
            if (res.vector or {}).get("error"):
                res.status = VECTOR_UNAVAILABLE
                res.detail = res.vector["error"]
                res.hint = "消息已同步；向量索引失败见 doctor 的 vector/dirty 段"

    except _Abort as exc:
        res.status, res.detail, res.hint = exc.status, exc.detail, exc.hint
    except errors.SyncBusy as exc:
        res.status, res.detail, res.hint = CONCURRENT, str(exc), exc.hint
    except errors.AccountNotFound as exc:
        res.status, res.detail = ACCOUNT_NOT_FOUND, str(exc)
    except errors.AgentWeChatError as exc:
        res.status, res.detail, res.hint = FAILED, str(exc), exc.hint
    except OSError as exc:
        res.status, res.detail = DISK_ERROR, f"{type(exc).__name__}: {exc}"
    except Exception as exc:                 # noqa: BLE001 — 编排层兜底：未预期异常也要落终态
        res.status, res.detail = FAILED, f"{type(exc).__name__}: {exc}"
    finally:
        res.finished_at = int(now if now is not None else time.time())
        if res.account:
            _record(res, stats)
            _append_log({"ts": res.finished_at, "account": res.account,
                         "status": res.status, "exported": res.exported,
                         "skipped": res.skipped,
                         "vector": stats.get("vector", {}),
                         "duration_ms": max(0, (res.finished_at - res.started_at) * 1000)})
    return res


# ---------- 日志清理 ----------

def log_path() -> pathlib.Path:
    return paths.HOME_DIR / LOG_NAME


def clean_logs(*, yes: bool) -> dict:
    """删除同步日志与 launchd 的 out/err 日志。不加 `--yes` 只回报将要删什么。

    launchd 的两个日志装的是 `sync --json` 的信封（含 detail/hint），一并算作同步日志。
    """
    paths_ = [log_path(), *(paths.HOME_DIR / n for n in LAUNCHD_LOGS)]
    try:
        existing = [p for p in paths_ if p.exists()]
        size = sum(p.stat().st_size for p in existing)
        if not yes:
            return {"would_remove": len(existing), "bytes": size,
                    "files": [p.name for p in existing]}
        for p in existing:
            p.unlink()
        return {"removed": len(existing), "bytes": size,
                "files": [p.name for p in existing]}
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
        raise errors.SyncInputError(
            f"interval_minutes 至少 5 分钟（收到 {interval_minutes}）",
            hint="用 --interval-minutes 给一个 ≥5 的值（默认 360）")
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
    """写 plist 到真实家目录的 `~/Library/LaunchAgents/`。**不加载**（默认不启用）。"""
    dest = paths.launch_agents_dir() / f"{LABEL}.plist"
    try:
        paths.launch_agents_dir().mkdir(parents=True, exist_ok=True)
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
    dest = paths.launch_agents_dir() / f"{LABEL}.plist"
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
    dest = paths.launch_agents_dir() / f"{LABEL}.plist"
    uid = os.getuid()
    return {
        "plist": str(dest),
        "enable": f"launchctl bootstrap gui/{uid} {dest}",
        "kickstart": f"launchctl kickstart -k gui/{uid}/{LABEL}",
        "disable": f"launchctl bootout gui/{uid}/{LABEL}",
        # `doctor` 本来就恒输出 JSON，没有 `--json` 这个参数——印一个不存在的参数，
        # 用户照着抄会得到 `unrecognized arguments`（退出码 2）。
        "inspect": "agent-wechat doctor",
    }
