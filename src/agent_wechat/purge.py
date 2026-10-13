"""数据销毁：清单 → 计划 → 执行 → 核查。只做 CLI，不进 MCP。

设计取舍与实测依据见 内部设计记录。

两条不可违反的约定：
- 路径的唯一来源是**拥有它的模块**（keys.CACHE_FILE / store.db_paths() /
  decrypt.tmp_dir() / vector.MODEL_CACHE_DIR / tasks.TASKS_FILE / exclusions.path() /
  media.export_root() / paths.HOME_DIR）。
  本模块**不得**自己拼 "passphrase.json" 这类字面量——那是不随拥有者演进的第二份来源。
- 引用其它模块的常量一律走 `from . import keys` + `keys.CACHE_FILE`，
  绝不 `from .keys import CACHE_FILE`：后者在导入时绑定值，测试 monkeypatch 会失效。
"""
from __future__ import annotations

import contextlib
import glob
import os
import pathlib
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass

from . import (bootstrap, decrypt, errors, exclusions, keys, media, messages, moments, paths,
               store, tasks, vector)

_run = subprocess.run          # 模块级替换点（同 account._run）：测试注入桩


@dataclass(frozen=True)
class Artifact:
    """一个可被销毁的路径。每条一个对象，path 是单数——计划表逐行渲染与它一一对应。

    **没有人类可读的 label 字段，不要加回来**：它此前只服务于 `--all` 的 loose 行
    （「已知产物：向量模型缓存」），而那些行现在都进了 items 段，靠 scope + sensitive +
    rebuild 三列说话。最后一个读者消失之后，label 就成了没人核对、会静默漂移的第二份来源
    ——正是本模块开篇禁止的那件事，而它恰好是 media/ 与 moments/ 漏掉的那次事故的成因。

    `unrebuildable` 与上面那条告诫**同源、不冲突**：「删了就不可重建」这条事实的**结构化
    形式**在这里（出处即本表），`rebuild` 那段自由文本只管展示代价。判据不与文本重叠——
    计划表该不该为一项单列「不可重建：…」读的是这个布尔；拿 `rebuild` 的措辞或路径比对当
    判据，等于让渲染层自己给自己出题，文案改一个词就静默失效。第二件不可重建的产物出现时，
    它在这里标 True 即可，入口层不必再认一遍。
    """
    scope: str | None   # 具名范围的开关名；None = 有拥有者但没有开关，只随 --all 一起删
    path: pathlib.Path
    kind: str           # "file" | "dir"：决定 unlink 还是 rmtree，不靠调用方猜
    sensitive: bool
    rebuild: str        # 重建代价，写进计划表（**只负责展示**；判据见 unrebuildable）
    unrebuildable: bool = False   # 删了就回不来的产物；计划表据它单列一句（spec §4.1③）


