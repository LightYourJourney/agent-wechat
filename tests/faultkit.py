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

    @staticmethod
    def _path_matches(f, path):
        """路径判据：Fault.path 为 None＝不绑路径；调用方没有路径 getter 时也不绑。"""
        if f.path is None or path is None:
            return True
        return os.path.abspath(os.fspath(path)) == os.path.abspath(f.path)

    def match(self, api, path=None):
        # 序号按**每个故障各自**的命中次数计：非目标路径的调用既不命中、也不消耗序号。
        for i, f in enumerate(self.faults):
            if f.api != api or not self._path_matches(f, path):
                continue
            n = self.seen.get(i, 0) + 1
            self.seen[i] = n
            if n == f.nth:
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


def _self_arg(*a, **k):
    """Path 方法：`self` 通常是位置 arg0，关键字形态也接住。"""
    return a[0] if a else k.get("self")


def _path_arg(*a, **k):
    """单路径 API（os.unlink/mkdir/chmod、shutil.rmtree）：`path` 位置或关键字。"""
    return a[0] if a else k.get("path")


def _dst_arg(*a, **k):
    """双路径 API（os.replace、shutil.copy2）：绑**目标**，缺 dst 时退回 src。

    copy2 的真实写入发生在 dst（`decrypt.snapshot` 就是 `copy2(snap, dst)`）；
    绑 src 会让「写目标失败」这类故障打偏。
    """
    if len(a) >= 2:
        return a[1]
    if k.get("dst") is not None:
        return k["dst"]
    return a[0] if a else k.get("src")


def _cmd_arg(*a, **k):
    """subprocess.run：命令是位置 arg0 或关键字 `args`。"""
    return a[0] if a else k.get("args")


def _wrap(orig, inj, api, get_path=None, payload_index=None):
    def inner(*a, **k):
        f = inj.match(api, get_path(*a, **k) if get_path else None)
        if f is None:
            return orig(*a, **k)
        if f.kind == "SHORT_WRITE":
            args, kwargs = list(a), dict(k)
            if payload_index < len(args):
                payload = args[payload_index]
                args[payload_index] = payload[: len(payload) // 2]
            else:
                payload = kwargs["data"]       # write_text/write_bytes 的关键字形态
                kwargs["data"] = payload[: len(payload) // 2]
            orig(*args, **kwargs)
            raise OSError(errno.EIO, "simulated short write")
        if f.kind == "EINTR":
            raise InterruptedError("simulated EINTR")
        if f.kind == "SUBPROCESS_NONZERO":
            cmd = a[0] if a else k.get("args")
            return subprocess.CompletedProcess(cmd, 1, "", "simulated failure")
        _raise(f.kind)
    return inner


def install(faults):
    """装故障；**自带独立 MonkeyPatch**，`inj.undo()` 只撤故障，不动 conftest 的隔离 patch。"""
    inj = Injector(faults)
    mp = inj.mp
    P = pathlib.Path
    for name in ("write_text", "write_bytes"):
        orig = getattr(P, name)
        mp.setattr(P, name, _wrap(orig, inj, name, _self_arg, payload_index=1))
    for name in ("mkdir", "chmod", "unlink"):
        orig = getattr(P, name)
        mp.setattr(P, name, _wrap(orig, inj, name, _self_arg))
    mp.setattr(P, "resolve", _wrap(P.resolve, inj, "resolve", _self_arg))
    mp.setattr(os, "replace", _wrap(os.replace, inj, "os.replace", _dst_arg))
    for name in ("unlink", "mkdir", "chmod"):
        orig = getattr(os, name)
        mp.setattr(os, name, _wrap(orig, inj, "os." + name, _path_arg))
    mp.setattr(shutil, "rmtree",
               _wrap(shutil.rmtree, inj, "shutil.rmtree", _path_arg))
    mp.setattr(shutil, "copy2", _wrap(shutil.copy2, inj, "shutil.copy2", _dst_arg))
    mp.setattr(subprocess, "run",
               _wrap(subprocess.run, inj, "subprocess.run", _cmd_arg))
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
                     lambda *a, **k: None))
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
    """只做一件事：白名单**外**不得出现文件（半成品 / 临时残留）。

    「白名单内的文件是否完整」不在这里判——没有内容类型契约，无法通用地定义
    「可解析」。调用方对自己期望的产物自行解析（JSON / sqlite / …）。
    """
    for p in pathlib.Path(root).rglob("*"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        if rel not in whitelist:
            raise AssertionError(f"陌生文件残留：{rel}")
