## OPPONENT_MODELING
- Use live opponent stats (`postflop_aggr`, `fold_to_raise`, barrel frequency, per-street fold/call-down, passivity) only behind confidence/sample gates (≥30g); sub-30g matchups are directional noise — do not record as actionable weaknesses. OR-combine tendencies with modest magnitudes.
- SB-open/BB-defense adaptation must use open-response evidence (`open_response_samples`, pfr/vpip), not generic action confidence; never classify unknown openers as tight by default.
- `estimate_preflop_strength` saturates pocket pairs to 1.0; use `preflop_hand_profile()` / `classify_preflop_hand()` buckets for preflop range gates.
- Do not confuse `value_profile['tier']` with opponent archetype; verify claimed archetype/board-range primitives exist and are live before planning around them.

## POSTFLOP_STRATEGY
- DEFENSIVE late-street fold/all-in/texture/pot-odds/polarization/barrel guard accumulation is saturated; add no new defensive guard unless it targets a distinct decision point and has ≥100g validation. [POSSIBLY EXHAUSTED]
- `facing_barrel_continuation` is now LIVE (def strategy_helpers.py:272, import strategy.py:34, call strategy.py:1059). Open question is whether the live nudge (cap ≤0.06) LEAKS, not whether to add/remove. Before changing, run ≥30g paired-texture net-chips comparison; if it leaks, use a pot-odds-grounded variant (cap 0.10-0.12), not the exhausted flat nudge.
- Detection-without-handler is recurring dead code; every new detector must wire a consuming action site in the same generation and verify reachability/fire-rate. Re-introducing a byte-identical prior attempt without new rationale is the same trap.
- Confirm named primitives exist in current source before referencing them; docstrings, memories, stale notes, and old helper names are not definitions.
- Audit action-selection paths for raw-ratio bypasses, skipped `choose_raise`, downstream caps, dispatch-order shadowing, and overlapping handler order before modifying behavior.
- Verify trap-guard exclusion lists after any `_should_checkraise_trap` refactor; dropping value/bluff exclusions can suppress intended value sizing.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence and confidence; low aggression/passivity alone may indicate calling-station behavior.
- Exhaustion applies to DEFENSIVE guards only; new offensive bluff/value paths remain permitted when backed by firing-rate logs and ≥100g H2H.
- Structural bluff modules require current-source live-path verification before being treated as successful or expanded.

## PARAMETER_TUNING
- DEFENSIVE sizing constant tuning (caps/floors/defensive call thresholds) has no sustained gain; constants-only inside an Architect-defined structural hypothesis with per-constant H2H backing. Offensive sizing floors/tiers remain permitted. [POSSIBLY EXHAUSTED]
- Exclude new defensive sizing-tier/floor/cap increases from Tuner work unless current source proves dispatch order, downstream caps, and target live path are not the blocker.
- The v110 `_spr_commitment_gate` value-tier floors (0.50/0.55/0.60) sit BELOW existing base ratios (0.60/0.70/0.85) → narrow firing window catches only extreme downward-stacking. Resolve: either RAISE the floor so it binds, or REMOVE the dead bound — do not carry a kept-but-inert constant.
- Do not reintroduce stacked value-sizing boosts such as `value_sizing_delta` at `choose_raise` unless current source and matchup evidence prove underbetting.

## GENERAL
- Any new structural path, constant change, or matchup target requires ≥100g H2H validation before treating as successful, repeating, or expanding; <30g H2H is directional noise, not an actionable weakness.
- Treat commit messages as advisory; trust the git diff (v107 claimed a thin-value probe_mode mutation byte-identical to v102).
- Select crossover parents by H2H win-rate and diversity, not raw Glicko; verify the crossover tool actually executed rather than falling back to master+worker copy.
- Verify branch_from logic considers current top-rated bots, not just stagnation ancestor (v107 branched from v102 when v106 was available).
- Worker boundaries are mandatory: Architect defines structural logic; Tuner may only adjust constants within that structure.
- Re-verify strategy.py line count each generation; core limit 2000, bundle refactors only when source nears cap.
- Attribution test: when isolating a donor trait into a base, pair candidate H2H vs opponents where donor>base by ≥3pp; if candidate doesn't recover ≥half the gap, the trait was not the edge source.
- Orphan dead-code trap: worker removes import+call site but def lives in a non-target file. Expand target_files OR add a post-commit cleanup gate that auto-strips orphaned defs.

