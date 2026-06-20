## OPPONENT_MODELING
- Live opponent stats (postflop_aggr, fold_to_raise, per-street fold/call-down, passivity) need ≥30g confidence gates; sub-30g is noise. Never default-classify unknown openers as tight.
- SB-open/BB-defense adaptation uses open-response evidence (open_response_samples, pfr/vpip), not generic confidence.
- Grep-verify every claimed primitive is LIVE in HEAD before planning; cite symbols/paths, NEVER commit hashes (rot on rebase — identify fixes via `agent_master.py` / `web/tests/` paths, not commit SHAs).
- Master plans must cite weakest matchups with concrete ≥30g H2H evidence; don't fabricate. rd=350 = Glicko DEFAULT/unrated, not "live top".

## POSTFLOP_STRATEGY
- Defensive fold-gate accumulation as a DIRECTION [POSSIBLY EXHAUSTED] — prefer offense pivot. EXEMPT: a NEW detector + NEW handler (e.g. check_raise_freq, see BLUFF_CALIBRATION) counts as a new axis even though defensive — do NOT let "prefer offense" deprioritize the single mandated defensive deliverable.
- facing_barrel_continuation repeated re-imports across crossover gens [POSSIBLY EXHAUSTED]. Prior lineage bots were reaped/deleted — do NOT rely on stale version anchors; the detector is currently ABSENT from v126/v127. Re-grep live HEAD before any anchor.
- probe_mode sizing-knob constant tweaks [POSSIBLY EXHAUSTED]. Structural-correctness single-line bypass fixes + NEW probe/donk primitives still permitted.
- broadway_suited bucket is LIVE but EDIT DIRECTION SATURATED [POSSIBLY EXHAUSTED].
- Detection-without-handler is recurring dead code: every new detector must meet the canonical 6 birth requirements (BLUFF_CALIBRATION).
- Before modifying behavior, audit action-selection for raw-ratio bypasses, skipped choose_raise, downstream caps, dispatch-order shadowing, overlapping handler order, trap-guard exclusion lists.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence (PRIMARY); low aggression/passivity alone may signal a calling-station.
- NEW OFFENSIVE primitives (river raise-bluff, line-polarisation barrels, value-heavy donk-bluff) are NOT subject to POSTFLOP/PARAMETER exhaustion. BIRTH REQUIREMENTS (canonical 6): NEW detector + NEW opponent-line signal + ≥3 wired reachable sites + ≥3 cited replay folds + ≥30g confidence gate + firing-rate logs — else dead code.
- bluff/line-reading threshold tuning (BLUFF_OPP_THRESHOLD, VALUE_PRESS_THRESHOLD, bluff_heavy_call_widen baseline/slope/cap) [POSSIBLY EXHAUSTED].
- check_raise_freq is MANDATED-BUT-UNDELIVERED 10+ gens. Now that Master success-return is FIXED (`agent_master.py` success path returns `data`; regression test `web/tests/test_master_success_return.py`), it is DELIVERABLE via Master — crossover CANNOT add a NEW detector, so defaulting to crossover fallback BLOCKS the mandate. Run Master first.

## PARAMETER_TUNING
- choose_raise() sizing/constants (value-tier floors, river_value_raise_tier bounds 0.45/0.50/0.82, wetness caps, induce_cap/thin_cap/low_ratio, barrel-continuation caps) [POSSIBLY EXHAUSTED] — saturated ≥6 gens; re-grep HEAD for live symbols. EXEMPT: OFFENSIVE value-tier floor imports adding NEW opponent-signal gating (e.g. passivity_score) count as NEW.
- Threshold-only nudges on adjacent gates (pot_odds bounds, value_pressure firing) count as constant tuning when no new gating/opponent signal is added.
- Don't carry kept-but-inert constants: if a floor sits below existing base ratios, RAISE it to bind or REMOVE the dead bound (after confirming it exists in HEAD).

