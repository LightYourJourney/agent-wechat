"""purge 的单元测试。**本文件每个测试都不得指向真实 ~/.agent-wechat。**

红线在下面的 autouse fixture 里：它把四个路径常量与 paths.HOME_DIR 全部指向 tmp_path，
并断言 artifacts() 解析出的路径确实不在真实数据目录下。写成 autouse 而不是单个测试函数
是有意的——单独跑本文件里任何一个测试时，保护都必须生效（spec §7）。
"""
import os
import pathlib
import sqlite3
import uuid

import pytest
import sqlite_vec

from agent_wechat import (decrypt, errors, exclusions, keys, messages, paths, purge,
                          store, tasks, vector)

# 导入时取值：此刻 autouse fixture 尚未 patch，paths.HOME_DIR 就是真实路径。
# 不重述 paths 的解析逻辑（它读 AGENT_WECHAT_HOME）——那正是 purge.py 明令禁止的第二份来源。
REAL_HOME = paths.HOME_DIR


# ---------- `purge --chat` 的夹具助手 ----------

# victim 用**自洽**的一对 (chat_id → chat_md5)：`chat_md5` 必须等于 `messages.chat_md5(chat_id)`。
# 计划里的 `{"chat_id": …, "chat_md5": …}`、messages 行的 `chat_md5` 列、vec0 的 `acct_chat`
# 分区键**三处是同一个值**——生产上必然如此（export 侧 chat_md5 是源头、chat_id 由它解析，
# `messages.chat_md5(chat_id)` 必然回到原值），而两列不一致的夹具编码的是生产上不存在的形态。
# 代价实测过：`chat_id="victim"` 配 `chat_md5="aaaa"` 时，`chat_row_counts` 按
# `md5("victim")` 数向量、而种进去的那行走的是 "aaaa" 那一路 → 数出来 0，
# 「向量行有没有被删掉」这条断言当场变瞎（正是「夹具与现实形态不符」那一类）。
VICTIM = "victim"
VICTIM_MD5 = messages.md5_of(VICTIM)          # 不从被测代码反推，本条即定义
KEEPER = "keeper"
KEEPER_MD5 = messages.md5_of(KEEPER)


def _row(chat_id: str, chat_md5: str, n: int, account: str = "a") -> dict:
    """一条 messages 行（键集合 == messages.ROW_COLUMNS，与 `upsert_batch` 的契约一致）。"""
    return {"msg_key": f"{account}|{chat_md5}|message_0.db|{n}", "account": account,
            "chat_md5": chat_md5, "chat_id": chat_id, "shard": "message_0.db",
            "local_id": n, "server_id": n, "ts": 100 + n, "msg_type": 1,
            "sender_id": None, "sender_name": None, "is_self": 0,
            "content": f"消息 {n}", "media_hint": None}


def _victim_row(n: int) -> dict:
    return _row(VICTIM, VICTIM_MD5, n)


def _keeper_row(n: int) -> dict:
    """留在索引里的另一个会话：没有它，「删干净了」与「把索引删空了」分不开。"""
    return _row(KEEPER, KEEPER_MD5, n)


class _BrokenExclusions:
    """`count()` 抛 `sqlite3.DatabaseError` = 「排除清单读不出来」（spec §4.2 的 fail-closed 闸门）。

    只打桩 `count`，别的一概不假装：`execute_chat` 的闸门走的正是它。`path()` 也要给——
    拒绝时构造的 hint 会读它，缺了就会在构造异常的路上抛 AttributeError，
    把「清单读不出来」变成「测试自己写错了」。
    """

    def count(self, con) -> int:
        raise sqlite3.DatabaseError("disk I/O error（桩）")

    def path(self) -> pathlib.Path:
        return paths.HOME_DIR / "exclusions.db"


def _load_vec(con) -> None:
    """裸 `sqlite_vec.load`：夹具与断言都要**独立于被测代码**（不用 vector.load）。"""
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)


def _install_vector_index(con) -> None:
    """把库变成「装了向量索引的机器」：建 vec0 表，并给**每条消息**种一行向量。

    vec0 的 metadata 列（`account` / `ts`）**不许 NULL**，两列都必须给值——只给 account
    会报 `Expected integer for INTEGER metadata column ts, received NULL`。
    `acct_chat` 走 `messages.acct_chat`（构建侧、查询侧与删除谓词共用的那个分区键）。
    """
    _load_vec(con)
    con.executescript(store.VECTOR_SCHEMA)
    rows = con.execute("SELECT id, account, chat_md5, ts FROM messages").fetchall()
    con.executemany(
        "INSERT INTO vec_messages(msg_rowid, embedding, acct_chat, account, ts, preview) "
        "VALUES (?,?,?,?,?,NULL)",
        [(r["id"], sqlite_vec.serialize_float32([0.1] * store.VECTOR_DIM),
          messages.acct_chat(r["account"], r["chat_md5"]), r["account"], r["ts"])
         for r in rows])
    con.commit()


@pytest.fixture(autouse=True)
def _never_touch_real_home(monkeypatch, tmp_path):
    """红线：把全部路径常量指向 tmp_path，并断言不在真实数据目录下。"""
    home = tmp_path / "home"
    monkeypatch.setattr(paths, "HOME_DIR", home)
    monkeypatch.setattr(keys, "CACHE_FILE", home / "passphrase.json")
    monkeypatch.setattr(store, "DB_PATH", home / "index.db")
    monkeypatch.setattr(tasks, "TASKS_FILE", home / "tasks.json")
    monkeypatch.setattr(vector, "MODEL_CACHE_DIR", home / "models")
    # spec §7：lsof 注入桩。必须打在 **_run** 这一层，不能把 _holders 整个换掉——
    # 本文件里的 test_holders_parses_lsof_output 要调的正是真 _holders，autouse fixture
    # 先执行会把它替换成桩，那个测试就永远拿不到真函数。
    # 三个字段都是被测代码的隐式契约：_holders 只读 stdout，_tmutil 还要读 returncode 与
    # stderr（非零退出要报「无法判定」而不是「无输出」）。少一个就是一个 AttributeError。
    monkeypatch.setattr(purge, "_run",
                        lambda *a, **kw: type("R", (), {"stdout": "", "stderr": "",
                                                        "returncode": 0})())
    # 遍历 known_artifacts() 而不是 artifacts()：后者只含有 CLI 开关的那几个，scope=None
    # 的（models/、tasks.json）同样会被删，也必须逐个确认路径被重定向过。
    arts = purge.known_artifacts()
    # 10 → 11：F-A（T7 复验）给 tasks 加了跨进程锁文件，已按本用例的红线要求逐条核对
    # （它是 tasks.lock_file()，跟随被 patch 的 TASKS_FILE，落点仍在 tmp 内）。
    assert len(arts) == 11, \
        f"红线：清单条目数由 11 变为 {len(arts)}，必有产物未被保护，须先核对再改这个数"
    for a in arts:
        assert a.path != REAL_HOME and REAL_HOME not in a.path.parents, \
            f"红线：{a.path} 指向真实数据目录"


def test_db_paths_is_the_wal_trio(monkeypatch, tmp_path):
    """index 是文件族：基库 + WAL + SHM。三者缺一都会在异常路径下留明文（spec §2）。"""
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "index.db")
    got = store.db_paths()
    assert got == (tmp_path / "index.db",
                   tmp_path / "index.db-wal",
                   tmp_path / "index.db-shm")


def test_plan_touches_nothing(tmp_path):
    """dry-run 的本质是「没动手」，不是「返回值对」（spec §7）。"""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 1000)
    before = sorted((p.name, p.stat().st_size, p.stat().st_mtime) for p in home.iterdir())
    purge.plan(frozenset({"index"}))
    # --all 是 plan() 唯一越过 lstat 的分支（要 iterdir 枚举顶层），必须一并验证：
    # 只测 all_=False 的话，往 --all 分支里加一次写入，本文件四个测试全都看不见。
    purge.plan(frozenset(), all_=True)
    after = sorted((p.name, p.stat().st_size, p.stat().st_mtime) for p in home.iterdir())
    assert before == after


def test_plan_marks_missing_artifacts():
    """产物不存在时标 exists=False，而不是报错（幂等的前半，spec §5.1 第 5 步）。"""
    p = purge.plan(frozenset({"keys"}))
    keys_items = [i for i in p.items if i.artifact.scope == "keys"]
    assert len(keys_items) == 1
    assert keys_items[0].exists is False
    assert keys_items[0].size == 0


def test_plan_index_has_three_items():
    """index 必须是三个独立 Artifact（基库/-wal/-shm），各自一行、各自 stat（spec §4）。"""
    p = purge.plan(frozenset({"index"}))
    assert [i.artifact.path.name for i in p.items] == \
        ["index.db", "index.db-wal", "index.db-shm"]
    assert all(i.artifact.kind == "file" for i in p.items)


def test_plan_refuses_unknown_scope():
    """未知范围必须是**响亮的拒绝**，不能是静默空操作。

    没有这道闸门时，一次改名（CLI 加了 `--media` 而 artifacts() 里那条被改名，或反过来）
    会让 `purge --media --yes` 印一张空表、报「已删除 0 项，释放 0 B」并**退 0**——一门专门
    用来销毁数据的命令报成功却一个字节没删。这比删错更难发现：没有任何输出提示异常。
    """
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.plan(frozenset({"mediia"}))
    assert "mediia" in str(ei.value)


def test_named_scopes_are_exactly_the_documented_five():
    """具名范围的名单是一份**要成对演进**的契约：新增一个就要同时加 CLI 开关、文档行与
    test_cli 里的 parametrize 用例。这条钉住名单本身，test_cli 那条钉住开关真的能用。"""
    assert {a.scope for a in purge.artifacts()} == \
        {"keys", "index", "media", "moments", "tmp"}


def test_plan_all_enumerates_home_including_strays(tmp_path):
    """--all 的计划**不由 known_artifacts() 生成**，而是枚举 HOME_DIR 顶层，
    这样才列得出散落文件（spec §3）；已知产物反过来要从 loose 里排除。"""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 1000)        # 有 CLI 开关的已知产物
    (home / "models").mkdir()                           # **没有** CLI 开关的已知产物
    (home / "leaked.db").write_bytes(b"stray")          # 真散落：不属于任何已知产物
    p = purge.plan(frozenset(), all_=True)
    assert p.all_ is True
    assert {x.path.name for x in p.loose} == {"leaked.db"}, \
        "loose 必须只剩真散落——两类已知产物（有开关的与没有开关的）都在 items 里"
    # 反断言：排除集若用 artifacts()（只含有开关的那些）算，models/ 会**同时**出现在 items
    # 与 loose 里，而计划表会把同一个路径渲染两遍。这个测试此前只查 index.db，恰好看不见
    # models/ 那一半——score=None 的产物是这次新增的，正是漏得掉的那类。
    # （注意别把它说成「体积重复计入 freed」：--all 的 freed 是 _tree_size 走盘量出来的，
    #   不是 items+loose 求和，所以重复项不会让 freed 翻倍。）
    both = ({x.path for x in p.loose} & {i.artifact.path for i in p.items})
    assert not both, f"同一路径同时出现在 items 与 loose：{both}"
    assert {"index.db", "models"} <= {i.artifact.path.name for i in p.items}


def test_execute_keys_does_not_touch_index(tmp_path):
    """反向断言：防范围泄漏。比「删了该删的」更重要（spec §7）。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "passphrase.json").write_text("{}")
    (home / "index.db").write_bytes(b"x" * 500)
    purge.execute(purge.plan(frozenset({"keys"})))
    assert not (home / "passphrase.json").exists()
    assert (home / "index.db").exists()


def test_execute_is_idempotent(tmp_path):
    home = tmp_path / "home"; home.mkdir(parents=True)
    # 必须先放一个真产物：home 空着的话两次都会走「跳过不存在项」的短路，
    # execute() 就算一个字节都不删，这个测试照样通过——它测的就不是幂等了。
    (home / "passphrase.json").write_text("{}")
    r1 = purge.execute(purge.plan(frozenset({"keys"})))
    r2 = purge.execute(purge.plan(frozenset({"keys"})))
    assert r1.removed == (home / "passphrase.json",)   # 第一次真删了
    assert r2.removed == ()                            # 第二次什么都没删
    assert r2.freed == 0


def test_execute_continues_after_one_failure(tmp_path, monkeypatch):
    """部分失败不中止：某个路径删不掉，不应放弃其余（spec §5.1 第 4 步）。
    这正是提交信息里写的行为——把 except 分支里的 append 删掉，本测试即失败。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 10)
    (home / "index.db-wal").write_bytes(b"y" * 20)
    real_remove = purge._remove

    def flaky(item):
        if item.artifact.path.name == "index.db":
            raise PermissionError("模拟：该文件删不掉")
        return real_remove(item)

    monkeypatch.setattr(purge, "_remove", flaky)
    r = purge.execute(purge.plan(frozenset({"index"})))
    assert [p.name for p, _ in r.failed] == ["index.db"]
    assert not (home / "index.db-wal").exists()        # 后面那一项仍然被删了


