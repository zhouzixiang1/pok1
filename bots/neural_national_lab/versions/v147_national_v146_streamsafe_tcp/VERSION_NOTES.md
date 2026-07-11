# v147 - v146 strategy with stream-safe numeric framing

Parent: `v146_national_v140_gru_crosshand_cls_tcp`.

This version intentionally makes no strategy, model, weight, or neural-gate
change. It fixes the native TCP stream parser so numeric server messages are
not committed at an arbitrary `recv()` boundary. For example, the fragments
`raise 2` + `00call` now dispatch as `raise 200`, `call`; the same protection
applies to negative `earnChips` values.

The parser holds a trailing numeric token for a 50 ms quiet window. It appends
any immediately available bytes before parsing, while still flushing a
complete standalone numeric message quickly enough for the 60 second action
window.

v146 remains the model-strength artifact. v147 is the protocol-safe candidate
for subsequent evaluation and official EXE certification. The 2026-07-10 live
pool re-evaluation found that v146 improved the aggregate over v140 but
regressed against `national_v141`; this version does not claim to resolve that
strategy nemesis.
