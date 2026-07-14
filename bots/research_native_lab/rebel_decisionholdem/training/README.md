# Training boundary through M3

Deterministic tabular Kuhn and limit-Leduc LCFR are the only completed training
systems in this milestone. Their states are fully represented by the JSON
checkpoint formats `route-a-kuhn-lcfr-v1` and `route-a-leduc-lcfr-v1`.
Checkpoint/resume equality is bit-exact and covered by tests.

`tools/train_blueprint.py` can project the trained Leduc average policy into a
small, versioned national action/hand table and atomically package a native
prototype. This is a packaging and decision-influence test only: the postflop
abstraction has nine made-hand categories, the policy rows are an arithmetic
projection from Leduc, and no HUNL regrets were trained. It therefore does not
satisfy M4 blueprint-only completion.

No HUNL blueprint training, ReBeL self-play data generation, value network,
policy network, or long-running job has been started. The Common M0--M2 policy
entry is integrated and content-bound at M3, but it is not yet a complete
socket/deadline product. Large-scale training may begin only after the parent
comparison accepts both routes' M3 evidence and M4's real
abstraction/checkpoint contract passes small-scale validation.

The toy LCFR solvers validate the iteration-weighted algorithm stated in the
DecisionHoldem paper. They do not resolve the official README's conflicting
MCCFR labels or reproduce the unpublished/missing HUNL blueprint pipeline.
