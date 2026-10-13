"""bootstrap 的接口测试：hexdump 解析、两条早退路径、密钥不泄漏。

红线护栏：本文件的 autouse fixture 把 `bootstrap.subprocess` / `bootstrap.shutil`
换成桩，任何走到 Popen（lldb attach）/ pkill 的路径都会立刻 AssertionError——
**绝不 attach 真实微信进程**。所有测试都在 `AGENT_WECHAT_HOME` 临时目录下运行。
"""
from types import SimpleNamespace

import pytest

from agent_wechat import bootstrap
from agent_wechat.bootstrap import _parse_passphrase
from agent_wechat.errors import CaptureError

ACC = "wxid_testacct01_ab12"
BARE = "wxid_testacct01"
HEX = "deadbeef" * 8          # 64 字符 = 32 字节


@pytest.fixture(autouse=True)
def _forbid_real_process(monkeypatch):
    """红线：C8 测试不得真的起 lldb / pkill；which 默认视为缺失。"""
    def _boom(*a, **k):
        raise AssertionError("C8 测试不得启动子进程（红线：绝不 attach 真实微信）")
    monkeypatch.setattr(bootstrap, "subprocess", SimpleNamespace(Popen=_boom, run=_boom))
    monkeypatch.setattr(bootstrap, "shutil", SimpleNamespace(which=lambda name: None))


def _lldb_which(monkeypatch, path: str = "/usr/bin/lldb") -> None:
    monkeypatch.setattr(bootstrap, "shutil", SimpleNamespace(which=lambda name: path))


def _hexdump(hexstr: str, addr: str = "0x000000016f3d8a00", per_line: int = 16) -> str:
    """构造 lldb `memory read --format x` 的 hexdump 文本（17B/行是真实 16B/行的子集）。"""
    lines = []
    step = per_line * 2
    for i in range(0, len(hexstr), step):
        chunk = hexstr[i:i + step]
        bs = " ".join(f"0x{chunk[j:j + 2]}" for j in range(0, len(chunk), 2))
        lines.append(f"{addr}: {bs}")
    return "\n".join(lines)


# ---------- _parse_passphrase：纯解析 ----------

def test_parse_passphrase_single_line():
    assert _parse_passphrase(_hexdump(HEX)) == HEX


def test_parse_passphrase_accumulates_across_lines():
    """lldb 每行 16 字节；32 字节分两行，须跨行拼接。"""
    out = _hexdump(HEX, per_line=16)
    assert len(out.splitlines()) == 2
    assert _parse_passphrase(out) == HEX


def test_parse_passphrase_ignores_non_hexdump_lines():
    out = f"(lldb) memory read\n{_hexdump(HEX)}\n(lldb) detach\n"
    assert _parse_passphrase(out) == HEX


def test_parse_passphrase_truncated_returns_none():
    """不足 32 字节一律 None，绝不截短采用。"""
    assert _parse_passphrase(_hexdump(HEX[:62])) is None      # 31 字节
    assert _parse_passphrase(_hexdump(HEX[:32])) is None      # 16 字节


def test_parse_passphrase_incomplete_byte_at_line_end_returns_none():
    """末字节被截成 `0x1`：该行不匹配，少 1 字节 → None。"""
    line = "0x0: " + " ".join(f"0x{HEX[j:j + 2]}" for j in range(0, 62, 2)) + " 0x1"
    assert _parse_passphrase(line) is None


def test_parse_passphrase_no_match_returns_none():
    assert _parse_passphrase("") is None
    assert _parse_passphrase("no hexdump here\n") is None


def test_parse_passphrase_uppercase_hex_is_not_recognized():
    """固化现状（不变量）：正则只认小写 hex——不改成大小写不敏感。"""
    upper = HEX.upper()
    assert _parse_passphrase(_hexdump(upper)) is None


def test_parse_passphrase_uppercase_address_is_not_recognized():
    """固化现状：地址段同样只认小写 hex。"""
    assert _parse_passphrase(_hexdump(HEX, addr="0x000000016F3D8A00")) is None


