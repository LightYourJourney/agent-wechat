"""用例层（usecases）：MCP 与 CLI 共享的入口策略。

C2 的核心断言在这里：**同一个 contact + 同一个 limit，两个入口走的是同一个
用例函数、拿到同一个解析结果**——归一化、消歧、未命中三件事只实现一次。
"""
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from agent_wechat import (account, cli, errors, keys, mcp_server, messages, paths, reader,
                          search, store, tasks, usecases, vector)
# 合成微信目录（真实 sqlcipher 解密链路；路径全部指向 tmp_path，绝不碰真实数据）
from test_reader_export_pipeline import ACCT, FRIEND, env, wechat  # noqa: F401

# reader 的行形状：多出 remark/nick_name/alias，对外只保留三字段
CANDS = [{"username": "wxid_a", "display": "张三", "kind": "direct",
          "remark": "张三", "nick_name": "zc", "alias": "z3"},
         {"username": "888@chatroom", "display": "张三（群）", "kind": "group",
          "remark": "张三（群）", "nick_name": None, "alias": None}]
EXPECTED = [{"username": "wxid_a", "display": "张三", "kind": "direct"},
            {"username": "888@chatroom", "display": "张三（群）", "kind": "group"}]


@pytest.fixture()
def stub(monkeypatch, tmp_path):
    """把账号解析与 MessageReader 替成桩，并记录会话解析与落库调用的实参。"""
    seen: dict = {}

    class _StubReader:
        def __init__(self, acct):
            seen["readers"] = seen.get("readers", []) + [acct]
            self._drift = []         # 真实 MessageReader 的状态：collect_degradations 会读

        def resolve_one(self, contact):
            seen.setdefault("contacts", []).append(contact)
            return seen["resolution"]

        def recent(self, chat_id, limit=50):
            seen.setdefault("limits", []).append(limit)
            return []

        def contacts(self, query=None, limit=200):
            seen["contacts_limit"] = limit
            return []

        def display_name(self, chat_id):
            return f"名:{chat_id}"

        def roster(self):
            return []            # 检索前的懒刷新会调它；空名册 → 不写库

        # 三个 rebuild 的回填入口：默认空，用例按需塞 `seen["iter_*"]`
        def iter_all(self, *a, **k):
            yield from seen.get("iter_all", ())

        def iter_extras_input(self):
            yield from seen.get("iter_extras", ())

        def iter_parts_input(self):
            yield from seen.get("iter_parts", ())

    monkeypatch.setattr(account, "resolve_account", lambda a: "acct")
    monkeypatch.setattr(reader, "MessageReader", _StubReader)
    # 检索用例现在会先 _refresh_roster → store.open()：不隔离 DB_PATH 就会碰真实索引库
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "index.db")
    # store.connect() 还会经 paths.ensure_home_dirs() 建目录（并 shell 出 tmutil）：
    # HOME_DIR 同样要隔离，否则 media_export 开索引库时会在真实 ~/.agent-wechat 下动土
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path / "home")
    return seen


def _unique(stub):
    stub["resolution"] = ("wxid_target", [])
    return stub


def _ambiguous(stub):
    stub["resolution"] = (None, CANDS)
    return stub


def _miss(stub):
    stub["resolution"] = (None, [])
    return stub


# ---------- 归一化：下界一律 1，上界只有文档化了区间的工具才夹 ----------

@pytest.mark.parametrize("n,expected", [(-1, 1), (0, 1), (1, 1), (500, 500), (1000, 500)])
def test_recent_limit_bounds(n, expected):
    assert usecases.clamp_limit(n, usecases.LIMIT_RECENT) == expected


@pytest.mark.parametrize("n,expected", [(-1, 1), (0, 1), (50, 50), (51, 50), (1000, 50)])
def test_semantic_limit_bounds(n, expected):
    assert usecases.clamp_limit(n, usecases.LIMIT_SEMANTIC) == expected


@pytest.mark.parametrize("n,expected", [(-1, 1), (0, 1), (20, 20), (1000, 1000)])
def test_unbounded_tools_only_clamp_lower(n, expected):
    """contacts / search_history 文档只写「返回上限」无区间 → 只夹下界。"""
    assert usecases.clamp_limit(n) == expected


def test_chat_context_reader_is_lazy_and_reused(stub):
    """上下文本身不构造 reader（无 contact 的检索不该碰密钥），构造后实例内复用。"""
    _unique(stub)
    ctx = usecases.ChatContext()
    assert stub.get("readers") is None
    ctx.resolve("张三")
    ctx.resolve("李四")
    assert stub["readers"] == ["acct"]


