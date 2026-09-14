"""CLI 入口策略的回归护栏。

缺陷背景：`-n` 的非正值会一路传到 SQL 而不被拦下——`reader.recent` 的
`LIMIT -1` 在 SQLite 里表示「不限量」，取回后再被 `pieces[-limit:]` 错位切片，
于是 `-n -1` **静默丢掉一条**消息、`-n 0` 返回空；`search` 则退化成无上限全表
扫描。归一化现已收进 usecases（见 tests/test_usecases.py），CLI 只负责渲染。
"""
import os
import pathlib
from types import SimpleNamespace

import pytest

from agent_wechat import account, bootstrap, cli, errors, keys, reader, search, usecases, vector


@pytest.fixture()
def captured(monkeypatch):
    """替掉 MessageReader 与账号解析，捕获传给核心函数的 limit。"""
    seen: dict = {}

    class _StubReader:
        def __init__(self, acct):
            self.acct = acct

        def resolve_one(self, contact):
            return ("wxid_target", [])

        def recent(self, chat_id, limit=50):
            seen["recent_limit"] = limit
            return []

        def display_name(self, chat_id):
            return "张三"

    monkeypatch.setattr(reader, "MessageReader", _StubReader)
    monkeypatch.setattr(account, "resolve_account", lambda a: "acct")
    return seen


def test_recent_clamps_non_positive_limit(captured):
    for n in (-1, 0):
        cli.cmd_recent(SimpleNamespace(account=None, contact="张三", n=n))
        assert captured["recent_limit"] == 1, f"-n {n} 未被夹到 1"


def test_recent_clamps_large_limit_to_500(captured):
    """C2 起 CLI 也夹上界：`-n 1000` 与 MCP 的 1-500 对齐（有意的行为变更）。"""
    cli.cmd_recent(SimpleNamespace(account=None, contact="张三", n=1000))
    assert captured["recent_limit"] == 500


def test_search_clamps_non_positive_limit(monkeypatch):
    seen: dict = {}

    def fake_search(query, **kwargs):
        seen["limit"] = kwargs["limit"]
        return {"mode": "fts", "total": 0, "hits": []}

    monkeypatch.setattr(account, "resolve_account", lambda a: "acct")
    monkeypatch.setattr(search, "search", fake_search)
    for n in (-1, 0):
        cli.cmd_search(SimpleNamespace(account=None, contact=None, query="会议", n=n))
        assert seen["limit"] == 1, f"-n {n} 未被夹到 1"


def test_search_renders_self_as_me(monkeypatch, capsys):
    """自己发的消息一律印「我」，CLI search 也不例外。

    终审端到端实测的缺口：cmd_recent / cmd_semantic / mcp_server 三处都有
    `is_self` 分支，唯独 search 的 hit 渲染漏了——自己发的私聊消息印成自己的 wxid。
    """
    monkeypatch.setattr(usecases, "search_history", lambda *a, **k: {
        "mode": "fts", "total": 1,
        "hits": [{"ts": 1700000000, "chat_id": "wxid_friend01",
                  "sender_id": "wxid_t", "sender_name": None, "is_self": 1,
                  "content": "今晚一起吃饭吗"}]})
    cli.cmd_search(SimpleNamespace(account=None, contact=None, query="吃饭", n=10))
    assert capsys.readouterr().out == (
        "mode=fts total=1\n  [1700000000] wxid_friend01 我: 今晚一起吃饭吗\n")


# ---------- C1：统一错误契约（core 抛项目异常，CLI 翻转成 stderr + 退出码 1） ----------

def test_main_prints_project_error_to_stderr_and_exits_1(monkeypatch, capsys):
    """错误路径必须是单行中文 + 退出码 1，且**不得**出现 Traceback。"""
    monkeypatch.setattr(account.paths, "account_dirs", lambda: [])
    with pytest.raises(SystemExit) as ei:
        cli.main(["contacts", "--account", "wxid_missing_0000"])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "账号目录不存在" in err and "wxid_missing_0000" in err
    assert "Traceback" not in err


