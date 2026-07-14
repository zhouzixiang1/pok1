# Route A M3 small-game and Common-integration report

Date: 2026-07-14

Route base: `6ee160c93cee8d0afdad111c4c82bc6ddb6012ca`

Frozen Common dependency:
`cc8beed256024fadd1cf89b0e40dcdea6a5c959d`

Scope: A1 ReBeL-like and A2 DecisionHoldem-like M3 correctness only. No HUNL
training, neural model, complete 70-hand Bot, or `national_v*` candidate is
claimed.

## Reproduction commands

```bash
/home/zzx/.cache/pok-research-py312/bin/python -m pytest \
  bots/research_native_lab/rebel_decisionholdem/tests -q
python -m pytest bots/research_native_lab/rebel_decisionholdem/tests -q
python -m bots.research_native_lab.rebel_decisionholdem.tools.run_small_game_validation \
  --iterations 10000 --seed 19
python -m bots.research_native_lab.rebel_decisionholdem.tools.milestone_manifest
```

All runtime outputs remained temporary or ignored; no training asset was
retained by this gate.

## Frozen quantitative results

- Kuhn LCFR iterations: 10,000.
- Kuhn NashConv: `1.7222093396239424e-05`.
- Kuhn exploitability: `8.611046698119712e-06`.
- Kuhn checkpoint SHA-256:
  `bb07a4b94c883fb549f19bd88477da52f5125248f8095c0442caad092f2eb311`.
- Leduc physical deals / infosets: 120 / 288.
- Leduc uniform exploitability: `2.373611111111111`.
- Leduc 100-iteration LCFR exploitability: `0.0344898288222515`.
- Leduc checkpoint SHA-256:
  `a8c1292ad102be6eaf76488e2a976ac2f1de7ea936240476fae095e73e013405`.
- Coarse Leduc-to-national projection rows / SHA-256: 140 /
  `89f59803585b18285f64936134d9708f4b65687c6ce5059a56cb5446f113b786`.

The projection hash is evidence about the retained M4 prototype only; it is not
a HUNL blueprint score.

## A1 PBS, ranges, labels, and exact evaluation

Kuhn retains two deliberately distinct representations:

- `KuhnMarginalPublicBeliefState` is the paper-shaped tuple of two normalized
  private-card ranges. An observed action Bayes-updates its actor's range. A
  tested two-action line shows player 0's range updating on the first action and
  player 1's range updating on the second.
- `KuhnPublicBeliefState` keeps all six legal deals as an exact blocker-aware
  truth oracle. It is not represented as the ReBeL network input and is never
  used to claim a source-faithful joint PBS.

`LeducPublicBeliefState` extends the exact oracle to all 120 ordered physical
private/private/public deals. Informative actions by each player change the
corresponding posterior and, through card removal, can change the other
marginal as well. Closing preflop creates an explicit chance-pending boundary;
observing the public rank filters impossible deals and updates both private-rank
ranges. Zero-reach actions and out-of-order chance observations fail closed.

The label API distinguishes two different mathematical objects:

- `conditional_deviation_action_values` divides by the exact posterior mass of
  a private state. Its policy-weighted mixture equals the conditional on-policy
  continuation value. Range-weighting both players' values matches the separate
  full-tree evaluator and is zero-sum.
- `cfr_counterfactual_action_values` computes the standard unnormalized
  `sum_h pi_c(h) pi_-i(h) v_i(h,a)`: own reach is omitted and no posterior
  normalization is applied. For both Kuhn and Leduc, subtracting the
  policy-weighted node value exactly reproduces the production solver's
  one-iteration regret update.

This closes the M3 equation/range oracle. It does not implement ReBeL's PBS
depth-limited CFR-D/CFR-AVG loop, self-play label generation, learned value or
policy network, sampled-iteration safe play, or HUNL search. Those remain M5+
work.

## A2 Linear CFR and exact exploitability

The production Kuhn and Leduc solvers implement alternating full-tree Linear
CFR. During each player's update the policy is frozen across the complete
chance traversal. Both regret and average-strategy deltas are multiplied by the
absolute iteration `t`.

Kuhn is differentially checked for 1, 2, and 7 iterations against
`EquationLinearCFRReference`, a separate implementation that reconstructs
public-path reaches and applies the LCFR equations per information set rather
than calling the production recursive traversal. Regrets and strategy sums
agree to `1e-14`. Checkpoint resume remains bit-exact. Both checkpoint loaders
reject noncanonical decoded keys, booleans, non-finite values, negative average
sums, missing rows, and fidelity drift.

The exact Kuhn evaluator enumerates every deterministic response policy and
recovers the known equilibrium value `-1/18` with zero exploitability. The
independent Leduc evaluator computes information-set-consistent deterministic
best responses over its 120-deal tree. Both strategy validators now reject
unknown/missing actions or infosets, NaN/infinity, booleans, every negative
probability, and non-unit rows before evaluation.

This validates the published LCFR algorithm at small-game scale. It does not
resolve DecisionHoldem's LCFR/MCCFR documentation conflict, reproduce the AGPL
implementation, or replace its missing cluster/blueprint assets.

## A2 abstraction and safe-solving falsifier

The abstraction tests cover all 1,326 physical starting combinations mapping
to exactly 169 order-independent preflop classes, real five-to-seven-card hand
categories, deterministic action-size mapping, exact 2x raise boundaries, and
explicit nearest-only off-tree diagnostics. Postflop still has only nine made-
hand categories and is not DecisionHoldem's published cluster abstraction.

The Coin Toss fixture is source-shaped but deliberately narrower than a full
resolver reproduction:

