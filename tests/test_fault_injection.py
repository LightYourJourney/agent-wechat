"""A 快层矩阵：keys / tasks / store / purge（一）＋ media / decrypt / moments /
chatview / 两条网络面 / 容器闸门（二）。

用例本身是判据：`inj.assert_fired()` 证明注入点真的打中；失败的用例先按发现登记
（R2-A-NNN），不在本文件里放松断言。
"""
import json

import pytest

from agent_wechat import errors, keys, paths, purge, store, tasks
from faultkit import Fault, expect_project_error, install
from moments_fixtures import ACCT as M_ACCT, env          # noqa: F401  (共享夹具)
from test_decrypt_pipeline import fake_wechat_db          # noqa: F401  (共享夹具)
from test_media import make_media_tree, make_voice_tree

# `moments_fixtures.env` 经 `request.module.XML` 取**调用方文件自己的** XML（形态刻意不共享）。
XML = "<SnsDataItem><TimelineObject><id>P1</id><username>u</username>" \
      "<createTime>1735689600</createTime><contentDesc>正文</contentDesc>" \
      "<ContentObject><type>2</type><mediaList/></ContentObject></TimelineObject>" \
      "<LocalExtraInfo><nickname>老王</nickname></LocalExtraInfo></SnsDataItem>"

CASES = [
    # (flow, api, fault, keyword)
    ("keys", "write_text", "ENOSPC", "无法写入|No space left"),
    ("keys", "write_text", "EACCES", "无法写入|Permission denied"),
    ("keys", "write_text", "SHORT_WRITE", "无法写入|No space left"),
    ("tasks", "os.replace", "REPLACE_FAIL", "权限|Permission denied"),
    ("tasks", "write_text", "ENOSPC", "空间|No space left"),
    ("store", "sqlite3.connect", "SQLITE_LOCKED", "锁定|locked"),
    ("store", "sqlite3.connect", "SQLITE_CORRUPT", "损坏|malformed"),
]


def _home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


# id 只取前三列：关键词列含中文，拼进去会被 pytest 转义成 \uXXXX，nodeid 就不可选中了。
@pytest.mark.parametrize("flow,api,fault,keyword", CASES,
                         ids=["-".join(str(v) for v in case[:3]) for case in CASES])
def test_fault_matrix(tmp_path, monkeypatch, flow, api, fault, keyword):
    home = _home(tmp_path)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")

    def op():
        if flow == "keys":
            keys.save_cached_hex("wxid_testacct01_ab12", "00" * 32)
        elif flow == "tasks":
            tasks.start("noop", lambda tid: 0)
        else:
            with store.open() as con:
                con.commit()

    inj = install([Fault(api, fault, nth=1)])
    try:
        expect_project_error(op, keyword=keyword)
        inj.assert_fired()
        if flow == "store":
            # 不变量 2：连接失败不得留下半成品 index.db（否则 touch 式回归会静默通过）。
            assert not store.DB_PATH.exists(), "连接失败不得留下半成品 index.db"
        # 不变量 2：不得留下临时文件；密钥缓存若存在必须可解析（不得半截 JSON）。
        # 扫 tmp_path 而非 home：tasks 的 tmp 落在 TASKS_FILE 同级（conftest 把它钉在
        # tmp_path/tasks.json），只扫 home 的话这条断言对 tasks 流程永远不可能红。
        assert not list(tmp_path.rglob("*.tmp")), "失败后不得留下临时文件"
        if flow == "keys" and (home / "passphrase.json").exists():
            json.loads((home / "passphrase.json").read_text())
    finally:
        inj.undo()


