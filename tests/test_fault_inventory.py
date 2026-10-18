import pathlib
from scripts.fault_inventory import iter_sites, expand

SRC = '''
import os, pathlib, shutil, subprocess

def save(p, data):
    pathlib.Path(p).write_text(data)
    os.replace(p, p + ".new")
    shutil.rmtree(p, ignore_errors=True)
    subprocess.run(["tmutil", "isexcluded", p])
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
