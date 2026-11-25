"""消息读取层：基于实测 schema（docs/schema-4.1.13.md）解码微信 4.1.13 库。

要点：
- 消息表 Msg_<md5(chat_username)>，每会话一张，跨分片按时间分区
- content: WCDB_CT=0 明文 / 4=zstd；群消息前缀 "<sender>:\n"
- local_type 需 & 0xffffffff；real_sender_id 经本库 Name2Id 解析
"""
from __future__ import annotations

import contextlib
import dataclasses
import html
import pathlib
import sqlite3
import threading
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, asdict
from typing import Any

from . import (appmsg, decrypt, errors, keys, media, messages, msgextra,
                paths, roster, schema, store)
from .errors import DataNotFound, DependencyMissing

try:
    import zstandard
    _HAS_ZSTD = True
except ImportError:  # pragma: no cover
    _HAS_ZSTD = False

# **每线程一个**解压器（Phase 03 验收发现）：`ZstdDecompressor` 不是线程安全的——同一个实例
# 派生的 decompressobj 共用底层 DCtx。此前是模块级单例，MCP 里后台线程跑导出、工具调用
# 并发读近期消息时，两边同时解码就会互相踩上下文：8 线程实测偶发「Data corruption detected」，
# 压得更紧时整个进程段错误（基线 97b78dc 同样复现，与解密后端无关）。
_ZSTD_LOCAL = threading.local()


def _zstd() -> zstandard.ZstdDecompressor | None:
    if not _HAS_ZSTD:
        return None
    d = getattr(_ZSTD_LOCAL, "d", None)
    if d is None:
        d = _ZSTD_LOCAL.d = zstandard.ZstdDecompressor()
    return d

# 类型词汇表的 owner 是 messages.py；此处转出以保持既有引用点可用。
TYPE_NAMES = messages.TYPE_NAMES

# 导出/回看读的列清单（含 packed_info_data 与 server_id 之外的两个媒体列）。
# 两处 SELECT 共用一个常量：列清单漂了，回看与前向会对同一条消息给出不同的 refs。
_EXPORT_COLUMNS = ("local_id, local_type, real_sender_id, create_time, "
                   "message_content, WCDB_CT_message_content, packed_info_data")


@dataclass
class Message:
    chat_id: str
    shard: str
    local_id: int
    ts: int
    msg_type: int
    type_name: str
    sender_id: str | None
    sender_name: str | None
    is_self: bool
    content: str | None
    media_hint: str | None
    sticker_md5: str | None = None   # 仅 msg_type=47 且 XML 里有合法 md5 时非空
    msg_key: str = ""          # 由 _read_rows 赋（需 account + chat_md5 + shard + local_id 四者齐备）

    def to_dict(self) -> dict:
        return asdict(self)


# `media_refs` 的一行：(msg_key, kind, file_key, extra)。前向扫描与回看共用（见 `_batched`）。
RefRow = tuple[str, str, str | None, str | None]


