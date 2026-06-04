# 远程部署教程

## 1. 克隆项目

```bash
git clone --recurse-submodules https://github.com/zhouzixiang1/pok1.git
cd pok1
```

> 如果忘记加 `--recurse-submodules`，克隆后执行：
> ```bash
> git submodule update --init --recursive
> ```

## 2. 环境准备

### Python（需要 3.11+）

```bash
# 推荐使用 conda 或 venv
conda create -n pok python=3.13 -y
conda activate pok
# 或者
python -m venv venv && source venv/bin/activate
```

### Python 依赖

```bash
pip install -r web/requirements.txt
pip install claude-agent-sdk numpy
```

### Node.js（前端需要，需要 18+）

```bash
# macOS
brew install node

# Ubuntu/Debian
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
```

## 3. 前端构建（如需 Web Dashboard）

```bash
cd web/frontend
npm install
npm run build        # 构建到 web/server/static/
cd ../..
```

> 开发模式可用 `npm run dev`（端口 5173，自动代理 API 到 8000）

## 4. 启动方式

### 方式 A：完整 Web 服务（推荐）

```bash
# 启动全栈：Orchestrator + Daemon + Frontend（端口 8000）
python web/main.py

# 自定义端口
python web/main.py --port 3000

# 不启动后台对弈守护进程（仅 Web UI）
python web/main.py --no-daemon

# 跳过前端构建（已构建过时）
python web/main.py --no-build

# 开发模式（uvicorn 自动重载）
python web/main.py --dev
```

浏览器打开 `http://<服务器IP>:8000` 即可看到 Dashboard。

### 方式 B：仅后台进化（无 Web UI）

```bash
# 持续进化
python web/core/orchestrator.py

# 跑一代就停
python web/core/orchestrator.py --one-gen
```

### 方式 C：仅 Glicko-2 对弈评测

```bash
python web/core/elo_daemon.py --workers 14 --pairs 5 -v
```

## 5. 本地对战测试

```bash
# 两个 bot 对战 50 局
python engine/battle.py bots/claude_v5/main.py bots/claude_v4/main.py -n 50 -v

# 镜像对战（消除运气因素）
python engine/battle.py bots/claude_v5/main.py bots/claude_v5/main.py -n 10

# 全量天梯赛（所有 bot 循环对战）
python engine/ladder.py -n 20 -v

# 指定 bot 天梯
python engine/ladder.py -b 1 4 5 6 -n 20 -j 4
```

## 6. Botzone 上传

```bash
# 设置环境变量
export BOTZONE_EMAIL="your@email.com"
export BOTZONE_PASSWORD="your_password"

# 上传 bot
python scripts/botzone_upload_match.py upload --source bots/claude_v5/main.py --bot-name test --execute

# 排位赛
python scripts/botzone_upload_match.py rank-match --bot-name test --execute
```

## 7. Claude Code 集成（进化系统需要）

进化系统使用 `claude-agent-sdk` 调用 Claude API。需要：

1. **Claude Code 已登录**：
   ```bash
   claude login
   ```

2. 或者设置 API Key：
   ```bash
   export ANTHROPIC_API_KEY="sk-ant-..."
   ```

## 8. TCP 竞赛服务器（可选）

```bash
cd sever
python main.py              # TCP :10001 + Web :18080

# 用 bot_adapter 桥接本地 bot 到 TCP 服务器
python bot_adapter.py --bot ../bots/claude_v5 --name test

# TCP 服务器测试
python -m pytest tests/ -v
```

## 常用命令速查

| 场景 | 命令 |
|------|------|
| 启动全栈 | `python web/main.py` |
| 仅进化 | `python web/core/orchestrator.py` |
| 仅评测 | `python web/core/elo_daemon.py --workers 14 -v` |
| 快速对战 | `python engine/battle.py bots/claude_v5/main.py bots/claude_v4/main.py -n 10` |
| 天梯赛 | `python engine/ladder.py -n 20 -v` |
| 前端开发 | `cd web/frontend && npm run dev` |
| 后端测试 | `cd web && python -m pytest tests/ -v` |
| 合并 bot 文件 | `python merge_bot.py bots/claude_v5/` |
| 重置进化 | `python scripts/reset_evolution.py` |

## 目录结构

```
pok/
├── bots/                # 所有 bot（claude_v1~v6 + neural_bot）
├── engine/              # 本地对战引擎（battle.py, ladder.py, judge.py）
├── sever/               # TCP 竞赛服务器（git 子模块）
├── web/                 # Web 全栈
│   ├── main.py          # 统一入口
│   ├── core/            # 后端核心（进化、评测、工具）
│   ├── server/          # FastAPI 路由
│   └── frontend/        # React 前端
├── scripts/             # 工具脚本（Botzone、重置等）
├── merge_bot.py         # 多文件 bot 合并为单文件
└── CLAUDE.md            # Claude Code 项目指令
```
