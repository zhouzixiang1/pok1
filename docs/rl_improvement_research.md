# HoldemRL 改进方案完整调研报告

> 调研时间：2026-06-07
> 调研范围：2019-2026 年 HUNL (Heads-Up No-Limit Texas Hold'em) RL 领域前沿论文、开源项目、最佳实践

---

## 一、当前模型诊断

### 1.1 训练结果总结

| 阶段 | Cycles | Epsilon | vs Random | vs CallBot | vs Aggro |
|------|--------|---------|-----------|------------|----------|
| Round 1 (cycle 550 peak) | 550 | 0.28 | 87-91% | 75-78% | 57-62% |
| Round 2 (cycle 550-1050) | +500 | 0.01 | 87-91% | 75-78% | 54-61% |
| **改善** | — | ↓0.27 | **0** | **0** | **0** |

**结论**：epsilon 衰减后零改善。模型在 cycle 550 已收敛到局部最优，后续 500 cycles 完全停滞。

### 1.2 决策模式分析（200 局诊断）

| 问题 | 数据 | 正确策略 |
|------|------|----------|
| **Flop All-in 过多** | vs Random 55%, vs CallBot 48%, vs Aggro 66% | Flop 应该用小注试探/价值下注 |
| **不会小注诈唬** | vs CallBot 的诈唬几乎全是 All-in | 应该用 0.25-0.5x pot 小注诈唬 |
| **Turn/River 退缩** | Turn 44% All-in + 37% Fold (vs Aggro) | 应该根据牌力做精细决策 |
| **Q 值不稳定** | 同一动作 Q 值 std > 100 | Q 值方差应远小于均值 |

### 1.3 根因分析

```
DQN 自博弈在 HUNL 上的三大根本问题：

1. 算法不匹配：DQN 为完全信息 MDP 设计，HUNL 是不完全信息博弈
   - DQN 无法建模对手的持牌分布（信念状态）
   - 无法区分 "对手 check 因为他弱" 和 "对手 check 因为他在慢打"

2. 自博弈退化：纯 self-play 没有对手多样性
   - 策略坍缩到 Push-Fold 均衡（简化的纳什均衡子集）
   - 不存在对手建模的进化压力

3. 信用分配困难：稀疏 reward（只有手牌结束时有信号）
   - 每手牌 2-5 步决策，只有最后一步直接关联 reward
   - 中间步骤的梯度信号被 γ^t 衰减淹没
```

---

## 二、前沿方法调研

### 2.1 算法选择：PPO 优于 DQN

**关键文献**：
- [Reevaluating Policy Gradient Methods for Imperfect-Information Games (2025, NeurIPS 2025)](https://arxiv.org/abs/2502.08938)
  - 7000 次训练、345,000 CPU 小时的大规模对比实验
  - **结论：PPO/PPG/MMD (通用策略梯度) 全面优于 NFSP、PSRO、ESCHER、R-NaD**
  - NFSP 和 R-NaD 在部分情况下接近 PPO，但整体不如
  - ESCHER 完全不 competitive，PSRO 大部分情况下不 competitive
- [Policy Gradient Methods Converge Globally in IIGs (NeurIPS 2025)](https://neurips.cc/virtual/2025/poster/117678)
  - 证明了正则化策略梯度在不完全信息博弈中的全局收敛性

**为什么 PPO 比 DQN 更适合 HUNL**：

| 维度 | DQN (当前) | PPO |
|------|-----------|-----|
| 策略类型 | 确定性 (argmax Q) | 随机性 (softmax policy) |
| 探索 | Epsilon-greedy（硬切换） | 熵正则化（自然探索） |
| 信用分配 | TD(0) 单步 | GAE (λ-return) 多步 |
| 策略坍缩 | 容易（Q 值方差大时） | PPO-clip 防止过大更新 |
| On/Off-policy | Off-policy（有 distribution shift） | On-policy（无偏差） |
| 多步决策 | 只通过 γ 传递 | 通过 advantage function 直接 |

**AlphaHoldem (AAAI 2022)** 验证了 PPO 在 HUNL 上的有效性：
- 端到端 RL，无 CFR，单 GPU 3 天训练
- 击败 Slumbot 和 DeepStack
- 推理速度 2.9ms/决策（比 DeepStack 快 1000x）
- 关键创新：**Trinal-Clip PPO**（在标准 PPO-clip 外增加一个 clip 项）

### 2.2 状态表示：从 Flat Vector 到结构化张量

**AlphaHoldem (AAAI 2022) — 多维张量表示**：
- 牌张信息：6 通道 × 4×13 稀疏二值矩阵（手牌、flop/turn/river 公共牌、所有可见牌）
- 下注信息：24 通道 × 4×n_b 稀疏二值矩阵（每个位置的下注历史 + 合法动作）
- 使用 CNN 处理张量，自动学习空间特征
- 优势：完整编码历史信息，适合深度网络学习

**AlphaExploitem (2025/2026) — 层次 Transformer**：
- 三个输入流：当前手牌信息、当前手牌行动历史、跨手牌会话历史
- **层次历史编码器**：
  - Within-hand Transformer：每手已完成的牌 → 总结向量 h_i
  - Across-hand Transformer：序列 {h_1, ..., h_M} → 会话级上下文 z
- Token 类型标签：agent action、opponent action、private card、community card、opponent card
- 每种类型使用独立 embedding table
- 输出：policy head + value head

**Belief-Aware MuZero (2026) — 辅助预测头**：
- 在 MuZero 基础上增加两个辅助头：
  - **Winner head**：预测 P(玩家获胜)，交叉熵训练
  - **Rank head**：预测最终排名分布
- 用 ego-conditioned 潜状态表示信念
- 损失函数：`L = L_MuZero + α·L_winner + β·L_rank`

**对比当前 HoldemRL 的 132 维 flat vector**：

| 信息 | 当前编码 | 前沿方法 |
|------|---------|---------|
| 牌面 | 52 维 one-hot | 结构化 4×13 矩阵 |
| 行动历史 | 64 维压缩向量（最多 8 步） | 完整 token 序列 |
| 对手行为 | 仅 aggressor ratio | 完整对手行动序列 |
| 跨手牌信息 | 无 | 层次 Transformer 编码 |
| 信念状态 | 无 | ego-conditioned 潜状态 |

### 2.3 动作空间离散化

**当前方案（8 种 raise 桶）的问题**：
- 0.25x, 0.5x, 0.75x, 1x, 1.5x, 2x, 3x, 5x pot — 过于粗糙且选择太多
- 模型 vs CallBot 时 preflop 50% 选 R2x — 说明它学到了 "标准 raise = 2x pot"，但这在现实中太大了

**AlphaHoldem 的方案**：
- 不使用人工定义的 raise 桶
- 使用 4×n_b 矩阵编码所有可能的下注选项
- 通过 CNN + policy head 直接输出连续动作

**ReBeL / RL-CFR 的方案**：
- 动态动作抽象：RL 学习最优的离散化方案
- 每个决策点的 raise 选项由 RL agent 决定

**实用建议（可直接实施的改进）**：

```
方案 A — 精简为 5 种动作 (最快见效):
  0: Fold
  1: Check/Call
  2: All-in
  3: Raise Half Pot (0.5x)
  4: Raise Pot (1x)
  5: Raise 2x Pot

方案 B — 位置相关的自适应离散化 (中等难度):
  Preflop: fold, call, raise 2.5BB, raise 3BB, raise 4BB, all-in
  Postflop: fold, check/call, raise 0.5pot, raise pot, raise 2xpot, all-in

方案 C — 连续动作输出 (需要改算法为 PPO):
  policy head 输出 raise_percentage (0-1)，映射到 [min_raise, max_raise]
```

### 2.4 Reward 设计与信用分配

**当前问题**：
- 每手牌只有终止时有 reward = chip_delta / BB
- 中间步骤 reward = 0
- TD(0) 通过 γ^t 传递，但 γ=0.99 导致长路径的梯度信号极弱

**前沿方法**：

**1. GAE (Generalized Advantage Estimation) — PPO 的标准配置**
- 使用 λ-return 而非 TD(0)
- `A_t = Σ (γλ)^l · δ_{t+l}` 其中 `δ_t = r_t + γV(s_{t+1}) - V(s_t)`
- λ=0.95 是常用值，平衡偏差和方差
- **直接解决信用分配问题**

**2. 辅助 Reward — Bluff-Aware Reward (Stanford CS224R, 2025)**
- Monte Carlo 识别诈唬机会（弱牌 + 激进行动）
- 成功诈唬（对手 fold）→ 额外 +reward
- 被抓诈唬（对手 call 且摊牌输）→ 额外 -reward

**3. 辅助任务 — Belief-Aware MuZero (2026)**
- Winner prediction head：预测最终胜负概率
- Rank prediction head：预测最终排名
- 这些辅助任务提供密集的训练信号，改善表示学习

**4. CFR-guided Reward (ToolPoker, 2026)**
- 使用预训练的 CFR solver 提供 GTO 动作作为参考
- Reward = 局内 reward + α × |agent_action - GTO_action|
- 需要外部 solver，实施成本高

**实用建议**：

```python
# 改进的 Reward 设计
def compute_reward(env, action, result):
    # 1. 基础 reward（手牌结果）
    base_reward = (final_chips - initial_chips) / big_blind
    
    # 2. 中间步骤 reward（通过 GAE 自动计算，无需手动设计）
    # GAE 会通过 value function 估算每步的边际贡献
    
    # 3. 辅助任务 loss（不直接加入 reward，而是作为额外的训练 loss）
    # - Winner prediction: P(获胜)
    # - Equity estimation: 当前胜率
```

### 2.5 对手建模与对手池

**当前问题**：纯自博弈（self-play vs 自己的过去版本）→ 策略退化

**前沿方法**：

**1. K-Best 对手池 (phulin/poker2, 2024)**
- 维护一个历史 checkpoint 池（K 个最强模型）
- 每个 actor 随机选一个对手对打
- 定期淘汰弱对手、加入新对手
- 防止策略退化到循环均衡

**2. PSRO (Policy-Space Response Oracles, Lanctot 2017)**
- 维护策略种群 {π_1, ..., π_K}
- 每轮计算对当前种群混合策略的 best response
- 新策略加入种群
- Fusion-PSRO (2024) 改进：通过 Nash 策略融合生成更好的 best response

**3. AlphaExploitem 的跨手牌适应 (2025/2026)**
- 核心创新：层次 Transformer 编码跨手牌历史
- 在线学习对手行为模式
- 从 30 局内适应对手风格

**4. 课程式对手渐进 (Stanford CS224R, 2025)**
- WeakOpponent → MediumOpponent → StrongOpponent 三级课程
- 逐步增加难度，确保策略稳健

**对我们最实用的方案**：

```
对手多样性策略（按实施难度排序）：

Level 1 — POK Bot 对手池 (半天实现):
  - 将 bot1-bot6 包装成 HoldemEnv opponent
  - Actor 50% 自博弈 + 50% 随机选 POK bot

Level 2 — 历史 Checkpoint 池 (1 天实现):
  - 保存每 100 cycles 的模型 checkpoint
  - Actor 30% 自博弈 + 30% POK bot + 40% 历史 checkpoint

Level 3 — 在线对手建模 (需要 Transformer 架构):
  - 编码跨手牌历史
  - Attention 机制学习对手行为模式
```

### 2.6 开源项目对比分析

| 项目 | 算法 | 架构 | 游戏规模 | 训练时间 | 推荐度 |
|------|------|------|---------|---------|--------|
| **AlphaHoldem** (AAAI 2022) | PPO (Trinal-Clip) | CNN 伪孪生 | 完整 HUNL | 3 天/GPU | ★★★★★ |
| **phulin/poker2** (2024) | PPO + K-Best / ReBeL-style | CNN/Transformer/MLP | 完整 HUNL | 数天/GPU | ★★★★☆ |
| **dan-k-k/GTO-Poker-AI** (2025) | NFSP | MLP | HUNL | 可中断恢复 | ★★★☆☆ |
| **RL-CFR** (ICML 2024) | RL + CFR | MLP + CFR solver | 完整 HUNL | 需要 CFR solver | ★★★☆☆ |
| **EricSteinberger/NFSP** | NFSP | MLP | Leduc | 数小时 | ★★☆☆☆ |
| **neuron_poker** | DQN | MLP | 简化 Hold'em | — | ★☆☆☆☆ |

---

## 三、改进方案

### 方案评估矩阵

| 方案 | 预期提升 | 实施时间 | 训练时间 | 依赖 |
|------|---------|---------|---------|------|
| **A: DQN 快速修复** | vs Aggro +3-5% | 0.5 天 | 6-8 小时 | 无 |
| **B: PPO 重写** | vs Aggro +10-15% | 3 天 | 1-2 天/GPU | 无 |
| **C: AlphaHoldem 复刻** | 接近 Slumbot | 1-2 周 | 3 天/GPU | 无 |
| **D: ReBeL 架构** | 超越 Slumbot | 3-4 周 | 1 周/GPU | CFR solver |

---

### 方案 A：DQN 快速修复（0.5 天）

**改动最小，验证对手多样性是否有效。**

1. **引入 POK bot 对手**：写 `POKBotOpponent` 类，Actor 50% 自博弈 + 50% 打 bot
2. **精简动作空间**：8 raise 桶 → 3 raise 桶 (half-pot, pot, 2x-pot)
3. **Reward shaping**：惩罚过度 All-in（筹码 > 5×底池时 All-in → -0.5）

**预期**：vs Aggro 55% → 60%，vs CallBot 78% → 82%

---

### 方案 B：PPO 重写（3 天）— 推荐方案

**核心改动：从 DQN 切换到 PPO Actor-Critic。**

#### B1. 算法架构

```
Actor-Critic 网络 (共享 backbone):
  Input: obs (132 维 flat → 后续升级为 tensor)
    ↓
  Backbone: 5 层 MLP (512-1024-512-1024-512)
    ↓
  Policy Head: Linear → softmax → π(a|s)  (11 actions)
  Value Head: Linear → V(s)               (1 scalar)
  
训练:
  - PPO-clip with entropy bonus
  - GAE (γ=0.99, λ=0.95)
  - Trinal-Clip (AlphaHoldem 的改进 PPO)
  - Legal action masking (mask invalid actions before softmax)
```

#### B2. 状态表示升级

```
Phase B2.1 (初始): 保持 132 维 flat vector
Phase B2.2 (升级): AlphaHoldem 式 tensor
  - Card tensor: 6 × 4×13 二值矩阵 (手牌、公共牌)
  - Action tensor: 24 × 4×n_b 二值矩阵 (下注历史)
  - 使用 Conv2D layers 处理
Phase B2.3 (最终): AlphaExploitem 式 token 序列
  - 当前手牌 token 序列
  - 当前手牌行动 token 序列
  - 跨手牌历史 token 序列（层次 Transformer）
```

#### B3. 对手池

```
训练对手来源:
  1. 自博弈 (当前模型)
  2. POK bot1-bot6 (6 种不同风格)
  3. 历史 checkpoint (每 200 cycles 保存一次)

Actor 对手选择策略:
  - 40% 自博弈
  - 30% POK bot (随机选择)
  - 30% 历史 checkpoint (随机选择)
```

#### B4. 训练配置

```
PPO 超参 (参考 AlphaHoldem):
  - lr: 3e-4
  - batch_size: 4096 steps
  - mini_batch: 512
  - PPO epochs: 4 per batch
  - clip_range: 0.2
  - entropy_coef: 0.01
  - γ: 0.99
  - GAE λ: 0.95
  - Trinal-Clip: 额外 clip 项
  
训练规模:
  - 10,000 PPO updates
  - 每个 update 收集 4096 steps
  - 总计 ~40M steps
  - 预计 1-2 天 (RTX 4060)
```

**预期**：vs Aggro 60% → 70-75%，vs CallBot 78% → 85%+，策略更平滑

---

### 方案 C：AlphaHoldem 复刻（1-2 周）

在方案 B 基础上，完整复刻 AlphaHoldem 论文的方法：

1. **伪孪生架构**：两个独立的牌张/行动编码分支，共享后层
2. **多维 tensor 状态表示**：6 通道牌张 + 24 通道行动历史
3. **Trinal-Clip PPO**：三项 clip loss（当前/历史/全局）
4. **自博弈对手选择**：与不同历史版本对战
5. **模型评估与选择**：基于 exploitability 指标

**预期**：接近或击败 Slumbot

---

### 方案 D：ReBeL / RL-CFR 架构（3-4 周）

在方案 C 基础上，引入博弈论核心：

1. **Public Belief State**：用对手持牌概率分布代替单点观测
2. **CFR 子博弈求解**：作为 teacher signal
3. **Value + Policy 双头网络**：ReBeL 式架构
4. **动态动作抽象**：RL-CFR 式自动选择最优离散化

**预期**：超越 Slumbot，接近 GTO Wizard AI

---

## 四、推荐执行路径

```
Week 1: 方案 B — PPO 重写
  Day 1-2: PPO Actor-Critic 实现 + 训练循环
  Day 2-3: POK Bot 对手池 + 对手多样性训练
  Day 3:   训练启动 + 监控

Week 2: 方案 B2 — 状态表示升级
  Day 1-2: AlphaHoldem 式 tensor 编码
  Day 2-3: Trinal-Clip PPO
  Day 3:   从头训练 + 对比实验

Week 3+: 方案 C/D — 完整架构升级
  根据方案 B 结果决定是否继续
```

## 五、参考文献

1. **Reevaluating PG Methods for IIGs** (2025). arXiv:2502.08938. NeurIPS 2025.
   - 7000 runs, PPO > NFSP/PSRO/ESCHER/R-NaD in IIGs
2. **AlphaHoldem** (2022). AAAI 2022. 
   - End-to-end PPO for HUNL, defeats Slumbot/DeepStack, 3 days training
3. **AlphaExploitem** (2025/2026). arXiv:2605.09150.
   - Hierarchical transformer for cross-hand opponent modeling
4. **RL-CFR** (2024). ICML 2024.
   - RL-guided dynamic action abstraction + CFR
5. **Belief-Aware MuZero** (2026). arXiv:2603.27751.
   - Auxiliary winner/rank prediction heads for imperfect info
6. **ToolPoker** (2026). arXiv:2602.00528.
   - LLM + CFR solver + composite reward for poker
7. **Bluff-Aware Reward Shaping** (2025). Stanford CS224R.
   - Curriculum opponents + Monte Carlo bluff detection + LLM feedback
8. **Fusion-PSRO** (2024). arXiv:2405.21027.
   - Nash policy fusion for PSRO diversity
9. **Policy Gradient Convergence in EFGs** (2025). NeurIPS 2025.
   - Theoretical proof: regularized PG converges globally in IIGs
10. **Self-Play Survey** (2024). arXiv:2408.01072.
    - Comprehensive survey of self-play methods in RL

## 六、开源项目

| 项目 | URL | 用途 |
|------|-----|------|
| phulin/poker2 | github.com/phulin/poker2 | PPO + K-Best + ReBeL-style, 多架构 |
| dan-k-k/GTO-Poker-AI | github.com/dan-k-k/GTO-Poker-AI | NFSP + opponent range estimation |
| SarathL754/MARL-Texas-Holdem | github.com/SarathL754/Multi-agent-RL-texas-holdem-aec | PPO + PettingZoo AEC |
| EricSteinberger/NFSP | github.com/EricSteinberger/Neural-Fictitous-Self-Play | NFSP baseline (Leduc) |
