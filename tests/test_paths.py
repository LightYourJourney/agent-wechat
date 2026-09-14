"""paths 的路径契约测试。

C8 修正：`key_info_db` 的 docstring 写的是 `key_info.db`（明文 SQLite），实现却返回
`key_info.dat`——两个文件在真实目录里同时存在且语义不同（.dat 是每次登录重写的
单个 blob，.db 是带 -wal/-shm 的明文 SQLite）。以 docstring 为准修正为 `.db`。
"""
import pytest

from agent_wechat import errors, paths

ACC = "wxid_testacct01_ab12"
BARE = "wxid_testacct01"


def test_key_info_db_points_at_dot_db():
    p = paths.key_info_db(ACC)
    assert p.name == "key_info.db"
    assert p == paths.XWECHAT_FILES / "all_users" / "login" / BARE / "key_info.db"
    assert p != paths.key_info_dat(ACC)          # 与 app_data 的 .dat 不是同一份


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
