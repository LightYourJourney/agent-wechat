"""keys 的缓存读取契约。

`_load_cache` 的 except 白名单原本只列了 `FileNotFoundError`——它想要的是「文件不存在」，
但路径解析不了（ELOOP）、权限不足（EACCES）同样是 OSError，接不住，裸异常会穿透适配层。

三种情形的**区别**就是本文件的全部内容：不存在 / 损坏 → 空缓存（可用状态）；
读不到 → 项目异常（不可用状态，且**不能**伪装成空缓存）。
"""
import pytest

import json

from agent_wechat import errors, keys, paths

ACC = "wxid_testacct01_ab12"
BARE = "wxid_testacct01"


def test_load_cache_treats_missing_file_as_empty(tmp_path, monkeypatch):
    """文件不存在 = 首次使用。这是既有行为，与下面两条一起钉住「三种情形各有其位」。"""
    monkeypatch.setattr(keys, "CACHE_FILE", tmp_path / "nope.json")
    assert keys._load_cache() == {"accounts": {}}


def test_load_cache_treats_corrupt_json_as_empty(tmp_path, monkeypatch):
    """缓存损坏 = 与「没有」等价：重新 bootstrap 即可。既有行为，别在修 OSError 时顺手改掉。"""
    f = tmp_path / "passphrase.json"
    f.write_text("{ 这不是 json")
    monkeypatch.setattr(keys, "CACHE_FILE", f)
    assert keys._load_cache() == {"accounts": {}}


def test_load_cache_raises_project_error_on_symlink_loop(tmp_path, monkeypatch):
    """读不到 ≠ 不存在。返回 {} 会让 status 印「缓存 ✗」，而真相是根本读不了——用户会去
    重新 bootstrap（那也会失败）。这正是本项目一直在修的那类假象。
    实测（修前）：`OSError [Errno 62] Too many levels of symbolic links` 裸穿透，
    CLI 给出一整个 traceback。"""
    a = tmp_path / "a"; b = tmp_path / "b"
    a.symlink_to(b); b.symlink_to(a)
    monkeypatch.setattr(keys, "CACHE_FILE", a / "passphrase.json")
    with pytest.raises(errors.HomeUnavailable) as ei:
        keys._load_cache()
    assert "无法读取" in str(ei.value)


# ---------- EA-005：写缓存的失败语义 ----------

def test_save_cached_hex_raises_project_error_on_unwritable_home(tmp_path, monkeypatch):
    """EA-005：落盘的三个 IO 步骤此前全裸抛 OSError（core 只抛项目异常——裸 OSError 会
    穿透两个适配层：CLI 一整个 traceback、MCP 落 generic_payload）。只读目录 → 项目异常。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    home.chmod(0o500)                     # r-x：mkdir 过（已存在），写文件必失败
    try:
        with pytest.raises(errors.HomeUnavailable) as ei:
            keys.save_cached_hex("wxid_testacct01", "ab" * 32)
        assert "无法写入密钥缓存" in str(ei.value)
        assert ei.value.hint
    finally:
        home.chmod(0o700)


def test_save_cached_hex_reports_a_failed_chown_as_a_warning(tmp_path, monkeypatch):
    """EA-005：sudo 场景归还属主（chown）失败**不该让整次引导判负**——缓存已经以 0600
    落盘，最坏是 sudo 之外读不到。此前 chown 的 OSError 会让 bootstrap 以 traceback 收场；
    修后返回一句给用户看的警告（由引导流程经事件通道转达），文件与权限照旧。"""
    monkeypatch.setattr(keys, "CACHE_FILE", tmp_path / "passphrase.json")
    monkeypatch.setenv("SUDO_UID", "501")
    monkeypatch.setenv("SUDO_GID", "20")

    def boom(path, uid, gid):
        raise OSError("EPERM: chown 被拒")

    monkeypatch.setattr(keys.os, "chown", boom)
    warn = keys.save_cached_hex("wxid_testacct01", "ab" * 32)
    assert warn and "属主未归还" in warn
    f = tmp_path / "passphrase.json"
    assert f.exists() and f.stat().st_mode & 0o777 == 0o600


def test_save_cached_hex_is_quiet_on_the_normal_path(tmp_path, monkeypatch):
    """正常路径：无警告、0600（与既有行为逐字一致）。"""
    monkeypatch.setattr(keys, "CACHE_FILE", tmp_path / "passphrase.json")
    monkeypatch.delenv("SUDO_UID", raising=False)
    monkeypatch.delenv("SUDO_GID", raising=False)
    assert keys.save_cached_hex("wxid_testacct01", "ab" * 32) is None
    assert (tmp_path / "passphrase.json").stat().st_mode & 0o777 == 0o600


# ---------- EA-049：缓存的「形状」与「值」损坏 ----------

def test_load_cache_normalizes_bad_shapes_as_corrupt(tmp_path, monkeypatch):
    """EA-049：合法 JSON 但**形状不对**（根是数组 / `accounts` 是数组 / 条目是字符串）
    此前会在下游 `.get()` 处裸抛 AttributeError——「损坏」的定义要写全，一律按空缓存处理
    （与 JSONDecodeError 同分支）。把形状归约删掉，第一条断言必红。"""
    f = tmp_path / "passphrase.json"
    monkeypatch.setattr(keys, "CACHE_FILE", f)
    for bad in ('[1, 2]', '{"accounts": [1]}', '{"accounts": {"wxid_a": "oops"}}'):
        f.write_text(bad)
        assert keys._load_cache() == {"accounts": {}}, bad
        assert keys.load_cached_hex("wxid_a") is None, bad


def test_get_passphrase_reports_a_corrupt_hex_as_bootstrap_repairable(tmp_path, monkeypatch):
    """EA-049：非 64 位 hex / 非法 hex 的缓存值此前让 `bytes.fromhex` 裸抛 ValueError。
    修后是 KeyNotFound（项目异常）＋「重新 bootstrap」指引（与「尚无缓存」分开说）。"""
    f = tmp_path / "passphrase.json"
    monkeypatch.setattr(keys, "CACHE_FILE", f)
    for bad in ("ab", "zz" * 32):
        f.write_text(json.dumps({"accounts": {BARE: {"passphrase_hex": bad}}}))
        with pytest.raises(errors.KeyNotFound) as ei:
            keys.get_passphrase(ACC)
        assert "损坏" in str(ei.value), bad