def test_the_usecase_limit_defaults_are_pinned_to_500():
    """EA-094：媒体/朋友圈两个用例层的 `limit` 默认曾是 2000——与 TOOLS.md §9/§10 的默认列
    （500）和两个入口（MCP 500）都不一致，且两个入口**都恒传** limit，用例层那份只在测试
    里可达。默认值应当只存在于入口；这两处对齐成 500。

    **这条是硬编码的 pin**（F5，T8 复审：原用例名自称「match the documented value」，实际
    不读文档）：MCP 侧参数表 ↔ 签名的真正对账在 `tests/test_tools_doc.py`；用例层的默认
    没有文档行可读，就只能钉值。把它改回 2000，断言必红。
    """
    import inspect

    for fn in (usecases.media_export, usecases.moments_export):
        assert inspect.signature(fn).parameters["limit"].default == 500, fn.__name__


def test_semantic_search_survives_without_a_passphrase_cache(env, monkeypatch):
    """EA-024：没有密钥缓存（`purge --keys` 之后、或换机只拷了 index.db 的机器）时，
    语义检索**不该整条失败**——展示名富化把「本地索引可查」绑死在它并不需要的密钥上。

    真链路（不桩 reader）：桩掉 `vector.semantic_search` 只留下用例层富化那一行；把富化的
    try/except 删掉，第一条断言必红（`KeyNotFound` 穿出，而 `build_vector_index` 同样
    不需要密钥——「先 build 再 semantic」这条常规路径会在第二步炸）。
    """
    monkeypatch.setattr(keys, "CACHE_FILE", env.home / "被-purge--keys-删掉的那份.json")
    monkeypatch.setattr(vector, "semantic_search", lambda *a, **k: {
        "mode": "semantic", "total": 1,
        "hits": [{"chat_id": FRIEND, "ts": 1, "sender": "x", "is_self": False,
                  "content": "爬山", "media_hint": None, "distance": 0.1}]})

    res = usecases.semantic_search("爬山", account=ACCT)
    assert res["hits"][0]["display"] == FRIEND, res       # 读不到名册 → 回落裸 chat_id
    # F1（T8 复审）：回落必须**自证**——no-key 时 reader 根本没构造出来，`_drift` 里
    # 不会有 roster 那条（TOOLS §8 承诺「显示名降级 ⇒ degraded 非空」）。
    assert [d["capability"] for d in res["degraded"]] == ["roster"], res["degraded"]
    assert "hits" in usecases.search_history("你好", account=ACCT), "兄弟工具同一时刻照常"

    # 反例：缓存正常时 display 仍是展示名、且不许误报降级（回落不许变成常态）
    monkeypatch.setattr(keys, "CACHE_FILE", env.home / "passphrase.json")
    res2 = usecases.semantic_search("爬山", account=ACCT)
    assert res2["hits"][0]["display"] == "张三", res2
    assert res2["degraded"] == [], res2["degraded"]


def test_result_shapes(stub, monkeypatch):
    """类型化结果：recent 返回三字段结构、semantic 的 hits 带 display、contacts 原样。"""
    _unique(stub)
    res = usecases.recent_messages("张三", limit=7)
    assert (res.chat_id, res.display, res.messages) == ("wxid_target", "名:wxid_target", [])
    assert stub["limits"] == [7]

    monkeypatch.setattr(vector, "semantic_search", lambda *a, **k: {
        "mode": "semantic", "total": 1,
        "hits": [{"chat_id": "wxid_target", "ts": 1, "sender": "张三",
                  "is_self": False, "content": "x", "media_hint": None, "distance": 0.1}]})
    out = usecases.semantic_search("爬山")
    assert out["hits"][0]["display"] == "名:wxid_target"

    assert usecases.contacts("张三") == []
    assert stub["contacts_limit"] == 200      # reader 默认值：CLI 不传 limit


# ---------- 消歧的三种结果：唯一命中 / 歧义 / 未命中 ----------

def test_unique_match_resolves_to_chat_id(stub):
    _unique(stub)
    assert usecases.ChatContext().resolve("张三") == "wxid_target"


def test_ambiguity_returns_candidates_trimmed_and_kinded(stub):
    _ambiguous(stub)
    res = usecases.ChatContext().resolve("张三")
    assert isinstance(res, usecases.NeedConfirm)
    assert [c.to_dict() for c in res.candidates] == EXPECTED   # 只保留对外三字段
    assert [c.kind for c in res.candidates] == ["direct", "group"]


def test_ambiguity_caps_candidates_at_ten(stub):
    stub["resolution"] = (None, [dict(CANDS[0], username=f"wxid_{i}") for i in range(25)])
    res = usecases.ChatContext().resolve("张三")
    assert len(res.candidates) == 10


