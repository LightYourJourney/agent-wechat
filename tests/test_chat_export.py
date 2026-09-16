# tests/test_chat_export.py
"""甲的回填与导出的用例层测试。夹具按 spec §2.5 的实测形态构造，内容全部合成。"""
import pathlib
import zlib

import pytest

from agent_wechat import errors, store, usecases


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
    """collect_degradations 读 `rdr._drift`，故这里必须给一个空 list，不能给 None。"""
    _drift: list = []


class _Ctx:
    """替掉 ChatContext：chat_export 只用 account / resolve / reader / degradations，
    四者都不碰微信库。"""

    def __init__(self, account=None):
        self.account = account or "acct"
        self._r = _StubReader()

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
      · 删掉 `zlib.decompress`（把压缩字节当 XML 喂 `parse`）→ 没有 appmsg 块，
        产物里只剩 `[appmsg: 同意]` 这行提示文字，`引用`/`同意` 都不出现；
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
    assert "同意" in text                        # 结构化标题（不是 [appmsg: 同意] 提示）
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


@pytest.mark.parametrize("bad", ["2026-9", "2026-13", "上个月"])
def test_chat_export_rejects_a_malformed_month(con, chat_env, bad):
    """月份格式错必须**响亮报错**，不能静默 0 命中（`media export` / `moments export`
    同一口径）。三个参数各自都能让 `media.month_bounds` 抛裸 `ValueError`
    （`int("")` / `int("13")` 越界 / 切片不成形）——删掉那段校验，逃出 core 层的就不是
    `SearchInputError` 了，这条红。
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
