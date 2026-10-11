"""导出管道：流式、幂等、断点续传。任务登记在 usecases 层，这里只有管道。"""
from __future__ import annotations

from collections.abc import Callable

from . import reader, store


def run_export(acct: str, chat_id: str | None = None, full: bool = False,
               progress: Callable[[int], None] | None = None,
               on_reader: Callable[[reader.MessageReader], None] | None = None,
               on_skipped: Callable[[int], None] | None = None,
               _reader_factory=None) -> int:
    """全量/增量导出。恒定内存：reader 流式产批 → store 批量幂等 upsert。

    **返回值 = 未被排除的行数**（= 实际入库行数，spec §5.4 的计数契约）。加了排除清单之后
    「读到的行数」与「入库的行数」不再相等：被 `purge --chat` 删掉的那些仍在微信库里，
    每次全量重扫都会读到，而它们一条都不许入库。照旧返回读到的行数，CLI 印的
    「本轮 N 行」与索引总量的增量就对不上，MCP 的 agent 也会据此以为导入了 N 条。

    `on_reader`：跑完时回调一次，把这次导出**用到的那个 reader** 交给调用方。
    `username_pool` 是唯一按设计降级的能力（spec §6），而它的漂移只落在 reader 身上，
    这里的返回值又只有行数——没有这条通道，CLI 就看不到降级：用户只看到「导出成功」，
    索引里却静默躺着 `md5:<hex>` 会话（spec §7 要消灭的正是这个）。
    MCP 的长任务路径不传：任务结果（rows）是已发布契约，那条要另一个设计。

    `on_skipped`：有行被排除时回调一次，参数是本次跳过的**行数**。与 `on_reader` 同构——
    「成功但有保留」不该只体现在数字变小上（spec §7）。注意它是**跨表**的：
    被排除的消息行与属于它们的媒体引用行都计入，所以它 ≥ 被排除的消息行数。
    量词只能是「行」：`media rebuild-refs` / `appmsg rebuild` 那两个同款回调一条 `messages`
    都不写，说「条」会被读成消息条数（`cli._note_skipped` 记着同一笔账）。

    `_reader_factory`：测试用的桩入口（省略即 `reader.MessageReader`）。
    """
    r = (_reader_factory or reader.MessageReader)(acct)
    total = 0
    skipped = 0
    with store.open() as con:
        if full and not chat_id:
            # 只重置本账号：run_export 只重导 acct，连带清掉别的账号的水位线会让
            # 它们白跑一次全量（upsert 幂等，不丢数据，但纯属浪费）
            store.clear_export_watermarks(con, acct)
            con.commit()
        resume = {} if full else None
        if resume is None:
            resume = store.load_export_watermarks(con, acct)
        for rows, refs in r.iter_all(chat_id=chat_id, resume=resume):
            if refs:
                # 先写 refs，再 upsert_batch。refs 自己 BEGIN IMMEDIATE（判据的读也因此落在
                # 写事务里，Task 4 fix1）；upsert_batch 复用同一事务并在尾部 commit——
                # 两者同事务（spec §4.3），本批的读与写之间没有窗口。
                skipped += store.upsert_media_refs(con, refs)
            dropped = store.upsert_batch(con, rows)
            skipped += dropped
            total += len(rows) - dropped
            if progress and total % 20_000 < store.BATCH:
                progress(total)
    if on_skipped is not None and skipped:
        on_skipped(skipped)
    if on_reader is not None:
        on_reader(r)
    return total
