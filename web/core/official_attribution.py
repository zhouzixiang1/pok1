"""Deterministic responsibility attribution for official EXE evidence.

The official platform is a compliance oracle for the candidate. Opponent,
platform, and harness faults invalidate a round but must never be rewritten as
candidate protocol failures.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


ATTRIBUTION_SCHEMA_VERSION = 1
ATTRIBUTION_POLICY_ID = "official-attribution-v1"

_CANDIDATE_BLOCKING_WIRE_KINDS = {
    "wire_action_whitespace",
    "wire_raise_format",
    "wire_bet_token",
    "wire_action_format",
    "unsolicited_client_action",
    "bot_response_timeout",
    "pending_bot_response_timeout",
    "wire_stream_eof_remainder",
    "illegal_call",
    "illegal_check",
    "illegal_raise",
    "illegal_allin",
    "illegal_fold",
}
_PLATFORM_WIRE_KINDS = {
    "platform_silent_timeout_gap",
    "platform_silent_pending_gap",
    "platform_silent_idle_gap",
    "unknown_server_message",
    "wire_replay_error",
    "wire_probe_upstream_connect_failed",
    "wire_stream_error",
}
_HARNESS_MARKERS = (
    "official_round_exception",
    "official_acceptance_suite_exception",
    "port_busy_",
    "no_progress_timeout",
    "round_timeout",
    "incomplete_round",
    "thp_missing",
    "thp_incomplete",
    "wire_probe_",
    "platform_exited_early",
    "official platform",
    "wine",
    "xvfb",
    "xdotool",
)


def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8", "surrogateescape")).hexdigest()


def round_topology(receipt: dict[str, Any]) -> dict[str, Any]:
    kind = str(receipt.get("round_kind") or "")
    explicit = receipt.get("topology")
    if isinstance(explicit, dict) and isinstance(explicit.get("connections"), dict):
        return explicit
    bot_a = receipt.get("bot_a") if isinstance(receipt.get("bot_a"), dict) else {}
    bot_b = receipt.get("bot_b") if isinstance(receipt.get("bot_b"), dict) else {}
    role_a = str(bot_a.get("role") or "candidate")
    role_b = str(bot_b.get("role") or ("candidate" if kind == "self_play" else "opponent"))
    return {
        "schema_version": 1,
        "round_kind": kind,
        "connections": {
            "A": {
                "role": role_a,
                "instance_id": bot_a.get("instance_id") or f"{role_a}_a",
                "name": bot_a.get("name") or "BotA",
                "path": bot_a.get("path"),
                "launch_slot": "A",
            },
            "B": {
                "role": role_b,
                "instance_id": bot_b.get("instance_id") or f"{role_b}_b",
                "name": bot_b.get("name") or "BotB",
                "path": bot_b.get("path"),
                "launch_slot": "B",
            },
        },
    }


def _connection_subject(topology: dict[str, Any], conn: Any) -> tuple[str, str, str]:
    label = str(conn or "")
    item = (topology.get("connections") or {}).get(label) or {}
    return (
        str(item.get("role") or "harness"),
        str(item.get("instance_id") or f"connection_{label or 'unknown'}"),
        label,
    )


def _finding(
    *,
    round_id: str,
    index: int,
    code: str,
    category: str,
    subject_domain: str,
    subject_instance_id: str,
    candidate_impact: str,
    certainty: str = "deterministic",
    connection: str = "",
    evidence: Any = None,
) -> dict[str, Any]:
    payload = {
        "round_id": round_id,
        "code": code,
        "category": category,
        "subject_domain": subject_domain,
        "subject_instance_id": subject_instance_id,
        "candidate_impact": candidate_impact,
        "certainty": certainty,
        "connection": connection,
        "evidence": evidence,
    }
    payload["finding_id"] = f"{round_id}:{index:03d}:{_digest(payload)[:12]}"
    return payload


def _wire_findings(
    receipt: dict[str, Any],
    topology: dict[str, Any],
    *,
    start_index: int,
) -> list[dict[str, Any]]:
    summary = receipt.get("wire_replay_summary")
    if not isinstance(summary, dict):
        summary = {}
    findings: list[dict[str, Any]] = []
    round_id = str(receipt.get("round_id") or receipt.get("round_kind") or "round")
    for offset, raw in enumerate(summary.get("issues") or [], start=start_index):
        issue = raw if isinstance(raw, dict) else {"kind": str(raw)}
        code = str(issue.get("kind") or "wire_issue")
        if code in _PLATFORM_WIRE_KINDS or (
            code == "wire_stream_eof_remainder"
            and issue.get("direction") != "bot_to_server"
        ):
            domain, instance, conn = "platform", "official_exe", str(issue.get("conn") or "")
            impact, category = "retry", "platform"
        else:
            domain, instance, conn = _connection_subject(topology, issue.get("conn"))
            if domain == "candidate" and code in _CANDIDATE_BLOCKING_WIRE_KINDS:
                impact = "block"
            else:
                impact = "retry" if domain in {"opponent", "platform", "harness"} else "review"
            category = "timeout" if "timeout" in code else "protocol"
        findings.append(_finding(
            round_id=round_id,
            index=offset,
            code=code,
            category=category,
            subject_domain=domain,
            subject_instance_id=instance,
            candidate_impact=impact,
            connection=conn,
            evidence=issue,
        ))
    return findings


def _issue_subject(issue: str, topology: dict[str, Any]) -> tuple[str, str, str]:
    lower = issue.lower()
    for conn, item in (topology.get("connections") or {}).items():
        name = str(item.get("name") or "").lower()
        file_prefix = "bota." if conn == "A" else "botb."
        if (name and lower.startswith(name + "_exited_early")) or file_prefix in lower:
            return str(item.get("role") or "harness"), str(item.get("instance_id") or conn), str(conn)
    match = re.search(r"\bconn=([AB])\b", issue)
    if match:
        return _connection_subject(topology, match.group(1))
    if "platform" in lower or "wine" in lower:
        return "platform", "official_exe", ""
    return "harness", "official_harness", ""


def _receipt_findings(
    receipt: dict[str, Any],
    topology: dict[str, Any],
    *,
    start_index: int,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    round_id = str(receipt.get("round_id") or receipt.get("round_kind") or "round")
    observed_exit = receipt.get("observed_exit") if isinstance(receipt.get("observed_exit"), dict) else {}
    platform_exited = observed_exit.get("subject_domain") == "platform"
    replay = receipt.get("wire_replay_summary") if isinstance(receipt.get("wire_replay_summary"), dict) else {}
    structured_wire_codes = {
        str(item.get("kind") or "")
        for item in (replay.get("issues") or [])
        if isinstance(item, dict)
    }
    for offset, raw in enumerate(receipt.get("issues") or [], start=start_index):
        issue = str(raw)
        lower = issue.lower()
        if any(lower.startswith(f"wire_{code.lower()}:") for code in structured_wire_codes if code):
            continue
        domain, instance, conn = _issue_subject(issue, topology)
        code = lower.split(":", 1)[0].strip().replace(" ", "_")[:120] or "round_issue"
        if any(marker in lower for marker in _HARNESS_MARKERS):
            domain = "platform" if ("platform" in lower or "wine" in lower) else "harness"
            instance = "official_exe" if domain == "platform" else "official_harness"
            impact, category = "retry", domain
        elif any(token in lower for token in (
            "connectionreseterror",
            "brokenpipeerror",
            "connectionrefusederror",
            "connectionabortederror",
        )):
            domain, instance, conn = "platform", "official_exe", ""
            impact, category = "retry", "communication"
        elif re.search(r"\b[a-z_][a-z0-9_.]*(?:error|exception)\s*:", lower):
            impact = "block" if domain == "candidate" else "retry"
            category = "runtime"
        elif "traceback" in lower:
            domain, instance, conn = "harness", "official_harness", ""
            impact, category = "retry", "harness"
        elif "_exited_early" in lower:
            if platform_exited:
                domain, instance, conn = "platform", "official_exe", ""
                impact, category = "retry", "platform"
            elif observed_exit.get("subject_domain"):
                domain = str(observed_exit.get("subject_domain"))
                instance = str(observed_exit.get("subject_instance_id") or instance)
                conn = str(observed_exit.get("connection") or conn)
                impact = "block" if domain == "candidate" else "retry"
                category = "runtime"
            else:
                domain, instance, conn = "harness", "official_harness", ""
                impact, category = "retry", "harness"
        elif any(token in lower for token in ("illegal_", "protocol_", "wire_")):
            impact = "block" if domain == "candidate" else "retry"
            category = "protocol"
        else:
            impact, category = "retry", "harness"
        findings.append(_finding(
            round_id=round_id,
            index=offset,
            code=code,
            category=category,
            subject_domain=domain,
            subject_instance_id=instance,
            candidate_impact=impact,
            connection=conn,
            evidence={"issue": issue},
        ))
    return findings


def attribute_round(receipt: dict[str, Any]) -> dict[str, Any]:
    topology = round_topology(receipt)
    findings = _wire_findings(receipt, topology, start_index=1)
    findings.extend(_receipt_findings(receipt, topology, start_index=len(findings) + 1))
    # The formatted wire issues are duplicated in receipt issues. Keep one
    # deterministic finding for each subject/code/evidence tuple.
    unique: dict[str, dict[str, Any]] = {}
    for item in findings:
        key = _digest({
            "code": item.get("code"),
            "subject": item.get("subject_instance_id"),
            "impact": item.get("candidate_impact"),
            "connection": item.get("connection"),
        })
        unique.setdefault(key, item)
    findings = list(unique.values())
    blockers = [item for item in findings if item.get("candidate_impact") == "block"]
    retries = [item for item in findings if item.get("candidate_impact") == "retry"]
    if blockers:
        candidate_verdict = "fail"
    elif retries or not bool(receipt.get("passed")):
        candidate_verdict = "inconclusive"
    else:
        candidate_verdict = "pass"
    return {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "policy_id": ATTRIBUTION_POLICY_ID,
        "topology": topology,
        "findings": findings,
        "candidate_verdict": candidate_verdict,
        "candidate_blocking": bool(blockers),
        "countable": bool(receipt.get("passed")) and candidate_verdict == "pass" and not retries,
        "retry_required": bool(retries) or (not bool(receipt.get("passed")) and not blockers),
        "candidate_finding_ids": [item["finding_id"] for item in blockers],
        "retry_finding_ids": [item["finding_id"] for item in retries],
    }


def attribute_suite(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    attributed = [attribute_round(receipt) for receipt in rounds]
    candidate_blockers = [
        finding
        for item in attributed
        for finding in item.get("findings") or []
        if finding.get("candidate_impact") == "block"
    ]
    retry_findings = [
        finding
        for item in attributed
        for finding in item.get("findings") or []
        if finding.get("candidate_impact") == "retry"
    ]
    if candidate_blockers:
        verdict = "fail"
    elif retry_findings or any(not item.get("countable") for item in attributed):
        verdict = "inconclusive"
    else:
        verdict = "pass"
    return {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "policy_id": ATTRIBUTION_POLICY_ID,
        "candidate_verdict": verdict,
        "candidate_blocking": bool(candidate_blockers),
        "inconclusive": verdict == "inconclusive",
        "countable_rounds": sum(1 for item in attributed if item.get("countable")),
        "rounds": attributed,
        "candidate_findings": candidate_blockers,
        "retry_findings": retry_findings,
    }
