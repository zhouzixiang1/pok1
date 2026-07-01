# 进化源选择与评分解释修复记录（2026-07-01）

## 背景

v236-v238 实跑后，系统暴露出两个根问题：

1. 用户看到的“强”容易被理解为绝对强度，但后台实际应区分榜单分、进化选择分、样本量、RD 和 H2H 覆盖。
2. 进化系统的 source-v oscillation 检测只读 `pipeline.prepare_done`，没有以成功提交的 lineage 为准，导致重启/失败/旧 prepare 事件放大为“震荡”，并在 v238 后继续把策略拉回 `v235×v206`。

另一个运行卫生问题也被定位：`post_generation_cleanup` 在 archivist housekeeping commit 之后还会合并 `experience_pool.md`，但旧逻辑没有配套提交，因此 v238 后留下了经验池脏项。

## 文献依据

- Glicko-2 将选手强度表达为 rating、RD、volatility，并建议用 `rating ± 2*RD` 这样的区间表达置信强度；因此进化选择不能只看点估计，必须把 RD/样本覆盖纳入机械选择。来源：[Glicko-2 example, Mark Glickman](https://www.glicko.net/glicko/glicko2.pdf)
- UCB/多臂老虎机文献把选择问题表述为 exploration vs exploitation，并用置信上界处理“不确定但可能好”的臂；这支持我们把用户榜单展示和进化选择分拆开，而不是把低样本高点估计当作已验证强者。来源：[Auer, Cesa-Bianchi, Fischer 2002](https://homes.di.unimi.it/~cesabian/Pubblicazioni/ml-02.pdf)
- MAP-Elites 的目标不是只返回单个最高分个体，而是保留多个高质量且行为不同的精英；这对应本系统中 selection_score + 行为 fingerprint/archive 的方向。来源：[Mouret & Clune 2015](https://arxiv.org/abs/1504.04909)
- Competitive coevolution 的 Hall of Fame/Archive 思想用于防止遗忘旧策略，但 archive 应帮助保持泛化，不应让当前进化被少数旧 source 反复拉回。来源：[Rosin & Belew 1997](https://dl.acm.org/doi/10.1162/evco.1997.5.1.1)

## 修复方案

### 面向用户

- 保留用户可理解的 leaderboard score，但同时展示/解释 selection score、strength confidence、H2H 覆盖、H2H 局数和 RD。
- “某 bot 强”解释为“在当前样本与置信约束下更适合被推荐/选择”，不是绝对强。
- 对低样本或高 RD bot 明确提示：进化选择分已降权。

### 面向进化系统

- source-v 历史优先读取 `pipeline.committed`，只在旧日志没有 commit 事件时 fallback 到 `pipeline.prepare_done`。
- oscillation 仍作为 backstop，但不能抢在可信 stagnation/crossover 和新强者之前：
  - 若当前 leader 已在震荡集合内，视为收敛而不是震荡。
  - 若 stagnation 高/中置信，交给正常 selection-score crossover 选择器，不再强制使用震荡集合高低配。
  - 若震荡集合外存在高/中置信、selection_score 明显高于震荡集合的 bot，则从该 bot 继续 master；多个近似并列时优先更新版本，避免倒退到旧冠军。
- post-cleanup 的 `experience_pool.md` 合并写入后，若工作树原本干净，则只提交 `web/core/experience_pool.md`；若已有脏项，则跳过并记录告警，避免吞掉用户/任务改动。

## 重启后三代观察点

必须观察以下事件：

- `pipeline.source_oscillation_breakout`：说明震荡被可信外部 source 打断。
- `pipeline.source_oscillation_deferred`：说明高/中置信 stagnation 接管了 crossover 选择。
- `pipeline.crossover_decided`：若 trigger 仍是 `oscillation`，检查是否确实没有可信 breakout。
- `pipeline.post_cleanup_experience_commit_staged` / `pipeline.post_cleanup_experience_commit_done`：说明经验池合并不再留下脏项。
- `pipeline.post_cleanup_experience_commit_skipped`：若出现，必须检查 `preexisting_dirty` 是否来自用户改动或运行中旧进程。
- `pipeline.redundant_tool_call`、`pipeline.quality_failed`、`pipeline.precommit_eval`、`orchestrator.crashed`：作为重启后三代异常重点。

## 多 agent 说明

本轮两次尝试启动 explorer 子 agent，工具均返回 `agent thread limit reached`。因此实际调研采用本地并行审计：代码路径、事件日志、git 状态、运行进程、评分数据和外部文献同时交叉验证。后续若 agent 槽位释放，应再补一轮只读 review。
