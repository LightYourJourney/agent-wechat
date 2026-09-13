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


def test_plan_touches_nothing(tmp_path):
    """dry-run 的本质是「没动手」，不是「返回值对」（spec §7）。"""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 1000)
    before = sorted((p.name, p.stat().st_size, p.stat().st_mtime) for p in home.iterdir())
    purge.plan(frozenset({"index"}))
    # --all 是 plan() 唯一越过 lstat 的分支（要 iterdir 枚举顶层），必须一并验证：
    # 只测 all_=False 的话，往 --all 分支里加一次写入，本文件四个测试全都看不见。
    purge.plan(frozenset(), all_=True)
    after = sorted((p.name, p.stat().st_size, p.stat().st_mtime) for p in home.iterdir())
    assert before == after


def test_plan_marks_missing_artifacts(tmp_path):
    """产物不存在时标 exists=False，而不是报错（幂等的前半，spec §5.1 第 5 步）。"""
    p = purge.plan(frozenset({"keys"}))
    keys_items = [i for i in p.items if i.artifact.scope == "keys"]
    assert len(keys_items) == 1
    assert keys_items[0].exists is False
    assert keys_items[0].size == 0


def test_plan_index_has_three_items(tmp_path):
    """index 必须是三个独立 Artifact（基库/-wal/-shm），各自一行、各自 stat（spec §4）。"""
    p = purge.plan(frozenset({"index"}))
    assert [i.artifact.path.name for i in p.items] == \
        ["index.db", "index.db-wal", "index.db-shm"]
    assert all(i.artifact.kind == "file" for i in p.items)


def test_plan_all_enumerates_home_including_strays(tmp_path):
    """--all 的计划**不由 artifacts() 生成**，而是枚举 HOME_DIR 顶层，
    这样才列得出 models/、tasks.json 与散落文件（spec §3）。"""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 1000)        # 已知产物：必须被排除在 loose 之外
    (home / "leaked.db").write_bytes(b"stray")          # 模拟 decrypt --out 的散落明文
    (home / "models").mkdir()
    p = purge.plan(frozenset(), all_=True)
    assert p.all_ is True
    assert {x.name for x in p.loose} >= {"leaked.db", "models"}
    # 反断言：`if p not in known` 这条排除若被删掉，这个测试原本仍会通过，但已知产物
    # 会在 items 与 loose 里各出现一次，体积被重复计入 T4 的 freed（用户看到的「释放 N B」）。
    assert "index.db" not in {x.name for x in p.loose}


def test_execute_keys_does_not_touch_index(tmp_path):
    """反向断言：防范围泄漏。比「删了该删的」更重要（spec §7）。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "passphrase.json").write_text("{}")
    (home / "index.db").write_bytes(b"x" * 500)
    purge.execute(purge.plan(frozenset({"keys"})))
    assert not (home / "passphrase.json").exists()
    assert (home / "index.db").exists()


def test_execute_is_idempotent(tmp_path):
    home = tmp_path / "home"; home.mkdir(parents=True)
    r1 = purge.execute(purge.plan(frozenset({"keys"})))
    r2 = purge.execute(purge.plan(frozenset({"keys"})))
    assert r1.removed == ()
    assert r2.removed == ()          # 两次都成功，第二次什么都没删


def test_execute_all_removes_tree_and_does_not_recreate(tmp_path):
    """--all 删整树，且**不重建** HOME_DIR（spec §5.1 第 6 步）。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 100)
    (home / "leaked.db").write_bytes(b"stray")     # 不属于任何具名范围
    (home / "models").mkdir()
    r = purge.execute(purge.plan(frozenset(), all_=True))
    assert not home.exists()
    assert r.home_removed is True


def test_execute_all_on_missing_home_is_idempotent(tmp_path):
    """HOME_DIR 不存在时 --all 仍成功（AC4）。rmtree 对缺失路径会抛 FileNotFoundError，
    spec §5.1 第 5 步要求把它纳入幂等规则。"""
    r = purge.execute(purge.plan(frozenset(), all_=True))
    assert r.home_removed is False
    assert r.failed == ()


def test_execute_all_reports_what_it_removed(tmp_path):
    """--all 走整树 rmtree，绕过了 items 循环。若不在此补齐 removed/freed，
    CLI 会在删光整棵 HOME_DIR 之后打印「已删除 0 项，释放 0 B」——与 spec §3/§6
    要求的「删了什么 + 释放多少」直接冲突。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 4096)
    (home / "leaked.db").write_bytes(b"y" * 100)     # 散落文件也计入体积
    r = purge.execute(purge.plan(frozenset(), all_=True))
    assert r.freed == 4196
    assert {p.name for p in r.removed} >= {"index.db", "leaked.db"}
