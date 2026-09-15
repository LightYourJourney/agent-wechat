"""真机媒体冒烟（默认不跑：uv run pytest tests/ -m real -v）。

只读冒烟：解密产物不落盘（语音走 HOME/tmp 的用后即焚快照，与 `media export`
语音路径同一读取机制），不向 ~/.agent-wechat/media/ 写任何文件。
"""
import sqlite3

import pytest

from agent_wechat import decrypt, errors, keys, media, paths

# slow：默认 deselect 由 addopts 的 -m 'not slow' 负责（与 test_vector_real 同一机制）；
# real：真机冒烟的统一入口标记，`-m real` 只跑真实数据路径的用例。
pytestmark = [pytest.mark.slow, pytest.mark.real]


def _real_account() -> str:
    """取一个已 bootstrap（有 passphrase 缓存）的真实账号；条件不满足时 skip 不是 fail。"""
    if not paths.XWECHAT_FILES.is_dir():
        pytest.skip("本机没有真实微信树")
    for acct in paths.account_dirs():
        try:
            keys.get_passphrase(acct)
        except keys.KeyNotFound:
            continue
        return acct
    pytest.skip("没有已缓存 passphrase 的账号（先 bootstrap）")


def test_real_derive_keys_and_decrypt_one_image():
    """真账号 derive_keys → 任意真 dat 解密出已知魔数（spec §2.2：推导后必须验证）。

    优先取 `dats[1]`：`derive_keys` 内部正是拿首个样本验证的，再断言 `dats[0]` 是近同义反复
    （它必过，测不出真信号）；只有一个样本时才退回 `dats[0]`——此时它确实承担了
    「真机可解」的信号，别无选择。没有样本 dat（该账号没有聊天图片）时 skip 不是 fail。
    """
    acct = _real_account()
    dats = media._attach_dats(acct)
    if not dats:
        pytest.skip("msg/attach 下没有样本 dat（该账号没有聊天图片）")
    mk = media.derive_keys(acct)
    sample = dats[1] if len(dats) > 1 else dats[0]
    out = media.decrypt_dat(sample.read_bytes(), mk)
    assert media.detect_ext(out) is not None


def test_real_voice_probe():
    """media_N.db 的 VoiceInfo 至少一条且以 \\x02#!SILK_V3 或 #!SILK_V3 开头（spec §2.4）。

    单库打不开（密钥不对/库损坏/漂移）与产品路径同口径：翻过这一个继续，不中断探针
    （`media._export_voices` 对单个分片失败也是 continue）；全都没探到才 skip，不是 fail。
    """
    acct = _real_account()
    dbs = media._voice_db_paths(acct)
    if not dbs:
        pytest.skip("没有语音分片库（media_*.db）")
    passphrase = keys.get_passphrase(acct)
    for db in dbs:
        try:
            with decrypt.decrypted(db, passphrase) as plain:
                con = sqlite3.connect(f"file:{plain}?mode=ro", uri=True)
                try:
                    row = con.execute(
                        "SELECT voice_data FROM VoiceInfo WHERE voice_data IS NOT NULL "
                        "LIMIT 1").fetchone()
                finally:
                    con.close()
        except (errors.AgentWeChatError, sqlite3.Error):
            continue
        if row is None:
            continue
        blob = row[0]
        assert blob.startswith(b"\x02#!SILK_V3") or blob.startswith(b"#!SILK_V3")
        return
    pytest.skip("语音分片里没有可读的 VoiceInfo 行（或全部打不开）")
