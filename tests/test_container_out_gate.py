"""C-11：四个 `--out` 入口的容器路径判据（`SECURITY.md` §3 的只读宣称）。

**本文件绝不往微信容器里写任何东西。** 被拒的调用全部用 `paths.CONTAINER` 的**子路径
字符串**构造，而闸门在解析出产物根 / 翻开任何库之前就叫停——「写在拦之前」由这几条
断言证明（探针路径额外断言它**不存在**）。反向用例只在 `tmp_path` 里写。

四个入口的拒绝都走 CLI 层（退出码 1 + 一行中文，不是 traceback）：MCP 的
`export_chat_media` / `export_moments` / `export_chat` 走的是**同一批 `usecases` 函数**，
故 core 层的闸门一处覆盖两个适配层。
"""
import os
import pathlib

import pytest

from agent_wechat import cli, errors, paths, usecases
from moments_fixtures import ACCT, env  # noqa: F401  (EA-117 共享夹具)

XML = """<SnsDataItem><TimelineObject><id>P1</id><username>u</username>
<createTime>1735689600</createTime><contentDesc>正文</contentDesc>
<ContentObject><type>2</type><mediaList/></ContentObject></TimelineObject>
<LocalExtraInfo><nickname>老王</nickname></LocalExtraInfo></SnsDataItem>"""

# 一个**不存在**的容器子路径：任何一次被拒的调用都不许把它建出来。
PROBE = str(paths.CONTAINER / "xwechat_files" / "__c11_gate_probe__")


def _run_cli(*argv: str) -> int:
    with pytest.raises(SystemExit) as ei:
        cli.main(list(argv))
    return ei.value.code


# ---------------------------------------------------------------- 判据本身

def test_predicate_covers_container_and_children_only():
    """范围：`CONTAINER` 本身与其**子路径**为真；容器之外（含容器的**祖先**、以及从
    子路径用 `..` 爬出去）为假。容器里被判真的那两条都不被创建——判据是纯路径面的。"""
    child = paths.CONTAINER / "xwechat_files" / ACCT / "db_storage"
    assert paths.in_wechat_container(paths.CONTAINER) is True
    assert paths.in_wechat_container(child) is True
    assert paths.in_wechat_container(str(child)) is True              # str 与 Path 同判
    assert paths.in_wechat_container(child / "..") is True            # `..` 回到容器内 = 仍在
    assert paths.in_wechat_container(paths.CONTAINER / "..") is False  # 爬出容器
    assert paths.in_wechat_container(paths.CONTAINER.parent) is False  # 祖先不收
    assert paths.in_wechat_container(pathlib.Path("/tmp")) is False
    assert not pathlib.Path(PROBE).exists(), "判据本身不许建任何东西"


