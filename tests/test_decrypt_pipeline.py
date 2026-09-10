"""集成测试：合成加密库走通 snapshot → 派生 → sqlcipher 解密 → 只读打开全链路。

用 passphrase 文本形式加密（与微信场景一致：sqlcipher 默认参数 = SQLCipher 4），
验证 decrypt.decrypted() 对"微信式"库的完整处理。
"""
import sqlite3
import subprocess

import pytest

from agent_wechat import decrypt, keys


@pytest.fixture()
def fake_wechat_db(tmp_path):
    """造一个带消息表的库，再用 32 字节 passphrase 加密成微信式库。"""
    plain = tmp_path / "plain.db"
    con = sqlite3.connect(plain)
    con.execute("CREATE TABLE Chat_Msg(ChatName TEXT, MesLocalID INTEGER, Message TEXT, CreateTime INTEGER)")
    con.executemany("INSERT INTO Chat_Msg VALUES (?,?,?,?)",
                    [("contact_wxid", i, f"消息{i}", 1700000000 + i) for i in range(10)])
    con.commit()
    con.close()

    passphrase = b"wechat-like-passphrase-0123456789ab"   # 文本形式 → sqlcipher 走 PBKDF2，与微信一致
    enc = tmp_path / "message_9.db"
    subprocess.run(["sqlcipher", str(plain),
                    f"ATTACH DATABASE '{enc}' AS e KEY '{passphrase.decode()}';",
                    "SELECT sqlcipher_export('e');", "DETACH DATABASE e;"],
                   check=True, capture_output=True)
    # 密文头必须是随机字节（非 SQLite 头），且我们的 HMAC 公式认这把钥匙
    assert enc.read_bytes()[:16] != b"SQLite format 3\x00"
    p1 = enc.read_bytes()[:4096]
    enc_key = keys.derive_key(passphrase, p1[:keys.SALT_SZ])
    assert keys.page1_hmac_ok(enc_key, p1), "HMAC 公式应认可该密钥"
    return enc, passphrase


def test_decrypt_pipeline(fake_wechat_db, tmp_path, monkeypatch):
    enc, passphrase = fake_wechat_db
    monkeypatch.setattr(keys.paths, "HOME_DIR", tmp_path / "home")

    with decrypt.decrypted(enc, passphrase) as plain:
        con = sqlite3.connect(f"file:{plain}?mode=ro", uri=True)
        n, = con.execute("SELECT count(*) FROM Chat_Msg").fetchone()
        last = con.execute("SELECT Message FROM Chat_Msg ORDER BY MesLocalID DESC LIMIT 1").fetchone()
        con.close()
    assert n == 10 and last[0] == "消息9"


def test_wrong_key_fails(fake_wechat_db, tmp_path, monkeypatch):
    enc, _ = fake_wechat_db
    monkeypatch.setattr(keys.paths, "HOME_DIR", tmp_path / "home")
    with pytest.raises(RuntimeError, match="sqlcipher 解密失败"):
        with decrypt.decrypted(enc, bytes(range(32, 64))):
            pass
