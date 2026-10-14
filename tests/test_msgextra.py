# tests/test_msgextra.py
"""EA-041：位置（48）/ 名片（42）的结构化旁路。夹具形态**取自真机实测**（2026-10-14，202 条、
50 种结构），不是审计条目里那个自造载荷——两者的差异见 `msgextra` 的模块头。"""
import zlib

import pytest

from agent_wechat import messages, msgextra, paths, store, usecases

# 真机四种形态（值全部换成合成值，结构逐字取自实测）
_LOC_DECL = ('<?xml version="1.0"?>\n<msg><location x="39.9" y="116.4" scale="1" '
             'label="某地" maptype="0" poiname="P" poiid="pid" adcode="100000" '
             'cityname="北京" poiBusinessHour="" infourl="" /></msg>')
_LOC_BARE = ('<msg><location x="1.5" y="2.5" scale="1" label="L" maptype="0" poiname="P" '
             'isFromPoiList="false"/></msg>')
_CARD_DECL = ('<?xml version="1.0"?>\n<msg bigheadimgurl="http://h/a.jpg" username="wxid_a" '
              'nickname="甲" fullpy="jia" shortpy="J" alias="aliasA" imagestatus="3" scene="17" '
              'province="北京" city="北京" sign="签名" sex="1" certflag="0" certinfo=""'
              ' brandIconUrl="http://h/i.png" brandHomeUrl="http://h/home" brandFlags="0" '
              'regionCode="CN" biznamecardinfo="X" antispamticket="T" />')
_CARD_BARE = '<msg username="wxid_b" nickname="乙" alias="aliasB" percard="1" sex="2"/>'


def test_parse_handles_the_four_real_shapes():
    """四种真机形态：声明有无 × 群前缀有无（42 的承载元素是**根自己**，48 是子元素）。"""
    # 群前缀用**已登记**的夹具 wxid（`security/allowlist.txt`）；不要在这里造新值。
    card_grp = "wxid_friend01:\n" + _CARD_DECL
    for raw, kind in ((_LOC_DECL, 48), (_LOC_BARE, 48), (_CARD_DECL, 42), (_CARD_BARE, 42),
                      (card_grp, 42)):
        got = msgextra.parse(kind, raw)
        assert got is not None and got.kind == kind, raw[:40]
    loc = msgextra.parse(48, _LOC_DECL)
    assert dict(loc.fields)["x"] == "39.9"
    assert dict(loc.fields)["label"] == "某地"
    assert dict(msgextra.parse(42, card_grp).fields)["username"] == "wxid_a", "群前缀没剥掉"


def test_parse_keeps_only_the_allow_list():
    """**白名单**：认不出来的属性一律不进产物。

    真机实测 42 里有 `antispamticket`（最长 606 字符的加好友凭据）、`brandHomeUrl`
    （最长 2,487）、`brandSubscriptConfigUrl`（578）、`biznamecardinfo`（300）——都对阅读
    没有信息量，第一个还是每次会话一换的凭据。`appmsg` 那套「名字像密钥 / 值像不透明块」的
    过滤器抓不住它们（实测 202 条里只有 9 条 `biznamecardinfo` 命中），所以这里反过来：
    只留清单上那几个。把 `_KEEP` 换成「除了黑名单都留」这条会红。
    """
    fields = dict(msgextra.parse(42, _CARD_DECL).fields)
    assert fields["username"] == "wxid_a" and fields["nickname"] == "甲"
    for never in ("antispamticket", "brandHomeUrl", "brandSubscriptConfigUrl",
                  "biznamecardinfo", "bigheadimgurl"):
        assert never not in fields, f"{never} 不该进产物"
    # 空值是常态（实测 `certinfo`/`infourl` 有近一半是空串）：不落字段，也不落空串
    assert "certinfo" not in dict(msgextra.parse(42, _CARD_DECL).fields)


def test_field_order_is_the_allow_list_not_the_attribute_order():
    """字段序取自 `_KEEP`：XML 属性本身无序，照 ET 的字典序排会让「x 在 y 前面」这种约定
    随实现漂——而两个模板都按这个序渲染。"""
    got = [k for k, _ in msgextra.parse(48, _LOC_DECL).fields]
    assert got[:3] == ["x", "y", "scale"], got


def test_parse_is_total():
    """**对任意 str 全定义、不抛**（与 `appmsg.parse` 同契约）。"""
    for bad in ("", "not xml", "<msg><location", "<!DOCTYPE x [<!ENTITY e 'v'>]><msg/>",
                "<msg><location x='&#'" + "1" * 6000 + ";'/></msg>", None):
        assert msgextra.parse(48, bad) is None, bad
    assert msgextra.parse(49, _LOC_DECL) is None, "别的类型不归这里"
    assert msgextra.parse(48, "<msg><other/></msg>") is None, "缺承载元素"


def test_kind_name_does_not_guess():
    assert msgextra.kind_name(48) == "位置" and msgextra.kind_name(42) == "名片"
    assert msgextra.kind_name(77) == "type77"


