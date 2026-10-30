"""丙 · message_media 的四态判定。

夹具形态依据 spec §2 的实测，不是「觉得应该长什么样」：
- 一条消息至多一份媒体（44,303 条实测 ≥2 个 hex 的有 0 条）
- 媒体消息**可能没有 ref**（索引内 image 37 / video 222 条）→ none 必须由 msg_type 判
- VoiceInfo.local_id 与 messages.local_id 同空间
"""
from __future__ import annotations

import datetime
import pathlib

import pytest

from agent_wechat import errors, media, messages, paths, store, usecases

TS = int(datetime.datetime(2026, 9, 15, 12, 0).timestamp())
MON = datetime.datetime.fromtimestamp(TS).strftime("%Y-%m")
CM = messages.md5_of("wxid_x")     # 与 _FakeCtx.resolve 的 chat_id 同源：生产里 chat_md5 就是它的 md5
ACCT = "wxid_testacct01_ab12"


def _row(local_id, msg_type, ts=TS):
    return {"msg_key": messages.row_key(ACCT, CM, "message_0.db", local_id),
            "account": ACCT, "chat_md5": CM, "chat_id": "wxid_x",
            "shard": "message_0.db", "local_id": local_id, "server_id": None,
            "ts": ts, "msg_type": msg_type, "sender_id": "wxid_x",
            "sender_name": None, "is_self": 0, "content": None, "media_hint": None}


class _FakeReader:
    """只实现 collect_degradations 用到的那一样：真实 `MessageReader._drift` 的状态。"""

    def __init__(self, drift=()):
        self._drift = list(drift)


class _FakeCtx:
    """只实现 message_media 用到的那几样：account / resolve / reader。"""

    def __init__(self, chat_id="wxid_x", account=ACCT, drift=()):
        self.account, self._chat = account, chat_id
        self.reader = _FakeReader(drift)

    def resolve(self, contact):
        return self._chat

    def degradations(self):
        """真实 `ChatContext.degradations()` 的形状：**信封 dict**，不是 `Degradation`。

        生产路径已经不走它了（走 `collect_degradations(ctx.reader)`）；留着是为了让「用错
        出口」这个回归在变异时表现为真缺陷（dict 流进声明为 Degradation 的字段），
        而不是「夹具缺个方法」。
        """
        return [{"capability": d.capability,
                 "effect": usecases.DEGRADE_EFFECT.get(d.capability, "部分功能降级")}
                for d in self.reader._drift]


@pytest.fixture
def idx(tmp_path, monkeypatch):
    """建一个只有 messages 的索引；返回 (con, 行列表)。

    必须隔离 `HOME_DIR`：`store.connect` 会调 `paths.ensure_home_dirs()`，不隔离就会在
    真实 `~/.agent-wechat` 下建目录（本仓库有夹具写到真实数据上的前科）。

    连接的生命周期由本夹具持有（与 test_store.py 的 `con` 夹具同一个口径）：不 close 会把
    文件句柄与 `-wal`/`-shm` 留给下一个测试。
    """
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path / "home")
    con = store.connect(tmp_path / "index.db")
    rows = [_row(1, messages.TYPE_TEXT),          # → none
            _row(2, messages.TYPE_IMAGE),         # 无 ref → gone
            _row(3, messages.TYPE_IMAGE),         # 有 ref，未导出，本体在 → exportable
            _row(4, messages.TYPE_IMAGE),         # 有 ref，已导出 → exported
            _row(5, messages.TYPE_VOICE),         # 语音，本体在，未导出 → exportable
            _row(6, messages.TYPE_VOICE)]         # 语音，无本体 → gone
    store.upsert_batch(con, rows)
    store.upsert_media_refs(con, [
        (rows[2]["msg_key"], "image", "b" * 32, None),
        (rows[3]["msg_key"], "image", "c" * 32, None),
    ])
    con.commit()
    yield con, rows
    con.close()