def test_execute_tolerates_stale_plan(tmp_path):
    """CLI 是「先 dry-run 出计划 → 用户确认 → 再 execute」，两步之间产物可能已被删掉。
    spec §5.1 第 5 步要求这算成功，不是失败。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "passphrase.json").write_text("{}")
    p = purge.plan(frozenset({"keys"}))
    (home / "passphrase.json").unlink()                # 计划之后、执行之前没了
    r = purge.execute(p)
    assert r.failed == ()
    assert r.removed == ()


def test_execute_dir_reports_tree_size_not_inode_size(tmp_path):
    """`purge --tmp` 删的是解密明文临时目录。目录的 lstat().st_size 只是 inode 自身
    大小（几十到几百字节），若直接相加，无论删掉多少明文都报「释放 ~96 B」。

    计划表与汇总行两个数字同源，在本测试里一并钉住（见下）。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    tmpdir = home / "tmp"
    tmpdir.mkdir()
    (tmpdir / "a.db").write_bytes(b"a" * 3000)         # 模拟散落的解密明文
    (tmpdir / "b.db").write_bytes(b"b" * 1000)
    p = purge.plan(frozenset({"tmp"}))
    item = p.items[0]
    assert item.artifact.kind == "dir"
    # 同一个数字有两处要用：**计划表**（用户据以说 --yes 的那张表）与 execute 的汇总行。
    # _item 对目录走 _tree_size 之后两处同源；此前计划表靠渲染器印「—」回避，代价是最大的
    # 一项（media/，10 GB）在那张表上看起来无事发生。
    assert item.size == 4000, "计划表里的目录体积必须是整树，不是 lstat().st_size"
    r = purge.execute(p)
    assert r.freed == 4000
    assert r.freed == item.size, "计划表与汇总行必须是同一个数字"
    assert not tmpdir.exists()


def test_execute_media_scope_does_not_touch_index(tmp_path):
    """`--media` 只能删导出物。**反向断言**在这里比正断言重要：范围泄漏是不可逆的（一次
    误删就是 5 GB 索引 + 2.5–3 小时重建，或一枚要 sudo + 重登才回得来的密钥），而正断言
    「media 被删了」在泄漏发生时照样绿。

    media/ 是 HOME_DIR 里最大也最可读的一堆（解密后的图片/视频/语音 + 浏览器直接打开的
    HTML/MD），它此前根本没有开关——只能连同索引与密钥一起 --all 掉。
    """
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 1000)
    (home / "passphrase.json").write_bytes(b"k" * 10)
    media_dir = home / "media"; media_dir.mkdir()
    (media_dir / "shot.jpg").write_bytes(b"j" * 500)
    r = purge.execute(purge.plan(frozenset({"media"})))
    assert not media_dir.exists()
    assert (home / "index.db").exists(), "范围泄漏：--media 动了检索库"
    assert (home / "passphrase.json").exists(), "范围泄漏：--media 动了密钥缓存"
    assert r.freed == 500


def test_execute_moments_scope_does_not_touch_media(tmp_path):
    """同上的反向断言，针对 `--moments`：朋友圈导出与媒体导出是两个目录、两个开关。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "media").mkdir()
    (home / "media" / "shot.jpg").write_bytes(b"j" * 500)
    moments_dir = home / "moments"; moments_dir.mkdir()
    (moments_dir / "sns.html").write_bytes(b"h" * 300)
    r = purge.execute(purge.plan(frozenset({"moments"})))
    assert not moments_dir.exists()
    assert (home / "media" / "shot.jpg").exists(), "范围泄漏：--moments 动了媒体导出"
    assert r.freed == 300


def test_named_scope_never_touches_scopeless_artifacts(tmp_path):
    """`models/` 与 `tasks.json` 的 scope 是 None——**任何具名范围都不得碰到它们**，它们只
    随 `--all` 一起走。判据的写法（`all_ or a.scope in scopes`）一旦被改成别的形状这里会红：
    models/ 是公开模型资产，被 --media 顺手删掉是纯损失（要靠重新下载才回得来）。
    """
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "models").mkdir()
    (home / "models" / "m.onnx").write_bytes(b"m" * 100)
    (home / "tasks.json").write_text("{}")
    (home / "media").mkdir()
    purge.execute(purge.plan(frozenset({"media"})))
    assert (home / "models" / "m.onnx").exists(), "scope=None 的产物被具名范围删掉了"
    assert (home / "tasks.json").exists(), "scope=None 的产物被具名范围删掉了"


def test_execute_all_removes_tree_and_does_not_recreate(tmp_path):
    """--all 删整树，且**不重建** HOME_DIR（spec §5.1 第 6 步）。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 100)
    (home / "leaked.db").write_bytes(b"stray")     # 不属于任何具名范围
    (home / "models").mkdir()
    r = purge.execute(purge.plan(frozenset(), all_=True))
    assert not home.exists()
    assert r.home_removed is True


def test_execute_all_on_missing_home_is_idempotent():
    """HOME_DIR 不存在时 --all 仍成功（AC4）。rmtree 对缺失路径会抛 FileNotFoundError，
    spec §5.1 第 5 步要求把它纳入幂等规则。"""
    r = purge.execute(purge.plan(frozenset(), all_=True))
    assert r.home_removed is False
    assert r.failed == ()


def test_execute_all_reports_what_it_removed(tmp_path):
    """--all 走整树 rmtree，绕过了 items 循环。若不在此补齐 removed/freed，
    CLI 会在删光整棵 HOME_DIR 之后打印「已删除 0 项，释放 0 B」——与 spec §3/§6
    要求的「删了什么 + 释放多少」直接冲突。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 4096)
    (home / "leaked.db").write_bytes(b"y" * 100)     # 散落文件也计入体积
    r = purge.execute(purge.plan(frozenset(), all_=True))
    assert r.freed == 4196
    assert {p.name for p in r.removed} >= {"index.db", "leaked.db"}


def test_execute_refuses_when_index_is_held(tmp_path, monkeypatch):
    """有进程持有 index.db 时拒绝执行。**不要用 tasks 注册表判断**：它只覆盖本工具登记过
    的任务，而这里要防的是「文件仍被打开」本身（非本工具进程也算）——句柄检测跨进程且
    直接对应 unlink 的后果（spec §5.1 第 1 步）。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 100)
    monkeypatch.setattr(purge, "_holders", lambda p: [4242] if p.name == "index.db" else [])
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute(purge.plan(frozenset({"index"})))
    assert "4242" in str(ei.value)
    assert (home / "index.db").exists()          # 一个字节都没删


def test_execute_refuses_symlink_and_leaves_target(tmp_path):
    """符号链接必须拒绝：unlink 只删链接不删目标，而「列目录看到空了」的核查会给出
    false-clean——用户以为明文没了，其实还在（spec §5.1 第 2 步、AC7）。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    real = tmp_path / "elsewhere.db"
    real.write_bytes(b"plaintext" * 100)
    (home / "index.db").symlink_to(real)
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute(purge.plan(frozenset({"index"})))
    assert "符号链接" in str(ei.value)
    assert real.exists()                          # 目标必须完好无损


def test_holders_parses_lsof_output(tmp_path, monkeypatch):
    """lsof -n -P 不可省（否则 DNS 解析卡 30 秒，见 account.py:60 的既有教训）。"""
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return type("R", (), {"stdout": "COMMAND  PID USER\nWeChat 1234 leo\n"})

    monkeypatch.setattr(purge, "_run", fake_run)
    p = tmp_path / "index.db"; p.write_bytes(b"x")
    assert purge._holders(p) == [1234]
    assert seen["argv"][:3] == ["lsof", "-n", "-P"]


def test_execute_all_refuses_symlink_inside_home(tmp_path, monkeypatch):
    """--all 的整树 rmtree 会把树内的符号链接当作普通条目 unlink，**目标明文原封不动
    留在盘上**；核查却报「计划内路径已清空」——用户的 5 GB 明文还在外置盘上。
    实测于 Python 3.12.13：rmtree(含 symlink 的目录, onexc=...) 删掉树、保住目标。"""
    real = tmp_path / "elsewhere"; real.mkdir()
    (real / "secret.db").write_bytes(b"plaintext" * 100)
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "leaked.db").symlink_to(real / "secret.db")     # HOME_DIR 顶层的符号链接
    monkeypatch.setattr(paths, "HOME_DIR", home)
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute(purge.plan(frozenset(), all_=True))
    assert "符号链接" in str(ei.value)
    assert home.exists()                                # 一个字节都没删
    assert (real / "secret.db").exists()                # 目标明文完好


def test_execute_all_allows_intra_tree_symlink(tmp_path, monkeypatch):
    """HuggingFace 的 blob/snapshot 布局会在 models/ 下建指向**树内** blobs/ 的符号链接
    （本仓库 vector.py 用 huggingface_hub 下载模型就会产生，实测本机有 5 个）。
    目标在树内 → 整树 rmtree 会连它一起删，不存在 false-clean；一律拒绝会让
    `purge --all` 在标准安装上永远跑不起来，而 spec §9 又否掉了 --force。"""
    home = tmp_path / "home"
    blob = home / "models" / "blobs" / "abc123"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"weights")
    snap = home / "models" / "snapshots" / "rev1"
    snap.mkdir(parents=True)
    (snap / "model.onnx").symlink_to(blob)      # 树内链接：不得拒绝
    monkeypatch.setattr(paths, "HOME_DIR", home)
    r = purge.execute(purge.plan(frozenset(), all_=True))
    assert r.failed == ()
    assert not home.exists()                    # 整棵树连同 blob 一起没了


def test_execute_all_refuses_symlink_loop(tmp_path, monkeypatch):
    """符号链接环：3.12 的 Path.resolve 抛 RuntimeError（3.13 才改成 OSError）。
    接不住的话它会以裸异常穿透 execute()，违反「core 层只抛 AgentWeChatError 子类」。
    环本身不留东西，但属病态配置，拒绝比放行更容易解释——且与「判不出来就别删」一致。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    a, b = home / "a", home / "b"
    a.symlink_to(b); b.symlink_to(a)          # a ↔ b
    monkeypatch.setattr(paths, "HOME_DIR", home)
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute(purge.plan(frozenset(), all_=True))
    assert "符号链接" in str(ei.value)
    assert home.exists()                      # 一个字节都没删


def test_execute_all_tolerates_symlinked_home_ancestor(tmp_path, monkeypatch):
    """home 的**祖先**是符号链接时（macOS 上 /tmp → /private/tmp，$TMPDIR 与 mktemp -d
    给出的路径同理），link.resolve() 给的是真路径而 paths.HOME_DIR 未解析——两边形态不同
    会让树内链接被误判为逃逸、整片拒绝。home 自身不是链接，故 is_symlink() 那道闸门不触发，
    只有 home 也解析过才判得对。"""
    real_parent = tmp_path / "real"; real_parent.mkdir()
    link_parent = tmp_path / "link"; link_parent.symlink_to(real_parent)
    home = link_parent / "home"                 # home 自身不是链接，祖先是
    base = home / "models"
    blob = base / "blobs" / "abc"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"weights")
    (base / "snapshots").mkdir()
    (base / "snapshots" / "m.onnx").symlink_to(blob)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    r = purge.execute(purge.plan(frozenset(), all_=True))
    assert r.failed == ()
    assert not (real_parent / "home").exists()  # 整棵树被删掉


