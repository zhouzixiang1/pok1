# Memory Index

- [Exploitability probe fix](exploitability-probe-never-ran-fix.md) — 8-gen probe blackout root causes (silent shutdown + nested-fork deadlock) and the safe workers=1+wait_for calling convention for run_exploitability_probes_async.
- [orchestrator cost<0 handler conflation](orchestrator-cost-negative-handler-conflation.md) — cycle_failed -1.0 shares the "API auth error (401/403)" loop branch; diagnostic noise not bug.
- [Attempt-top reset wipes sequential sibling](attempt-top-reset-wipes-sequential-sibling.md) — B-group _reset_target_files_to_source at attempt-loop top deletes preceding worker edits in sequential-overlap mode (safe in parallel/disjoint + single-task).
- [Phase0 schema change test staleness](phase0-schema-change-test-staleness.md) — Phase 0 per-opponent stats schema left 4 tests asserting OLD flat shape (850 pass/4 fail); daemon get_global_stats total_hands undercount bug; incremental etag is a no-op tripwire.
- [AIVAT side-pot bias bug](aivat-sidepot-bias-bug.md) — Phase-1 AIVAT real-log fix correctly addressed the 3 audit bugs but left: 96% of allin-showdowns have a side pot (mean 3555 chips); `equity*pot - contrib` treats returned side-pot chips as at-risk → per-hand bias up to ~3555 chips. Fix = use main_pot=2*min(contrib).
- [Phase2 SPRT truncation default PASS](phase2-sprt-truncation-default-pass.md) — AgentAssay SPRT n_max truncation defaults to PASS (not rate-fallback) to keep type-I ≤ α; UNDECIDED/rate_fallback are dead code; run_decision_tests_sprt NOT yet wired into tool_gates.
