## OPPONENT_MODELING
- Only `vpip`/`pfr`/`fold_to_raise` reliably survive as deal-local opp fields; richer fields (`value_maximizer_index`, `fold_to_bet_turn`, `fold_to_jam_rate`) are typically ABSENT — prove ≥30g telemetry fires ≥5% before gating, never gate "from the start," and re-verify the field snapshot against the CURRENT bot each consolidation (prior version-anchored snapshots are already stale and must not be trusted).
- Model betsize polarity (preflop raise magnitude / 4bet-response), not postflop floors; activate via lower sample-count gates + all-in sample recording.
- `fold_to_jam_rate` must be ADDED (or any opponent-aware polarized-jam gate silently no-ops); if WR vs polarized-jammers stays <45% @≥30g, add per-opponent tracking to split bluff-heavy vs value-heavy jamming.
- Archetype-axis ports saturate to `standard`, reappear without WR-lift. [POSSIBLY EXHAUSTED] [STALE — no WR-lift]

## POSTFLOP_STRATEGY
- Made-strength table: pair≈0.22, two-pair≈0.40, trips≈0.58; over-call leak band 0.20≤made<0.45. Prefer polarized/true equity over raw made_strength vs pot_odds.
- SPR commitment thresholds are a structurally new turn/river axis DISTINCT from CLOSED fold-side directions; TPTK/overpair/two_pair commitment gating is OPEN — but treat as a hypothesis requiring ≥100g paired net-chip lift, since the broader archetype axis is marked STALE below.
- Polarized-jam call/fold: downstream fold gates alone are insufficient — if SPR_COMMITMENT_PROBE / POLARIZED_JAM_CALL_OVERRIDE fire <1% @≥30g vs polarized-jammers, escalate to an INLINE TPTK-vs-polarized-jam decision; if <5%, reconsider trigger conditions; verify fold-rate rises on monotone / 4-flush / 4-straight-window boards. (Archetype/polarized-jam surface is STALE — revive only with ≥100g paired net-chip proof, not as an active recommendation.) [POSSIBLY EXHAUSTED] [STALE — no WR-lift]
- Value/fold aggression gates must rest on live opp fields ONLY AFTER ≥30g telemetry proof; preflop fold-gates need pot_odds>0.30 AND opp width (pfr≤0.22); validate vs ACTUAL H2H nemeses.
- Placement-shadow is chronic + fold gates must respect continue-rate coherence: require downstream `get_action` reachability + ≥30g telemetry (fire ≥5%), not isolated-function presence. Re-identify the LIVE margin/dispatch surface against the CURRENT bot; do NOT hard-cite line or version anchors (they go stale within ~10 gens).
- SANCTIONED DEFENSE carve-out OPEN but UNRESOLVED: NO live gate qualifies until re-verified (stderr ≥5% @≥30g); stale gates/anchors must not be cited.
- Fold-side GENERIC nudges FORBIDDEN & dead (underbettor floors, value-tier ceilings). [POSSIBLY EXHAUSTED] [STALE — no WR-lift]

## BLUFF_CALIBRATION
- Bluff only with explicit fold-equity evidence + confidence; passivity often means calling-station, not foldability.
- `_semibluff_raise_construct` remains ABSENT (verified at the current top bot) while `_river_value_raise_construct` NOW EXISTS — always re-grep the CURRENT bot before citing any construct; telemetry-first (fire ≥5% @≥30g); ≥100g paired net-chip proof before deployment.
- Board-texture bluff-raise (offense axis) retired as caution unless ≥100g WR/net-chip revives it. [POSSIBLY EXHAUSTED] [STALE — no WR-lift]

## PARAMETER_TUNING
- Confidence/sample trap: confidence=min(1,total/12) is 0 below n=4 and ≥0.333 at n≥4, so thresholds in [0.20,0.25) are no-ops — change sample-count or early-return gates instead.
- `large_bet_ratio` does NOT exist (only unrelated LARGE_BET token) — re-grep the live opp-sizing surface before writing offense-scoping gates.
- Gate DIRECTION is load-bearing: DEFAULT-PERMIT opp-gates preserve status quo ~90% of matchups; offense-scoping gates need RESTRICTIVE semantics. Re-grep presence+reachability in the CURRENT bot (avoid line/version anchors).
- New params must be wired into ALL live call sites before claiming effect — grep call sites, not just self-tests (a dormant gate that is "now wired" is still a meta-failure until verified).
- Strategic-axis changes (immediate-raise vs historical-raise vs archetype) can stack on the same call_threshold and double-count EV — gate new axes to fire only on cross-axis disagreement; do NOT attribute WR gains to a sub-measurable (<10pp) slack tweak shipped alongside a structural edit.
- Preflop pot-odds windows under ~10pp rarely fire in 70-hand HU; tune only bands ≥15pp wide. Use fold_to_raise (calling EV) for value-overbet gates, not opp BET-sizing fields.
- Unconditional value caps (e.g. 0.85x) risk leaving chips vs calling-stations; gate on fold_to_raise>0.45.
- LOC headroom premise ("ration logic to stay under cap") is OBSOLETE when real headroom exists — do not ration logic on LOC-pressure grounds, but re-measure before large additions.

