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


# 追加到 tests/test_appmsg.py

QUOTE_XML = """<msg><appmsg><title>我同意</title><type>57</type><refermsg>
<displayname>张三</displayname><content>明天下午三点开会</content>
<createtime>1700000000</createtime><svrid>123456789</svrid><fromusr>wxid_abc123def456</fromusr>
<type>1</type></refermsg></appmsg></msg>"""

LINK_XML = """<msg><appmsg><title>一篇文章</title><des>摘要</des><type>5</type>
<url>https://example.com/a</url></appmsg></msg>"""

FILE_XML = """<msg><appmsg><title>报告.pdf</title><type>6</type><appattach>
<totallen>12345</totallen><fileext>pdf</fileext><md5>deadbeef</md5></appattach></appmsg></msg>"""

TRANSFER_XML = """<msg><appmsg><title>转账</title><des>请收款</des><type>2000</type>
<wcpayinfo><paysubtype>1</paysubtype><feedesc>¥100.00</feedesc>
<pay_memo>还款</pay_memo><transcationid>TX1</transcationid><transferid>TR1</transferid>
<begintransfertime>1700000000</begintransfertime><effectivedate>1700000000</effectivedate>
<invalidtime>1700086400</invalidtime><receiver_username>wxid_testacct02</receiver_username>
</wcpayinfo></appmsg></msg>"""

RED_PACKET_XML = """<msg><appmsg><title>恭喜发财</title><type>2000</type>
<wcpayinfo><paysubtype>3</paysubtype><feedesc>¥8.88</feedesc><transcationid>TX2</transcationid>
<transferid>TR2</transferid><effectivedate>1</effectivedate><receiver_username>wxid_x</receiver_username>
<redenvelopetype>1</redenvelopetype></wcpayinfo></appmsg></msg>"""


def test_parse_quote():
    r = appmsg.parse(QUOTE_XML)
    assert r.reason is None
    assert r.appmsg.kind == appmsg.KIND_QUOTE
    assert r.appmsg.title == "我同意"
    assert r.appmsg.quote.content == "明天下午三点开会"
    assert r.appmsg.quote.displayname == "张三"
    assert r.appmsg.quote.svrid == 123456789
    assert r.appmsg.quote.create_time == 1700000000
    assert r.appmsg.quote.from_usr == "wxid_abc123def456"
    assert r.appmsg.link is None and r.appmsg.file is None and r.appmsg.transfer is None


def test_parse_link_and_file():
    lk = appmsg.parse(LINK_XML).appmsg
    assert (lk.kind, lk.link.url, lk.link.des) == (appmsg.KIND_LINK, "https://example.com/a", "摘要")
    fl = appmsg.parse(FILE_XML).appmsg
    assert (fl.kind, fl.file.fileext, fl.file.totallen) == (appmsg.KIND_FILE, "pdf", 12345)


def test_parse_transfer_names_the_fields_not_the_raw_paths():
    """回归：这一段若退化成 fields 里的 `wcpayinfo.feedesc`，断言必须红。"""
    t = appmsg.parse(TRANSFER_XML).appmsg.transfer
    assert t.amount_desc == "¥100.00"
    assert t.memo == "还款"
    assert t.receiver == "wxid_testacct02"
    assert t.transaction_id == "TX1"      # 源里是 <transcationid>（原库拼错），解析端用正确拼写
    assert t.transfer_id == "TR1"
    assert t.payer is None                # 实测 61% 为空，空是常态不是异常
    assert t.is_red_envelope is False


def test_parse_red_envelope_flag():
    t = appmsg.parse(RED_PACKET_XML).appmsg.transfer
    assert t.is_red_envelope is True
    assert t.amount_desc == "¥8.88"


