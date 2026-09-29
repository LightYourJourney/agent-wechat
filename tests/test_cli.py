"""CLI 入口策略的回归护栏。

缺陷背景：`-n` 的非正值会一路传到 SQL 而不被拦下——`reader.recent` 的
`LIMIT -1` 在 SQLite 里表示「不限量」，取回后再被 `pieces[-limit:]` 错位切片，
于是 `-n -1` **静默丢掉一条**消息、`-n 0` 返回空；`search` 则退化成无上限全表
扫描。归一化现已收进 usecases（见 tests/test_usecases.py），CLI 只负责渲染。
"""
import json
import os
import pathlib
from types import SimpleNamespace

import pytest

from agent_wechat import (account, bootstrap, cli, errors, keys, media, paths, reader,
                          search, store, usecases, vector)
# 合成微信目录（真实 sqlcipher 解密链路；路径全部指向 tmp_path，绝不碰真实数据）。
# 跨模块复用夹具与 test_usecases 同法：`env` 依赖它**自己模块**里的 module 级 `wechat`。
from test_reader_export_pipeline import (ACCT, FRIEND, T0, build_shard, drift_tree,  # noqa: F401
                                         enc_db, env, wechat)
# T8 的 C3 用例走**真实** message_media，要的就是一份只含 messages 的索引：与
# test_usecases_media_link 同一套夹具（索引 + `_FakeCtx`），抄一份必漂。导入的都是常量、
# 桩类与 fixture，没有 test_* 名，不会被本模块重复收集。
from test_usecases_media_link import CM, MON, TS, _FakeCtx, idx  # noqa: F401


@pytest.fixture()
def captured(monkeypatch):
    """替掉 MessageReader 与账号解析，捕获传给核心函数的 limit。"""
    seen: dict = {}

    class _StubReader:
        def __init__(self, acct):
            self.acct = acct
            self._drift = []         # 真实 MessageReader 的状态：collect_degradations 会读

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


def test_search_clamps_non_positive_limit(env, monkeypatch):
    """`env` 不只是给数据：`usecases._refresh_roster` 会开 `store.open()`，缺了它就按
    import 期绑定的 `store.DB_PATH` 打开**真实** index.db（新加的 DDL 会写进去）。"""
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
                  "content": "今晚一起吃饭吗"}],
        "degraded": []})
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
            self._drift = []         # 真实 MessageReader 的状态：collect_degradations 会读

        def contacts(self, query=None, limit=200):
            return []

    monkeypatch.setattr(account, "resolve_account", lambda a: "acct")
    monkeypatch.setattr(reader, "MessageReader", _StubReader)
    with pytest.raises(SystemExit) as ei:
        cli.main(["contacts"])
    assert ei.value.code == 0


def test_semantic_core_error_reaches_stderr_with_hint(env, monkeypatch, capsys):
    """semantic_search 现在抛异常而非返回 error dict；CLI 的翻转必须含 hint 单行输出。

    `env` 同 test_search_clamps_non_positive_limit：这条也会先经 `_refresh_roster`
    开一次 store——不隔离就会碰真实 index.db。"""
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
    （USAGE.md §9「输出约定与错误提示」文档化的就是这一条）。
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
    from agent_wechat import keys, paths, purge, store, tasks, vector
    home = tmp_path / "home"; home.mkdir(parents=True)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")
    # models/ 与 tasks.json 是**没有 CLI 开关**的产物（scope=None）：`--foo` 选不中它们，但
    # `--all` 会删，而 purge 取的正是 known_artifacts() 里那两个**真实路径**。此前不必 patch
    # 只是因为它们仅作为从 home 枚举出的 loose 行存在；升成产物后不 patch 就真的会碰真机
    # ——实测症状是 `purge --all --yes` 去扫真实的 ~/.agent-wechat/models（huggingface 布局里
    # snapshots/ 全是符号链接），报「待删路径是符号链接」退出 1。
    monkeypatch.setattr(vector, "MODEL_CACHE_DIR", home / "models")
    monkeypatch.setattr(tasks, "TASKS_FILE", home / "tasks.json")
    # lsof（_holders）与 tmutil（_tmutil）都经 _run：桩打在最低层，一处覆盖两个外部命令。
    # 不加这个，任何走进 execute/verify 的 CLI 测试都会去跑**真实的** lsof 与 tmutil。
    # 三个字段都是被测代码的隐式契约：_holders 只读 stdout，_tmutil 还要读 returncode 与
    # stderr（非零退出要报「无法判定」而不是「无输出」）。少一个就是一个 AttributeError。
    monkeypatch.setattr(purge, "_run",
                        lambda *a, **kw: type("R", (), {"stdout": "", "stderr": "",
                                                        "returncode": 0})())
    # $TMPDIR 同样要隔离：verify 会 glob 它找 awcap_* 残留，否则读的是本机真实临时目录
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmpdir"))
    # 红线（同 tests/test_purge.py 的 autouse fixture，但查的是结果而不是 patch 列表）：
    # 每一条已知产物的路径都必须落在 tmp_path 下。上面漏 patch 过一次，症状不是测试失败，
    # 而是测试去读真实数据目录——所以兜底断言必须不看具体 patch 了哪些常量。
    for a in purge.known_artifacts():
        assert a.path.is_relative_to(tmp_path), f"红线：{a.path} 指向真实数据目录"
    return home


def test_purge_named_scopes_can_combine(tmp_path, monkeypatch):
    """具名范围可任意组合——这正是 argparse 互斥组做不到的那一半（spec §3）。"""
    home = _isolate(tmp_path, monkeypatch)
    (home / "index.db").write_bytes(b"x" * 10)
    # main() 结尾是 raise SystemExit(code)，**从不返回**（cli.py:284）。既有测试一律
    # 写成 pytest.raises(SystemExit) 再断 ei.value.code（tests/test_cli.py:70-71 等）。
    # 写成 `assert cli.main(...) == 0` 的话 SystemExit(0) 会逃出测试体，测试以未捕获
    # 异常失败而非断言失败，实现者会误判成「实现没跑通」。
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--index", "--media", "--moments", "--tmp"])   # dry-run，退出 0
    assert ei.value.code == 0
    assert (home / "index.db").exists()                     # 一个字节没删


@pytest.mark.parametrize("scope", ["keys", "index", "media", "moments", "tmp"])
def test_every_named_scope_has_a_working_flag(scope, tmp_path, monkeypatch, capsys):
    """五个具名范围各要有一个**真的能用**的开关。两半缺一不可：argparse 必须接受
    `--<scope>`（开关存在），且 plan() 里必须有产物认领它（否则是静默空操作——报成功、
    一个字节没删）。

    漂移的两个方向都被这条钉住：加了开关没加产物 → plan 抛「未知的范围」、退出 1（不是 0）；
    加了产物没加开关 → argparse 退 2。tests/test_purge.py 里另有一条钉住名单本身。
    """
    _isolate(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", f"--{scope}"])
    assert ei.value.code == 0, f"--{scope} 没被接受，或 dry-run 不是干净退出"
    out = capsys.readouterr().out
    assert f"  {scope} " in out, f"--{scope} 没有渲染出属于它的行：{out!r}"


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


def test_render_plan_shows_tree_size_for_directory(tmp_path, monkeypatch, capsys):
    """目录行必须显示**整树**体积，不能是 inode 自身大小。

    这条此前是反过来的（要求目录行印「—」占位）：当时 `PlanItem.size` 对目录取
    `lstat().st_size`，与 execute 汇总行的 `_tree_size` 差好几个数量级，渲染器只好回避。
    回避的代价是**最大的一项看不到体积**——media/ 有 10 GB，在那张用户据以说 --yes 的表上
    看起来无事发生。修法挪到了源头：`_item` 对目录走 `_tree_size`，两处同源，渲染器不必再藏。
    """
    home = _isolate(tmp_path, monkeypatch)
    (home / "tmp").mkdir()
    (home / "tmp" / "plaintext.db").write_bytes(b"x" * 500_000)   # 目录里确实有半 MB 明文
    with pytest.raises(SystemExit):
        cli.main(["purge", "--tmp"])
    out = capsys.readouterr().out
    assert "500 KB" in out, f"目录行必须显示整树体积：{out!r}"
    assert "—" not in out, f"不该再有「—」占位（它等于让用户看不见要删多少）：{out!r}"


def test_purge_all_dry_run_renders_loose_rows(tmp_path, monkeypatch, capsys):
    """--all 的 `plan.loose` 渲染分支此前**从未被执行过**：测试全用具名范围，
    报告的冒烟也用 `--index`。那儿一个格式串写坏会静默发布，而它正是
    spec §3「枚举 HOME_DIR 顶层条目逐条列出」那条要求的落点。"""
    home = _isolate(tmp_path, monkeypatch)
    (home / "leaked.db").write_bytes(b"stray")      # 真散落：不属于任何已知产物
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--all"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "leaked.db" in out
    assert "不属于任何已知产物" in out
    # 敏感度写「未知」而不是留空：这张表里空着读起来是「已检查过、不要紧」，
    # 而真实含义是「没人认识它」——两者正相反。
    assert "未知" in out, f"无主条目的敏感度必须写「未知」：{out!r}"
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


def test_purge_all_scopeless_artifacts_are_full_rows(tmp_path, monkeypatch, capsys):
    """`models/` 与 `tasks.json` 有拥有者、没有 CLI 开关（scope=None）。它们此前的呈现是
    loose 段里一句「已知产物：…」——**敏感度与重建代价两列是空的**，而整张 `--all` 表的意义
    就在于让用户看清自己要删掉什么、删了要付什么代价。

    现在它们进 items 段，与有开关的产物共用同一套列。spec §3「每行点名它属于哪个已知产物」
    那条要求因此以更强的形式满足：不只是点名，还带上了那两列。"""
    home = _isolate(tmp_path, monkeypatch)          # 这两个常量由 _isolate 一并重定向
    (home / "models").mkdir()
    (home / "tasks.json").write_text("{}")
    (home / "leaked.db").write_bytes(b"stray")
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--all"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    lines = {name: next(l for l in out.splitlines() if name in l)
             for name in ("models", "tasks.json", "leaked.db")}
    assert "重新下载" in lines["models"], "models 行必须带重建代价——loose 行时代这一列是空的"
    assert "否" in lines["models"], "models 是公开模型资产，敏感度列应为「否」"
    assert "否" in lines["tasks.json"], "tasks.json 不敏感，敏感度列应为「否」"
    # 反断言：它们不得再作为「不属于任何已知产物」的散落行出现——那读起来是「没人认识的野
    # 文件」，而 models/ 恰恰是向量模型的落点（删了要重新下载）。
    assert "不属于任何已知产物" not in lines["models"]
    assert "不属于任何已知产物" not in lines["tasks.json"]
    assert "不属于任何已知产物" in lines["leaked.db"]


