# HoldemRL — 双人德州扑克 DMC 自博弈 RL 训练框架

> 架构灵感来源于 DanLM: "Tokenization Is All You Need to Master Complex Card Games"
> Botzone 掼蛋/斗地主排行榜 #1，零领域知识，端到端学习

## 项目背景

当前 POK 项目使用 **LLM 驱动的代码进化**（Master Architect → Workers → Reviewer → Critic）来迭代改进德州扑克 bot。`ref/DanLM` 提供了另一种路径：**深度强化学习自博弈**，直接通过梯度优化神经网络策略。

HoldemRL 将 DanLM 的 RL 自博弈方法移植到双人 Heads-up No-Limit Texas Hold'em，与 POK 现有的 LLM 进化系统形成互补。

### 两种方法对比

| 维度 | DanLM (掼蛋 RL) | POK (德扑 LLM 进化) | HoldemRL (德扑 RL) |
|------|-----------------|---------------------|-------------------|
| 策略表示 | 神经网络权重 | Python 源代码 | 神经网络权重 |
| 优化方法 | 梯度下降 + 自博弈 | LLM 代码生成 + 评估 | 梯度下降 + 自博弈 |
| 可解释性 | 黑盒 | 完全白盒 | 黑盒 |
| 硬件需求 | GPU 集群 | LLM API | 单 GPU |
| Botzone 部署 | 需推理引擎 | 直接上传 Python | 仅作本地对手 |
| 训练代码 | 闭源 | N/A | 开源 |

## 架构设计

### 整体流水线

```
Actor 进程 (自博弈收集样本)
    ↓  (s, a, r, s') transitions
Replay Buffer (100K 容量)
    ↓  batch_size 采样
Learner (Q-Network 梯度更新)
    ↓  周期性同步
Target Network (Double DQN)
    ↓
评估 vs baseline bots
    ↓
保存 checkpoint
```

### 两套 Q-Network 架构

**MLP Q-Network (DanZero 风格)**
```
flat_obs (132维)
  → Linear(132, 512) → ReLU
  → Linear(512, 1024) → ReLU
  → Linear(1024, 512) → ReLU
  → Linear(512, 1024) → ReLU
  → Linear(1024, 512) → ReLU
  → Dueling Head:
      Value Stream: Linear(512, 256) → ReLU → Linear(256, 1)
      Advantage Stream: Linear(512, 256) → ReLU → Linear(256, 11)
  → Q(s,a) = V(s) + A(s,a) - mean(A)
```
参数量: ~2,434,060

**Transformer Q-Network (DanLM 风格)**
```
token_sequence (128 tokens)
  → Token Embedding (80 vocab → 128 dim) + Positional Encoding
  → 4× Causal Transformer Block (128 dim, 4 heads, 512 ff)
  → LayerNorm → context vector

hand_features (132维)
  → Hand MLP (132 → 256 → 128) → hand context

[context; hand_context] (256 dim)
  → Dueling Q-Value Head → Q(s,a)
```
参数量: ~1,031,900

### 动作空间设计

德扑的 raise 金额是连续值，需要离散化。HoldemRL 使用 11 种离散动作：

| 动作 | 编码 | 说明 |
|------|------|------|
| Fold | 0 | 弃牌 |
| Check/Call | 1 | 过牌/跟注 |
| All-in | 2 | 全下 |
| Raise 0.25x | 3 | 加注到 0.25 倍底池 |
| Raise 0.5x | 4 | 加注到 0.5 倍底池 |
| Raise 0.75x | 5 | 加注到 0.75 倍底池 |
| Raise 1x | 6 | 加注到 1 倍底池 |
| Raise 1.5x | 7 | 加注到 1.5 倍底池 |
| Raise 2x | 8 | 加注到 2 倍底池 |
| Raise 3x | 9 | 加注到 3 倍底池 |
| Raise 5x | 10 | 加注到 5 倍底池 |

raise 金额会被自动 clamp 到合法范围（最小加注额 ≤ raise ≤ 筹码上限）。

### 观察空间设计

**v0 编码 (132 维 flat 向量, 用于 MLP)**

| 分量 | 维度 | 内容 |
|------|------|------|
| card_vec | 52 | 手牌 + 公共牌的 one-hot (每张牌占一位) |
| history | 64 | 阶段 one-hot、底池/下注归一化、最近 8 次行动编码 |
| player | 16 | 筹码、位置、底池赔率、阶段、allin 标志等 |

**v1 编码 (token 序列, 用于 Transformer)**

使用 ~80 词的自定义 tokenizer：

