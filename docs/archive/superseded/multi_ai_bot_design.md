# Multi-AI Iterative Bot Evolution 设计文档

## 1. 概述与愿景

### 1.1 核心思路

利用多个 AI 大模型（Claude、GPT、Gemini、DeepSeek 等）各自独立设计德州扑克 Bot，然后通过本地对战系统进行公平竞赛，将比赛结果结构化反馈给各 LLM，驱动下一轮迭代改进。经过多轮进化，各 Bot 的策略水平不断提升，最终收敛出"超级 Bot"。

```
┌─────────────────────────────────────────────────────────────┐
│                    Evolution Loop                            │
│                                                              │
│   ┌──────────┐    ┌──────────┐    ┌──────────┐             │
│   │ Claude   │    │   GPT    │    │  Gemini  │  ...         │
│   │ generates│    │ generates│    │ generates│              │
│   │  bot     │    │  bot     │    │  bot     │              │
│   └────┬─────┘    └────┬─────┘    └────┬─────┘             │
│        │               │               │                     │
│        ▼               ▼               ▼                     │
│   ┌────────────────────────────────────────┐                │
│   │         本地对战评估                      │                │
│   │    mirror_battle + ELO 天梯              │                │
│   └────────────────────┬───────────────────┘                │
│                        │                                     │
│                        ▼                                     │
│   ┌────────────────────────────────────────┐                │
│   │      对战日志分析                        │                │
│   │  VPIP / PFR / 弃牌率 / 关键失误手牌     │                │
│   └────────────────────┬───────────────────┘                │
│                        │                                     │
│                        ▼                                     │
│   ┌────────────────────────────────────────┐                │
│   │      结构化反馈 → 各 LLM                 │                │
│   │   "你在翻牌圈弃牌过多，面对加注弃牌率     │                │
│   │    达 62%，建议降至 45% 以下"             │                │
│   └────────────────────┬───────────────────┘                │
│                        │                                     │
│          ┌─────────────┘                                     │
│          ▼                                                   │
│     下一轮迭代 (回到顶部)                                      │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 为什么用多个 LLM

不同 AI 的架构和训练数据不同，导致它们的策略偏好各异：

| LLM | 可能的策略偏好 |
|-----|--------------|
| Claude | 擅长长链推理，可能偏向 GTO 均衡策略 |
| GPT | 强于模式匹配，可能擅长对手建模和剥削 |
| Gemini | 多模态理解，可能在牌面纹理分析上有优势 |
| DeepSeek | 代码能力强，可能实现更复杂的计算逻辑 |

这种多样性是进化的关键——不同策略方向并行探索，比单一路径更高效。

### 1.3 收敛假设

两人无限注德州扑克存在纳什均衡。迭代竞争 + 结构化反馈应该驱动 Bot 趋向纳什均衡策略，每个 LLM 探索策略空间的不同区域，最终收敛到接近最优的打法。

### 1.4 与现有项目的关系

项目中 `bots/bot1/main.py` 到 `bots/bot6/main.py` 展示了单 LLM（Claude）的迭代进化路径：

- **bot_1**: 基础蒙特卡洛模拟 + 对手建模 + 牌面纹理分析
- **bot_2**: 增加翻前 169 手牌表、概念漂移检测、3Bet/4Bet 逻辑
- **bot_3**: 多风格组合（GTO/剥削紧/剥削松/剥削疯/剥削弱）+ EXP3 元学习器
- **bot_4**: 进一步精炼
- **bot_5**: 河牌精确枚举、超池下注、反特定 Bot 剥削、增强诈唬
- **bot_6**: 最新迭代

这套多 AI 方案将此模式推广为并行进化系统。

---

## 2. 系统架构

### 2.1 五阶段循环

```
阶段 1: LLM Bot 生成
    各 LLM 根据协议规范 + 策略知识 + 反馈生成 bot 代码

阶段 2: 注册
    新 Bot 放入 bots/ 目录，通过代码验证管线

阶段 3: 评估
    调用 ladder.py 运行天梯赛，或用 anchor_runner.py 做锚点测试