def test_main_keeps_argparse_exit_code_2():
    """退出码维持 0/1；argparse 自己的参数错（SystemExit(2)）必须原样保留。"""
    with pytest.raises(SystemExit) as ei:
        cli.main(["definitely-not-a-subcommand"])
    assert ei.value.code == 2


def test_main_success_still_exits_0(monkeypatch):
    class _StubReader:
        def __init__(self, acct):
            pass

        def contacts(self, query=None, limit=200):
            return []

    monkeypatch.setattr(account, "resolve_account", lambda a: "acct")
    monkeypatch.setattr(reader, "MessageReader", _StubReader)
    with pytest.raises(SystemExit) as ei:
        cli.main(["contacts"])
    assert ei.value.code == 0


def test_semantic_core_error_reaches_stderr_with_hint(monkeypatch, capsys):
    """semantic_search 现在抛异常而非返回 error dict；CLI 的翻转必须含 hint 单行输出。"""
    monkeypatch.setattr(account, "resolve_account", lambda a: "acct")

    def boom(*a, **k):
        raise vector.VectorError("向量索引为空", hint="请先运行 build_vector_index")

    monkeypatch.setattr(vector, "semantic_search", boom)
    with pytest.raises(SystemExit) as ei:
        cli.main(["semantic", "爬山"])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "向量索引为空（请先运行 build_vector_index）" in err
    assert "Traceback" not in err


# ---------- C8：bootstrap 的 print 迁到 CLI，四处输出逐字（同序/同流向 stdout） ----------

def test_cmd_bootstrap_output_is_verbatim(monkeypatch, capsys):
    """跑真实 bootstrap 编排（仅桩掉账号/捕获/校验/落盘），断言 CLI 的完整 stdout。"""
    monkeypatch.setattr(
        bootstrap.account, "status",
        lambda: SimpleNamespace(logged_in=True, current="wxid_testacct01_ab12", pid=999))
    monkeypatch.setattr(bootstrap, "capture_passphrase_lldb", lambda timeout: "ab" * 32)
    monkeypatch.setattr(
        bootstrap.keys, "verify_passphrase",
        lambda acct, pw: [("m/message_0.db", True)] * 3 + [("m/message_1.db", False)] * 2)
    saved: dict = {}
    monkeypatch.setattr(bootstrap.keys, "save_cached_hex",
                        lambda bare, h: saved.update(bare=bare, h=h))

    with pytest.raises(SystemExit) as ei:
        cli.main(["bootstrap", "--timeout", "7"])
    assert ei.value.code == 0

    out = capsys.readouterr()
    assert out.err == ""                      # 全部流向 stdout
    assert out.out == (
        "当前登录账号: wxid_testacct01 (pid=999)\n"
        "正在 attach lldb… 请在微信内【退出登录 → 重新登录】以触发密钥派生。\n"
        f"✅ HMAC 校验通过 3/5 个库，passphrase 已缓存到 {keys.CACHE_FILE}\n"
        "账号 wxid_testacct01 引导完成。之后所有操作均无需 sudo。\n"
    )
    assert saved == {"bare": "wxid_testacct01", "h": "ab" * 32}


def test_contact_miss_suffix_names_the_cli_command(monkeypatch, capsys):
    """未命中时 CLI 该提示 `contacts`，不是 MCP 的工具名 list_contacts。

    core 只抛中立的 ContactNotFound；下一步指引是入口专属的，由适配层追加
    （USAGE.md:195-196 文档化的就是这一条）。
    """
    monkeypatch.setattr(account, "resolve_account", lambda a: "acct")

    def boom(*a, **k):
        raise errors.ContactNotFound("找不到联系人「张三」")

    monkeypatch.setattr(usecases, "recent_messages", boom)
    with pytest.raises(SystemExit) as ei:
        cli.main(["recent", "张三"])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert err.strip() == "找不到联系人「张三」，请用 contacts 查询"
    assert "list_contacts" not in err          # 不能把 MCP 工具名报给 CLI 用户


# ---------- purge：手写互斥（spec §3）与 dry-run 渲染（spec §6） ----------

