"""Official-platform evidence bundle builder.

The Windows EXE harness is a compliance oracle, not a strength evaluator.  This
module turns the raw harness outputs into a compact, deterministic evidence
bundle that can be consumed by dashboards, certification status, and a bounded
LLM compliance-analysis pass.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

from official_attribution import attribute_round, attribute_suite


SCHEMA_VERSION = 1
MAX_TEXT_TAIL_CHARS = 6000
MAX_ISSUES = 80

PROTOCOL_MARKERS = (
    "protocol_",
    "illegal_",
    "illegal ",
    "invalid action",
    "unknown action",
    "wire_action",
    "wire_raise_format",
    "wire_bet_token",
    "unsolicited_client_action",
    "postflop",
    "allin after",
    "raise using all remaining chips",
)
COMMUNICATION_MARKERS = (
    "sticky",
    "remaining",
    "unknown_server_message",
    "connectionrefused",
    "brokenpipe",
    "protocol error",
    "stdout",
    "json response",
    "botzone",
)
DECISION_MARKERS = (
    "official_full_round_incomplete_after_progress",
    "official_full_early_platform_close_after_progress",
    "obvious_decision_error",
)
TIMEOUT_MARKERS = (
    "timeout",
    "silent_timeout_gap",
    "platform_silent",
    "no_progress_timeout",
    "round_timeout",
    "pending_bot_response",
)
HARNESS_MARKERS = (
    "wine",
    "xvfb",
    "xdotool",
    "official platform window not found",
    "official platform did not listen",
    "port_busy_before_start",
    "missing_tools",
    "exe_missing",
    "wineprefix_missing",
    "official_platform_lock_timeout",
    "wire_probe",
    "official_full_settlement_incomplete",
    "connection reset",
    "connectionreseterror",
)
ARTIFACT_KEYS = (
    "receipt",
    "platform_log",
    "bot_a_log",
    "bot_b_log",
    "bot_a_stdout",
    "bot_a_stderr",
    "bot_b_stdout",
    "bot_b_stderr",
    "wire_events",
    "replay_summary",
)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dataclass_fields__"):
        return {key: _jsonable(val) for key, val in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
    except Exception:
        return None
    return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _tail_text(path: Path, *, max_chars: int = MAX_TEXT_TAIL_CHARS) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="replace")
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _artifact(
    path_value: Any,
    *,
    label: str,
    kind: str,
    artifact_root: Path | None = None,
) -> dict[str, Any] | None:
    if not path_value:
        return None
    path = Path(str(path_value))
    item: dict[str, Any] = {
        "label": label,
        "kind": kind,
        "path": str(path),
        "exists": path.exists(),
    }
    if path.exists() and path.is_file():
        try:
            if path.is_symlink():
                item["issue"] = "artifact_symlink_forbidden"
                return item
            if artifact_root is not None:
                try:
                    relative = path.resolve().relative_to(artifact_root.resolve())
                except ValueError:
                    item["issue"] = "artifact_outside_suite"
                    return item
                item["archive_path"] = relative.as_posix()
            stat = path.stat()
            item["size_bytes"] = stat.st_size
            item["sha256"] = _sha256(path)
        except OSError as exc:
            item["issue"] = f"artifact_stat_error: {type(exc).__name__}: {exc}"
    return item


def _round_report(payload: dict[str, Any]) -> dict[str, Any]:
    if "rounds" in payload and isinstance(payload.get("rounds"), list):
        return payload
    report = payload.get("report")
    if isinstance(report, dict) and isinstance(report.get("rounds"), list):
        return report
    return {}


def _suite_summary(payload: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    summary = report.get("summary") if isinstance(report, dict) else None
    if isinstance(summary, dict):
        return dict(summary)
    summary = payload.get("summary")
    return dict(summary) if isinstance(summary, dict) else {}


def _round_target_hands(receipt: dict[str, Any]) -> int:
    try:
        return int(receipt.get("target_hands", 0) or 0)
    except Exception:
        return 0


def _wire_evidence_required(receipt: dict[str, Any]) -> bool:
    """Return whether this official round must carry raw wire evidence."""
    return _round_target_hands(receipt) >= 70


def _path_exists(path_value: Any) -> bool:
    if not path_value:
        return False
    try:
        return Path(str(path_value)).exists()
    except Exception:
        return False


def _wire_artifact_issues(receipt: dict[str, Any]) -> list[str]:
    """Return evidence-completeness issues for the official wire probe.

    Current harness receipts include ``wire_probe``.  Older cached or fake
    reports may not; those are left alone so historical statuses are not
    reinterpreted.  For new full 70-hand rounds, raw wire events and their replay
    summary are mandatory evidence because bot logs alone cannot prove
    sticky-packet and pending-action behavior.
    """
    if not _wire_evidence_required(receipt):
        return []
    wire_probe = receipt.get("wire_probe")
    if not isinstance(wire_probe, dict):
        return []
    if not bool(wire_probe.get("enabled")):
        return ["wire_probe_disabled_for_full_round"]
    artifacts = receipt.get("artifacts") if isinstance(receipt.get("artifacts"), dict) else {}
    issues: list[str] = []
    if not _path_exists(artifacts.get("wire_events")):
        issues.append("wire_probe_missing_wire_events_artifact")
    if not _path_exists(artifacts.get("replay_summary")):
        issues.append("wire_probe_missing_replay_summary_artifact")
    return issues


def _all_issues(payload: dict[str, Any], report: dict[str, Any], rounds: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    for source in (payload.get("issues"), report.get("issues")):
        if isinstance(source, list):
            issues.extend(str(issue) for issue in source)
    for index, receipt in enumerate(rounds, start=1):
        prefix = receipt.get("round_id") or receipt.get("round_kind") or f"round_{index}"
        for issue in _wire_artifact_issues(receipt):
            issues.append(f"{prefix}: {issue}")
        for issue in receipt.get("issues") or []:
            issues.append(f"{prefix}: {issue}")
        replay = _extract_replay_summary(receipt)
        if replay:
            for issue in replay.get("issues") or []:
                kind = issue.get("kind") if isinstance(issue, dict) else str(issue)
                issues.append(f"{prefix}: wire_replay: {kind}")
    return list(dict.fromkeys(issues))[:MAX_ISSUES]


def classify_issues(issues: list[str]) -> dict[str, Any]:
    """Return a deterministic compliance classification for issue strings."""
    lower = "\n".join(issues).lower()
    if not issues:
        return {
            "classification": "pass",
            "blocking": False,
            "inconclusive": False,
            "violation": False,
            "blocking_issue_count": 0,
        }
    buckets: list[str] = []
    if any(marker in lower for marker in PROTOCOL_MARKERS):
        buckets.append("protocol")
    if any(marker in lower for marker in DECISION_MARKERS):
        buckets.append("obvious_decision_error")
    if any(marker in lower for marker in COMMUNICATION_MARKERS):
        buckets.append("communication")
    if any(marker in lower for marker in TIMEOUT_MARKERS):
        buckets.append("timeout")
    if any(marker in lower for marker in HARNESS_MARKERS):
        buckets.append("harness")
    if not buckets:
        buckets.append("inconclusive")
    blocking = any(bucket in {"protocol", "communication", "timeout", "obvious_decision_error"} for bucket in buckets)
    return {
        "classification": buckets[0],
        "buckets": buckets,
        "blocking": blocking,
        "inconclusive": (not blocking and "harness" in buckets) or buckets == ["inconclusive"],
        "violation": blocking,
        "blocking_issue_count": len(issues) if blocking else 0,
    }


def classify_round_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    """Classify one official round with receipt context, not issue text alone."""
    issues = [str(issue) for issue in receipt.get("issues") or []]
    summary = receipt.get("log_summary") or {}
    try:
        hands_started = int(summary.get("hands_started_min", 0) or 0)
        settlements = int(summary.get("settlements_min", 0) or 0)
        target_hands = int(receipt.get("target_hands", 0) or 0)
    except Exception:
        hands_started = 0
        settlements = 0
        target_hands = 0
    text = "\n".join(issues).lower()
    base = classify_issues(issues)
    if base.get("classification") == "protocol":
        return base
    if target_hands >= 70 and 0 < hands_started < target_hands:
        if any(marker in text for marker in (
            "thp_missing_for_full_70_hand_round",
            "thp_incomplete_for_full_certification",
            "exited_early",
            "server closed",
            "official_full_round_incomplete_after_progress",
        )):
            return {
                "classification": "obvious_decision_error",
                "buckets": ["obvious_decision_error"],
                "blocking": True,
                "inconclusive": False,
                "violation": False,
                "blocking_issue_count": max(1, len(issues)),
                "reason": (
                    "full official round made game progress but ended before "
                    f"{target_hands} hands (hands_started={hands_started}, settlements={settlements})"
                ),
            }
    if hands_started == 0 and any(marker in text for marker in (
        "connectionreseterror",
        "connection reset",
        "official_full_round_no_game_progress",
        "no_progress_timeout",
        "round_timeout",
        "platform_exited_early",
        "port_busy_before_start",
        "official platform did not listen",
        "wine",
        "xvfb",
    )):
        return {
            "classification": "harness",
            "buckets": ["harness"],
            "blocking": False,
            "inconclusive": True,
            "violation": False,
            "blocking_issue_count": 0,
            "reason": "official platform produced no game-progress evidence",
        }
    return base


def _round_dir_from_receipt(receipt: dict[str, Any]) -> Path | None:
    artifacts = receipt.get("artifacts") if isinstance(receipt.get("artifacts"), dict) else {}
    raw = artifacts.get("round_dir")
    if raw:
        return Path(str(raw))
    raw_receipt = artifacts.get("receipt")
    if raw_receipt:
        return Path(str(raw_receipt)).parent
    return None


def _wire_events_path(receipt: dict[str, Any]) -> Path | None:
    artifacts = receipt.get("artifacts") if isinstance(receipt.get("artifacts"), dict) else {}
    for key in ("wire_events", "wire_events_jsonl"):
        raw = artifacts.get(key)
        if raw:
            return Path(str(raw))
    round_dir = _round_dir_from_receipt(receipt)
    if round_dir is None:
        return None
    candidate = round_dir / "wire_events.jsonl"
    return candidate if candidate.exists() else None


def _extract_replay_summary(receipt: dict[str, Any]) -> dict[str, Any] | None:
    artifacts = receipt.get("artifacts") if isinstance(receipt.get("artifacts"), dict) else {}
    raw = artifacts.get("replay_summary")
    if raw:
        data = _read_json(Path(str(raw)))
        if data is not None:
            return data
    round_dir = _round_dir_from_receipt(receipt)
    if round_dir is not None:
        data = _read_json(round_dir / "replay_summary.json")
        if data is not None:
            return data
    wire_path = _wire_events_path(receipt)
    if wire_path is None or not wire_path.exists():
        return None
    try:
        from official_wire_probe import load_events, replay_events

        summary = replay_events(load_events(wire_path))
        if round_dir is not None:
            _write_json(round_dir / "replay_summary.json", summary)
        return summary
    except Exception as exc:
        return {
            "events_seen": 0,
            "issues": [{"kind": "wire_replay_error", "reason": f"{type(exc).__name__}: {exc}"}],
            "warnings": [],
        }


def _artifact_map(
    receipt: dict[str, Any],
    *,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    raw_artifacts = receipt.get("artifacts") if isinstance(receipt.get("artifacts"), dict) else {}
    artifacts: dict[str, Any] = {}
    for key in ARTIFACT_KEYS:
        item = _artifact(
            raw_artifacts.get(key),
            label=key,
            kind="jsonl" if key == "wire_events" else "log",
            artifact_root=artifact_root,
        )
        if item:
            artifacts[key] = item
    screenshots = raw_artifacts.get("screenshots") or []
    artifacts["screenshots"] = [
        item for item in (
            _artifact(
                path,
                label=f"screenshot_{idx}",
                kind="image",
                artifact_root=artifact_root,
            )
            for idx, path in enumerate(screenshots, start=1)
        )
        if item
    ]
    artifacts["thp_files"] = [
        item for item in (
            _artifact(
                path,
                label=f"thp_{idx}",
                kind="thp",
                artifact_root=artifact_root,
            )
            for idx, path in enumerate(raw_artifacts.get("thp_files") or [], start=1)
        )
        if item
    ]
    return artifacts


def _log_excerpt(path_value: Any, *, max_chars: int) -> str:
    if not path_value:
        return ""
    path = Path(str(path_value))
    if not path.exists() or not path.is_file():
        return ""
    return _tail_text(path, max_chars=max_chars)


def _round_evidence(
    receipt: dict[str, Any],
    *,
    max_log_chars: int,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    artifacts = receipt.get("artifacts") if isinstance(receipt.get("artifacts"), dict) else {}
    artifact_map = _artifact_map(receipt, artifact_root=artifact_root)
    artifact_issues = []
    for key, value in artifact_map.items():
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict) and item.get("issue"):
                artifact_issues.append(f"evidence_artifact_{key}:{item['issue']}")
    replay_summary = _extract_replay_summary(receipt)
    issues = [
        *_wire_artifact_issues(receipt),
        *artifact_issues,
        *[str(issue) for issue in receipt.get("issues") or []],
    ]
    if replay_summary:
        for issue in replay_summary.get("issues") or []:
            kind = issue.get("kind") if isinstance(issue, dict) else str(issue)
            issues.append(f"wire_replay: {kind}")
    attributed = attribute_round({
        **receipt,
        "wire_replay_summary": replay_summary or receipt.get("wire_replay_summary") or {},
        "issues": list(dict.fromkeys(issues)),
    })
    if attributed["candidate_verdict"] == "fail":
        classification = next(
            (item.get("category") for item in attributed["findings"] if item.get("candidate_impact") == "block"),
            "protocol",
        )
    elif attributed["candidate_verdict"] == "inconclusive":
        classification = next(
            (item.get("category") for item in attributed["findings"] if item.get("candidate_impact") == "retry"),
            "harness",
        )
    else:
        classification = "pass"
    return {
        "round_id": receipt.get("round_id", ""),
        "round_kind": receipt.get("round_kind", ""),
        "round_index": receipt.get("round_index"),
        "target_hands": receipt.get("target_hands"),
        "passed": bool(receipt.get("passed")),
        "duration_sec": receipt.get("duration_sec"),
        "bot_returncodes": receipt.get("bot_returncodes", {}),
        "issues": list(dict.fromkeys(issues))[:MAX_ISSUES],
        "classification": classification,
        "attribution": attributed,
        "log_summary": receipt.get("log_summary", {}),
        "completion_evidence": receipt.get("completion_evidence"),
        "thp_summaries": artifacts.get("thp_summaries", []),
        "canonical_thp": artifacts.get("canonical_thp"),
        "wire_replay_summary": replay_summary,
        "artifacts": artifact_map,
        "log_tails": {
            "bot_a_log": _log_excerpt(artifacts.get("bot_a_log"), max_chars=max_log_chars),
            "bot_b_log": _log_excerpt(artifacts.get("bot_b_log"), max_chars=max_log_chars),
            "bot_a_stdout": _log_excerpt(artifacts.get("bot_a_stdout"), max_chars=max(1000, max_log_chars // 3)),
            "bot_a_stderr": _log_excerpt(artifacts.get("bot_a_stderr"), max_chars=max(1000, max_log_chars // 3)),
            "bot_b_stdout": _log_excerpt(artifacts.get("bot_b_stdout"), max_chars=max(1000, max_log_chars // 3)),
            "bot_b_stderr": _log_excerpt(artifacts.get("bot_b_stderr"), max_chars=max(1000, max_log_chars // 3)),
        },
    }


def build_official_evidence_bundle(
    result_or_report: Any,
    *,
    output_path: str | Path | None = None,
    max_log_chars: int = MAX_TEXT_TAIL_CHARS,
) -> dict[str, Any]:
    """Build and optionally write a compact evidence bundle.

    ``result_or_report`` may be a ``NationalAcceptanceResult``, its
    ``model_dump()`` dictionary, or the raw ``summary.json`` report emitted by
    ``official_platform_harness``.
    """
    payload = _jsonable(result_or_report)
    if not isinstance(payload, dict):
        payload = {}
    report = _round_report(payload)
    rounds = [dict(item) for item in (report.get("rounds") or []) if isinstance(item, dict)]
    summary = _suite_summary(payload, report)
    suite_dir = summary.get("suite_dir")
    artifact_root = Path(str(suite_dir)).resolve() if suite_dir else None
    issues = _all_issues(payload, report, rounds)
    evidence_rounds = [
        _round_evidence(
            receipt,
            max_log_chars=max_log_chars,
            artifact_root=artifact_root,
        )
        for receipt in rounds
    ]
    for round_item in evidence_rounds:
        issues.extend(str(issue) for issue in round_item.get("issues") or [])
    issues = list(dict.fromkeys(issues))[:MAX_ISSUES]
    suite_attribution = attribute_suite([
        {
            **receipt,
            "wire_replay_summary": round_item.get("wire_replay_summary") or {},
            "issues": round_item.get("issues") or receipt.get("issues") or [],
        }
        for receipt, round_item in zip(rounds, evidence_rounds)
    ])
    round_verdicts = []
    for round_item in evidence_rounds:
        attributed = round_item.get("attribution") or {}
        round_verdicts.append({
            "classification": round_item.get("classification") or "inconclusive",
            "blocking": bool(attributed.get("candidate_blocking")),
            "inconclusive": attributed.get("candidate_verdict") == "inconclusive",
            "violation": bool(attributed.get("candidate_blocking")),
            "countable": bool(attributed.get("countable")),
            "candidate_verdict": attributed.get("candidate_verdict"),
            "policy_id": attributed.get("policy_id"),
        })
    wire_required_rounds = sum(1 for receipt in rounds if _wire_evidence_required(receipt))
    wire_complete_rounds = sum(
        1
        for receipt in rounds
        if _wire_evidence_required(receipt) and not _wire_artifact_issues(receipt)
    )
    candidate_verdict = suite_attribution.get("candidate_verdict")
    if candidate_verdict == "pass" and issues:
        candidate_verdict = "inconclusive"
        suite_attribution = {
            **suite_attribution,
            "candidate_verdict": "inconclusive",
            "inconclusive": True,
        }
    if candidate_verdict == "fail":
        first = (suite_attribution.get("candidate_findings") or [{}])[0]
        classification = first.get("category") or "protocol"
    elif candidate_verdict == "inconclusive":
        first = (suite_attribution.get("retry_findings") or [{}])[0]
        classification = first.get("category") or "harness"
    else:
        classification = "pass"
    verdict = {
        "classification": classification,
        "buckets": [classification],
        "blocking": candidate_verdict == "fail",
        "inconclusive": candidate_verdict == "inconclusive",
        "violation": candidate_verdict == "fail",
        "blocking_issue_count": len(suite_attribution.get("candidate_findings") or []),
        "candidate_verdict": candidate_verdict,
        "attribution_policy_id": suite_attribution.get("policy_id"),
    }
    raw_passed_value = payload.get("passed", report.get("passed"))
    raw_passed = (
        bool(raw_passed_value)
        if raw_passed_value is not None
        else bool(rounds) and not issues and all(bool(receipt.get("passed")) for receipt in rounds)
    )
    passed = raw_passed and not verdict["blocking"] and not verdict.get("inconclusive")
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "candidate": report.get("candidate") or payload.get("candidate") or "",
        "opponent": report.get("opponent") or (payload.get("opponents") or [None])[0],
        "purpose": "official_platform_compliance",
        "strength_evaluation": "not_applicable",
        "summary": {
            **summary,
            "passed": passed,
            "raw_passed": raw_passed,
            "wire_evidence_required_rounds": wire_required_rounds,
            "wire_evidence_complete_rounds": wire_complete_rounds,
        },
        "deterministic": {
            "passed": passed,
            "issues": issues,
            "round_classifications": round_verdicts,
            "attribution": suite_attribution,
            "rounds_requested": summary.get("rounds_requested"),
            "rounds_run": summary.get("rounds_run", len(rounds)),
            "target_hands": summary.get("target_hands") or payload.get("hands_per_pair"),
            **verdict,
        },
        "rounds": evidence_rounds,
        "artifact_root": str(artifact_root) if artifact_root is not None else "",
    }
    if output_path is not None:
        _write_json(Path(output_path), bundle)
        bundle["evidence_path"] = str(output_path)
    return bundle


def build_official_evidence_from_summary(
    summary_path: str | Path,
    *,
    output_path: str | Path | None = None,
    max_log_chars: int = MAX_TEXT_TAIL_CHARS,
) -> dict[str, Any]:
    path = Path(summary_path)
    data = _read_json(path)
    if data is None:
        data = {
            "passed": False,
            "issues": [f"summary_read_error: {path}"],
            "summary": {"suite_dir": str(path.parent), "rounds_run": 0},
            "rounds": [],
        }
    if output_path is None:
        output_path = path.parent / "official_evidence.json"
    return build_official_evidence_bundle(data, output_path=output_path, max_log_chars=max_log_chars)
