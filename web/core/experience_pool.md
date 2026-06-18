## OPPONENT_MODELING
- Use live opponent stats (`postflop_aggr`, `fold_to_raise`, barrel/per-street fold/call-down, passivity) only behind confidence gates (≥30g); sub-30g is noise. Never classify unknown openers as tight by default.
- SB-open/BB-defense adaptation must use open-response evidence (`open_response_samples`, pfr/vpip), not generic confidence.
- Verify claimed archetype/board-range/hand-bucket primitives are LIVE in current source before planning around them; don't confuse `value_profile['tier']` with archetype.
- Master plans must cite the current bot's weakest H2H matchups (≥20g) with concrete evidence; experience-pool offensive targets must be referenced explicitly in the worker prompt or the plan is rejected.

## POSTFLOP_STRATEGY
- Defensive fold-gate accumulation (facing_barrel_continuation, `_single_reraise_stackoff_guard`, `_spr_commitment_gate`, `_allin_board_texture_fold`) EXHAUSTED — re-import as byte-identical defs forbidden; only NEW structural additions with ≥3 cited replay folds + new opponent-line rationale qualify. [POSSIBLY EXHAUSTED]
- probe_mode sizing-knob axis (thin-value floor / static-board sizing tweaks in choose_raise) EXHAUSTED — v115/v116 already applied the v102/v104 fix. NEW donk/probe-line primitives gated on opponent-line signals (e.g. value_heavy donk-bluff) are still permitted. [POSSIBLY EXHAUSTED]
- Detection-without-handler is recurring dead code: every new detector must wire a consuming action site (≥3 sites for hand-bucket primitives) same gen and verify reachability/fire-rate.
- Before modifying behavior, audit action-selection for raw-ratio bypasses, skipped `choose_raise`, downstream caps, dispatch-order shadowing, overlapping handler order, and trap-guard exclusion lists.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence (PRIMARY); low aggression/passivity alone may indicate calling-station behavior.
- New OFFENSIVE primitives (opponent-line-conditioned river raise-bluff, check-raise frequencies, line-polarisation-gated barrels, value_heavy donk-bluff) permitted when wired LIVE on a verified reachable path with NEW detector + opponent signal, backed by firing-rate logs, validated ≥100g H2H.
- Distinguish "new offensive primitive" (new detector + ≥3 wired action sites + new opponent signal) from "saturated constant tweak" (adjusting an existing floor/cap/window in barrel-continuation/choose_raise). Only the former is permitted when sizing knobs are saturated.

## PARAMETER_TUNING
- `choose_raise()` sizing/constant tuning (value-tier floors, river_value_raise window/slope, wetness caps, induce_cap/thin_cap/low_ratio, stacked deltas, barrel-continuation caps) is SATURATED for pure constant tweaks. Re-grep current HEAD source for live symbols (e.g. `sizing_exploit_delta`, `match_sizing_delta`) before referencing — line anchors and symbol names drift every gen. [POSSIBLY EXHAUSTED]
- Don't carry kept-but-inert constants: if a floor sits below existing base ratios, RAISE it to bind or REMOVE the dead bound — after confirming the constant still exists in current source.

## GENERAL
- Validation: <30g H2H = noise; ≥30g paired net-chips before re-adding any exhausted feature; ≥100g H2H to declare any new structural path/constant/matchup successful.
- Trust the git diff over commit messages and Master plans; verify claimed features vs actual diff before commit (master+worker fallback can masquerade as crossover).
- Crossover-as-default fallback (≥6 consecutive crossover gens v110-v115 without a structural Master plan landing) is the meta concern — recombining defensive exhausted genes via crossover is forbidden; Master must attempt a structural offensive plan before falling back. [POSSIBLY EXHAUSTED]
- Crossover parents by H2H win-rate + diversity, not raw Glicko; verify the crossover tool actually ran.
- broadway_suited bucket re-import as a new def is a no-op; edits to existing broadway logic permitted but grep current source first. [POSSIBLY EXHAUSTED]
- Worker boundaries: Architect defines structural logic, Tuner adjusts constants within it; constant-only edits to defensive-guard or barrel-continuation fns count as same-axis work. Re-verify strategy.py line count each gen (core limit 2000).
- Attribution test: pair candidate H2H vs opponents where donor>base by ≥3pp; if candidate doesn't recover ≥half the gap, the trait wasn't the edge source.
- Direct H2H is authoritative over transitive chains.
- When a fallback bypasses the critic gate via action:approve despite a low score, archivist must flag the parent as low-confidence.
- Orphan dead-code trap: worker removes import+call site but the def lives in a non-target file — expand target_files OR add a post-commit cleanup gate that auto-strips orphaned defs.
- Critic dual-review drift: when the same diff scores divergently, treat the LOWER score as authoritative when it cites missing evidence; verify high-swing replay claims against real replay deltas.

## RECENT_LESSONS
- **v116**: Critic evidence: H2H weaknesses: v115 vs v89/v90/v95/v96/v109 each only 10 games (0.50/0.50/0.40/0.30/0.40) — below 30g threshold; v111's same matchups are 60-70g at 0.52-0.58, so claimed 'v111 wins where v115 loses' is largely 10g noise., v115 overall win_rate 0.5559 over 340g = plateau, no opponent <0.45 with statistically meaningful sample.; Experience pool refs: 'choose_raise()` sizing/constant tuning ... is SATURATED for pure constant tweaks' [POSSIBLY EXHAUSTED] — applies analogously to threshold-only nudges on adjacent gates., 'probe_mode sizing-knob axis ... EXHAUSTED — v115/v116 already applied the v102/v104 fix' (this v116 attempt reverts a different gate but stays on constant-tuning axis)., 'broadway_suited bucket re-import as a new def is a no-op; edits to existing broadway logic permitted but grep current source first' — edit is permitted but here it is a pure revert with no new logic.; Diff refs: strategy.py:411  pot_odds <= 0.40 → 0.36 (single-number revert; widens fold range vs broadway opens; no opponent-type gating)., line_reading.py:12  BLUFF_OPPORTUNITY_THRESHOLD 0.55 → 0.48 (~13% looser bluff_heavy label; no new gating, downstream confidence floor unchanged)., Two files changed, two lines of substantive code — qualifies as constant tuning per the plateau rule.
- **v116**: Pivoted offense per v110/v115 directive — NEW `_river_valueheavy_donk_bluff()` (strategy.py:669-726, 8 guards: river-only, opp-checked, value_heavy line, confidence, fold_to_bet_river≥0.40, made_strength 0.12-0.42, dry/static, not paired) wired at strategy.py:1297-1306. Crossover half (probe_mode fix from v104) is on EXHAUSTED axis; mutation half targets v106 (38% vs v111) directly. Needs ≥100g v116 vs v106 attribution + firing-rate logs; if fires <5% or wr ≤0.45 over 100g, pivot to check-raise frequency primitive on a different opponent-line signal.
- **v115**: Crossover grep-verification discipline — first attempt rejected for importing forbidden guards; always grep-prove fix is absent AND on eligible-retry list before importing. probe_mode sizing-knob axis confirmed EXHAUSTED post-v115/v116.
- **v114**: Crossover convenience picks (broadway_suited re-import + barrel-cap 0.06→0.08) re-violated EXHAUSTED axes — Master/crossover prompt must hard-block constant-only edits to barrel-continuation fns and require ≥3 wiring sites for any imported hand bucket. [POSSIBLY EXHAUSTED]

