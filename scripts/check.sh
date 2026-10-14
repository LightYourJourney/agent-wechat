#!/usr/bin/env bash
# 单一门禁入口（spec §7）。**任何一步失败即非零退出，并指明是哪一步、哪一条**。
#
# 用法：scripts/check.sh [--fast]
#   --fast  只跑快速检查（import 环 + `import mcp` 泄漏），给 pre-commit 用（实测 ~0.07 s）；
#   不带参数跑全部五步，给 pre-push 用（实测 ~70 s，其中绝大部分是 pytest）。
#
# 五步与 spec §7 的对应：
#   ① 运行时 import 环（scripts/check_import_cycles.py）
#   ② vulture 候选数 **≤ 基线**，且**不许出现基线外的新条目**（security/gate-baseline.txt）
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
  # ② vulture：候选集必须**不比基线多**。基线只许放**存量**条目（spec §7③）——
  #    新增同类即失败，删掉旧的不会失败（那是改善，只在输出里提示一行）。
  # 基线文件里 `#` 开头的是说明（与 security/allowlist.txt 同款），**不进比较**。
  # **行号不进比较**（`file:12:` → `file::`）：条目的身份是 (文件, 类别, 符号)，行号只是
  # 它这一刻坐在哪——拿行号当身份的话，随便在前文加一行注释就会让整份基线失效，
  # 而那种门禁的下场是被绕过。
  norm() { sed -E 's/:[0-9]+:/::/' | sort; }
  BASE=$(grep -v '^#' security/gate-baseline.txt | grep -v '^$' | norm)
  echo "── ② vulture 候选（基线 $(printf '%s\n' "$BASE" | grep -c . ) 条）"
  VOUT=$(uvx --offline vulture src/ --min-confidence 60 2>/dev/null | norm)
  NEW=$(comm -13 <(printf '%s\n' "$BASE") <(printf '%s\n' "$VOUT") | grep -v '^$' || true)
  GONE=$(comm -23 <(printf '%s\n' "$BASE") <(printf '%s\n' "$VOUT") | grep -v '^$' || true)
  if [ -n "$NEW" ]; then
    echo "❌ 新增的 vulture 候选（要么修掉，要么走一次显式的基线更新）：" >&2
    printf '   %s\n' $NEW >&2
    FAILED+=("② vulture")
  else
    echo "✅ 没有基线外的候选"
  fi
  [ -n "$GONE" ] && echo "ℹ️ 基线里这几条已经不存在了（可以从基线删掉）：$(printf '%s ' $GONE)"

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