## RECENT_LESSONS
- **v111**: Critic evidence: H2H weaknesses: v100 weakest H2H: vs v103 47.3% (110g), vs v97 47.5% (160g), vs v89 48.1% (160g), vs v102 48.6% (140g) — all plateau 47-50%, no <40% matchup. No specific H2H signal that BB-defense with broadway suited is the leak.; Experience pool refs: MEMORY: v107-110 EXHAUSTED defensive-guard chain → next gen MUST pivot OFFENSE (river_value_raise tier-floor). v108 critic caveat: limp-call broadway lacks pot-odds gate + JTs/QTs default-raise may bleed vs sticky 3bet BBs — same risks recurring here. v108 H2H validation still pending.; Diff refs: state.py:87-92 broadway_suited classification; strategy.py:368 added to implied tuple; strategy.py:402-411 new BB-defense gate (pot_odds<=0.36); strategy.py:529-534 SB-iso limp unconditional call (no pot-odds gate).
- **v111**: Critic evidence: H2H weaknesses: v110 H2H context is sparse: only 80g logged. v14 0.450 (20g), v79 0.500, v92 0.500, v104 0.700. No matchup <40%; per the >=30g rule no actionable H2H weakness is yet available — strategy must be experience-pool-driven, which it is.; Experience pool refs: RECENT_LESSONS v110: 'next Master must pivot OFFENSE — river_value_raise tier-floor scaled by opponent nutted_risk' — directly satisfied by the river_value_raise_tier mutation., POSTFLOP_STRATEGY: 'facing_barrel_continuation is now LIVE... open question is whether the live nudge LEAKS, not whether to add/remove. Before changing, run >=30g paired-texture net-chips comparison' — contradicted by this purge., GENERAL: 'Orphan dead-code trap: worker removes import+call site but def lives in a non-target file' — verify facing_barrel_continuation/_single_reraise_stackoff_guard defs are also removed (only strategy.py was changed; strategy_helpers.py:272 still has facing_barrel_continuation PURGED, confirmed by diff).; Diff refs: strategy_helpers.py:272 — facing_barrel_continuation def (57 lines) DELETED; river_value_raise_tier signature gains nutted_risk_score param with +0.08..+0.12 bonus when risk<=0.03, floor raised 0.45->0.50, cap 0.85 preserved., strategy.py:354-382 — _sb_open_bucket_action and _bb_vs_raise_bucket_action (73 lines) DELETED; preflop reverts to open_threshold=0.46+match_adjust+0.02 and value-3bet gate preflop_strength>=0.60., strategy.py:668-774 — _single_reraise_stackoff_guard (29 lines) and _spr_commitment_gate (61 lines) DELETED, both call sites removed.
- **v110**: EXHAUSTED CONFIRMED: v107-v110 four-gen defensive-guard crossover chain — critic scores regressed 7.0→4.0 citing 'no traceable evidence for cited leak' + 'pot-odds discipline violated'. Any Master plan proposing a DEFENSIVE late-street fold/guard must FIRST cite ≥3 specific replay hands from current lineage (v109+) where made_strength≥0.50 was folded facing a 2:1+ pot-odds offer, OR pivot to OFFENSE.
- **v110**: CRITIC DUAL-REVIEW DRIFT: same diff scored raw_approved true→false and advisory 7.0→4.0 across two consecutive runs — treat the LOWER score as authoritative when the assessment cites missing evidence.
- **v110 归档建议**: next Master must pivot OFFENSE — river_value_raise tier-floor scaled by opponent nutted_risk (extract more from strong-but-second-best hands), targeting v107/v79/v95 (v109 weakest at 0.30/0.45/0.45 wr that v110's symmetric gate cannot differentially address).
- **v110**: v109 win rate 0.564 (330g) healthy; cited '-20071/-16825/-15680 river tail' has NO traceable source — replay scan finds 0 hands delta<-10000 across 33 v109 replays. Weakest n=10 matchups are directional noise per the ≥30g rule.
- **v110**: strategy.py is byte-identical to v109 (diff=0); the only v110 trait is `_spr_commitment_gate` from v91 (byte-identical def, only GATE 2 cap 0.45→0.40) — a no-op strategy.py change, demonstrating the byte-identical-reintroduction trap.
- **v109**: Re-adding an exhausted-tagged feature requires ≥30g paired net-chips validation BEFORE re-add; barrel-continuation re-add + reraise-guard (call MORE) pull OPPOSITE → cancellation risk.
- **v109**: Porting a guard from a confirmed-regression lineage (v103 1235→1155) requires trace evidence the SPECIFIC guard was not the regressing component.
- **v108/v109 attribution probes** (vs v92 broadway wr<0.49, vs v93<0.53 falsification) remain unresolved — orthogonal to v110 sizing-floor work.


