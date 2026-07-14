# 项目理解文档 — 双人德州扑克 AI Bot 自进化框架

## Why
用户希望全面理解项目结构，特别是双人德州扑克（Texas Hold'em）部分的实现方法。本文档梳理整个项目如何用于双人德州扑克 bot 的开发、评估和进化。

---

## 项目整体架构

本项目 (`pok`) 包含三个相对独立的子系统：

| 子系统 | 目录 | 游戏类型 | 核心方法 |
|--------|------|----------|----------|
| **Poker Bot Evolution** | `engine/`, `bots/`, `web/`, `scripts/` | 双人无限制德州扑克 | LLM 驱动的多 Agent 进化管线 |
| **DanLM** | `DanLM/` | 掼蛋 (GuanDan) | 纯强化学习自博弈 |
| **TCP 竞赛服务器** | `sever/` | 双人无限制德州扑克 | TCP 网络对战 |

本文档聚焦 **Poker Bot Evolution** 子系统——如何用 LLM 驱动的进化管线来自动改进双人德州扑克 bot。

---

## 一、双人德州扑克引擎 (`engine/`)

### 1.1 游戏裁判 `engine/judge.py` (577行)

#### 核心类：`Holdem` — 完整德州扑克状态机

**游戏参数**:
- 2 人单挑 (Heads-up)
- 每局 50 手牌，起始筹码 20000
- 小盲 50，大盲 100
- 决策超时 30 秒
- Botzone 游戏 ID: `63dcfaddee1bce5e6c8f4b53`

**牌的表示**:
- 整数 0-51（52 张牌）
- `rank = card // 4 + 2` (2-14, 其中 14=A)
- `suit = card % 4` (0=Heart♥, 1=Diamond♦, 2=Spade♠, 3=Club♣)

**游戏轮次**: `PRE_FLOP=0 → FLOP=1 → TURN=2 → RIVER=3`

**动作编码**:

| 值 | 含义 |
|----|------|
| `0` | Call / Check |
| `-1` | Fold |
| `-2` | All-in |
| `>0` | Raise（增量，即额外加多少筹码） |

**关键**: `engine/judge.py` 使用 **raise-as-increment** 语义。bot 输出 `>0` 表示在当前下注基础上**额外**加多少。

**核心方法 `player_action(bet)`**:
- `FOLD(-1)`: 弃牌，对手直接赢
- `ALLIN(-2)`: 全下，不允许连续两个 allin
- `CALL(0)`: 跟注/过牌，含多种非法检查
  - 翻牌后第一个行为不能 call
  - Preflop BB 在 SB call 后不能再 call
  - 筹码不够时允许全下所有剩余
- `>0 (raise)`: 加注增量，严格验证
  - allin 后不能 raise
  - raise 后总额必须 > 当前最大注
  - raise 金额 = 全部筹码时必须用 allin
  - 最低加注: `raise_to >= last_raise_to * 2`

**牌力评估系统**:
- `hand_type_of_cards(cards)` — 5 张牌的牌型判定（同花顺 > 四条 > 葫芦 > 同花 > 顺子 > 三条 > 两对 > 一对 > 高牌）
- `find_max_hand_type()` — 从 7 张牌中选最优 5 张（C(7,5)=21 种组合）
- `compare_full_cards()` — 完整的两手牌比较，考虑踢脚牌
- `_is_wheel()` — 正确处理 A-2-3-4-5 最小顺子

**Bot 通信协议**:
```python
# 输入 (裁判 -> Bot)
{
    "requests": [...],    # 历史请求列表
    "responses": [...],   # 历史回复列表
    "data": ...           # 可选持久化状态 (bot 自定义)
}

# 输出 (Bot -> 裁判)
{"response": <int action>, "data": ...}
```

**`judge(input_json)` — 无状态裁判函数**:
- 首次调用（空 log）: 初始化牌堆、庄家、盲注
- 后续调用: 从 log 恢复游戏状态，处理 bot 回复
- Bot 崩溃（verdict != "OK"）自动视为弃牌
- 边池计算: `get_player_final_chips()` 正确处理全下时的主池和超额

### 1.2 对战系统 `engine/battle.py` (727行)

#### Bot 进程管理

**两种进程模式**:
1. **`_PersistentBot`** — 持久化进程（一局一个 Popen，行分隔 JSON），性能提升约 2 倍
2. **`_call_bot_subprocess()`** — 每次决策创建新子进程，用于 debug（可捕获 stderr）