def test_purge_delete_failure_is_reentrant(tmp_path, monkeypatch):
    """不变量 2/4（销毁口径）：删除失败时目标不得消失；故障消失后重试必须删干净。

    purge 的 index 产物来自 `store.db_paths()`（随 `store.DB_PATH`），不是 HOME 里猜的路径。
    """
    home = _home(tmp_path)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    db = store.db_paths()[0]
    db.write_text("seed")
    plan = purge.plan(frozenset({"index"}))
    inj = install([Fault("unlink", "EACCES", path=str(db))])
    try:
        purge.execute(plan)
        inj.assert_fired()
        assert db.exists(), "删除失败时目标不得消失（不得半删）"
        assert db.read_text() == "seed", "删除失败时既有内容不得被截断"
    finally:
        inj.undo()
    purge.execute(purge.plan(frozenset({"index"})))
    assert not db.exists(), "重试必须删干净"


def test_keys_write_failure_preserves_existing_cache(tmp_path, monkeypatch):
    """不变量 4：既有密钥缓存必须在写入失败后原样可解析——不得被删掉或截断。"""
    home = _home(tmp_path)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    seeded = {"accounts": {"old": {"passphrase_hex": "aa" * 32, "captured_at": 0}}}
    (home / "passphrase.json").write_text(json.dumps(seeded))   # 必须在 install 之前种
    inj = install([Fault("write_text", "ENOSPC", nth=1)])
    try:
        expect_project_error(
            lambda: keys.save_cached_hex("wxid_testacct01_ab12", "00" * 32),
            keyword="无法写入|No space left")
        inj.assert_fired()
    finally:
        inj.undo()
    assert (home / "passphrase.json").exists(), "写入失败不得删掉既有缓存"
    data = json.loads((home / "passphrase.json").read_text())
    assert "old" in data["accounts"], "既有缓存条目不得丢失"
    assert data["accounts"]["old"] == seeded["accounts"]["old"], "既有条目必须原样保留"


def test_store_failure_preserves_existing_rows(tmp_path, monkeypatch):
    """不变量 4：注入前种入数据；失败后既有行必须原样还在。"""
    home = _home(tmp_path)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")
    from agent_wechat import messages
    row = messages.make_row(account="wxid_testacct01_ab12", chat_md5="m" * 32,
                            chat_id="c", shard="message_0.db", local_id=1, ts=1,
                            msg_type=1, sender_id="u", sender_name=None, is_self=0,
                            content="seed", media_hint=None)
    with store.open() as con:
        store.upsert_batch(con, [row]); con.commit()
    inj = install([Fault("commit", "EIO", nth=1)])
    try:
        with pytest.raises(errors.AgentWeChatError):
            with store.open() as con:
                store.upsert_batch(con, [messages.make_row(
                    account="wxid_testacct01_ab12", chat_md5="m" * 32, chat_id="c",
                    shard="message_0.db", local_id=2, ts=2, msg_type=1, sender_id="u",
                    sender_name=None, is_self=0, content="two", media_hint=None)])
                con.commit()
    finally:
        inj.undo()
    with store.open() as con:
        n = con.execute("SELECT count(*) FROM messages").fetchone()[0]
    assert n == 1, "失败后既有数据必须原样保留（不得出现第二条，也不得丢第一条）"


def test_store_exit_commit_failure_is_project_error(tmp_path, monkeypatch):
    """不变量 4/错误契约：`with store.open()` 的**退出提交**失败必须是项目异常。

    不走 `upsert_batch`（它在 330 自己 commit，故障会先在那里命中）：直接插一行、
    不显式 commit，让 234 的退出提交成为第一次提交。
    """
    home = _home(tmp_path)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")
    from agent_wechat import messages
    row = messages.make_row(account="wxid_testacct01_ab12", chat_md5="m" * 32,
                            chat_id="c", shard="message_0.db", local_id=1, ts=1,
                            msg_type=1, sender_id="u", sender_name=None, is_self=0,
                            content="seed", media_hint=None)
    inj = install([Fault("commit", "EIO", nth=1)])
    try:
        with pytest.raises(errors.AgentWeChatError):
            with store.open() as con:
                con.execute(store._INSERT_SQL, {**row, "exported_at": 0})
    finally:
        inj.undo()
    with store.open() as con:
        n = con.execute("SELECT count(*) FROM messages").fetchone()[0]
    assert n == 0, "退出提交失败不得落库"


