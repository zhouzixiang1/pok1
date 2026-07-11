# Opponent-Aware Neural National Bot Research Note

Date: 2026-07-10

This note records research decisions for the national-native neural line. It is
not strength evidence and does not change the requirement for paired native TCP
evaluation against the live classic pool.

## Primary Sources And Direct Implications

- [Opponent Modeling in Deep Reinforcement Learning](https://proceedings.mlr.press/v48/he16.html)
  supports joint value/policy learning with explicit opponent-action auxiliary
  supervision. Its mixture-of-experts result motivates a future latent opponent
  mode head instead of hand-tuned opponent categories.
- [Metric Policy Representations for Opponent Modeling](https://arxiv.org/abs/2106.05802)
  motivates opponent-disjoint validation and a contrastive policy embedding for
  unseen classic bots. Identity labels must not be inputs to the deployed model.
- [Deep Counterfactual Regret Minimization](https://arxiv.org/abs/1811.00164)
  uses advantage and average-strategy networks trained from game-tree traversal.
  OpenSpiel's Apache-licensed implementation is retained under the ignored
  `ref/llm_evolution/open_spiel` checkout as a possible offline teacher.
- [Depth-Limited Solving for Imperfect-Information Games](https://arxiv.org/abs/1805.08195)
  shows why a poker leaf does not have a single state value independent of
  continuation strategy. Any online lookahead here must evaluate multiple
  opponent continuations or a public-belief representation, not ordinary
  perfect-information MCTS.
- [ReBeL](https://arxiv.org/abs/2007.13544) supports combining a learned public
  belief value with test-time search. The official repository exposes only
  Liar's Dice, so it is an architectural reference rather than drop-in poker
  code.
- [DecisionHoldem](https://arxiv.org/abs/2201.11580) combines a blueprint with
  safer depth-limited solving over diverse opponent private-hand ranges. Its
  AGPL repository is retained read-only under
  `ref/llm_evolution/decisionholdem`; core real-time search remains compiled,
  so no code is copied into the bot.
- [RL-CFR](https://arxiv.org/abs/2403.04344) learns dynamic action abstractions.
  This supports predicting a small legal raise set before search instead of
  expanding arbitrary chip amounts.
- [Real-Time Parallel CFR](https://arxiv.org/abs/2605.19928) reports a practical
  CPU/GPU pipeline with batched neural leaf evaluation. It makes bounded CFR a
  credible use of the 60-second national window, but only after a protocol-
  aligned game model and value teacher are validated.
- [PokerSkill](https://arxiv.org/abs/2605.30094) uses a deterministic context
  engine to retrieve layered expert skills before asking an LLM. Its official
  CC BY-NC repository is retained under `ref/llm_evolution/PokerSkill`. For this
  project, the safe use is offline scenario generation, failure explanation,
  and distillation; the formal bot must not call an online LLM.

## OpenSpiel Opportunity And Gaps

OpenSpiel now includes a repeated-poker wrapper with:

```text
max_num_hands=70, reset_stacks=True, rotate_dealer=True
```

and an HUNL game string with 20000 stacks and 50/100 blinds. This is unusually
close to the national match format and is worth testing as an offline CFR/Deep
CFR teacher. It is not yet protocol-equivalent. Before using its labels, tests
must compare:

1. legal action sets and raise-to semantics at every street;
2. the national strict consecutive-raise rule;
3. postflop first/second pass wire conventions;
4. all-in runout and settlement values;
5. seat rotation, card mapping, and 70-hand accumulated return.

OpenSpiel and Torch remain training dependencies only. Submitted bots stay
stdlib-only and carry exported weights.

## Frozen Architecture Experiments

The first formal temporal dataset uses 16 public features per completed hand,
strictly before the current hand, truncated to 32 hands. The planned comparison
uses identical splits, seeds, heads, losses, and model-selection rules:

| Encoder | Purpose |
|---|---|
| aggregate MLP only | Existing non-temporal ablation |
| temporal GRU | Current low-latency primary path |
| Deep Sets | Tests whether order adds value beyond hand distribution |
| small Transformer over prior hands | Tests longer-range mode changes and recency |
| GRU plus mixture-of-experts | Learns soft latent opponent modes without hard types |

Selection uses validation opponents only and whole-match clustered bootstrap.
Calibration and held-out opponents remain untouched until the architecture and
ensemble seeds are frozen. A larger model wins only if the median validation
score improves across seeds and stdlib inference stays safely within the
national time budget.

The implemented sweep now spans roughly 26-thousand to 4.9-million parameters.
On this machine, untrained xlarge exports required about 296-977 ms for one
stdlib value-plus-response inference at maximum history length. The sweep
records this measurement for every seed and rejects architectures when the
estimated sequential latency of the complete seed ensemble exceeds a
configurable runtime budget. These timings establish deployability only; model
quality remains entirely data- and evaluation-dependent.

## Execution Order

1. Finish and freeze `matchscope_v152_temporal` with content hashes.
2. Run GRU/Deep-Set/Transformer/MoE scaling experiments on identical splits.
3. Select an uncertainty-driven policy using clustered match bootstrap.
4. Prove active gains by native TCP ablation and live-classic paired evaluation.
5. In parallel, build an OpenSpiel national-semantics differential test before
   accepting any CFR teacher labels.
6. Consider bounded public-belief search only after the single-pass neural
   candidate is protocol-clean and measurably beneficial.
