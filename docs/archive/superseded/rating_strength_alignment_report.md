# 对战评分与演化强度口径对齐报告

日期：2026-06-30

## 结论

后台对战执行和 Glicko-2 批量更新本身基本合理，主要问题在消费端：排行榜、父代选择、precommit 对手选择、LLM 提示词曾把稀疏 `head_to_head.json` 的 `h2h_avg_wr` 当成唯一强度指标。这样会把“只打过少数对手且样本偏好”的 bot 推到榜首，也会误导演化系统继续沿错误父代和错误弱点做计划。

本次修复把用户侧和演化侧统一到 `leaderboard_score`：

- H2H 仍然保留，但必须携带 `h2h_coverage`、`h2h_games`、`h2h_source`。
- 当 `match_history.jsonl` 能重建出更完整的 active-pool H2H 时，优先使用重建结果。
- 综合强度使用 active-pool H2H、保守 Glicko、RD 不确定性、总体胜率混合计算。
- LLM 提示词不再把 `h2h_avg_wr` 称为 canonical skill metric。

## 现状证据

当前 active pool 为 30 个 bot，理论 active pair 数为 435。

修复前的主要风险：

- `/api/ratings` 直接按 `h2h_avg_wr` 排序。
- `generation_scheduler._pick_crossover_parents()` 直接按 `load_h2h_avg_winrates()` 排父代。
- `combined_analyst.md`、`orchestrator.md`、`master_prompt.md` 等提示词明确写着 H2H 是 primary/canonical 指标。
- `head_to_head.json` 是当前 daemon 内存/保存视图，可能因为重启、轮转、保存时机而比 `match_history.jsonl` 稀疏。

本次调试时的真实快照：

- `head_to_head.json` 覆盖 93/435 active pairs，覆盖率 21.4%。
- `match_history.jsonl` 可重建 435/435 active pairs，覆盖率 100%。
- 修复后新榜首来自完整 active-pool 重建数据，而不是稀疏单 pair H2H。
- `claude_v205` 从稀疏 H2H 榜首回落到综合强度中游：`leaderboard_score=0.4967`，`h2h_avg_wr=0.5037`，`h2h_games=1130`，`rank=16`。

## 文献和工程依据

- Glicko-2 明确把 rating 和 rating deviation 分开，RD 表示不确定性；排行榜不能只看均值。参考：Mark Glickman, Glicko-2 rating system, http://www.glicko.net/glicko/glicko2.pdf
- TrueSkill 使用贝叶斯技能分布，核心思想同样是“技能估计 + 不确定性”，并用保守技能做匹配/排序更稳健。参考：Herbrich, Minka, Graepel, TrueSkill, https://www.microsoft.com/en-us/research/publication/trueskilltm-a-bayesian-skill-rating-system/
- TrueSkill 2 继续强调从历史结果中联合估计技能，并把不确定性作为系统状态的一部分。参考：https://www.microsoft.com/en-us/research/publication/trueskill-2-improved-bayesian-skill-rating-system/
- Whole-History Rating 用完整历史而不是最新局部片段估计棋类/对战强度；这支持从 `match_history.jsonl` 重建完整 active-pool H2H，而不是只信当前稀疏 H2H 文件。参考：https://www.remi-coulom.fr/WHR/
- Bradley-Terry 成对比较模型说明“成对结果”可以构成强度估计，但稀疏成对矩阵需要建模或补全，不能把单个 pair 的胜率当总体强度。参考：https://doi.org/10.1093/biomet/39.3-4.324

## 修复设计

新增统一模块：

- `web/core/rating_snapshot.py`

核心职责：

- 从 `head_to_head.json` 和 `match_history.jsonl` 二选一，优先覆盖率更高的 active-pool H2H。
- draw 按 0.5 胜计算，与 Glicko draw 语义一致。
- 产出统一 row 字段：`leaderboard_score`、`rank_basis`、`strength_confidence`、`h2h_avg_wr`、`h2h_weighted_wr`、`h2h_games`、`h2h_coverage`、`h2h_source`。
- 排序按 `leaderboard_score`，不再按裸 `h2h_avg_wr`。

综合分当前口径：

```text
h2h_reliability = min(coverage / 0.8, 1) * min(h2h_games / target_games, 1)
target_games = max(100, opponents_total * 10)
rating_score = clamp(0.5 + (r - 1500) / 800)
conservative_score = clamp(rating_score - rd / 700)
leaderboard_score =
  h2h_score * (0.55 * h2h_reliability)
  + conservative_score * (0.80 - 0.45 * h2h_reliability)
  + stats_score * (0.20 - 0.10 * h2h_reliability)
```

解释：

- H2H 覆盖足、样本足时，H2H 是最大权重。
- H2H 稀疏时，自动退回更依赖保守 Glicko 和总体胜率。
- RD 越高，保守评分越低，防止高波动 bot 被过早推上榜首。

## 用户侧变化

- `/api/ratings` 默认按 `leaderboard_score` 排序。
- rating row 增加覆盖率、样本量、来源、强度置信字段。
- `/api/bots` 和 SSE 数据流使用同一套强度快照。
- 前端 Overview 首屏主数字改为“综合强度”，H2H 和覆盖率作为旁证展示。
- Bot 管理页默认排序改为“综合强度”，仍可手动切到 H2H 或版本。

## 演化侧变化

- `_pick_crossover_parents()` 改为按 `load_strength_scores()` 选择父代。
- precommit top opponents 改为 `top_strength`。
- `get_status`、orchestrator context、combined analyst、stagnation analyzer、agent review 都展示 score + H2H + coverage。
- daemon 保存周期检测 H2H 覆盖率：当 `match_history.jsonl` 重建覆盖更高时，自动回填 `head_to_head.json` 并写 `rating.h2h_rebuilt_from_history` 系统事件。
- `rating_history.jsonl` 新快照会附带 `leaderboard_score`、`h2h_coverage`、`h2h_source`。
- 提示词不再写 “H2H is canonical skill metric”，改为 “H2H 是 matchup evidence，综合强度才是排序依据”。

## 重启后观察项

重启后至少观察三代或三个 save cycle：

- `/api/ratings` 前五名是否稳定带有 `h2h_source=match_history_rebuilt` 或覆盖率足够的 `head_to_head`。
- `web/core/results/system_events.jsonl` 是否出现 `rating.h2h_rebuilt_from_history`，且没有重复刷屏。
- `rating_history.jsonl` 新增快照是否包含 `leaderboard_score`。
- `pipeline_state.json` 的 source/parent 选择是否不再引用稀疏 H2H 榜首。
- 新 prompt 生成的 master/combined analysis 是否同时提到 score、coverage、RD，而不是只说 H2H。