def test_four_states_are_all_reachable(tmp_path, monkeypatch, idx):
    """四态各自可达 —— 这是本任务的核心断言。"""
    con, rows = idx
    out_root = tmp_path / "media"
    # 4 号消息：产物存在
    d = out_root / CM / "image" / MON
    d.mkdir(parents=True)
    (d / ("c" * 32 + ".jpg")).write_bytes(b"x")
    # 3 号消息：本体在微信树里
    wx = tmp_path / "wx"
    monkeypatch.setattr("agent_wechat.paths.XWECHAT_FILES", wx)
    img = wx / ACCT / "msg/attach" / CM / MON / "Img"
    img.mkdir(parents=True)
    (img / ("b" * 32 + ".dat")).write_bytes(b"x")
    # 语音：5 号有本体
    monkeypatch.setattr(usecases, "_voice_keys_for",
                        lambda acct: frozenset({(CM, 5)}), raising=False)

    res = usecases.message_media(
        "wxid_x", keys=[r["msg_key"] for r in rows],
        out_dir=str(out_root), account=ACCT, _ctx=_FakeCtx(), _con=con)
    # 按 msg_key 取，**不依赖返回顺序**：六条的 ts 相同，按 ts 排序对它们没有意义
    assert {s.msg_key: s.status for s in res.states} == {
        rows[0]["msg_key"]: "none",        # 纯文本 → 没有媒体
        rows[1]["msg_key"]: "gone",        # 图片、无 ref、本体也不在 → 有媒体，找不到
        rows[2]["msg_key"]: "exportable",  # 有 ref、本体在微信树里、未导出
        rows[3]["msg_key"]: "exported",    # 有 ref、产物已落盘
        rows[4]["msg_key"]: "exportable",  # 语音、VoiceInfo 有本体、未导出
        rows[5]["msg_key"]: "gone",        # 语音、VoiceInfo 无本体
    }
    assert res.counts == {"none": 1, "exported": 1, "exportable": 2, "gone": 2}


def test_by_kind_breakdown_excludes_none_state(tmp_path, monkeypatch, idx):
    """**by_kind 由本层派生**（MCP 信封与 CLI 那几行读的是同一份）。

    这条是唯一能钉住推导的断言：两个适配层各自的测试都只把 by_kind 喂进渲染/信封，
    推错了它们照样绿。夹具沿用四条状态全可达的那套（image 三种状态 + voice 两种 +
    一条 none 态）：只有一种 kind、或没有 none 态时，下面两条断言都会恒真。
    `none` 态没有 kind，只该进池化的 counts。
    """
    con, rows = idx
    out_root = tmp_path / "media"
    d = out_root / CM / "image" / MON
    d.mkdir(parents=True)
    (d / ("c" * 32 + ".jpg")).write_bytes(b"x")           # 4 号：已导出
    wx = tmp_path / "wx"
    monkeypatch.setattr(paths, "XWECHAT_FILES", wx)
    img = wx / ACCT / "msg/attach" / CM / MON / "Img"
    img.mkdir(parents=True)
    (img / ("b" * 32 + ".dat")).write_bytes(b"x")         # 3 号：本体在，未导出
    monkeypatch.setattr(usecases, "_voice_keys_for",
                        lambda acct: frozenset({(CM, 5)}))   # 5 号语音有本体

    res = usecases.message_media(
        "wxid_x", keys=[r["msg_key"] for r in rows],
        out_dir=str(out_root), account=ACCT, _ctx=_FakeCtx(), _con=con)
    assert res.by_kind == {"image": {"exported": 1, "exportable": 1, "gone": 1},
                           "voice": {"exportable": 1, "gone": 1}}
    # kind=None 混进桶里会多出一个 None 键（'none' 态本来只该进池化的 counts）
    assert None not in res.by_kind
    # 桶里恰好是 counts 的三态之和：没有状态被漏掉，也没有状态被算两次
    assert sum(sum(b.values()) for b in res.by_kind.values()) == (
        res.counts["exported"] + res.counts["exportable"] + res.counts["gone"])
    assert res.counts["none"] == 1                        # 夹具自证：确实有一条 none 态


