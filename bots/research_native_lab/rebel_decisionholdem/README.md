# Route A research package

This directory is the isolated research area for two separate candidates:

- A1: a ReBeL-like clean-room reproduction;
- A2: a DecisionHoldem-like clean-room reproduction.

The current deliverable is the first correctness milestone only. It contains an
exact Kuhn poker model, exact best-response/exploitability evaluation, an A1
paper-shaped marginal-range PBS update loop backed by a separate exact joint
toy oracle, and an A2 alternating Linear CFR plus a Coin Toss plain/safe
resolving oracle. It is not a HUNL bot, contains no neural model, and must not
enter the `national_v*` lifecycle.

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

`common_runtime/` contains only the toy game and exact evaluation. A1 and A2 do
not import each other's strategy code. National TCP/rule contracts belong to
the separately owned `common_contracts/` branch and are not duplicated here.

The next permitted work is to extend the small-game validation toward exact
counterfactual values, a documented resolving gadget, and Leduc. HUNL training
remains forbidden until the shared national contracts and the preceding route
gates pass.