def test_unknown_kind_still_gets_title_and_fields():
    """27 种里未具名建模的 23 类必须仍有专门呈现（标题 + 字段表），不是原样吐 XML。"""
    xml = "<msg><appmsg><title>接龙</title><type>53</type><solitaire_info>abc</solitaire_info></appmsg></msg>"
    a = appmsg.parse(xml).appmsg
    assert a.kind == 53 and a.title == "接龙"
    assert ("solitaire_info", "abc") in a.fields or ("appmsg.solitaire_info", "abc") in a.fields
    assert not any(v.startswith("<?xml") or v.startswith("<msg") for _, v in a.fields)


WEAPP_XML = """<msg><appmsg><title>某个小程序</title><type>33</type>
<weappinfo><appid>wx0123456789abcdef</appid><pagepath>pages/index</pagepath></weappinfo></appmsg></msg>"""

# **注意 kind=33**：33 的 appattach 不是具名子树，所以密钥确实会走到 fields 那道过滤上。
# 若用 kind=6，appattach 整棵会被跳过，这条测试就成了假护栏（恒绿）。
AES_XML = """<msg><appmsg><title>某个小程序</title><type>33</type>
<appattach><aeskey>00112233445566778899aabbccddeeff</aeskey>
<fileuploadtoken>UPLOADTOKEN123</fileuploadtoken><percent>80</percent></appattach>
<weappinfo><appid>a</appid></weappinfo></appmsg></msg>"""


def test_nested_fields_use_dotted_paths():
    """点分路径用**非具名**类型验（33 的 weappinfo）。"""
    a = appmsg.parse(WEAPP_XML).appmsg
    assert ("weappinfo.appid", "wx0123456789abcdef") in a.fields
    assert ("weappinfo.pagepath", "pages/index") in a.fields


def test_named_subtrees_are_not_repeated_in_fields():
    """回归：不跳过具名子树，同一个值会在具名块与字段表里各出现一遍。
    删掉 `_NAMED_SUBTREES` 那段，这条会红。"""
    a = appmsg.parse(FILE_XML).appmsg          # kind=6，appattach 已被 FileRef 具名建模
    assert a.file.fileext == "pdf"
    assert not any(p.startswith("appattach") for p, _ in a.fields)


def test_key_material_never_reaches_fields():
    """**红线**：`fields` 会进 MCP 信封；实测 type 6 的 `aeskey` 100%、
    `fileuploadtoken` 90%。删掉 `_SECRET_ISH` 那道过滤，这条会红。

    最后一条断言是**反假护栏**：同一棵子树里的良性字段必须还在——否则前两条可能是
    因为整棵 `appattach` 被跳过而通过的，那就什么也没守住。
    """
    a = appmsg.parse(AES_XML).appmsg
    joined = " ".join([a.title or ""] + [f"{p}={v}" for p, v in a.fields])
    assert "00112233445566778899aabbccddeeff" not in joined
    assert "UPLOADTOKEN123" not in joined
    assert not any(("aeskey" in p) or ("token" in p) for p, _ in a.fields)
    assert ("appattach.percent", "80") in a.fields


def test_token_bearing_values_are_dropped_even_under_a_benign_name():
    """名字干净但值里带凭据的，也要丢。"""
    xml = ('<msg><appmsg><title>x</title><type>33</type>'
           '<weappinfo><appid>a</appid>'
           '<iconurl>https://cdn/x?token=SECRET123&amp;idx=1</iconurl>'
           '</weappinfo></appmsg></msg>')
    a = appmsg.parse(xml).appmsg
    assert "SECRET123" not in " ".join(v for _, v in a.fields)


def test_reason_distinguishes_rejected_from_malformed():
    """回归：§5 要求两种分开计数，`None` 返回无法区分。"""
    assert appmsg.parse("<!DOCTYPE x [<!ENTITY a 'b'>]><msg/>").reason == "rejected"
    assert appmsg.parse("<msg><unclosed></msg>").reason == "malformed"
    assert appmsg.parse("<msg><appmsg><type>5</type></appmsg></msg>").reason is None


