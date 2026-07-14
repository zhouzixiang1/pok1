---
name: restart-jun19-3-rootcause-fixes
description: Three post-restart root-cause fixes reviewed Jun19 (signature empty-output retry, MAP-Elites quantile binning, QD-eval committed gate) — verdicts and non-blocking caveats.
metadata:
  type: project
---

Reviewed 3 restart-residual root-cause fixes on 2026-06-19 (web/core/). All three fix the true root cause, all 78 relevant tests pass. Each is a targeted, well-commented change.

**Fix1 llm_query.py empty-output retry (P1)** — VERDICT: fixes root cause, no new bug.
- claude_agent_sdk 0.2.91 signature bug has TWO modes: (a) raises ClaudeSDKError mid-stream [already retried], (b) stream "succeeds" with ResultMessage but 0 TextBlocks → _process_stream returns ([],cost,usage) WITHOUT raising. Mode (b) escaped all retry layers → parse_json_output('')=None → Master JSON collapse. Fix adds `if not texts and sdk_attempt<MAX-1: continue` retry in _run_stream_with_signature_retry.
- continue→finally(aclose)→next loop iter ordering is correct (query_gen already exhausted; new query_gen created next iter).
- No false-positive: ALL run_claude_query callers (Master/Reviewer/Critic/Analysts) expect text output; empty=always failure. Worker tool-use-only-with-no-text edge case is theoretical and identical to pre-existing signature-retry behavior (retry is whole-prompt restart, file edits on disk persist — no rollback, no new risk).
- Cost: returns ONLY last attempt's cost_usd (not summed across retries) — same pre-existing gap as signature-error retry path; not a new bug. On exhausted-empty the returned cost reflects the final failed attempt (better than pre-fix which returned cost for the 0-token-looking empty).

**Fix2 map_elites.py quantile binning (P2)** — VERDICT: fixes root cause, one advisory caveat.
- New _quantile_edges(values,n_buckets=5) computes interpolated quantile edges; build_behavior_archive uses dynamic edges instead of fingerprint_to_bc. Edge cases (all-identical/<5/degenerate) return None→fallback static edges (verified). fingerprint_to_bc/aggression_bucket/looseness_bucket now UNUSED in prod build (only tests call them) — safe.
- 🟡 CAVEAT (non-blocking): dynamic edges shift a bot's niche_key across generations when the active-bot distribution changes. write_behavior_archive preserves k3 fields by matching niche_key+bot_name; a bot that shifts niche loses its k3 median (reverts to single-eval). Confirmed NO gate/reap/Master reads behavior_archive (only generation_scheduler:352 reevaluate_top_elites, which reads scalar fitness per entry, not niche stability) — so impact is advisory-only diversity-signal degradation, not correctness. Acceptable.
- qd_async_eval.py:377 archive merge searches by `bot==bot_name` (not niche key) → dynamic edges do NOT break QD eval merge.

**Fix3 generation_scheduler.py QD-eval committed gate (P3)** — VERDICT: fixes root cause, no new bug.
- Gates launch_qd_eval on `git_has_tag(ctx.next_v)` (authoritative commit proof). abandon_generation (MCP tool, tool_bot_management.py:177) rmtree's bot dir but leaves no tag; ctx.next_v still holds planned version → would fire against missing main.py. Gate correctly skips with qd_eval_skipped event (reason=not_committed).
- No race: commit_bot (LLM tool) tags atomically BEFORE _run_one_cycle returns; post_generation_cleanup runs after cycle return in orchestrator_loop (orchestrator.py:968→973). Tag always exists before cleanup when committed.
- Asymmetry with exploitability probe (uses main.py exists check at :797) is intentional/fine — probe fires earlier in same cleanup; both guards independently correct.
- qd_eval_skipped is safe — no downstream expects qd_eval_start/done every cleanup (QD eval is fire-and-forget).

**Test debt**: test_qd_eval_cancel_skips_archive_write→test_qd_eval_watchdog_cancel_keeps_result asserts eval_mode k3 (not single), correctly reflects qd_async_eval.py:346-367 watchdog-keep logic.
