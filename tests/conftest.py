"""全测试套件的**真实数据隔离闸**。

为什么需要它：`store.py:20` 的 `DB_PATH = paths.HOME_DIR / "index.db"` 是 **import 期绑定**，
所以 `monkeypatch.setattr(paths, "HOME_DIR", …)` **拦不住它** —— 忘记 patch `store.DB_PATH`
的用例会以默认路径打开**用户真实**的 `~/.agent-wechat/index.db`，而 `store.connect()`
会执行整个 SCHEMA。本周期实测到 `test_media_export_unwritable_out_dir_is_one_line` 就是这样漏的
（它既没 patch `paths.HOME_DIR`、也没 patch `store.DB_PATH`，只 patch 了
`pathlib.Path.mkdir`——所以默认路径原样生效），T3 的新 DDL 因此落进了真实库。
> 订正：此处原写「它只 patch 了 `paths.HOME_DIR`」，**与事实不符**（T4b 审阅者核出）。
> 「它确实会漏」这个结论成立，但当时给的机制是错的。

**放 conftest 而不是逐条补 patch**：逐条补只修这一次，下一份忘记 patch 的用例照样漏。
用例如需自己的库，照常 `monkeypatch.setattr(store, "DB_PATH", …)` —— 它的 patch 在本夹具
之后生效，覆盖本夹具，互不冲突。

**闸两道，缺一即漏：**
- `store.DB_PATH` —— **import 期绑定**的那一份（上面说的那个洞）。
- `paths.HOME_DIR` —— **调用时求值**的那些路径都读它：`exclusions.path()`、`decrypt.tmp_dir()`、
  `media.export_root()`、`moments.export_root()`，以及 `purge` 的整条计划/执行/核查。只闸 DB_PATH 的话，
  一句 `exclusions.path().unlink()` 就能删掉用户真实的排除清单（T2/T3/T7 都会碰它）。

**第三个闸（F3，2026-10-14 补）**：`tasks.TASKS_FILE` 也是 import 期绑定，此前被列在
「刻意不管」里，直到 T6 复审逮到真实事故——`tasks.start` 的守护线程越过用例 teardown
（monkeypatch 已还原真实路径）才走到 `_persist()`，把三条合成记录**整表覆写**进用户真实的
`~/.agent-wechat/tasks.json`（原记录不可找回）。两道一起上：本闸挡住「用例压根没 patch」，
起线程的用例自己 `_wait_done` 到终态（`status` 与 `_persist` 同锁 → 看见终态即已落盘）。

**第四个闸（T9 复审 F1 连带，2026-10-14 补）**：测试期**不许起真实 `tmutil`**。让写入路径统一
过 `paths.ensure_home_dirs()`（F1 的修法）之后，一批原本不碰它的用例（`test_keys` 的写缓存、
`test_paths` 的建目录）会真的对**临时目录**执行 `tmutil addexclusion`——那是改系统状态，且本机
实测一次调用就是 10s 级。闸的做法是把 `paths.shutil.which` 钉成「找不到 tmutil」，于是
`ensure_home_dirs` 走便宜路径（连子进程都不起）。要测 tmutil 本身的用例自己桩
`paths.subprocess.run`（`test_paths.py` 的两条就是），它们在本夹具之后生效、互不冲突。

**刻意不管**另外两个 **import 期绑定**的常量（它们不经过上面三道闸，各有各的理由）：
`vector.MODEL_CACHE_DIR` 必须指向真实模型缓存（`-m slow` 的向量用例要用）；
`keys.CACHE_FILE` 的写点全在用例内自带 patch（2026-10-14 grep 复核过）。
"""
import pytest

from agent_wechat import exclusions, paths, store, tasks


@pytest.fixture(autouse=True)
def _forbid_real_tmutil(monkeypatch):
    """见模块 docstring「第四个闸」：测试期不起真实 tmutil。"""
    from types import SimpleNamespace
    monkeypatch.setattr(paths, "shutil", SimpleNamespace(which=lambda name: None))


@pytest.fixture(autouse=True)
def _isolate_index_db(tmp_path, monkeypatch):
    """每条用例一个自己的 HOME_DIR 与 index.db —— 谁也不可能写到用户真实的那个。"""
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    # 断言查的是**结果**，不是「patch 了哪些常量」——漏掉一条的那一刻就要当场红
    assert exclusions.path().is_relative_to(tmp_path), "排除清单会落到真实 ~/.agent-wechat"
    assert store.DB_PATH.is_relative_to(tmp_path), "索引库会落到真实 ~/.agent-wechat"
    assert tasks.TASKS_FILE.is_relative_to(tmp_path), "任务注册表会落到真实 ~/.agent-wechat"