def test_long_decimal_charref_does_not_escape_parse():
    """回归：`normalize` **自己**会抛 `ValueError` —— Python 3.11+ 对十进制数字串
    有 4300 位的 int 转换上限，而 `_keep_charref` 的 `int(group(2), 10)` 会撞上它。

    实测：`normalize("<msg>&#" + "1"*6000 + ";</msg>")` 抛
    `ValueError: Exceeds the limit (4300 digits) for integer string conversion`；
    `is_dangerous` 不拦它（无 DTD/ENTITY），而载荷对手可控。

    **这条守的是「`normalize` 必须在 `parse` 的 `try` 之内」**：把它挪到 try 外，
    裸 ValueError 当场逃出 core 层，整次 `appmsg rebuild` 崩掉。挪回去，绿。
    """
    res = appmsg.parse("<msg>&#" + "1" * 6000 + ";</msg>")
    assert res.reason == "malformed"      # 被接住 → 归 malformed，而不是崩
    assert res.appmsg is None


def test_kind_name_falls_back_without_guessing():
    """含义未确认的类型不许猜（spec §10.1）。"""
    assert appmsg.kind_name(appmsg.KIND_QUOTE) == "引用"
    assert appmsg.kind_name(9999) == "appmsg:9999"


def test_no_content_leaks_into_fields():
    """`fields` 里不得出现 XML 片段或标签。

    **这条原来守的是空串**：`QUOTE_XML` 的 `fields` 恒为 `()`（`type`/`title` 被跳过、
    `refermsg` 是具名子树），`joined == ""`，任何单点缺陷都不会让它红——包括它 docstring
    点名的「把 XML 片段当值带出」（实施期审阅用 12 个变异确认）。
    **所以夹具换成 `fields` 非空的那种**，并顺带钉住「非叶子不入表」。
    """
    a = appmsg.parse(WEAPP_XML).appmsg
    assert a.fields, "夹具的 fields 不许为空，否则这条断言守的是空串"
    joined = " ".join(f"{p}={v}" for p, v in a.fields)
    assert "<" not in joined and ">" not in joined
    assert not any(p == "weappinfo" for p, _ in a.fields)   # 非叶子不入表


def test_malformed_numeric_fields_do_not_crash_or_escape():
    """**契约**：`parse` 对任意 `str` 全定义、不抛——数字字段解不开就给兜底值。

    回归：brief 字面写法 `int(appmsg.findtext("type") or -1)` 在 `<type>五十七</type>` 上
    抛**裸 ValueError**（与 T1 的 `&#abc;` 同形，同一个成因：对手可控的载荷 + 没兜底的 int）。
    删掉 `_to_int` 那道闸，这条会红。**控制器已复现 brief 字面写法确实抛。**
    """
    cases = [
        ('<msg><appmsg><title>t</title><type>57</type><refermsg><content>x</content>'
         '<svrid>abc</svrid></refermsg></appmsg></msg>', 57),
        ('<msg><appmsg><title>t</title><type>五十七</type></appmsg></msg>', -1),
        ('<msg><appmsg><title>t</title><type></type></appmsg></msg>', -1),
        ('<msg><appmsg><title>t</title><type>6</type><appattach>'
         '<totallen>一兆</totallen></appattach></appmsg></msg>', 6),
    ]
    for xml, want in cases:
        r = appmsg.parse(xml)                     # 不抛即通过
        assert r.reason is None and r.appmsg.kind == want


def test_quote_is_none_for_non_quote_kinds():
    """回归：`test_parse_quote` 断言了 `link/file/transfer is None`，**偏偏没断言 `quote`**
    ——「quote 恒建」这个 bug 原来没有任何用例会红（实施期的变异 M15）。"""
    for xml in (LINK_XML, FILE_XML, TRANSFER_XML):
        a = appmsg.parse(xml).appmsg
        assert a.quote is None, f"kind={a.kind} 不该有 quote"


