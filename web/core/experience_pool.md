## OPPONENT_MODELING
- Use live opponent stats (`postflop_aggr`, `fold_to_raise`, barrel/per-street fold/call-down, passivity) only behind confidence/sample gates (≥30g); sub-30g matchups are directional noise, not actionable weaknesses.
- SB-open/BB-defense adaptation must use open-response evidence (`open_response_samples`, pfr/vpip), not generic action confidence; never classify unknown openers as tight by default.
- Preflop range gates must use `preflop_hand_profile()` / `classify_preflop_hand()` buckets (shipped v88) and `broadway_suited` (shipped v111), not raw `estimate_preflop_strength` which saturates pocket pairs to 1.0.
- Do not confuse `value_profile['tier']` with opponent archetype; verify claimed archetype/board-range primitives exist and are live before planning around them.

## POSTFLOP_STRATEGY
- DEFENSIVE late-street fold/all-in/texture/pot-odds/polarization/barrel guard accumulation is saturated; new defensive guards require a distinct decision point AND ≥3 cited replay hands from current lineage where made_strength≥0.50 folded vs 2:1+ pot-odds, plus ≥100g validation. [POSSIBLY EXHAUSTED]
- Detection-without-handler is recurring dead code; every new detector must wire a consuming action site in the same generation and verify reachability/fire-rate. Re-introducing a byte-identical prior attempt without new rationale is the same trap.
- Confirm named primitives exist in current source via grep before referencing them; docstrings, memories, stale notes, and old helper names are not definitions.
- Audit action-selection paths for raw-ratio bypasses, skipped `choose_raise`, downstream caps, dispatch-order shadowing, overlapping handler order, and trap-guard exclusion lists before modifying behavior.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence and confidence; low aggression/passivity alone may indicate calling-station behavior.
- New offensive bluff/value paths remain permitted when wired LIVE, backed by firing-rate logs, and validated ≥100g H2H — exhaustion applies to DEFENSIVE guards only.
- Structural bluff modules require current-source live-path verification before being treated as successful or expanded.

## PARAMETER_TUNING
- DEFENSIVE sizing constant tuning (caps/floors/defensive call thresholds, including induce_cap/thin_cap/low_ratio/value-tier floor at the same call sites) has no sustained gain; constants-only inside an Architect-defined structural hypothesis with per-constant H2H backing. [POSSIBLY EXHAUSTED]
- Exclude new defensive sizing-tier/floor/cap increases from Tuner work unless current source proves dispatch order, downstream caps, and target live path are not the blocker.
- Offensive sizing floors/tiers (e.g., river_value_raise tier-floor) remain permitted when wired LIVE and validated ≥100g H2H; do not reintroduce stacked `value_sizing_delta` boosts at `choose_raise` without matchup evidence of underbetting.
- Do not carry kept-but-inert constants: if a floor sits below existing base ratios, RAISE the floor so it binds or REMOVE the dead bound — after first confirming the constant still exists in current source.

## GENERAL
- Validation thresholds: <30g H2H is directional noise; ≥30g paired net-chips required BEFORE re-adding any exhausted-tagged feature; ≥100g H2H required to declare any new structural path / constant change / matchup target successful or expandable.
- Treat commit messages and Master plans as advisory; trust the git diff — verify claimed features against actual diff before commit (v113 probe_mode bullet was entirely unrealized yet committed).
- Select crossover parents by H2H win-rate and diversity, not raw Glicko; verify the crossover tool actually executed rather than falling back to master+worker copy. Verify `branch_from` considers current top-rated bots, not just the stagnation ancestor.
- Worker boundaries mandatory: Architect defines structural logic; Tuner may only adjust constants within that structure. Re-verify strategy.py line count each generation; core limit 2000, bundle refactors only when source nears cap.
- Attribution test: when isolating a donor trait into a base, pair candidate H2H vs opponents where donor>base by ≥3pp; if candidate doesn't recover ≥half the gap, the trait was not the edge source.
- Crossover-fallback chains often mask Master JSON collapse rather than design intent (6 consecutive observed). v91 fragments specifically have been mined twice in 3 gens (v110 _spr_commitment_gate, v112 value-tier floor) — further v91 re-imports require fresh traceable evidence.
- Orphan dead-code trap: worker removes import+call site but def lives in a non-target file. Expand target_files OR add a post-commit cleanup gate that auto-strips orphaned defs.
- Before re-proposing a feature, `git log --grep <feature>` and grep current source — repeated re-derivation of already-shipped buckets is a no-op.
- Critic dual-review drift observed: when the same diff scores divergently, treat the LOWER score as authoritative when the assessment cites missing evidence; verify high-swing replay claims (e.g., -20000 tail losses) against real replay deltas before building a gen.

