# tests/test_chat_export.py
"""甲的回填与导出的用例层测试。夹具按 spec §2.5 的实测形态构造，内容全部合成。"""
import pathlib
import zlib

import pytest

from agent_wechat import errors, messages, store, usecases


class _FakeReader:
    """替掉 MessageReader：只提供 iter_parts_input。
    **注意**：本夹具**必须**把 `store.DB_PATH` patch 到临时库 —— `store.connect()`
    会执行整个 SCHEMA，指错路径就是把 DDL 写进用户真实的 5.3 GB index.db。"""

    def __init__(self, rows):
        self._rows = rows

    def iter_parts_input(self):
        yield from self._rows


@pytest.fixture()
def con(tmp_path, monkeypatch):
    db = tmp_path / "index.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    c = store.connect(db)
    yield c
    c.close()


def test_rebuild_resolves_ref_msg_key_from_server_id(con, monkeypatch):
    """回指：svrid 命中同一次扫描里的 server_id 时，ref_msg_key 必须被解析出来。
    回归：把 `ref_msg_key` 的解析删掉，这条断言会红。"""
    rows = [
        (1001, "acct|aaa|message_0.db|1", None),                      # 被引用的原文
        (1002, "acct|aaa|message_0.db|2",
         '<msg><appmsg><title>同意</title><type>57</type><refermsg>'
         '<displayname>张三</displayname><content>明天开会</content>'
         '<svrid>1001</svrid></refermsg></appmsg></msg>'),
    ]
    monkeypatch.setattr(usecases, "ChatContext", lambda acct=None: type(
        "C", (), {"account": "acct", "reader": _FakeReader(rows)})())
    res = usecases.appmsg_rebuild_parts(account="acct")
    assert res["parts"] == 1 and res["resolved"] == 1
    got = store.msg_parts_by_keys(con, ["acct|aaa|message_0.db|2"])
    assert got["acct|aaa|message_0.db|2"][2] == "acct|aaa|message_0.db|1"


def test_rebuild_leaves_ref_null_when_target_is_unknown(con, monkeypatch):
    """目标不在本次扫描里 → ref_msg_key 为 NULL，**不是 0、不是空串**。
    渲染端靠 NULL 决定「退化成内联显示」，空串会让它以为能跳。"""
    rows = [(None, "acct|aaa|message_0.db|2",
             '<msg><appmsg><type>57</type><refermsg><content>x</content>'
             '<svrid>999999</svrid></refermsg></appmsg></msg>')]
    monkeypatch.setattr(usecases, "ChatContext", lambda acct=None: type(
        "C", (), {"account": "acct", "reader": _FakeReader(rows)})())
    res = usecases.appmsg_rebuild_parts(account="acct")
    assert res["unresolved"] == 1
    assert store.msg_parts_by_keys(con, ["acct|aaa|message_0.db|2"])[
        "acct|aaa|message_0.db|2"][2] is None


def test_rebuild_counts_rejected_and_malformed_separately(con, monkeypatch):
    """回归：两者混计则「我们主动拒绝」与「解不开」在报告里长得一样。"""
    rows = [
        (None, "acct|aaa|message_0.db|1", "<!DOCTYPE x [<!ENTITY a 'b'>]><msg/>"),
        (None, "acct|aaa|message_0.db|2", "<msg><unclosed></msg>"),
    ]
    monkeypatch.setattr(usecases, "ChatContext", lambda acct=None: type(
        "C", (), {"account": "acct", "reader": _FakeReader(rows)})())
    res = usecases.appmsg_rebuild_parts(account="acct")
    assert res["rejected"] == 1 and res["malformed"] == 1 and res["parts"] == 0


def test_rebuild_does_not_cross_link_a_foreign_account(con, monkeypatch):
    """回归：`msg_parts` 是**多账号共用**的表，而 `sid_map` 只装本账号的 server_id。

    阶段 2 的 `UPDATE` 若**不加账号前缀限定**，异账号的同号 `server_id` 会把引用接到错的消息
    上——那是往用户真实库里写一条**错的引用关系**。删掉那个 `substr` 谓词，这条会红。

    （T4 审阅者在双账号种子上实测：不加限定时外来行的 `ref_msg_key` 变成了
    `'acct|aaa|message_0.db|1'`；加上后保持 NULL。）
    """
    # 先塞一条**别的账号**的行，它的 ref_svrid 会被本账号的引用撞上
    store.upsert_msg_parts(con, [("other|zzz|message_0.db|9", 57, 1001, None, b"<msg/>")])
    con.commit()        # 释放写锁：rebuild 走 store.open() 另开一条连接，不提交会撞 "database is locked"
    rows = [
        (1001, "acct|aaa|message_0.db|1", None),
        (1002, "acct|aaa|message_0.db|2",
         '<msg><appmsg><title>同意</title><type>57</type><refermsg>'
         '<displayname>张三</displayname><content>明天开会</content>'
         '<svrid>1001</svrid></refermsg></appmsg></msg>'),
    ]
    monkeypatch.setattr(usecases, "ChatContext", lambda acct=None: type(
        "C", (), {"account": "acct", "reader": _FakeReader(rows)})())
    usecases.appmsg_rebuild_parts(account="acct")
    assert store.msg_parts_by_keys(
        con, ["other|zzz|message_0.db|9"])["other|zzz|message_0.db|9"][2] is None, \
        "异账号的行被跨账号接上了"


def test_rebuild_is_idempotent(con, monkeypatch):
    rows = [(1, "acct|aaa|message_0.db|1",
             '<msg><appmsg><title>t</title><type>5</type><url>u</url></appmsg></msg>')]
    monkeypatch.setattr(usecases, "ChatContext", lambda acct=None: type(
        "C", (), {"account": "acct", "reader": _FakeReader(rows)})())
    usecases.appmsg_rebuild_parts(account="acct")
    usecases.appmsg_rebuild_parts(account="acct")
    assert store.count_msg_parts(con, "acct") == 1


