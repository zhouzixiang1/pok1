---
name: aivat-phase1-broken-detection-real-judge-log
description: AIVAT Phase 1 (commit d2fad78) all-in detection + realized-delta extraction are broken against real judge logs; latent (flag default OFF, no caller enables it) but ON-path is fundamentally biased.
metadata:
  type: project
---

Phase 1 AIVAT variance reduction (commit d2fad78, `engine/aivat.py` + `web/core/engine/aivat.py` identical, `web/core/eval_stats.py` bootstrap CI) is committed but **never enabled in production** — `aivat_enabled` defaults False and no caller (elo_daemon, precommit eval) passes True. So zero-regression holds and the bug is dormant. BUT the ON-path is fundamentally broken and will silently regress when Phase 2 wires it into the CI gate.

**Why:** Two root defects, both confirmed empirically by capturing real `judge()` logs (allin-vs-call and fold bots):

1. **`_hand_realized_delta` reads only `final_result`, which exists ONLY in the game-finish display.** Per-hand realized deltas live in `temp_result` (judge.py:586 passes `temp_result=result` to make_request_json; make_finish_json:487 sets final_result from the *running total* `matchdata.total_win_chips`, NOT per-hand). So for all hands except the last, `_hand_realized_delta` returns 0.0 (fallback). The last hand returns `final_result.win_chips` = the **whole-game running total**, not the last hand's delta. Net effect: `aivat_adjust_game` = sum(allin-showdown EVs) + running_total_at_finish. Demonstrated bias of -20460 chips (raw +19650 vs aivat -810) on a fold-then-allin game. AIVAT is NOT unbiased — feeding it to a CI mean/CI gate yields garbage.

2. **Mutual-allin eligibility requires final-display chips == [0,0]/pot 40000, which only matches stack-equal allins.** When one player covers the other (the COMMON preflop-shove case), the hand settles at chips=[0, 19900]/pot 20100 (one player allin, other called with covering stack leaving chips behind). These are genuine all-in showdowns reaching 5 public cards (chance component exists) but AIVAT marks them NOT eligible → keeps raw realized (which itself is 0 per bug #1). ~50% of allin hands skipped in the allin-vs-call probe. Coverage gap (not bias by itself — keeping realized would be unbiased, but realized is broken too).

Split/off-by-one: `split_log_into_hands` puts hand N's settlement display (carrying hand=N+1) into hand N+1's segment, so `temp_result` is shifted across segments even if it were read.

**How to apply:** Before Phase 2 wires AIVAT ON, both must be fixed: (a) read per-hand delta from `temp_result` with the split off-by-one corrected (or track `total_win_chips` deltas between consecutive displays), and (b) broaden eligibility to stack-mismatch allins (detect via round_player_bet reaching player's full stack, not chips==0). The synthetic-log unit tests (`web/tests/test_logic_aivat.py`) pass but test the internal math on hand-built `[0,0]/40000` logs that don't match real engine output — they do NOT validate the real-log path. Equity math itself (river `compare_full_cards` + tie=0.5; MC with known-hole sampling unseen community; AA vs KK ~0.82) is correct. Performance: MC n_sims=2000 over allin hands with pure-python evaluator is heavy but moot while OFF. Related: [[engine-judge-reraise-architectural-bug]].
