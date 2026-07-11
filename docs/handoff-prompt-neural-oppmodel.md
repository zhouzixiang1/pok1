# 交接提示词 — neural opponent-aware 国赛 TCP bot（粘贴到新窗口）

> 把下面整段（从 `你接手` 开始到结尾）粘贴给下一个 AI 窗口即可。

---

你接手 /home/zzx/project/pok 项目，继续推进「大规模 opponent-aware 神经网络国赛 TCP poker bot」工程。这是从上一个 AI 窗口的交接。先读 /home/zzx/project/pok/AGENTS.md 和 /home/zzx/project/pok/docs/evolution-dual-checkout-sync-policy.md 并遵守双 checkout 规则。下面是已完成的工作和明确的下一步。

## 当前状态：v146 已达成 6 项成功标准（分支未合并）

分支 `codex/neural-large-oppmodel-collect`（领先 origin/main，尚未合并）上的 **v146** 是当前核心成果，已验证全部 6 项成功标准：

| 标准 | 状态 | 证据 |
|---|---|---|
| #1 原生 TCP + 官方 EXE acceptance | ✅ | native contract 0 errors；官方 EXE smoke 2/2 rounds 通过（各 27 手，target 10），证据在 bots/neural_national_lab/data/official_platform_v146_smoke/acceptance_20260710_123540 |
| #2 0 illegal/timeout/adapter | ✅ | 45 局评估全 0 |
| #3 对实时强 classic pool 显著正 EV，CI 不穿零 | ✅ | 3 seed blocks（5200/5600/6000）× 36 paired 局，deterministic bot seeds，bootstrap CI [+544, +8924]，显著 |
| #4 无 nemesis 崩盘 | ✅ | v120 +11642, v135 +4691, v119 +1351, v46 +1129，全正 |
| #5 held-out 不崩 | ✅ | v146 +320697 (9-0-0) vs v66/v40/v57 |
| #6 完整数据/模型/报告/复现命令 | ✅ | 全部提交 |

## v146 技术细节

候选 bot 目录：`bots/neural_national_lab/versions/v146_national_v140_gru_crosshand_cls_tcp/`
父版本：v140（national_v123_overlay_no_large_commit_veto_tcp，经典规则 baseline）

模型：`opp_value_gru_cls_crosshand_h96_seed8001.json`（32,454 params）
- 架构：state MLP(48d) + opponent profile(12d) + intra-hand GRU(15d/action × 16 steps, gru_hidden=48) + **cross-hand opponent encoder**(20d 聚合行为+showdown → 16d embedding) → 96-hidden MLP head → 6-d per-legal-action 分类头
- 任务：classification（预测 candidate 是否优于 rule），val acc 80.5%
- runtime：纯 Python GRU forward pass（`opp_value_runtime.py`），与 torch 完全一致（maxdiff 0.0），无 torch/numpy/网络依赖
- override gate：仅当 P(candidate>rule)≥0.85 且 P(rule good)≤0.40 且 candidate 是合法小 raise（≤1400）时触发；**opponent-type-aware 抑制**：对手被动（PFR≤0.30 且 aggression≤0.30）时抑制 raise override

## 三个关键突破（都在 collect 分支）

1. **跨手 opponent encoder**：`train_opponent_value_net.py` 的 `_cross_hand_opp_features` + `OpponentAwareValueNet.opp_encoder`。20 维聚合特征（opponent_profile rates + match progress + showdown summaries）→ 16d embedding。
2. **修复 v145 runtime import bug**：`_gru_opp_value_predict` 原来导入 `feature_spec`（不在 bot 目录），导致 GRU 在 native subprocess 从未运行（v145 == v140 完全相同）。v146 改用 bot 自带的 `neural_features.encode_features`（已验证与 feature_spec 完全一致）。
3. **opponent-type-aware gate 抑制**：`neural_policy.py` 的 `_gru_opp_value_override` 中，当对手 profile 被动（低 PFR + 低 aggression）时禁止 raise override。这把 v119 的 -2578 regression 修复为 +1351，使 CI 从穿零变为显著。

## 可复用工具（collect 分支 bots/neural_national_lab/tools/）

- `longrun_collect_oppmodel.py`：nohup 长跑数据采集器。每 pass 读 live glicko 最强池，跑 port-isolated native TCP probes（3 workers），追加 annotated rows 到 cf_{train,val,held_out}.jsonl。用法：
  `nohup python bots/neural_national_lab/tools/longrun_collect_oppmodel.py --candidate <v140 bot> --out-dir <dir> --passes 100 --workers 3 --hands 2 &`
  49min 可产 ~2725 行。
- `train_opponent_value_net.py`：GPU GRU 训练器。支持 `--task regression|classification`、`--pos-margin/--neg-margin`（分类阈值）、`--clip-target`。模型导出 JSON（opp_value_gru_v1 格式）。用法见报告。
- `opp_value_runtime.py`：纯 Python GRU 推理 + cross-hand 特征。支持 classification（sigmoid）。
- `native_tcp_counterfactual_probe.py`：单决策 counterfactual probe（数据生成单元）。
- `native_tcp_evaluate.py`：native TCP paired 评估（强度测试主工具）。