def test_execute_all_refuses_nested_symlink(tmp_path, monkeypatch):
    """符号链接不只在 HOME_DIR 顶层：树内任意深度都会被 rmtree 当普通条目 unlink，
    目标明文留存。spec §5.1 第 2 步要求对**每个待删路径**做 lstat，故必须走整棵树。"""
    real = tmp_path / "elsewhere"; real.mkdir()
    (real / "secret.db").write_bytes(b"plaintext" * 100)
    home = tmp_path / "home"; (home / "models").mkdir(parents=True)
    (home / "models" / "link.db").symlink_to(real / "secret.db")
    monkeypatch.setattr(paths, "HOME_DIR", home)
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute(purge.plan(frozenset(), all_=True))
    assert "符号链接" in str(ei.value)
    assert home.exists()                                # 一个字节都没删
    assert (real / "secret.db").exists()                # 目标明文完好


def test_execute_all_refuses_symlinked_home(tmp_path, monkeypatch):
    """HOME_DIR 本身是符号链接时 --all 必须拒绝（spec §5.1 第 2 步）。
    实测 3.12.13：rmtree(link) 不传 handler 抛 OSError；传了 onexc 则一条
    'Cannot call rmtree on a symbolic link' 进 handler，链接与目标都完好。"""
    real = tmp_path / "real-home"; real.mkdir()
    (real / "index.db").write_bytes(b"x" * 100)
    link = tmp_path / "home-link"; link.symlink_to(real)
    monkeypatch.setattr(paths, "HOME_DIR", link)
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute(purge.plan(frozenset(), all_=True))
    assert "符号链接" in str(ei.value)
    assert (real / "index.db").exists()          # 目标必须完好无损


def test_verify_does_not_call_kept_artifacts_residue(tmp_path):
    """**22 条评审里最严重的一条**：选择性 purge 后 index.db 是按范围故意保留的，
    拿「HOME_DIR 空不空」当判据会把它误报为残留，与 AC2 直接打架（spec §5.2）。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "passphrase.json").write_text("{}")
    (home / "index.db").write_bytes(b"x" * 100)
    plan = purge.plan(frozenset({"keys"}))
    purge.execute(plan)
    rep = purge.verify(plan)
    assert rep.not_removed == ()                       # 计划内的都删掉了
    assert any(p.name == "index.db" for p in rep.kept)  # 保留项归入 kept，不是残留


def test_verify_reports_unremoved_planned_paths(tmp_path):
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "passphrase.json").write_text("{}")
    plan = purge.plan(frozenset({"keys"}))
    rep = purge.verify(plan)                            # 没执行就核查
    assert [p.name for p in rep.not_removed] == ["passphrase.json"]


def test_verify_reports_surviving_loose_entry(tmp_path):
    """--all 部分失败时活下来的 loose 顶层条目必须报为残留。它既不在 items 里、又被从
    kept 里排除，此前**两个桶都不进**，报告会说「计划内路径已清空」——
    正是 not_removed 这个字段存在的意义所在的那种假象（spec §5.2 第 1 项）。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 100)         # 具名产物
    (home / "leaked.db").write_bytes(b"stray")          # loose：不属于任何具名范围
    rep = purge.verify(purge.plan(frozenset(), all_=True))   # 什么都不删 = 全都没删掉
    names = {p.name for p in rep.not_removed}
    assert {"index.db", "leaked.db"} <= names, f"loose 存活项漏报，实际只报了 {names}"
    assert rep.kept == ()          # 两者都在计划内，不得落进「按范围保留」桶


def test_verify_reports_dangling_symlink_as_present(tmp_path):
    """断链的链接文件仍在盘上。exists() 跟随符号链接会判它「已删除」，而链接明明还在——
    在这门专门用来抓假象的字段里制造假象，故用不跟随链接的判据（`name_there`）。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "passphrase.json").symlink_to(home / "gone.json")   # 目标不存在
    rep = purge.verify(purge.plan(frozenset({"keys"})))
    assert [p.name for p in rep.not_removed] == ["passphrase.json"]


def test_verify_reports_still_held_index(tmp_path, monkeypatch):
    """held 是 spec §5.2 的第三项，但 fixture 的 _run 桩让 _holders 恒返回 []，
    于是它在全部 verify 测试里都是空字典——把 i.exists 的判据反写、或把键名写错，
    测试会全绿。故此处直接覆盖 _holders。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 100)
    (home / "index.db-wal").write_bytes(b"w" * 10)      # 存在，但无人持有
    monkeypatch.setattr(purge, "_holders",
                        lambda p: [4242] if p.name == "index.db" else [])
    rep = purge.verify(purge.plan(frozenset({"index"})))
    assert rep.held.get("index.db") == [4242]
    # 钉住 pids 的真值过滤：写成 `if i.exists` 再取值的话，index.db-wal 会以**空 pid 列表**
    # 进 held，渲染时多出一行「⚠️ index.db-wal 仍被 pid  持有」——一条对用户说的假话。
    #（原注释写的是「去掉 `and (pids := ...)` 测试仍会通过」——实测证伪：只去掉真值判断，
    #  下面的断言会红；walrus 一并去掉则 pids 未定义，那是 NameError，也不是绿。）
    assert "index.db-wal" not in rep.held


def test_tmp_leaks_probe_follows_the_writer(tmp_path, monkeypatch):
    """bootstrap 的 mkstemp（`prefix=TMP_PREFIX`，未传 dir=）落 `tempfile.gettempdir()`；
    进程被 kill 时残留。spec §2 那张表的第四行——核查必须检查**同一个目录**（EA-101）。

    两个形态都钉：① TMPDIR 指到别处（且要清 `tempfile` 的缓存，否则 gettempdir() 仍答旧值）；
    ② TMPDIR 指向**不可写**目录——gettempdir() 的回落链决定实际落点，探测必须跟着它走，
    不能在 purge 里另写一份 `os.environ.get("TMPDIR","/tmp")`。"""
    import tempfile as _tf

    from agent_wechat import bootstrap

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(_tf, "tempdir", None)          # 清缓存：gettempdir() 首次调用后不再看 env
    (tmp_path / (bootstrap.TMP_PREFIX + "abc123.lldb")).write_text("script")
    rep = purge.verify(purge.plan(frozenset({"keys"})))
    assert any("awcap_" in x for x in rep.tmp_leaks)

    bad = tmp_path / "nowrite"
    bad.mkdir(mode=0o500)
    monkeypatch.setenv("TMPDIR", str(bad))
    monkeypatch.setattr(_tf, "tempdir", None)
    real = _tf.gettempdir()
    # **名字必须唯一**：这一步落在**真实**的 `gettempdir()` 里（被测的就是那个目录），
    # 固定名会让并发跑的两个 pytest 互删、被 kill 时留残留（review-code 的 Standards 轴 #15）。
    tag = f"def456-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    probe = pathlib.Path(real) / (bootstrap.TMP_PREFIX + tag + ".lldb")
    probe.write_text("script")
    try:
        rep2 = purge.verify(purge.plan(frozenset({"keys"})))
        assert any(tag in x for x in rep2.tmp_leaks), real
    finally:
        probe.unlink(missing_ok=True)


def test_verify_degrades_when_tmutil_missing(monkeypatch):
    """tmutil 缺失/超时时该项报「无法判定」，不能让整个核查崩掉（spec §5.2）。"""
    def boom(*a, **k):
        raise FileNotFoundError("tmutil")

    monkeypatch.setattr(purge, "_run", boom)
    rep = purge.verify(purge.plan(frozenset({"keys"})))
    assert "无法判定" in rep.tm_excluded


def test_tmutil_maps_nonzero_exit_to_undecidable(monkeypatch):
    """非零退出且只写了 stderr 时，此前被印成「无输出」——用户读到的意思是「就是没输出」，
    而不是「判不出来」，而这两件事对一门专门用来抓假象的核查字段含义正相反。
    实测：正常用法恒 rc=0（`[Included]`/`[Excluded]` 都走 stdout，排没排除都是 0），
    所以这个分支不会误伤正常输出。"""
    def exit64(*a, **k):
        return type("R", (), {"stdout": "", "stderr": "boom", "returncode": 64})()

    monkeypatch.setattr(purge, "_run", exit64)
    rep = purge.verify(purge.plan(frozenset({"keys"})))
    assert "无法判定" in rep.tm_excluded and "64" in rep.tm_excluded


def test_execute_refuses_when_home_is_root(monkeypatch):
    """闸门在 execute 里也要有一道，且是第一句：CLI 是「先 plan → 再 execute」，plan 到
    execute 之间 paths.HOME_DIR 可能已被改指（或首次 plan 走的不是这条路径），到那时就只剩
    「已经开始删」了。测试文件里那道 autouse 红线保护不了这条——它保护的是测试自己。"""
    monkeypatch.setattr(paths, "HOME_DIR", pathlib.Path("/"))
    p = purge.PurgePlan(all_=False, items=(), loose=())
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute(p)
    assert "文件系统根" in str(ei.value)


def test_execute_all_watches_loose_entries_for_holders(tmp_path, monkeypatch):
    """`--all` 的删除面是**整棵树**，而 items 只是具名产物：loose（models/、tasks.json、
    散落明文）同样会被 rmtree 删掉，那里一个被写着的文件 unlink 会「成功但空间不释放」
    ——正是这道检查存在的理由。此前只遍历 items，而错误文案「有进程正在使用待删产物」
    是个超集说法。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 10)
    (home / "models").mkdir()
    monkeypatch.setattr(purge, "_holders",
                        lambda p: [777] if p.name == "models" else [])
    p = purge.plan(frozenset(), all_=True)
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute(p)
    assert "正在使用" in str(ei.value) and "models" in str(ei.value)
    assert (home / "models").exists(), "拒绝时一个字节都不该删"


def test_plan_refuses_when_home_unreadable(tmp_path):
    """spec §6 那一行：「plan 阶段权限不足（目录不可读）→ PurgeRefused（**不是**「删除失败」，
    发生在任何 unlink 之前）」。此前这种情况被 _item() 吞成「不存在」——dry-run 对一堆活着的
    文件打「（不存在）」，--yes 则在 is_symlink() 上抛裸 PermissionError。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 10)
    os.chmod(home, 0o000)
    try:
        with pytest.raises(errors.PurgeRefused) as ei:
            purge.plan(frozenset({"index"}))
    finally:
        os.chmod(home, 0o700)          # 不还原的话 pytest 自己清不掉这棵树
    assert "不可读" in str(ei.value)


def test_plan_refuses_when_home_parent_unsearchable(tmp_path, monkeypatch):
    """闸门里那个 `except OSError: unreadable = True` 分支（为堵裸 OSError 而加）此前无测试。
    HOME_DIR 的**父目录**不可搜索时，`home.exists()` 抛 EACCES 而不是返回 False（3.12 的
    _IGNORED_ERRNOS 不含 EACCES，实测确认）——「在不在」都判不出来，与「不可读」同归一类。"""
    parent = tmp_path / "sealed"; parent.mkdir()
    home = parent / "home"; home.mkdir()
    monkeypatch.setattr(paths, "HOME_DIR", home)
    os.chmod(parent, 0o000)
    try:
        with pytest.raises(errors.PurgeRefused) as ei:
            purge.plan(frozenset({"index"}))
    finally:
        os.chmod(parent, 0o700)          # 不还原的话 pytest 自己清不掉这棵树
    assert "不可读" in str(ei.value)


def test_plan_refuses_home_with_symlinked_ancestor(tmp_path, monkeypatch):
    """闸门的两条主目录判据只 resolve 了左边（`resolved == Path.home()`）。$HOME 路径链上
    有符号链接时（macOS 上 /tmp → /private/tmp 恒成立），未经 resolve 的 Path.home() 与
    已 resolve 的 resolved **永远不相等**——于是「数据目录 = $HOME 本身」这种少打
    /.agent-wechat 的手滑被放行，`purge --all --yes` 会删掉整个家目录。
    实测（修前）：`HOME=/tmp/leohome AGENT_WECHAT_HOME=/tmp/leohome` 闸门放行、照常印计划表。
    同一个错误在 _escapes_home 里已修（那里注释明写「两边都要解析过才能比」）。"""
    real = tmp_path / "realhome"; real.mkdir()
    link = tmp_path / "linkhome"; link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("HOME", str(link))
    monkeypatch.setattr(paths, "HOME_DIR", link)
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.plan(frozenset({"keys"}))
    assert "用户主目录" in str(ei.value)


