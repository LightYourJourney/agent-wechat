"""集成测试：合成加密库走通 snapshot → 派生 → sqlcipher 解密 → 只读打开全链路。

用 passphrase 文本形式加密（与微信场景一致：sqlcipher 默认参数 = SQLCipher 4），
验证 decrypt.decrypted() 对"微信式"库的完整处理。
"""
import os
import pathlib
import sqlite3
import subprocess

import pytest

from agent_wechat import decrypt, keys, paths


def build_fake_wechat_db(tmp_path):
    """造一个带消息表的库，再用 32 字节 passphrase 加密成微信式库。

    抽成普通函数（而非只留夹具）：矩阵驱动也要造同一形态的库——见 `tests/test_media.py`
    的 `make_media_tree`，同一理由（夹具与驱动各写一份会漂移）。
    """
    plain = tmp_path / "plain.db"
    enc = tmp_path / "message_9.db"
    passphrase = b"wechat-like-passphrase-0123456789ab"
    if enc.exists():
        # 幂等：崩溃一致性用例会在**同一状态**上重跑整个场景（spec §4.5），第二次进来时
        # 库已经在了——重建会撞 `table Chat_Msg already exists`。
        return enc, passphrase
    con = sqlite3.connect(plain)
    con.execute("CREATE TABLE Chat_Msg(ChatName TEXT, MesLocalID INTEGER, Message TEXT, CreateTime INTEGER)")
    con.executemany("INSERT INTO Chat_Msg VALUES (?,?,?,?)",
                    [("contact_wxid", i, f"消息{i}", 1700000000 + i) for i in range(10)])
    con.commit()
    con.close()

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


@pytest.fixture()
def fake_wechat_db(tmp_path):
    return build_fake_wechat_db(tmp_path)


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


def test_a_wrapped_key_echo_cannot_ship_a_sixty_two_char_fragment(tmp_path, monkeypatch):
    """F3（T9 复审，P3）：sqlcipher 回显含密钥的语句时按 **~80 列折行**，第二处回显因此
    留下密钥的**前缀**（实测 62/64 位）——而 `errors.redact` 只认「恰好 64 位连续 hex」，
    残段一路进对外信封。这是 EA-047/053 那次修复没覆盖到的面（折行与截断无关，是 sqlcipher
    自己的输出格式化）。

    修法：错误消息只取 stderr 的**首个非空行**。回显块（第二处回显 + caret 行）正是藏
    残段的地方，而首行已经带够了定位信息（`Parse error near line N: near "…"`）。

    把 `_first_line` 换回 `r.stderr.strip()` 本测试必红。
    """
    import re

    from agent_wechat import errors

    db = tmp_path / "message_0.db"
    db.write_bytes(b"x" * 8192)
    key_hex = "cd" * 32                                  # 合成密钥（非真实值）
    wrapped = "  PRAGMA key = \"x'" + key_hex[:62]      # 折行：第 62 位处断开

    class _R:
        returncode = 1
        stdout = ""
        stderr = ("Parse error near line 1: unrecognized token: \"\"x'" + key_hex + ";'\n"
                  "SELECT 1;\"\n"
                  f"{wrapped}\n"
                  "             ^--- error here\n")

    monkeypatch.setattr(decrypt.subprocess, "run", lambda argv, **kw: _R())
    with pytest.raises(errors.DecryptError) as ei:
        with decrypt.decrypted(db, b"w" * 32):
            pass

    msg = str(ei.value)
    # 判据一：回显块（`PRAGMA key = "x'<62位>` 与 caret 行）整体不进消息——残段住在那里。
    # 首行不含 "PRAGMA key"（它是 `unrecognized token: ""x'<64位>;'`），故这条判据是准的。
    assert "PRAGMA key" not in msg, f"回显块进了消息：{msg!r}"
    assert "^--- error here" not in msg
    # 判据二（最终目标）：脱敏之后不许留下任何 ≥48 位连续 hex——62 位残段在完整 stderr 里
    # 会让这条红（带上整个 stderr 的版本实测红）。
    assert not re.search(r"[0-9a-fA-F]{48,}", errors.redact(msg)), errors.redact(msg)
    assert "Parse error" in msg, "首行带得动定位信息——不许把诊断一起丢掉"


# ---------- EA-019：崩溃后留在 tmp/ 的分片明文 ----------

