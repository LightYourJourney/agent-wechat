"""C-11：`--out` 四个入口与 `AGENT_WECHAT_HOME` 的容器路径判据（`SECURITY.md` §3）。

**本文件绝不往微信容器里写任何东西。** 被拒的调用全部用容器可写范围（`CONTAINER_DATA`
及其祖先链）的**子路径字符串**构造，而闸门在解析出产物根 / 建任何目录 / 翻开任何库之前
就叫停——「写在拦之前」由这几条断言证明（探针路径额外断言它**不存在**）。反向用例只在
`tmp_path` 里写。

四个入口的拒绝都走 CLI 层（退出码 1 + 一行中文，不是 traceback）：MCP 的
`export_chat_media` / `export_moments` / `export_chat` 走的是**同一批 `usecases` 函数**，
故 core 层的闸门一处覆盖两个适配层。`AGENT_WECHAT_HOME` 的闸门长在
`paths.ensure_home_dirs`（所有写路径的收口处）。
"""
import os
import pathlib

import pytest

from agent_wechat import cli, errors, paths, tasks, usecases
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

def test_predicate_covers_container_data_and_its_ancestor_chain():
    """范围：`container_bundle()`（整个容器 bundle，含 `Data`）本身与其**子路径**，以及它的
    **全部祖先**一路到 `/`（含 `/` 本身）为真；非祖先的容器外路径为假。
    被判真的那几条都不被创建——判据是纯路径面的。"""
    child = paths.CONTAINER / "xwechat_files" / ACCT / "db_storage"
    assert paths.in_wechat_container(paths.CONTAINER) is True
    assert paths.in_wechat_container(child) is True
    assert paths.in_wechat_container(str(child)) is True              # str 与 Path 同判
    assert paths.in_wechat_container(child / "..") is True            # `..` 回到容器内 = 仍在
    # 闸门的根是**整个容器 bundle**：Data 与 Documents 都只是它的子目录
    assert paths.in_wechat_container(paths.CONTAINER_DATA) is True
    assert paths.in_wechat_container(paths.CONTAINER_DATA / "Library") is True
    # Data 的**兄弟目录**同样收——Phase 00 的单调性缺口：
    # `Data/..` 是 bundle（判真），而 `Data/..` 的子目录此前落到「祖先的子目录」判假。
    assert paths.in_wechat_container(paths.CONTAINER_DATA.parent / "x") is True
    assert paths.in_wechat_container(paths.CONTAINER_DATA / ".." / "x") is True
    # 祖先链：两级祖先 + 一路到 `/`（含 `/` 本身）全都收
    assert paths.in_wechat_container(paths.CONTAINER_DATA.parent) is True   # …/com.tencent.xinWeChat
    assert paths.in_wechat_container(paths.CONTAINER_DATA.parents[2]) is True  # ~/Library
    assert paths.in_wechat_container(paths.CONTAINER_DATA.parents[3]) is True  # ~
    assert paths.in_wechat_container(paths.CONTAINER_DATA.parents[4]) is True  # /Users（由 home 推导）
    assert paths.in_wechat_container(pathlib.Path("/")) is True
    # 不是祖先、也不在容器里的路径仍然放行
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
    # 上溯）：`into/../` 是 CONTAINER 的真实父目录（= Data），仍在闸门内。
    assert paths.in_wechat_container("into/../xwechat_files") is True
    assert paths.in_wechat_container("into/../elsewhere") is True
    # 祖先链一路收到 `/`：第 3、4、5 个 `..` 都仍在祖先链上
    assert paths.in_wechat_container("into/../../..") is True             # → ~/Library/Containers
    assert paths.in_wechat_container("into/../../../..") is True          # → ~/Library
    assert paths.in_wechat_container("into/../../../../..") is True       # → ~
    # 祖先的**同级**不是祖先（`~/Library/Containers/elsewhere` 里没有容器），放行
    assert paths.in_wechat_container("into/../../../elsewhere") is False
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
    # 祖先链的换拼写各钉一条：`~/Library` 本身（② 分支）与 Data 的一个祖先（① 分支）
    lib_variant = str(paths.CONTAINER_DATA.parents[2]).replace("/Library", "/library")
    assert paths.in_wechat_container(lib_variant) is True
    data_variant = str(paths.CONTAINER_DATA).replace("/Library/", "/library/")
    assert paths.in_wechat_container(data_variant + "/xwechat_files") is True


# ---------------------------------------------------------------- 四个入口（+ chat index）

def test_media_export_cli_refuses_container_out(capsys):
    """`media export --out`：终审实测能把明文媒体写进容器——现在在任何读取之前即拒。"""
    assert _run_cli("media", "export", "--chat", "谁", "--out", PROBE) == 1
    err = capsys.readouterr().err
    # 钉两样**不同**的东西：被拒范围（bundle 根）与用户给的落点（PROBE，在 Data 下）。
    # 只断言后者是空转——PROBE 自己就会被回显，把文案里的根删掉也照样绿（review 抓到的）。
    assert "微信容器目录" in err and str(paths.container_bundle()) in err
    assert str(PROBE) in err
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

