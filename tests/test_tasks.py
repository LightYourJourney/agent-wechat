"""通用长任务注册表：登记/进度/持久化/孤儿收割/跨进程新鲜度/失败不崩进程。"""
import json
import os
import threading
import time

import pytest

from agent_wechat import errors, tasks


def _simulate_restart():
    """模拟进程重启：丢掉内存视图。

    实现每次操作都从磁盘重读（F2，T7 复审），所以「重启」在本模块里只剩这一个动作——
    下一个 status/start/update 自然重读磁盘。
    """
    tasks._tasks.clear()


def _wait_done(tid, timeout=2.0):
    """等后台任务跑完并返回它的记录；超时即失败（别让测试挂死）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = tasks.status(tid)
        if st["status"] != "running":
            return st
        time.sleep(0.01)
    raise AssertionError(f"任务 {tid} 未在 {timeout}s 内结束")


def test_start_runs_runner_and_records_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    done = []

    def runner(tid):
        tasks.update(tid, rows=42)
        done.append(tid)
        return 42

    tid = tasks.start("export", runner, account="acct")
    for _ in range(200):                      # 后台线程，等待完成
        if done:
            break
        time.sleep(0.01)
    assert done == [tid]
    st = tasks.status(tid)
    assert st["kind"] == "export" and st["account"] == "acct"
    assert st["status"] == "done" and st["rows"] == 42
    assert json.loads((tmp_path / "tasks.json").read_text())[tid]["status"] == "done"


def test_runner_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")

    def boom(tid):
        raise RuntimeError("模型下载失败")

    tid = tasks.start("vector", boom)
    for _ in range(200):
        if tasks.status(tid)["status"] != "running":
            break
        time.sleep(0.01)
    st = tasks.status(tid)
    assert st["status"] == "failed" and "模型下载失败" in st["error"]


def test_status_lists_recent_and_unknown_id(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    with pytest.raises(errors.TaskNotFound):      # 未知 id 是错误，不是 dict
        tasks.status("nope")
    tasks.start("vector", lambda tid: 0)
    time.sleep(0.05)
    listed = tasks.status()
    assert isinstance(listed, list) and listed and "kind" in listed[0]


def test_status_list_tolerates_a_record_without_started_at(tmp_path, monkeypatch):
    """EA-081（在 T7 修轮落地：本次重写动了同一行）：外部写入的记录可能缺 `started_at`，
    排序不许裸 `KeyError` 把整条列表打挂——其余记录仍要答得出来（与「坏文件不能让注册表
    整体不可用」同一立场）。把排序键改回 `t["started_at"]`，第一条断言必红。
    """
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    (tmp_path / "tasks.json").write_text(json.dumps({
        "old_tool": {"id": "old_tool", "kind": "export", "status": "done", "rows": 3},
        "normal": {"id": "normal", "kind": "export", "status": "done", "rows": 1,
                   "started_at": int(time.time())},
    }))
    listed = tasks.status()
    assert {t["id"] for t in listed} == {"old_tool", "normal"}, listed
    assert [t["id"] for t in listed][0] == "normal", "排序把缺键的排在后面（0 兜底）"


# ---------- 持久化成真：重启后仍答得上来 ----------

def test_task_survives_process_restart(tmp_path, monkeypatch):
    """start() 落盘 → 内存清空 + 重新载入（= 重启）后照样查得到。"""
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    tid = tasks.start("export", lambda _tid: 7, account="acct")
    assert _wait_done(tid)["rows"] == 7

    _simulate_restart()
    st = tasks.status(tid)                        # 只能来自磁盘
    assert st["status"] == "done" and st["rows"] == 7 and st["account"] == "acct"
    assert [t["id"] for t in tasks.status()] == [tid]


def test_missing_or_corrupt_file_degrades_to_empty(tmp_path, monkeypatch):
    """坏文件不能让注册表不可用：缺失/非法 JSON/根不是对象，一律静默降级为空；
    **读**路径不写盘，坏文件原样留着（要修好它是下一次写的事，见 F4 用例）。"""
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "absent.json")
    assert tasks.status() == []                   # 文件不存在
    tid = tasks.start("vector", lambda _tid: 1)   # 登记照常
    assert _wait_done(tid)["rows"] == 1

    for bad in ("{ 这不是 json", "[1, 2]"):
        path = tmp_path / "broken.json"
        path.write_text(bad)
        monkeypatch.setattr(tasks, "TASKS_FILE", path)
        tasks._tasks.clear()                      # 内存视图也丢掉（= 重启后读到坏文件）
        assert tasks.status() == []
        assert path.read_text() == bad            # 读路径不写盘，坏文件原样留着


def test_a_corrupt_registry_does_not_disable_the_registry(tmp_path, monkeypatch):
    """F4（T7 复审）：`tasks.json` 坏掉时注册表必须**继续可用**——旧实现在载入失败路径
    清空内存表，于是 `start` 刚返回的 id 下一次调用就成「未知任务」，runner 收尾还会
    KeyError 崩掉；写入被永久冻结、坏文件永不自愈。

    修后：读失败保留内存视图、照常落盘（下一次写把坏文件修好）。把 `_load_locked`
    改成「读失败就清空」，第一条断言必红。
    """
    target = tmp_path / "tasks.json"
    monkeypatch.setattr(tasks, "TASKS_FILE", target)
    target.write_text("{ 坏文件")
    tasks._tasks.clear()

    tid = tasks.start("export", lambda _tid: 5, account="acct")
    assert tasks.status(tid)["status"] in ("running", "done"), \
        "start 刚返回的 id 必须查得到（不许变成「未知任务」）"
    st = _wait_done(tid)
    assert st["status"] == "done" and st["rows"] == 5, "runner 收尾不许 KeyError 崩掉"
    healed = json.loads(target.read_text())       # 下一次写把坏文件修好
    assert healed[tid]["status"] == "done"


def test_a_failed_read_keeps_the_in_memory_view(tmp_path, monkeypatch):
    """F4（T7 复审）的判别面：**读失败不许清空内存视图**。

    上面的「坏文件自愈」用例盖不住这一条——`start` 的第一次落盘就把坏文件修好了，
    后续读自然正常。真正区分的是「读不动 + 写也写不动」：此刻若把内存表清空，
    `start` 刚返回的 id 立刻变「未知任务」（旧实现在这里的形态）。把 `_load_locked`
    的「读失败保留视图」改成清空，第一条断言必红。
    """
    target = tmp_path / "tasks.json"
    monkeypatch.setattr(tasks, "TASKS_FILE", target)
    tid = tasks.start("export", lambda _tid: 1, account="acct")
    _wait_done(tid)

    real_replace = os.replace

    def boom(src, dst):
        raise OSError("EROFS")

    target.write_text("{ 坏文件")                  # 读不动
    monkeypatch.setattr(tasks.os, "replace", boom)  # 也写不动
    assert tasks.status(tid)["status"] == "done", \
        "读失败不许把内存里已有的记录清掉（清了就是「未知任务」）"
    monkeypatch.setattr(tasks.os, "replace", real_replace)


def test_a_leftover_runner_cannot_write_into_a_repointed_registry(tmp_path, monkeypatch):
    """F3（T6 复审的真实事故）：runner 的收尾/进度只能写**本注册表里存在**的记录。

    残留的 runner 线程越过用例 teardown（TASKS_FILE 已还原成真实路径）时，`_finish`
    重读磁盘会发现 id 不在——必须整个跳过。旧实现的下标/整表写回会把测试记录写进
    别人的注册表（真实事故：`~/.agent-wechat/tasks.json` 被三条合成记录整表替换，
    原记录不可找回）。把「不存在就跳过」改成 `setdefault`/下标，第二条断言必红。
    """
    here, there = tmp_path / "here.json", tmp_path / "there.json"
    monkeypatch.setattr(tasks, "TASKS_FILE", here)
    tid = tasks.start("export", lambda _tid: 1, account="acct")
    assert _wait_done(tid)["status"] == "done"

    canary = {"canary": {"id": "canary", "kind": "export", "status": "done",
                         "started_at": int(time.time())}}
    there.write_text(json.dumps(canary))
    monkeypatch.setattr(tasks, "TASKS_FILE", there)   # 还原成「真实路径」的等价物

    tasks._finish(tid, status="done", rows=99)        # 残留线程的收尾调用
    tasks.update(tid, rows=100)                       # 残留线程的进度调用
    assert json.loads(there.read_text()) == canary, "别人的注册表一个字节都不许动"


def test_concurrent_readers_never_see_a_partial_file(tmp_path, monkeypatch):
    """EA-014 的可观测判据（F3，T7 复审：此前那条用例是假护栏——把注入点放在任何 I/O
    之前，「直写目标」的实现在它眼里同样合法）。

    真判据：写者在跑，读者**每一次**都能 `json.loads`。旧实现直接 write_text（O_TRUNC
    后逐块写），V2 两进程探针实测 957 次坏读；把 `_persist_locked` 的 tmp+os.replace
    换回直写，本用例必红。
    """
    target = tmp_path / "tasks.json"
    monkeypatch.setattr(tasks, "TASKS_FILE", target)
    tid = tasks.start("export", lambda _t: 1, account="acct")
    _wait_done(tid)

    stop = threading.Event()
    failures = []

    def writer():
        for n in range(400):
            tasks.update(tid, rows=n)
        stop.set()

    def reader():
        while not stop.is_set():
            try:
                json.loads(target.read_text())
            except FileNotFoundError:
                pass                              # os.replace 的瞬时不可见窗口（不该出现，但不算坏读）
            except ValueError as e:
                failures.append(repr(e))

    w = threading.Thread(target=writer)
    r = threading.Thread(target=reader)
    w.start(); r.start()
    w.join(30); r.join(30)
    assert failures == [], f"读者看到了空/半截文件：{failures[:3]}"


def test_a_failed_write_keeps_the_old_file_and_leaves_no_tmp(tmp_path, monkeypatch):
    """写失败不许留半截、不许留 tmp 残骸。注入点选在 `os.replace`（真正的替换那一步）：
    非原子（直写目标）的实现在这里**已经改过目标文件**，必红（F3 的判据修正）。
    """
    target = tmp_path / "tasks.json"
    monkeypatch.setattr(tasks, "TASKS_FILE", target)
    tid = tasks.start("export", lambda _t: 1, account="acct")
    _wait_done(tid)
    before = target.read_text()

    real_replace = os.replace

    def boom(src, dst):
        raise OSError("ENOSPC")

    monkeypatch.setattr(tasks.os, "replace", boom)
    with pytest.raises(OSError):
        tasks.update(tid, rows=123)
    assert target.read_text() == before, "失败的那次写不许动到目标文件"
    assert list(tmp_path.glob("*.tmp")) == [], "失败不留 tmp 残骸"

    monkeypatch.setattr(tasks.os, "replace", real_replace)
    tasks.update(tid, rows=124)                   # 恢复后照常写
    assert json.loads(target.read_text())[tid]["rows"] == 124


# ---------- 孤儿收割：上次进程留下的 running ----------

def test_orphan_running_record_is_harvested_as_failed(tmp_path, monkeypatch):
    """载入时把遗留 running 标成 failed（枚举只有三值，不新增 interrupted）。"""
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    (tmp_path / "tasks.json").write_text(json.dumps({
        "dead": {"id": "dead", "kind": "export", "status": "running", "rows": 3,
                 "started_at": int(time.time()), "error": None},
        "live": {"id": "live", "kind": "vector", "status": "done", "rows": 9,
                 "started_at": int(time.time()), "error": None},
    }))
    _simulate_restart()

    st = tasks.status("dead")
    assert st["status"] == "failed"
    assert st["error"] == tasks.ORPHAN_ERROR
    assert any("一" <= c <= "鿿" for c in st["error"])   # 非空中文
    assert tasks.status("live")["status"] == "done"              # 已完成的不动

    # 读是读路径，不写盘：磁盘上那条仍是 running，等下一次写才落下
    assert json.loads((tmp_path / "tasks.json").read_text())["dead"]["status"] == "running"


# ---------- rows 的两个写入方 ----------

def test_rows_progress_then_final_value(tmp_path, monkeypatch):
    """运行中 update(rows=) 写进度（单调递增），跑完被 runner 返回值的终值覆盖。"""
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    seen = []

    def runner(tid):
        for n in (5, 9, 12):
            tasks.update(tid, rows=n)
            seen.append(tasks.status(tid)["rows"])
        return 99                                 # 终值

    st = _wait_done(tasks.start("export", runner))
    assert seen == [5, 9, 12]                     # 进度就写在这里
    assert seen == sorted(seen) and seen[-1] < st["rows"]
    assert st["status"] == "done" and st["rows"] == 99      # 终值覆盖进度值
    assert json.loads((tmp_path / "tasks.json").read_text())[st["id"]]["rows"] == 99


def test_runner_returning_none_keeps_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")

    def runner(tid):
        tasks.update(tid, rows=13)
        return None

    st = _wait_done(tasks.start("vector", runner))
    assert st["status"] == "done" and st["rows"] == 13


# ---------- 失败文案带上 hint ----------

def _fail_with(exc):
    def runner(_tid):
        raise exc
    tid = tasks.start("vector", runner, account=None)
    return _wait_done(tid)


def test_failure_error_carries_hint(tmp_path, monkeypatch):
    """MCP 用户也要看得到修复路径：error 用 errors.to_text 的「消息（hint）」形式。"""
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    st = _fail_with(errors.VectorError("索引与当前模型不一致", hint="请改用 rebuild=True 重建"))
    assert st["status"] == "failed"
    assert st["error"] == "索引与当前模型不一致（请改用 rebuild=True 重建）"


def test_failure_error_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    st = _fail_with(errors.VectorError("超长" * 1000, hint="修"))
    assert st["status"] == "failed"
    # 上限是 redact 的 300 字 + 省略号（原先断言 <=500，抓不到 500→300 的收窄）
    assert st["error"].endswith("…")
    assert len(st["error"]) <= 301


def test_a_running_record_with_a_live_pid_is_not_harvested(tmp_path, monkeypatch):
    """EA-015：载入即收割此前**无条件**把 running 改判 failed——另一个进程（MCP server）
    里正在跑的任务被一读就写死，`get_task_status` 答「失败」而它其实还在跑。

    修后判据是**记录的 pid 是否活着**：本进程 pid 的记录保持 running；
    不存在的 pid 才收割。把 `_pid_alive` 判据删掉（回到无条件收割），第一条断言必红。
    """
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    now = int(time.time())
    (tmp_path / "tasks.json").write_text(json.dumps({
        "alive": {"id": "alive", "kind": "export", "status": "running", "rows": 5,
                  "started_at": now, "error": None, "pid": os.getpid()},
        "gone": {"id": "gone", "kind": "export", "status": "running", "rows": 2,
                 "started_at": now, "error": None, "pid": 999_999_999},
    }))
    _simulate_restart()

    assert tasks.status("alive")["status"] == "running", "活进程的任务不许被判死"
    st = tasks.status("gone")
    assert st["status"] == "failed" and st["error"] == tasks.ORPHAN_ERROR


def test_a_stale_running_record_is_harvested_even_if_its_pid_is_alive(tmp_path, monkeypatch):
    """F6（T7 复审）：`os.kill(pid, 0)` 分不清「原任务」与「复用了该 pid 的无关进程」——
    一条 30 天前开始、pid 恰好指向活进程的记录必须按**时间兜底**收割，否则该账号的新导出
    会被这条幽灵永久吸收（返回幽灵 id、runner 永不执行）。

    把 STALE_RUNNING_SECONDS 那条判断删掉，两条断言必红。
    """
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    (tmp_path / "tasks.json").write_text(json.dumps({
        "ghost": {"id": "ghost", "kind": "export", "status": "running", "rows": 1,
                  "started_at": int(time.time()) - 30 * 24 * 3600, "error": None,
                  "pid": os.getpid(), "account": "acct", "chat_id": None, "full": False},
    }))
    _simulate_restart()

    st = tasks.status("ghost")
    assert st["status"] == "failed" and st["error"] == tasks.ORPHAN_ERROR
    tid = tasks.start("export", lambda _t: 1, account="acct", chat_id=None, full=False)
    assert tid != "ghost", "幽灵不许吸收新导出（否则该 account 的导出永远秒回同一个 id）"
    assert _wait_done(tid)["status"] == "done"


# ---------- 准入控制（EA-122）与它的目标键（F1）/ 原子性（F7） ----------

def test_admission_absorbs_only_the_same_work(tmp_path, monkeypatch):
    """EA-122 + F1（T7 复审）：准入只吸收**同一次工作**——同 kind、且目标字段
    （account/chat_id/full/rebuild）全部相同；不同目标绝不静默吸收。

    此前只看 (kind, account)：要 chatB 的调用会拿到 chatA 的 task_id、`full=True` 被
    增量任务吸收、`build_vector_index()`（全部账号）被单账号任务吸收——调用方按 done
    轮询后得出「李四导出了/全量跑了」这个**错误**结论（信封逐字节相同，无任何信号）。
    把准入条件里的 `all(rec.get(k) == fields.get(k) …)` 退回只看 account，中段断言必红。
    """
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    gate = threading.Event()

    def runner(_tid):
        gate.wait(timeout=5)
        return 1

    started = []

    def begin(**kw):
        tid = tasks.start(kw.pop("kind"), runner, **kw)
        started.append(tid)
        return tid

    first = begin(kind="export", account="acct", chat_id="chatA", full=False)
    assert begin(kind="export", account="acct", chat_id="chatA", full=False) == first, \
        "完全相同的请求 → 复用既有 id"
    assert begin(kind="export", account="acct", chat_id="chatB", full=False) != first, \
        "不同 chat_id 不许被吸收"
    assert begin(kind="export", account="acct", chat_id="chatA", full=True) != first, \
        "full=True 不许被增量任务吸收"
    assert begin(kind="export", account="acct2", chat_id="chatA", full=False) != first, \
        "不同 account 不受准入限制"
    assert begin(kind="vector", account="acct") != first, "不同 kind 不受准入限制"
    vec_all = begin(kind="vector", account=None)
    assert begin(kind="vector", account=None) == vec_all, "两次「全部账号」是同一件事"
    assert begin(kind="vector", account="acctX") != vec_all, \
        "「全部账号」与单账号是不同目标（两个方向都不许吸收）"

    gate.set()
    for tid in set(started):
        assert _wait_done(tid)["status"] == "done"


def test_concurrent_starts_cannot_both_win(tmp_path, monkeypatch):
    """F7（T7 复审）：准入的「判据 → 插入 → 落盘」必须在**同一把锁内**——check-then-act
    分两次取锁时，并发 start 会各起一条（同 kind 同目标），失败面仍是 `database is locked`。

    用慢 uuid4 把旧实现的窗口撑开（判据之后释放锁、uuid4 期间被调度）：把 start 拆回
    两个 `with _lock`，本用例必红。
    """
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    real_uuid4 = tasks.uuid.uuid4
    monkeypatch.setattr(tasks.uuid, "uuid4",
                        lambda: (time.sleep(0.02), real_uuid4())[1])
    gate = threading.Event()

    def runner(_tid):
        gate.wait(timeout=5)
        return 1

    barrier = threading.Barrier(2)
    got = []

    def racer():
        barrier.wait(timeout=5)
        got.append(tasks.start("export", runner, account="acct", chat_id="chatA",
                               full=False))

    threads = [threading.Thread(target=racer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    try:
        assert len(set(got)) == 1, f"并发 start 各起了一条：{got}"
    finally:
        gate.set()
    assert _wait_done(got[0])["status"] == "done"


# ---------- 落盘失败的收尾（F5：不许留幽灵 running） ----------

def test_start_rolls_back_when_the_registry_cannot_be_written(tmp_path, monkeypatch):
    """F5（T7 复审）：`start` 的落盘失败必须**回滚**刚插入的记录——否则内存里留下一条
    runner 永不执行、却会被准入吸收的幽灵 running：之后每次同目标 start 都秒回同一个 id，
    任务永远不跑（比报错更坏）。

    判别面要**读也读不动**（坏文件）：只要磁盘可读，下一次 start 的重读就会把内存幽灵
    冲掉，回滚与否看起来都对；读不动时才只有回滚能挡住它。把 `except OSError:
    _tasks.pop(...)` 删掉，`_wait_done` 会等到 running 超时（红）。
    """
    target = tmp_path / "tasks.json"
    monkeypatch.setattr(tasks, "TASKS_FILE", target)
    target.write_text("{ 坏文件")                  # 读不动：内存视图是唯一的真相
    tasks._tasks.clear()
    real_replace = os.replace

    def boom(src, dst):
        raise OSError("ENOSPC")

    monkeypatch.setattr(tasks.os, "replace", boom)
    with pytest.raises(OSError):                  # 异常照旧上抛（调用方看得见）
        tasks.start("export", lambda _t: 1, account="acct")

    monkeypatch.setattr(tasks.os, "replace", real_replace)
    tid = tasks.start("export", lambda _t: 1, account="acct")
    assert _wait_done(tid)["status"] == "done", "新任务必须真的跑（不许被幽灵吸收）"


def test_the_temp_file_name_carries_the_pid(tmp_path, monkeypatch):
    """F5：tmp 名带 pid——固定名会让两个进程共用同一个临时文件，一方的 `os.replace`
    撞上另一方刚搬走它的 ENOENT 窗口（落盘的进程反而抛错、留下幽灵）。"""
    target = tmp_path / "tasks.json"
    monkeypatch.setattr(tasks, "TASKS_FILE", target)
    seen = []
    real_replace = os.replace
    monkeypatch.setattr(tasks.os, "replace",
                        lambda src, dst: (seen.append(src.name), real_replace(src, dst))[1])

    tid = tasks.start("export", lambda _t: 1, account="acct")
    _wait_done(tid)
    assert seen, "至少发生过一次落盘"
    assert all(str(os.getpid()) in name for name in seen), seen