阶段 4: 分析
    解析对战日志，提取策略指标和关键失误

阶段 5: 反馈
    构造结构化反馈，传回各 LLM，进入下一轮
```

### 2.2 现有可复用组件

| 组件 | 文件 | 功能 |
|------|------|------|
| 游戏引擎 | `engine/judge.py` | `Holdem` 类：完整的牌局状态机，牌面用整数 0-51 表示，动作编码 `0`=跟注/过牌、`-1`=弃牌、`-2`=全下、`>0`=加注额 |
| 对战引擎 | `engine/battle.py` | `mirror_battle()` 镜像对战（交换底牌消除运气）、子进程执行、30s 超时 |
| 天梯赛 | `engine/ladder.py` | 循环赛 + ELO 排名（初始 1200，K=40/20），支持多进程并行和断点续跑 |
| 锚点测试 | `engine/anchor_runner.py` | 一个 Bot vs 所有其他 Bot，镜像对并行执行 |
| Botzone 集成 | `scripts/botzone_upload_match.py` | 上传代码、创建房间、启动比赛、归档日志 |

### 2.3 需要新增的组件

```
scripts/
  evolution_manager.py      # 进化管理器：编排全流程
  battle_analyzer.py        # 对战日志分析 + 结构化反馈生成
  llm_interface/
    __init__.py
    base.py                 # 抽象 LLM 接口
    claude_adapter.py       # Claude API
    openai_adapter.py       # GPT API
    gemini_adapter.py       # Gemini API
  prompt_templates/
    initial_generation.md   # 首轮生成 Prompt
    iteration_improve.md    # 迭代改进 Prompt
    strategy_primer.md      # 扑克策略知识库
evolution_results/          # 进化运行结果
  run_001/
    config.json
    round_001/
      claude_v1.py          # Bot 快照
      gpt_v1.py
      ladder_report.json    # ELO 排名
      feedback/
        claude_feedback.md  # 结构化反馈
        gpt_feedback.md
    round_002/
      ...
```

---

## 3. Bot 设计接口

### 3.1 协议规范

每个 LLM 生成的 Bot 必须遵守以下协议（从 `judge.py` 和 `battle.py` 提取）：

**输入格式（通过 stdin 接收 JSON）：**
```json
{
  "requests": [
    {
      "num_players": 2,
      "dealer_id": 0,
      "my_id": 0,
      "my_chips": 20000,
      "my_cards": [12, 35],
      "public_cards": [3, 22, 48],
      "history": [
        {"round": 0, "player_id": 0, "action": 100, "action_type": "raise"},
        {"round": 0, "player_id": 1, "action": 0, "action_type": "call"}
      ],
      "hand": 0,
      "max_hand": 50,
      "total_win_chips": [0, 0],
      "total_win_games": [0, 0]
    }
  ],
  "responses": [100],
  "data": null
}
```

**输出格式（通过 stdout 输出 JSON）：**
```json
{"response": 0}
```

**动作编码：**

| 值 | 含义 |
|----|------|
| `0` | 跟注 / 过牌（call / check） |
| `-1` | 弃牌（fold） |
| `-2` | 全下（all-in） |
| `>0` | 加注金额（raise），须 >= `round_raise` 且 <= `chips - 1` |

**牌面表示：**
- 整数 0–51
- 点数 = `card // 4 + 2`（2–14，14 = Ace）
- 花色 = `card % 4`（0=红桃、1=方块、2=黑桃、3=梅花）

**游戏参数：**
- 2 人无限注德州扑克
- 每局 50 手牌，每手初始筹码 20000
- 小盲 50，大盲 100
- 每次决策限时 30 秒（子进程超时则判负）

### 3.2 上下文包

每个 LLM 应接收以下上下文：

1. **协议规范**（上述内容）
2. **游戏规则**（四轮下注：翻前/翻牌/转牌/河牌；手牌评估顺序）
3. **现有 Bot 示例**（提供 `bot1/main.py` 的关键函数作为参考）
4. **策略知识库**（第 8 节的内容）
5. **对手数据**（后续迭代中：对手 ELO、观察到的 VPIP/PFR 等统计）
6. **历史战绩**（后续迭代中：输给了哪些类型的对手、具体失误手牌）

