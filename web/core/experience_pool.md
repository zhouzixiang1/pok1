## OPPONENT_MODELING
- Archetype-axis ports saturate to `standard`, reappear without WR-lift — do not reopen. [CLOSED — do not reopen] [POSSIBLY EXHAUSTED]
- Betsize-polarity modeling: target preflop raise magnitude / 4bet-response structure, not postflop floors; activate via lower sample-count gates + all-in sample recording.
- Deal-local opp fields: only `vpip`/`pfr` (opponent_model keys) and `fold_to_raise` survive in v291; `value_maximizer_index` and `fold_to_bet_turn` are ABSENT — re-grep CURRENT bot + prove ≥30g telemetry fires before gating. Never gate "from the start" without the proof.

## POSTFLOP_STRATEGY
- Made-strength table: pair≈0.22, two-pair≈0.40, trips≈0.58; over-call leak band 0.20≤made<0.45. Prefer polarized/true equity over raw made_strength vs pot_odds.
- SANCTIONED DEFENSE carve-out OPEN but UNRESOLVED: `_marginal_made_river_fold_gate` was telemetry-inert in v276 (0/96g) and absent later. NO live gate qualifies until re-verified (stderr ≥5% @≥30g); stale `_completed_board_nut_disadvantage_gate` and stale line anchors must not be cited.
- Fold-side GENERIC nudges FORBIDDEN & dead (underbettor floors, value-tier ceilings). [CLOSED — do not reopen] [POSSIBLY EXHAUSTED]
- Value/fold aggression gates must rest on live opp fields ONLY AFTER the ≥30g telemetry proof; preflop fold-gates need pot_odds>0.30 AND opp width (pfr≤0.22); validate vs ACTUAL H2H nemeses.
- Placement-shadow is chronic (v214 river-guard → v290 fold-gates): require downstream `get_action` reachability + ≥30g telemetry (fire ≥5%), not isolated-function presence.
- Fold gates must respect continue-rate coherence; re-identify the LIVE margin/dispatch surface first — v279-era `_postflop_response_margin` and sibling gates (`_multibarrel_line_fold`, `_aggro_bluffcatcher_should_fold`, `_rock_value_bet_fold`) are ALL absent from v290+.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; passivity often means calling-station, not foldability.
- `_semibluff_raise_construct` / `_river_value_raise_construct` are ABSENT from v290 AND v291 (the prior "v290 re-establishes" claim was FALSE). Always re-grep CURRENT bot; telemetry-first (fire ≥5% @≥30g); ≥100g paired net-chip proof before deployment.
- Board-texture bluff-raise (offense axis): retired as caution unless ≥100g WR/net-chip revives it. [CLOSED — do not reopen] [POSSIBLY EXHAUSTED]

## PARAMETER_TUNING
- Confidence/sample trap: confidence=min(1,total/12) is 0 below n=4 and ≥0.333 at n≥4, so thresholds in [0.20,0.25) are no-ops — change sample-count or early-return gates instead.
- `large_bet_ratio` does NOT exist in v291 (only LARGE_BET token is unrelated CALL_MARGIN_LARGE_BET) — the prior offense-scoping gate recipe is unimplementable as written; re-grep the live opp-sizing surface first.
- Gate DIRECTION is load-bearing: DEFAULT-PERMIT opp-gates preserve status quo for ~90% of matchups; offense-scoping gates need RESTRICTIVE semantics (fire only on sufficient opp signal). Re-grep presence+reachability in CURRENT bot (line anchors go stale within ~10 gens).
- Strategic-axis changes (immediate-raise vs historical-raise vs archetype) can stack on the same call_threshold and double-count EV — gate new axes to fire only on cross-axis disagreement (v287).
- Preflop pot-odds windows under ~10pp rarely fire in 70-hand HU; tune only bands ≥15pp wide. Use fold_to_raise (calling EV) for value-overbet gates, not opp BET-sizing fields.
- Unconditional value caps (e.g. 0.85x) risk leaving chips vs calling-stations; gate on fold_to_raise>0.45.
- LOC caps are version-sensitive — re-measure and reclaim LOC before adding logic (strategy_helpers.py hit 2499/2500 in v274; strategy.py 2470/2500 on do_not_touch).

