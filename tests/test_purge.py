"""purge 的单元测试。**本文件每个测试都不得指向真实 ~/.agent-wechat。**

红线在下面的 autouse fixture 里：它把四个路径常量与 paths.HOME_DIR 全部指向 tmp_path，
并断言 artifacts() 解析出的路径确实不在真实数据目录下。写成 autouse 而不是单个测试函数
是有意的——单独跑本文件里任何一个测试时，保护都必须生效（spec §7）。
"""
import pytest

from agent_wechat import decrypt, errors, keys, paths, purge, store, tasks, vector

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
    # spec §7：lsof 注入桩。必须打在 **_run** 这一层，不能把 _holders 整个换掉——
    # 本文件里的 test_holders_parses_lsof_output 要调的正是真 _holders，autouse fixture
    # 先执行会把它替换成桩，那个测试就永远拿不到真函数。
    monkeypatch.setattr(purge, "_run",
                        lambda *a, **kw: type("R", (), {"stdout": ""})())
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
    # 会在 items 与 loose 里各出现一次——计划表会把同一个路径渲染两遍。
    # （注意别把它说成「体积重复计入 freed」：--all 的 freed 是 _tree_size 走盘量出来的，
    #   不是 items+loose 求和，所以重复项不会让 freed 翻倍。）
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
    # 必须先放一个真产物：home 空着的话两次都会走「跳过不存在项」的短路，
    # execute() 就算一个字节都不删，这个测试照样通过——它测的就不是幂等了。
    (home / "passphrase.json").write_text("{}")
    r1 = purge.execute(purge.plan(frozenset({"keys"})))
    r2 = purge.execute(purge.plan(frozenset({"keys"})))
    assert r1.removed == (home / "passphrase.json",)   # 第一次真删了
    assert r2.removed == ()                            # 第二次什么都没删
    assert r2.freed == 0


def test_execute_continues_after_one_failure(tmp_path, monkeypatch):
    """部分失败不中止：某个路径删不掉，不应放弃其余（spec §5.1 第 4 步）。
    这正是提交信息里写的行为——把 except 分支里的 append 删掉，本测试即失败。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 10)
    (home / "index.db-wal").write_bytes(b"y" * 20)
    real_remove = purge._remove

    def flaky(item):
        if item.artifact.path.name == "index.db":
            raise PermissionError("模拟：该文件删不掉")
        return real_remove(item)

    monkeypatch.setattr(purge, "_remove", flaky)
    r = purge.execute(purge.plan(frozenset({"index"})))
    assert [p.name for p, _ in r.failed] == ["index.db"]
    assert not (home / "index.db-wal").exists()        # 后面那一项仍然被删了


def test_execute_tolerates_stale_plan(tmp_path):
    """CLI 是「先 dry-run 出计划 → 用户确认 → 再 execute」，两步之间产物可能已被删掉。
    spec §5.1 第 5 步要求这算成功，不是失败。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "passphrase.json").write_text("{}")
    p = purge.plan(frozenset({"keys"}))
    (home / "passphrase.json").unlink()                # 计划之后、执行之前没了
    r = purge.execute(p)
    assert r.failed == ()
    assert r.removed == ()


