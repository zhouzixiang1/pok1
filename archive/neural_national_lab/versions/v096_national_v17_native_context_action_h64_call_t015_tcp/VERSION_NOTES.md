# v096_national_v17_native_context_action_h64_call_t015_tcp

Action-context native TCP value probe forked from v095.

Change:

- Keeps native national TCP and the call-only neural intervention path.
- Uses `native_context_action_h64_seed1731.json`, trained on the d128
  leader-enriched dataset with state, opponent profile, and rule-action context
  features.
- The feature vector is 78-dimensional: the previous 60-dimensional
  native-context state/profile vector plus 18 rule-action context features.
- Lowers the runtime threshold/margin from v095's `0.40` to `0.15`; offline
  scan on the d128 action-context dataset selected 12 call interventions, all
  positive targets.
- Neural fold, raises, and all-ins remain disabled.

Interpretation:

- v094 showed that scalar thresholding a 60-feature model still lost to v082.
- v095 showed a clean positive diff against v082 on the current Glicko top-5
  protocol bot pool. v096 tests whether the lower offline-safe threshold adds
  more of that signal without introducing negative paired matches.

Result:

- The lower threshold did not hold up online. On the same current
  `.evolution_pok` conservative-Glicko top5 and seed range used for v095,
  `native_tcp_paired_v096_vs_evolution_glicko_top5_h70_m5_seed2026071900.json`
  scored `+97604` chips over 3500 hands.
- The paired diff versus v082 was `-3767`, and the paired diff versus v095 was
  `-14007`.
- The failure came from newly opened negative windows against `national_v7` and
  `national_v11`, including one `-29880` paired row for each. Compliance still
  stayed clean: 0 illegal actions, 0 timeouts, and 0 adapter actions.
- v096 is a recorded boundary experiment. Keep v095's `0.40` threshold unless
  new action-context data specifically explains and gates the v7/v11 failures.
