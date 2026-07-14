# 掼蛋引擎改造为德州扑克引擎 Spec

## Why
项目已有一个完整的掼蛋 RL 系统（DanLM），包含 Web 对战 UI、评估框架、并行探索等基础设施。同时 `engine/judge.py` 已有完整的德州扑克规则引擎。将掼蛋引擎改造为德州扑克，可以复用 DanLM 的可扩展架构（Web UI、Agent 系统、评估框架），结合已有的德州扑克规则引擎和 LLM 进化管线，实现德州扑克 bot 的自我进化。

## What Changes

### 新增文件

1. **`holdem_engine/`** — 德州扑克引擎适配层（纯 Python）
   - `round.py` — `HoldemRound` 类，封装 `Holdem` 提供类 `GuanDanRound` 接口
   - `game.py` — `HoldemGame` 类，管理多手牌对局（筹码、庄家轮转）
   - `cards.py` — 卡牌工具（发牌、显示、牌力查询）
   - `actions.py` — 动作编码与合法性判定
   - `observation.py` — `HoldemObservation` 数据类

2. **`holdem_ui/`** — 德州扑克 Web 对战 UI
   - `server.py` — FastAPI 后端（游戏路由）
   - `game_manager.py` — `HoldemGameSession` 游戏会话管理
   - `ui_agent.py` — `HoldemUIAgent` 封装 bot 子进程
   - `static/index.html` — 前端页面
   - `static/app.js` — 前端逻辑
   - `static/style.css` — 样式

3. **`holdem_engine/tests/`** — 测试用例
   - `test_hand_eval.py` — 牌力判定测试
   - `test_round.py` — 下注流程测试
   - `test_game.py` — 完整对局测试
   - `test_settlement.py` — 摊牌结算测试

4. **`docs/holdem_engine_design.md`** — 设计文档

### 依赖的已有文件（不修改，直接复用）

- `engine/judge.py` — 德州扑克完整规则引擎（`Holdem` 类、牌力评估、`judge()` 函数）
- `engine/battle.py` — 对战系统（`mirror_battle`、`_PersistentBot`）
- `engine/ladder.py` — ELO 天梯系统
- `web/core/glicko2.py` — Glicko-2 评分
- `bots/` — 现有德州扑克 bot（bot1-bot6, claude_vN）

### 不做的事

- 不修改 `engine/judge.py`、`engine/battle.py` 等已有的德州扑克引擎代码
- 不修改 `DanLM/danzero/` 下的编译后 `.so` 文件
- 不修改 `web/` 下的 LLM 进化管线代码（改造后的引擎通过 bot 子进程接口接入现有进化系统）
- 不实现 RL 训练循环（利用现有 LLM 进化管线驱动 bot 改进）

## Impact
- Affected specs: 新增独立的德州扑克 Web 对战模块
- Affected code: 新建 `holdem_engine/` 和 `holdem_ui/` 目录，复用 `engine/` 和 `bots/`

## ADDED Requirements

### Requirement: HoldemRound — 单手牌德州扑克引擎

系统 SHALL 提供 `HoldemRound` 类，封装 `engine/judge.py` 的 `Holdem` 类，提供清晰的 API：

```python
class HoldemRound:
    def __init__(self, player_chips, dealer_idx, small_blind=50, big_blind=100, deck=None)
    def get_observation() -> HoldemObservation
    def step(action: int) -> HoldemObservation | None  # action: 0=call, -1=fold, -2=allin, >0=raise增量
    @property done -> bool
    @property pot -> int
    @property public_cards -> list[Card]
    @property player_cards -> list[list[Card]]
    @property winner -> list[int] | None  # 手牌结束时获胜玩家
```

#### Scenario: 正常一手牌流程
- **WHEN** 创建 `HoldemRound(player_chips=[20000, 20000], dealer_idx=0)`
- **AND** 依次执行盲注、四轮下注（preflop → flop → turn → river）
- **THEN** `done=True`，`winner` 包含获胜玩家，筹码正确分配

#### Scenario: 一方弃牌
- **WHEN** 玩家在任意阶段执行 fold (-1)
- **THEN** `done=True`，对手获胜，奖池归对手

#### Scenario: 双方全下
- **WHEN** 一方 allin 后另一方也 allin
- **THEN** 直接发完所有公共牌，摊牌比大小，正确计算边池

### Requirement: HoldemObservation — 观察数据

系统 SHALL 提供 `HoldemObservation` 数据类：