def test_voice_keys_none_degrades_not_lies(tmp_path, monkeypatch, idx):
    """语音库读不到时必须出 degraded，且语音不得被判成 exportable。"""
    con, rows = idx
    monkeypatch.setattr(usecases, "_voice_keys_for", lambda acct: None)
    res = usecases.message_media(
        "wxid_x", keys=[r["msg_key"] for r in rows if r["msg_type"] == 34],
        out_dir=str(tmp_path / "media"), account=ACCT, _ctx=_FakeCtx(), _con=con)
    assert [s.status for s in res.states] == ["gone", "gone"]
    # 断言打在稳定键上：`effect` 是给人看的人话，改一次文案不该让这条变红
    assert any(d.capability == "msg_media" for d in res.degraded)
    assert all(d.effect for d in res.degraded)


def test_media_type_without_ref_is_gone_not_none(tmp_path, monkeypatch, idx):
    """**本任务最重要的一条**：媒体消息没有 ref ≠ 没有媒体。

    若把 none 判据写成「有没有 ref」，这条会红。
    """
    con, rows = idx
    monkeypatch.setattr(usecases, "_voice_keys_for", lambda acct: frozenset())
    res = usecases.message_media(
        "wxid_x", keys=[rows[1]["msg_key"]],       # TYPE_IMAGE，无 ref
        out_dir=str(tmp_path / "media"), account=ACCT, _ctx=_FakeCtx(), _con=con)
    assert res.states[0].status == "gone"
    assert res.states[0].kind == "image"


def test_default_keys_answers_the_chat_media_types(tmp_path, monkeypatch, idx):
    """keys=None（默认）答**该会话全部媒体类型的消息**——这正是 docstring 那句承诺。

    local_id=2 那条无 ref 的图片必须在内：它既是「按 msg_type 筛、不按 ref 筛」的证据，
    也是本文件里唯一一条会走到 chat_md5 过滤的用例。纯文本那条必须不在内。
    """
    con, rows = idx
    monkeypatch.setattr(paths, "XWECHAT_FILES", tmp_path / "wx")   # 空树：本体一律不在
    monkeypatch.setattr(usecases, "_voice_keys_for", lambda acct: frozenset())
    res = usecases.message_media(
        "wxid_x", out_dir=str(tmp_path / "media"), account=ACCT,
        _ctx=_FakeCtx(), _con=con)
    assert res.counts == {"none": 0, "exported": 0, "exportable": 0, "gone": 5}
    got = {s.msg_key for s in res.states}
    assert rows[1]["msg_key"] in got          # 无 ref 的图片
    assert rows[0]["msg_key"] not in got      # 纯文本


def test_default_keys_does_not_leak_other_chats(tmp_path, monkeypatch, idx):
    """索引里装的是**所有会话**：chat_md5 那一滤少一条，别的会话的媒体就会被算进本会话。

    夹具里那条 `chat_md5` 是本会话的；换一个 chat_id 问，答案必须是空。
    """
    con, _rows = idx
    monkeypatch.setattr(paths, "XWECHAT_FILES", tmp_path / "wx")
    res = usecases.message_media(
        "wxid_other", out_dir=str(tmp_path / "media"), account=ACCT,
        _ctx=_FakeCtx(chat_id="wxid_other"), _con=con)
    assert res.states == []


def test_reader_drift_reaches_degraded_as_typed_values(tmp_path, monkeypatch, idx):
    """reader 的漂移必须以 **`Degradation` 对象**进 degraded —— 入口层读的是 `.capability`。

    `ChatContext.degradations()` 返回的是信封形状的 dict（search/semantic 的出口），拿它当
    本结果类型的元素，会把 `AttributeError` 推迟到 MCP/CLI 渲染时才炸——而那正是最需要
    可读错误、最需要这条降级说明的 schema 漂移时刻。`_drift` 为空时两种写法没区别，
    所以夹具**必须灌一条真漂移**，否则下面的循环恒真。
    """
    con, rows = idx
    drift = errors.SchemaDrift("messages", "message_0.db", "Msg_deadbeef", ("local_id",))
    monkeypatch.setattr(paths, "XWECHAT_FILES", tmp_path / "wx")
    res = usecases.message_media(
        "wxid_x", keys=[rows[0]["msg_key"]], out_dir=str(tmp_path / "media"),
        account=ACCT, _ctx=_FakeCtx(drift=(drift,)), _con=con)
    assert res.degraded, "漂移没有被收集，下面的断言将恒真"
    assert all(isinstance(d, usecases.Degradation) for d in res.degraded), res.degraded
    assert res.degraded[0].capability == "messages"
    assert res.degraded[0].effect          # 人话由 DEGRADE_EFFECT 查出来，不是空串


