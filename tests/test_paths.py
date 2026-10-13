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


def _fresh_tm_state(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(paths, "_tm_exclusion_checked", False)
    monkeypatch.setattr(paths, "_tm_exclusion_ok", None)


def test_tm_exclusion_state_distinguishes_success_from_failure(tmp_path, monkeypatch):
    """EA-054：备份排除此前「置位在尝试之前 + 静默 except」——成功与失败在进程内完全
    不可区分，也没有任何命令报它（同为安全控制的「提交拦截」有 `scan.py --check-hooks`
    自检，这条却是全静默的）。现在留三态状态。

    把 `_tm_exclusion_ok = ...` 的两处赋值删掉（回到「只置 checked」）本测试必红。
    """
    _fresh_tm_state(monkeypatch, tmp_path)
    assert paths.time_machine_excluded() is None        # 还没建过目录 = 没尝试过

    monkeypatch.setattr(paths.shutil, "which", lambda name: "/usr/bin/tmutil")
    ran = []
    monkeypatch.setattr(paths.subprocess, "run",
                        lambda argv, **kw: ran.append(argv) or SimpleNamespace(returncode=0))
    paths.ensure_home_dirs()
    assert paths.time_machine_excluded() is True
    assert ran == [["tmutil", "addexclusion", str(paths.HOME_DIR)]]


def test_tm_exclusion_failure_is_recorded_not_swallowed(tmp_path, monkeypatch):
    """失败（tmutil 不在 / addexclusion 非零退出）记 False——`ensure_home_dirs` 本身
    照常返回（这条控制不该挡住任何命令），但状态要能被 status 报出来。"""
    _fresh_tm_state(monkeypatch, tmp_path)
    monkeypatch.setattr(paths.shutil, "which", lambda name: None)      # 便宜路径：不起子进程

    def _no_subprocess(*a, **k):
        raise AssertionError("tmutil 都不在，不该起子进程")
    monkeypatch.setattr(paths.subprocess, "run", _no_subprocess)
    paths.ensure_home_dirs()
    assert paths.time_machine_excluded() is False

    # 另一种失败形态：tmutil 在但 addexclusion 非零退出（check=True → CalledProcessError）
    _fresh_tm_state(monkeypatch, tmp_path)
    monkeypatch.setattr(paths.shutil, "which", lambda name: "/usr/bin/tmutil")

    def _fails(argv, **kw):
        raise paths.subprocess.CalledProcessError(1, argv)
    monkeypatch.setattr(paths.subprocess, "run", _fails)
    paths.ensure_home_dirs()
    assert paths.time_machine_excluded() is False


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
