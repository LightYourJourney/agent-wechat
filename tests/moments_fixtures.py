"""朋友圈相关测试的共享夹具（EA-117：此前 `ACCT` / `_passthrough` / `env` 在 3–4 个文件里
各留一份副本；改一处要记得改四处的形状与 test_cli 的做法相反）。

**XML 刻意不共享**：三个文件各自的 `<TimelineObject>` 形态不同（好友 wxid / 空 mediaList /
带 token 的 f-string），统一会改变各文件测的东西。`env` 因此用 `request.module.XML` 取
**调用方文件自己的** XML。
"""
from __future__ import annotations

import contextlib
import json
import sqlite3

import pytest

from agent_wechat import keys, paths

ACCT = "wxid_testacct01_ab12"


@contextlib.contextmanager
def _passthrough(db_path, passphrase):
    """`decrypt.decrypted` 的直通桩（原样搬自四个文件）。

    **不带 `tmpdir=` 形参**（EA-048 已把那个形参从 `decrypt.decrypted` 删掉，全仓零调用方）：
    review-code 的 Standards 轴 #11 指出，上一版留着它并给了一句「外部桩同签名」的理由——
    而那是个**编出来的理由**（签名早就不一样了），死形参被新共享夹具「合法化」了。
    """
    yield db_path


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
    d.mkdir(parents=True)
    home.mkdir()
    # `iter_posts` 经 `keys.get_passphrase` 取钥；不隔离就会读到**真实机器的**
    # passphrase.json。
    (home / "passphrase.json").write_text(json.dumps(
        {"accounts": {"wxid_testacct01": {"passphrase_hex": "aa" * 32, "captured_at": 0}}}))
    monkeypatch.setattr(paths, "XWECHAT_FILES", wx)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    monkeypatch.setattr(dec_mod, "decrypted", _passthrough)
    # 超集：三个调用方原来各自 patch 一或两个模块，统一成两个（raising=False 对谁都不伤）
    for mod in ("agent_wechat.moments", "agent_wechat.usecases"):
        monkeypatch.setattr(f"{mod}.decrypt", dec_mod, raising=False)
    con = sqlite3.connect(d / "sns.db")
    con.execute("CREATE TABLE SnsTimeLine(tid INTEGER PRIMARY KEY DESC, user_name TEXT,"
                " content TEXT, pack_info_buf TEXT)")
    con.execute("INSERT INTO SnsTimeLine(tid, user_name, content) VALUES (1,'u',?)", (xml,))
    con.commit()
    con.close()
    return home