def test_purge_dry_run_prints_plan_and_deletes_nothing(tmp_path, monkeypatch, capsys):
    home = _isolate(tmp_path, monkeypatch)
    (home / "index.db").write_bytes(b"x" * 2048)
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--index"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "将删除" in out and "未删除任何文件" in out
    assert (home / "index.db").exists()


def test_purge_index_says_the_exclusion_list_survives(tmp_path, monkeypatch, capsys):
    """`purge --index` 是常规操作（回收 5.6 GB / 强制重建）。它删完之后必须**响亮地**
    告诉用户排除清单还在——否则用户会以为隐私删除随索引一起没了，
    或者更糟：以为索引没了删除就没了、于是不敢重建。"""
    # 用既有的 _isolate；预先塞两条墓碑
    home = _isolate(tmp_path, monkeypatch)
    from agent_wechat import exclusions, store
    with store.open() as con:
        exclusions.add(con, [("a|m|message_0.db|1", "a", "m"),
                             ("a|m|message_0.db|2", "a", "m")])
    (home / "index.db").write_bytes(b"x" * 10)
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--index", "--yes"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "排除清单保留" in out and "2 条" in out
    assert (home / "index.db").exists() is False


def test_exclusions_survive_index_purge(tmp_path, monkeypatch):
    """**AC4 前半、不变式①（spec §4.1）**：`purge --index` 之后排除清单必须**还在**。

    它没了，用户重建后以为早删干净的会话原样回来，而**他不会知道**——所以这条不挂任何
    标记：它是全套里唯一一条断言「清单活过 --index」的测试（上面那条只断言那句提示，
    不断言文件本身）。与后半（重导后仍被拦）是两个不同的东西，故意分开两条。
    """
    home = _isolate(tmp_path, monkeypatch)
    from agent_wechat import exclusions, store
    with store.open() as con:
        exclusions.add(con, [("a|aaaa|message_0.db|1", "a", "aaaa")])
    (home / "index.db").write_bytes(b"x" * 10)
    with pytest.raises(SystemExit):
        cli.main(["purge", "--index", "--yes"])
    assert (home / "exclusions.db").exists(), "排除清单必须活过 --index"
    with store.open() as con:
        assert exclusions.count(con) == 1


def test_reimport_after_index_purge_is_still_blocked(tmp_path, monkeypatch):
    """**AC4 后半**：重建索引之后，那批仍必须被拦（spec §5.4 第 1 条）。

    拦在**写入端**的判据是 Task 4 的（`_filter_excluded` + `upsert_batch` 的返回值）。
    """
    home = _isolate(tmp_path, monkeypatch)
    from agent_wechat import exclusions, store
    with store.open() as con:
        exclusions.add(con, [("a|aaaa|message_0.db|1", "a", "aaaa")])
    (home / "index.db").write_bytes(b"x" * 10)
    with pytest.raises(SystemExit):
        cli.main(["purge", "--index", "--yes"])
    with store.open() as con:          # 重建索引（store.open 会把 schema 建回来）
        n = store.upsert_batch(con, [{
            "msg_key": "a|aaaa|message_0.db|1", "account": "a", "chat_md5": "aaaa",
            "chat_id": "victim", "shard": "message_0.db", "local_id": 1, "server_id": 1,
            "ts": 100, "msg_type": 1, "sender_id": None, "sender_name": None,
            "is_self": 0, "content": "本该删掉的", "media_hint": None}])
        assert n == 1, "重建索引之后那条仍必须被拦"
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_purge_all_says_how_many_exclusions_are_lost(tmp_path, monkeypatch, capsys):
    """`--all` 是真把清单删掉的那条路，它必须报出**失去了多少条**（spec §4.1③）——
    而这个数只能是删**之前**那份：删完文件就没了，那时读只剩 0 或「没有」。"""
    home = _isolate(tmp_path, monkeypatch)
    from agent_wechat import exclusions, store
    with store.open() as con:
        exclusions.add(con, [("a|m|message_0.db|1", "a", "m"),
                             ("a|m|message_0.db|2", "a", "m")])
    (home / "index.db").write_bytes(b"x" * 10)
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--all", "--yes"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert not home.exists()
    assert "排除清单已随之删除：2 条" in out, f"--all 必须报出删前那份计数：{out!r}"


def test_purge_all_does_not_claim_an_empty_list_was_lost(tmp_path, monkeypatch, capsys):
    """空清单是**常态**：`store.connect()` 每次都把 exclusions.db 建出来，所以每个跑过一次
    工具的用户都有它。此时「已随之删除：0 条排除失效」是假话——没有任何东西失效。"""
    home = _isolate(tmp_path, monkeypatch)
    with store.open():                 # 只把清单文件建出来，一条墓碑都不写
        pass
    (home / "index.db").write_bytes(b"x" * 10)
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--all", "--yes"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert not home.exists()
    assert "排除清单" not in out, f"空清单不该印「已随之删除」那句：{out!r}"


def test_purge_index_says_when_the_exclusion_list_is_unreadable(tmp_path, monkeypatch,
                                                                capsys):
    """**「读不了」必须与「没有清单」分开说**（M-6）——两者此前都返回 `None`、都不出声，
    而含义正相反：前者意味着**无法确认排除还在不在生效**（用户此刻最需要知道的一句），
    后者是常态（`store.connect()` 每次都把清单文件建出来）。

    夹具是「不是数据库的文件」：`ATTACH` 本身不报错，读它才报 `file is not a database`。
    提示走 stderr、且**不影响退出码**——这句提示不该把销毁本身弄成失败。
    """
    home = _isolate(tmp_path, monkeypatch)
    (home / "exclusions.db").write_bytes("这不是一个数据库".encode())
    (home / "index.db").write_bytes(b"x" * 10)
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--index", "--yes"])
    assert ei.value.code == 0, "读不出来只是提示，不许把销毁本身弄成失败"
    captured = capsys.readouterr()
    assert "排除清单读不出来" in captured.err, f"这句必须印出来：{captured.err!r}"
    assert "排除清单保留" not in captured.out, "读不出来时不许说「保留 N 条」"


def test_purge_all_says_an_unreadable_list_is_gone_without_a_count(tmp_path, monkeypatch,
                                                                   capsys):
    """`--all` 把读不出来的清单**真删掉**时，后果那句话照样要说（M-6）：
    条数缺了不代表「被删内容下次导出会回来」这条后果也缺了——那正是用户必须知道的。"""
    home = _isolate(tmp_path, monkeypatch)
    (home / "exclusions.db").write_bytes("这不是一个数据库".encode())
    (home / "index.db").write_bytes(b"x" * 10)
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--all", "--yes"])
    assert ei.value.code == 0
    assert not home.exists()
    captured = capsys.readouterr()
    assert "排除清单读不出来" in captured.err, "那句警告（读不出来）照旧要在 stderr 上"
    # 「已随之删除」那句与它的兄弟（带条数的那句）同在 stdout：这里是同一句话的**缺数据形态**。
    assert "条数读不出来" in captured.out and "下次导出会回来" in captured.out


# ---------- T6：降级说明的出口（spec §7） ----------

def test_recent_renders_degradation_to_stderr_not_stdout(env, monkeypatch, capsys):
    """降级是**成功但有保留**——它不该污染 stdout（那是给人看的表，也是管道里
    机器可读的部分），所以走 stderr（spec §7）。"""
    from agent_wechat import usecases
    monkeypatch.setattr(usecases, "collect_degradations",
                        lambda r: (usecases.Degradation(
                            "roster", "contact/contact.db", "chat_room", (),
                            "显示名暂不可用"),))
    with pytest.raises(SystemExit) as ei:
        cli.main(["recent", FRIEND, "-n", "3", "--account", ACCT])
    assert ei.value.code == 0
    out, err = capsys.readouterr()
    assert "显示名暂不可用" in err
    assert "显示名暂不可用" not in out


def test_search_renders_roster_degradation_to_stderr_not_stdout(env, tmp_path,
                                                                monkeypatch, capsys):
    """Ruling 13：search 的 hits 靠名册取 `sender_name`（`search.py:13`），而
    `_refresh_roster` 吞掉漂移——显示名**静默**回落成 wxid，用户无从知道。所以这条路
    也要与 recent 同一个出口：stderr 一行说明，stdout 不含它。走真实链路（漂移的
    contact.db：`chat_room` 没了、检索本身照常）。"""
    from agent_wechat import paths
    root = tmp_path / "wx"
    drift_tree(root, ACCT, drop_table="chat_room")
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)
    assert cli.cmd_search(SimpleNamespace(account=ACCT, contact=None,
                                          query="会议记录", n=5)) == 0
    out, err = capsys.readouterr()
    assert out == "mode=fts total=0\n"          # 空索引：stdout 只有表头这一行
    assert "显示名" in err


