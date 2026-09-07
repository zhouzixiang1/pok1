# Master Scout 吞吐修复（2026-09-07）

6 小时窗口（约 16:50–22:50）LLM 占用保持 4/4、约 188 万 tok/h，但 **0 出版**：
v328–v334 连续放弃，全部停在规划关（`master_exhausted` /
`master_analysis_failed` / `cycle_timeout_master_stuck` /
`crossover_llm_exhausted`）。文献探针弱点槽已正确，不是 identity 问题。

## 死因

1. **三个 Scout 凑不齐合法提案**（`three_distinct_schema_valid_scout_proposals_required`）。
   高频拒码：`proposal_snapshot_evidence_too_many`、
   `proposal_worker_binding_cannot_fit_minimum_prompt`（binding 11k–12.6k，
   `provider_budget_chars` 为负）、
   `proposal_mechanism_shared_leaf_requires_full_namespace:fold_to_raise`、
   `proposal_cited_sample_too_small`（H2H 行 max≈37，过不了聚合 200）。
   schema retry 的 pin/budget/sample 提示没有 renderer，或 pin 撞上已占用
   `change_symbol`。
2. **饱和器抢许可**：池满后 45s 才让路，90s cooldown 挡住第二/第三个
   Scout；让路后同一 tick 立刻 refill。
3. **Crossover stall 被夹在 180s**：CROSSOVER 桶没豁免 generic stall clamp，
   GLM `effort=max` 思考静默 >180s 即杀流（v334/v335）。

## 修复（不关 saturator，cap 仍为 4）

- Scout：聚合 list 指针绑定最强行 `games`；超过 3 条 snapshot 引用保留最强
  3 条而不是整包拒绝；worker binding 机械裁剪；碰撞 pin 降级为 avoid；
  OpponentTracker 真实叶子合法；未知叶子不再级联 shared-leaf 误杀；
  修复提示渲染 pin/budget/sample。
- Crossover 能力快照重验证转发 preplan 已冻结的 capability 对象，避免
  父探针重跑与 static-only 锚点不一致。
- Saturator：15s 让路；`waiting>=2` 忽略 cooldown；让路后 holdoff 不 refill。
- CROSSOVER 豁免 180s stall clamp；`env.runtime` 对齐 Master/Worker 超时。

强度门（30/200 两档、完整 70 手 native TCP）未放宽。
