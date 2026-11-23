"""销毁与回填用例：按会话销毁目标、媒体引用/部件/位置名片的回填。"""
from __future__ import annotations

import sqlite3
import zlib

from typing import TYPE_CHECKING

from .. import appmsg, errors, exclusions, messages, store
from ._core import NeedConfirm, pkg_context

if TYPE_CHECKING:
    from collections.abc import Callable
from .media import _decode_extra

def purge_chat_targets(contact: str, account: str | None = None) -> list[dict] | NeedConfirm:
    """会话名 → 每个**索引里（或排除清单里）有它**的账号各一条 `{account, chat_id}`（EA-092：无 `chat_md5`）。

    **空集也回一条**（`account` 为空串，见下），所以调用方拿到的列表恒非空：会话名解析
    不出会抛 `ContactNotFound`、有歧义会回 `NeedConfirm`，两者都不走这条路。

    `chat_md5` 在**函数内部**用它查排除清单（**`messages.chat_md5()`**，不是 `messages.md5_of()`——后者对退化形态
    （`md5:<hex>`，`degraded_chat_id` 的产物）会**再哈希一次**，得到一个错的 md5。
    错法是静默的——`media/<chat_md5>/` 指向不存在的目录、导出物删不掉，而核查会报
    「0 行、目录不存在」一片 ✅（spec §5.1 第 1 步、终审 D1）。返回给调用方的**不含**
    `chat_md5`（按 `purge_chat_targets` 的返回契约——消费者要 md5 自行派生，EA-092）。

    跨账号：隐私不是按账号分的，凡是索引里有这个会话的账号都要删；`chat_md5` 与账号无关，
    所以 `media/<chat_md5>/` 天然是共享的。

    **`exclusions.excluded` 那一支不能省**：`--undo` 的存在理由就是「一次误删之后」
    （spec §5.6），而那时这个会话在 `messages` 里**一条都不剩**——只按 messages 找会话的话，
    撤销会拿到空清单、报一句「没有可删的东西」并退 0，墓碑原样留着，而用户以为已经撤销。
    这一支不会留下删不掉的残留：`--undo` 把这批墓碑删干净之后，这个账号下一次就不在并集里了。

    **`index.db` 不存在时一个字节都不许建**：`store.open()`（→ `store.connect()` →
    `executescript(SCHEMA)`）会把刚被 `purge --index` 删掉的索引连 schema 一起建回来，
    而 `--undo` 恰恰是紧跟销毁之后跑的那条命令——它会**静默撤销那次索引 purge**。
    索引库不在 ≡ 没有活消息（那一支本来就没有行可给），所以跳过它、只查排除清单即可；
    那一支走 `exclusions.standalone()`（`:memory:` + ATTACH），同样不落文件。
    """
    chat_id = pkg_context(account).resolve_optional(contact)
    if isinstance(chat_id, NeedConfirm):
        return chat_id
    if chat_id is None:
        raise errors.ContactNotFound(f"找不到会话「{contact}」")
    md5 = messages.chat_md5(chat_id)
    try:
        if store.DB_PATH.exists():
            # 判据与 `ChatIdentity.messages_where` 同源（去掉账号那一列）：只按 `chat_id=?`
            # 找会漏掉**只剩退化 id** 的账号——计划 0 条、核查 ✅，正文原样留着。
            ident = messages.ChatIdentity.of("", chat_id)
            with store.open() as con:
                rows = con.execute(
                    f"SELECT account FROM messages WHERE {ident.messages_where} "
                    "UNION SELECT account FROM exclusions.excluded WHERE chat_md5=? "
                    "ORDER BY account", (*ident.messages_args[1:], md5)).fetchall()
        else:
            with exclusions.standalone(store.DB_PATH) as con:
                rows = con.execute(
                    "SELECT account FROM exclusions.excluded WHERE chat_md5=? "
                    "ORDER BY account", (md5,)).fetchall()
    except (sqlite3.Error, OSError, errors.IndexUnavailable, errors.SchemaMigrationError) as exc:
        # 与 plan_chat 同一口径：读不了索引/清单时裸 sqlite3.Error 会一路逃到 CLI 的
        # `except AgentWeChatError` 之外，用户看到的是一整个 traceback 而不是一行拒绝。
        # EA-074 之后 `store.connect()` 会先翻一次（IndexUnavailable）——同样是「读不了」，
        # 在这里统一翻成销毁路径自己的 PurgeRefused，`__cause__` 取最深那层病因。
        raise errors.PurgeRefused(
            f"读不出会话「{contact}」的销毁目标：{exc}",
            hint="确认 index.db 可读、且没有别的进程正在写它（导出任务 / MCP server）；"
                 "库损坏（file is not a database 等）时先 purge --index 删掉再重跑 export"
        ) from (exc.__cause__ or exc)
    if not rows:
        # 空集**不是「无事可做」**，恰好相反：索引里没有活行，不代表 `media/<md5>/` 不在盘上
        # ——`purge --index` 之后逐会话重建的那个窗口里，解密出的图片/语音/HTML 还摊在盘上，
        # 而命令会报一句「没有可删的东西」退 0（spec §5.7 只要求「不写墓碑」，没要求跳过
        # 计划与核查）。所以这里回一条**账号为空**的目标，让计划 → 执行 → 核查照常走一遍：
        # 导出物只取决于 chat_md5、与账号无关，于是计划表印得出那一行、`--yes` 删得掉它。
        # 索引侧各项恒为 0 且不会误伤——`account` 是账号目录名，生产上不可能为空，所以
        # `account=''` / 前缀 `'|md5|'` 一个真实行都命不中；墓碑也因此自然为 0 条。
        return [{"account": "", "chat_id": chat_id}]
    return [{"account": r[0], "chat_id": chat_id} for r in rows]