def test_bad_month_is_rejected_not_silently_empty(tmp_path, monkeypatch, idx):
    """月份写错要报错，不是静默 0 命中 —— 与 media_export / moments_export 同一口径与文案。

    `2026-9` 一并列出：格式必须逐字符同形（两位月），否则它比不中任何目录。
    """
    con, _rows = idx
    monkeypatch.setattr(usecases, "_voice_keys_for", lambda acct: frozenset())
    with pytest.raises(errors.SearchInputError) as ei:
        usecases.message_media("wxid_x", months=["2026-13", "2026-9"],
                               out_dir=str(tmp_path / "media"), account=ACCT,
                               _ctx=_FakeCtx(), _con=con)
    assert "2026-13" in str(ei.value) and "2026-9" in str(ei.value)


@pytest.mark.parametrize("bad", ["9999-12", "0000-01"])
def test_month_outside_the_datetime_range_is_rejected_too(tmp_path, monkeypatch, idx, bad):
    """**形状合规 ≠ 可表示**：`MONTH_RE` 放行 `9999-12` / `0000-01`，而 `datetime` 的年是
    1–9999，上界还要算**下个月**的 1 号——于是 `media.month_bounds` 抛 `ValueError`。

    这条压的是**共用那一段**（`month_bounds`）而不是某一个命令：`media export` 经
    `_month_clause` 走它、`chat export` 直接走它，`message_media` 是前者的调用方。
    把翻译层删掉 → 逃出来的是裸 `ValueError`，这条红（同类用例见
    `test_chat_export_rejects_a_malformed_month` 的后两个参数）。

    `2026-13` 那种形状错的**不由这里拦**——`bad_months` 抢在 `month_bounds` 之前，
    所以本用例只放形状合规、越界的那两个。
    """
    con, _rows = idx
    monkeypatch.setattr(usecases, "_voice_keys_for", lambda acct: frozenset())
    with pytest.raises(errors.SearchInputError) as ei:
        usecases.message_media("wxid_x", months=[bad], out_dir=str(tmp_path / "media"),
                               account=ACCT, _ctx=_FakeCtx(), _con=con)
    assert bad in str(ei.value)


def test_months_filter_applies_before_limit(tmp_path, monkeypatch, idx):
    """limit 必须卡住**命中月份**的条数，而不是「先按时间截 N 条、再滤月份」。

    退回取回后再滤的写法，这里会变空：limit=2 只留下两条最新的（都在更晚的那个月），
    目标月一条不剩 → counts 全 0，而「那个月没有媒体」与「我只看了最新两条」无从区分。
    月份一律由 media.month_of 推出，不硬编码月份串。
    """
    con, _rows = idx
    monkeypatch.setattr(paths, "XWECHAT_FILES", tmp_path / "wx")
    monkeypatch.setattr(usecases, "_voice_keys_for", lambda acct: frozenset())
    ts_new = TS + 32 * 86400
    assert media.month_of(ts_new) != media.month_of(TS)      # 夹具自证：两条确实跨月
    store.upsert_batch(con, [_row(11, messages.TYPE_IMAGE, ts=ts_new),
                             _row(12, messages.TYPE_IMAGE, ts=ts_new)])
    res = usecases.message_media(
        "wxid_x", months=[media.month_of(TS)], limit=2, out_dir=str(tmp_path / "media"),
        account=ACCT, _ctx=_FakeCtx(), _con=con)
    assert len(res.states) == 2
    assert {media.month_of(s.ts) for s in res.states} == {media.month_of(TS)}
    assert res.truncated is True


