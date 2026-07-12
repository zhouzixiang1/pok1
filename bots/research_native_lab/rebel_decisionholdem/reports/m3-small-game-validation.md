# Route A first small-game validation report

Date: 2026-07-12

Config: `../configs/small_game_gate.json`

## Reproduction commands

```bash
python -m pytest bots/research_native_lab/rebel_decisionholdem/tests -q
python -m bots.research_native_lab.rebel_decisionholdem.tools.run_small_game_validation \
  --iterations 10000 --seed 19
```

Observed result on Python 3.14.4:

- tests: `16 passed` (final rerun recorded after manifest verification);
- end-to-end validation wall time: about 1.4 seconds;
- A2 Kuhn LCFR iterations: 10,000;
- A2 NashConv: `1.7222093396239424e-05`;
- A2 exploitability: `8.611046698119712e-06`;
- LCFR canonical checkpoint digest:
  `bb07a4b94c883fb549f19bd88477da52f5125248f8095c0442caad092f2eb311`.

The digest covers the iteration counter, all regrets, and all average-strategy
accumulators. The checkpoint test compares a 400+800 resumed run with an
uninterrupted 1,200-iteration run and requires identical payload, digest, and
average strategy.

`tools/milestone_manifest.py` regenerates the validation metrics and A1 trace
digest, counts current test functions, hashes the complete non-generated package
tree, and verifies the committed manifest. The manifest excludes only itself to
avoid a recursive hash; a dedicated pytest fails if a source/report/test is
added or changed without regenerating the same snapshot.

## A1 result

The A1 toy loop now exposes two deliberately separate states:

- `KuhnMarginalPublicBeliefState` is the ReBeL-paper-shaped tuple of normalized
  per-player `Delta S_i` ranges. A public action Bayes-updates only the acting
  player's range.
- `KuhnPublicBeliefState` stores the exact joint probability of all six legal
  Kuhn deals. It is a toy label/blocker oracle and verification extension, not
  the learnable PBS representation claimed by ReBeL.

Tests cover:

- uniform initial marginals;
- direct Bayes posterior;
- joint-to-marginal projection and exact agreement for the acting player's
  Bayes posterior;
- the explicit boundary where the paper-shaped non-acting marginal stays
  unchanged while the exact joint oracle exposes blocker correlation;
- private-card exclusion in the joint truth oracle;
- zero-likelihood observation rejection;
- deterministic seeded complete-hand trace;
- zero-sum terminal utility and on-policy continuation value in expectation.

This establishes the PBS/range plumbing only. The supplied fixture policy is
not trained. The value-label helper is explicitly on-policy, not ReBeL's
counterfactual value target. CFR-D/CFR-AVG, sampled-iteration safe test-time
play, value/policy networks, and HUNL search remain unimplemented.

## A2 result

The A2 solver is an independent alternating full-tree LCFR implementation. It
weights both instantaneous regret and average-strategy contributions by the
absolute iteration number. The strategy is frozen across all six chance deals
during each player's update, preventing chance traversal order from changing an
infoset policy within one CFR update.

This validates the LCFR algorithm named by the DecisionHoldem paper and README
opening. It does not resolve the same README's later MCCFR labels, the
`BlueprintMCCFR.h` versus README-named `.cpp` mismatch, or the missing
`Depth_limit_Search.h`. Strict DecisionHoldem blueprint fidelity remains
blocked despite the toy LCFR result.

Exact exploitability is computed without sampling: all 64 deterministic Kuhn
policies for each responding player are enumerated. The evaluator separately
checks a known Kuhn equilibrium at game value `-1/18` and zero exploitability.
The 10,000-iteration result is well below the frozen `0.02` gate.

The Coin Toss resolver reproduces the public safe-solving counterexample:

| Resolver | P(guess Heads) | H/T safety margins | exploitability delta |
|---|---:|---:|---:|
| isolated/plain | 0.50 | `(0.5, -0.5)` | 0.25 |
| alternative-payoff constrained | 0.25 | `(0.0, 0.0)` | 0.0 |

This proves that the test detects an unsafe isolated solve and accepts the
per-type constrained solution. It is a functional validation oracle from the
2017 safe-solving paper, not a reconstruction of DecisionHoldem's unpublished
diverse-opponent resolver.

## Stage conclusion

- A1 paper-shaped marginal update plus exact-toy oracle boundary: **pass**.
- A2 toy LCFR deterministic convergence and exact BR/exploitability: **pass**;
  DecisionHoldem blueprint fidelity remains unresolved.
- A2 checkpoint/resume: **pass**.
- A2 plain-vs-safe Coin Toss falsifier: **pass**.
- Leduc: **pending**.
- ReBeL counterfactual leaf values/search/self-play learning: **pending**.
- DecisionHoldem HUNL abstraction/blueprint/off-tree resolver: **pending**.
- HUNL large training: **not authorized; not started**.

Therefore this is a successful first correctness milestone, not permission to
claim either candidate complete or strong.
