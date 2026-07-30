#!/usr/bin/env bash
# pok-arena 新平台(botzone 风格)进程管理:启停 serve-web、查状态、看日志、清理。
#
# 与 scripts/arena-ctl.sh(TCP 通道,端口 50101/50180)并列、互不干扰:
#   - arena-ctl.sh    → 旧 TCP 观赛平台(serve,端口 50101 + 50180)
#   - platform-ctl.sh → 新平台(serve-web,端口 50280,独立 arena_platform.db)
#
# 用法:
#   platform-ctl start              启动新平台 serve-web(后台)
#   platform-ctl stop               停止 serve-web + 正在跑的 bot 容器
#   platform-ctl restart            重启
#   platform-ctl status             查运行状态(/api/health + /api/state)
#   platform-ctl logs [n]           tail 日志(默认 50 行)
#   platform-ctl health             只查健康端点
#   platform-ctl clean              停止 + 清运行产物(保留 arena_platform.db 历史)
#   platform-ctl docker-ps          查正在跑的 bot 容器
#   platform-ctl docker-clean       停所有 arena bot 容器(异常残留时用)
#
# 环境变量(均可选;优先读项目根 .env):
#   POK_PLATFORM_HOST   (0.0.0.0 公网 / 127.0.0.1 本机) 绑定地址
#   POK_PLATFORM_PORT   (50280)       web 端口
#   POK_PLATFORM_DB     (arena_platform.db)  SQLite 库路径
#   POK_PLATFORM_UPLOAD (bot_uploads)  bot 上传根目录
#   POK_PLATFORM_LOG_LEVEL (WARNING)  日志级别(DEBUG/INFO/WARNING/ERROR)
#   POK_PLATFORM_RUNDIR (./platform-ctl)  PID + 日志目录
#   SMTP_* / POK_PLATFORM_RATE_LIMIT / POK_PLATFORM_MAX_CONCURRENT_MATCHES 等见 .env.example
set -u