| Variant | solve H/T prior | P(guess Heads) | H/T margins | full-game loss delta |
|---|---:|---:|---:|---:|
| isolated blueprint-reach | `(0.6, 0.4)` | `1.0` | `(1.5, -1.5)` | `0.75` |
| simplified per-type constraint | `(0.5, 0.5)` | `0.25` | `(0.0, 0.0)` | `0.0` |

The `(0.6,0.4)` subgame reach is derived from the paper blueprint playing Play
with probability `3/4` on Heads and `1/2` on Tails; isolated solving therefore
selects the paper's unsafe always-Heads strategy. The constrained fixture uses
the Section-2 Sell payoffs `(0.5,-0.5)`. It does **not** reconstruct Figure-3
Resolve with blueprint CBVs `(0,0.5)`, the complete augmented game, or
DecisionHoldem's unpublished diverse-opponent solver. Its role is only to prove
that the gate rejects an unsafe replacement and accepts a per-type bounded
functional alternative. Certificates are recomputed and forged safety fields
are rejected, but that recomputation is not an independent theorem prover.

## Common M0--M2 integration

The route manifest binds the entire 55-file Common package tree and the exact
critical-interface hashes. At this report revision:

- Common package tree SHA-256:
  `62b0d0dc04a4dfdfda9d6b89ae8fd0328e20958bdaa4c425e88ca2ebbd00fa66`;
- critical interface tree SHA-256:
  `c3aed6e32453f45031365fbe5a9d654464364c78fcb3d66c82dba323d53395f5`;
- national-game contract SHA-256:
  `e23831c0e83349a576658938b450b044cf527a1c4452284b6efa21445c09ffab`.

`CommonA2StrategyRuntime` is the tested strategy entry. It receives tokens via
Common `StreamDecoder`/`NationalProtocolSession`, obtains the issued
`NationalGameState`, projects only within-hand fields into A2, intersects route
action candidates with Common `LegalActionSet`, returns a Common `Action`, and
consumes the one-shot decision lease. Tests cover fragmented/sticky tokens,
preflop close, postflop `check -> call`, all-in runout, all 52 card conversions,
and exact minimum raises.

The adapter fails closed on a copied/unissued state, stale full-state binding,
wrong actor, unknown hero cards, route/Common legality disagreement, zero legal
blueprint mass, and any partially dropped unavailable probability mass. It
records available/dropped mass, fallback status, legal raise bounds, and the
full-state binding. `full_state_id` is used only for stale-send validation.
Policy lookup uses `information_state_id`; neither `observation_id` nor
`match_context_id` is a route strategy feature. Mixed-strategy sampling uses a
replayable counter RNG over bot seed, decision occurrence, within-hand
information state, and blueprint digest—not hand number or match score.

The retained coarse Leduc projection currently assigns positive mass to an
action that aliases or is unavailable in at least the initial Common state, so
the strict entry rejects it. This executable negative result is intentional:
the adapter is integrated, but the prototype policy is not silently promoted
to an M4 blueprint-only Bot. M4 must define and train a Common-native action
mapping whose complete probability measure is legal at every reached state.

This is a real dependency, not a copied rules implementation. It remains an M3
strategy/session seam: socket ownership, official 0.30-second throttling,
deadline supervision, terminal THP proof, and a complete 70-hand match are not
yet productized in this entry.

## Retained M4 prototype boundary

The older `native_entry.py` and atomic export remain available only to preserve
the previously tested packaging prototype. They still use the coarse Leduc
projection and are not the Common-authoritative entry. Their hard-coded spread
of Leduc raise probability across national sizes is an invented adaptation;
nearest-action translation is not safe resolving. No result from that shell can
be reported as M4 completion or national compliance.

## Final verification and resource result

The final manifest-bound suite contains 82 static test functions expanding to
105 pytest cases. The complete command, including `test_manifest.py`, passed
under both Python 3.12 and the default Python 3.14.4 with zero skip/failure. The
manifest test recomputes all dynamic validation metrics, the complete route tree
hash, and the Common package/critical-interface hashes. A dedicated drift test
also mutates the computed Common package digest and proves that verification
fails closed.

For a nonrecursive resource measurement, the same suite excluding the manifest
test module produced:

- Python 3.12: 103 passed in 18.10 seconds; 18.20 seconds elapsed and 37,888 KiB
  maximum RSS under `/usr/bin/time -v`;
- Python 3.14.4: 103 passed in 15.62 seconds; 15.82 seconds elapsed and 53,360
  KiB maximum RSS.

The 10,000-iteration end-to-end small-game validation completed in 1.01 seconds
elapsed with 19,752 KiB maximum RSS. No GPU, network service, swap, large
training process, or retained checkpoint was used. All 38 route Python files
compiled with bytecode redirected outside the tree, and all four route JSON
files parsed. The content check found changes only under
`bots/research_native_lab/rebel_decisionholdem/`.

## Stage conclusion

- A1 Kuhn/Leduc PBS and both-player range updates: **pass at exact toy scope**.
- A1 conditional labels, standard CFVs, full-tree and zero-sum consistency:
  **pass at exact toy scope**.
- A2 LCFR formula/reference, checkpointing, BR and exploitability: **pass at
  exact toy scope**.
- A2 source-shaped unsafe/simplified-safe falsifier: **pass as a functional
  boundary only; full safe resolver pending**.
- A2 Common state/action/legal/session integration: **pass as an M3 strategy
  seam; native productization and complete match pending**.
- Real HUNL A2 blueprint-only Bot: **M4 not complete**.
- ReBeL self-play/search/value-policy loop: **M5 not complete**.
- Large HUNL training: **not started and not authorized by this report alone**.

M3 is therefore ready for independent review. It is not evidence that either
route-A candidate is complete, nationally compliant, or strong.