### 3.3 代码质量要求

- 单文件 Python 脚本，无外部依赖（仅标准库：`json`、`random`、`itertools`、`bisect`、`math`、`collections`）
- 用 `.get(key, default)` 访问所有字段，不能假设字段一定存在
- 处理边界情况：首手（无历史）、全下局面、对手崩溃（视为弃牌）
- 30 秒内必须输出 JSON，不得有多余 stdout 输出
- 蒙特卡洛模拟次数需有上限（现有 Bot 使用 500–900 次）

### 3.4 最小可用 Bot 模板

```python
import json
import random
import itertools

def evaluate_5(cards):
    """评估 5 张牌的牌型，返回可比较的元组"""
    ranks = sorted((c // 4 + 2 for c in cards), reverse=True)
    suits = [c % 4 for c in cards]
    # ... 手牌评估逻辑 ...
    return (0,)  # 高牌

def evaluate_best(cards):
    """从 N 张牌中选最优 5 张"""
    best = None
    for combo in itertools.combinations(cards, 5):
        score = evaluate_5(list(combo))
        if best is None or score > best:
            best = score
    return best

def get_action(req, requests):
    """核心决策函数，返回动作值"""
    my_cards = req["my_cards"]
    public_cards = req.get("public_cards", [])
    # ... 策略逻辑 ...
    return 0  # 默认过牌/跟注

def main():
    payload = json.loads(input())
    requests = payload["requests"]
    req = dict(requests[-1])
    action = get_action(req, requests)
    print(json.dumps({"response": int(action)}))

if __name__ == "__main__":
    main()
```

---

## 4. Prompt 工程

### 4.1 首轮生成 Prompt 结构

```
## 任务
设计一个两人无限注德州扑克 AI Bot。输出一个完整的 Python 文件。

## 游戏协议
[完整的协议规范，见第 3.1 节]

## 策略要求
你的 Bot 应包含以下模块：
1. 翻前手牌强度评估
2. 对手行为建模（基于历史操作统计）
3. 牌面纹理分析（同花/顺子/对子可能性）
4. 胜率估算（蒙特卡洛模拟或精确计算）
5. 下注尺度决策（基于胜率、位置、底池赔率）
6. 比赛级策略（领先时保守、落后时激进）

## 代码约束
- 单文件，无外部依赖，30s 超时
- 输出严格是 {"response": int} JSON
- 参考代码骨架见附录

## 附录：参考代码
[bot1/main.py 的关键函数摘要]
```

### 4.2 迭代改进 Prompt 结构

```
## 任务
改进你的德州扑克 Bot。

## 你的上一版代码
[完整代码]

## 战绩分析
- 总战绩：142 胜 138 负 20 平（胜率 47.3%）
- ELO：1185（下降 15）
- 平均筹码差：-45/局

## 对手分析
| 对手 | 风格 | 胜负 | 胜率 | 筹码差 |
|------|------|------|------|--------|
| opponent_A | 激进型 | 22-28 | 44% | -120 |
| opponent_B | 被动型 | 30-20 | 60% | +80 |

## 关键弱点
1. 面对河牌圈加注弃牌过多（62%，建议降至 45%）
2. 翻前 3Bet 频率不足（8%，建议提升至 15%）
3. 对子面牌面诈唬检测能力弱

## 典型失误手牌
[3-5 手关键对局的完整日志]

## 改进指令
保持已验证有效的策略，重点改进上述弱点。
```

### 4.3 LLM 特化适配

| LLM | Prompt 优化方向 |
|-----|----------------|
| Claude | 提供详细架构指导 + 分步骤推理指令，擅长长上下文理解 |
| GPT | 提供完整代码示例 + 格式演示，对少样本学习敏感 |
| Gemini | 提供更严格的结构约束 + 格式模板，确保输出格式正确 |
| DeepSeek | 可以处理更长上下文，适合提供更多参考代码 |

---

## 5. 评估管线

### 5.1 三层评估

