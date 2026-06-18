---
name: phase0-schema-change-test-staleness
description: Phase 0 bot_action_stats per-opponent schema change (A+B+D) left 4 tests in test_logic_bot_action_stats.py asserting the OLD flat shape → 4 failures.
metadata:
  type: project
---

Phase 0 daemon priority + incremental stats diff (working tree, uncommitted as of 2026-06-16) changed `compute_all_bot_stats` to return a PER-OPPONENT breakdown `{bot: {opp: {street:{...}, total_hands}}}` and added conditional features (fold_to_bet/cbet/barrel) to the action dicts + street stats.

**Why:** Schema prerequisite for Phase 0 (per-opponent modeling). Intentional.

**How to apply:** When touching this diff, the test file `web/tests/test_logic_bot_action_stats.py` was NOT updated and 4 tests fail on the new schema:
- `test_allin_encoded_as_minus_two` — action dict now has `opponent/fold_to_bet/cbet/barrel` keys (exact-equality assert)
- `test_basic_per_street_shape` — street dict now has 9 keys not 6
- `test_compute_all_bot_stats_single_pass` — asserts flat shape, now per-opponent
- `test_compute_bot_action_stats_delegates_to_all` — flat (compute_bot_action_stats) no longer == per-opponent (compute_all_bot_stats)

These are EXPECTED schema-driven failures, not logic bugs. Full suite: 850 passed, 4 failed (these 4). The daemon integration is correct (`get_global_stats` flattens before write_locked_json). Related: `get_global_stats` has a real `total_hands` undercount bug (assigns `= last_opponent.total_hands` instead of summing `+=`) — see findings. The incremental etag cache in `compute_all_bot_stats` is functionally a no-op tripwire (always rescans all files; `nothing_changed` branch is `pass`), so the "incremental etag" telemetry log is misleading but not incorrect data.

Pattern: this is the recurring [[arch-audit-fix-jun16]] / [[residual-issues-rootcause-jun15]] cross-step-consistency hazard — schema change must propagate to tests. The `_pop_next_job`/`_is_external`/`_external_priority` priority-aware daemon dispatch (Phase 0 F) has NO test coverage either.
