# Route A M0 source, license, and fidelity audit

Audit time: 2026-07-12 15:55 +08:00

Implementation-status revision: 2026-07-14 (source conclusions unchanged;
M3 PBS/LCFR/Common-integration evidence and the explicitly non-faithful M4
prototype are reflected below)

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
| Section 4, `beta=(Delta S1,...,Delta SN)` and Bayes update after a public action | `beliefs_`, `RlRunner::sample_state`, `normalize_beliefs_inplace` in the Liar's Dice code | `KuhnMarginalPublicBeliefState` stores two normalized per-player ranges and Bayes-updates each actor's range as that player acts; the exact Leduc joint PBS exposes both private-rank marginals and public-card blockers | paper-faithful clean-room for toy marginal shape/update plus exact validation extension | both players' informative action updates, joint-to-marginal projection, public-rank conditioning and zero-evidence rejection | any acting-range posterior differs from direct Bayes or a public blocker leaves impossible positive mass |
| No direct paper counterpart: exact Kuhn/Leduc joint-deal truth with card conflicts | none; verification-only extension | exact six-deal Kuhn and 120-deal Leduc posteriors project both ranges and retain blocker correlations | inspired verification extension / exact-toy oracle | impossible same-card deals, joint posterior, projection and zero-sum label tests | must never be used to claim the ReBeL learnable PBS is a full joint tensor |
| Eq. 1--2 and `v_hat: B -> R^(|S1|+|S2|)` | `get_query`, `CFR::get_hand_values`, `update_value_network` | exact posterior-normalized continuation/deviation labels plus separately named standard unnormalized CFR action values for Kuhn and Leduc | small-game equation oracle only; learned ReBeL target remains pending | conditional values match independent full-tree expectations and range-weighted zero sum; unnormalized CFVs reproduce one-step LCFR regrets | no value network, PBS search target generation, or heldout error evidence exists before M5 |
| Algorithm 1 root solve -> value target -> sampled leaf PBS -> repeat | `RlRunner::step`, `sample_state_to_leaf`, Python `CFVExp` | deterministic PBS/action/update/terminal trace only | functional adaptation | same seed/deal produces identical complete trace | not self-play learning; cannot be cited as ReBeL training |
| Section 5.1 CFR-D; Appendix-I CFR-AVG changes leaf PBS from current to average policy | `CFR`, `get_belief_propogation_strategy` in Liar's Dice code | not implemented | unresolved fidelity gap | future exact Kuhn/Leduc exploitability test | failure if more search increases exploitability or leaf beliefs use the wrong policy |
| Section 5.3 optional policy net and warm start | released Liar's Dice MLP/config only | not implemented | unresolved fidelity gap | future warm-start ablation | no policy-net claim before heldout PBS and search-quality evidence |
| Section 6 random search-iteration sampling for safe play in expectation | no poker artifact | not implemented | unresolved fidelity gap | future exact best-response comparison | argmax/final-iterate substitution is not faithful |
| Section 7 at most nine poker actions; observed off-tree action added to subgame | no poker code/assets | not implemented | unresolved fidelity gap | later national legal-action/off-tree tests | nearest-neighbor-only translation fails the route requirement |

### A2 DecisionHoldem-like

