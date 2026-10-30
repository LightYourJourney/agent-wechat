#!/usr/bin/env python3
"""vulture 候选门禁（spec §7②③）。**只用标准库**，退出码 0 / 1 / 2。

  0 = 候选集不比基线多
  1 = 出现基线外的候选（逐条点名）
  2 = **这一步没跑起来**（vulture 缺失或异常退出）

1 与 2 分开，因为「有新增死代码」与「这步没跑」要人做的事不同（删代码 vs 修环境）。
混成一个码，报告里就分不出那次红是哪一种。

**为什么是脚本、而不是 check.sh 里的一段 shell（2026-10-14）**：这条判据一天里错了两次，
两次都只在「工具在场/缺席」变化时现形——工具正常时的输出与「这一步根本没跑」在屏幕上
长得一模一样。

  ① 取不到 vulture 时输出为空，旧判据当成「没有候选」，印了一个 ✅ 并真放行过一次 push；
  ② 修完 ① 之后，又把 vulture 的 **rc=3（＝找到死代码，它的正常语义）**当成「工具坏了」
     ——本仓基线里就存着候选，rc=3 是这一刻的常态，于是门禁在工具完全正常时恒红。

两次都不是引号或 `comm` 的用法问题，是**判据本身**没人验；而写在 shell 里就没法被 pytest
钉住。所以按 ①（`check_import_cycles.py`）的规格做成一步：判据是函数，`tests/test_vulture_gate.py`
直接调它，真路径用例真的去跑 vulture。

**调用口径**：`<当前解释器> -m vulture src/ --min-confidence 60`。用 `sys.executable` 而不是
PATH 上的 `vulture` 或 `uvx`：门禁由哪个解释器跑，就用哪个解释器里的那把尺——不依赖 PATH
（钩子里的 PATH 与登录 shell 不同）、不依赖网络、不依赖 uv 缓存。vulture 因此是 dev 依赖
（spec §7⑤ 要求「需入库依赖的候选在停点材料里标注」）。

**比较口径**（与 `security/gate-baseline.txt` 的生成口径必须一致）：
  · **行号不进身份**：`file:12: 消息` 归一成 `file:: 消息`。条目的身份是 (文件, 类别, 符号)，
    行号只是它此刻坐在哪；拿行号当身份的话，在前文加一行注释就会让整份基线失效，
    而那种门禁的下场是被绕过。
  · 归一化后**去重**：同一个 (文件, 类别, 符号) 在同一文件里出现两次（实测 `media.py` 的
    `text_factory` 就在 1162/1490 两处）只算一条。不去重的话重复项会被集合差当成「新增」，
    门禁为一个幻影变红——而假红比不红更快被绕开。
"""
from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
BASELINE = ROOT / "security" / "gate-baseline.txt"
MIN_CONFIDENCE = 60

# vulture 自己的退出码契约（它 README 的 "Exit codes" 表）：
#   0 = 没找到死代码 / 1 = 输入有问题（文件缺失、语法错、编码错）/ 2 = 命令行参数错 /
#   3 = **找到死代码**
# 只有 0 与 3 是「它真的跑完了」。3 尤其不能当失败——本仓基线里存着候选，它是常态。
_OK_CODES = (0, 3)
_LINENO = re.compile(r":\d+:")
_ROOT_PREFIX = str(ROOT) + "/"


def normalize(line: str) -> str:
    """把一条输出行归一成它的**身份**：`(文件, 类别, 符号)`。

    两样东西都不进身份：

      · **行号**（`file:12: 消息` → `file:: 消息`）——行号只是这条候选此刻坐在哪，
        在前文加一行注释不该让整份基线失效。
      · **仓根前缀**——vulture 印的是**相对 CWD** 的路径（实测：同一棵树，从仓根跑印
        `src/agent_wechat/appmsg.py`，从别处跑印绝对路径）。不剥掉它的话，换一个目录调用
        就会把整份基线判成「新增」：28 条幻影，而假红比不红更快被绕开。
    """
    line = line.strip()
    if line.startswith(_ROOT_PREFIX):
        line = line[len(_ROOT_PREFIX):]
    return _LINENO.sub("::", line)


def entries(lines: Iterable[str]) -> set[str]:
    return {normalize(line) for line in lines if line.strip()}


def run_vulture(src: Path) -> tuple[int, list[str], str]:
    """跑 vulture，原样返回 (退出码, stdout 行, stderr)。"""
    proc = subprocess.run(
        [sys.executable, "-m", "vulture", str(src), "--min-confidence", str(MIN_CONFIDENCE)],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout.splitlines(), proc.stderr


def baseline(path: Path) -> set[str]:
    return entries(line for line in path.read_text().splitlines()
                   if not line.startswith("#"))


def report(rc: int, now: set[str], base: set[str], tool_stderr: str = "") -> int:
    """印出结论并给出退出码（0 干净 / 1 有新增 / 2 这步没跑起来）。

    `tool_stderr` 只在 rc 超出契约时用得上（要把它自己的话原样带给看日志的人）。
    """
    if rc not in _OK_CODES:
        print(f"❌ vulture 这一步没跑起来（退出码 {rc}）——**判为失败，不判为「没有候选」**",
              file=sys.stderr)
        print(f"   它自己的话：{tool_stderr.strip() or '(stderr 为空)'}", file=sys.stderr)
        print("   补救：`uv sync`（vulture 在 pyproject 的 dev 组，装好后离线可用）。"
              "这一步不许跳过。", file=sys.stderr)
        return 2

    print(f"基线 {len(base)} 条 | 本次候选 {len(now)} 条")
    new = sorted(now - base)
    gone = sorted(base - now)
    if new:
        print("❌ 新增的 vulture 候选（要么修掉，要么走一次显式的基线更新）：", file=sys.stderr)
        for entry in new:
            print(f"   {entry}", file=sys.stderr)
        return 1
    print("✅ 没有基线外的候选")
    if gone:
        print(f"ℹ️ 基线里这几条已经不存在了（可以从基线删掉）：{' '.join(gone)}")
    return 0


def main() -> int:
    rc, lines, tool_stderr = run_vulture(SRC)
    return report(rc, entries(lines), baseline(BASELINE), tool_stderr)


if __name__ == "__main__":
    raise SystemExit(main())