# ---------- 旁路表：往返、幂等、排除清单拦截 ----------

@pytest.fixture()
def con(tmp_path, monkeypatch):
    """**必须**把 `store.DB_PATH` patch 掉：`store.connect()` 会执行整个 SCHEMA，
    指错路径就是把 DDL 写进用户真实的 index.db（与 `test_chat_export` 同一条红线）。"""
    db = tmp_path / "index.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path / "home")
    (tmp_path / "home").mkdir()
    c = store.connect(db)
    yield c
    c.close()


def test_extras_round_trip(con):
    key = "acct|aaa|message_0.db|1"
    blob = zlib.compress(_LOC_DECL.encode(), 6)
    assert store.upsert_extras(con, [(key, 48, blob)]) == 0
    con.commit()
    got = store.extras_by_keys(con, [key, "acct|aaa|message_0.db|999"])
    assert got[key][0] == 48 and got[key][1] == blob
    assert "acct|aaa|message_0.db|999" not in got, "查不到的 key 不出现在结果里"
    # 幂等：同一个 key 再写一遍是 DO UPDATE，不是第二行
    assert store.upsert_extras(con, [(key, 48, b"<msg/>")]) == 0
    con.commit()
    assert store.extras_by_keys(con, [key])[key][1] == b"<msg/>"


def test_upsert_extras_skips_excluded_keys(con):
    """**排除清单拦截**（spec §5.4 第 2 条）：回填命令直接回微信库重扫，不拦的话
    `purge --chat` 删掉的位置/名片载荷会重新长回来。与 `upsert_msg_parts` 同一条判据。"""
    key = f"acct|{_CHAT_MD5}|message_0.db|1"
    from agent_wechat import exclusions
    _seed_message_row(con, key, 1)
    exclusions.add_from_messages(con, "acct", "c")
    n = store.upsert_extras(con, [(key, 48, b"<msg/>")])
    con.commit()
    assert n == 1, "被排除的 key 必须被挡下"
    assert store.extras_by_keys(con, [key]) == {}


def test_extras_counted_and_deleted_by_msg_key_prefix(con):
    """`chat_row_counts` / `delete_chat` 对第八张表的接线（两处都是宽谓词，孤儿行照样盖得住）。"""
    key = f"acct|{_CHAT_MD5}|message_0.db|1"
    orphan = f"acct|{_CHAT_MD5}|message_0.db|90"      # 消息行不在，载荷在（回填只写这张表）
    store.upsert_extras(con, [(key, 48, b"<msg/>"), (orphan, 42, b"<msg/>")])
    con.commit()
    assert store.chat_row_counts(con, "acct", "c")["extras"] == 2
    got = store.delete_chat(con, account="acct", chat_id="c")
    con.commit()
    assert got["extras"] == 2 and store.chat_row_counts(con, "acct", "c")["extras"] == 0
    assert store.extras_by_keys(con, [key, orphan]) == {}, "孤儿行也要被前缀谓词扫掉"


# ---------- 回填命令 ----------

_CHAT_MD5 = messages.chat_md5('c')


def _seed_message_row(con, msg_key: str, lid: int) -> None:
    """种一行 messages（走 `upsert_batch`，不手写 INSERT——列的 NOT NULL 约束会跟着 schema 漂）。"""
    store.upsert_batch(con, [{"msg_key": msg_key, "account": "acct",
                              "chat_md5": _CHAT_MD5, "chat_id": "c", "shard": "message_0.db",
                              "local_id": lid, "server_id": lid, "ts": 1, "msg_type": 48,
                              "sender_id": None, "sender_name": None, "is_self": 0,
                              "content": "", "media_hint": None}])


class _FakeReader:
    """替掉 MessageReader：只提供 `iter_extras_input`（同 `test_chat_export` 的 `_FakeReader`）。"""
    def __init__(self, rows):
        self._rows = rows

    def iter_extras_input(self):
        yield from self._rows


def _ctx(rows):
    return lambda acct=None: type("C", (), {"account": "acct", "reader": _FakeReader(rows)})()


def test_rebuild_writes_extras_and_counts_the_empty_ones(con, monkeypatch):
    """`extras` 与 `empty` 是**两个分母**：前者是实际落库的条数，后者是「落库了但没有可读
    字段」的条数。把 `empty` 并进 `extras`（或去掉那个分支）本用例会红。"""
    rows = [
        ("acct|aaa|message_0.db|1", 48, zlib.compress(_LOC_DECL.encode(), 6)),
        ("acct|aaa|message_0.db|2", 42, zlib.compress(b"<msg><other/></msg>", 6)),
        ("acct|aaa|message_0.db|3", 48, zlib.compress(_LOC_BARE.encode(), 6)),
    ]
    monkeypatch.setattr(usecases, "ChatContext", _ctx(rows))
    res = usecases.msgextra_rebuild(account="acct")
    assert res == {"extras": 3, "empty": 1, "skipped": 0}, res
    got = store.extras_by_keys(con, ["acct|aaa|message_0.db|1"])
    assert got["acct|aaa|message_0.db|1"][1] == zlib.compress(_LOC_DECL.encode(), 6)