def test_predicate_resolves_relative_path_against_cwd_and_follows_symlinks(tmp_path, monkeypatch):
    """相对路径按**进程 CWD** 展开、符号链接跟随。

    一次测两条：CWD 换到 `tmp_path`，再走**相对**路径穿过一个指向容器的符号链接——
    「into/xwechat_files」只有在「按 CWD 解析 + 跟随软链」两件事都成立时才落在容器内。
    反向（`outside/sub`、裸相对路径 `elsewhere`）钉住「跟随软链」不是「一律为真」。
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "into").symlink_to(paths.CONTAINER)
    (tmp_path / "outside").symlink_to(tmp_path / "elsewhere")
    assert paths.in_wechat_container("into/xwechat_files") is True
    assert paths.in_wechat_container("into/xwechat_files/../xwechat_files") is True
    # `..` 是**符号链接感知**的（resolve 的 POSIX 语义：先跟随软链、再对**真实**父目录
    # 上溯）：`into/../` 是 CONTAINER 的真实父目录 —— 爬出容器，故为假。
    assert paths.in_wechat_container("into/../xwechat_files") is False
    assert paths.in_wechat_container("into/../elsewhere") is False
    assert paths.in_wechat_container("outside/sub") is False
    assert paths.in_wechat_container("elsewhere/x") is False


def test_predicate_case_variant_of_container():
    """macOS 默认 APFS **不区分大小写**：`~/library/containers/…` 与 `~/Library/Containers/…`
    实测是同一个 inode，而 `Path.resolve()` 不做大小写规范化 ⇒ 只比字符串的判据会被换一种
    拼写绕过。故判据另用 `samefile` 比既有祖先的 inode（只在真的指同一目录时为真）。

    区分大小写的卷上构造不出「同一 inode 的另一种拼写」：跳过（那时两种拼写本就不是同一个
    目录，判据也没有要堵的洞）。删掉 `in_wechat_container` 里那段 `samefile` 回落在本机必红。
    """
    variant = str(paths.CONTAINER).replace("/Library/", "/library/")
    if not os.path.exists(variant):
        pytest.skip("本卷区分大小写：无同 inode 的大小写变体可测")
    assert variant != str(paths.CONTAINER), "变体必须真的换了拼写（否则测的不是那条回落）"
    assert paths.in_wechat_container(variant + "/xwechat_files") is True


# ---------------------------------------------------------------- 四个入口（+ chat index）

def test_media_export_cli_refuses_container_out(capsys):
    """`media export --out`：终审实测能把明文媒体写进容器——现在在任何读取之前即拒。"""
    assert _run_cli("media", "export", "--chat", "谁", "--out", PROBE) == 1
    err = capsys.readouterr().err
    assert "微信容器目录" in err and str(paths.CONTAINER) in err
    assert "Traceback" not in err
    assert not pathlib.Path(PROBE).exists(), "被拒的调用不许在容器里留下任何东西"


def test_chat_export_cli_refuses_container_out(capsys):
    assert _run_cli("chat", "export", "--chat", "谁", "--out", PROBE) == 1
    assert "微信容器目录" in capsys.readouterr().err
    assert not pathlib.Path(PROBE).exists()


def test_chat_index_cli_refuses_container_out(capsys):
    """`chat index --out` 与 `chat export` 同根（SECURITY.md §1 同一行），一并覆盖。"""
    assert _run_cli("chat", "index", "--out", PROBE) == 1
    assert "微信容器目录" in capsys.readouterr().err
    assert not pathlib.Path(PROBE).exists()


def test_moments_export_cli_refuses_container_out(capsys):
    assert _run_cli("moments", "export", "--out", PROBE) == 1
    assert "微信容器目录" in capsys.readouterr().err
    assert not pathlib.Path(PROBE).exists()


def test_decrypt_cli_refuses_container_out(capsys):
    """整库明文（SECURITY.md §1 极高敏感）。它同时钉住「闸门先于账号解析与解密」：
    本用例没有任何账号/密钥夹具，跑得动就说明它在那两步之前退出。"""
    assert _run_cli("decrypt", "message_0.db", "--out", PROBE) == 1
    assert "微信容器目录" in capsys.readouterr().err
    assert not pathlib.Path(PROBE).exists()


def test_usecases_media_export_refuses_before_touching_anything():
    """core 层的闸门独立钉一条（MCP 的 `export_chat_media` 走的就是这里）：
    联系人不解析、索引库不打开——故 `contact` 可以是一个不存在的名字。"""
    with pytest.raises(errors.ContainerWriteRefused) as ei:
        usecases.media_export("不存在的人", kinds=["image"], out_dir=PROBE)
    assert isinstance(ei.value, errors.AgentWeChatError)     # 适配层一个 except 接得住
    assert not pathlib.Path(PROBE).exists()


# ---------------------------------------------------------------- 反向：闸门不过宽

def test_reverse_chat_index_in_plain_tmp_dir_is_not_refused(tmp_path, capsys):
    """普通目录仍然放行。空目录下 `chat index` 零依赖可跑（「还没导出过任何会话」那条路）。"""
    assert _run_cli("chat", "index", "--out", str(tmp_path / "out")) == 0
    assert "微信容器目录" not in capsys.readouterr().err


def test_reverse_moments_export_writes_into_plain_tmp_dir(env, tmp_path, capsys):
    """反向硬证据：普通目录里**真的写出产物**（`index.html` 落盘 + 目录 0700），
    而不是「只是没报错」。"""
    out = tmp_path / "export"
    assert _run_cli("moments", "export", "--account", ACCT, "--out", str(out)) == 0
    capsys.readouterr()
    assert (out / "index.html").exists()
    assert (out.stat().st_mode & 0o777) == 0o700
