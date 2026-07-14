# Route B M5: public-range counterfactual leaf values

This sibling package extends the immutable Route B M4 release without changing
any tracked byte below `cfr_neural_search/`.  M5 owns its label data, split,
model, and inference receipts.  The M4 dependency is content-bound by
`contracts/m4_dependency_c44dd1eb.json` and is revalidated with the original
M4 verifier.

M5 does not use the M3 `GameState -> (u, -u)` rollout leaf.  Its neural
consumer accepts one public HUNL state and both players' complete reach ranges,
then returns masked counterfactual-value vectors with shape `[2, 1326]`.
Online TCP search and any strength, official-EXE, or promotion claim remain
outside M5.

The current prerequisite gate contains no generated labels or trained model.
It establishes:

- a Common-replayed, private-card-free public HUNL state and two complete
  physical 1,326-combination reach ranges;
- exact Kuhn/Leduc checks, fold/showdown/river-decision micro-oracles, and an
  exact 44-river turn all-in runout oracle;
- range-weighted raw/deployed zero-sum diagnostics and explicit fallback
  telemetry; and
- a sealed exact-provider receipt with a system-owned source closure and
  recursive runtime helper/code binding.

The runtime binding is an integrity check for provider/dependency mutation
while the contract-held pinned manifest builder and consumer entrypoints remain
intact.  It is not a Python sandbox and does not claim to survive arbitrary
rewriting of the interpreter, the contract class, or the consumer method.
Future formal model execution must establish its own isolated process/artifact
boundary before training or online use can be authorized.

Run `python -m bots.research_native_lab.cfr_neural_search_m5.tools.verify_oracle_gate`
from the repository root.  A passing payload intentionally reports
`passed_no_labels_no_training`; label generation, training, and online TCP use
remain forbidden by `contracts/oracle_gate_v1.json` until the next authority
gate is merged.