#### 核心对战函数

**`battle()`** — 标准对战:
- 两个 bot 对战 n_games 局，每局 50 手
- 支持 verbose/debug 模式
- 返回 `(match_wins, draws, n_played, all_logs)`

**`mirror_battle()`** — 镜像对战（**核心公平性机制**）:
- 每局先正常打一次，再用手牌交换后的镜像牌堆打一次
- **镜像牌堆构造**: `mirror_deck = deck[:-4] + deck[-2:] + deck[-4:-2]`
  - 交换双方底牌，公共牌不变
  - 庄家位置也交换
- **胜负判定**: `net_chips_0 = chips_normal[0] + chips_mirror[0]`
- **效果**: 消除发牌运气因素，纯策略评估

**`battle_generator()`** — 生成器版，逐步 yield 事件，用于实时展示

**`human_battle_generator()`** — 人机对战，通过 `human_sync` 同步人类操作

### 1.3 ELO 排名赛 `engine/ladder.py` (954行)

- **初始分数**: 1200，K=40（前30局）/ K=20（稳定期）
- **段位**: 王者(2000+) > 大师 > 钻石 > 铂金 > 黄金 > 白银 > 青铜(<1000)
- **循环赛**: N 个 bot 双向循环，共 N*(N-1) 场
- **并行**: 多 worker 独立子进程执行
- **断点续跑**: `checkpoint.json` 保存/恢复

### 1.4 锚点基准测试 `engine/anchor_runner.py` (642行)

- 指定一个"锚点"bot，与所有其他 bot 进行镜像对战
- `ProcessPoolExecutor` 并行执行
- 评估特定 bot 的全面实力

---

## 二、Bot 实现 (`bots/`)

### 2.1 Bot 结构

每个 bot 是模块化的多文件 Python 包（stdlib only）：

```
botN/
├── main.py         # 入口：读 stdin JSON → 调用策略 → 输出 stdout JSON
├── constants.py    # 常量（盲注、模拟次数、阈值）
├── card_utils.py   # 牌力工具函数
├── state.py        # 游戏状态重建
├── strategy.py     # 核心策略逻辑
├── postflop.py     # 翻牌后策略
├── simulation.py   # 蒙特卡洛模拟
├── opponent.py     # 对手建模
└── tournament.py   # 锦标赛压力调整
```

### 2.2 Bot 入口点模式

```python
def main():
    payload = json.loads(input())
    requests = payload["requests"]
    req = dict(requests[-1])
    action = get_action(req, requests)
    state = reconstruct_state(req)
    action = sanitize_action(action, state, req["my_chips"])
    print(json.dumps({"response": int(action)}))
```

### 2.3 `sanitize_action()` — 动作合法性过滤

所有 bot 必须在输出前调用，确保动作合法：
- 对手 allin 后只允许 fold/allin
- 筹码不够跟注时转为 fold/allin
- raise 金额 >= 全部筹码时转为 allin
- raise 金额不满足最低要求时转为 call/fold

### 2.4 策略差异

| Bot | 模拟次数 | 特色 |
|-----|----------|------|
| bot1 | 500/700/900 | 基础蒙特卡洛 |
| bot5 | 900/1200/1500 | Chen 公式预计算表(169手牌)、反锁定压力、特定对手检测 |
| bot6 | 类似 bot5 | 增强的后位策略 |

### 2.5 LLM 进化 Bot (claude_v2-vN)

与 bot1-bot6 共享相同的模块化结构，但策略代码由 LLM 生成和迭代优化。`.completed` 哨兵文件标记该版本已完成进化。

---

## 三、LLM 驱动的进化管线 (`web/core/`)

这是本项目的**核心创新**——用 LLM Agent 协作来自动改进德州扑克 bot。