def test_plan_refuses_symlink_loop_home(tmp_path, monkeypatch):
    """指向符号链接环的 AGENT_WECHAT_HOME 让 **dry-run 也崩**：3.12 的 Path.resolve 对环抛
    RuntimeError（3.13 才改成 OSError），而 `resolved = home.resolve()` 不在 try 内——
    用户只是想看看会删什么，拿到的是一整个 traceback。实测（修前）：
    `RuntimeError: Symlink loop from '/private/tmp/purgeloop/a'`。
    这也是「core 层一律抛 AgentWeChatError 子类」那条约定的违反。"""
    a = tmp_path / "a"; b = tmp_path / "b"
    a.symlink_to(b); b.symlink_to(a)
    monkeypatch.setattr(paths, "HOME_DIR", a)
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.plan(frozenset({"keys"}))
    assert "无法解析" in str(ei.value)


def test_plan_refuses_sudo_users_home(tmp_path, monkeypatch):
    """sudo 下 HOME=/var/root，只比 Path.home() 的话 /Users/<你> 能过闸——而本项目为 bootstrap
    文档化了 sudo，「少打 /.agent-wechat」正是这道闸门要接住的手滑。SUDO_UID 是 sudo 给出的
    **发起者** uid，拿它查 passwd 才还原得出那个人。

    这里走**真实** passwd 查询而不是打桩：要钉的正是「查询这一步真的会被走到」。
    闸门在 plan() 第一句，所以即使它失效也不会碰真实数据（autouse 红线还兜着）。"""
    real_home = pathlib.Path.home()        # 必须在改 $HOME 之前取；REAL_HOME 是数据目录不是家目录
    monkeypatch.setenv("HOME", str(tmp_path / "rootlike"))   # 模拟 sudo 下的 $HOME
    monkeypatch.setenv("SUDO_UID", str(os.getuid()))
    monkeypatch.setattr(paths, "HOME_DIR", real_home)        # 发起者（本进程用户）的家目录
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.plan(frozenset({"keys"}))
    assert "用户主目录" in str(ei.value)


def test_execute_refuses_when_artifact_parent_becomes_unreadable(tmp_path, monkeypatch):
    """产物父目录不可搜索时（lstat 需要父目录的 +x），符号链接检查此前抛**裸 OSError**：
    违反「core 层一律抛 AgentWeChatError 子类」，在 CLI 那边就是一个 traceback。
    计划先生成再改坏权限，正是 plan→execute 之间那个窗口的形态。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    sub = home / "sub"; sub.mkdir()
    (sub / "index.db").write_bytes(b"x" * 10)
    monkeypatch.setattr(store, "DB_PATH", sub / "index.db")
    p = purge.plan(frozenset({"index"}))
    os.chmod(sub, 0o000)
    try:
        with pytest.raises(errors.PurgeRefused) as ei:
            purge.execute(p)
    finally:
        os.chmod(sub, 0o700)
    assert "权限" in str(ei.value.hint or "") or "符号链接" in str(ei.value)
    assert (sub / "index.db").exists()


def test_execute_refuses_symlink_before_reporting_holders(tmp_path, monkeypatch):
    """检查顺序：links 必须排在 held **之前**。`_holders` 走 `lsof <path>`，而 lsof 会
    **解析符号链接**——产物是链接时，占用检查报出的是一堆持有**目标**的不相关进程
    （实测：拿 /etc/hosts 当目标，lsof 列出 7 个持有者）。退出码与安全性都对，但用户照着
    那条理由去杀进程也删不掉这个链接——真正的原因是它是链接。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    real = tmp_path / "elsewhere.db"; real.write_bytes(b"plaintext" * 10)
    (home / "index.db").symlink_to(real)
    monkeypatch.setattr(purge, "_holders", lambda p: [4242])   # 模拟 lsof 解析到目标的持有者
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute(purge.plan(frozenset({"index"})))
    assert "符号链接" in str(ei.value)
    assert "4242" not in str(ei.value), "理由必须是「它是链接」，不是「被 pid 4242 持有」"
    assert real.exists()


def test_execute_tmp_refuses_out_of_tree_symlink_in_tree(tmp_path):
    """具名范围里 kind="dir" 的只有 tmp/，它走的是 rmtree——与 --all 同一原语，故同样要扫
    它整棵树。实测：`tmp/evil.db -> /outside/secret.db`（700 KB）+ `purge --tmp --yes`
    退 0、「已删除 1 项」，树外明文原封不动，而核查里一个字都没提它——正是 spec §5.1 第 2 步
    要防的那种假清空。"""
    outside = tmp_path / "outside"; outside.mkdir()
    (outside / "secret.db").write_bytes(b"p" * 700_000)
    home = tmp_path / "home"; (home / "tmp").mkdir(parents=True)
    (home / "tmp" / "evil.db").symlink_to(outside / "secret.db")
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute(purge.plan(frozenset({"tmp"})))
    assert "符号链接" in str(ei.value)
    assert (home / "tmp" / "evil.db").is_symlink()      # 拒绝在任何删除之前
    assert (outside / "secret.db").exists()


def test_execute_tmp_allows_intra_tree_symlink(tmp_path):
    """目标在树内的链接不拒绝：rmtree 会把目标一并删掉，不存在假清空。一律拒绝会让
    blob/snapshot 式布局（huggingface_hub 下载模型就会产生，本仓库 vector.py 用它）
    永远删不掉，而 spec §9 又否掉了 --force。"""
    home = tmp_path / "home"; (home / "tmp").mkdir(parents=True)
    (home / "tmp" / "real.db").write_bytes(b"plaintext" * 10)
    (home / "tmp" / "alias.db").symlink_to(home / "tmp" / "real.db")
    r = purge.execute(purge.plan(frozenset({"tmp"})))
    assert r.failed == ()
    assert not (home / "tmp").exists()


def test_execute_all_allows_intra_tree_symlink_at_artifact_path(tmp_path):
    """M1 的连带修复：`--all` 下 `tmp/`（具名产物、kind=dir）**本身**是指向树内的链接时，
    此前会被 items 那条（文件型才该有的无条件拒绝）拦下，理由还写着「unlink 只删链接」——
    而 --all 根本不走 unlink，它 rmtree 整棵树，树内目标会一并删掉。判据该是 _escapes_home。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    real_tmp = home / "real-tmp"; real_tmp.mkdir()
    (real_tmp / "plain.db").write_bytes(b"plaintext" * 10)
    (home / "tmp").symlink_to(real_tmp)
    r = purge.execute(purge.plan(frozenset(), all_=True))
    assert r.failed == ()
    assert not home.exists()
    assert not real_tmp.exists()          # 树内目标随整树一并删除


def test_execute_all_partial_failure_reports_survivor(tmp_path):
    """`--all` 的部分失败分支（rmtree 中途某项删不掉）此前只被手工验收过，没有任何测试钉住。
    四条裁定一起钉：失败被记录、freed 扣掉没删掉的那部分、removed 不含仍活着的项、
    残留核查点名它。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 100)
    stuck = home / "stuck"; stuck.mkdir()
    (stuck / "plain.db").write_bytes(b"y" * 500)
    os.chmod(stuck, 0o500)                    # 可读可进入、不可写：rmtree 删不掉里面的文件
    try:
        p = purge.plan(frozenset(), all_=True)
        r = purge.execute(p)
        rep = purge.verify(p)
    finally:
        os.chmod(stuck, 0o700)                # 不还原的话 pytest 自己清不掉这棵树
    assert any(x.name == "stuck" for x, _ in r.failed), f"失败未被记录：{r.failed}"
    assert not any(x.name == "stuck" for x in r.removed)
    assert r.freed == 100, f"freed 必须扣掉活下来的 500 B，实际 {r.freed}"
    assert r.home_removed is False
    assert any(x.name == "stuck" for x in rep.not_removed), "残留核查必须点名它"


def test_exclusions_is_a_known_artifact():
    """排除清单必须进产物表——否则 `--all` 会把它当散落文件印成「未知」，
    而它是**唯一一个删了就不可重建**的东西（spec §4.1③）。"""
    arts = {a.path.name: a for a in purge.known_artifacts()}
    assert "exclusions.db" in arts
    a = arts["exclusions.db"]
    assert a.scope is None, "没有独立开关，只随 --all 一起走"
    assert a.sensitive is True
    assert "不可重建" in a.rebuild


# ---------- `purge --chat`：plan / execute / verify ----------

def test_plan_chat_touches_nothing(tmp_path):
    """dry-run 的本质是「没动手」，不是「返回值对」——两个库都要逐字节不变。

    **夹具是真库，不是两个垃圾文件。** brief 原版往 index.db / exclusions.db 各写 100 字节
    `b"x"`——plan_chat 必须从索引里数行，而垃圾字节连 `store.connect()` 的
    `executescript(SCHEMA)` 都过不去（`file is not a database`），于是那条测试对**任何**
    实现都红，红的还不是它要守的东西。改用真库后判据反而更硬：真库打开是读、关闭是 checkpoint，
    实测两个文件的字节与 mtime 都不变；而写墓碑动的是 exclusions.db、写索引动的是 index.db，
    两边的字节都在比。Task 7 的 dry-run 测试同样是真库形态（判据：墓碑 0 条 + 消息还在）。
    """
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:
        store.upsert_batch(con, [_victim_row(1), _victim_row(2)])
    before = {p.name: p.read_bytes() for p in home.iterdir()}
    plan = purge.plan_chat([{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}])
    after = {p.name: p.read_bytes() for p in home.iterdir()}
    assert before == after, "dry-run 动了磁盘"
    assert plan.entries[0].counts["messages"] == 2, \
        "计划必须真的数过索引——这条同时证明上面那两行是被读过的，不是碰巧没动"


def test_execute_chat_writes_the_tombstone_and_reports_it(tmp_path):
    """**核查要能证明不可逆性已经建立**（spec §5.5）：墓碑没写成功时，
    索引干净、目录没了、报告一片 ✅——而下次 --full 全捞回来。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:
        store.upsert_batch(con, [_victim_row(1), _victim_row(2)])
    report = purge.execute_chat(purge.plan_chat(
        [{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}]))
    with store.open() as con:
        assert exclusions.count(con) == 2
        assert store.chat_row_counts(con, "a", VICTIM)["messages"] == 0
    assert report[0].tombstone == 2


def test_verify_chat_flags_a_missing_tombstone(tmp_path, monkeypatch):
    """注入桩让墓碑写入失败 → 核查必须报「不可逆性未建立」并退 1，而不是报成功。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:
        store.upsert_batch(con, [_victim_row(1)])
    # 桩打在 `_write_tombstone` 上：它是写墓碑的**唯一**入口，而核查读的是排除清单的实际条数。
    # 桩打在 exclusions.add 上没用——execute_chat 走的是裸 SQL（见 _write_tombstone 的 docstring）。
    monkeypatch.setattr(purge, "_write_tombstone", lambda *a, **k: 0)
    plan = purge.plan_chat([{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}])
    purge.execute_chat(plan)
    res = purge.verify_chat(plan)
    assert res.tombstone_ok is False
    assert "不可逆性未建立" in res.render()


