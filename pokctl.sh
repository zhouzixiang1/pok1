#!/usr/bin/env bash
# pokctl.sh — Pok Web 服务管理脚本
# 用法: ./pokctl.sh <start|stop|status|restart|logs> [args...]

set -euo pipefail

# ── 切换到项目根目录 ──
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── 路径定义 ──
LOG_DIR="web/logs"
PID_FILE="$LOG_DIR/.server.pid"
STDOUT_LOG="$LOG_DIR/server.stdout.log"
MAIN_PY="web/main.py"

# ── 检测 Python ──
detect_python() {
    if [ -n "${VIRTUAL_ENV:-}" ]; then
        echo "$VIRTUAL_ENV/bin/python"
    elif [ -x ".venv/bin/python" ]; then
        echo ".venv/bin/python"
    elif command -v python3 &>/dev/null; then
        echo "python3"
    else
        echo "python"
    fi
}

PYTHON="$(detect_python)"

# ── 工具函数 ──
read_pid() {
    if [ -f "$PID_FILE" ]; then
        # PID 文件格式: {"pid": 12345} 或纯数字
        local content
        content="$(cat "$PID_FILE")"
        if [[ "$content" =~ \"pid\":\ *([0-9]+) ]]; then
            echo "${BASH_REMATCH[1]}"
        elif [[ "$content" =~ ^[0-9]+$ ]]; then
            echo "$content"
        else
            echo ""
        fi
    fi
}

is_alive() {
    local pid="$1"
    [ -n "$pid" ] && [ -d "/proc/$pid" ] 2>/dev/null
}

find_port_from_args() {
    # 从参数中提取 --port 值
    local args=("$@")
    for ((i = 0; i < ${#args[@]}; i++)); do
        if [ "${args[$i]}" = "--port" ] && [ $((i + 1)) -lt ${#args[@]} ]; then
            echo "${args[$((i + 1))]}"
            return
        fi
    done
    echo "8000"
}

kill_orphan() {
    local pid
    pid="$(read_pid)"
    if [ -n "$pid" ] && is_alive "$pid"; then
        echo "发现旧进程 (PID: $pid)，正在停止..."
        kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
        sleep 1
        if is_alive "$pid"; then
            kill -9 -"$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null || true
            sleep 0.5
        fi
    fi
    rm -f "$PID_FILE"
}

# ── 子命令 ──
cmd_start() {
    local port
    port="$(find_port_from_args "$@")"

    # 检查是否已在运行
    local old_pid
    old_pid="$(read_pid)"
    if [ -n "$old_pid" ] && is_alive "$old_pid"; then
        echo "服务已在运行 (PID: $old_pid, 端口: $port)"
        exit 0
    fi

    # 清理孤儿
    kill_orphan

    # 确保日志目录存在
    mkdir -p "$LOG_DIR"

    echo "正在启动服务 (端口: $port)..."
    nohup setsid "$PYTHON" "$MAIN_PY" "$@" >> "$STDOUT_LOG" 2>&1 &
    local server_pid=$!

    # 写入 PID 文件
    echo "{\"pid\": $server_pid}" > "$PID_FILE"

    # 等待验证进程存活
    sleep 2
    if is_alive "$server_pid"; then
        echo "服务已启动 ✓"
        echo "  PID:   $server_pid"
        echo "  端口:  $port"
        echo "  日志:  $STDOUT_LOG"
        echo "  应用日志: $LOG_DIR/app.log"
    else
        echo "✗ 服务启动失败，请检查日志:"
        echo "  $STDOUT_LOG"
        rm -f "$PID_FILE"
        exit 1
    fi
}

cmd_stop() {
    local pid
    pid="$(read_pid)"

    if [ -z "$pid" ]; then
        echo "服务未运行 (无 PID 文件)"
        # 尝试通过端口查找
        local port=8000
        local found_pid
        found_pid="$(ss -tlnp 2>/dev/null | grep ":${port}" | grep -oP 'pid=\K[0-9]+' | head -1 || true)"
        if [ -n "$found_pid" ]; then
            echo "发现端口 $port 上有进程 (PID: $found_pid)"
            pid="$found_pid"
        else
            exit 0
        fi
    fi

    if ! is_alive "$pid"; then
        echo "服务未运行 (PID $pid 已不存在)"
        rm -f "$PID_FILE"
        exit 0
    fi

    echo "正在停止服务 (PID: $pid)..."

    # Phase 1: 读取 daemon PID 并先杀 daemon（daemon 在独立进程组，kill -- -$pid 打不到它）
    local daemon_pid_file="$SCRIPT_DIR/web/core/results/.daemon_pid"
    if [ -f "$daemon_pid_file" ]; then
        local daemon_pid
        daemon_pid=$(python3 -c "import json; print(json.load(open('$daemon_pid_file'))['pid'])" 2>/dev/null || echo "")
        if [ -n "$daemon_pid" ] && is_alive "$daemon_pid"; then
            echo "  停止 daemon (PID: $daemon_pid, 独立进程组)..."
            kill -9 -"$daemon_pid" 2>/dev/null || kill -9 "$daemon_pid" 2>/dev/null || true
        fi
    fi

    # Phase 2: SIGTERM 到主进程组
    kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true

    # 等待进程退出（30s 预算：orchestrator 快速取消 + daemon 已被杀）
    local waited=0
    while [ $waited -lt 30 ] && is_alive "$pid"; do
        sleep 1
        waited=$((waited + 1))
    done

    # Phase 3: 超时则 SIGKILL
    if is_alive "$pid"; then
        echo "  超时（${waited}s），强制终止..."
        kill -9 -"$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null || true
        # 兜底：杀所有可能残留的子进程
        pkill -9 -f "python.*elo_daemon" 2>/dev/null || true
        pkill -9 -f "python.*bots/" 2>/dev/null || true
        sleep 1
    fi

    if is_alive "$pid"; then
        echo "✗ 无法停止服务 (PID: $pid)"
        exit 1
    else
        rm -f "$PID_FILE"
        echo "服务已停止 ✓"
    fi
}

cmd_status() {
    local pid
    pid="$(read_pid)"

    if [ -z "$pid" ]; then
        echo "服务未运行 (无 PID 文件)"
        exit 1
    fi

    if is_alive "$pid"; then
        echo "服务运行中 ✓"
        echo "  PID: $pid"
        # 检查端口
        local listening
        listening="$(ss -tlnp 2>/dev/null | grep "pid=${pid}" || true)"
        if [ -n "$listening" ]; then
            local port
            port="$(echo "$listening" | grep -oP ':\K[0-9]+' | head -1 || true)"
            echo "  端口: ${port:-未知}"
        fi
        echo "  日志: $STDOUT_LOG"
    else
        echo "服务未运行 (PID $pid 已不存在，PID 文件残留)"
        exit 1
    fi
}

cmd_restart() {
    echo "正在重启服务..."
    cmd_stop
    sleep 1
    cmd_start "$@"
}

cmd_logs() {
    local target="${1:-$STDOUT_LOG}"
    if [ ! -f "$target" ]; then
        echo "日志文件不存在: $target"
        echo "可用日志:"
        ls -la "$LOG_DIR"/*.log 2>/dev/null || echo "  (无)"
        exit 1
    fi
    tail -f "$target"
}

# ── 入口 ──
usage() {
    cat <<EOF
Pok Web 服务管理工具

用法: $0 <command> [options]

Commands:
  start [args...]    后台启动服务 (args 透传给 python web/main.py)
  stop               优雅停止服务
  status             查询服务状态
  restart [args...]  重启服务
  logs [file]        实时查看日志 (默认: server.stdout.log, 可指定 app.log)

Examples:
  $0 start                    # 默认启动 (端口 8000)
  $0 start --port 3000        # 指定端口
  $0 start --no-build         # 跳过前端构建
  $0 start --no-daemon        # 禁用内部 daemon
  $0 stop
  $0 status
  $0 logs                     # 查看 stdout 日志
  $0 logs web/logs/app.log    # 查看应用日志
EOF
}

case "${1:-}" in
    start)   shift; cmd_start "$@" ;;
    stop)    shift; cmd_stop ;;
    status)  shift; cmd_status ;;
    restart) shift; cmd_restart "$@" ;;
    logs)    shift; cmd_logs "$@" ;;
    -h|--help|help) usage ;;
    *)       echo "未知命令: ${1:-}"; usage; exit 1 ;;
esac
