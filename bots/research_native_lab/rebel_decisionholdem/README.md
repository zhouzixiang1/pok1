# Route A research package

This directory is the isolated research area for two separate candidates:

- A1: a ReBeL-like clean-room reproduction;
- A2: a DecisionHoldem-like clean-room reproduction.

The current deliverable is the M0--M3 small-game correctness package. It contains
exact Kuhn and two-round limit-Leduc trees, exact best-response/exploitability
evaluation, an A1 paper-shaped marginal-range PBS update loop backed by a
separate exact joint toy oracle, A2 alternating Linear CFR, and a Coin Toss
plain/safe-resolving falsifier. It also contains a deliberately labelled M4
prototype that projects the Leduc policy into a coarse national abstraction and
packages a native TCP entry. That projection is **not** a trained HUNL blueprint,
not a safe resolver, and not a complete A2 candidate. There is no neural model,
and nothing here may enter the `national_v*` lifecycle.

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
A1 and A2 do not import each other's strategy code. The provisional
`decisionholdem_like/native_entry.py` exercises packaging, sticky framing,
street-order and all-in-runout behavior, but it is not the authoritative national
state implementation. M4 must replace or bind that shell to the separately owned
and frozen `common_contracts/` state/rule oracle before a complete-match claim.

The next work is M4: train a real HUNL abstraction rather than the Leduc seed
projection, preserve regrets separately from average strategy, integrate the
shared national contracts, run complete 70-hand blueprint-only matches, and
freeze abstraction/asset hashes. Plain and safe online resolving, off-tree
action injection and multi-range leaves remain later A2 work. Large training
must not begin until the shared national contracts and their differential gates
are integrated.
