"""Bounded LLM analysis for official-platform compliance evidence."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any, Awaitable, Callable


ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = ROOT / "web" / "core" / "prompts" / "official_platform_analysis.md"
DEFAULT_MAX_EVIDENCE_CHARS = 45000

ComplianceVerdict = str
AnalysisRunner = Callable[[str], str | Awaitable[str]]

ALLOWED_VERDICTS = {"pass", "fail", "inconclusive"}
ALLOWED_FAILURE_CLASSES = {
    "protocol",
    "communication",
    "state_machine",
    "timeout",
    "platform_race",
    "harness",
    "obvious_decision_error",
    "none",
}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _compact_round(round_item: dict[str, Any], *, max_tail_chars: int = 1200) -> dict[str, Any]:
    log_tails = round_item.get("log_tails") or {}
    compact_tails = {}
    for key, value in log_tails.items():
        text = str(value or "")
        compact_tails[key] = text[-max_tail_chars:] if len(text) > max_tail_chars else text
    replay = round_item.get("wire_replay_summary") or {}
    if replay:
        replay = {
            "events_seen": replay.get("events_seen"),
            "hands_started_min": replay.get("hands_started_min"),
            "settlements_min": replay.get("settlements_min"),
            "issues": (replay.get("issues") or [])[:20],
            "warnings": (replay.get("warnings") or [])[:10],
            "pending_expected_actions": (replay.get("pending_expected_actions") or [])[:10],
            "max_platform_silent_gap_sec": replay.get("max_platform_silent_gap_sec"),
        }
    return {
        "round_id": round_item.get("round_id"),
        "round_kind": round_item.get("round_kind"),
        "round_index": round_item.get("round_index"),
        "target_hands": round_item.get("target_hands"),
        "passed": round_item.get("passed"),
        "classification": round_item.get("classification"),
        "issues": (round_item.get("issues") or [])[:30],
        "log_summary": round_item.get("log_summary") or {},
        "thp_summaries": round_item.get("thp_summaries") or [],
        "wire_replay_summary": replay,
        "artifact_paths": {
            key: value.get("path")
            for key, value in (round_item.get("artifacts") or {}).items()
            if isinstance(value, dict) and value.get("path")
        },
        "log_tails": compact_tails,
    }


def compact_evidence_for_llm(evidence: dict[str, Any], *, max_chars: int = DEFAULT_MAX_EVIDENCE_CHARS) -> dict[str, Any]:
    """Keep compliance evidence small enough for reliable LLM attribution."""
    compact = {
        "schema_version": evidence.get("schema_version"),
        "candidate": evidence.get("candidate"),
        "opponent": evidence.get("opponent"),
        "purpose": evidence.get("purpose"),
        "strength_evaluation": "not_applicable",
        "summary": evidence.get("summary") or {},
        "deterministic": evidence.get("deterministic") or {},
        "rounds": [_compact_round(dict(item)) for item in (evidence.get("rounds") or []) if isinstance(item, dict)],
    }
    raw = json.dumps(compact, ensure_ascii=False, indent=2)
    if len(raw) <= max_chars:
        return compact
    # Trim log tails first, preserving deterministic issues and replay details.
    for round_item in compact["rounds"]:
        for key, text in list((round_item.get("log_tails") or {}).items()):
            value = str(text or "")
            round_item["log_tails"][key] = value[-300:] if len(value) > 300 else value
    raw = json.dumps(compact, ensure_ascii=False, indent=2)
    if len(raw) <= max_chars:
        return compact
    compact["rounds"] = [
        {
            key: round_item.get(key)
            for key in (
                "round_id",
                "round_kind",
                "round_index",
                "target_hands",
                "passed",
                "classification",
                "issues",
                "wire_replay_summary",
                "thp_summaries",
            )
        }
        for round_item in compact["rounds"]
    ]
    return compact


def build_official_analysis_prompt(evidence: dict[str, Any], *, prompt_template: str | None = None) -> str:
    template = prompt_template
    if template is None:
        template = PROMPT_PATH.read_text(encoding="utf-8")
    compact = compact_evidence_for_llm(evidence)
    evidence_json = json.dumps(compact, ensure_ascii=False, indent=2)
    return template.replace("{evidence_json}", evidence_json)


def safe_default_analysis(evidence: dict[str, Any], *, reason: str = "llm_not_run") -> dict[str, Any]:
    deterministic = evidence.get("deterministic") or {}
    blocking = bool(deterministic.get("blocking"))
    classification = str(deterministic.get("classification") or "none")
    failure_class = classification if classification in ALLOWED_FAILURE_CLASSES else "none"
    return {
        "schema_version": 1,
        "analysis_source": "default",
        "compliance_verdict": "fail" if blocking else ("pass" if deterministic.get("passed") else "inconclusive"),
        "failure_class": failure_class,
        "blocking": blocking,
        "confidence": 0.0,
        "deterministic_blocking": blocking,
        "evidence": [],
        "root_cause": reason,
        "repair_guidance": "",
        "prompt_feedback": "",
        "strength_evaluation": "not_applicable",
        "ignored_strength_fields": [],
        "notes": [reason],
    }


def _parse_json_output(text: str) -> tuple[dict[str, Any] | None, str]:
    try:
        from llm_query import parse_json_output_with_mode

        parsed, mode = parse_json_output_with_mode(text)
        return (parsed if isinstance(parsed, dict) else None), mode
    except Exception:
        try:
            data = json.loads(text)
            return (data if isinstance(data, dict) else None), "OK"
        except Exception:
            return None, "PARSE_ERROR"


def normalize_official_analysis(raw: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    deterministic = evidence.get("deterministic") or {}
    deterministic_blocking = bool(deterministic.get("blocking"))
    deterministic_passed = bool(deterministic.get("passed"))
    deterministic_class = str(deterministic.get("classification") or "none")

    verdict = str(raw.get("compliance_verdict") or "inconclusive").lower()
    if verdict not in ALLOWED_VERDICTS:
        verdict = "inconclusive"
    failure_class = str(raw.get("failure_class") or "none").lower()
    if failure_class not in ALLOWED_FAILURE_CLASSES:
        failure_class = deterministic_class if deterministic_class in ALLOWED_FAILURE_CLASSES else "none"
    try:
        confidence = float(raw.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    notes: list[str] = []
    if deterministic_blocking and verdict != "fail":
        verdict = "fail"
        if deterministic_class in ALLOWED_FAILURE_CLASSES:
            failure_class = deterministic_class
        notes.append("llm_pass_overridden_by_deterministic_blocking_evidence")
    elif deterministic_passed and verdict == "fail":
        verdict = "inconclusive"
        notes.append("llm_failure_without_deterministic_confirmation_is_advisory")

    evidence_items = raw.get("evidence")
    if not isinstance(evidence_items, list):
        evidence_items = []
    ignored_strength_fields = sorted(
        key for key in raw
        if key.lower() in {"strength", "strength_score", "rating", "rating_delta", "winrate", "win_rate"}
    )

    return {
        "schema_version": 1,
        "analysis_source": "llm",
        "compliance_verdict": verdict,
        "failure_class": failure_class,
        "blocking": deterministic_blocking,
        "confidence": confidence,
        "deterministic_blocking": deterministic_blocking,
        "evidence": evidence_items[:20],
        "root_cause": str(raw.get("root_cause") or ""),
        "repair_guidance": str(raw.get("repair_guidance") or ""),
        "prompt_feedback": str(raw.get("prompt_feedback") or ""),
        "strength_evaluation": "not_applicable",
        "ignored_strength_fields": ignored_strength_fields,
        "notes": notes,
    }


async def run_official_llm_analysis(
    evidence: dict[str, Any],
    *,
    runner: AnalysisRunner | None = None,
    output_path: str | Path | None = None,
    log_file: str | Path | None = None,
    ui: Any = None,
) -> dict[str, Any]:
    """Run bounded LLM compliance analysis.

    ``runner`` is intentionally injectable so tests and offline tools can verify
    parsing and normalization without touching the live LLM backend.
    """
    prompt = build_official_analysis_prompt(evidence)
    if runner is None:
        async def _default_runner(prompt_text: str) -> str:
            from llm_query import run_claude_query

            output, _, _ = await run_claude_query(
                prompt_text,
                [],
                ui,
                "OFFICIAL PLATFORM COMPLIANCE ANALYST",
                Path(log_file) if log_file else None,
                tools=[],
            )
            return output

        runner = _default_runner
    try:
        result = runner(prompt)
        output = await result if inspect.isawaitable(result) else result
        raw, parse_mode = _parse_json_output(str(output or ""))
        if raw is None:
            analysis = safe_default_analysis(evidence, reason=f"llm_parse_failed:{parse_mode}")
            analysis["analysis_source"] = "default_parse_failed"
        else:
            analysis = normalize_official_analysis(raw, evidence)
            analysis["parse_mode"] = parse_mode
    except Exception as exc:
        analysis = safe_default_analysis(evidence, reason=f"llm_analysis_error:{type(exc).__name__}: {exc}")
        analysis["analysis_source"] = "default_error"
    if output_path is not None:
        _write_json(Path(output_path), analysis)
        analysis["analysis_path"] = str(output_path)
    return analysis


def run_official_llm_analysis_sync(
    evidence: dict[str, Any],
    *,
    runner: AnalysisRunner | None = None,
    output_path: str | Path | None = None,
    log_file: str | Path | None = None,
    ui: Any = None,
) -> dict[str, Any]:
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            run_official_llm_analysis(
                evidence,
                runner=runner,
                output_path=output_path,
                log_file=log_file,
                ui=ui,
            )
        )
    raise RuntimeError("run_official_llm_analysis_sync cannot run inside an active event loop")
