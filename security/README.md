# security/ —— 标识外流的出口拦截

政策见 `docs/SECURITY.md` §6；设计与理由见
`内部设计记录`。
**本文件只讲怎么做。**

## 它拦什么

三种形状：`wxid_[A-Za-z0-9_-]{4,}`、`[0-9]{6,}@chatroom`、`1[3-9][0-9]{9}`。

⚠️ 手机号那支**只在仓库内使用**。仓库外（`/tmp`、`~/.claude`、`~/.codex`）的复查只用前
两种形状——实测手机号在 `/private/tmp` 单目录就有 1000 个以上假阳性（别的项目构建树里的
UUID 段与数字常量），误报比到 10³:6，人眼看不见信号。
命中且**不在** `allowlist.txt` 里 → 拦下。

**它扫不到**：自定义微信号、群名、备注名、聊天正文片段、导出产物。
形状扫描有原理边界——能枚举的只有形状。

⚠️ **二进制 / 非 UTF-8 文件根本没有被检查**——这是与上面**不同**的一条边界：
`--all` 读不出 UTF-8 就整份跳过；`--staged` 对二进制文件一行都不扫（git 不为二进制给新增行），
只在 stderr 点名警告一句（「X 是二进制文件——它一行都没被检查」），**退出码不变**。
上面那句说的是「读了也未必看得懂」，这句说的是**压根没读**——别把它读成「二进制也扫过、干净」。

生成的依赖锁（`uv.lock`）**整份跳过**（`--all` 与 `--staged` 同一条规则）：里面是第三方包
元数据，实测单独贡献 47 处手机号形假阳性。

## 装钩子（每个 clone 都要装一次）

钩子**不入库**，所以换机器 / 换 clone 必须重装。**装到共享 hooks 目录**——
`git rev-parse --git-common-dir` 下的 `hooks/`。主检出与它的所有 worktree 共用同一个，
所以在哪个里装效果一样；而直接写 `.git/hooks/` 在 worktree 里会**失败**：worktree 的
`.git` 是一个**文件**，不是目录。

**在你想保护的那个检出里跑**。（若本计划是在 worktree 中执行的，安装与下面的三个探针
验收留到合回 main 之后从主检出做。）

```bash
H=$(cd "$(git rev-parse --git-common-dir)" && pwd)/hooks
mkdir -p "$H"

cat > "$H/pre-commit" <<'SH'
#!/bin/sh
# 标识外流拦截：扫暂存区新增行。装法与理由见 security/README.md。
set -u
root=$(git rev-parse --show-toplevel) || exit 1
for py in "$root/.venv/bin/python3" /usr/bin/python3; do
    [ -x "$py" ] && exec "$py" "$root/security/scan.py" --staged
done
echo "scan: 找不到可用的 Python 解释器（试过 .venv/bin/python3 与 /usr/bin/python3）" >&2
exit 2
SH

cat > "$H/commit-msg" <<'SH'
#!/bin/sh
# 标识外流拦截：扫提交信息。**2026-09-27 那次真标识就躺在提交信息里。**
set -u
root=$(git rev-parse --show-toplevel) || exit 1
for py in "$root/.venv/bin/python3" /usr/bin/python3; do
    if [ -x "$py" ]; then
        "$py" "$root/security/scan.py" --text "$1" || exit $?
        exit 0
    fi
done
echo "scan: 找不到可用的 Python 解释器（试过 .venv/bin/python3 与 /usr/bin/python3）" >&2
exit 2
SH

chmod +x "$H/pre-commit" "$H/commit-msg"
```

**`chmod +x` 不能省。** 没有执行位时 git 会**跳过**钩子——它**不会失败**、照常提交，
照常提交。这是本机制最隐蔽的失效方式。

## 退出码

| 码 | 含义 |
|---|---|
| `0` | 干净 |
| `1` | 有未登记的命中（**拦截**） |
| `2` | **扫描器自身出错**（找不到 git / 解释器 / 提交信息不是 UTF-8）——同样拦截 |

`2` 也拦，这是刻意的：**fail-closed**。放行等于「装了但从未生效」。

## 环境注意

- 钩子在**你的登录 shell** 里跑；git 可能是 GitHub Desktop 自带的 stripped 版。
  解释器按 `.venv/bin/python3` → `/usr/bin/python3` 依次找，都找不到就退出 2。
- GitHub Desktop **3.5.5 起**会执行 `pre-commit` 与 `commit-msg`（本机 3.6.6 ✓）。
  它**不拦截 `pre-push`**——不要依赖 pre-push 兜底。
- 逃生门是 `git commit --no-verify`。**用它之前先想清楚**。

## 加一条豁免

**不要手抄值**（手抄意味着你先把值读了出来）：

```bash
uv run python security/scan.py --values            # 找到 #指纹
uv run python security/scan.py --allow <指纹>      # 按指纹登记，全程不打印值
uv run python security/scan.py --verify-allowlist  # 确认没有死条目
```

`allowlist.txt` 里**只许放假值**。这一条没有任何自动化能替你守——
「拿清单里的值去真实数据里查」这类 oracle 是**必要不充分**的（群解散了就查不出来）。

## 定期复查（仓库外那一层）

扫描器**只管仓库内**。仓库外靠手工：

```bash
grep -rlIE 'wxid_[A-Za-z0-9_-]{4,}|[0-9]{6,}@chatroom' ~/.claude ~/.codex
```

（**刻意不含手机号那一支**：实测它在 `/private/tmp` 单目录就有 1000 个以上假阳性，落在别的
项目构建树的 UUID 段与数字常量中间；同一版本在 `~/.claude/plugins` 下 wxid/chatroom
形状为 **0**、命中**全部**是手机号夹具。误报比到 10³:6，人眼看不见信号。）

**只看命中的顶层位置有没有落在 `docs/SECURITY.md` §4 那个清单之外，不要照抄输出**——
实测它会列出上千条路径，而且会把第三方插件里手机号形状的夹具算进来。