## RECENT_LESSONS
- **v114**: Critic evidence: H2H weaknesses: v103 vs claude_v89: 0.464 (140g) — closest to a 'weakness', uncited, v103 vs claude_v109: 0.475 (40g), v103 vs claude_v106: 0.480 (100g), v103 vs claude_v104: 0.482 (110g), v103 vs claude_v102: 0.486 (140g); Experience pool refs: [POSSIBLY EXHAUSTED] v113: 'Repeated import of v89 broadway_suited bucket across v107/v108/v113 without wiring into ≥3 decision points has produced no measurable lift — next reuse must extend to ≥3 decision points or be abandoned' — v114 wires it into only 2 points (sb_vs_iso + bb_vs_raise), violating the requirement, [POSSIBLY EXHAUSTED CONFIRMED] v107-v111: 'next gen MUST pivot OFFENSE — river_value_raise tier-floor (≥0.50x) scaled by opponent nutted_risk' — v114 instead does defensive barrel-fold tuning, the opposite direction, [POSSIBLY EXHAUSTED] 'DEFENSIVE sizing constant tuning (caps/floors/defensive call thresholds…) has no sustained gain; constants-only inside an Architect-defined structural hypothesis with per-constant H2H backing' — barrel-cap 0.06→0.08 is exactly this with no per-constant H2H backing; Diff refs: state.py:87-90 — adds broadway_suited classification (suited & high≥11 & low≥10), strategy.py:369 — broadway_suited added to `implied` set in `_sb_open_bucket_action`, strategy.py:403 — broadway_suited added to `bluff_raise` set in `_bb_vs_raise_bucket_action`
- **v113**: Repeated import of v89 broadway_suited bucket across v107/v108/v113 without wiring into ≥3 decision points (sb_vs_iso, bb_vs_raise 3-bet, postflop classification) has produced no measurable lift — next reuse must extend to ≥3 decision points or be abandoned. [POSSIBLY EXHAUSTED]
- **v113**: Target v106 sub-50% matchup directly with opponent-line-conditioned barrel-fold (fire only when opp barrel_freq≥0.55 AND river check-down range polarised) rather than further global cap tuning.
- **v112**: v91 value-tier sizing floor (0.58/0.62/0.68) ported as OFFENSIVE primitive into v111 base — verified LIVE (thin_cap=None path), fills a genuine gap (v111 had no _value_floor block). Attribution of v91's H2H gaps (v103 0.40, v104 0.43) to the sizing floor remains UNRESOLVED — needs ≥100g H2H vs v91/v103/v104.
- **v107-v111 EXHAUSTED CONFIRMED** (5 consecutive defensive gens, critic regressed 7.0→4.0): next gen MUST pivot OFFENSE — river_value_raise tier-floor (≥0.50x) scaled by opponent nutted_risk, targeting weakest H2H 0.40-0.55. Defensive late-street guard plans must FIRST cite ≥3 specific replay hands from v109+ where made_strength≥0.50 folded vs 2:1+ pot-odds, OR auto-abandon. [POSSIBLY EXHAUSTED]

