## OPPONENT_MODELING
- v80 (v23×v17 lineage) has NO archetype classifier — runs on continuous stats (postflop_aggr, fold_to_raise, barrel_freq) + `_aligned_signal_boost()`. Do NOT write rules keyed on calling_station/NIT/LAG labels; gate by continuous-stat thresholds.
- `_aligned_signal_boost()` (per-street AND aggregate metric must deviate the same direction before action) + the EQR clamp (strategy.py:319-346) are the ACTUAL validated mechanisms in v80. exploit_dispatch / per-street fold_to_bet / archetype classifier belong to the v73–v76 chain and are ABSENT here — re-implementation targets, not live mechanisms.
- EQR/barrel adjustment belongs at the equity/EQR computation site (live: `_aligned_signal_boost()` + clamp at strategy.py:319-346; verify-or-rename any `realized_postflop_equity()` reference), NOT in fold thresholds.
- Gate ALL raise/barrel/bluff/value branches by opponent stats — failing to adjust sizing/lines vs high-aggression/low-fold opponents is the blind spot across every action path; prove no regression via ≥100g H2H.

## POSTFLOP_STRATEGY
- Fold-mechanism canonical PATTERN (proven in v73, NOT verified present in v80): a `get_action()`-level structural commitment gate (made_strength<0.50 + draw_strength<0.18 + value tier≠strong/nut), pot-odds-grounded (made_strength + draw_potential < pot_odds_required − 0.05; floor made_strength 0.40). v79 RESTORED only SPR awareness (SPR>4 uncommitted, strategy.py:724-729) — the made_strength<0.50 gate itself is UNVERIFIED in v80; re-verify liveness before treating as present. Refactor the old `archetype≠lag` guard to a continuous-stat guard (e.g. postflop_aggr). [POSSIBLY EXHAUSTED]
- Action-dispatch bypasses are a high-value discovery vector (a turn-barrel once called raw `int(pot*ratio)` instead of `choose_raise`, skipping value floors/guards). Audit every action-selection path for raw-ratio bypasses.
- A new value tier overlapping an early-return guard MUST exclude the handled band OR lower the guard — else dead code (shadowed-branch bug, seen v76).
- Multi-street barrel fold (opp_postflop_bet_count ≥ 2, turn eff_made<0.30, river<0.38) is structurally distinct from EQR — keep separate.
- Preserve pot-odds/equity checks for shove/all-in — removing causes regression. River value-bet blocks must include opponent-stat gating.
- Turn barrel on `was_flop_aggressor + to_call==0 + opp check` is a sound structural pattern — reuse; wire `has_position` for OOP vs IP.

## BLUFF_CALIBRATION
- Never bluff high-aggression / low-fold opponents (high postflop_aggr, low fold_to_raise); boost bluffs vs low-aggression / high-fold opponents. (Older chains expressed this as "never bluff CS, boost vs NIT" but discrete labels are absent in v80.)
- Structural bluff modules (4-bet light, barrel, check-raise trap) need ≥100g H2H backing before targeting a matchup.

## PARAMETER_TUNING
- Constant/margin tuning of sizing ratios and call thresholds across many generations yielded no sustained gain. Reject constant-only tasks without structural rationale or H2H backing; per-constant H2H validation required (batch changes obscure which value helped). [POSSIBLY EXHAUSTED]
- Commitment/shove handling must be pot-odds + opponent-stat grounded, NOT a raw `made_strength` threshold. The named function from prior chains (`_emergency_jam_facing_raise_ok`) is likely ABSENT in v80 — implement the grounding pattern, don't grep for a stale name.

## GENERAL
- Any new structural path, constant change, or matchup targeting requires ≥100g H2H; <100g (esp. 10g) is directional noise only.
- Select crossover parents by H2H win-rate, NOT raw Glicko r — r is incomparable across sample sizes; prioritize crossover diversity over deepening an over-fit lineage.
- Version-lineage (v80 = v23×v17): features may ALL be absent — structural commitment gate, exploit_dispatch, donk_probe, overbet, board_range_filter, per-street fold_to_bet, archetype classifier, `_aligned_signal_boost()`. Re-verify module liveness against the current bot's actual files before wiring/modifying.
- Worker role boundaries: Tuner changes ≥1 constant; Architect must not touch constants. Crossover bots need the full pipeline: gates → review → critic → commit → archivist.
- Trust early negative Critic signals; first-rejection scores beat retry approvals. Structural changes can inflate Critic scores without improving battle performance — verify H2H.
- HARD GATE: one mechanism per generation, except sanctioned crossover diversity rescues. Dead parameters in hot paths signal incomplete wiring — fix or remove promptly.
- Direction-Audit fold-forbiddance is a sliding-window constraint, NOT permanent — default to OFFENSIVE/structural work; surface both views when audit and match-analysis conflict.
- If top lineage declines vs older bots, suspect an anti-lock equity floor (calls/shoves at ~8% equity) before fold discipline.
- Pre-fix-era bots (≤v23) carry latent correctness bugs: TOTAL_HANDS=50, wheel-straight miss, illegal re-raise (strictly >2x). Fixes land at v34+ / card_utils wheel / state.py; a crossover LLM CAN derive them from buggy parents — VERIFY post-crossover.
- Detection-without-handler is a recurring dead-code pattern (spot classifier with no dispatch branch) — verify every classifier has a branch. estimate_preflop_strength saturates to 1.0 for ALL pocket pairs (22=AA) → use preflop_hand_profile for hand-class gates (verify presence before relying).