```python
@dataclass
class HoldemObservation:
    player: int                    # 当前行动玩家 (0 或 1)
    legal_actions: list[dict]      # 合法动作列表 [{action: int, label: str, min_raise: int, max_raise: int}]
    stage: str                     # "preflop" / "flop" / "turn" / "river"
    pot: int                       # 当前奖池
    player_chips: list[int]        # 双方筹码
    player_bets: list[int]         # 本阶段下注
    hole_cards: list[Card]         # 当前玩家手牌
    public_cards: list[Card]       # 公共牌
    is_new_stage: bool             # 是否进入新阶段
```

#### Scenario: Preflop SB 行动
- **WHEN** 盲注已下，轮到 SB (player=0) 行动
- **THEN** `legal_actions` 包含 fold、call、raise、allin，`stage="preflop"`，`public_cards=[]`

### Requirement: HoldemGame — 多手牌对局管理

系统 SHALL 提供 `HoldemGame` 类管理完整的多手牌对局：

```python
class HoldemGame:
    def __init__(self, starting_chips=20000, small_blind=50, big_blind=100, hands_per_game=50)
    def new_hand(deck=None) -> HoldemRound  # 开始新一手牌
    def finish_hand(round: HoldemRound) -> bool  # 结束一手牌，返回对局是否结束
    @property game_over -> bool
    @property winner -> int  # 对局获胜者
    @property hand_count -> int
    @property player_chips -> list[int]
```

#### Scenario: 完整对局
- **WHEN** 运行 50 手牌后一方筹码 > 另一方
- **THEN** `game_over=True`，`winner` 为筹码多的玩家

### Requirement: 卡牌与动作工具模块

系统 SHALL 提供 `holdem_engine/cards.py` 和 `holdem_engine/actions.py`：

- `cards.py`: `deal_deck(seed)`, `card_to_display(card)`, `card_to_int(card)`, `int_to_card(i)`
- `actions.py`: `is_legal_action(game_state, action)`, `get_legal_actions(game_state)`, `action_to_label(action, state)`

### Requirement: HoldemGameSession — Web 对战会话

系统 SHALL 提供 `HoldemGameSession` 类（在 `holdem_ui/game_manager.py`）：

- 管理单局或完整对局的生命周期
- Phase 状态机: `idle → playing → hand_over → game_over`
- 人类出牌: `play_action(action)` → 更新状态
- AI 自动行动: `advance_one_ai()` → 调用 bot 子进程
- 状态序列化: `to_state_json()` → 前端 JSON

#### Scenario: 人机对战一手牌
- **WHEN** 人类选择 call，AI 选择 raise
- **THEN** 状态正确更新，轮次推进，最终正确结算

### Requirement: HoldemUIAgent — Bot 封装

系统 SHALL 提供 `HoldemUIAgent` 类（在 `holdem_ui/ui_agent.py`）：

- 封装 `_PersistentBot` 或 `_call_bot_subprocess`
- 提供 `select_play(observation, round_obj)` 接口
- 支持从文件路径加载任意德州扑克 bot

### Requirement: Web 游戏后端

系统 SHALL 在 `holdem_ui/server.py` 提供 FastAPI 路由：

| 端点 | 方法 | 功能 |
|------|------|------|
| `/api/new-game` | POST | 创建新游戏（指定 bot、模式） |
| `/api/state` | GET | 获取当前状态 |
| `/api/play` | POST | 人类出牌 |
| `/api/action-options` | GET | 获取合法动作列表 |
| `/api/hint` | POST | 切换 AI 提示 |

### Requirement: Web 游戏前端

系统 SHALL 在 `holdem_ui/static/` 提供德州扑克游戏前端：

- 2人对位显示（人类在下方，AI 在上方）
- 手牌显示（2张，带花色符号）
- 公共牌区域（0-5张，按阶段逐步显示）
- 奖池和筹码显示
- 操作按钮（Fold / Call / Raise滑块 / All-in）
- 对局历史记录

### Requirement: 全量测试覆盖

系统 SHALL 提供以下测试：

#### test_hand_eval.py — 牌力判定测试
- 皇家同花顺 > 同花顺 > 四条 > 葫芦 > 同花 > 顺子 > 三条 > 两对 > 一对 > 高牌
- 同牌型下踢脚牌比较
- A-2-3-4-5 最小顺子
- 7 张牌选最优 5 张

#### test_round.py — 下注流程测试
- Preflop 盲注正确扣除
- 四轮下注正确推进
- Call/Fold/Raise/All-in 动作正确执行
- 最低加注规则
- 边池计算

#### test_game.py — 完整对局测试
- 50 手牌庄家轮转
- 筹码累积正确
- 对局结束条件

#### test_settlement.py — 摊牌结算测试
- 双方摊牌比大小
- 单方弃牌对手赢
- 全下边池计算

## MODIFIED Requirements
无

## REMOVED Requirements
无