def test_messages_in_range_takes_the_most_recent_n_not_the_oldest(con, monkeypatch):
    """**取向**是这个函数唯一容易写错的地方（spec §4.4）。
    范围里有 10 条、limit=3，必须是**最近 3 条**，且 truncated=True。
    回归：把 ORDER BY 改成 ASC 再取前 3，这条断言会红。"""
    con.executemany(
        "INSERT INTO messages (msg_key, account, chat_md5, chat_id, shard, local_id,"
        " ts, msg_type, is_self, content, exported_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(f"acct|aaa|message_0.db|{i}", "acct", "aaa", "wxid_testacct02", "message_0.db",
          i, 1000 + i, 1, 0, f"m{i}", 0) for i in range(1, 11)])
    con.commit()
    from agent_wechat import search
    rows, truncated = search.messages_in_range(
        "wxid_testacct02", account="acct", limit=3)
    assert truncated is True
    # **必须**是普通 dict，不是 `sqlite3.Row`：T6 的 `chatview.build` 要 dict
    # （`json.dumps` 序列化、`.get()`、`{**r}` 三样 `Row` 都不支持）。
    # 少了这条，把 `[dict(r) for r in ...]` 简化成 `list(reversed(...))` 会让本文件三条用例
    # **全绿**，而故障推迟到 T6 才爆（T5 审阅者实测：改完 3 passed）。
    assert type(rows[0]) is dict, "必须是 dict，不是 sqlite3.Row"
    assert [r["content"] for r in rows] == ["m8", "m9", "m10"]     # 正序，且是最新的三条


def test_messages_in_range_filters_and_reports_not_truncated(con):
    """**下界是含的**（`[lo, hi)`）：`after_ts=1005` 带回 ts=1005 那条。

    回归：把 `m.ts >= ?` 改成 `m.ts > ?`，或把 `before_ts` 的 `<` 改成 `<=`，这条会红。

    **初稿这条写错过，值得记下来**：原写 `after_ts=1004` 期望 `["m5","m6","m7"]`，
    那**只有独占下界才成立**（含下界会多带回 m4）。而独占下界与本仓三处已落地事实矛盾：
    `search.py:39` 同名参数用 `>=`、`usecases.py:460` 的月份筛选是
    `(m.ts >= ? AND m.ts < ?)`、`tests/test_media.py:143` 把 `month_bounds` 钉成
    `[lo, hi)`「下界含、相邻月首尾相接无缝隙」。**而 T6 把 `month_bounds` 的值直接喂给
    `after_ts`/`before_ts`** —— 照初稿写就是给 `chat export --months` 的**每月第一秒开一个
    静默数据丢失的缝**。（T5 实施者发现并拒绝改断言，标 `xfail(strict=True)` 请裁定。）
    """
    con.executemany(
        "INSERT INTO messages (msg_key, account, chat_md5, chat_id, shard, local_id,"
        " ts, msg_type, is_self, content, exported_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(f"acct|aaa|message_0.db|{i}", "acct", "aaa", "wxid_testacct02", "message_0.db",
          i, 1000 + i, 1, 0, f"m{i}", 0) for i in range(1, 11)])
    con.commit()
    from agent_wechat import search
    rows, truncated = search.messages_in_range(
        "wxid_testacct02", account="acct", after_ts=1005, before_ts=1008)
    assert [r["content"] for r in rows] == ["m5", "m6", "m7"]
    assert truncated is False


def test_messages_in_range_is_scoped_to_one_account_and_one_chat(con):
    """回归：删掉 `m.account = ?` 或 `m.chat_id = ?` 中**任一个**谓词，这条会红。

    （T5 交付时这两个谓词**都没有**夹具钉住：E1/E2 两个变异各自删掉一个，
    全套用例仍然全绿。见 task-5-report.md §3。）

    两条都必须钉：`messages` 是**多账号共用**的表（T4b 的跨账号错接就出在这上面），
    且同一账号下各会话也各是各的。夹具让三条行 **ts 全相同**，于是「谁被带回来」
    只由谓词决定，与 limit、与排序都无关。
    """
    con.executemany(
        "INSERT INTO messages (msg_key, account, chat_md5, chat_id, shard, local_id,"
        " ts, msg_type, is_self, content, exported_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [("acct|aaa|message_0.db|1", "acct", "aaa", "wxid_testacct02",
          "message_0.db", 1, 5000, 1, 0, "本会话", 0),
         ("other|aaa|message_0.db|1", "other", "aaa", "wxid_testacct02",
          "message_0.db", 1, 5000, 1, 0, "异账号同会话", 0),
         ("acct|bbb|message_0.db|1", "acct", "bbb", "wxid_otherchat",
          "message_0.db", 1, 5000, 1, 0, "同账号异会话", 0)])
    con.commit()
    from agent_wechat import search
    rows, truncated = search.messages_in_range("wxid_testacct02", account="acct")
    assert [r["content"] for r in rows] == ["本会话"]
    assert truncated is False   # truncated 的「假」这一半（修复轮后 filters_and_reports 也钉它）


# ---------------- T9：会话时间线导出（chat_export / chatview.write）----------------

class _StubReader:
    """collect_degradations 读 `rdr._drift`（必须给一个空 list，不能给 None），
    `_display_or_id` 读 `display_name` —— 两者都不碰微信库。"""
    _drift: list = []

    def __init__(self, display="测试群"):
        self._display = display

    def display_name(self, chat_id):
        return self._display


class _Ctx:
    """替掉 ChatContext：chat_export 只用 account / resolve / reader / degradations，
    四者都不碰微信库。"""

    def __init__(self, account=None, display="测试群"):
        self.account = account or "acct"
        self._r = _StubReader(display)

    @property
    def reader(self):
        return self._r

    def resolve(self, contact):
        return contact

    def resolve_optional(self, contact):
        return contact

    def degradations(self):
        return []


@pytest.fixture()
def chat_env(tmp_path, monkeypatch):
    """chat_export 的环境：桩掉 ChatContext 与 message_media。
    **`con` 夹具已把 `store.DB_PATH` 指向临时库，这里不碰真实 index.db、不碰微信目录。**
    """
    monkeypatch.setattr(usecases, "ChatContext", _Ctx)
    monkeypatch.setattr(
        usecases, "message_media",
        lambda *a, **k: usecases.MessageMediaResult(
            chat_id="wxid_testacct02", out_dir=str(tmp_path), states=[], counts={}))
    return tmp_path / "media"


