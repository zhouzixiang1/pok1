# Route A M3 small-game validation report

Date: 2026-07-12

Config: `../configs/small_game_gate.json`

## Reproduction commands

```bash
python -m pytest bots/research_native_lab/rebel_decisionholdem/tests -q
python -m bots.research_native_lab.rebel_decisionholdem.tools.run_small_game_validation \
  --iterations 10000 --seed 19
python -m bots.research_native_lab.rebel_decisionholdem.tools.train_blueprint \
  --iterations 100 \
  --checkpoint /tmp/route-a2-leduc-lcfr.json \
  --export /tmp/route-a2-prototype-export
python -m bots.research_native_lab.rebel_decisionholdem.tools.milestone_manifest
```

The original Kuhn result on Python 3.14.4 remains unchanged:

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

The restart-recovery extension adds exact Leduc evidence:

- 120 ordered physical deals and 288 information sets;
- uniform-profile player-0 value: `-0.07812499999999978`;
- uniform-profile best responses: player 0 `2.0875`, player 1
  `2.659722222222222`;
- uniform exploitability: `2.373611111111111`;
- 100-iteration alternating-LCFR exploitability:
  `0.0344898288222515`;
- Leduc LCFR checkpoint SHA-256:
  `a8c1292ad102be6eaf76488e2a976ac2f1de7ea936240476fae095e73e013405`;
- the one-shot measurement used for these metrics took 6.49 seconds wall time
  and 25,336 KiB maximum RSS; it did not use GPU or write a retained checkpoint.

Final recovery verification on Python 3.14.4: `58 passed in 25.18s`; the timed
process took 25.34 seconds wall time with 53,688 KiB maximum RSS. The 49 static
test functions expand to 58 pytest cases through parameterization. No GPU,
network service, large training process or retained experiment asset was used.

`tools/milestone_manifest.py` regenerates the Kuhn and Leduc validation metrics,
A1 trace digest, coarse-projection digest and decoder version; counts current
test functions; hashes the complete non-generated package tree; and verifies the
committed manifest. The manifest excludes only itself to avoid a recursive hash.
A dedicated pytest fails if a source/report/test is added or changed without
regenerating the same snapshot.

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

## Exact Leduc extension

The independent six-card tree uses two physical copies of each of three ranks,
one private card per player, one public card, one-chip antes, 2/4-chip fixed
bets, at most two raises per street, and player 0 first on both streets. All 120
ordered deals are traversed. The exact evaluator computes profile value and a
deterministic information-set-consistent best response; a recovery bug that
descended through zero-probability opponent branches was exposed with a pure
strategy profile and fixed.

The Leduc solver freezes the strategy during every chance-complete player update,
weights regret and average-strategy deltas by the absolute LCFR iteration, and
updates the two players alternately. The 7+13 checkpoint/resume path is bit-exact
with an uninterrupted 20-iteration run. Checkpoint loading now rejects a changed
fidelity boundary, non-canonical or duplicate decoded information-set keys,
booleans/strings as numeric state, non-finite values and negative average sums.

This is paper-faithful clean-room LCFR at the algorithm level. It does not settle
the official DecisionHoldem repository's LCFR/MCCFR conflict and does not imply
that its missing HUNL assets or online resolver have been reproduced.

## M4 prototype boundary

The recovery snapshot also preserves a preliminary A2 packaging path so its
limitations are executable rather than aspirational:

- preflop uses all 169 order-independent classes;
- postflop uses only nine made-hand categories, versus the paper's large cluster
  tables;
- the national action prototype uses fold, check/call, exact minimum raise,
  0.5-pot, pot, 1.5-pot and all-in; this intentionally differs from the paper's
  2-pot/4-pot sizes;
- each Leduc fixed-limit raise probability is spread across those five aggressive
  actions with hard-coded `0.35/0.30/0.20/0.10/0.05` weights. Those weights are
  an invented functional adaptation with no DecisionHoldem fidelity claim;
- 140 sparse policy rows are an arithmetic projection from the Leduc average
  strategy, not regrets trained on HUNL;
- projection SHA-256:
  `89f59803585b18285f64936134d9708f4b65687c6ce5059a56cb5446f113b786`;
- off-tree raises are retained for diagnostics but only nearest-translated; no
  safe resolve or actual-size action injection exists;
- the atomic export binds the entrypoint, runtime, blueprint, modes, abstraction
  versions and fidelity metadata, and rejects drift, extra files and symlinks.

The native shell was unit-tested for fragmented/sticky numeric tokens, the
preflop closing-check boundary, postflop `check -> call`, platform-suppressed
peer closing-call accounting, official send throttling state separation and
all-in runout. Recovery review fixed three protocol-state bugs: an unsolicited
action after a preflop closing check, acting again during all-in runout, and
dropping a peer contribution when the platform jumps directly to a new street.

This shell is not yet bound to the separately owned frozen `common_contracts`
`NationalGameState`, has not completed a 70-hand TCP match, and has no HUNL-
trained blueprint. It is therefore an M4 prototype only, not a blueprint-only
candidate or official-platform claim.

## Stage conclusion

- A1 paper-shaped marginal update plus exact-toy oracle boundary: **pass**.
- A2 toy LCFR deterministic convergence and exact BR/exploitability: **pass**;
  DecisionHoldem blueprint fidelity remains unresolved.
- A2 checkpoint/resume: **pass**.
- A2 plain-vs-safe Coin Toss falsifier: **pass**.
- Leduc exact tree/value/BR and alternating LCFR: **pass at M3 toy scope**.
- Sparse Leduc-to-national projection and export: **prototype evidence only;
  M4 not complete**.
- ReBeL counterfactual leaf values/search/self-play learning: **pending**.
- DecisionHoldem-like HUNL abstraction/blueprint/common-state integration:
  **pending M4**.
- DecisionHoldem-like plain/safe resolve, actual off-tree injection and diverse
  opponent leaves: **pending after blueprint-only**.
- HUNL large training: **not started**.

Therefore the route-A M3 small-game gate is reproducibly green at toy scope. It
is not permission to claim either candidate complete, nationally compliant, or
strong; M4 still requires a real HUNL blueprint-only Bot and full-match evidence.
