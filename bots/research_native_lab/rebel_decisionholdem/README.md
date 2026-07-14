# Route A research package

This directory is the isolated research area for two separate candidates:

- A1: a ReBeL-like clean-room reproduction;
- A2: a DecisionHoldem-like clean-room reproduction.

The current deliverable is the M0--M3 small-game correctness package. It contains
exact Kuhn and two-round limit-Leduc trees, exact best-response/exploitability
evaluation, A1 Kuhn/Leduc public-belief and two-player range updates, both
posterior-normalized labels and standard unnormalized CFR action values, and A2
alternating Linear CFR checked against a structurally independent equation
reference. The source-shaped Coin Toss fixture proves that blueprint-reach
isolated solving can choose the paper's unsafe always-Heads strategy; its
alternative-payoff constraint is a **functional falsifier**, not a reproduction
of the paper's full Resolve augmented game or DecisionHoldem's resolver.

The A2 policy entry `CommonA2StrategyRuntime` now depends on the frozen Common
M0--M2 `NationalProtocolSession`, `NationalGameState`, `Action`, and
`LegalActionSet`. Route action candidates must all agree with Common; unavailable
blueprint probability mass and zero-valid-mass fallbacks fail closed rather than
being silently renormalized. Policy lookup uses within-hand information only;
`full_state_id` is retained solely for stale-send rejection. The exact Common
commit, critical files, and complete package tree are content-bound in the M3
manifest.

The retained coarse Leduc projection fails this strict entry because it assigns
some mass to an unavailable/aliased action. That tested rejection is an M4
blocker, not a reason to sanitize the policy into a playable claim.

The package also retains a deliberately labelled M4 prototype that projects the
Leduc policy into a coarse national abstraction and packages the older standalone
TCP shell. That projection is **not** a trained HUNL blueprint, not a safe
resolver, not the Common-authoritative policy entry, and not a complete A2
candidate. There is no neural model, and nothing here may enter the
`national_v*` lifecycle.

No DecisionHoldem AGPL source code was copied. The public repository was used
only to audit availability, symbol names, binary-only boundaries, assets, and
license obligations. See `reports/m0-source-fidelity.md`.

The DecisionHoldem paper/README opening says Linear CFR, but the same README's
framework section labels the blueprint code MCCFR and names files missing from
the committed tree. The toy LCFR result therefore does not remove the frozen
blueprint-fidelity gap.

## Reproduce the small-game gate

From the repository root:

```bash
python -m pytest bots/research_native_lab/rebel_decisionholdem/tests -q
python -m bots.research_native_lab.rebel_decisionholdem.tools.run_small_game_validation \
  --iterations 10000 --seed 19
python -m bots.research_native_lab.rebel_decisionholdem.tools.milestone_manifest
python -m bots.research_native_lab.rebel_decisionholdem.tools.train_blueprint \
  --iterations 100 \
  --checkpoint /tmp/route-a2-leduc-lcfr.json \
  --export /tmp/route-a2-prototype-export
```

Checkpoint/resume is explicit and deterministic:

```bash
python -m bots.research_native_lab.rebel_decisionholdem.tools.run_small_game_validation \
  --iterations 400 --checkpoint /tmp/route-a-kuhn-lcfr.json
python -m bots.research_native_lab.rebel_decisionholdem.tools.run_small_game_validation \
  --iterations 1200 --checkpoint /tmp/route-a-kuhn-lcfr.json --resume
```

Runtime outputs should go to a temporary or ignored experiment directory. Large
data and checkpoints must not be committed.

## Route boundary

`common_runtime/` contains only strategy-neutral toy games and exact evaluation.
A1 and A2 do not import each other's strategy code. The Common-authoritative M3
policy seam is `decisionholdem_like/common_native_entry.py`; it reconstructs
state and consumes one-shot decision leases through the separately owned
`common_contracts/` package. It is not yet a socket/deadline product. The older
`decisionholdem_like/native_entry.py` remains only an explicitly non-authoritative
packaging prototype and cannot support a complete-match claim.

The next work is M4: train a real HUNL abstraction rather than the Leduc seed
projection, preserve regrets separately from average strategy, productize the
Common-authoritative entry, run complete 70-hand blueprint-only matches, and
freeze abstraction/asset hashes. Plain and safe online resolving, off-tree
action injection and multi-range leaves remain later A2 work. This M3 gate does
not itself authorize large HUNL training; the parent comparison plan must first
accept both routes' integration evidence.