def test_execute_chat_refuses_when_lock_is_held(tmp_path):
    """拿不到写锁 → PurgeRefused，不许裸 OperationalError 穿透（Global Constraints）。

    **注意这条路径的入口不只一个**：`store.connect()` 每次都跑 `executescript(SCHEMA)`，
    那也是要写锁的——所以异常可能是从 `store.open()` 里抛出来的，而不是 `BEGIN IMMEDIATE`。
    实测（本机 SQLite 3.50 / WAL）：**种子库**（本夹具、以及跑过一次的机器）connect 成功、
    异常从 phase ① 的 `BEGIN IMMEDIATE` 抛；**新库**（SCHEMA 还没建过）才从 connect 抛。
    实现里那层 `except` 仍必须包住前者（见 execute_chat）；本测试只断言「翻成了 PurgeRefused
    且索引零变化」，不假定它从哪一行抛出——把包装范围绑在「哪一句抛」上就是下一次的洞。
    """
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:                     # 先种数据，否则「零变化」是个空断言
        store.upsert_batch(con, [_victim_row(1), _victim_row(2)])
    plan = purge.plan_chat([{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}])
    holder = store.connect()                      # 另开一条连接持住写事务
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO display_names VALUES ('a','x','y','z')")
    try:
        with pytest.raises(errors.PurgeRefused) as ei:
            purge.execute_chat(plan)
    finally:
        holder.rollback(); holder.close()
    # 消息不许说谎（F4/F6）：标题是**操作**（库损坏、磁盘满也会落到这一支），真因在 `{exc}` 里，
    # 并带上本轮进度——「此前已完成的」不能被说成没发生。数的单位是**项**（账号 × 会话），
    # 不是会话：跨账号 purge 里同一个会话是两项。
    msg = str(ei.value)
    assert "删除未执行" in msg and "database is locked" in msg, msg
    assert "本次计划 1 项" in msg and "此前已完成 0 项" in msg, msg
    # docstring 承诺了「索引零变化」就得真断言：没有这一句，一个「先删一半再抛」的实现
    # 也能让上面那条 pytest.raises 通过——**这门命令上那是最坏的形状**。
    with store.open() as con:
        assert store.chat_row_counts(con, "a", VICTIM)["messages"] == 2
        assert exclusions.count(con) == 0, "拒绝执行时不许落下墓碑"


def test_crash_between_tombstone_and_delete_is_recoverable(tmp_path):
    """**停在安全的那一侧**（Review Focus 3）：墓碑已提交、删除未提交时，
    状态必须是「挡住了但没删掉」——核查看得见、重跑能收口；
    反过来（删了但没挡）那批东西会复活，是这条命令最坏的形状。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:
        store.upsert_batch(con, [_victim_row(1), _victim_row(2)])
        # 模拟「墓碑落了、删除没跑」：只写墓碑
        con.execute("INSERT OR IGNORE INTO exclusions.excluded "
                    "(msg_key, account, chat_md5, excluded_at) "
                    "SELECT msg_key, account, chat_md5, 0 FROM messages "
                    "WHERE account='a' AND chat_id=?", (VICTIM,))
    plan = purge.plan_chat([{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}])
    res = purge.verify_chat(plan)
    assert res.leftover, "核查必须看见那两行还在"
    assert res.tombstone_ok, "墓碑已经落了，这一侧是安全的"
    purge.execute_chat(plan)                 # 重跑收口
    assert purge.verify_chat(plan).leftover == ()


def test_execute_chat_blocks_a_row_that_lands_between_the_two_phases(tmp_path, monkeypatch):
    """两段式里 phase ② 的**重派生**不是装饰（spec §5.4 的不变式「墓碑覆盖被删集合」）。

    一条在「墓碑已提交、删除还没跑」之间落进 `messages` 的消息会被谓词 DELETE 删掉，
    它必须同时进墓碑——否则**删了但没挡**，下次 `--full` 又回来（本命令最坏的形状）。

    注入点：在 `_write_tombstone` 的**第一次**调用之后塞一行——等价于「另一个写者在窗口里
    提交了一行」。真并发在这个夹具里造不出来（spec §5.4 说造不出来：phase ① 写的是
    exclusions.db 另一个库文件，主库那把写锁管不到它），所以按该节给的办法注入一行。
    **把 phase ② 改成「先删再派生」，这条会红**：那时重派生扫到的 messages 已经空了，
    窗口行被删掉却没被挡住；`report[0].tombstone` 也同时钉住「两阶段之和」这个计数契约。
    """
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:
        store.upsert_batch(con, [_victim_row(1), _victim_row(2)])
    real = purge._write_tombstone
    calls = []

    def spy(con, account, chat_id):
        n = real(con, account, chat_id)
        calls.append(n)
        if len(calls) == 1:            # phase ① 之后落进来的一行：phase ① 的墓碑覆盖不到它
            store.upsert_batch(con, [_victim_row(3)])
        return n

    monkeypatch.setattr(purge, "_write_tombstone", spy)
    plan = purge.plan_chat([{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}])
    report = purge.execute_chat(plan)
    with store.open() as con:
        assert store.chat_row_counts(con, "a", VICTIM)["messages"] == 0, "窗口行也要被删掉"
        blocked = {r[0].rsplit("|", 1)[1]
                   for r in con.execute("SELECT msg_key FROM exclusions.excluded")}
    assert blocked == {"1", "2", "3"}, f"被删的都必须被挡住，实际挡住 {blocked}"
    assert report[0].tombstone == 3, "报告要含 phase ② 补写的那条（两阶段之和）"


def test_verify_chat_counts_only_this_chats_exclusions(tmp_path, monkeypatch):
    """核查数的是**本次这个会话**的排除条数，不是清单的全局总数（spec §5.5）。

    全局数会被早先别的会话的排除垫高：一条本该报「不可逆性未建立」的核对上了别人的墓碑，
    于是报 ✅——而这次删掉的东西一条都没被挡住、下次 `--full` 全回来。
    """
    home = tmp_path / "home"; home.mkdir(parents=True)
    other = messages.md5_of("另一个会话")       # 形状照生产：chat_md5 是 md5，不是随便一个串
    with store.open() as con:
        store.upsert_batch(con, [_victim_row(1), _victim_row(2)])
        exclusions.add(con, [(f"a|{other}|message_0.db|1", "a", other),
                             (f"a|{other}|message_0.db|2", "a", other)])
    monkeypatch.setattr(purge, "_write_tombstone", lambda *a, **k: 0)
    plan = purge.plan_chat([{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}])
    purge.execute_chat(plan)
    res = purge.verify_chat(plan)
    assert res.tombstone_written == 0, "别的会话的 2 条不能算进本次"
    assert res.tombstone_ok is False
    assert "不可逆性未建立" in res.render()


def test_execute_chat_is_idempotent_and_tolerates_missing_export(tmp_path):
    """幂等 + 导出目录不存在都算成功（spec §5.7）。第二次要 0 条、不报错。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:
        store.upsert_batch(con, [_victim_row(1)])
    plan = purge.plan_chat([{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}])
    assert plan.entries[0].export_exists is False
    purge.execute_chat(plan)
    again = purge.execute_chat(plan)
    assert again[0].deleted["messages"] == 0
    assert again[0].tombstone == 0


def test_execute_chat_refuses_when_the_exclusion_list_is_unreadable(tmp_path, monkeypatch):
    """**fail-closed**（spec §4.2）：读不出排除清单就没法建立不可逆性，
    那么这次删除就只是「暂时看不见」——宁可什么都不做，也不做一个看起来成功的假删除。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:
        store.upsert_batch(con, [_victim_row(1)])
    monkeypatch.setattr(purge, "exclusions", _BrokenExclusions())
    with pytest.raises(errors.PurgeRefused):
        purge.execute_chat(purge.plan_chat(
            [{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}]))
    with store.open() as con:
        assert store.chat_row_counts(con, "a", VICTIM)["messages"] == 1, "一行都不许删"


def test_chat_purge_on_a_machine_with_a_vector_index(tmp_path):
    """**装了向量索引的机器上**（本机即是）走完整的 plan → execute → verify。

    这是「purge 的连接必须能碰向量表」落地的唯一直接证据：`store.connect()` 从不加载
    sqlite-vec，三处连接若都开裸连接，plan 的第一句 `chat_row_counts` 就抛 PurgeRefused
    ——而此前**没有任何测试**跑过「purge 在装了索引的机器上」这条路（Task 3 的修复只覆盖
    到 store 层，`tests/test_delete_chat.py` 两个用例走的正是裸连接会抛的那两支）。
    """
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:
        store.upsert_batch(con, [_victim_row(1), _victim_row(2), _keeper_row(1)])
        _install_vector_index(con)          # victim 2 条、keeper 1 条
    plan = purge.plan_chat([{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}])
    assert plan.entries[0].counts["vectors"] == 2, \
        "计划阶段就该数得到向量行——数不到说明这条连接不认识 vec_messages"
    purge.execute_chat(plan)
    with store.open() as con:
        _load_vec(con)
        # 判据是**全表分组**，不是「victim 那个键下还剩几条」：后者用的是被测代码那套派生
        # （`messages.chat_md5` ↔ 行里的 chat_md5 列），派生错时它会**跟着一起错**、
        # 报 0 而向量其实还在——本次删除最坏的那个形状。全表分组验的是「留下的只有 keeper」。
        left = dict(con.execute(
            "SELECT acct_chat, count(*) FROM vec_messages GROUP BY acct_chat"))
    assert left == {messages.acct_chat("a", KEEPER_MD5): 1}, f"victim 的向量必须真没了：{left}"
    assert purge.verify_chat(plan).leftover == ()


# ---------- 评审 fix 轮（F1..F7）----------

def test_plan_chat_purges_every_entry_not_just_the_first(tmp_path):
    """两条**不同会话**的 entry：第二条的导出目录也必须被删、被核（F1'）。

    早先那版把 `export_dir` 放在 `ChatPlan` 上、只取 `resolved[0]` 的值：第 2..n 条既不删
    也不核，核查还会对着残留报 ✅。导出物逐条挂在 entry 上之后自然正确——这条把它钉住。

    核查分两步断言（删之前 / 删之后）：只查第一条的实现会在**第一步**露馅（只报得出 d1），
    永远报 False 的实现在第二步露馅。
    """
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:
        store.upsert_batch(con, [_victim_row(1), _keeper_row(1)])
    d1 = home / "media" / VICTIM_MD5
    d2 = home / "media" / KEEPER_MD5
    for d in (d1, d2):
        d.mkdir(parents=True); (d / "chat.html").write_text("导出物")
    plan = purge.plan_chat([{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5},
                            {"account": "a", "chat_id": KEEPER, "chat_md5": KEEPER_MD5}])
    assert {e.export_dir for e in plan.entries} == {d1, d2}, "每个会话各算自己的目录"
    assert set(purge.verify_chat(plan).export_dirs) == {d1, d2}, "核查要看得见两个都还在"
    reports = purge.execute_chat(plan)
    assert [r.export_removed for r in reports] == [True, True]
    assert not d1.exists() and not d2.exists(), "第二条的目录也必须被删"
    assert purge.verify_chat(plan).export_still_there is False


def test_plan_chat_shares_one_export_dir_across_accounts(tmp_path):
    """跨账号：同一个会话在两个账号里都有 → 两条 entry **共享同一个** `media/<md5>/`
    （目录按会话 md5 分，与账号无关，spec §5.1 第 1 步）。

    于是删除必须**幂等**：第一条真把它删了，第二条看到的是「已经不在了」——那也算成功。
    非幂等的实现（目录不存在即报 False）会让一次正常的跨账号 purge 以「导出物没删掉」退 1。
    """
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:
        store.upsert_batch(con, [_victim_row(1), _row(VICTIM, VICTIM_MD5, 1, account="b")])
    media = home / "media" / VICTIM_MD5
    media.mkdir(parents=True); (media / "chat.html").write_text("导出物")
    plan = purge.plan_chat([{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5},
                            {"account": "b", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}])
    assert {e.export_dir for e in plan.entries} == {media}, "同一个会话，同一个目录"
    assert purge.verify_chat(plan).export_dirs == (media,), "共享的目录只报一次（去重）"
    reports = purge.execute_chat(plan)
    assert [r.export_removed for r in reports] == [True, True], \
        "第二条看到的是「已经不在了」——那也是成功（幂等）"
    assert not media.exists()
    res = purge.verify_chat(plan)
    assert res.export_still_there is False
    assert "导出目录仍在" not in res.render()


def test_plan_chat_does_not_report_a_dangling_export_symlink_as_removed(tmp_path):
    """`media/<md5>` 是**悬空符号链接**时：`rmtree` 跟随它、抛 `FileNotFoundError`，
    而链接本身原封不动（M-B）。

    于是「FileNotFoundError → 成功」这条幂等分支会报「已删除」，核查若用 `exists()`
    （问「有没有内容」，悬空链接答「没有」）也跟着说干净——**说删了、东西还在、核查说干净**，
    正是本项目最忌讳的那一类。判据要用 `lstat`（问「这个名字还在吗」）。
    """
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:
        store.upsert_batch(con, [_victim_row(1)])
    media = home / "media"; media.mkdir()
    link = media / VICTIM_MD5
    link.symlink_to(media / "早就没有了")
    plan = purge.plan_chat([{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}])
    report = purge.execute_chat(plan)
    assert report[0].export_removed is False, "链接还在，就不算删掉"
    assert os.path.lexists(link), "前提：rmtree 没能删掉这个悬空链接"
    res = purge.verify_chat(plan)
    assert res.export_still_there is True
    assert str(link) in res.render()


def test_execute_chat_allows_a_tree_internal_link(tmp_path):
    """树内链接**不拒**（判据的另一半，防的是「见到链接就拒」那种过度拒绝）。

    判据跟的是**删除原语**：`rmtree` 会把链接当普通条目 unlink、而它的目标在树内，
    随整树一并消失，不存在 false-clean。一律拒绝会让 `media/<md5>/` 里任何一个内部软链
    把整个会话变成删不掉的死结——`_escapes_home` 的 docstring 记过这条取舍（blob/snapshot
    式布局就是这种），`--all` 那条同款回归在 `test_execute_all_allows_intra_tree_symlink`。
    """
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:
        store.upsert_batch(con, [_victim_row(1)])
    d = home / "media" / VICTIM_MD5
    (d / "chat").mkdir(parents=True)
    (d / "chat" / "index.html").write_text("导出物")
    (d / "latest").symlink_to(d / "chat")            # 目标在树内
    plan = purge.plan_chat([{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}])
    assert purge._escaping_links(plan, home) == [], "树内目标不该被判成逃逸"
    report = purge.execute_chat(plan)
    assert report[0].export_removed is True
    assert not os.path.lexists(d) and not (d / "chat" / "index.html").exists()
    assert purge.verify_chat(plan).export_still_there is False


def test_execute_chat_contains_a_failing_export_recheck(tmp_path, monkeypatch):
    """re-check **自己抛的** OSError 必须被就地兜住（M-D）。

    **这是一条注入式判据，不是真 TOCTOU 夹具**：单线程里造不出「`rmtree` 先以 ENOENT 失败、
    紧接着父目录又不可搜索/不再是目录」那个窗口（把 `media` 换成普通文件的话，`rmtree` 先抛的
    是 `NotADirectoryError`，根本走不到这个 handler）。所以把 `name_there` 换成抛异常的桩。
    判据本身是**真**的：Python 里处理器内抛出的异常不走同一 try 的同级 handler，没有那层兜底
    它就会裸穿出去（删掉兜底那条 `except OSError` 即红），而那正是「core 层只抛项目异常」的
    契约——同一个 EACCES 从 `rmtree` 来是 `removed = False`、从 `lstat` 来却是裸异常，太不一致。
    """
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:
        store.upsert_batch(con, [_victim_row(1)])
    media = home / "media"; media.mkdir()
    link = media / VICTIM_MD5
    link.symlink_to(media / "早就没有了")   # 先让 rmtree 走 ENOENT 那一支
    # 前提断言（`test_plan_chat_does_not_report_a_dangling_export_symlink_as_removed` 同款）：
    # 链接没造出来时 rmtree 走的是另一支，这条测试就测不到它要测的那个 handler。
    assert os.path.lexists(link), "前提：悬空链接确实在盘上"
    plan = purge.plan_chat([{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}])

    def boom(_p):
        raise PermissionError(13, "Permission denied（桩）")

    monkeypatch.setattr(purge, "name_there", boom)
    report = purge.execute_chat(plan)
    assert report[0].export_removed is False, \
        "判不出来 = 没删掉：不许报成功，更不许把裸 OSError 抛出 execute_chat"


def test_plan_chat_turns_a_broken_index_into_purge_refused(tmp_path):
    """索引坏了/读不了时，裸 `sqlite3.DatabaseError` 不许逃到 CLI（F2）。

    `cli.py` 只 catch `AgentWeChatError`，裸异常 = 用户看到一整个 traceback。
    夹具就是「不是数据库的文件」——这也正好是 brief 原版 dry-run 测试的那个夹具，
    在这里它才是对的用法（那边它测的是 plan_chat 正常路径，于任何实现都红）。
    """
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 100)
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.plan_chat([{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}])
    assert "读取索引失败" in str(ei.value), "要点名是哪一步失败"
    assert isinstance(ei.value.__cause__, (sqlite3.Error, OSError)), "病因要留住"


def test_verify_chat_turns_a_broken_index_into_purge_refused(tmp_path):
    """同 F2：核查读不了库时必须**响亮报错**，不许静默返回「没残留」——
    那是拿未知状态冒充 ✅，正是这门命令最不该有的假象。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:
        store.upsert_batch(con, [_victim_row(1)])
    plan = purge.plan_chat([{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}])
    (home / "index.db").write_bytes(b"x" * 100)
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.verify_chat(plan)
    assert "核查失败" in str(ei.value)
    assert isinstance(ei.value.__cause__, (sqlite3.Error, OSError))


