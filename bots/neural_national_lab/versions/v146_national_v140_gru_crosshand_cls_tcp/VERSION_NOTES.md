# v146 — Cross-Hand Encoder + Opponent-Type-Gated Override (CI > 0)

Parent: v145. Three key changes:
1. Cross-hand opponent encoder (20-d behavioural+showdown -> 16-d embedding).
2. Fixed v145 import bug (feature_spec absent from bot dir -> GRU never ran).
3. Opponent-type-aware gate: suppress raise override vs passive opponents
   (PFR<=0.30 AND aggression<=0.30). Fixes v119 regression (-2578 -> +1351).

Model: opp_value_gru_cls_crosshand_h96_seed8001.json, val acc 80.5%, 32454 params.

Result (3 seed blocks x 3 matches, 36 matches, deterministic):
COMBINED bootstrap CI: mean +4703, 95% CI [+544, +8924] -> SIGNIFICANT.
v120 +11642, v135 +4691, v119 +1351, v46 +1129. No nemesis.
Held-out (v66/v40/v57): v146 +320697 (9-0), no collapse. 0 illegal/timeout.
Report: docs/neural-national-v146-report.md