def _decode_content(raw: bytes | str | None, ct_flag: int | None) -> str | None:
    """WCDB 压缩列解码：CT=0 明文，CT=4 zstd。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if ct_flag == 4:
        dctx = _zstd()
        if dctx is None:
            raise DependencyMissing("缺少 zstandard：uv add zstandard")
        try:
            # `decompressobj` 而不是 `decompress`：后者遇到头部不带内容长度的**合法**帧直接抛
            # 「could not determine content size」。代价是截断帧它只吐半截、**不抛**——所以
            # 还要看 `eof`：没有走到帧尾就是坏数据，不许把半截正文当正文静默入库。
            dobj = dctx.decompressobj()
            out = dobj.decompress(raw)
            if not dobj.eof:
                raise errors.DecryptError(
                    "消息正文解压失败（zstd 帧不完整：数据被截断）",
                    hint="该条消息在微信库里可能已损坏；等微信完成同步后重试，"
                         "仍失败请附带这条提示反馈")
            return out.decode("utf-8", "replace")
        except zstandard.ZstdError as exc:
            # core 只抛项目异常（ARCHITECTURE）：裸 ZstdError 会以 traceback 穿到入口。
            raise errors.DecryptError(
                f"消息正文解压失败（zstd）：{exc}",
                hint="该条消息在微信库里可能已损坏；等微信完成同步后重试，仍失败请附带此信息反馈"
            ) from exc
    return raw.decode("utf-8", "replace")


def _type_name(t: int) -> str:
    return TYPE_NAMES.get(t, f"type{t}")


def _media_hint(t: int, content: str | None) -> str | None:
    if t == messages.TYPE_TEXT or t in (messages.TYPE_SYSTEM, messages.TYPE_REVOKE):
        return None
    if t == messages.TYPE_APPMSG and content and "<title>" in content:
        raw = content.split("<title>", 1)[1].split("</title>", 1)[0].strip()
        # 这是 XML 的文本节点：CDATA 去壳（内容按字面），否则做实体反转义——此前
        # `Q&amp;A` 原样入库，FTS 搜「Q&A」搜不到、展示也带着 `&amp;`。不走
        # `appmsg.parse`：这里是逐行导出的热路径，为一个标题解析整段 XML 不划算。
        if raw.startswith("<![CDATA[") and raw.endswith("]]>"):
            title = raw[len("<![CDATA["):-len("]]>")].strip()
        else:
            title = html.unescape(raw)
        return messages.appmsg_hint(title)
    return f"[{_type_name(t)}]"


# 常驻的 MCP server 里跨工具调用复用 contact.db 的读取结果（联系人查询、单个会话的名册、
# 显示名）。签名是库文件与 -wal 的 (inode, 大小, mtime_ns)：微信一写库签名就变，整份作废。
# 只放一个账号；条目数设上限，满了清空（查询串五花八门，不必做 LRU）。
_CONTACT_CACHE_MAX = 64
_contact_cache: dict[str, Any] = {"sig": None, "items": {}}
_contact_lock = threading.Lock()


def _file_sig(db: pathlib.Path) -> tuple:
    out: list[object] = [str(db)]
    for p in (db, db.with_name(db.name + "-wal")):
        try:
            st = p.stat()
            out.append((st.st_ino, st.st_size, st.st_mtime_ns))
        except OSError:
            out.append(None)
    return tuple(out)


# 按 username 精确取行时 TEXT 与 BLOB 两种存储类别都认：SQLite 里 TEXT 参数与 BLOB 值永不
# 相等，而 `text_factory = bytes` 读出来一律是 bytes，看不出微信存的是哪一种。
_ONE_NAME = "(?, ?)"


def _name_args(name: str) -> tuple[str, bytes]:
    return name, name.encode("utf-8")


def _s(v: object) -> str | None:
    if v is None:
        return None
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)


def is_chat_id(s: str) -> bool:
    """wxid / 群 / 公众号 / filehelper 形态的 username 可直接当会话 id 用，无需解析。"""
    return s.endswith("@chatroom") or s.startswith(("wxid_", "gh_", "filehelper"))


def _db_matches(pattern: str, rel: str) -> bool:
    """`"message/*.db"` 匹配 `"message/message_0.db"`；其余是精确匹配。"""
    if pattern.endswith("/*.db"):
        return rel.startswith(pattern[:-4]) and rel.endswith(".db")   # 留下 "message/"
    return pattern == rel


class MessageReader:
    """按账号读取消息。只读微信目录；解密副本用后即焚。"""

    def __init__(self, acct: str) -> None:
        self.acct = acct
        # passphrase **惰性**取（见 `passphrase`）：构造 reader 不碰密钥缓存。
        self._passphrase: bytes | None = None
        self.self_wxid = paths.bare_wxid(acct)
        self._contacts_all: list[dict] | None = None   # 实例级缓存，见 _contacts_all_cached
        self._probed: set[tuple[str, str, str]] = set()   # (rel, capability, only or table)
        self._drifted: dict[tuple[str, str, str], errors.SchemaDrift] = {}
        self._drift: list[errors.SchemaDrift] = []        # 本次实例见过的漂移（T5 读）
        # 非漂移成因的源级降级（F2，T8 复审）：`_drift` 只装 SchemaDrift，而「文件在、
        # 解不开」是 DecryptError——不记的话会话名静默落 `md5:<hex>`，用户只知道名字丢了
        # 一些，不知道去修哪个库。`collect_degradations` 把两者合并成 degraded 条目。
        self._source_notes: list[tuple[str, str]] = []
        # 上一次 `iter_all` 的回看游标：{(shard, chat_md5): 下一个位置}。由导出在数据
        # 提交后落盘；在这里初始化，是为了让「fake reader 忘了它」变成响亮的
        # AttributeError，而不是静默地永远不推进游标。
        self.audit_marks: dict[tuple[str, str], int] = {}

    def _contacts_all_cached(self) -> list[dict]:
        """**全量**联系人（不带 `LIMIT`）。同一 reader 生命周期内只解密一次 contact.db
        （resolve_chat + display_name 常在同一次工具调用里先后执行）。

        **必须全量**：真机 contact 表 12,799 行，而 `resolve_chat` / `display_name` 都扫它。
        取前 5000 行（按 `remark, nick_name` 排序）会让窗口外的 **61%** 联系人按真名解析不出来
        ——`recent --contact X` / `search --contact X` / `chat export --chat X` 全线失效，
        展示名静默回落裸 id。与 `roster()` 同源同理由（见它那句「**不带 limit**」）。

        （原实现是 `contacts(limit=5000)`，而 docstring 已自称「全量」——名与实不符。）
        """
        if self._contacts_all is None:
            self._contacts_all = self.contacts(limit=None)   # **必须显式传 None**：不传就是默认 200
        return self._contacts_all

    def _contact_memo(self, key: tuple, compute: Callable[[], Any]) -> Any:
        """跨调用复用 contact.db 的读取结果（见模块级 `_contact_cache`）。

        这次计算里记下了漂移或源级降级的**不缓存**：降级说明挂在 reader 实例上，命中缓存的
        下一次调用拿不到它，名字静默回落 wxid 却没有 degraded——宁可再读一次库。
        """
        sig = _file_sig(paths.db_storage(self.acct) / "contact/contact.db")
        with _contact_lock:
            if _contact_cache["sig"] == sig and key in _contact_cache["items"]:
                return _contact_cache["items"][key]
        notes = len(self._drift) + len(self._source_notes)
        val = compute()
        if len(self._drift) + len(self._source_notes) == notes:
            with _contact_lock:
                if _contact_cache["sig"] != sig or len(_contact_cache["items"]) >= _CONTACT_CACHE_MAX:
                    _contact_cache["sig"], _contact_cache["items"] = sig, {}
                _contact_cache["items"][key] = val
        return val

    @property
    def passphrase(self) -> bytes:
        """第一次真正要解密时才读密钥缓存。

        此前在 `__init__` 里读：构造 reader 就需要密钥，于是只碰本地索引的用例（contact 已是
        chat_id 的 `get_message_context`、工作台 `/api/media`、只为收降级而访问 `ctx.reader`）
        也得在每个调用方各自绕开构造——同一条规则守在 N 处，下一个调用方照漏。缓存不在时
        照旧抛 `KeyNotFound`，只是时机挪到首次 `_open`。
        """
        if self._passphrase is None:
            self._passphrase = keys.get_passphrase(self.acct)
        return self._passphrase

    @contextlib.contextmanager
    def _open(self, rel: str) -> Iterator[decrypt.ReadConnection]:
        db = paths.db_storage(self.acct) / rel
        if not db.exists():
            raise DataNotFound(f"{db} 不存在")
        with decrypt.open_db(db, self.passphrase) as con:
            con.text_factory = bytes   # 库内 TEXT 含非 UTF-8 字节，统一按 bytes 取
            yield con

    def prewarm_keys(self) -> int:
        """预先派生会话、联系人、表情与各消息分片的密钥（见 `decrypt.prewarm_keys`）。"""
        ds = paths.db_storage(self.acct)
        return decrypt.prewarm_keys([ds / "session" / "session.db", ds / "contact" / "contact.db",
                                     ds / "emoticon" / "emoticon.db",
                                     *paths.message_shards(self.acct)], self.passphrase)

    def _check(self, capability: str, rel: str, con: decrypt.ReadConnection, *,
               table: str | None = None, only: str | None = None) -> None:
        """校验该能力在**这个库**上的声明，缺了抛 `SchemaDrift`。

        缓存键含表名：`Msg_*` 的真名是 md5，同一个 capability 在不同分片上对应不同的表。

        通配声明（`Msg_*`）的抽样规则：调用方先把该库里真实存在的匹配表名传进来
        （`table=`）；一条也没有时调用方**不该调这个方法**——那是「没有消息」而不是
        「结构漂移」（spec §4 的陷阱）。

        `only=` 只校验**这一张表**的声明：`roster` 要逐源降级（T4），没有它一次调用
        会走完该能力的全部声明、第一条漂移就抛，后面的源永远记不上（`chat_room` 与
        `contact` 物理无关，少一个仍有另一个）。

        **漂了也要按类型重抛（`_drifted`）**：结论是实例级缓存的，命中就静默放行的话，
        第二次调用会绕开 guard、撞上后面那条裸 SQL——抛的是 `sqlite3.OperationalError`
        而不是 `SchemaDrift`（同一个 reader 上第二次 `roster()` 就是这种形状）。
        """
        key = (rel, capability, only or table or "")
        if key in self._probed:
            return
        if key in self._drifted:               # 探过且漂了：重抛同一个漂移，不重新探
            raise self._drifted[key]
        for req in schema.CAPABILITIES[capability]:
            if not _db_matches(req.db, rel):
                continue                       # 这条声明属于另一个库
            if only is not None and req.table != only:
                continue                       # 只校验这一张表的声明（roster 逐源降级）
            # `table=` 只对**通配**声明有意义（`Msg_*` 的真名运行时才知道）；
            # 精确表名的声明用它自己的表名——否则探 chat_room 时会拿这个名字
            # 去探 name2id 的声明，得到的是无关的结论。
            effective = (table or req.table) if req.table.endswith("*") else req.table
            result = schema.probe(con, req, effective)
            if not result.ok:
                # 报 `effective` 而不是 `table or req.table`：`Name2Id` 缺了的时候
                # 后者会点名 `Msg_<md5>`——一张**存在**的表，用户会查错方向。
                drift = errors.SchemaDrift(capability, rel, effective,
                                           result.missing_columns)
                # 漂移单独存一份（不进 `_probed`，否则重抛那条分支永远走不到）——
                # 它同时也是缓存：同一个库里不必反复探。
                self._drifted[key] = drift
                self._record_drift(drift)
                raise drift
        self._probed.add(key)

    def _record_source_degrade(self, capability: str, rel: str) -> None:
        """记一条**非漂移**的源级降级（F2，T8 复审）。同一 `(capability, rel)` 只记一条。"""
        if (capability, rel) not in self._source_notes:
            self._source_notes.append((capability, rel))

    def _record_drift(self, drift: errors.SchemaDrift) -> None:
        """把漂移记进 `self._drift`（T5 读），**同一 `(capability, table)` 只记一条**。

        `_check` 的缓存键含 `rel`，而 `_drift` 的粒度是 `(capability, table)`：重抛那条
        路径不会再记一次，真正的重复来自多分片——`username_pool` 逐片探（每片一条
        `message/*.db` 声明），两片都缺 `Name2Id` 说的是同一件事，不去重用户就看到两行
        一样的降级说明（T5 的去重只是兜底，不该靠它兜）。
        """
        if not any(d.capability == drift.capability and d.table == drift.table
                   for d in self._drift):
            self._drift.append(drift)

    # ---------- 会话 / 联系人 ----------

    def username_pool(self) -> set[str]:
        """全部已知用户名（session + contact + 各分片 Name2Id），供 md5 反查。

        **降级的能力**：源之间彼此独立，缺一个仍有其余（spec §6）——漂移的那一支跳过，
        继续用剩下的。抛在这里等于让「少一个源」变成「什么都查不到」。
        """
        pool: set[str] = {self.self_wxid, "filehelper"}
        for rel, sql in (("session/session.db", "SELECT username FROM SessionTable"),
                         ("contact/contact.db", "SELECT username FROM name2id")):
            try:
                with self._open(rel) as con:
                    self._check("username_pool", rel, con)
                    pool |= {u.decode("utf-8", "replace")
                             for (u,) in con.execute(sql) if isinstance(u, bytes)}
            except (DataNotFound, errors.SchemaDrift):
                continue
            except errors.DecryptError:
                # EA-079 + F2（T8 复审）：纯名字源「文件在、**解不开**」（换钥/半截文件/
                # 损坏）→ 跳过（池子仍有其余源，解析不出的会话走既有的 `md5:<hex>` 回落），
                # 但**留一条降级记录**（非漂移成因）——否则这次降级对用户完全不可见。
                # 下面**分片源不加**这一条：跳过会让时间线静默出现空洞。
                self._record_source_degrade("username_pool", rel)
                continue
        for shard in paths.message_shards(self.acct):
            rel = f"message/{shard.name}"
            try:
                with self._open(rel) as con:
                    self._check("username_pool", rel, con)
                    pool |= {u.decode("utf-8", "replace")
                             for (u,) in con.execute("SELECT user_name FROM Name2Id")}
            except (DataNotFound, errors.SchemaDrift):
                continue
        return pool

    def md5_map(self) -> dict[str, str]:
        """md5(chat_username) -> chat_username。"""
        return {messages.md5_of(u): u for u in self.username_pool()}

    def contacts(self, query: str | None = None, limit: int | None = 200) -> list[dict]:
        """联系人搜索：备注/昵称/微信号模糊匹配。

        `limit=None` → **不拼 `LIMIT`**（取全量）。`_contacts_all_cached` 用它：真机 contact 表
        有 12,799 行，而 `resolve_chat` / `display_name` 都扫这份数据——取窗口会让**窗口外的
        联系人按真名解析不出来**（实测窗口外抽 40 条，`resolve_one` 命中 0）。

        结果跨调用复用（`_contact_memo`，contact.db 一变就作废）；返回的是副本，调用方改了也
        不会污染缓存。
        """
        rows = self._contact_memo(("contacts", query or None, limit),
                                  lambda: self._contacts_uncached(query, limit))
        return [dict(r) for r in rows]

    def _contacts_uncached(self, query: str | None, limit: int | None) -> list[dict]:
        with self._open("contact/contact.db") as con:
            self._check("contacts", "contact/contact.db", con)
            sql = ("SELECT username, alias, remark, nick_name FROM contact "
                   "WHERE COALESCE(delete_flag,0)=0")
            args: list = []
            if query:
                sql += " AND (remark LIKE ? OR nick_name LIKE ? OR alias LIKE ? OR username LIKE ?)"
                args = [f"%{query}%"] * 4
            sql += " ORDER BY remark, nick_name"
            if limit is not None:            # None = 全量（见 docstring）
                sql += " LIMIT ?"
                args.append(limit)
            out = []
            for u, alias, remark, nick in con.execute(sql, args):
                u, remark, nick, alias = _s(u), _s(remark), _s(nick), _s(alias)
                if u is None:
                    continue                 # 没有 username 的行既不能解析也不能展示
                out.append({"username": u, "display": remark or nick or u,
                            "remark": remark, "nick_name": nick, "alias": alias,
                            "kind": ("group" if u.endswith("@chatroom")
                                     else "official" if u.startswith("gh_") else "direct")})
            return out

    def resolve_chat(self, name_or_id: str) -> list[dict]:
        """把口述名字解析成会话 id 候选。"""
        q = name_or_id.lower()
        return [c for c in self._contacts_all_cached()
                if q in (c["display"] or "").lower()
                or q in (c["username"] or "").lower()]

    def resolve_one(self, name_or_id: str) -> tuple[str | None, list[dict]]:
        """名字/ID → 唯一会话 id。返回 (chat_id, 候选列表)；唯一命中时 chat_id 非 None。"""
        if is_chat_id(name_or_id):
            return name_or_id, []
        cands = self.resolve_chat(name_or_id)
        # 自定义微信号形态上认不出（见 is_chat_id），子串匹配又常带出 `zhangsan2` 这类同前缀的
        # 人。**整串等于某人的 username** 就是指名——除非另有人的展示名也整串等于它（那才真歧义）。
        if (any(c["username"] == name_or_id for c in cands)
                and not any(c["display"] == name_or_id and c["username"] != name_or_id
                            for c in cands)):
            return name_or_id, []
        return (cands[0]["username"] if len(cands) == 1 else None), cands

    def display_name(self, chat_id: str) -> str:
        """实例里已有全量联系人就直接查；没有就只读这一个人——真机全表 1.2 万行，为一个名字
        读一遍要约 0.5 s。规则与 `contacts()` 相同：备注 > 昵称 > username。"""
        if self._contacts_all is not None:
            return next((c["display"] for c in self._contacts_all
                         if c["username"] == chat_id), chat_id)
        return self._contact_memo(("display", chat_id), lambda: self._display_uncached(chat_id))

    def _display_uncached(self, chat_id: str) -> str:
        with self._open("contact/contact.db") as con:
            self._check("contacts", "contact/contact.db", con)
            for u, remark, nick in con.execute(
                    "SELECT username, remark, nick_name FROM contact "
                    f"WHERE COALESCE(delete_flag,0)=0 AND username IN {_ONE_NAME}",
                    _name_args(chat_id)):
                if _s(u) == chat_id:
                    return _s(remark) or _s(nick) or chat_id
        return chat_id

    def roster(self) -> list[roster.RosterRow]:
        """本账号的显示名名册。**逐源降级**（spec §6）：`chat_room` 与 `contact` 在物理上
        无关，少一个仍有另一个——整体降级会把「还能给你群昵称」白白丢掉。

        漂移记进 `self._drift`（不抛），由调用方（usecases）决定怎么呈现。
        两次查询在同一次 contact.db 解密里完成，由 `_open` 管理生命周期。
        名册的**规则**不在这里——`roster.build_rows` 是全项目唯一那份。
        """
        return self._contact_memo(("roster",), lambda: self._roster_rows(None))

    def roster_for(self, chat_id: str) -> list[roster.RosterRow]:
        """只算一个会话要用的名册行：这个群的成员（私聊就是对方一人）及其联系人信息。

        `recent` 只换一个会话的发送者名，却要解析全部群的成员表（真机 230 个群约 1.4 s）。
        结果是 `roster()` 里该会话那部分的超集，规则同为 `roster.build_rows`。
        """
        return self._contact_memo(("roster_for", chat_id), lambda: self._roster_rows(chat_id))

    def _roster_rows(self, only_chat: str | None) -> list[roster.RosterRow]:
        groups: dict[str, list[tuple[str, str | None]]] = {}
        contacts: dict[str, tuple[str | None, str | None]] = {}
        with self._open("contact/contact.db") as con:
            # 源一：群成员与群昵称（`name2id` 被下一条 SQL 的 JOIN 用到，属同一个源）
            try:
                self._check("roster", "contact/contact.db", con, only="chat_room")
                self._check("roster", "contact/contact.db", con, only="name2id")
                # chat_room.id → name2id.rowid → 群 username（实测 3/3 相等，spec §2.1）
                sql = ("SELECT n.username, cr.ext_buffer FROM chat_room cr "
                       "JOIN name2id n ON n.rowid = cr.id")
                args: tuple = ()
                if only_chat is not None:
                    sql, args = sql + f" WHERE n.username IN {_ONE_NAME}", _name_args(only_chat)
                for username, blob in con.execute(sql, args):
                    chat_id = _s(username)
                    if chat_id:
                        groups[chat_id] = roster.parse_members(
                            blob if isinstance(blob, bytes) else None)
            except errors.SchemaDrift:
                pass                      # 群昵称不可用；私聊行仍可产出
            # 源二：全部联系人（私聊行 + 群成员的名字来源）
            try:
                self._check("roster", "contact/contact.db", con, only="contact")
                # **不带 limit**：真实 contact 表有 12,782 行——任何窗口都会让名册静默漏人
                # （spec §2.6；`_contacts_all_cached()` 当年取前 5000 行就是这么漏的）。
                # 只算一个会话时按名单精确取（对方本人 + 群成员），名单之外的人用不上。
                sql = ("SELECT username, remark, nick_name FROM contact "
                       "WHERE COALESCE(delete_flag,0)=0")
                args = ()
                if only_chat is not None:
                    names = {only_chat, *(k for ms in groups.values() for k, _ in ms if k)}
                    args = tuple(a for n in sorted(names) for a in _name_args(n))
                    sql += f" AND username IN ({','.join('?' * len(args))})"
                for u, remark, nick in con.execute(sql, args):
                    key = _s(u)
                    if key:
                        contacts[key] = (_s(remark), _s(nick))
            except errors.SchemaDrift:
                pass                      # 备注/昵称不可用；群昵称仍可产出
        return roster.build_rows(groups, contacts)

    def sticker_captions(self) -> dict[str, str]:
        """`md5 → 中文名`，来自 `emoticon/emoticon.db`。

        **降级的能力**（与 `roster()` 同一条理由）：库缺失、结构漂移、解密失败——
        一律返回空表，绝不抛。贴纸没有名字仍要能导出，只是回落短 ID。

        `language_` 实测**全是 `'default'`**（925/925），不是 `'zh_cn'`——这是微信
        自己的表，语言列名与 XML 里 `desc` 的多语言结构不是一回事。仍按
        `language_='default'` 精确筛：漏筛会把同一 md5 因未来多语言而重复计入。

        调用方**只需调一次**：925 行的载入成本很低，但导出循环里每批调一次是浪费。
        `iter_all` 那条路径由调用方在循环外取好再传下去（见 usecases）。
        """
        out: dict[str, str] = {}
        try:
            with self._open("emoticon/emoticon.db") as con:
                self._check("stickers", "emoticon/emoticon.db", con)
                for md5, caption in con.execute(
                        "SELECT md5_, caption_ FROM kStoreEmoticonCaptionsTable "
                        "WHERE language_='default'"):
                    k, v = _s(md5), _s(caption)
                    if k and v:
                        out[k.lower()] = v
        except (DataNotFound, errors.SchemaDrift, errors.AgentWeChatError, sqlite3.Error):
            return {}
        return out

    def _apply_roster(self, chat_id: str, msgs: list[Message]) -> list[Message]:
        """把发送者换成名册里的显示名。

        **只给 `recent` 用**。不要挂到 `_decode_row` 或 `iter_all` 上：那条路径的产物
        要写进索引库，而索引库侧是由查询时的 join 统一覆盖的——在这里换名会让新导出
        存名字、存量存 wxid，两边分道扬镳（spec §6、§7）。
        """
        mapping = {(c, k): d for c, k, d in self.roster_for(chat_id)}
        out: list[Message] = []
        for m in msgs:
            name = mapping.get((chat_id, m.sender_id or ""))
            out.append(dataclasses.replace(m, sender_name=name) if name else m)
        return out

    # ---------- 消息 ----------

    def _name2id(self, con: decrypt.ReadConnection) -> dict[int, str]:
        return {rowid: name for rowid, u in con.execute("SELECT rowid, user_name FROM Name2Id")
                if (name := _s(u)) is not None}

    def _shard_tables(self, con: decrypt.ReadConnection, chat_md5: str | None) -> list[str]:
        if chat_md5:
            return [f"Msg_{chat_md5}"] if con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (f"Msg_{chat_md5}",)).fetchone() else []
        return [r[0].decode() for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%' ORDER BY name")]

    def _decode_row(self, lid: int, ltype: int, sender: int | None, ts: int,
                    mc: bytes | str | None, ct: int | None, md5hex: str,
                    chat_map: Mapping[str, str], n2i: Mapping[int, str],
                    sticker_names: Mapping[str, str] | None = None) -> Message:
        """单行解码：压缩、类型掩码、群前缀、媒体提示。所有读取路径共用此函数。"""
        chat_id = chat_map.get(md5hex) or messages.degraded_chat_id(md5hex)
        content = _decode_content(mc, ct)
        t = ltype & 0xffffffff
        sender_u, sender_name = (n2i.get(sender) if sender is not None else None), None
        if chat_id.endswith("@chatroom") and content:
            head, sep, rest = content.partition(":\n")
            if sep and head and "\n" not in head:   # 群消息 "<X>:\n正文" 前缀
                if t == messages.TYPE_TEXT:
                    sender_u, sender_name, content = head, head, rest
                elif head.endswith("@chatroom"):
                    # 非文本：head 是群 id 的**残留**前缀（实测 1,952 条，全是 10000），
                    # 只剥正文、**不当发送者**——head 是群 id，当成 sender 会污染 sender_id。
                    # 收窄到 @chatroom 形态是因为唯一的假阳性（群 appmsg 的标题自带 ":\n"）
                    # 其 head 是 XML：放成「非文本一律剥」会把那条的标题削掉（实测 1 条）。
                    # 剥掉后正文若整段是 XML，下面的兜底会把它换成占位符（R9）。
                    content = rest
        # 贴纸：`content` 变成可读串，`media_hint` **不动**（spec D5）。
        # XML 只在这里解一次——`iter_all` 走同一条路径，所以索引里的 content
        # 与实时 `recent` 是一致的，不会一个显示标识一个显示 `[sticker]`。
        sticker_md5 = None
        if t == messages.TYPE_STICKER:
            ref = media.extract_sticker_ref(t, content)
            if ref is not None:
                sticker_md5, name, _pack = ref
                name = name or (sticker_names or {}).get(sticker_md5)
                content = messages.sticker_display(sticker_md5, name)
        hint = _media_hint(t, content)
        if t == messages.TYPE_TEXT:
            body = content
        elif sticker_md5:
            # 贴纸的 content 已是显示串（上面那段），**不能用 hint**：hint 是类型标记，
            # 两列语义不同（spec D5）。没有合法 md5 时不走这支，回落下面的 `[sticker]`。
            body = content
        elif hint:
            body = hint
        else:
            # 非文本的正文若整段是 XML，一律给类型占位符，别把 XML 喂给上层。
            # **判据是 `"<"` 而不是 `"<?xml"`**：真机群 sysmsg 里带 `<?xml` 声明的只有 1 行，
            # 根直接是 `<sysmsg …>` 的有 1,960 行——只认声明会把多数派放过去（实测共 2,221 行）。
            # 这里只到得了 `hint is None` 的类型（10000/10002）；人类文本走上面那条分支，
            # 所以「有人粘了一段 HTML」「有人写 `<video>就能显示hdr`」那 18 行不受影响。
            body = (content if content and not content.startswith("<")
                    else f"[{_type_name(t)}]")
        return Message(chat_id=chat_id, shard="", local_id=lid, ts=ts,
                       msg_type=t, type_name=_type_name(t), sender_id=sender_u,
                       sender_name=sender_name, is_self=(sender_u == self.self_wxid),
                       content=body, media_hint=hint, sticker_md5=sticker_md5)

    def _read_rows(self, con: decrypt.ReadConnection, table: str, shard: str, md5hex: str,
                   chat_map: Mapping[str, str], n2i: Mapping[int, str],
                   order_desc: bool = False, limit: int | None = None,
                   sticker_names: Mapping[str, str] | None = None,
                   before_ts: int | None = None) -> list[Message]:
        sql = (f"SELECT local_id, local_type, real_sender_id, create_time, "
               f"message_content, WCDB_CT_message_content FROM '{table}'")
        params: tuple = ()
        if before_ts is not None:
            sql += " WHERE create_time < ?"
            params = (int(before_ts),)
        sql += " ORDER BY local_id" + (" DESC" if order_desc else "")
        if limit:
            sql += f" LIMIT {int(limit)}"
        out = []
        for lid, ltype, sender, ts, mc, ct in con.execute(sql, params):
            m = self._decode_row(lid, ltype, sender, ts, mc, ct, md5hex, chat_map, n2i,
                                 sticker_names=sticker_names)
            m.shard = shard
            m.msg_key = messages.row_key(self.acct, md5hex, shard, m.local_id, m.ts)
            out.append(m)
        return out

    def recent(self, chat_id: str, limit: int = 50,
               before_ts: int | None = None) -> list[Message]:
        """某会话最近 N 条（时间正序）。分片按时间分区 → 从最新分片起查，够数即停。

        before_ts（unix 秒，不含）：只取它之前的消息——往前翻页用。
        """
        md5hex = messages.md5_of(chat_id)
        chat_map = {md5hex: chat_id}
        names = self.sticker_captions()        # **循环外取一次**，见该方法 docstring
        pieces: list[Message] = []
        for shard in reversed(paths.message_shards(self.acct)):
            with self._open(f"message/{shard.name}") as con:
                tables = self._shard_tables(con, md5hex)
                if tables:          # 空 = 该会话在这分片里没有消息，**不是**漂移（spec §4）
                    self._check("messages", f"message/{shard.name}", con, table=tables[0])
                    # EA-017：Name2Id 只在这张表的行解码里用——挪进 `if`。无条件读它时，
                    # 「目标会话不在本分片」的分片（`tables` 为空）照样会去碰 Name2Id，
                    # 缺那张表的分片就抛裸 sqlite 错误（守卫白设）。
                    n2i = self._name2id(con)
                for table in tables:
                    pieces += self._read_rows(con, table, shard.name, md5hex, chat_map,
                                              n2i, order_desc=True, limit=limit,
                                              sticker_names=names, before_ts=before_ts)
            if len(pieces) >= limit:
                break
        # EA-016：排序键此前只有 `m.ts`——扫描是 `ORDER BY local_id DESC`（`order_desc=True`），
        # 稳定排序会让同秒的两条保持**降序**到达序（「后发的在前」），与「时间正序」的契约、
        # 与索引侧（`ORDER BY ts`）都不一致。加 `local_id` 作次键：先发的在前，且确定。
        pieces.sort(key=lambda m: (m.ts, m.local_id))
        # EA-080：`[-0:]` 是**全量**切片（`-0 == 0`）——`limit=0` 必须返回空列表，
        # 与 `if limit:` 的「0 = 不拼 LIMIT」在 SQL 侧的口径分开写清楚。
        if limit <= 0:
            return []
        return self._apply_roster(chat_id, pieces)[-limit:]

    def _export_piece(self, row: tuple, *, shard: str, md5hex: str,
                      chat_map: Mapping[str, str], n2i: Mapping[int, str],
                      sticker_names: Mapping[str, str] | None
                      ) -> tuple[messages.MessageRow, RefRow | None]:
        """一行源数据 → `(messages 行, media_ref 或 None)`。

        **前向扫描与回看共用**：两条路径若各写一份 refs 提取，导出与回看对同一条
        消息会得出不同的 `media_refs`（贴纸那种「`file_key` 位放 md5」的约定尤其容易走偏）。
        """
        lid, ltype, sender, ts, mc, ct, packed = row
        m = self._decode_row(lid, ltype, sender, ts, mc, ct, md5hex, chat_map, n2i,
                             sticker_names=sticker_names)
        ref = None
        if m.msg_type in (messages.TYPE_IMAGE, messages.TYPE_VIDEO):
            # 提取只用原始 XML（_decode_row 产出的 content 是 [image] 占位）；
            # 只在图片/视频上多解一次压缩，量小可忽略
            got = media.extract_media_ref(m.msg_type, _decode_content(mc, ct), packed)
            if got:
                kind, fk, xm = got
                ref = (messages.row_key(self.acct, md5hex, shard, lid, m.ts), kind, fk, xm)
        elif m.sticker_md5:
            # 贴纸引用：`file_key` 位放 md5（它就是这个 kind 的定位键），`xml_md5` 位留 None。
            ref = (messages.row_key(self.acct, md5hex, shard, lid, m.ts),
                   "sticker", m.sticker_md5, None)
        return messages.make_row(
            account=self.acct, chat_md5=md5hex, chat_id=m.chat_id,
            shard=shard, local_id=m.local_id, ts=m.ts,
            msg_type=m.msg_type, sender_id=m.sender_id,
            sender_name=m.sender_name, is_self=1 if m.is_self else 0,
            content=m.content, media_hint=m.media_hint), ref

    def _batched(self, src_rows: Iterable[tuple], *, batch: int, shard: str, md5hex: str,
                 chat_map: Mapping[str, str], n2i: Mapping[int, str],
                 sticker_names: Mapping[str, str] | None
                 ) -> Iterator[tuple[list[messages.MessageRow], list[RefRow]]]:
        """源行 → `(messages 行批次, refs 批次)`。**前向扫描与回看共用**：

        以前两条路径各写一份解码 + 分批循环，改一处忘另一处就会让「导出」与「回看」
        对同一条消息产出不同的 media_refs（贴纸的 `file_key` 位放 md5 尤其容易走偏）。
        """
        rows: list[messages.MessageRow] = []
        refs: list[RefRow] = []
        for row in src_rows:
            out, ref = self._export_piece(
                row, shard=shard, md5hex=md5hex, chat_map=chat_map,
                n2i=n2i, sticker_names=sticker_names)
            if ref:
                refs.append(ref)
            rows.append(out)
            if len(rows) >= batch:
                yield rows, refs
                rows, refs = [], []
        if rows:
            yield rows, refs

    def iter_all(self, chat_id: str | None = None,
                 resume: dict[tuple[str, str], int] | None = None,
                 batch: int | None = None, *,
                 audit_cursors: Mapping[tuple[str, str], int] | None = None,
                 audit_rows: int = 0,
                 audit_sink: Callable[[list, list], None] | None = None,
                 anchors: Mapping[tuple[str, str], int | None] | None = None,
                 on_rebuilt: Callable[[str, str], None] | None = None
                 ) -> Iterator[tuple[list[messages.MessageRow], list[RefRow]]]:
        """全量流式导出（生成器）。逐分片→逐表→按批 yield。

        resume: {(shard, chat_md5): 已导出的 max(local_id)}（来自 store.load_export_watermarks；
        local_id 是分片内序号，键必须含 shard）。
        `audit_cursors` / `audit_rows` / `audit_sink`（Phase 02）：在同一次分片解密里
        顺带回看每个 `(分片, 会话)` 的旧行，经 `audit_sink(rows, refs)` 交给调用方——
        **不混进 yield**：`rows` 是「本轮新写入」的已发布计数契约，混进来会让每次增量都
        报出几万行。回看后的游标记在 `self.audit_marks`，由调用方在数据提交后落盘。
        `audit_sink is None` 时完全不回看（`audit_rows` 也就无所谓）。
        `anchors`（schema v10）：{(shard, chat_md5): 水位线那一行的 create_time}。源表里
        `local_id = 水位线` 的行**不在或 create_time 对不上** = 表被重建过（local_id 从 1 重来）
        或最新那条被删了：该会话从 0 重扫（键含 create_time，重扫幂等），并先调
        `on_rebuilt(shard, chat_md5)` 让调用方把旧水位线清掉——否则它按 max 单调不降，
        下一轮还会再判一次。锚点为 None（v10 之前的水位线）不判。

        yield (rows, refs)：rows 是 list[dict]，字段与 store.upsert_batch 对齐；refs 是
        [(msg_key, media_kind, file_key, xml_md5)]，与 store.upsert_media_refs 对齐——
        图片/视频消息顺带抽取（spec §4.3），贴纸消息带出 md5（`file_key` 位），
        调用方得以把两者写进同一个事务。
        """
        batch = batch or store.BATCH
        resume = resume or {}
        # 本轮回看后的游标：{(shard, chat_md5): 下一个位置}。**只在调用方把对应数据
        # 提交之后**才该落盘（见 `store.save_audit_cursors`）；先游标后数据，崩在中间
        # 就是这段历史再也不会被回看。
        self.audit_marks = {}
        chat_map = self.md5_map()
        sticker_names = self.sticker_captions()   # **循环外取一次**（同 recent）
        want_md5 = messages.md5_of(chat_id) if chat_id else None
        for shard in paths.message_shards(self.acct):
            with self._open(f"message/{shard.name}") as con:
                tables = self._shard_tables(con, want_md5)
                if tables:          # 空 = 该分片里没有匹配的表，**不是**漂移（spec §4）
                    self._check("messages", f"message/{shard.name}", con, table=tables[0])
                    n2i = self._name2id(con)      # EA-017：只在有表可读时才碰它
                for table in tables:
                    md5hex = table[4:]
                    # EA-112：不再逐表比对 want_md5——`_shard_tables(con, want_md5)` 已早退到
                    # 唯一那张表，这里再判一次是恒真分支（同一判据两处维护）。
                    start = resume.get((shard.name, md5hex), 0)
                    anchor = (anchors or {}).get((shard.name, md5hex))
                    rebuilt = bool(start and anchor is not None
                                   and self._table_rebuilt(con, table, start, anchor))
                    if rebuilt:
                        start = 0
                        if on_rebuilt is not None:
                            on_rebuilt(shard.name, md5hex)
                    cur = con.execute(
                        f"SELECT {_EXPORT_COLUMNS} FROM '{table}' "
                        f"WHERE local_id > ? ORDER BY local_id", (start,))
                    yield from self._batched(
                        cur, batch=batch, shard=shard.name, md5hex=md5hex,
                        chat_map=chat_map, n2i=n2i, sticker_names=sticker_names)

                    if audit_sink is not None and audit_rows > 0:
                        # 回看与导出**共用同一次分片解密**：另开一趟会让每轮增量多付
                        # 一次分片快照与扫描（CLI 回退后端下是 500MB 量级的整库导出）。游标走到表尾
                        # （本段不足 audit_rows）就绕回 0，下一轮从头再走。
                        #
                        # 回看的行走 `audit_sink` 而**不**混进 yield：调用方要把「本轮
                        # 新写入的行」与「回看过的行」分开计数（`rows` 是已发布契约），
                        # 混在一起会让每次增量都报出几万行。
                        # 表重建过：旧游标是上一代的编号，回看也从头走
                        a_start = 0 if rebuilt else (audit_cursors or {}).get(
                            (shard.name, md5hex), 0)
                        got = con.execute(
                            f"SELECT {_EXPORT_COLUMNS} FROM '{table}' "
                            f"WHERE local_id > ? ORDER BY local_id LIMIT ?",
                            (a_start, int(audit_rows))).fetchall()
                        self.audit_marks[(shard.name, md5hex)] = (
                            0 if len(got) < audit_rows else int(got[-1][0]))
                        for a_rows, a_refs in self._batched(
                                got, batch=batch, shard=shard.name, md5hex=md5hex,
                                chat_map=chat_map, n2i=n2i, sticker_names=sticker_names):
                            audit_sink(a_rows, a_refs)

    @staticmethod
    def _table_rebuilt(con: decrypt.ReadConnection, table: str, local_id: int,
                       anchor: int) -> bool:
        row = con.execute(f"SELECT create_time FROM '{table}' WHERE local_id = ?",
                          (local_id,)).fetchone()
        return row is None or int(row[0] or 0) != anchor

    def raw_contents(self, shard: str, wanted: Iterable[tuple[str, int]]
                     ) -> dict[tuple[str, int], str | None]:
        """按 `(chat_md5, local_id)` 取**解压后的原始** `message_content`；分片只解密一次。

        给贴纸回源用（`media.sticker_cdn_urls`）：凭据在原始 XML 里，`_decode_row` 产出的
        `content` 已经是显示串。行不在 → 值为 None；分片打不开 / 表不在照常抛，由调用方
        按「分片读不出来」记账。原始 XML 只在返回值里存在，不落盘。
        """
        out: dict[tuple[str, int], str | None] = {}
        with self._open(f"message/{shard}") as con:
            for chat_md5, local_id in wanted:
                row = con.execute(
                    f"SELECT message_content, WCDB_CT_message_content FROM 'Msg_{chat_md5}' "
                    f"WHERE local_id = ?", (local_id,)).fetchone()
                out[(chat_md5, local_id)] = _decode_content(*row) if row else None
        return out

    def iter_extras_input(self) -> Iterator[tuple[str, int, bytes]]:
        """为 `msg_extras` 回填供料：逐条 yield `(msg_key, msg_type, payload)`。

        `payload` 是 `appmsg.normalize` 规范化后、**zlib 压缩**的 XML（与
        `msg_parts.raw_xml` 同一存盘形态）——只对 48/42 解压，其余类型一层代价都不付。

        **不在这里判「有没有可读字段」**：那是 `usecases` 的事（它要用同一份
        `msgextra.parse` 同时产出「落库的字节」与「跑完后的计数」）。被判空
        （`msgextra.parse` 返回 None）的行**照样落库**：那一趟解压已经付过了，
        丢掉只会让下次重扫再解一遍；而「这条没有可读字段」本身也是结论。

        **刻意不调 `md5_map()`**（与 `iter_parts_input` 同一条理由）：产物里没有 chat_id。
        """
        for shard in paths.message_shards(self.acct):
            with self._open(f"message/{shard.name}") as con:
                tables = self._shard_tables(con, None)
                if tables:
                    self._check("messages", f"message/{shard.name}", con, table=tables[0])
                for table in tables:
                    md5hex = table[4:]
                    for lid, ltype, ts, mc, ct in con.execute(
                            f"SELECT local_id, local_type, create_time, message_content, "
                            f"WCDB_CT_message_content FROM '{table}'"):
                        t = ltype & 0xffffffff
                        if t not in msgextra.KINDS or mc is None:
                            continue
                        raw = _decode_content(mc, ct)
                        if not raw:
                            continue
                        yield (messages.row_key(self.acct, md5hex, shard.name, lid, ts), t,
                               zlib.compress(appmsg.normalize(raw).encode(), 6))

    def iter_parts_input(self) -> Iterator[tuple[int | None, str, str | None]]:
        """为 msg_parts 回填供料：**全量扫描**，逐条 yield `(server_id, msg_key, appmsg_raw)`。

        `appmsg_raw` 仅在 `msg_type == 49` 时非 None —— 其余类型**不解压**，所以不付
        zstd 的代价（实测全量扫描 38 s，其中大头正是解压这 6 万余条 appmsg）。

        为什么是全量而不是只扫 appmsg：引用回指需要一张**账号级**的
        `server_id → msg_key` 表，而被引用的原文大多是普通文本消息。只扫 appmsg
        会让绝大部分 svrid 解析不出来。

        **刻意不调 `md5_map()`**（`iter_all` 要它，是因为 `_decode_row` 得反查会话名）：
        本方法的产物里没有 chat_id，调它只是白白把 session / contact 与各分片的
        Name2Id 全解一遍密。

        `server_id` 照原样带出，`0` 与 `NULL` 都可能（实测 15 个 0）——丢不丢是调用方
        的事：`sid_map` 的主键放不下「没有 server_id」这件事，两者都不该进去。
        """
        for shard in paths.message_shards(self.acct):
            with self._open(f"message/{shard.name}") as con:
                tables = self._shard_tables(con, None)
                if tables:          # 空 = 该分片里没有匹配的表，**不是**漂移（spec §4）
                    self._check("messages", f"message/{shard.name}", con, table=tables[0])
                for table in tables:
                    md5hex = table[4:]
                    for lid, ltype, sid, ts, mc, ct in con.execute(
                            f"SELECT local_id, local_type, server_id, create_time, "
                            f"message_content, WCDB_CT_message_content FROM '{table}'"):
                        raw = (_decode_content(mc, ct)
                               if (ltype & 0xffffffff) == messages.TYPE_APPMSG
                               and mc is not None else None)
                        yield (sid, messages.row_key(self.acct, md5hex, shard.name, lid, ts),
                               raw)
