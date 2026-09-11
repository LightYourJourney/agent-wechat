"""消息读取层：基于实测 schema（docs/schema-4.1.13.md）解码微信 4.1.13 库。

要点：
- 消息表 Msg_<md5(chat_username)>，每会话一张，跨分片按时间分区
- content: WCDB_CT=0 明文 / 4=zstd；群消息前缀 "<sender>:\n"
- local_type 需 & 0xffffffff；real_sender_id 经本库 Name2Id 解析
"""
from __future__ import annotations

import contextlib
import hashlib
import sqlite3
from dataclasses import dataclass, asdict

from . import decrypt, keys, paths, store
from .errors import DataNotFound, DependencyMissing

try:
    import zstandard
    _ZSTD = zstandard.ZstdDecompressor()
except ImportError:  # pragma: no cover
    _ZSTD = None

TYPE_NAMES = {1: "text", 3: "image", 34: "voice", 43: "video", 47: "sticker",
              48: "location", 51: "status", 10000: "system", 10002: "revoke"}


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

    def to_dict(self) -> dict:
        return asdict(self)


def _decode_content(raw, ct_flag) -> str | None:
    """WCDB 压缩列解码：CT=0 明文，CT=4 zstd。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if ct_flag == 4:
        if _ZSTD is None:
            raise DependencyMissing("缺少 zstandard：uv add zstandard")
        return _ZSTD.decompress(raw).decode("utf-8", "replace")
    return raw.decode("utf-8", "replace")


def _type_name(t: int) -> str:
    return TYPE_NAMES.get(t, f"type{t}")


def _media_hint(t: int, content: str | None) -> str | None:
    if t == 1 or t in (10000, 10002):
        return None
    if t == 49 and content and "<title>" in content:
        title = content.split("<title>", 1)[1].split("</title>", 1)[0].strip()
        return f"[appmsg: {title[:60]}]" if title else "[appmsg]"
    return f"[{_type_name(t)}]"


def _s(v) -> str | None:
    if v is None:
        return None
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)


def is_chat_id(s: str) -> bool:
    """wxid / 群 / 公众号 / filehelper 形态的 username 可直接当会话 id 用，无需解析。"""
    return s.endswith("@chatroom") or s.startswith(("wxid_", "gh_", "filehelper"))


class MessageReader:
    """按账号读取消息。只读微信目录；解密副本用后即焚。"""

    def __init__(self, acct: str):
        self.acct = acct
        self.passphrase = keys.get_passphrase(acct)
        self.self_wxid = paths.bare_wxid(acct)
        self._contacts_all: list[dict] | None = None   # 实例级缓存，见 _contacts_all_cached

    def _contacts_all_cached(self) -> list[dict]:
        """全量联系人。同一 reader 生命周期内只解密一次 contact.db
        （resolve_chat + display_name 常在同一次工具调用里先后执行）。"""
        if self._contacts_all is None:
            self._contacts_all = self.contacts(limit=5000)
        return self._contacts_all

    @contextlib.contextmanager
    def _open(self, rel: str):
        db = paths.db_storage(self.acct) / rel
        if not db.exists():
            raise DataNotFound(f"{db} 不存在")
        with decrypt.decrypted(db, self.passphrase) as plain:
            con = sqlite3.connect(f"file:{plain}?mode=ro", uri=True)
            con.text_factory = bytes   # 库内 TEXT 含非 UTF-8 字节，统一按 bytes 取
            try:
                yield con
            finally:
                con.close()

    # ---------- 会话 / 联系人 ----------

    def username_pool(self) -> set[str]:
        """全部已知用户名（session + contact + 各分片 Name2Id），供 md5 反查。"""
        pool: set[str] = {self.self_wxid, "filehelper"}
        for rel, sql in (("session/session.db", "SELECT username FROM SessionTable"),
                         ("contact/contact.db", "SELECT username FROM name2id")):
            try:
                with self._open(rel) as con:
                    pool |= {u.decode("utf-8", "replace")
                             for (u,) in con.execute(sql) if isinstance(u, bytes)}
            except DataNotFound:
                continue
        for shard in paths.message_shards(self.acct):
            try:
                with self._open(f"message/{shard.name}") as con:
                    pool |= {u.decode("utf-8", "replace")
                             for (u,) in con.execute("SELECT user_name FROM Name2Id")}
            except DataNotFound:
                continue
        return pool

    def md5_map(self) -> dict[str, str]:
        """md5(chat_username) -> chat_username。"""
        return {hashlib.md5(u.encode()).hexdigest(): u for u in self.username_pool()}

    def contacts(self, query: str | None = None, limit: int = 200) -> list[dict]:
        """联系人搜索：备注/昵称/微信号模糊匹配。"""
        with self._open("contact/contact.db") as con:
            sql = ("SELECT username, alias, remark, nick_name FROM contact "
                   "WHERE COALESCE(delete_flag,0)=0")
            args: list = []
            if query:
                sql += " AND (remark LIKE ? OR nick_name LIKE ? OR alias LIKE ? OR username LIKE ?)"
                args = [f"%{query}%"] * 4
            sql += " ORDER BY remark, nick_name LIMIT ?"
            args.append(limit)
            out = []
            for u, alias, remark, nick in con.execute(sql, args):
                u, remark, nick, alias = _s(u), _s(remark), _s(nick), _s(alias)
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
        return (cands[0]["username"] if len(cands) == 1 else None), cands

    def display_name(self, chat_id: str) -> str:
        return next((c["display"] for c in self._contacts_all_cached()
                     if c["username"] == chat_id), chat_id)

    # ---------- 消息 ----------

    def _name2id(self, con: sqlite3.Connection) -> dict[int, str]:
        return {rowid: _s(u) for rowid, u in con.execute("SELECT rowid, user_name FROM Name2Id")}

    def _shard_tables(self, con: sqlite3.Connection, chat_md5: str | None) -> list[str]:
        if chat_md5:
            return [f"Msg_{chat_md5}"] if con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (f"Msg_{chat_md5}",)).fetchone() else []
        return [r[0].decode() for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%' ORDER BY name")]

    def _decode_row(self, lid, ltype, sender, ts, mc, ct, md5hex, chat_map, n2i) -> Message:
        """单行解码：压缩、类型掩码、群前缀、媒体提示。所有读取路径共用此函数。"""
        chat_id = chat_map.get(md5hex, f"md5:{md5hex}")
        content = _decode_content(mc, ct)
        t = ltype & 0xffffffff
        sender_u, sender_name = n2i.get(sender), None
        if chat_id.endswith("@chatroom") and content and t == 1:
            head, sep, rest = content.partition(":\n")
            if sep and head and "\n" not in head:   # 群消息 "<wxid>:\n正文" 前缀
                sender_u, sender_name, content = head, head, rest
        hint = _media_hint(t, content)
        if t == 1:
            body = content
        elif hint:
            body = hint
        else:
            body = (content if content and not content.startswith("<?xml")
                    else f"[{_type_name(t)}]")
        return Message(chat_id=chat_id, shard="", local_id=lid, ts=ts,
                       msg_type=t, type_name=_type_name(t), sender_id=sender_u,
                       sender_name=sender_name, is_self=(sender_u == self.self_wxid),
                       content=body, media_hint=hint)

    def _read_rows(self, con, table, shard, md5hex, chat_map, n2i,
                   order_desc=False, limit=None) -> list[Message]:
        sql = (f"SELECT local_id, local_type, real_sender_id, create_time, "
               f"message_content, WCDB_CT_message_content FROM '{table}'")
        sql += " ORDER BY local_id" + (" DESC" if order_desc else "")
        if limit:
            sql += f" LIMIT {int(limit)}"
        out = []
        for row in con.execute(sql):
            m = self._decode_row(*row, md5hex, chat_map, n2i)
            m.shard = shard
            out.append(m)
        return out

    def recent(self, chat_id: str, limit: int = 50) -> list[Message]:
        """某会话最近 N 条（时间正序）。分片按时间分区 → 从最新分片起查，够数即停。"""
        md5hex = hashlib.md5(chat_id.encode()).hexdigest()
        chat_map = {md5hex: chat_id}
        pieces: list[Message] = []
        for shard in reversed(paths.message_shards(self.acct)):
            with self._open(f"message/{shard.name}") as con:
                n2i = self._name2id(con)
                for table in self._shard_tables(con, md5hex):
                    pieces += self._read_rows(con, table, shard.name, md5hex, chat_map,
                                              n2i, order_desc=True, limit=limit)
            if len(pieces) >= limit:
                break
        pieces.sort(key=lambda m: m.ts)
        return pieces[-limit:]

    def iter_all(self, chat_id: str | None = None, resume: dict[str, int] | None = None,
                 batch: int | None = None):
        """全量流式导出（生成器）。逐分片→逐表→按批 yield。

        resume: {(shard, chat_md5): 已导出的 max(local_id)}（来自 store.watermark；
        local_id 是分片内序号，键必须含 shard）。
        yield 批次为 list[dict]，字段与 store.upsert_batch 对齐。
        """
        batch = batch or store.BATCH
        resume = resume or {}
        chat_map = self.md5_map()
        want_md5 = hashlib.md5(chat_id.encode()).hexdigest() if chat_id else None
        for shard in paths.message_shards(self.acct):
            with self._open(f"message/{shard.name}") as con:
                n2i = self._name2id(con)
                for table in self._shard_tables(con, want_md5):
                    md5hex = table[4:]
                    if want_md5 and md5hex != want_md5:
                        continue
                    start = resume.get((shard.name, md5hex), 0)
                    cur = con.execute(
                        f"SELECT local_id, local_type, real_sender_id, create_time, "
                        f"message_content, WCDB_CT_message_content FROM '{table}' "
                        f"WHERE local_id > ? ORDER BY local_id", (start,))
                    rows: list[dict] = []
                    for row in cur:
                        m = self._decode_row(*row, md5hex, chat_map, n2i)
                        rows.append({
                            "msg_key": f"{self.acct}|{md5hex}|{shard.name}|{m.local_id}",
                            "account": self.acct, "chat_md5": md5hex,
                            "chat_id": m.chat_id,
                            "shard": shard.name, "local_id": m.local_id, "server_id": None,
                            "ts": m.ts, "msg_type": m.msg_type, "sender_id": m.sender_id,
                            "sender_name": m.sender_name, "is_self": 1 if m.is_self else 0,
                            "content": m.content, "media_hint": m.media_hint,
                        })
                        if len(rows) >= batch:
                            yield rows
                            rows = []
                    if rows:
                        yield rows
