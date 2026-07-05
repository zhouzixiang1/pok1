# v086_national_v17_native_context_value_fc_tcp

Opponent-conditioned native TCP value probe forked from
`v084_national_v17_native_state_value_call_probe_tcp`, with the protocol
entrypoint from `v085_national_v17_profile_trace_tcp`.

Change:

- Keeps the national TCP entrypoint native; no national adapter is used.
- Adds the v085 match-level opponent action profile into each strategy request.
- Loads `native_context_value_h16_seed1301.json`, a 60-dimensional tiny MLP
  trained on native TCP counterfactual rows:
  state features plus 12 opponent-profile features.
- Limits neural intervention to preflop value proposals after at least two
  observed opponent actions.
- Keeps the conservative fold-to-call probe, but also allows a narrow
  preflop raise-to-fold veto when the context value head strongly prefers
  folding over the rule raise.
- Does not allow neural raises or all-ins.

Interpretation:

- v086 is a runtime validation probe for opponent-conditioned neural value
  heads, not a completed strength bot.
- Positive evidence would be paired improvement against rule bots without the
  broad negative drift seen in the state-only v084 probe.
