# v090_national_v17_native_context_balanced_value_tcp

Balanced-threshold fork of
`v088_national_v17_native_context_strict_value_tcp`.

Change:

- Keeps v088's native TCP entrypoint, opponent-profile features, h32 value
  head, and rule-raise-pot-only fold/call proposal surface.
- Raises the value proposal threshold and margin from v088's `0.30` to `0.40`.

Interpretation:

- v088 was positive versus v082 on the top5 paired control but had several
  small negative rows and a `national_v20` regression.
- v089 at `0.55` was too conservative and lost the positive signal.
- v090 tests the middle threshold before committing to the v088 gate.
