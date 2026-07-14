# Route A1 M5a PBS and exact-label contract validation

Date: 2026-07-14

## Result and scope

M5a passes its bounded correctness gate. It freezes a blocker-aware HUNL
public-belief-state contract and a complete exact Kuhn/Leduc value-label
fixture. This is infrastructure for a later neural leaf-value milestone, not
that milestone itself.

The gate does not generate HUNL labels, train a network, implement
depth-limited CFR/CFR-D/CFR-AVG or online resolving, certify an official EXE,
or provide strength evidence. The committed config, artifact and additive
manifest keep all corresponding flags false.

M4 remains a separate historical A2 blueprint-only gate. Its manifest,
artifact, scale evidence, TCP evidence and report were not regenerated or
edited. The additive M5a manifest anchors their existing bytes by SHA-256.

## Frozen outputs

The exact fixture is
`artifacts/m5a_exact_label_fixture.json`:

- raw file SHA-256:
  `ae4e8eca65d2c99429f0a7f064abfac9f468347903ab3dd131959865c7ff8797`;
- canonical body SHA-256:
  `3c07efb96f256c2466fcd140a2967032ca1d0cf2edf474c540733d179f39387f`;
- size: 1,107,756 bytes;
- examples: 100 total, comprising all 4 Kuhn and all 96 Leduc public decision
  nodes;
- public-family splits: 80 train, 15 validation and 5 test;
- complete ordered identity-list SHA-256:
  `ef1be544a7aff691d791c2935a2c0342684005735c08c61f24d1a5e86015afe0`;
- critical-source snapshot SHA-256:
  `941d18e36b39cb674c2cdc9da672858a0e013f077a5b3597e34ad069bdcb99ae`.

A second build to an independent output path was byte-identical to the
committed artifact. The builder scans every declared critical source before
generation and rechecks the same closure after publication. Verification
reconstructs the exact solvers and all labels instead of trusting wrapper
hashes alone.

## HUNL public-belief-state contract

`rebel_like/hunl_pbs.py` uses Common's fixed lexicographic 1,326-combination
registry. Its registry SHA-256 is
`59a53ca8f0a82e12a77de78885d6ce8f4e816c3d7b2d35fb06a7cba12e3133c3`.
The two stored vectors are reach factors, not private-hand marginals. For
public board `B`, the normalized joint belief is

```text
J(h0,h1) = K_B(h0,h1) * beta0(h0) * beta1(h1) / Z,
```

where `K_B` rejects board conflicts and shared private cards. Projected player
marginals, legal-hand masks and label-valid masks are derived from this joint.
A public action multiplies only the acting player's factor, but both projected
marginals may change through card compatibility. The factor-vector normalizer
is deliberately distinct from the blocker-aware probability of the observed
public event.

Construction accepts only the exact nonterminal Common
`hand_public_dict()` schema after terminal fields are removed. It reconstructs
`NationalGameState`, validates the history, and requires an exact public
round-trip. It rejects private cards, payoff/outcome data, opponent showdown
cards, match context, seed/timing data, future cards, unknown keys, invalid
boards, invalid action histories and bool-as-integer aliases. Uniform factors
are legal only at a true new-hand root; a mid-hand reset is rejected.

Observed raises use the exact legal Common raise-to amount. There is no
nearest-size translation. Street-closing actions proven by a relay or uniquely
inferred at an official boundary update the state once. Public-node identity,
mathematical PBS identity and trace provenance have separate digests. The
network-shaped input contains public state, the two factors and the registry
identity, but never the action trace.

The HUNL tests check blocker-aware joint counts on boards of 0, 3, 4 and 5
cards: 1,624,350; 1,271,256; 1,167,480; and 1,070,190 ordered compatible joint
deals. A reduced exact oracle also falsifies the tempting but incorrect
assumption that independently normalized factors are the projected
marginals.

## Exact label semantics

Both exact games train the checked-in alternating Linear CFR solvers for four
iterations. Beliefs and labels use the frozen average policy. The artifact
also embeds and independently binds the checkpoint, current policy and
average policy so these meanings cannot be silently exchanged.

