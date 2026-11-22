"""`scripts/check_import_cycles.py` 必须看得见**包**。

2026-11-21 `usecases` 拆成包后，该脚本用 `ROOT.glob("*.py")` 只扫顶层文件，整包的
import 边一条都没进环检查——门禁对 `usecases` 完全失明，而当时的注释还写着「每次门禁
都验它」。这组用例把它钉住：包内文件必须作为独立模块参与检测，且**真的能报出环**。
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_import_cycles as cic  # noqa: E402


def test_package_members_are_scanned_as_modules():
    """`usecases/x.py` 要以 `usecases.x` 的身份出现在模块表里。"""
    sources = {p.stem: p for p in sorted(cic.ROOT.glob("*.py"))}
    for pkg_dir in sorted(p for p in cic.ROOT.iterdir()
                          if p.is_dir() and (p / "__init__.py").exists()):
        for f in sorted(pkg_dir.glob("*.py")):
            sources[f"{pkg_dir.name}.{f.stem}"] = f
    mods = {name: cic.classify(p) for name, p in sources.items()}
    assert "usecases._core" in mods, "包内文件没进模块表——门禁对整包失明"
    assert "usecases.chat" in mods
    # 包内兄弟边必须落在包命名空间里，而不是被当成不存在的顶层模块丢掉
    assert "usecases._core" in mods["usecases.chat"]["rt"], mods["usecases.chat"]


def test_a_package_internal_cycle_is_reported(tmp_path, monkeypatch):
    """负对照：造一个 `a → b → a` 的包内环，必须被报出来。

    探针若退化成「包内边全丢」，上面的模块表断言仍会过（名字在、边空）——这一条专门
    钉住「边真的进了图」。
    """
    pkg = tmp_path / "agent_wechat" / "pkgx"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text("from . import b\n")
    (pkg / "b.py").write_text("from . import a\n")
    monkeypatch.setattr(cic, "ROOT", tmp_path / "agent_wechat")
    sources = {p.stem: p for p in sorted(cic.ROOT.glob("*.py"))}
    for pkg_dir in sorted(p for p in cic.ROOT.iterdir()
                          if p.is_dir() and (p / "__init__.py").exists()):
        for f in sorted(pkg_dir.glob("*.py")):
            sources[f"{pkg_dir.name}.{f.stem}"] = f
    rt = {n: cic.classify(p)["rt"] for n, p in sources.items()}
    cycles = cic.runtime_cycles(rt)
    assert any({"pkgx.a", "pkgx.b"} <= set(c) for c in cycles), rt


def test_relative_imports_resolve_by_level_not_by_name(tmp_path, monkeypatch):
    """包内 `from .media import …` 必须记成 `pkgx.media`，**不能**因顶层有同名
    `media.py` 就被错记成顶层 `media`；`from .. import x`（level 2）指向顶层 x。

    回归对象：只处理 `level == 1` 的旧实现会把 level 2 全丢、把同名子模块错记成顶层，
    于是 `usecases` 包内的边要么消失、要么指向错误节点——门禁看起来在跑，实际失明。
    """
    root = tmp_path / "agent_wechat"
    (root / "pkgx").mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (root / "media.py").write_text("")            # 顶层同名模块
    (root / "errors.py").write_text("")
    (root / "pkgx" / "__init__.py").write_text("")
    (root / "pkgx" / "media.py").write_text("")
    (root / "pkgx" / "chat.py").write_text(
        "from .media import thing\nfrom .. import errors\n")
    monkeypatch.setattr(cic, "ROOT", root)

    rt = cic.classify(root / "pkgx" / "chat.py")["rt"]
    assert "pkgx.media" in rt, rt
    assert "errors" in rt, rt
    assert "media" not in rt, f"同名子模块被错记成顶层：{rt}"


def test_top_level_package_import_is_an_edge(tmp_path, monkeypatch):
    """`from . import usecases`（指向**包**而不是文件）必须记成一条边。

    回归对象：只查 `ROOT/{n}.py` 的实现忽略包目录，`cli.py` 里 16 处
    `from . import usecases` 因此零边。
    """
    root = tmp_path / "agent_wechat"
    root.mkdir(parents=True)
    (root / "cli.py").write_text("from . import usecases\n")
    (root / "usecases").mkdir()
    (root / "usecases" / "__init__.py").write_text("")
    monkeypatch.setattr(cic, "ROOT", root)

    assert "usecases" in cic.classify(root / "cli.py")["rt"]
