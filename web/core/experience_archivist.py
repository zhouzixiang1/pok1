"""Experience pool consolidation and archivist analysis.

Experience consolidation runs every 3 generations to deduplicate the pool.
Archivist analysis runs conditionally after commit to assess generation quality.
"""

import json

from evolution_infra import (
    run_claude_query, parse_json_output,
    locked_file, get_logs_dir,
    PROMPTS_DIR, EXPERIENCE_FILE, ARCHIVE_DIR,
)


async def _consolidate_experience_pool(ui):
    """Use LLM to deduplicate and consolidate the experience pool.

    Reads the current experience_pool.md, asks LLM to merge redundant entries,
    and writes back a consolidated version. Runs every 3 generations.

    Strategy: ask LLM to output the consolidated text directly (not edit in-place),
    then write it back here as a guaranteed fallback. The LLM's text output is the
    source of truth — no dependency on the agent using Edit tool.
    """
    if not EXPERIENCE_FILE.exists():
        return

    with locked_file(EXPERIENCE_FILE, "r") as ef:
        content = ef.read()
    if not content or len(content.split("\n")) < 20:
        return  # Too short to bother consolidating

    # Load template and substitute
    template_file = PROMPTS_DIR / "experience_consolidator.md"
    if not template_file.exists():
        return
    consolidate_prompt = template_file.read_text()
    consolidate_prompt = substitute_template(consolidate_prompt, {
        "pool_content": content,
        "exhausted_directions": "",
    })
    log_file = get_logs_dir(0) / "experience_consolidation_io.txt"

    try:
        ui.clear_io()
        output, _, _ = await run_claude_query(
            consolidate_prompt, [], ui,
            "EXPERIENCE CONSOLIDATOR", log_file,
        )
        consolidated = output.strip() if output else ""
        # Strip accidental code fences if LLM added them
        if consolidated.startswith("```"):
            lines = consolidated.split("\n")
            consolidated = "\n".join(
                l for l in lines if not l.strip().startswith("```")
            ).strip()

        if consolidated and len(consolidated) > 50:
            tmp = EXPERIENCE_FILE.with_suffix(".tmp")
            tmp.write_text(consolidated + "\n", encoding="utf-8")
            tmp.replace(EXPERIENCE_FILE)
            ui.log_history("Experience pool consolidated and written back.", "success")
        else:
            ui.log_history("Experience pool consolidation produced no output — skipping write.", "warn")
    except Exception as e:
        ui.log_history(f"Experience pool consolidation failed: {e}", "warn")


async def _run_archivist_analysis(version, source_v, snapshot, ui):
    """Run Cycle Archivist LLM analysis on a completed generation.

    Called conditionally (rating decline, experience pool growth, or forced).
    Returns a JSON dict with assessment and strategic advice.
    """
    prompt_file = PROMPTS_DIR / "archivist.md"
    if prompt_file.exists():
        prompt = prompt_file.read_text()
    else:
        prompt = (
            "You are the Cycle Archivist for a poker bot evolution system.\n"
            "Analyze the completed generation and provide a strategic assessment.\n\n"
            "## Archive Snapshot\n{snapshot}\n\n"
            "Output ONLY a JSON block:\n"
            "```json\n"
            '{"generation_assessment": "improvement|neutral|regression", '
            '"archive_notes": "brief summary of what this generation achieved", '
            '"experience_updates": ["lesson to add to experience pool"], '
            '"strategic_advice": "suggestion for next generation"}\n'
            "```\n"
        )

    prompt = prompt.replace("{snapshot}", json.dumps(snapshot, indent=2, ensure_ascii=False))

    # Build recent rating trend context
    trend_lines = []
    for check_v in range(max(1, version - 4), version + 1):
        check_archive = ARCHIVE_DIR / f"v{check_v}.json"
        if check_archive.exists():
            try:
                with open(check_archive, "r") as f:
                    s = json.load(f)
                r = s.get("rating", {}).get("r", "?")
                wr = s.get("h2h_avg_wr", "?")
                trend_lines.append(f"v{check_v}: r={r}, h2h_avg_wr={wr}")
            except Exception:
                pass
    if trend_lines:
        prompt += f"\n\n## Recent Rating Trend\n" + "\n".join(trend_lines)

    log_file = get_logs_dir(version) / "archivist_io.txt"
    try:
        output, _, _ = await run_claude_query(
            prompt, [], ui,
            "CYCLE ARCHIVIST", log_file,
            tools=["Bash", "Read"],
        )
        data = parse_json_output(output)
        if data and isinstance(data, dict):
            return data
        return {"archive_notes": output[:300] if output else "No output"}
    except Exception as e:
        return {"error": str(e)}
