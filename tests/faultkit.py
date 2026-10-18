"""故障注入原语（测试侧）。**不改产品代码**；桩绑定目标路径 + 调用序号。

每个 `install()` **自带独立的 `pytest.MonkeyPatch`**：`inj.undo()` 只撤注入的故障，
不会碰到 conftest 里那套真实数据隔离夹具——撤掉后者会让用例写进用户真实的数据目录。
"""
from __future__ import annotations

import errno
import hashlib
import os
import pathlib
import shutil
import signal
import sqlite3
import subprocess
from dataclasses import dataclass

import pytest

from agent_wechat import errors


@dataclass(frozen=True)
class Fault:
    api: str
    kind: str
    path: str | None = None
    nth: int = 1


_ERRNO = {"ENOSPC": errno.ENOSPC, "EACCES": errno.EACCES, "EIO": errno.EIO,
          "EINTR": errno.EINTR, "REPLACE_FAIL": errno.EACCES}


class Injector:
    def __init__(self, faults):
        self.faults, self.seen, self.fired = list(faults), {}, []
        self.mp = pytest.MonkeyPatch()      # 独立于 conftest 的隔离 patch

    def undo(self):
        self.mp.undo()

    def match(self, api, path=None):
        key = api
        n = self.seen.get(key, 0) + 1
        self.seen[key] = n
        for f in self.faults:
            if f.api != api or f.nth != n:
                continue
            if f.path is not None and path is not None:
                if os.path.abspath(os.fspath(path)) != os.path.abspath(f.path):
                    continue
            self.fired.append(f)
            return f
        return None

    def assert_fired(self):
        assert self.fired, "故障桩一次都没命中——注入点打偏了"


def _raise(kind):
    if kind in _ERRNO:
        raise OSError(_ERRNO[kind], os.strerror(_ERRNO[kind]))
    if kind == "SQLITE_LOCKED":
        raise sqlite3.OperationalError("database is locked")
    if kind == "SQLITE_CORRUPT":
        raise sqlite3.DatabaseError("database disk image is malformed")
    if kind == "SUBPROCESS_START_FAIL":
        raise FileNotFoundError("simulated: command not found")
    if kind == "SUBPROCESS_TIMEOUT":
        raise subprocess.TimeoutExpired("sim", 1)
    if kind == "NETWORK_CONNECT_FAIL":
        raise OSError("simulated network failure")
    if kind == "NETWORK_TIMEOUT":
        raise TimeoutError("simulated network timeout")
    if kind == "NETWORK_TRUNCATED":
        raise OSError("simulated truncated response")
    if kind == "GATE_FAULT":
        raise OSError(errno.EACCES, "simulated gate failure")
    if kind == "ELOOP":
        raise OSError(errno.ELOOP, "simulated symlink loop")
    raise AssertionError(f"未知故障类 {kind}")


