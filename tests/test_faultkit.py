import os, pathlib, pytest
from agent_wechat import errors
from faultkit import Fault, install, expect_project_error, hash_tree


def test_write_text_enospc_fires_once(tmp_path, monkeypatch):
    inj = install([Fault("write_text", "ENOSPC", path=str(tmp_path / "a"), nth=1)])
    try:
        with pytest.raises(OSError) as ei:
            (tmp_path / "a").write_text("x")
        assert ei.value.errno == 28
        inj.assert_fired()
        assert not (tmp_path / "a").exists()
    finally:
        inj.undo()


def test_short_write_leaves_partial_then_raises(tmp_path, monkeypatch):
    p = tmp_path / "a"
    inj = install([Fault("write_text", "SHORT_WRITE", path=str(p))])
    try:
        with pytest.raises(OSError):
            p.write_text("abcdef")
        assert p.exists() and p.read_text() == "abc"
    finally:
        inj.undo()


def test_expect_project_error_requires_hint_and_keyword():
    def boom():
        raise errors.ContainerWriteRefused("拒绝写进容器", hint="换目录")
    expect_project_error(boom, keyword="容器")


def test_hash_tree_changes_with_content(tmp_path):
    (tmp_path / "a").write_text("1")
    h1 = hash_tree(tmp_path)
    (tmp_path / "a").write_text("2")
    assert hash_tree(tmp_path) != h1
