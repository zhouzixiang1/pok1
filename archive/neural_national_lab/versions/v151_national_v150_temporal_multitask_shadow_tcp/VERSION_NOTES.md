# v151 - temporal cross-hand multi-task shadow

Parent: `v150_national_v149_multitask_p1_shadow_tcp`.

This diagnostic version adds a real sequence of completed-hand public opponent
summaries to the native TCP lifecycle. Each decision receives at most the last
32 strictly prior hands. A second GRU encodes the sequence alongside the
existing 20-dimensional aggregate opponent features and current-hand GRU.

The bundled 27,186-parameter `opp_multitask_gru_v2` weight is a pipeline smoke
artifact trained on relabeled copies of the first v152 validation probe. It is
not independent data and provides no strength evidence. It runs shadow-only;
the inherited active policy remains unchanged. Its purpose is to verify:

- server-event and native-bot sequence feature parity;
- no future-hand leakage and bounded 32-hand retention;
- CUDA training and deterministic JSON export;
- stdlib-only v2 value and response inference in the native bot.

Formal models must be retrained from the opponent-disjoint
`matchscope_v152_temporal` dataset before any active override or strength claim.