def test_purge_without_scope_exits_2(capsys):
    """用法错误 = 2（仓库既有约定：argparse 的参数错是 2、应用层是 1，见 cli.py:281）。"""
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge"])
    assert ei.value.code == 2
    assert "范围" in capsys.readouterr().err


def test_purge_all_with_named_scope_exits_2(capsys):
    """--all 与具名范围互斥。**用手写校验 + parser.error()**，不用 argparse 互斥组：
    互斥组不支持嵌套，把四个 flag 放一组会连 --index --tmp 一起禁掉（spec §3）。"""
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--all", "--keys"])
    assert ei.value.code == 2
    assert "不能与" in capsys.readouterr().err


def _isolate(tmp_path, monkeypatch):
    """把 purge 会碰的路径全部指向 tmp_path，并把外部命令注入桩。patch 的是**模块属性**——
    purge.py 用 `from . import keys` 取常量，所以 patch keys.CACHE_FILE 才生效
    （`from .keys import CACHE_FILE` 那种写法 patch 不掉，会删到真实数据，spec §4）。"""
    from agent_wechat import keys, paths, purge, store
    home = tmp_path / "home"; home.mkdir(parents=True)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")
    # lsof（_holders）与 tmutil（_tmutil）都经 _run：桩打在最低层，一处覆盖两个外部命令。
    # 不加这个，任何走进 execute/verify 的 CLI 测试都会去跑**真实的** lsof 与 tmutil。
    # 三个字段都是被测代码的隐式契约：_holders 只读 stdout，_tmutil 还要读 returncode 与
    # stderr（非零退出要报「无法判定」而不是「无输出」）。少一个就是一个 AttributeError。
    monkeypatch.setattr(purge, "_run",
                        lambda *a, **kw: type("R", (), {"stdout": "", "stderr": "",
                                                        "returncode": 0})())
    # $TMPDIR 同样要隔离：verify 会 glob 它找 awcap_* 残留，否则读的是本机真实临时目录
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmpdir"))
    return home


def test_purge_named_scopes_can_combine(tmp_path, monkeypatch):
    """三个具名范围可任意组合——这正是 argparse 互斥组做不到的那一半（spec §3）。"""
    home = _isolate(tmp_path, monkeypatch)
    (home / "index.db").write_bytes(b"x" * 10)
    # main() 结尾是 raise SystemExit(code)，**从不返回**（cli.py:284）。既有测试一律
    # 写成 pytest.raises(SystemExit) 再断 ei.value.code（tests/test_cli.py:70-71 等）。
    # 写成 `assert cli.main(...) == 0` 的话 SystemExit(0) 会逃出测试体，测试以未捕获
    # 异常失败而非断言失败，实现者会误判成「实现没跑通」。
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--index", "--tmp"])             # dry-run，退出 0
    assert ei.value.code == 0
    assert (home / "index.db").exists()                     # 一个字节没删


def test_purge_refused_renders_single_line_not_traceback(tmp_path, monkeypatch, capsys):
    """有进程持有产物时，CLI 必须打一行中文（含 hint）到 stderr 并退 1，
    而不是让 PurgeRefused 穿透成 traceback——这正是 C1 契约要消灭的东西，spec §6 有明文行。

    **这条路径此前零覆盖**，而且藏法和 T6 那个 NameError 一模一样：_isolate 的 _run 桩
    令 _holders 恒返回 []，于是占用拒绝这个分支一次都不会进——被桩挡住的不可达代码，
    看起来像被测过。"""
    from agent_wechat import purge as _purge
    home = _isolate(tmp_path, monkeypatch)
    (home / "passphrase.json").write_bytes(b"x")
    monkeypatch.setattr(_purge, "_holders",
                        lambda p: [4242] if p.name == "passphrase.json" else [])
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--keys", "--yes"])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "4242" in err, f"拒绝理由必须带 pid，实际：{err!r}"
    assert "（" in err, f"to_text 应渲染成 message（hint），实际：{err!r}"
    assert "Traceback" not in err
    assert (home / "passphrase.json").exists()          # 拒绝时一个字节都没删


