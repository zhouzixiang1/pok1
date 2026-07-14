# Checklist

- [ ] pokctl.sh 位于项目根目录，有执行权限
- [ ] `start` 能后台启动服务，终端关闭后服务仍运行
- [ ] `stop` 能优雅停止服务和子进程
- [ ] `status` 正确报告运行状态（PID + 端口）
- [ ] `restart` 正确执行 stop → start
- [ ] `logs` 能实时查看日志
- [ ] PID 文件正确写入和清理
- [ ] 旧 PID（孤儿进程）在启动时被正确处理
- [ ] 额外参数（--port, --no-build 等）正确透传
- [ ] .gitignore 已添加 PID 文件
- [ ] 脚本在项目虚拟环境下能正确找到 python