```
快速评估（开发期）
  battle() 10-20 局标准对战
  用于：快速验证代码能跑、基本策略可用

标准评估（迭代期）
  mirror_battle() 50 局镜像对战
  用于：每轮迭代后的正式评估
  优势：交换底牌消除运气偏差

完整评估（阶段性）
  ladder.py 全 Bot 循环赛
  用于：每 3 轮迭代后的全面排名
  输出：ELO 排名 + 对战矩阵 + 详细统计
```

### 5.2 镜像对战原理

每场对局打两次：

1. **正局**：正常发牌，Bot A 和 Bot B 各拿自己的底牌
2. **镜像局**：交换双方底牌（牌堆最后 4 张重排），同一组公共牌

胜负按两局筹码净差判定：如果正局 Bot A 赢 3000、镜像局 Bot A 输 1000，则 Bot A 净赢 2000，判 Bot A 胜。这消除了底牌运气带来的偏差，使评估更加公平。

（实现在 `battle.py` 的 `mirror_battle()` 函数中。）

### 5.3 ELO 排名系统

项目使用标准 ELO 系统（实现在 `engine/ladder.py`）：

- 初始积分：1200
- K 因子：前 30 局 K=40（快速调整），之后 K=20（稳定期）
- 期望胜率计算：`E_a = 1 / (1 + 10^((R_b - R_a) / 400))`
- 段位划分：青铜(<1000)、白银(1000-1200)、黄金(1200-1400)、铂金(1400-1600)、钻石(1600-1800)、大师(1800-2000)、王者(2000+)

### 5.4 统计显著性

- 50 局镜像对可提供合理的方差缩减
- 接近的 Bot（ELO 差 < 50）建议 100+ 局镜像对
- 跟踪胜率的置信区间

---

## 6. 反馈循环与对战分析

### 6.1 从对战日志提取的指标

对战日志格式（由 `battle.py` 生成）：

```json
{
  "logs": [
    {"output": {"command": "request", "content": {"0": {...}}, "display": {...}}},
    {"0": {"response": "0", "verdict": "OK"}, "output": null},
    ...
  ]
}
```

从日志中提取的关键指标：

**Bot 级别指标：**
| 指标 | 含义 | 计算方式 |
|------|------|----------|
| VPIP | 翻前主动入池率 | 翻前自愿跟注/加注的次数 ÷ 翻前决策次数 |
| PFR | 翻前加注率 | 翻前加注次数 ÷ 翻前决策次数 |
| AF | 激进度因子 | (加注次数) ÷ 跟注次数 |
| Fold% | 面对加注弃牌率 | 面对加注时弃牌的次数 ÷ 面对加注总次数 |
| All-in% | 全下频率 | 全下次数 ÷ 总决策次数 |
| WSD% | 摊牌胜率 | 摊牌获胜次数 ÷ 摊牌总次数 |

**对手级别指标：**
| 指标 | 含义 |
|------|------|
| 对各对手的胜率 | 按 Bot 分组的胜/负/平统计 |
| 筹码差趋势 | 前半手牌 vs 后半手牌的筹码变化 |
| 各轮弱点 | 翻前/翻牌/转牌/河牌各输多少筹码 |

**关键失误手牌（最亏的 N 手）：**
- 提供完整的请求/响应序列
- Bot 在决策时看到的信息（底牌、公共牌、历史）
- 它采取了什么行动 vs 估计的最优行动

### 6.2 反馈格式示例

```markdown
## 战绩总结
- 你的 Bot (claude_v3) 参加了 300 局镜像对战，对手 6 个
- 总战绩：142-138-20（胜率 47.3%）
- ELO：1185（起始 1200，下降 15）
- 平均筹码差：-45/局

## 对手分析
| 对手 | 风格 | 战绩 | 胜率 | 筹码差 |
|------|------|------|------|--------|
| opponent_A | 激进型 | 22-28 | 44% | -120 |
| opponent_B | 被动型 | 30-20 | 60% | +80 |
| opponent_C | GTO型 | 20-30 | 40% | -200 |

## 关键弱点
1. 面对河牌圈加注弃牌过多（62%，建议降至 45%）
2. 翻前 3Bet 频率不足（8%，建议提升至 15%）
3. 对子面牌面的诈唬检测能力弱

## 典型失误手牌
### 手牌 #37（亏 4200 筹码）
- 你的底牌：[K♠, Q♥]，公共牌：[A♦, 7♣, 7♠, 3♥, 2♣]
- 对手加注 400（底池 800）
- 你弃牌（fold）
- 分析：面对对子面的小额加注，AQ+ 应该跟注。弃牌过于保守。

[更多手牌...]
```

