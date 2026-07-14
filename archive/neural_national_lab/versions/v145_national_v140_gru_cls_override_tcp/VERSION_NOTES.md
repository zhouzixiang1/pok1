# v145 — GRU Classification Override Gate

Parent: v144 (shadow). v145 enables the override gate with the classification
GRU value net.

## Model
opp_value_gru_cls_h96_es12_seed4002.json — GRU opponent value net trained as
a **classification** task: predicts P(candidate legal action's chip-delta > 0)
per legal action. Val accuracy 79.6%, held-out accuracy 70.3%, ECE 0.06-0.13.
29,718 params. Pure-Python runtime (sigmoid applied to head logits).

## Override gate
gru_opp_value_override=true. Switches from the rule action to a small
constructive raise candidate only when:
- stage in preflop/flop/turn
- candidate is a raise-type label (raise_half/raise_pot), not fold/call/allin
- P(candidate > rule) >= cls_min_prob (0.80)
- P(rule action is good) <= cls_rule_max_prob (0.55)
- candidate amount in (0, 1400] and passes _candidate_action legality
- opponent profile has >= 3 observed actions

## Ablation result
v145 vs strongest classic pool (v46, v120, v119, v135), seed5200 m2 paired,
deterministic bot seeds: **identical to v140** (-363, 4-2-2, 0 illegal/timeout).
The conservative gate did not fire on a net-positive decision in these 8
matches, so v145 neither helps nor harms. This is expected: the gate is
intentionally strict. Lower thresholds risk harmful overrides at 70% held-out
accuracy.

## Status
Pipeline complete and safe. The override is conservative (no regression) but
not yet strong enough to measurably beat the rule bot — needs more/better data
and/or a cross-hand opponent encoder to find higher-value spots.