def test_execute_chat_turns_secure_delete_on_before_any_delete(tmp_path, monkeypatch):
    """`secure_delete` 必须在**任何 DELETE 之前**打开（spec §2.2b，F3 的尺寸无关判据）。

    判据取**删除时刻的连接状态**：删掉实现里那行 `PRAGMA` 就红（本环境默认是 0）。
    比字节判据稳：它不受 chunk 几何、活跃页、WAL 帧的影响。
    """
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:
        store.upsert_batch(con, [_victim_row(1), _victim_row(2)])
    seen = []
    real = store.delete_chat

    def spy(con, **kw):
        seen.append(con.execute("PRAGMA secure_delete").fetchone()[0])
        return real(con, **kw)

    monkeypatch.setattr(purge.store, "delete_chat", spy)
    purge.execute_chat(purge.plan_chat([{"account": "a", "chat_id": VICTIM,
                                         "chat_md5": VICTIM_MD5}]))
    assert seen == [1], f"删除那一刻 secure_delete={seen}（要的是 1）"


def test_execute_chat_deletes_the_text_from_the_file_family(tmp_path):
    """`secure_delete` 的字节侧护栏（spec §2.2b 实测二，F3）。

    **先守卫守卫**：调用前标记必须真能在文件族里搜到——否则「命中 0」对任何实现都成立，
    包括根本没删的那个。

    **它守卫的是「DELETE 是在这个 pragma 下跑的」，不是「字节不存在」**：本命令的保证边界是
    「无存活行 + msg_key 被挡」（见 `execute_chat` 的 docstring）。vec0 的 `preview` 装的是
    **被删行前一条消息的显示正文**（真内容），chunk 没整块清空时它在**活跃页**里留着
    （评审实测 1200 行形态下开了 pragma 也能搜到 1–3 次）。这里量的是 `messages.content`：
    关掉 pragma 时它在 freelist 页里留着（实测 190 次命中），开着 + 关连接（+ 一次显式
    checkpoint）之后是 0。
    """
    mark = "ZQXMARKERQXZ"
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:
        store.upsert_batch(con, [dict(_victim_row(1), content=mark),
                                 dict(_victim_row(2), content=f"{mark}2")])

    def hits() -> int:
        return sum(p.read_bytes().count(mark.encode()) for p in home.iterdir())

    assert hits() > 0, "守卫守卫：种下去的正文必须能在文件族里搜到"
    purge.execute_chat(purge.plan_chat([{"account": "a", "chat_id": VICTIM,
                                         "chat_md5": VICTIM_MD5}]))
    assert hits() == 0, "被删正文仍留在文件族里（含 -wal）——DELETE 不是在这个 pragma 下跑的？"


def test_execute_chat_truncates_the_wal_even_while_another_connection_is_open(tmp_path):
    """另一个进程（MCP server）把库开着时，**关连接那一下既不清 `-wal` 也不回填**：
    被删正文的旧页会原样留在文件族里，直到下一次 checkpoint。显式 `wal_checkpoint(TRUNCATE)`
    让「文件族里不留被删正文」从「碰巧没人开着库」变成有意行为（F3）。"""
    mark = "ZQXMARKERQXZ"
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:
        store.upsert_batch(con, [dict(_victim_row(1), content=mark),
                                 dict(_victim_row(2), content=f"{mark}2")])
    plan = purge.plan_chat([{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}])
    other = store.connect()                   # 别的进程：连接开着、不写
    try:
        purge.execute_chat(plan)
        hits = sum(p.read_bytes().count(mark.encode()) for p in home.iterdir())
        assert hits == 0, f"别的连接开着时被删正文仍留在文件族里（{hits} 次）"
    finally:
        other.close()


def test_execute_chat_refuses_when_a_fresh_index_is_locked(tmp_path):
    """**新库**形态：SCHEMA 还没建过时 `CREATE TABLE` 就要写锁，拒绝是从 fail-closed
    闸门里抛出来的——但真因是**索引库被别的进程占着**，与排除清单毫无关系（F5）。

    计划是手搭的：这条路上 `plan_chat` 自己就会先失败（它也要连库）。
    只报「排除清单读不出来」+ 一个 `exclusions.db` 可读性 hint，会把用户支去查一个
    没问题的文件——所以消息与 hint 必须覆盖两种原因。
    """
    home = tmp_path / "home"; home.mkdir(parents=True)
    holder = sqlite3.connect(home / "index.db")
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("CREATE TABLE seed(x)")        # 拿住写锁，但**不建** messages 那些表
    plan = purge.ChatPlan((purge.ChatEntry(
        "a", VICTIM, VICTIM_MD5, {"messages": 1}, 1,
        home / "media" / VICTIM_MD5, False, 0),))
    try:
        with pytest.raises(errors.PurgeRefused) as ei:
            purge.execute_chat(plan)
    finally:
        holder.rollback(); holder.close()
    msg, hint = str(ei.value), ei.value.hint or ""
    assert "database is locked" in msg, "真因要出现在消息里"
    assert "导出任务" in hint, "锁那条修法要在 hint 里"
    assert "exclusions.db" in hint, "排除清单那条修法也要在（两种原因都覆盖）"


def test_verify_chat_turns_an_unsearchable_media_parent_into_purge_refused(tmp_path):
    """`media/` 父目录不可搜索时，`Path.exists()` 抛 EACCES——而 **EACCES 不在 pathlib 的
    _IGNORED_ERRNOS 里**（3.12），所以它不会退化成 False，而是抛 `PermissionError`。
    核查里那几个 `exists()` 必须在 try 之内、并翻成 `PurgeRefused`（F2'）：否则用户看到的
    是一整个 traceback，而这门命令的全部价值就是「把『删干净了没有』说清楚」。
    """
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:
        store.upsert_batch(con, [_victim_row(1)])
    media = home / "media"
    (media / VICTIM_MD5).mkdir(parents=True)
    plan = purge.plan_chat([{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}])
    os.chmod(media, 0o000)                    # 可读可进入都没有：判不出来
    try:
        with pytest.raises(errors.PurgeRefused) as ei:
            purge.verify_chat(plan)
    finally:
        os.chmod(media, 0o700)                # 不还原的话 pytest 自己清不掉这棵树
    assert "核查失败" in str(ei.value)
    assert isinstance(ei.value.__cause__, OSError)


def test_verify_chat_derives_chat_md5_instead_of_trusting_the_plan(tmp_path):
    """核查的 `chat_md5` **就地派生**，不复用计划里传进来的那个（F7）。

    `_write_tombstone` 写的是 messages 行里的 `chat_md5` 列；核查按 `(account, chat_md5)`
    数墓碑。两边一旦不同源，核查会读 0 → 误报「不可逆性未建立」（真出问题时也可能被垫成 ✅）。
    判别形态用**退化会话名** `md5:<hex>`：它是 `chat_md5` 与 `md5_of` 唯一分道扬镳的地方，
    这里故意传 `md5_of` 那一支（CLI 若用错帮手就是这个值）。

    **`plan_chat` 侧的目录也走同一判据**（M-C）：`media/<md5>/` 必须由 `chat_id` 现推，
    退回「照抄调用方传的 `item["chat_md5"]`」时，路径会指向一个不存在的目录——
    真目录永远删不掉、核查看不见（这条断言就是那个回归的判据）。
    """
    chat = messages.degraded_chat_id("aaaa")
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:
        store.upsert_batch(con, [_row(chat, "aaaa", 1)])
    plan = purge.plan_chat([{"account": "a", "chat_id": chat,
                             "chat_md5": messages.md5_of(chat)}])
    assert plan.entries[0].export_dir == home / "media" / "aaaa", \
        "计划侧的目录也要派生（退化形态的 md5 就是那段 hex），不许用传进来的值"
    assert plan.entries[0].chat_md5 == "aaaa", "同理，计划里存的也必须是生效的那个"
    purge.execute_chat(plan)
    res = purge.verify_chat(plan)
    assert res.tombstone_written == 1, "墓碑就在那儿，只是键要用派生的那个"
    assert res.tombstone_ok is True