---

## 7. 进化策略

### 7.1 独立进化（Lineage Mode）

每个 LLM 维护自己的 Bot 血统：

```
Claude:  claude_v1 → claude_v2 → claude_v3 → ...
GPT:     gpt_v1    → gpt_v2    → gpt_v3    → ...
Gemini:  gemini_v1 → gemini_v2 → gemini_v3 → ...
```

每个 LLM 只能看到自己的代码和战绩。这是对各 LLM 纯策略推理能力的检验。

**优点**：策略多样性高，纯粹的 LLM 能力对比
**缺点**：收敛慢，可能有冗余探索
**适用**：前几轮探索阶段

### 7.2 交叉授粉（Cross-Pollination）

每轮评估后，匿名共享顶级 Bot 的策略摘要：

```
反馈中增加：
"当前排名第一的 Bot（匿名）使用以下策略特征：
 - 翻前 3Bet 频率 18%
 - 面对加注弃牌率 42%
 - 河牌超池下注频率 12%
 - 激进度因子 2.3"
```

**优点**：加速收敛，融合不同 LLM 的战略洞察
**缺点**：可能导致策略趋同
**适用**：中期加速阶段

### 7.3 锦标赛选择（Tournament Selection）

每轮淘汰底部 Bot，保留顶部 Bot 代码作为下一代的参考：

```
Round N:   claude_v3, gpt_v3, gemini_v3, deepseek_v3
评估...    排名: 1.gpt  2.claude  3.deepseek  4.gemini
淘汰:      gemini_v3 被淘汰
Round N+1: 所有 LLM 都能参考 gpt_v3 的代码片段来生成下一代
```

**优点**：强选择压力，快速向最优策略逼近
**缺点**：多样性损失，有陷入局部最优的风险
**适用**：后期精炼阶段

### 7.4 推荐混合方案

分三个阶段：

```
阶段 1：独立探索（3 轮）
  各 LLM 独立迭代，建立多样化的策略基线
  评估方式：快速评估（10 局）+ 标准评估（50 局）

阶段 2：交叉授粉（3 轮）
  匿名共享策略摘要，加速学习
  评估方式：标准评估（50 局）+ 完整天梯

阶段 3：锦标赛选择（反复迭代直到收敛）
  淘汰底部 Bot，共享顶部代码
  评估方式：完整天梯 + anchor_runner 深度测试
```

### 7.5 收敛判定

满足以下条件时认为收敛：

1. **ELO 稳定**：顶级 Bot 的 ELO 标准差连续 3 轮 < 20
2. **头对头接近**：顶级 Bot 之间的胜率接近 50/50（45%-55%）
3. **策略指标趋同**：VPIP、PFR、激进度等关键指标方差缩小

---

## 8. 关键扑克策略概念

> 本节是 LLM 必须理解的扑克策略知识库，应包含在初始 Prompt 中。

### 8.1 基础概念

**底池赔率（Pot Odds）**
```
底池赔率 = 跟注金额 / (底池 + 跟注金额)
需要的最低胜率 = 底池赔率

示例：底池 600，对手加注 200，你需要跟注 200
底池赔率 = 200 / (600 + 200) = 25%
你需要至少 25% 胜率才能盈利跟注
```

**期望值（EV）**
```
EV = Σ(概率 × 收益)
所有决策应追求最大化 EV
```

**胜率估算**
```
翻前：可通过蒙特卡洛模拟估算
    - 发出完整的 5 张公共牌和对手底牌
    - 统计获胜比例
翻后：可精确枚举（尤其河牌）或模拟
    - 只需补全未发出的公共牌和对手底牌
```

