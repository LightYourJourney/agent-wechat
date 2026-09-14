"""导出管道：流式、幂等、断点续传。任务登记在 usecases 层，这里只有管道。"""
from __future__ import annotations

from . import reader, store


def run_export(acct: str, chat_id: str | None = None, full: bool = False,
               progress=None, on_reader=None) -> int:
    """全量/增量导出。恒定内存：reader 流式产批 → store 批量幂等 upsert。

    `on_reader`：跑完时回调一次，把这次导出**用到的那个 reader** 交给调用方。
    `username_pool` 是唯一按设计降级的能力（spec §6），而它的漂移只落在 reader 身上，
    这里的返回值又只有行数——没有这条通道，CLI 就看不到降级：用户只看到「导出成功」，
    索引里却静默躺着 `md5:<hex>` 会话（spec §7 要消灭的正是这个）。
    MCP 的长任务路径不传：任务结果（rows）是已发布契约，那条要另一个设计。
    """
    r = reader.MessageReader(acct)
    total = 0
    with store.open() as con:
        if full and not chat_id:
            # 只重置本账号：run_export 只重导 acct，连带清掉别的账号的水位线会让
            # 它们白跑一次全量（upsert 幂等，不丢数据，但纯属浪费）
            store.clear_export_watermarks(con, acct)
            con.commit()
        resume = {} if full else None
        if resume is None:
            resume = store.load_export_watermarks(con, acct)
        for batch in r.iter_all(chat_id=chat_id, resume=resume):
            store.upsert_batch(con, batch)
            total += len(batch)
            if progress and total % 20_000 < store.BATCH:
                progress(total)
    if on_reader is not None:
        on_reader(r)
    return total
