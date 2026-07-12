# Route A M0 source, license, and fidelity audit

Audit time: 2026-07-12 15:55 +08:00

Repository base: `6ee160c93cee8d0afdad111c4c82bc6ddb6012ca`

Scope: A1 ReBeL-like and A2 DecisionHoldem-like only

## Decision

M0 passes as a provenance and falsifiability gate, with three material fidelity
gaps frozen up front:

1. The official ReBeL repository is Apache-2.0 but explicitly contains only a
   Liar's Dice implementation. It has no HUNL source, poker model, poker
   checkpoint, or HUNL reproduction recipe. A poker implementation can only be
   called a **ReBeL-like clean-room reproduction**.
2. ReBeL Section 4 represents the learnable PBS as per-player distributions
   `beta=(Delta S1,...,Delta SN)`. The six-deal Kuhn joint distribution in this
   package is an exact toy truth/blocker oracle, not a source-faithful network
   input. The implementation now exposes the paper-shaped marginal API
   separately.
3. The DecisionHoldem repository is AGPL-3.0. Its README requires external
   `sevencards_strength.bin`, four street-cluster files, and
   `blueprint_strategy.dat`. The repository contains `AlascasiaHoldem.so` and
   `blueprint.so`, but not corresponding real-time-search source. The paper says
   the diverse-opponent safe-search details will appear in later work. No such
   details are supplied by the audited repository. A clean-room route must not
   claim exact resolver fidelity. Separately, the paper and README opening call
   the blueprint algorithm Linear CFR, while the README framework/detail section
   calls it MCCFR. This unresolved LCFR-vs-MCCFR conflict also blocks an exact
   blueprint reproduction claim.

No third-party source code was copied into this package. In particular, no AGPL
code or binary was imported, linked, translated, or used to generate code.

The machine-readable source versions, hashes, and local national-rule hashes
are in `../manifests/sources.json`.

## Primary-source findings

### ReBeL

- Paper: Brown et al., NeurIPS 2020, arXiv:2007.13544.
- Official repository: `facebookresearch/rebel` at
  `7960a42750f3407ea9eb2c3333d4c2a7961f6df4`, archived read-only, Apache-2.0.
- The README states that the repository contains only Liar's Dice. Released
  checkpoints cover Liar's Dice configurations, not poker.
- Section 4 defines the PBS with per-player `Delta S_i` distributions. Exact
  joint deal enumeration is useful as a toy label/blocker oracle but is an
  additional validation representation, not the claimed learnable PBS shape.
  The paper's illustrative poker PBS has 104 probabilities, 52 per player,
  rather than a Cartesian joint tensor over both private-card spaces.
- Relevant official symbols for audit, not copied implementation:
  `RlRunner::step`, `RlRunner::sample_state`, `beliefs_`,
  `normalize_beliefs_inplace`, `CFR::step`, `CFR::update_value_network`,
  `get_query`, `get_belief_propogation_strategy`, and Python `CFVExp`.
- Paper scale is not reproducible from the public repository: the HUNL system
  used a single training machine and up to 128 eight-GPU data-generation
  machines. The paper reports interactive decisions below five seconds, but
  does not release the poker assets that achieved those results.

### DecisionHoldem

- Paper: Zhou et al., arXiv:2201.11580v2 (2024-05-28).
- Official repository: `AI-Decision/DecisionHoldem` at
  `a9ea9a545c7bb24f4e657bc6d1f75af66aa1bb51`, AGPL-3.0.
- The paper specifies 169/50,000/5,000/1,000 hand abstractions across
  preflop/flop/turn/river and actions `F,C,0.5P,P,2P,4P,A`, with narrower
  actions after repeated raises. It reports roughly 200 million LCFR
  iterations, 48 CPU cores, 3--4 days, and about 4,000 core-hours.
- Audited source symbols include `blueprint_cfr`, `blueprint_cfrp`,
  `dfs_discount`, `update_strategy`, `Singleiter`,
  `multiprocess_blueprint`, and the best-response/CFV routines in
  `Exploitability.h`. These names only establish availability; this package does
  not copy their implementation.
