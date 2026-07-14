# v093_national_v17_native_context_rulebase_h64_call_tcp

Rule-baseline supervised native TCP value probe forked from
`v092_national_v17_native_context_call_only_early_tcp`.

Change:

- Keeps the native national TCP entrypoint and call-only runtime intervention.
- Replaces the d77 h32 head with
  `native_context_rulebase_h64_seed1721.json`, trained from the d128
  leader-enriched native-context dataset.
- The d128 rulebase dataset keeps `raise_pot=0` in the supervised target mask,
  so the runtime margin compares call against a trained rule baseline instead
  of an unconstrained output.
- Uses a stricter value gate: call value at least `0.35` and at least `0.35`
  above the predicted raise-pot value.
- Requires at least one observed opponent action but removes v092's early
  opponent-action cap; the model must learn to avoid bad v1/v2 buckets.
- Neural fold, neural raises, and all-ins remain disabled.

Interpretation:

- Offline scan on the d128 rulebase dataset selected 13 call interventions at
  threshold `0.35`, with 13 positive and 0 negative call targets.
- This is still a runtime probe. Promotion requires paired native TCP evidence
  against `.evolution_pok` rating leaders and the older top-rule set.
