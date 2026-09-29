"""数据销毁：清单 → 计划 → 执行 → 核查。只做 CLI，不进 MCP。

设计取舍与实测依据见 内部设计记录。

两条不可违反的约定：
- 路径的唯一来源是**拥有它的模块**（keys.CACHE_FILE / store.db_paths() /
  decrypt.tmp_dir() / vector.MODEL_CACHE_DIR / tasks.TASKS_FILE / exclusions.path() /
  paths.HOME_DIR）。
  本模块**不得**自己拼 "passphrase.json" 这类字面量——那是不随拥有者演进的第二份来源。
- 引用其它模块的常量一律走 `from . import keys` + `keys.CACHE_FILE`，
  绝不 `from .keys import CACHE_FILE`：后者在导入时绑定值，测试 monkeypatch 会失效。
"""
from __future__ import annotations

import glob
import os
import pathlib
import shutil
import subprocess
from dataclasses import dataclass

from . import decrypt, errors, exclusions, keys, media, moments, paths, store, tasks, vector

_run = subprocess.run          # 模块级替换点（同 account._run）：测试注入桩


@dataclass(frozen=True)
class Artifact:
    """一个可被销毁的路径。每条一个对象，path 是单数——计划表逐行渲染与它一一对应。

    **没有人类可读的 label 字段，不要加回来**：它此前只服务于 `--all` 的 loose 行
    （「已知产物：向量模型缓存」），而那些行现在都进了 items 段，靠 scope + sensitive +
    rebuild 三列说话。最后一个读者消失之后，label 就成了没人核对、会静默漂移的第二份来源
    ——正是本模块开篇禁止的那件事，而它恰好是 media/ 与 moments/ 漏掉的那次事故的成因。
    """
    scope: str | None   # 具名范围的开关名；None = 有拥有者但没有开关，只随 --all 一起删
    path: pathlib.Path
    kind: str           # "file" | "dir"：决定 unlink 还是 rmtree，不靠调用方猜
    sensitive: bool
    rebuild: str        # 重建代价，写进计划表


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
                 "不可重建：它没了，被删内容下次导出会回来"),
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
    scopes: frozenset[str]
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
    return PurgePlan(scopes=scopes, all_=all_, items=items, loose=loose)


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
    # lexists 而非 exists：exists 跟随符号链接，断链会被判成「已删除」而链接文件明明还在——
    # 在这门专门用来抓假象的字段里制造假象。
    not_removed = tuple(p for p in planned if os.path.lexists(p))

    planned_set = set(planned)
    try:
        top = sorted(paths.HOME_DIR.iterdir())
    except OSError:
        top = []
    kept = tuple(p for p in top if p not in planned_set)

    held = {i.artifact.path.name: pids
            for i in plan.items if i.exists and (pids := _holders(i.artifact.path))}

    tmpdir = os.environ.get("TMPDIR", "/tmp")
    tmp_leaks = tuple(sorted(glob.glob(os.path.join(tmpdir, "awcap_*"))))

    return ResidueReport(
        not_removed=not_removed, kept=kept, held=held, tmp_leaks=tmp_leaks,
        tm_excluded=_tmutil(["isexcluded", str(paths.HOME_DIR)]),
        snapshots=_tmutil(["listlocalsnapshots", "/"]),
    )
