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

import os
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


@dataclass(frozen=True)
class PlanItem:
    artifact: Artifact
    exists: bool
    size: int          # 字节；不存在时为 0
    is_symlink: bool


@dataclass(frozen=True)
class PurgePlan:
    scopes: frozenset[str]
    all_: bool
    items: tuple[PlanItem, ...]        # 具名产物（--all 时亦为「已知产物」部分）
    loose: tuple[pathlib.Path, ...]    # 仅 --all：HOME_DIR 顶层不属于任何具名产物的条目


def _item(a: Artifact) -> PlanItem:
    try:
        st = a.path.lstat()            # lstat 而非 stat：符号链接本身也要能看出来
    except OSError:
        return PlanItem(a, False, 0, False)
    return PlanItem(a, True, st.st_size, a.path.is_symlink())


def plan(scopes: frozenset[str], all_: bool = False,
         home: pathlib.Path | None = None) -> PurgePlan:
    """生成删除计划。**纯只读**：不创建、不删除、不写任何东西。

    `--all` 的 loose 是「枚举 HOME_DIR 顶层减去已知产物」，不是 artifacts() 的产物——
    artifacts() 只认识三个具名范围，列不出 models/ 与散落文件。
    """
    home = home or paths.HOME_DIR
    # --all 时 items 仍全部列出：计划表要显示 index/keys 的重建代价（spec §3 的 --all 表样）
    items = tuple(_item(a) for a in artifacts() if all_ or a.scope in scopes)
    loose: tuple[pathlib.Path, ...] = ()
    if all_:
        known = {a.path for a in artifacts()}
        try:
            top = sorted(home.iterdir())
        except OSError:
            # 承重分支，不是防御性填充：HOME_DIR 不存在时（全新安装，或刚跑过一次 --all
            # 之后）iterdir 会抛 FileNotFoundError，而 spec §5.1 第 5 步要求 --all 对此
            # 幂等。覆盖它的测试是 T4 的 test_execute_all_on_missing_home_is_idempotent，
            # 不是 T3 那两个（它们走 all_=False，根本不进这个分支）。
            top = []
        loose = tuple(p for p in top if p not in known)
    return PurgePlan(scopes=scopes, all_=all_, items=items, loose=loose)


@dataclass(frozen=True)
class PurgeReport:
    removed: tuple[pathlib.Path, ...]
    failed: tuple[tuple[pathlib.Path, str], ...]
    freed: int                      # 已删除项的字节数之和
    home_removed: bool


def _remove(item: PlanItem) -> None:
    if item.artifact.kind == "file":
        item.artifact.path.unlink(missing_ok=True)
    else:
        shutil.rmtree(item.artifact.path)


def _tree_size(root: pathlib.Path) -> int:
    """整树的字节数（只读元数据）。供 --all 用：那条路径绕过 items 循环，没有现成体积可加。

    os.walk 默认吞掉遍历中的 OSError——与「部分失败继续」同一语义：算不准的项不计入，
    不因为一个不可读的子目录就让整次汇报失败。
    """
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def execute(plan: PurgePlan) -> PurgeReport:
    """执行删除。**部分失败不中止**：敏感度高的产物应当继续被清除（spec §5.1 第 4 步）。"""
    removed: list[pathlib.Path] = []
    failed: list[tuple[pathlib.Path, str]] = []
    freed = 0

    # --all 时 items 只是给计划表看的（含重建代价），实际删除走下面的 rmtree 一次性完成，
    # 免得对同一棵树做两遍（unlink 一遍再 rmtree 一遍）。
    if not plan.all_:
        for item in plan.items:
            if not item.exists:
                continue
            try:
                _remove(item)
                removed.append(item.artifact.path)
                freed += item.size
            except OSError as exc:
                failed.append((item.artifact.path, str(exc)))

    home_removed = False
    if plan.all_:
        if paths.HOME_DIR.exists():
            # --all 不走上面的 items 循环，removed/freed 必须在这里补齐：否则 CLI 会在
            # 删光整棵 HOME_DIR 之后打印「已删除 0 项，释放 0 B」，与 spec §3/§6 要求的
            # 「删了什么 + 释放多少」直接冲突。体积要在 rmtree **之前**取。
            freed = _tree_size(paths.HOME_DIR)
            shown = [i.artifact.path for i in plan.items if i.exists] + list(plan.loose)
            # onexc 收集而非抛出：与「部分失败继续删完」同一语义。
            # 用 onexc 不用 onerror——后者在 3.12 已弃用，本仓库 requires-python >=3.12。
            shutil.rmtree(paths.HOME_DIR,
                          onexc=lambda fn, p, exc: failed.append((pathlib.Path(p), str(exc))))
            # 删完再回看谁真的没了：部分失败时不会把仍存在的条目虚报为已删
            removed = [p for p in shown if not p.exists()]
            home_removed = not paths.HOME_DIR.exists()
    return PurgeReport(tuple(removed), tuple(failed), freed, home_removed)