_MSG_SQL = ("INSERT INTO messages (msg_key, account, chat_md5, chat_id, shard, local_id,"
            " ts, msg_type, is_self, content, exported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)")


def _seed(con, n=1):
    con.executemany(_MSG_SQL, [
        (f"acct|aaaa|message_0.db|{i}", "acct", "aaaa", "wxid_testacct02",
         "message_0.db", i, 100 + i, 1, 0, f"第{i}条", 0) for i in range(1, n + 1)])
    con.commit()


def _html(res):
    """产物里的 HTML 那一份（不靠 `files[0]` 的次序：那由 formats 的次序决定）。"""
    return pathlib.Path(next(f for f in res.files if f.endswith(".html"))).read_text(
        encoding="utf-8")


def test_chat_export_writes_both_files_under_the_chat_media_tree(con, chat_env):
    """落盘布局：{out}/{chat_md5}/chat/index.{html,md} —— 与 media export 同根。"""
    _seed(con)
    res = usecases.chat_export("wxid_testacct02", out_dir=str(chat_env), account="acct")
    assert sorted(pathlib.Path(f).name for f in res.files) == ["index.html", "index.md"]
    for f in res.files:
        p = pathlib.Path(f)
        assert p.is_file() and p.parent == chat_env / "aaaa" / "chat"
    assert "第1条" in (chat_env / "aaaa" / "chat" / "index.html").read_text(encoding="utf-8")


def test_chat_export_degrades_loudly_when_msg_parts_is_empty(con, chat_env):
    """回归：**未回填**必须与「这些消息本来就没有可读内容」印得不一样（丙 R23 同形）。
    把 `count_msg_parts == 0` 那段降级公告删掉，这条断言会红。"""
    _seed(con)
    res = usecases.chat_export("wxid_testacct02", out_dir=str(chat_env), account="acct")
    caps = {d.capability for d in res.degraded}
    assert any("msg_parts" in c for c in caps), f"未回填必须公告，实际 degraded={caps}"


def test_chat_export_counts_unparsed_and_rejected_separately(con, chat_env):
    """解码收敛到 `usecases._decode_part`（`message_parts` 用的是**同一个**）之后，
    chat_export 的口径**一字不变**：解压失败与 XML 解不开合计 `unparsed`，命中危险载荷守卫
    计 `rejected`，两者分开（spec §5）。

    三条载荷都是**别人写进去的字节**（回填路径不落库解析失败的行）：索引库可被手工改动、
    半拷贝的 WAL 会落下半页——与 `_decode_part` 的注释同源。
    回归（验收点）：把三分类并成两类（`except zlib.error` 那一支也算 rejected，或两个计数
    同增），这条会红——**chat_export 一侧的口径闸**，与
    `tests/test_mcp_server.py::test_get_message_parts_counts_unparsed_and_rejected_separately` 配对。
    """
    _seed(con, n=3)
    store.upsert_msg_parts(con, [
        ("acct|aaaa|message_0.db|1", 49, None, None,
         zlib.compress(b"<msg><unclosed></msg>", 6)),                    # XML 解不开
        ("acct|aaaa|message_0.db|2", 49, None, None,
         zlib.compress(b"<!DOCTYPE x [<!ENTITY a 'b'>]><msg/>", 6)),     # 危险载荷
        ("acct|aaaa|message_0.db|3", 49, None, None, b"\x00\x01 not-zlib"),  # 解压就失败
    ])
    con.commit()
    res = usecases.chat_export("wxid_testacct02", out_dir=str(chat_env), account="acct")
    assert (res.unparsed, res.rejected) == (2, 1)


_TRANSFER_XML = (
    "<msg><appmsg><title>转账</title><type>2000</type><wcpayinfo>"
    "<feedesc>￥1.00</feedesc><pay_memo>合成备注</pay_memo>"
    "<begintransfertime>99999999999999999999</begintransfertime>"
    "<effectivedate>not-a-number</effectivedate>"
    "<invalidtime>-99999999999999999999</invalidtime>"
    "</wcpayinfo></appmsg></msg>")


def test_chat_export_survives_an_out_of_range_timestamp(con, chat_env):
    """**载荷对手可控**（任何人可以给你发一条 appmsg）：`<begintransfertime>` 只要求是数字，
    `_to_int` 于是放行 `99999999999999999999`，而 `chat_text.when` 的
    `datetime.fromtimestamp` 会抛裸 `OverflowError`。

    后果不是「少印一个日期」：它在 core 层无人接住（`chatview.write` 只接 `OSError`、
    `cli.py` 只接 `AgentWeChatError`），CLI 打的是 traceback（违反 USAGE.md §9 的
    「单行中文」），MCP 回「内部错误」。**两个模板都要过**——它们各自调 `when`，只修一侧
    这条仍会红。

    消息自身的 `ts` 走**同一条**路径（`chat_html` 的 `.when` / `chat_md` 的标题行），
    所以那条也一并压上 —— 用 `2**62`（SQLite 存得下、`fromtimestamp` 抛 `OSError`），
    与载荷那条（Python 整数可以更大，抛 `OverflowError`）一起覆盖三类异常里的两类；
    `ValueError` 是第三种（`253402300800` = 公元 10000 年）。

    回归：把 `when` 里的 `try/except` 去掉（或收窄成只接 `ValueError`）→ 本用例红。
    """
    con.execute(_MSG_SQL, ("acct|aaaa|message_0.db|1", "acct", "aaaa",
                           "wxid_testacct02", "message_0.db", 1, 2 ** 62,
                           49, 0, "[appmsg: 转账]", 0))
    con.commit()
    store.upsert_msg_parts(con, [("acct|aaaa|message_0.db|1", 2000, None, None,
                                  zlib.compress(_TRANSFER_XML.encode(), 6))])
    con.commit()
    res = usecases.chat_export("wxid_testacct02", out_dir=str(chat_env), account="acct")
    assert sorted(pathlib.Path(f).name for f in res.files) == ["index.html", "index.md"]
    for f in res.files:
        out = pathlib.Path(f).read_text(encoding="utf-8")
        assert "99999999999999999999" not in out, "超范围的时间戳原样进了产物"
        assert "合成备注" in out, "整块转账信息被丢弃了 —— 要的是「印不出来就空着」"
        # 标签在、值是空串：`when` 返回 "" 而不是编一个日期，也不是把整行删掉。
        assert "发起时间：" in out