def test_truncated_flags_early_stop_in_both_branches(tmp_path, monkeypatch, idx):
    """limit 截断了答案要如实上报：`keys` 分支与整会话分支都置位，没截断则不置位。"""
    con, rows = idx
    monkeypatch.setattr(paths, "XWECHAT_FILES", tmp_path / "wx")
    monkeypatch.setattr(usecases, "_voice_keys_for", lambda acct: frozenset())
    one = usecases.message_media(
        "wxid_x", keys=[rows[0]["msg_key"]], out_dir=str(tmp_path / "media"),
        account=ACCT, _ctx=_FakeCtx(), _con=con)
    assert one.truncated is False                       # 一条就答完，没有截断
    by_keys = usecases.message_media(
        "wxid_x", keys=[r["msg_key"] for r in rows], limit=2,
        out_dir=str(tmp_path / "media"), account=ACCT, _ctx=_FakeCtx(), _con=con)
    assert (len(by_keys.states), by_keys.truncated) == (2, True)
    whole = usecases.message_media(
        "wxid_x", limit=2, out_dir=str(tmp_path / "media"), account=ACCT,
        _ctx=_FakeCtx(), _con=con)
    assert (len(whole.states), whole.truncated) == (2, True)


def test_keys_from_another_chat_are_dropped(tmp_path, monkeypatch, idx):
    """keys 必须按解析出的会话收窄：不加 chat_md5 那一滤，别的会话的媒体会被冠上本会话的
    chat_id 报出去（agent 常常是拿一个未加过滤的 keys 列表来问的）。"""
    con, rows = idx
    monkeypatch.setattr(paths, "XWECHAT_FILES", tmp_path / "wx")
    monkeypatch.setattr(usecases, "_voice_keys_for", lambda acct: frozenset())
    foreign_cm = messages.md5_of("wxid_other")
    foreign = messages.row_key(ACCT, foreign_cm, "message_0.db", 99)
    store.upsert_batch(con, [{**rows[0], "msg_key": foreign, "local_id": 99,
                              "chat_md5": foreign_cm, "chat_id": "wxid_other"}])
    res = usecases.message_media(
        "wxid_x", keys=[rows[0]["msg_key"], foreign],
        out_dir=str(tmp_path / "media"), account=ACCT, _ctx=_FakeCtx(), _con=con)
    assert [s.msg_key for s in res.states] == [rows[0]["msg_key"]]


def test_relative_out_dir_is_answered_absolute(tmp_path, monkeypatch, idx):
    """`--out 相对路径` 是 CLI 直接交给本层的：`states[].path` 与 `out_dir` 必须绝对化，
    否则 MCP 客户端拿到的是用不了的相对路径。"""
    con, rows = idx
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "XWECHAT_FILES", tmp_path / "wx")
    monkeypatch.setattr(usecases, "_voice_keys_for", lambda acct: frozenset())
    d = tmp_path / "media" / CM / "image" / MON
    d.mkdir(parents=True)
    (d / ("c" * 32 + ".jpg")).write_bytes(b"x")
    res = usecases.message_media(
        "wxid_x", keys=[rows[3]["msg_key"]], out_dir="media", account=ACCT,
        _ctx=_FakeCtx(), _con=con)
    assert res.states[0].status == "exported"
    assert pathlib.Path(res.out_dir).is_absolute()
    assert pathlib.Path(res.states[0].path) == (d / ("c" * 32 + ".jpg")).resolve()


def test_empty_keys_answers_zero_states(tmp_path, idx):
    """空 keys 是合法问法（`--keys ,` 与 MCP 的 `keys=","` 归一化后就是它）：答案是零条状态。

    若把 `keys is not None` 写成 `if keys`，空列表会掉进「整个会话」那一支，把「问零条」
    答成「全部媒体消息」—— 这条就是为它设的。SQLite 接受空 IN 列表（实测 3.50.4：
    `k IN ()` 为假、不报错），所以这里不需要空列表早返回。
    """
    con, _rows = idx
    res = usecases.message_media(
        "wxid_x", keys=[], out_dir=str(tmp_path / "media"), account=ACCT,
        _ctx=_FakeCtx(), _con=con)
    assert res.states == []
    assert res.counts == {"none": 0, "exported": 0, "exportable": 0, "gone": 0}


