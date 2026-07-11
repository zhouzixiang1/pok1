#!/usr/bin/env bash
# pok-arena 验收总览:pytest 协议单测 + 健壮性验收(断线/非法/重开)。
#
# 对应 HANDOFF 11 项验收标准(L186-198):
#   1 import 冒烟        -> pytest(单测隐含 import)
#   6 CLI 各命令冒烟     -> serve/connect/thp/status 已在开发期验证
#   7 断线判负           -> accept_disconnect.py
#   8 非法行为           -> accept_illegal.py
#   9 重开 re-arm        -> accept_rearm.py
#   10 数据隔离 / 11 安全基线(127.0.0.1) -> 设计保证(arena 不 import pok1,默认 host)
# 手动另跑(需外部 bot):
#   2 两个 native_bot >=10 手无死锁(no-\n 路径) — 见 scripts/run_native_demo.sh
#   3 两个 connect 跑完一场 + native_bot 验证
#   4 /arena SSE 实时 + snapshot 刷新(浏览器)
#   5 CLI 终端闭环 serve->connect->status->thp
set -u
cd "$(dirname "$0")/.."
PY="${PY:-./.venv/bin/python}"
export POK_ARENE_WEB_PORT="${POK_ARENE_WEB_PORT:-}"

pass=0; fail=0
run() {
  local name="$1"; shift
  echo "=== $name ==="
  if "$@"; then pass=$((pass+1)); else fail=$((fail+1)); echo "  !! $name 失败"; fi
  echo
}

run "[1/6] pytest 协议单测(35)"     $PY -m pytest arena/backend/tests/ -q
run "[7] 断线 forfeit"               $PY scripts/accept_disconnect.py
run "[8] 非法 bet"                   $PY scripts/accept_illegal.py
run "[9] 重开 re-arm"                $PY scripts/accept_rearm.py

echo "========================================"
echo "验收总览: 通过 $pass, 失败 $fail"
[ "$fail" -eq 0 ] && echo "✓ 自动化验收全通过(2/3/4/5 需外部 bot/浏览器手动跑)"
exit $fail
