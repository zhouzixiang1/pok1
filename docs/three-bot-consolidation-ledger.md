# Three-bot consolidation ledger

Status date: 2026-07-15

This ledger binds the provenance of the A1, A2, and B research work without
making that research tree active. The consolidation branch starts from
`origin/main@8df50853bd3d57ff07e6e768f7cae1b8ae12eedb`.

## Preserved history

The following source tips and exact dirty-worktree snapshots are ancestors of
this branch:

| Scope | Preserved object |
| --- | --- |
| Native evaluation contracts shared by all three routes | `26e8c834b7a1c7852fa2283f76775d2c1ce7d163` |
| A1 ReBeL-like and A2 DecisionHoldem-like committed work | `420a781ce157f7686015d1b92fbaea07ce193082` |
| A1/A2 uncommitted native-entry snapshot | `e438aaa0993bfa48a9593d66be3d339b17d0b52f` |
| B CFR/neural-search committed work | `181378f94db19606711c6866f23c00003a9d22bb` |
| B runtime, match-controller, and opponent-tracker snapshot | `446d791af373b5ecb764d6d349b588dca0340299` |
| TCP seeded-deck and research-evaluation script snapshot | `e653a9297dabf8c66a5a948c3b67d0e0fdc69810` |
| Strict-bootstrap prompt-evidence task | `fc7d62d30783d2ae8710dc8f331d717f3d902e36` |
| Retired neural-advice task, history only | `35f96e237da0cab30dea2840b1558b031f4e45c4` |

Each source was merged with Git's `ours` strategy. This deliberately records
the complete commit graph while retaining the current strict active tree. A
normal merge would have activated 307 files under
`bots/research_native_lab/` (about 62.5 MB), including candidate-owned TCP
paths, helpers, and large assets that violate the current five-file candidate
ABI. `bots/research_native_lab/` therefore does not exist in this branch's
working tree.

The snapshot commits are exact preservation objects. They are not reviewed or
certified releases. In particular, the A1/A2 snapshot retains its original
whitespace defects rather than silently rewriting the source worktree.

## Explicit exclusions

- `pok-arena` remains an independent, remote-backed orphan branch and worktree.
  It is retained and is not a merge input.
- The autonomous `.evolution_pok` checkout remains runtime-owned. No candidate,
  checkpoint, rating, or live result was copied from it.
- Ignored evaluation output, temporary model files, and local secret material
  are not captured.
- The one retired neural task is ancestry-only; no retired neural file appears
  in the active tree. All legacy-untrusted content remains outside the active
  trust boundary.
- `codex/national-protocol-evolution-alignment` is still being completed in its
  owning worktree and remains an independent retained branch. It is not a
  three-bot implementation or a content merge input here.

## Delivery finding

This branch is a safe consolidation point, not a declaration that three bots
have been delivered. The preserved implementations are not strict
`bots/national_v<N>/` five-artifact candidates and cannot currently be rated or
certified by the active control plane.

The audit found release-blocking gaps in every route:

- A1's range/search path has an unhandled indexing failure and depends on a
  missing deployment model; its online-search closure is not proven through
  the strict policy ABI.
- A2's live blueprint lookup and resolver path do not establish working plain
  or safe resolve behavior through the strict runtime.
- B's opponent tracker and 70-hand controller are not wired into live policy
  decisions, the online solver is not called by the native entry path, and its
  deployment model and budget enforcement are incomplete.
- The research evaluation scripts do not yet satisfy the authoritative
  hand-70 settlement contract, all four decision budgets, paired-block
  inference, confidence intervals, Holm correction, or complete resource and
  latency reporting.

Consequently there is no defensible A1-versus-A2-versus-B recommendation yet.
Any later comparison must first migrate each route into three independent
strict candidates, bind system-owned assets, and run the same immutable native
TCP evaluation contract at 250 ms, 5 s, 20 s, and 50 s.

## Next integration gate

Before content from a preserved source becomes active, it must be selectively
ported and pass all of the following:

1. exactly five candidate artifacts and no candidate-owned I/O or socket path;
2. static and dynamic national capability checks;
3. focused native protocol tests, including natural hand 70;
4. identical resource envelopes and paired seed blocks for all three routes;
5. frozen H2H, pool, heldout, stable-anchor, nemesis, and ablation evidence;
6. confidence intervals, family-wise Holm correction, latency tails, and
   CPU/GPU, memory, and disk accounting;
7. signed official certification before any strength recommendation.
