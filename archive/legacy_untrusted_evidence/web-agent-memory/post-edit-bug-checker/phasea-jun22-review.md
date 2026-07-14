---
name: phasea-jun22-review
description: Phase A (A1-A7) post-edit review findings — battle.py 4-tuple stderr change isolated to web/core only; all callers correct; 1037/1038 tests pass (1 pre-existing unrelated failure).
metadata:
  type: project
---

Phase A (A1-A7) review of uncommitted changes in web/core (evolution-plan-refresh-jun21).

**Key architectural fact verified:** `web/core/engine/battle.py` and top-level `engine/battle.py` are TWO INDEPENDENT FILES (confirmed `diff -q` = DIFFERENT). The daemon's `from engine.battle import mirror_battle` resolves to **web/core/engine/battle.py** (CORE_DIR is on sys.path before PROJECT_ROOT; `engine` package = web/core/engine/). The A1 4-tuple `_PersistentBot.call()` signature change (3→4 tuple) is correctly isolated — only caller is web/core/engine/battle.py:216, updated. The CLI path (engine/battle.py) and anchor_runner.py keep their own old 3-tuple, UNAFFECTED.

**Why:** critical to confirm the running generation's daemon subprocess gets the new code (it does) without breaking the CLI ladder.

**How to apply:** when reviewing engine changes, NEVER assume engine/battle.py == web/core/engine/battle.py. They diverged. The `engine` namespace collision is real.

**Other findings:**
- `_call_bot` external signature UNCHANGED (still returns 3-tuple). Only `_PersistentBot.call` changed. All 5 `_call_bot` callers valid.
- A1 `time.sleep(0.003)` per decision = ~25s added wall-clock per daemon match (5 pairs × 2 halves × 70 hands × ~6 dec × 2 bots × 3ms). Acceptable, not a correctness bug.
- A1 stderr buffer unbounded but drained per-call + bot recreated/closed per game — no real leak.
- A1 mirror-game log entries now carry `"stderr"` key; judge.py:547 + bot_action_stats.py read only `response`/`verdict` — extra key harmless. save_match_replay serializes raw — replays grow but won't break.
- A3 placement-shadow AST detector is advisory-only, try/except-wrapped, adds additive key to gate result (all_passed unaffected).
- A4 `_summarize_hand` new `replay_file=""` default = backward-compatible. `_verify_cited_replays` returns [] if manifest missing (non-blocking).
- A5 `run_literature_probe` new @tool registered tool_planning→tool_pipeline→tools.py. master_prompt uses substitute_template (str.replace, no KeyError).
- A6 research_governance.py is UNTRACKED but present; all imported funcs exist. tool_eval hook try/except.
- A7 adaptive probes use get_bot_dir(int) correctly; weaknesses var in scope. Minor: may select graveyard (culled) bots as opponents.
- Tests: 1037 pass / 1 fail. The 1 failure (test_llm_infra_error TestMatchAnalystSentinel) is PRE-EXISTING — touches _analyze_recent_matches which is NOT in the Phase A diff (grep count=0).

Related: [[exploitability-probe-never-ran-fix]].
