# Iteration Note: Bidirectional HL Calibration

## Observation
- A single-direction 10-group run (`20260527_214848_iter01_pre`) marked the candidate `RED` even though `1.py` and `bots/bot001/main.py` had identical hashes.
- Pair-level inspection showed player/seat bias: the candidate was always evaluated as player 0, so player-id asymmetry and stochastic equity estimates leaked into the result.

## Change
- `hl_loop.py run` now runs each baseline in both directions by default:
  - forward: candidate as player 0
  - reverse: candidate as player 1
- Matchup logs now record `candidate_slot`, and analysis computes wins and chip differential from the candidate's actual slot.
- Same-code control runs are detected by sha256 and reported as `YELLOW` observation runs instead of policy-improvement evidence.
- Added `test_hl_loop.py` to lock candidate-slot aggregation and control-run classification.

## Result
- Formal bidirectional control run: `20260527_220516_iter01_bidirectional_10`
- Result: `YELLOW` control run, `avg_mirror_chip_diff = 540.0`, `illegal_or_crash_count = 0`, `sanitized_action_count = 0`.
- Interpretation: harness is now suitable for comparing distinct candidate snapshots, but same-code control runs should not be used as merge evidence.

## Next Loop
- Create a real candidate snapshot after a narrow policy change, then compare it against `bot001`.
- Continue inspecting extracted anti-lock and probe hands, but require a distinct candidate-vs-baseline signal before changing `1.py` strategy.