def test_miss_raises_documented_contact_not_found(stub):
    """未命中在用例层抛项目异常，文案**中立于入口**——后缀由适配层各自追加。"""
    _miss(stub)
    with pytest.raises(errors.ContactNotFound) as ei:
        usecases.ChatContext().resolve("张三")
    assert str(ei.value) == "找不到联系人「张三」"


# ---------- 两个入口一致（C2 核心断言） ----------

def test_recent_same_contact_same_limit_both_entrances(stub, capsys):
    """同一 contact + 同一 limit：MCP 与 CLI 命中同一个用例函数、同一解析结果。"""
    _unique(stub)
    mcp_res = mcp_server.get_recent_messages(contact="张三", limit=1000)
    cli_code = cli.cmd_recent(SimpleNamespace(account=None, contact="张三", n=1000))
    capsys.readouterr()
    assert (mcp_res["chat_id"], cli_code, stub["contacts"]) == \
        ("wxid_target", 0, ["张三", "张三"])
    assert stub["limits"] == [500, 500]          # 同一上界：MCP 与 CLI 都不超过 500
    assert stub["readers"] == ["acct", "acct"]   # 各自一次，不再满文件重复构造


CLI_AMBIGUOUS = [
    ("recent", lambda a: cli.cmd_recent(SimpleNamespace(account=None, contact="张三", n=5))),
    ("search", lambda a: cli.cmd_search(
        SimpleNamespace(account=None, contact="张三", query="会议", n=5))),
    ("export", lambda a: cli.cmd_export(
        SimpleNamespace(account=None, contact="张三", full=False))),
    ("semantic", lambda a: cli.cmd_semantic(
        SimpleNamespace(account=None, contact="张三", query="爬山", n=5))),
]


@pytest.mark.parametrize("name,run", CLI_AMBIGUOUS)
def test_cli_four_commands_render_ambiguity_identically(name, run, stub, capsys, monkeypatch):
    """recent/search/export/semantic 四处一致：候选（含 kind）→ stdout，
    一行说明 → stderr，退出码 1；且歧义时不得继续执行副作用（不起检索/导出）。"""
    _ambiguous(stub)
    monkeypatch.setattr(search, "search", _boom)
    monkeypatch.setattr(vector, "semantic_search", _boom)
    monkeypatch.setattr(usecases.tasks, "start", _boom)   # 任务登记归用例层
    monkeypatch.setattr(usecases.export, "run_export", _boom)

    assert run(None) == 1
    out, err = capsys.readouterr()
    assert json.loads(out) == EXPECTED      # stdout 就是 MCP need_confirm 的候选数组
    assert "无法唯一解析「张三」" in err
    assert len(err.strip().splitlines()) == 1


@pytest.mark.parametrize("name,kwargs", [
    ("get_recent_messages", {"contact": "张三"}),
    ("search_history", {"query": "会议", "contact": "张三"}),
    ("semantic_search", {"query": "爬山", "contact": "张三"}),
])
def test_mcp_tools_return_need_confirm_envelope(name, kwargs, stub):
    _ambiguous(stub)
    res = getattr(mcp_server, name)(**kwargs)
    assert res == {"need_confirm": EXPECTED}


def test_mcp_and_cli_report_miss_identically(stub):
    """未命中：MCP 信封的 error 文案 = CLI stderr 的单行文案（同一条异常）。"""
    _miss(stub)
    res = mcp_server.get_recent_messages(contact="张三")
    assert res == {"error": "找不到联系人「张三」，请用 list_contacts 查询"}
    with pytest.raises(SystemExit) as ei:
        cli.main(["recent", "张三"])
    assert ei.value.code == 1


def test_cli_semantic_success_output_unchanged(stub, capsys, monkeypatch):
    """成功路径的 CLI 输出形状不变：mode/total + chat_id + distance（不打印 display）。"""
    _unique(stub)
    monkeypatch.setattr(vector, "semantic_search", lambda *a, **k: {
        "mode": "semantic", "total": 1,
        "hits": [{"chat_id": "wxid_target", "ts": 1, "sender": "张三",
                  "is_self": False, "content": "爬山", "media_hint": None,
                  "distance": 0.5}]})
    assert cli.cmd_semantic(SimpleNamespace(account=None, contact="张三",
                                            query="爬山", n=5)) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == ["mode=semantic total=1",
                                "  [1] wxid_target 张三: 爬山  distance=0.5000"]


# ---------- export 歧义回归：曾把歧义误报成「找不到联系人」 ----------

def _boom(*a, **k):
    raise AssertionError("歧义路径不得继续执行")