def test_chat_export_reports_no_degradation_once_backfilled(con, chat_env):
    """反向守卫：回填之后同一路径不该再报降级 —— 防「降级公告写成了常量」。"""
    _seed(con)
    store.upsert_msg_parts(con, [("acct|aaaa|message_0.db|1", 5, None, None, b"<msg/>")])
    con.commit()
    res = usecases.chat_export("wxid_testacct02", out_dir=str(chat_env), account="acct")
    assert not any("msg_parts" in d.capability for d in res.degraded)


def test_chat_export_propagates_truncated_into_the_artifact(con, chat_env):
    """回归：`truncated` 只回终端、产物顶部不印 —— 这条断言读的是**产物文本**。"""
    _seed(con, n=10)
    res = usecases.chat_export("wxid_testacct02", limit=3, out_dir=str(chat_env),
                               account="acct")
    assert res.truncated is True
    text = pathlib.Path(res.files[0]).read_text(encoding="utf-8")
    assert "截断" in text


# ---------------- 以下是 brief 之外补的夹具（每条 docstring 都写明怎么红）----------------

def test_chat_export_parses_msg_parts_and_links_the_quote_anchor(con, chat_env):
    """跨任务接口（T6 实施者发现、控制器钉死）：喂给 `chatview.build` 的 `parts` 必须是
    **已解析的 `AppMsg` 映射**，不是 `msg_parts_by_keys` 的原始四元组；`refs` 取四元组的
    **第三项**（`ref_msg_key`），`None` 的不进表（`build` 用「在不在表里」判退化）。

    夹具里 `raw_xml` 按 `appmsg_rebuild_parts` 的**真实形态**写入（zlib + 规范化 XML），
    两条消息都落在本次范围内。回归（任一 → 这条会红）：
      · 删掉 `zlib.decompress`（把压缩字节当 XML 喂 `parse`）→ 没有 appmsg 块：
        判别串是 `引用：同意`（结构化标题，只有块里才有）与 `昨天说的那件事`，
        以及 `<a href="#` —— 三者一起消失。
        **`同意` 不是判别串**：`content` 兜底文案 `[appmsg: 同意]` 里也有它（初稿这条
        docstring 把 `同意` 当成了闸，是假的）；
      · 把 `parts` 传成原始四元组 → `ViewMsg.appmsg` 不是 `AppMsg`，模板取 `.kind` 时崩；
      · `refs` 误取 `v[1]`（`ref_svrid`）→ 值是整数 1001，`in_range` 里没有它，
        `<a href="#` 消失。
    **`refs` 里那个 `if v[2]` 这条夹具杀不掉**：`chatview.build` 是拿
    `target in in_range` 判的，`None` 落进去与「键不在表里」等价（都是 None）。
    它是无区分度的防御性写法，照 brief 留着，但不假装它有闸。
    """
    xml = ('<msg><appmsg><title>同意</title><type>57</type><refermsg>'
           '<displayname>张三</displayname><content>昨天说的那件事</content>'
           '<svrid>1001</svrid></refermsg></appmsg></msg>')
    con.executemany(_MSG_SQL, [
        ("acct|aaaa|message_0.db|1", "acct", "aaaa", "wxid_testacct02",
         "message_0.db", 1, 1001, 1, 0, "被引用的原文", 0),
        ("acct|aaaa|message_0.db|2", "acct", "aaaa", "wxid_testacct02",
         "message_0.db", 2, 1002, 49, 0, "[appmsg: 同意]", 0)])
    con.commit()
    store.upsert_msg_parts(con, [("acct|aaaa|message_0.db|2", 57, 1001,
                                  "acct|aaaa|message_0.db|1",
                                  zlib.compress(xml.encode(), 6))])
    con.commit()
    res = usecases.chat_export("wxid_testacct02", out_dir=str(chat_env), account="acct")
    text = _html(res)
    # 判别串是**结构化标题**，不是 `同意`：`content` 的兜底文案 `[appmsg: 同意]` 里也有后者
    assert "引用：同意" in text
    assert "昨天说的那件事" in text               # 引用原文：渲染不依赖回指成功
    assert '<a href="#' in text                  # 回指命中 → 整块可点跳
    # 解析计数：这一条**成功解析**，不该被算成解不开或拒绝
    assert (res.unparsed, res.rejected) == (0, 0)


def test_chat_export_resolves_a_relative_out_dir(con, chat_env, tmp_path, monkeypatch):
    """`--out 相对路径` 必须绝对化：产物的 `../image/…` 是拿 `view.out_dir` 与媒体状态的
    **绝对**路径算 `os.path.relpath` 得来的（`chat_text.media_src`）。不绝对化时，
    `relpath('/…/media/aaaa/image/x.jpg', 'media/aaaa')` 按 **cwd** 展开左操作数
    ——cwd 一变，`src` 就悄悄错（`<img>` 照常渲染、打不开）。

    删除 `chat_export` 里对输出根的 `.resolve()` → 第一条断言红（`out_dir` 变相对路径）。
    第二条断言钉的是「相对引用本身」这个契约（本夹具的 cwd 与产物同根，故它杀不掉
    `.resolve()`，只钉住 `media_prefix` 那一半）。
    """
    monkeypatch.chdir(tmp_path)
    abs_img = tmp_path / "media" / "aaaa" / "image" / "2026-09" / "x.jpg"
    monkeypatch.setattr(
        usecases, "message_media",
        lambda *a, **k: usecases.MessageMediaResult(
            chat_id="wxid_testacct02", out_dir=str(tmp_path),
            states=[usecases.MediaRefState(
                msg_key="acct|aaaa|message_0.db|1", ts=101, kind="image",
                status="exported", path=str(abs_img))],
            counts={}))
    _seed(con)
    res = usecases.chat_export("wxid_testacct02", out_dir="media", account="acct")
    assert pathlib.Path(res.out_dir).is_absolute()
    assert 'src="../image/2026-09/x.jpg"' in _html(res)


