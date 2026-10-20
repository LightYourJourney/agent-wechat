"""B0：模块 → 用例的**冻结**映射（非 `test_` 前缀，不被 pytest 收集）。

清单只许扩充、不许删减：某个模块的映射用不上，说明这个模块根本没有用例执行到它——
那是发现，不是豁免。`tests/test_b_mapping.py` 用 settrace 探针逐个核实。

映射刻意**窄**：`test_reader_export_pipeline.py` 这类 24 秒的集成文件不进表，
否则每个变异体都要多付 24 秒（B 阶段是「位点数 × 映射耗时」的量级）。
窄到漏杀的位点会在 B 的存活者三分类里以「测试缺口」的形式还回来——不会静默。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

MAPPING: dict[str, list[str]] = {
    "paths": ["tests/test_paths.py", "tests/test_container_out_gate.py"],
    "store": ["tests/test_store.py", "tests/test_msg_parts_store.py"],
    "purge": ["tests/test_purge.py"],
    "usecases": ["tests/test_usecases.py", "tests/test_exclusions.py"],
    "decrypt": ["tests/test_decrypt_pipeline.py"],
    "media": ["tests/test_media.py"],
    "messages": ["tests/test_messages.py", "tests/test_msgextra.py"],
    "search": ["tests/test_store.py", "tests/test_display_names.py"],
    "tasks": ["tests/test_tasks.py"],
}


def verify_mapping(module: str, mapping: dict | None = None,
                   timeout: float = 180) -> list[str]:
    """逐个在子进程里带 settrace 跑；返回**没通过探针**的 nodeid。

    探针返回非零有两种成因：没执行到该模块任何一行（MISS），或该用例自身失败——
    后一种是映射表要重挑的信号，不是「漏覆盖」，两者都算「这一格没被证明」。
    """
    mapping = MAPPING if mapping is None else mapping
    probe = pathlib.Path(__file__).with_name("_trace_probe.py")
    missed: list[str] = []
    for nodeid in mapping[module]:
        env = dict(os.environ, AW_TRACE_MODULE=module)
        r = subprocess.run([sys.executable, str(probe), nodeid], env=env,
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            missed.append(nodeid)
    return missed


def hit_lines(module: str, mapping: dict | None = None, timeout: float = 180) -> int:
    """映射用例**一共执行到该模块多少行**（§6.3 要求落台账的那一列）。"""
    mapping = MAPPING if mapping is None else mapping
    probe = pathlib.Path(__file__).with_name("_trace_probe.py")
    total = 0
    for nodeid in mapping[module]:
        env = dict(os.environ, AW_TRACE_MODULE=module)
        r = subprocess.run([sys.executable, str(probe), nodeid], env=env,
                           capture_output=True, text=True, timeout=timeout)
        last = (r.stdout.strip().splitlines() or [""])[-1]
        if last.startswith("HIT "):
            total += int(last.split()[1])
    return total
