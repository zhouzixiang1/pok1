"""Direction Auditor: detect repetitive evolution directions via LLM.

Checks recent git history, critic rejections, and master plans to determine
if the evolution system is stuck repeating the same approach.
"""

import json
import re

from evolution_infra import (
    run_claude_query, parse_json_output,
    locked_file, get_logs_dir,
    PROMPTS_DIR, WORKER_FAILURES_FILE, EXPERIENCE_FILE,
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
    # Use full commit body (%B) so the LLM can do semantic analysis
    # on rich strategy descriptions rather than just subject lines.
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
            # Get full commit body for richer context — LLM will parse semantically
            try:
                body = _git("log", tag, "-1", "--format=%B", check=False).strip()
                # Use first line as summary, keep full body for LLM context
                first_line = body.split("\n")[0] if body else "?"
            except Exception:
                body = ""
                first_line = "?"
            # Get parent
            parent = None
            try:
                parent = git_get_parent(v)
            except Exception:
                pass
            parent_str = f" ← v{parent}" if parent else ""
            # Include full body if it has multi-line strategy detail
            if len(body) > len(first_line) + 10:
                history_lines.append(f"  v{v}{parent_str}: {first_line}\n    {body[len(first_line):].strip()[:400]}")
            else:
                history_lines.append(f"  v{v}{parent_str}: {first_line}")
    except Exception:
        pass

    # ── Collect recent critic local-optima rejections ──
    # Prefer the structured local_optima_warning signal (only written when True,
    # via _record_quality_failure's `v is not False` filter) over raw error text,
    # so the auditor sees "this gen was rejected AS a local optimum" rather than
    # an opaque rejection. This is what should drive repetition_detected for the
    # exhausted-direction loop (observed: v82 was rejected as local-optima but
    # the old code only surfaced error[:200], so the auditor missed the signal
    # and returned repetition_detected=false).
    rejection_lines = []
    try:
        if WORKER_FAILURES_FILE.exists():
            with locked_file(WORKER_FAILURES_FILE, "r") as f:
                entries = [json.loads(l.strip()) for l in f if l.strip()]
            lo_by_gen = {}
            for e in entries:
                if e.get("local_optima_warning") is not True:
                    continue
                if str(e.get("worker_id", "")) != "critic":
                    continue
                g = e.get("gen")
                if g is None:
                    continue
                # Dedup by gen: keep the most recent (retry_workers can reject
                # the same gen repeatedly).
                if g not in lo_by_gen or e.get("timestamp", 0) > lo_by_gen[g][1]:
                    reason = (e.get("local_optima_reason") or "").strip()
                    err_first = (e.get("error", "")).split("\n")[0][:150]
                    lo_by_gen[g] = (reason or err_first, e.get("timestamp", 0))
            for g in sorted(lo_by_gen, reverse=True)[:10]:
                rejection_lines.append(f"  v{g} CRITIC LOCAL-OPTIMA REJECT: {lo_by_gen[g][0]}")
    except Exception:
        pass

    # ── Collect recent master plan analysis (from pipeline logs) ──
    master_log_lines = []
    for check_v in range(max(1, source_v - 4), source_v + 1):
        # Try reading analysis from checkpoint first (more reliable)
        try:
            from evolution_infra import read_pipeline_checkpoint
            ckpt = read_pipeline_checkpoint()
            if ckpt and "master_plan" in ckpt:
                analysis_text = ckpt["master_plan"].get("analysis", "")
                if analysis_text:
                    master_log_lines.append(f"  v{check_v} Master: {analysis_text[:300]}")
                    continue  # skip the regex fallback for this version
        except Exception:
            pass
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

    # ── Collect EXHAUSTED directions from the experience pool ──
    # The pool marks repeatedly-tried-no-gain directions; surfacing them here
    # lets the auditor flag repetition even when git commit messages don't
    # obviously repeat (e.g. "constant tuning" resurfaces under many guises).
    exhausted_lines = []
    try:
        if EXPERIENCE_FILE.exists():
            marker_re = re.compile(r"\[[A-Z ]*EXHAUSTED[^\]]*\]")
            current_section = ""
            for line in EXPERIENCE_FILE.read_text(encoding="utf-8").splitlines():
                if line.startswith("## "):
                    current_section = line.replace("## ", "").strip()
                    continue
                if marker_re.search(line):
                    # Mirror STEP2 guards (tool_planning._extract_exhausted_keywords):
                    # RECENT_LESSONS is free-form critic commentary, not a direction;
                    # overlong entries are review dumps, not directions.
                    if current_section.upper() == "RECENT_LESSONS":
                        continue
                    cleaned = marker_re.sub("", line).strip(" -•")
                    if not cleaned:
                        continue
                    if len(cleaned) > 500:
                        continue
                    exhausted_lines.append(f"  [{current_section}] {cleaned}")
    except Exception:
        pass

    # ── Build generation_history for prompt ──
    gen_history = f"## Source version: v{source_v}\n\n"
    if history_lines:
        gen_history += "## Recent generations (commit messages):\n" + "\n".join(history_lines) + "\n\n"
    if rejection_lines:
        gen_history += "## Recent critic local-optima rejections:\n" + "\n".join(rejection_lines) + "\n\n"
    if master_log_lines:
        gen_history += "## Recent Master analysis summaries:\n" + "\n".join(master_log_lines[-5:]) + "\n\n"
    if exhausted_lines:
        gen_history += "## Experience-pool EXHAUSTED directions (do NOT repeat):\n" + "\n".join(exhausted_lines) + "\n\n"
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
