# v097_national_v17_native_context_action_h64_call_t015_rr045_tcp

Action-context native TCP value probe forked from v096.

Change:

- Keeps native national TCP and the call-only neural intervention path.
- Uses the same `native_context_action_h64_seed1731.json` 78-feature value head
  as v095/v096.
- Keeps v096's lower runtime threshold/margin of `0.15`, but adds an opponent
  profile guard: both direct and proposal gates require observed opponent
  `raise_rate <= 0.45`.
- Neural fold, raises, and all-ins remain disabled.

Rationale:

- v096 failed despite an offline-clean threshold scan. Paired trace on the
  v7/v11 hard-negative seed showed the online failure came from the relaxed
  gate converting rule raises into calls/checks in high-opponent-raise-rate
  states: examples include `rule 871 -> 0` at `raise_rate=1.0`, `rule 221 -> 0`
  at `raise_rate=0.75`, and `rule 263 -> 0` at `raise_rate=0.5`.
- v097 tests whether blocking those high-raise-rate profile states preserves
  v096's extra low-threshold upside while avoiding the v7/v11 negative window.

Status:

- Native TCP paired hard-negative trace against `national_v7` seed
  `2026071902`, 10 hands: v095 scored `0`, v096 scored `-38880`, and v097
  scored `0`. v097 made no neural action changes in that trace, confirming the
  `raise_rate <= 0.45` guard blocks the v096 failure bucket.
- Current-pool paired evaluation against `national_v18`, `national_v3`,
  `national_v16`, `national_v14`, `national_v5`, `national_v7`, and
  `national_v11`, seed base `2026071900`, 5 matches per opponent, 70 hands per
  match, paired mode, 4900 total hands:
  - v097: `+41006` chips, `+8.369` chips/hand, 35/35 compliant matches.
  - v095 control: `+80486` chips, `+16.426` chips/hand.
  - v082 control: `+76977` chips, `+15.710` chips/hand.
  - Diffs: v097-v095 `-39480`, v097-v082 `-35971`, v095-v082 `+3509`.
  - All three reports recorded 0 candidate illegal actions, 0 candidate
    timeouts, and 0 candidate adapter actions.

Conclusion:

- v097 is a useful protocol-safe diagnostic boundary version, but not a
  strength candidate. The guard fixes the v096 high-raise-rate failure trace
  while sacrificing too much `national_v3` upside in the broader current-pool
  evaluation. Keep v095 as the best current native action-context neural
  baseline until larger current-leader action-value data is collected.
