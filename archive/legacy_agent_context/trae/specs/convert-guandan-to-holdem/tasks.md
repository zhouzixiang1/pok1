# Tasks

- [ ] Task 1: 创建 `holdem_engine/` 核心引擎适配层
  - [ ] Task 1.1: 创建 `holdem_engine/__init__.py` 模块入口
  - [ ] Task 1.2: 创建 `holdem_engine/cards.py` — 卡牌工具（发牌、显示、Card↔int 转换）
  - [ ] Task 1.3: 创建 `holdem_engine/actions.py` — 动作编码与合法性判定
  - [ ] Task 1.4: 创建 `holdem_engine/observation.py` — HoldemObservation 数据类
  - [ ] Task 1.5: 创建 `holdem_engine/round.py` — HoldemRound 类（封装 engine/judge.py 的 Holdem）
  - [ ] Task 1.6: 创建 `holdem_engine/game.py` — HoldemGame 类（多手牌对局管理）

- [ ] Task 2: 创建 `holdem_engine/tests/` 全量测试
  - [ ] Task 2.1: 创建 `test_hand_eval.py` — 牌力判定测试（10+ 用例）
  - [ ] Task 2.2: 创建 `test_round.py` — 下注流程测试（盲注、四轮、动作合法性）
  - [ ] Task 2.3: 创建 `test_game.py` — 完整对局测试（50手、庄家轮转、筹码累积）
  - [ ] Task 2.4: 创建 `test_settlement.py` — 摊牌结算测试（比牌、弃牌、边池）

- [ ] Task 3: 创建 `holdem_ui/` Web 对战 UI
  - [ ] Task 3.1: 创建 `holdem_ui/game_manager.py` — HoldemGameSession 游戏会话管理
  - [ ] Task 3.2: 创建 `holdem_ui/ui_agent.py` — HoldemUIAgent 封装 bot 子进程
  - [ ] Task 3.3: 创建 `holdem_ui/server.py` — FastAPI 后端路由
  - [ ] Task 3.4: 创建 `holdem_ui/static/index.html` — 前端页面
  - [ ] Task 3.5: 创建 `holdem_ui/static/app.js` — 前端逻辑
  - [ ] Task 3.6: 创建 `holdem_ui/static/style.css` — 样式

- [ ] Task 4: 编写设计文档 `docs/holdem_engine_design.md`
  - [ ] Task 4.1: 记录改造点、接口变更、架构图

# Task Dependencies
- Task 1.2-1.4 (cards/actions/observation) 可并行，无依赖
- Task 1.5 (round) 依赖 Task 1.2-1.4
- Task 1.6 (game) 依赖 Task 1.5
- Task 2.1 (hand_eval) 可与 Task 1 并行（直接测试 judge.py 的函数）
- Task 2.2-2.4 依赖 Task 1 完成
- Task 3.1-3.2 依赖 Task 1 完成
- Task 3.3 (server) 依赖 Task 3.1-3.2
- Task 3.4-3.6 (前端) 依赖 Task 3.3
- Task 4 可在 Task 1 完成后开始
