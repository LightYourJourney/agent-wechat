"""真机：两个解密后端逐库逐表读出同样的内容（默认不跑：uv run pytest tests/ -m real -v）。

进程内后端替代了「整库 sqlcipher_export」——它必须在真实的微信库上与 CLI 后端逐字节一致，
合成库覆盖不到真实库里的自定义分词器 FTS 虚表、WITHOUT ROWID 表、非 UTF-8 TEXT 等形态。
只算摘要，不打印任何正文；两条路径都只在 HOME/tmp 留用后即焚的快照。
"""
import hashlib
import sqlite3

import pytest

from agent_wechat import decrypt, keys, paths

pytestmark = [pytest.mark.slow, pytest.mark.real]


def _real_account() -> str:
    if not paths.XWECHAT_FILES.is_dir():
        pytest.skip("本机没有真实微信树")
    for acct in paths.account_dirs():
        try:
            keys.get_passphrase(acct)
        except keys.KeyNotFound:
            continue
        return acct
    pytest.skip("没有已缓存 passphrase 的账号（先 bootstrap）")


def _digest(con) -> dict[str, str]:
    con.text_factory = bytes
    out = {}
    for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        t = name.decode()
        try:
            rows = [repr(r) for r in con.execute(f'SELECT * FROM "{t}"')]
            out[t] = hashlib.sha256("\n".join(sorted(rows)).encode()).hexdigest()
        except sqlite3.Error as exc:         # 自定义分词器的 FTS 虚表：两边必须同样读不了
            out[t] = f"ERR:{type(exc).__name__}"
    return out


def test_inproc_and_cli_backends_agree_on_every_real_db(monkeypatch):
    acct = _real_account()
    pp = keys.get_passphrase(acct)
    dbs = paths.all_dbs(acct)
    assert dbs
    for db in dbs:
        # 每个库先冻结一份快照：微信在跑，两次读取之间可能进新消息
        monkeypatch.delenv(decrypt.BACKEND_ENV, raising=False)
        with decrypt._keyed_snapshot(db, pp) as (_td, frozen, _key):
            with decrypt.open_db(frozen, pp) as con:
                a = _digest(con)
            monkeypatch.setenv(decrypt.BACKEND_ENV, "cli")
            with decrypt.open_db(frozen, pp) as con:
                b = _digest(con)
        assert a == b, db.name
