## OPPONENT_MODELING
- v82 uses continuous stats (postflop_aggr, fold_to_raise, barrel_freq) plus per-street fold_to_bet/call-down tracking (ftr_flop/turn/river) and passivity_score in opponent.py; gate exploitation on confidence>=0.25 AND passivity>=0.60.
- `_aligned_signal_boost()` and EQR clamp are validated noise filters; extend to preflop defense and value sizing, not fold thresholds.
- `estimate_preflop_strength` saturates to 1.0 for all pocket pairs — use `preflop_hand_profile` for hand-class gates.

## POSTFLOP_STRATEGY
- Canonical fold mechanism (made_strength<0.50, draw_strength<0.18, non-strong/nut tier, pot-odds-grounded) is unverified in v82; re-verify liveness before reuse. [POSSIBLY EXHAUSTED]
- Audit every action-selection path for raw-ratio bypasses that skip `choose_raise`; these are high-value bugs.
- New value tiers must not overlap early-return guards; exclude handled bands or lower guards to avoid shadowed dead code.
- Multi-street barrel fold thresholds (turn eff_made<0.30, river<0.38) are structurally distinct from EQR — keep separate.
- Turn barrel on `was_flop_aggressor + to_call==0 + opp check` is sound; wire `has_position` for OOP/IP distinctions.
- Preserve pot-odds/equity checks for shove/all-in; river value-bet blocks must include opponent-stat gating.
- Commitment/shove handling must be pot-odds + opponent-stat grounded, not a raw `made_strength` threshold.

## BLUFF_CALIBRATION
- Never bluff high-aggression / low-fold opponents; boost bluffs vs low-aggression / high-fold opponents.
- Structural bluff modules (4-bet light, barrel, check-raise trap, overbet, donk_probe) need ≥100g H2H backing before targeting a matchup.

## PARAMETER_TUNING
- Constant/margin tuning of sizing ratios and call thresholds yielded no sustained gain; constants are allowed only with structural rationale AND per-constant H2H backing — never standalone. [POSSIBLY EXHAUSTED]

## GENERAL
- Any new structural path, constant change, or matchup targeting requires ≥100g H2H; <100g is directional noise.
- Select crossover parents by H2H win-rate, not raw Glicko r; prioritize diversity over deepening an over-fit lineage.
- v82 = v81→MASTER. LIVE: per-street fold_to_bet/call-down tracking, passivity_score, passive_exploit.py, `_aligned_signal_boost()`, EQR clamp, SPR awareness, overbet.py, donk_probe.py. STILL ABSENT: archetype classifier, exploit_dispatch, board_range_filter, structural commitment gate. Re-verify liveness before wiring/modifying.
- Worker role boundaries: Tuner changes ≥1 constant; Architect must not touch constants. Crossover bots need full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores beat retries. Structural changes can inflate Critic scores without improving battle performance — verify H2H.
- HARD GATE: one mechanism per generation, except sanctioned crossover diversity rescues. Dead parameters in hot paths signal incomplete wiring — fix or remove promptly.
- Direction-Audit fold-forbiddance is a sliding-window constraint, not permanent; default to OFFENSIVE/structural work and surface conflicts between audit and match-analysis.
- Post-crossover verification is mandatory: a crossover LLM can derive correctness fixes absent from both parents — verify TOTAL_HANDS, wheel straight, and re-raise compliance.
- Detection-without-handler is a recurring dead-code pattern; verify every classifier has a branch.

## RECENT_LESSONS
- **v83**: Critic evidence: H2H weaknesses: v82 vs v80: 45% (20g) — aggressive barrel-sequencing bot, v82 vs v13: 45% (20g) — early-lineage value bot, v82 vs v79/v50/v61: 40% (10g each, small-sample noise); Experience pool refs: RECENT_LESSONS: 'v82 real weaknesses vs aggressive/value-extraction bots (v80=45.0%)', POSTFLOP_STRATEGY: canonical fold mechanism marked [POSSIBLY EXHAUSTED] — but this change adds a NEW gating dimension (line classification) on top, not the same mechanism, GENERAL: 'Detection-without-handler is a recurring dead-code pattern; verify every classifier has a branch' — value_heavy IS wired (3 sites); bluff_heavy is NOT wired (minor dead branch); Diff refs: line_reading.py: line_polarization_profile() — 66-line novel classifier with value/bluff signal aggregation, strategy.py:772-774 — fold gate when facing all-in with value_heavy line + weak hand, strategy.py:801-803 — fold gate when facing shove with value_heavy line + weak hand
- **v82**: v81's actual H2H are tightly clustered 45–55% with small samples; its real weaknesses are vs aggressive/value-extraction bots (v80=45.0%, v48=45.7%, v34=45.7%, v31=47.1%), while v30/v62/v78 are parity or winning. Target next-gen work at v80/v48/v34, not calling stations; verify matchup keys before acting.
- **v82**: Added per-street fold_to_bet + call-down tracking + passivity_score in opponent.py and passive_exploit.py wired into strategy.py. Validate ≥100g H2H vs v80/v48/v34; ensure passivity gating does not blunder value vs aggressive opponents.
- **v82**: strategy.py is at 1498/1500 lines — next generation must extract helpers before adding code or it will fail the size gate.
- **v82**: `passive_exploit.py` second_barrel_vs_station is correctly wired and not shadowed by `should_probe_bet`; verify delayed_cbet/river_thin_value branches remain reachable.
- **v81**: Crossover dead-code trap: imported `classify_street_texture` but never wired it into a decision path. Always verify cross-imported functions are actually called.
- **v81**: v79 had a passive-opponent deficit (v30 45.0%, v62 47.5%, v78 47.5%); v27's overbet+donk_probe beat v30 51.76% (680g), justifying the crossover. Validate ≥100g H2H vs v30/v62/v78 to confirm parity-plus.
- **v80**: barrel_plan VALUE branch (~postflop.py:1050) lacks opponent-stat gating while BLUFF branch gates on fold_to_raise>0.52. Add `postflop_aggr<0.30` or tier≠nut exclusion if H2H vs high-aggr lineage regresses ≥100g.

