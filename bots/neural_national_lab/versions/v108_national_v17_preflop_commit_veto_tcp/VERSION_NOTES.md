# v108_national_v17_preflop_commit_veto_tcp

Native national TCP neural-value risk-veto probe derived from v107.

Change:

- Keeps v107's opponent-profile guarded preflop proposals.
- Keeps v107's turn/river large-commit value veto behavior.
- Extends the large-commit value veto to preflop only for already huge pots:
  - stage is preflop,
  - `to_call >= 10000`,
  - `pot >= 18000`,
  - rule label is `call` or `allin`,
  - learned rule value is at most `-0.18`,
  - best learned label is at least `0.50` above the rule label.
- The veto implementation now supports stage-specific thresholds so preflop can
  be guarded more tightly than turn/river.

Trace rationale:

- v107's larger holdout seed block `2026073900` regressed to `-37881`, mainly
  because v3 scored `-34616`.
- Bot-seed-aligned trace of seed `2026073908` reproduced the main v3 failure:
  v3 `-27427`, with two `-20000` hands.
- Both failures were preflop large-commit continue/all-in decisions. v107's
  veto did not apply because it was limited to turn/river.
- The same multi-action value head already marked these rule labels as poor:
  one all-in had rule value around `-0.199` and best-label margin `0.53`; one
  call had rule value around `-0.262` and best-label margin `0.58`.

Status:

- This is a narrow risk-control ablation, not a new trained model.
- It should be judged by native TCP paired evaluation against the same strong
  rule pool and compared directly against v107 on seed block `2026073900`.