def media_rebuild_refs(account: str | None = None,
                       progress: Callable[[int], None] | None = None,
                       on_skipped: Callable[[int], None] | None = None) -> int:
    """全量重扫微信库，只为存量历史回填 media_refs（spec §4.5）。幂等 upsert；
    **不写 messages**（消息内容不变，只有 refs 需要补）。分钟级，同步执行。

    **返回值只数未被排除的行**（计数契约，与 `export.run_export` 同款）：它是全量重扫，
    被 `purge --chat` 删掉的会话仍在微信库里，每次都会读到——`store.upsert_media_refs`
    把它们拦在写入端，这里据此把「读到的」与「入库的」分开计。
    `on_skipped`：有行被排除时回调一次，参数是本次跳过的**行数**（「成功但有保留」的出口）。
    """
    ctx = pkg_context(account)
    n = 0
    skipped = 0
    with store.open() as con:
        for _rows, refs in ctx.reader.iter_all():
            k = store.upsert_media_refs(con, refs)
            skipped += k
            n += len(refs) - k            # ← 计数契约：只数未被排除的
            if progress and n and n % 20_000 < store.BATCH:
                progress(n)
    if on_skipped is not None and skipped:
        on_skipped(skipped)
    return n


def msgextra_rebuild(account: str | None = None,
                     progress: Callable[[int], None] | None = None,
                     on_skipped: Callable[[int], None] | None = None) -> dict:
    """全量重扫微信库回填 `msg_extras`（EA-041）。幂等；**只写 msg_extras**——
    不碰 messages / FTS / 向量 / 水位线 / msg_parts。

    **为什么是一条独立命令而不是并进 `chat export`**：与 `appmsg rebuild` / `media
    rebuild-refs` 同一个理由（`cli.py` 那张「谁写谁」的表）——导出链每多写一张表，
    就多一个「导出到一半失败留下半张表」的形态；而这三张旁路表都是**可重算**的，
    让它们各自有一条显式的、幂等的回填入口更省事。历史行也借此可补（原始 XML 只在
    微信库里，导出时不解压 48/42）。

    计数的分母（与 `media_rebuild_refs` / `appmsg_rebuild_parts` 同款）：
      · `extras` = **实际落库**的条数（缓冲数 − 被排除清单挡下的）；被挡下的行一条都不落库
        ——本命令直接回微信库重扫，不拦的话 `purge --chat` 删掉的位置/名片载荷会重新长回来
        （spec §5.4 第 2 条）；
      · `empty` = **本轮扫到的** 48/42 里没有一个可读字段的条数——两种成因都算：载荷危险 /
        解不开 / 缺承载元素（`parse` 返回 None），或解得开但一个白名单属性都没命中
        （`fields` 为空元组）。它不是错误。
        **它含被排除清单挡下的那些行**（复审 T14 的 N8）：判据只有一处、在 `upsert_extras`
        里，要在这里把两者分开就得复刻那份判据——而重复的判据正是本模块一直在消的东西。
        所以它**不是 `extras` 的子集**，两个数别并排当包含关系读（`skipped` 单列）；
      · `skipped` = 被排除清单挡下的行数（`on_skipped` 有值时回调一次，「成功但有保留」的出口）。
    """
    ctx = pkg_context(account)
    stored = empty = skipped = 0
    with store.open() as con:
        # 入口就开 IMMEDIATE：本函数是这次写入的事务 owner（commit 在 `store.open()` 出口）。
        # 与 `appmsg_rebuild_parts` 不同，这里**没有 TEMP 表**先开 deferred 事务，
        # 所以 `upsert_extras` 里那句 guard 会正常触发；入口这一句是为了让整次回填
        # （含排除判据的读）落在同一把写锁下。
        con.execute("BEGIN IMMEDIATE")
        buf: list[tuple[str, int, bytes]] = []

        def flush() -> None:
            nonlocal skipped
            if buf:
                skipped += store.upsert_extras(con, buf)
                buf.clear()
                if progress:
                    # 报**实际落库**的数（与另两处同款）：报缓冲数会让进度条上的最后一个数
                    # 比完成行还大，两句话对不上。
                    progress(stored - skipped)

        for key, msg_type, payload in ctx.reader.iter_extras_input():
            # `empty` 的判据是「**没有一个可读字段**」，不是「parse 返回 None」——两者不等价：
            # 载荷解得开、承载元素也在，但一个白名单属性都没命中时 `Extra.fields` 是**空元组**，
            # 那正是「这条没有可读内容」的常态（真机 42 里有只带 `percard` 的名片）。
            # **走共享的那一份解码**（`_decode_extra`）：自己再写一遍 `zlib.decompress(...).decode()`
            # 已经漂过一次——那边是 `decode()`（严格），这边写成 `decode("utf-8", "ignore")`，
            # 同一个坏载荷在两个入口一个抛一个不抛（review-code 的 Standards 轴 #6）。
            extra = _decode_extra(msg_type, payload)
            if extra is None or not extra.fields:
                empty += 1
            buf.append((key, msg_type, payload))
            stored += 1
            if len(buf) >= store.BATCH:
                flush()
        flush()
    if on_skipped is not None and skipped:
        on_skipped(skipped)
    return {"extras": stored - skipped, "empty": empty, "skipped": skipped}