def _wrap(orig, inj, api, get_path=None, payload_index=None):
    def inner(*a, **k):
        f = inj.match(api, get_path(*a) if get_path else None)
        if f is None:
            return orig(*a, **k)
        if f.kind == "SHORT_WRITE":
            args = list(a)
            p = args[payload_index]
            args[payload_index] = p[: len(p) // 2]
            orig(*args, **k)
            raise OSError(errno.EIO, "simulated short write")
        if f.kind == "EINTR":
            raise InterruptedError("simulated EINTR")
        if f.kind == "SUBPROCESS_NONZERO":
            return subprocess.CompletedProcess(a[0], 1, "", "simulated failure")
        _raise(f.kind)
    return inner


def install(faults):
    """装故障；**自带独立 MonkeyPatch**，`inj.undo()` 只撤故障，不动 conftest 的隔离 patch。"""
    inj = Injector(faults)
    mp = inj.mp
    P = pathlib.Path
    for name in ("write_text", "write_bytes"):
        orig = getattr(P, name)
        mp.setattr(P, name, _wrap(orig, inj, name, lambda self, *a, **k: self,
                                  payload_index=1))
    for name in ("mkdir", "chmod", "unlink"):
        orig = getattr(P, name)
        mp.setattr(P, name, _wrap(orig, inj, name, lambda self, *a, **k: self))
    mp.setattr(P, "resolve", _wrap(P.resolve, inj, "resolve",
                                   lambda self, *a, **k: self))
    for name in ("replace", "unlink", "mkdir", "chmod"):
        orig = getattr(os, name)
        mp.setattr(os, name, _wrap(orig, inj, "os." + name,
                                   lambda src, dst=None, **k: dst or src))
    for name in ("rmtree", "copy2"):
        orig = getattr(shutil, name)
        mp.setattr(shutil, name, _wrap(orig, inj, "shutil." + name,
                                       lambda src, *a, **k: src))
    mp.setattr(subprocess, "run",
               _wrap(subprocess.run, inj, "subprocess.run",
                     lambda cmd, *a, **k: cmd))
    orig_connect = sqlite3.connect

    class _FaultConnection(sqlite3.Connection):
        # sqlite3.Connection 是不可变的 C 类型，不能直接 monkeypatch 它的 commit；
        # 用 factory 子类接住 commit 这个边界。
        def commit(self):
            f = inj.match("commit")
            if f is not None:
                _raise(f.kind)
            return super().commit()

    def connect(*a, **k):
        database = a[0] if a else k.get("database")
        f = inj.match("sqlite.connect", database)
        if f is not None:
            _raise(f.kind)
        if "factory" in k or len(a) > 5:
            return orig_connect(*a, **k)
        return orig_connect(*a, factory=_FaultConnection, **k)
    mp.setattr(sqlite3, "connect", connect)
    from agent_wechat import sns_media
    mp.setattr(sns_media, "fetch",
               _wrap(sns_media.fetch, inj, "sns_media.fetch",
                     lambda m, *a, **k: None))
    return inj


def install_kill(api: str, nth: int):
    """子进程用：第 nth 次命中时写 marker 并 SIGKILL 自己。

    marker 用 `os.open`/`os.write` 写：被桩的边界可能正是 `write_text` / `open`
    这类写 API，用它们写 marker 会再次命中 hook，marker 就永远落不了盘。
    """
    marker = os.environ["AW_KILL_MARKER"]
    count = {"n": 0}

    def hook(*a, **k):
        count["n"] += 1
        if count["n"] == nth:
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, api.encode())
            finally:
                os.close(fd)
            os.kill(os.getpid(), signal.SIGKILL)
    return hook


def expect_project_error(fn, *, keyword: str):
    """keyword 形如 "空间|No space left"：任一命中即可（项目文案中文 + 内嵌 strerror 英文）。"""
    with pytest.raises(errors.AgentWeChatError) as ei:
        fn()
    assert ei.value.hint, "项目异常必须带可执行 hint"
    text = str(ei.value) + (ei.value.hint or "")
    assert any(k in text for k in keyword.split("|")), f"期望关键词 {keyword!r} 均未出现在：{text}"


def hash_tree(root: pathlib.Path) -> str:
    h = hashlib.sha256()
    for p in sorted(pathlib.Path(root).rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode()); h.update(b"\0")
            h.update(p.read_bytes()); h.update(b"\0")
    return h.hexdigest()


def assert_retry_succeeds(inj, fn, root: pathlib.Path) -> None:
    """不变量 3：故障消失后，在**同一状态**上重试一次必须成功。"""
    inj.undo()                         # 只撤故障注入，不碰 conftest 的隔离 patch
    fn()
    assert pathlib.Path(root).exists()


def assert_no_partial(root: pathlib.Path, whitelist: set[str]):
    """白名单内的文件必须完整可解析；白名单外不得出现文件。"""
    for p in pathlib.Path(root).rglob("*"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        if rel not in whitelist:
            raise AssertionError(f"陌生文件残留：{rel}")