def test_parse_passphrase_takes_first_32_of_longer_dump():
    """固化现状（无长度上限）：多余字节被 `[:32]` 截掉，取前 32 字节。"""
    long_hex = ("ab" * 32) + ("cd" * 32)          # 64 字节
    assert _parse_passphrase(_hexdump(long_hex, per_line=16)) == "ab" * 32


# ---------- capture_passphrase_lldb：两条早退路径（不触进程） ----------

def test_capture_missing_lldb_error_text_unchanged():
    with pytest.raises(CaptureError) as ei:
        bootstrap.capture_passphrase_lldb()
    assert str(ei.value) == "未检测到 lldb，请先: xcode-select --install"


def test_capture_wechat_not_found_error_text_unchanged(monkeypatch):
    _lldb_which(monkeypatch)
    monkeypatch.setattr(bootstrap.account, "wechat_pid", lambda: None)
    with pytest.raises(CaptureError) as ei:
        bootstrap.capture_passphrase_lldb()
    assert str(ei.value) == "未找到微信进程，请先启动微信"


def test_capture_pid_zero_treated_as_not_running(monkeypatch):
    """`if not pid` 的现状：0 同样视为未运行（不得到处拼 lldb 脚本）。"""
    _lldb_which(monkeypatch)
    monkeypatch.setattr(bootstrap.account, "wechat_pid", lambda: 0)
    with pytest.raises(CaptureError) as ei:
        bootstrap.capture_passphrase_lldb()
    assert str(ei.value) == "未找到微信进程，请先启动微信"


# ---------- EA-004：收尾只向**本进程组**发信号 ----------

class _FakeProc:
    def __init__(self, pid: int = 4242):
        self.pid = pid

    def wait(self, timeout=None):
        return 0


def test_terminate_capture_signals_only_its_own_process_group(monkeypatch):
    """EA-004：收尾此前无条件 `pkill -9 -x lldb`——按**名字**杀，会连坐用户自己的 lldb
    会话（或以其它身份开的）。现在 Popen 起独立进程组，收尾只向 `getpgid(proc.pid)` 发
    SIGTERM，且**不许**退到按名 pkill。把首选改回 `pkill` 本测试的首条断言必红。
    """
    import signal as _signal

    seen = []
    monkeypatch.setattr(bootstrap.os, "getpgid", lambda pid: pid + 1)
    monkeypatch.setattr(bootstrap.os, "killpg", lambda pgid, sig: seen.append((pgid, sig)))

    def _no_pkill(*a, **k):
        raise AssertionError("不许按名字 pkill——那会连坐用户自己的 lldb 会话")
    monkeypatch.setattr(bootstrap.subprocess, "run", _no_pkill)

    bootstrap._terminate_capture(_FakeProc(pid=4242))
    assert seen == [(4243, _signal.SIGTERM)], "只向本进程组发一次 SIGTERM（wait 返回即止）"


