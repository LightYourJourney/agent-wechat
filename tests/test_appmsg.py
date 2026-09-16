# tests/test_appmsg.py
"""appmsg 规范化。夹具形态全部来自 spec §2.5 的真机实测（内容为合成）。"""
from agent_wechat import appmsg

# 三种真实前缀形态：wxid / 自定义微信号 / openim
PREFIXED = [
    ("wxid_testacct01:\n<msg><appmsg><type>57</type></appmsg></msg>", "wxid_testacct01"),
    ("Ulrica-fs:\n<msg><appmsg><type>57</type></appmsg></msg>", "自定义微信号"),
    ("12345678@openim:\n<msg><appmsg><type>57</type></appmsg></msg>", "@openim"),
]


def test_strips_group_prefix_for_all_three_sender_shapes():
    """回归：按 `wxid_` 前缀剥的写法会让第 2、3 种原样进 XML 解析器 —— 两类都必须被剥掉。"""
    for raw, why in PREFIXED:
        out = appmsg.normalize(raw)
        assert out.startswith("<msg>"), f"{why} 的前缀没被剥掉：{out[:40]!r}"


def test_plain_single_chat_payload_untouched():
    """单聊载荷以 `<?xml …?>` 开头、**没有群前缀**，规范化后应以 `<msg>` 起头。

    注意这条**同时**压在 `_XML_DECL` 上（首行含 `<`/`>`，`_GROUP_PREFIX` 本就匹配不了它）：
    删掉剥声明的规则，它会红。
    """
    raw = "<?xml version=\"1.0\"?>\n<msg><appmsg><type>5</type></appmsg></msg>"
    assert appmsg.normalize(raw).startswith("<msg>")


def test_does_not_strip_a_leading_colon_line_that_is_not_the_group_prefix():
    """安全阀：载荷已知是 XML，故判据要求首行之后**必须**是 `<?xml` 或 `<msg`。

    这条断言守的是判据的**强度**：把前瞻放宽成裸 `(?=<)`，第二行就会红。
    """
    assert appmsg.normalize("补充说明:\n请看下面") == "补充说明:\n请看下面"
    assert appmsg.normalize("http://example.com/x:\n<other/>").startswith("http://example.com/x:")


def test_strips_all_xml_declarations():
    """实测 8,980 条无声明、11 条声明位置非法（嵌在 <msg> 内部）—— 全部要剥。"""
    assert appmsg.normalize('<?xml version="1.0"?>\n<msg/>') == "<msg/>"
    assert appmsg.normalize('<msg><?xml version="1.0"?> <appmsg/></msg>') == "<msg> <appmsg/></msg>"


def test_drops_illegal_character_references():
    """实测最后 1 条失败即此。按 **XML 1.0 的合法码位**判，不是枚举碰到的坏值。

    **断言必须落在实体文本上。** 初稿写成 `assert "\\x00" not in ...` 是**恒真的**——
    输入是字面量 `&#x0;`，解析出来的 Python 字符串里根本没有 NUL 字节，所以关掉这条
    规则也不会红（T1 实现者用变异测试发现的）。
    """
    for bad in ("&#x0;", "&#0;", "&#x1;", "&#xB;", "&#xC;", "&#x1F;", "&#31;"):
        assert bad not in appmsg.normalize(f"<msg>a{bad}b</msg>"), f"{bad} 非法，应剔除"
    for good in ("&#x9;", "&#10;", "&#13;", "&#20013;", "&#x4E2D;"):
        assert good in appmsg.normalize(f"<msg>a{good}b</msg>"), f"{good} 合法，不许动"


def test_malformed_character_reference_shapes_do_not_crash():
    """回归：`&#abc;` 这类**形状不合法**的引用不是数值引用。

    早先的正则 `&#(x?)([0-9a-fA-F]+);` 配 `re.I` 会把它匹配成十进制、`int('abc', 10)`
    抛裸 ValueError（评审发现，控制器复现）。载荷是对手可控的，所以这条要守的是
    **「不崩」**而不是「恰好没遇到」——退回 `(x?)([0-9a-fA-F]+)` 的写法它立刻红。
    """
    for weird in ("&#abc;", "&#e;", "&#1a;", "&#;", "&#x;", "&#xg;"):
        out = appmsg.normalize(f"<msg>a{weird}b</msg>")     # 不抛即通过
        assert isinstance(out, str)
    # 形状不合法的不做越权删除：原样留着，由 ET 判成 malformed 更诚实
    assert "&#abc;" in appmsg.normalize("<msg>a&#abc;b</msg>")


def test_group_prefix_has_a_length_bound():
    """判据的 `{1,64}` 上限：64 仍是用户名，65 不是。两端都钉住。"""
    ok_head = "x" * 64
    assert appmsg.normalize(f"{ok_head}:\n<msg/>") == "<msg/>"
    long_head = "x" * 65
    assert appmsg.normalize(f"{long_head}:\n<msg/>").startswith(long_head)


def test_is_dangerous_rejects_doctype_and_entity():
    assert appmsg.is_dangerous("<!DOCTYPE foo [<!ENTITY a 'b'>]><msg/>")
    assert appmsg.is_dangerous("<msg><!ENTITY x 'y'></msg>")
    assert not appmsg.is_dangerous("<msg><appmsg><title>a</title></appmsg></msg>")