def test_execute_dir_reports_tree_size_not_inode_size(tmp_path):
    """`purge --tmp` 删的是解密明文临时目录。目录的 lstat().st_size 只是 inode 自身
    大小（几十到几百字节），若直接相加，无论删掉多少明文都报「释放 ~96 B」。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    tmpdir = home / "tmp"
    tmpdir.mkdir()
    (tmpdir / "a.db").write_bytes(b"a" * 3000)         # 模拟散落的解密明文
    (tmpdir / "b.db").write_bytes(b"b" * 1000)
    p = purge.plan(frozenset({"tmp"}))
    item = p.items[0]
    assert item.artifact.kind == "dir"
    assert item.size < 2000, "前提：目录的 st_size 是 inode 大小，远小于整树体积"
    r = purge.execute(p)
    assert r.freed == 4000
    assert not tmpdir.exists()


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


def test_execute_refuses_when_index_is_held(tmp_path, monkeypatch):
    """有进程持有 index.db 时拒绝执行。**不要用 tasks 注册表判断**：tasks._ensure_loaded()
    载入时就把 running 收割成 failed（tasks.py:62-64，test_tasks.py:115-129 固化），
    独立进程永远看不到 running——那条路是死的（spec §5.1 第 1 步）。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 100)
    monkeypatch.setattr(purge, "_holders", lambda p: [4242] if p.name == "index.db" else [])
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute(purge.plan(frozenset({"index"})))
    assert "4242" in str(ei.value)
    assert (home / "index.db").exists()          # 一个字节都没删


def test_execute_refuses_symlink_and_leaves_target(tmp_path):
    """符号链接必须拒绝：unlink 只删链接不删目标，而「列目录看到空了」的核查会给出
    false-clean——用户以为明文没了，其实还在（spec §5.1 第 2 步、AC7）。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    real = tmp_path / "elsewhere.db"
    real.write_bytes(b"plaintext" * 100)
    (home / "index.db").symlink_to(real)
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute(purge.plan(frozenset({"index"})))
    assert "符号链接" in str(ei.value)
    assert real.exists()                          # 目标必须完好无损


def test_holders_parses_lsof_output(tmp_path, monkeypatch):
    """lsof -n -P 不可省（否则 DNS 解析卡 30 秒，见 account.py:60 的既有教训）。"""
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return type("R", (), {"stdout": "COMMAND  PID USER\nWeChat 1234 leo\n"})

    monkeypatch.setattr(purge, "_run", fake_run)
    p = tmp_path / "index.db"; p.write_bytes(b"x")
    assert purge._holders(p) == [1234]
    assert seen["argv"][:3] == ["lsof", "-n", "-P"]


def test_execute_all_refuses_symlink_inside_home(tmp_path, monkeypatch):
    """--all 的整树 rmtree 会把树内的符号链接当作普通条目 unlink，**目标明文原封不动
    留在盘上**；核查却报「计划内路径已清空」——用户的 5 GB 明文还在外置盘上。
    实测于 Python 3.12.13：rmtree(含 symlink 的目录, onexc=...) 删掉树、保住目标。"""
    real = tmp_path / "elsewhere"; real.mkdir()
    (real / "secret.db").write_bytes(b"plaintext" * 100)
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "leaked.db").symlink_to(real / "secret.db")     # HOME_DIR 顶层的符号链接
    monkeypatch.setattr(paths, "HOME_DIR", home)
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute(purge.plan(frozenset(), all_=True))
    assert "符号链接" in str(ei.value)
    assert home.exists()                                # 一个字节都没删
    assert (real / "secret.db").exists()                # 目标明文完好


def test_execute_all_allows_intra_tree_symlink(tmp_path, monkeypatch):
    """HuggingFace 的 blob/snapshot 布局会在 models/ 下建指向**树内** blobs/ 的符号链接
    （本仓库 vector.py 用 huggingface_hub 下载模型就会产生，实测本机有 5 个）。
    目标在树内 → 整树 rmtree 会连它一起删，不存在 false-clean；一律拒绝会让
    `purge --all` 在标准安装上永远跑不起来，而 spec §9 又否掉了 --force。"""
    home = tmp_path / "home"
    blob = home / "models" / "blobs" / "abc123"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"weights")
    snap = home / "models" / "snapshots" / "rev1"
    snap.mkdir(parents=True)
    (snap / "model.onnx").symlink_to(blob)      # 树内链接：不得拒绝
    monkeypatch.setattr(paths, "HOME_DIR", home)
    r = purge.execute(purge.plan(frozenset(), all_=True))
    assert r.failed == ()
    assert not home.exists()                    # 整棵树连同 blob 一起没了


def test_execute_all_refuses_symlink_loop(tmp_path, monkeypatch):
    """符号链接环：3.12 的 Path.resolve 抛 RuntimeError（3.13 才改成 OSError）。
    接不住的话它会以裸异常穿透 execute()，违反「core 层只抛 AgentWeChatError 子类」。
    环本身不留东西，但属病态配置，拒绝比放行更容易解释——且与「判不出来就别删」一致。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    a, b = home / "a", home / "b"
    a.symlink_to(b); b.symlink_to(a)          # a ↔ b
    monkeypatch.setattr(paths, "HOME_DIR", home)
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute(purge.plan(frozenset(), all_=True))
    assert "符号链接" in str(ei.value)
    assert home.exists()                      # 一个字节都没删


