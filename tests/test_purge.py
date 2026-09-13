"""purge 的单元测试。**本文件每个测试都不得指向真实 ~/.agent-wechat。**

红线在下面的 autouse fixture 里：它把四个路径常量与 paths.HOME_DIR 全部指向 tmp_path，
并断言 artifacts() 解析出的路径确实不在真实数据目录下。写成 autouse 而不是单个测试函数
是有意的——单独跑本文件里任何一个测试时，保护都必须生效（spec §7）。
"""
import pytest

from agent_wechat import decrypt, keys, paths, purge, store, tasks, vector

# 导入时取值：此刻 autouse fixture 尚未 patch，paths.HOME_DIR 就是真实路径。
# 不重述 paths 的解析逻辑（它读 AGENT_WECHAT_HOME）——那正是 purge.py 明令禁止的第二份来源。
REAL_HOME = paths.HOME_DIR


@pytest.fixture(autouse=True)
def _never_touch_real_home(monkeypatch, tmp_path):
    """红线：把全部路径常量指向 tmp_path，并断言不在真实数据目录下。"""
    home = tmp_path / "home"
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")
    monkeypatch.setattr(tasks, "TASKS_FILE", home / "tasks.json")
    monkeypatch.setattr(vector, "MODEL_CACHE_DIR", home / "models")
    arts = purge.artifacts()
    assert len(arts) == 5, \
        f"红线：清单条目数由 5 变为 {len(arts)}，必有产物未被保护，须先核对再改这个数"
    for a in arts:
        assert a.path != REAL_HOME and REAL_HOME not in a.path.parents, \
            f"红线：{a.path} 指向真实数据目录"


def test_db_paths_is_the_wal_trio(monkeypatch, tmp_path):
    """index 是文件族：基库 + WAL + SHM。三者缺一都会在异常路径下留明文（spec §2）。"""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "index.db")
    got = store.db_paths()
    assert got == (tmp_path / "index.db",
                   tmp_path / "index.db-wal",
                   tmp_path / "index.db-shm")