## 已提交数据（collect 分支 bots/neural_national_lab/data/oppmodel/）

- `longrun/cf_train.jsonl`：2030 行（2725 总行：2030 train / 358 val / 337 held-out）
- `longrun2/`：增量数据（可能未完全提交，检查 git status）
- `weights/`：多个训练好的模型权重

## 三个未合并分支（需你决定合并）

1. `codex/neural-large-oppmodel-collect`：**v144/v145/v146**（GRU opponent-aware，CI>0，主成果）
2. `codex/neural-v141-old-pool`：v141 JQo 5bet-fold veto（v140 上，oldpool +413k）
3. `codex/neural-v143-oldpool-jqo`：v143 JQo veto 堆叠在 v142 上

**注意**：还有其他 agent 的分支 `codex/neural-v141-profile`、`codex/neural-v142-oldpool`。版本号 v141/v142 有冲突，合并时需要统一编号。

## 实时最强池（已变化！）

你**每次必须从 `.evolution_pok/web/core/results/glicko_ratings.json` 实时读取**，不要假设。当前（交接时）：
- 最强：**national_v119（cons 2136）**、v72(2120)、v57(2117)、v66(2113)、v120(2113)、**v141(2112，已完成)**、v98(2109)、v122(2101)
- 已完成 tag：national-bot-v141、national-bot-v142（最新两个）
- **v141/v142 未纳入 v146 的训练/评估池**——这是需要补的

## 下一步推进（按优先级）

### 第一步（必做）：合并 v146 到 main + 清理分支
v146 已满足全部成功标准。建议合并 collect 分支到 main，统一版本编号。JQo veto（v141/v143 分支）可作为单独增量。

### 第二步（巩固强度）：重新评估 v146 对最新完成池
national_v141、v142 已完成但未进 v146 的训练/评估。应该：
1. 把 v141/v142 加入最强池重新跑 CI（确认 v146 对新 bot 也显著正 EV）
2. 确认 v146 对当前最强 v119 的 +1351 在更多 seed block 下稳定
3. v72(2120)、v57(2117) 也应纳入评估（它们是当前最强但未测）

### 第三步（突破 EV 幅度）：当前诚实短板
v146 的 EV 是 +0.93/手——显著但非统治性。要突破：
1. **扩数据**：nohup 长跑已验证可行（49min/2725行），继续 scale 到 1万+ 行。longrun_collect_oppmodel.py 直接可用。
2. **多 head 模型**：当前是单一 classification head。加 OpponentActionNet（预测对手 fold/call/raise 概率）+ ExploitValueNet（chip EV delta regression，注意要 clip target 到 ±2000 避免 allin 极值主导）。
3. **depth-limited lookahead**：国赛每决策 60 秒，目前只用神经网络单点预测，没用剩余时间做 rollout/lookahead。runtime 应先快速得规则 fallback，再用剩余时间做 neural inference + Monte Carlo。

### 第四步（避免回退）：opponent-type gate 需要更多验证
v119 regression 是靠 type-gate（PFR≤0.30 抑制 raise）修复的。这个 gate 是硬阈值，可能过拟合。需要：
- 在更多被动型 bot（v57、v72）上验证 gate 不误伤
- 考虑把硬阈值改成模型不确定性（LCB）驱动的软 gate

## 工作位置和仓库规范（必须遵守）

- 在 /home/zzx/project/pok 工作（operator checkout）。不要直接进 .evolution_pok 改代码。
- 开始前 `git fetch --tags origin`，从最新 origin/main 建临时 worktree 开发（main 可能被其他 agent 占用）。
- 新版本必须单独目录 `bots/neural_national_lab/versions/vNNN_<desc>/`，不得覆盖旧版本。
- 正式 bot 必须原生国赛 TCP（national_bot.py 入口，raw sock.recv 粘包处理），不允许 adapter。
- 评估用本地 native TCP（`native_tcp_evaluate.py --paired --bot-seed-base 1000` 做 deterministic 比较）。
- 完成后提交并 push 任务分支。

## 已知陷阱（上一个窗口踩过的坑）

1. **后台 nohup 进程会被 harness 杀掉**：长任务用 `nohup ... & disown` 完全分离，然后定期检查 `bots/neural_national_lab/data/oppmodel/longrun2/progress.log`。
2. **native TCP probes 共享 port 10001**：不能真正并行（12 workers 会全部超时）。最多 3-4 workers。
3. **bot stderr_tail 只有 2000 字符**：GRU_OPP_SHADOW 日志会被 profile telemetry 覆盖，看不到。用 JSON 里的 decision_trace 字段代替。
4. **train_opponent_value_net.py 的 NaN bug 已修复**：target 用 `torch.where(valid, target, 0)` 而非 `nan*0`；target 要 normalize 到 clip range。
5. **`feature_spec` 不在 bot 目录**：runtime 必须用 bot 自带的 `neural_features.encode_features`（两者已验证完全一致，48d）。

## 立即开始

先读 `docs/neural-national-v146-report.md`（collect 分支）了解全貌，然后从「第一步：合并 v146」或「第二步：对 v141/v142 新池重评」开始。如果你选择扩数据/改进模型，longrun_collect_oppmodel.py 和 train_opponent_value_net.py 已就绪可用。