def appmsg_rebuild_parts(account: str | None = None,
                         progress: Callable[[int], None] | None = None,
                         on_skipped: Callable[[int], None] | None = None) -> dict:
    """全量重扫微信库回填 msg_parts（spec §4.1）。幂等；**只写 msg_parts**——
    不碰 messages / FTS / 向量 / 水位线。

    两阶段，因为引用可以跨分片（实测分片内命中 99.86% 是下界）：
      1. 一边把 server_id→msg_key 灌进 TEMP TABLE，一边把 appmsg 攒批写 msg_parts
      2. 一条 SQL 解析回指

    实测整次 37.5–38.7 s；同轮 `index.db` 增长 +232.8 MB（本机实测）。

    计数口径（**两组不同的分母**，别并排当同一个读）：
      - `parts` / `rejected` / `malformed` = **本轮**的条数（`parts` 是成功解析**且真的
        落库**的条数——解析失败的不计，被排除清单挡下的也不计：缓冲时计数减去 `skipped`
        就是它；所以它**不等于**「扫到的 appmsg 条数」）；
      - `resolved` / `unresolved` = 跑完后**该账号整张 `msg_parts` 表**里 `ref_svrid`
        非空的行中，回指解出来 / 没解出来的行数（同一个谓词 ⇒ 两者之和恒等于该谓词下的
        引用行数）。删改后再跑一次，这组随整表变化，而 `parts` 只反映本轮。

    `skipped` 是新增的键（既有键一个不动）：被排除清单挡下、**没落库**的行数——
    `appmsg rebuild` 直接回微信库重扫，被 `purge --chat` 删掉的会话每次都会读到，
    不拦的话它们的原始载荷（`raw_xml` 里是完整正文）会重新长回来（spec §5.4 第 2 条）。
    它不是第三个分母：`skipped ⊆ 缓冲时计的数`，而 `parts` 报的就是那个差
    （**实际落库**的条数——与 `run_export` / `media_rebuild_refs` 的返回值同款口径：
    三处都只数真正写进库的那些）。`on_skipped`：有行被排除时
    回调一次（与 `media_rebuild_refs` / `run_export` 同款，「成功但有保留」的出口）。
    """
    ctx = pkg_context(account)
    prefix = f"{ctx.account}|"
    parts = rejected = malformed = 0
    skipped = 0
    with store.open() as con:
        # **入口就把事务开成 IMMEDIATE**：本函数是这次写入的事务 owner（commit 在
        # `store.open()` 出口），而 `flush()` 里那句 TEMP 表（sid_map）的 INSERT 会先开出一个
        # **deferred** 事务——`con.in_transaction` 为真、但主库写锁还没拿到
        # （实测：`CREATE TEMP TABLE` 不会，TEMP 表的 INSERT 会；此刻另一个连接可以提交墓碑）。
        # 于是 `upsert_msg_parts` 里那句 `if not con.in_transaction: BEGIN IMMEDIATE` 在
        # **本形态下不触发**，排除判据的「读」与随后的「写」之间就有了窗口：窗口里落下的
        # 墓碑会被本批的写绕过，被删消息的 appmsg 原始载荷永久复活（spec §5.4）。
        con.execute("BEGIN IMMEDIATE")
        con.execute("CREATE TEMP TABLE sid_map("
                    "server_id INTEGER PRIMARY KEY, msg_key TEXT NOT NULL)")
        sid_buf: list[tuple[int, str]] = []
        part_buf: list[tuple[str, int, int | None, str | None, bytes]] = []

        def flush(wrote_parts: bool) -> None:
            """把两批攒下的行落库。**同一批里一起写**：`sid_map` 与 `msg_parts` 都只是
            缓冲，谁先落都不影响结果（阶段 2 在全部扫完之后才跑）。"""
            nonlocal skipped
            if sid_buf:
                # `OR IGNORE`：`server_id` 实测全局唯一（846,785 个、重复 0 次），但那是
                # 上一次测量的结论，而消息还在不断进来。真撞上重复时**静默保留第一条映射**
                # ——即退化成一个错引用（引用解到先出现的那条），而**不是**以裸
                # IntegrityError 崩掉整次回填。这正是它被判 Minor 而非 Important 的原因：
                # 错的是稀有情形下的一个引用，不是进程的存亡。
                con.executemany("INSERT OR IGNORE INTO sid_map VALUES (?,?)", sid_buf)
                sid_buf.clear()
            if part_buf:
                skipped += store.upsert_msg_parts(con, part_buf)
                part_buf.clear()
            if wrote_parts and progress:
                # 进度报的也是**实际落库**的数（与 `run_export` / `media_rebuild_refs` 同款）：
                # 报缓冲数会让进度条上的最后一个数比完成行还大，两句话对不上。
                progress(parts - skipped)

        for sid, key, raw in ctx.reader.iter_parts_input():
            # `if sid` 一次挡掉两种「没有 server_id」：NULL 与 0（实测 15 个 0）。
            # 它们是哨兵而不是可用标识，进 sid_map 会让 `ref_svrid=0` 的引用被错解。
            if sid:
                sid_buf.append((sid, key))
            if raw is not None:
                res = appmsg.parse(raw)
                if res.reason == "rejected":
                    rejected += 1
                elif res.reason == "malformed" or res.appmsg is None or res.normalized is None:
                    # 后两条只为让类型收窄：成功（reason 为 None）时两者必有，见 ParseResult
                    malformed += 1
                else:
                    # `quote` 仅 kind=57 才非 None；`svrid` 在 XML 里是字符串，转不出 int
                    # 时 `_to_int` 已经给了 None（**不抛**）。给 None 的那条**照样落库**，
                    # 否则它的正文也跟着没了。
                    svrid = res.appmsg.quote.svrid if res.appmsg.quote else None
                    # 正文用 `parse` 已经算出的规范化文本（`res.normalized`），**不再**
                    # 调一次 `normalize`——那样既白跑一遍，又把存盘形态耦合到「`normalize`
                    # 幂等」这个隐含前提上。
                    part_buf.append((key, res.appmsg.kind, svrid, None,
                                     zlib.compress(res.normalized.encode(), 6)))
                    parts += 1
            if len(sid_buf) >= store.BATCH or len(part_buf) >= store.BATCH:
                flush(bool(part_buf))
        flush(False)

        # 阶段 2：一条 SQL 解析回指。**按账号前缀限定**——sid_map 只装了本账号的
        # server_id，不限定的话别的账号里恰好同号的引用会被接到本账号的消息上。
        # 前缀比对而非 LIKE：wxid 里含 `_`，LIKE 的 `_` 是单字符通配符（同
        # `count_msg_parts`）。
        con.execute(
            "UPDATE msg_parts SET ref_msg_key = (SELECT msg_key FROM sid_map "
            "WHERE server_id = msg_parts.ref_svrid) "
            "WHERE ref_svrid IS NOT NULL AND substr(msg_key, 1, ?) = ?",
            (len(prefix), prefix))
        # 计数从**表的状态**算（而不是 UPDATE 的 changes）：与上面那条 UPDATE 同谓词，
        # 所以 `resolved + unresolved` 恒等于该谓词下 `ref_svrid` 非空的行数（和不变）。
        # **但它是整表的**：上一次回填留下的行照样被算进来——这正是它与 `parts` 分母不同的
        # 地方，别把两者当成同一次扫描的两个视角。
        refs, resolved = (
            con.execute("SELECT count(*), count(ref_msg_key) FROM msg_parts "
                        "WHERE ref_svrid IS NOT NULL AND substr(msg_key, 1, ?) = ?",
                        (len(prefix), prefix)).fetchone())
    if on_skipped is not None and skipped:
        on_skipped(skipped)
    # `parts` 报的是**实际落库**的条数（缓冲数 − 被排除挡下的），与另两处（`run_export` /
    # `media_rebuild_refs` 的返回值）同一口径：CLI 那句「本轮写入 msg_parts N 条」印的就是它，
    # 报缓冲数会让用户被告知写入了 N 条而实际一条没写（终审 I-3）。
    return {"parts": parts - skipped, "resolved": resolved, "unresolved": refs - resolved,
            "rejected": rejected, "malformed": malformed, "skipped": skipped}
