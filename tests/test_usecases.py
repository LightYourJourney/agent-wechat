"""用例层（usecases）：MCP 与 CLI 共享的入口策略。

C2 的核心断言在这里：**同一个 contact + 同一个 limit，两个入口走的是同一个
用例函数、拿到同一个解析结果**——归一化、消歧、未命中三件事只实现一次。
"""
import json
from types import SimpleNamespace

import pytest

from agent_wechat import account, cli, errors, mcp_server, paths, reader, search, store, usecases, vector
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

    monkeypatch.setattr(account, "resolve_account", lambda a: "acct")
    monkeypatch.setattr(reader, "MessageReader", _StubReader)
    # 检索用例现在会先 _refresh_roster → store.open()：不隔离 DB_PATH 就会碰真实索引库
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "index.db")
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


# ---------- media_export：单会话 + limit 夹取（T6，MCP T7 共用此入口） ----------

def test_media_export_wires_single_chat_and_clamped_limit(stub, monkeypatch):
    """media_export：消歧后以**单元素** chat_ids 交 export_media，limit 夹到 1-2000，
    结果 out_dir 直取 report.out_dir（media.py 赋的实际输出根，入口不重算）。"""
    from agent_wechat import media
    _unique(stub)
    seen: dict = {}

    def fake_export_media(account, chat_ids, *, kinds, months, out_dir, max_files,
                          progress=None):
        seen.update(account=account, chat_ids=chat_ids, kinds=kinds, months=months,
                    out_dir=out_dir, max_files=max_files)
        return media.MediaReport(image=media.Counts(ok=1), out_dir="/default/media")

    monkeypatch.setattr(media, "export_media", fake_export_media)
    res = usecases.media_export("张三", kinds=["image"], months=["2026-09"],
                                out_dir=None, limit=99999)
    assert seen == {"account": "acct", "chat_ids": ["wxid_target"], "kinds": ["image"],
                    "months": ["2026-09"], "out_dir": None, "max_files": 2000}
    assert res.chat_id == "wxid_target" and res.out_dir == "/default/media"
    assert res.report.image.ok == 1


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
