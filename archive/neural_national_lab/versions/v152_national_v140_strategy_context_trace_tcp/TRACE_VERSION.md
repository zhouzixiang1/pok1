# v152 Strategy-Context Trace Bot

Parent policy: `v140_national_v123_overlay_no_large_commit_veto_tcp`.

This is a native national TCP diagnostic version, not a strength candidate.
It preserves the v140 rule and neural-overlay action path and, only when
`POK_TRACE_DECISIONS=1`, adds `strategy_context` to each structured decision
trace. The payload contains:

- schema `v140_strategy_context_v1`;
- a bounded 66-dimensional value-head feature vector;
- compact raw rule-path inputs, excluding the full opponent range weights;
- a six-dimensional summary of the exact range weights used by v140.

The context is captured from the original strategy call after equity/range and
postflop profiles are computed. Encoding performs no simulation and consumes no
random numbers. Trace mode remains disabled by default on the official TCP
entrypoint.

Required promotion evidence for using this bot as a data source:

1. action parity with v140 under deterministic Python seeds;
2. native TCP paired parity on independent deck and bot-seed blocks;
3. every traced context declares dimension 66 and finite values in `[0, 1]`;
4. zero illegal actions, timeouts, wrappers, or adapters.