def test_media_rebuild_refs_cannot_revive_a_purged_chat(tmp_path, monkeypatch):
    """四条复活路径里的第三条**端到端**（spec §7）：删完之后跑 `media rebuild-refs`。

    它全量重扫**微信库**（事实三），被删的会话在那里一条不少——拦得住拦不住全看写入端。
    此前只有 store 层的拦截有测试（`test_msg_parts_and_media_refs_intercepts` 直接调
    `store.upsert_media_refs`）：那条钉的是「拦截函数本身对」，**驱动 usecase/CLI 那一层
    没有**——`media_rebuild_refs` 若哪天换成另一条写入路径（或忘了带 `account`），
    store 层那条照样绿，而已删消息的媒体引用会重新长回来：正文不会复活，但
    `count_media_refs` **刻意不 join** `messages`，于是核查里那句「0 行」不再成立。

    判别性：摘掉 `store.upsert_media_refs` 里的 `_filter_excluded`，下面两条断言即时红。
    """
    from agent_wechat import usecases

    ref = (f"a|{VICTIM_MD5}|message_0.db|1", "image", "f" * 32, None)
    with store.open() as con:
        store.upsert_batch(con, [_victim_row(1)])
        store.upsert_media_refs(con, [ref])
    with store.open() as con:                       # 前提：先有一条，否则「仍是 0」是空话
        assert store.chat_row_counts(con, "a", VICTIM)["refs"] == 1

    purge.execute_chat(purge.plan_chat(
        [{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}]))
    with store.open() as con:
        assert store.chat_row_counts(con, "a", VICTIM)["refs"] == 0, "前提：已被删干净"

    class _Reader:
        def iter_all(self, **_kw):
            yield [], [ref]        # 微信库里那条还在——这正是这条路要拦的东西

    monkeypatch.setattr(usecases, "ChatContext", lambda acct=None: type(
        "C", (), {"account": "a", "reader": _Reader()})())
    on_skipped: list[int] = []
    n = usecases.media_rebuild_refs("a", on_skipped=on_skipped.append)
    assert on_skipped == [1], "挡下了就要走「成功但有保留」那条出口"
    assert n == 0, "被墓碑挡下的行不许计入「已回填」的条数（计数契约）"
    with store.open() as con:
        assert store.chat_row_counts(con, "a", VICTIM)["refs"] == 0, \
            "media rebuild-refs 把已删会话的媒体引用写回来了"
        assert con.execute("SELECT count(*) FROM media_refs").fetchone()[0] == 0


def test_verify_counts_the_tombstone_by_prefix_not_the_derived_columns():
    """核查数墓碑走 `msg_key` 前缀（`exclusions.count_chat`），与删除端 / 撤销端**同源**——
    不押在 `account` / `chat_md5` 两个派生列的忠实上。

    那两列只在「逐字等于 `msg_key` 的前两段」时才与前缀等价，而 spec §4 只是**要求**它们忠实。
    核查端若另立一套判据，同一个会话就会在删除侧被判「不可逆性已建立」、在核查侧被判「未建立」。

    **判别性**：把计数改回 `account=? AND chat_md5=?`，下面那条**派生列写错**的墓碑就数不到
    —— `tombstone_written` 变 0、核查误报「不可逆性未建立」并退 1。
    """
    from agent_wechat import exclusions, store
    rows = [_victim_row(1)]
    with store.open() as con:
        store.upsert_batch(con, rows)
        # 墓碑的 msg_key 属于该会话，**两列故意写错**（模拟投影失真）。
        exclusions.add(con, [(rows[0]["msg_key"], "别的账号", "别的md5")])
    plan = purge.plan_chat([{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}])
    res = purge.verify_chat(plan)
    assert res.tombstone_written == 1, (
        f"msg_key 属于该会话，前缀就数得到它——派生列失真不该影响核查：{res}")
    assert res.tombstone_ok, "核查不该因为派生列失真而误报「不可逆性未建立」"


def test_undo_chat_derives_the_md5_instead_of_trusting_the_target():
    """`undo_chat` 的 `chat_md5` **就地派生**（`messages.chat_md5(t["chat_id"])`），不读入参
    里那一个——与 plan / delete / verify 同一口径。

    它是同族四个动作里唯一还信调用方的。今天生产调用方
    （`usecases.purge_chat_targets`）传的值是对的，所以这是**潜在**缺陷；但 `--undo` 是误删
    之后的救援路径，调用方哪天用 `md5_of` 解析退化会话名（`md5:<hex>`），传进来的 md5 就指向
    另一个前缀——撤销**静默不生效**（一条都不删，输出还报「已解除 0 条」），而这是最不该
    静默失败的那条路。

    判别性：入参里故意把 `chat_md5` 写成 `"0"*32`，撤销照样要把该会话的墓碑删净；
    实现换回 `t["chat_md5"]` 即红（返回的 `UndoEntry.chat_md5` 也会是那个错值——入口就是拿
    它印给用户的）。
    """
    from agent_wechat import exclusions, store
    rows = [_victim_row(n) for n in (1, 2)]
    with store.open() as con:
        store.upsert_batch(con, rows)
        exclusions.add(con, [(r["msg_key"], "a", VICTIM_MD5) for r in rows])
    wrong = "0" * 32
    per = purge.undo_chat([{"account": "a", "chat_id": VICTIM, "chat_md5": wrong}], apply=True)
    assert [e.chat_md5 for e in per] == [VICTIM_MD5], \
        f"返回的必须是派生值（入口拿它印给用户）：{per}"
    assert [e.count for e in per] == [2], per
    with exclusions.standalone() as con:
        assert exclusions.count(con) == 0, \
            "入参 md5 写错也要撤销得了——判据按 chat_id 派生，不押在调用方身上"

def test_write_tombstone_goes_through_the_owning_module(monkeypatch):
    """EA-075：墓碑的生产写入端必须在**拥有排除清单的模块**里（`exclusions.add_from_messages`）；
    `_write_tombstone` 只是它在 purge 事务里的调用点。把 SQL 抄回 purge，本用例必红。"""
    from agent_wechat import exclusions, purge

    calls = []

    def spy(con, account, chat_id):
        calls.append((account, chat_id))
        return 3

    monkeypatch.setattr(exclusions, "add_from_messages", spy)
    monkeypatch.setattr(exclusions, "add",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("测试播种接口不该被生产路径调用")))
    assert purge._write_tombstone(object(), "acct", "wxid_x") == 3
    assert calls == [("acct", "wxid_x")]


# ---------- 批 B7 · 销毁路径（T13） ----------

def test_plan_has_no_second_source_for_the_tree():
    """EA-031：`plan(home=…)` 与 `execute`/`verify` 操作的不是同一棵树——计划给 A 出的表，
    执行删的是 `paths.HOME_DIR`（`execute` 只吃 plan、树却写死取全局）。今天全仓零调用方，
    所以是埋着的雷。

    照同模块 `plan_chat` 的先例**删掉入参**（那处的 `home` 也是同样理由删的：「导出根的
    拥有者是 `media.export_root()`，本模块开篇那条禁令禁的正是这种第二份来源」）。
    判据是结构性的：签名里不许再有第二个决定「删哪棵树」的入口。
    """
    import inspect
    assert "home" not in inspect.signature(purge.plan).parameters
    assert "home" not in inspect.signature(purge.plan_chat).parameters


def test_execute_chat_refuses_when_home_is_root(monkeypatch):
    """EA-099：`_check_home_is_sane` 的调用点是 plan / execute / plan_chat——会话轴的**执行侧
    没有闸门**（`execute_chat` 自己不调它，而它删的是 `HOME_DIR/media/<md5>`）。CLI 路径上
    `_cmd_purge_chat` 先走 `plan_chat` 所以今天不可达，但「执行前必须过闸」这条不变量没有落
    在执行的那一侧——将来多一个入口（MCP、脚本）就会漏。

    照 `test_execute_refuses_when_home_is_root` 的写法手搭 `ChatPlan`：闸门必须是
    `execute_chat` 的**第一条语句**（在链接闸门与占用检查之前——顺序与 `execute()` 一致，
    否则坏树下 `_escaping_links` 读 `paths.HOME_DIR` 当基准，判据本身就失真）。
    """
    monkeypatch.setattr(paths, "HOME_DIR", pathlib.Path("/"))
    entry = purge.ChatEntry("a", VICTIM, VICTIM_MD5,
                            {"messages": 0, "vectors": 0, "parts": 0, "refs": 0,
                             "display_names": 0},
                            0, pathlib.Path("/tmp/does-not-matter"), False, 0)
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute_chat(purge.ChatPlan((entry,)))
    assert "文件系统根" in str(ei.value)


def test_execute_all_allows_intra_tree_file_symlink(tmp_path, monkeypatch):
    """EA-100：`--all` 下**文件型**产物的树内符号链接被过度拒绝。

    items 那一段的判据是 `kind == "file" or _escapes_home(...)`——文件型**无条件**拒绝，
    理由（`execute` 里那段注）是「文件型走 unlink，只删链接、目标必留存」。但 `--all` 下
    文件型 item **根本不走 unlink**，走的是整树 rmtree：目标在树内就会被一并删掉，不存在
    false-clean。M1 已经为同一形状的目录型（`tmp/` 自己是树内链接）改判过，这一半漏了。

    后果是 `purge --all` 被**永久挡住**（spec §9 否掉了 --force，用户只能回去手敲 rm -rf），
    而它什么都没保护到。反向仍是硬要求：目标逃出树外必须拒（见下一条同名反向用例）。
    """
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "passphrase.json").symlink_to(home / "real-keys.json")   # 树内目标
    (home / "real-keys.json").write_text("{}")
    monkeypatch.setattr(paths, "HOME_DIR", home)
    r = purge.execute(purge.plan(frozenset(), all_=True))
    assert r.failed == ()
    assert not home.exists(), "整棵树该被删掉（链接与目标都在树内）"


def test_execute_all_still_refuses_a_file_symlink_leaving_the_tree(tmp_path, monkeypatch):
    """EA-100 的反向守卫：改判之后**树外**目标仍须拒——那才是 `_escapes_home` 要防的
    false-clean（删掉链接、目标明文留在外置盘上，而核查报「已清空」）。"""
    real = tmp_path / "elsewhere"; real.mkdir()
    (real / "keys.json").write_text("secret")
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "passphrase.json").symlink_to(real / "keys.json")
    monkeypatch.setattr(paths, "HOME_DIR", home)
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute(purge.plan(frozenset(), all_=True))
    assert "符号链接" in str(ei.value)
    assert (real / "keys.json").read_text() == "secret"


def test_execute_refuses_a_hardlinked_artifact(tmp_path, monkeypatch):
    """EA-030：硬链接绕过**全部**链接闸门——闸门只问 `is_symlink()`，而 `unlink(2)` 删的是
    目录项：同 inode 的另一个名字完全不受影响。

    实测（审计原样复现）：`ln $OUT $H/index.db` 之后 `purge --index --yes` 报「✅ 计划内路径
    已清空」并退 0，而那 200 KB 明文在 `$OUT` 下原样可读。`st_nlink` 会诚实地告诉你「还有
    别的名字」，只是代码没问。

    **不承诺「字节级不存在」**（那是 `purge --index` 都做不到的，见 `execute_chat` 的诚实
    口径）：只拒绝「还有另一个名字在盘上」，并让用户自己处理。
    判据只能挂在**文件型**上——目录的 `st_nlink` 恒 > 1（等于 2 + 子目录数）。
    """
    home = tmp_path / "home"; home.mkdir(parents=True)
    outside = tmp_path / "keep-index.db"
    outside.write_bytes(b"P" * 4096)
    os.link(outside, home / "index.db")               # nlink=2，不是符号链接
    monkeypatch.setattr(paths, "HOME_DIR", home)
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute(purge.plan(frozenset({"index"})))
    assert "硬链接" in str(ei.value)
    assert (home / "index.db").exists() and outside.exists()      # 两个名字都在
    assert outside.read_bytes() == b"P" * 4096                    # 明文原样


