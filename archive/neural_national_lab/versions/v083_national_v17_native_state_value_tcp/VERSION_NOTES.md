# v083_national_v17_native_state_value_tcp

Native national TCP state-value prototype forked from
`v082_national_v17_trace_force_tcp`.

Change:

- Keeps the national v17 rulebase and direct `national_bot.py` TCP entrypoint.
- Loads `native_state_value_h16_seed1201.json`, trained from native TCP
  single-decision counterfactual probes against `national_v3`, `national_v9`,
  `national_v17`, and `national_v18`.
- Adds `multi_action_value_feature_set="state"` support in `neural_policy.py`
  so the value head can consume the 48-dimension native state features emitted
  by `tools/native_tcp_counterfactual_probe.py`.
- Enables only conservative preflop fold-to-call proposals:
  `multi_action_value_propose_labels=["call"]`,
  `multi_action_value_propose_rule_labels=["fold"]`, small paid-call caps, and
  no raise/all-in proposal path.

Training evidence:

- Dataset:
  `data/native_tcp_value_v082_top_rules_d16_state_clip2000.jsonl`.
- Rows: 16 decision rows, 47 masked action targets, all preflop.
- Training used CUDA via `train_multi_action_value.py`.
- Metrics:
  `data/native_tcp_value_v082_top_rules_d16_state_h16_seed1201_metrics.json`
  had validation best-label accuracy 0.25, so this is not a promotion-grade
  model.

Interpretation:

- v083 is an end-to-end native neural integration smoke candidate.
- It should be evaluated only as a cautious probe; it is not evidence of
  comprehensive domination over rule bots.