def test_semantic_renders_degradation_to_stderr_not_stdout(monkeypatch, capsys, tmp_path):
    """semantic 的 hits 同样靠名册（`display` 由 `display_name` 富化，Ruling 13）。
    这条走**用例层**：漂移挂在 reader 上（T4 记的形状），`usecases.semantic_search`
    翻成信封的 degraded，CLI 把它印到 stderr——接线与渲染一起钉住。"""
    from agent_wechat import paths, store

    class _DriftedReader:
        def __init__(self, acct):
            self._drift = [errors.SchemaDrift("roster", "contact/contact.db", "chat_room")]

        def roster(self):
            return []                    # 名册不可用 → 空 → 不写库（T5 的守卫）

    monkeypatch.setattr(account, "resolve_account", lambda a: "acct")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "index.db")   # _refresh_roster 会开库
    # HOME_DIR 也要隔离：store.connect → paths.ensure_home_dirs() 否则会对真实
    # ~/.agent-wechat 做 mkdir 并跑一次 tmutil addexclusion
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path)
    monkeypatch.setattr(reader, "MessageReader", _DriftedReader)
    monkeypatch.setattr(vector, "semantic_search", lambda *a, **k: {
        "mode": "semantic", "total": 0, "hits": []})
    assert cli.cmd_semantic(SimpleNamespace(account=None, contact=None,
                                            query="爬山", n=5)) == 0
    out, err = capsys.readouterr()
    assert out == "mode=semantic total=0\n"
    assert "显示名" in err             # 策略表的文案（不是表名），与 recent 同一出口


def test_export_renders_username_pool_degradation_to_stderr(env, tmp_path, monkeypatch, capsys):
    """`username_pool` 是**唯一按设计降级**的能力（spec §6），而 export 是它唯一的
    消费者——导出只返回行数，漂移以前被丢掉：索引里静默躺着 `md5:<hex>` 会话，之后按
    群名 search 返回 0 且**无任何提示**。更糟的是它把一个原本响亮的失败（裸
    OperationalError → 导出失败）变成了「成功」。降级说明走 stderr、不进 stdout——那是
    给人看的表，也是管道里机器可读的部分（spec §7）。"""
    from agent_wechat import paths
    root = tmp_path / "wx"
    ds = root / ACCT / "db_storage"
    # session 源漂移：SessionTable 少了 username 列（微信改列比删表常见）。
    # 导出本身照常跑——消息在分片里，与 session 无关。
    enc_db(ds / "session" / "session.db", {
        "SessionTable": ("CREATE TABLE SessionTable (user_name TEXT)", [(FRIEND,)])})
    build_shard(root, ACCT, "message_0.db", {FRIEND: [(1, 1, 1, T0, "你好", 0)]}, [FRIEND])
    monkeypatch.setattr(paths, "XWECHAT_FILES", root)

    assert cli.cmd_export(SimpleNamespace(account=ACCT, contact=None, full=False)) == 0
    out, err = capsys.readouterr()
    assert out == ("  已写入 1 行…\n"
                   f"完成：本轮 1 行；索引总量 {{'messages': 1, 'chats': 1, 'last_ts': {T0}}}\n")
    assert "部分会话的显示名无法反查" in err      # 策略表的文案，用户不需要知道表名
    assert "SessionTable" in err                 # Degradation 形状带得出诊断


# ---------- media export / media rebuild-refs（CLI 渲染层；用例层测试在 test_usecases.py） ----------

def test_media_export_needs_confirm_on_ambiguous_contact(monkeypatch, capsys):
    """多候选 → stdout 出候选 JSON、退出码 1（与 recent/search 同形）。

    media_export 打桩：CLI 渲染层测试不打真库。"""
    monkeypatch.setattr(usecases, "media_export", lambda *a, **k: usecases.NeedConfirm(
        [usecases.Candidate("wxid_a", "张三01", "friend"),
         usecases.Candidate("wxid_b", "张三02", "friend")]))
    with pytest.raises(SystemExit) as ei:
        cli.main(["media", "export", "--chat", "张三"])
    assert ei.value.code == 1
    out, err = capsys.readouterr()
    cands = json.loads(out)              # stdout 可被脚本直接 json 解析（_need_confirm 契约）
    assert [c["username"] for c in cands] == ["wxid_a", "wxid_b"]
    assert cands[0] == {"username": "wxid_a", "display": "张三01", "kind": "friend"}
    assert "无法唯一解析「张三」" in err


def test_media_export_renders_counts_and_degraded_to_stderr(monkeypatch, capsys):
    """ok/failed 计数到 stdout；degraded 行到 stderr（spec §7）。

    用例层打桩返回合成 MediaExportResult：CLI 测试不跑真实导出（不碰微信树）。
    顺带钉住 CLI → 用例层的参数编组（--kinds/--months 拆列表、--limit/--out 透传）。"""
    seen: dict = {}

    def fake_media_export(contact, **kw):
        seen.update(kw, contact=contact)
        return usecases.MediaExportResult(
            chat_id="wxid_target", out_dir="/tmp/out/media",
            report=media.MediaReport(
                # remaining 必须非 0：truncated 只在本会导出的条目**真被上限挡下**时才置位，
                # 夹具里 `truncated=True` + `remaining=0` 是生产不可能出现的组合，留着就等于
                # 把「还有 0 个未导出」这种荒唐话钉成期望输出。skipped 同理：图片给 5、
                # 语音给 1（这两行足够让「写死 skipped=0」在断言上变红；视频行留默认 0），
                # 否则新键只被断言了「存在于格式里」而没被断言「真的印出来」。
                image=media.Counts(ok=3, failed=1, skipped=5, remaining=7),
                voice=media.Counts(ok=2, raw_only=1, skipped=1, remaining=2),
                video=media.Counts(ok=1, thumb=2, missing=1, remaining=1),
                # 贴纸行同样给**全非零**：不给就是只断言了「格式里有这一行」，
                # 而没断言「值真的透出来」（上一轮为此吃过一次）。
                sticker=media.Counts(ok=4, failed=2, skipped=6, missing=3, remaining=1),
                degraded=["pilk 不可用：语音导出为原始 .slk"],
                truncated=True))

    monkeypatch.setattr(usecases, "media_export", fake_media_export)
    with pytest.raises(SystemExit) as ei:
        cli.main(["media", "export", "--chat", "张三", "--out", "/tmp/out"])
    assert ei.value.code == 0
    assert seen == {"contact": "张三", "kinds": ["image", "voice", "video"],
                    "months": None, "out_dir": "/tmp/out", "limit": 2000,
                    "force": False, "account": None, "with_cdn": False}
    out, err = capsys.readouterr()
    assert out == ("会话 wxid_target\n"
                   "图片   ok=3  skipped=5  failed=1  missing=0\n"
                   "语音   ok=2  skipped=1  failed=0  raw_only=1\n"
                   "视频   ok=1  skipped=0  thumb=2  missing=1\n"
                   "贴纸   ok=4  skipped=6  failed=2  missing=3\n"
                   "（达到 --limit 上限，还有 11 个未导出 —— 重跑同一条命令即可继续）\n"
                   "输出目录: /tmp/out/media\n")
    assert err == "⚠️  pilk 不可用：语音导出为原始 .slk\n"


def test_media_export_force_flag_is_forwarded(monkeypatch):
    """`--force` 必须一路透到用例层。

    变异测试实测：把 `cmd_media_export` 里的 `force=args.force` 写死成 `force=False`，
    既有那条（走默认值）仍然全绿——开关是不是个摆设，只有这条能看出来。
    """
    seen: dict = {}

    def fake_media_export(contact, **kw):
        seen.update(kw, contact=contact)
        return usecases.MediaExportResult(chat_id="wxid_target", out_dir="/tmp/out/media",
                                          report=media.MediaReport())

    monkeypatch.setattr(usecases, "media_export", fake_media_export)
    with pytest.raises(SystemExit) as ei:
        cli.main(["media", "export", "--chat", "张三", "--force"])
    assert ei.value.code == 0
    assert seen["force"] is True