def test_chat_export_clamps_a_negative_limit(con, chat_env):
    """`limit=-1` 在 SQLite 里是**不限量**，不是「一条都不要」（T5 把区间校验判给调用方）。

    删掉 `clamp_limit`（把 -1 直通 `messages_in_range`）→ 三条全进来、
    `truncated` 变 False（`len(rows) == -1` 恒假），两条断言同时红。
    """
    _seed(con, n=3)
    res = usecases.chat_export("wxid_testacct02", limit=-1, out_dir=str(chat_env),
                               account="acct")
    assert res.truncated is True
    text = _html(res)
    assert "第3条" in text and "第1条" not in text


@pytest.mark.parametrize("bad", ["2026-9", "2026-13", "上个月", "9999-12", "0000-01"])
def test_chat_export_rejects_a_malformed_month(con, chat_env, bad):
    """月份格式错必须**响亮报错**，不能静默 0 命中（`media export` / `moments export`
    同一口径）。前三个参数让 `MONTH_RE` 直接失配；**后两个（`9999-12` / `0000-01`）逐字
    符合格式**，却落在 `datetime` 的可表示区间之外——拦它们的是 `media.month_bounds`
    里的翻译层，而不是这段格式校验。

    判据是「逃出来的是 `SearchInputError`」（CLI 印单行中文、MCP 回带指引的信封），
    不是「谁拦的」：任一层漏掉，逃出 core 层的就不再是这个类型，这条红。
    """
    _seed(con)
    with pytest.raises(errors.SearchInputError):
        usecases.chat_export("wxid_testacct02", months=[bad], out_dir=str(chat_env),
                             account="acct")


def test_chat_export_months_span_becomes_the_range_window(con, chat_env):
    """`--months` → `[min(lo), max(hi))` 的窗口（`media.month_bounds` 是 `[lo, hi)`，
    下界含、上界不含——衔接处不能开缝）。夹具的 ts 是 101/102/103 秒（1970 年），
    故窗口取 1970-01 能带回全部三条、取 1970-02 一条都带不回。

    把 `after_ts`/`before_ts` 传反（或漏传 `before_ts`）→ 第一条断言红；
    漏传 `after_ts` → 第二条断言红。
    """
    _seed(con, n=3)
    jan = usecases.chat_export("wxid_testacct02", months=["1970-01"], out_dir=str(chat_env),
                               account="acct")
    assert len(jan.files) == 2 and "第3条" in _html(jan)
    feb = usecases.chat_export("wxid_testacct02", months=["1970-02"], out_dir=str(chat_env),
                               account="acct")
    assert "第1条" not in _html(feb) and "第3条" not in _html(feb)


def test_chat_export_rejects_an_unknown_format(con, chat_env):
    """未知格式必须报错：不拦就是「写了 0 个文件、退出码 0」——用户拿到一条成功消息
    却没有任何产物，与「导出成功但内容为空」长得一模一样。

    删掉 `chatview.write` 里的格式校验（改成 `_RENDERERS.get(fmt)` 静默跳过）→ 这条红。
    """
    _seed(con)
    with pytest.raises(errors.SearchInputError):
        usecases.chat_export("wxid_testacct02", formats=("pdf",), out_dir=str(chat_env),
                             account="acct")


def test_chat_export_announces_dropped_media_states(con, chat_env, monkeypatch):
    """媒体状态没覆盖到的消息必须**转述到产物顶部**：它们在产物里与「这条消息本来
    没有媒体」印得一模一样（丙 R23 同形）。

    **判据是「答回的条数 < keys 的条数」，不是 `MessageMediaResult.truncated`**：keys 路径下
    `message_media` 是「一个 key 一行」（非媒体消息也在里面、状态为 `none`），于是
    `truncated`（= `len(rows) >= lim`）在「limit 恰好等于 key 数」时**恒为真** —— 照它公告
    就是每次导出都报一次假警（真机实测：`--limit 200` 时 `states` 与 `keys` 等长，终端却印
    「媒体状态可能被截断」）。故夹具把 `truncated` 钉成 False、`states` 钉成空表：
    换回 `mm.truncated` 判据 → 这条红；删掉这段公告 → 也红。
    """
    monkeypatch.setattr(
        usecases, "message_media",
        lambda *a, **k: usecases.MessageMediaResult(
            chat_id="wxid_testacct02", out_dir="", states=[], counts={}, truncated=False))
    _seed(con, n=3)
    res = usecases.chat_export("wxid_testacct02", out_dir=str(chat_env), account="acct")
    assert any("媒体状态" in d.effect for d in res.degraded), res.degraded
    assert "媒体状态" in _html(res)          # 顶部也印（与 truncated/unparsed 同一位置）


def test_chat_export_title_is_the_display_name(con, chat_env):
    """产物标题必须是**展示名**，不是裸 chat_id：这是本功能第一个给人看的产物，
    `<title>` 与页首那行都印同一份 `view.title`（`_display_or_id`，与 recent/search 同源）。

    把 `title=_display_or_id(ctx, chat_id)` 改回 `title=chat_id` → 两条断言同时红。
    夹具的 `_StubReader.display_name` 恒返回「测试群」，与 chat_id 不同 —— 所以
    「取到展示名」与「取到 chat_id」在产物里是可分辨的。
    """
    _seed(con)
    res = usecases.chat_export("wxid_testacct02", out_dir=str(chat_env), account="acct")
    text = _html(res)
    assert "<title>测试群</title>" in text
    assert "会话 <b>测试群</b>" in text           # 页首那行读的是同一个 view.title
    # MD 侧同一份标题（`chat_text` 之外没有第二份措辞）
    md = pathlib.Path(next(f for f in res.files if f.endswith(".md"))).read_text("utf-8")
    assert "测试群" in md.splitlines()[0]