def _holders(path: pathlib.Path) -> list[int]:
    """持有该文件的进程 pid 列表。

    为什么是这个机制而不是查 tasks 注册表：purge 是独立进程，而 tasks._ensure_loaded()
    是唯一读路径、载入时就把磁盘上的 running 一律改判为 failed（tasks.py:62-64），
    于是永远看不到正在跑的长任务——恰好放行它本该拦住的场景。持有句柄检测跨进程，
    且直接对应真正要防的那件事：unlink 一个仍被打开的文件会「成功但空间不释放」。

    已知限制（本轮明确不修）：`lsof <目录>` 列出的是**持有该目录本身**的进程，不是「开着
    目录里某个文件」的进程——实测同一个进程，`lsof -n -P d/open.db` 找得到，`lsof -n -P d`
    却返回 exit 1、无输出。于是 `purge --tmp` 探测不到正在 decrypt 的任务（它持有
    `tmp/awdec-*/plain.db`），照样会删掉那个目录。要覆盖得换 `lsof +D`：它逐个文件递归，
    在 --all 那种整树规模上代价不可接受。
    """
    if not path.exists():
        return []
    try:
        r = _run(["lsof", "-n", "-P", str(path)],
                 capture_output=True, text=True, timeout=15)
    except Exception:
        return []          # lsof 缺失/超时：不因探测失败而阻断销毁
    pids = []
    for line in (r.stdout or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            pids.append(int(parts[1]))
    return sorted(set(pids))


def known_artifacts() -> tuple[Artifact, ...]:
    """**全部已知产物**的清单。**调用时求值**——测试靠 monkeypatch 重定向路径。

    与 artifacts() 的分工是「有没有 CLI 开关」：这里含 scope=None 的那几条，它们只随
    `--all` 一起删，但计划表照样要印出敏感度与重建代价。

    **为什么合成一张表**：此前「有哪些产物」写在两处——artifacts() 三个具名范围，
    known_artifact_label() 补上 models/ 与 tasks.json。而本模块开篇那条约定恰恰禁止第二份
    来源：拥有者演进时，没被改到的那张表不会跟着演进（media/ 与 moments/ 就是这么漏掉的
    ——它们的拥有者 09-15/09-16 才落地，而这两张表都停在 09-13）。
    """
    out = [Artifact("keys", keys.CACHE_FILE, "file", True,
                    "重建需 sudo + 在微信内退出登录再重新登录")]
    out += [Artifact("index", p, "file", True, "重建 ≈2.5–3 小时（68 万向量）")
            for p in store.db_paths()]
    # 本表的顺序即计划表的行序，也即 CLI 那句「必须指定至少一个范围：…」里的开关序
    # （cli.py 从这张表演绎名单），三处同源，改这一处就够。
    out += [
        Artifact("media", media.export_root(), "dir", True,
                 "重新导出：需联网抓 CDN，图片与语音还要重解微信本体"),
        Artifact("moments", moments.export_root(), "dir", True, "重新导出：需联网抓 CDN"),
        Artifact("tmp", decrypt.tmp_dir(), "dir", True, "无"),
        Artifact(None, vector.MODEL_CACHE_DIR, "dir", False, "重新下载"),
        Artifact(None, tasks.TASKS_FILE, "file", False, "无"),
        # 唯一一个删了就不可重建的产物：它没了，被删的内容下次导出就全回来了（spec §4.1③）
        Artifact(None, exclusions.path(), "file", True,
                 "不可重建：它没了，被删内容下次导出会回来", unrebuildable=True),
    ]
    return tuple(out)


def artifacts() -> tuple[Artifact, ...]:
    """**具名范围**的清单：有 CLI 开关、能被单独选中的那些（`--keys` / `--index` /
    `--media` / `--moments` / `--tmp`）。名单与顺序都由 known_artifacts() 决定，
    CLI 的那句「必须指定至少一个范围：…」也从这里演绎，不另写一份。"""
    return tuple(a for a in known_artifacts() if a.scope is not None)


@dataclass(frozen=True)
class PlanItem:
    artifact: Artifact
    exists: bool
    size: int          # 字节；不存在时为 0
    is_symlink: bool


@dataclass(frozen=True)
class LooseEntry:
    """`--all` 时 HOME_DIR 顶层的散落条目。**带体积**，因为计划表要逐条印出来——
    spec §3 的 `--all` 样例表里 `models/` 有 95 MB、`tasks.json` 有 2 B，只有路径的
    loose 印不出那一列。"""
    path: pathlib.Path
    size: int


@dataclass(frozen=True)
class PurgePlan:
    all_: bool
    items: tuple[PlanItem, ...]        # 具名产物（--all 时亦为「已知产物」部分）
    loose: tuple[LooseEntry, ...]      # 仅 --all：HOME_DIR 顶层不属于任何具名产物的条目


def _item(a: Artifact) -> PlanItem:
    try:
        st = a.path.lstat()            # lstat 而非 stat：符号链接本身也要能看出来
    except (FileNotFoundError, NotADirectoryError):
        return PlanItem(a, False, 0, False)      # 真的不存在
    except OSError as exc:
        # 权限之类的失败不等于「不存在」——把它当不存在会让 dry-run 对着活文件印「（不存在）」，
        # 而这个命令的全部价值就在于让用户在动手前看清将发生什么。
        raise errors.PurgeRefused(f"无法读取待删路径：{a.path}",
                                  hint=f"{exc}；请先修正权限再重试") from exc
    is_link = a.path.is_symlink()
    # 目录量整树。此前目录一律由渲染器印「—」来回避这件事（inode 大小与整树体积差几个数量级），
    # 代价是**最大的一项看不到体积**——media/ 有 10 GB，印成「—」等于让用户在据以说 --yes 的
    # 那张表上看不见自己要删掉多少东西。而 execute 汇总行用的正是 _tree_size，两个数字本来就
    # 该是同一个（T4 已在 freed 上修过这个错，这是它最后一块残留）。
    # `not is_link` 的理由同 _loose_entry：跟随链接会把**目标**的整树算进来，而删除时 unlink
    # 的只是链接本身，目标是留存的。
    if a.kind == "dir" and not is_link:
        return PlanItem(a, True, _tree_size(a.path), False)
    return PlanItem(a, True, st.st_size, is_link)


def _loose_entry(p: pathlib.Path) -> LooseEntry:
    """散落条目的体积。**目录量整树**：`lstat().st_size` 是 inode 自身大小（几十到几百字节），
    会让 models/ 这种几百 MB 的目录印成「96 B」——T4 已在 freed 上修过同一个错误，
    这是它在计划表里的形态。量不出来就报 0：比报一个假数字好。

    `is_dir() and not is_symlink()` 而不是直接 `is_dir()`：前者跟随符号链接，指向目录的链接
    会被当成目录量整树，而 --all 下它只会被 rmtree 当作普通条目 unlink，目标留在盘上。
    """
    try:
        if p.is_dir() and not p.is_symlink():
            return LooseEntry(p, _tree_size(p))
        return LooseEntry(p, p.lstat().st_size)
    except OSError:
        return LooseEntry(p, 0)


def _home_of_uid(uid: int) -> pathlib.Path | None:
    """passwd 里该 uid 的家目录；查不到（容器里的临时 uid、非 POSIX）返回 None。"""
    try:
        import pwd
    except ImportError:
        return None
    try:
        return pathlib.Path(pwd.getpwuid(uid).pw_dir)
    except KeyError:
        return None


def _user_homes() -> tuple[pathlib.Path, ...]:
    """「这个人的主目录」的全部候选，**每个都已 resolve**。

    为什么要一组而不是一个：判据来自 `pathlib.Path.home()`（即 `$HOME`），于是换过 `HOME`
    的 shell 里这套判据跟着变——本项目为 bootstrap 文档化了 `sudo`，而 `sudo` 下
    `HOME=/var/root`，此时 `/Users/<你>` 能过闸，「少打 /.agent-wechat」的手滑拦不住。
    `SUDO_UID` 是 sudo 给出的**发起者** uid，拿它查 passwd 才还原得出那个人。

    为什么要 resolve：调用方拿 `home.resolve()` 的**产物**来比，而 `Path.home()` 是未解析的。
    macOS 上 `/tmp → /private/tmp` 恒成立，只解析一边会让两条判据**永远为假**——
    `_escapes_home` 里同一个错误已经修过（那里注释明写「两边都要解析过才能比」）。
    """
    raw = {pathlib.Path.home()}
    uids = [os.getuid()]
    sudo_uid = os.environ.get("SUDO_UID", "")
    if sudo_uid.isdigit():
        uids.append(int(sudo_uid))
    for uid in uids:
        h = _home_of_uid(uid)
        if h is not None:
            raw.add(h)
    out = []
    for h in raw:
        try:
            out.append(h.resolve(strict=False))
        except (OSError, RuntimeError):
            continue        # 单个候选解析不出来就跳过它，不让一个坏候选拖垮整道闸门
    return tuple(out)


def _check_home_is_sane(home: pathlib.Path) -> None:
    """拒绝看起来不像数据目录的 HOME_DIR。**这是一道数据丢失闸门，不是洁癖。**

    `paths.HOME_DIR` 来自 `$AGENT_WECHAT_HOME`，而**设了但为空**的环境变量会让
    `os.environ.get(...)` 返回 `""` 而不是默认值，`Path("")` 就是 `.`——于是
    `purge --all --yes` 会递归删掉**当前工作目录**（实测确认，且输出最后一行还打着 ✅）。
    该变量是文档化的一等开关（含 MCP 的 env），所以「shell 里没设上」是现实输入。

    「用户主目录」跑的是 `_user_homes()` 的**全部候选**（`$HOME` ∪ 本进程 uid 的家 ∪
    `$SUDO_UID` 的家），而不是单个 `Path.home()`：提权下 `$HOME` 变成 `/var/root`，
    只比一个的话 `/Users/<你>` 就过闸了。
    """
    if not home.is_absolute():
        # 先判这个再 resolve：`Path("")` 是相对路径，但它 resolve 之后就是 CWD 了，
        # 先 resolve 会让消息退化成「拒绝销毁用户主目录」——指不到真正的原因。
        raise errors.PurgeRefused(
            f"拒绝在相对路径上执行销毁：{home!r}",
            hint="AGENT_WECHAT_HOME 是否被设成了空值？空值会被解析成当前目录")
    try:
        resolved = home.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        # 符号链接环：3.12 的 Path.resolve 抛 RuntimeError（3.13 才改成 OSError）。
        # 不接住的话它会以裸异常穿透 CLI，**dry-run 也会崩**——而这个命令的全部价值就在于
        # 让用户在动手前看清将发生什么。判不出来就不删。
        raise errors.PurgeRefused(
            f"无法解析待销毁目录，拒绝执行销毁：{home}",
            hint=f"{exc}；请检查该路径是否为符号链接环") from exc
    if resolved == pathlib.Path("/"):
        raise errors.PurgeRefused(f"拒绝销毁文件系统根：{resolved}",
                                  hint="AGENT_WECHAT_HOME 指向了不该被销毁的目录")
    for h in _user_homes():
        if resolved == h:
            raise errors.PurgeRefused(f"拒绝销毁用户主目录：{resolved}",
                                      hint="AGENT_WECHAT_HOME 指向了不该被销毁的目录")
        if h.is_relative_to(resolved):
            raise errors.PurgeRefused(f"拒绝销毁用户主目录的祖先：{resolved}",
                                      hint="AGENT_WECHAT_HOME 指向了不该被销毁的目录")


def plan(scopes: frozenset[str], all_: bool = False,
         home: pathlib.Path | None = None) -> PurgePlan:
    """生成删除计划。**纯只读**：不创建、不删除、不写任何东西。

    `--all` 的 loose 是「枚举 HOME_DIR 顶层减去**全部已知产物**」（known_artifacts()），
    而不是减去具名范围：后者列不出 models/ 这类**没有开关**的产物，它们会同时出现在 items
    与 loose 里，计划表把同一个路径渲染两遍。
    """
    home = home or paths.HOME_DIR
    # 闸门在 plan 里而不是只在 execute 里：dry-run 的那张表就是用户据以说 --yes 的东西，
    # 计划表照常渲染完再由 execute 拒绝，等于默许了一张「将删除当前目录」的表。
    _check_home_is_sane(home)
    # 请求的范围必须真有产物认领。否则一次改名（或 CLI 开关与那张表各改一处）会让 purge 变成
    # **静默空操作**：dry-run 印一张空表、--yes 报「已删除 0 项，释放 0 B」并退 0——一门专门
    # 用来销毁数据的命令报成功却一个字节没删，是这个模块最不该产出的假象。
    named = {a.scope for a in artifacts()}
    unknown = sorted(str(s) for s in scopes if s not in named)
    if unknown:
        raise errors.PurgeRefused(
            f"未知的范围：{'、'.join(unknown)}",
            hint="CLI 的开关与 purge.artifacts() 的范围名漂移了（某一处改了名）")
    # spec §6 那一行：plan 阶段权限不足（目录不可读）→ PurgeRefused，**不是**「删除失败」，
    # 发生在任何 unlink 之前。此前这种情况会被 _item() 吞成「不存在」，于是 dry-run 对一堆
    # 活着的文件打「（不存在）」，而 --yes 会在 is_symlink() 上抛裸 PermissionError。
    # 用 os.access 取值而不是 try-stat：它对「不存在」返回 False（幂等路径要靠 exists 区分），
    # 而 exists() 在**父目录不可搜索**时会抛 EACCES 而非返回 False（3.12 的 _IGNORED_ERRNOS
    # 不含 EACCES），那种输入与「不可读」同归一类：判不出来就不删。
    try:
        unreadable = home.exists() and not os.access(home, os.R_OK | os.X_OK)
    except OSError:
        unreadable = True
    if unreadable:
        raise errors.PurgeRefused(f"目录不可读，拒绝生成销毁计划：{home}",
                                  hint="请先修正权限（需要读 + 进入权限），再重试")
    # --all 时 items 仍全部列出：计划表要显示 index/keys 的重建代价（spec §3 的 --all 表样）
    items = tuple(_item(a) for a in known_artifacts() if all_ or a.scope in scopes)
    loose: tuple[pathlib.Path, ...] = ()
    if all_:
        known = {a.path for a in known_artifacts()}
        try:
            top = sorted(home.iterdir())
        except OSError:
            # 承重分支，不是防御性填充：HOME_DIR 不存在时（全新安装，或刚跑过一次 --all
            # 之后）iterdir 会抛 FileNotFoundError，而 spec §5.1 第 5 步要求 --all 对此
            # 幂等。覆盖它的测试是 T4 的 test_execute_all_on_missing_home_is_idempotent，
            # 不是 T3 那两个（它们走 all_=False，根本不进这个分支）。
            top = []
        loose = tuple(_loose_entry(p) for p in top if p not in known)
    return PurgePlan(all_=all_, items=items, loose=loose)


@dataclass(frozen=True)
class PurgeReport:
    removed: tuple[pathlib.Path, ...]
    failed: tuple[tuple[pathlib.Path, str], ...]
    freed: int                      # 已删除项的字节数之和
    home_removed: bool


def _remove(item: PlanItem) -> bool:
    """删除单个产物，返回是否真的删掉了东西。kind 决定 unlink 还是 rmtree——调用方不猜。

    入口重查存在性：CLI 是「先 plan（默认 dry-run）→ 用户确认 → 再 execute」，
    两步之间产物可能已被删掉，spec §5.1 第 5 步要求这算**成功**而不是失败。
    """
    if not item.artifact.path.exists():
        return False
    if item.artifact.kind == "file":
        item.artifact.path.unlink(missing_ok=True)
    else:
        shutil.rmtree(item.artifact.path)
    return True


def _escapes_home(link: pathlib.Path, home: pathlib.Path) -> bool:
    """该符号链接**是否会在删树之后留下东西**，或**判不出来**——后者也返回 True。

    判据不是「目标在 home 之外」，而是「树删完之后它还会不会在」：目标在树内 → 会被一并
    删掉 → False；目标在树外且存在 → 留存 → True；解析不出来（符号链接环、EACCES 等）
    → 无法保证 → True（保守，与「判不出来就别删」一致）。

    判据跟着删除原语走，不是「见到链接就拒」：
      · **文件型**具名范围走 unlink → 只删链接，**目标必留存** → 无条件拒绝（见 execute 的 links）；
      · **目录型**具名范围（`tmp/`）的 item 路径与子树、以及 `--all` 的**整树扫描**走 rmtree →
        目标也在树内时会一并删除，不存在 false-clean，故这几条走本函数判定。
        （`--all` 的 **item 路径**不在此列：文件型的 item 在 execute 里被无条件拒绝，够不到本函数。）
    只有目标逃出树外时才真的对应 spec §5.1 第 2 步那句「其实还在外置盘上」。

    为什么不能一律拒绝：vector.py 用 huggingface_hub 下载模型，其 blob/snapshot 布局会在
    models/ 下建若干指向**树内** blobs/ 的符号链接（实测本机 ~/.agent-wechat 有 5 个，
    全部树内、树外 0 个）。一律拒绝会让 `purge --all` 在标准安装上永远跑不起来，
    而它什么都没保护到——spec §9 又否掉了 --force，用户只能回去手敲 rm -rf。
    """
    home = home.resolve(strict=False)   # 两边都要解析过才能比。macOS 上 /tmp → /private/tmp、
                                        # $TMPDIR 与 mktemp -d 给出的路径常带符号链接祖先；
                                        # 只解析 link 不解析 home，会把树内目标一律判成「树外」
                                        # 而整片拒绝——即整树扫描那条回归的残留形态。
    try:
        target = link.resolve(strict=False)
    except (OSError, RuntimeError):
        # 解析不出来就**判为逃逸并拒绝**：判不出来就无法保证目标会随树一起消失，对这种命令
        # 宁可拒绝。RuntimeError 是 3.12 对符号链接环抛的（3.13 才改成 OSError）；环本身不会
        # 留下东西，但它是病态配置，拒绝比放行更容易解释。不接住的话它会以裸异常穿透
        # execute()，违反「core 层只抛 AgentWeChatError 子类」。
        return True
    try:
        if not target.exists():
            return False      # 断链：目标本就不存在，删掉链接不留任何东西
    except OSError:
        # EACCES 等（pathlib 的 _IGNORED_ERRNOS 不含 EACCES）：目标可能真存在只是读不到。
        # 同上，判为逃逸。
        return True
    try:
        target.relative_to(home)
    except ValueError:
        return True           # 目标在 home 之外
    return False


def _tree_entries(root: pathlib.Path) -> list[pathlib.Path]:
    """整树的每个条目。指向目录的符号链接也要收——os.walk 把它们放进 dirnames
    且不跟随（followlinks 默认 False），只遍历 filenames 会漏掉它们。

    与 _tree_size 同样吞掉遍历中的 OSError：算不准的项跳过，不因一个不可读的子目录中断。
    """
    out: list[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            out.append(pathlib.Path(dirpath) / name)
    return out


def _tree_size(root: pathlib.Path) -> int:
    """整树的字节数（只读元数据）。供 --all 用：那条路径绕过 items 循环，没有现成体积可加。

    os.walk 默认吞掉遍历中的 OSError——与「部分失败继续」同一语义：算不准的项不计入，
    不因为一个不可读的子目录就让整次汇报失败。
    """
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def execute(plan: PurgePlan) -> PurgeReport:
    """执行删除。**部分失败不中止**：敏感度高的产物应当继续被清除（spec §5.1 第 4 步）。"""
    # 与 plan() 同一道闸门，且必须是第一条语句：CLI 是「先 plan → 再 execute」，plan 到
    # execute 之间 paths.HOME_DIR 可能被改指（或首次 plan 走的不是这条路径），到这里就
    # 只剩「已经开始删」了。
    _check_home_is_sane(paths.HOME_DIR)

    # 符号链接检查在 **execute 时刻**重算，不用 plan 时刻的 i.is_symlink：CLI 是
    # 「先 plan（dry-run）→ 用户确认 → 再 execute」，这个窗口内路径被换成链接就漏掉了。
    #
    # 为什么必须拒绝（实测于 Python 3.12.13 / macOS，三种情形）：
    #   rmtree(link, onexc=h)  → **不抛**，handler 收到 OSError('Cannot call rmtree on a
    #                            symbolic link')，链接与目标都完好；
    #   rmtree(link) 不传 handler → 抛 OSError；
    #   rmtree(整树) 树内含 symlink → **树被删掉，而链接的目标明文原封不动留在盘上**。
    # 第三种才是真正的危险：删除后核查会报「计划内路径已清空」，用户以为明文没了，
    # 其实还在外置盘上——正是 spec §5.1 第 2 步要防的 false-clean。
    #
    # 顺序：这一段在**占用检查之前**。两者都会拒绝，但这里的理由更具体、也更便宜；而且
    # 占用检查走 `lsof <path>`，**lsof 会解析符号链接**——产物是链接时它会报出一堆持有
    # **目标**的不相关进程（实测：拿 /etc/hosts 做目标，lsof 列出 7 个持有者），用户照着
    # 那条理由去杀进程也删不掉这个链接，真正的原因只是它是链接。
    try:
        links = [i.artifact.path for i in plan.items
                 if i.artifact.path.is_symlink()
                 and (i.artifact.kind == "file" or _escapes_home(i.artifact.path, paths.HOME_DIR))]
        # kind="dir" 的**具名**产物（tmp/）与 --all 走同一个原语 rmtree，故同样要扫它整棵树：
        # 树内链接会被 rmtree 当作普通条目 unlink、目标明文留存，而核查会报「已清空」——
        # 实测 `tmp/evil.db -> /outside/secret.db` + `purge --tmp --yes` 退 0、「已删除 1 项」，
        # 树外那 700 KB 一个字都没提。判据仍是 _escapes_home 而不是「见到链接就拒」：树内目标
        # 会被 rmtree 一并删掉，一律拒绝会让 blob/snapshot 式布局永远删不掉。
        for i in plan.items:
            if i.artifact.kind == "dir" and not i.artifact.path.is_symlink():
                links += [p for p in _tree_entries(i.artifact.path)
                          if p.is_symlink() and _escapes_home(p, paths.HOME_DIR)]
        if plan.all_:
            # spec §5.1 第 2 步要求对**每个待删路径**做 lstat。--all 的待删路径是**整棵树**
            # ——plan.loose 只有顶层，那是 §3 对「计划表逐条展示」的要求，与删除范围是两回事。
            # 树内任意深度的符号链接都会被 rmtree 当作普通条目 unlink、目标明文留存，而删除后
            # 核查会报「计划内路径已清空」——正是该步点名的「以为明文没了，其实还在外置盘上」。
            # 所以这里走一遍整棵树，而不是只看 plan.loose——但只拒绝**目标逃出树外**的链接
            # （树内目标会被 rmtree 一并删掉，不存在 false-clean；一律拒绝会让标准安装永远跑不起来）。
            links += [p for p in _tree_entries(paths.HOME_DIR)
                      if p.is_symlink() and _escapes_home(p, paths.HOME_DIR)]
            if paths.HOME_DIR.is_symlink():
                links.append(paths.HOME_DIR)
    except OSError as exc:
        # 这条 lstat 需要父目录的 +x。权限不足时它不是「没找到路径」而是**判不出来**，
        # 此前会以裸 PermissionError 穿透 execute（CLI 那边就是一个 traceback）。core 层
        # 一律抛项目异常，且判不出来就该拒绝——所以归到符号链接这道闸门的拒绝里。
        raise errors.PurgeRefused(
            f"无法判定待删路径是否为符号链接，拒绝删除：{exc}",
            hint="请先修正权限（父目录需要读 + 进入权限），再重试") from exc
    links = list(dict.fromkeys(links))     # 同一路径可能既在 items 又在树扫描里，去重
    if links:
        raise errors.PurgeRefused(
            "待删路径是符号链接，拒绝删除：" + "、".join(str(p) for p in links),
            hint="删链接不会删目标，会造成「已清干净」的假象；请手工处理")

    # --all 的删除面是**整棵树**，而 items 只是具名产物：loose（models/、tasks.json、散落明文）
    # 同样会被 rmtree 删掉，那里一个被写着的文件 unlink 会「成功但空间不释放」——正是这道检查
    # 存在的理由。此前只遍历 items，而错误文案（「有进程正在使用待删产物」）是个超集说法。
    # 残余：树**深处**被持有的文件仍探测不到，那需要 `lsof +D`；_holders 的 docstring 已论证
    # 它的整树代价不可接受。
    watched = [i.artifact.path for i in plan.items if i.exists]
    if plan.all_:
        watched += [e.path for e in plan.loose]
    held = {p: pids for p in watched if (pids := _holders(p))}
    if held:
        detail = "；".join(f"{p.name} 被 pid {','.join(map(str, v))} 持有"
                          for p, v in held.items())
        # 措辞覆盖全部具名范围：held 可能是 passphrase.json 或解密 tmp，不只是 index
        raise errors.PurgeRefused(f"有进程正在使用待删产物，拒绝删除：{detail}",
                                  hint="等该进程结束（如向量构建任务）后重试")

    removed: list[pathlib.Path] = []
    failed: list[tuple[pathlib.Path, str]] = []
    freed = 0

    # --all 时 items 只是给计划表看的（含重建代价），实际删除走下面的 rmtree 一次性完成，
    # 免得对同一棵树做两遍（unlink 一遍再 rmtree 一遍）。
    if not plan.all_:
        for item in plan.items:
            if not item.exists:
                continue
            try:
                # 目录的 lstat().st_size 是 inode 自身大小（几十到几百字节），不是整树体积。
                # 直接拿它相加会让 `purge --tmp`——解密明文的临时目录——无论删掉多少都报
                # 「释放 ~96 B」。与 --all 那条已修的同型：对销毁操作报一个假数字，且这处
                # 更隐蔽（看起来合理的 96 B，而非显眼的 0 B）。故目录先量整树，且必须在删之前量。
                size = _tree_size(item.artifact.path) if item.artifact.kind == "dir" else item.size
                if not _remove(item):
                    continue          # 计划生成后已被删掉：spec §5.1 第 5 步算成功，不计数
                removed.append(item.artifact.path)
                freed += size
            except OSError as exc:
                failed.append((item.artifact.path, str(exc)))

    home_removed = False
    if plan.all_:
        if paths.HOME_DIR.exists():
            # --all 不走上面的 items 循环，removed/freed 必须在这里补齐：否则 CLI 会在
            # 删光整棵 HOME_DIR 之后打印「已删除 0 项，释放 0 B」，与 spec §3/§6 要求的
            # 「删了什么 + 释放多少」直接冲突。体积要在 rmtree **之前**取。
            freed = _tree_size(paths.HOME_DIR)
            shown = ([i.artifact.path for i in plan.items if i.exists]
                     + [e.path for e in plan.loose])
            # onexc 收集而非抛出：与「部分失败继续删完」同一语义。
            # 用 onexc 不用 onerror——后者在 3.12 已弃用，本仓库 requires-python >=3.12。
            shutil.rmtree(paths.HOME_DIR,
                          onexc=lambda fn, p, exc: failed.append((pathlib.Path(p), str(exc))))
            if paths.HOME_DIR.exists():
                # 部分失败：把没删掉的那部分从 freed 里扣回去，否则报出的体积大于实际释放的
                freed -= _tree_size(paths.HOME_DIR)
            # 删完再回看谁真的没了：部分失败时不会把仍存在的条目虚报为已删
            removed = [p for p in shown if not p.exists()]
            home_removed = not paths.HOME_DIR.exists()
    return PurgeReport(tuple(removed), tuple(failed), freed, home_removed)


@dataclass(frozen=True)
class ResidueReport:
    not_removed: tuple[pathlib.Path, ...]   # 计划内、但仍存在的 → 真残留
    kept: tuple[pathlib.Path, ...]          # HOME_DIR 里计划外的 → 按范围保留 / 散落
    held: dict[str, list[int]]              # 仍持有 index 的进程
    tmp_leaks: tuple[str, ...]              # $TMPDIR 下的 awcap_*
    tm_excluded: str
    snapshots: str


def _tmutil(args: list[str]) -> str:
    """外部命令探测：带 timeout、异常降级为「无法判定」，绝不因探测失败而中断核查。"""
    try:
        r = _run(["tmutil", *args], capture_output=True, text=True, timeout=10)
    except Exception:
        return "无法判定（tmutil 不可用或超时）"
    if r.returncode != 0:
        # 非零退出此前被印成「无输出」——用户读到的意思是「就是没输出」而不是「判不出来」，
        # 而这两件事对一门专门用来抓假象的核查字段含义正相反。
        # 实测：正常用法恒 rc=0（[Included]/[Excluded] 都走 stdout，排没排除都是 0；
        # 用法错是 64），所以这个分支不会误伤正常输出。
        detail = (r.stderr or "").strip().splitlines()
        return f"无法判定（tmutil 退出码 {r.returncode}：{detail[0] if detail else '无 stderr'}）"
    return (r.stdout or "").strip() or "无输出"


def verify(plan: PurgePlan) -> ResidueReport:
    """删后核查。**必须接收 plan**：判据是「计划内的是否都没了、计划外的是什么」，
    不是「HOME_DIR 是否为空」——后者会把按范围故意保留的产物误报为残留。"""
    # 计划内 = 具名产物 ∪（--all 时的）loose 顶层条目。**两者都在删除范围内，故都要查存活**：
    # 只查 items 而把 loose 仅用于从 kept 里排除的话，一个在部分失败中活下来的 loose 条目
    # （models/、tasks.json、散落明文）**两个桶都不进**，报告会说「计划内路径已清空」——
    # 正是本字段存在的意义所在的那种假象（spec §5.2 第 1 项）。
    planned = [i.artifact.path for i in plan.items] + [e.path for e in plan.loose]
    # 判据收敛到 `name_there`（模块里只留一个存活判据）：它不跟随符号链接（断链也算残留
    # ——`exists()` 会把「链接还在」判成「已删除」，在这门专门用来抓假象的字段里制造假象），
    # 也不把 `OSError` 吞成 False（`os.path.lexists` 会：父目录不可搜索时它会对残留报
    # 「不在」，同样是假象，M-4）。它抛出的 `OSError` 就地翻成 `PurgeRefused`——「核查不了」
    # 与「核查通过」必须分开，core 层也不许裸穿（同 `verify_chat`）。
    try:
        not_removed = tuple(p for p in planned if name_there(p))
    except OSError as exc:
        raise errors.PurgeRefused(
            f"核查失败，本次结果不可信：{exc}",
            hint="请先修正权限（父目录需要读 + 进入权限），再重试") from exc

    planned_set = set(planned)
    try:
        top = sorted(paths.HOME_DIR.iterdir())
    except OSError:
        top = []
    kept = tuple(p for p in top if p not in planned_set)

    held = {i.artifact.path.name: pids
            for i in plan.items if i.exists and (pids := _holders(i.artifact.path))}

    # 目录用 tempfile 的口径（mkstemp 落在那儿；**不要**在这里复刻回落链——EA-101）
    tmp_leaks = tuple(sorted(glob.glob(os.path.join(
        tempfile.gettempdir(), bootstrap.TMP_PREFIX + "*"))))

    return ResidueReport(
        not_removed=not_removed, kept=kept, held=held, tmp_leaks=tmp_leaks,
        tm_excluded=_tmutil(["isexcluded", str(paths.HOME_DIR)]),
        snapshots=_tmutil(["listlocalsnapshots", "/"]),
    )


# ---------- 会话级销毁（`purge --chat`）：计划 → 执行 → 核查 ----------

def _root_cause(exc: BaseException) -> BaseException:
    """EA-074：`store.connect()` 已把建立段的 sqlite 错误翻成 `IndexUnavailable`——
    这里把**最深那层病因**取回来，让外层 `raise ... from` 的 `__cause__` 仍指向原始
    `sqlite3.Error` / `OSError`（调用方与测试都按那条契约看病因）。
    """
    cur, seen = exc, 0
    while isinstance(cur, errors.IndexUnavailable) and cur.__cause__ is not None and seen < 4:
        cur = cur.__cause__
        seen += 1
    return cur


@contextlib.contextmanager
def _open_index() -> Iterator[sqlite3.Connection]:
    """purge 的库连接：`store.open()` 的连接 + **尽力加载 sqlite-vec**。

    **为什么必须在这里收口**：`store.connect()` 从不加载扩展（加载是 `vector.connect()` 的
    职责，见 vector.py 的模块 docstring），而装了向量索引的机器上（本机即是）
    `chat_row_counts` / `delete_chat` **第一句**就碰 `vec_messages`——store 侧已把
    `no such module: vec0` 翻成 PurgeRefused（`store._vec_exec` 的契约），于是 plan /
    execute / verify 三处会全部死在第一句。把「purge 的连接必须能碰向量表」这条知识收在
    本函数一处，而不是散在三处 `with` 块里靠后来人记得。轻装安装上没有向量表，
    `vector.load` 返回 False 也无妨——`store._has_table` 自会跳过那几处。
    """
    with store.open() as con:
        vector.load(con)
        yield con


@dataclass(frozen=True)
class ChatEntry:
    """一个账号下、某个会话的待删账（导出物是**逐条**的）。

    跨账号 purge 时多条 entry 会指向**同一个** `media/<md5>/`：目录按会话 md5 分，
    与账号无关（spec §5.1 第 1 步）。所以删除/核查都得幂等，且不许只看第一条。
    """
    account: str
    chat_id: str
    chat_md5: str
    counts: dict[str, int]      # messages/vectors/parts/refs/display_names
    tombstone: int              # 将写入的排除条数（= counts["messages"]）
    export_dir: pathlib.Path
    export_exists: bool
    export_size: int


@dataclass(frozen=True)
class ChatPlan:
    entries: tuple[ChatEntry, ...]


@dataclass(frozen=True)
class ChatReport:
    account: str
    deleted: dict[str, int]
    tombstone: int
    export_removed: bool


@dataclass(frozen=True)
class ChatResidue:
    leftover: tuple[str, ...]        # 核查后仍在的表/路径
    tombstone_written: int
    tombstone_expected: int
    export_dirs: tuple[pathlib.Path, ...]   # 核查时**仍在**的导出目录（去重）；render() 要印它们

    @property
    def export_still_there(self) -> bool:
        """是否还有导出目录留着。**派生自 `export_dirs`**，不另存一个 bool：
        两个字段能各说各话，而这里说的正是「核查有没有看见残留」。"""
        return bool(self.export_dirs)

    @property
    def tombstone_ok(self) -> bool:
        return self.tombstone_written >= self.tombstone_expected

    def render(self) -> str:
        lines = []
        for x in self.leftover:
            lines.append(f"  ❌ 计划内仍未删除：{x}")
        for d in self.export_dirs:
            lines.append(f"  ❌ 导出目录仍在：{d}")
        if self.tombstone_ok:
            lines.append(f"  ✅ 排除清单：{self.tombstone_written} 条（不可逆性已建立）")
        else:
            lines.append(
                f"  ❌ 不可逆性未建立：排除清单只有 {self.tombstone_written} 条，"
                f"本次应写 {self.tombstone_expected} 条——**被删内容下次导出会回来**")
        return "\n".join(lines)


def name_there(p: pathlib.Path) -> bool:
    """**这个路径名还在吗**——不跟随符号链接（悬空链接也算在），判不出来就抛。

    与 `exists()` 的分工：`exists()` 问「有没有内容」，悬空链接答「没有」，于是「说删了、
    链接还在、核查说干净」这种假象就成立了；这里问的是「这个名字还在不在」。
    与 `os.path.lexists()` 的分工：那个把 OSError 吞成 False——父目录不可搜索（EACCES）
    时它会对残留报「不在」，同样是假象。走 `Path.lstat()`（EACCES 会抛，由调用方的
    try 翻成 PurgeRefused），只有真的没有这个名字才返回 False。

    **公开**（此前叫 `_name_there`）：入口层已经在消费它了——`purge --chat` 在索引库不在
    的那条路上要判导出物在不在，而它必须与核查、与删除用的是**同一个**存活判据（CLI 另写
    一套 `exists()` 就会把悬空链接读成「已删除」）。私有名是谎，改成公开。
    """
    try:
        p.lstat()
    except FileNotFoundError:
        return False
    return True


def chat_export_dir(chat_id: str) -> pathlib.Path:
    """该会话的导出物目录：`media.export_root() / md5(chat_id)`。

    **公开**：入口层也要走它。`purge --chat` 有一条**索引库不在**的路（`purge --index`
    之后），那条路上不能调 `plan_chat`——它开 `store.open()`，会把刚删掉的索引连 schema
    当场建回来；于是 CLI 此前自己拼了一遍同样的路径，`ChatEntry.export_dir` 成了同一件事
    的第二个说法。导出根的拥有者仍是 `media.export_root()`、会话 md5 的拥有者仍是
    `messages.chat_md5`，本函数只是把这两个**既有的**单一来源合成一处。
    """
    return media.export_root() / messages.chat_md5(chat_id)


def plan_chat(resolved: list[dict]) -> ChatPlan:
    """**纯只读**：只算将删什么，一个字节都不动。`resolved` 是
    `[{"account":…, "chat_id":…, "chat_md5":…}, …]`（解析由 cli 层做，见 Task 7）。

    **多条 entry 是常态，不是异常**：CLI 的 `--chat` 收的是一个**会话名**，而
    `usecases.purge_chat_targets` 把它解析成「每个索引里有它的账号各一条」——跨账号 purge
    天生就是多条。所以导出物**逐条挂在 entry 上**（dir / exists / size 各自算），
    execute 与 verify 各自处理自己的那一份。
    （早先那版把三个字段放在 `ChatPlan` 上、只取 `resolved[0]` 的值：第 2..n 条既不删也不核，
    核查还会对着残留报 ✅。）

    `chat_md5` **就地派生**（`messages.chat_md5(item["chat_id"])`），不用 `item["chat_md5"]`：
    与 `verify_chat` / `store.chat_row_counts` / `store.delete_chat` 同一口径。调用方哪天若
    用 `md5_of` 解析退化会话名（`md5:<hex>`），传进来的值会指向一个**不存在**的目录——
    真目录永远删不掉，而核查看不见（与 F7 同一模式）。

    `home` 参数**没有过调用方**（全仓 grep 为零），已删：导出根的拥有者是
    `media.export_root()`，本模块开篇那条「不得自己拼字面量」禁的正是这种第二份来源。
    """
    _check_home_is_sane(paths.HOME_DIR)
    try:
        entries = []
        for item in resolved:
            with _open_index() as con:
                counts = store.chat_row_counts(con, item["account"], item["chat_id"])
            md5 = messages.chat_md5(item["chat_id"])
            d = chat_export_dir(item["chat_id"])      # 与 CLI 那条路同一个出处（见本函数）
            exists = d.exists()
            entries.append(ChatEntry(
                item["account"], item["chat_id"], md5, counts, counts["messages"],
                d, exists, _tree_size(d) if exists and not d.is_symlink() else 0))
    except (sqlite3.Error, OSError, errors.IndexUnavailable) as exc:
        # 索引打不开/读不了（损坏、权限、被别的进程占着）时，裸 `sqlite3.DatabaseError` /
        # `sqlite3.OperationalError` 会一路逃到 CLI 的 `except AgentWeChatError` 之外——
        # 用户看到的是一整个 traceback 而不是一行拒绝。core 层只抛项目异常（errors.py 开篇
        # 契约），所以在模块边界翻，并点名**哪一步**失败。
        # EA-074 之后 `store.connect()` 会把建立段的 sqlite 错误先翻成 `IndexUnavailable`
        # ——那也是「读不了」，在同一处一并翻（`__cause__` 保持最深那层病因）。
        # `exists()` / `_tree_size()` 也在 try 里：`media/` 父目录不可搜索时 pathlib 抛 EACCES
        # （EACCES 不在它忽略的 errno 里），同样不许裸穿。
        raise errors.PurgeRefused(
            f"读取索引失败，未生成销毁计划：{exc}",
            hint="确认 index.db 可读、且没有别的进程正在写它（导出任务 / MCP server）；"
                 "库损坏（file is not a database 等）时先 purge --index 删掉再重跑 export"
        ) from _root_cause(exc)
    return ChatPlan(tuple(entries))


def _write_tombstone(con: sqlite3.Connection, account: str, chat_id: str) -> int:
    """把该会话当前**全部**消息写进排除清单，返回本次新写入的条数。

    **必须从同一个谓词派生**（`INSERT … SELECT`），不许「先在内存里读一个 msg_key 集合、
    再照着写」：后者在「读集合」与「写墓碑」之间留一个窗口，那期间写进来的新消息不在墓碑里、
    却会被随后的谓词 DELETE 一并删掉——**删了但没挡**，下次 `--full` 复活（spec §5.1）。

    **它被调用两次**（两段式，见 `execute_chat`）：① 独立事务里落一次（耐久），
    ② 删除事务里再落一次（盖住两次之间的窗口行）。`INSERT OR IGNORE` 让第二次是幂等的
    ——所以两次 `rowcount` 之和恰是**本次新增**的条数，重复的既不计入也不会真的写第二遍。

    单独成函数是为了**可注入**：`tests/test_purge.py` 的「墓碑没写成 → 核查必须报
    不可逆性未建立」那条测试要把它换成不写库的桩。

    EA-075：SQL 已归位到 `exclusions.add_from_messages`（生产写入端属于拥有排除清单的模块），
    本函数只是它在 purge 事务里的调用点。
    """
    return exclusions.add_from_messages(con, account, chat_id)


def _escaping_links(plan: ChatPlan, home: pathlib.Path) -> list[pathlib.Path]:
    """导出物里**删完之后还会留下东西**的链接（判不出来也算）——按目录去重。

    与 `execute()` 给目录型产物做的是**同一套判据**（`_tree_entries` + `_escapes_home`），
    只是根换成了每个 `ChatEntry.export_dir`。为什么必须有：`rmtree` 把树内链接当普通条目
    unlink、**目标明文原封不动**，于是运行报「导出物已删除」、核查也报干净——正是
    `execute()` 那段注释点名的 false-clean（终审 I-2）。

    根链接与树内链接**同判**（对齐 `execute()` 的目录型分支）：目标解析到 home 之外、或
    判不出来 → 拒绝；目标在树内 → 随 rmtree 一并删除，不拒——一律拒绝会让 blob/snapshot
    式的内部布局永远删不掉（完整论证见 `_escapes_home`）。
    """
    out: list[pathlib.Path] = []
    # 跨账号的多条 entry 共享同一个 `media/<md5>/`：同一棵树只扫一遍、同一个链接只报一次。
    for d in dict.fromkeys(e.export_dir for e in plan.entries):
        if d.is_symlink():
            # 根是链接时不再往下走：`os.walk` 会**跟随**它，列出来的全是目标的内部条目。
            if _escapes_home(d, home):
                out.append(d)
            continue
        out += [p for p in _tree_entries(d) if p.is_symlink() and _escapes_home(p, home)]
    return out


def _link_target(link: pathlib.Path) -> str:
    """链接指向哪里——**只用于错误消息**。读不出来就照实说，不猜。"""
    try:
        return os.readlink(link)
    except OSError as exc:
        return f"（读不出指向：{exc}）"


def execute_chat(plan: ChatPlan) -> tuple[ChatReport, ...]:
    """执行：**墓碑先落，然后才删**，两段式（spec §5.4）。

    顺序不能反：先删后挡的话崩在中间就是「删了但没挡」——那批东西会复活；
    先挡后删最坏只是「挡住了但没删掉」，核查看得见、重跑能收口（spec §5.1）。

    **保证的边界（诚实口径）**：本命令保证的是「索引里没有存活行 + 它们的 msg_key 进了排除
    清单」。**不是**「字节层面不存在」：`secure_delete` 只清 SQLite 自己释放的页，而 vec0 的
    `preview` 列装的是**被删行前一条消息的显示正文**（`vector._INSERT_VEC` 写入的 `preview` 列，真内容）——chunk 没整块
    清空时它仍留在**活跃页**里（评审实测 1200 行形态下，开了本 pragma 也还能在 `messages` 与
    `vec_messages_auxiliary` 的活跃页里搜到 1–3 次，dbstat 显示它们不在 freelist）。要字节级
    清除只能重建：`purge --index` 后重跑 export。
    """
    # 逃逸链接闸门：**必须在任何删除/提交之前**（与 `execute()` 同一位置——先扫链接，再做
    # 占用/fail-closed 那些检查）。它挡的不是「目标在树外」这件事本身，而是**删完之后目标还
    # 在、而报告与核查都说删干净了**那个 false-clean（终审 I-2）。具名范围对目录型产物是同一
    # 套判据：只拒绝目标逃出 HOME_DIR（或判不出来）的链接，树内目标随 rmtree 一并删除。
    links = _escaping_links(plan, paths.HOME_DIR)
    if links:
        # **总数放最前**：`errors.redact` 会把超过 300 字符的消息从尾部剪掉，而一对
        # 「绝对路径的链接 → 目标」就 ~150 字符起——全部列出来必被剪，用户连「有几处」
        # 都看不到（实测三个链接时消息断在第二处中间）。列第一处足够定位（它们同在该
        # 导出目录树内），其余按计数说明。
        raise errors.PurgeRefused(
            f"导出目录里有 {len(links)} 处链接指向数据目录之外，拒绝删除："
            f"{links[0]} → {_link_target(links[0])}"
            + (f"（另有 {len(links) - 1} 处，同在该目录树内）" if len(links) > 1 else ""),
            hint="删链接不会删目标，会造成「已清干净」的假象；请手工处理这些链接后重试")
    out = []
    # fail-closed（spec §4.2）：读不出排除清单就没法建立不可逆性，这一次删除就只是「暂时看不见」。
    # 宁可什么都不做，也不做一个看起来成功的假删除。
    try:
        with _open_index() as con:
            exclusions.count(con)
    except (sqlite3.Error, OSError, errors.IndexUnavailable) as exc:   # EA-074：connect 段已翻过一次
        # 这条闸门开的是**整条连接**（`store.connect()` 会 ATTACH 排除清单并跑 SCHEMA），
        # 所以异常有两个来源、修法完全不同，文案必须两个都点到。实测：**新库**形态下
        # `CREATE TABLE` 就要写锁，异常是从 connect 里抛的——跟排除清单一点关系都没有，
        # 只报「排除清单读不出来」会把用户支去查一个没问题的文件。
        raise errors.PurgeRefused(
            f"未做任何删除：排除清单或索引库不可用（{exc}）",
            hint="看括号里的真因：`database is locked` → 有别的进程正在写索引"
                 "（导出任务 / MCP server），等它结束后重试；"
                 f"否则检查 {exclusions.path()} 是否可读——它没了，被删内容下次导出就会回来"
        ) from _root_cause(exc)
    for e in plan.entries:
        try:
            with _open_index() as con:
                con.execute("PRAGMA secure_delete=ON")   # 必须在任何 DELETE 之前（spec §2.2b 实测）
                # ---- ① 墓碑先落，**独立提交一次** ----
                # 跨库事务在 WAL main 下**不保证崩溃中途的原子性**（spec §5.4 引 SQLite 官方原文：
                # 多库事务的原子性以「main 不是 WAL」为前提）。这一段事务只改 exclusions.db 一个
                # 文件，所以提交它没有跨库问题、崩溃也丢不了——而它要防的那一支是最坏的：
                # 删除落了、墓碑没落，被删内容的 msg_key 随行消失，墓碑再也补不回来。
                con.execute("BEGIN IMMEDIATE")
                written = _write_tombstone(con, e.account, e.chat_id)
                con.commit()
                # ---- ② 再派生一次 + 删除，同一事务 ----
                # 重派生是为了盖住「① 提交之后、② 删除之前别人写进来的行」：那些行会被下面的
                # 谓词 DELETE 删掉，必须同时被墓碑挡住。INSERT OR IGNORE，幂等。
                con.execute("BEGIN IMMEDIATE")
                written += _write_tombstone(con, e.account, e.chat_id)
                deleted = store.delete_chat(con, account=e.account, chat_id=e.chat_id)
                con.commit()
                # 删除提交后再显式 checkpoint 一次（**best-effort**，失败不管）：被删的正文
                # 是在这条连接上删的，`-wal` 里可能还留着含它的旧帧，本来要等下一次 checkpoint
                # （或碰巧关连接时）才落盘/截断。另一个进程（MCP server）持着库时这一步会失败
                # ——那种情况只能等它自己 checkpoint，所以忽略失败，不因此让命令报错。
                with contextlib.suppress(sqlite3.Error):
                    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except (sqlite3.Error, OSError, errors.IndexUnavailable) as exc:   # EA-074：connect 段已翻过一次
            # **这一层必须包住 `_open_index()` 本身**，不能只包 `BEGIN IMMEDIATE`——
            # `store.connect()` 每次都跑 `executescript(SCHEMA)`，而那是 DDL、要写锁。
            # 实测（本机 SQLite 3.50 / WAL，另一条连接持 `BEGIN IMMEDIATE` + 一次写）：
            #   · 表已齐全的库（跑过一次的机器、以及全部测试）→ connect 成功，
            #     异常从 phase ① 的 `BEGIN IMMEDIATE` 抛；
            #   · **新库**（SCHEMA 还没建过）→ `CREATE TABLE` 要写锁，异常就从 connect 里抛。
            # 两种形态都存在，所以包装范围不能跟着「哪一句抛」变：只包 BEGIN 的话，
            # 新库那条路上裸 `OperationalError` 会穿透出去，违反「core 层只抛项目异常」。
            # `OSError` 与 `sqlite3.Error` 并列接住：`store.connect()` 第一句就是
            # `paths.ensure_home_dirs()`（mkdir），它的失败以 `OSError` 冒出来——另三处
            # （fail-closed 闸门 / `plan_chat` / `verify_chat`）本来就是这对二元组（M-1）。
            # 回滚不用手写：`store.open()` 的 except 分支已经做了。
            #
            # 消息只说**实际发生的事**：标题是**操作**（不是「拿不到写锁」——库损坏、磁盘满
            # 都会落到这一支，真因在 `{exc}` 里），并带上本轮进度——不然「此前已完成的」
            # 会被说成没发生。两个数字数的是**项**（`plan.entries` 的条数 = 账号 × 会话），
            # **不是会话数**：跨账号 purge 里同一个会话是两项（每个有它的账号各一条）。
            raise errors.PurgeRefused(
                f"删除未执行：{e.account}/{e.chat_id} 未被删除"
                f"（本次计划 {len(plan.entries)} 项，此前已完成 {len(out)} 项）：{exc}",
                hint="`database is locked` → 有别的进程正在写索引（导出任务 / MCP server），"
                     "等它结束后重试；库损坏（file is not a database 等）→ 先 purge --index "
                     "删掉索引，再重跑 export") from exc
        # 删**这条 entry 自己的**导出物目录。**幂等**：目录不在了也算成功（沿用既有约定
        # 「计划生成后已被删掉」= 成功）——跨账号的两条 entry 共享同一个 `media/<md5>/`，
        # 第一条把它删掉之后，第二条看到的就是「不存在」，那必须报 True：
        # 否则一次正常的跨账号 purge 会以「导出物没删掉」退 1（假失败）。
        # 判据取**当下**的文件系统，不用计划时刻的 `e.export_exists`：plan → --yes 之间
        # 目录可能被删掉、也可能被重新导出出来，后者同样该删（本命令要的正是它消失）。
        try:
            shutil.rmtree(e.export_dir)
            removed = True
        except FileNotFoundError:
            # 「不在了」有两种：真的删掉了（幂等成功），以及 `media/<md5>` 是**悬空符号链接**
            # ——`rmtree` 会**跟随**它、报 FileNotFoundError，而链接本身原封不动。后者必须报
            # False：它是残留，报成功就是「说删了、东西还在」，随后核查还会跟着说干净。
            #
            # 这次 re-check **自己抛的 OSError 不会被下面那个兄弟处理器接住**：Python 里
            # 处理器内抛出的异常不走同一 try 的同级 handler。所以就地兜住、按同一口径记 False
            # ——否则父目录在这两步之间变得不可搜索（EACCES）或不再是目录（NotADirectoryError）
            # 时，同一个错误从 `rmtree` 来是 `removed = False`、从 `lstat` 来却是裸 OSError
            # 穿透出去（违反「core 层只抛项目异常」）。
            try:
                removed = not name_there(e.export_dir)
            except OSError:
                removed = False
        except OSError:
            removed = False          # 部分失败继续：按既有约定逐条报告、退 1
        # tombstone 报的是**本次真正新写进去的条数**（两阶段之和，见 _write_tombstone），
        # 既不是计划值、也不是「该会话当前被挡住的条数」：
        #  · 计划值会让「写入失败」照样报成功（测试用桩把写入变成 0 时，计划值仍是 N）；
        #  ·「当前被挡住的条数」在重跑时会把上一次写的**再报一遍**——那一次其实一条没写
        #    （`INSERT OR IGNORE` 全命中），而这门命令的幂等契约（spec §5.7）要求第二次报 0。
        #「当前被挡住的条数」是核查那一侧的事（`ChatResidue.tombstone_written`），
        # 它读的是排除清单的实际条数——核查才是证明不可逆性的那个字段。
        out.append(ChatReport(e.account, deleted, written, removed))
    return tuple(out)


def verify_chat(plan: ChatPlan) -> ChatResidue:
    """核查。**第二项比第一项要紧**：墓碑没写成功时文件系统上看不出任何异常，
    而下次 --full 会把它们全捞回来（spec §5.5）。

    `chat_md5` **就地派生**（`messages.chat_md5(e.chat_id)`），不复用计划里传进来的那个：
    墓碑行的 `chat_md5` 列来自 messages（`_write_tombstone` 的 `INSERT … SELECT`），两边必须
    同一口径才能对上——与 `store.chat_row_counts` / `store.delete_chat` 同一模式。退化会话名
    （`md5:<hex>`）正是两者分道扬镳的地方：调用方若拿了 `messages.md5_of` 那一支，传进来的
    值与墓碑对不上，核查会读 0 → 误报「不可逆性未建立」（真出问题时同样可能被垫成 ✅）。
    """
    try:
        leftover = []
        for e in plan.entries:
            with _open_index() as con:
                counts = store.chat_row_counts(con, e.account, e.chat_id)
            leftover += [f"{e.account}/{e.chat_id}:{k}={v}" for k, v in counts.items() if v]
        with _open_index() as con:
            # **按会话数，不是全局总数**：全局数会被早先别的会话的排除垫高，于是「本次一条没写成」
            # 也照样 ≥ 期望 → 核查报 ✅ 而不可逆性其实没建立。这条命令最不该有的假象。
            # 判据走 `exclusions.count_chat`（`msg_key` 前缀）——与删除端（`store.delete_chat`）、
            # 撤销端（`exclusions.remove_chat`）**同源**，不押在 `account` / `chat_md5` 两个派生列
            # 的忠实上（那两个列只有「逐字等于 `msg_key` 的前两段」时才与前缀等价，而 §4 只是
            # 要求它们忠实）。判据同源，两处就不可能对同一个会话给出不同的答案。
            written = sum(exclusions.count_chat(con, e.account, messages.chat_md5(e.chat_id))
                          for e in plan.entries)
        # 导出物**逐条**查，并去重（跨账号的两条 entry 共享同一个 `media/<md5>/`）：
        # 只查第一条会在多会话时漏报；只回一个 bool 则说不出「哪个还在」。
        # 存活判据是 `name_there`（**不跟随符号链接**）：悬空链接在这个字段里也算残留——
        # 这里问的是「这个名字还在吗」，不是「有没有内容」。
        # 它必须在 try 里：父目录不可搜索时 `lstat` 抛 EACCES（EACCES 不在 pathlib 忽略的
        # errno 里），那也要翻成 PurgeRefused，不许裸穿、更不许被吞成「不在」。
        surviving = tuple(dict.fromkeys(
            e.export_dir for e in plan.entries if name_there(e.export_dir)))
    except (sqlite3.Error, OSError, errors.IndexUnavailable) as exc:   # EA-074：connect 段已翻过一次
        # 同 plan_chat：读不了库时不许裸 sqlite3.Error 逃到 CLI。而且**「核查不了」必须与
        # 「核查通过」区分开**——静默返回一个空 leftover 就是对着未知状态报 ✅。
        raise errors.PurgeRefused(
            f"核查失败，本次结果不可信：{exc}",
            hint="确认 index.db / exclusions.db 可读且没有别的进程正在写索引"
                 "（导出任务 / MCP server）；库损坏时先 purge --index 删掉再重跑 export"
        ) from _root_cause(exc)
    return ChatResidue(tuple(leftover), written, sum(e.tombstone for e in plan.entries),
                       surviving)


@dataclass(frozen=True)
class UndoEntry:
    """撤销的一条目标：哪个账号、哪个会话、清单里有多少条待解除。"""
    account: str
    chat_md5: str
    count: int


def undo_chat(targets: list[dict], *, apply: bool = False) -> tuple[UndoEntry, ...]:
    """把该会话的墓碑从排除清单里移出。**只删墓碑，不恢复数据**（spec §5.6）。

    与 plan/execute/verify 同族，所以住在 core 而不是入口层：那三件事都由本模块拥有，
    撤销却整个住在 `cli.py` 里，别的入口就没法复用它——而 `--undo` 恰恰是「一次误删之后」
    的那条救援路径，不该只有 CLI 走得到。

    `apply=False` 只数、一个字都不改（投币前的预览）；`apply=True` 删净并提交。两者返回
    的都是**删之前**数出来的条数——入口要把它印给用户（`0 条` 也是一种要印出来的事实）。

    `chat_md5` **就地派生**（`messages.chat_md5(t["chat_id"])`），不用 `t["chat_md5"]`：
    与 `plan_chat` / `verify_chat` / `store.chat_row_counts` / `store.delete_chat` 同一口径。
    调用方哪天若用 `md5_of` 解析退化会话名（`md5:<hex>`），传进来的值指向另一个前缀——
    撤销会**静默不生效**（一条都不删，输出报「已解除 0 条」），而 `--undo` 恰恰是误删之后
    的救援路径，最不该静默失败（同 `plan_chat` 那段记的 F7 模式）。

    不写 `store.open()`：它的 `connect()` 会 `ensure_home_dirs()` + `executescript(SCHEMA)`，
    索引库不在时（`purge --index` 之后，而这正是撤销最可能被跑的时刻）会把刚删掉的
    `index.db` 连 schema 建回来——静默撤销那次索引 purge。走 `exclusions.standalone()`：
    `:memory:` + ATTACH，清单文件不存在时给一张**内存里的空表**，一个字节都不落盘。
    """
    try:
        with exclusions.standalone() as con:
            per = []
            for t in targets:
                md5 = messages.chat_md5(t["chat_id"])      # 就地派生，理由见 docstring
                per.append(UndoEntry(t["account"], md5,
                                     exclusions.count_chat(con, t["account"], md5)))
            if apply:
                for e in per:
                    exclusions.remove_chat(con, e.account, e.chat_md5)
                con.commit()      # remove_chat 不 commit（它的契约）：由调用方收口
    except (sqlite3.Error, OSError) as exc:
        # 读不出来就一行拒绝，不许裸异常穿到 CLI 的漏斗之外（core 层只抛项目异常）。
        # F10（T6 复审）：本路径走 `exclusions.standalone()`，**不经** `store.connect`——
        # 不存在可命中的 IndexUnavailable（EA-074 的翻译到不了这里），元组里不留死项。
        raise errors.PurgeRefused(
            f"撤销未执行：{exc}",
            hint=f"检查 {exclusions.path()} 是否可读、是否损坏"
        ) from _root_cause(exc)
    return tuple(per)
