"""paths 的路径契约测试。

C8 修正：`key_info_db` 的 docstring 写的是 `key_info.db`（明文 SQLite），实现却返回
`key_info.dat`——两个文件在真实目录里同时存在且语义不同（.dat 是每次登录重写的
单个 blob，.db 是带 -wal/-shm 的明文 SQLite）。以 docstring 为准修正为 `.db`。
"""
import pathlib
from types import SimpleNamespace

import pytest

from agent_wechat import errors, paths

ACC = "wxid_testacct01_ab12"
BARE = "wxid_testacct01"


def test_key_info_db_points_at_dot_db():
    p = paths.key_info_db(ACC)
    assert p.name == "key_info.db"
    assert p == paths.XWECHAT_FILES / "all_users" / "login" / BARE / "key_info.db"
    assert p != paths.key_info_dat(ACC)          # 与 app_data 的 .dat 不是同一份


def test_account_dirs_distinguishes_unreadable_from_absent(tmp_path, monkeypatch):
    """EA-002：`is_dir()` 把 OSError 静默吞成 `False`——「不可搜索/无权限」于是被当成
    「不存在」返回 `[]`（微信明明装着，工具报「没找到账号」）。判不出来 ≠ 不存在：
    前者抛项目异常（hint 指向完全磁盘访问权限），后者才是 `[]`。

    把 `_listdir_or_project_error` 换回 `is_dir()` 早退，第二条断言必红。
    """
    root = tmp_path / "xwechat_files"
    (root / ACC).mkdir(parents=True)
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    assert paths.account_dirs() == [ACC]

    monkeypatch.setattr(paths, "XWECHAT_FILES", tmp_path / "nope")
    assert paths.account_dirs() == []                  # 真不在 = 正常形态

    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    root.chmod(0o000)
    try:
        with pytest.raises(errors.HomeUnavailable) as ei:
            paths.account_dirs()
        assert "完全磁盘访问权限" in (ei.value.hint or "")
    finally:
        root.chmod(0o700)                              # 别让 pytest 的清理失败


def test_shard_and_db_probes_distinguish_unreadable_from_absent(tmp_path, monkeypatch):
    """EA-002（另两个探针）：同一形状。判不出来 → 项目异常；真不在 → `[]`。"""
    db_root = tmp_path / "xwechat_files" / ACC / "db_storage"
    (db_root / "message").mkdir(parents=True)
    (db_root / "message" / "message_0.db").write_bytes(b"")
    (db_root / "contact").mkdir()
    (db_root / "contact" / "contact.db").write_bytes(b"")
    monkeypatch.setattr(paths, "XWECHAT_FILES", tmp_path / "xwechat_files")

    assert [p.name for p in paths.message_shards(ACC)] == ["message_0.db"]
    # `*/*.db` 覆盖 db_storage 下所有子目录：contact 与 message 两处都算
    assert paths.all_dbs(ACC) == [db_root / "contact" / "contact.db",
                                db_root / "message" / "message_0.db"]

    monkeypatch.setattr(paths, "XWECHAT_FILES", tmp_path / "missing")
    assert paths.message_shards(ACC) == [] and paths.all_dbs(ACC) == []

    monkeypatch.setattr(paths, "XWECHAT_FILES", tmp_path / "xwechat_files")
    message_dir = db_root / "message"
    message_dir.chmod(0o000)
    try:
        with pytest.raises(errors.HomeUnavailable):
            paths.message_shards(ACC)
    finally:
        message_dir.chmod(0o700)


