import csv

from scripts.fault_inventory import check, expand, iter_sites

SRC = '''
import os, pathlib, shutil, subprocess

def save(p, data):
    pathlib.Path(p).write_text(data)
    os.replace(p, p + ".new")
    shutil.rmtree(p, ignore_errors=True)
    subprocess.run(["tmutil", "isexcluded", p])
'''

# 每种可枚举边界各一次：删掉任一规则都会让下面的集合比对失败。
ALL_KINDS_MEDIA = '''
import os, pathlib, shutil, sqlite3, subprocess
from urllib.request import urlopen

def _fetch_cdn_bytes(url):
    return None

def in_wechat_container(p):
    pathlib.Path(p).resolve()
    p.exists()
    os.path.samefile(p, p)

def everything(p, data, con, fd):
    pathlib.Path(p).write_text(data)
    pathlib.Path(p).write_bytes(data)
    pathlib.Path(p).mkdir()
    pathlib.Path(p).chmod(0o600)
    pathlib.Path(p).unlink()
    os.mkdir(p)
    os.chmod(p, 0o600)
    os.replace(p, p + ".new")
    os.unlink(p)
    os.fdopen(fd, "w")
    open(p, "w")
    urlopen("https://example.invalid/x")
    shutil.rmtree(p, ignore_errors=True)
    shutil.copy2(p, p + ".bak")
    subprocess.run(["tmutil"])
    subprocess.Popen(["true"])
    subprocess.call(["true"])
    subprocess.check_output(["true"])
    con.commit()
    con.executescript("SELECT 1")
    con.execute("ATTACH DATABASE ? AS x", (p,))
    sqlite3.connect(p)
'''

ALL_KINDS_SNS = '''
def fetch(m):
    return None
'''


def test_iter_sites_finds_every_boundary_kind(tmp_path):
    f = tmp_path / "m.py"
    f.write_text(SRC)
    sites = iter_sites(tmp_path)
    apis = sorted(s.api for s in sites)
    assert apis == ["os.replace", "shutil.rmtree", "subprocess.run", "write_text"]
    assert all(s.qualname == "save" for s in sites)


def test_expand_marks_structural_na_not_dropped(tmp_path):
    (tmp_path / "m.py").write_text(SRC)
    rows = expand(iter_sites(tmp_path))
    write_rows = [r for r in rows if r["boundary_id"].endswith(":write_text")]
    kinds = {r["fault_class"] for r in write_rows}
    assert "ENOSPC" in kinds and "SQLITE_LOCKED" in kinds
    assert next(r for r in write_rows if r["fault_class"] == "SQLITE_LOCKED")["applicability"] == "n/a"


def test_iter_sites_follows_module_level_subprocess_alias(tmp_path):
    (tmp_path / "m.py").write_text(
        "import subprocess\n"
        "_run = subprocess.run\n"
        "def probe():\n"
        "    _run(['pgrep', '-x', 'WeChat'])\n")
    got = [(s.api, s.kind, s.qualname) for s in iter_sites(tmp_path)]
    assert got == [("subprocess.run", "subprocess", "probe")]


def test_iter_sites_follows_popen_alias(tmp_path):
    (tmp_path / "m.py").write_text(
        "import subprocess\n"
        "_popen = subprocess.Popen\n"
        "def probe():\n"
        "    _popen(['true'])\n")
    got = [(s.api, s.kind, s.qualname) for s in iter_sites(tmp_path)]
    assert got == [("subprocess.Popen", "subprocess", "probe")]


def test_iter_sites_finds_cdn_fetch_def_and_fdopen(tmp_path):
    (tmp_path / "media.py").write_text(
        "import os\n"
        "def _fetch_cdn_bytes(url):\n"
        "    return None\n"
        "def dump(fd):\n"
        "    f = os.fdopen(fd, 'w')\n")
    got = {(s.api, s.kind) for s in iter_sites(tmp_path)}
    assert got == {("_fetch_cdn_bytes", "network"), ("os.fdopen", "file_write")}