def _mk_awdec(tmp_home: pathlib.Path, pid: int, name: str = "x") -> pathlib.Path:
    d = tmp_home / "tmp" / f"awdec-{pid}-{name}"
    d.mkdir(parents=True)
    (d / "plain.db").write_bytes(b"\x00" * 4096)      # 假装是明文产物
    return d


def test_stale_awdec_dirs_of_dead_processes_are_swept(tmp_path, monkeypatch):
    """EA-019：「用后即焚」此前只覆盖 `finally` 那条优雅路径——SIGKILL/崩溃/daemon 线程被
    解释器丢弃时，分片**明文**会留在 `tmp/`（实测 8192 字节的 `plain.db`），且全仓没有
    任何清扫路径。

    判活用 `os.kill(pid, 0)`：已死进程的目录清掉；**活进程的目录一律不动**（多进程下
    无条件 rmtree 会误删正在跑的任务的明文——`purge.py` 那句注释点过同款坑）。
    """
    from agent_wechat import decrypt
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(decrypt, "_swept_stale", False)

    import subprocess as _sub, sys as _sys
    dead = _sub.Popen([_sys.executable, "-c", "pass"])
    dead.wait()                                       # 拿一个**确定已退出**的 pid
    stale = _mk_awdec(home, dead.pid)
    mine = _mk_awdec(home, os.getpid(), "mine")
    foreign = home / "tmp" / "someone-elses-dir"      # 非本工具形态：不许动
    foreign.mkdir()

    swept = decrypt.sweep_stale_tempdirs()
    assert swept == 1 and not stale.exists(), "已死进程的明文目录必须清掉"
    assert mine.exists(), "本进程的目录不许动（可能正在用）"
    assert foreign.exists(), "非 awdec 形态的目录一律不碰"


def test_the_sweep_runs_before_the_first_decryption(tmp_path, monkeypatch):
    """清扫挂在 `ensure_home_dirs` 上（第一次解密之前），且每进程只扫一次。"""
    from agent_wechat import decrypt
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(decrypt, "_swept_stale", False)

    import subprocess as _sub, sys as _sys
    dead = _sub.Popen([_sys.executable, "-c", "pass"])
    dead.wait()
    stale = _mk_awdec(home, dead.pid)

    db = tmp_path / "message_0.db"
    db.write_bytes(b"x" * 8192)
    monkeypatch.setattr(decrypt.subprocess, "run",
                        lambda argv, **kw: (_ for _ in ()).throw(
                            AssertionError("不该走到 sqlcipher")))
    # **精确到注入的那一句**：`pytest.raises(Exception)` 分不开「停在该停的地方」与
    # 「更早炸了」（review-code 的 Standards 轴 #13）——而这里的桩抛的正是 AssertionError，
    # 那个宽判据等于把两者当同一件事。
    with pytest.raises(AssertionError, match="不该走到 sqlcipher"):
        with decrypt.decrypted(db, b"w" * 32):
            pass
    assert not stale.exists(), "第一次解密之前就该把陈旧明文清掉"
    # 幂等：第二次调用不再重复扫（flag 置位后 tmp 下新出现的目录不动）
    again = _mk_awdec(home, dead.pid, "later")
    decrypt.ensure_home_dirs()
    assert again.exists(), "每进程只扫一次"


def test_the_sweep_keeps_anything_it_cannot_prove_is_dead(tmp_path, monkeypatch):
    """复审 N4：「判不出来一律留着」是整条清扫的**安全底座**（docstring、⑤、台账都专门声明），
    但此前没有任何用例覆盖——而它正是「绝不无条件 rmtree」唯一会救命的场景（别人的活进程）。

    两条支路都钉：`PermissionError`（别人的进程）与 `OverflowError`（pid 超出 C long，
    `os.kill` 对它抛的**不是** OSError——复审 N1 实测它会逃出 core，让每个新进程的第一次
    解密都失败）。把 `except (OSError, OverflowError)` 收窄回 `except OSError`
    （或用 `<` 直接删掉整个 except）本测试必红。
    """
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True)
    monkeypatch.setattr(paths, "HOME_DIR", home)

    import errno as _errno
    for name, exc in (("someone-elses", PermissionError(_errno.EPERM, "not mine")),
                      ("silly-pid", OverflowError("Python int too large to convert to C long"))):
        d = home / "tmp" / f"awdec-{4242}-{name}"
        d.mkdir()
        monkeypatch.setattr(decrypt.os, "kill",
                            lambda pid, sig, _e=exc: (_ for _ in ()).throw(_e))
        assert decrypt.sweep_stale_tempdirs() == 0
        assert d.exists(), f"{type(exc).__name__} 下不许删（判不出来 = 留着）"


