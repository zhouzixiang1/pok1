# Battle Memory Lesson Flow

The evolution system now treats battle experience as a structured data flow, not
as a single markdown scratchpad.

Runtime files under `web/core/results/`:

- `battle_evidence.jsonl`: deterministic replay evidence extracted before any
  LLM call. Rows include `evidence_id`, bots, sample size, win rate, chip EV,
  action counts, street action counts, and spot tags.
- `battle_pending_summaries.jsonl`: replay summaries whose evidence has been
  captured but whose LLM lesson extraction is still pending or skipped.
- `battle_lessons.jsonl`: structured lessons with `lesson_id`, `evidence_ids`,
  scope, confidence, status, and lesson text.
- `battle_experience.md`: legacy markdown compatibility output from the older
  incremental LLM path.

Important behavior:

- If `POK_BATTLE_EXPERIENCE_LLM=0`, replay summaries are still preserved as
  evidence and pending summaries. Matches are marked `summary_ready`, not
  `done`, so marker state remains honest while avoiding duplicate replay
  parsing.
- `run_master` and generation selection inject source-aware structured battle
  memory. Master prompts must cite relevant `lesson_id` / `evidence_id` when
  using battle experience as a planning basis.
- Worker prompts treat cited battle IDs as the scoped evidence contract. A
  single pending summary is not enough justification for a broad rewrite unless
  supported by H2H, replay spotlight, or repeated evidence.

Design sources inspected locally:

- `ref/llm_evolution/experience_memory/reflexion`
- `ref/llm_evolution/experience_memory/ExpeL`
- `ref/llm_evolution/experience_memory/voyager`
- `ref/llm_evolution/experience_memory/Deep-CFR`