def test_chat_export_rejects_non_contiguous_months(con, chat_env):
    """`--months` 必须**连续**：`messages_in_range` 只有一对边界，非连续月份只能取包络
    ——`2026-08,2026-10` 会把 9 月也带进来，而 `limit` / `truncated` 还按放大后的窗口算，
    **产物上完全看不出多导了一个月**（静默放大窗口比报错更坏，与「格式不符不静默 0 命中」
    同源）。兄弟命令 `media export` 是逐月 OR，非连续能正确表达 —— 那两处不动。

    删掉连续性校验 → 这条红（当前实现会静默跑完并返回产物）。下半段钉住「连续月份照常工作」，
    免得把闸写成了「多个月份一律拒绝」。
    """
    _seed(con, n=3)
    with pytest.raises(errors.SearchInputError):
        usecases.chat_export("wxid_testacct02", months=["2026-08", "2026-10"],
                             out_dir=str(chat_env), account="acct")
    ok = usecases.chat_export("wxid_testacct02", months=["1970-01", "1970-02"],
                              out_dir=str(chat_env), account="acct")
    assert "第3条" in _html(ok) and len(ok.files) == 2


def test_chat_export_rejects_descending_months(con, chat_env):
    """反序月份必须拒 —— **理由不是「不连续」**：`["1970-02","1970-01"]` 这两个月本身首尾相接。

    真实的理由是**顺序**：实现用 `bounds[0][0]` 与 `bounds[-1][1]` 取窗口，反序会取到
    退化/倒置的窗口（算过：`["1970-02","1970-01"]` → `[feb_lo, feb_lo)` 空区间；
    `["1970-03","1970-01"]` → `after > before`），`messages_in_range` 两种情况都返回
    **0 行且 `truncated=False`** —— 一个没有任何信号的空产物。

    这条单独立着，是因为下一个人很可能把校验「简化」成**先排序再判集合是否连续**：
    那样反序会被放行、窗口照样坏，而 `test_..._non_contiguous_months` 那条（只测缺口）
    照样全绿。删掉顺序要求 / 改成排序后判连续 → 这条红；把错误信息里的「按时间升序」
    改成只说「连续」→ 第二条断言红（诊断说错了原因，下一个人就会照着它去「简化」）。
    """
    _seed(con, n=3)
    with pytest.raises(errors.SearchInputError) as ei:
        usecases.chat_export("wxid_testacct02", months=["1970-02", "1970-01"],
                             out_dir=str(chat_env), account="acct")
    # 诊断必须点名「升序」，否则「连续」这个说法会把人引向排序式简化
    assert "升序" in str(ei.value), str(ei.value)
    # 三个月反序（真·倒置窗口）同样拒
    with pytest.raises(errors.SearchInputError):
        usecases.chat_export("wxid_testacct02", months=["1970-03", "1970-01"],
                             out_dir=str(chat_env), account="acct")


def test_chat_export_rejects_an_empty_format_list(con, chat_env):
    """`--format ""` 在 CLI 里过滤空项后成为空表：不能「零个产物 + exit 0」——
    那与「格式名写错了」在「产物列表短了一截」上长得一模一样
    （`chatview.write` 的注释正是这么写的，而空表此前能绕过那道闸）。

    删掉空表校验 → 这条红（返回 `[]`、不抛，还白建一级目录）。
    第二条断言钉住**校验发生在建目录之前**：失败的一次调用不该在 `--out` 下留空目录。
    """
    _seed(con)
    with pytest.raises(errors.SearchInputError):
        usecases.chat_export("wxid_testacct02", formats=(), out_dir=str(chat_env),
                             account="acct")
    assert not (chat_env / "aaaa" / "chat").exists()


def test_chat_export_reports_only_the_files_it_actually_wrote(con, chat_env, monkeypatch):
    """`files` 只装**真正落盘**的产物：单文件写失败（磁盘满 / 权限 / 只读挂载）不进列表 ——
    「列表短了一截」本身就是信号，而不是让调用方以为两份都写成了。

    这条是 F58.2 的行为面：`chatview.write` 的 docstring 原写「写失败返回 None 并 continue」
    （brief 逐字带进来的），而函数返回 `list[Path]`、从不返回 None —— 改成如实之后，
    必须有一条夹具钉住**那个 `continue`**。
    把 `except OSError: continue` 改成 `except OSError: pass`（把没写成的路径也 append）→ 这条红。
    """
    _seed(con)
    real = pathlib.Path.write_text

    def fake(self, data, *a, **kw):
        if str(self).endswith("index.md"):
            raise OSError(28, "No space left on device", str(self))   # ENOSPC
        return real(self, data, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "write_text", fake)
    res = usecases.chat_export("wxid_testacct02", out_dir=str(chat_env), account="acct")
    assert sorted(pathlib.Path(f).name for f in res.files) == ["index.html"]
    assert not (chat_env / "aaaa" / "chat" / "index.md").exists()
    assert "第1条" in _html(res)          # 写成了的那份照常可用


# ---------------- T10：索引页（chat index）----------------

def test_chat_index_links_to_each_exported_chat(con, chat_env):
    """回归：索引页必须**链到产物**（相对路径）—— 只列名字不给链接，**读产物那条**会红
    （不是 `res.chats`：它只数条目，与链不链无关。T10 实施者指出原 docstring 指错了断言）。"""
    _seed(con)
    usecases.chat_export("wxid_testacct02", out_dir=str(chat_env), account="acct")
    res = usecases.chat_index(out_dir=str(chat_env), account="acct")
    assert res.chats == 1
    page = pathlib.Path(res.file).read_text(encoding="utf-8")
    assert "aaaa/chat/index.html" in page


def test_chat_index_on_empty_out_dir_writes_nothing(tmp_path):
    """没有产物时：不抛异常、**也不写一个空壳页面**（空产物比没有产物更难分辨）。"""
    res = usecases.chat_index(out_dir=str(tmp_path / "nothing"), account="acct")
    assert res.chats == 0 and res.file is None
    # 上面那两条只钉住「返回值的说法」；**这一条才钉住「一个字节都没写」**：
    # 写一张空页面却回 `file=None`（自相矛盾的实现）此前能全绿。
    assert not (tmp_path / "nothing").exists(), "空目录被建出来了"


