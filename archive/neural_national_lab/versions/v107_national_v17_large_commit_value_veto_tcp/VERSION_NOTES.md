# v107_national_v17_large_commit_value_veto_tcp

Native national TCP neural-value risk-veto probe derived from v106.

Change:

- Keeps v106's v2/v3 opponent-profile guard for preflop multi-action proposals:
  at least 8 observed opponent actions and opponent `raise_rate <= 0.30`.
- Adds `large_commit_veto_enabled`: on turn/river, if the rule action would
  continue or all-in against a very large call, ask the same multi-action value
  head whether the rule label is strongly negative.
- The veto returns `fold` only when:
  - stage is turn or river,
  - `to_call >= 8000`,
  - `pot >= 18000`,
  - rule label is `call` or `allin`,
  - learned rule value is at most `-0.20`,
  - best learned label is at least `0.45` above the rule label.

Trace rationale:

- v106 fixed broad non-v2/v3 pollution but still lost heavily to v2/v3.
- The v106 trace seed `2026073700` showed exactly four large turn continue/all-in
  decisions meeting this value condition, all against v2/v3 and all on hands
  that settled `-20000`.
- One-action force probes confirmed that folding these decision classes can
  recover large chunks of the v2/v3 deficit on the same native TCP seed.

Status:

- This is still an ablation, not comprehensive rule-bot domination.
- It is intentionally narrow so it can be judged by native TCP paired evaluation
  without disrupting v106's non-v2/v3 draw behavior.