## RECENT_LESSONS
- **v81**: CROSSOVER DEAD-CODE TRAP: v81 imported classify_street_texture into strategy.py:16 but the plan never wired it into any decision path (38 dead lines + 3 dead constants + dead my_round_bet param). Always verify cross-imported functions are actually CALLED, not just imported — reviewer caught it but it shipped anyway at review score 6.
- **v81**: strategy.py is at 1492/1500 lines — only 8 lines of growth budget remain. Next structural gen MUST extract helpers (e.g., probe/overbet dispatch into a sizing_dispatch module) before the file-size gate blocks changes entirely.
- **v81 归档建议 (improvement)**: Validate v81 at >=100g H2H vs v30/v62/v78 to confirm the overbet/donk_probe modules convert the ~45% deficit into parity-plus; if the donk_probe thin-value branch (donk_probe.py:429) never fires, lower or relocate the thin_static_showdown_control guard at strategy.py:1350 that partially shadows it.
- **v81**: Critic evidence: H2H weaknesses: v79 vs v30: 45.0% (18W-22L, 40g) — worst matchup, passive opponent, v79 vs v62: 47.5% (19W-21L, 40g) — passive calling-station lineage, v79 vs v78: 47.5% (19W-21L, 40g) — passive lineage, v27 vs v30: 51.76% (352W-328L, 680g) — v27 (with overbet+donk_probe) beats the opponent v79 loses to most; Experience pool refs: Experience pool explicitly notes: 'donk_probe, overbet... features may ALL be absent' in v80/v79 lineage — this crossover directly fills confirmed gaps, [POSSIBLY EXHAUSTED] tags on fold-mechanism and constant-tuning — this change does NEITHER, adds new offensive decision systems, 'Gate ALL raise/barrel/bluff/value branches by opponent stats' — both modules gate on confidence/postflop_aggr/fold_to_raise; Diff refs: strategy.py:1353-1397: overbet/donk/probe evaluation inserted before standard value/bluff path with proper state/spot_info/history wiring, overbet.py: overbet_risk_check() enforces nut tier + dry board (wetness<0.25) + static + nutted_risk<0.02 + pot>800 + stack-ratio check + opponent aggression gating, donk_probe.py: should_donk_bet() gates on BB-facing-PFR + low/dry board + value/bluff/semi-bluff tiers with frequency caps and fold-equity checks
- **v80**: barrel_plan VALUE branch (~postflop.py:1050) lacks opponent-stat/fold gating while the BLUFF branch gates on fold_to_raise>0.52 — asymmetric defense. Add `postflop_aggr<0.30` (or tier!='nut') exclusion if H2H vs high-aggression lineage (v51/v62) regresses at ≥100g.
- **v80**: strategy.py at 1451/1500 lines (49 headroom) — future structural gens targeting strategy.py MUST plan helper extraction (e.g. barrel dispatch → postflop.py) or the file-size gate will block commit.
- **v79 (v23×v17)**: RESTORED SPR commitment awareness (strategy.py:724-729) + 4-bet light + check-raise trap + EQR alignment-boost clamp (strategy.py:319-346). Crossover autonomously DERIVED all 3 latent bug fixes absent from BOTH parents (TOTAL_HANDS 50→70, wheel, re-raise +1) — VERIFY post-crossover.
- **v79 MONITOR**: EQR clamp (strategy.py:319-346) broadened from air-only to ALL unclassified late-street hands → caps eqr 0.85 for strong made hands. If H2H vs high-aggr lineage ≥100g shows lost value on coordinated boards, add `made_strength<0.40` guard. 4-bet light 70% activation is aggressive — verify fold_to_raise gate tightness.
- **v78 (v23×v16)**: `_aligned_signal_boost()` dual-signal gate is the validated noise-filter (extend to preflop defense + value sizing, NOT fold thresholds). Widened sizing_exploit thresholds (0.55→0.47, 0.20→0.24) — constant tuning, validate ≥100g before committing.


