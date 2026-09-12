"""paths 的路径契约测试。

C8 修正：`key_info_db` 的 docstring 写的是 `key_info.db`（明文 SQLite），实现却返回
`key_info.dat`——两个文件在真实目录里同时存在且语义不同（.dat 是每次登录重写的
单个 blob，.db 是带 -wal/-shm 的明文 SQLite）。以 docstring 为准修正为 `.db`。
"""
from agent_wechat import paths

ACC = "wxid_testacct01_ab12"
BARE = "wxid_testacct01"


def test_key_info_db_points_at_dot_db():
    p = paths.key_info_db(ACC)
    assert p.name == "key_info.db"
    assert p == paths.XWECHAT_FILES / "all_users" / "login" / BARE / "key_info.db"
    assert p != paths.key_info_dat(ACC)          # 与 app_data 的 .dat 不是同一份
