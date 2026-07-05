# Neural National v108-v109 Report

Date: 2026-07-06

## Goal

Continue the native national TCP neural route after v107. The target is still
comprehensive domination of rule bots; this report records progress and failed
ablations toward that target.

All results below use native national TCP bots. No national adapter path was
used.

## Starting Point

v107 was positive on two small 3-match seed blocks, but a larger 10-match
holdout exposed that the result was not stable:

`native_tcp_paired_v107_vs_current_top8_plus_v7_h70_m10_seed2026073900.json`

- 90 paired matches / 12600 hands.
- Total: `-37881`, mean/hand `-3.006`, W-L-D `3-18-69`.
- v2: `-3016`, W-L-D `2-8-0`.
- v3: `-34616`, W-L-D `1-9-0`.
- 0 illegal actions, 0 timeouts, 0 adapter actions.

Bot-seed-aligned trace of v107 on seed `2026073908` reproduced the largest v3
failure. The bad decisions were preflop large-commit continue/all-in spots.
v107's large-commit value veto was limited to turn/river, so it could not block
these preflop failures.

## v108

Path:

`bots/neural_national_lab/versions/v108_national_v17_preflop_commit_veto_tcp`

Change:

- Derived from v107.
- Keeps v107's opponent-profile guarded preflop proposals.
- Keeps v107's turn/river large-commit value veto.
- Extends large-commit value veto to preflop with tighter stage-specific
  thresholds:
  - `to_call >= 10000`,
  - `pot >= 18000`,
  - rule label is `call` or `allin`,
  - learned rule value is at most `-0.18`,
  - best learned label is at least `0.50` above the rule label.

Direct regression seed block:

`native_tcp_paired_v108_vs_current_top8_plus_v7_h70_m10_seed2026073900.json`

- 90 paired matches / 12600 hands.
- Total: `+918835`, mean/hand `+72.923`, W-L-D `73-1-16`.
- Every opponent was positive.
- v2: `+119089`, W-L-D `10-0-0`.
- v3: `+118064`, W-L-D `10-0-0`.
- 0 illegal actions, 0 timeouts, 0 adapter actions.

Independent holdout:

`native_tcp_paired_v108_vs_current_top8_plus_v7_h70_m10_seed2026074000.json`

- 90 paired matches / 12600 hands.
- Total: `-31443`, mean/hand `-2.495`, W-L-D `36-12-42`.
- Non-v2/v3 opponents were all positive.
- v2: `-183870`, W-L-D `4-6-0`.
- v3: `-139305`, W-L-D `4-6-0`.
- 0 illegal actions, 0 timeouts, 0 adapter actions.

Combined across the two 10-match blocks:

- 180 paired matches / 25200 hands.
- Total: `+887392`, mean/hand `+35.214`.
- Overall W-L-D: `109-13-58`.
- Non-v2/v3 opponents were strongly positive.
- v2 remained negative overall: `-64781`.
- v3 remained negative overall: `-21241`.

Conclusion: v108 is a major improvement over v107 and shows strong exploit
potential, but it is still not comprehensive domination because v2/v3 are not
stable across seed blocks.

## v109

Path:

`bots/neural_national_lab/versions/v109_national_v17_commit_veto_only_tcp`

Change:

- Derived from v108.
- Keeps the stage-specific large-commit value veto.
- Disables ordinary neural call/raise advice and multi-action proposals.

Reason:

Trace of v108 on v2/v3 seed block `2026074000` showed ordinary preflop
`fold/call -> raise` proposals were a large neural-change loss source. v109
tested whether the robust component was only the large-commit veto.

Result:

`native_tcp_paired_v109_vs_v2v3_h70_m10_seed2026074000.json`

- 20 paired matches / 2800 hands.
- Total: `-189349`, mean/hand `-67.625`, W-L-D `11-9-0`.
- v2: `-80647`, W-L-D `5-5-0`.
- v3: `-108702`, W-L-D `6-4-0`.
- 0 illegal actions, 0 timeouts, 0 adapter actions.

Conclusion: veto-only is not enough. It reduces some v108 seed4000 damage, but
still loses clearly to v2/v3.

## Current Diagnosis

- v108's preflop large-commit veto is useful and should not be discarded.
- Broad ordinary preflop proposals are unstable: on seed4000 they are a major
  loss source against v2/v3, while the overall bot can still be very strong on
  other seed blocks.
- The next route should train or gate v2/v3 preflop proposals using both
  successful seed3900 and failed seed4000 traces, rather than disabling all
  proposals or relying only on a simple raise-rate threshold.
- Completion remains unproven: the best current artifact is not yet uniformly
  positive against v2/v3 across large seed blocks.