### 3.1 进化管线架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    每代进化周期 (Generation Cycle)                │
│                                                                  │
│  Phase 1: prepare_generation()                                   │
│    ├─ 停滞检测 (Stagnation Detection)                            │
│    ├─ 策略决策: master (从祖先进化) or crossover (交叉两个父代)    │
│    └─ 创建 GenerationContext                                    │
│                                                                  │
│  Phase 2: _run_one_cycle() — LLM 驱动                            │
│    ├─ Direction Auditor: 检查方向重复                             │
│    ├─ Master Architect: 分析数据，规划 Worker 任务                │
│    ├─ Workers (×2-3, 并行): 执行代码修改                         │
│    ├─ Quality Gates: 编译+烟雾测试+决策测试                      │
│    ├─ Reviewer: 代码审查评分                                     │
│    ├─ Critic: 战略质量评估                                       │
│    ├─ Pre-commit Eval: 镜像对战回归检查                          │
│    └─ Commit: Git commit + bot-v{N} tag                         │
│                                                                  │
│  Phase 3: post_generation_cleanup()                              │
│    ├─ 收割最弱 bot (池 > 30 时)                                  │
│    └─ 整理经验池 (每3代)                                         │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 LLM Agent 角色

| Agent | 工具 | 职责 |
|-------|------|------|
| **Orchestrator** | MCP tools only | 驱动管线，决定进化流程 |
| **Master Architect** | Bash, Read | 分析评分/比赛/经验池，规划 Worker 任务 |
| **Workers** (×2-3) | Bash, Read, Edit | 修改 bot 源代码 |
| **Reviewer** | Bash, Read | 审查代码变更，评分 1-10 |
| **Critic** | Bash, Read | 独立战略评估，≥6 通过 |
| **Direction Auditor** | None | 检测进化方向重复 |
| **Stagnation Analyst** | None | 分析评分趋势停滞 |

### 3.3 Worker 角色边界（严格隔离）

| 角色 | 允许 | 禁止 |
|------|------|------|
| **Algorithmic Logic Architect** | 新函数、重构逻辑、新 import | 改数值常量 |
| **Hyperparameter Tuner** | 数值常量、阈值 | 新函数、类、import、控制流 |
| **Opponent Modeler** | 每街追踪、下注模式 | 决策流程修改 |

边界由 `_validate_worker_boundaries()` 在每次 Worker 运行后检查。

### 3.4 Master Architect 的工作流程

1. **输入数据**: Glicko-2 评分、H2H 胜负矩阵、bot_stats、经验池、源代码
2. **分析**: 识别当前 bot 的弱点和对手的克制策略
3. **规划**: 生成 JSON 任务计划，分配给 2 个 Worker（一个逻辑架构师 + 一个参数调优师）
4. **约束**: 每个 worker_prompt 必须在 2000 字符内，需要具体代码骨架

### 3.5 质量门控 (Quality Gates)

| 层级 | 检查 | 标准 |
|------|------|------|
| 编译检查 | `py_compile` | 无语法错误 |
| 烟雾测试 | 1 局镜像对战 vs 参考bot | 不崩溃 |
| 决策测试 | 15 个预定义场景 | ≥70% 通过率 |
| 文件大小 | 行数检查 | 核心策略 ≤1500 行，辅助 ≤1200 行 |
| 代码审查 | Reviewer LLM | 评分 ≥7 |
| 战略审查 | Critic LLM | 评分 ≥6 |
| 回归检查 | 镜像对战 | 不劣于父代 |

### 3.6 决策测试场景 (`test_scenarios.json`)

15 个预定义的扑克场景，检测灾难性错误：

| 场景 | 禁止动作 |
|------|----------|
| SB 拿 AA，首次行动 | 禁止 fold |
| SB 拿 7-2 杂色 | 禁止 allin |
| 翻牌 AAA 三头 | 禁止 fold |
| 河牌坚果同花面对下注 | 禁止 fold |
| 河牌未中听牌面对大注 | 必须 fold |
| BB 拿 AKs 面对全下 | 禁止 fold |
| SB 拿 JJ 面对 3bet | 禁止 fold |

### 3.7 经验池 (`experience_pool.md`)

记录从过去迭代中学到的战略教训：
- **OPPONENT_MODELING**: 对手追踪数据必须接入决策逻辑
- **POSTFLOP_STRATEGY**: 翻牌后弃牌门 + EQR 收紧
- **PARAMETER_TUNING**: 翻牌后尺寸比例已调优
- **GENERAL**: Worker 角色边界至关重要

### 3.8 评分系统

**Glicko-2** (进化系统使用):
- 默认: r=1500, rd=350, sigma=0.06
- 保守评分 = `r - 2*rd`（95% 置信下界）
- RD 置信度: <50 绿, 50-100 黄, 100-200 橙, >200 红
- 后台守护进程持续运行镜像对战，逐局更新评分

**ELO** (天梯赛使用):
- 初始 1200, K=40/20
- 段位划分

