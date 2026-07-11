# Neural National v106-v107 Report

Date: 2026-07-06

## Goal

Continue the native national TCP neural route without overwriting old bots. The
focus was to explain why v102/v105 still lost to the `.evolution_pok` strong
rule pool, then create independent follow-up versions.

All evaluations in this report used native national TCP bot entrypoints. No
national adapter path was used.

## Infrastructure

Added `bots/neural_national_lab/tools/analyze_native_tcp_trace.py`.

The tool reads `native_tcp_evaluate.py --trace-decisions` JSON files and:

- extracts candidate `decision_trace` rows from paired native TCP matches,
- recomputes the version-local neural policy and multi-action value scores,
- separates neural changes from `sanitize_action` changes,
- summarizes interventions by opponent, street, action flow, and hand outcome,
- keeps compact best/worst neural-change examples.

Also fixed `web/core/national_native.py` trace execution. The native runner used
`subprocess.PIPE` for bot stderr/stdout and only drained after the match. Large
decision traces could fill stderr pipes, block the bot, and create artificial
timeouts. The runner now writes bot stdout/stderr to temporary files and reads
them after process exit, so trace collection no longer changes match behavior.

## Trace Findings

Same seed block: `2026073700`, v2/v3/v5/v14, 1 paired match per opponent.

| Version | Net | Neural Changes | Finding |
|---|---:|---:|---|
| v102 | `-61633` | 26/474 | v2/v3 changes were positive, v14/v5 changes were negative. |
| v105 | `-61919` | 4/459 | Almost no v2/v3 neural coverage; losses were mostly unchanged rule decisions. |
| v106 | `-58075` | 10/460 | Preserved v2/v3 low-aggression changes and removed v14/v5 pollution. |
| v107 | `-2765` | 34/538 | Added turn large-commit value veto; v3 became positive, v2 deficit shrank. |

Important distinction: `final_changed` is not the same as a neural change.
Many final changes are the national-protocol sanitizer adjusting raise totals to
legal amounts. The analyzer reports `neural_changed` separately.

## v106

Path:

`bots/neural_national_lab/versions/v106_national_v17_v2v3_profile_guard_tcp`

Design:

- Derived from v102.
- Keeps the v102 hard-negative multi-action value model.
- Adds profile scope to multi-action proposals:
  - at least 8 observed opponent actions,
  - opponent `raise_rate <= 0.30`.

Reason:

- v102's v2/v3 neural changes were positive on trace: `+700` vs v2 and `+700`
  vs v3.
- v102's v14/v5 neural changes were negative: `-2984` vs v14 and `-2984` vs v5.
- The bad v14/v5 cases had high observed raise rates or insufficient profile
  evidence; the good v2/v3 cases were low aggression after enough observations.

Current-top8+v7, seed block `2026073700`, 27 paired matches / 3780 hands:

- v102: `-186443`
- v105: `-200929`
- v106: `-173990`

v106 improved by removing broad pollution, but v2/v3 remained heavily negative.

## v107

Path:

`bots/neural_national_lab/versions/v107_national_v17_large_commit_value_veto_tcp`

Design:

- Derived from v106.
- Keeps v106's profile guard.
- Adds `large_commit_veto_enabled`.
- On turn/river, if the rule line continues/all-ins facing a huge call, the
  same multi-action value head can veto to fold when:
  - `to_call >= 8000`,
  - `pot >= 18000`,
  - rule label is `call` or `allin`,
  - learned rule value is at most `-0.20`,
  - best learned label is at least `0.45` above the rule label.

Reason:

- v106's v2/v3 losses were dominated by a few turn large-commit decisions.
- The trace condition found exactly four such decisions in the v2/v3/v5/v14
  trace set, all against v2/v3, all on `-20000` hands.
- Single-action force probes showed folding those decision classes recovered a
  large share of the deficit on the same native TCP seed.

## Evaluation

Current-top8+v7 pool:

- `.evolution_pok/bots/national_v2`
- `.evolution_pok/bots/national_v3`
- `.evolution_pok/bots/national_v5`
- `.evolution_pok/bots/national_v7`
- `.evolution_pok/bots/national_v8`
- `.evolution_pok/bots/national_v9`
- `.evolution_pok/bots/national_v14`
- `.evolution_pok/bots/national_v15`
- `.evolution_pok/bots/national_v16`

Seed block `2026073700`, 27 paired matches / 3780 hands:

| Version | Net | Mean/Hand | W-L-D | Compliance |
|---|---:|---:|---:|---|
| v102 | `-186443` | `-49.324` | 0-13-14 | 27/27 |
| v105 | `-200929` | `-53.156` | 0-20-7 | 27/27 |
| v106 | `-173990` | `-46.029` | 0-6-21 | 27/27 |
| v107 | `+24034` | `+6.358` | 4-2-21 | 27/27 |

v107 per-opponent on `2026073700`:

- v2: `+11745`, W-L-D 2-1-0
- v3: `+12289`, W-L-D 2-1-0
- v5/v7/v8/v9/v14/v15/v16: all `0`, W-L-D 0-0-3 each

Holdout seed block `2026073800`, same pool, 27 paired matches / 3780 hands:

- v107: `+22295`, mean/hand `+5.898`, W-L-D 13-1-13
- v2: `+10354`, W-L-D 3-0-0
- v3: `+11091`, W-L-D 3-0-0
- v5: `-50`, W-L-D 1-1-1
- v7/v8/v9/v14/v15/v16: each `+150`, W-L-D 1-0-2

Protocol compliance for v107:

- `0` candidate illegal actions.
- `0` candidate timeouts.
- `0` adapter actions.
- `27/27` compliant matches on both `2026073700` and `2026073800`.

## Verdict

v107 is the strongest neural-national artifact in this sequence so far. It is a
clear, reproducible native TCP performance improvement over v102/v105/v106 on
the current strong rule pool and on a holdout seed block.

It is not yet comprehensive rule-bot domination. The current behavior is closer
to: beat v2/v3 on these seed blocks, draw most other strong rule bots, and avoid
broad pollution. The next route should enlarge holdout evaluation, stress-test
the large-commit veto on more seeds, and collect counterfactual data for non-v2
opponents so the bot can move from draw-heavy to clearly positive across the
whole pool.
