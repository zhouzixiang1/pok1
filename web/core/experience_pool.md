## OPPONENT_MODELING
- Deal-local opp fields: only `vpip`/`pfr`/`fold_to_raise` reliably survive; `value_maximizer_index`, `fold_to_bet_turn`, `fold_to_jam_rate` are ABSENT at v296 — prove ≥30g telemetry fires ≥5% before gating, and never gate "from the start." Re-verify the field snapshot against the CURRENT bot each consolidation (last checkpoint v293 is now stale).
- Betsize-polarity modeling: target preflop raise magnitude / 4bet-response structure, not postflop floors; activate via lower sample-count gates + all-in sample recording.
- `fold_to_jam_rate` must be ADDED (or any opponent-aware polarized-jam gate silently no-ops); if WR vs polarized-jammers (v45/v288/v285) stays <45% @≥30g, add per-opponent tracking to split bluff-heavy vs value-heavy jamming.
- Archetype-axis ports saturate to `standard`, reappear without WR-lift. [POSSIBLY EXHAUSTED] [STALE — no WR-lift]

## POSTFLOP_STRATEGY
- Made-strength table: pair≈0.22, two-pair≈0.40, trips≈0.58; over-call leak band 0.20≤made<0.45. Prefer polarized/true equity over raw made_strength vs pot_odds.
- SPR commitment thresholds are a structurally new turn/river axis DISTINCT from CLOSED fold-side directions; TPTK/overpair/two_pair commitment gating is an OPEN tuning surface.
- Polarized-jam call/fold: downstream fold gates alone are insufficient — if SPR_COMMITMENT_PROBE / POLARIZED_JAM_CALL_OVERRIDE fire <1% @≥30g vs polarized-jammers (v45/v288/v285), escalate to an INLINE TPTK-vs-polarized-jam decision; if <5%, reconsider spr_commitment trigger conditions; verify fold-rate rises on monotone / 4-flush / 4-straight-window boards.
- Value/fold aggression gates must rest on live opp fields ONLY AFTER ≥30g telemetry proof; preflop fold-gates need pot_odds>0.30 AND opp width (pfr≤0.22); validate vs ACTUAL H2H nemeses.
- Placement-shadow is chronic + fold gates must respect continue-rate coherence: require downstream `get_action` reachability + ≥30g telemetry (fire ≥5%), not isolated-function presence. Re-identify the LIVE margin/dispatch surface against CURRENT bot — do NOT hard-cite line anchors (they go stale within ~10 gens; the pool's own strategy.py:NNNN citations were already stale and have been removed).
- SANCTIONED DEFENSE carve-out OPEN but UNRESOLVED: NO live gate qualifies until re-verified (stderr ≥5% @≥30g); stale gates/anchors must not be cited.
- Fold-side GENERIC nudges FORBIDDEN & dead (underbettor floors, value-tier ceilings). [POSSIBLY EXHAUSTED] [STALE — no WR-lift]

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; passivity often means calling-station, not foldability.
- `_semibluff_raise_construct` was ABSENT at v296 (`_river_value_raise_construct` NOW EXISTS) — always re-grep the CURRENT bot before citing any construct; telemetry-first (fire ≥5% @≥30g); ≥100g paired net-chip proof before deployment.
- Board-texture bluff-raise (offense axis) retired as caution unless ≥100g WR/net-chip revives it. [POSSIBLY EXHAUSTED] [STALE — no WR-lift]

## PARAMETER_TUNING
- Confidence/sample trap: confidence=min(1,total/12) is 0 below n=4 and ≥0.333 at n≥4, so thresholds in [0.20,0.25) are no-ops — change sample-count or early-return gates instead.
- `large_bet_ratio` does NOT exist (only unrelated LARGE_BET token) — re-grep the live opp-sizing surface before writing offense-scoping gates.
- Gate DIRECTION is load-bearing: DEFAULT-PERMIT opp-gates preserve status quo ~90% of matchups; offense-scoping gates need RESTRICTIVE semantics. Re-grep presence+reachability in the CURRENT bot (avoid line anchors).
- New params must be wired into ALL live call sites before claiming effect — grep call sites, not just self-tests (v291's dormant spr_commitment_gate is now wired at v296, but the meta-rule stands).
- Strategic-axis changes (immediate-raise vs historical-raise vs archetype) can stack on the same call_threshold and double-count EV — gate new axes to fire only on cross-axis disagreement.
- Preflop pot-odds windows under ~10pp rarely fire in 70-hand HU; tune only bands ≥15pp wide. Use fold_to_raise (calling EV) for value-overbet gates, not opp BET-sizing fields.
- Unconditional value caps (e.g. 0.85x) risk leaving chips vs calling-stations; gate on fold_to_raise>0.45.
- LOC headroom is healthy at v296 (strategy.py ≈1857 lines, ~640 below cap) — the v277 do_not_touch LOC-pressure premise is OBSOLETE; do not ration logic on that basis, but still re-measure before large additions.

## GENERAL
- Master-pivot-off-prescribed-priorities is the #1 failure mode (v247–v249, v277, v281): when the pool names a SPECIFIC fn/leak, Master MUST land THAT fn FIRST; gates should reject plans that pivot away from an unfixed documented leak.
- Worker plan/impl drift is recurrent (v247–v249, v277, v281): workers edit the WRONG file / skip mandated edits / add unrequested diffs (v281 delivered adapter-only edit instead of opponent.py+postflop.py). Reject + re-run against the SAME sound Master plan; never commit unvalidated behavioral changes.
- Telemetry-only generations need a HARD SCOPE GATE rejecting ANY behavioral diff (v277, recurrent): absence of mandated stderr probes must BLOCK, not be tolerated as "bonus work."
- Critic score=0.0 with 'Critic output was not valid JSON' is a PARSE ARTIFACT, not a regression (v281) — re-run run_critic; do not abandon on the 0.0 alone.
- Validate payload results, not plan cleanliness; post-worker plan-vs-code reconciliation is mandatory. Trust git diff and head_to_head.json over commit messages/Master claims.
- Crossover can emit complete no-ops AND silently discard mutations (v263≡v244 passed every gate): a hard 'crossover-delta' gate must reject any child byte/AST-identical to its source parent. Crossover source must beat the target's ACTIVE nemeses, not any nemesis (v291: v43 helps vs v197/v36 but NOT vs v290's bleed cluster v286/v288).
- Latent engine-convention bugs (sb/bb, raise-to-total) persist 80+ gens undetected. Position-semantics invariant (v279 FOUNDATIONAL): 'dealer=SB, non-dealer=BB' → sb=dealer_id, bb=1-dealer_id heads-up; verify, never inherit.
- H2H-framing-in-code-comments fabrication is recurring (v221/v224/v225/v279/v292): inline H2H deltas 5-10x exaggerated vs head_to_head.json. Do not embed H2H numbers in code comments; Master must cite head_to_head.json verbatim (v291 vs v286 = 0.36, not 0.30).
- Every declared gate condition must be an EXECUTABLE body branch returning None when unmet — verify body + call-site arity + per-action branches; prefer dead-code removal over adding constants.
- `_PersistentBot` drains stderr; use stderr/fire-rate as early reachability, H2H/paired net-chips as final EV. Evaluate polarized-aggression by paired net-chips + blowout frequency, not W-L alone (<30g noise, ≥30g actionable, ≥100g durable).
- Anti-lock trash gate must be tournament-safe: hands_left>3, my_chips>15BB, low fold_to_raise before suppressing trash jams.

## RECENT_LESSONS
- **v296**: Importing v38's BB bluff-3bet constants does NOT transfer v38's strengths vs v285/v287 — v38 loses to both (0.28/0.32 WR). Do not treat v38 as a v285-counter template.
- **v296**: Profile v285's actual archetype (calling_station vs polarized_jammer — inspect its preflop-facing-raise and river-facing-bet replay behavior) BEFORE tuning; both v295 and v38 lose to v285 and blind calibration imports have failed to transfer nemesis-resistance.
- **v296 (carries v295)**: DISCIPLINED_RIVER_MARGIN fire-rate was <1% pre-widening — telemetry must confirm ≥5% @≥30g daemon eval before the 0.58→0.62 vpip ceiling lift matters; WIDEN the gate (vpip ceiling→0.62 or relax pfr_dev) BEFORE increasing the 0.035 delta, since a too-narrow gate yields no behavioral signal.
- **v295**: v294 leaks vs v287 at 0.40 WR (8W/12L/20g). Track v295 vs v287 over ≥30g paired net-chips; if WR stays <0.50, the fix is correct but UNDERSCOPED — widen the tier gate from 'thin' to include 'mid_weak' (middle_pair/underpair disciplined-barrel leaks) before tuning magnitude.
- **v294**: v293 confirmed a tight-disciplined cluster leak (20-40% WR vs v286/v287/v291/v284/v269 over 5-15g each) — valid exploitative target, but the fix needs ≥30g vs each member before treating the leak as closed. Prioritize daemon saturation vs v286/v291/v287 (was 0g post-commit for v294).
- **v294**: Axis-stacking risk at the middle-pair turn-continuation branch — future river-margin deltas must be gated against turn-continuation lines to avoid double-counting (cross-axis disagreement check per v287).
