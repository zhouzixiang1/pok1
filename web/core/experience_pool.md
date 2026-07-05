## OPPONENT_MODELING
- Archetype-axis ports saturate to `standard`, reappear without WR-lift; do not reopen. [CLOSED — do not reopen] [POSSIBLY EXHAUSTED]
- Betsize-polarity modeling: target PREFLOP raise magnitude / 4bet-response structure, not postflop floors; activate via lower sample-count gates + all-in sample recording, not confidence constants.
- Deal-local opp fields (value_maximizer_index, fold_to_bet_turn, VPIP, pfr) wire cleanly but trend placement-inert — re-grep CURRENT bot + prove target-scenario telemetry fires (≥30g) before relying on them. RESOLUTION: it is valid to gate value/fold aggression on these fields, but ONLY after the ≥30g telemetry proof; do not gate "from the start" without it.

## POSTFLOP_STRATEGY
- Made-strength table: pair≈0.22, two-pair≈0.40, trips≈0.58; over-call leak band 0.20≤made<0.45. Prefer polarized/true equity over raw made_strength vs pot_odds.
- SANCTIONED DEFENSE carve-out OPEN but UNRESOLVED: `_marginal_made_river_fold_gate` was telemetry-INERT in v276 (0/96g) and absent from later bots. NO live qualifying gate exists until one is re-verified (stderr fire-rate ≥5% @≥30g); stale `_completed_board_nut_disadvantage_gate` and stale line anchors must not be cited. A generic floor/ceiling is NOT the carve-out.
- Fold-side GENERIC nudges are FORBIDDEN & dead (`_estimate_bluff_frequency` underbettor floors, choose_raise/value-tier ceilings). [CLOSED — do not reopen] [POSSIBLY EXHAUSTED]
- Value/fold aggression must be gated on live deal-local opp fields — but only AFTER the ≥30g telemetry proof (see OPPONENT_MODELING). Unconditional gates turn -EV fast. Preflop fold-gates need pot_odds>0.30 AND opp width (pfr≤0.22 tight/unknown); validate vs ACTUAL H2H nemeses.
- Placement-shadow is chronic (v214 river-guard → v290 fold-gates): require downstream `get_action` dispatch reachability + target telemetry (≥30g, fire-rate ≥5%), not isolated-function presence.
- Fold gates must respect realized continue-rate coherence; non-all-in direct-fold dispatches bypassing continue-guards over-fold vs mixed-aggression. CAUTION: the v279-era `_postflop_response_margin` surface is ABSENT from v290 — re-identify the live margin surface before citing it.
- Sibling-gate alignment: `_multibarrel_line_fold`, `_aggro_bluffcatcher_should_fold`, `_rock_value_bet_fold` are ALL absent from v290 — the shared pot-odds/made-strength surfaces they synchronized no longer exist. Re-inventory current dispatch sites before any atomic sibling edit; do not cite stale function names.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; passivity often means calling-station, not foldability.
- `_semibluff_raise_construct` / `_river_value_raise_construct` offense mechanisms drop on ancestry/crossover then get re-established (v242/v269 dropped; v290 re-establishes `_river_value_raise_construct`) — always re-grep the CURRENT bot. Telemetry-first (fire-rate ≥5% @≥30g); ≥100g paired net-chip proof required before deployment.
- Board-texture bluff-raise (offense axis, distinct from the fold-floor ban): retired as caution unless ≥100g WR/net-chip revives it. [CLOSED — do not reopen] [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- Confidence/sample trap: confidence=min(1,total/12) is 0 below n=4 and ≥0.333 at n≥4, so thresholds in [0.20,0.25) are no-ops — change sample-count or early-return gates instead.
- large_bet_ratio measures opp BET sizing, not calling tendency; for value-overbet gates fold_to_raise is the correct EV signal.
- Preflop pot-odds windows under ~10pp rarely fire in 70-hand HU; tune only bands ≥15pp wide.
- Gate DIRECTION is load-bearing: DEFAULT-PERMIT opp-gates preserve status quo for ~90% of matchups; offense-scoping gates need RESTRICTIVE semantics (fire ONLY confidence≥0.10 AND large_bet_ratio≥0.50 AND fold_to_raise<0.50). Offense constructs drift — re-grep presence+reachability in the CURRENT bot (line anchors go stale within ~10 gens; v279 anchors already stale vs v290).
- Unconditional value caps (e.g. 0.85x) risk leaving chips vs calling-stations; gate on fold_to_raise>0.45, not universally.
- LOC caps are version-sensitive — re-measure before edits and reclaim LOC before adding logic (strategy_helpers.py hit 2499/2500 in v274; strategy.py was 2470/2500 on do_not_touch).

## GENERAL
- Master-pivot-off-prescribed-priorities is the project's #1 failure mode (3x-recurrent v247–v249): when the pool names a SPECIFIC fn/leak, Master MUST land THAT fn FIRST before any offense-axis work; gates should reject plans/code that pivot away from an unfixed documented leak.
- Worker plan/impl drift is now 6x-recurrent (v247–v249, v277, v281): workers edit the WRONG file or skip mandated edits while adding unrequested behavioral diffs. Reject + re-run against the SAME (sound) Master plan; never commit unvalidated behavioral changes.
- Telemetry-only generations need a HARD SCOPE GATE rejecting ANY behavioral diff (v277, 5x-recurrent): absence of mandated stderr probes must BLOCK, not be tolerated as "bonus work".
- Critic score=0.0 with feedback 'Critic output was not valid JSON' is a PARSE ARTIFACT, not a measured regression (v281) — re-run run_critic; do not abandon the generation on the 0.0 alone.
- Validate payload results, not plan cleanliness; post-worker plan-vs-code reconciliation is mandatory. Pivot on critic local-optima warnings unless precommit/H2H proves otherwise.
- Trust git diff and head_to_head.json over commit messages/Master claims; verify crossover rationale (parent must lose to targets donor beats). A lower-rated donor losing to the parent's nemeses can silently drop hard-won defensive gates — always diff fold-gate presence post-crossover.
- Crossover can emit complete no-ops AND silently discard mutations: hard 'crossover-delta' gate must reject any child byte/AST-identical to its source parent (v263≡v244 passed every gate). Inventory current functions/dispatch sites before declaring a mechanism new/missing/preserved.
- Latent engine-convention bugs (sb/bb assignment, raise-to-total) can persist 80+ generations undetected — verify engine/judge.py contract via reconstruct_state unit tests before trusting downstream logic.
- Position-semantics invariant (v279 FOUNDATIONAL, migrated advisory): engine/judge.py canonical 'dealer=SB, non-dealer=BB' → sb=dealer_id, bb=1-dealer_id heads-up. v241's `next_player(dealer_id,1/2)` derivation INVERTED seats for 80+ gens, silently killing sb_open/bb_vs_raise preflop_spot branches — verify seat-derivation against engine/judge.py, never inherit it.
- H2H-framing-in-code-comments fabrication is a 4x recurring pattern (v221/v224/v225/v279): inline comments cite H2H deltas 5-10x exaggerated vs head_to_head.json. Do not embed H2H numbers in code comments; reviewers treat inline H2H claims as untrusted.
- Every declared gate condition must be an EXECUTABLE body branch returning None when unmet — verify body + call-site arity + per-action branches, not just the trigger. Prefer dead-code removal/dispatch repair over adding constants.
- `_PersistentBot` drains stderr; use stderr/fire-rate as early reachability evidence, H2H/paired net-chips as final EV proof. Precommit timeout fallback can mask weak evidence — pause daemon interference before precommit.
- Evaluate polarized-aggression by paired net-chips + blowout frequency, not W-L alone; <30g noise, ≥30g actionable, ≥100g durable.
- Anti-lock trash gate must be tournament-safe: hands_left>3, my_chips>15BB, low fold_to_raise before suppressing trash jams; short-stack trash jams can be necessary double-up escapes.

## RECENT_LESSONS
- **v290**: SPR commitment thresholds are a structurally new turn/river axis DISTINCT from the three CLOSED fold-side directions — treat TPTK/overpair/two_pair commitment gating as an open tuning surface, not a closed direction.
- **v290**: Bypassing all five fold gates (incl. board_texture-aware should_fold_postflop) means committed hands never see flush_pressure/straight_pressure on dynamic boards — revisit if v290 loses to polarized jammers on wet textures.
- **v290 归档建议**: If live mirror battles show v290 beats jam-mergers (v287) but loses to polarized jammers, add opponent_model.fold_to_jam_rate gating on the TPTK threshold (drop 4→3 when flush_pressure>=1.0 OR straight_pressure>=1.0) rather than weakening baseline commitment levels.
- **v288**: When adding a fold/jam-defer gate, ensure its trigger slack is LOOSER than existing code's slack — v288's standard_jam slack (0.01) was tighter than existing 0.02, a no-op delta; only the wide_jammer carve-out (allin_rate≥0.20) actually changes behavior.
- **v288**: Before stacking preflop logic onto bb_vs_raise/sb_vs_reraise, resolve foundational sb_open/bb_vs_raise seat detection (v279 position-semantics invariant in GENERAL) — unverified seat routing silently misfires new preflop gates.
- **v288 归档建议**: Before treating v288 as a v46/v36 counter, run ≥30g stderr telemetry to confirm PREFLOP_JAM_DEFENSE.wide_jammer fires vs v46/v36 — and cite one replay hand (GxHx#anchor) proving v287 folded a +EV spot to a preflop jam (over-fold diagnosis unproven at n=15).
- **v287**: Strategic-axis changes (immediate-raise vs historical-raise vs archetype) can stack on the same call_threshold and double-count EV — gate new axes to fire only on cross-axis disagreement, or document bounded stacking magnitudes.
- **v287**: Before tuning bb_vs_raise / defense thresholds, profile the nemesis's actual open-size distribution via replay — if nemeses classify 'standard'/'unknown' (raise_samples<4), the historical axis is inert; pivot to a different street/mechanism.