### 3.9 后台守护进程 (`elo_daemon.py`)

- 持续运行镜像对战（`ProcessPoolExecutor`）
- 对战选择: 60% 低评估对 + 40% 评分多样性对
- 逐局 Glicko-2 更新
- 写入所有结果文件（`fcntl` 文件锁保证并发安全）
- 回放文件上限 200 个
- 支持 `.reap_signal` 信号立即刷新 bot 列表

---

## 四、Bot 与平台集成

### 4.1 Botzone 平台集成 (`scripts/botzone_upload_match.py`)

**完整的 Botzone 客户端** (2580行)：
- 登录（含验证码识别）
- 上传 bot 源码（base64 编码，最大 4MB）
- 启动排名赛
- 创建游戏房间 + 批量房间系列赛
- Socket.IO 轮询客户端获取实时结果
- 比赛日志解析（每手牌决策、筹码变化）
- CSV 导出

**凭据**: `BOTZONE_EMAIL` / `BOTZONE_PASSWORD` 环境变量

### 4.2 TCP 竞赛服务器 (`sever/`)

独立的 TCP 网络对战平台（git submodule）：
- TCP :10001 + Web :18080
- 70 手/局，60 秒超时
- **13 条严格规则验证器**（非法动作 = 自动弃牌）
- 有状态 `GameEngine` 对象

**与 `engine/judge.py` 的关键差异**:

| 方面 | `engine/judge.py` | `sever/` |
|------|--------------------|----------|
| 通信 | 子进程 JSON (stdin/stdout) | TCP 文本行 |
| 牌格式 | 整数 0-51 | `<suit,rank>` 元组 |
| 花色映射 | ♥=0, ♦=1, ♠=2, ♣=3 | ♠=0, ♥=1, ♦=2, ♣=3 |
| Raise 语义 | **增量** (raise-as-increment) | **总额** (raise-to-total) |
| 动作格式 | 整数 (-2,-1,0,>0) | 文字 ("call","fold","raise 200") |
| 最低加注 | `≥ round_raise * 2` | `≥ last_raise_to * 2` |

`bot_adapter.py` 桥接两种系统，但存在花色映射和 raise 语义的差异。

---

## 五、数据流全景

```
Workers 编辑 bots/claude_v{N}/  (LLM 驱动的代码修改)
        │
elo_daemon.py  ← 后台子进程，持续运行镜像对战
        │           ProcessPoolExecutor，逐局 Glicko-2 更新
        │
web/core/results/
  ├── glicko_ratings.json    ← Glicko-2 评分 (fcntl 锁)
  ├── rating_history.jsonl   ← 定期评分快照
  ├── head_to_head.json      ← 胜负矩阵
  ├── bot_stats.json         ← 聚合统计
  ├── match_history.jsonl    ← 比赛摘要
  ├── match_replay/          ← 完整回放 (≤200)
  ├── pipeline_state.json    ← 管线检查点
  └── llm_costs.jsonl        ← LLM 成本
        │
FastAPI 后端 (读取文件，fcntl.LOCK_SH + 2s TTL 缓存)
        │
两个 SSE 流:
  /api/data/stream      ← 定期轮询 (3s/10s/15s)
  /api/evolution/stream ← 事件驱动 (LLM 输出实时流)
        │
React 前端仪表盘 (10 个页面)
```

---

## 六、关键设计决策总结

### 6.1 为什么选择 LLM 进化而非 RL

- 德州扑克 bot 的策略空间巨大，传统 RL（如 DanZero 对掼蛋）需要大量训练资源
- LLM 可以理解代码语义，进行有针对性的改进
- 利用 Glicko-2 评分和 H2H 数据作为反馈信号
- 经验池积累战略知识，避免重复探索

### 6.2 公平性保证

- **镜像对战**: 交换底牌，消除发牌运气
- **净筹码差判定**: 综合正反两局结果
- **Glicko-2 RD**: 量化评分不确定性，避免过早下结论

### 6.3 进化质量保证

- 多层质量门控（编译→烟雾→决策→审查→批评→回归）
- Worker 角色边界防止无意义的改动
- Direction Auditor 防止进化方向重复
- 停滞检测自动切换策略（从 master 到 crossover）

---

## Impact
- 本文档为纯知识梳理，不影响任何代码
- 无 breaking changes

## ADDED Requirements
无（纯文档，不做代码修改）

## MODIFIED Requirements
无

## REMOVED Requirements
无