def test_render_residue_shows_held_with_pids(capsys):
    """held 的**渲染层**钉子。T6 的复审指出：execute 在有进程持有时会拒绝执行，
    所以走 cli.main 不可能构造出「verify 时有进程持有」的状态——这条只能直接单测渲染函数。

    空 pid 列表的**过滤在 verify 侧**（purge.py 的 `and (pids := _holders(...))`），
    渲染层并不设防：它只是照 `','.join(map(str, pids))` 直印。本测试钉的是**格式串**，
    即那些 pid 确实会被印出来，而不是断言渲染函数自带空列表守卫。"""
    from agent_wechat import cli as _cli, purge as _purge
    rep = _purge.ResidueReport(not_removed=(), kept=(), held={"index.db": [4242]},
                               tmp_leaks=(), tm_excluded="否", snapshots="无输出")
    _cli._render_residue(rep)
    out = capsys.readouterr().out
    assert "index.db" in out and "4242" in out
    assert "pid  持有" not in out           # 空 pid 的假警告形态


def test_purge_yes_executes_and_reports(tmp_path, monkeypatch, capsys):
    """--yes 是唯一真正删东西的路径，其汇总行「已删除 N 项，释放 X」是两条裁定的**唯一
    用户可见出口**——而 T7 的四个测试此前全是 dry-run 或以退出码 2 结束，这条路径零覆盖，
    只有 T9 的手工验收护着，手工验收不防回归。"""
    home = _isolate(tmp_path, monkeypatch)
    (home / "passphrase.json").write_bytes(b"x" * 2_000_000)   # 2 MB，便于断言体积
    (home / "index.db").write_bytes(b"y" * 3000)
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--keys", "--yes"])
    assert ei.value.code == 0
    assert not (home / "passphrase.json").exists()
    assert (home / "index.db").exists()          # 范围外的一个字节没删
    out = capsys.readouterr().out
    assert "已删除 1 项" in out
    assert "2 MB" in out, f"freed 必须反映真实字节数，而实际输出是：{out!r}"


def test_render_plan_shows_no_size_for_directory(tmp_path, monkeypatch, capsys):
    """目录行不得显示体积。`PlanItem.size` 对目录是 `lstat().st_size`（inode 自身大小，
    几十到几百字节），而 execute 的汇总行用 `_tree_size` 量整树——同一个 tmp/ 会在计划表里
    显示 96 B、在汇总行显示几百 MB，差好几个数量级。spec §3 的表样对那一行标的正是「—」。"""
    home = _isolate(tmp_path, monkeypatch)
    (home / "tmp").mkdir()
    (home / "tmp" / "plaintext.db").write_bytes(b"x" * 500_000)   # 目录里确实有半 MB 明文
    with pytest.raises(SystemExit):
        cli.main(["purge", "--tmp"])
    out = capsys.readouterr().out
    assert "500 KB" not in out and "500000 B" not in out, f"目录行不该显示体积：{out!r}"
    assert "—" in out, f"目录行应显示「—」：{out!r}"


def test_purge_all_dry_run_renders_loose_rows(tmp_path, monkeypatch, capsys):
    """--all 的 `plan.loose` 渲染分支此前**从未被执行过**：测试全用具名范围，
    报告的冒烟也用 `--index`。那儿一个格式串写坏会静默发布，而它正是
    spec §3「枚举 HOME_DIR 顶层条目逐条列出」那条要求的落点。"""
    home = _isolate(tmp_path, monkeypatch)
    (home / "leaked.db").write_bytes(b"stray")      # 不属于任何具名范围
    (home / "models").mkdir()
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--all"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "leaked.db" in out and "models" in out
    assert "不属于任何具名范围" in out
    assert (home / "leaked.db").exists()            # dry-run，一个字节没删


