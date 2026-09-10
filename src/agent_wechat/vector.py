"""向量语义检索：本地 embedding（fastembed/ONNX）+ sqlite-vec KNN。

隔离带：全部 fastembed 与 sqlite-vec 调用都收在本模块（上游 sqlite-vec 为
pre-v1，存在已知崩溃/内存泄漏 bug），便于回归测试与将来整体替换。
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Sequence

from . import paths, store

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
DIM = 512                                   # 必须与 store.VECTOR_SCHEMA 的 float[512] 一致
MODEL_ID = f"fastembed:{MODEL_NAME}:{DIM}"
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："   # BGE 官方 s2p 指令，仅查询侧加
MODEL_CACHE_DIR = paths.HOME_DIR / "models"   # 固定目录：不用 $TMPDIR（重启即清）
BATCH = 256                                  # fastembed 实测最优批大小
COMMIT_ROWS = 5120                           # 每 ~20 批一个事务：频繁 commit 会让插入掉 ~43%
K_MAX = 4096                                 # sqlite-vec 硬上限
EMBED_TYPES = (1, 49)                        # 1=文本；49=appmsg（content 形如 "[appmsg: 标题]"）


class VectorError(RuntimeError):
    """向量层不可用（扩展缺失/模型不可用）。消息里带用户可执行的修复指引。"""


def connect() -> sqlite3.Connection:
    """store 连接 + 加载 sqlite-vec 扩展 + 建向量表（扩展不可用则 fail-fast）。"""
    con = store.connect()
    con.row_factory = sqlite3.Row
    try:
        con.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(con)
    except Exception as e:
        con.close()
        raise VectorError(
            "无法加载 sqlite-vec 扩展（需要解释器支持 enable_load_extension）："
            f"{e}. uv 管理的 Python 3.12 已验证可用；换 python.org/conda 发行版需重新验证。"
        ) from e
    con.enable_load_extension(False)
    con.execute("SELECT vec_version()").fetchone()      # 探活：真正验证扩展可用
    con.executescript(store.VECTOR_SCHEMA)
    return con
