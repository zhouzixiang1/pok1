# v088_national_v17_native_context_strict_value_tcp

Strict opponent-conditioned native TCP value probe forked from
`v087_national_v17_native_context_early_veto_tcp`.

Change:

- Keeps the native national TCP entrypoint and v085 opponent-profile request
  data.
- Replaces the h16 profile model with
  `native_context_value_h32_seed1512.json`, trained from the expanded
  top-rule native TCP fold/call counterfactual set:
  `native_tcp_value_v085_profile_fc_top5_d77_context_clip4000.jsonl`.
- Keeps neural intervention preflop-only and rule-raise-pot-only.
- Uses strict value thresholds: proposal value at least `0.30` and at least
  `0.30` above the rule raise-pot value.
- Allows only value-head `fold` and `call` proposals; neural raises and all-ins
  remain disabled.
- Makes the call runtime gate configurable so this version can test
  raise-pot-to-call downsizing. Earlier versions still keep their own local
  behavior.

Interpretation:

- v088 is a runtime validation probe for the larger native context value
  dataset, not a promotion candidate.
- It specifically tests whether high-confidence value-head overrides can avoid
  v086's broad tail losses while still producing measurable behavior changes.
