# LLM Evolution Stages

This document describes the active raw national-TCP evolution pipeline. It is
an operational contract, not a history of older generations.

## One active protocol

Generated bots are native clients for the national competition TCP protocol.
The system-owned entry point owns sockets, stream splitting, authoritative game
state, action validation, deadlines, process isolation, and official send
throttling. Candidate code owns poker policy only.

The candidate ABI is:

```python
def get_baseline_decision(context): ...

def iter_decisions(context, baseline, deadline): ...
```

Candidate decisions are typed mappings:

```text
{"kind": "pass"}
{"kind": "fold"}
{"kind": "allin"}
{"kind": "raise", "raise_to": 400}
```

`pass` is deliberately semantic. The socket owner maps it to the legal wire
token `check` or `call` from authoritative state. Candidate code cannot emit a
wire token directly. Integer actions, strings, `call`/`check` intents, and
malformed mappings are rejected and replaced by the system fallback.

The policy receives a schema-versioned `decision_context` containing cards,
hand/street identity, authoritative betting and stack values, semantic history,
line flags, legal actions, opponent evidence, and a monotonic deadline. It does
not reconstruct state from a parallel request history.

Historical JSON bots, adapters, local subprocess engines, RL experiments, and
their analyses live under `archive/`. They are not executable dependencies,
prompt evidence, parents, rating opponents, or gate inputs.

## Runtime ownership

```text
official EXE / local national server
              |
        raw TCP byte stream
              |
  system-owned national_bot runtime
     | parser + boundary inference
     | authoritative state/tracker
     | decision_context builder
     | timeout/process supervisor
     | typed-intent validator
     | wire action mapper/throttle
              |
       candidate policy process
          policy.py only
     (no candidate helpers/assets)
```

The official Windows EXE is the protocol authority. The local `sever/` stack is
for deterministic development and native strength matches; disagreement is
resolved in favor of the official captures and the two pinned oracle documents.

## Frozen planning evidence

At generation start, the scheduler freezes one content-addressed evaluation
bundle. Source selection, direction audit, planning, workers, review, and
precommit all consume that same cutoff. They do not reopen mutable daemon files.

During the strict epoch bootstrap, historical experience is represented by an
explicit empty evidence envelope. Old battle summaries, experience pools,
spotlights, worker-failure notes, and neural experiment reports cannot be
silently relabeled as current evidence.

The control API exposes status and current evidence inspection, but it has no
standalone live-data match/performance/stagnation analyst. A missing current
H2H matrix remains empty instead of falling back to daemon pair counters.

There is no active cross-generation experience LLM stage. The identity-bound
native replay-memory kernel is internal storage only and is not injected into
Master or Worker prompts. Free-form Markdown consolidation, background
experience analysis, attribution, guardian-note replay, and keyword-derived
hard bans are retired. The Cycle Archivist produces a content-bound archive
annotation that is never fed back into planning.

## Generation timeline

1. **Prepare** creates a fresh candidate artifact from the current strict
   policy baseline. The first strict candidate is materialized from system-owned
   runtime assets plus a fresh `policy.py`; old bot bytes are lineage metadata,
   not a code parent.
2. **Direction audit** identifies one falsifiable policy opportunity using only
   frozen, typed evidence.
3. **Literature probe** runs only when stagnation policy requests it. Its output
   is advisory and must be converted into a testable local hypothesis.
4. **Master proposal ensemble** produces independent proposals. Deterministic
   validation checks source symbols, call leaves, file authority, expected
   behavior, and a falsifier. Anonymous ballots choose exactly one proposal.
5. **Plan compilation** converts the selected proposal into immutable Worker
   contracts. The LLM cannot expand its own write scope or substitute live
   evidence.
6. **Workers** edit isolated lease-epoch workspaces. Each output is snapshotted,
   content-addressed, checked against its declared files, and materialized
   atomically. Candidate code may edit only `policy.py`; helper modules and
   candidate-owned packaged assets are outside the five-file ABI.
7. **Quality gates** verify artifact closure, stdlib portability, sandboxed
   execution, typed intents, raw-stream behavior, official action semantics,
   reachability, telemetry fidelity, and decision deadlines.
8. **Review** checks the actual prepared-to-final artifact and gate evidence.
9. **Critic** supplies schema-valid advisory strategy analysis. Its score is not
   an acceptance threshold.
10. **Precommit evaluation** runs complete 70-hand native TCP matches. Match
    win/draw/loss from final net-chip sign is primary; chip magnitude is only an
    equal-primary-score tie-breaker.
11. **Official certification** runs the durable signed EXE policy: five
    70-hand self-play rounds and three 70-hand rounds against an eligible
    opponent. Official chip results have zero strength weight.
12. **Publication** validates candidate bytes, certificate, staged Git blobs,
    immutable tag tree, and remote authority before one publication
    transaction creates the commit and `national-bot-v<N>` tag.
13. **Archivist** records only the content-bound archive snapshot and
    annotation; it cannot produce future planning advice or lessons.

## LLM trust boundary

LLMs may propose policy changes and edit files granted by a compiled Worker
contract. They do not own:

- protocol parsing or sticky-packet splitting;
- authoritative chips, pot, street closure, or action legality;
- subprocess or socket lifecycle;
- evaluation schedules, ratings, certificates, signatures, or publication;
- evidence identity, generation cutoffs, task scope, or retry state;
- system runtime and packaged asset manifests.

Schema validation, content digests, capability checks, sandbox execution, and
replayable workflow journals enforce those boundaries. A model failure pauses
or retries a stage; it never converts missing evidence into a pass.

## Crash and retry behavior

The generation checkpoint has an immutable workflow run ID and monotonic CAS
revision. Worker effects use fenced leases and a SQLite-WAL journal. Official
certification and publication use durable intents. Recovery replays frozen
inputs and verifies current artifact hashes before resuming; it does not rebuild
a plan from mutable live files.

An infrastructure change is reconciled only when the exact current evaluation
contract proves it neutral. A changed candidate, parent, opponent, parser,
runtime, oracle, or gate input abandons the stale attempt and starts from a new
baseline.

## Evaluation authority

- **Local national TCP:** strategy strength and deterministic regression.
- **Official Windows EXE:** submission protocol legality and formal completion.
- **Web Arena:** presentation and diagnostics only.
- **Archived systems and data:** historical provenance only.

One local strength sample is one complete 70-hand native match. Partial matches,
individual hands, Arena results, official settlement amounts, and archived
results never update Glicko or head-to-head strength.

## Operator entry points

```bash
python web/main.py
python web/core/orchestrator.py --one-gen
python web/core/elo_daemon.py --once
python -m pytest sever/tests -q
python scripts/official_certify.py full bots/national_v<N> --wait-if-busy
```

The operator checkout and autonomous evolution checkout are synchronized only
through `origin/main`; see `docs/evolution-dual-checkout-sync-policy.md`.
