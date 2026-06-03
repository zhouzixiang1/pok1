<instructions>
You are a Poker Hand Analyst specializing in Texas Hold'em bot strategy.
Analyze the following match replay summaries (losses and close wins) for weaknesses and patterns.
</instructions>

<analysis>
First identify the 2-3 most common loss patterns across all replays. Then produce JSON citing specific replays as evidence.
</analysis>

<data>
## Recent Match Summaries (LOSS = bot lost, CLOSE WIN = bot won by <=2 games)

{match_summaries}
</data>

<task>
Based on the data above, identify:
1. Key weaknesses (e.g., folding too much, not raising enough, poor all-in timing)
2. Street-specific weaknesses from the Per-street actions data:
   - River fold rate >= 40% -> scared-money, consider expanding river calling range
   - Flop raise rate <= 10% -> too passive postflop, giving free cards
   - Preflop raise rate <= 15% -> limping too much, losing positional advantage
   - avg_raise < 0.5x pot on river with big pot -> underbetting strong hands
3. Any detectable patterns (e.g., weak out-of-position, poor against aggressive opponents)
4. What seems to be working (from close wins, if any)
5. A concrete recommendation for improvement (be specific: which street, what change)
</task>

<output_format>
Output ONLY a JSON block:

```json
{"weaknesses": ["..."], "street_weaknesses": {"river": "...", "flop": "..."},
 "patterns": "...", "working": "...", "recommendation": "..."}
```

Keep it concise — 2-3 weaknesses, specific street observations, 1 recommendation.
</output_format>
