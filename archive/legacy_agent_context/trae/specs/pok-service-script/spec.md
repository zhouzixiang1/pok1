# Pok Web 服务管理脚本 — 设计规格

## 目标

将 `python web/main.py` 的启动/停止封装为一个独立的 shell 管理脚本 (`pokctl.sh`)，使服务不再依赖终端会话在线，支持后台守护运行、优雅停止、状态查询。

## 背景

当前启动方式：在终端中执行 `python web/main.py`，关闭终端则服务终止。

- `web/main.py` 启动 uvicorn FastAPI 服务，默认端口 8000
- 服务内部会通过 `daemon_management.py` 管理一个 `elo_daemon.py` 子进程（独立进程组）
- 日志通过 `logging_config.py` 输出到 `web/logs/app.log`（RotatingFileHandler）+ stderr
- PID 文件已有先例：`web/core/results/.daemon_pid` 用于 daemon 子进程

## 方案：单文件 Shell 脚本

创建 `pokctl.sh`（项目根目录），提供 `start` / `stop` / `status` / `restart` / `logs` 子命令。

### 设计要点

1. **PID 管理**
   - PID 文件：`web/logs/.server.pid`（与日志同目录）
   - 写入格式：`{"pid": <PID>}`
   - 使用 `start_new_session=true`（`setsid`）确保进程组独立
   - 启动时先检查并清理旧 PID（孤儿进程处理）

2. **后台启动**
   - 使用 `nohup` + `setsid` 启动，stdout/stderr 重定向到 `web/logs/server.stdout.log`
   - 不使用 `--dev` 模式（生产启动）
   - 透传额外参数给 `python web/main.py`（如 `--port 3000 --no-build`）

3. **优雅停止**
   - 发送 SIGTERM 到进程组（`kill -- -<PID>`）
   - 等待最多 10 秒，超时则 SIGKILL
   - 清理 PID 文件
   - 注意：elo_daemon 已有独立生命周期管理（atexit + lifespan shutdown），不需要额外处理

4. **状态查询**
   - 检查 PID 文件是否存在
   - 检查 `/proc/<PID>` 是否存在
   - 检查端口是否在监听

5. **日志查看**
   - `logs` 子命令 `tail -f web/logs/server.stdout.log`

### 脚本结构

```
pokctl.sh <command> [options]

Commands:
  start [args...]    后台启动服务，args 透传给 python web/main.py
  stop               优雅停止服务
  status             查询服务状态
  restart [args...]  stop + start
  logs               实时查看服务日志 (tail -f)

Options (for start/restart):
  --port PORT        指定端口 (默认 8000)
  --no-build         跳过前端构建
  --no-daemon        禁用内部 daemon
```

### 关键实现细节

- 脚本开头 `cd` 到自身所在目录（项目根目录）
- 使用 `python3` 或检测虚拟环境中的 `python`
- 启动成功后等待 2 秒验证进程是否存活，失败则报错
- `stop` 时同时检查 PID 文件中的进程是否存在，不存在则尝试通过端口查找（`lsof`/`ss`）
- 所有输出信息清晰明确，包含 PID 和端口

### 文件变更清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 新建 | `pokctl.sh` | 服务管理脚本 |
| 修改 | `.gitignore` | 添加 `web/logs/.server.pid` |

### 不做的事

- 不做 systemd service（保持简单，shell 脚本足够）
- 不修改 `web/main.py` 本身
- 不引入新依赖