def test_corrupt_index_is_a_project_error_not_a_bare_sqlite_one(tmp_path, monkeypatch):
    """损坏的 index.db → `errors.IndexUnavailable`，**不是**裸 sqlite3 异常。

    core 的契约（errors.py 开篇）是只抛项目异常，适配层才做翻译：裸 `sqlite3.DatabaseError`
    逃到 CLI 是一整个 traceback、逃到 MCP 只剩「内部错误: …」。这里**必须**走生产路径
    （不注入 `_con`）——畸形文件是在 `store.connect` 的 `executescript(SCHEMA)` 里炸的。
    """
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "index.db")
    (tmp_path / "index.db").write_bytes(b"not a sqlite database at all")
    with pytest.raises(errors.IndexUnavailable) as ei:
        usecases.message_media("wxid_x", out_dir=str(tmp_path / "media"),
                               account=ACCT, _ctx=_FakeCtx())
    assert isinstance(ei.value, errors.AgentWeChatError)
    assert "本地检索库不可用" in str(ei.value)
    assert ei.value.hint          # 单行中文 + 可执行动作，正是适配层要渲染的东西


def test_many_keys_are_queried_in_batches(tmp_path, monkeypatch, idx):
    """一次问几万个 key 必须分批：一条 `IN (?,…)` 塞下全部 key 时 SQLite 抛
    `too many SQL variables`（实测本机上限 32766），经上面那条包装就变成「检索库不可用」
    ——用户拿不到答案，只拿到一句误导的错误。

    规模刻意超过该上限：不分批时这条**必红**（而不是「恰好也能过」）。
    """
    con, rows = idx
    monkeypatch.setattr(paths, "XWECHAT_FILES", tmp_path / "wx")   # 空微信树：本体一律不在
    monkeypatch.setattr(usecases, "_voice_keys_for", lambda acct: frozenset())
    n = 32766 + 4                                   # 单条语句装不下的规模
    keys = ([r["msg_key"] for r in rows]
            + [messages.row_key(ACCT, CM, "message_9.db", i) for i in range(n)])
    res = usecases.message_media(
        "wxid_x", keys=keys, limit=500, out_dir=str(tmp_path / "media"), account=ACCT,
        _ctx=_FakeCtx(), _con=con)
    # 只答索引里真有的那几条；不存在的 key 静默略过（与 TOOLS.md §11 的承诺一致）
    assert len(res.states) == len(rows)
    assert {s.msg_key for s in res.states} == {r["msg_key"] for r in rows}
    # 去重：同一个 key 落在两个批里不该答出两行（夹具里 rows[0] 的 key 再出现一次）
    dup = usecases.message_media(
        "wxid_x", keys=[rows[0]["msg_key"]] * (store.KEYS_BATCH + 1), limit=500,
        out_dir=str(tmp_path / "media"), account=ACCT, _ctx=_FakeCtx(), _con=con)
    assert len(dup.states) == 1


# ---------- T9：贴纸是第四个 kind（不再是 none），但**永不** exportable ----------

STICKER_MD5 = "d0f46ed3ce8bac33df5f7055656d8bd9"