# ---------- A 快层矩阵（二）：media / decrypt / moments / chatview / 网络 / gate ----------

# 媒体树里的会话名（`make_media_tree` 按它算 cm，见 test_media.make_tree 的合成树）。
MEDIA_CHAT = "chatA@chatroom"

LATE_CASES = [
    # (flow, api, fault, keyword)
    ("media_image", "write_bytes", "ENOSPC", "空间|No space left"),
    ("media_image", "write_bytes", "SHORT_WRITE", "空间|No space left"),
]


# id 只取前三列：关键词列含中文，拼进去会被 pytest 转义成 \uXXXX，nodeid 就不可选中了。
@pytest.mark.parametrize("flow,api,fault,keyword", LATE_CASES,
                         ids=["-".join(str(v) for v in case[:3]) for case in LATE_CASES])
def test_fault_matrix_late(tmp_path, monkeypatch, flow, api, fault, keyword):
    """媒体批量导出：产品契约是**逐文件降级**（`save_artifact` 吞 OSError、记 failed）。

    故本格不适用「抛项目异常」那一条（spec §4.4 不变量 1 的**已记录例外**，同 SIGKILL）；
    实际判据是：失败进报告（failed ≥ 1）且**不留半截产物**（SHORT_WRITE 必须被清理）。
    """
    _root, acct, _cm = make_media_tree(tmp_path, monkeypatch)
    out = tmp_path / "out"
    inj = install([Fault(api, fault, nth=1)])
    try:
        from agent_wechat import media
        rep = media.export_media(acct, [MEDIA_CHAT], kinds=["image"], months=["2026-09"],
                                 out_dir=str(out))
        inj.assert_fired()
        assert rep.image.failed >= 1, "写失败必须进报告，不得静默当作成功"
        if fault == "SHORT_WRITE":
            left = list(out.rglob("beef.jpg"))
            assert not left, f"半截产物必须被清理：{left}"
    finally:
        inj.undo()


def test_decrypt_snapshot_copy_failure(fake_wechat_db, tmp_path, monkeypatch):
    from agent_wechat import decrypt
    enc, _passphrase = fake_wechat_db
    home = tmp_path / "home"; home.mkdir(); monkeypatch.setattr(paths, "HOME_DIR", home)
    inj = install([Fault("shutil.copy2", "EACCES")])
    try:
        with pytest.raises(errors.AgentWeChatError):
            with decrypt.decrypted(enc, b"\x00" * 32):
                pass
    finally:
        inj.undo()
    inj.assert_fired()


def test_moments_index_write_failure(env, tmp_path, monkeypatch):
    from agent_wechat import usecases
    inj = install([Fault("write_text", "ENOSPC", nth=1)])
    try:
        with pytest.raises(errors.AgentWeChatError):
            usecases.moments_export(account=M_ACCT, out_dir=str(tmp_path / "out"))
    finally:
        inj.undo()
    inj.assert_fired()


def test_chatview_write_failure(tmp_path, monkeypatch):
    """chatview：产品契约是**单文件失败不进返回列表**（吞 OSError）。但「没写成」必须
    同时意味着**盘上没有半截文件**——本用例钉后者（`R2-A-009`）。"""
    from agent_wechat import chatview
    view = chatview.ChatView(chat_id="wxid_x", title="X", generated_at=0,
                             out_dir=str(tmp_path / "v"), msgs=(), counts={}, unparsed=0,
                             rejected=0, degraded=(), truncated=False)
    inj = install([Fault("write_text", "SHORT_WRITE", nth=1)])
    try:
        out = chatview.write(view)
        inj.assert_fired()
        assert [p.name for p in out] == ["index.md"], "失败的那个格式不得进返回列表"
        assert not (tmp_path / "v" / "chat" / "index.html").exists(), \
            "半截 index.html 必须被清理（写失败 = 没写成）"
    finally:
        inj.undo()


