# Official 2021 called-all-in wire runout oracle — 2026-07-19

This document records a production official-EXE wire behavior. It has
compliance and replay authority only. It has zero strategy, rating, H2H, or
strength weight.

## Frozen provenance

- Official EXE SHA-256:
  `9d01b443d4920a7e06a487d87ea1b050ea2ca5359023602f98c3c236c734e81a`.
- Initial formal bootstrap job:
  `37bc2c6555b516b6568f45c85cdf8b9e23b0c06e6bbca207d5367a561759dae6`.
- Initial durable job result digest:
  `c055966f5385fd921ece46920202a70477522d700bea106dd45cc1bae3196f9a`.
- Contract-41 correction job:
  `b4575bb7163f551cb586f6391f728c1e6dc1671b11a279a4392504af8a4c7ebf`.
- Correction-job result digest:
  `fb7846b74c7c237226b99d2b4e8647c8b82ad9801917e59baceadd8d83424ce1`.
- Candidate artifact:
  `f4e7b845a9bc18827532208556b67b76c2ecbb63baf9d2cf8a2a65ef7a54ca50`.
- First-strict control artifact:
  `1cfe42b96566017ba470573b0aa9bc46a992c966779ff63db2470248d7440db2`.
- Machine-readable fixture:
  `sever/tests/fixtures/official_allin_runout_wire_oracle_20260719.json`,
  SHA-256
  `a81c804d1940437fb259d0119c7bc1b06e968fcd5f20eb4364ab3f594156ef48`.

The job naturally completed all eight requested rounds. Five complete 70-hand
rounds passed. Three rounds stopped only because the then-current replay
incorrectly required five observed public cards before every `oppo_hands`.
Attribution classified the outcome as non-blocking harness infrastructure and
the candidate verdict as inconclusive. No certificate was produced and the
first-strict control was not consumed.

The correction job also completed all eight requested rounds. Two rounds
passed under Contract 41. Four short rounds were stopped by a live-observer
race: delimiter-free `call`/`check` bytes had reached the EXE, which sent the
next street, while the recorder's bounded idle flush had not yet emitted the
semantic action. Every finalized causal replay was issue-free. Two complete
70-hand rounds proved that the official THP, like the wire, may omit the
not-yet-sent runout suffix after a called all-in. Contract 41 incorrectly
required five THP public cards and timed out those rounds. The correction job
produced no certificate, did not consume the first-strict control, and has zero
strength or rating authority.

## Direct observations

The causally ordered raw recorder captured the same terminal sequence at three
different streets:

| Round / hand | Last public street | Observed board cards | Raw wire SHA-256 |
|---|---:|---:|---|
| `self_play_01` / 4 | turn | 4 | `fdf8356114caf957d2c871ed3c6273e4837d2f4c0b6982a4c6b52e4f0ea07e08` |
| `self_play_02` / 10 | flop | 3 | `0d098d48fcb09b4a98de6c8742a3e53b4485461a404dafd7806785d814d0d77e` |
| `opponent_01` / 2 | preflop | 0 | `5fd8a7dc035878fd2cd52c78048c59c0a264ead7bc615b22d82100cd2d5b11b7` |

Each sequence was:

```text
allin → call → earnChips(A) → earnChips(B)
       → oppo_hands(A/B) → next preflop
```

The EXE did not relay any not-yet-observed flop, turn, or river cards. Before
the reveal, each connection-local replay proved adjacent opposing-actor
`allin → call`, no fold, both stacks zero, equal street contributions, pot
40000, and that connection's current-hand settlement. The two captured
connections then cross-bound the same hand/street/board prefix, the initiator
and responder views of the same raw actions, exact all-in net settlements
(`+20000/-20000` or `0/0`), and each revealed holding to the peer's private cards. Bot logs,
stdout/stderr, and the replay prefix before `oppo_hands` contained no issue;
decision latency stayed below five seconds.

The correction job added two complete-round THP observations:

| Round / hand | Called-all-in street | Wire cards | THP cards payload | THP SHA-256 | Wire SHA-256 |
|---|---:|---:|---|---|---|
| `self_play_03` / 6 | turn | 4 | `4h5s\|4s8d/AcAd3s/4d` | `a9516d06d7e7b093c24468e35376f519cb51df83166a128902049050860e1aab` | `e9882362fd1113ec4e5d219a41a364b14785332a1b63280ec853cbf8d689ad54` |
| `opponent_01` / 1 | flop | 3 | `AcTh\|TdKd/9sAdKh` | `3e8fd8ab81e6e96fedb10c67474851d8bb4bb59bf9ffc372a469ea6eae514d59` | `a9f2f11940313dff15e8a23328d2748a0a717e8e6da1542e6046a02958a4ee56` |

