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


@pytest.mark.xfail(strict=True,
                   reason="后半段依赖 Task 4 的 store.upsert_batch → int 契约（今天返回 None，"
                          "被排除的行照写）；Task 4 落地后本标记会 XPASS(strict) 变红，删掉它即可")
def test_tombstone_survives_index_purge_and_reimport(tmp_path, monkeypatch):
    """**AC4**：`purge --index` 之后排除清单必须还在，重建索引之后那批仍被拦。

    这是不变式①（spec §4.1）——它没了，用户重建后以为早删干净的会话原样回来，
    而**他不会知道**。所以这条必须真跑一遍：删索引 → 确认清单还在 → 重建 → 确认仍被拦。

    **前半段（清单活过 `--index`）今天就成立**，红的是后半段：拦在写入端的判据是
    Task 4 的（spec §5.4 第 1 条），本任务只负责产物表与提示。
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
        n = store.upsert_batch(con, [{
            "msg_key": "a|aaaa|message_0.db|1", "account": "a", "chat_md5": "aaaa",
            "chat_id": "victim", "shard": "message_0.db", "local_id": 1, "server_id": 1,
            "ts": 100, "msg_type": 1, "sender_id": None, "sender_name": None,
            "is_self": 0, "content": "本该删掉的", "media_hint": None}])
        assert n == 1, "重建索引之后那条仍必须被拦"
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


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

    def fake_rebuild(account=None, progress=None):
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