- The paper and the README introduction say Linear CFR. The same README later
  labels `Multi_Blureprint.h` and a README-named `BlueprintMCCFR.cpp` as MCCFR.
  At the audited commit the tracked file is actually `BlueprintMCCFR.h`, not
  `.cpp`; `Multi_Blureprint.h` is present, but the README-named
  `Depth_limit_Search.h` is absent. Therefore the shipped blueprint algorithm
  cannot be identified as a faithful LCFR implementation from the published
  material.
- The paper reports 6,000 online iterations on preflop/flop and 10,000 on
  turn/river, but does not specify enough of the diverse-opponent safe resolver
  to reproduce it. The repository's actual real-time interface is binary-only.
- Reported Slumbot/OpenStack chip rates are historical evidence, not a
  reproducible strength certificate under this project's 70-hand contract.

## Fidelity matrix

### A1 ReBeL-like

| Paper formula/section | Official source symbol or asset | Current implementation | Fidelity label | Verification | Falsifier / next gate |
|---|---|---|---|---|---|
| Section 4, `beta=(Delta S1,...,Delta SN)` and Bayes update after a public action | `beliefs_`, `RlRunner::sample_state`, `normalize_beliefs_inplace` in the Liar's Dice code | `KuhnMarginalPublicBeliefState` stores two normalized per-player ranges and Bayes-updates the acting range | paper-faithful clean-room for toy representation/update | joint-to-marginal projection; acting-range posterior equals exact joint truth; zero-evidence rejection | any acting-range posterior differs from direct Bayes calculation |
| No direct paper counterpart: exact Kuhn joint-deal truth with card conflicts | none; verification-only extension | `KuhnPublicBeliefState` retains all six legal deals, projects marginals, and supplies exact blocker-aware labels | inspired verification extension / exact-toy oracle | impossible same-card deals, joint posterior, projection and zero-sum label tests | must never be used to claim the ReBeL learnable PBS is a six-deal joint tensor |
| Eq. 1--2 and `v_hat: B -> R^(|S1|+|S2|)` | `get_query`, `CFR::get_hand_values`, `update_value_network` | only exact **on-policy** Kuhn continuation labels | unresolved fidelity gap | zero-sum expectation test | do not pass the next A1 gate until counterfactual values match an exact small-game oracle |
| Algorithm 1 root solve -> value target -> sampled leaf PBS -> repeat | `RlRunner::step`, `sample_state_to_leaf`, Python `CFVExp` | deterministic PBS/action/update/terminal trace only | functional adaptation | same seed/deal produces identical complete trace | not self-play learning; cannot be cited as ReBeL training |
| Section 5.1 CFR-D; Appendix-I CFR-AVG changes leaf PBS from current to average policy | `CFR`, `get_belief_propogation_strategy` in Liar's Dice code | not implemented | unresolved fidelity gap | future exact Kuhn/Leduc exploitability test | failure if more search increases exploitability or leaf beliefs use the wrong policy |
| Section 5.3 optional policy net and warm start | released Liar's Dice MLP/config only | not implemented | unresolved fidelity gap | future warm-start ablation | no policy-net claim before heldout PBS and search-quality evidence |
| Section 6 random search-iteration sampling for safe play in expectation | no poker artifact | not implemented | unresolved fidelity gap | future exact best-response comparison | argmax/final-iterate substitution is not faithful |
| Section 7 at most nine poker actions; observed off-tree action added to subgame | no poker code/assets | not implemented | unresolved fidelity gap | later national legal-action/off-tree tests | nearest-neighbor-only translation fails the route requirement |

### A2 DecisionHoldem-like