def test_refermsg_is_skipped_under_non_quote_kinds():
    """回归：`refermsg` 要在**所有** kind 下跳，不只是 57。
    原来「非 57 的 kind 下出现 refermsg」无人守（变异 M2）。"""
    xml = ('<msg><appmsg><title>t</title><type>33</type>'
           '<refermsg><content>被引用的原文</content></refermsg>'
           '<weappinfo><appid>a</appid></weappinfo></appmsg></msg>')
    a = appmsg.parse(xml).appmsg
    assert ("weappinfo.appid", "a") in a.fields
    assert not any(p.startswith("refermsg") for p, _ in a.fields)


def test_top_level_type_and_title_are_not_repeated_in_fields():
    """回归：`type`/`title` 已由 `AppMsg.kind` / `AppMsg.title` 承载，不在 `fields` 里再来一份。
    （这条替代了初稿里那条**映射写错**的守卫——你指出的 M3：`test_no_content_leaks_into_fields`
    的 `fields` 恒为空，跳不跳都绿。）"""
    a = appmsg.parse(LINK_XML).appmsg
    assert not any(p in ("type", "title") for p, _ in a.fields)


def test_fields_contains_only_leaves():
    """回归：`fields` 只收**叶子**。把有子元素的节点也塞进去，这条会红（变异 M11）。"""
    a = appmsg.parse(WEAPP_XML).appmsg
    assert ("weappinfo.appid", "wx0123456789abcdef") in a.fields
    assert not any(p == "weappinfo" for p, _ in a.fields)


def test_named_leaves_are_not_repeated_in_fields():
    """回归：只跳子树挡不住 `url`/`des`——它们是 `appmsg` 的**兄弟叶子**。
    实测 kind=5 的 `fields` 里它们与 `Link` 逐字重复。删掉 `_NAMED_LEAVES` 那行会红。"""
    a = appmsg.parse(LINK_XML).appmsg
    assert a.link.url == "https://example.com/a"               # 具名块里有
    assert not any(p in ("url", "des") for p, _ in a.fields)   # 字段表里不该再有


def test_deeply_nested_payload_does_not_blow_the_stack():
    """回归：`_leaf_fields` 必须**迭代**遍历，不能用递归。

    载荷是对手可控的，而 `is_dangerous` 只挡 DTD/实体、**不挡深嵌套**。实测 5 万层嵌套下
    递归版抛 `RecursionError`（裸异常逃出 core 层，违反错误契约），迭代版正常。
    （实施期 T2 实现者发现。）
    """
    depth = 50_000
    xml = "<msg><appmsg><title>t</title><type>33</type>" + "<a>" * depth + "</a>" * depth \
          + "</appmsg></msg>"
    r = appmsg.parse(xml)                       # 不抛即通过
    assert r.reason is None


def test_link_url_is_subject_to_the_value_filter():
    """回归：`_link` 直接 findtext("url") 会绕过值过滤——同一个值走 `fields` 就丢、
    走 `Link` 就进信封（spec §4.5 明列信封含 link）。删掉 `_link` 里那道过滤会红。

    **末一条是反假护栏（F19 要求 3）**：过滤**收紧过头**换个方向同样有害——
    良性带查询串的 url 必须原样保留。此前没有任何夹具带良性查询串，
    所以「有 `?` 就丢」这种收得过紧的写法不会红（实施期探得）。
    """
    xml = ('<msg><appmsg><title>x</title><type>5</type>'
           '<url>https://cdn.example/x?token=REALTOKEN9&amp;idx=1</url></appmsg></msg>')
    a = appmsg.parse(xml).appmsg
    assert a.link.url is None
    # 良性查询串（`?idx=1` 不是凭据形态）不许被误丢
    benign = ('<msg><appmsg><title>x</title><type>5</type>'
              '<url>https://example.com/a?idx=1</url></appmsg></msg>')
    assert appmsg.parse(benign).appmsg.link.url is not None


