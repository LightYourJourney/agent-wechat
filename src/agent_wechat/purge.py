"""数据销毁：清单 → 计划 → 执行 → 核查。只做 CLI，不进 MCP。

设计取舍与实测依据见 内部设计记录。

两条不可违反的约定：
- 路径的唯一来源是**拥有它的模块**（keys.CACHE_FILE / store.db_paths() /
  decrypt.tmp_dir() / vector.MODEL_CACHE_DIR / tasks.TASKS_FILE / paths.HOME_DIR）。
  本模块**不得**自己拼 "passphrase.json" 这类字面量——那是不随拥有者演进的第二份来源。
- 引用其它模块的常量一律走 `from . import keys` + `keys.CACHE_FILE`，
  绝不 `from .keys import CACHE_FILE`：后者在导入时绑定值，测试 monkeypatch 会失效。
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
from dataclasses import dataclass

from . import decrypt, errors, keys, paths, store, tasks, vector

_run = subprocess.run          # 模块级替换点（同 account._run）：测试注入桩


@dataclass(frozen=True)
class Artifact:
    """一个可被销毁的路径。每条一个对象，path 是单数——计划表逐行渲染与它一一对应。"""
    scope: str          # "keys" | "index" | "tmp"
    label: str          # 人类可读说明
    path: pathlib.Path
    kind: str           # "file" | "dir"：决定 unlink 还是 rmtree，不靠调用方猜
    sensitive: bool
    rebuild: str        # 重建代价，写进计划表


def artifacts() -> tuple[Artifact, ...]:
    """三个具名范围的清单。**调用时求值**——测试靠 monkeypatch 重定向路径。"""
    out = [
        Artifact("keys", "密钥缓存", keys.CACHE_FILE, "file", True,
                 "重建需 sudo + 在微信内退出登录再重新登录"),
        Artifact("tmp", "解密临时目录", decrypt.tmp_dir(), "dir", True, "无"),
    ]
    out += [Artifact("index", "本地检索库（含向量索引）", p, "file", True,
                     "重建 ≈2.5–3 小时（68 万向量）")
            for p in store.db_paths()]
    return tuple(out)
