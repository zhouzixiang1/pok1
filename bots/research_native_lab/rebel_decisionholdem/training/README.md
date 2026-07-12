# Training boundary for the first milestone

Only deterministic tabular Kuhn LCFR is authorized in this milestone. Its state
is fully represented by the JSON checkpoint format
`route-a-kuhn-lcfr-v1`. Checkpoint/resume equality is covered by tests.

No HUNL blueprint, self-play data generation, value network, policy network, or
long-running training job has been started. Large-scale training may begin only
after the source/fidelity audit, common national-rule gates, and small-game
counterfactual/safe-solving gates are complete.

The toy LCFR solver validates the algorithm stated in the DecisionHoldem paper.
It does not resolve the official README's conflicting MCCFR labels or reproduce
the unpublished/missing HUNL blueprint pipeline.