Both THP files contain all 70 ordered `STATE` records, exact named all-in
earnings and the official footer. Their public-card payload stops at the exact
wire-observed prefix; it does not contain the unsent suffix. This direct
evidence supersedes the initial, unproved assumption that THP always retains
five public cards after an omitted called-all-in runout.

## Production interpretation

The 2021 EXE deals the remaining board internally after a called all-in. It may
omit all not-yet-sent public-street messages and proceed directly to settlement
and showdown. A client must not act again. The system must not fabricate or
append the unseen board to wire evidence.

An incomplete-board `oppo_hands` is accepted only when all of these are proved:

1. The observed board is an exact legal prefix: preflop 0, flop 3, or turn 4.
2. The final two actions on the current street are adjacent `allin → call` by
   different actors; actions from a prior street cannot prove the terminal
   suffix, fold has not occurred, and every prior street is independently
   closed unless it is already a proved called-all-in runout.
3. Both stacks are zero, the street contributions are equal, and the pot is
   40000.
4. The connection received its current-hand settlement before its reveal,
   except for the independently known natural-hand-70 settlement omission.
5. Final replay cross-binds both connection records, the same hand/street/board
   prefix, complementary raw action provenance, an exact all-in net settlement
   pair (`+20000/-20000` or `0/0`), and both revealed holdings.
6. The strict official THP record for that same hand contains either the exact
   legal wire-observed public prefix (0, 3, or 4 cards) or a complete legal
   five-card board. No intermediate shape is accepted. It cross-binds
   the THP action sequence's terminal `c`, blind/name order, both revealed
   private holdings, the observed wire-board prefix, and all-in earnings. This
   requirement applies to every omitted runout, not only natural hand 70.

Socket scheduling may interleave one connection's reveal before the other
connection's settlement. Therefore the first locally proved reveal is
provisional until replay finalization cross-binds the peer record. Missing or
conflicting peer proof remains a formal wire issue.

A non-final causal replay may temporarily project
`provisional_street_boundary_unproved` only when the same connection has exact
raw `bot_to_server` action bytes awaiting its one bounded idle/EOF flush. The
warning disappears after a successful causal flush. A legacy capture, a
boundary without that pending source, an illegal flushed action, or any
unresolved finalized buffer remains a strict issue.

For a called-all-in natural hand 70 the wire proof remains provisional because
the EXE omits the final `earnChips` pair. Both `oppo_hands` reveals remain
mandatory. Formal certification must independently require the
strict canonical THP state 69, its exact wire prefix or complete five-card
board, exact all-in earnings, and the footer. A missing, malformed, or
mismatched THP record cannot be repaired by this wire rule.

## Local mirror and fail-closed boundary

`sever/engine/game.py` must deal the complete remaining board internally and
write it to THP/evaluator state, but must omit those future street messages from
the TCP clients before settlement/showdown. This keeps local protocol behavior
aligned with the official EXE while preserving complete internal game truth.

The following remain invalid:

- an incomplete board without a called all-in;
- a malformed board prefix, non-adjacent/unfinished all-in, or all-in followed
  by fold;
- an action suffix borrowed from a prior street, or any skipped/unclosed prior
  street before the all-in street;
- unequal live stacks/contributions or a non-40000 terminal pot;
- a reveal before the local terminal settlement outside natural hand 70;
- a missing, duplicate, conflicting, or colliding peer reveal;
- a non-zero-sum or zero-sum-but-impossible all-in settlement pair;
- two locally plausible records whose raw all-in/call provenance is not
  complementary across the two TCP connections;
- any omitted runout whose strict THP board is neither the exact observed
  prefix nor a complete legal five-card board, or mismatches blind/name order,
  terminal action, revealed holes, observed board prefix, or all-in earnings;
- hand-70 certification without strict THP state 69,
  exact-prefix-or-full-board proof, and footer proof;
- a called-all-in hand 70 without both wire showdown reveals.

This oracle cannot admit a strength sample. Only a complete content-bound
70-hand native TCP match may carry strength authority.
