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

**只闸 `store.DB_PATH`。** 其余 import 期绑定的 HOME 派生路径**刻意不管**：
`vector.MODEL_CACHE_DIR` 必须指向真实模型缓存（`-m slow` 的向量用例要用），
`keys.CACHE_FILE` / `tasks.TASKS_FILE` 的写入路径不在本任务的故障面内。
"""
import pytest

from agent_wechat import store


@pytest.fixture(autouse=True)
def _isolate_index_db(tmp_path, monkeypatch):
    """每条用例一个自己的 index.db —— 谁也不可能写到用户真实的那个。"""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "index.db")