Every example carries three deliberately different namespaces:

- `oracle_on_policy_private_values`: posterior-normalized continuation value
  for each fixed player and compatible private state;
- `oracle_forced_action_conditional_q`: posterior-normalized conditional value
  for each legal forced action of the actor;
- `oracle_unnormalized_cfr_action_values`: standard counterfactual action
  values weighted by chance and opponent reach while omitting the acting
  player's own reach.

Utilities are chips from a fixed player perspective with per-hand net payoff
origin. Labels use blocker-aware projected marginals rather than raw reach
factors. Every example must have a nonzero Q-versus-counterfactual-value
separation, preventing an implementation from satisfying the contract by
copying one namespace into the other.

The production validator replays public actions from the root, including the
Leduc public-card chance transition, reconstructs factors and recomputes all
three namespaces from full terminal trees. The test suite contains a second
brute-force oracle covering all 100 public nodes. That oracle does not call the
production PBS label helpers; it independently enumerates compatible deals,
Bayes likelihoods, continuations, conditional Q values and counterfactual
weights.

## Strictness and adversarial checks

The config and artifact readers reject duplicate keys and non-finite numbers.
Schema validation uses exact JSON types, so Python aliases such as `false ==
0`, `true == 1`, and integer/float substitutions do not pass. Tests cover
forged reach factors, labels, solver/generator bindings, omitted or additional
public nodes, wrong split counts, embedded-config drift, source-closure drift,
unknown HUNL public fields, bool player indices, bool card/action indices and
non-lowercase SHA-256 strings.

The additive manifest scans every Route A Python file with `ast`, including
tests. Imports of top-level `engine`, `engine.*` and `sever.bot_adapter` are
forbidden. A collection-time assertion also checks that pytest did not load
those modules. `sever.engine.*`, which is the national TCP implementation, is
not the forbidden legacy top-level engine.

## Validation evidence

The M5a source tests passed:

```text
python -m pytest \
  bots/research_native_lab/rebel_decisionholdem/tests/test_m5a_hunl_pbs.py \
  bots/research_native_lab/rebel_decisionholdem/tests/test_m5a_label_contract.py -q
44 passed in 18.89s
```

The national-native protocol regression was run separately and is the formal
protocol shard for this gate:

```text
python -m pytest sever/tests/test_national_platform_alignment.py -q
10 passed in 0.01s
```

An additional compatibility run included Common, the national-native shard
and the legacy-adapter shard:

```text
python -m pytest bots/research_native_lab/common_contracts/tests \
  sever/tests/test_national_platform_alignment.py \
  sever/tests/test_national_alignment.py -q
290 passed, 1 skipped in 178.10s
```

That additional adapter result is not counted as the formal national-native
gate. Neither protocol result is official Windows EXE certification or poker
strength evidence.

The final complete Route A source tree passed with bytecode and pytest's cache
provider disabled:

```text
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider \
  bots/research_native_lab/rebel_decisionholdem/tests -q
246 passed
```

## Frozen M4 anchors

The additive M5a manifest binds these unchanged historical files:

- M4 manifest:
  `ad453b1d444396678d14b0929369f6a589a78aa6df9887a27ebcb8a748bda99e`;
- M4 HUNL smoke artifact:
  `aa50b89b5b3d9822712c4f6a93a25448437526071aec3f9760c8abcdb4600539`;
- M4 scale evidence:
  `b54419b35d2f3148d08d83dcc303121828fcbc6b6d2180b1676689096d7239ed`;
- M4 local TCP evidence:
  `1e1cceafa466946354e0a58ebf7c921a41f0f607350422fb3ca731e8e0eb42de`;
- M4 validation report:
  `9e1d6f43772607d653e979af7be7f843498a62d50816819ffed0e7b7cf7b268a`.

These anchors preserve M4 as historical evidence while allowing M5a to bind
the new implementation and tests without pretending the old whole-tree
snapshot describes the newer tree.