HERE="$(cd "$(dirname "$0")/.." && pwd)"
# 加载项目根 .env(若存在);已导出的环境变量优先不被覆盖
if [ -f "$HERE/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$HERE/.env"
  set +a
fi
RUNDIR="${POK_PLATFORM_RUNDIR:-$HERE/platform-ctl}"
PY="${PY:-$HERE/.venv/bin/python}"
HOST="${POK_PLATFORM_HOST:-0.0.0.0}"
PORT="${POK_PLATFORM_PORT:-50280}"
DB="${POK_PLATFORM_DB:-$HERE/arena_platform.db}"
UPLOAD="${POK_PLATFORM_UPLOAD:-$HERE/bot_uploads}"
LOG_LEVEL="${POK_PLATFORM_LOG_LEVEL:-WARNING}"
LOGDIR="$RUNDIR/logs"
mkdir -p "$RUNDIR" "$LOGDIR" "$UPLOAD" "$(dirname "$DB")"
cd "$HERE"

PID_FILE="$RUNDIR/serve.pid"
STDOUT_LOG="$LOGDIR/serve.stdout"

# ── 辅助 ──────────────────────────────────────────────────
serve_pid() { cat "$PID_FILE" 2>/dev/null || true; }

is_running() {
  local p; p="$(serve_pid)"
  [ -n "$p" ] && kill -0 "$p" 2>/dev/null
}

# 停所有 arena bot 容器(serve-web 异常退出后可能残留)
docker_clean_arena() {
  if ! command -v docker >/dev/null 2>&1; then return; fi
  local ids
  ids="$(docker ps -q --filter "name=arena-" 2>/dev/null)"
  if [ -n "$ids" ]; then
    echo "$ids" | xargs -r docker stop >/dev/null 2>&1 || true
    echo "已停止残留 bot 容器"
  fi
}

# ── 命令 ──────────────────────────────────────────────────

cmd_start() {
  if is_running; then
    echo "新平台已在运行(pid $(serve_pid))  http://127.0.0.1:$PORT/"
    return 0
  fi
  echo ">> 启动新平台 serve-web(host=$HOST port=$PORT db=$DB)..."
  nohup "$PY" -m arena.backend.cli serve-web \
    --host "$HOST" --web-port "$PORT" \
    --db-path "$DB" --upload-root "$UPLOAD" \
    --log-file "$LOGDIR/serve.log" --log-level "$LOG_LEVEL" \
    > "$STDOUT_LOG" 2>&1 &
  echo $! > "$PID_FILE"
  sleep 3
  if ! is_running; then
    echo "!! serve-web 启动失败,见 $STDOUT_LOG"
    tail -20 "$STDOUT_LOG"
    rm -f "$PID_FILE"
    return 1
  fi
  # 健康检查(绑 0.0.0.0 时用 loopback 探测)
  local probe_host="127.0.0.1"
  local health
  health="$(curl -fsS "http://$probe_host:$PORT/api/health" 2>/dev/null || true)"
  if [ -z "$health" ]; then
    echo "   进程已起(pid $(serve_pid))但 /api/health 无响应,可能仍在初始化"
    echo "   稍候再试:platform-ctl status"
  else
    echo "   ✅ 就绪(pid $(serve_pid))  bind=$HOST:$PORT  访问 http://127.0.0.1:$PORT/"
  fi
}

cmd_stop() {
  local p; p="$(serve_pid)"
  if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
    # 先 SIGTERM 给 uvicorn 优雅停(它 internally 关 docker session)
    kill "$p" 2>/dev/null
    # 等 5 秒
    local i
    for i in $(seq 1 50); do
      kill -0 "$p" 2>/dev/null || break
      sleep 0.1
    done
    # 仍未退出则 SIGKILL
    if kill -0 "$p" 2>/dev/null; then
      kill -9 "$p" 2>/dev/null || true
      echo "停止 serve-web(pid $p,强制 SIGKILL)"
    else
      echo "停止 serve-web(pid $p,优雅退出)"
    fi
  else
    echo "serve-web 未运行"
  fi
  rm -f "$PID_FILE"
  # 清残留 bot 容器(异常退出时)
  docker_clean_arena
}

cmd_status() {
  if is_running; then
    echo "serve-web: 运行中(pid $(serve_pid),端口 $PORT)"
  else
    echo "serve-web: 未运行"
    return 0
  fi
  # /api/health
  local health
  health="$(curl -fsS "http://127.0.0.1:$PORT/api/health" 2>/dev/null || true)"
  if [ -n "$health" ]; then
    echo "  health: $health"
  else
    echo "  health: (无响应,可能仍在启动)"
  fi
  # 数据库摘要
  if [ -f "$DB" ]; then
    "$PY" - "$DB" <<'PYEOF' 2>/dev/null || true
import sys
from arena.backend.platform.store import Store
s = Store(sys.argv[1])
try:
    print(f"  数据: {len(s.list_users())} 用户 / {len(s.list_bots())} bot / {s.count_matches()} 对局 / {len(s.leaderboard())} 排行榜")
except Exception:
    pass
PYEOF
  fi
  # 运行中的 bot 容器
  if command -v docker >/dev/null 2>&1; then
    local n
    n="$(docker ps -q --filter "name=arena-" 2>/dev/null | wc -l)"
    echo "  bot 容器: $n 个运行中"
  fi
}

cmd_logs() {
  local n="${1:-50}"
  if [ -f "$LOGDIR/serve.log" ]; then
    tail -n "$n" "$LOGDIR/serve.log"
  elif [ -f "$STDOUT_LOG" ]; then
    tail -n "$n" "$STDOUT_LOG"
  else
    echo "无日志(serve-web 未启动过或 log-level 过高)"
  fi
}

cmd_health() {
  curl -fsS "http://127.0.0.1:$PORT/api/health" 2>/dev/null \
    && echo || echo "(无响应,serve-web 未运行或仍在启动)"
}

cmd_docker_ps() {
  if ! command -v docker >/dev/null 2>&1; then echo "docker 未安装"; return; fi
  local lines
  lines="$(docker ps --filter "name=arena-" --format "table {{.Names}}\t{{.Image}}\t{{.Status}}" 2>/dev/null)"
  if [ -n "$lines" ]; then echo "$lines"; else echo "(无 arena bot 容器运行)"; fi
}

cmd_docker_clean() {
  echo "停止所有 arena bot 容器..."
  docker_clean_arena
  echo "完成"
}

cmd_clean() {
  echo ">> 停止服务..."
  cmd_stop
  echo ">> 清理运行产物(保留 $DB 历史)..."
  rm -f "$LOGDIR"/*.log "$STDOUT_LOG" "$RUNDIR"/*.pid
  echo "   已清 $RUNDIR(日志/PID)。数据库 $DB 保留。"
  echo "   如需清空历史数据:rm -f $DB"
}

case "${1:-}" in
  start)        cmd_start ;;
  stop)         cmd_stop ;;
  restart)      cmd_stop; sleep 1; cmd_start ;;
  status)       cmd_status ;;
  logs)         shift; cmd_logs "${1:-50}" ;;
  health)       cmd_health ;;
  docker-ps)    cmd_docker_ps ;;
  docker-clean) cmd_docker_clean ;;
  clean)        cmd_clean ;;
  *) cat <<EOF
用法: platform-ctl <命令>

  start              启动新平台 serve-web(后台,端口 $PORT)
  stop               停止 serve-web + 残留 bot 容器
  restart            重启
  status             查运行状态(进程/health/数据/容器)
  logs [n]           tail 日志(默认 50 行)
  health             只查 /api/health
  docker-ps          查正在跑的 bot 容器
  docker-clean       停所有 arena bot 容器(异常残留时用)
  clean              停止 + 清日志/PID(保留数据库)

环境: POK_PLATFORM_HOST($HOST) POK_PLATFORM_PORT($PORT)
      POK_PLATFORM_DB($DB) POK_PLATFORM_UPLOAD($UPLOAD)
      POK_PLATFORM_LOG_LEVEL($LOG_LEVEL) POK_PLATFORM_RUNDIR($RUNDIR)

前端: http://127.0.0.1:$PORT/   Wiki: http://127.0.0.1:$PORT/#/wiki
EOF
     exit 1 ;;
esac
