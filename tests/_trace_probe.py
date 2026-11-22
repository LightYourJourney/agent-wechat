"""settrace 行探针（B0 辅助脚本，下划线开头 ⇒ 不被 pytest 收集）。

在**子进程**里跑一个用例，断言它真的执行到了目标模块的某一行——
「映射表里写了这个模块」与「这个用例真的碰过这个模块」是两件事，
映射表写错时后者照样能红。

用法：`AW_TRACE_MODULE=<模块名> python tests/_trace_probe.py <nodeid>`
退出码 0 = 命中且用例通过；1 = 没命中或用例本身失败。
"""
import importlib
import os
import sys
import types

import pytest

_mod = importlib.import_module(f"agent_wechat.{os.environ['AW_TRACE_MODULE']}")
# 模块在 import 期就已加载，`__file__` 此刻是绝对路径；两种写法都收，免得
# editable 安装与直接 `src/` 布局下 `co_filename` 的分辨率不同。
_names = {_mod.__file__, os.path.realpath(_mod.__file__)}
# **包也要收**：`usecases` 2026-11-21 拆成六个子模块后，代码行落在
# `usecases/chat.py` 之类的地方，`__init__` 里只剩重导出——只比 `__file__` 的探针
# 会永远报 MISS（不是「用例没覆盖」，是探针看得太窄）。
_pkg_dirs = ([os.path.abspath(_mod.__path__[0]),
              os.path.realpath(_mod.__path__[0])]
             if hasattr(_mod, "__path__") else [])
_pkg_prefixes = tuple(d + os.sep for d in _pkg_dirs)
hits: set[int] = set()


def _in_target(filename: str) -> bool:
    """这一帧的源码文件是不是目标模块（或其包内文件）。

    **纯字符串前缀判断，不做 `realpath`**：这个函数由 `settrace` 在**每一行**上调用，
    `os.path.realpath` 一次约 16 µs（实测），比集合判断慢 600 多倍——探针会把被测用例
    拖慢到失去意义。`co_filename` 已经是 import 时的路径，两种拼写（`__path__` 原样与
    realpath 展开）各比一次即可，与 `_names` 的两种写法同一个道理。
    """
    if filename in _names:
        return True
    return filename.startswith(_pkg_prefixes) if _pkg_prefixes else False


def _tracer(frame: types.FrameType, event: str, _arg: object) -> object:
    if not _in_target(frame.f_code.co_filename):
        return None                      # 别的文件不进逐行模式（否则慢一个数量级）
    if event == "line":
        hits.add(frame.f_lineno)
    return _tracer


sys.settrace(_tracer)
try:
    rc = pytest.main(["-q", "-p", "no:cacheprovider", *sys.argv[1:]])
finally:
    sys.settrace(None)
print(f"HIT {len(hits)}" if hits else "MISS")
sys.exit(0 if (hits and rc == 0) else 1)
