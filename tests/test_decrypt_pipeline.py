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


def test_the_key_never_reaches_argv_and_a_truncation_cannot_ship_a_fragment(
        tmp_path, monkeypatch):
    """EA-053 + EA-047。

    ① 派生密钥不再经 **argv** 交给 sqlcipher——命令改走 stdin（另一本地用户/进程在
       sqlcipher 存活窗口内 `ps` 可见的暴露面消失）。把 input= 退回 argv，第一条断言必红。
    ② sqlcipher 的 stderr 在**脱敏之前**不许截断：`[:300]` 曾把跨过截断点的密钥切成
       「前半段」，而 redact 需要完整 64 位才认得出（截断必须发生在脱敏之后）。把
       `[:300]` 加回消息构造，第二条断言必红。
    """
    import re

    from agent_wechat import errors

    db = tmp_path / "message_0.db"
    db.write_bytes(b"x" * 8192)                     # ≥1 页，过「不足一页」判据
    seen = {}

    class _R:
        returncode = 1
        stdout = ""
        stderr = "x" * 260 + "ab" * 32 + "tail"     # 密钥挂在 260 偏移：截断点落在密钥**内部**

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return _R()

    monkeypatch.setattr(decrypt.subprocess, "run", fake_run)
    with pytest.raises(errors.DecryptError) as ei:
        with decrypt.decrypted(db, b"w" * 32):
            pass

    assert not any(re.search(r"[0-9a-fA-F]{64}", str(a)) for a in seen["argv"]), seen["argv"]
    assert "PRAGMA key" in seen["kw"]["input"]      # 密钥在 stdin 里（不出现在 argv）

    payload = errors.to_payload(ei.value)["error"]
    assert "abababab" not in payload, payload       # 密钥的任何 ≥8 位前缀都不许进信封
    assert len(payload) <= 320                      # 而长度上限仍然生效（脱敏后才截）