def test_export_ambiguity_returns_need_confirm_not_not_found(stub, monkeypatch):
    """回归（C2 前必失败）：`export_full_history` 自己调 resolve_one 并把候选丢进
    `_cands`，于是歧义输入返回「找不到联系人「张三」」——候选丢失，agent 拿到死胡同。"""
    _ambiguous(stub)
    monkeypatch.setattr(usecases.tasks, "start", _boom)
    res = mcp_server.export_full_history(contact="张三")
    assert "error" not in res, res
    assert res == {"need_confirm": EXPECTED}


def test_cli_export_ambiguity_lists_candidates(stub, monkeypatch, capsys):
    _ambiguous(stub)
    monkeypatch.setattr(usecases.export, "run_export", _boom)
    assert cli.cmd_export(SimpleNamespace(account=None, contact="张三", full=False)) == 1
    out, err = capsys.readouterr()
    assert json.loads(out) == EXPECTED
    assert "无法唯一解析「张三」" in err


def test_export_miss_is_contact_not_found_with_suffix(stub):
    """未命中：core 抛中立的 ContactNotFound，**MCP 出口**才补上工具名后缀。

    对外形状与 TOOLS.md:105/268 文档化的逐字节一致——只是后缀的责任从 core
    移到了适配层（core 不该知道 `list_contacts` 这个工具名）。
    """
    _miss(stub)
    with pytest.raises(errors.ContactNotFound) as ei:
        usecases.export_plan(contact="张三")
    assert str(ei.value) == "找不到联系人「张三」"
    assert mcp_server.export_full_history(contact="张三") == {
        "error": "找不到联系人「张三」，请用 list_contacts 查询"}


def test_export_without_contact_keeps_full_scope(stub):
    """省略 contact = 导出全部：不得解析、不得碰 reader。"""
    plan = usecases.export_plan(contact=None, full=True)
    assert (plan.account, plan.chat_id, plan.full) == ("acct", None, True)
    assert stub.get("readers") is None