| Paper formula/section | Official source symbol or asset | Current implementation | Fidelity label | Verification | Falsifier / next gate |
|---|---|---|---|---|---|
| Section 2/Table 1 hand and action abstraction | game-tree headers exist; required cluster binaries mostly external | exact 169 preflop classes; postflop only nine made-hand categories; national action prototype is `F,C,min,0.5P,P,1.5P,A`, not the paper's `F,C,0.5P,P,2P,4P,A` | functional adaptation / unresolved real-cluster gap | 1,326-combination class count, five-to-seven-card category tests, differential national-action validation | nine postflop buckets and the changed sizes cannot be reported as DecisionHoldem abstraction fidelity |
| Section 2 says LCFR; README later says MCCFR; Brown/Sandholm 2019 defines LCFR weight `t` for regret and average-strategy updates | AGPL symbols `blueprint_cfr`, `dfs_discount`, `update_strategy` inspected only; tracked `BlueprintMCCFR.h`/`Multi_Blureprint.h` conflict with LCFR prose | independent alternating full-tree Kuhn and exact two-round Leduc `LinearCFR`, with a frozen policy across each chance-complete player update; Kuhn is differentially checked against a separate equation-oriented implementation | paper-faithful clean-room **toy LCFR**, but unresolved fidelity gap for the DecisionHoldem blueprint | exact formula-level regret/average accumulators, exact BR/exploitability, deterministic convergence and bit-exact checkpoint/resume; Leduc 120 deals/288 infosets | either toy solver can pass while strict reproduction remains blocked; frozen convergence thresholds remain falsifiers |
| Approximately 200M iterations on abstract HUNL | external `blueprint_strategy.dat` and cluster files | no HUNL training; the package exports only a labelled Leduc-policy seed projection | unresolved fidelity gap; projection is a functional packaging prototype | content-bound export and policy-decision influence tests | the projection cannot satisfy M4 or be called a HUNL blueprint |
| Off-tree online search, 6k/10k iterations | `AlascasiaHoldem.so`; source absent | nearest-action diagnostic mapping only; no action injection or re-solve | unresolved fidelity gap / explicitly unsafe translation | exact/off-tree mapping tests | binary behavior cannot be inferred; nearest-only mapping fails the online-search requirement |
| Safe depth-limited solving with diverse opponent ranges | paper defers DecisionHoldem details; Brown/Sandholm 2017 gives the public Coin Toss example | source-shaped functional fixture uses the paper blueprint's `3/4` vs `1/2` Play reach, so unsafe isolated solve always guesses Heads; a simplified per-type Sell-payoff constraint forces `q(H)=1/4` | **functional falsifier only**, not the paper's full Resolve augmented game and not DecisionHoldem | unsafe full-game loss delta `0.75`; constrained zero margin violation/delta; certificates recomputed and forged labels rejected | the simplified constraint uses Sell payoffs `(0.5,-0.5)`, not Figure-3 Resolve CBVs `(0,0.5)`, and cannot pass the HUNL safe-solving gate |
| Blueprint-only interface | `blueprint.so`, but required blueprint asset is external | Common-authoritative M3 policy entry over `NationalProtocolSession` plus a separate content-bound legacy packaging prototype driven by a coarse Leduc seed projection | interface integration pass; functional packaging adaptation, **not M4 complete** | Common state/action/legal-set dependency, card mapping, one-shot leases, state-bound stale rejection, no hidden invalid probability mass, sticky framing/runout; the coarse projection is explicitly rejected for unavailable mass | no complete 70-hand match, socket/deadline product, HUNL-trained asset, or playable-candidate claim |

## National adaptation delta

The rules below are owned by frozen Common M0--M2. The M3 A2 policy entry now
consumes that state/action boundary, while full native productization remains a
later stage:

- The internal game must use 20,000 chips per player, 50/100 blinds, reset each
  hand, and exactly 70 hands per match.
- National `raise X` is the current-street raise-to total. Exact `200 -> 400`
  is legal; `2x+1` may be a conservative policy but is not the legality rule.
- The official postflop `call`/`check` street-closing semantics, all-in behavior,
  sticky TCP framing, suppressed closing actions, suit mapping, and final-hand
  THP proof belong in the shared national adapter, not in A1/A2 strategy code.
  The M3 strategy entry imports those Common objects; it does not copy them.
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
- **Leduc M3:** implemented as an independent exact 120-deal/288-infoset tree;
  full-tree LCFR, exact value/BR/exploitability and bit-exact resume pass the
  frozen gate. This strengthens the toy algorithm evidence only.
- **Common integration M3:** the A2 policy entry depends on the content-bound
  Common state/action/legal-set and one-shot session. It rejects stale/copied
  state, legality disagreement, and unavailable blueprint mass. This is an
  interface gate, not a complete TCP Bot or match result.
- **A2 M4 prototype:** coarse Leduc-to-national projection and a native packaging
  shell exist, but they are explicitly not a HUNL blueprint or complete Bot.
- **HUNL training/native TCP completion:** not started.
