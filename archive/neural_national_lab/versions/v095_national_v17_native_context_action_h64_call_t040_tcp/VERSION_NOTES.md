# v095_national_v17_native_context_action_h64_call_t040_tcp

Action-context native TCP value probe forked from v094.

Change:

- Keeps native national TCP and the call-only neural intervention path.
- Uses `native_context_action_h64_seed1731.json`, trained on the d128
  leader-enriched dataset with state, opponent profile, and rule-action context
  features.
- The feature vector is 78-dimensional: the previous 60-dimensional
  native-context state/profile vector plus 18 rule-action context features.
- Lowers the runtime threshold/margin to `0.40`; offline scan on the d128
  action-context dataset selected 11 call interventions, all positive targets.
- Neural fold, raises, and all-ins remain disabled.

Interpretation:

- v094 showed that scalar thresholding a 60-feature model still lost to v082.
- v095 tests whether giving the value head the actual rule action size/context
  lets it avoid broad `raise_pot` buckets that should not be downgraded.

Result:

- On the current `.evolution_pok` conservative-Glicko top5 at test time
  (`national_v1`, `national_v7`, `national_v3`, `national_v2`, `national_v11`),
  the paired native TCP report
  `native_tcp_paired_v095_vs_evolution_glicko_top5_h70_m5_seed2026071900.json`
  scored `+111611` chips over 3500 hands.
- The same seed/control report for v082 scored `+101371`; the paired diff
  `native_tcp_diff_v095_minus_v082_evolution_glicko_top5_h70_m5_seed2026071900.json`
  is `+10240`, with 15 positive rows, 0 negative rows, and 10 zero rows.
- Compliance stayed clean: 0 illegal actions, 0 timeouts, and 0 adapter
  actions.
- This is a real positive neural increment over v082, but not a promotion-grade
  breakthrough because v095 still has losing rows and neutral rows against the
  top protocol bot pool.
