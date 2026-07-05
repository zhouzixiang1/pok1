## OPPONENT_MODELING
- Deal-local opp fields: only `vpip`/`pfr` and `fold_to_raise` survive in v293; `value_maximizer_index`, `fold_to_bet_turn`, `fold_to_jam_rate` are ABSENT — re-grep CURRENT bot + prove ≥30g telemetry fires before gating. Never gate "from the start" without the proof.
- Betsize-polarity modeling: target preflop raise magnitude / 4bet-response structure, not postflop floors; activate via lower sample-count gates + all-in sample recording.
- Archetype-axis ports saturate to `standard`, reappear without WR-lift. [CLOSED — do not reopen] [POSSIBLY EXHAUSTED] [STALE — no WR-lift]

## POSTFLOP_STRATEGY
- Made-strength table: pair≈0.22, two-pair≈0.40, trips≈0.58; over-call leak band 0.20≤made<0.45. Prefer polarized/true equity over raw made_strength vs pot_odds.
- SPR commitment thresholds are a structurally new turn/river axis DISTINCT from the three CLOSED fold-side directions — treat TPTK/overpair/two_pair commitment gating as an open tuning surface (active since v290).
- Value/fold aggression gates must rest on live opp fields ONLY AFTER the ≥30g telemetry proof; preflop fold-gates need pot_odds>0.30 AND opp width (pfr≤0.22); validate vs ACTUAL H2H nemeses.
- Placement-shadow is chronic + fold gates must respect continue-rate coherence (v214→v290): require downstream `get_action` reachability + ≥30g telemetry (fire ≥5%), not isolated-function presence. v279-era `_postflop_response_margin` and siblings (`_multibarrel_line_fold`, `_aggro_bluffcatcher_should_fold`, `_rock_value_bet_fold`, `_marginal_made_river_fold_gate`) are ALL absent from v293 — re-identify the LIVE margin/dispatch surface first.
- SANCTIONED DEFENSE carve-out OPEN but UNRESOLVED: NO live gate qualifies until re-verified (stderr ≥5% @≥30g); stale `_completed_board_nut_disadvantage_gate` and stale line anchors must not be cited.
- Fold-side GENERIC nudges FORBIDDEN & dead (underbettor floors, value-tier ceilings). [CLOSED — do not reopen] [POSSIBLY EXHAUSTED] [STALE — no WR-lift]

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; passivity often means calling-station, not foldability.
- `_semibluff_raise_construct` / `_river_value_raise_construct` are ABSENT from v293 (prior "v290 re-establishes" claim was FALSE). Always re-grep CURRENT bot; telemetry-first (fire ≥5% @≥30g); ≥100g paired net-chip proof before deployment.
- Board-texture bluff-raise (offense axis) retired as caution unless ≥100g WR/net-chip revives it. [CLOSED — do not reopen] [POSSIBLY EXHAUSTED] [STALE — no WR-lift]

## PARAMETER_TUNING
- Confidence/sample trap: confidence=min(1,total/12) is 0 below n=4 and ≥0.333 at n≥4, so thresholds in [0.20,0.25) are no-ops — change sample-count or early-return gates instead.
- `large_bet_ratio` does NOT exist in v293 (only LARGE_BET token is unrelated CALL_MARGIN_LARGE_BET) — prior offense-scoping gate recipe unimplementable; re-grep the live opp-sizing surface first.
- Gate DIRECTION is load-bearing: DEFAULT-PERMIT opp-gates preserve status quo ~90% of matchups; offense-scoping gates need RESTRICTIVE semantics (fire only on sufficient opp signal). Re-grep presence+reachability in CURRENT bot (line anchors go stale within ~10 gens).
- Strategic-axis changes (immediate-raise vs historical-raise vs archetype) can stack on the same call_threshold and double-count EV — gate new axes to fire only on cross-axis disagreement (v287).
- Preflop pot-odds windows under ~10pp rarely fire in 70-hand HU; tune only bands ≥15pp wide. Use fold_to_raise (calling EV) for value-overbet gates, not opp BET-sizing fields.
- Unconditional value caps (e.g. 0.85x) risk leaving chips vs calling-stations; gate on fold_to_raise>0.45.
- LOC caps are version-sensitive — re-measure and reclaim LOC before adding logic (strategy.py 2470/2500 on do_not_touch).