def test_chat_index_reads_label_and_count_from_the_index_scoped_to_one_account(con, chat_env):
    """会话名与条数来自**一次查库**（`GROUP BY chat_md5`），且只数本账号的行。

    两条断言各杀一个变异：
      · `assert "3 条"` —— 删掉 `WHERE account = ?`：`messages` 是**多账号共用**的表，
        异账号的 97 行会并进同一个 `aaaa` 分组，条数变成 3+97=100 → 红。
        （异账号行数**故意取 97**：100 的十进制写法里没有 "3"，不会与 "3 条" 撞子串。）
      · `assert "wxid_testacct02"` —— 标签必须来自查库结果，而不是一律走 `md5:` 回落；
        把查询结果丢掉、标签恒为 `md5:aaaa` → 红。
    """
    _seed(con, n=3)
    con.executemany(_MSG_SQL, [
        (f"other|aaaa|message_0.db|{i}", "other", "aaaa", "wxid_otheracct",
         "message_0.db", i, 100 + i, 1, 0, f"异账号第{i}条", 0) for i in range(1, 98)])
    con.commit()
    usecases.chat_export("wxid_testacct02", out_dir=str(chat_env), account="acct")
    res = usecases.chat_index(out_dir=str(chat_env), account="acct")
    page = pathlib.Path(res.file).read_text(encoding="utf-8")
    assert "3 条" in page, "条数不是本账号那 3 条（账号谓词丢了？）"
    assert "wxid_testacct02" in page, "标签没有取查库结果"


def test_chat_index_falls_back_to_the_degraded_md5_label(con, chat_env):
    """目录里有产物、索引里没有这个会话（重建过索引 / purge 过 index.db）：
    标签回落成 `md5:{chat_md5}` —— 与 `messages.degraded_chat_id` **同形**，
    不自造第三种写法；条数印「未知」而**不是 0 条**（0 是一条确定的信息，这里我们只是不知道）。

    三条断言各杀一个变异：
      · 删掉 `degraded_chat_id` 回落（换成空串或自造形态）→ 第一条红；
      · 模板把「未知」那支删掉、一律印 `{messages} 条` → 第二条红（那会印成 `0 条`）；
      · 无脑 `int` 出一个条数（比如拿 `len(entries)`）→ 还是 `0 条`，第二条红。
    """
    _seed(con)
    usecases.chat_export("wxid_testacct02", out_dir=str(chat_env), account="acct")
    con.execute("DELETE FROM messages")       # 索引没了，产物还在
    con.commit()
    res = usecases.chat_index(out_dir=str(chat_env), account="acct")
    assert res.chats == 1, "产物在，就该照样列出来（查不到索引不等于没导过）"
    page = pathlib.Path(res.file).read_text(encoding="utf-8")
    assert messages.degraded_chat_id("aaaa") in page, "回落形态与 messages.degraded_chat_id 不同形"
    assert "条数未知" in page and "0 条" not in page


def test_chat_index_links_the_markdown_only_when_it_was_written(con, chat_env):
    """`has_md` = 产物目录里**真的**有 `index.md`（`chatview.write` 单文件写失败时只留 HTML）。

    两条断言各杀一个变异：
      · `has_md` 恒 True（无条件印 md 链接）→ 第一条红：`--format html` 时那是个**死链**；
      · `has_md` 恒 False（`(p.parent / "index.md").exists()` 这行被删）→ 第二条红。
    """
    _seed(con)
    usecases.chat_export("wxid_testacct02", formats=("html",), out_dir=str(chat_env),
                         account="acct")
    res = usecases.chat_index(out_dir=str(chat_env), account="acct")
    assert "aaaa/chat/index.md" not in pathlib.Path(res.file).read_text(encoding="utf-8")
    usecases.chat_export("wxid_testacct02", formats=("html", "md"),
                         out_dir=str(chat_env), account="acct")
    res = usecases.chat_index(out_dir=str(chat_env), account="acct")
    assert "aaaa/chat/index.md" in pathlib.Path(res.file).read_text(encoding="utf-8")


def test_chat_index_escapes_the_label_it_reads_from_the_index(con, chat_env):
    """`chat_id` 是从索引库读出来的文本 —— 与 `chat_html` 同一立场
    （消息与名字都可能是对手可控的），一律走 `html.escape`。

    把模板里的 `_esc(e.label)` 换成裸 `e.label` → 第一条断言红（`<script>` 原样进产物）。

    夹具：导出**之后**才把那条消息的 `chat_id` 改成脚本串 —— 产物目录名（`chat_md5`）
    仍是 `aaaa`，枚举照常命中，改的只是「页面上要印的那个名字」。
    """
    _seed(con)
    usecases.chat_export("wxid_testacct02", out_dir=str(chat_env), account="acct")
    con.execute("UPDATE messages SET chat_id=?", ("<script>alert(1)</script>",))
    con.commit()
    res = usecases.chat_index(out_dir=str(chat_env), account="acct")
    page = pathlib.Path(res.file).read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_chat_index_reports_a_write_failure_as_a_project_error(con, chat_env, monkeypatch):
    """写不进去时逃出来的必须是 `AgentWeChatError` 子类，**不是裸 `OSError`**（core 层的错误契约）。

    删掉 `except OSError → MediaOutputError` 那段 → 逃出来的是裸 `OSError`（磁盘满/只读挂载
    时用户看到 traceback），这条红。手法照 `test_chat_export_reports_only_the_files_it_actually_wrote`
    那一半：单文件写失败。**索引页只有一个产物，故这里不沿用 `chatview.write` 的「吞掉、不进列表」**
    ——吞掉就是「退出码 0、什么都没有」，同 `moments_export` 的取舍。
    """
    _seed(con)
    usecases.chat_export("wxid_testacct02", out_dir=str(chat_env), account="acct")

    def boom(self, *a, **kw):                     # 补丁在 chat_export 之后才打
        raise OSError(28, "No space left on device", str(self))

    monkeypatch.setattr(pathlib.Path, "write_text", boom)
    with pytest.raises(errors.MediaOutputError):
        usecases.chat_index(out_dir=str(chat_env), account="acct")


# ---------------- 两个新 CLI 命令的线级用例 ----------------
#
# 此前 `grep -rn '"chat", "export"\|"appmsg", "rebuild"' tests/` **零命中**：`chat index`
# 有两条线级用例，而这两个（`cli.py` 的 `cmd_chat_export` / `cmd_appmsg_rebuild`）一条都没有
# —— argparse 的属性名、拆表、progress 回调、stderr 那几行全在网外。

