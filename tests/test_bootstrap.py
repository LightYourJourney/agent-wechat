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


# ---------- bootstrap：进度事件与落缓存 ----------

def _stub_pipeline(monkeypatch, *, verify_result, saved: dict):
    monkeypatch.setattr(bootstrap.account, "status",
                        lambda: SimpleNamespace(logged_in=True, current=ACC, pid=999))
    seen = {}

    def capture(timeout):
        seen["timeout"] = timeout
        return HEX

    monkeypatch.setattr(bootstrap, "capture_passphrase_lldb", capture)

    def verify(acct, pw):
        seen["verify"] = (acct, pw)
        return verify_result

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
