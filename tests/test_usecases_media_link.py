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
    return con, rows


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
