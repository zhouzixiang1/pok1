#!/usr/bin/env bash
# pokctl.sh — Pok Web 服务管理脚本
# 用法: ./pokctl.sh <start|stop|status|restart|logs> [args...]

set -euo pipefail

# ── 切换到项目根目录 ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
ROOT_REAL="$(pwd -P)"
ROOT_GIT_TOP="$(git -C "$ROOT_REAL" rev-parse --show-toplevel 2>/dev/null || true)"
if [ -n "$ROOT_GIT_TOP" ]; then
    ROOT_GIT_TOP="$(cd "$ROOT_GIT_TOP" && pwd -P)"
else
    ROOT_GIT_TOP="$ROOT_REAL"
fi
PROC_ROOT="${POKCTL_PROC_ROOT:-/proc}"

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
    [ -n "$pid" ] && [ -d "$PROC_ROOT/$pid" ] 2>/dev/null
}

proc_cwd() {
    local pid="$1"
    readlink -f "$PROC_ROOT/$pid/cwd" 2>/dev/null || true
}

proc_cmdline() {
    local pid="$1"
    tr '\0' ' ' < "$PROC_ROOT/$pid/cmdline" 2>/dev/null || true
}

path_is_in_checkout() {
    local path="$1"
    local real_path git_top
    [ -n "$path" ] || return 1
    real_path="$(readlink -f "$path" 2>/dev/null || true)"
    [ -n "$real_path" ] || return 1

    git_top="$(git -C "$real_path" rev-parse --show-toplevel 2>/dev/null || true)"
    if [ -n "$git_top" ]; then
        git_top="$(cd "$git_top" && pwd -P)"
        [ "$git_top" = "$ROOT_GIT_TOP" ]
        return
    fi

    # Fallback for non-git deployments. In the normal repository layout the
    # git-top check above prevents nested checkouts such as .evolution_pok from
    # being treated as this checkout merely because they share a path prefix.
    [ "$real_path" = "$ROOT_REAL" ] && return 0
    case "$real_path" in
        "$ROOT_REAL"/*) return 0 ;;
        *) return 1 ;;
    esac
}

pid_matches_checkout_program() {
    local pid="$1"
    local rel_program="$2"
    local cwd cmd

    is_alive "$pid" || return 1
    cwd="$(proc_cwd "$pid")"
    cmd="$(proc_cmdline "$pid")"

    case "$cmd" in
        *"$rel_program"*|*"$ROOT_REAL/$rel_program"*) ;;
        *) return 1 ;;
    esac

    path_is_in_checkout "$cwd" && return 0
    case "$cmd" in
        *"$ROOT_REAL/$rel_program"*) return 0 ;;
    esac
    return 1
}

pid_is_checkout_server() {
    pid_matches_checkout_program "$1" "$MAIN_PY"
}

pid_is_checkout_daemon() {
    pid_matches_checkout_program "$1" "web/core/elo_daemon.py"
}

find_listening_pid() {
    local port="$1"
    ss -H -tlnp 2>/dev/null \
        | awk -v suffix=":$port" '$4 ~ suffix "$" { print; exit }' \
        | grep -oP 'pid=\K[0-9]+' \
        | head -1 || true
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

arg_present() {
    local needle="$1"
    shift || true
    local arg
    for arg in "$@"; do
        if [ "$arg" = "$needle" ]; then
            return 0
        fi
    done
    return 1
}

frontend_static_ready() {
    [ -f "web/server/static/index.html" ] && [ -d "web/server/static/assets" ]
}

configure_evolution_publish_env() {
    : "${POK_EVOLUTION_RUNTIME:=1}"
    : "${POK_REQUIRE_EVOLUTION_PUSH:=$POK_EVOLUTION_RUNTIME}"
    : "${EVOLUTION_GIT_PUSH:=$POK_REQUIRE_EVOLUTION_PUSH}"
    : "${POK_OFFICIAL_REQUIRED:=1}"
    : "${POK_OFFICIAL_SMOKE_GATE:=1}"
    : "${POK_OFFICIAL_PRECOMMIT_GATE:=1}"
    : "${POK_OFFICIAL_PRECOMMIT_SELF_ROUNDS:=1}"
    : "${POK_OFFICIAL_PRECOMMIT_OPPONENT_ROUNDS:=1}"
    : "${POK_OFFICIAL_PRECOMMIT_TARGET_HANDS:=10}"
    : "${POK_OFFICIAL_JOB_RECONCILER:=1}"
    : "${POK_OFFICIAL_SIGNING_KEY:=$HOME/.config/pok/official_certifier_ed25519_epoch2}"
    # An explicit value is only a preference. Python validates it against the
    # content-bound official-opponent policy and otherwise selects an eligible
    # active bot; the launcher must never inject a legacy path as a bypass.
    : "${POK_OFFICIAL_OPPONENT:=}"
    : "${POK_ACTIVE_NATIVE_CONTRACT_FILTER:=1}"
    export \
        POK_EVOLUTION_RUNTIME POK_REQUIRE_EVOLUTION_PUSH EVOLUTION_GIT_PUSH \
        POK_OFFICIAL_REQUIRED POK_OFFICIAL_SMOKE_GATE POK_OFFICIAL_PRECOMMIT_GATE \
        POK_OFFICIAL_PRECOMMIT_SELF_ROUNDS \
        POK_OFFICIAL_PRECOMMIT_OPPONENT_ROUNDS POK_OFFICIAL_PRECOMMIT_TARGET_HANDS \
        POK_OFFICIAL_JOB_RECONCILER POK_OFFICIAL_SIGNING_KEY POK_OFFICIAL_OPPONENT \
        POK_ACTIVE_NATIVE_CONTRACT_FILTER
}

kill_orphan() {
    local pid
    pid="$(read_pid)"
    if [ -n "$pid" ] && is_alive "$pid"; then
        if ! pid_is_checkout_server "$pid"; then
            echo "PID 文件指向的进程不属于当前 checkout，移除残留 PID 文件 (PID: $pid)"
            rm -f "$PID_FILE"
            return 0
        fi
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

cleanup_arena_orphans() {
    if [ "${POKCTL_SKIP_ARENA_CLEANUP:-0}" = "1" ]; then
        return 0
    fi
    if [ -f "scripts/national_arena.py" ]; then
        "$PYTHON" scripts/national_arena.py cleanup-orphans >/dev/null 2>&1 || true
    fi
}

# ── 子命令 ──
cmd_start() {
    local port
    port="$(find_port_from_args "$@")"

    # 检查是否已在运行
    local old_pid
    old_pid="$(read_pid)"
    if [ -n "$old_pid" ] && is_alive "$old_pid"; then
        if ! pid_is_checkout_server "$old_pid"; then
            echo "PID 文件指向的进程不属于当前 checkout，移除残留 PID 文件 (PID: $old_pid)"
            rm -f "$PID_FILE"
            old_pid=""
        else
            echo "服务已在运行 (PID: $old_pid, 端口: $port)"
            exit 0
        fi
    fi

    local port_pid
    port_pid="$(find_listening_pid "$port")"
    if [ -n "$port_pid" ] && is_alive "$port_pid"; then
        if pid_is_checkout_server "$port_pid"; then
            echo "服务已在运行 (PID: $port_pid, 端口: $port)，但 PID 文件缺失"
            exit 0
        fi
        echo "端口 $port 已被其他 checkout 或外部进程占用 (PID: $port_pid)，不在当前目录启动。"
        echo "请到对应 checkout 运行它自己的 pokctl.sh，或换一个 --port。"
        exit 1
    fi

    if arg_present "--no-build" "$@" && ! frontend_static_ready; then
        echo "✗ --no-build requested, but frontend static build is missing."
        echo "  Missing: web/server/static/index.html or web/server/static/assets"
        echo "  Start without --no-build, or run: cd web/frontend && npm ci && npm run build"
        exit 1
    fi

    # 清理孤儿
    kill_orphan

    # 确保日志目录存在
    mkdir -p "$LOG_DIR"
    configure_evolution_publish_env

    echo "正在启动服务 (端口: $port)..."
    nohup setsid "$PYTHON" "$MAIN_PY" "$@" >> "$STDOUT_LOG" 2>&1 &
    local server_pid=$!

    # 写入 PID 文件
    local cmd_text cmd_json
    cmd_text="$PYTHON $MAIN_PY $*"
    cmd_json="$(printf '%s' "$cmd_text" | sed 's/\\/\\\\/g; s/"/\\"/g')"
    printf '{"pid": %s, "pgid": %s, "port": %s, "cmd": "%s", "started_at": "%s"}\n' \
        "$server_pid" "$server_pid" "$port" "$cmd_json" "$(date -Iseconds)" > "$PID_FILE"

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
        # 尝试通过端口查找，但只接管当前 checkout 启动的服务。
        local port=8000
        local found_pid
        found_pid="$(find_listening_pid "$port")"
        if [ -n "$found_pid" ]; then
            if pid_is_checkout_server "$found_pid"; then
                echo "发现当前 checkout 的服务正在端口 $port 运行 (PID: $found_pid)"
                pid="$found_pid"
            else
                echo "端口 $port 上的进程不属于当前 checkout，跳过停止 (PID: $found_pid)"
                return 0
            fi
        else
            cleanup_arena_orphans
            return 0
        fi
    fi

    if ! is_alive "$pid"; then
        echo "服务未运行 (PID $pid 已不存在)"
        rm -f "$PID_FILE"
        cleanup_arena_orphans
        return 0
    fi
    if ! pid_is_checkout_server "$pid"; then
        echo "PID $pid 不属于当前 checkout 的 web/main.py，拒绝停止；移除本目录残留 PID 文件"
        rm -f "$PID_FILE"
        return 1
    fi

    echo "正在停止服务 (PID: $pid)..."

    # Phase 1: 读取 daemon PID 并先优雅停止 daemon（daemon 在独立进程组，kill -- -$pid 打不到它）
    local daemon_pid_file="$SCRIPT_DIR/web/core/results/.daemon_pid"
    if [ -f "$daemon_pid_file" ]; then
        local daemon_pid
        daemon_pid=$("$PYTHON" - "$daemon_pid_file" <<'PY' 2>/dev/null || true
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    value = json.load(fh).get("pid", "")
print(value)
PY
)
        # 验证 PID 是合法正整数 (>1)，防止 PID 0 或负数导致 kill 误杀
        if [ -n "$daemon_pid" ] && [[ "$daemon_pid" =~ ^[0-9]+$ ]] && [ "$daemon_pid" -gt 1 ] && is_alive "$daemon_pid"; then
            if ! pid_is_checkout_daemon "$daemon_pid"; then
                echo "  daemon PID 文件指向的进程不属于当前 checkout，跳过 (PID: $daemon_pid)"
            else
                echo "  优雅停止 daemon (PID: $daemon_pid, 独立进程组)..."
                kill -- -"$daemon_pid" 2>/dev/null || kill "$daemon_pid" 2>/dev/null || true
                local daemon_waited=0
                while [ $daemon_waited -lt 12 ] && is_alive "$daemon_pid"; do
                    sleep 1
                    daemon_waited=$((daemon_waited + 1))
                done
                if is_alive "$daemon_pid"; then
                    echo "  daemon ${daemon_waited}s 未退出，强制终止..."
                    kill -9 -"$daemon_pid" 2>/dev/null || kill -9 "$daemon_pid" 2>/dev/null || true
                fi
            fi
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
        # 兜底：只杀当前 checkout 下的 elo_daemon 残留。
        local proc_dir proc_pid
        for proc_dir in /proc/[0-9]*; do
            proc_pid="${proc_dir##*/}"
            if pid_is_checkout_daemon "$proc_pid"; then
                echo "  强制终止当前 checkout daemon 残留 (PID: $proc_pid)"
                kill -9 -"$proc_pid" 2>/dev/null || kill -9 "$proc_pid" 2>/dev/null || true
            fi
        done
        sleep 1
    fi

    if is_alive "$pid"; then
        echo "✗ 无法停止服务 (PID: $pid)"
        exit 1
    else
        rm -f "$PID_FILE"
        cleanup_arena_orphans
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
        if ! pid_is_checkout_server "$pid"; then
            echo "PID 文件残留：PID $pid 不属于当前 checkout 的 web/main.py"
            exit 1
        fi
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

管理 national_tcp_policy_v1 Web/API 控制面的进程生命周期。本工具不会
将归档结果、Arena 诊断或官方 EXE 筹码提升为强度/评级证据。

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
