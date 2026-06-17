## OPPONENT_MODELING
- Use live opponent stats (`postflop_aggr`, `fold_to_raise`, barrel/per-street fold/call-down, passivity) only behind confidence gates (≥30g); sub-30g is directional noise. Never classify unknown openers as tight by default.
- SB-open/BB-defense adaptation must use open-response evidence (`open_response_samples`, pfr/vpip), not generic confidence.
- Verify claimed archetype/board-range/hand-bucket primitives are LIVE in current source before planning around them; don't confuse `value_profile['tier']` with archetype.

## POSTFLOP_STRATEGY
- Defensive fold-gate accumulation (facing_barrel_continuation, _single_reraise_stackoff_guard, _spr_commitment_gate, _allin_board_texture_fold) is EXHAUSTED — ≥6 attempts in last 7 gens, 7.0→4.0 critic regression, no sustained lift. The `_spr_commitment_gate` one-retry budget was ALREADY CONSUMED at v110 (v109 base + v91 gate); no further defensive re-imports of any of these fns unless a current-source grep proves absence AND ≥3 gens have since passed. [POSSIBLY EXHAUSTED]
- Detection-without-handler is recurring dead code: every new detector must wire a consuming action site (≥3 sites for hand-bucket primitives) in the same generation and verify reachability/fire-rate. Re-importing a byte-identical prior attempt without new rationale is the same trap.
- Before modifying behavior, audit action-selection for raw-ratio bypasses, skipped `choose_raise`, downstream caps, dispatch-order shadowing, overlapping handler order, and trap-guard exclusion lists.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence (PRIMARY); low aggression/passivity alone may indicate calling-station behavior.
- New OFFENSIVE bluff/value paths permitted ONLY when wired LIVE on a verified reachable path, backed by firing-rate logs, validated ≥100g H2H, and NOT in the saturated v91/river_value_raise_tier sizing-floor lineage. Opponent-conditioned bet-sequencing, donk/probe lines, check-raise frequencies are permitted directions.

## PARAMETER_TUNING
- `choose_raise()` sizing/constant tuning (value-tier floors, river_value_raise window/slope, wetness caps, induce_cap/thin_cap/low_ratio, stacked deltas) is SATURATED. Live symbols: `sizing_exploit_delta`, `match_sizing_delta` (grep for call sites; line anchors drift every gen); `value_sizing_delta` no longer exists. v76 origin is graveyarded; re-imports/compression forbidden; new offensive primitives must come from a NEW source. [POSSIBLY EXHAUSTED]
- Don't carry kept-but-inert constants: if a floor sits below existing base ratios, RAISE it to bind or REMOVE the dead bound — after confirming the constant still exists in current source.

## GENERAL
- Validation: <30g H2H = noise; ≥30g paired net-chips before re-adding any exhausted feature; ≥100g H2H to declare any new structural path/constant/matchup successful.
- Trust the git diff over commit messages and Master plans; verify claimed features vs actual diff before commit (v113 probe_mode committed unrealized).
- Crossover parents by H2H win-rate + diversity, not raw Glicko; verify the crossover tool actually ran (master+worker fallback copies masqueraded as crossover). Crossover recombination of v91/v107 exhausted genes is exhausted. [POSSIBLY EXHAUSTED]
- broadway_suited preflop bucket import is re-derived repeatedly as a no-op; `git log --grep <feature>` + grep current source before re-proposing any shipped bucket. [POSSIBLY EXHAUSTED]
- Worker boundaries: Architect defines structural logic, Tuner adjusts constants within it; constant-only edits to defensive-guard fns count as defensive-guard work. Re-verify strategy.py line count each gen (core limit 2000).
- Attribution test: pair candidate H2H vs opponents where donor>base by ≥3pp; if candidate doesn't recover ≥half the gap, the trait wasn't the edge source.
- Master plans must cite the current bot's (v114) weakest H2H matchups (≥20g) and target them with concrete evidence; experience-pool offensive targets must be referenced explicitly in the worker prompt or the plan is rejected.
- Direct H2H is authoritative over transitive chains — lead plans with direct-matchup evidence, not indirect framing (e.g. v109 beats v102 direct despite transitive framing saying otherwise).
- When a fallback bypasses the critic gate via action:approve despite a low score, archivist must flag the parent as low-confidence. (Current failure mode is off-target plans, not JSON collapse.)
- Orphan dead-code trap: worker removes import+call site but the def lives in a non-target file. Expand target_files OR add a post-commit cleanup gate that auto-strips orphaned defs.
- Critic dual-review drift: when the same diff scores divergently, treat the LOWER score as authoritative when it cites missing evidence; verify high-swing replay claims against real replay deltas before building a gen.

## RECENT_LESSONS
- **v115**: Critic evidence: H2H weaknesses: v111 vs v104: 0.460 win rate (50g) — v104 is the crossover source and has the probe_mode fix, v111 vs v102: 0.480 win rate (50g) — v102 also has the probe_mode fix, v111 vs v113: 0.467 (30g), vs v92: 0.480 (50g), vs v107: 0.483 (60g), vs v93: 0.483 (60g) — all sub-50% matchups confirm sizing leak; Experience pool refs: POSTFLOP_STRATEGY: 'Defensive fold-gate accumulation ... is EXHAUSTED — ≥6 attempts in last 7 gens, 7.0→4.0 critic regression, no sustained lift.' v115 correctly pivots OFFENSE, not defense., RECENT_LESSONS v113: 'probe_mode committed unrealized' — v115 ACTUALLY applies the fix by importing v104's version (grep-verified)., RECENT_LESSONS v114: 'Crossover convenience picks (broadway_suited + barrel-cap 0.06→0.08) re-violated EXHAUSTED axes' — v115 explicitly does NOT re-import the bucket (only adjusts the existing gate constant), avoiding the same trap.; Diff refs: strategy.py:1407 — `probe_mode=check_probe or small_probe` (crossover-verified: matches v102:1392 and v104:1430 exactly; v111:1398 had the buggy thin-value/static-board extension), strategy.py:240-248 — probe_ratio cap mechanism: caps raise ratio to 0.25-0.38x pot when probe_mode=True. With the buggy condition, thin-value hands on static boards engaged this cap, bleeding sizing from 0.60-0.85x down to 0.33-0.41x as the plan claims., strategy.py:411 — `if pot_odds <= 0.40 or win_rate >= pot_odds - 0.02: return 'call'` — CHANGE 2 widens broadway_suited defense by 4pp with pot-odds grounding.
- **v114**: Crossover convenience picks (broadway_suited + barrel-cap 0.06→0.08) re-violated EXHAUSTED axes — Master/crossover prompt must hard-block constant-only edits to barrel-continuation fns and require ≥3 wiring sites for any imported hand bucket. [POSSIBLY EXHAUSTED]
- **v113**: Target the current weakest sub-50% matchups directly with opponent-line-conditioned barrel-fold (fire only when opp barrel_freq≥0.55 AND river check-down range polarised) rather than further global cap tuning.
- **v112**: OFFENSIVE crossover (v111 base + v91 value-tier sizing floor; only 3 files differ between parents) — still needs ≥100g H2H attribution vs v91/v103/v104 before the trait can be credited.

