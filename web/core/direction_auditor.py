"""Direction Auditor: detect repetitive evolution directions via LLM.

Checks recent git history, critic rejections, and master plans to determine
if the evolution system is stuck repeating the same approach.
"""

import json

from evolution_infra import (
    run_claude_query, parse_json_output,
    locked_file, get_logs_dir,
    PROMPTS_DIR, WORKER_FAILURES_FILE,
)
from output_schema import validate_agent_output


async def _run_direction_audit(source_v, ui):
    """Run Direction Auditor to detect repetitive evolution directions.

    Reads recent generation history (git tags, commit messages, critic rejections)
    and asks an LLM whether the evolution is stuck in a local optimum.

    Returns a dict: {repetition_detected, exhausted_directions, mandatory_constraints,
                     suggested_direction, confidence, last_directions}.
    Returns a safe no-repetition default on failure.
    """
    audit_prompt_path = PROMPTS_DIR / "direction_auditor_prompt.md"
    if not audit_prompt_path.exists():
        ui.log_history("Direction Auditor prompt not found — skipping audit.", "warn")
        return {"repetition_detected": False, "exhausted_directions": [],
                "mandatory_constraints": None, "suggested_direction": None,
                "confidence": "low", "last_directions": []}

    # ── Collect recent generation history ──
    history_lines = []
    try:
        from evolution_infra import _git, git_get_parent
        tag_output = _git("tag", "-l", "bot-v*", "--sort=version:refname", check=False)
        tags = [t.strip() for t in tag_output.splitlines() if t.strip()]
        recent_tags = tags[-6:] if len(tags) > 6 else tags

        for tag in recent_tags:
            v_str = tag.replace("bot-v", "")
            try:
                v = int(v_str)
            except ValueError:
                continue
            # Get commit message for strategy context
            try:
                msg = _git("log", tag, "-1", "--format=%s", check=False).strip()
            except Exception:
                msg = "?"
            # Get parent
            parent = None
            try:
                parent = git_get_parent(v)
            except Exception:
                pass
            parent_str = f" ← v{parent}" if parent else ""
            history_lines.append(f"  v{v}{parent_str}: {msg}")
    except Exception:
        pass

    # ── Collect recent critic/quality rejections ──
    rejection_lines = []
    try:
        if WORKER_FAILURES_FILE.exists():
            with locked_file(WORKER_FAILURES_FILE, "r") as f:
                entries = [json.loads(l.strip()) for l in f if l.strip()]
            for e in entries[-10:]:
                role = e.get("role", "?")
                gen = e.get("gen", "?")
                err = e.get("error", "")[:200]
                if "critic" in role.lower() or "reject" in err.lower():
                    rejection_lines.append(f"  v{gen} {role}: {err}")
    except Exception:
        pass

    # ── Collect recent master plan analysis (from pipeline logs) ──
    master_log_lines = []
    for check_v in range(max(1, source_v - 4), source_v + 1):
        log_file = get_logs_dir(check_v) / "master_io.txt"
        if log_file.exists():
            try:
                content = log_file.read_text()
                # Extract last "analysis" field from JSON output
                import re
                for m in re.finditer(r'"analysis":\s*"([^"]{0,300})', content):
                    pass
                if m:
                    master_log_lines.append(f"  v{check_v} Master: {m.group(1)}")
            except Exception:
                pass

    # ── Build generation_history for prompt ──
    gen_history = f"## Source version: v{source_v}\n\n"
    if history_lines:
        gen_history += "## Recent generations (commit messages):\n" + "\n".join(history_lines) + "\n\n"
    if rejection_lines:
        gen_history += "## Recent critic/quality rejections:\n" + "\n".join(rejection_lines) + "\n\n"
    if master_log_lines:
        gen_history += "## Recent Master analysis summaries:\n" + "\n".join(master_log_lines[-5:]) + "\n\n"
    if not history_lines and not rejection_lines:
        gen_history += "No recent generation history available.\n"

    # ── Call LLM ──
    audit_prompt = audit_prompt_path.read_text()
    audit_prompt = audit_prompt.replace("{generation_history}", gen_history)

    log_file = get_logs_dir(source_v) / "direction_audit_io.txt"
    try:
        output, _, _ = await run_claude_query(
            audit_prompt, [], ui, "DIRECTION AUDITOR", log_file,
        )
        data = parse_json_output(output)
        if data and "repetition_detected" in data:
            data, errors = validate_agent_output("direction_auditor", data)
            if errors:
                ui.log_history(f"Direction Auditor validation issues: {'; '.join(errors[:3])}", "warn")
            data.setdefault("exhausted_directions", [])
            data.setdefault("mandatory_constraints", None)
            data.setdefault("suggested_direction", None)
            data.setdefault("confidence", "low")
            data.setdefault("last_directions", [])
            return data
    except Exception as e:
        ui.log_history(f"Direction Auditor error: {e}. Skipping.", "warn")

    return {"repetition_detected": False, "exhausted_directions": [],
            "mandatory_constraints": None, "suggested_direction": None,
            "confidence": "low", "last_directions": []}