**位置优势**
- 2 人德州中，庄家（小盲）翻前先行动，翻后后行动
- 翻后后行动有信息优势，可以更准确地决策

### 8.2 手牌评估

**牌型排名（从弱到强）：**

| 等级 | 名称 | 示例 |
|------|------|------|
| 1 | 高牌 | A K 9 7 3 |
| 2 | 一对 | A A 9 7 3 |
| 3 | 两对 | A A K K 3 |
| 4 | 三条 | A A A 7 3 |
| 5 | 顺子 | 5 6 7 8 9 |
| 6 | 同花 | A♠ K♠ 9♠ 7♠ 3♠ |
| 7 | 葫芦 | A A A K K |
| 8 | 四条 | A A A A 3 |
| 9 | 同花顺 | 5♠ 6♠ 7♠ 8♠ 9♠ |

**7 选 5 最优**：从 7 张牌（2 底牌 + 5 公共牌）中穷举 C(7,5)=21 种组合，取最大牌型。

**翻前手牌强度**：可用 Chen 公式或预计算 169 种底牌组合的胜率表。

### 8.3 策略框架

**GTO（博弈论最优）**
- 目标：找到不可被剥削的均衡策略
- 特征：混合策略（同一手牌有时诈唬有时价值下注）
- 优势：不怕对手剥削，但未必最大化 EV

**剥削性打法**
- 目标：针对对手弱点偏离 GTO，追求更高 EV
- 前提：需要准确的对手建模
- 风险：如果对对手判断错误，可能反被剥削

**范围思维**
- 不只考虑自己的一手牌，而是考虑自己整个范围（所有可能持有的手牌）
- 下注策略应使范围均衡：价值下注和诈唬按一定比例搭配

**牌面纹理**
- **湿面**（Wet board）：有同花听牌、顺子听牌可能性，需要更谨慎
- **干面**（Dry board）：没有听牌可能性，更适合激进攻势
- **对子面**（Paired board）：公共牌有对子，葫芦和四条可能性增加
- **高牌面**：公共牌有大牌（A、K），影响击中范围

### 8.4 对手建模指标

从历史操作中推断对手风格的关键指标：

| 指标 | 含义 | 典型范围 |
|------|------|----------|
| VPIP | 翻前主动入池率 | 紧=20-30%，松=50%+ |
| PFR | 翻前加注率 | 被动=10%，激进=30%+ |
| AF | 激进度因子 | 被动<1.5，激进>3.0 |
| Fold to Raise | 面对加注弃牌率 | 紧=50%+，松=30%- |
| All-in Rate | 全下频率 | 正常<5%，疯子型>15% |
| Postflop Aggr | 翻后激进度 | 衡量翻牌后是否持续施压 |

对手风格分类：
- **紧-被动（Nit）**：VPIP 低，AF 低 → 只在有强牌时行动
- **紧-激进（TAG）**：VPIP 中等，AF 高 → 最难对付的标准风格
- **松-被动（Calling Station）**：VPIP 高，AF 低 → 多跟注少加注，可以价值下注
- **松-激进（Maniac/LAG）**：VPIP 高，AF 高 → 频繁加注诈唬，可以设陷阱

### 8.5 下注理论

**价值下注（Value Bet）**
- 目的：用强牌从更弱的牌中榨取价值
- 尺度取决于牌力等级：
  - **超强牌（Nut）**：可以慢打设陷阱，或超池下注
  - **强牌（Strong）**：标准尺度（50-75% 底池）
  - **薄价值（Thin Value）**：小尺度（25-40% 底池），对手范围中有很多更弱的牌

**诈唬（Bluff）**
- 目的：用弱牌迫使对手弃牌
- 成功率要求：诈唬金额 / (底池 + 诈唬金额) 底池赔率
- **阻隔牌诈唬**：持有对手强牌组合的阻隔牌（如牌面有三张同花时你持有 A 该花色）

**半诈唬（Semi-Bluff）**
- 当前是弱牌但有听牌成强牌的可能
- 即使被跟注也有较高胜率（同花听牌 ~35%，顺子听牌 ~32%）
- 风险低于纯诈唬，应优先使用

