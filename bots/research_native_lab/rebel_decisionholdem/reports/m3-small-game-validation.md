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

- tests: `14 passed` (1.29 seconds in the final rerun);
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

## A1 result

The A1 toy loop stores the exact joint probability of all six legal Kuhn deals.
Each public action multiplies those probabilities by the acting player's
private-card-conditioned action likelihood and normalizes the result. Tests
cover:

- uniform initial marginals;
- direct Bayes posterior;
- private-card exclusion without a false factorized-range assumption;
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

- A1 exact PBS/range-update minimum loop: **pass**.
- A2 LCFR deterministic convergence and exact BR/exploitability: **pass**.
- A2 checkpoint/resume: **pass**.
- A2 plain-vs-safe Coin Toss falsifier: **pass**.
- Leduc: **pending**.
- ReBeL counterfactual leaf values/search/self-play learning: **pending**.
- DecisionHoldem HUNL abstraction/blueprint/off-tree resolver: **pending**.
- HUNL large training: **not authorized; not started**.

Therefore this is a successful first correctness milestone, not permission to
claim either candidate complete or strong.