def test_sticker_message_is_a_covered_kind_not_none(tmp_path, monkeypatch, idx):
    """贴纸**不再是 `none`**——这是本轮唯一改动的已文档化 MCP 契约。

    回归对象：`_KIND_OF_TYPE` 里没有 47 时，贴纸落进 `if kind is None` 那条早退，
    返回 `kind=None, status="none"`，且 `by_kind` 里根本没有 sticker 这一项。

    **后半段同样是要害：贴纸一律 `gone`，永不 `exportable`。** 夹具刻意把一份 md5 命名的
    文件放在本机（真实布局 `business/emoticon/Persist/<xx>/<md5>`），两行仍都是 gone——
    presence 是写死的常量，不是存在性判定。谁把它「修」成任何 `resolve_*` 调用，这条先红。
    理由（本轮裁定）：`exportable` 的文档语义是「有产物可拿」，而贴纸**拿不出来**
    （本地是解不开的密文、CDN 传输不通），产出它就是谎报。
    """
    mon = monkeypatch  # 便于对齐本文件其它用例的命名习惯
    wx = tmp_path / "wx"
    mon.setattr(paths, "XWECHAT_FILES", wx)
    mon.setattr(usecases, "_voice_keys_for", lambda acct: frozenset(), raising=False)
    # 本机有那份 md5 命名的文件（`Persist` 那一支）
    p = wx / ACCT / "business/emoticon/Persist" / STICKER_MD5[:2] / STICKER_MD5
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x")

    con, rows = idx
    rows.append(_row(7, messages.TYPE_STICKER))
    # 8 号：本机没有那份文件 → 同样是 gone。**两支都要**：只测一支，「一律 gone」就只证明了
    # 一半，而另一半（本机有文件时是不是就 exportable 了）恰是本轮最容易走偏的地方。
    rows.append(_row(8, messages.TYPE_STICKER))
    store.upsert_batch(con, rows[-2:])
    store.upsert_media_refs(con, [
        (rows[-2]["msg_key"], "sticker", STICKER_MD5, None),
        (rows[-1]["msg_key"], "sticker", "f" * 32, None),
    ])
    con.commit()

    res = usecases.message_media(
        "wxid_x", keys=[r["msg_key"] for r in rows],
        out_dir=str(tmp_path / "media"), account=ACCT, _ctx=_FakeCtx(), _con=con)
    by_key = {s.msg_key: s for s in res.states}
    assert by_key[rows[-2]["msg_key"]].kind == "sticker"
    assert by_key[rows[-2]["msg_key"]].status == "gone"     # 本机有那份文件也一样
    assert by_key[rows[-1]["msg_key"]].status == "gone"
    assert "sticker" in res.by_kind
    assert res.by_kind["sticker"] == {"gone": 2}


def test_sticker_without_md5_is_gone_not_guessed(tmp_path, monkeypatch, idx):
    """`file_key` 为空（XML 无 md5）→ `gone`，**不猜文件名**。

    与 `_state_of` 对 video/image 的同一条立场：没有钥匙就说「定位不了」，
    不许按 ts 猜一个路径出来。实测索引里确实有拿不到 ref 的媒体消息
    （image 259 条），这条路径不是假想。
    """
    monkeypatch.setattr(paths, "XWECHAT_FILES", tmp_path / "wx")
    monkeypatch.setattr(usecases, "_voice_keys_for", lambda acct: frozenset(), raising=False)
    con, rows = idx
    rows.append(_row(9, messages.TYPE_STICKER))
    store.upsert_batch(con, rows[-1:])
    store.upsert_media_refs(con, [(rows[-1]["msg_key"], "sticker", None, None)])
    con.commit()
    res = usecases.message_media(
        "wxid_x", keys=[rows[-1]["msg_key"]],
        out_dir=str(tmp_path / "media"), account=ACCT, _ctx=_FakeCtx(), _con=con)
    st = {s.msg_key: s for s in res.states}[rows[-1]["msg_key"]]
    assert (st.kind, st.status) == ("sticker", "gone")


def test_default_keys_include_sticker_messages(tmp_path, monkeypatch, idx):
    """keys=None（默认）的「全部媒体类型的消息」必须含贴纸 —— 这正是 `_MEDIA_TYPES` 的作用。

    漏掉 `_MEDIA_TYPES` 里的 TYPE_STICKER，贴纸消息在 SQL 里就被整条滤掉，
    `_KIND_OF_TYPE[47]` 于是成了**静默 no-op**（kind 判定根本轮不到它）：删掉那一个常量
    这条必红。**连 ref 都没有的贴纸也要答**——按 ref 筛就等于把「定位不了」谎报成
    「不是媒体」。
    """
    con, rows = idx
    monkeypatch.setattr(paths, "XWECHAT_FILES", tmp_path / "wx")
    monkeypatch.setattr(usecases, "_voice_keys_for", lambda acct: frozenset())
    rows.append(_row(13, messages.TYPE_STICKER))       # 刻意**不写** media_refs 行
    store.upsert_batch(con, rows[-1:])
    con.commit()
    res = usecases.message_media(
        "wxid_x", out_dir=str(tmp_path / "media"), account=ACCT,
        _ctx=_FakeCtx(), _con=con)
    got = {s.msg_key: s for s in res.states}
    assert got[rows[-1]["msg_key"]].kind == "sticker"
    assert got[rows[-1]["msg_key"]].status == "gone"   # 没有 ref 也是 gone，不是 none
    assert res.by_kind["sticker"] == {"gone": 1}



