"""C 阶段属性测试（C-1..C-6，spec §5）。

每条属性都写成**可失败**的形式：语料由 hypothesis 生成，失败即一条发现（Task 10 修）。
`max_examples=100`、`deadline=None`（解析类调用在慢机器上会超过默认 200ms）。

**映射说明**（计划允许「API 名以仓库实际为准」，此处逐条核对过，无替换）：
C-1 `usecases.MONTH_RE` + `media.month_bounds`；C-2 `appmsg.parse`；C-3 `messages.chat_md5`
/ `messages.degraded_chat_id`；C-4 `paths.in_wechat_container`；C-5 `chat_md._esc_md`；
C-6 `reader._type_name`。
"""
from hypothesis import given, settings, strategies as st

from agent_wechat import appmsg, chat_md, errors, media, messages, paths, reader

N = 100
s = settings(max_examples=N, deadline=None)


@given(st.text())
@s
def test_month_matched_is_representable_or_project_error(text):
    """C-1：形状合法 ⇒ 区间可表示（lo<hi）；越界 ⇒ `SearchInputError`（不是裸 ValueError）。

    非法**形状**的拒绝由既有用例覆盖（`2026-9` / `2026-13` 那一族）；这里钉的是
    **形状合法但不可表示**：`MONTH_RE` 是纯形状判据，`0000-01` 与 `9999-12` 都逐字合规，
    而 `datetime` 的年是 1–9999，上界还要算下个月的 1 号。
    """
    from agent_wechat.usecases import MONTH_RE
    if not MONTH_RE.fullmatch(text):
        return
    try:
        lo, hi = media.month_bounds([text])[0]
    except errors.SearchInputError:
        return
    assert lo < hi


@given(st.text())
@s
def test_appmsg_parse_never_raises(raw):
    """C-2：任意文本只返回 ParseResult，不抛非项目异常（docstring 说「全定义、不抛」）。"""
    out = appmsg.parse(raw)
    assert out is not None
    # 三种结果都必须可区分（docstring）：成功 ⇔ reason is None；否则 reason 必须已命名。
    assert (out.appmsg is not None) == (out.reason is None)
    assert out.reason in (None, "rejected", "malformed")


@given(st.text())
@s
def test_chat_md5_idempotent(text):
    """C-3：幂等；退化形态是 `md5:<hex>`，不二次哈希。"""
    h = messages.chat_md5(text)
    assert messages.chat_md5(text) == h
    assert messages.degraded_chat_id(h) == f"md5:{h}"


@given(st.text(alphabet=st.characters(blacklist_characters="/\x00")))
@s
def test_path_predicate_monotone(text):
    """C-4：容器内 ⇒ 所有子路径内（闸门的单调性——它按「祖先链」判，必须闭合）。"""
    base = paths.CONTAINER_DATA / text
    if paths.in_wechat_container(base):
        assert paths.in_wechat_container(base / "x")


@given(st.text())
@s
def test_md_escape_has_no_raw_controls(text):
    """C-5：转义后不得残留除换行/制表外的裸控制字符。"""
    out = chat_md._esc_md(text)
    assert not any(ord(c) < 32 and c not in "\n\t" for c in out)


@given(st.integers())
@s
def test_unknown_msg_type_never_keyerrors(t):
    """C-6：未知类型回落默认名，不抛 KeyError。"""
    name = reader._type_name(t)
    assert isinstance(name, str) and name
