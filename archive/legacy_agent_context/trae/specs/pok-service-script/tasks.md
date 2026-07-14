# Tasks

## Task 1: 创建 pokctl.sh 脚本
- [ ] 实现脚本框架：cd 到项目根目录、变量定义（PID_FILE、LOG_DIR、LOG_FILE）
- [ ] 实现 `start` 子命令：孤儿清理 → nohup setsid 启动 → PID 写入 → 存活验证
- [ ] 实现 `stop` 子命令：读取 PID → SIGTERM 进程组 → 等待/超时 SIGKILL → 清理 PID
- [ ] 实现 `status` 子命令：PID 文件检查 → /proc 检查 → 端口检查
- [ ] 实现 `restart` 子命令：stop → sleep 1 → start
- [ ] 实现 `logs` 子命令：tail -f 日志文件
- [ ] 参数透传：start/restart 的额外参数传递给 python web/main.py

## Task 2: 更新 .gitignore
- [ ] 添加 `web/logs/.server.pid`

## Task 3: 验证
- [ ] 赋予执行权限 chmod +x
- [ ] 测试 start/stop/status/restart/logs 各子命令