def test_a_video_thumbnail_alone_is_not_reported_as_exported(tmp_path, monkeypatch, idx):
    """EA-036：`media.exported_path` 会命中 **`{stem}_thumb.jpg`**（视频的**缩略图**），
    而 `_state_of` 一律报 `exported`——两个模板于是产出
    `<video controls src="…/{stem}_thumb.jpg">`：**坏播放器**（jpg 不是视频源，浏览器里是
    黑框）。该分支在全量测试里零覆盖（变异删掉它套件全绿）。

    四态契约**不加第五态**（贴纸设计时已定）：缩略图命中时按「本体不在」继续判。
    判据在 `_state_of`（有信息的那一层），这里直接喂给它两种产物路径。
    把判据里的 `not media.is_thumbnail(p)` 删掉本测试必红。
    """
    con, _ = idx
    vrow = _row(7, messages.TYPE_VIDEO)
    store.upsert_batch(con, [vrow])
    con.commit()
    monkeypatch.setattr(media, "resolve_video", lambda *a, **k: None)   # 本体不在本机

    # ① 产物命中**缩略图** → 不是「已导出」（本体没导出来）
    monkeypatch.setattr(media, "exported_path",
                        lambda *a, **k: str(tmp_path / "video" / "ef01_thumb.jpg"))
    st = usecases._state_of(vrow, ("video", "ef01"), out_root=tmp_path,
                            account=ACCT, voice_keys=None)
    assert st.status == "gone", f"缩略图不是本体，却报成 {st.status}"

    # ② 反向守卫：真本体（.mp4）仍必须报 exported（别把判据做成一刀切）
    monkeypatch.setattr(media, "exported_path",
                        lambda *a, **k: str(tmp_path / "video" / "ef01.mp4"))
    st2 = usecases._state_of(vrow, ("video", "ef01"), out_root=tmp_path,
                             account=ACCT, voice_keys=None)
    assert st2.status == "exported" and st2.path.endswith("ef01.mp4")


def test_a_key_count_exactly_equal_to_the_limit_is_not_truncated(tmp_path, monkeypatch, idx):
    """`keys` 分支的 `truncated` 必须是 `len(rows) > limit`，不是 `>=`。

    这条路**没有 SQL `LIMIT`**：`rows` 是候选全量，`rows[:lim]` 只在超过 lim 时才真的丢东西。
    用 `>=` 时「key 数恰好等于 limit」（真机常见：200 个 key + 默认 limit 200）会一条不丢地
    报 `truncated=True`——MCP 于是告诉 agent「counts 只是部分」，CLI 还会印「已提前收手」。

    `else` 分支保持 `>=`（那条路的 SQL 自己截断了，取回正好 lim 条时确实可能还有更多）。
    """
    con, rows = idx
    monkeypatch.setattr(paths, "XWECHAT_FILES", tmp_path / "wx")
    monkeypatch.setattr(usecases, "_voice_keys_for", lambda acct: frozenset())
    keys = [r["msg_key"] for r in rows]
    exact = usecases.message_media(
        "wid", keys=keys, limit=len(keys), out_dir=str(tmp_path / "m"),
        account=ACCT, _ctx=_FakeCtx(), _con=con)
    assert len(exact.states) == len(keys), "该答的都要答回"
    assert exact.truncated is False, "一条没丢却报截断 = 假警"