def test_empty_out_is_not_gated_and_cwd_in_container_does_not_matter(monkeypatch, capsys):
    """`--out ""` 的语义是「没给」（落点=默认根）⇒ **不该**被闸门拒——判据与写侧同为**真值**判断。

    钉的是一个只在**两个条件同时成立**时才现形的差异：`--out ""` **且 CWD 恰在容器内**。
    用 `is not None` 时 `in_wechat_container("")` 判的是 CWD（resolve("") = CWD）= 容器 ⇒ 误拒；
    真值判断则跳过闸门、照常走默认根（`chat index` 在空产物根下零依赖返回）。
    本用例只 `chdir`（读操作，不写容器），且默认根在 `~/.agent-wechat`。
    """
    monkeypatch.chdir(paths.CONTAINER)      # 只切 CWD：没有对容器的任何写入
    assert _run_cli("chat", "index", "--out", "") == 0
    assert "微信容器目录" not in capsys.readouterr().err


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


# ---------------------------------------------------------------- AGENT_WECHAT_HOME

def test_home_dir_in_container_is_refused_before_creating(monkeypatch):
    """`AGENT_WECHAT_HOME` 指进 Data ⇒ `ensure_home_dirs` 在 mkdir 之前拒绝，
    容器里不留任何东西（判据是 `in_wechat_container`，与 `--out` 同一个）。"""
    home = paths.CONTAINER_DATA / "__c11_home_probe__"
    monkeypatch.setattr(paths, "HOME_DIR", home)
    with pytest.raises(errors.ContainerWriteRefused) as ei:
        paths.ensure_home_dirs()
    assert "AGENT_WECHAT_HOME" in (ei.value.hint or "")
    assert not home.exists(), "被拒的调用不许在容器里建出数据目录"


def test_home_dir_on_container_ancestor_is_refused(monkeypatch):
    """祖先链一路收到 `/`：`~/Library` 与 `/` 本身都被拒（同一条判据）。"""
    for home in (paths.CONTAINER_DATA.parents[2], pathlib.Path("/")):
        monkeypatch.setattr(paths, "HOME_DIR", home)
        with pytest.raises(errors.ContainerWriteRefused):
            paths.ensure_home_dirs()


def test_home_dir_outside_container_still_creates(monkeypatch, tmp_path):
    """反向：容器之外的 HOME_DIR 照常建目录（0700）——闸门不是「一律拒绝」。"""
    home = tmp_path / "home_outside"
    monkeypatch.setattr(paths, "HOME_DIR", home)
    paths.ensure_home_dirs()
    assert home.is_dir()
    assert (home.stat().st_mode & 0o777) == 0o700


def test_cli_refuses_home_in_container(monkeypatch, tmp_path, capsys):
    """CLI 层：HOME 在容器里时，`moments export` 在解析账号/读盘之前就以一行中文退出
    （错误文案带 AGENT_WECHAT_HOME 这个可执行的修复动作）。"""
    monkeypatch.setattr(paths, "HOME_DIR", paths.CONTAINER_DATA / "__c11_cli_home__")
    assert _run_cli("moments", "export", "--account", ACCT, "--out", str(tmp_path / "out")) == 1
    err = capsys.readouterr().err
    assert "微信容器目录" in err and "AGENT_WECHAT_HOME" in err
    assert "Traceback" not in err


def test_media_export_default_root_refuses_container_home(monkeypatch):
    """默认产物根（`HOME_DIR/media`）同样受闸：`media export` 不带 `--out` 时，
    `media.export_media` 在语音分支之前就先 `ensure_tree(export_root())`——闸必须在
    进入它之前叫停。整支复审实测过：不调 `ensure_home_dirs` 时 `--kinds image`
    能把图片写进容器的 `media/`。"""
    home = paths.CONTAINER_DATA / "__c11_media_home__"
    monkeypatch.setattr(paths, "HOME_DIR", home)
    with pytest.raises(errors.ContainerWriteRefused):
        usecases.media_export("不存在的人", kinds=["image"])
    assert not home.exists(), "被拒的调用不许建出 HOME，更不许写 media/"


def test_tasks_persist_refuses_container_home(monkeypatch):
    """`tasks._persist_locked` 的直写已收口到 `ensure_home_dirs`：HOME 在容器里时
    连终态落盘都不许发生（此前是一条绕过闸门的 `HOME_DIR.mkdir`）。"""
    home = paths.CONTAINER_DATA / "__c11_tasks_home__"
    monkeypatch.setattr(paths, "HOME_DIR", home)
    with pytest.raises(errors.ContainerWriteRefused):
        tasks._persist_locked({})
    assert not home.exists()