**超池下注（Overbet）**
- 下注超过底池大小（可达 2.2x 底池）
- 适用于：河牌圈两极化范围（要么超强要么很弱）
- 现有 Bot 5 实现了河牌超池下注逻辑

### 8.6 比赛级策略

在 50 手牌的比赛中，需要考虑全局筹码变化：

**锦标赛压力**
```
领先时：降低风险偏好
  - 提高跟注门槛（需要更高胜率才跟注）
  - 减少诈唬频率
  - 避免大底池的边缘局面

落后时：提高风险偏好
  - 降低跟注门槛
  - 增加诈唬频率
  - 在边缘局面更激进
```

**锁定胜局检测**
```
如果：当前筹码领先 > 剩余所有手牌可能被迫输掉的最大值
那么：此后每手都弃牌，稳赢比赛
示例：领先 2000，剩余 3 手牌，每手最多被迫输 100（大盲），最多输 300
      2000 > 300，可以安全锁定
```

**反锁定检测**
```
如果：弃牌会让对手获得锁定胜局
那么：即使牌不好也必须继续跟注
这要求降低跟注门槛，有时需要"强制战斗"
```

### 8.7 现有 Bot 创新点总结

供 LLM 参考的关键策略创新：

| Bot | 创新点 |
|-----|--------|
| bot_1 | 蒙特卡洛加权胜率估算、对手范围建模、实现后权益调整、坚果风险画像 |
| bot_2 | 169 手牌翻前查找表、概念漂移检测、CBet 追踪、安全剥削框架 |
| bot_3 | 5 风格策略组合（GTO/剥削紧/剥削松/剥削疯/剥削弱）+ EXP3 元学习器在线选风格 |
| bot_5 | 河牌精确枚举（零方差胜率）、超池下注（极化范围 2.2x 底池）、反特定 Bot 剥削模块 |

---

## 9. 实际实施

### 9.1 目录结构

```
pok/
  bots/
    bot1/main.py ~ bot6/main.py         # 现有 Bot
    claude_v1.py                 # 多 AI 进化 Bot
    gpt_v1.py
    gemini_v1.py
    deepseek_v1.py
    claude_v2.py                 # 迭代后
    ...
  docs/
    multi_ai_bot_design.md       # 本文档
  scripts/
    ladder.py                    # 现有：天梯赛
    anchor_runner.py             # 现有：锚点测试
    evolution_manager.py         # 新增：进化管理器
    battle_analyzer.py           # 新增：对战分析器
    llm_interface/               # 新增：LLM API 适配
      __init__.py
      base.py
      claude_adapter.py
      openai_adapter.py
      gemini_adapter.py
    prompt_templates/            # 新增：Prompt 模板
      initial_generation.md
      iteration_improve.md
      strategy_primer.md
  evolution_results/             # 新增：进化运行结果
    run_001/
      config.json
      round_001/
        bots/                    # Bot 代码快照
        ladder_report.json       # ELO 排名
        feedback/                # 结构化反馈
      round_002/
        ...
```

### 9.2 Bot 命名规范

格式：`{llm_name}_v{iteration}.py`

- `claude_v1.py`、`claude_v2.py`、`claude_v3.py`
- `gpt_v1.py`、`gpt_v2.py`
- `gemini_v1.py`
- `deepseek_v1.py`

与现有 `bot<N>/main.py` 共存。天梯系统 `ladder.py` 的 `discover_bots()` 目前匹配 `bot\d+` 目录格式。进化管理器可将 LLM 生成 Bot 映射为 `bot101/main.py` 等（100+ 命名空间留给多 AI Bot）。

### 9.3 代码验证管线

LLM 生成的 Bot 进入天梯前必须通过：

```
1. 语法检查
   python -c "import py_compile; py_compile.compile('bots/claude_v1.py')"

2. 协议合规
   运行单局测试：对战 bot_1，验证 JSON 输出格式

3. 超时测试
   验证标准翻前决策在 30 秒内完成

4. 稳定性测试
   连续 5 局不崩溃、不出无效动作

5. 基本竞争力
   5 局测试中至少赢 1 局（>10% 胜率）
```

