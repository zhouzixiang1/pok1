# v089_national_v17_native_context_high_precision_tcp

High-precision threshold fork of
`v088_national_v17_native_context_strict_value_tcp`.

Change:

- Keeps the same native TCP entrypoint, opponent-profile features, and
  `native_context_value_h32_seed1512.json` value head as v088.
- Raises the multi-action value proposal threshold and margin from `0.30` to
  `0.55`.
- Still allows only preflop rule-raise-pot to fold/call proposals; neural
  raises and all-ins remain disabled.

Interpretation:

- v088 improved the top5 paired control by `+43221` chips over 3500 hands, but
  retained negative rows against `national_v20` and weak median behavior.
- v089 tests whether a higher threshold can keep the large positive outliers
  while cutting lower-confidence negative interventions.
