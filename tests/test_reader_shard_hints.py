"""分片「没有更早消息」提示的跨进程缓存（`reader._empty_before` 落盘那一半）。

没有它，每条 CLI 命令（新进程）第一次大幅往前翻都要把比目标新的分片逐个打开一遍
（真机 6 片 × 约 28 ms）。全部用临时目录里的假分片，不碰微信目录。"""
import pathlib

from agent_wechat import paths, reader


def _shard(tmp_path: pathlib.Path, data: bytes = b"shard") -> pathlib.Path:
    p = tmp_path / "message" / "message_0.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _reload(monkeypatch):
    """模拟「换了个进程」：内存表清空、加载标志归零。"""
    reader._empty_before.clear()
    monkeypatch.setattr(reader, "_hints_loaded", False)
    reader._load_hints()


def test_hints_survive_a_new_process(monkeypatch, tmp_path):
    shard = _shard(tmp_path)
    key = (reader._file_sig(shard), "a" * 32)
    reader._empty_before[key] = 1700000000.0
    reader._save_hints()
    assert (paths.HOME_DIR / "shard_hints.json").is_file()

    _reload(monkeypatch)
    assert reader._empty_before == {key: 1700000000}


def test_a_changed_shard_invalidates_its_hints(monkeypatch, tmp_path):
    """微信写过这个分片（签名变）：旧提示不再可信，丢掉重扫。"""
    shard = _shard(tmp_path)
    reader._empty_before[(reader._file_sig(shard), "b" * 32)] = 17
    reader._save_hints()
    shard.write_bytes(b"shard longer now")

    _reload(monkeypatch)
    assert reader._empty_before == {}


def test_a_shard_that_vanished_invalidates_its_hints(monkeypatch, tmp_path):
    shard = _shard(tmp_path)
    reader._empty_before[(reader._file_sig(shard), "c" * 32)] = 17
    reader._save_hints()
    shard.unlink()

    _reload(monkeypatch)
    assert reader._empty_before == {}


def test_a_broken_hints_file_is_ignored(monkeypatch):
    paths.HOME_DIR.mkdir(parents=True, exist_ok=True)
    (paths.HOME_DIR / "shard_hints.json").write_text("{ not json", encoding="utf-8")
    _reload(monkeypatch)                       # 不抛
    assert reader._empty_before == {}


def test_a_shard_without_the_chat_is_persisted_as_null(monkeypatch, tmp_path):
    """`inf`（分片里根本没有这个会话的表）最值得记：下次连开都不用开。JSON 没有 Infinity，
    落盘用 `null`，读回来还是 inf。"""
    shard = _shard(tmp_path)
    key = (reader._file_sig(shard), "d" * 32)
    reader._empty_before[key] = float("inf")
    reader._save_hints()
    raw = (paths.HOME_DIR / "shard_hints.json").read_text(encoding="utf-8")
    assert "Infinity" not in raw and "null" in raw

    _reload(monkeypatch)
    assert reader._empty_before == {key: float("inf")}


def test_a_later_hint_wins_after_a_reload(monkeypatch, tmp_path):
    """跨进程合并：两份提示取更强的那个（「没有比更晚的时刻更早的消息」）。"""
    shard = _shard(tmp_path)
    sig = reader._file_sig(shard)
    reader._empty_before[(sig, "e" * 32)] = 200
    reader._save_hints()
    reader._empty_before.clear()
    monkeypatch.setattr(reader, "_hints_saved_at", None)      # 解掉 30 s 限流，再存一次
    reader._empty_before[(sig, "e" * 32)] = 100        # 更弱的提示不会盖掉强的
    reader._save_hints()
    _reload(monkeypatch)
    assert reader._empty_before[(sig, "e" * 32)] == 200