| Paper formula/section | Official source symbol or asset | Current implementation | Fidelity label | Verification | Falsifier / next gate |
|---|---|---|---|---|---|
| Section 2/Table 1 hand and action abstraction | game-tree headers exist; required cluster binaries mostly external | no HUNL abstraction | unresolved fidelity gap | future bucket/action manifest and collision tests | cannot call a toy solver a blueprint Bot |
| Section 2 says LCFR; README later says MCCFR; Brown/Sandholm 2019 defines LCFR weight `t` for regret and average-strategy updates | AGPL symbols `blueprint_cfr`, `dfs_discount`, `update_strategy` inspected only; tracked `BlueprintMCCFR.h`/`Multi_Blureprint.h` conflict with LCFR prose | independent alternating full-tree Kuhn `LinearCFR`, with a frozen policy across each chance-complete player update | paper-faithful clean-room **toy LCFR**, but unresolved fidelity gap for the DecisionHoldem blueprint | exact BR/exploitability, deterministic convergence, bit-exact checkpoint/resume payload | toy LCFR can pass while strict reproduction remains blocked; 10,000-iteration exploitability over 0.02 also fails the toy gate |
| Approximately 200M iterations on abstract HUNL | external `blueprint_strategy.dat` and cluster files | not started | unresolved fidelity gap | later small-to-large scaling gate | no large training until small-game and common national contracts pass |
| Off-tree online search, 6k/10k iterations | `AlascasiaHoldem.so`; source absent | not implemented | unresolved fidelity gap | future explicit subgame/tree/action tests | binary behavior cannot be inferred and represented as source-faithful |
| Safe depth-limited solving with diverse opponent ranges | paper defers details; no source found | Coin Toss per-type alternative-payoff constraint from Brown/Sandholm 2017 | functional adaptation | plain resolver adds 0.25 exploitability; constrained resolver has zero per-type margin violation and zero delta | this oracle is not the DecisionHoldem resolver and cannot pass the HUNL safe-solving gate |
| Blueprint-only interface | `blueprint.so`, but required blueprint asset is external | toy LCFR policy only | unresolved fidelity gap | future complete native match with all search disabled | no claim of a playable DecisionHoldem blueprint yet |

## National adaptation delta

These are frozen requirements for later stages, not implemented in this toy
milestone:

- The internal game must use 20,000 chips per player, 50/100 blinds, reset each
  hand, and exactly 70 hands per match.
- National `raise X` is the current-street raise-to total. Exact `200 -> 400`
  is legal; `2x+1` may be a conservative policy but is not the legality rule.
- The official postflop `call`/`check` street-closing semantics, all-in behavior,
  sticky TCP framing, suppressed closing actions, suit mapping, and final-hand
  THP proof belong in the shared national adapter, not in A1/A2 strategy code.
- ReBeL's at-most-nine actions and DecisionHoldem's Table-1 actions are not
  automatically legal national actions. Every abstract action must be converted
  through the shared oracle, and the exact observed off-tree raise-to value must
  remain available to the route-specific search.
- The online hard deadline remains 60 seconds despite unrestricted offline
  research time. Later candidates need a legal cached action by about 250 ms,
  complete strategy snapshots, and a hard compute stop near 54 seconds to leave
  room for the official 0.30-second send throttle and cleanup.
- Canonical A1/A2 versions remain within-hand equilibrium methods. Cross-hand
  opponent posterior and 70-hand score control belong to route B and must not be
  silently added to the reproduction scores.

## License policy

- Academic papers are specifications and may be paraphrased; their text/code is
  not treated as software under an inferred open-source license.
- ReBeL's Apache-2.0 code may be studied, but the current small-game code is an
  independent implementation and carries no imported source.
- DecisionHoldem AGPL code and binaries are audit-only. Importing, translating,
  linking, or distributing them requires a separate user decision and full
  license compliance. Until then the default is clean-room implementation from
  published algorithms.
- OpenSpiel is an Apache-2.0 future differential oracle, not the national rules
  source and not a dependency of this milestone.

## Gate conclusion

- **M0 source/provenance:** pass.
- **M0 exact DecisionHoldem reproduction:** impossible with currently published
  material; LCFR-vs-MCCFR, missing search source, and missing assets remain
  strict blockers. The route is explicitly downgraded to clean-room/functional
  adaptation.
- **Small-game correctness prerequisite:** implemented and reported separately.
- **Leduc:** not yet implemented; Kuhn plus the published Coin Toss safety
  example is the current small-game evidence.
- **HUNL training/native TCP:** prohibited at this milestone.
