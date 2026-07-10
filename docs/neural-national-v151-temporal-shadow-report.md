# v151 Temporal Opponent-Model Shadow Report

Date: 2026-07-10

## Status

`v151_national_v150_temporal_multitask_shadow_tcp` is a diagnostic native TCP
bot, not a strength candidate. It inherits the active rule policy from v150 and
runs the new temporal opponent model only in shadow mode. The bundled model was
trained from duplicated smoke rows and must not be used as performance evidence.

## Implemented Contract

- Each decision carries up to 32 strictly prior completed-hand summaries.
- Every summary contains 16 public features covering opponent action mix,
  preflop/postflop aggression, public street reach, showdown, and settlement.
- The probe and native bot use the same `public_opponent_hand_v1` feature
  implementation. No opponent private cards enter the sequence.
- The multi-task trainer supports GRU, GRU with soft mixture-of-experts,
  Deep Sets, and a small exportable Transformer over the same temporal input.
- Value heads predict hand, tail, and full-match deltas. The response head uses
  a separately encoded public state with hero private-card features masked.
- JSON models run in a stdlib-only runtime. Torch and CUDA are training-only.
- Offline policy selection resamples whole `(opponent, deck seed, bot seed)`
  clusters and prioritizes the opponent-stratified cluster CI lower bound.

## Evidence

The current implementation passed:

```text
python -m pytest bots/neural_national_lab/tests -q
31 passed

python -m pytest web/tests/test_national_native_workflow.py -q
34 passed

python -m pytest sever/tests/test_national_platform_alignment.py -q
8 passed
```

Torch and stdlib outputs agree for aggregate-only, GRU, Deep Sets, and
Transformer models, including empty, 32-hand, and over-length temporal inputs.
A CUDA smoke sweep trained and exported all four temporal encoders and selected
an architecture using validation data only. The smoke dataset is intentionally
too small and duplicated to support any architecture or strength conclusion.

The formal scaling grid includes an xlarge range of about 4.2-4.9 million
parameters. Local stdlib benchmarks measured 296-977 ms for a maximum-history
value-plus-response inference across these xlarge encoders. Runtime is recorded
per seed, summed for the complete seed ensemble, and enforced as a
model-selection gate.

`check_native_contract` returns no errors for v151. Official EXE acceptance and
paired native strength evaluation are deliberately deferred until an active
model is trained on the formal opponent-disjoint dataset.

## Dataset Snapshot

After two complete v152 collection passes, the strict audit reported:

- value rows: 96 train, 24 validation, 24 held-out;
- behavior rows: 466 train, 154 validation, 43 held-out;
- train opponents: v119, v120, v121, v122, v135, v141, v63, v72;
- validation opponents: v98 and v142;
- held-out opponents: v57 and v66;
- 807 temporal rows with valid strictly-prior sequence lengths;
- all six counterfactual action classes represented;
- zero opponent overlap and zero protocol-invalid probes.

This is an early snapshot of the still-running 160-pass collection. Formal
training must wait for substantially more match clusters and freeze the final
files with hashes.

## Remaining Gates

1. Complete and freeze the v152 dataset.
2. Run multi-seed scaling on real data and select without held-out leakage.
3. Create a new active native TCP version with an uncertainty-driven policy.
4. Run neural and encoder/head ablations against the live classic pool.
5. Require positive ordinary and opponent-stratified paired CIs, no nemesis,
   zero protocol errors, and at least +5 chips per hand before official EXE
   certification or a success claim.
