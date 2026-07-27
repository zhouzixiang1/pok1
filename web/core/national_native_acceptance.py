"""Native acceptance and precommit helpers moved from ``national_native``.

Holds the TCP smoke / acceptance / precommit entry points plus their stats
helpers (mean, rounded, ci, first-strict batch progress). Every intra-companion
call to a moved symbol routes through ``_nn.<name>(...)``; main-side constants
and helpers are also accessed via ``_nn.``.
"""

from __future__ import annotations

import asyncio
import statistics
import threading
import time
from pathlib import Path
from typing import Any

from eval_stats import paired_bootstrap_ci
from strength_order import (
    is_precommit_gate_matchup,
    is_strength_matchup,
    precommit_outcome_blockers,
)
from bot_artifact import canonical_digest, hash_path

import national_native as _nn


def _acceptance_opponent_runtime_mode(label: str, path: Path) -> str:
    """Prove that an acceptance opponent is a strict direct artifact."""

    resolved_label, resolved_path = _nn.resolve_bot(path)
    if resolved_label != label or resolved_path != Path(path).absolute():
        raise RuntimeError("strict_policy_opponent_identity_mismatch")
    return "direct_content_bound_policy_artifact"


async def run_native_tcp_smoke(
    candidate_token: str | Path,
    *,
    source_v: int | None = None,
    opponent_token: str | Path | None = None,
    self_play: bool = False,
    hands: int = 1,
    timeout_sec: float | None = 90.0,
    timing_plan: _nn.NativeMatchTimingPlan | dict[str, Any] | None = None,
    progress_callback: Any = None,
) -> dict[str, Any]:
    """Run a minimal direct-TCP national smoke match for a candidate bot."""
    hands = max(1, min(70, int(hands)))
    try:
        candidate_label, candidate_dir = _nn.resolve_bot(candidate_token)
    except Exception as exc:
        return {
            "passed": False,
            "execution_mode": "native_tcp",
            "hands": hands,
            "issues": [f"native_smoke_candidate_error={type(exc).__name__}: {str(exc)[:300]}"],
            "outcome": "candidate_failure",
            "failure_side": "candidate",
        }

    if self_play and opponent_token is not None:
        return {
            "candidate": candidate_label,
            "passed": False,
            "execution_mode": "native_tcp",
            "hands": hands,
            "issues": ["native_smoke_self_play_and_opponent_are_mutually_exclusive"],
            "outcome": "infrastructure_failure",
            "failure_side": "harness",
        }
    if self_play:
        opponents = [(candidate_label, candidate_dir)]
    elif opponent_token is not None:
        try:
            opponents = [_nn.resolve_bot(opponent_token)]
        except Exception as exc:
            return {
                "candidate": candidate_label,
                "passed": False,
                "execution_mode": "native_tcp",
                "hands": hands,
                "issues": [f"native_smoke_opponent_error={type(exc).__name__}: {str(exc)[:300]}"],
                "outcome": "infrastructure_failure",
                "failure_side": "opponent",
            }
    else:
        opponents = _nn.select_acceptance_opponents(candidate_label, source_v, limit=1)

    if not opponents:
        return {
            "candidate": candidate_label,
            "passed": False,
            "execution_mode": "native_tcp",
            "hands": hands,
            "issues": ["native_smoke_no_opponent"],
            "outcome": "infrastructure_failure",
            "failure_side": "opponent",
        }

    opponent_label, opponent_dir = opponents[0]
    try:
        opponent_mode = _nn._acceptance_opponent_runtime_mode(
            opponent_label,
            opponent_dir,
        )
        result = await _nn.run_native_tcp_pair(
            candidate_dir,
            opponent_dir,
            hands,
            timeout_sec=timeout_sec,
            timing_plan=timing_plan,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        return {
            "candidate": candidate_label,
            "opponent": opponent_label,
            "passed": False,
            "execution_mode": "native_tcp",
            "hands": hands,
            "issues": [f"native_smoke_exception={type(exc).__name__}: {str(exc)[:500]}"],
            "outcome": "infrastructure_failure",
            "failure_side": "harness",
        }

    player_rows = list((result.get("per_player") or {}).values())
    if self_play:
        candidate_issues = [
            str(issue)
            for row in player_rows
            for issue in (row.get("compliance_issues") or [])
        ]
        opponent_issues = []
    else:
        candidate_row = (result.get("per_player") or {}).get(candidate_label) or {}
        opponent_row = (result.get("per_player") or {}).get(opponent_label) or {}
        candidate_issues = list(candidate_row.get("compliance_issues") or [])
        opponent_issues = list(opponent_row.get("compliance_issues") or [])
    attributed = set(candidate_issues + opponent_issues)
    unscoped_issues = [
        str(item) for item in result.get("issues") or []
        if str(item) not in attributed
    ]
    if candidate_issues:
        outcome, failure_side, issues = "candidate_failure", "candidate", candidate_issues
    elif opponent_issues or unscoped_issues:
        outcome, failure_side = "infrastructure_failure", (
            "opponent" if opponent_issues and not unscoped_issues else "harness"
        )
        issues = opponent_issues + unscoped_issues
    else:
        outcome, failure_side, issues = "passed", "", []
    passed = outcome == "passed"
    return {
        "candidate": candidate_label,
        "opponent": opponent_label,
        "self_play": bool(self_play),
        "opponent_runtime_mode": opponent_mode,
        "passed": passed,
        "execution_mode": "native_tcp",
        "artifact_execution": result.get("artifact_execution") or {},
        "native_full_match_liveness_budget": result.get(
            "native_full_match_liveness_budget"
        ),
        "native_match_timing_plan": result.get("native_match_timing_plan"),
        "native_match_timing_plan_digest": result.get(
            "native_match_timing_plan_digest"
        ),
        "native_match_timeout_phase": result.get("native_match_timeout_phase"),
        "native_terminal_abort": result.get("native_terminal_abort"),
        "hands": hands,
        "issues": issues,
        "outcome": outcome,
        "failure_side": failure_side,
        "result": result,
    }


def _summary_from_results(bots: list[tuple[str, Path]], results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    runtime_rows: dict[str, list[dict[str, Any]]] = {label: [] for label, _ in bots}
    summary = {
        label: {
            "matches": 0,
            "net_chips": 0,
            "illegal_actions": 0,
            "timeouts": 0,
            "native_process_failures": 0,
            "json_response_stdout": 0,
            "artifact_executions": [],
            "passed_compliance": True,
            "runtime_telemetry": _nn._empty_runtime_telemetry(),
        }
        for label, _ in bots
    }
    for result in results:
        for label, pdata in result["per_player"].items():
            row = summary[label]
            row["matches"] += 1
            row["net_chips"] += int(pdata.get("earnings", 0) or 0)
            row["illegal_actions"] += int(pdata.get("illegal_actions", 0) or 0)
            row["timeouts"] += int(pdata.get("timeouts", 0) or 0)
            runtime_rows.setdefault(label, []).append(pdata.get("runtime_telemetry", {}) or {})
            native = pdata.get("native", {}) or {}
            row["native_process_failures"] += int(native.get("process_failures", 0) or 0)
            row["json_response_stdout"] += int(native.get("json_response_stdout", 0) or 0)
            row["artifact_executions"].append(
                dict(pdata.get("artifact_execution") or {})
            )
            row["passed_compliance"] = (
                row["passed_compliance"]
                and bool(pdata.get("passed_compliance", result.get("passed_compliance", False)))
            )
    for label, rows in runtime_rows.items():
        if label in summary:
            summary[label]["runtime_telemetry"] = _nn._merge_runtime_telemetry(rows)
    return summary


async def run_native_acceptance_for_candidate(
    candidate_token: str | Path,
    *,
    source_v: int | None = None,
    opponent_tokens: list[str | Path] | None = None,
    hands: int = 70,
    max_opponents: int = 2,
    timeout_sec: float | None = None,
    timing_plan: _nn.NativeMatchTimingPlan | dict[str, Any] | None = None,
    progress_callback: Any = None,
) -> _nn.NationalAcceptanceResult:
    candidate = _nn.resolve_bot(candidate_token)
    if opponent_tokens:
        opponents = [_nn.resolve_bot(token) for token in opponent_tokens]
    else:
        opponents = _nn.select_acceptance_opponents(candidate[0], source_v, limit=max_opponents)
    bots = [candidate] + [opp for opp in opponents if opp[0] != candidate[0]]
    if len(bots) < 2:
        return _nn.NationalAcceptanceResult(
            candidate=candidate[0],
            opponents=[],
            hands_per_pair=hands,
            passed=False,
            outcome="infrastructure_failure",
            failure_side="opponent",
            issues=["need at least one opponent for native national acceptance"],
            summary={"passed_compliance": False},
            report={"execution_mode": "native_tcp"},
        )
    pair_indices = [(0, idx) for idx in range(1, len(bots))]
    if timeout_sec is None:
        timeout_sec = max(180.0, float(hands * len(pair_indices) * 5))

    results: list[dict[str, Any]] = []
    opponent_runtime_modes: dict[str, str] = {}
    try:
        for pair_index, (i, j) in enumerate(pair_indices):
            pair_seed = 71_000 + pair_index * 1_000
            bot_seed = 171_000 + pair_index * 1_000
            mode = _nn._acceptance_opponent_runtime_mode(bots[j][0], bots[j][1])
            opponent_runtime_modes[bots[j][0]] = mode
            result = await _nn.run_native_tcp_pair(
                bots[i][1],
                bots[j][1],
                hands,
                deck_seed_base=pair_seed,
                bot_seed_base=bot_seed,
                timeout_sec=timeout_sec,
                timing_plan=timing_plan,
                progress_callback=progress_callback,
            )
            results.append(result)
    except TimeoutError:
        issue = f"native_national_acceptance_timeout: exceeded {timeout_sec:g}s"
        return _nn.NationalAcceptanceResult(
            candidate=candidate[0],
            opponents=[opp[0] for opp in bots[1:]],
            hands_per_pair=hands,
            passed=False,
            outcome="infrastructure_failure",
            failure_side="harness",
            issues=[issue],
            summary={
                "matches": 0,
                "net_chips": 0,
                "passed_compliance": False,
            },
            report={
                "generated_at": _nn.datetime.now().isoformat(timespec="seconds"),
                "hands_per_pair": hands,
                "execution_mode": "native_tcp",
                "candidate_only": True,
                "timeout_sec": timeout_sec,
                "timed_out": True,
                "issues": [issue],
            },
        )

    summary = _nn._summary_from_results(bots, results)
    matrix: dict[str, dict[str, Any]] = {label: {} for label, _ in bots}
    for result in results:
        a = result["bot_a"]
        b = result["bot_b"]
        matrix[a][b] = {
            "net_chips": result["net_chips_a"],
            "per_hand": result["net_chips_a_per_hand"],
            "passed_compliance": result["passed_compliance"],
            "artifact_execution": result.get("artifact_execution") or {},
            "native_full_match_liveness_budget": result.get(
                "native_full_match_liveness_budget"
            ),
            "native_match_timing_plan": result.get("native_match_timing_plan"),
            "native_match_timing_plan_digest": result.get(
                "native_match_timing_plan_digest"
            ),
            "native_match_timeout_phase": result.get("native_match_timeout_phase"),
            "native_terminal_abort": result.get("native_terminal_abort"),
            "issues": result["issues"],
        }
        matrix[b][a] = {
            "net_chips": result["net_chips_b"],
            "per_hand": round(result["net_chips_b"] / max(1, result["hands_played"]), 3),
            "passed_compliance": result["passed_compliance"],
            "artifact_execution": result.get("artifact_execution") or {},
            "native_full_match_liveness_budget": result.get(
                "native_full_match_liveness_budget"
            ),
            "native_match_timing_plan": result.get("native_match_timing_plan"),
            "native_match_timing_plan_digest": result.get(
                "native_match_timing_plan_digest"
            ),
            "native_match_timeout_phase": result.get("native_match_timeout_phase"),
            "native_terminal_abort": result.get("native_terminal_abort"),
            "issues": result["issues"],
        }
    report = {
        "generated_at": _nn.datetime.now().isoformat(timespec="seconds"),
        "hands_per_pair": hands,
        "execution_mode": "native_tcp",
        "artifact_executions": [
            dict(result.get("artifact_execution") or {}) for result in results
        ],
        "pair_count": len(pair_indices),
        "bots": [{"label": label, "path": str(path)} for label, path in bots],
        "results": results,
        "full_match_liveness_budgets": [
            result.get("native_full_match_liveness_budget")
            for result in results
        ],
        "native_match_timing_plans": [
            result.get("native_match_timing_plan") for result in results
        ],
        "native_match_timing_plan_digests": [
            result.get("native_match_timing_plan_digest") for result in results
        ],
        "opponent_runtime_modes": opponent_runtime_modes,
        "summary": summary,
        "matrix": matrix,
        "candidate_only": True,
        "timeout_sec": timeout_sec,
    }
    candidate_summary = summary.get(candidate[0], {})
    candidate_issues: list[str] = []
    opponent_issues: list[str] = []
    unscoped_issues: list[str] = []
    for result in results:
        rows = result.get("per_player") or {}
        candidate_issues.extend((rows.get(candidate[0]) or {}).get("compliance_issues") or [])
        for opponent in bots[1:]:
            opponent_issues.extend((rows.get(opponent[0]) or {}).get("compliance_issues") or [])
        attributed = set(candidate_issues + opponent_issues)
        unscoped_issues.extend(
            str(item) for item in result.get("issues") or []
            if str(item) not in attributed
        )
    if candidate_issues:
        outcome, failure_side, issues = "candidate_failure", "candidate", candidate_issues
    elif opponent_issues or unscoped_issues:
        outcome = "infrastructure_failure"
        failure_side = "opponent" if opponent_issues and not unscoped_issues else "harness"
        issues = opponent_issues + unscoped_issues
    else:
        outcome, failure_side, issues = "passed", "", []
    return _nn.NationalAcceptanceResult(
        candidate=candidate[0],
        opponents=[opp[0] for opp in bots[1:]],
        hands_per_pair=hands,
        passed=outcome == "passed" and bool(candidate_summary.get("passed_compliance")),
        outcome=outcome,
        failure_side=failure_side,
        issues=issues,
        summary=candidate_summary,
        matrix=matrix.get(candidate[0], {}),
        report=report,
    )


def _mean(values: list[int]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _rounded(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def _ci(values: list[int]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    return paired_bootstrap_ci(values)


def _first_strict_batch_progress(
    *,
    batch_plan: dict[str, Any],
    control_execution_scope: dict[str, Any],
    timing_plan: _nn.NativeMatchTimingPlan,
    completed_receipts: list[dict[str, Any]],
    state: str,
    next_repeat: int | None,
) -> dict[str, Any]:
    """Build a replay-validated durable projection for a partial v143 batch.

    The journal remains the sole execution authority.  This projection is the
    checkpoint-visible index that proves which ordered physical samples are
    already complete and which exact sample may be requested next.  It does
    not contain raw replay bytes and it never authorizes an unverified receipt.
    """

    from bot_artifact import canonical_digest
    from first_strict_execution_journal import (
        execution_scope_digest,
        normalize_execution_scope,
        read_control_execution_receipt,
    )

    if state not in {"pending_next_sample", "waiting_live_lease", "completed"}:
        raise RuntimeError("first_strict_batch_progress_state_invalid")
    scope = normalize_execution_scope(control_execution_scope)
    expected_digest = str(batch_plan.get("batch_plan_digest") or "")
    if (
        batch_plan.get("schema_version") != 1
        or batch_plan.get("authority") != "native_precommit_batch_v1"
        or len(expected_digest) != 64
        or batch_plan.get("timing_plan_digest") != timing_plan.digest()
        or batch_plan.get("max_new_samples_per_invocation") != 1
    ):
        raise RuntimeError("first_strict_batch_plan_invalid")
    raw_rows = batch_plan.get("ordered_samples")
    if not isinstance(raw_rows, list) or len(raw_rows) != 8:
        raise RuntimeError("first_strict_batch_plan_rows_invalid")
    scope_digest = execution_scope_digest(scope)
    planned_rows: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_rows, start=1):
        if not isinstance(raw, dict):
            raise RuntimeError("first_strict_batch_plan_row_invalid")
        repeat = raw.get("repeat")
        deck_seed = raw.get("deck_seed_base")
        bot_seed = raw.get("bot_seed_base")
        if (
            raw.get("opponent") != "first_strict_control_v1"
            or raw.get("opponent_index") != 0
            or repeat != index
            or type(deck_seed) is not int
            or type(bot_seed) is not int
            or bot_seed != deck_seed + 1_000_000_000
            or raw.get("native_match_timing_plan_digest") != timing_plan.digest()
        ):
            raise RuntimeError("first_strict_batch_plan_row_binding_invalid")
        match_identity = {
            "scope": scope,
            "scope_digest": scope_digest,
            "repeat": repeat,
            "deck_seed_base": deck_seed,
            "bot_seed_base": bot_seed,
            "hands": 70,
            "timing_plan": timing_plan.snapshot(),
            "timing_plan_digest": timing_plan.digest(),
        }
        planned_rows.append({
            "repeat": repeat,
            "deck_seed_base": deck_seed,
            "bot_seed_base": bot_seed,
            "match_run_id": "first-strict-native:" + canonical_digest(
                match_identity
            ),
        })
    if batch_plan.get("sample_plan_digest") != canonical_digest({
        "sample_plan": [
            {
                "opponent": "first_strict_control_v1",
                "opponent_index": 0,
                "repeat": row["repeat"],
                "deck_seed_base": row["deck_seed_base"],
                "bot_seed_base": row["bot_seed_base"],
                "native_match_timing_plan_digest": timing_plan.digest(),
            }
            for row in planned_rows
        ]
    }):
        raise RuntimeError("first_strict_batch_sample_digest_invalid")
    if batch_plan.get("batch_plan_digest") != canonical_digest({
        key: value
        for key, value in batch_plan.items()
        if key != "batch_plan_digest"
    }):
        raise RuntimeError("first_strict_batch_digest_invalid")

    completed_by_repeat: dict[int, dict[str, Any]] = {}
    for entry in completed_receipts:
        if not isinstance(entry, dict) or type(entry.get("repeat")) is not int:
            raise RuntimeError("first_strict_batch_completed_entry_invalid")
        repeat = int(entry["repeat"])
        if repeat in completed_by_repeat or not 1 <= repeat <= len(planned_rows):
            raise RuntimeError("first_strict_batch_completed_repeat_invalid")
        receipt = entry.get("execution_receipt")
        evidence, issues = read_control_execution_receipt(
            receipt,
            expected_scope=scope,
        )
        if issues or not isinstance(evidence, dict):
            raise RuntimeError(
                "first_strict_batch_completed_receipt_invalid:"
                + ";".join(str(issue) for issue in issues[:8])
            )
        expected = planned_rows[repeat - 1]
        input_payload = evidence.get("input") or {}
        result_payload = evidence.get("result") or {}
        if (
            input_payload.get("repeat") != repeat
            or input_payload.get("deck_seed_base") != expected["deck_seed_base"]
            or input_payload.get("bot_seed_base") != expected["bot_seed_base"]
            or input_payload.get("match_run_id") != expected["match_run_id"]
            or result_payload.get("match_run_id") != expected["match_run_id"]
            or receipt.get("match_run_id") != expected["match_run_id"]
        ):
            raise RuntimeError("first_strict_batch_completed_binding_invalid")
        completed_by_repeat[repeat] = {
            **expected,
            "execution_receipt": dict(receipt),
        }
    completed_repeats = sorted(completed_by_repeat)
    if completed_repeats != list(range(1, len(completed_repeats) + 1)):
        raise RuntimeError("first_strict_batch_completed_order_invalid")
    if next_repeat is not None and (
        type(next_repeat) is not int
        or not 1 <= next_repeat <= len(planned_rows)
        or next_repeat != len(completed_repeats) + 1
    ):
        raise RuntimeError("first_strict_batch_next_repeat_invalid")
    if state == "completed" and (
        next_repeat is not None or len(completed_repeats) != len(planned_rows)
    ):
        raise RuntimeError("first_strict_batch_completion_invalid")
    return {
        "schema_version": 1,
        "kind": "first-strict-native-precommit-batch-progress",
        "state": state,
        "batch_plan_digest": expected_digest,
        "sample_plan_digest": batch_plan.get("sample_plan_digest"),
        "scope_digest": scope_digest,
        "candidate_artifact_hash": scope["candidate_artifact_hash"],
        "control_artifact_hash": scope["control_artifact_hash"],
        "timing_plan_digest": timing_plan.digest(),
        "sample_count": len(planned_rows),
        "max_new_samples_per_invocation": 1,
        "planned_samples": planned_rows,
        "completed_samples": [completed_by_repeat[key] for key in completed_repeats],
        "next_repeat": next_repeat,
    }


async def run_native_precommit(
    candidate_token: str | Path,
    opponents: list[dict[str, Any]],
    *,
    hands: int = 70,
    matches_per_opponent: int = 1,
    parent_label: str = "",
    deck_seed_base: int | None = 91_000,
    sample_plan: list[dict[str, Any]] | None = None,
    batch_plan: dict[str, Any] | None = None,
    control_execution_scope: dict[str, Any] | None = None,
    cancel_token: threading.Event | None = None,
    timing_plan: _nn.NativeMatchTimingPlan | dict[str, Any] | None = None,
    progress_callback: Any = None,
) -> dict[str, Any]:
    from bot_artifact import canonical_digest, hash_path
    from first_strict_control import validate_control_receipt
    from first_strict_execution_journal import (
        normalize_execution_scope,
        read_control_execution_receipt,
    )
    from national_native_timing import (
        _resolve_native_match_timing_plan,
        validate_native_match_timing_evidence,
    )
    from precommit_eval_contract import build_native_precommit_batch_plan

    candidate = _nn.resolve_bot(candidate_token)
    hands = int(hands)
    if hands != 70:
        raise ValueError(
            f"native precommit strength samples must contain exactly 70 hands; got {hands}"
        )
    precommit_timing_plan = _resolve_native_match_timing_plan(
        timing_plan,
        hands=hands,
        requested_timeout_sec=_nn.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
    )
    matches_per_opponent = max(1, int(matches_per_opponent))
    matchups: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    aggregate_net_chips: list[int] = []
    total_wins = total_losses = total_draws = 0
    resolved_opponents: list[dict[str, Any]] = []
    frozen_samples: dict[tuple[str, int], dict[str, Any]] = {}
    if sample_plan is not None:
        for row in sample_plan:
            if not isinstance(row, dict):
                raise ValueError("native precommit sample plan contains a non-object row")
            key = (str(row.get("opponent") or ""), int(row.get("repeat") or 0))
            if not key[0] or key[1] < 1 or key in frozen_samples:
                raise ValueError("native precommit sample plan has an invalid or duplicate key")
            frozen_samples[key] = dict(row)
        expected_rows = len(opponents) * matches_per_opponent
        if len(frozen_samples) != expected_rows:
            raise ValueError(
                f"native precommit sample plan has {len(frozen_samples)} rows; "
                f"expected {expected_rows}"
            )
    system_control_items = [
        item
        for item in opponents
        if str(item.get("authority") or "") == "system_first_strict_control"
    ]
    first_strict_batch_plan: dict[str, Any] | None = None
    if system_control_items:
        if len(system_control_items) != 1 or len(opponents) != 1:
            raise ValueError("first strict control batch shape is invalid")
        if sample_plan is None or not isinstance(batch_plan, dict):
            raise ValueError("first strict control batch plan is missing")
        try:
            from precommit_eval_contract import build_native_precommit_batch_plan

            expected_batch_plan = build_native_precommit_batch_plan(
                list(sample_plan),
                native_timing_plan=precommit_timing_plan,
                first_strict_control=True,
            )
        except Exception as exc:
            raise ValueError("first strict control batch plan is invalid") from exc
        if batch_plan != expected_batch_plan:
            raise ValueError("first strict control batch plan drifted")
        first_strict_batch_plan = expected_batch_plan
    elif batch_plan is not None:
        raise ValueError("ordinary native precommit must not carry a batch plan")
    if not opponents:
        blockers.append({"reason": "native_no_opponents", "details": "Native precommit requires at least one opponent."})
    def raise_if_cancelled() -> None:
        if cancel_token is not None and cancel_token.is_set():
            raise asyncio.CancelledError(
                "native precommit attempt cancelled before the next full match"
            )

    completed_batch_receipts: list[dict[str, Any]] = []
    new_batch_samples = 0
    for opp_index, item in enumerate(opponents):
        raise_if_cancelled()
        reason = str(item.get("reason") or "precommit")
        token = item.get("path") or item.get("token") or item.get("name")
        system_control = str(item.get("authority") or "") == "system_first_strict_control"
        if system_control:
            from first_strict_control import validate_control_receipt
            from first_strict_execution_journal import normalize_execution_scope

            control_receipt = item.get("control_receipt") or {}
            control_identity = control_receipt.get("control") or {}
            opponent = (
                str(item.get("name") or control_identity.get("control_id") or ""),
                Path(str(token)).absolute(),
            )
            if str(opponent[1]) != str(control_identity.get("path") or ""):
                raise RuntimeError("first_strict_control_path_binding_mismatch")
            control_active_bots = list(
                control_receipt.get("active_policy_bots") or []
            )

            expected_control_flags = {
                "precommit_gate_admitted": True,
                "formal_bootstrap_opponent_admitted": True,
                "strength_admitted": False,
                "rating_eligible": False,
                "official_opponent_eligible": False,
            }
            invalid_flags = [
                field for field, expected in expected_control_flags.items()
                if item.get(field) is not expected
            ]
            if invalid_flags:
                raise RuntimeError(
                    "first_strict_control_flags_invalid:"
                    + ",".join(invalid_flags)
                )
            if item.get("formal_bootstrap_scope") != "first_policy_bot_empty_pool_only":
                raise RuntimeError("first_strict_control_formal_scope_invalid")
            gate_authoritative = True
            strength_authoritative = False
            rating_eligible = False

            control_issues = validate_control_receipt(
                control_receipt,
                candidate_version=control_receipt.get(
                    "candidate_version"
                ),
                source_version=control_receipt.get(
                    "source_version"
                ),
                active_bots=control_active_bots,
                # Plan/receipt construction already performs a full refresh.
                # A cold process or changed ref/stat cache key still forces a
                # complete refresh here; the function also closes with an
                # unconditional full refresh below.
                force_protocol_refresh=False,
            )
            if control_issues:
                raise RuntimeError(
                    "first_strict_control_contract_invalid:"
                    + ";".join(control_issues[:8])
                )
            try:
                normalized_control_execution_scope = normalize_execution_scope(
                    control_execution_scope
                )
            except Exception as exc:
                raise RuntimeError(
                    "first_strict_control_execution_scope_invalid:"
                    + str(exc)
                ) from exc
            expected_execution_bindings = {
                "candidate_version": int(
                    control_receipt.get("candidate_version") or 0
                ),
                "candidate_label": candidate[0],
                "candidate_artifact_hash": hash_path(candidate[1]),
                "control_id": str(item.get("name") or opponent[0]),
                "control_artifact_hash": str(
                    ((control_receipt.get("control") or {}).get("artifact_hash"))
                    or ""
                ),
                "control_receipt_digest": str(
                    control_receipt.get("receipt_digest") or ""
                ),
                "native_match_timing_plan_digest": precommit_timing_plan.digest(),
            }
            mismatched_execution_bindings = [
                field
                for field, expected in expected_execution_bindings.items()
                if normalized_control_execution_scope.get(field) != expected
            ]
            if mismatched_execution_bindings:
                raise RuntimeError(
                    "first_strict_control_execution_scope_binding_mismatch:"
                    + ",".join(mismatched_execution_bindings)
                )
            opponent_runtime_mode = "system_first_strict_control"
        else:
            opponent = _nn.resolve_bot(token)
            normalized_control_execution_scope = None
            gate_authoritative = is_precommit_gate_matchup(item)
            strength_authoritative = is_strength_matchup(item)
            rating_eligible = bool(
                item.get("rating_eligible", strength_authoritative)
            )
            opponent_runtime_mode = _nn._acceptance_opponent_runtime_mode(
                opponent[0], opponent[1]
            )
        resolved_opponents.append({
            "name": item.get("name") or opponent[0],
            "reason": reason,
            "path": str(opponent[1]),
            "runtime_mode": opponent_runtime_mode,
            "precommit_gate_admitted": gate_authoritative,
            "formal_bootstrap_opponent_admitted": bool(
                item.get("formal_bootstrap_opponent_admitted", False)
            ),
            "formal_bootstrap_scope": str(
                item.get("formal_bootstrap_scope") or ""
            ),
            "strength_admitted": strength_authoritative,
            "rating_eligible": rating_eligible,
            "official_opponent_eligible": bool(
                item.get("official_opponent_eligible", not system_control)
            ),
        })
        samples: list[int] = []
        repeats: list[dict[str, Any]] = []
        candidate_issues: list[str] = []
        opponent_issues: list[str] = []
        hands_played_total = 0
        for repeat in range(matches_per_opponent):
            # A first-strict provider invocation may create at most one new
            # physical sample.  Recovered receipts are cheap reads and may be
            # traversed first, but once a fresh runner has completed its
            # journalled receipt, return a durable continuation boundary rather
            # than relying on the same SDK stream for the remaining 7 matches.
            if (
                system_control
                and first_strict_batch_plan is not None
                and new_batch_samples >= int(
                    first_strict_batch_plan[
                        "max_new_samples_per_invocation"
                    ]
                )
            ):
                return {
                    "evaluation_protocol": "national_native_tcp",
                    "candidate": candidate[0],
                    "candidate_path": str(candidate[1]),
                    "opponents": resolved_opponents,
                    "matchups": [],
                    "sample_plan": list(sample_plan or []),
                    "native_match_timing_plan": precommit_timing_plan.snapshot(),
                    "native_match_timing_plan_digest": precommit_timing_plan.digest(),
                    "control_execution_scope": normalized_control_execution_scope,
                    "first_strict_batch_pending": _nn._first_strict_batch_progress(
                        batch_plan=first_strict_batch_plan,
                        control_execution_scope=normalized_control_execution_scope,
                        timing_plan=precommit_timing_plan,
                        completed_receipts=completed_batch_receipts,
                        state="pending_next_sample",
                        next_repeat=repeat + 1,
                    ),
                    "blockers": [],
                    "passed": False,
                }
            raise_if_cancelled()
            if system_control:
                from first_strict_control import validate_control_receipt

                control_issues = validate_control_receipt(
                    control_receipt,
                    candidate_version=control_receipt.get(
                        "candidate_version"
                    ),
                    source_version=control_receipt.get(
                        "source_version"
                    ),
                    active_bots=control_active_bots,
                    force_protocol_refresh=False,
                )
                if control_issues:
                    raise RuntimeError(
                        "first_strict_control_contract_drift:"
                        + ";".join(control_issues[:8])
                    )
            sample_key = (str(item.get("name") or opponent[0]), repeat + 1)
            frozen = frozen_samples.get(sample_key) if sample_plan is not None else None
            if sample_plan is not None and frozen is None:
                raise ValueError(
                    f"native precommit sample plan is missing {sample_key[0]} repeat {sample_key[1]}"
                )
            if frozen is not None and frozen.get(
                "native_match_timing_plan_digest"
            ) != precommit_timing_plan.digest():
                raise ValueError(
                    "native precommit sample plan timing plan digest mismatch:"
                    f"{sample_key[0]}:{sample_key[1]}"
                )
            seed = (
                frozen.get("deck_seed_base")
                if frozen is not None
                else (
                    None
                    if deck_seed_base is None
                    else int(deck_seed_base) + (opp_index * 100_000) + (repeat * 1_000)
                )
            )
            bot_seed = (
                frozen.get("bot_seed_base")
                if frozen is not None
                else (None if seed is None else int(seed) + 1_000_000_000)
            )
            execution_ticket = None
            if system_control:
                from first_strict_execution_journal import begin_control_execution

                execution_ticket = begin_control_execution(
                    scope=normalized_control_execution_scope,
                    repeat=repeat + 1,
                    deck_seed_base=int(seed),
                    bot_seed_base=int(bot_seed),
                    timing_plan=precommit_timing_plan,
                )
                if execution_ticket.get("pending") is True:
                    if first_strict_batch_plan is None:
                        raise RuntimeError("first_strict_live_lease_without_batch_plan")
                    return {
                        "evaluation_protocol": "national_native_tcp",
                        "candidate": candidate[0],
                        "candidate_path": str(candidate[1]),
                        "opponents": resolved_opponents,
                        "matchups": [],
                        "sample_plan": list(sample_plan or []),
                        "native_match_timing_plan": precommit_timing_plan.snapshot(),
                        "native_match_timing_plan_digest": precommit_timing_plan.digest(),
                        "control_execution_scope": normalized_control_execution_scope,
                        "control_execution_pending": execution_ticket,
                        "first_strict_batch_pending": _nn._first_strict_batch_progress(
                            batch_plan=first_strict_batch_plan,
                            control_execution_scope=normalized_control_execution_scope,
                            timing_plan=precommit_timing_plan,
                            completed_receipts=completed_batch_receipts,
                            state="waiting_live_lease",
                            next_repeat=repeat + 1,
                        ),
                        "blockers": [],
                        "passed": False,
                    }
            recovered_execution = bool(
                system_control and execution_ticket.get("recovered") is True
            )
            if recovered_execution:
                result = execution_ticket["execution"]
                execution_receipt = execution_ticket["execution_receipt"]
            else:
                result = await _nn.run_native_strength_pair(
                    candidate[1],
                    opponent[1],
                    hands,
                    deck_seed_base=seed,
                    bot_seed_base=bot_seed,
                    timeout_sec=_nn.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
                    timing_plan=precommit_timing_plan,
                    capture_events=system_control,
                    progress_callback=progress_callback,
                    **(
                        {"control_execution_ticket": execution_ticket}
                        if system_control
                        else {}
                    ),
                )
                execution_receipt = None
            timing_issues = validate_native_match_timing_evidence(
                result,
                timing_plan=precommit_timing_plan,
            )
            if system_control and timing_issues:
                raise RuntimeError(
                    "first_strict_control_execution_timing_evidence_drift:"
                    + ";".join(timing_issues)
                )
            if system_control and not recovered_execution:
                # The runner has already made the atomic durable transition.
                # This idempotent reference read is still bounded so a later
                # operator SQLite lock cannot hang the precommit coroutine.
                reference_deadline = (
                    time.monotonic()
                    + precommit_timing_plan.post_execution_completion_timeout_us
                    / 1_000_000.0
                )
                execution_receipt = await _nn._await_first_strict_control_completion(
                    execution_ticket,
                    result,
                    deadline_monotonic=reference_deadline,
                )
            if system_control:
                if not isinstance(execution_receipt, dict):
                    raise RuntimeError("first_strict_batch_execution_receipt_missing")
                completed_batch_receipts.append({
                    "repeat": repeat + 1,
                    "execution_receipt": execution_receipt,
                })
                if not recovered_execution:
                    new_batch_samples += 1
            # A complete match/journal receipt is the smallest interruptible
            # evidence unit.  Never admit it or launch the next sample after the
            # owning cycle has timed out.
            raise_if_cancelled()
            if system_control:
                # Revalidate after every full match as well as before it.  A
                # concurrently published strict bot, altered system asset, or
                # runtime-template drift revokes the empty-pool authority and
                # must force replanning before this sample is admitted.
                control_issues = validate_control_receipt(
                    control_receipt,
                    candidate_version=control_receipt.get(
                        "candidate_version"
                    ),
                    source_version=control_receipt.get(
                        "source_version"
                    ),
                    active_bots=control_active_bots,
                    force_protocol_refresh=False,
                )
                if control_issues:
                    raise RuntimeError(
                        "first_strict_control_contract_drift_after_match:"
                        + ";".join(control_issues[:8])
                    )
            net = int(result.get("net_chips_a", 0) or 0)
            hands_played = int(result.get("hands_played", 0) or 0)
            hands_played_total += hands_played
            c_issues = [
                str(issue)
                for issue in result.get("issues", [])
                if str(issue).startswith(candidate[0] + ":") or str(issue).startswith("hands_played=")
            ]
            o_issues = [
                str(issue)
                for issue in result.get("issues", [])
                if not (str(issue).startswith(candidate[0] + ":") or str(issue).startswith("hands_played="))
            ]
            complete = hands_played == hands
            compliance_passed = bool(result.get("passed_compliance", False))
            artifact_execution = result.get("artifact_execution") or {}
            expected_execution_artifacts = {
                candidate[0]: (
                    str(normalized_control_execution_scope.get(
                        "candidate_artifact_hash"
                    ) or "")
                    if system_control
                    else hash_path(candidate[1])
                ),
                opponent[0]: (
                    str(normalized_control_execution_scope.get(
                        "control_artifact_hash"
                    ) or "")
                    if system_control
                    else hash_path(opponent[1])
                ),
            }
            artifact_execution_valid = _nn._artifact_execution_is_valid(
                artifact_execution,
                expected_execution_artifacts,
            )
            if not artifact_execution_valid:
                c_issues.append("native_artifact_execution_identity_invalid")
            c_issues.extend(timing_issues)
            sample_valid = (
                complete
                and compliance_passed
                and artifact_execution_valid
                and not c_issues
                and not o_issues
            )
            gate_sample_admitted = gate_authoritative and sample_valid
            strength_sample_admitted = strength_authoritative and sample_valid
            if gate_sample_admitted:
                samples.append(net)
                aggregate_net_chips.append(net)
            candidate_issues.extend(c_issues)
            opponent_issues.extend(o_issues)
            repeat_result = {
                "repeat": repeat + 1,
                "deck_seed_base": seed,
                "bot_seed_base": bot_seed,
                "hands_played": hands_played,
                "net_chips": net,
                "candidate_issues": c_issues,
                "opponent_issues": o_issues,
                "complete": complete,
                "passed_compliance": compliance_passed,
                "sample_valid": sample_valid,
                "precommit_gate_admitted": gate_sample_admitted,
                "formal_bootstrap_opponent_admitted": bool(
                    item.get("formal_bootstrap_opponent_admitted", False)
                ),
                "formal_bootstrap_scope": str(
                    item.get("formal_bootstrap_scope") or ""
                ),
                "strength_admitted": strength_sample_admitted,
                "opponent_runtime_mode": opponent_runtime_mode,
                "rating_eligible": rating_eligible,
                "official_opponent_eligible": bool(
                    item.get("official_opponent_eligible", not system_control)
                ),
                "evaluation_authority": (
                    "first_strict_bootstrap_regression_v1"
                    if system_control
                    else "local_precommit_strength"
                ),
                "artifact_execution": artifact_execution,
                "artifact_execution_valid": artifact_execution_valid,
                "local_runtime_budget": {
                    "profile_id": _nn.NATIVE_MATCH_TIMING_PROFILE_ID,
                    "timing_plan": precommit_timing_plan.snapshot(),
                    "timing_plan_digest": precommit_timing_plan.digest(),
                    "hard_deadline_sec": (
                        precommit_timing_plan.bot_a.hard_deadline_us / 1_000_000.0
                    ),
                    "refinement_budget_sec": (
                        precommit_timing_plan.bot_a.refinement_budget_us / 1_000_000.0
                    ),
                    "baseline_target_sec": (
                        precommit_timing_plan.bot_a.baseline_target_us / 1_000_000.0
                    ),
                    "match_timeout_request_sec": _nn.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
                    "match_timeout_effective_sec": (
                        precommit_timing_plan.effective_timeout_us / 1_000_000.0
                    ),
                    "full_match_liveness_budget": (
                        precommit_timing_plan.liveness_budget_snapshot()
                    ),
                    "scope": (
                        "first_strict_bootstrap_regression_only"
                        if system_control
                        else "local_strength_only"
                    ),
                },
            }
            if system_control:
                # This result is produced by the live native runner. Make the
                # zero-migration authority explicit so the first-strict
                # validator never has to infer it from a missing field.
                repeat_result["migration_projection"] = False
                # Full events/hand records/settlements live only in the
                # content-addressed execution authority.  The checkpoint result
                # carries a small reference plus independently recomputed
                # summary fields.
                repeat_result["execution_receipt"] = execution_receipt
            else:
                repeat_result["raw"] = result
            repeats.append(repeat_result)
        if system_control:
            # Close the cached per-match guard with a full Git/artifact refresh
            # before any samples can leave this function as admitted evidence.
            control_issues = validate_control_receipt(
                control_receipt,
                candidate_version=control_receipt.get("candidate_version"),
                source_version=control_receipt.get("source_version"),
                active_bots=control_active_bots,
                force_protocol_refresh=True,
            )
            if control_issues:
                raise RuntimeError(
                    "first_strict_control_contract_drift_final:"
                    + ";".join(control_issues[:8])
                )
        wins = sum(1 for value in samples if value > 0)
        losses = sum(1 for value in samples if value < 0)
        draws = sum(1 for value in samples if value == 0)
        if gate_authoritative:
            total_wins += wins
            total_losses += losses
            total_draws += draws
        mean = _nn._mean(samples)
        ci_lo, ci_hi = _nn._ci(samples)
        matchup = {
            "opponent": item.get("name") or opponent[0],
            "reason": reason,
            "precommit_gate_admitted": gate_authoritative,
            "formal_bootstrap_opponent_admitted": bool(
                item.get("formal_bootstrap_opponent_admitted", False)
            ),
            "formal_bootstrap_scope": str(
                item.get("formal_bootstrap_scope") or ""
            ),
            "strength_authoritative": strength_authoritative,
            "strength_admitted": strength_authoritative,
            "opponent_runtime_mode": opponent_runtime_mode,
            "rating_eligible": rating_eligible,
            "official_opponent_eligible": bool(
                item.get("official_opponent_eligible", not system_control)
            ),
            "evaluation_authority": (
                "first_strict_bootstrap_regression_v1"
                if system_control
                else "local_precommit_strength"
            ),
            "protocol": "national_native_tcp",
            "hands_per_match": hands,
            "matches": matches_per_opponent,
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "n_played": len(samples),
            "samples_expected": matches_per_opponent,
            "hands_played_total": hands_played_total,
            "net_chips": samples,
            "net_chips_mean": _nn._rounded(mean),
            "net_chip_ci": [_nn._rounded(ci_lo), _nn._rounded(ci_hi)],
            "candidate_compliance_issues": candidate_issues,
            "opponent_compliance_issues": opponent_issues,
            "artifact_executions": [
                row.get("artifact_execution") or {} for row in repeats
            ],
            "repeats": repeats,
        }
        if system_control:
            # The aggregate carries the same explicit zero-migration boundary
            # as every repeat. Absence remains invalid at the fail-closed
            # consumer in first_strict_control.py.
            matchup["migration_projection"] = False
        matchups.append(matchup)
        if gate_authoritative and candidate_issues:
            blockers.append({"reason": "native_candidate_compliance", "opponent": matchup["opponent"], "details": "; ".join(candidate_issues[:5])})
        if gate_authoritative and opponent_issues:
            blockers.append({"reason": "native_opponent_compliance", "opponent": matchup["opponent"], "details": "; ".join(opponent_issues[:5])})
        if gate_authoritative and any(not row["complete"] for row in repeats):
            blockers.append({"reason": "native_incomplete_match", "opponent": matchup["opponent"], "details": f"{hands_played_total}/{hands * matches_per_opponent} hands completed"})
        if gate_authoritative and len(samples) != matches_per_opponent:
            blockers.append({
                "reason": "native_precommit_sample_shortfall",
                "opponent": matchup["opponent"],
                "details": f"{len(samples)}/{matches_per_opponent} complete compliant 70-hand samples admitted",
            })
    agg_mean = _nn._mean(aggregate_net_chips)
    agg_ci_lower, agg_ci_upper = _nn._ci(aggregate_net_chips)
    if not aggregate_net_chips:
        blockers.append({"reason": "native_no_samples", "details": "Native precommit produced zero completed match samples."})
    outcome_blockers, outcome_gate = precommit_outcome_blockers(
        matchups,
        parent_label=parent_label,
        aggregate_reason="aggregate_native_regression",
    )
    blockers.extend(outcome_blockers)
    control_gate = None
    if any(
        str(item.get("authority") or "") == "system_first_strict_control"
        for item in opponents
    ):
        from first_strict_control import control_gate_blockers

        control_blockers, control_gate = control_gate_blockers(
            matchups,
            expected_execution_scope=control_execution_scope,
        )
        blockers.extend(control_blockers)
    paired_payload = {
        "protocol": "national_native_tcp",
        "hands_per_match": hands,
        "matches_per_opponent": matches_per_opponent,
        "aggregate_ci_lower": _nn._rounded(agg_ci_lower),
        "aggregate_ci_upper": _nn._rounded(agg_ci_upper),
        "aggregate_threshold": None,
        "aggregate_gate_bound": outcome_gate.get("primary_match_score"),
        "aggregate_gate_rule": "complete_70_hand_wld_loss_margin",
        "outcome_gate": outcome_gate,
        "first_strict_control_gate": control_gate,
        "net_chips_samples": len(aggregate_net_chips),
        "strength_net_chips_samples": sum(
            len(matchup.get("net_chips") or [])
            for matchup in matchups
            if is_strength_matchup(matchup)
        ),
        "gate_degraded": len(aggregate_net_chips) < 2,
        "net_chips_mean": _nn._rounded(agg_mean),
        "net_chips_std": round(statistics.pstdev(aggregate_net_chips), 1) if len(aggregate_net_chips) > 1 else None,
        "net_chips_min": min(aggregate_net_chips) if aggregate_net_chips else None,
        "net_chips_max": max(aggregate_net_chips) if aggregate_net_chips else None,
        "secondary_net_chip_ci": [_nn._rounded(agg_ci_lower), _nn._rounded(agg_ci_upper)],
    }
    return {
        "evaluation_protocol": "national_native_tcp",
        "candidate": candidate[0],
        "candidate_path": str(candidate[1]),
        "opponents": resolved_opponents,
        "matchups": matchups,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "total_draws": total_draws,
        "aggregate_net_chips": aggregate_net_chips,
        "sample_plan": list(sample_plan or []),
        "native_match_timing_plan": precommit_timing_plan.snapshot(),
        "native_match_timing_plan_digest": precommit_timing_plan.digest(),
        "native_precommit_batch_plan": first_strict_batch_plan,
        "first_strict_batch": (
            _nn._first_strict_batch_progress(
                batch_plan=first_strict_batch_plan,
                control_execution_scope=normalized_control_execution_scope,
                timing_plan=precommit_timing_plan,
                completed_receipts=completed_batch_receipts,
                state="completed",
                next_repeat=None,
            )
            if first_strict_batch_plan is not None
            else None
        ),
        "control_execution_scope": (
            normalized_control_execution_scope
            if any(
                str(item.get("authority") or "")
                == "system_first_strict_control"
                for item in opponents
            )
            else None
        ),
        "paired_bootstrap": paired_payload,
        "artifact_execution_contract": {
            "schema_version": _nn.DIRECT_ARTIFACT_EXECUTION_SCHEMA_VERSION,
            "mode": "direct_content_bound_policy_artifact",
        },
        "blockers": blockers,
        "passed": not blockers,
    }