def test_execute_allows_a_normal_artifact(tmp_path, monkeypatch):
    """EA-030 的反向守卫：`nlink == 1` 的正常产物**不得**被拒——否则 `--index` 在标准安装
    上永远跑不起来（实测本机 `~/.agent-wechat` 32,151 个文件 `nlink` 全为 1）。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "index.db").write_bytes(b"x" * 100)
    monkeypatch.setattr(paths, "HOME_DIR", home)
    r = purge.execute(purge.plan(frozenset({"index"})))
    assert r.failed == ()
    assert not (home / "index.db").exists()


def test_execute_all_refuses_a_hardlinked_file_in_the_tree(tmp_path, monkeypatch):
    """EA-030 的 `--all` 一侧：树内任意文件有第二个名字时同样拒绝——`--all` 宣称「整棵树
    没了」，而那个名字下的字节还在。判据挂在 `not is_dir` 上（目录的 nlink 恒 > 1）。"""
    outside = tmp_path / "keep.db"
    outside.write_bytes(b"P" * 128)
    home = tmp_path / "home" / "models"
    home.mkdir(parents=True)
    os.link(outside, home / "weight.bin")
    monkeypatch.setattr(paths, "HOME_DIR", tmp_path / "home")
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute(purge.plan(frozenset(), all_=True))
    assert "硬链接" in str(ei.value)
    assert outside.read_bytes() == b"P" * 128


def test_a_dangling_dir_symlink_is_reported_not_silently_counted_as_removed(tmp_path,
                                                                          monkeypatch):
    """EA-029：目录型产物是**悬空符号链接**时，报告说「已删除 0 项」并退 0，而同一份输出里
    核查印 ❌——「退出码」这条通道说的是「全部成功」。

    根因是判据分叉：`_remove()` 用 `Path.exists()`（**跟随**链接）问「有没有内容」，悬空
    链接答「没有」，于是它走进了「计划生成后已被删掉 → 算成功」那条幂等短路。模块自己在
    `verify` / `execute_chat` / CLI 三处早就收敛成 `name_there()`，`_remove` 是最后两处
    例外之一。

    修后行为：`name_there` 说「这个名字还在」→ 真去删 → `rmtree(悬空链接)` 抛
    `OSError('Cannot call rmtree on a symbolic link')` → 进 `failed` → CLI 退 1。
    契约（与条目验收一致）：**要么拒绝、要么 `failed` 非空**，且链接仍在盘上。
    """
    home = tmp_path / "home"; home.mkdir(parents=True)
    (home / "media").symlink_to("/nonexistent/target")
    monkeypatch.setattr(paths, "HOME_DIR", home)
    plan = purge.plan(frozenset({"media"}))
    try:
        r = purge.execute(plan)
    except errors.PurgeRefused:
        pass                                    # 「拒绝」也是可接受的一支
    else:
        assert r.removed == (), "一个字节都没删，却被算成已删除"
        assert r.failed, "什么都没删，failed 却是空的——退出码会报成功"
    assert (home / "media").is_symlink(), "链接本身没动过"


def test_execute_all_does_not_count_a_failed_path_as_removed(tmp_path, monkeypatch):
    """EA-098：`--all` 的「已删除 N 项」把仍在盘上的路径数进去——**同一个路径同时进
    `removed` 与 `failed`**。根因与 EA-029 同源：回看用的是 `exists()`（跟随链接），而
    悬空链接的 `exists()` 恒为 False。

    审计原样复现：`chmod 500`（可读可进入、不可写）的 HOME_DIR 里放一个悬空 `dangling.db`，
    `purge --all --yes` 印「已删除 1 项」而 wéi 一被删掉的是**树**、那个 db 还在，⛔ 与
    ⚠️ 也都在同一份输出里。

    判据（条目的验收）：`removed ∩ {failed 的路径} == ∅`，且 `removed` 里每个路径都真的不在。
    """
    home = tmp_path / "home"; home.mkdir()
    (home / "dangling.db").symlink_to("/nonexistent/target")
    (home / "keep.txt").write_text("x")
    home.chmod(0o500)                                  # 可读可进入、不可写
    monkeypatch.setattr(paths, "HOME_DIR", home)
    try:
        r = purge.execute(purge.plan(frozenset(), all_=True))
    finally:
        home.chmod(0o700)                              # 让 pytest 能清理 tmp_path
    failed_paths = {p for p, _ in r.failed}
    assert not (set(r.removed) & failed_paths), "同一路径既报「已删除」又报「未能删除」"
    assert all(not p.exists() for p in r.removed), "removed 里混进了仍在盘上的路径"
    assert purge.name_there(home / "dangling.db"), "悬空链接必须算「还在」"


def test_verify_reports_snapshots_from_tmutil(monkeypatch, tmp_path):
    """EA-032：`snapshots` 字段**零测试断言**（全仓只有 `tm_excluded` 的两条降级断言）。
    将来 `listlocalsnapshots` 的参数或解析被改动，测试侧完全无声。

    这条钉住的是**取数那条链路**：调用参数是 `tmutil listlocalsnapshots /`，且 stdout 原样
    进字段。**它不管「数得对不对」**——数数在渲染层的 `_snapshots_line` 里，由
    `tests/test_cli.py::test_render_residue_flags_an_included_home_and_local_snapshots`
    与 `::test_render_residue_keeps_the_undecidable_wording` 守住（复审 T13 指出这句
    docstring 原先自称的口径比断言宽，如实改窄）。
    """
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        out = ("Snapshots for disk /:\n"
               "com.apple.TimeMachine.2026-01-01-120000.local\n"
               "com.apple.TimeMachine.2026-01-02-120000.local\n")
        return type("R", (), {"stdout": out, "stderr": "", "returncode": 0})()

    monkeypatch.setattr(purge, "_run", fake_run)
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setattr(paths, "HOME_DIR", home)
    rep = purge.verify(purge.plan(frozenset({"keys"})))
    assert rep.snapshots.count("com.apple.TimeMachine") == 2
    assert ["tmutil", "listlocalsnapshots", "/"] in calls


def test_execute_chat_refuses_a_hardlinked_export(tmp_path):
    """EA-030 的**会话轴**那一半（复审 T13 的条件②）：`execute_chat` 只 rmtree
    `media/<md5>/`、`_escaping_links` 只看符号链接、`verify_chat` 只看目录名还在不在——
    **没有任何 nlink 判据**，于是 EA-030 那个假象在会话轴上原样成立：报告说
    `export_removed=True`、核查说不在、而明文在另一个名字下原样可读，退 0。

    本批把同一族判据补在产物轴上之后这个不对称才可见，故一并挂上（判据、消息形状与
    产物轴同源）。反向仍是硬要求：树内**没有**第二名字的导出物照常删得掉（下一条）。
    """
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:
        store.upsert_batch(con, [_victim_row(1)])
    d = home / "media" / VICTIM_MD5
    (d / "chat").mkdir(parents=True)
    (d / "chat" / "index.html").write_text("明文导出物")
    outside = tmp_path / "keep.html"
    os.link(d / "chat" / "index.html", outside)      # 同 inode 的第二个名字，在树外
    plan = purge.plan_chat([{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}])
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute_chat(plan)
    assert "硬链接" in str(ei.value)
    assert outside.read_text() == "明文导出物", "拒绝之后一个字节都不该少"
    assert (d / "chat" / "index.html").exists()


def test_execute_chat_deletes_an_export_without_second_names(tmp_path):
    """复审条件②的反向守卫：正常的导出物（每个名字 nlink==1）**不得**被新闸门拒——否则
    会话销毁在全量用户上直接不可用。"""
    home = tmp_path / "home"; home.mkdir(parents=True)
    with store.open() as con:
        store.upsert_batch(con, [_victim_row(1)])
    d = home / "media" / VICTIM_MD5
    (d / "chat").mkdir(parents=True)
    (d / "chat" / "index.html").write_text("导出物")
    plan = purge.plan_chat([{"account": "a", "chat_id": VICTIM, "chat_md5": VICTIM_MD5}])
    report = purge.execute_chat(plan)
    assert report[0].export_removed is True
    assert not os.path.lexists(d)


def test_the_refusal_message_survives_redaction_when_there_are_many_entries(tmp_path,
                                                                          monkeypatch):
    """复审 T13 的条件③：拒绝消息必须**带总数、只点第一处**——`errors.redact` 会把超过 300
    字符的消息从尾部剪掉（`execute_chat` 的逃逸链接那条早就为同一个原因这么写了，注释里
    记着实测）。修轮前实测：8 个硬链接就把「有几处」剪没了，用户只看到头两条路径。

    判据走**与 CLI 同一条通道**（`errors.render`，它内部就是 `redact(str(exc))`），断的是
    用户真正看到的那句话里还有没有总数。
    """
    outside = tmp_path / "keep.bin"
    outside.write_bytes(b"P" * 64)
    home = tmp_path / "home"; home.mkdir()
    for i in range(8):
        os.link(outside, home / f"f{i}.bin")
    monkeypatch.setattr(paths, "HOME_DIR", home)
    with pytest.raises(errors.PurgeRefused) as ei:
        purge.execute(purge.plan(frozenset(), all_=True))
    shown = errors.to_text(ei.value)
    assert "共 8 处" in shown, f"总数被 redact 剪掉了：{shown!r}"
    assert outside.read_bytes() == b"P" * 64


def test_escapes_home_refuses_everything_it_cannot_prove_is_inside(tmp_path):
    """B 阶段（T14）：`_escapes_home` 是**删除**边界的判据（`execute`/`undo_chat` 都靠它），
    而它的三个非平凡分支此前**零用例**——把 `return True`（树外）、`return False`（树内）
    或 `strict=False` 任一处改掉，`test_purge.py` 全绿。删错一个文件是不可逆的。

    四条各钉一头：
    ① 树内目标 → False；② 解析到树外 → True；③ 断链 → False（删链接不留东西）；
    ④ 符号链接环 → True（判不出来就拒绝，且不许裸异常穿出）。
    """
    home = tmp_path / "home"
    inside = home / "media"
    inside.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    in_tree = inside / "a.bin"
    in_tree.write_bytes(b"x")
    assert purge._escapes_home(in_tree, home) is False

    escape = inside / "escape"
    escape.symlink_to(outside)
    assert purge._escapes_home(escape, home) is True

    broken = inside / "broken"
    broken.symlink_to(tmp_path / "does-not-exist")
    assert purge._escapes_home(broken, home) is False

    loop = inside / "loop"
    loop.symlink_to(loop)
    assert purge._escapes_home(loop, home) is True

    # home 自己不存在时也不许裸异常（`strict=False` 那处）
    missing_home = tmp_path / "not-yet"
    assert purge._escapes_home(in_tree, missing_home) is True   # 树外 ⇒ 拒绝


def test_escapes_home_refuses_when_existence_cannot_be_probed(tmp_path, monkeypatch):
    """B 阶段（T14）：`_escapes_home` 的 `except OSError: return True`（探测存在性失败
    ⇒ 判为逃逸）没有用例——删掉或改成 False，删除边界会在「读不到」时放行。

    EACCES 是真实形态（`Path.exists()` 只吞 ENOENT/ENOTDIR/EBADF/ELOOP）。
    """
    link = tmp_path / "x"
    link.write_bytes(b"x")
    real = pathlib.Path.exists

    def boom(self):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(pathlib.Path, "exists", boom)
    try:
        assert purge._escapes_home(link, tmp_path) is True
    finally:
        monkeypatch.setattr(pathlib.Path, "exists", real)
