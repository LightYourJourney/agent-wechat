import os, pathlib, shutil, subprocess, pytest
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


def test_path_matcher_ignores_other_paths(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    inj = install([Fault("write_text", "EACCES", path=str(a))])
    try:
        b.write_text("x")
        assert b.read_text() == "x"
        assert not inj.fired
        with pytest.raises(OSError) as ei:
            a.write_text("x")
        assert ei.value.errno == 13
        inj.assert_fired()
        assert not a.exists()
    finally:
        inj.undo()


def test_nth_matcher_fires_only_on_nth_call(tmp_path):
    p = tmp_path / "a"
    inj = install([Fault("write_text", "EACCES", nth=2)])
    try:
        p.write_text("one")
        assert p.read_text() == "one"
        assert not inj.fired
        with pytest.raises(OSError) as ei:
            p.write_text("two")
        assert ei.value.errno == 13
        assert p.read_text() == "one"
        inj.assert_fired()
    finally:
        inj.undo()


def test_keyword_call_forms_do_not_crash(tmp_path):
    p = tmp_path / "a"
    inj = install([Fault("write_text", "SHORT_WRITE", path=str(p))])
    try:
        with pytest.raises(OSError):
            p.write_text(data="abcdef")
        assert p.read_text() == "abc"
        inj.assert_fired()
    finally:
        inj.undo()
    inj = install([Fault("subprocess.run", "SUBPROCESS_NONZERO")])
    try:
        r = subprocess.run(args=["true"], capture_output=True, text=True)
        assert isinstance(r, subprocess.CompletedProcess) and r.returncode == 1
        inj.assert_fired()
    finally:
        inj.undo()


def test_copy2_binds_destination(tmp_path):
    src = tmp_path / "src"
    src.write_text("payload")
    ok, bound = tmp_path / "ok", tmp_path / "bound"
    inj = install([Fault("shutil.copy2", "EACCES", path=str(bound))])
    try:
        shutil.copy2(src, ok)
        assert ok.read_text() == "payload"
        assert not inj.fired
        with pytest.raises(OSError) as ei:
            shutil.copy2(src, bound)
        assert ei.value.errno == 13
        inj.assert_fired()
        assert not bound.exists()
    finally:
        inj.undo()