def test_iter_sites_covers_every_enumerable_kind(tmp_path):
    (tmp_path / "media.py").write_text(ALL_KINDS_MEDIA)
    (tmp_path / "sns_media.py").write_text(ALL_KINDS_SNS)
    got = {(s.api, s.kind) for s in iter_sites(tmp_path)}
    assert got == {
        ("write_text", "file_write"), ("write_bytes", "file_write"),
        ("mkdir", "meta"), ("chmod", "meta"), ("unlink", "meta"),
        ("os.mkdir", "meta"), ("os.chmod", "meta"),
        ("os.replace", "replace"), ("os.unlink", "meta"),
        ("shutil.rmtree", "meta"), ("shutil.copy2", "copy"),
        ("sqlite3.connect", "sqlite"), ("commit", "sqlite"),
        ("executescript", "sqlite"), ("attach", "sqlite"),
        ("subprocess.run", "subprocess"), ("subprocess.Popen", "subprocess"),
        ("subprocess.call", "subprocess"), ("subprocess.check_output", "subprocess"),
        ("os.fdopen", "file_write"), ("open", "file_write"),
        ("urlopen", "network"),
        ("fetch", "network"), ("_fetch_cdn_bytes", "network"),
        ("resolve", "gate"), ("samefile", "gate"), ("exists", "gate"),
    }


def test_iter_sites_recurses_into_subpackages(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "m.py").write_text("def f(p):\n    p.mkdir()\n")
    sites = iter_sites(tmp_path)
    assert [(s.rel, s.api) for s in sites] == [("sub/m.py", "mkdir")]


def test_boundary_id_disambiguates_same_line_sites(tmp_path):
    (tmp_path / "m.py").write_text("def f(p):\n    p.unlink(); p.unlink()\n")
    ids = sorted({r["boundary_id"] for r in expand(iter_sites(tmp_path))})
    assert ids == ["m.py:2:unlink#1", "m.py:2:unlink#2"]


def _write_rows(path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def _valid_rows(tmp_path):
    (tmp_path / "m.py").write_text(SRC)
    rows = expand(iter_sites(tmp_path))
    for r in rows:
        r["flow"] = "tasks"
    return rows


def test_check_accepts_valid_table(tmp_path):
    rows = _valid_rows(tmp_path)
    p = tmp_path / "t.csv"
    _write_rows(p, rows)
    assert check(p, tmp_path) == 0


def test_check_rejects_applicability_mismatch(tmp_path, capsys):
    rows = _valid_rows(tmp_path)
    r = rows[0]
    r["applicability"] = "applicable" if r["applicability"] == "n/a" else "n/a"
    p = tmp_path / "t.csv"
    _write_rows(p, rows)
    assert check(p, tmp_path) == 2
    err = capsys.readouterr().err
    assert r["boundary_id"] in err and "applicability" in err


def test_check_rejects_keyword_mismatch(tmp_path, capsys):
    rows = _valid_rows(tmp_path)
    r = next(x for x in rows if x["applicability"] == "applicable")
    r["expected_keyword"] = "不存在"
    p = tmp_path / "t.csv"
    _write_rows(p, rows)
    assert check(p, tmp_path) == 2
    assert "expected_keyword" in capsys.readouterr().err


def test_check_rejects_na_reason_mismatch(tmp_path, capsys):
    rows = _valid_rows(tmp_path)
    r = next(x for x in rows if x["applicability"] == "n/a")
    r["reason"] = "随便"
    p = tmp_path / "t.csv"
    _write_rows(p, rows)
    assert check(p, tmp_path) == 2
    assert "reason" in capsys.readouterr().err


def test_check_rejects_duplicate_keys(tmp_path, capsys):
    rows = _valid_rows(tmp_path)
    rows.append(dict(rows[0]))
    p = tmp_path / "t.csv"
    _write_rows(p, rows)
    assert check(p, tmp_path) == 2
    assert "重复" in capsys.readouterr().err


def test_check_rejects_empty_flow(tmp_path, capsys):
    rows = _valid_rows(tmp_path)
    rows[0]["flow"] = ""
    p = tmp_path / "t.csv"
    _write_rows(p, rows)
    assert check(p, tmp_path) == 2
    assert "flow" in capsys.readouterr().err


def test_check_rejects_empty_enumeration(tmp_path, capsys):
    header = ["flow", "boundary_id", "file", "line", "fault_class",
              "applicability", "reason", "expected_keyword", "case_id"]
    p = tmp_path / "t.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=header).writeheader()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert check(p, empty) == 2
    assert "枚举结果为空" in capsys.readouterr().err


def test_main_exits_2_when_enumeration_is_empty(tmp_path, monkeypatch):
    import scripts.fault_inventory as fi
    monkeypatch.setattr(fi, "iter_sites", lambda *a, **k: [])
    assert fi.main(["--write", str(tmp_path / "x.csv")]) == 2
    assert not (tmp_path / "x.csv").exists()
