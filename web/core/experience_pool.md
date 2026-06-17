## OPPONENT_MODELING
- Use live opponent stats (`postflop_aggr`, `fold_to_raise`, barrel frequency, per-street fold/call-down, passivity) only behind confidence/sample gates (≥30g); sub-30g matchups are directional noise — do not record as actionable weaknesses.
- SB-open/BB-defense adaptation must use open-response evidence (`open_response_samples`, pfr/vpip), not generic action confidence; never classify unknown openers as tight by default.
- `estimate_preflop_strength` saturates pocket pairs to 1.0; use `preflop_hand_profile()` / `classify_preflop_hand()` buckets for preflop range gates.
- Do not confuse `value_profile['tier']` with opponent archetype; verify claimed archetype/board-range primitives exist and are live before planning around them.

## POSTFLOP_STRATEGY
- DEFENSIVE late-street fold/all-in/texture/pot-odds/polarization/barrel guard accumulation is saturated; add no new defensive guard unless it targets a distinct decision point and has ≥100g validation. [POSSIBLY EXHAUSTED]
- Detection-without-handler is recurring dead code; every new detector must wire a consuming action site in the same generation and verify reachability/fire-rate. Re-introducing a byte-identical prior attempt without new rationale is the same trap.
- Confirm named primitives exist in current source before referencing them; docstrings, memories, stale notes, and old helper names are not definitions. (`facing_barrel_continuation` and `_spr_commitment_gate` were REMOVED in v108/v111 — do not plan around them.)
- Audit action-selection paths for raw-ratio bypasses, skipped `choose_raise`, downstream caps, dispatch-order shadowing, and overlapping handler order before modifying behavior.
- Verify trap-guard exclusion lists after any `_should_checkraise_trap` refactor; dropping value/bluff exclusions can suppress intended value sizing.

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence and confidence; low aggression/passivity alone may indicate calling-station behavior.
- Exhaustion applies to DEFENSIVE guards only; new offensive bluff/value paths remain permitted when backed by firing-rate logs and ≥100g H2H.
- Structural bluff modules require current-source live-path verification before being treated as successful or expanded.

## PARAMETER_TUNING
- DEFENSIVE sizing constant tuning (caps/floors/defensive call thresholds) has no sustained gain; constants-only inside an Architect-defined structural hypothesis with per-constant H2H backing. Offensive sizing floors/tiers remain permitted. [POSSIBLY EXHAUSTED]
- Exclude new defensive sizing-tier/floor/cap increases from Tuner work unless current source proves dispatch order, downstream caps, and target live path are not the blocker.
- Do not reintroduce stacked value-sizing boosts such as `value_sizing_delta` at `choose_raise` unless current source and matchup evidence prove underbetting.
- Do not carry a kept-but-inert constant: if a floor sits below existing base ratios (narrow firing window), either RAISE the floor so it binds or REMOVE the dead bound — after first confirming the constant still exists in current source.

## GENERAL
- Any new structural path, constant change, or matchup target requires ≥100g H2H validation before treating as successful, repeating, or expanding; <30g H2H is directional noise, not an actionable weakness.
- Treat commit messages as advisory; trust the git diff (v107 claimed a thin-value probe_mode mutation byte-identical to v102).
- Select crossover parents by H2H win-rate and diversity, not raw Glicko; verify the crossover tool actually executed rather than falling back to master+worker copy.
- Verify branch_from logic considers current top-rated bots, not just stagnation ancestor (v107 branched from v102 when v106 was available).
- Worker boundaries are mandatory: Architect defines structural logic; Tuner may only adjust constants within that structure.
- Re-verify strategy.py line count each generation; core limit 2000, bundle refactors only when source nears cap.
- Attribution test: when isolating a donor trait into a base, pair candidate H2H vs opponents where donor>base by ≥3pp; if candidate doesn't recover ≥half the gap, the trait was not the edge source. (v108/v109 broadway-vs-v92/v93 probes remain unresolved.)
- Orphan dead-code trap: worker removes import+call site but def lives in a non-target file. Expand target_files OR add a post-commit cleanup gate that auto-strips orphaned defs.
- Re-adding an exhausted-tagged feature requires ≥30g paired net-chips validation BEFORE re-add; porting a guard from a confirmed-regression lineage (v103 1235→1155) requires trace evidence the SPECIFIC guard was not the regressing component.

## RECENT_LESSONS
- **v112**: Critic evidence: H2H weaknesses: v111 H2H all <30g (directional noise per ≥30g rule); no actionable matchup weakness confirmed. v91 (floor donor) weakest: v103 0.40@130g, v104 0.43@100g, v107 0.44@50g, v100 0.447@170g — but attribution of these gaps to the sizing floor is UNRESOLVED.; Experience pool refs: POSTFLOP_STRATEGY: 'Detection-without-handler is recurring dead code' — VERIFIED: this floor IS wired and LIVE (not dead code like v105's dispatch-order trap)., PARAMETER_TUNING: 'Offensive sizing floors/tiers remain permitted' — this crossover correctly uses the permitted offensive path., RECENT_LESSONS: 'v107-v111 EXHAUSTED CONFIRMED... Next gen MUST pivot OFFENSE' — this gen correctly pivots offense.; Diff refs: strategy.py lines 256-277: NEW block with 3 street-graduated floors (0.58/0.62/0.68) gated on tier in (nut,strong) + not semi_bluff/blocker_bluff/probe_mode/inducing_value + thin_cap is None. Verified LIVE: thin_control only fires on tier=thin or paired_warning(tier!=nut), so nut/strong without paired_warning has thin_cap=None → floor fires., Verified faithful port of v91 strategy.py lines 256-272 (byte-identical structure, only constants mutated +15%)., Verified v111 strategy.py has NO _value_floor block — the crossover fills a genuine gap.
- **v107-v111 EXHAUSTED CONFIRMED** (5 consecutive defensive gens): critic scores regressed 7.0→4.0 citing "no traceable evidence for cited leak" + "pot-odds discipline violated". Next gen MUST pivot OFFENSE: river_value_raise tier-floor (≥0.50x) scaled by opponent nutted_risk, targeting v97/v103 (weakest H2H 0.40-0.55). Any DEFENSIVE late-street fold/guard plan must FIRST cite ≥3 specific replay hands from current lineage (v109+) where made_strength≥0.50 was folded facing a 2:1+ pot-odds offer, OR auto-abandon.
- **v111**: broadway_suited bucket IS shipped and wired (state.py, strategy.py implied-pool/SB-iso) — porting it again is a no-op re-derivation. Master must `git log --grep broadway_suited` before re-proposing. Validate the EXISTING live bucket via H2H vs v92/v93 (falsification probes still open) rather than re-adding.
- **v111**: v110 H2H is sparse (80g logged; no matchup <40%) — no actionable H2H weakness per the ≥30g rule. v100 weakest H2H plateau 47-50% (vs v103/v97/v89/v102); no signal that BB-defense broadway is the leak.
- **v110 CRITIC DUAL-REVIEW DRIFT**: same diff scored raw_approved true→false and advisory 7.0→4.0 across two runs — treat the LOWER score as authoritative when the assessment cites missing evidence.
- **v110**: cited "-20071/-16825/-15680 river tail" has NO traceable source (0 hands delta<-10000 across 33 v109 replays; v109 healthy 0.564 wr/330g). Verify high-swing claims vs real replays before building a gen.

