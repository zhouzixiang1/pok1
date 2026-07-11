#!/usr/bin/env bash
# pok-arena 进程管理:启动/停止 serve + bot、查状态、导出 THP、看日志、清理。
# 单桌单场演示与比赛现场一键开赛/关赛。
#
# 用法:
#   arena-ctl start [hands] [connect|native]   启动 serve 并拉两个 bot
#   arena-ctl stop                              停止 serve + bot
#   arena-ctl status                            查 serve 状态(/api/state)
#   arena-ctl thp [list|<match-id>]             列出/导出 THP 棋谱
#   arena-ctl logs [serve|botA|botB] [n]        tail 日志
#   arena-ctl clean                             停止并清运行目录
#
# 环境变量:
#   POK_ARENA_TCP_PORT (50101) / WEB_PORT (50180) / RECORDS / RUNDIR
#   NATIVE_BOT_DIR (/tmp/nv141/bots/national_v141) / NATIVE_BOT_PY (anaconda python3)
set -u

HERE="$(cd "$(dirname "$0")/.." && pwd)"
RUNDIR="${POK_ARENA_RUNDIR:-/tmp/pok-arena-ctl}"
PY="${PY:-$HERE/.venv/bin/python}"
TCP_PORT="${POK_ARENA_TCP_PORT:-50101}"
WEB_PORT="${POK_ARENA_WEB_PORT:-50180}"
RECORDS="${POK_ARENA_RECORDS:-$RUNDIR/records}"
LOGDIR="$RUNDIR/logs"
mkdir -p "$RUNDIR" "$LOGDIR" "$RECORDS"

serve_pid() { cat "$RUNDIR/serve.pid" 2>/dev/null || true; }
is_running() { local p; p="$(serve_pid)"; [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }

cmd_start() {
  if is_running; then echo "serve 已在运行(pid $(serve_pid))"; return 0; fi
  local hands="${1:-70}"
  local botmode="${2:-connect}"
  echo ">> 启动 serve(tcp $TCP_PORT, web $WEB_PORT, $hands 手/场)..."
  nohup "$PY" -m arena.backend.cli serve \
    --tcp-port "$TCP_PORT" --web-port "$WEB_PORT" --hands-per-match "$hands" \
    --records-dir "$RECORDS" --log-file "$LOGDIR/serve.log" --log-level INFO \
    > "$LOGDIR/serve.stdout" 2>&1 &
  echo $! > "$RUNDIR/serve.pid"
  sleep 2.5
  if ! is_running; then
    echo "!! serve 启动失败,见 $LOGDIR/serve.stdout"; tail -15 "$LOGDIR/serve.stdout"; return 1
  fi
  echo "   serve 已起(pid $(serve_pid))  前端 http://127.0.0.1:$WEB_PORT/  TCP $TCP_PORT"
  case "$botmode" in
    native) start_native_bots ;;
    *) start_connect_bots ;;
  esac
  echo ">> 完成。观察:arena-ctl status | logs serve | thp list"
}

start_connect_bots() {
  echo ">> 拉起两个 connect 跟随器(自带协议练习器,发 \\n)..."
  nohup "$PY" -m arena.backend.cli connect 127.0.0.1 "$TCP_PORT" BotA --quiet > "$LOGDIR/botA.log" 2>&1 &
  echo $! > "$RUNDIR/botA.pid"
  nohup "$PY" -m arena.backend.cli connect 127.0.0.1 "$TCP_PORT" BotB --quiet > "$LOGDIR/botB.log" 2>&1 &
  echo $! > "$RUNDIR/botB.pid"
  echo "   BotA=$(cat "$RUNDIR/botA.pid") BotB=$(cat "$RUNDIR/botB.pid")"
}

start_native_bots() {
  local botdir="${NATIVE_BOT_DIR:-/tmp/nv141/bots/national_v141}"
  local botpy="$botdir/national_bot.py"
  local bopy="${NATIVE_BOT_PY:-/home/zzx/anaconda3/envs/pytorch/bin/python3}"
  if [ ! -f "$botpy" ]; then
    echo "!! 未找到 native_bot($botpy)。设 NATIVE_BOT_DIR, 或用 'start . connect'。"
    echo "   提取: git -C ~/project/pok archive origin/main bots/national_v141 | tar -x -C /tmp/nv141"
    return 1
  fi
  echo ">> 拉起两个 native_bot(发裸字节无 \\n, 验证 no-\\n 路径; python=$bopy)..."
  POK_OFFICIAL_ACTION_DELAY=0 nohup "$bopy" "$botpy" --host 127.0.0.1 --port "$TCP_PORT" --name NativeA > "$LOGDIR/botA.log" 2>&1 &
  echo $! > "$RUNDIR/botA.pid"
  POK_OFFICIAL_ACTION_DELAY=0 nohup "$bopy" "$botpy" --host 127.0.0.1 --port "$TCP_PORT" --name NativeB > "$LOGDIR/botB.log" 2>&1 &
  echo $! > "$RUNDIR/botB.pid"
  echo "   NativeA=$(cat "$RUNDIR/botA.pid") NativeB=$(cat "$RUNDIR/botB.pid")"
}

cmd_stop() {
  for f in botA botB serve; do
    local p; p="$(cat "$RUNDIR/$f.pid" 2>/dev/null || true)"
    if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
      kill "$p" 2>/dev/null && echo "停止 $f (pid $p)"
    fi
    rm -f "$RUNDIR/$f.pid"
  done
}

cmd_status() {
  if is_running; then echo "serve: 运行中(pid $(serve_pid))"; else echo "serve: 未运行"; fi
  "$PY" -m arena.backend.cli status --host 127.0.0.1 --port "$WEB_PORT" 2>/dev/null \
    || echo "(web 未就绪或 serve 未启动)"
}

cmd_thp() {
  if [ "${1:-}" = "" ] || [ "${1:-}" = "list" ]; then
    "$PY" -m arena.backend.cli thp list --records-dir "$RECORDS"
  else
    "$PY" -m arena.backend.cli thp show "$1" --records-dir "$RECORDS"
  fi
}

cmd_logs() { local f="${1:-serve}"; tail -n "${2:-30}" "$LOGDIR/$f.log" 2>/dev/null || echo "无 $f 日志"; }

cmd_clean() { cmd_stop; rm -rf "$RECORDS"; rm -f "$LOGDIR"/*.log "$RUNDIR"/*.pid; echo "已清理 $RUNDIR"; }

case "${1:-}" in
  start)  shift; cmd_start "${1:-70}" "${2:-connect}" ;;
  stop)   cmd_stop ;;
  status) cmd_status ;;
  thp)    shift; cmd_thp "${1:-}" ;;
  logs)   shift; cmd_logs "${1:-serve}" "${2:-30}" ;;
  clean)  cmd_clean ;;
  *) cat <<EOF
用法: arena-ctl <命令>
  start [hands] [connect|native]   启动 serve + 两个 bot(默认 connect)
  stop                              停止 serve + bot
  status                            查 serve 状态
  thp [list|<match-id>]             列出/导出 THP
  logs [serve|botA|botB] [n]        tail 日志(默认 serve 30 行)
  clean                             停止并清运行目录
环境: POK_ARENA_TCP_PORT/WEB_PORT/RECORDS/RUNDIR, NATIVE_BOT_DIR/NATIVE_BOT_PY
EOF
     exit 1 ;;
esac
