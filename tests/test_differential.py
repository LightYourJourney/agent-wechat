"""C 阶段差分/往返（D-1..D-3，spec §5）。

三条都钉**两条独立路径结果一致**：导出↔索引↔检索、首跑↔二跑、计划↔磁盘事实。
失败即发现（Task 10 修）。
"""
import pathlib

import pytest

from agent_wechat import export, media, paths, purge, search, store
from test_reader_export_pipeline import ACCT, env, wechat  # noqa: F401  (夹具链)


def test_export_index_search_msgkey_multiset(env, tmp_path):  # noqa: F811
    """D-1：导出→索引（条数守恒）→检索（命中必须都在索引里）。

    固定条件：`run_export(full=True)` 不截断；不涉及 months。
    """
    n = export.run_export(ACCT, full=True)
    assert n > 0
    with store.open() as con:
        indexed = [r["msg_key"] for r in con.execute(
            "SELECT msg_key FROM messages WHERE account=?", (ACCT,))]
    assert len(indexed) == n, "导出条数与索引行数必须相等"
    hits = search.search("你好", account=ACCT, limit=len(indexed) + 10)["hits"]
    assert hits, "夹具里的已知正文必须可检索"
    keys = set(indexed)
    assert all(h["msg_key"] in keys for h in hits), "检索命中了索引里不存在的 msg_key"


def test_media_export_idempotent(tmp_path, monkeypatch):
    """D-2：首跑真的导出（image.ok ≥ 1），二跑新增 0、跳过 ≥ 1。

    两条路径（首跑 / 二跑）必须对「哪些文件已经在盘上」给出**同一个**判断——
    首跑全写、二跑全跳，否则就是重复落盘或漏导。
    """
    from test_media import make_media_tree

    _root, account, _cm = make_media_tree(tmp_path, monkeypatch)
    out = tmp_path / "m"
    # 传入的是**会话名**（`export_media` 内部把它解析成目录名 chat_md5）；传 md5 反而
    # 一个文件都匹配不上——计划里的 `[cm]` 是笔误，实测首跑 ok=0。
    r1 = media.export_media(account, ["chatA@chatroom"], kinds=["image"],
                            months=["2026-09"], out_dir=str(out))
    assert r1.image.ok >= 1, "首跑必须真的导出至少 1 个文件"
    r2 = media.export_media(account, ["chatA@chatroom"], kinds=["image"],
                            months=["2026-09"], out_dir=str(out))
    assert r2.image.ok == 0 and r2.image.skipped >= 1, "二跑必须全部跳过"


def test_purge_plan_equals_deleted_plus_residual(tmp_path, monkeypatch):
    """D-3：计划集 = 实删 ∪ 残留；残留由**独立磁盘枚举**得出（不信计划的自我陈述）。

    `store.DB_PATH` 是 import 期绑定，必须一起重定向——只改 `paths.HOME_DIR` 的话
    `purge.plan` 计划的还是 conftest 那个库，本用例就测了个寂寞。
    """
    home = tmp_path / "home"; home.mkdir(exist_ok=True)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    db = home / "index.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    db.write_text("x")
    plan = purge.plan(frozenset({"index"}))
    planned = {i.artifact.path for i in plan.items if i.exists}
    assert planned, "计划里必须真的被删的条目，否则本用例空转"
    purge.execute(plan)
    residual = {p for p in planned if p.exists()}      # 独立枚举：直接看磁盘
    deleted = planned - residual
    assert planned == deleted | residual
    assert not residual, "index 范围应删干净（有残留即缺陷）"
