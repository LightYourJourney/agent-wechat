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
hits: set[int] = set()


def _tracer(frame: types.FrameType, event: str, _arg: object) -> object:
    if frame.f_code.co_filename not in _names:
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
