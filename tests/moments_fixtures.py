"""朋友圈相关测试的共享夹具（EA-117：此前 `ACCT` / `_passthrough` / `env` 在 3–4 个文件里
各留一份副本；改一处要记得改四处的形状与 test_cli 的做法相反）。

**XML 刻意不共享**：三个文件各自的 `<TimelineObject>` 形态不同（好友 wxid / 空 mediaList /
带 token 的 f-string），统一会改变各文件测的东西。`env` 因此用 `request.module.XML` 取
**调用方文件自己的** XML。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from agent_wechat import keys, paths
from helpers import plain_open_db

ACCT = "wxid_testacct01_ab12"


# `decrypt.open_db` 的直通桩（合成 sns.db 是明文，直接只读打开）。保留这个名字只因三个
# 朋友圈测试模块按它导入；实现就是 helpers 里那一份，不再包一层。
_passthrough = plain_open_db


@pytest.fixture()
def env(tmp_path, monkeypatch, request):
    # `request.module.XML`：各文件自己的 XML（形态不同，刻意不统一）。
    return build_env(tmp_path, monkeypatch, request.module.XML)


def build_env(tmp_path, monkeypatch, xml):
    """抽成普通函数（同 `test_media.make_media_tree` / `build_fake_wechat_db`）：
    矩阵驱动也要同一形态的环境，两份会漂移。"""
    from agent_wechat import decrypt as dec_mod

    wx, home = tmp_path / "wx", tmp_path / "home"
    d = wx / ACCT / "db_storage" / "sns"
    d.mkdir(parents=True, exist_ok=True)
    home.mkdir(exist_ok=True)
    # `iter_posts` 经 `keys.get_passphrase` 取钥；不隔离就会读到**真实机器的**
    # passphrase.json。
    (home / "passphrase.json").write_text(json.dumps(
        {"accounts": {"wxid_testacct01": {"passphrase_hex": "aa" * 32, "captured_at": 0}}}))
    monkeypatch.setattr(paths, "XWECHAT_FILES", wx)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    monkeypatch.setattr(dec_mod, "open_db", _passthrough)
    # 超集：三个调用方原来各自 patch 一或两个模块，统一成两个（raising=False 对谁都不伤）
    for mod in ("agent_wechat.moments", "agent_wechat.usecases"):
        monkeypatch.setattr(f"{mod}.decrypt", dec_mod, raising=False)
    con = sqlite3.connect(d / "sns.db")
    # 幂等：崩溃一致性用例会在同一状态上重跑（spec §4.5）。
    con.execute("CREATE TABLE IF NOT EXISTS SnsTimeLine(tid INTEGER PRIMARY KEY DESC,"
                " user_name TEXT, content TEXT, pack_info_buf TEXT)")
    con.execute("INSERT OR REPLACE INTO SnsTimeLine(tid, user_name, content) VALUES (1,'u',?)",
                (xml,))
    con.commit()
    con.close()
    return home