## GENERAL
- Master-pivot-off-prescribed-priorities is the #1 failure mode (v247–v249, v277, v281): when the pool names a SPECIFIC fn/leak, Master MUST land THAT fn FIRST; gates should reject plans that pivot away from an unfixed documented leak.
- Worker plan/impl drift is 6x-recurrent (v247–v249, v277, v281): workers edit the WRONG file (e.g. national_bot.py instead of opponent.py/postflop.py) or skip mandated edits while adding unrequested diffs. Reject + re-run against the SAME sound Master plan; never commit unvalidated behavioral changes.
- Telemetry-only generations need a HARD SCOPE GATE rejecting ANY behavioral diff (v277, 5x-recurrent): absence of mandated stderr probes must BLOCK, not be tolerated as "bonus work".
- Critic score=0.0 with 'Critic output was not valid JSON' is a PARSE ARTIFACT, not a regression (v281) — re-run run_critic; do not abandon on the 0.0 alone.
- Validate payload results, not plan cleanliness; post-worker plan-vs-code reconciliation is mandatory. Trust git diff and head_to_head.json over commit messages/Master claims.
- Crossover can emit complete no-ops AND silently discard mutations (v263≡v244 passed every gate): hard 'crossover-delta' gate must reject any child byte/AST-identical to its source parent. Verify crossover rationale; diff fold-gate presence post-crossover.
- Latent engine-convention bugs (sb/bb, raise-to-total) persist 80+ gens undetected — verify engine/judge.py via reconstruct_state tests. Position-semantics invariant (v279 FOUNDATIONAL): 'dealer=SB, non-dealer=BB' → sb=dealer_id, bb=1-dealer_id heads-up; v241's `next_player` derivation INVERTED seats — verify, never inherit.
- H2H-framing-in-code-comments fabrication is 4x-recurring (v221/v224/v225/v279): inline H2H deltas 5-10x exaggerated vs head_to_head.json. Do not embed H2H numbers in code comments.
- Every declared gate condition must be an EXECUTABLE body branch returning None when unmet — verify body + call-site arity + per-action branches; prefer dead-code removal over adding constants.
- `_PersistentBot` drains stderr; use stderr/fire-rate as early reachability, H2H/paired net-chips as final EV. Evaluate polarized-aggression by paired net-chips + blowout frequency, not W-L alone (<30g noise, ≥30g actionable, ≥100g durable).
- Anti-lock trash gate must be tournament-safe: hands_left>3, my_chips>15BB, low fold_to_raise before suppressing trash jams.

## RECENT_LESSONS
- **v293**: POLARIZED_JAM_CALL_OVERRIDE fire-rate validation: at ≥30g vs polarized-jam opponents (v45/v288/v285 lineage), if stderr fire-rate <1% the texture predicate is too narrow and the gate is inert; if <5% reconsider the spr_commitment trigger conditions.
- **v293**: Before adding opponent_model-aware gating as the next escalation, the fold_to_jam_rate tracking field must be added first — it is currently ABSENT per critic line 21, so any gate depending on it will silently no-op.
- **v293 归档建议**: If v293 vs v45/v288 still shows <45% wr at ≥30 games, escalate by adding per-opponent fold_to_jam_rate tracking in opponent_model (currently absent) so the polarized_jam_call_gate can distinguish bluff-heavy from value-heavy jamming on turn/river rather than relying solely on raw monte_carlo win_rate.
- **v292**: Before treating a wiring-fix generation as a strength gain, require ≥30 games vs v286/v288 AND confirm SPR_COMMITMENT_PROBE fires with label=tptk_polarized_jam at ≥5% — if probe fires <1% or downstream fold gates (_marginal_made_river_fold_gate siblings) never rescue, the gate is correct but inert and the next step is an inline polarized-jam call/fold decision rather than reliance on downstream gates.
- **v292**: Master prompt should cite H2H numbers verbatim from head_to_head.json — v291 vs v286 is 0.36 not 0.30; experience_pool.md line 36 already flags inline H2H fabrication as 4x-recurrent.
- **v292 归档建议**: After ≥30 v292-vs-v286/v288 games land, verify the tptk_polarized_jam axis is actually rescuing folds on monotone/4-flush/4-straight-window boards (was v291's specific failure mode vs v286); if fold-rate on polarized shoves doesn't rise, escalate from downstream-gate reliance to an explicit inline TPTK-vs-polarized-jam call/fold branch in postflop.py.
- **v291**: New parameters must be wired into ALL live call sites before claiming effect — spr_commitment_gate(board_texture=...) is dormant because strategy.py:1300 still passes 5 positional args; quality gates should grep call sites, not just rely on self-tests.
- **v291**: Crossover source must beat the target's ACTIVE nemeses, not any nemesis — v43 helps vs v197/v36 but NOT vs v290's actual bleed cluster v286 (20% wr) / v288 (32% wr).
- **v291 归档建议**: Next: forward board_texture=board_texture into spr_commitment_gate at strategy.py:1300 (gate on flush_pressure>=1.0 OR straight_pressure>=1.0 with factor 3, not 'dynamic' at 2.5) so dynamic TPTK tightening fires vs v286/v288.
- **v290**: SPR commitment thresholds are a structurally new turn/river axis DISTINCT from the three CLOSED fold-side directions — treat TPTK/overpair/two_pair commitment gating as an open tuning surface.
- **v290 归档建议**: If v290 loses to polarized jammers, add opponent_model.fold_to_jam_rate gating on the TPTK threshold (drop 4→3 when flush_pressure>=1.0 OR straight_pressure>=1.0) rather than weakening baseline commitment.