def test_voice_export_write_failure(tmp_path, monkeypatch):
    """语音分支：产品契约是**逐文件降级**（failed 计数），不是抛异常。"""
    _root, acct, chat_name = make_voice_tree(tmp_path, monkeypatch)
    from agent_wechat import media
    inj = install([Fault("write_bytes", "EACCES", nth=1)])
    try:
        rep = media.export_media(acct, [chat_name], kinds=["voice"],
                                 out_dir=str(tmp_path / "out"))
        inj.assert_fired()
        assert rep.voice.ok == 0 and (rep.voice.failed + rep.voice.raw_only) >= 1, \
            "写失败必须进报告（failed 或 raw_only），不得静默当作成功"
    finally:
        inj.undo()


def test_moments_fetch_network_failure_is_project_error():
    """网络面 A（moments `--with-media`，模拟，不发真实请求）：opener 失败 → SnsMediaError。

    URL 用**白名单内**的宿主：用 example.invalid 会在 `host_allowed` 就被拒，
    opener 一次都不调用——那样这条用例对「网络失败」是空转。
    """
    from agent_wechat import moments, sns_media

    class _BadOpener:
        def __init__(self):
            self.calls = 0

        def open(self, *a, **k):
            self.calls += 1
            raise OSError("simulated network failure")

    op = _BadOpener()
    m = moments.SnsMedia(kind="image", url="https://shmmsns.qpic.cn/sns/u/1",
                         token="TK", key="00")
    with pytest.raises(errors.SnsMediaError) as ei:
        sns_media.fetch(m, attempts=1, sleep=0, opener=op)
    assert op.calls == 1, "必须真的走到网络调用（否则测的不是这条分支）"
    assert "重试" in str(ei.value), f"必须是下载失败，而不是白名单/参数早退：{ei.value}"


def test_sticker_cdn_fetch_network_failure_returns_none(monkeypatch):
    """网络面 B（贴纸 `--with-cdn`）：`_fetch_cdn_bytes` 的契约是**不抛**、失败返回 None。

    URL 用**白名单内**的主机（贴纸 CDN）：用 example.invalid 会在 host 判据处早退，
    `_cdn_opener` 一次都不调用——那样这条用例对「网络失败」是空转。
    """
    from agent_wechat import media

    class _BadOpener:
        def __init__(self):
            self.calls = 0

        def open(self, *a, **k):
            self.calls += 1
            raise OSError("simulated network failure")

    op = _BadOpener()
    monkeypatch.setattr(media, "_cdn_opener", lambda: op)
    assert media._fetch_cdn_bytes("http://vweixinf.tc.qq.com/x", "0" * 32) is None
    assert op.calls == 1, "必须真的走到网络调用（否则测的不是这条分支）"


def test_gate_internal_fault_does_not_write_into_container(tmp_path, monkeypatch):
    """不变量 5（P1 口径）：闸门自身判不出来时，容器根下零写入。

    闸门 `in_wechat_container` 对 resolve 失败返回 False（"判不出来"）——本用例把它
    逼到那条路径上：若随后 HOME 被建进容器，就是 fail-open，记 P1。
    """
    fake_data = tmp_path / "Containers" / "com.tencent.xinWeChat" / "Data"
    fake_data.mkdir(parents=True)
    monkeypatch.setattr(paths, "CONTAINER_DATA", fake_data)
    home = fake_data / "home_probe"
    monkeypatch.setattr(paths, "HOME_DIR", home)
    inj = install([Fault("resolve", "EACCES", path=str(home), nth=1)])
    try:
        try:
            paths.ensure_home_dirs()
        except errors.AgentWeChatError:
            pass                      # 拒了也算守住了不变量
    finally:
        inj.undo()
    assert not home.exists(), "闸门判不出来时不得把数据目录建进容器（fail-open = P1，R2-A-008）"