def test_render_plan_all_shows_size_for_loose_entries(tmp_path, monkeypatch, capsys):
    """spec §3 的 `--all` 样例表里 `models/` 有 95 MB、`tasks.json` 有 2 B，而实现只印路径。
    目录必须用**整树**体积，不能用 `lstat().st_size`（inode 自身大小）——那正是 T4 在 freed
    上修过的同一个错误的兄弟形态，落在渲染器里：models/ 会印成「96 B」。"""
    home = _isolate(tmp_path, monkeypatch)
    (home / "models").mkdir()
    (home / "models" / "model.onnx").write_bytes(b"x" * 500_000)
    (home / "tasks.json").write_bytes(b"{}")
    with pytest.raises(SystemExit):
        cli.main(["purge", "--all"])
    out = capsys.readouterr().out
    assert "500 KB" in out, f"loose 目录行必须印整树体积（不是 inode 大小）：{out!r}"
    assert "2 B" in out, f"loose 文件行必须印体积：{out!r}"


def test_render_plan_uses_decimal_units(tmp_path, monkeypatch, capsys):
    """体积单位必须是十进制且标签与数字相符。此前 ≥1 GiB 分支除以 1024**3 却标 GB
    （印 4.94 GB，与 help 的 5.3 GB 打架），小文件走 MiB 分支把 182 B 印成 0.0 MiB
    （读起来像空文件）——同一个文件在一次会话里出现三个数字。"""
    home = _isolate(tmp_path, monkeypatch)
    (home / "index.db").write_bytes(b"x" * 2_000_000)      # 2 MB
    (home / "passphrase.json").write_bytes(b"y" * 182)     # 182 B
    with pytest.raises(SystemExit):
        cli.main(["purge", "--index", "--keys"])
    out = capsys.readouterr().out
    assert "2 MB" in out, f"十进制 MB 未出现：{out!r}"
    assert "182 B" in out, f"小文件必须印字节数而不是 0.0 MiB：{out!r}"


def test_human_size_never_rounds_into_the_next_unit():
    """999,500–999,999 此前印成「1000 KB」、999,999,999 印成「1000.0 MB」——用户按单位读
    会以为「1000.0 MB」比「1.00 GB」还大（真实值差 1 字节）。舍入把数字推进下一档时，
    单位必须跟着走。

    同时钉住 spec §3 计划表的五个样例：GB 保两位小数、整数不印「.0」——`5.30 GB` 与
    `12 MB` 只在「整数省略小数」这一条规则下同时成立（spec 自己的格式并不自洽，
    这是唯一解）。"""
    assert cli._human_size(999_499) == "999 KB"
    assert cli._human_size(999_500) == "1.0 MB"
    assert cli._human_size(999_999) == "1.0 MB"
    assert cli._human_size(999_949_999) == "999.9 MB"
    assert cli._human_size(999_999_999) == "1.00 GB"
    assert cli._human_size(5_300_000_000) == "5.30 GB"
    assert cli._human_size(12_000_000) == "12 MB"
    assert cli._human_size(182) == "182 B"
    assert cli._human_size(95_000_000) == "95 MB"
    assert cli._human_size(2) == "2 B"


def test_purge_index_help_has_no_hardcoded_size(capsys):
    """help 里硬编码的「5.3 GB」与渲染器是两个独立数字来源，会随索引增长漂移（今天一致
    纯属巧合）。argparse 的 help 是构建时字符串，动态取值要在 --help 时读盘——不合适。
    这个数字的正确位置是 dry-run 表，那里它取自真实产物。"""
    with pytest.raises(SystemExit):
        cli.main(["purge", "--help"])
    out = capsys.readouterr().out
    assert "5.3 GB" not in out, f"help 不该带会漂移的硬编码体积：{out!r}"
    assert "--index" in out, "反面：别把整行 help 删掉来过关"
    assert "MiB" not in out and "4.94 GB" not in out