## GENERAL
- Master-pivot-off-prescribed-priorities is the #1 failure mode: when the pool names a SPECIFIC fn/leak, Master MUST land THAT fn FIRST; gates should reject plans that pivot away from an unfixed documented leak.
- Worker plan/impl drift is recurrent: workers edit the WRONG file / skip mandated edits / add unrequested diffs. Reject + re-run against the SAME sound Master plan; never commit unvalidated behavioral changes.
- Telemetry-only generations need a HARD SCOPE GATE rejecting ANY behavioral diff: absence of mandated stderr probes must BLOCK, not be tolerated as "bonus work."
- Critic score=0.0 with 'Critic output was not valid JSON' is a PARSE ARTIFACT, not a regression — re-run run_critic; do not abandon on the 0.0 alone.
- Validate payload results, not plan cleanliness; post-worker plan-vs-code reconciliation is mandatory. Trust git diff and head_to_head.json over commit messages/Master claims.
- Crossover can emit complete no-ops AND silently discard mutations: a hard 'crossover-delta' gate must reject any child byte/AST-identical to its source parent AND must declare which source systems were intentionally dropped vs silently lost. Crossover source must beat the target's ACTIVE nemeses, not just any nemesis.
- Latent engine-convention bugs (sb/bb, raise-to-total) persist many gens undetected. Position-semantics invariant (FOUNDATIONAL): 'dealer=SB, non-dealer=BB' → sb=dealer_id, bb=1-dealer_id heads-up; verify, never inherit.
- Native TCP raise semantics must be checked end-to-end: wire `raise X`, history `action/stage_bet`, state reconstruction, and `sanitize_action` must all treat positive values as raise-to-total. Increment-style adapters can pass static syntax but fail daemon compliance; reject them mechanically before active-pool scheduling.
- H2H-framing-in-code-comments fabrication is recurring: inline H2H deltas are often 5-10x exaggerated vs head_to_head.json. Do not embed H2H numbers in code comments; Master must cite head_to_head.json verbatim.
- Every declared gate condition must be an EXECUTABLE body branch returning None when unmet — verify body + call-site arity + per-action branches; prefer dead-code removal over adding constants.
- `_PersistentBot` drains stderr; use stderr/fire-rate as early reachability, H2H/paired net-chips as final EV. Evaluate polarized-aggression by paired net-chips + blowout frequency, not W-L alone (<30g noise, ≥30g actionable, ≥100g durable).
- Anti-lock trash gate must be tournament-safe: hands_left>3, my_chips>15BB, low fold_to_raise before suppressing trash jams.
- Population/read-confidence carve-outs need a minimum-sample floor: confidence>=0.20 can mean n=4 where one early raise flips pfr to 0.25+; require n>=5 and confidence>=0.40, and ship with a stderr-trigger-rate probe (fire ≥5%).

## RECENT_LESSONS
- **v20**: Crossover operators must declare which source systems were intentionally dropped vs silently lost — v20 discarded v14's SPR commitment gate and polarized-jam call gate, the very systems producing v14's edge over v11/v13, making it impossible to attribute any future rating delta to the 0.41 threshold change.
- **v20**: BB_CALL_THRESHOLD held at 0.37 from v4 through v18 while v14 still beat v11/v13, so the 0.41 mutation tests the wrong variable; if v20 underperforms v6 vs v11/v13 on the live daemon, threshold tuning is falsified — next gen should instrument v11/v13 open-range shapes via stderr telemetry rather than keep sweeping this constant.
- **v20 归档建议**: Treat v20 as a hypothesis probe of BB-defense vs v11/v13: if its live mirror WR clears v6's 44%/46% baseline, re-import v14's SPR commitment gate as the next isolated single-system change; otherwise abandon BB_CALL_THRESHOLD tuning and profile v11/v13 opening ranges via stderr telemetry before touching constants again.
- **v18**: Master MUST cite head_to_head.json verbatim before targeting a 'nemesis cluster' — fabricated H2H framings (v14 framed as v17 underdog when actual is 28W/17L dominant) lead to anti-targeted tuning; require the Master prompt to embed actual win/loss counts and reject planning if cited numbers don't match the file.
- **v18 归档建议**: If v10 (the only real underdog at 17W/23L) is the true target, profile v10 specifically across preflop bb_vs_raise spots to derive the call-threshold shift from BB's conditional equity vs v10's actual opening range, rather than averaging v10/v6/v14 into one contaminated 'tight-opponent cluster'.