def test_execute_all_tolerates_symlinked_home_ancestor(tmp_path, monkeypatch):
    """home 的**祖先**是符号链接时（macOS 上 /tmp → /private/tmp，$TMPDIR 与 mktemp -d
    给出的路径同理），link.resolve() 给的是真路径而 paths.HOME_DIR 未解析——两边形态不同
    会让树内链接被误判为逃逸、整片拒绝。home 自身不是链接，故 is_symlink() 那道闸门不触发，
    只有 home 也解析过才判得对。"""
    real_parent = tmp_path / "real"; real_parent.mkdir()
    link_parent = tmp_path / "link"; link_parent.symlink_to(real_parent)
    home = link_parent / "home"                 # home 自身不是链接，祖先是
    base = home / "models"
    blob = base / "blobs" / "abc"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"weights")
    (base / "snapshots").mkdir()
    (base / "snapshots" / "m.onnx").symlink_to(blob)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    r = purge.execute(purge.plan(frozenset(), all_=True))
    assert r.failed == ()
    assert not (real_parent / "home").exists()  # 整棵树被删掉


def test_execute_all_refuses_nested_symlink(tmp_path, monkeypatch):
    """符号链接不只在 HOME_DIR 顶层：树内任意深度都会被 rmtree 当普通条目 unlink，
    目标明文留存。spec §5.1 第 2 步要求对**每个待删路径**做 lstat，故必须走整棵树。"""
    real = tmp_path / "elsewhere"; real.mkdir()
    (real / "secret.db").write_bytes(b"plaintext" * 100)
    home = tmp_path / "home"; (home / "models").mkdir(parents=True)
    (home / "models" / "link.db").symlink_to(real / "secret.db")
    monkeypatch.setattr(paths, "HOME_DIR", home)
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute(purge.plan(frozenset(), all_=True))
    assert "符号链接" in str(ei.value)
    assert home.exists()                                # 一个字节都没删
    assert (real / "secret.db").exists()                # 目标明文完好


def test_execute_all_refuses_symlinked_home(tmp_path, monkeypatch):
    """HOME_DIR 本身是符号链接时 --all 必须拒绝（spec §5.1 第 2 步）。
    实测 3.12.13：rmtree(link) 不传 handler 抛 OSError；传了 onexc 则一条
    'Cannot call rmtree on a symbolic link' 进 handler，链接与目标都完好。"""
    real = tmp_path / "real-home"; real.mkdir()
    (real / "index.db").write_bytes(b"x" * 100)
    link = tmp_path / "home-link"; link.symlink_to(real)
    monkeypatch.setattr(paths, "HOME_DIR", link)
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute(purge.plan(frozenset(), all_=True))
    assert "符号链接" in str(ei.value)
    assert (real / "index.db").exists()          # 目标必须完好无损


