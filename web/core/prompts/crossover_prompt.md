<instructions>
You are the Crossover & Mutation Engine for an evolving Texas Hold'em AI population.
Generate a new poker bot (Child) from TWO elite parent bots. Use Read and Bash tools. Do not use webReader, web-search, file:// URLs, or GitHub URLs.
</instructions>

<data_context>
Read `web/core/results/head_to_head.json` to understand each parent's strengths/weaknesses against specific opponents. Find matchups where one parent loses (WR < 40%) and the other wins (WR > 55%). If Parent B beats opponents that Parent A loses to, strongly consider importing Parent B's approach for those matchups. Read `web/core/results/bot_stats.json` for overall win rates.
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
- **Parent A (Alpha)**: `bots/claude_v{parent_a_version}/`
- **Parent B (Beta)**: `bots/claude_v{parent_b_version}/`
</parents>

<action>
1. Read both parent bots' source code
2. Design crossover + mutation strategy based on H2H data and code analysis
3. Write the full Python code into `bots/claude_v{version}/`
4. Run quality checks:
   - `python -m py_compile bots/claude_v{version}/main.py`
   - `python web/core/smoke_tester.py bots/claude_v{version}/main.py`
5. The bot must output `{"response": int}` via stdout. Action encoding: 0=call/check, -1=fold, -2=all-in, >0=raise (additive). Game rules: dealer=SB, postflop BB acts first, 70 hands/match, 20000 starting chips, 50/100 blinds.
</action>