def test_the_tmpdir_the_pipeline_creates_is_recognizable_by_the_sweeper(tmp_path, monkeypatch):
    """创建点与清扫点的**形态必须同源**（EA-019）：`decrypted()` 建的目录名要能被
    `_AWDEC_RE` 认出并解出**本进程的 pid**。

    否则清扫形同虚设——它只认带 pid 的形态，而崩溃残留永远认不出来。这一点在「手工造
    目录」的用例里测不到（变异 D4：把创建前缀退回 `awdec-` 时，那两条用例照样全绿）。
    """
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(decrypt, "_swept_stale", True)      # 只看它建了什么

    seen = {}

    def fake_run(argv, **kw):
        seen["names"] = [p.name for p in (home / "tmp").iterdir()]
        raise RuntimeError("停在 sqlcipher 这一步")
    monkeypatch.setattr(decrypt.subprocess, "run", fake_run)

    db = tmp_path / "message_0.db"
    db.write_bytes(b"x" * 8192)
    with pytest.raises(RuntimeError):
        with decrypt.decrypted(db, b"w" * 32):
            pass

    names = seen["names"]
    assert len(names) == 1, names
    m = decrypt._AWDEC_RE.match(names[0])
    assert m, f"清扫认不出这个形态：{names[0]}"
    assert int(m.group(1)) == os.getpid(), names


def test_the_plaintext_workspace_is_gone_after_success(fake_wechat_db, tmp_path, monkeypatch):
    """B 阶段（T14）：正常走完一次解密后，`tmp/awdec-<pid>-*` 里那份**明文**必须随之消失。

    EA-019 的清扫只兜「崩溃残留」；优雅路径的删除此前零断言——把 `finally` 里的
    `shutil.rmtree(td, ...)` 删掉，`test_decrypt_pipeline.py` 全绿，而用户的明文会一直
    躺在 HOME/tmp 下。
    """
    enc, passphrase = fake_wechat_db
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True)
    monkeypatch.setattr(paths, "HOME_DIR", home)

    with decrypt.decrypted(enc, passphrase) as plain:
        ws = plain.parent
        assert ws.exists() and plain.exists(), "解密期间明文副本必须在"
    assert not ws.exists(), "退出上下文后临时目录（含明文）必须被删掉"
    assert list((home / "tmp").iterdir()) == [], "tmp 下不该留下任何东西"


def test_a_zero_exit_that_produced_no_file_is_still_a_failure(tmp_path, monkeypatch):
    """B 阶段（T14）：`returncode != 0 **or** not out.exists()` 里的两个判据此前只覆盖了
    前者——把 `or` 改成 `and`，既有用例全绿，而「sqlcipher 静默返回 0 却没写出文件」会让
    `decrypted()` 交出一个**不存在的**明文路径，错误推迟到调用方（症状离现场很远）。
    """
    db = tmp_path / "message_0.db"
    db.write_bytes(b"x" * 8192)                     # ≥1 页，过「不足一页」判据
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True)
    monkeypatch.setattr(paths, "HOME_DIR", home)

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(decrypt.subprocess, "run", lambda *a, **kw: _R())
    from agent_wechat import errors
    with pytest.raises(errors.DecryptError, match="sqlcipher 解密失败"):
        with decrypt.decrypted(db, b"w" * 32):
            pass


def test_first_line_returns_an_empty_string_when_there_is_none():
    """没有可用行时返回空串（不是 None）：消息模板直接插值它，`None` 会印成字面量
    「None」。首行还要去掉两侧空白。"""
    assert decrypt._first_line("") == ""
    assert decrypt._first_line("\n  \n") == ""
    assert decrypt._first_line("  boom  \nsecond") == "boom"


def test_sweep_stale_tempdirs_tolerates_an_unreadable_tmp(tmp_path, monkeypatch):
    """`tmp/` 读不了时返回 0：清扫绝不因此挡住解密，且返回值是 int（调用方会累加/上报），
    不是 None。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / "tmp").write_bytes(b"x")            # tmp 是文件：iterdir 抛 NotADirectoryError
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(decrypt, "_swept_stale", False)
    assert decrypt.sweep_stale_tempdirs() == 0