def _wait_done(tid, timeout=5.0):
    """轮询到任务不再是 running。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = tasks.status(tid)
        if st["status"] != "running":
            return st
        time.sleep(0.01)
    raise AssertionError(f"任务 {tid} 未在 {timeout}s 内结束")


def test_export_task_carries_skipped_rows_beside_rows(tmp_path, monkeypatch):
    """计数契约的**另一半**（spec §5.4）：`rows` 只数实际入库的行，被排除挡下的那些另计、
    并以一条降级报出。CLI 侧接了 `on_skipped`，MCP 侧此前没接——任务记录里只有变小了的
    `rows`，agent 见数少了却不知道少在哪，正是「报告与事实不一致」。

    判据落在**任务记录**上：种一个被排除的会话 + 一个正常的，跑 MCP 那条路
    （`usecases.export_history` + 轮询到 done），断言 `skipped` = 被挡下的行数、`rows`
    只数正常那个。去掉 runner 里的 `on_skipped`，`skipped` 键整个不存在，本测试即红。
    """
    from agent_wechat import exclusions
    monkeypatch.setattr(tasks, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(account, "resolve_account", lambda a: "acct")

    def _row(md5, chat_id, local_id):
        return {"msg_key": messages.row_key("acct", md5, "message_0.db", local_id),
                "account": "acct", "chat_md5": md5, "chat_id": chat_id,
                "shard": "message_0.db", "local_id": local_id, "server_id": local_id,
                "ts": 100 + local_id, "msg_type": messages.TYPE_TEXT, "sender_id": None,
                "sender_name": None, "is_self": 0, "content": "正文", "media_hint": None}

    victim = messages.chat_md5("victim")
    keep = messages.chat_md5("keep")
    victim_rows = [_row(victim, "victim", n) for n in (1, 2)]
    keep_rows = [_row(keep, "keep", n) for n in (1, 2, 3)]
    # 墓碑必须种在 store.connect() 那条路上（run_export 内部走 store.open() → DB_PATH）
    con = store.connect()
    exclusions.add(con, [(r["msg_key"], "acct", victim) for r in victim_rows])
    con.commit()
    con.close()

    class _R:
        def __init__(self, acct):
            pass

        def iter_all(self, **kw):
            yield victim_rows + keep_rows, []      # 微信库里那两条「还在」

    monkeypatch.setattr(reader, "MessageReader", _R)

    res = usecases.export_history(full=True)
    st = _wait_done(res["task_id"])
    assert st["status"] == "done", st
    assert st["rows"] == len(keep_rows), f"rows 只数入库的：{st}"
    assert st.get("skipped") == len(victim_rows), f"被挡下的行必须另计：{st}"
    con = store.connect()
    assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == len(keep_rows)
    con.close()


# ---------- media_export：单会话 + limit 只余下界（CLI 与 MCP 共用此入口） ----------

def test_media_export_wires_single_chat_and_passes_limit_through(stub, monkeypatch):
    """media_export：消歧后以**单元素** chat_ids 交 export_media，`limit` **原样透传**
    （不再夹到 2000 —— 夹取曾使 `--limit 99999` 静默变成 2000），
    结果 out_dir 直取 report.out_dir（media.py 赋的实际输出根，入口不重算）。

    不导视频时**不开索引库**（`con=None`）——索引只有视频归属用得上，不给图片/语音
    多背一条失败路径。
    """
    from agent_wechat import media
    _unique(stub)
    seen: dict = {}

    def fake_export_media(account, chat_ids, *, kinds, months, out_dir, max_files,
                          force=False, progress=None, con=None, with_cdn=False):
        seen.update(account=account, chat_ids=chat_ids, kinds=kinds, months=months,
                    out_dir=out_dir, max_files=max_files, force=force, con=con,
                    with_cdn=with_cdn)
        return media.MediaReport(image=media.Counts(ok=1), out_dir="/default/media")

    monkeypatch.setattr(media, "export_media", fake_export_media)
    res = usecases.media_export("张三", kinds=["image"], months=["2026-09"],
                                out_dir=None, limit=99999)
    assert seen == {"account": "acct", "chat_ids": ["wxid_target"], "kinds": ["image"],
                    "months": ["2026-09"], "out_dir": None, "max_files": 99999,
                    "force": False, "con": None, "with_cdn": False}
    assert res.chat_id == "wxid_target" and res.out_dir == "/default/media"
    assert res.report.image.ok == 1


def test_media_export_opens_index_for_video(stub, monkeypatch):
    """视频归属要索引：用例层自己开 `store` 并把**可用的连接**交给 export_media。

    改前 `usecases.media_export` 根本没有 store 访问——`export_media` 只能按账号级枚举，
    多会话无从归属。忘了传 con 这条必红（`None.execute` 直接 AttributeError）。
    连接的生命周期归用例层：只在调用期间有效，出栈即关（下面在假函数**内部**查库）。
    """
    from agent_wechat import media
    _unique(stub)
    seen: dict = {}

    def fake_export_media(account, chat_ids, *, kinds, months, out_dir, max_files,
                          force=False, progress=None, con=None, with_cdn=False):
        seen["con"] = con
        seen["refs"] = con.execute("SELECT count(*) FROM media_refs").fetchone()[0]
        return media.MediaReport(out_dir="/default/media")

    monkeypatch.setattr(media, "export_media", fake_export_media)
    usecases.media_export("张三", kinds=["video"])
    assert isinstance(seen["con"], sqlite3.Connection)
    assert seen["refs"] == 0            # 真查到了隔离后的索引库（stub 夹具把 DB_PATH 指到 tmp_path）


def test_media_export_unavailable_index_skips_video_and_degrades_loudly(stub, monkeypatch):
    """索引库打不开 → **本次不导视频** + 如实降级；其余类型照常。

    绝不退回 `export_media(con=None)` 那条账号级枚举：单会话请求下它会把账号级视频树里的
    **全部**文件写进这个会话的目录（错归属，比不导出更坏）。断言「没连接时绝不请求 video」
    这个硬不变量——把 video 从 kinds 里摘掉才是唯一合规的做法。
    """
    from agent_wechat import media
    _unique(stub)
    seen: dict = {}

    def fake_export_media(account, chat_ids, *, kinds, months, out_dir, max_files,
                          force=False, progress=None, con=None, with_cdn=False):
        # 用例层的硬不变量：没有索引连接时绝不请求视频导出
        assert con is not None or "video" not in kinds
        seen["kinds"], seen["con"] = kinds, con
        return media.MediaReport(out_dir="/default/media")

    def boom(*a, **kw):
        raise errors.HomeUnavailable("数据目录不可用：/tmp/x")

    monkeypatch.setattr(media, "export_media", fake_export_media)
    monkeypatch.setattr(store, "open", boom)
    res = usecases.media_export("张三", kinds=["image", "video"])
    assert seen["kinds"] == ["image"]          # video 被摘掉，图片照常导
    assert seen["con"] is None                 # 本来就没连接（不是「拿老路子凑」）
    assert len(res.report.degraded) == 1
    assert "索引库不可用" in res.report.degraded[0] and "未导出任何视频" in res.report.degraded[0]


def test_media_export_passes_large_limit_through(stub, monkeypatch):
    """`--limit 5000` 必须原样透到 `media.export_media`（不再被夹成 2000）。

    变异：把 `clamp_limit(limit)` 改回 `clamp_limit(limit, (1, 2000))` → 期望 5000 得到 2000 → 红。
    """
    from agent_wechat import media
    _unique(stub)
    seen: dict = {}

    def fake_export_media(account, chat_ids, *, kinds, months, out_dir, max_files,
                          force=False, progress=None, con=None, with_cdn=False):
        seen.update(max_files=max_files, force=force)
        return media.MediaReport()

    monkeypatch.setattr(media, "export_media", fake_export_media)
    usecases.media_export("张三", kinds=["voice"], limit=5000)
    assert seen == {"max_files": 5000, "force": False}


def test_media_export_force_is_passed_through(stub, monkeypatch):
    """`force=True` 必须透传（CLI `--force` → usecases → media），断链就在这条。"""
    from agent_wechat import media
    _unique(stub)
    seen: dict = {}
    monkeypatch.setattr(media, "export_media",
                        lambda account, chat_ids, **kw: seen.update(kw) or media.MediaReport())
    usecases.media_export("张三", kinds=["voice"], force=True)
    assert seen["force"] is True


def test_media_export_rejects_bad_months(stub):
    """`--months` 非法格式（`2026-9` / `2026-13`）→ SearchInputError。

    改前静默 0 命中、exit 0：月份串拿去和目录名比对，格式错的那些永远比不中，用户
    看到的是「这个月没有媒体」而不是「你的月份写错了」。
    """
    _unique(stub)
    for bad in ("2026-9", "2026-13", "202609", "2026-00", "2026-13-01"):
        with pytest.raises(errors.SearchInputError) as ei:
            usecases.media_export("张三", kinds=["image"], months=[bad])
        assert "月份格式应为 YYYY-MM" in str(ei.value) and bad in str(ei.value)


# ---------- CLI recent 上界 500（有意的用户可见变更） ----------

def test_cli_recent_n_1000_is_capped_at_500(stub, capsys):
    _unique(stub)
    assert cli.cmd_recent(SimpleNamespace(account=None, contact="张三", n=1000)) == 0
    capsys.readouterr()
    assert stub["limits"] == [500]


# ---------- build_vector_index：wire 单数 account → core 复数 accounts ----------

def test_build_vector_index_translates_account_to_accounts(monkeypatch):
    """wire 层语义不变（省略 = 索引全部账号）的护栏：翻译只发生在用例层。

    core 的 `accounts` 是复数列表（None = 全部、[] = 一个都不索引）；
    wire 的 `account` 保持单数（None = 全部）。任务登记归用例层所有，接缝也在这里
    （core 的 vector 模块已无 start_build_task）。
    """
    seen: list[tuple] = []
    monkeypatch.setattr(usecases, "start_vector_build",
                        lambda accounts, rebuild: seen.append((accounts, rebuild)) or "tid")

    assert usecases.build_vector_index(account=None) == {"task_id": "tid", "status": "running"}
    assert usecases.build_vector_index(account="acct") == {"task_id": "tid", "status": "running"}
    assert usecases.build_vector_index(account=None, rebuild=True) == {
        "task_id": "tid", "status": "running"}

    assert seen == [(None, False), (["acct"], False), (None, True)]


# ---------- 合成库全链路（真实解密链路，路径全在临时目录） ----------

def test_usecases_three_outcomes_on_synthetic_wechat(env):
    """唯一命中 / 歧义 / 未命中：走真实 reader（快照 + 解密 + contact.db 解析）。"""
    res = usecases.recent_messages("张三", limit=3, account=ACCT)
    assert (res.chat_id, res.display) == (FRIEND, "张三")
    assert len(res.messages) == 3

    nc = usecases.recent_messages("friend", account=ACCT)   # friend01 / friend02 两个 username
    assert isinstance(nc, usecases.NeedConfirm)
    assert {c.username for c in nc.candidates} == {FRIEND, "wxid_friend02"}
    assert {c.kind for c in nc.candidates} == {"direct"}

    with pytest.raises(errors.ContactNotFound):
        usecases.recent_messages("不存在的名字", account=ACCT)


def test_cli_recent_renders_synthetic_messages(env, capsys):
    """端到端：CLI → usecases → 真实解密 → 渲染（时间正序、zstd 解码、媒体提示）。"""
    assert cli.cmd_recent(SimpleNamespace(account=ACCT, contact="张三", n=7)) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 7
    assert any(line.endswith("压缩消息内容") for line in out)      # WCDB_CT=4 已解码
    assert out[-1].endswith("[appmsg: 分享标题]")                # 媒体提示替代正文


# ---------- T6：消息信封加稳定标识 msg_key（spec §4.3） ----------

def test_search_hits_carry_msg_key(tmp_path, monkeypatch):
    """检索命中也要带稳定标识 —— 只给 id/shard/local_id 是问不出媒体的。

    用**真索引**而不是桩：`_HIT_COLUMNS` 漏选 msg_key 时这里必须红。
    注意 hits 里的既有 `id` 是 messages.id（rowid），与 msg_key 是两回事。
    """
    from agent_wechat import messages
    acct, cm = "wxid_testacct01_ab12", "a" * 32
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "index.db")
    con = store.connect()
    store.upsert_batch(con, [{
        "msg_key": messages.row_key(acct, cm, "message_0.db", 7),
        "account": acct, "chat_md5": cm, "chat_id": "wxid_x",
        "shard": "message_0.db", "local_id": 7, "server_id": None,
        "ts": 1700000000, "msg_type": messages.TYPE_TEXT,
        "sender_id": "wxid_x", "sender_name": "张三",
        "is_self": 0, "content": "你好世界", "media_hint": None}])
    con.commit()
    con.close()
    res = search.search("你好", account=acct)
    assert res["hits"], "夹具没建出命中，下面的断言将恒真"
    assert res["hits"][0]["msg_key"] == messages.row_key(acct, cm, "message_0.db", 7)


@pytest.mark.parametrize("name", ["Candidate", "NeedConfirm", "RecentMessages",
                                  "ExportPlan", "Degradation"])
def test_result_types_are_frozen(name):
    """B 阶段（T14）：5 个结果类型的 `@dataclass(frozen=True)` 一共**零用例**——
    把任一处改回默认（可变），`test_usecases.py` 全绿。

    这是对外契约（docstring 原话：「它是对外契约的一部分」）：入口把对象交给 CLI/MCP
    之后，调用方拿到的是**快照**，不是可被人反手改掉的活引用。
    """
    import dataclasses

    cls = getattr(usecases, name)
    assert cls.__dataclass_params__.frozen is True, f"{name} 必须 frozen"
    # 判据落到**行为**上：frozen 的类型，任何一次 setattr 都抛 FrozenInstanceError。
    # 用 `__new__` 造实例是为了不依赖各类型自己的构造签名。
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(cls.__new__(cls), "_frozen_probe", 1)


def test_a_degradation_defaults_to_drift():
    """B 阶段（T14）：`drift: bool = True` 的默认值没有用例——改成 False 时 CLI 就不再给
    「结构不符」后缀，而 `test_usecases.py` 全绿。"""
    d = usecases.Degradation("stickers", "contact.db", "contact", (), "少一部分")
    assert d.drift is True
    assert usecases.Degradation("stickers", "c.db", "t", (), "e", drift=False).drift is False


def test_sniff_ext_reads_the_magic_bytes():
    """`_sniff_ext` 的每条魔数分支都要钉住：删掉某个 `return` 会一路落到函数末尾的
    `return None`，于是内容明明是 PNG/GIF/WebP 却被判成「不是图片」，导出侧把它记成
    `media_garbage` 并丢掉（`test_moments_usecases` 只覆盖了乱码那一边）。"""
    assert usecases._sniff_ext(b"\x89PNG\r\n\x1a\n" + b"x") == "png"
    assert usecases._sniff_ext(b"GIF87a" + b"x") == "gif"
    assert usecases._sniff_ext(b"GIF89a" + b"x") == "gif"
    assert usecases._sniff_ext(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"x") == "webp"
    assert usecases._sniff_ext(b"\xff\xd8\xff" + b"x") == "jpg"
    assert usecases._sniff_ext(b"\x00" * 4 + b"ftyp" + b"x") == "mp4"
    assert usecases._sniff_ext(b"not an image") is None


def test_index_ready_reports_false_when_the_index_cannot_be_opened(monkeypatch):
    """判不出来时必须是 `False`（而不是 None / 抛出）：调用方拿它决定要不要印
    「索引为空」的提示。删掉 `except` 里的 `return False` 会返回 None——真值判断下
    看起来一样，类型契约上不是 bool。"""
    def boom(*a, **k):
        raise sqlite3.DatabaseError("disk I/O error（桩）")

    monkeypatch.setattr(store, "open", boom)
    assert usecases.index_ready() is False


def test_every_entry_point_hands_back_the_ambiguity_instead_of_guessing(stub):
    """五个解析入口遇到歧义都必须把 `NeedConfirm` **原样交回**：删掉那行早退，
    函数会带着 NeedConfirm 对象继续跑（拼 chat_md5、开库、渲染），报出来的是下游的
    AttributeError / KeyError，而不是「请从这些候选里选一个」。"""
    _ambiguous(stub)
    assert isinstance(usecases.media_export("张三", kinds=["image"]), usecases.NeedConfirm)
    assert isinstance(usecases.message_media("张三"), usecases.NeedConfirm)
    assert isinstance(usecases.chat_export("张三"), usecases.NeedConfirm)
    assert isinstance(usecases.purge_chat_targets("张三"), usecases.NeedConfirm)
    assert isinstance(usecases.moments_export("张三"), usecases.NeedConfirm)


def test_voice_keys_for_is_the_documented_seam(monkeypatch):
    """`_voice_keys_for` 是**测试接缝**：写成函数而不是模块级别名，替换它才能生效。
    删掉 `return` 会让它变成 None（= 「读不到」），调用方随之降级。"""
    sentinel = frozenset({("chat-md5", 1)})
    monkeypatch.setattr(usecases.media, "voice_body_keys", lambda a: sentinel)
    assert usecases._voice_keys_for("acct") is sentinel


def test_start_vector_build_only_names_a_single_account(monkeypatch):
    """任务元数据沿用单数 `account` 字段：**恰好一个**账号才写它，多账号与空列表都写
    None。把 `and`/`== 1`/`accounts[0]` 中任何一处改掉，多账号任务会被挂到某个账号名下
    （或单账号因为索引 1 越界而崩）。"""
    seen = {}

    def fake_start(kind, fn, **kw):
        seen.update(kind=kind, **kw)
        return "tid"

    monkeypatch.setattr(tasks, "start", fake_start)
    assert usecases.start_vector_build(["a"], False) == "tid"
    assert seen["account"] == "a"
    assert usecases.start_vector_build(["a", "b"], False) == "tid"
    assert seen["account"] is None
    assert usecases.start_vector_build([], False) == "tid"
    assert seen["account"] is None


def test_start_vector_build_reports_skipped_and_final_rows(monkeypatch):
    """结束时必须补 `skipped` 并把返回值（终值）交出去：删掉任一行，任务记录会停在
    中间进度，或让 `tasks.start` 拿到 None。"""
    updates = []

    def fake_build(accounts=None, rebuild=False, progress=None):
        if progress:
            progress(7)
        return {"skipped_this_run": 3, "embedded_this_run": 9}

    monkeypatch.setattr(usecases.vector, "build_index", fake_build)
    monkeypatch.setattr(tasks, "update", lambda tid, **kw: updates.append((tid, kw)))
    holders = []
    monkeypatch.setattr(tasks, "start",
                        lambda kind, fn, **kw: (holders.append(fn), "tid")[1])

    usecases.start_vector_build(["a"], False)
    assert holders[0]("tid") == 9
    assert updates[-1] == ("tid", {"skipped": 3})


def test_media_rebuild_refs_reports_progress(stub):
    """回填全量重扫时要报进度（n 每满一批的那一段）：删掉 `progress(n)`，分钟级命令
    在 CLI 上全程无输出。"""
    refs = [(messages.row_key("acct", "md5", "message_0.db", 1), "image", "fk", None)]
    stub["iter_all"] = [([], refs)]
    seen = []
    n = usecases.media_rebuild_refs(progress=seen.append)
    assert n == 1 and seen == [1], (n, seen)


def test_msgextra_rebuild_flushes_in_batches_and_reports_progress(stub):
    """缓冲满 BATCH 时就地 flush，收尾再 flush 剩下的：只有 2001 行才分得出
    「循环内 flush」与「只靠收尾 flush」——前者的进度是 [2000, 2001]，后者只有 [2001]。"""
    key = messages.row_key("acct", "md5", "message_0.db", 0)
    stub["iter_extras"] = [(f"{key}{i}", 48, b"<x/>") for i in range(2001)]
    seen = []
    res = usecases.msgextra_rebuild(progress=seen.append)
    assert res["extras"] == 2001
    assert seen == [2000, 2001], seen


def test_appmsg_rebuild_parts_reports_skipped_rows(stub, monkeypatch):
    """有行被排除清单挡下时回调一次 `on_skipped(行数)`——「成功但有保留」的出口，
    静默吞掉会让用户以为全量回填干净了。"""
    import types as _types
    stub["iter_parts"] = [(1, "acct|md5|message_0.db|1", b"<msg/>")]
    monkeypatch.setattr(usecases.appmsg, "parse", lambda raw: _types.SimpleNamespace(
        reason=None, appmsg=_types.SimpleNamespace(kind=1, quote=None), normalized="x"))
    monkeypatch.setattr(store, "upsert_msg_parts", lambda con, rows: len(rows))
    seen = []
    usecases.appmsg_rebuild_parts(on_skipped=seen.append)
    assert seen == [1], seen
