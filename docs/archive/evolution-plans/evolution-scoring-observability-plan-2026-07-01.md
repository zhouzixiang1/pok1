# 进化评分与日志可观测性修复方案（2026-07-01）

## 结论

后台对战评分不能只回答“谁的点估计最高”。德州扑克 bot 对战方差高，调度又不是完全均匀随机，用户和进化系统都应该使用同一套强度口径：

- `selection_score`：进化机械选择用分数，基于 `leaderboard_score` 并对低置信行降权。
- `leaderboard_score`：用户展示的综合强度，混合 active-pool H2H、H2H 覆盖、H2H 局数、保守 Glicko、RD 和总胜率。
- `h2h_avg_wr`：只作为 matchup 证据，不能单独当“最强”排序依据。
- `strength_confidence` / `strength_note`：必须和分数一起展示，避免低样本 bot 被误读。

## 文献依据

- Glicko-2 的核心价值是同时维护 rating、RD 和 volatility；RD 表示评分不确定性，所以排序应使用保守下界或显式置信度，而不是裸 rating。
- TrueSkill/TrueSkill2 把 rating 系统描述为带假设的概率生成模型；模型质量取决于假设是否贴近比赛机制。对本项目来说，样本覆盖、对手选择和高方差都必须进入解释。
- Bradley-Terry 模型说明 pairwise comparison 可以估计相对强度，但结果本身是概率性的；相互克制和小样本会让单一胜率不稳定。
- 近年的 Elo 理论分析指出 Elo 有在线更新和解释性优势，但存在固有 bias/variance；调度分布会影响收敛与估计质量。

参考：

- Mark Glickman, Glicko-2 rating system: https://glicko.net/glicko/glicko2.pdf
- Herbrich, Minka, Graepel, TrueSkill: https://www.microsoft.com/en-us/research/wp-content/uploads/2006/01/TR-2006-80.pdf
- M. E. J. Newman, Efficient Computation of Rankings from Pairwise Comparisons: https://www.jmlr.org/papers/volume24/22-1086/22-1086.pdf
- Olesker-Taylor and Zanetti, An Analysis of Elo Rating Systems via Markov Chains: https://proceedings.neurips.cc/paper_files/paper/2024/file/f9db8bd38c36391ddc4ccc0d23effdbe-Paper-Conference.pdf

## 当前代码对齐情况

已经对齐：

- `web/core/rating_snapshot.py` 统一生成强度行，按 `selection_score` 排序，并输出覆盖度、样本量、来源和置信说明。
- `/api/ratings`、`/api/bots`、`/api/data/stream` 都使用统一强度快照。
- `tool_helpers._select_precommit_opponents`、crossover parent 选择使用 `selection_score`。
- prompt 已要求 Master/Analyst 使用 `leaderboard_score`、覆盖度和 RD，而不是单独使用 `h2h_avg_wr`。
- LLM role 生命周期日志已有 start、first_activity、progress、done、failed/cancelled。

本轮补齐：

- source-loop fallback 从 conservative Glicko leader 改为 unified `selection_score` leader，缺数据时才回退 conservative rating。
- LLM stream silent watchdog：即使 SDK 长时间不产生新 message，也记录 `pipeline.llm_role_stream_silent`，方便判断“进程活着但流静默”。

## 多角色审计分工

- 文献/统计角色：确认评分系统必须显式样本覆盖、不确定性和调度偏差。
- 后端评分角色：确认统一快照、API、source selection、precommit opponent 选择都使用同一强度口径。
- 前端用户角色：确认默认榜单不把裸 H2H 或裸 rating 包装成“最强”，并显示置信信息。
- 运行日志角色：确认重启、LLM role、daemon、precommit、commit/abandon 都有可追踪事件。

## 用户侧规范

- 默认榜单按 `selection_score` 或 `leaderboard_score` 展示；标题要避免只写“评分最高”。
- 每个强度结论旁边显示 H2H 覆盖、H2H 局数、RD 和 `strength_confidence`。
- H2H 胜率排序可以保留为诊断视图，但必须被理解为 matchup/样本视角，不作为默认“最强”口径。
- 当用户问“哪个 bot 最强”时，回答应包含：当前榜首、分数、置信度、覆盖、样本局数、数据源，以及是否只是暂时领先。

## 进化系统规范

- 父代选择、交叉父代、source-loop repair、precommit top opponents 都应优先用 `selection_score`。
- `leaderboard_score` 可用于解释和趋势展示；低置信 bot 不能直接成为机械选择的唯一依据。
- `h2h_avg_wr` 用于定位弱点和挑 nemesis，不用于全局强度排序。
- precommit 仍以 parent/top/weak/nemesis 分层，blocking 只来自 parent/top/weak 和 aggregate net-chips gate；nemesis/PSRO 继续 telemetry-only。
- 如果 active pool 发生大量增删，应以 match_history 重建 active-pool H2H，避免稀疏 `head_to_head.json` 误导。

## 日志与验收

需要能从 `web/core/results/events.jsonl` 回答以下问题：

- 当前代处于哪个 stage，run_id 和 attempt 是什么。
- 每个 LLM role 是否启动、何时首包、是否持续输出、是否静默、是否完成/失败/取消。
- precommit 选择了哪些对手，选择依据是什么。
- commit、archivist、reap、priority eval、daemon restart 是否有明确事件。

实机验收：

- 通过 `scripts/pok_restart_observe.sh` 重启。
- 跟踪至少 3 个 terminal generation events：`pipeline.commit_done`、`pipeline.archivist_done`、`pipeline.abandoned`、`pipeline.master_exhausted` 或 timeout 类事件。
- 同时检查 LLM role 日志没有长时间无事件盲区；若出现 `pipeline.llm_role_stream_silent`，应能从字段判断是哪个 role、静默多久、累计 message/tool/text 计数。
