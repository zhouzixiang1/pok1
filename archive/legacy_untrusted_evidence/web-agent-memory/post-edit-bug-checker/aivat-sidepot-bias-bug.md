---
name: aivat-sidepot-bias-bug
description: Phase-1 AIVAT real-log fix left a side-pot/stack-mismatch bias bug — 96% of allin-showdown hands inject up to ~3555 chips of (equity-1)*sidepot bias because pot/contrib use the full pot incl. returned side-pot chips.
metadata:
  type: project
---

Phase-1 AIVAT real-log bug-fix (diff on engine/aivat.py + web/core/engine/aivat.py, 22 tests pass, both files identical) correctly fixed the 3 audit bugs: `_hand_realized_delta` now reads per-hand `temp_result` (sum-of-hands == game final_result, verified); eligibility widened to `chips[0]==0 OR chips[1]==0` (verified vs 200-game replay: 836 stack-mismatch vs 41 mutual allins — old both-0 code missed ~95%); split_log_into_hands keeps hand-N's settlement frame (rendered hand=N+1, judge.py:577-578) in segment N.

**LEFTOVER CRITICAL BUG (not caught by the fix or its 22 tests):** `_extract_allin_snapshot` captures `pot` and `contrib_me/opp` from the RIVER last_display, which includes the SIDE POT (the covering player's unmatched excess that is RETURNED at settlement). The AIVAT formula `equity*pot - contrib` then treats returned chips as at-risk, injecting bias = `(equity-1)*side_pot`.

**Why:** engine judge.py does NOT force the covering player to match an allin — when one player shoves and `allin_occurred` + round_action_left==0, it runs out the board WITHOUT the cover calling the full shove (verified: real hand pot=34098, cme=20000, copp=14098, side=5902; P1's `0` check did not match P0's 15338 shove). Realized delta is ±min_contrib (side returned), but current code computes equity*full_pot - full_contrib.

**Magnitude (40-replay scan):** 348/364 allin-showdown hands (96%) have nonzero side-pot; mean side-pot 3555 chips, max 19900. Per-hand AIVAT bias up to ~3555 chips — dwarfs the runout variance AIVAT exists to remove, and is skill-correlated (bigger shoves → bigger side pots → bigger bias), so it's a systematic not zero-mean distortion.

**Correct fix:** compute `min_contrib = min(contrib_me, contrib_opp)`, `main_pot = 2*min_contrib`, use `equity*main_pot - min_contrib` (side pot has no chance event). Verified on real hand: equity=0.5 coinflip → current=-2951 (wrong), main-only=0 (correct), realized=0.

**Why tests miss it:** TestRealStructureIntegration uses AA-vs-KK with contrib_me==contrib_opp (side=1, negligible 0.5-chip error). No test exercises contrib_me != contrib_opp with a large side pot. The "unbiased_vs_demo_bias" test only asserts `total>0 and total<100000` — far too loose to catch a few-thousand-chip bias.

**How to apply:** When reviewing/fixing AIVAT, the equity-relevant wager is the MATCHED (main) pot only. Any allin where `contrib_me != contrib_opp` by more than 1 chip must clamp to `2*min(contrib)`. Add a test with cme=20000/copp=6000 asserting the adjusted value uses main_pot=12000 not full pot.
