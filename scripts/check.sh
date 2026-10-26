#!/usr/bin/env bash
# 单一门禁入口（spec §7）。**任何一步失败即非零退出，并指明是哪一步、哪一条**。
#
# 用法：scripts/check.sh [--fast]
#   --fast  只跑快速检查（import 环 + `import mcp` 泄漏），给 pre-commit 用（实测 ~0.07 s）；
#   不带参数跑全部五步，给 pre-push 用（本机三次连跑 133–156 s，绝大部分是 pytest；
#   文件缓存与机器负载会拉大，实测见过 275 s）。
#   **计时时不要并跑其它 pytest**：SQLite 的 fsync 争用会把墙钟放大到 3 倍以上（实测）。
#
# 五步与 spec §7 的对应：
#   ① 运行时 import 环（scripts/check_import_cycles.py）
#   ② vulture 候选**集**不超基线（scripts/check_vulture.py；基线 security/gate-baseline.txt）
#   ③ pytest 全量
#   ④ security/scan.py --all（占位符/形状扫描）
#   ⑤ security/scan.py --check-hooks（钩子自检）
set -uo pipefail
cd "$(dirname "$0")/.."

PY=${PY:-.venv/bin/python}
[ -x "$PY" ] || PY=python3
FAILED=()

# 每一步的完整输出**同时**落进一个日志文件：失败时只印一行「哪一步」在事后是查不出原因的
# ——2026-10-14 就吃过一次（③ pytest 红了一次、复跑又绿，而那次的细节已经被截断的终端吃掉）。
# 日志用 mktemp，失败时把路径印出来。
LOG=$(mktemp -t awgate)

step() {                      # step <名字> <要跑的...>
  local name=$1; shift
  echo "── $name"
  # `set -o pipefail`（文件头）让管道的退出码取**最右的非零**——所以 `if !` 判的就是
  # 那一步自己的成败，不必再看 `PIPESTATUS`。（此前那半句判据连同「pipefail 未开」的注释
  # 一起删掉了：`pipefail` 一直是开的，而 `${FAILED[-1]}` 的负下标还要 bash 4.3+，
  # macOS 自带的是 3.2——两处都是死代码。review-code 的 Standards 轴 #1/#2。）
  if ! "$@" 2>&1 | tee -a "$LOG"; then FAILED+=("$name"); fi
}

step "① import 环" "$PY" scripts/check_import_cycles.py

if [ "${1:-}" != "--fast" ]; then
  # ② vulture 候选**集**必须不比基线多。基线只许放**存量**条目（spec §7③）——
  #    新增同类即失败，删掉旧的不会失败（那是改善，只在输出里提示一行）。
  #
  #    判据本身在 scripts/check_vulture.py 里（Python，能被 pytest 钉住）。**为什么不在这一段
  #    shell 里**：2026-10-14 它在这里一天错了两次，两次都只在「工具在场/缺席」变化时才现形
  #    （先是取不到 vulture 时把空输出当成「没有候选」印了个 ✅，真放行过一次 push；改完又把
  #    vulture 的 rc=3「找到死代码」当成工具坏了，门禁在工具完全正常时恒红）。两次都不是引号
  #    或 `comm` 的用法问题，是判据本身没人验——所以按 ① 的规格挪成脚本，退出码 0/1/2
  #    （1＝有新增候选，2＝**这一步没跑起来**）与全部来龙去脉写在那个文件的 docstring 里。
  step "② vulture 候选" "$PY" scripts/check_vulture.py

  step "③ pytest" "$PY" -m pytest tests/ -q
  step "④ scan --all" "$PY" security/scan.py --all
  step "⑤ 钩子自检" "$PY" security/scan.py --check-hooks
fi

if [ ${#FAILED[@]} -gt 0 ]; then
  echo
  echo "❌ 门禁未通过：${FAILED[*]}" >&2
  echo "   完整输出（含那一步的原始报错）：$LOG" >&2
  exit 1
fi
rm -f "$LOG"
echo
echo "✅ 门禁通过"
