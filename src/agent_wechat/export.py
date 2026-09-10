"""导出管道：流式、幂等、断点续传；长任务注册转发到 tasks（MCP 后台执行用）。"""
from __future__ import annotations

from . import reader, store, tasks


def start_task(account: str, chat_id: str | None, full: bool) -> str:
    """登记导出长任务（后台线程执行）。签名与行为保持不变。"""
    return tasks.start("export", lambda tid: run_export(account, chat_id=chat_id, full=full),
                       account=account, chat_id=chat_id)


def task_status(task_id: str | None = None) -> dict | list:
    return tasks.status(task_id)


def run_export(acct: str, chat_id: str | None = None, full: bool = False,
               progress=None) -> int:
    """全量/增量导出。恒定内存：reader 流式产批 → store 批量幂等 upsert。"""
    r = reader.MessageReader(acct)
    total = 0
    with store.connect() as con:
        if full and not chat_id:
            con.execute("DELETE FROM watermark")
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