### 9.4 LLM API 集成

统一接口设计：

```python
class LLMAdapter:
    def generate_bot(self, prompt: str, context: dict) -> str:
        """生成 Bot Python 代码"""
        raise NotImplementedError

    def improve_bot(self, current_code: str, feedback: str, context: dict) -> str:
        """基于反馈改进 Bot"""
        raise NotImplementedError
```

配置通过环境变量管理 API Key：
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `GEMINI_API_KEY`

### 9.5 进化管理器伪代码

```python
def run_evolution(config):
    # 1. 初始化：加载 LLM 适配器
    llms = load_adapters(config["llms"])

    for round in range(config["rounds"]):
        # 2. 各 LLM 生成/改进 Bot
        for llm in llms:
            if round == 0:
                code = llm.generate_bot(initial_prompt)
            else:
                feedback = load_feedback(llm.name, round - 1)
                prev_code = load_bot_code(llm.name, round - 1)
                code = llm.improve_bot(prev_code, feedback)

            # 3. 代码验证
            if validate_bot(code):
                save_bot(llm.name, round, code)
            else:
                # 使用上一版或生成默认 Bot
                save_bot(llm.name, round, fallback_code)

        # 4. 运行天梯评估
        run_ladder(all_bots, config["games_per_matchup"])

        # 5. 分析结果，生成反馈
        analyze_and_generate_feedback(round)

        # 6. 检查收敛
        if check_convergence():
            break
```

---

## 10. 风险与成本

### 10.1 LLM 代码质量风险

| 风险 | 缓解措施 |
|------|----------|
| 输出非 JSON | 验证管线拦截，用 try/except 包装 main() |
| 无限循环 | battle.py 的 30s 超时机制兜底 |
| 计算量过大 | Prompt 中限定模拟次数（< 1000） |
| 协议违规 | 验证管线 + sanitize_action() 函数 |
| 外部依赖 | Prompt 约束 + 验证管线 import 检查 |

### 10.2 API 成本估算

| 轮次 | 操作 | 预估成本/轮 |
|------|------|------------|
| 生成/改进 | 4 个 LLM × $0.10-2.00 | $0.40-8.00 |
| 评估 | 本地运行（无 API 费用） | $0 |
| **10 轮总计** | | **$4-80** |

优化建议：早期用较便宜的模型（如 GPT-4o-mini），后期用高端模型精炼。

### 10.3 可复现性

- 每个 Bot 的随机数使用固定种子（方便回放分析）
- 归档每轮的完整 Prompt 和 LLM 响应
- 标记使用的 LLM 模型版本

### 10.4 Botzone 部署

最终验证：将最佳进化 Bot 上传到 Botzone 在线平台
- 工具：`scripts/botzone_upload_match.py`
- 游戏 ID：`63dcfaddee1bce5e6c8f4b53`
- 最大上传：4MB（单文件 Python Bot 远低于此限制）

---

## 11. 成功指标

### 11.1 量化指标

| 指标 | 目标 |
|------|------|
| ELO 收敛 | 顶级 Bot 的 ELO 标准差 < 20 |
| 对基线胜率 | 每轮迭代对 bot_1 的胜率持续提升 |
| Botzone 排名 | 最佳进化 Bot 达到钻石以上 |
| 策略多样性 | 不同 LLM 血统的 VPIP/PFR 方差保持 > 5% |

### 11.2 里程碑

| 阶段 | 里程碑 | 验证标准 |
|------|--------|----------|
| M1 | 基础设施完成 | 至少 3 个 LLM 能生成可通过验证管线的 Bot |
| M2 | 基本竞争力 | 至少 3 个 LLM 生成的 Bot 对 bot_1 胜率 > 40% |
| M3 | 超越现有最佳 | 至少 1 个 LLM 生成的 Bot 对 bot_5 胜率 > 55% |
| M4 | 混合优势 | 交叉授粉产生的 Bot 超越所有单一血统 |
| M5 | 线上验证 | 最佳进化 Bot 在 Botzone 达到钻石段位 |