## GENERAL
- Master-pivot-off-prescribed-priorities is the #1 failure mode (v247–v249, v277, v281): when the pool names a SPECIFIC fn/leak, Master MUST land THAT fn FIRST; gates should reject plans that pivot away from an unfixed documented leak.
- Worker plan/impl drift is 6x-recurrent (v247–v249, v277, v281): workers edit the WRONG file or skip mandated edits while adding unrequested diffs. Reject + re-run against the SAME sound Master plan; never commit unvalidated behavioral changes.
- Telemetry-only generations need a HARD SCOPE GATE rejecting ANY behavioral diff (v277, 5x-recurrent): absence of mandated stderr probes must BLOCK, not be tolerated as "bonus work".
- Critic score=0.0 with 'Critic output was not valid JSON' is a PARSE ARTIFACT, not a regression (v281) — re-run run_critic; do not abandon on the 0.0 alone.
- Validate payload results, not plan cleanliness; post-worker plan-vs-code reconciliation is mandatory. Trust git diff and head_to_head.json over commit messages/Master claims.
- Crossover can emit complete no-ops AND silently discard mutations (v263≡v244 passed every gate): hard 'crossover-delta' gate must reject any child byte/AST-identical to its source parent.
- Latent engine-convention bugs (sb/bb, raise-to-total) persist 80+ gens undetected. Position-semantics invariant (v279 FOUNDATIONAL): 'dealer=SB, non-dealer=BB' → sb=dealer_id, bb=1-dealer_id heads-up; v241's `next_player` INVERTED seats — verify, never inherit.
- H2H-framing-in-code-comments fabrication is 5x-recurring (v221/v224/v225/v279/v292): inline H2H deltas 5-10x exaggerated vs head_to_head.json. Do not embed H2H numbers in code comments; Master must cite head_to_head.json verbatim (v291 vs v286 = 0.36, not 0.30).
- Every declared gate condition must be an EXECUTABLE body branch returning None when unmet — verify body + call-site arity + per-action branches; prefer dead-code removal over adding constants.
- `_PersistentBot` drains stderr; use stderr/fire-rate as early reachability, H2H/paired net-chips as final EV. Evaluate polarized-aggression by paired net-chips + blowout frequency, not W-L alone (<30g noise, ≥30g actionable, ≥100g durable).
- Anti-lock trash gate must be tournament-safe: hands_left>3, my_chips>15BB, low fold_to_raise before suppressing trash jams.

## RECENT_LESSONS
- **v293**: POLARIZED_JAM_CALL_OVERRIDE fire-rate validation — at ≥30g vs polarized-jam opponents (v45/v288/v285 lineage), stderr <1% ⇒ texture predicate too narrow/inert; <5% ⇒ reconsider spr_commitment trigger conditions.
- **v293**: `opponent_model.fold_to_jam_rate` is ABSENT in v293 — must be ADDED first or any opponent-aware polarized-jam gate silently no-ops. If v293 vs v45/v288 stays <45% wr @≥30g, escalate by adding per-opponent fold_to_jam_rate tracking so polarized_jam_call_gate distinguishes bluff-heavy vs value-heavy jamming.
- **v292**: Before treating the wiring-fix gen as a strength gain, require ≥30g vs v286/v288 AND confirm SPR_COMMITMENT_PROBE fires label=tptk_polarized_jam @≥5% — if <1% or downstream fold gates never rescue, the gate is correct but inert → next step is an INLINE polarized-jam call/fold decision, not downstream-gate reliance.
- **v292 归档建议**: After ≥30 v292-vs-v286/v288 games, verify the tptk_polarized_jam axis actually rescues folds on monotone/4-flush/4-straight-window boards (v291's failure mode vs v286); if fold-rate on polarized shoves doesn't rise, escalate to an explicit inline TPTK-vs-polarized-jam call/fold branch in postflop.py.
- **v291**: New parameters must be wired into ALL live call sites before claiming effect — spr_commitment_gate(board_texture=...) was dormant because strategy.py:1300 passed 5 positional args; quality gates must grep call sites, not just self-tests. Forward board_texture=board_texture there (gate on flush_pressure>=1.0 OR straight_pressure>=1.0 @ factor 3, not 'dynamic' @ 2.5).
- **v291**: Crossover source must beat the target's ACTIVE nemeses, not any nemesis — v43 helps vs v197/v36 but NOT vs v290's actual bleed cluster v286 (20% wr) / v288 (32% wr).
