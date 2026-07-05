# v091_national_v17_native_context_call_only_tcp

Call-only opponent-conditioned native TCP value probe forked from
`v088_national_v17_native_context_strict_value_tcp`.

Change:

- Keeps the native national TCP entrypoint and the same h32 native-context value
  model, `native_context_value_h32_seed1512.json`.
- Keeps neural intervention preflop-only and rule-raise-pot-only.
- Disables neural fold entirely after v088 showed severe generalized losses on
  `.evolution_pok` conservative-Glicko leaders.
- Allows only value-head `call` proposals, so the remaining experiment is a
  narrow `raise_pot -> call` downgrade rather than a raise-to-fold veto.
- Keeps the v088 value thresholds: proposal value at least `0.30` and at least
  `0.30` above the rule raise-pot value.

Interpretation:

- v091 is a damage-control diagnostic, not a promotion candidate.
- It tests whether v088's negative generalization came mainly from
  `raise_pot -> fold` overrides. A useful result must preserve v082 strength on
  `.evolution_pok` leaders and still show positive same-seed lift somewhere.