def test_rebuild_is_idempotent(con, monkeypatch):
    rows = [("acct|aaa|message_0.db|1", 48, zlib.compress(_LOC_BARE.encode(), 6))]
    monkeypatch.setattr(usecases, "ChatContext", _ctx(rows))
    first = usecases.msgextra_rebuild(account="acct")
    second = usecases.msgextra_rebuild(account="acct")
    assert first["extras"] == second["extras"] == 1
    assert con.execute("SELECT count(*) FROM msg_extras").fetchone()[0] == 1, "幂等：不是两行"


def test_rebuild_reports_skipped_rows_through_the_callback(con, monkeypatch):
    """被排除清单挡下的行：`skipped` 计数 + `on_skipped` 回调（「成功但有保留」的出口，
    与 `appmsg rebuild` / `media rebuild-refs` 同款）。"""
    key = f"acct|{_CHAT_MD5}|message_0.db|1"
    _seed_message_row(con, key, 1)
    from agent_wechat import exclusions
    exclusions.add_from_messages(con, "acct", "c")
    con.commit()
    seen: list[int] = []
    monkeypatch.setattr(usecases, "ChatContext",
                        _ctx([(key, 48, zlib.compress(_LOC_BARE.encode(), 6))]))
    res = usecases.msgextra_rebuild(account="acct", on_skipped=seen.append)
    assert res["extras"] == 0 and res["skipped"] == 1 and seen == [1]
    assert store.extras_by_keys(con, [key]) == {}


def test_a_missing_table_is_not_an_error(con):
    """**老库兼容**：`msg_extras` 是随 SCHEMA 一起 `CREATE TABLE IF NOT EXISTS` 建的，
    所以既有 `index.db` 在下一次 `connect()` 时就有了它（本夹具正是这条路径：先 connect
    再查表）。这里钉的是「新建的表是空的、读它不抛」——渲染层据此把「没有载荷」与
    「载荷解析成空」分开。"""
    assert con.execute("SELECT count(*) FROM msg_extras").fetchone()[0] == 0
    assert store.extras_by_keys(con, ["acct|aaa|message_0.db|1"]) == {}
    assert store.chat_row_counts(con, "acct", "c")["extras"] == 0


def test_the_secret_gate_drops_a_credential_carrying_value():
    """**合成值**夹具（复审 T14 的 N3）：白名单之外还有一道凭据闸（`appmsg.is_secret`，
    与 `fields` 同一份判据）。真机 202 条上它**从不触发**——所以照真机造夹具抓不到它，
    必须自造：`?token=` / `&filekey=` 这类参数名会被 `_SECRET_SEG` 逐段命中。

    `_KEEP` 是这一族的第一道防线，但它是**名字**层面的；值层面的凭据只能靠这道闸。
    把 `parse` 里那行 `is_secret` 删掉，本用例红。
    """
    # `&filekey=` 在 XML 里必须写成 `&amp;filekey=`（真机也是转义过的形态）——写成裸 `&`
    # 的话 `ET` 直接判 malformed，这条用例就变成在测「解不开」而不是测那道闸了（实测踩过）。
    raw = ('<msg><location x="1" y="2" label="某地?token=SECRET123" '
           'poiname="A&amp;filekey=abc" adcode="100000"/></msg>')
    fields = dict(msgextra.parse(48, raw).fields)
    assert "label" not in fields and "poiname" not in fields, "带凭据参数的值必须被丢掉"
    assert fields["adcode"] == "100000", "干净的值照常保留（别把闸做成一刀切）"


def test_a_corrupt_payload_in_the_table_does_not_raise(con, monkeypatch):
    """`_decode_extra` 的失败面（复审 T14 的 N3）：`payload` 是**别人写进去的字节**
    （索引库可被手工改动、半拷贝的 WAL 会落下半页），裸 `zlib.error` / `UnicodeDecodeError`
    不得逃出 core 层。三条失败支路都返回 None → 该 key 不进 `extras` → 模板回落印占位。

    仓库里此前没有任何用例往 `msg_extras` 写坏载荷（`msg_parts` 那边有先例：
    `test_chat_export_counts_unparsed_and_rejected_separately`）。把 `_decode_extra` 的
    `try/except` 删掉，本用例红。
    """
    bad = [("acct|aaa|message_0.db|1", 48, b"\x00\x01 not-zlib"),
           ("acct|aaa|message_0.db|2", 48, zlib.compress(b"<msg><location x='&#'" + b"1" * 6000
                                                         + b";'/></msg>", 6)),
           ("acct|aaa|message_0.db|3", 48, zlib.compress(b"not xml at all", 6))]
    for key, t, blob in bad:
        assert usecases._decode_extra(t, blob) is None, key
    # 真路径：写进表再读回来，不抛
    store.upsert_extras(con, bad)
    con.commit()
    got = store.extras_by_keys(con, [k for k, _, _ in bad])
    assert len(got) == 3
    assert [usecases._decode_extra(t, b) for t, b in got.values()] == [None, None, None]
