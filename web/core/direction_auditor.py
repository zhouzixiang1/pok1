"""Direction Auditor over immutable strict-policy publication history."""

import hashlib
from pathlib import Path

from bot_namespace import (
    FIRST_STRICT_POLICY_VERSION,
    bot_tag,
    parse_bot_version,
)
from evolution_infra import (
    run_claude_query, parse_json_output,
    get_logs_dir,
    PROMPTS_DIR,
)
from national_runtime_authority import strict_published_bot_names
from output_schema import validate_agent_output
from llm_availability import LLMAvailabilityBlocked


def _render_direction_provider_prompt(inputs):
    from llm_query import LLMRenderedMaterial

    if not isinstance(inputs, dict) or set(inputs) != {
        "generation_history", "source_v",
    }:
        raise ValueError("Direction Auditor renderer input contract mismatch")

    template = (
        Path(__file__).resolve().parent / "prompts" / "direction_auditor_prompt.md"
    ).read_text(
        encoding="utf-8"
    )
    text = template.replace(
        "{generation_history}",
        str(inputs["generation_history"]),
    )
    return LLMRenderedMaterial(
        text=text,
        evidence_kind="annotated_completion_direction_history",
        evidence_provenance={
            "source_v": int(inputs["source_v"]),
            "generation_history_digest": hashlib.sha256(
                str(inputs["generation_history"]).encode("utf-8")
            ).hexdigest(),
        },
    )


async def _run_direction_audit(source_v, ui):
    """Run Direction Auditor to detect repetitive evolution directions.

    Reads only completion-tag commit messages whose bot directories satisfy the
    strict published identity. Mutable worker/critic logs and archived tags are
    outside this prompt's evidence boundary.

    Returns a dict: {repetition_detected, exhausted_directions, mandatory_constraints,
                     suggested_direction, confidence, last_directions}.
    Returns a safe no-repetition default on failure.
    """
    audit_prompt_path = (
        Path(__file__).resolve().parent / "prompts" / "direction_auditor_prompt.md"
    )
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
        strict_versions = sorted({
            version
            for name in strict_published_bot_names()
            if (version := parse_bot_version(name)) is not None
            and version >= FIRST_STRICT_POLICY_VERSION
        })
        for v in strict_versions[-6:]:
            tag = bot_tag(v)
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
            parent_str = (
                f" ← v{parent}"
                if parent is not None and int(parent) >= FIRST_STRICT_POLICY_VERSION
                else ""
            )
            # Include full body if it has multi-line strategy detail
            if len(body) > len(first_line) + 10:
                history_lines.append(f"  v{v}{parent_str}: {first_line}\n    {body[len(first_line):].strip()[:400]}")
            else:
                history_lines.append(f"  v{v}{parent_str}: {first_line}")
    except Exception:
        pass

    # ── Build generation_history for prompt ──
    gen_history = f"## Source version: v{source_v}\n\n"
    if history_lines:
        gen_history += (
            "## Recent strict published generations (immutable completion commits):\n"
            + "\n".join(history_lines)
            + "\n\n"
        )
    else:
        gen_history += "No strict published generation history available.\n"

    # ── Call LLM ──
    log_file = get_logs_dir(source_v) / "direction_audit_io.txt"
    try:
        from llm_query import render_llm_prompt

        rendered_prompt = render_llm_prompt(
            "DIRECTION AUDITOR",
            producer=_render_direction_provider_prompt,
            renderer_inputs={
                "generation_history": gen_history,
                "source_v": int(source_v),
            },
        )
        output, _, _ = await run_claude_query(
            rendered_prompt, [], ui, "DIRECTION AUDITOR", log_file,
            tools=[],
        )
        from llm_query import parse_json_output_with_mode
        data, failure_mode = parse_json_output_with_mode(output)
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
    except LLMAvailabilityBlocked:
        # Availability is a control-plane stop, not an audit judgement.  Let the
        # caller persist/park the generation without advancing the checkpoint.
        raise
    except Exception as e:
        from llm_failure import is_llm_infra_error
        if is_llm_infra_error(e):
            ui.log_history(
                f"Direction Auditor LLM infrastructure error (NOT a business judgement): {e}. "
                "Returning no-repetition default with llm_failed marker.",
                "warn",
            )
            return {"repetition_detected": False, "exhausted_directions": [],
                    "mandatory_constraints": None, "suggested_direction": None,
                    "confidence": "low", "last_directions": [],
                    "llm_failed": True}
        ui.log_history(f"Direction Auditor error: {e}. Skipping.", "warn")

    # RC4 (parse collapse + silent PASS): reaching here means the LLM output
    # failed to parse (NO_JSON/NO_FENCE/PARSE_ERROR) or lacked the
    # repetition_detected key, OR an exception skipped the parse. Previously
    # this fell through to a silent {repetition_detected: False} default, which
    # tool_planning.run_master consumed as a clean "no repetition" PASS —
    # marking a FAILED audit gate as an authoritative clean pass. Emit a
    # failure event and return an explicitly-uncertain result (parse_failed=True)
    # so Master knows the anti-repetition gate is degraded, not clean.
    _fm = locals().get("failure_mode", "EXCEPTION")
    _out = locals().get("output", "") or ""
    try:
        from event_bus import warn
        warn("pipeline.direction_audit_parse_failed",
             f"Direction Auditor v{source_v} parse failed (mode={_fm}); "
             "returning uncertain default (gate degraded, not clean PASS)",
             source_v=source_v, failure_mode=_fm, output_len=len(_out))
    except Exception:
        pass
    return {"repetition_detected": False, "exhausted_directions": [],
            "mandatory_constraints": None, "suggested_direction": None,
            "confidence": "low", "last_directions": [],
            "parse_failed": True}