def test_chat_export_cli_splits_months_and_format_and_prints_artifacts(con, chat_env,
                                                                      capsys):
    """`chat export` 的线级路径：`--months` / `--format` 的**拆表与去空白**、产物逐条印出。

    `--format` 是**单数**属性名（`args.format`），`--months` 才是复数——写反了 argparse 会
    直接 AttributeError。两条参数都**带空白**，所以 `.strip()` 那一步是载荷的一部分：
    去掉它，`MONTH_RE.fullmatch(" 1970-01 ")` 失配 → `SearchInputError` → 退出码 1，本用例红。

    产物逐条印出（`res.files` 的口径：**只印真正落盘的**）——`--format html` 时不能出现
    `index.md`。
    """
    from agent_wechat import cli
    _seed(con, n=3)
    with pytest.raises(SystemExit) as ei:
        cli.main(["chat", "export", "--chat", "wxid_testacct02",
                  "--months", " 1970-01 , 1970-02 ", "--format", "html",
                  "--out", str(chat_env), "--account", "acct"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "会话 wxid_testacct02   3 条" in out
    assert f"产物：{chat_env}/aaaa/chat/index.html" in out
    assert "index.md" not in out, "只点了 html，却印了 md 的路径（拆表或 files 口径错了）"


def test_chat_export_cli_warns_on_stderr_when_truncated(con, chat_env, capsys):
    """触顶那一行走 **stderr**（stdout 留给产物路径，管道里不被污染），且**退出码仍是 0**
    ——截断是提示不是失败。

    产物顶部**也会**印（spec §4.5）：这里顺带把两侧一起钉住，免得下一个人照着
    「只回终端不印产物」的旧注释把产物那一半删掉。
    """
    from agent_wechat import cli
    _seed(con, n=3)
    with pytest.raises(SystemExit) as ei:
        cli.main(["chat", "export", "--chat", "wxid_testacct02", "--limit", "1",
                  "--out", str(chat_env), "--account", "acct"])
    assert ei.value.code == 0
    cap = capsys.readouterr()
    assert "触及 --limit" in cap.err and "触及 --limit" not in cap.out
    # 产物那一侧用的是 `chat_text.TRUNCATED_NOTE` 的措辞（**不是**终端这一句）：两处都印，
    # 但各说各的话，所以这里不能拿终端的字串去产物里找。
    page = (chat_env / "aaaa" / "chat" / "index.html").read_text(encoding="utf-8")
    assert "本次导出被 limit 截断" in page, "产物顶部没印截断提示（spec §4.5）"


def test_appmsg_rebuild_cli_prints_the_two_denominators_and_progress(con, monkeypatch,
                                                                     capsys):
    """`appmsg rebuild` 的线级路径：`--account` 的属性名、四组计数、以及 `progress` 回调
    真的被接上了（`cmd_appmsg_rebuild` 里那个局部函数——删掉它，终端在 40 秒里一片安静）。

    计数有两组**不同的分母**（见 `usecases.appmsg_rebuild_parts` 的 docstring）：`parts`
    是本轮写入，`resolved` / `unresolved` 是该账号整表。这条用例两者都印出来，顺带钉住
    它们**不会被并成一句**（并了就看不出 1 与 2 各是谁）。
    """
    from agent_wechat import cli
    rows = [
        (1001, "acct|aaa|message_0.db|1", None),                      # 被引用的原文
        (1002, "acct|aaa|message_0.db|2",
         '<msg><appmsg><title>同意</title><type>57</type><refermsg>'
         '<svrid>1001</svrid><content>明天开会</content></refermsg></appmsg></msg>'),
    ]
    monkeypatch.setattr(usecases, "ChatContext", lambda acct=None: type(
        "C", (), {"account": "acct", "reader": _FakeReader(rows)})())
    # `flush` 只在攒够 `store.BATCH` 行时才报进度，而真机的 BATCH 是 2000 —— 夹具那两行
    # 永远攒不满，progress 一次也不会被调用（这一条正是在**没有** patch 时红的）。
    # 调小它只是让「回调接没接上」可观测，不改变任何计数。
    monkeypatch.setattr(store, "BATCH", 1)
    with pytest.raises(SystemExit) as ei:
        cli.main(["appmsg", "rebuild", "--account", "acct"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "本轮写入 msg_parts 1 条" in out
    assert "回指解析 1 / 未解析 0" in out
    assert "拒绝 0；解不开 0" in out
    assert "已回填 1 条 appmsg" in out, "progress 回调没接上（40 秒静默）"


def test_chat_index_cli_on_empty_out_dir_is_not_an_error(tmp_path, capsys):
    """一个产物都没有时：印提示、**退出码 0**（这不是错误）。

    删掉 `cmd_chat_index` 里 `file is None` 那条分支（改成抛错、或什么都不印）→ 两条断言红。

    本用例**没有任何 WeChat 夹具**：`--out` 指向不存在的目录，实现必须在解析账号 /
    开索引库**之前**就返回，否则这里会去碰真实的微信容器/索引库。
    """
    from agent_wechat import cli
    with pytest.raises(SystemExit) as ei:
        cli.main(["chat", "index", "--out", str(tmp_path / "nothing"), "--account", "acct"])
    assert ei.value.code == 0, "「还没有导出过」不是错误"
    out = capsys.readouterr().out
    assert "没有已导出的会话" in out and "chat export" in out


def test_chat_index_cli_prints_the_artifact_path(con, chat_env, capsys):
    """成功路径：条数与产物路径都要印出来（用户照着打开）。

    删掉 `print(f"产物：{res.file}")` → 第二条断言红。
    """
    from agent_wechat import cli
    _seed(con)
    usecases.chat_export("wxid_testacct02", out_dir=str(chat_env), account="acct")
    with pytest.raises(SystemExit) as ei:
        cli.main(["chat", "index", "--out", str(chat_env), "--account", "acct"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "已导出的会话：1 个" in out
    assert f"产物：{chat_env}/index.html" in out
