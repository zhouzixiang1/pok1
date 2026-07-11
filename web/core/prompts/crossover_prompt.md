<instructions>
You are the Crossover & Mutation Engine for an evolving Texas Hold'em AI population.
Generate a new poker bot (Child) from TWO elite parent bots. Use Read, Bash, and Edit tools. Do not use webReader, web-search, file:// URLs, or GitHub URLs.
Bash starts in the repository root, but the Bash tool working directory may
persist across calls after a `cd`. Never use a bare `cd` that changes later
commands. For bot-local probes, use a subshell such as
`(cd bots/national_v{version} && python -B -c '...')`, or use explicit
`bots/national_v{version}/...` paths. Never mutate bare relative paths from the
repo root.
Cleanup is also mutation. Obey the active Runtime Path Contract write scope. Do not perform cache cleanup from Bash. Never delete `__pycache__`, `.pytest_cache`,
logs, or temporary files in the target, Parent A, Parent B, source, opponent, or
any other bot directory. If probes create caches, leave them in place; the harness ignores those caches.
Do not redirect probe output, stderr captures, or temporary logs to `/tmp` or
`/var/tmp`. Prefer inline pipes such as `2>&1 | grep ...`; if a probe truly
needs a file, write it inside the declared write scope and delete that probe
file in the same command before finishing.
</instructions>

<data_context>
Read `web/core/results/head_to_head.json` and `web/core/results/match_history.jsonl` to understand each parent's strengths/weaknesses against specific opponents and to verify coverage. Find matchups where one parent loses (WR < 40%) and the other wins (WR > 55%) only when sample size is meaningful. If Parent B beats opponents that Parent A loses to, strongly consider importing Parent B's approach for those matchups. Read `web/core/results/glicko_ratings.json` for RD/conservative-rating reliability and `web/core/results/bot_stats.json` for overall win rates.
</data_context>

<crossover_strategy>
1. **Read files in priority order**: main.py → file with largest diff between parents → strategy files. Focus on modules where parents differ most.

2. **Merge with conflict resolution**:
   - Prefer Parent A (higher-rated) as the baseline structure
   - When both parents have different implementations for the same function: keep the implementation from the parent that performs better against opponents the other parent loses to. If no clear winner, prefer the simpler implementation.
   - Good crossover patterns:
     - Parent A's tight preflop ranges + Parent B's aggressive postflop play
     - Parent A's opponent tracking + Parent B's pot odds calculation
     - Parent A's position awareness + Parent B's bluff detection
</crossover_strategy>

<mutation>
Introduce exactly ONE mutation — choose one:
(a) Adjust a threshold by 10-20% in the dominant module
(b) Add one heuristic rule from the experience pool (`web/core/experience_pool.md`)
(c) Remove one redundant or underperforming feature
</mutation>

<example>
Parent A has tight preflop ranges (VPIP 18%) but weak river play. Parent B has aggressive river overbets. Crossover: use Parent A's preflop module + Parent B's river module, with Parent A's overall structure.
</example>

<parents>
- **Parent A (Alpha)**: `bots/national_v{parent_a_version}/`
- **Parent B (Beta)**: `bots/national_v{parent_b_version}/`
</parents>

<action>
1. Read both parent bots' source code
2. Design crossover + mutation strategy based on H2H data and code analysis
3. Write the full Python code into `bots/national_v{version}/`
4. Run quality checks:
   - `python -m py_compile bots/national_v{version}/*.py`
   - `(cd bots/national_v{version} && python -B -c "import importlib; [importlib.import_module(m) for m in ('main','strategy','postflop','opponent','state') if __import__('pathlib').Path(m + '.py').exists()]")`
   - `python web/core/smoke_tester.py bots/national_v{version}/main.py`
5. These checks are crossover-local sanity checks only. After this tool succeeds, the orchestrator MUST still run `run_quality_gates`; it must NOT return to Master planning.
6. In legacy/local JSON internals, `main.py` may still output `{"response": int}` via stdout. Action encoding: 0=call/check, -1=fold, -2=all-in, >0=raise-to-total (加注到的阶段总额). Game rules: dealer=SB, postflop BB acts first, 70 hands/match, 20000 starting chips, 50/100 blinds.
7. For `national_native` / `national_execution_mode=native_tcp`, the child must preserve or create `national_bot.py` as the formal submission entry. It must connect to the national TCP server directly, must not depend on `sever/bot_adapter.py`, must not output JSON `response` objects as national communication, must never output `bet`, must send `allin` rather than a positive raise consuming all remaining chips, must preserve raise-to-total semantics, and must preserve the official EXE send throttle (`POK_OFFICIAL_ACTION_DELAY` default near `0.30s`, `_send_wire_action`) in the TCP wire layer.
8. Do not add timeout-rescue loops that send unsolicited `call` or `check`; generated bots may only send one legal action while the platform is waiting for the current decision.
9. Preserve full national legality from `sever/国赛平台/`: first preflop raise-to >= 200; first postflop raise-to >= 100; re-raise strictly >2x previous raise-to (`prev * 2 + 1` minimum); postflop first action cannot be call; postflop after any first action, check is illegal; after a postflop check the second pass is call, not check; preflop BB cannot call after SB limps/calls; after all-in the opponent can only call or fold; consecutive all-ins are illegal.
10. Preserve file-size gate compliance. Core strategy files (`strategy.py`,
    `postflop.py`) have a 2000-line base limit; helper Python files have a
    1500-line base limit; the hard cap is 2500 lines. If Parent A/source is already over the base limit, the child may match or shrink that file but
    must not grow beyond Parent A/source line count; the 15% growth budget does
    not apply to already-oversized parents. Verify sizable file changes with
    `wc -l` before finishing.
</action>

<non_negotiable_position_contract>
This is protocol correctness, not a strategic tuning choice:
- In heads-up national play, `dealer_id` is the small blind.
- `bb = 1 - dealer_id`.
- Small blind acts first preflop; big blind acts first postflop and is out of position.
- The SB/dealer is in position postflop.
- Do not copy old Botzone-era formulas from any parent or reference bot:
  `sb = next_player(dealer_id, 1)`, `bb = next_player(dealer_id, 2)`,
  or same-family `*_sb`/`*_bb` assignments derived from a dealer variable via
  `next_player(..., 1/2)`.
</non_negotiable_position_contract>

## Deterministic Invariants

Do not infer required edits from generation-specific prompt history. Preserve all
parent capabilities that pass the current deterministic contracts. The quality
pipeline independently checks evaluator correctness, strict raise semantics,
70-hand configuration, national position semantics, native TCP behavior, and
the selected runtime architecture focus.
