# v094_national_v17_native_context_rulebase_h64_call_t075_tcp

High-threshold fork of
`v093_national_v17_native_context_rulebase_h64_call_tcp`.

Change:

- Keeps the same native TCP h64 rulebase value head and call-only runtime path.
- Raises both call value and call-vs-raise-pot margin thresholds from `0.35`
  to `0.75`.

Interpretation:

- v093 remained absolute positive on the current `.evolution_pok` Glicko top5
  seed set, but lost `-59820` chips versus v082 and had 22 negative diff rows.
- v094 tests whether those losses are from marginal value-head calls that can
  be filtered by a much stricter threshold.
