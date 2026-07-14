---
name: engine-judge-reraise-architectural-bug
description: engine/judge.py uses a single last_raise_to check for both first-raise minimum and re-raise minimum, unlike sever/validator.py which separates them
metadata:
  type: feedback
---

engine/judge.py uses `last_raise_to` (initialized to big_blind for preflop, big_blind//2 for postflop) as the baseline for the re-raise check. This single comparison `raise_to <= last_raise_to * 2` handles BOTH the minimum first raise AND consecutive re-raises. Any change to this comparison operator affects blind posting (which goes through player_action with bet > 0) AND first raises, not just re-raises.

**Why:** Unlike `sever/validator.py` which has separate rules (6, 7, 9 for first-raise minimums using `<` comparisons, and rule 8 for consecutive raises using `_last_raise_amount()`), `engine/judge.py` has a single unified check with no separation.

**How to apply:** When modifying the re-raise comparison in `engine/judge.py`, either (a) add separate first-raise handling before the unified check, or (b) exempt the blind-posting path (which goes through `player_action` during `deal_cards_and_blind`). Blind posting sets `last_raise_to` during the blind sequence, so the check fires on the BB blind.