```
词表组成:
  0-7:    特殊 token (PAD, START, SEP, AGENT, OPPONENT, MASK, UNK, END)
  8-11:   阶段 token (PREFLOP, FLOP, TURN, RIVER)
  12-16:  动作 token (FOLD, CHECK, CALL, RAISE, ALLIN)
  17-19:  保留
  20-27:  加注桶 token (0.25x 到 5x)
  28-29:  玩家 token (P0, P1)
  30-81:  牌面 token (card_int 0-51)
  82-89:  底池/筹码状态 token
```

Token 序列格式：
```
[START] [STAGE] [POT_SIZE] [CHIP_STATUS]
[AGENT] [CARD] [CARD] [SEP]  -- 手牌
[P0] [ACTION] [RAISE_BIN?] [SEP]  -- 每次行动
[P1] [ACTION] [RAISE_BIN?] [SEP]
[FLOP] [CARD] [CARD] [CARD] [SEP]  -- 公共牌
[PAD] ...  -- 填充到 max_seq_len=128
```

### 奖励设计

每手牌结束时计算奖励：
```
reward = (最终筹码 - 初始筹码) / 大盲注
reward = clip(reward, -100, 100)  # 防止极端值
```

使用 chip delta 而非胜负作为奖励信号，提供更细粒度的梯度反馈。

### DMC 训练循环

借鉴 DanLM 的 cycle-based 设计：

```
每个 cycle:
  1. Actor 收集 N/k 个 transition (自博弈)
  2. 推入 Replay Buffer
  3. Learner 执行 S 次梯度更新
  4. Soft update target network
  5. 每 C 个 cycle: 评估 + 保存 checkpoint
```

### 训练技巧

| 技巧 | 说明 |
|------|------|
| Double DQN | 用 online network 选动作，target network 评估 Q 值 |
| Dueling DQN | V(s) + A(s,a) 分解，提升价值估计稳定性 |
| Soft Target Update | τ=0.005 指数移动平均更新 target network |
| Epsilon Decay | ε 从 0.3 线性衰减到 0.01 (100K steps) |
| Gradient Clipping | max_norm=10.0 防止梯度爆炸 |
| Huber Loss | SmoothL1Loss 比 MSE 对异常值更鲁棒 |

## 目录结构

```
rl/
├── core/
│   ├── __init__.py          # 包说明
│   ├── holdem_env.py        # Gymnasium 环境 (封装 engine/judge.py)
│   ├── tokenizer.py         # 德扑 tokenizer (~80 词表)
│   ├── encoder.py           # 编码器 (v0 flat / v1 tokens)
│   └── config.py            # 训练超参 (HoldemRLConfig)
├── models/
│   ├── __init__.py
│   ├── q_network.py         # MLP Q-Network (DanZero 风格)
│   └── transformer.py       # Transformer Q-Network (DanLM 风格)
├── training/
│   ├── __init__.py
│   ├── replay_buffer.py     # 回放缓冲区 (支持 PER)
│   └── trainer.py           # DMC 训练协调器
├── eval/
│   └── __init__.py           # 评估框架 (3 种对手)
├── scripts/
│   ├── train.py             # 训练入口脚本
│   ├── evaluate.py          # 评估入口脚本
│   └── rl_bot.py            # POK 子进程 bot 封装
└── requirements.txt          # torch, numpy, gymnasium
```

## 使用指南

### 安装依赖

```bash
pip install torch numpy gymnasium
```

### 训练

```bash
# MLP 训练 (默认)
python -m rl.scripts.train --arch mlp --cycles 1000 --device cuda

# Transformer 训练 (DanLM 风格)
python -m rl.scripts.train --arch transformer --cycles 1000 --device cuda

# 从 checkpoint 恢复
python -m rl.scripts.train --resume rl/checkpoints/cycle_000100.pt

# 自定义参数
python -m rl.scripts.train \
    --arch mlp \
    --cycles 2000 \
    --lr 3e-4 \
    --batch-size 4096 \
    --buffer-size 200000 \
    --num-actors 8 \
    --device cuda
```

### 评估

```bash
# vs 所有内置对手
python -m rl.scripts.evaluate \
    --checkpoint rl/checkpoints/best_model.pt \
    --opponent all \
    --games 500

# vs 特定对手
python -m rl.scripts.evaluate \
    --checkpoint rl/checkpoints/best_model.pt \
    --opponent aggro \
    --games 1000
```

### 与 POK Bot 对战

```bash
# RL bot vs bot5 (注意: subprocess 模式每步需加载模型，较慢)
python engine/battle.py bots/bot5/main.py rl/scripts/rl_bot.py -n 10 -v

# 指定 checkpoint
RL_CKPT=rl/checkpoints/best_model.pt python engine/battle.py \
    bots/bot5/main.py rl/scripts/rl_bot.py -n 10 -v
```