def test_purge_all_refuses_on_empty_home_env(tmp_path, monkeypatch, capsys):
    """`AGENT_WECHAT_HOME=`（设了但为空）会让 os.environ.get 返回 ""，
    而 Path("") 是 `.`——`purge --all --yes` 因此会递归删掉当前工作目录。
    实测确认过：目录连内容全没，而最后一行还打着 ✅。这是一道数据丢失闸门。

    注意这条**不走 `_isolate`**：`_isolate` 会把 HOME_DIR patch 成绝对路径，正好把这个缺陷
    掩掉。也因此它必须由 plan() 的第一道闸门拦下——任何一次 execute 都已经太晚了。

    `chdir(tmp_path)` 是**爆炸半径控制**，不是隔离洁癖：闸门一旦回归，这条测试做的第一件事就是
    rmtree 当前工作目录，而 pytest 的 CWD 就是仓库本身。绑到 tmp_path 后，回归只毁一个临时目录，
    测试仍然照常红（断言「相对路径」在 stderr 里）。"""
    from agent_wechat import paths as _paths
    monkeypatch.chdir(tmp_path)          # CWD 就是缺陷要删的东西，先挪到临时目录
    monkeypatch.setenv("AGENT_WECHAT_HOME", "")
    monkeypatch.setattr(_paths, "HOME_DIR", pathlib.Path(""))
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--all", "--yes"])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "相对路径" in err and "Traceback" not in err


def test_purge_dry_run_refuses_symlink_loop_without_traceback(tmp_path, monkeypatch, capsys):
    """指向符号链接环的 AGENT_WECHAT_HOME 让裸 RuntimeError 穿透 CLI——而这条是 **dry-run**：
    用户只是想看看会删什么，拿到的是一整个 traceback。核心修复在 purge._check_home_is_sane，
    这里钉的是用户可见的那个结果：退 1、理由可读、不出现 traceback。

    不走 `_isolate`：它会把 HOME_DIR patch 成绝对路径，正好把这个输入掩掉。
    闸门在 plan() 第一句，所以任何真实产物都不会被 lstat 到。"""
    from agent_wechat import paths as _paths
    a = tmp_path / "a"; b = tmp_path / "b"
    a.symlink_to(b); b.symlink_to(a)
    monkeypatch.setattr(_paths, "HOME_DIR", a)
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--index"])          # dry-run：没有 --yes
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err, f"不得以裸异常穿透：{err!r}"
    assert "无法解析" in err


def test_status_refuses_symlink_loop_home_without_traceback(tmp_path, monkeypatch, capsys):
    """status 是诊断命令：环境坏掉时它正是用户唯一的工具，不能自己也崩。
    头三行不依赖 HOME_DIR，照常打印；到读缓存那行转成项目异常，退 1、理由可读。

    账号用 stub——不依赖微信此刻是否在跑（真实 st.accounts 为空时循环不执行，
    这条测试就会静默地什么都不测）。"""
    from agent_wechat import paths as _paths
    a = tmp_path / "a"; b = tmp_path / "b"
    a.symlink_to(b); b.symlink_to(a)
    monkeypatch.setattr(account, "status",
                        lambda: SimpleNamespace(running=False, current=None,
                                                accounts=["wxid_stub_ab12"], pid=None))
    monkeypatch.setattr(_paths, "HOME_DIR", a)
    monkeypatch.setattr(keys, "CACHE_FILE", a / "passphrase.json")
    with pytest.raises(SystemExit) as ei:
        cli.main(["status"])
    assert ei.value.code == 1
    out, err = capsys.readouterr()
    assert "本机账号: wxid_stub_ab12" in out, f"不依赖 HOME_DIR 的部分该照常输出：{out!r}"
    assert "Traceback" not in err, f"不得以裸异常穿透：{err!r}"
    assert "无法读取" in err