def test_terminate_capture_escalates_to_sigkill_when_wait_times_out(monkeypatch):
    """SIGTERM 后收不干净（wait 超时）才升级 SIGKILL——仍不走 pkill。

    `wait` 只超时**一次**：一直不返回的话落到 pkill 才是设计行为（见下一条测试），
    那测的就不是升级了。
    """
    import signal as _signal

    seen = []
    monkeypatch.setattr(bootstrap.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(bootstrap.os, "killpg", lambda pgid, sig: seen.append(sig))

    import subprocess as _sub

    class _Stubborn(_FakeProc):
        calls = 0

        def wait(self, timeout=None):
            type(self).calls += 1
            if type(self).calls == 1:
                raise _sub.TimeoutExpired("lldb", timeout)   # 第一次收不干净
            return 0                                          # SIGKILL 之后收干净
    monkeypatch.setattr(bootstrap.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不许 pkill")))

    bootstrap._terminate_capture(_Stubborn(pid=7))
    assert seen == [_signal.SIGTERM, _signal.SIGKILL], seen


def test_terminate_capture_falls_back_to_pkill_only_when_killpg_is_unusable(monkeypatch):
    """killpg 两轮都不可用（getpgid 抛错）时才退到按名 pkill——**不删它**：删了就会在
    异常路径上静默丢失清理，留下一个 attach 着微信的 lldb。"""
    monkeypatch.setattr(bootstrap.os, "getpgid",
                        lambda pid: (_ for _ in ()).throw(ProcessLookupError(pid)))
    calls = []
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda argv, **kw: calls.append(argv))

    bootstrap._terminate_capture(_FakeProc())
    assert calls == [["pkill", "-9", "-x", "lldb"]], calls


def test_capture_starts_its_own_session_so_cleanup_cannot_hit_the_caller(monkeypatch):
    """EA-004 的另一半：没有 `start_new_session=True`，`killpg(os.getpgid(proc.pid))`
    命中的就是**调用者自己的进程组**（pytest / MCP server）——收尾会把自己的进程组杀掉。
    这条与上一条是一对：一条钉「向谁发」，一条钉「组是不是自己的」。
    """
    _lldb_which(monkeypatch)
    monkeypatch.setattr(bootstrap.account, "wechat_pid", lambda: 4242)
    seen = {}

    import subprocess as _sub

    def fake_popen(argv, **kw):
        seen.update(kw)
        seen["argv"] = argv
        return SimpleNamespace()
    # autouse fixture 把整个 subprocess 换成了桩，这里换回带常量的桩
    monkeypatch.setattr(bootstrap, "subprocess", SimpleNamespace(
        Popen=fake_popen, PIPE=_sub.PIPE, STDOUT=_sub.STDOUT))
    monkeypatch.setattr(bootstrap, "_read_capture", lambda proc, deadline: _hexdump(HEX))
    monkeypatch.setattr(bootstrap, "_terminate_capture", lambda proc: None)

    assert bootstrap.capture_passphrase_lldb(timeout=1) == HEX
    assert seen.get("start_new_session") is True
    assert "lldb" in seen["argv"][2] and "TERM=dumb" in seen["argv"][2]


# ---------- EA-052：读循环的 deadline ----------

def _pipe_reader():
    import os as _os
    r, w = _os.pipe()
    return _os, r, w, _os.fdopen(r, "r")


def test_read_capture_returns_at_the_deadline_without_a_newline(monkeypatch):
    """EA-052：`readline` 只在两次调用**之间**检查 deadline——lldb 输出卡在**半行**
    （没有换行）时它一直阻塞，`--timeout` 因此形同虚设。`os.read` 有多少读多少。

    管道里先到的是**行内半行**（无换行）：`readline` 只要缓冲区里有半行就会一直阻塞到
    换行出现（2s 后），而 deadline 只在两次调用之间被检查——`--timeout` 因此形同虚设。
    `os.read` 有多少读多少，按时返回。

    切点必须落在**行内**：切在行边界上时 select 一响就有整行可读，readline 不会阻塞，
    这条测试就测不出东西（M-F 变异实测过）。
    """
    import threading
    import time as _time

    _os, _r, w, reader = _pipe_reader()
    proc = SimpleNamespace(stdout=reader, poll=lambda: None)
    full = _hexdump(HEX) + "\n"
    cut = 30                    # 行内切开：首批到的是「半行」
    gate = threading.Event()    # 断言跑完才放行第二批，测试因此总是 ~deadline 就结束

    def writer():
        try:
            _os.write(w, full[:cut].encode())
            gate.wait(timeout=5)          # 旧实现若卡在 readline，这里超时后照样放行 → 它红
            _os.write(w, full[cut:].encode())
        except OSError:
            pass                          # 读端已关：迟到的写没人在意
        finally:
            _os.close(w)

    th = threading.Thread(target=writer, daemon=True)
    th.start()
    t0 = _time.monotonic()
    buf = bootstrap._read_capture(proc, _time.time() + 0.3)
    elapsed = _time.monotonic() - t0
    gate.set()
    th.join(timeout=5)
    reader.close()

    assert _parse_passphrase(buf) is None, "deadline 之后才到齐的数据不算数"
    assert elapsed < 1.0, f"半行不许把读循环钉住（readline 会等到 2s 后），实际 {elapsed:.2f}s"


def test_read_capture_parses_a_complete_hexdump_promptly():
    """反例：到齐的输出必须立刻解析出来并返回，不许「一律等到 deadline」。"""
    import time as _time

    _os, _r, w, reader = _pipe_reader()
    proc = SimpleNamespace(stdout=reader, poll=lambda: None)
    _os.write(w, (_hexdump(HEX) + "\n").encode())

    t0 = _time.monotonic()
    buf = bootstrap._read_capture(proc, _time.time() + 5)
    elapsed = _time.monotonic() - t0
    reader.close()
    _os.close(w)

    assert _parse_passphrase(buf) == HEX
    assert elapsed < 1.0, f"数据已到齐就该立刻返回，实际 {elapsed:.2f}s"


# ---------- bootstrap：进度事件与落缓存 ----------

def _stub_pipeline(monkeypatch, *, verify_result, saved: dict, unreadable: int = 0):
    monkeypatch.setattr(bootstrap.account, "status",
                        lambda: SimpleNamespace(logged_in=True, current=ACC, pid=999))
    seen = {}

    def capture(timeout):
        seen["timeout"] = timeout
        return HEX

    monkeypatch.setattr(bootstrap, "capture_passphrase_lldb", capture)

    def verify(acct, pw):
        seen["verify"] = (acct, pw)
        return verify_result, unreadable      # EA-051：返回值多了「读不到的库数」

    monkeypatch.setattr(bootstrap.keys, "verify_passphrase", verify)
    monkeypatch.setattr(bootstrap.keys, "save_cached_hex",
                        lambda bare, h: saved.update(bare=bare, h=h))
    return seen


def test_bootstrap_returns_bare_and_emits_ordered_progress(monkeypatch):
    events = []
    saved: dict = {}
    seen = _stub_pipeline(monkeypatch, verify_result=[("m/a.db", True), ("m/b.db", False)],
                          saved=saved)
    bare = bootstrap.bootstrap(timeout=7, progress=events.append)

    assert bare == BARE
    assert seen["timeout"] == 7
    assert seen["verify"] == (ACC, bytes.fromhex(HEX))
    assert saved == {"bare": BARE, "h": HEX}
    assert [e.kind for e in events] == ["account", "attach", "verified"]
    assert (events[0].bare, events[0].pid) == (BARE, 999)
    assert (events[2].ok_libs, events[2].total_libs) == (1, 2)


def test_bootstrap_without_progress_is_silent_and_works(monkeypatch):
    saved: dict = {}
    _stub_pipeline(monkeypatch, verify_result=[("m/a.db", True)], saved=saved)
    assert bootstrap.bootstrap() == BARE


def test_unreadable_dbs_get_a_permission_diagnosis_not_a_retry(monkeypatch):
    """EA-051：**一个库都没读到**时不许说「捕获值未通过校验，请重试」。

    `verify_passphrase` 此前把「读不到」静默 `continue`，「一个库都读不到」与「读到了但
    全不过」在返回值里同形——于是真问题（权限/完全磁盘访问权限）被包装成「再试一次捕获」，
    用户会一遍遍重跑 sudo 引导。第二个返回值让两条诊断分开。

    把 `total == 0` 那条分支删掉（回落到统一文案）本测试必红。
    """
    monkeypatch.setattr(bootstrap.account, "status",
                        lambda: SimpleNamespace(logged_in=True, current=ACC, pid=999))
    monkeypatch.setattr(bootstrap, "capture_passphrase_lldb", lambda t: HEX)
    monkeypatch.setattr(bootstrap.keys, "verify_passphrase", lambda a, p: ([], 3))
    _no_save(monkeypatch)

    with pytest.raises(CaptureError) as ei:
        bootstrap.bootstrap(timeout=1)
    msg = str(ei.value)
    assert "完全磁盘访问权限" in msg and "3 个不可读" in msg, msg
    assert "HMAC" not in msg, "读不到 ≠ 没过——不许给「重试捕获」的诊断"


def _no_save(monkeypatch):
    def boom(bare, h):
        raise AssertionError("一个库都没读到，不许落缓存")
    monkeypatch.setattr(bootstrap.keys, "save_cached_hex", boom)


def test_an_empty_account_reports_no_databases_not_a_permission_problem(monkeypatch):
    """F9（T9 复审，P4）：`unreadable == 0` 且 `total == 0` 时新文案自相矛盾——「一个库都没能
    读到（**0 个不可读**）」。库在盘上而一个 .db 都没有（账号刚登录还没同步、目录刚迁移）
    不是权限问题；说成权限会把用户送去改系统设置。

    删掉 `total == 0 and unreadable == 0` 那条分支本测试必红。
    """
    monkeypatch.setattr(bootstrap.account, "status",
                        lambda: SimpleNamespace(logged_in=True, current=ACC, pid=999))
    monkeypatch.setattr(bootstrap, "capture_passphrase_lldb", lambda t: HEX)
    monkeypatch.setattr(bootstrap.keys, "verify_passphrase", lambda a, p: ([], 0))
    _no_save(monkeypatch)

    with pytest.raises(CaptureError) as ei:
        bootstrap.bootstrap(timeout=1)
    msg = str(ei.value)
    assert "0 个不可读" not in msg, msg
    assert "完全磁盘访问权限" not in msg, "「没有库」与「读不到库」是两件事"
    assert "没有" in msg, msg


def test_bootstrap_not_logged_in_error_text_unchanged(monkeypatch):
    monkeypatch.setattr(bootstrap.account, "status",
                        lambda: SimpleNamespace(logged_in=False, current=None, pid=None))
    with pytest.raises(CaptureError) as ei:
        bootstrap.bootstrap()
    assert str(ei.value) == "无法确定当前登录账号，请先登录微信"


# ---------- 负向断言：passphrase 明文字节不进错误信息与异常链 ----------

def test_hmac_failure_error_text_and_chain_never_leak_passphrase(monkeypatch):
    saved: dict = {}
    _stub_pipeline(monkeypatch, verify_result=[("m/a.db", False), ("m/b.db", False)],
                   saved=saved)

    def _no_write(*a, **k):
        raise AssertionError("HMAC 未通过时不得落缓存")

    monkeypatch.setattr(bootstrap.keys, "save_cached_hex", _no_write)

    with pytest.raises(CaptureError) as ei:
        bootstrap.bootstrap()
    exc = ei.value

    assert str(exc) == ("捕获值未通过任何库的 HMAC 校验（2 个库），拒绝落缓存。"
                        "可能捕获了错误时刻的派生值，请重试。")
    assert saved == {}

    raw = bytes.fromhex(HEX)
    # 明文形态（hex / 字节 repr / 前 4 字节片段）均不得出现
    blobs = (HEX, raw.hex(), repr(raw), raw.hex()[:8])
    texts: list[str] = []
    e: BaseException | None = exc
    while e is not None:
        texts += [str(e), repr(e.args)]
        e = e.__cause__ or e.__context__
    for blob in blobs:
        assert all(blob not in t for t in texts), f"泄漏: {blob!r}"


def test_a_failed_ownership_handback_is_emitted_as_a_warning(tmp_path, monkeypatch):
    """EA-005：`save_cached_hex` 返回的警告（属主未归还）必须经 emit 通道到用户面前——
    verified 行照报（缓存确实落盘了），另加一行 warn；不许把整次引导判负。"""
    _lldb_which(monkeypatch)
    monkeypatch.setattr(bootstrap.account, "status", lambda: SimpleNamespace(
        logged_in=True, current=ACC, pid=999))
    monkeypatch.setattr(bootstrap, "capture_passphrase_lldb", lambda t: HEX)
    monkeypatch.setattr(bootstrap.keys, "verify_passphrase", lambda a, p: ([("db", True)], 0))
    monkeypatch.setattr(bootstrap.keys, "save_cached_hex",
                        lambda bare, h: "属主未归还（桩）：非 sudo 用户可能读不到密钥缓存")

    events = []
    assert bootstrap.bootstrap(timeout=1, progress=events.append) == BARE
    kinds = [e.kind for e in events]
    assert "verified" in kinds and "warn" in kinds, kinds
    warn = next(e for e in events if e.kind == "warn")
    assert "属主未归还" in warn.warning