def test_verify_does_not_call_kept_artifacts_residue(tmp_path):
    """**22 条评审里最严重的一条**：选择性 purge 后 index.db 是按范围故意保留的，
    拿「HOME_DIR 空不空」当判据会把它误报为残留，与 AC2 直接打架（spec §5.2）。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "passphrase.json").write_text("{}")
    (home / "index.db").write_bytes(b"x" * 100)
    plan = purge.plan(frozenset({"keys"}))
    purge.execute(plan)
    rep = purge.verify(plan)
    assert rep.not_removed == ()                       # 计划内的都删掉了
    assert any(p.name == "index.db" for p in rep.kept)  # 保留项归入 kept，不是残留


def test_verify_reports_unremoved_planned_paths(tmp_path):
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "passphrase.json").write_text("{}")
    plan = purge.plan(frozenset({"keys"}))
    rep = purge.verify(plan)                            # 没执行就核查
    assert [p.name for p in rep.not_removed] == ["passphrase.json"]


def test_verify_reports_surviving_loose_entry(tmp_path, monkeypatch):
    """--all 部分失败时活下来的 loose 顶层条目必须报为残留。它既不在 items 里、又被从
    kept 里排除，此前**两个桶都不进**，报告会说「计划内路径已清空」——
    正是 not_removed 这个字段存在的意义所在的那种假象（spec §5.2 第 1 项）。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 100)         # 具名产物
    (home / "leaked.db").write_bytes(b"stray")          # loose：不属于任何具名范围
    rep = purge.verify(purge.plan(frozenset(), all_=True))   # 什么都不删 = 全都没删掉
    names = {p.name for p in rep.not_removed}
    assert {"index.db", "leaked.db"} <= names, f"loose 存活项漏报，实际只报了 {names}"
    assert rep.kept == ()          # 两者都在计划内，不得落进「按范围保留」桶


def test_verify_reports_dangling_symlink_as_present(tmp_path):
    """断链的链接文件仍在盘上。exists() 跟随符号链接会判它「已删除」，而链接明明还在——
    在这门专门用来抓假象的字段里制造假象，故用 lexists。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "passphrase.json").symlink_to(home / "gone.json")   # 目标不存在
    rep = purge.verify(purge.plan(frozenset({"keys"})))
    assert [p.name for p in rep.not_removed] == ["passphrase.json"]


def test_verify_reports_still_held_index(tmp_path, monkeypatch):
    """held 是 spec §5.2 的第三项，但 fixture 的 _run 桩让 _holders 恒返回 []，
    于是它在全部 verify 测试里都是空字典——把 i.exists 的判据反写、或把键名写错，
    测试会全绿。故此处直接覆盖 _holders。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 100)
    (home / "index.db-wal").write_bytes(b"w" * 10)      # 存在，但无人持有
    monkeypatch.setattr(purge, "_holders",
                        lambda p: [4242] if p.name == "index.db" else [])
    rep = purge.verify(purge.plan(frozenset({"index"})))
    assert rep.held.get("index.db") == [4242]
    # 钉住 pids 的真值过滤：去掉 `and (pids := ...)` 的话本测试原本仍会通过，
    # 但渲染时会多出一行「⚠️ index.db-wal 仍被 pid  持有」（pid 为空）——一条对用户说的假话。
    assert "index.db-wal" not in rep.held


def test_verify_finds_tmp_lldb_leftovers(tmp_path, monkeypatch):
    """bootstrap.py:85 的 mkstemp 未传 dir=，落 $TMPDIR；进程被 kill 时残留。
    spec §2 那张表的第四行——核查必须实际检查它。"""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    (tmp_path / "awcap_abc123.lldb").write_text("script")
    rep = purge.verify(purge.plan(frozenset({"keys"})))
    assert any("awcap_" in x for x in rep.tmp_leaks)


def test_verify_degrades_when_tmutil_missing(tmp_path, monkeypatch):
    """tmutil 缺失/超时时该项报「无法判定」，不能让整个核查崩掉（spec §5.2）。"""
    def boom(*a, **k):
        raise FileNotFoundError("tmutil")

    monkeypatch.setattr(purge, "_run", boom)
    rep = purge.verify(purge.plan(frozenset({"keys"})))
    assert "无法判定" in rep.tm_excluded
