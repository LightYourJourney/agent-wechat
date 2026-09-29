"""`security/scan.py` 的测试。

两条贯穿全篇的判据：
  1. **输出里绝不出现命中值本身**——扫描器一旦打印真标识，输出就进了 Agent 会话
     记录、落进 ~/.claude/projects，堵出口的工具自己成了出口（spec §4.4）。
  2. **fail-closed**——扫描器自身出错必须非 0 退出，且与「发现泄漏」用不同退出码
     （0 干净 / 1 有命中 / 2 出错）。
"""
import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCAN_PATH = ROOT / "security" / "scan.py"


def _load():
    spec = importlib.util.spec_from_file_location("scan_under_test", SCAN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def scan():
    return _load()


# 一条**伪造的** wxid。绝不用真标识（spec §7.4）。
FAKE = "wxid_qz7x2m4k9p1v3n8b"


def test_patterns_match_the_three_shapes(scan):
    shapes = {s for _, _, s, _ in scan.hits_in_text(
        "a " + FAKE + " b 12345678901@chatroom c 13800138000 d")}
    assert shapes == {"wxid", "chatroom", "phone"}, "三种形状都要抓到"


def test_wxid_requires_four_chars_after_prefix(scan):
    assert scan.hits_in_text("wxid_abc") == [], "wxid_ 后不足 4 字符不该命中"
    assert [h[2] for h in scan.hits_in_text("wxid_abcd")] == ["wxid"]


def test_chatroom_requires_six_digits(scan):
    assert scan.hits_in_text("12345@chatroom") == [], "不足 6 位不算群 id"
    assert [h[2] for h in scan.hits_in_text("123456@chatroom")] == ["chatroom"]


def test_phone_boundaries(scan):
    assert scan.hits_in_text("1380013800") == [], "10 位不该命中"
    assert [h[2] for h in scan.hits_in_text("13800138000")] == ["phone"]
    assert scan.hits_in_text("12800138000") == [], "第 2 位不在 3-9 不该命中"


def test_hex32_is_not_a_pattern(scan):
    """[0-9a-f]{32} 有 2786 个 distinct，是任何 md5，纯噪声——spec §4.2 明确排除。"""
    assert scan.hits_in_text("d41d8cd98f00b204e9800998ecf8427e") == []


def test_output_never_contains_the_value(scan, tmp_path, monkeypatch, capsys):
    """本设计里最该被钉死的不变量（spec §4.4）。"""
    (tmp_path / "x.txt").write_text("hit " + FAKE + " end\n", encoding="utf-8")
    monkeypatch.setattr(scan, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(scan, "_tracked_files", lambda: ["x.txt"])
    monkeypatch.setattr(scan, "ALLOWLIST_PATH", tmp_path / "allowlist.txt")

    rc = scan.main(["--all"])
    out = capsys.readouterr()

    assert FAKE not in out.out, "命中值出现在 stdout 里了"
    assert FAKE not in out.err, "命中值出现在 stderr 里了"
    assert rc == scan.EXIT_HITS
    assert "x.txt:1" in out.out, "但要给出位置"


def test_missing_git_is_fail_closed(scan, monkeypatch, capsys):
    """Review Focus 5：GitHub Desktop 的 stripped git 环境里可能没有 git。"""
    monkeypatch.setattr(scan.shutil, "which", lambda _name: None)
    rc = scan.main(["--all"])
    assert rc == scan.EXIT_ERROR, "找不到 git 必须退出 2，不能崩栈、也不能放行"
    assert "git" in capsys.readouterr().err


def test_non_scanner_error_is_fail_closed(scan, monkeypatch, capsys):
    """任何**非 `ScannerError`** 的内部错误也必须退出 2（不是 1）。

    只捕获 `ScannerError` 时，其他异常会以**退出码 1** 逃逸——而 1 是契约留给
    「发现命中」的码，于是 0 干净 / 1 命中 / 2 扫描器出错 这个**区分会坍缩**，
    而那正是本任务存在的目的（spec §4.5）。这条测试就是它的护栏。
    """
    def boom(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(scan, "_git", boom)
    rc = scan.main(["--all"])

    assert rc == scan.EXIT_ERROR, "非 ScannerError 的崩溃也必须退出 2"
    assert "扫描器出错" in capsys.readouterr().err, "消息要点明是扫描器出错，不是发现泄漏"


def test_values_view_is_per_value_not_per_occurrence(scan, tmp_path, monkeypatch, capsys):
    """spec §4.6 的归类是逐**值**的（90 个），不是逐处的。"""
    (tmp_path / "a.txt").write_text("x " + FAKE + " y\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text(FAKE + "\n" + FAKE + "\n", encoding="utf-8")
    monkeypatch.setattr(scan, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(scan, "_tracked_files", lambda: ["a.txt", "b.txt"])
    monkeypatch.setattr(scan, "ALLOWLIST_PATH", tmp_path / "allowlist.txt")

    rc = scan.main(["--values"])
    out = capsys.readouterr().out

    assert FAKE not in out, "逐值视图同样不许打印值"
    assert out.count(scan.fingerprint(FAKE)) == 1, "同一个值只该有一行"
    assert "3 次" in out, "要给出出现次数"
    assert rc == scan.EXIT_HITS


def test_repo_scope_comes_from_git(scan, monkeypatch):
    """作用域由 `git ls-files` 决定——`.gitignore` 掉的东西天然不进（spec §4.4 作用域说明）。

    这条钉的是**机制**而不是结果：如果有人哪天把枚举改成自己 os.walk 目录树，
    `.venv/`、`node_modules/` 这些会被扫进来，而这条会红。
    （原先这里写的是「把 _tracked_files 打桩成 []，断言 collect_values() == {}」——
    没有文件当然没有命中，那条断言**永远不可能为它名字宣称的东西而失败**。）
    """
    calls = []
    monkeypatch.setattr(scan, "_git", lambda *a: (calls.append(a), "a.py\0b.py\0")[1])
    assert scan._tracked_files() == ["a.py", "b.py"]
    assert calls == [("ls-files", "-z")], "枚举必须走 git，不许自己去 walk 目录树"


def test_scan_repo_skips_generated_lockfiles(scan, tmp_path, monkeypatch):
    """生成的依赖锁不进扫描面——那里面全是第三方包元数据。

    `uv.lock` 单独贡献 47 个手机号形假阳性（52 个手机号值里的 47 个），
    不跳过它，「仓库内只有几十个 distinct、可管理」这个前提就是假的。
    """
    (tmp_path / "uv.lock").write_text('hash "13800138000"\n', encoding="utf-8")
    (tmp_path / "real.py").write_text("hit " + FAKE + "\n", encoding="utf-8")
    monkeypatch.setattr(scan, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(scan, "_tracked_files", lambda: ["uv.lock", "real.py"])
    monkeypatch.setattr(scan, "ALLOWLIST_PATH", tmp_path / "allowlist.txt")

    rels = {rel for rel, _n, _s, _v in scan.scan_repo()}

    assert rels == {"real.py"}, "uv.lock 不许进扫描面；真实文件必须照扫不误"


# 修改已跟踪的锁文件——这是 uv.lock 最常见的形态（重新生成后 git add）。
# 结构照 `git diff --cached --unified=0` 抄：`index` 行、`--- `/`+++ ` 双头、hunk 头。
DIFF_WITH_LOCK = """\
diff --git a/uv.lock b/uv.lock
index b8cb000..ba5ef67 100644
--- a/uv.lock
+++ b/uv.lock
@@ -1,0 +2,2 @@
+hash "13800138000"
+plain
diff --git a/real.py b/real.py
new file mode 100644
index 0000000..9cd59d8
--- /dev/null
+++ b/real.py
@@ -1,0 +1 @@
+hit " + FAKE + "
"""


def test_staged_skips_generated_lockfiles(scan, tmp_path, monkeypatch, capsys):
    """终审 Critical 1：`--staged` 也必须跳过生成的依赖锁。

    `SKIP_NAMES` 原先只在 `scan_repo()` 里生效，于是锁文件提交被**拦下**（那 47 个
    假阳性），而文档给的解法**解不开**：`--allow` 走 `collect_values()` → `scan_repo()`，
    那个文件在那儿被跳过 → 指纹进不了表 → 退出 2；手抄又会让 `--verify-allowlist`
    永远红。工具自相矛盾：`--all` 说干净、`--staged` 说 47 个未登记。

    删掉 `staged_added_lines` 里的 `SKIP_NAMES` 检查，这条会红（uv.lock 的两行会出现在
    结果里）；「stderr 为空」那半则防「二进制警告被无差别打印」。
    """
    (tmp_path / "uv.lock").write_text("x\n", encoding="utf-8")
    (tmp_path / "real.py").write_text("y\n", encoding="utf-8")
    monkeypatch.setattr(scan, "REPO_ROOT", tmp_path)

    rows = scan.staged_added_lines(DIFF_WITH_LOCK)

    assert {rel for rel, _n, _t in rows} == {"real.py"}, \
        "锁文件不进暂存区扫描面；真实文件的行必须照扫不误"
    assert not any("hash" in text for _rel, _n, text in rows), \
        "锁文件里的行一行都不许漏进来"
    assert capsys.readouterr().err == "", "纯文本 diff 不该触发二进制警告"


def test_allowlist_parses_values_ignoring_comments(scan, tmp_path):
    p = tmp_path / "allowlist.txt"
    p.write_text("# 注释行\n\n" + FAKE + "  全仓库通用夹具\n"
                 "12345678901@chatroom  # 行内注释\n", encoding="utf-8")
    assert scan.load_allowlist(p) == {FAKE, "12345678901@chatroom"}


def test_missing_allowlist_yields_empty_set_not_crash(scan, tmp_path):
    """Review Focus 4：清单不存在时不崩，行为是「全部按未登记处理」。"""
    assert scan.load_allowlist(tmp_path / "nope.txt") == set()


def test_allow_writes_value_but_never_prints_it(scan, tmp_path, monkeypatch, capsys):
    """Review Focus 1 + spec §4.4：--allow 的输出不许出现值。"""
    (tmp_path / "x.txt").write_text("hit " + FAKE + "\n", encoding="utf-8")
    allow = tmp_path / "allowlist.txt"
    monkeypatch.setattr(scan, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(scan, "_tracked_files", lambda: ["x.txt"])
    monkeypatch.setattr(scan, "ALLOWLIST_PATH", allow)

    rc = scan.main(["--allow", scan.fingerprint(FAKE)])
    out = capsys.readouterr()

    assert rc == scan.EXIT_CLEAN
    assert FAKE not in out.out and FAKE not in out.err, "承诺过不打印值"
    assert FAKE in allow.read_text(encoding="utf-8"), "但必须写进清单"


def test_allow_with_unknown_fingerprint_is_an_error(scan, tmp_path, monkeypatch, capsys):
    """未知编号退出 2——**且不许把参数原样回显**（终审 Minor 6）。

    `--allow` 是唯一能拿到**任意用户串**的入口。这里刻意传的是**值**而不是指纹
    （最可能的误用：把值粘进来），回显就等于扫描器亲手把值打到 stderr，
    与「默认绝不打印命中值」直接矛盾。恢复那句 `%s`，这条会红。
    """
    monkeypatch.setattr(scan, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(scan, "_tracked_files", lambda: [])
    monkeypatch.setattr(scan, "ALLOWLIST_PATH", tmp_path / "allowlist.txt")

    rc = scan.main(["--allow", FAKE])

    err = capsys.readouterr().err
    assert rc == scan.EXIT_ERROR
    assert FAKE not in err, "参数被原样回显了——那就等于扫描器自己打印了值"
    assert "指纹" in err or "sha256" in err, "要说清编号长什么样，否则用户只能瞎猜"


def test_verify_allowlist_flags_dead_entries(scan, tmp_path, monkeypatch, capsys):
    """spec §4.3 不变式 1：清单里每一项都必须在仓库里至少命中一次。

    注意 scan_repo() 会跳过 allowlist.txt 自身——否则这条不变式**永远为真**，
    是个假护栏。
    """
    allow = tmp_path / "allowlist.txt"
    allow.write_text("wxid_zzzznotinthere9  死条目\n", encoding="utf-8")
    monkeypatch.setattr(scan, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(scan, "_tracked_files", lambda: ["allowlist.txt"])
    monkeypatch.setattr(scan, "ALLOWLIST_PATH", allow)

    rc = scan.main(["--verify-allowlist"])
    out = capsys.readouterr().out
    assert rc == scan.EXIT_HITS, "死条目必须让它红"
    assert "wxid_zzzznotinthere9" not in out, "报死条目也不打印值"
    assert scan.fingerprint("wxid_zzzznotinthere9") in out


def test_verify_allowlist_missing_registry_is_an_error(scan, tmp_path, monkeypatch, capsys):
    """终审 Minor 9：清单被删 / 改名时，「一个条目都没校验过」不许报 verified（退出 0）。

    `load_allowlist` 对缺失文件返回空集，那是**别处**要的方向（全部按未登记处理，
    fail-closed）；拿它来**校验**就反了——死条目一条都查不出来，却报「验证通过」。
    删掉 `verify_allowlist` 里那句存在性检查，这条会红（退回 EXIT_CLEAN）。
    """
    monkeypatch.setattr(scan, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(scan, "_tracked_files", lambda: [])
    monkeypatch.setattr(scan, "ALLOWLIST_PATH", tmp_path / "nope.txt")

    rc = scan.main(["--verify-allowlist"])

    assert rc == scan.EXIT_ERROR, "「没有清单」不是「清单干净」"
    assert "不存在" in capsys.readouterr().err


def test_values_view_marks_registered_state(scan, tmp_path, monkeypatch, capsys):
    """Task 7 的人工归类靠这一列看进度——它必须真的跟着豁免清单变。

    没有这条，删掉那行 `state = ...` 不会有任何测试变红（Task 3 的审阅正是在
    `--values` 的 `dirs` 那半边记录过同一类空档）。
    """
    (tmp_path / "x.txt").write_text("hit " + FAKE + "\n", encoding="utf-8")
    allow = tmp_path / "allowlist.txt"
    monkeypatch.setattr(scan, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(scan, "_tracked_files", lambda: ["x.txt"])
    monkeypatch.setattr(scan, "ALLOWLIST_PATH", allow)

    scan.main(["--values"])
    assert "未登记" in capsys.readouterr().out, "登记前应显示未登记"

    scan.main(["--allow", scan.fingerprint(FAKE)])
    capsys.readouterr()

    scan.main(["--values"])
    out = capsys.readouterr().out
    assert "已登记" in out, "登记后应显示已登记"
    assert FAKE not in out, "加一列不等于放宽掩码原则"


def test_default_view_skips_registered_values(scan, tmp_path, monkeypatch, capsys):
    """豁免清单对**默认视图**的过滤必须真的生效。

    没有这条时，把 `unregistered = [h for h in hits if h[3] not in allow]` 换回
    `unregistered = hits`，十六条测试**全绿**——意味着「登记」这个动作可以在默认视图上
    **静默失效**而没有任何东西变红。
    """
    (tmp_path / "x.txt").write_text("hit " + FAKE + "\n", encoding="utf-8")
    allow = tmp_path / "allowlist.txt"
    monkeypatch.setattr(scan, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(scan, "_tracked_files", lambda: ["x.txt"])
    monkeypatch.setattr(scan, "ALLOWLIST_PATH", allow)

    assert scan.main(["--all"]) == scan.EXIT_HITS, "登记之前它应当报出来"

    scan.main(["--allow", scan.fingerprint(FAKE)])
    capsys.readouterr()

    assert scan.main(["--all"]) == scan.EXIT_CLEAN, "登记之后默认视图不该再报它"
    assert capsys.readouterr().out == "", "登记之后默认视图不该再打印任何一行"


DIFF_TWO_FILES = """\
diff --git a/a.txt b/a.txt
index b8cb000..ba5ef67 100644
--- a/a.txt
+++ b/a.txt
@@ -1,0 +2,2 @@ 尾部小节名
+added " + FAKE + "
+plain
diff --git a/del.txt b/del.txt
deleted file mode 100644
index 587be6b..0000000
--- a/del.txt
+++ /dev/null
@@ -1 +0,0 @@
-gone
diff --git a/b.txt b/b.txt
new file mode 100644
index 0000000..9cd59d8
--- /dev/null
+++ b/b.txt
@@ -0,0 +1 @@
+also " + "12345678901@chatroom" + "
"""


def test_staged_added_lines_extracts_new_side_only(scan, tmp_path, monkeypatch):
    """夹具是**照真实 `git diff --cached --unified=0` 抄下来的**，不是手编的。

    刻意包含真机必现、而手编夹具最容易漏的四样：`index` 行、`deleted file mode`、
    `new file mode`、以及 hunk 头尾部的**小节名**（`@@ … @@ 尾部小节名`）。
    本项目吃过的最大一类亏就是「夹具编码了我以为的形态」。

    **必须把 REPO_ROOT 换到 tmp_path 并真建出 a.txt / b.txt**：`staged_added_lines`
    对解析出来的路径做了存在性检查，在真仓库里这两个文件不存在，会抛 ScannerError。
    """
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("y\n", encoding="utf-8")
    monkeypatch.setattr(scan, "REPO_ROOT", tmp_path)

    rows = scan.staged_added_lines(DIFF_TWO_FILES)
    assert [(r[0], r[1]) for r in rows] == [("a.txt", 2), ("a.txt", 3), ("b.txt", 1)], \
        "行号要取 + 侧、只取新增行、且删掉的文件（+++ /dev/null）不许混进来"


def test_staged_unresolvable_path_is_fail_closed(scan, tmp_path, monkeypatch):
    """路径解析不出来时必须炸，不能静默跳过——静默跳过会让「装了但从没拦过」不可见。"""
    monkeypatch.setattr(scan, "REPO_ROOT", tmp_path / "nowhere")
    with pytest.raises(scan.ScannerError):
        scan.staged_added_lines(
            "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n+x\n")


DIFF_HEADER_MIMIC = """\
diff --git a/m.txt b/m.txt
index 1111111..2222222 100644
--- a/m.txt
+++ b/m.txt
@@ -1 +1,2 @@
--- x
+++ /dev/null
+still a hit " + FAKE + "
"""


def test_staged_header_mimic_does_not_swallow_the_rest_of_the_file(scan, tmp_path, monkeypatch):
    """hunk 体内**长得像文件头**的内容行，不许把后面的行吞掉。

    旧侧内容 `-- x` 渲染成 `--- x`，新侧内容 `++ /dev/null` 渲染成 `+++ /dev/null`。
    只判「上一行是 `--- `」的旧写法会把这一对当成「文件被删」→ `path = None` →
    **该文件后面所有行被静默跳过**，而进程退 0。**那是 fail-open**，且与
    `staged_added_lines` 自己「不静默跳过」的承诺直接矛盾。

    新判据按 `seen_hunk`：文件头只可能在**第一个 `@@` 之前**，所以 hunk 体内的
    `--- `/`+++ ` 一律是内容。仓库今天没有这种相邻对（审阅者实测 129 个文件、0 处命中），
    所以这是**潜伏**风险——但一个「只在没有伪装输入时才对」的解析器不算 fail-closed。
    """
    (tmp_path / "m.txt").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(scan, "REPO_ROOT", tmp_path)

    rows = scan.staged_added_lines(DIFF_HEADER_MIMIC)

    assert any("still a hit" in text for _rel, _n, text in rows), \
        "伪装的文件头不许吞掉它后面的新增行"
    assert {rel for rel, _n, _t in rows} == {"m.txt"}, "路径也不许被劫持"


DIFF_BINARY = """\
diff --git a/x.bin b/x.bin
new file mode 100644
index 0000000..c866266
Binary files /dev/null and b/x.bin differ
"""


def test_staged_binary_file_is_warned_not_silent(scan, tmp_path, monkeypatch, capsys):
    """终审 Important 2：二进制文件在 --staged 上是一条**静默**路径。

    夹具照真实 `git diff --cached --unified=0`（git 2.55）抄：二进制节**没有**
    `--- `/`+++ ` 双头、也没有 hunk，只有一行 `Binary files … differ`。于是它既没有
    `+` 行可扫（--staged 只认 `+` 行），也碰不到那条「路径解析不出来」的存在性检查——
    不点名就等于「扫过了、干净」。今天仓库里 0 个非 UTF-8 tracked 文件，所以是**潜伏**。

    删掉那句 print，这条会红；把警告改成拦下（动退出码）也会红——二进制文件本身
    不是泄漏证据，它只是**没被检查**。
    """
    monkeypatch.setattr(scan, "_git", lambda *a: DIFF_BINARY)
    monkeypatch.setattr(scan, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(scan, "ALLOWLIST_PATH", tmp_path / "allowlist.txt")

    rc = scan.main(["--staged"])

    captured = capsys.readouterr()
    assert "x.bin" in captured.err, "必须点名那个一行都没被检查的文件"
    assert captured.out == "", "点名不是命中，不该出现在 stdout"
    assert rc == scan.EXIT_CLEAN, "警告不改退出码"


def test_staged_empty_diff_is_clean_not_error(scan, monkeypatch, capsys):
    """Review Focus 2：空暂存区（无改动提交 / --amend / merge）必须干净退出 0。"""
    monkeypatch.setattr(scan, "staged_added_lines", lambda *a, **k: [])
    monkeypatch.setattr(scan, "load_allowlist", lambda *a, **k: set())
    rc = scan.main(["--staged"])
    assert rc == scan.EXIT_CLEAN
    assert capsys.readouterr().out == ""


def test_text_mode_is_fail_closed_on_undecodable_input(scan, tmp_path, capsys):
    """Review Focus 3：提交信息不是合法 UTF-8 时必须退出 2，不是崩栈、也不是放行。"""
    bad = tmp_path / "msg"
    bad.write_bytes(b"ok\n\xff\xfe not utf8\n")
    rc = scan.main(["--text", str(bad)])
    assert rc == scan.EXIT_ERROR
    assert "utf-8" in capsys.readouterr().err.lower()


def test_text_mode_reports_hits_without_values(scan, tmp_path, monkeypatch, capsys):
    """豁免清单必须打桩——`FAKE` 自己就在本仓库里（它在 `tests/test_scan.py` 与计划文档里），
    所以 Task 7 把它登记进真清单之后，这条会**跟着变红**。判据不该依赖仓库当前登记了什么。
    """
    msg = tmp_path / "msg"
    msg.write_text("subject\n\nbody mentions " + FAKE + "\n", encoding="utf-8")
    monkeypatch.setattr(scan, "ALLOWLIST_PATH", tmp_path / "allowlist.txt")
    rc = scan.main(["--text", str(msg)])
    out = capsys.readouterr().out
    assert rc == scan.EXIT_HITS
    assert FAKE not in out
    assert ":3" in out


def test_staged_warns_when_allowlist_is_edited(scan, monkeypatch, capsys):
    """spec §4.3 不变式 2：改豁免名单是需要本人确认的动作。"""
    monkeypatch.setattr(scan, "staged_added_lines",
                        lambda *a, **k: [("security/allowlist.txt", 3, "x")])
    monkeypatch.setattr(scan, "load_allowlist", lambda *a, **k: set())
    rc = scan.main(["--staged"])
    err = capsys.readouterr().err
    assert "豁免清单" in err
    assert rc == scan.EXIT_CLEAN, (
        "警告不是拦截：清单里的值本就已登记，不会被当成未登记命中")


def test_staged_show_values_prints_value_and_warns(scan, tmp_path, monkeypatch, capsys):
    """`--staged --show-values` 是把**暂存区里的真标识**打到 stdout 的那条路——钩子要拦的
    正是它——所以 spec §4.4 要求这个逃生阀**自己警告这一点**。

    两半都钉：删掉那句 print 会红；把警告改成无条件打印也会红（没开 `--show-values` 时
    stderr 必须为空——没有值可警告，就不该吓人）。
    """
    monkeypatch.setattr(scan, "staged_added_lines",
                        lambda *a, **k: [("x.txt", 1, "hit " + FAKE + "\n")])
    monkeypatch.setattr(scan, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(scan, "ALLOWLIST_PATH", tmp_path / "allowlist.txt")

    assert scan.main(["--staged", "--show-values"]) == scan.EXIT_HITS
    shown = capsys.readouterr()
    assert FAKE in shown.out, "显式 --show-values 时才放行值本身"
    assert "命中值本身" in shown.err, "但那一刻必须自己警告（spec §4.4）"

    assert scan.main(["--staged"]) == scan.EXIT_HITS
    masked = capsys.readouterr()
    assert FAKE not in masked.out, "不带 --show-values 时值不许出现"
    assert masked.err == "", "没有值可警告时，stderr 一个字都不该有"


def test_staged_skips_registered_values(scan, tmp_path, monkeypatch, capsys):
    """终审 Important 4：豁免清单对 `--staged`（**生产路径**，钩子走的就是它）的过滤。

    没有这条时，把 `unregistered = [h for h in hits if h[3] not in allow]` 换成
    `unregistered = hits`，整套测试**全绿**——意味着「登记」这个动作可以在**钩子那条路**
    上静默失效而没有任何东西变红（默认视图那半边已有 test_default_view_skips_registered_values，
    生产路径这半边一直空着）。这条与 `--text` 共用同一行，所以一并钉住。
    """
    monkeypatch.setattr(scan, "staged_added_lines",
                        lambda *a, **k: [("x.txt", 1, "hit " + FAKE + "\n")])
    allow = tmp_path / "allowlist.txt"
    allow.write_text(FAKE + "  夹具\n", encoding="utf-8")
    monkeypatch.setattr(scan, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(scan, "ALLOWLIST_PATH", allow)

    rc = scan.main(["--staged"])

    captured = capsys.readouterr()
    assert rc == scan.EXIT_CLEAN, "登记过的值不该再拦"
    assert captured.out == "", "登记过就不该再打印任何一行"
    assert captured.err == "", "没有值可警告时 stderr 一个字都不该有"


def test_main_wrapper_is_fail_closed(scan, monkeypatch, capsys):
    """`main` 是薄壳：**任何**逃出 `_run` 的异常都以 2 退出。

    删掉 `main` 里那层 try/except，这条就会红。
    """
    def boom(_args):
        raise RuntimeError("模拟一个没有任何内层处理器接住的错误")

    monkeypatch.setattr(scan, "_run", boom)
    rc = scan.main(["--all"])

    assert rc == scan.EXIT_ERROR
    assert "扫描器出错" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["--values"], ["--staged"]])
def test_broken_allowlist_is_fail_closed_on_every_entry(scan, tmp_path, monkeypatch, argv):
    """三条入口在清单不可读时都必须退出 2。

    这个缺陷类在本计划里出现过三次，所以按**入口**参数化而不是只测一条。
    """
    allow = tmp_path / "allowlist.txt"
    allow.write_bytes(b"\xff\xfe not utf-8\n")
    monkeypatch.setattr(scan, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(scan, "_tracked_files", lambda: [])
    monkeypatch.setattr(scan, "ALLOWLIST_PATH", allow)
    monkeypatch.setattr(scan, "staged_added_lines", lambda *a, **k: [])

    assert scan.main(argv) == scan.EXIT_ERROR
