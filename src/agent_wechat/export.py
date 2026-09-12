"""导出管道：流式、幂等、断点续传。任务登记在 usecases 层，这里只有管道。"""
from __future__ import annotations

from . import reader, store


def run_export(acct: str, chat_id: str | None = None, full: bool = False,
               progress=None) -> int:
    """全量/增量导出。恒定内存：reader 流式产批 → store 批量幂等 upsert。"""
    r = reader.MessageReader(acct)
    total = 0
    with store.connect() as con:
        if full and not chat_id:
            # 只重置本账号：run_export 只重导 acct，连带清掉别的账号的水位线会让
            # 它们白跑一次全量（upsert 幂等，不丢数据，但纯属浪费）
            con.execute("DELETE FROM watermark WHERE account = ?", (acct,))
            con.commit()
        resume = {} if full else None
        if resume is None:
            # 键含 shard：local_id 是分片内序号，跨分片取 max 会漏导非最新分片的新消息
            resume = {(shard, md5): mx for shard, md5, mx in con.execute(
                "SELECT shard, chat_md5, max_local_id FROM watermark WHERE account=?", (acct,))}
        for batch in r.iter_all(chat_id=chat_id, resume=resume):
            store.upsert_batch(con, batch)
            total += len(batch)
            if progress and total % 20_000 < store.BATCH:
                progress(total)
    return total