### 在代码中使用

```python
from rl.core.config import HoldemRLConfig
from rl.training.trainer import DMCTrainer
from rl.eval import evaluate, RandomOpponent

# 创建训练器
config = HoldemRLConfig(architecture="mlp", device="cuda")
trainer = DMCTrainer(config)

# 训练 100 个 cycle
for _ in range(100):
    trainer.train_cycle()

# 评估
result = evaluate(trainer.model, RandomOpponent(), num_games=100)
print(f"Win rate: {result['win_rate']:.1%}")

# 保存
trainer.save_checkpoint("rl/checkpoints/my_model.pt")
```

## 关键配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `architecture` | `"mlp"` | 模型架构: `"mlp"` 或 `"transformer"` |
| `lr` | `1e-4` | Adam 学习率 |
| `replay_buffer_size` | `100,000` | 回放缓冲区容量 (N) |
| `replay_buffer_diversity` | `2` | 历史权重版本数 (k) |
| `train_steps_per_cycle` | `8` | 每周期梯度步数 (S) |
| `batch_size` | `2048` | 采样批大小 |
| `num_actors` | `4` | 并行 Actor 数 |
| `eps_start` | `0.3` | 初始探索率 |
| `eps_end` | `0.01` | 最终探索率 |
| `dueling` | `True` | 启用 Dueling DQN |
| `double_dqn` | `True` | 启用 Double DQN |
| `target_update_tau` | `0.005` | Target network 软更新系数 |
| `reward_clip` | `100.0` | 奖励裁剪范围 |

## 冒烟测试结果

```
=== 5 个训练 Cycle (MLP, CPU, buffer=300) ===

Cycle 1: steps=2, eps=0.300, buffer=300
Cycle 2: steps=4, eps=0.300, buffer=300
Cycle 3: steps=6, eps=0.300, buffer=300
Cycle 4: steps=8, eps=0.300, buffer=300
Cycle 5: steps=10, eps=0.300, buffer=300

Eval vs random: win_rate=80.0% (8W/2L/0D)
  avg_reward=60.30
```

> 注: 仅 10 局的统计量，且未经过充分训练。完整训练 (1000+ cycles, GPU) 预期需要数小时。

## 已知限制与后续优化

### 当前限制

1. **RL Bot 对战速度**: `rl_bot.py` 使用 subprocess 模式，每次决策需加载 PyTorch 模型 (~2s)，单局 50 手牌约 200+ 秒。需要实现持久化进程协议来优化
2. **观察空间有损**: POK judge 的 JSON 协议信息量有限（缺少对手筹码、精确底池等），`rl_bot.py` 的观察编码存在近似
3. **自博弈对手单一**: 当前 Actor 使用自身作为对手，容易产生策略退化。需要引入 POK 的 bot1-bot6 作为多样化对手
4. **奖励设计简单**: 仅使用 chip delta，未考虑对手建模、位置价值等高级奖励信号

### 后续优化方向

1. **持久化进程协议**: 在 `rl_bot.py` 中实现 `_PersistentBot` 的行分隔 JSON 通信，每局只加载一次模型
2. **混合训练**: 将 POK 的 bot1-bot6 纳入 Actor 的对手池，增加训练多样性
3. **价值网络辅助**: 训练 V(s) 网络，集成到 LLM Worker 上下文中，提供更细粒度的策略反馈
4. **NTP 辅助任务**: Transformer 模型已支持 `forward_with_ntp()`，可以在 Q-learning loss 之外加入 Next-Token Prediction loss 来改善表示学习
5. **CFR/VRoUGE 对比**: Heads-up NL Hold'em 的博弈树极大，DMC 可能不够。可以考虑 CFR 类方法作为对比实验
6. **ONNX 导出**: 训练完成后导出 ONNX 格式，加速推理，减小模型体积

## 与 POK 进化系统的集成点

HoldemRL 作为 POK 项目的独立模块，可以通过以下方式与现有 LLM 进化系统协同：

1. **强 Baseline 对手**: 训练好的 RL bot 加入 `elo_daemon.py` 的对手池，为 LLM 进化的 bot 提供更强的训练压力
2. **策略反馈**: RL 价值网络的 Q(s,a) 估计可以注入 Worker 的上下文，帮助 LLM 理解哪些局面有利
3. **科学对比**: 在 Botzone 上同时提交 LLM bot 和 RL bot，对比两种方法的最终排名
4. **经验池增强**: RL bot 的对局回放可以加入 `experience_pool.md`，为 LLM 提供高质量的对局分析素材