def test_time_machine_excluded_queries_the_real_state(tmp_path, monkeypatch):
    """F2（T9 复审，P2）：EA-054 原修法记的是「上次 `addexclusion` 尝试的成败」——**进程内**
    状态，而 CLI 一次调用一个进程，唯一消费点 `status` 于是恒拿到 None（那行永不打印）；
    即便拿到，本机形态也是**假 ✗**（实测 addexclusion 对已存在目录需 ~11s > timeout=10）。
    改成只读查询 `tmutil isexcluded`：回答的是「**现在**排没排除」，与 purge 的核查同源。

    把函数换回「返回上次尝试的成败」本测试必红（它会返回 None）。
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(paths.shutil, "which", lambda name: "/usr/bin/tmutil")
    seen = []
    monkeypatch.setattr(paths.subprocess, "run", lambda argv, **kw: (
        seen.append(argv), SimpleNamespace(returncode=0, stdout="[Excluded] /tmp/x\n"))[1])

    # 真实形状：tmutil 的原文整段（含路径）——与 purge 的 `tmutil isexcluded` 核查同口径
    assert paths.time_machine_excluded() == "[Excluded] /tmp/x"
    assert seen == [["tmutil", "isexcluded", str(home)]], "只读查询，参数是 isexcluded"

    # 三种「判不出来」：非零退出 / 探测抛错 / 没有 tmutil —— 一律 None，不许当成「没排除」
    monkeypatch.setattr(paths.subprocess, "run",
                        lambda argv, **kw: SimpleNamespace(returncode=1, stdout=""))
    assert paths.time_machine_excluded() is None
    monkeypatch.setattr(paths.subprocess, "run",
                        lambda argv, **kw: (_ for _ in ()).throw(TimeoutError()))
    assert paths.time_machine_excluded() is None
    monkeypatch.setattr(paths.shutil, "which", lambda name: None)
    assert paths.time_machine_excluded() is None

    # 目录还没建：谈不上排除（不查、不印）
    monkeypatch.setattr(paths.shutil, "which", lambda name: "/usr/bin/tmutil")
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path / "nope")
    assert paths.time_machine_excluded() is None


def test_ensure_home_dirs_tries_the_exclusion_but_never_blocks(tmp_path, monkeypatch):
    """首次建目录仍是**唯一**的排除时机，所以那条 `addexclusion` 保留（尽力而为）；
    但它的成败不再当状态——失败不许挡住任何命令，也不该被报成「没排除」。"""
    home = tmp_path / "home"
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(paths, "_tm_exclusion_checked", False)
    monkeypatch.setattr(paths.shutil, "which", lambda name: "/usr/bin/tmutil")
    ran = []
    monkeypatch.setattr(paths.subprocess, "run",
                        lambda argv, **kw: ran.append(argv) or SimpleNamespace(returncode=0))
    paths.ensure_home_dirs()
    assert ran == [["tmutil", "addexclusion", str(home)]]
    assert home.is_dir()

    # 失败形态（超时）：照常返回，目录照旧可用
    monkeypatch.setattr(paths, "_tm_exclusion_checked", False)

    def _boom(argv, **kw):
        raise TimeoutError("tmutil 卡住")
    monkeypatch.setattr(paths.subprocess, "run", _boom)
    paths.ensure_home_dirs()
    assert home.is_dir()

    # 没有 tmutil：便宜路径，连子进程都不起。**探针用「记录」而不是「抛」**——实现的
    # `except Exception: pass` 会把抛出来的 AssertionError 一起吞掉，那条断言就永远不红
    # （变异 M5 实测如此）。
    monkeypatch.setattr(paths, "_tm_exclusion_checked", False)
    monkeypatch.setattr(paths.shutil, "which", lambda name: None)
    calls = []
    monkeypatch.setattr(paths.subprocess, "run", lambda *a, **k: calls.append(a))
    paths.ensure_home_dirs()
    assert home.is_dir()
    assert calls == [], f"tmutil 都不在，不该起子进程：{calls}"


def test_all_dbs_does_not_swallow_a_subdirectory_eacces(tmp_path, monkeypatch):
    """F8（T9 复审，P3）：`all_dbs` 此前用 `root.glob("*/*.db")`——CPython 的 glob 在**某个
    子目录** scandir 失败时静默跳过它，于是「整个 message/ 读不出来」被报成「这个账号没有
    message 分片」（少报；并直接喂坏 EA-051 的 unreadable 计数与 `HMAC N/N` 的分母）。

    逐层列举、每层过同一个 helper：读不到仍是项目异常，真没有才是空列表（EA-002 的口径）。
    把实现换回 `root.glob("*/*.db")` 本测试必红。
    """
    db_root = tmp_path / "xwechat_files" / ACC / "db_storage"
    (db_root / "message").mkdir(parents=True)
    (db_root / "message" / "message_0.db").write_bytes(b"")
    (db_root / "contact").mkdir()
    (db_root / "contact" / "contact.db").write_bytes(b"")
    monkeypatch.setattr(paths, "XWECHAT_FILES", tmp_path / "xwechat_files")
    assert len(paths.all_dbs(ACC)) == 2

    (db_root / "message").chmod(0o000)
    try:
        with pytest.raises(errors.HomeUnavailable) as ei:
            paths.all_dbs(ACC)
        assert "完全磁盘访问权限" in (ei.value.hint or "")
    finally:
        (db_root / "message").chmod(0o700)


def test_ensure_home_dirs_rejects_a_relative_home(tmp_path, monkeypatch):
    """EA-003：相对路径的 HOME_DIR 会让密钥缓存与明文索引落到**当前工作目录**——
    `ensure_home_dirs` 是路径的收口处，必须在这里拦下（项目异常 + hint）。"""
    monkeypatch.chdir(tmp_path)          # 相对路径关进 tmp——变异时也不许脏仓库 CWD
    monkeypatch.setattr(paths, "HOME_DIR", pathlib.Path("relative/home"))
    with pytest.raises(errors.HomeUnavailable) as ei:
        paths.ensure_home_dirs()
    assert "绝对路径" in str(ei.value)
    assert not (tmp_path / "relative").exists(), "判据必须在 mkdir 之前拦住"


def test_agent_wechat_home_empty_uses_the_default():
    """EA-003：`AGENT_WECHAT_HOME=""` 曾被解析成当前目录（`Path("")` == `.`）。
    模块常量在 import 期求值，只能用子进程验（与条目测试建议一致）。"""
    import os
    import pathlib as _pl
    import subprocess
    import sys

    env = {**os.environ, "AGENT_WECHAT_HOME": ""}
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'src'); from agent_wechat import paths;"
         "print(paths.HOME_DIR)"],
        capture_output=True, text=True, env=env, cwd=str(_pl.Path.cwd()))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(_pl.Path.home() / ".agent-wechat")


def test_ensure_home_dirs_raises_project_error_on_symlink_loop(tmp_path, monkeypatch):
    """AGENT_WECHAT_HOME 指向符号链接环时 mkdir 抛 FileExistsError——`exist_ok=True` 挡不住：
    CPython 的实现是 `except FileExistsError: if not exist_ok or not self.is_dir(): raise`，
    而 is_dir() 对环返回 False，于是原样抛出。

    不翻译的话它穿透适配层——CLI 那边是一整个 traceback，MCP 那边是「内部错误:
    FileExistsError: ...」（generic_payload 兜底，不带 hint）。而 errors.py 开篇那条契约是
    「core 只抛 AgentWeChatError 子类」。"""
    a = tmp_path / "a"; b = tmp_path / "b"
    a.symlink_to(b); b.symlink_to(a)
    monkeypatch.setattr(paths, "HOME_DIR", a)
    with pytest.raises(errors.HomeUnavailable) as ei:
        paths.ensure_home_dirs()
    assert "不可用" in str(ei.value)


def test_every_home_dir_path_owner_goes_through_the_gate():
    """EA-003 的收口项（T16 之后补）：**模块级的 `HOME_DIR` 派生常量**——那条路径的 owner
    ——必须自己调用 `paths.ensure_home_dirs()`。

    判据是「谁拥有这条路径，谁负责过闸」：owner 是唯一知道该往哪写的人。此前 `vector`
    （`HOME_DIR/models` 的 owner）不调它，只靠 `store.open()` **间接**过关——相对
    `AGENT_WECHAT_HOME` 那条确实会被拦住，但那是**传递性**保证：换一条不碰索引的入口，
    `MODEL_CACHE_DIR.mkdir()` 就会把模型落到进程的当前工作目录（EA-003 的原形态）。

    **豁免只有一个**：`paths.py` 自己（闸门的实现与 `HOME_DIR` 的定义处）。零豁免表是刻意的
    ——名单一旦要维护，「新 owner 忘了登记」就重新变成可能，而这条护栏要防的正是那个。

    **它覆盖不到的面**（写清楚，免得被读成全覆盖）：就地拼 `paths.HOME_DIR / "x"` 而**不**建
    模块常量的新写者。判据只认「模块级常量」这个形态——它是本仓四种 owner 的**共同**形态，
    而按行为驱动每个写入口成本高得多（见 `内部审计记录` ⑦）。
    """
    import re
    root = pathlib.Path(paths.__file__).parent
    owners = {}
    for f in sorted(root.glob("*.py")):
        m = re.search(r"^[A-Z_]+ = paths\.HOME_DIR", f.read_text(), re.M)
        if m:
            owners[f.name] = f
    # `paths.py` 不在里面是对的：它**定义** `HOME_DIR`（`pathlib.Path(...)`），不是从它派生。
    assert set(owners) == {"keys.py", "store.py", "tasks.py", "vector.py"}, \
        f"owner 名单变了，先核一遍再改这条：{sorted(owners)}"
    missing = [n for n, f in owners.items() if "ensure_home_dirs()" not in f.read_text()]
    assert not missing, f"这些 owner 没过闸（它们的产物会落到当前工作目录）：{missing}"