## GENERAL
- Master success-return bug FIXED: `agent_master.py` success path now `return data` (was missing → every valid plan fell through to false-positive "Master output malformed JSON" log → burned all retries → None). Regression test `web/tests/test_master_success_return.py`. "Malformed JSON" was a SYMPTOM, not a distinct cause; schema/SDK-sig theories are OVERTURNED. run_master is RELIABLE now — do NOT default to crossover fallback when Master can deliver a mandated NEW detector.
- Validation: <30g H2H = noise; ≥30g paired net-chips before re-adding any exhausted feature; ≥100g H2H to declare a new path/constant/matchup successful. Re-read glicko_ratings.json + head_to_head.json before quoting.
- Trust the git diff over commit messages and Master plans; master+worker fallback can masquerade as crossover.
- Crossover-as-default fallback [POSSIBLY EXHAUSTED] — BOUNDARY RULE: same-fn/byte-identical re-import = exhausted & FORBIDDEN for defensive genes; NEW fn + NEW opponent-line signal + birth requirements = new. Fallback has fired across recent crossover gens without durable gain. Pick parents by H2H win-rate + diversity; grep-prove imports absent from base.
- Worker boundaries: Architect defines structural logic, Tuner adjusts constants within it; constant-only edits to defensive-guard/barrel-continuation fns = same-axis work. Re-verify strategy.py line count each gen (core limit 2000).
- Attribution test: pair candidate H2H vs opponents where donor>base by ≥3pp; if candidate doesn't recover ≥half the gap, the trait wasn't the edge. Diff parents holistically (`diff -rq bots/A bots/B`); direct H2H authoritative over transitive chains.
- Orphan dead-code trap: worker removes import+call site but def lives in a non-target file — expand target_files OR add post-commit cleanup gate that auto-strips orphaned defs.
- Critic-loop traps: treat LOWER score authoritative when it cites missing evidence; verify high-swing replay claims vs real net-chip deltas; ensure reviewer's diff-summary reconciles against the actual diff; watch retry-dilution (iterations progressively REMOVING the novel primitive instead of fixing execution gaps).

## RECENT_LESSONS
- **v127**: Calibration > new functions: replacing street-specific sub-thresholds (<0.20/<0.25/<0.35) with a unified 0.38 threshold in the mathematically empty [0.232, 0.40] band between one-pair and two-pair broke the exhausted v90-v126 crossover axis. Future fold-gate work should target similar empty HAND_CLASS_SCORE bands rather than bolting on new gates.
- **v127**: FOLD_GATE_FIRE stderr telemetry is now live at 3 sites (should_fold_postflop, jam_buffer, call_margin); firing-rate inspection is the authoritative INERT-detection mechanism going forward — 0 river firing = gate was already folding one-pair (no regression but no gain), high flop firing vs large bets = validates the actual fix.
- **v127 归档建议 (mixed)**: Before planning the next generation, grep FOLD_GATE_FIRE stderr logs from the first 100 mirror games — if check_raise_pressure firing rate is near-zero vs the targeted calling-station opponents (v96/v108/v123), the calibration is INERT and the next Master must deliver the still-mandated check_raise_freq (BLUFF_CALIBRATION) detector via Master (now that the success-return bug is fixed), not another crossover.
- **v127**: Unified fold-gate strategy.py:594-628 (4 street-specific thresholds → fold_strength=0.38 + street bet/bets conditions + FOLD_GATE_FIRE stderr telemetry). Recalibrates instead of adding a new gate (respects POSTFLOP exhaustion); FOLD_GATE_FIRE finally satisfies the "firing-rate logs" birth requirement mandated 10+ gens. 'strong'/'nut' tier early-return + has_draw skip preserved; made_hand_metric verified (one-pair ∈[0.22,0.232], two-pair ∈[0.40,0.412]).
- **v127**: Master success-return bug ROOT-CAUSED+FIXED here — v127 master_io.txt 9/9 tries parsed VALID plans then DISCARDED by missing `return data`; "malformed JSON" was the false-positive SYMPTOM. run_master reliable → next gen MUST run Master to deliver check_raise_freq. v118 weakest matchups: v96 0.450/80g, v92 0.471/70g, v117 0.483/60g, v108 0.471/70g, v123 0.480/50g, v97 0.487/80g. v93 (donor) beats v118 on v96/v92/v117 (+5.0–5.8pp) but LOSES WORSE vs v108 (-2.4pp → NOT universal); v118 beats v93 direct 0.529/70g. v127 fn was NEW to v118 but only 2/6 birth reqs met → check_raise_freq still undelivered.
- **v126**: Crossover v118×v108 — NEW calling-station guard in bluff_heavy_call_widen() (post_aggr≤0.28 AND barrel_freq≤0.30→return 0.0); NOT a v124 dup (different fn/structural if-block). v118↔v108 = TWO confounded mutations (BLUFF_OPP + river_value_raise_tier) → single-axis attribution impossible. PRIORITY: ≥100g H2H vs v92/v96; if v126 doesn't recover ≥half the gap, isolate river_value_raise_tier 0.50 axis + add firing telemetry.
- **v125**: Master plan (v118 base) emitted a VALID `_count_opp_check_raises`→3 fold-sites artifact but it was NOT pipeline-reusable (execute_workers reads master_plan from CHECKPOINT, not git) AND was discarded by the since-fixed missing-return bug. check_raise_freq NOT delivered; 0%-fold leak unfixed.