def test_purge_dry_run_prints_absolute_target(tmp_path, monkeypatch, capsys):
    """计划表必须印出绝对目标目录。此前只印相对路径（`keys passphrase.json`、`all sub`），
    用户按下 --yes 时看不出这些相对路径指的是当前工作目录。"""
    home = _isolate(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--all"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert f"目标目录：{home.resolve()}" in out, f"计划表未印绝对目标：{out!r}"


def test_purge_index_refuses_when_artifact_parent_unreadable(tmp_path, monkeypatch, capsys):
    """产物父目录不可搜索时，CLI 必须是「一行中文 + 退出码 1」，不是 traceback。
    实测（修复前）：`purge --index --yes` 从 purge.py 的 is_symlink() 抛裸 PermissionError
    穿透到用户面前。这里把 index.db 挪进一个 mode 000 的子目录——HOME_DIR 本身仍可读，
    所以 plan 那道「目录不可读」的闸门不触发，走的正是这条历史 traceback 路径。"""
    from agent_wechat import store as _store
    home = _isolate(tmp_path, monkeypatch)
    sub = home / "sub"; sub.mkdir()
    (sub / "index.db").write_bytes(b"x" * 10)
    monkeypatch.setattr(_store, "DB_PATH", sub / "index.db")
    os.chmod(sub, 0o000)
    try:
        with pytest.raises(SystemExit) as ei:
            cli.main(["purge", "--index", "--yes"])
    finally:
        os.chmod(sub, 0o700)
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert len(err.strip().splitlines()) == 1, f"应当是单行中文，实际：{err!r}"
    assert (sub / "index.db").exists()


def test_purge_dry_run_refuses_when_artifact_parent_unreadable(tmp_path, monkeypatch, capsys):
    """dry-run 也必须退 1，而不是对着**活文件**印「（不存在）」——这个命令的全部价值就在于让
    用户在动手前看清将发生什么。根因是 _item() 把 lstat 的 OSError 一律当成「不存在」，
    这条走的是 plan 阶段（没有 --yes）。"""
    from agent_wechat import store as _store
    home = _isolate(tmp_path, monkeypatch)
    sub = home / "sub"; sub.mkdir()
    (sub / "index.db").write_bytes(b"x" * 10)
    monkeypatch.setattr(_store, "DB_PATH", sub / "index.db")
    os.chmod(sub, 0o000)
    try:
        with pytest.raises(SystemExit) as ei:
            cli.main(["purge", "--index"])              # dry-run：没有 --yes
    finally:
        os.chmod(sub, 0o700)
    assert ei.value.code == 1
    out, err = capsys.readouterr()
    assert "无法读取待删路径" in err and "Traceback" not in err
    assert "将删除" not in out, f"拒绝时不得印出计划表：{out!r}"


def test_purge_all_yes_reports_home_removed(tmp_path, monkeypatch, capsys):
    """`--all` 成功时 removed 数的是 HOME_DIR 的顶层条目，目录自身不在其中——空目录的 --all
    会印「已删除 0 项，释放 0 B」，用户从输出里看不出「目录本身也没了」。"""
    home = _isolate(tmp_path, monkeypatch)
    (home / "index.db").write_bytes(b"x" * 10)
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--all", "--yes"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert not home.exists()
    assert "数据目录本身" in out, f"未报告目录自身也被删：{out!r}"


def test_purge_all_loose_rows_name_known_artifacts(tmp_path, monkeypatch, capsys):
    """`--all` 的 loose 行此前一律印「不属于任何具名范围」——对 models/ 与 tasks.json 是误导：
    它们是 vector.MODEL_CACHE_DIR 与 tasks.TASKS_FILE，有拥有者，只是没有对应的 CLI 开关。
    spec §3 要求每行点名它属于哪个已知产物，或标「散落文件」。"""
    from agent_wechat import tasks as _tasks, vector as _vector
    home = _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(_vector, "MODEL_CACHE_DIR", home / "models")
    monkeypatch.setattr(_tasks, "TASKS_FILE", home / "tasks.json")
    (home / "models").mkdir()
    (home / "tasks.json").write_text("{}")
    (home / "leaked.db").write_bytes(b"stray")
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--all"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    lines = {name: next(l for l in out.splitlines() if name in l)
             for name in ("models", "tasks.json", "leaked.db")}
    assert "已知产物" in lines["models"] and "向量模型缓存" in lines["models"]
    assert "已知产物" in lines["tasks.json"] and "长任务注册表" in lines["tasks.json"]
    assert "不属于任何具名范围" in lines["leaked.db"]


def test_purge_dry_run_prints_plan_and_deletes_nothing(tmp_path, monkeypatch, capsys):
    home = _isolate(tmp_path, monkeypatch)
    (home / "index.db").write_bytes(b"x" * 2048)
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--index"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "将删除" in out and "未删除任何文件" in out
    assert (home / "index.db").exists()
