"""keys 的缓存读取契约。

`_load_cache` 的 except 白名单原本只列了 `FileNotFoundError`——它想要的是「文件不存在」，
但路径解析不了（ELOOP）、权限不足（EACCES）同样是 OSError，接不住，裸异常会穿透适配层。

三种情形的**区别**就是本文件的全部内容：不存在 / 损坏 → 空缓存（可用状态）；
读不到 → 项目异常（不可用状态，且**不能**伪装成空缓存）。
"""
import pytest

from agent_wechat import errors, keys


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
