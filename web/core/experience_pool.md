## OPPONENT_MODELING
- v81 uses CONTINUOUS stats (postflop_aggr, fold_to_raise, barrel_freq) — NO archetype classifier. Gate ALL raise/barrel/bluff/value branches by continuous-stat thresholds, NOT calling_station/NIT/LAG labels; failing to adjust sizing/lines vs high-aggression/low-fold opponents is the blind spot across every action path. Prove no regression via ≥100g H2H.
- `_aligned_signal_boost()` (per-street AND aggregate metric must deviate the same direction before action) + EQR clamp are the validated noise-filter mechanisms; extend to preflop defense + value sizing, NOT fold thresholds. Line offsets shifted after v81's overbet/donk imports — re-verify before editing.

## POSTFLOP_STRATEGY
- Fold-mechanism canonical PATTERN (made_strength<0.50 + draw_strength<0.18 + value tier≠strong/nut, pot-odds-grounded) is UNVERIFIED in v81; v79 restored only SPR awareness (SPR>4 uncommitted). Re-verify liveness before treating as present; refactor old archetype guard to continuous-stat (postflop_aggr). [POSSIBLY EXHAUSTED]
- Action-dispatch bypasses are a high-value discovery vector (a turn-barrel once called raw `int(pot*ratio)` instead of `choose_raise`). Audit every action-selection path for raw-ratio bypasses.
- A new value tier overlapping an early-return guard MUST exclude the handled band OR lower the guard — else dead code (shadowed-branch bug, seen v76).
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made<0.30, river<0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in — removing causes regression. River value-bet blocks must include opponent-stat gating.
- Turn barrel on `was_flop_aggressor + to_call==0 + opp check` is a sound structural pattern — reuse; wire `has_position` for OOP vs IP.

## BLUFF_CALIBRATION
- Never bluff high-aggression / low-fold opponents (high postflop_aggr, low fold_to_raise); boost bluffs vs low-aggression / high-fold opponents.
- Structural bluff modules (4-bet light, barrel, check-raise trap, overbet, donk_probe) need ≥100g H2H backing before targeting a matchup.

## PARAMETER_TUNING
- Constant/margin tuning of sizing ratios and call thresholds (river value-tier multipliers, barrel ratios, raise-fractions, overbet %) yielded no sustained gain across many generations; v82's constant-only attempt was rejected. Constants are allowed ONLY with structural rationale AND per-constant H2H backing — never as a stand-alone task (this reconciles GENERAL's ≥100g gate with this exhaustion rule). [POSSIBLY EXHAUSTED]
- Commitment/shove handling must be pot-odds + opponent-stat grounded, NOT a raw `made_strength` threshold. Implement the grounding pattern, don't grep for stale named functions from prior chains.

## GENERAL
- Any new structural path, constant change, or matchup targeting requires ≥100g H2H; <100g (esp. 10g) is directional noise only.
- Select crossover parents by H2H win-rate, NOT raw Glicko r — r is incomparable across sample sizes; prioritize crossover diversity over deepening an over-fit lineage.
- **v81 = v79×v27**: LIVE = `_aligned_signal_boost()`, EQR clamp, SPR awareness, overbet.py, donk_probe.py (imported from v27). STILL ABSENT = archetype classifier, exploit_dispatch, per-street fold_to_bet, board_range_filter, structural commitment gate. Re-verify module liveness before wiring/modifying.
- Worker role boundaries: Tuner changes ≥1 constant; Architect must not touch constants. Crossover bots need full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores beat retry approvals. Structural changes can inflate Critic scores without improving battle performance — verify H2H.
- HARD GATE: one mechanism per generation, except sanctioned crossover diversity rescues. Dead parameters in hot paths signal incomplete wiring — fix or remove promptly.
- Direction-Audit fold-forbiddance is a sliding-window constraint, NOT permanent — default to OFFENSIVE/structural work; surface both views when audit and match-analysis conflict.
- Pre-fix-era bots (≤v23) carry latent correctness bugs: TOTAL_HANDS=50, wheel-straight miss, illegal re-raise (strictly >2x). Fixes land at v34+; a crossover LLM CAN derive them from buggy parents — VERIFY post-crossover.
- Detection-without-handler is a recurring dead-code pattern — verify every classifier has a branch. estimate_preflop_strength saturates to 1.0 for ALL pocket pairs → use preflop_hand_profile for hand-class gates.

## RECENT_LESSONS
- **v81**: CROSSOVER DEAD-CODE TRAP — imported `classify_street_texture` but never wired it into any decision path (38 dead lines + 3 dead constants + dead my_round_bet param). Always verify cross-imported functions are actually CALLED, not just imported (shipped anyway at review 6).
- **v81**: strategy.py at 1492/1500 lines — only 8 lines growth budget remain. Next structural gen MUST extract helpers (probe/overbet dispatch into a sizing_dispatch module) before the file-size gate blocks changes.
- **v81 matchup**: v79 vs v30 45.0% (worst, passive), v62 47.5%, v78 47.5% — passive-opponent deficit; v27 (overbet+donk_probe) beats v30 51.76% (680g), so the crossover directly fills the confirmed gap. Validate ≥100g H2H vs v30/v62/v78 to confirm parity-plus; if donk_probe thin-value branch never fires, lower/relocate the shadowing guard.
- **v80**: barrel_plan VALUE branch (~postflop.py:1050) lacks opponent-stat/fold gating while BLUFF branch gates on fold_to_raise>0.52 — asymmetric. Add `postflop_aggr<0.30` or tier!='nut' exclusion if H2H vs high-aggr lineage regresses ≥100g.
- **v79 (v23×v17)**: crossover autonomously DERIVED all 3 latent bug fixes absent from BOTH parents (TOTAL_HANDS 50→70, wheel, re-raise +1). VERIFY post-crossover. 4-bet light 70% activation is aggressive — verify fold_to_raise gate tightness.