def test_secret_container_names_leak_nothing():
    """回归：名字过滤只锚末段时，`<aeskey><value>…</value></aeskey>` 的末段是 `value`，
    密钥值原样进字段表（审阅发现、控制器复现）。退回只锚末段，它红。"""
    xml = ('<msg><appmsg><title>t</title><type>33</type>'
           '<aeskey><value>00112233445566778899aabbccddeeff</value></aeskey>'
           '<weappinfo><appid>a</appid></weappinfo></appmsg></msg>')
    a = appmsg.parse(xml).appmsg
    joined = " ".join(v for _, v in a.fields)
    assert "00112233445566778899aabbccddeeff" not in joined
    assert ("weappinfo.appid", "a") in a.fields      # 反假护栏：良性字段还在


def test_numeric_fields_fall_back_to_none_not_zero():
    """回归：数字字段解不开给 `None`，**不是 0**（0 是合法值，混淆两者会让
    「没有这个字段」与「值是 0」不可区分）。覆盖 `kind` 之外的**六个**字段 ——
    原先只压到三处，转账那三个改了 `or 0` 全绿（复审变异 V9）。"""
    a = appmsg.parse('<msg><appmsg><title>t</title><type>6</type><appattach>'
                     '<totallen>一兆</totallen></appattach></appmsg></msg>').appmsg
    assert a.file.totallen is None
    q = appmsg.parse('<msg><appmsg><title>t</title><type>57</type><refermsg>'
                     '<content>x</content><svrid>abc</svrid>'
                     '<createtime>昨天</createtime></refermsg></appmsg></msg>').appmsg.quote
    assert q.svrid is None and q.create_time is None
    t = appmsg.parse('<msg><appmsg><title>t</title><type>2000</type><wcpayinfo>'
                     '<paysubtype>1</paysubtype><feedesc>¥1</feedesc>'
                     '<begintransfertime>前天</begintransfertime>'
                     '<effectivedate>大前天</effectivedate>'
                     '<invalidtime>下周</invalidtime>'
                     '</wcpayinfo></appmsg></msg>').appmsg.transfer
    assert t.begin_time is None and t.effective_date is None and t.invalid_time is None


def test_user_content_is_never_subject_to_the_value_filter():
    """**边界的另一侧**：值过滤只针对**载荷自身的凭据字段**，绝不碰用户正文。

    回归：把 `_is_secret` 顺手扩到 `Quote.content` / `displayname` / `from_usr`
    （「看起来更安全」），或对 `fields` 的所有值一律过滤 —— **这条会红**。
    真发生时的症状是**用户内容静默消失**，方向与泄漏相反、且比泄漏更难被发现
    （实施期探得：把值过滤扩到 Quote 三个字段后 30 条全绿）。
    """
    xml = ('<msg><appmsg><title>看这个</title><type>57</type><refermsg>'
           '<displayname>张三</displayname>'
           '<content>你发的 https://cdn.example/x?token=SECRET9&amp;idx=1 打不开</content>'
           '<svrid>1</svrid></refermsg></appmsg></msg>')
    q = appmsg.parse(xml).appmsg.quote
    assert "token=SECRET9" in q.content, "被引用的用户正文必须原样保留"
    assert q.displayname == "张三"


def test_secret_value_separator_set_covers_semicolon_and_comma():
    """`;` 与 `,` 也在分隔符集里。真实 CDN 以 `?`/`&` 为主，这两个是覆盖缺口——
    **两个都要有断言**：只喂 `;` 的话，从字符类里去掉 `,` 不会红（复审变异 V8）。"""
    for url in ("https://cdn.example/x;token=S9",
                "https://cdn.example/x,token=S9"):
        assert appmsg.parse(
            f'<msg><appmsg><title>x</title><type>5</type><url>{url}</url></appmsg></msg>'
        ).appmsg.link.url is None