def test_media_rebuild_refs_prints_total(monkeypatch, capsys):
    """进度行与总数行都到 stdout（media_rebuild_refs 打桩，不打真库）。"""

    def fake_rebuild(account=None, progress=None, on_skipped=None):
        if progress:
            progress(20_000)
        return 20_042

    monkeypatch.setattr(usecases, "media_rebuild_refs", fake_rebuild)
    with pytest.raises(SystemExit) as ei:
        cli.main(["media", "rebuild-refs"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "  已回填 20000 条媒体引用…" in out
    assert "完成：media_refs 共 20042 条" in out


def test_media_export_rejects_unknown_kind_with_search_input_error(captured, capsys):
    """kinds 非法 → SearchInputError 经 main() 既有 AgentWeChatError 翻译：单行中文到
    stderr、退出码 1。这条走**真实** usecases.media_export（captured 桩掉账号与
    reader）——非法 kinds 必须在碰任何导出逻辑之前被拒。

    **本轮订正**：原先拿 `image,sticker` 当非法值，改用拼错的 `stikers`——免得这条
    「非法值要被拒」和「贴纸本轮是不是可导出 kind」缠在一起（后者由下一条专门钉）。
    """
    with pytest.raises(SystemExit) as ei:
        cli.main(["media", "export", "--chat", "张三", "--kinds", "image,stikers"])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "未知的媒体类型" in err and "stikers" in err
    assert "Traceback" not in err


def test_media_export_accepts_sticker_kind(captured, capsys):
    """**导出恢复后贴纸是可导出 kind**：`--kinds sticker` 不再被拒，走完流程并如实报 0 条。

    这条是 `test_media_export_still_rejects_sticker_kind` 的**反面**——那条是导出挂起期间
    的守卫（`sticker` 在非法值之列）。T13 把 `MEDIA_KINDS` 收编 sticker，那条守卫必红，
    故就地换成这一条。**判别法**：把 `sticker` 从 `MEDIA_KINDS` 拿掉，这条立刻变红。

    夹具是空索引（`captured` 桩掉账号与 reader），所以 `_export_stickers` 走「无引用」
    那条降级路径：**不联网**、不落盘、退出码 0。
    """
    assert "sticker" in usecases.MEDIA_KINDS
    with pytest.raises(SystemExit) as ei:
        cli.main(["media", "export", "--chat", "张三", "--kinds", "sticker"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "贴纸" in out               # 计数行渲染出来了，不是走到校验就退出


def test_media_export_help_declares_cdn_credential_egress(capsys):
    """`--with-cdn` 必须在帮助里**讲明它会把凭据发出去**。

    这不是文案洁癖：`SECURITY.md` 承诺「不加则一次请求都不发」，而 CLI 帮助是用户
    唯一会读到的现场提示。藏着一个会外发凭据的开关，等于让那份承诺实际失效。
    """
    with pytest.raises(SystemExit) as ei:
        cli.main(["media", "export", "--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "--with-cdn" in out
    assert "filekey" in out or "凭据" in out


def test_media_export_rejects_bad_months_with_search_input_error(captured, capsys):
    """`--months 2026-9` → 单行报错 + 退出码 1，而不是静默 0 命中、exit 0。"""
    with pytest.raises(SystemExit) as ei:
        cli.main(["media", "export", "--chat", "张三", "--months", "2026-9"])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "月份格式应为 YYYY-MM" in err and "2026-9" in err
    assert "Traceback" not in err


def test_media_export_unwritable_out_dir_is_one_line(captured, tmp_path, monkeypatch, capsys):
    """`--out` 建不出来（只读目录/磁盘满）→ MediaOutputError 经既有翻译成单行，不是 traceback。"""
    bad = tmp_path / "ro" / "media"
    real_mkdir = pathlib.Path.mkdir

    def fake(self, *a, **kw):
        if self == bad:
            raise PermissionError(13, "Operation not permitted", str(self))
        return real_mkdir(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "mkdir", fake)
    with pytest.raises(SystemExit) as ei:
        cli.main(["media", "export", "--chat", "张三", "--out", str(bad)])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "输出目录不可用" in err and str(bad) in err
    assert "Traceback" not in err


# ---------- T8：media locate（CLI 渲染层；用例层测试在 test_usecases_media_link.py） ----------

def _media_states(*pairs):
    """(kind, status) → MediaRefState 列表。msg_key 只求唯一，不参与渲染。"""
    return [usecases.MediaRefState(f"k{i}", i, kind, status, None)
            for i, (kind, status) in enumerate(pairs)]


def _media_result(states=(), counts=None, *, by_kind=None, truncated=False, degraded=()):
    # by_kind 由用例层从 states 派生（推导的测试在 test_usecases_media_link.py），所以
    # 渲染层的夹具必须自己给：不给就是空，按类型那几行会静默消失。
    return usecases.MessageMediaResult(
        chat_id="wxid_x", out_dir="/tmp/m", states=list(states),
        counts=counts or {"none": 0, "exported": 0, "exportable": 0, "gone": 0},
        by_kind=by_kind or {}, truncated=truncated, degraded=tuple(degraded))


def test_media_locate_renders_states_and_counts(capsys, monkeypatch):
    """逐条 + 汇总都要印出来。替换用例层，不碰真实数据。"""
    from agent_wechat import usecases

    def _fake(contact, **kw):
        return usecases.MessageMediaResult(
            chat_id="wxid_x", out_dir="/tmp/m",
            states=[usecases.MediaRefState(msg_key="a|b|c|1", ts=1, kind="image",
                                           status="exported", path="/tmp/m/x.jpg"),
                    usecases.MediaRefState(msg_key="a|b|c|2", ts=2, kind="voice",
                                           status="gone", path=None)],
            counts={"none": 0, "exported": 1, "exportable": 0, "gone": 1})
    monkeypatch.setattr(usecases, "message_media", _fake)
    rc = cli.cmd_media_locate(SimpleNamespace(chat="wxid_x", keys=None, months=None,
                                              limit=200, out=None, account=None))
    assert rc == 0
    out = capsys.readouterr().out
    assert "  [exported  ] image 1  /tmp/m/x.jpg" in out
    assert "  [gone      ] voice 2  " in out
    assert "汇总：exported=1 exportable=0 gone=1 none=0" in out
    # 提示的主因是「微信只为近期（尚未导出）的消息保留本体」（与 MCP 工具描述同一套措辞，
    # 两个入口文档承诺「同一套四态」）——只写「微信已清理本体」会把用户支去「修复」而不是
    # 去导历史。断言落在主因那句话上，退回旧措辞即红。
    assert "微信只为近期（尚未导出）的消息保留本体" in out


def test_media_locate_wires_args_and_defaults(monkeypatch, capsys):
    """argparse 编组：--keys/--months 逗号拆列表、--limit 默认 200、--out/--account 透传。

    直接调 `cmd_*` 的测试绕开了 argparse，参数名与默认值改错也测不出来（brief 的
    逐字输出测试就是那样），所以这里必须经 `cli.main` 走一遍真参数表。"""
    seen: dict = {}

    def fake(contact, **kw):
        seen.update(kw, contact=contact)
        return _media_result()

    monkeypatch.setattr(usecases, "message_media", fake)
    with pytest.raises(SystemExit) as ei:
        cli.main(["media", "locate", "--chat", "张三", "--keys", "a|b|c|1, a|b|c|2",
                  "--months", "2026-08,2026-09"])
    assert ei.value.code == 0
    assert seen == {"contact": "张三", "keys": ["a|b|c|1", "a|b|c|2"],
                    "months": ["2026-08", "2026-09"], "limit": 200,
                    "out_dir": None, "account": None}


def test_media_locate_breaks_counts_down_by_kind(monkeypatch, capsys):
    """按 kind 再拆一次（C2）：池化的四个数字会把「语音几乎全可导出」与「图片视频几乎全
    gone」加成一个数，读起来像「这个会话的媒体大多没了」，拆开才是实情。

    夹具**必须含两种以上 kind 且状态不同**：只有一种 kind 时拆与不拆印出来一样，这里的
    断言会恒真。`none` 态没有 kind，只该进「汇总」。"""
    states = _media_states(("voice", "exportable"), ("voice", "exportable"),
                           ("image", "gone"), ("video", "gone"), (None, "none"))
    counts = {"none": 1, "exported": 0, "exportable": 2, "gone": 2}
    by_kind = {"voice": {"exportable": 2}, "image": {"gone": 1}, "video": {"gone": 1}}
    monkeypatch.setattr(usecases, "message_media",
                        lambda contact, **kw: _media_result(states, counts, by_kind=by_kind))
    assert cli.cmd_media_locate(SimpleNamespace(chat="wxid_x", keys=None, months=None,
                                                limit=200, out=None, account=None)) == 0
    out = capsys.readouterr().out
    assert "汇总：exported=0 exportable=2 gone=2 none=1" in out      # 池化那行仍在：它是总数
    assert "  按类型 image exported=0 exportable=0 gone=1" in out
    assert "  按类型 video exported=0 exportable=0 gone=1" in out
    assert "  按类型 voice exported=0 exportable=2 gone=0" in out
    # 只有这三个媒体类型：顺序由固定表决定（夹具里 voice 排在最前，印出来仍在最后），
    # `none` 态不进这一节
    assert out.count("  按类型 ") == 3
    # 夹具里那条 none 态（kind=None）**必须被断言到**：否则它是一条对该测试恒定无效的条目
    # ——删掉它、或让它混进 kind 桶，上面三条断言照样绿（那是本仓库定义里的假护栏）。
    # 它证明的是「none 态有逐条明细（kind 印成 `-`），但没有 kind 桶」。
    assert "  [none      ] -     4  " in out


def test_media_locate_prints_truncation_notice_only_when_truncated(monkeypatch, capsys):
    """触顶要印（C1）：没有这行，「就这些」与「我在 --limit 处停了」分不出来，
    而计数只是答完的这些。未截断时不许有——正常路径不该多一行噪音。"""
    args = SimpleNamespace(chat="wxid_x", keys=None, months=None, limit=1,
                           out=None, account=None)
    gone = _media_states(("image", "gone"))
    counts = {"none": 0, "exported": 0, "exportable": 0, "gone": 1}
    monkeypatch.setattr(usecases, "message_media",
                        lambda contact, **kw: _media_result(gone, counts, truncated=True))
    assert cli.cmd_media_locate(args) == 0
    assert "达到 --limit 上限，已提前收手" in capsys.readouterr().out

    monkeypatch.setattr(usecases, "message_media",
                        lambda contact, **kw: _media_result(gone, counts))
    assert cli.cmd_media_locate(args) == 0
    assert "达到 --limit 上限" not in capsys.readouterr().out


def test_media_locate_shows_sticker_bucket_and_qualifies_its_gone(monkeypatch, capsys):
    """贴纸进了 `by_kind` → 按类型那一节必须印它；它那句 `gone` 提示必须另说。

    两件都钉在这条上，因为两处都是**写死的枚举**：
    1. `cmd_media_locate` 的按类型循环用固定元组 `("image", "video", "voice")`——
       漏掉 sticker 时该桶在 CLI 里静默消失（而 MCP 信封有，两个入口的文档承诺
       「同一套 by_kind」）。删掉元组里的 "sticker" 这条必红。
    2. 上面那句「gone = 消息引用了媒体但本机找不到，导出更多历史比修复有用」**对贴纸
       是句错话**：贴纸取不出来是微信侧存成加密格式、方案未破，与「本机有没有」无关
       （`chat_text.STICKER_GONE` 就是为这句话设的）。没有贴纸时不许印那句——否则它是
       恒定的噪音，也证明不了它「针对贴纸」。
    """
    args = SimpleNamespace(chat="wxid_x", keys=None, months=None, limit=200,
                           out=None, account=None)
    states = _media_states(("image", "gone"), ("sticker", "gone"))
    counts = {"none": 0, "exported": 0, "exportable": 0, "gone": 2}
    by_kind = {"image": {"gone": 1}, "sticker": {"gone": 1}}
    monkeypatch.setattr(usecases, "message_media",
                        lambda contact, **kw: _media_result(states, counts, by_kind=by_kind))
    assert cli.cmd_media_locate(args) == 0
    out = capsys.readouterr().out
    assert "  按类型 sticker exported=0 exportable=0 gone=1" in out
    assert "取不出来" in out            # 贴纸专用那句：通用那句对它不成立
    assert "sticker" in out

    # 没有贴纸的会话：通用那句照旧，贴纸专用那句不出现在这里
    monkeypatch.setattr(usecases, "message_media", lambda contact, **kw: _media_result(
        _media_states(("image", "gone")),
        {"none": 0, "exported": 0, "exportable": 0, "gone": 1},
        by_kind={"image": {"gone": 1}}))
    assert cli.cmd_media_locate(args) == 0
    plain = capsys.readouterr().out
    assert "微信只为近期（尚未导出）的消息保留本体" in plain
    assert "按类型 sticker" not in plain
    assert "取不出来" not in plain


def test_media_locate_prints_absolute_paths_for_relative_out(tmp_path, monkeypatch, capsys,
                                                             idx):
    """`--out media`（相对路径）→ 打印出来的产物根与产物路径必须**绝对**（C3）：CLI 与 MCP
    的消费方都用不了相对路径，且它是 `cd` 之后才解析的——同一份产物在两次运行里会是两个串。

    这条走**真实** usecases.message_media（只在 `_ctx`/`_con` 处注入夹具的索引）：替身那条
    路上 out_dir 由替身自己编，绝对化是替身的性质，测不出这里。"""
    from agent_wechat import paths
    con, rows = idx
    real = usecases.message_media
    monkeypatch.setattr(usecases, "message_media",
                        lambda contact, **kw: real(contact, _ctx=_FakeCtx(), _con=con, **kw))
    monkeypatch.setattr(paths, "XWECHAT_FILES", tmp_path / "wx")   # 空微信树：不碰真实数据
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "media" / CM / "image" / MON
    d.mkdir(parents=True)
    (d / ("c" * 32 + ".jpg")).write_bytes(b"x")                   # rows[3] 的产物已落盘
    with pytest.raises(SystemExit) as ei:
        cli.main(["media", "locate", "--chat", "wxid_x", "--keys", rows[3]["msg_key"],
                  "--out", "media"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert f"产物根 {(tmp_path / 'media').resolve()}" in out
    assert f"  [exported  ] image {TS}  {(d / ('c' * 32 + '.jpg')).resolve()}" in out


def test_media_locate_rejects_bad_months_with_search_input_error(captured, tmp_path,
                                                                monkeypatch, capsys):
    """`--months 2026-9` → SearchInputError 经 main() 既有翻译：单行中文 + 退出码 1，
    不是静默 0 命中、exit 0。校验在用例层，CLI 不另写一套（这条走**真实**
    usecases.message_media，`captured` 只桩掉账号与 reader）。

    索引库必须隔离：它今天安全**只是因为**月份校验排在 `store.open()` 之前——把那道校验
    删掉或挪后（正是本用例要抓的变异），真实 `~/.agent-wechat/index.db` 就会被打开并跑一遍
    DDL。夹具不许依赖「被测代码的执行顺序恰好没走到那一步」。
    """
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "index.db")
    with pytest.raises(SystemExit) as ei:
        cli.main(["media", "locate", "--chat", "张三", "--months", "2026-9"])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "月份格式应为 YYYY-MM" in err and "2026-9" in err
    assert "Traceback" not in err


def test_media_locate_renders_degraded_to_stderr(monkeypatch, capsys):
    """降级说明走 stderr（spec §7）：成功但有保留，不该污染 stdout——那里是逐条明细与
    计数，也是管道里机器可读的部分。"""
    monkeypatch.setattr(usecases, "message_media", lambda contact, **kw: _media_result(
        _media_states(("voice", "gone")),
        {"none": 0, "exported": 0, "exportable": 0, "gone": 1},
        degraded=(usecases.Degradation("msg_media", "message/media_0.db", "VoiceInfo", (),
                                       "语音库读不到：尚未导出的语音消息一律按「本体不在本机」"
                                       "呈现（已导出的不受影响）"),)))
    assert cli.cmd_media_locate(SimpleNamespace(chat="wxid_x", keys=None, months=None,
                                                limit=200, out=None, account=None)) == 0
    out, err = capsys.readouterr()
    assert "语音库读不到" in err and "VoiceInfo" in err
    assert "语音库读不到" not in out


def test_media_locate_needs_confirm_on_ambiguous_contact(monkeypatch, capsys):
    """多候选 → stdout 出候选 JSON、退出码 1（与 media export 同形，不猜）。"""
    monkeypatch.setattr(usecases, "message_media", lambda *a, **k: usecases.NeedConfirm(
        [usecases.Candidate("wxid_a", "张三01", "friend"),
         usecases.Candidate("wxid_b", "张三02", "friend")]))
    with pytest.raises(SystemExit) as ei:
        cli.main(["media", "locate", "--chat", "张三"])
    assert ei.value.code == 1
    out, err = capsys.readouterr()
    assert [c["username"] for c in json.loads(out)] == ["wxid_a", "wxid_b"]
    assert "无法唯一解析「张三」" in err


def test_cli_query_commands_do_not_print_msg_key(monkeypatch, capsys):
    """`--keys` 的 msg_key **只来自 MCP 信封**（USAGE.md §7 写的就是这句），CLI 自己的
    `recent` / `search` / `semantic` 不打印它。

    这条钉住 USAGE 那句能力宣称：给 CLI 任何一行加上 msg_key，那句「CLI 不打印」就变成假话。
    （行格式本身是刻意冻结的契约，本测试不锁整行，只锁「key 不出现在输出里」。）
    """
    key = "acct|" + "d" * 32 + "|message_0.db|1"
    msg = reader.Message(chat_id="wxid_x", shard="message_0.db", local_id=1, ts=1,
                         msg_type=1, type_name="text", sender_id="wxid_x",
                         sender_name="张三", is_self=False, content="你好",
                         media_hint=None, msg_key=key)
    monkeypatch.setattr(usecases, "recent_messages",
                        lambda *a, **k: usecases.RecentMessages("wxid_x", "张三", [msg]))
    assert cli.cmd_recent(SimpleNamespace(account=None, contact="张三", n=5)) == 0
    out = capsys.readouterr().out
    assert "你好" in out and key not in out and "msg_key" not in out

    hit = {"msg_key": key, "ts": 1, "chat_id": "wxid_x", "is_self": 0,
           "sender_id": "wxid_x", "sender_name": "张三", "content": "你好"}
    monkeypatch.setattr(usecases, "search_history",
                        lambda *a, **k: {"mode": "fts", "total": 1, "hits": [hit],
                                         "degraded": []})
    assert cli.cmd_search(SimpleNamespace(query="你好", contact=None, n=5,
                                          account=None)) == 0
    out = capsys.readouterr().out
    assert "你好" in out and key not in out and "msg_key" not in out

    monkeypatch.setattr(usecases, "semantic_search",
                        lambda *a, **k: {"mode": "semantic", "total": 1, "degraded": [],
                                         "hits": [{**hit, "sender": "张三",
                                                   "distance": 0.1}]})
    assert cli.cmd_semantic(SimpleNamespace(query="你好", contact=None, n=5,
                                            account=None)) == 0
    out = capsys.readouterr().out
    assert "你好" in out and key not in out and "msg_key" not in out


# ---------- `purge --chat` / `--undo` / `--excluded`（Task 7） ----------
#
# 这是用户唯一直接接触的一层：销毁不可逆，所以「删的是谁」「删掉了什么」「没删掉什么」
# 三件事都必须出现在 stdout 上，且有测试盯着它们不被顺手删掉。

def _chat_row(n: int = 1, chat_id: str = "victim", account: str = "a") -> dict:
    """一条 messages 行；`chat_md5` 与 `chat_id` **自洽**。

    不自洽的夹具（`chat_id="victim"` 配 `chat_md5="aaaa"`）会让「导出目录对不对」这类断言
    变瞎——`tests/test_purge.py` 开篇记过这笔账。
    """
    from agent_wechat import messages
    md5 = messages.chat_md5(chat_id)
    return {"msg_key": f"{account}|{md5}|message_0.db|{n}", "account": account,
            "chat_md5": md5, "chat_id": chat_id, "shard": "message_0.db", "local_id": n,
            "server_id": n, "ts": 100, "msg_type": 1, "sender_id": None,
            "sender_name": None, "is_self": 0, "content": "要删的", "media_hint": None}


def _stub_resolution(monkeypatch, chat_id: str) -> None:
    """只换掉**解析那一半**（`ChatContext` 会去开真实微信库与联系人缓存）。

    `purge_chat_targets` 本身不换：这些用例要跑的正是「哪些账号该进 targets」这个判据。
    """
    class _StubCtx:
        def __init__(self, account=None):
            self.account = account

        def resolve_optional(self, contact):
            return chat_id

    monkeypatch.setattr(usecases, "ChatContext", _StubCtx)


def test_purge_chat_is_mutually_exclusive_with_scopes(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    for argv in (["purge", "--chat", "小李", "--media"],
                 ["purge", "--chat", "小李", "--all"]):
        with pytest.raises(SystemExit) as ei:
            cli.main(argv)
        assert ei.value.code == 2, f"{argv} 应当退 2"


def test_purge_chat_dry_run_shows_the_resolved_identity_and_the_moments_warning(
        tmp_path, monkeypatch, capsys):
    """计划表第一行必须印**解析真身**（这个项目栽过一次：61% 的会话按真名解析会静默落到裸 id），
    并且必须说清朋友圈不在范围内——否则用户以为清干净了。"""
    home = _isolate(tmp_path, monkeypatch)
    from agent_wechat import exclusions
    with store.open() as con:
        store.upsert_batch(con, [{
            "msg_key": "a|aaaa|message_0.db|1", "account": "a", "chat_md5": "aaaa",
            "chat_id": "victim", "shard": "message_0.db", "local_id": 1, "server_id": 1,
            "ts": 100, "msg_type": 1, "sender_id": None, "sender_name": None,
            "is_self": 0, "content": "要删的", "media_hint": None}])
    monkeypatch.setattr(usecases, "purge_chat_targets",
                        lambda *a, **kw: [{"account": "a", "chat_id": "victim",
                                           "chat_md5": "aaaa"}])
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--chat", "小李"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "victim" in out, "必须印出解析到的会话真身"
    assert "朋友圈" in out and "仍可能留在其中" in out
    # 判据是「不删任何东西、不改索引内容」——**不是「磁盘零变化」**（USAGE §7：要读索引的
    # 命令都会把 `exclusions.db` ATTACH 进来，本机还没有它会因此建出一个空文件）。
    # 墓碑也是磁盘状态：只查 index.db 的大小，一个会顺手写墓碑的实现照样能过。
    with store.open() as con:
        assert exclusions.count(con) == 0, "dry-run 不许写墓碑"
        assert store.chat_row_counts(con, "a", "victim")["messages"] == 1
    assert (home / "index.db").stat().st_size > 0        # 一个字节没删


def test_purge_undo_needs_the_chat_flag(tmp_path, monkeypatch, capsys):
    """`--undo` 只对会话有意义（评审 F2）。

    少了这一条，`purge --undo --media --yes` 会被当成一条普通的范围删除**跑掉**：整棵
    `media/` 没了（不可逆），而用户要的 `--undo` 毫无效果、也没有一个字提示。
    """
    _isolate(tmp_path, monkeypatch)
    for argv in (["purge", "--undo", "--media", "--yes"],
                 ["purge", "--undo", "--all", "--yes"],
                 ["purge", "--undo"]):
        with pytest.raises(SystemExit) as ei:
            cli.main(argv)
        assert ei.value.code == 2, f"{argv} 应当退 2（--undo 只与 --chat 同用）"


def test_purge_excluded_lists_by_chat_md5(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    from agent_wechat import exclusions
    with store.open() as con:
        exclusions.add(con, [("a|aaaa|message_0.db|1", "a", "aaaa"),
                             ("a|aaaa|message_0.db|2", "a", "aaaa")])
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--excluded"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "aaaa" in out and "2" in out


def test_purge_excluded_rejects_any_other_flag(tmp_path, monkeypatch, capsys):
    """spec §3 的字面要求：`--excluded` 与其它 flag 同给时退 2——**`--yes` 也是「其它
    flag」**（M-2）。带上它说明用户以为这条查询会动手；安静地忽略一个「执行」开关，等于
    让那个误解留着。"""
    _isolate(tmp_path, monkeypatch)
    for argv in (["purge", "--excluded", "--yes"],
                 ["purge", "--excluded", "--chat", "小李"],
                 ["purge", "--excluded", "--media"],
                 ["purge", "--excluded", "--all"]):
        with pytest.raises(SystemExit) as ei:
            cli.main(argv)
        assert ei.value.code == 2, f"{argv} 应当退 2"


def test_purge_undo_needs_yes(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    from agent_wechat import exclusions
    with store.open() as con:
        exclusions.add(con, [("a|aaaa|message_0.db|1", "a", "aaaa")])
    # 这一条 brief 里漏了桩（同族另外两条都有）。不桩的话 `purge_chat_targets` 会去解析
    # **真实**微信库里的「小李」，「不带 --yes 不许动」就退化成一条恒真的假护栏：真机上
    # targets 落到空列表、一段删除代码都没走到，那个 assert 自然成立。
    monkeypatch.setattr(usecases, "purge_chat_targets",
                        lambda *a, **kw: [{"account": "a", "chat_id": "victim",
                                           "chat_md5": "aaaa"}])
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--chat", "小李", "--undo"])
    assert ei.value.code == 0
    with store.open() as con:
        assert exclusions.count(con) == 1, "不带 --yes 不许动"


def test_purge_undo_yes_clears_the_tombstone_but_keeps_the_data_gone(
        tmp_path, monkeypatch, capsys):
    """**AC5 的后半，也是最容易漏的一半**：`--undo --yes` 之后清单里该会话的条目为 0，
    而**数据仍然不在**——撤销只放开拦截，重导才把消息带回来。

    少了后半句，这条命令的语义会被读成「撤销 = 恢复」——那正是它的输出里必须印死一句的原因。

    **夹具必须自洽**（`chat_id` ↔ `chat_md5` 成对，见 `_chat_row`）：不自洽时墓碑存的是
    messages 行里的列、而核查按**派生值**数（`purge.py` 的 `_write_tombstone` / `verify_chat`），
    于是真删那次会退 1——一条永远退 1 的实现也能过这条测试。
    """
    home = _isolate(tmp_path, monkeypatch)
    from agent_wechat import exclusions, messages
    md5 = messages.md5_of("victim")
    with store.open() as con:
        store.upsert_batch(con, [_chat_row(1)])
    monkeypatch.setattr(usecases, "purge_chat_targets",
                        lambda *a, **kw: [{"account": "a", "chat_id": "victim",
                                           "chat_md5": md5}])
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--chat", "小李", "--yes"])          # 先真删一次
    assert ei.value.code == 0, "删除成功就该退 0"
    with store.open() as con:
        assert exclusions.count(con) == 1
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 0

    capsys.readouterr()
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--chat", "小李", "--undo", "--yes"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "不会" in out and "export --full" in out, "必须写明撤销不恢复数据"
    with store.open() as con:
        assert exclusions.count(con) == 0, "撤销后清单里该会话应当为空"
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 0, \
            "撤销**不恢复数据**——消息要重跑 export --full 才回来"


def test_purge_chat_plan_counts_a_shared_export_dir_once(tmp_path, monkeypatch, capsys):
    """跨账号：两条 entry **共享同一个** `media/<md5>/`，体积只能算一遍。

    导出物那行是用户判断「删了能省多少」的唯一依据；逐条累加会让同一个目录算两次
    （1.2 MB 的目录印成 2.4 MB）。去重的口径与 `ChatResidue.export_dirs` 一致。
    """
    home = _isolate(tmp_path, monkeypatch)
    from agent_wechat import messages
    md5 = messages.md5_of("victim")
    d = home / "media" / md5
    d.mkdir(parents=True)
    (d / "chat.html").write_bytes(b"x" * 1_200_000)
    with store.open() as con:                    # 有索引的机器（索引库不在时走的是另一条提示）
        store.upsert_batch(con, [_chat_row(1)])
    monkeypatch.setattr(usecases, "purge_chat_targets", lambda *a, **kw: [
        {"account": "a", "chat_id": "victim", "chat_md5": md5},
        {"account": "b", "chat_id": "victim", "chat_md5": md5}])
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--chat", "小李"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert out.count(str(d)) == 1, "同一个导出目录只该占一行"
    assert "1.2 MB" in out and "2.4 MB" not in out, "共享目录的体积被算了两遍"


def test_purge_chat_reports_an_undeletable_export_dir_and_exits_1(tmp_path, monkeypatch, capsys):
    """`ChatReport.export_removed == False` 是「删了但没删掉」的**唯一**信号，
    必须被用户看见、并因此退 1——Task 6 的安全论证依赖这条通道存在。

    形状取**悬空符号链接**（裁决有意保留的那个不对称）：计划侧走 `exists()` 说「不存在」，
    而 execute 的 re-check 与 verify 走不跟随链接的判据 → False + 残留。
    """
    home = _isolate(tmp_path, monkeypatch)
    from agent_wechat import messages
    md5 = messages.md5_of("victim")
    media = home / "media"
    media.mkdir()
    link = media / md5
    link.symlink_to(media / "早就没有了")
    with store.open() as con:
        store.upsert_batch(con, [_chat_row(1)])
    monkeypatch.setattr(usecases, "purge_chat_targets", lambda *a, **kw: [
        {"account": "a", "chat_id": "victim", "chat_md5": md5}])
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--chat", "小李", "--yes"])
    assert ei.value.code == 1, "导出物没删掉就必须退 1"
    out = capsys.readouterr().out
    assert "导出物" in out and "未能删除" in out, "「删了但没删掉」必须在报告里说出来"
    assert str(link) in out, "核查那行要点名残留的是哪个目录"
    # 计划侧那个**有意保留的不对称**（裁决要的）：它走 `exists()`，问的是「有没有内容可删」，
    # 悬空链接答「没有」——所以计划表说「不存在」，而 execute 报失败、核查列残留。
    # 谁要是把 plan 侧也改成不跟随链接的判据，这行会红。
    assert "（不存在）" in out and "—" in out


def test_purge_chat_refuses_an_escaping_link_before_deleting_anything(
        tmp_path, monkeypatch, capsys):
    """`media/<md5>/` 树内指向**数据目录之外**的链接，必须在删任何东西之前拒绝（终审 I-2）。

    不拒的后果是 false-clean：`rmtree` 把链接当普通条目 unlink、**目标明文原封不动**，
    而运行报「导出物已删除」、核查也报干净——用户以为外面那份没了，它还在。
    判据与具名范围的目录型产物同源（`_tree_entries` + `_escapes_home`）。

    **判别性**：摘掉 `execute_chat` 开头那道闸门，这条当场红——删除会照常跑完
    （messages 归零、墓碑落库、导出目录消失），只剩树外那份文件还在。
    """
    home = _isolate(tmp_path, monkeypatch)
    from agent_wechat import exclusions, messages
    md5 = messages.chat_md5("victim")            # 与 `_chat_row` 自洽的那一个
    with store.open() as con:
        store.upsert_batch(con, [_chat_row(1)])
    export_dir = home / "media" / md5
    export_dir.mkdir(parents=True)
    (export_dir / "chat.html").write_text("导出物")
    outside = tmp_path / "outside"; outside.mkdir()
    secret = outside / "secret.txt"; secret.write_text("外面那份明文")
    # 链接存**相对目标**：`os.readlink` 原样报出来，消息里就看得见它指向哪里（绝对路径会长到
    # 被 `errors.redact` 那 300 字的上限截掉后半段——那是渲染层既有的长度上限，不是这里的判据）。
    (export_dir / "leak.txt").symlink_to("../../../outside/secret.txt")
    monkeypatch.setattr(usecases, "purge_chat_targets", lambda *a, **kw: [
        {"account": "a", "chat_id": "victim", "chat_md5": md5}])
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--chat", "小李", "--yes"])
    assert ei.value.code == 1, "拒绝就必须退 1，不许当成功"
    err = capsys.readouterr().err
    assert str(export_dir / "leak.txt") in err, "要点名是哪个链接"
    assert "../../../outside/secret.txt" in err, "还要说清它指向哪里"
    # 判据：拒绝时 messages / exclusions / 盘上的导出物**全部零变化**
    with store.open() as con:
        assert exclusions.count(con) == 0, "拒绝时不许写墓碑"
        assert store.chat_row_counts(con, "a", "victim")["messages"] == 1, "拒绝时不许删消息"
    assert (export_dir / "chat.html").read_text() == "导出物"
    assert secret.read_text() == "外面那份明文"


def test_purge_chat_exits_1_even_when_the_check_says_clean(tmp_path, monkeypatch, capsys):
    """报告侧那一句**自己**也是退出码的判据（注入式判据，不是真夹具）。

    今天 `export_removed == False` 必然伴随核查侧的残留，所以「只信核查」的写法也能过——
    但那样「删了但没删掉」就只剩**一条**通道兜着，而那条通道（不跟随链接的判据）恰好是
    本特性反复修过的地方。把核查换成一份干净的桩，报告侧那一句就成了唯一的判据：
    摘掉 `all(r.export_removed ...)`，这条必红。
    """
    home = _isolate(tmp_path, monkeypatch)
    from agent_wechat import messages, purge
    md5 = messages.md5_of("victim")
    media = home / "media"
    media.mkdir()
    (media / md5).symlink_to(media / "早就没有了")
    with store.open() as con:
        store.upsert_batch(con, [_chat_row(1)])
    monkeypatch.setattr(usecases, "purge_chat_targets", lambda *a, **kw: [
        {"account": "a", "chat_id": "victim", "chat_md5": md5}])
    monkeypatch.setattr(purge, "verify_chat",
                        lambda plan: purge.ChatResidue((), 1, 1, ()))
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--chat", "小李", "--yes"])
    assert ei.value.code == 1, "报告说导出物没删掉，就不能退 0"
    assert "未能删除" in capsys.readouterr().out


def test_purge_excluded_and_undo_do_not_recreate_the_index(tmp_path, monkeypatch, capsys):
    """这两条命令是**紧跟销毁之后**跑的那两条，而 `store.open()` 会把刚删掉的 index.db
    连同整个 schema 一起建回来（`_exclusion_count` 的开篇记的就是这笔账）。

    `--excluded` 的用途之一正是「`purge --index` 之后看看清单还剩多少」；`--undo` 同理
    ——它只碰 `exclusions.db`，跟索引库没有关系。

    **只桩解析层**（`ChatContext`），不桩 `purge_chat_targets`：桩掉它这条断言就成了假的
    ——生产路径上第一个 `store.open()` 就在那个函数里（评审 F1），桩掉之后守卫测不到它。
    """
    home = _isolate(tmp_path, monkeypatch)
    from agent_wechat import exclusions, messages
    md5 = messages.md5_of("victim")
    with store.open() as con:
        exclusions.add(con, [(f"a|{md5}|message_0.db|1", "a", md5)])
    _stub_resolution(monkeypatch, "victim")
    for argv in (["purge", "--excluded"], ["purge", "--chat", "小李", "--undo"]):
        (home / "index.db").unlink(missing_ok=True)   # 模拟 purge --index（第二轮它已被证明不在）
        with pytest.raises(SystemExit) as ei:
            cli.main(argv)
        assert ei.value.code == 0, argv
        assert not (home / "index.db").exists(), f"{argv} 把刚删掉的索引建回来了"
    with store.open() as con:            # 不带 --yes：清单一个字节都没动（两轮都没动）
        assert exclusions.count(con) == 1


def test_purge_chat_without_an_index_does_not_rebuild_it(tmp_path, monkeypatch, capsys):
    """删除那条路也不许把索引库建回来（F1 那个洞的另一半）。

    索引库不在时目标仍可能非空（只剩墓碑的那些账号），而 `plan_chat` 一进门就 `store.open()`
    ——刚被 `purge --index` 删掉的索引会当场长回来。活消息一条都没有：没有东西可删，
    说清楚，并指向 `--undo`。
    """
    home = _isolate(tmp_path, monkeypatch)
    from agent_wechat import exclusions, messages
    md5 = messages.md5_of("victim")
    with store.open() as con:
        exclusions.add(con, [(f"a|{md5}|message_0.db|1", "a", md5)])
    (home / "index.db").unlink()                   # 模拟 purge --index
    _stub_resolution(monkeypatch, "victim")
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--chat", "小李", "--yes"])
    assert ei.value.code == 0
    assert not (home / "index.db").exists(), "删除路线把刚删掉的索引建回来了"
    err = capsys.readouterr().err
    assert "索引库不在" in err and "--undo" in err
    with exclusions.standalone() as con:
        assert exclusions.count(con) == 1, "什么都没删（活消息本来就没有）"


def test_purge_chat_without_an_index_names_the_leftover_export(tmp_path, monkeypatch, capsys):
    """索引库不在那条路上，该会话的导出目录**仍在盘上**时要点名（M-3）。

    不说这句，用户会把「没有活消息可删」读成「这个会话在本工具里什么都不剩了」——而那份
    解密后的图/视频/语音（外加双击就能打开的 HTML）还摊在盘上，且本命令这一支一个字节
    都不会动它。判据走 `purge._name_there`（不跟随符号链接）：这里用**悬空符号链接**钉住
    这一点——`exists()` 会说「不在」，而那个名字确实还在。
    """
    home = _isolate(tmp_path, monkeypatch)
    from agent_wechat import exclusions, messages
    md5 = messages.chat_md5("victim")
    with store.open() as con:
        exclusions.add(con, [(f"a|{md5}|message_0.db|1", "a", md5)])
    (home / "index.db").unlink()                   # 模拟 purge --index
    media = home / "media"; media.mkdir()
    (media / md5).symlink_to(media / "早就没有了")  # 悬空：名字还在，内容不在
    _stub_resolution(monkeypatch, "victim")
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--chat", "小李", "--yes"])
    assert ei.value.code == 0
    err = capsys.readouterr().err
    assert "索引库不在" in err and "--undo" in err
    assert "导出物仍在盘上" in err, f"盘上那份导出物必须点名：{err!r}"
    assert str(media / md5) in err
    assert not (home / "index.db").exists(), "删除路线不许把刚删掉的索引建回来"
    with exclusions.standalone() as con:
        assert exclusions.count(con) == 1, "这一支什么都不改"


def test_purge_chat_undo_works_with_no_index_db_at_all(tmp_path, monkeypatch, capsys):
    """**撤销必须能在索引库完全不存在时跑通**（评审 F1 的判别条）。

    `--undo` 的典型用法就是 `purge --chat X --yes` 之后反悔，而用户还可能顺手跑过
    `purge --index` 去回收空间（本命令自己的汇总就在推荐它）。若这条路线里任何一个
    `store.open()` 走到，就会冒出一个全 schema 的 index.db——**静默撤销那次索引 purge**。
    所以：索引库不在时，目标只从排除清单出，一个字节都不建。
    """
    home = _isolate(tmp_path, monkeypatch)
    from agent_wechat import exclusions, messages
    md5 = messages.md5_of("victim")
    with store.open() as con:                      # 先建索引写墓碑，再把它删掉
        exclusions.add(con, [(f"a|{md5}|message_0.db|1", "a", md5)])
    (home / "index.db").unlink()
    _stub_resolution(monkeypatch, "victim")        # 不桩 purge_chat_targets
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--chat", "小李", "--undo", "--yes"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "已解除 1 条排除" in out
    assert not (home / "index.db").exists(), "撤销把刚删掉的索引建回来了"
    with exclusions.standalone() as con:           # 读清单**也不能**把索引建回来
        assert exclusions.count(con) == 0


def test_purge_chat_undo_reaches_a_chat_whose_messages_are_already_gone(
        tmp_path, monkeypatch, capsys):
    """**撤销的落地形态**：`purge` 之后索引里一条消息都没有，而 `--undo` 必须还能解除排除。

    目标集合若只按 `messages` 找会话，撤销会在这里报「没有可删的东西」并退 0——用户以为
    撤销成功，重跑 `export --full` 却什么也回不来（墓碑还在），而输出里没有一个字说破。
    （spec §5.6：撤销的存在理由就是「一次误删之后」——那时消息必然已经不在索引里。）
    """
    _isolate(tmp_path, monkeypatch)
    from agent_wechat import exclusions, messages
    md5 = messages.md5_of("victim")
    with store.open() as con:
        exclusions.add(con, [(f"a|{md5}|message_0.db|1", "a", md5)])
    _stub_resolution(monkeypatch, "victim")           # 不桩 purge_chat_targets
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--chat", "小李", "--undo", "--yes"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "已解除 1 条排除" in out
    with store.open() as con:
        assert exclusions.count(con) == 0, "消息没了也要能撤销"


def test_purge_chat_targets_derives_the_md5_of_a_degraded_chat_id(tmp_path, monkeypatch):
    """退化会话名（`md5:<hex>`）**不再哈希一次**：`chat_md5` 必须走 `messages.chat_md5`。

    错法是全静默的（spec 终审 D1）：`media/<错 md5>/` 指向一个不存在的目录，真目录永远
    删不掉，而核查对着空目录报一片 ✅。判别法：这里换回 `messages.md5_of`，这条必红。
    """
    _isolate(tmp_path, monkeypatch)
    from agent_wechat import messages
    with store.open() as con:
        store.upsert_batch(con, [_chat_row(1, chat_id="md5:aaaa")])
    _stub_resolution(monkeypatch, "md5:aaaa")
    targets = usecases.purge_chat_targets("小李")
    assert targets == [{"account": "a", "chat_id": "md5:aaaa", "chat_md5": "aaaa"}]
    assert messages.md5_of("md5:aaaa") != "aaaa", "前提：走 md5_of 会得到另一个值"


def test_purge_chat_targets_fans_out_over_every_account_with_the_chat(tmp_path, monkeypatch):
    """跨账号：隐私不按账号分，凡是索引里有这个会话的账号都要出现在计划里。"""
    _isolate(tmp_path, monkeypatch)
    with store.open() as con:
        store.upsert_batch(con, [_chat_row(1), _chat_row(1, account="b"),
                                 _chat_row(1, chat_id="别的会话")])
    _stub_resolution(monkeypatch, "victim")
    targets = usecases.purge_chat_targets("小李")
    assert [t["account"] for t in targets] == ["a", "b"], "别的会话的账号不该进来"


def test_purge_excluded_on_a_fresh_machine_prints_zero_and_creates_nothing(
        tmp_path, monkeypatch, capsys):
    """清单文件**不存在**的那条支路（`exclusions.standalone()` 的内存空表分支，评审 F5①）。

    这是新特性最可能的第一次运行形态（还没删过任何东西）。判据有两条，缺一不可：
    印「0 条」、且**没有**在磁盘上落下 exclusions.db——ATTACH 一个不存在的路径会把它建出来，
    一条只读的列清单命令不该做这件事。
    """
    home = _isolate(tmp_path, monkeypatch)
    assert not (home / "exclusions.db").exists(), "前提：清单文件不存在"
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--excluded"])
    assert ei.value.code == 0
    assert "0 条" in capsys.readouterr().out
    assert not (home / "exclusions.db").exists(), "一条只读命令把清单文件建出来了"


def test_purge_excluded_turns_a_broken_list_into_one_line(tmp_path, monkeypatch, capsys):
    """排除清单损坏 → 一行 `PurgeRefused`（退 1），不是 traceback、更不是「0 条」（评审 F4）。

    夹具是真的（往清单文件里写垃圾字节），不是注入式判据：损坏的清单是 spec §6 明列的
    拒绝场景，而**报 0 条**是这里最坏的失败——那意思是「一条都没被挡」，与事实相反，
    用户会以为重导不会把东西带回来。
    """
    home = _isolate(tmp_path, monkeypatch)
    (home / "exclusions.db").write_bytes(b"not a sqlite database" * 100)
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--excluded"])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "排除清单读不出来" in err and "Traceback" not in err


def test_purge_chat_undo_turns_a_broken_list_into_one_line(tmp_path, monkeypatch, capsys):
    """`--undo` 的读/写失败同样翻成一行（评审 F4）；桩打在**解析层**，撤销那段是真的。

    这是注入式判据（清单损坏是构造出来的），但判据本身是真的：拆掉 `_cmd_purge_chat_undo`
    的 `try/except`，裸 `sqlite3.DatabaseError` 会从 `main()` 的 `AgentWeChatError` 漏斗外
    穿出去——用户看到的是一整个 traceback。
    """
    home = _isolate(tmp_path, monkeypatch)
    (home / "exclusions.db").write_bytes(b"not a sqlite database" * 100)
    _stub_resolution(monkeypatch, "victim")        # 只桩解析层：撤销那段照跑
    monkeypatch.setattr(usecases, "purge_chat_targets",
                        lambda *a, **kw: [{"account": "a", "chat_id": "victim",
                                           "chat_md5": "aaaa"}])
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--chat", "小李", "--undo", "--yes"])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "撤销未执行" in err and "Traceback" not in err


def test_purge_chat_turns_a_broken_index_into_one_line(tmp_path, monkeypatch, capsys):
    """索引库损坏 → `purge_chat_targets` 那一层就翻成一行（评审 F4）。

    与 Task 6 的 `test_plan_chat_turns_a_broken_index_into_purge_refused` 同一形状、不同入口：
    这里要钉的是**新表面**不许把裸 `sqlite3.DatabaseError` 漏给用户。
    """
    home = _isolate(tmp_path, monkeypatch)
    (home / "index.db").write_bytes(b"not a sqlite database" * 100)
    _stub_resolution(monkeypatch, "victim")
    with pytest.raises(SystemExit) as ei:
        cli.main(["purge", "--chat", "小李"])
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "读不出会话" in err and "Traceback" not in err


def test_export_wires_the_skipped_note_to_stderr(monkeypatch, capsys):
    """`on_skipped` 的 CLI 接线，三处之一（Task 4 留给本任务的遗留②）。

    在此之前三处接线**零测试**：把 `cli.py` 里的 `on_skipped=_note_skipped` 摘掉，全量仍绿
    ——而那意味着「成功但有保留」这唯一一条出口实际断线，用户只看到数字变小。
    """
    from agent_wechat import export

    def fake_run_export(acct, **kw):
        kw["on_skipped"](5)
        return 1

    monkeypatch.setattr(usecases, "export_plan",
                        lambda **kw: usecases.ExportPlan("a", None, False))
    monkeypatch.setattr(export, "run_export", fake_run_export)
    with pytest.raises(SystemExit) as ei:
        cli.main(["export"])
    assert ei.value.code == 0
    out, err = capsys.readouterr()
    assert "跳过 5 行（已排除）" in err
    assert "跳过" not in out, "「成功但有保留」走 stderr，不污染 stdout（spec §7）"


def test_media_rebuild_refs_wires_the_skipped_note_to_stderr(monkeypatch, capsys):
    """三处之二：`media rebuild-refs` 只写 media_refs，**一条 messages 都不写**。"""
    def fake_rebuild(account=None, progress=None, on_skipped=None):
        on_skipped(7)
        return 3

    monkeypatch.setattr(usecases, "media_rebuild_refs", fake_rebuild)
    with pytest.raises(SystemExit) as ei:
        cli.main(["media", "rebuild-refs"])
    assert ei.value.code == 0
    out, err = capsys.readouterr()
    assert "跳过 7 行（已排除）" in err and "跳过" not in out


def test_appmsg_rebuild_wires_the_skipped_note_to_stderr(monkeypatch, capsys):
    """三处之三：`appmsg rebuild` 只写 msg_parts。"""
    def fake_rebuild(account=None, progress=None, on_skipped=None):
        on_skipped(11)
        return {"parts": 1, "resolved": 0, "unresolved": 0, "rejected": 0,
                "malformed": 0, "skipped": 11}

    monkeypatch.setattr(usecases, "appmsg_rebuild_parts", fake_rebuild)
    with pytest.raises(SystemExit) as ei:
        cli.main(["appmsg", "rebuild"])
    assert ei.value.code == 0
    out, err = capsys.readouterr()
    assert "跳过 11 行（已排除）" in err and "跳过" not in out
