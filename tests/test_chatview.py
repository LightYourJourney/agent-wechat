# tests/test_chatview.py
"""ChatView 组装。纯函数，不需要真实数据。"""
from agent_wechat import appmsg, chatview


def _row(key, ts, content="hi", msg_type=1):
    return {"msg_key": key, "ts": ts, "msg_type": msg_type, "content": content,
            "sender_name": "我" if ts % 2 else "张三", "is_self": ts % 2,
            "chat_id": "wxid_testacct02", "shard": "message_0.db"}


def test_anchor_is_stable_and_short():
    a = chatview.anchor_of("acct|aaa|message_0.db|1")
    assert a == chatview.anchor_of("acct|aaa|message_0.db|1")
    assert len(a) == 12 and a != chatview.anchor_of("acct|aaa|message_0.db|2")


def test_ref_anchor_is_none_when_target_is_outside_this_export():
    """回归：目标不在本次范围内时**必须**是 None —— 空串会渲染成指向自己的死锚点。"""
    v = chatview.build(
        "c", title="t", rows=[_row("k1", 10)], parts={}, media={}, out_dir="/tmp",
        truncated=False, unparsed=0, rejected=0, degraded=(),
        refs={"k1": "acct|aaa|message_0.db|999"})
    assert v.msgs[0].ref_anchor is None


def test_ref_anchor_resolves_when_target_is_in_range():
    v = chatview.build(
        "c", title="t", rows=[_row("k1", 10), _row("k2", 20)], parts={}, media={},
        out_dir="/tmp", truncated=False, unparsed=0, rejected=0, degraded=(),
        refs={"k2": "k1"})
    by_key = {m.msg_key: m for m in v.msgs}
    assert by_key["k2"].ref_anchor == by_key["k1"].anchor


def test_counts_and_flags_are_carried_through():
    """truncated / unparsed / rejected 必须原样进 ChatView —— 产物顶部要印它们。

    `degraded` 也一并钉住（F48）：它同样是产物顶部要印的东西，此前只在 `degraded=()`
    下被传过，于是「写死成空元组」全绿。chat_id / title / out_dir 是纯透传、且 T7/T8
    的用例直接读它们，顺手一起断言，不为此另开用例。
    """
    from agent_wechat.usecases import Degradation

    d = (Degradation(capability="msg_parts", db="index.db", table="msg_parts",
                     missing_columns=(), effect="msg_parts 未回填"),)
    v = chatview.build("c", title="t", rows=[_row("k1", 10)], parts={}, media={},
                       out_dir="/tmp", truncated=True, unparsed=2, rejected=1, degraded=d)
    assert (v.truncated, v.unparsed, v.rejected) == (True, 2, 1)
    assert v.degraded == d
    assert (v.chat_id, v.title, v.out_dir) == ("c", "t", "/tmp")


# ---------------- 以下是 brief 之外补的夹具（每条 docstring 都写明怎么红）----------------


def test_is_self_and_sender_are_true_for_own_messages():
    """回归：`is_self` / `sender` 的搬运要**两侧**都断言。

    `_row(key, ts)` 的 `is_self = ts % 2`、`sender_name = "我" if ts % 2 else "张三"`。
    **只断言 `ts` 为偶数的行是没用的**——它的 `is_self` 恰是 `False`，与「写死 False」不可区分。
    T6 审阅者实测（我也复现了）：把 `is_self` 写死成 `False` → 全量 631 passed 无人红，
    而后果是**整份导出被判成别人发的**（时间线左右对齐整体翻转）。
    这条断言 `ts=21`（**奇数**）那一行，删掉 `bool(...)` 或写死即红。
    """
    v = chatview.build(
        "c", title="t", rows=[_row("k1", 10), _row("k2", 21)], parts={}, media={},
        out_dir="/tmp", truncated=False, unparsed=0, rejected=0, degraded=())
    by_key = {m.msg_key: m for m in v.msgs}
    assert by_key["k1"].is_self is False and by_key["k1"].sender == "张三"
    assert by_key["k2"].is_self is True and by_key["k2"].sender == "我"   # ← 承重的那一侧


def test_row_fields_are_wired_into_the_view_msg():
    """行 → ViewMsg 的字段搬运：删掉 `kind=_kind_of(row["msg_type"])` 那行（或改成恒
    `str(row["msg_type"])`）→ 第 1 条断言红；删掉 `anchor=anchor_of(key)` → 第 2 条红；
    删掉 `text=` / `sender=` 的搬运 → 第 3 条红。
    `is_self` 这里只压在 `ts=10` 这个**偶数**侧（值为 False），奇数侧由
    `test_is_self_and_sender_are_true_for_own_messages` 承重——别把两半合成一条又只留偶数行。"""
    v = chatview.build("c", title="t", rows=[_row("k1", 10, content="正文")], parts={},
                       media={}, out_dir="/tmp", truncated=False, unparsed=0,
                       rejected=0, degraded=())
    m = v.msgs[0]
    assert m.kind == "text"                      # msg_type 1 → TYPE_NAMES
    assert m.anchor == chatview.anchor_of("k1")  # 锚点就是 msg_key 的短哈希
    assert (m.ts, m.text, m.sender, m.is_self) == (10, "正文", "张三", False)


def test_unknown_msg_type_falls_back_to_the_number():
    """未知 msg_type 走 `str(msg_type)`（spec §4.3 的 `…`）；把兜底删成 KeyError 或
    恒 `"unknown"` → 这条红。49 单独走 'appmsg'：messages.TYPE_NAMES 刻意不含它。"""
    v = chatview.build("c", title="t", rows=[_row("k1", 10, msg_type=9999),
                                             _row("k2", 11, msg_type=49)],
                       parts={}, media={}, out_dir="/tmp", truncated=False,
                       unparsed=0, rejected=0, degraded=())
    kinds = {m.msg_key: m.kind for m in v.msgs}
    assert kinds["k1"] == "9999" and kinds["k2"] == "appmsg"


def test_parts_and_media_are_attached_by_msg_key():
    """按 msg_key 挂载 appmsg 与媒体：删掉 `appmsg=parts.get(key)` 或 `media=media.get(key)`
    任一行 → 对应断言红；把 `.get(key)` 换成按位置取（`parts[0]`）也红。"""
    from agent_wechat.usecases import MediaRefState

    quote = appmsg.AppMsg(kind=57, title="同意", fields=(), link=None, file=None,
                          transfer=None, quote=appmsg.Quote(
                              svrid=1, displayname="张三", content="明天开会",
                              create_time=None, from_usr=None))
    state = MediaRefState(msg_key="k2", ts=20, kind="image", status="gone", path=None)
    v = chatview.build("c", title="t", rows=[_row("k1", 10), _row("k2", 20)],
                       parts={"k1": quote}, media={"k2": state}, out_dir="/tmp",
                       truncated=False, unparsed=0, rejected=0, degraded=())
    by_key = {m.msg_key: m for m in v.msgs}
    assert by_key["k1"].appmsg is quote and by_key["k2"].appmsg is None
    assert by_key["k2"].media is state and by_key["k1"].media is None


def test_counts_count_messages_by_kind():
    """counts 按 kind 计数（产物顶部/信封要印）。把它改成 `{}` 或恒 `{"text": 1}` → 这条红。"""
    v = chatview.build("c", title="t",
                       rows=[_row("k1", 10), _row("k2", 20), _row("k3", 21, msg_type=3)],
                       parts={}, media={}, out_dir="/tmp", truncated=False,
                       unparsed=0, rejected=0, degraded=())
    assert v.counts == {"text": 2, "image": 1}
