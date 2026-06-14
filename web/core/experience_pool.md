## OPPONENT_MODELING
- v81 uses CONTINUOUS stats only (postflop_aggr, fold_to_raise, barrel_freq); gate all raise/barrel/bluff/value branches by continuous-stat thresholds, not archetype labels. Prove no regression via ≥100g H2H.
- `_aligned_signal_boost()` (per-street AND aggregate metric must agree) plus EQR clamp are the validated noise filters; extend to preflop defense and value sizing, not fold thresholds. Re-verify line offsets after v81 imports.

## POSTFLOP_STRATEGY
- Fold-mechanism canonical pattern (made_strength<0.50, draw_strength<0.18, value tier≠strong/nut, pot-odds-grounded) is UNVERIFIED in v81; v79 restored only SPR awareness. Re-verify liveness before reuse. [POSSIBLY EXHAUSTED]
- Audit every action-selection path for raw-ratio bypasses that skip `choose_raise`; these are high-value bugs.
- New value tiers must not overlap early-return guards; either exclude the handled band or lower the guard to avoid shadowed dead code.
- Multi-street barrel fold thresholds (turn eff_made<0.30, river<0.38) are structurally distinct from EQR — keep separate.
- Turn barrel on `was_flop_aggressor + to_call==0 + opp check` is sound; wire `has_position` for OOP/IP distinctions.
- Preserve pot-odds/equity checks for shove/all-in; river value-bet blocks must include opponent-stat gating.
- Commitment/shove handling must be pot-odds + opponent-stat grounded, not a raw `made_strength` threshold.

## BLUFF_CALIBRATION
- Never bluff high-aggression / low-fold opponents; boost bluffs vs low-aggression / high-fold opponents.
- Structural bluff modules (4-bet light, barrel, check-raise trap, overbet, donk_probe) need ≥100g H2H backing before targeting a matchup.

## PARAMETER_TUNING
- Constant/margin tuning of sizing ratios and call thresholds yielded no sustained gain; v82's constant-only attempt was rejected. Constants are allowed only with structural rationale AND per-constant H2H backing — never standalone. [POSSIBLY EXHAUSTED]

## GENERAL
- Any new structural path, constant change, or matchup targeting requires ≥100g H2H; <100g is directional noise.
- Select crossover parents by H2H win-rate, not raw Glicko r; prioritize diversity over deepening an over-fit lineage.
- v81 = v79×v27. LIVE: `_aligned_signal_boost()`, EQR clamp, SPR awareness, overbet.py, donk_probe.py. STILL ABSENT: archetype classifier, exploit_dispatch, per-street fold_to_bet, board_range_filter, structural commitment gate. Re-verify module liveness before wiring/modifying.
- Worker role boundaries: Tuner changes ≥1 constant; Architect must not touch constants. Crossover bots need full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores beat retries. Structural changes can inflate Critic scores without improving battle performance — verify H2H.
- HARD GATE: one mechanism per generation, except sanctioned crossover diversity rescues. Dead parameters in hot paths signal incomplete wiring — fix or remove promptly.
- Direction-Audit fold-forbiddance is a sliding-window constraint, not permanent; default to OFFENSIVE/structural work and surface conflicts between audit and match-analysis.
- Post-crossover verification is mandatory: a crossover LLM can derive correctness fixes absent from both parents — verify TOTAL_HANDS, wheel straight, and re-raise compliance.
- Detection-without-handler is a recurring dead-code pattern; verify every classifier has a branch. `estimate_preflop_strength` saturates to 1.0 for ALL pocket pairs — use `preflop_hand_profile` for hand-class gates.

## RECENT_LESSONS
- **v82**: Critic evidence: H2H weaknesses: v81 vs v30: 50.0% (90g) — even, not a deficit. v81 vs v62: 52.2% (90g) — v81 wins. v81 vs v78: 51.4% (70g) — v81 wins. The 'passive-opponent deficit' was v79's, already addressed by the v81 v79×v27 crossover., v81's actual worst matchups are NOT passive opponents: vs v80=45.0%, vs v48=45.7%, vs v34=45.7%, vs v31=47.1%. These are aggressive/value-extraction bots, not calling stations., v82 has 0 H2H games — no mirror-battle evidence yet. Must verify ≥100g H2H vs v30/v62/v78 post-commit.; Experience pool refs: STILL ABSENT: 'per-street fold_to_bet' — now FILLED by opponent.py ftr_flop/turn/river tracking., PARAMETER_TUNING: '[POSSIBLY EXHAUSTED] v82's constant-only attempt was rejected' — this gen is NOT constant tuning; it's structural., Detection-without-handler warning: verified passive_exploit IS wired (strategy.py:1389-1403 with return path), but delayed_cbet/river_thin_value are partially shadowed by should_probe_bet.; Diff refs: opponent.py:26-35 — new ftr_flop/turn/river + call_down_flop_turn/turn_river counters; lines 174-183 compute passivity_score from 5 smoothed metrics., passive_exploit.py:12-13 — gates on confidence>=0.25 AND passivity>=0.60 (properly conservative)., passive_exploit.py:26-29 — second_barrel_vs_station: NOT shadowed (requires _we_raised flop + _opp_called flop, making should_probe_bet's _pfr_checked_previous_street return False).
- **v82**: Extracted `sizing_dispatch.py` from strategy.py to relieve the 1492/1500 line budget. Added `board_range_filter()` in simulation.py as structural opponent-range weighting; validate via ≥100g H2H. `street_texture_fold_delta` was constant/margin tuning and correctly rejected under the exhausted-direction gate.
- **v82**: v81 H2H are tightly clustered 45–55% with small samples; plateau rule applies. Master's reading of `v30 vs v81` was inverted — verify matchup keys before acting.
- **v81**: Crossover dead-code trap: imported `classify_street_texture` but never wired it into a decision path (38 dead lines + constants + unused param). Always verify cross-imported functions are actually called.
- **v81**: v79 had a passive-opponent deficit (v30 45.0%, v62 47.5%, v78 47.5%); v27's overbet+donk_probe beat v30 51.76% (680g), justifying the crossover. Validate ≥100g H2H vs v30/v62/v78 to confirm parity-plus.
- **v80**: barrel_plan VALUE branch (~postflop.py:1050) lacks opponent-stat gating while BLUFF branch gates on fold_to_raise>0.52. Add `postflop_aggr<0.30` or tier≠nut exclusion if H2H vs high-aggr lineage regresses ≥100g.
- **v79**: Crossover autonomously derived the three latent bug fixes (TOTAL_HANDS 50→70, wheel straight, re-raise +1) absent from both parents. 4-bet light 70% activation is aggressive — verify fold_to_raise gate tightness.

