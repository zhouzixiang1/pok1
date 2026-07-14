"""Pipeline tools: pre-commit evaluation and inline evaluation (battle-based)."""

import json
import logging
import os
from pathlib import Path
import threading
import time

from bot_namespace import bot_name as active_bot_name, parse_bot_version
from tool_runtime_guard import tool

from evolution_core import (
    get_bot_dir,
    get_active_bots,
)

from tool_helpers import (
    _json_tool_result, _get_ui,
    _matching_checkpoint, _record_gate, _gate_payload, _state_blocked,
    _quality_gate_ok, _review_gate_ok, _critic_gate_ok,
    _select_precommit_opponents, _resolve_version_args,
    _set_pipeline_status,
    _prepare_official_profile_refresh,
)
from evolution_infra import write_pipeline_checkpoint, MAX_PRECOMMIT_RETRIES
from system_log import log_system_event
from pipeline_schema import GateResult, ScoreCard
from workflow_profiles import get_workflow_profile
from failure_classification import INFRA_BLOCKER_REASONS, is_infra_blocker
from pipeline_intents import make_intent
from precommit_eval_contract import (
    PrecommitEvalContractError,
    build_evaluation_contract,
    create_precommit_plan,
    opponents_from_plan,
    validate_evaluation_contract,
    validate_precommit_plan,
)
from strength_order import (
    is_precommit_gate_matchup,
    is_strength_matchup,
)

try:
    from candidate_store import append_candidate_event
except Exception:  # pragma: no cover
    append_candidate_event = None

from logging_config import get_logger
log = get_logger("tool_eval")


# H1 (2026-06-29): thread-safe shutdown flag for precommit-eval cancellation.
# Blocking mirror battles run in short-lived owned executors. A running Python
# thread still cannot be force-cancelled and can keep spawning battles after an
# outer timeout, so this Event is checked between mirror games to let an
# orchestrator CYCLE_TIMEOUT abort the drain promptly.
# Set by orchestrator via set_precommit_shutdown() on timeout/cancel.
_PRECOMMIT_SHUTDOWN = threading.Event()


def set_precommit_shutdown():
    """Signal in-flight precommit mirror battles to abort ASAP.

    Called by the orchestrator's CYCLE_TIMEOUT / CancelledError handler so
    subprocess-spawning drain loops break out instead of running to completion.
    Idempotent; reset_precommit_shutdown() clears it before the next cycle.
    """
    _PRECOMMIT_SHUTDOWN.set()


def reset_precommit_shutdown():
    """Clear the precommit shutdown flag (call at the start of each cycle)."""
    _PRECOMMIT_SHUTDOWN.clear()


async def _abandon_first_strict_generation(payload: dict, *, reason: str):
    """Fence and remove a rejected deterministic first-migration candidate.

    Returning an ``action`` string is not an execution boundary.  This helper
    invokes the existing actor-fenced abandon implementation before the tool
    returns, while retaining a deterministic pause/abandon route if cleanup is
    temporarily rate-limited or otherwise cannot complete.
    """

    try:
        from system_strict_bootstrap import abandon_rejected_blueprint

        checkpoint = _matching_checkpoint(
            payload.get("version"),
            payload.get("source_v"),
        )
        result = await abandon_rejected_blueprint(
            checkpoint,
            reason=str(reason),
            result=dict(payload),
        )
    except Exception as exc:
        result = {
            **dict(payload),
            "action": "abandon_generation",
            "abandoned": False,
            "abandon_error": (
                f"abandon_exception:{type(exc).__name__}:{str(exc)[:300]}"
            ),
        }
    result["abandon_reason"] = str(reason)
    result["intent"] = make_intent(
        "abandoned" if result["abandoned"] else "abandon",
        next_tool=(
            None if result["abandoned"] else "abandon_generation"
        ),
        failure_class=str(result.get("failure_class") or "control_plane"),
        authority="tool:precommit_eval",
        safe_to_auto_execute=not result["abandoned"],
        reason=str(reason),
    )
    return _json_tool_result(result)


def is_precommit_shutdown() -> bool:
    """True if precommit battles have been signalled to abort."""
    return _PRECOMMIT_SHUTDOWN.is_set()


# Group A (root-cause-audit follow-up 2026-06-22): blocker reasons that indicate
# INFRASTRUCTURE failure (daemon crash / CPU contention / slow battle-MC), NOT a
# bot regression. These must NOT force the Orchestrator to rework worker code
# (which is unchanged and would give the same result) — they trigger an
# infra-aware retry with lower n_games instead. v147 timed out on attempt 1/2,
# passed on attempt 3 at n_games=6: the bot was fine, the infra wasn't.
def _is_infra_blocker(reason):
    """True if this blocker reason is an infrastructure failure, not a bot
    regression. Infra blockers trigger retry-with-lower-n_games; regression
    blockers (lost_to_parent / aggregate_precommit_regression / semantic_regression)
    still hard-fail the gate."""
    return is_infra_blocker(reason)


# ──────────────────────────────────────────────
# Precommit eval tuning constants
# ──────────────────────────────────────────────
# Default and max n_games per opponent for precommit eval. 8 gives enough paired
# net-chip observations for the bootstrap gate; 16 is the hard ceiling so
# precommit eval still fits within the cycle budget.
PRECOMMIT_DEFAULT_N_GAMES = 8
PRECOMMIT_MIN_N_GAMES = 4
PRECOMMIT_MAX_N_GAMES = 16

def _env_enabled(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _official_gate_enabled(name: str, *, include_required: bool = True) -> bool:
    return (include_required and _env_enabled("POK_OFFICIAL_REQUIRED")) or _env_enabled(name)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _official_bot_token(value) -> str:
    path = Path(value)
    if path.name == "national_bot.py":
        return str(path.parent)
    return str(path)


def _request_official_precommit_status(
    *,
    candidate,
    self_play_rounds: int,
    opponent_rounds: int,
    target_hands: int,
) -> dict:
    """Queue compliance evidence without bypassing opponent eligibility."""
    from official_certification import (
        STATUS_INCONCLUSIVE,
        STATUS_PENDING,
        build_spec,
        official_compliance_verdict,
        select_official_opponent,
    )
    from official_certification_job import start_or_poll_job

    candidate_token = _official_bot_token(candidate)
    selection = None
    opponent = None
    if opponent_rounds > 0:
        selection = select_official_opponent(
            candidate_token,
            get_active_bots(),
            preferred=os.environ.get("POK_OFFICIAL_OPPONENT", "").strip() or None,
            allow_bootstrap_grandfather=False,
        )
        if not selection.get("selected"):
            return {
                "status": STATUS_INCONCLUSIVE,
                "mode": "compliance",
                "passed": False,
                "blocking": False,
                "inconclusive": True,
                "classification": "inconclusive",
                "issues": ["official_precommit_no_eligible_opponent"],
                "opponent_selection": selection,
            }
        opponent = selection["opponent"]["path"]

    spec = build_spec(
        "compliance",
        candidate_token,
        opponent=opponent,
        self_play_rounds=self_play_rounds,
        opponent_rounds=opponent_rounds,
        target_hands=target_hands,
    )
    job = start_or_poll_job(spec, opponent_selection=selection)
    status = (
        job.get("status")
        if job.get("state") == "completed" and isinstance(job.get("status"), dict)
        else {
            "status": STATUS_PENDING,
            "mode": "compliance",
            "pending": bool(job.get("pending")),
            "queued": job.get("state") == "queued",
            "issues": list(job.get("issues") or []),
            "official_job": job,
            "summary": {
                "self_play_rounds": self_play_rounds,
                "opponent_rounds": opponent_rounds,
                "target_hands": target_hands,
            },
        }
    )
    verdict = official_compliance_verdict(status)
    return {
        **status,
        "blocking": False,
        "inconclusive": bool(verdict.get("inconclusive")),
        "classification": verdict.get("classification"),
        "opponent_selection": status.get("opponent_selection") or selection,
        "request_opponent_selection": selection,
        "official_job": job,
    }


def _national_sample_contract_blockers(
    paired_bootstrap: dict,
    *,
    expected_samples: int,
) -> list[dict]:
    sample_count = int(paired_bootstrap.get("net_chips_samples", 0) or 0)
    blockers: list[dict] = []
    if int(paired_bootstrap.get("hands_per_match", 0) or 0) != 70:
        blockers.append({
            "reason": "national_strength_hands_not_70",
            "details": "Every production national strength sample must be one complete 70-hand match.",
        })
    if sample_count > 0 and sample_count < expected_samples:
        blockers.append({
            "reason": "national_sample_shortfall",
            "details": (
                f"National precommit completed {sample_count}/{expected_samples} "
                "required full-match samples."
            ),
        })
    return blockers


def _national_precommit_shape(workflow_profile, sample_target: int) -> tuple[int, int]:
    if getattr(workflow_profile, "national_execution_mode", None) != "native_tcp":
        raise RuntimeError("precommit supports only national native_tcp evaluation")
    hands = 70
    configured_matches = int(os.environ.get(
        "POK_NATIONAL_PRECOMMIT_MATCHES",
        str(getattr(workflow_profile, "national_precommit_matches", 1)),
    ))
    return (
        max(1, min(70, hands)),
        max(
            2,
            configured_matches,
            min(PRECOMMIT_MAX_N_GAMES, max(PRECOMMIT_MIN_N_GAMES, sample_target)),
        ),
    )


def _observed_native_sample_plan(result: dict) -> list[dict]:
    rows: list[dict] = []
    for opponent_index, matchup in enumerate(result.get("matchups") or []):
        for repeat in matchup.get("repeats") or []:
            rows.append({
                "opponent": str(matchup.get("opponent") or ""),
                "opponent_index": opponent_index,
                "repeat": int(repeat.get("repeat") or 0),
                "deck_seed_base": repeat.get("deck_seed_base"),
                "bot_seed_base": repeat.get("bot_seed_base"),
            })
    return rows


async def _run_national_precommit_backend(
    *,
    v: int,
    source_v: int,
    requested_n_games: int,
    effective_n_games: int | None = None,
    candidate_name: str,
    parent_name: str,
    candidate_entry,
    code_fingerprint: str,
    workflow_profile,
    candidate_id: str,
    opponents: list,
    all_opponents: list,
    precommit_attempt: int,
    initial_blockers: list,
    started_at: float,
    precommit_plan: dict,
    evaluation_contract: dict,
    workflow_run_id: str = "",
    checkpoint_revision: int = 0,
):
    """Run the sole active 70-hand native TCP precommit backend."""
    settings = precommit_plan.get("settings") or {}
    national_hands = int(settings.get("hands_per_match") or 0)
    national_matches = int(settings.get("matches_per_opponent") or 0)

    if getattr(workflow_profile, "national_execution_mode", None) != "native_tcp":
        raise RuntimeError("precommit supports only national native_tcp evaluation")
    native_tcp_mode = True
    system_control_plan = any(
        str(item.get("authority") or "") == "system_first_strict_control"
        for item in opponents
    )
    opponents_with_paths = []
    for item in opponents:
        copied = dict(item)
        if str(item.get("authority") or "") != "system_first_strict_control":
            try:
                copied["path"] = str(
                    get_bot_dir(parse_bot_version(item["name"]))
                )
            except Exception:
                pass
        opponents_with_paths.append(copied)

    blockers = list(initial_blockers or [])
    execution_protocol = "national_native_tcp" if native_tcp_mode else "national"
    control_execution_scope = None
    if system_control_plan:
        control_receipt = opponents_with_paths[0].get("control_receipt") or {}
        control_execution_scope = {
            "workflow_run_id": str(workflow_run_id),
            "checkpoint_revision": int(checkpoint_revision),
            "candidate_version": int(v),
            "candidate_label": str(candidate_name),
            "candidate_artifact_hash": str(code_fingerprint),
            "control_id": str(opponents_with_paths[0].get("name") or ""),
            "control_artifact_hash": str(
                ((control_receipt.get("control") or {}).get("artifact_hash"))
                or ""
            ),
            "control_receipt_digest": str(
                control_receipt.get("receipt_digest") or ""
            ),
            "precommit_plan_digest": str(
                precommit_plan.get("plan_digest") or ""
            ),
            "evaluation_contract_digest": str(
                evaluation_contract.get("contract_digest") or ""
            ),
            "precommit_attempt": int(precommit_attempt),
        }
    if national_hands != 70:
        blockers.append({
            "reason": "national_strength_hands_not_70",
            "details": f"Production precommit requires 70 hands per match; plan requested {national_hands}.",
        })
    if not blockers and opponents_with_paths:
        try:
            from national_native import run_native_precommit

            national_result = await run_native_precommit(
                str(candidate_entry),
                opponents_with_paths,
                hands=national_hands,
                matches_per_opponent=national_matches,
                parent_label=parent_name,
                sample_plan=list(precommit_plan.get("sample_plan") or []),
                control_execution_scope=control_execution_scope,
            )
            blockers.extend(national_result.get("blockers") or [])
            if system_control_plan:
                from first_strict_control import validate_control_receipt

                control_receipt = opponents_with_paths[0].get("control_receipt")
                control_issues = validate_control_receipt(
                    control_receipt,
                    candidate_version=v,
                    source_version=source_v,
                )
                if control_issues:
                    blockers.append({
                        "reason": "first_strict_control_contract_drift",
                        "details": ";".join(control_issues[:12]),
                    })
            if native_tcp_mode and _observed_native_sample_plan(national_result) != list(
                precommit_plan.get("sample_plan") or []
            ):
                blockers.append({
                    "reason": "native_precommit_sample_plan_mismatch",
                    "details": "Native precommit did not execute the frozen deck/bot seed schedule.",
                })
        except Exception as exc:
            national_result = {
                "evaluation_protocol": execution_protocol,
                "candidate": candidate_name,
                "opponents": opponents_with_paths,
                "matchups": [],
                "total_wins": 0,
                "total_losses": 0,
                "total_draws": 0,
                "paired_bootstrap": {
                    "protocol": execution_protocol,
                    "hands_per_match": national_hands,
                    "matches_per_opponent": national_matches,
                    "net_chips_samples": 0,
                    "net_chips_mean": None,
                    "gate_degraded": True,
                },
                "blockers": [{
                    "reason": "native_precommit_exception" if native_tcp_mode else "national_precommit_exception",
                    "details": f"{type(exc).__name__}: {str(exc)[:500]}",
                }],
                "passed": False,
            }
            blockers.extend(national_result["blockers"])
    else:
        national_result = {
            "evaluation_protocol": execution_protocol,
            "candidate": candidate_name,
            "opponents": opponents_with_paths,
            "matchups": [],
            "total_wins": 0,
            "total_losses": 0,
            "total_draws": 0,
            "paired_bootstrap": {
                "protocol": execution_protocol,
                "hands_per_match": national_hands,
                "matches_per_opponent": national_matches,
                "net_chips_samples": 0,
                "net_chips_mean": None,
                "gate_degraded": True,
            },
            "blockers": blockers,
            "passed": False,
        }

    official_platform_result = {}
    if native_tcp_mode and _official_gate_enabled("POK_OFFICIAL_PRECOMMIT_GATE") and not blockers:
        # The official Windows platform is a protocol/compliance oracle here.
        # Strength and long-run tracking stay on the local native TCP harness.
        official_self_rounds = max(0, _env_int("POK_OFFICIAL_PRECOMMIT_SELF_ROUNDS", 1))
        official_opponent_rounds = max(0, _env_int("POK_OFFICIAL_PRECOMMIT_OPPONENT_ROUNDS", 1))
        official_hands = max(1, min(70, _env_int("POK_OFFICIAL_PRECOMMIT_TARGET_HANDS", 10)))
        try:
            official_platform_result = _request_official_precommit_status(
                candidate=candidate_entry,
                self_play_rounds=official_self_rounds,
                opponent_rounds=official_opponent_rounds,
                target_hands=official_hands,
            )
            national_result["official_platform"] = official_platform_result
        except Exception as exc:
            official_platform_result = {
                "passed": False,
                "blocking": False,
                "issues": [f"official_platform_compliance_exception: {type(exc).__name__}: {str(exc)[:500]}"],
            }
            national_result["official_platform"] = official_platform_result

    total_wins = int(national_result.get("total_wins", 0) or 0)
    total_losses = int(national_result.get("total_losses", 0) or 0)
    total_draws = int(national_result.get("total_draws", 0) or 0)
    matchups = list(national_result.get("matchups") or [])
    paired_bootstrap_payload = dict(national_result.get("paired_bootstrap") or {})
    from strength_order import summarize_70_hand_net_chips, summarize_match_outcomes

    gate_samples = [
        int(value)
        for matchup in matchups
        if is_precommit_gate_matchup(matchup)
        for value in (matchup.get("net_chips") or [])
    ]
    strength_samples = [
        int(value)
        for matchup in matchups
        if is_strength_matchup(matchup)
        for value in (matchup.get("net_chips") or [])
    ]
    precommit_gate_order = summarize_70_hand_net_chips(gate_samples)
    strength_order = summarize_70_hand_net_chips(strength_samples)
    outcome_order = summarize_match_outcomes(total_wins, total_losses, total_draws)
    gate_sample_count = int(
        paired_bootstrap_payload.get("net_chips_samples", 0) or 0
    )
    strength_sample_count = int(
        paired_bootstrap_payload.get(
            "strength_net_chips_samples",
            gate_sample_count,
        )
        or 0
    )
    expected_gate_samples = national_matches * sum(
        1 for item in opponents_with_paths if is_precommit_gate_matchup(item)
    )
    expected_strength_samples = national_matches * sum(
        1 for item in opponents_with_paths if is_strength_matchup(item)
    )
    strength_evidence_required = bool(
        settings.get("strength_evidence_required", True)
    )
    minimum_gate_samples = int(
        settings.get("control_min_samples") or 2
    )
    if gate_sample_count <= 0 and not blockers:
        blockers.append({
            "reason": "national_no_samples",
            "details": "National precommit produced zero completed match samples.",
        })
    blockers.extend(
        _national_sample_contract_blockers(
            paired_bootstrap_payload,
            expected_samples=expected_gate_samples,
        )
    )
    if gate_sample_count != outcome_order["samples"]:
        blockers.append({
            "reason": "national_outcome_sample_mismatch",
            "details": (
                f"Outcome counts describe {outcome_order['samples']} samples but "
                f"the admitted precommit vector contains {gate_sample_count}."
            ),
        })
    if precommit_gate_order["samples"] != gate_sample_count or (
        precommit_gate_order["positive_matches"] != total_wins
        or precommit_gate_order["negative_matches"] != total_losses
        or precommit_gate_order["zero_matches"] != total_draws
    ):
        blockers.append({
            "reason": "national_precommit_sign_mismatch",
            "details": "Admitted precommit net-chip signs disagree with the recorded W/L/D outcomes.",
        })
    if strength_order["samples"] != strength_sample_count:
        blockers.append({
            "reason": "national_strength_sample_mismatch",
            "details": (
                f"Strength vector contains {strength_order['samples']} samples but "
                f"the runtime declared {strength_sample_count}."
            ),
        })
    if strength_evidence_required and (
        strength_order["positive_matches"] != total_wins
        or strength_order["negative_matches"] != total_losses
        or strength_order["zero_matches"] != total_draws
    ):
        blockers.append({
            "reason": "national_strength_sign_mismatch",
            "details": "Admitted strength signs disagree with the recorded W/L/D outcomes.",
        })
    if system_control_plan:
        from first_strict_control import (
            control_gate_blockers,
            validate_control_receipt,
        )

        control_blockers, control_gate = control_gate_blockers(
            national_result,
            expected_sample_plan=list(precommit_plan.get("sample_plan") or []),
            expected_execution_scope=control_execution_scope,
        )
        existing_reasons = {
            str(item.get("reason") or "")
            for item in blockers
            if isinstance(item, dict)
        }
        blockers.extend(
            item for item in control_blockers
            if str(item.get("reason") or "") not in existing_reasons
        )
        final_control_issues = validate_control_receipt(
            opponents_with_paths[0].get("control_receipt"),
            candidate_version=v,
            source_version=source_v,
        )
        if final_control_issues and not any(
            isinstance(item, dict)
            and item.get("reason") == "first_strict_control_contract_drift"
            for item in blockers
        ):
            blockers.append({
                "reason": "first_strict_control_contract_drift",
                "details": ";".join(final_control_issues[:12]),
            })
    else:
        control_gate = None
    passed = (
        bool(national_result.get("passed"))
        and len(blockers) == 0
        and gate_sample_count == expected_gate_samples
        and strength_sample_count == expected_strength_samples
        and gate_sample_count >= minimum_gate_samples
        and (not strength_evidence_required or strength_sample_count >= 2)
    )

    try:
        log_system_event(
            "pipeline.precommit_eval.national",
            "info" if passed else "warn",
            f"National {'native TCP ' if native_tcp_mode else ''}precommit "
            f"{'passed' if passed else 'FAILED'} for v{v}: "
            f"{total_wins}W-{total_losses}L-{total_draws}D vs {len(all_opponents)} opponents",
            {
                "version": v,
                "source_v": source_v,
                "passed": passed,
                "evaluation_protocol": execution_protocol,
                "execution_mode": "native_tcp",
                "hands_per_match": national_hands,
                "matches_per_opponent": national_matches,
                "blockers": blockers,
                "paired_bootstrap": paired_bootstrap_payload,
                "elapsed_sec": round(time.time() - started_at, 2),
            },
        )
    except Exception:
        pass

    result = {
        "version": v,
        "source_v": source_v,
        "n_games": national_matches,
        "requested_n_games": requested_n_games,
        "workflow_profile_id": workflow_profile.profile_id,
        "evaluation_protocol": execution_protocol,
        "national_execution_mode": "native_tcp",
        "hands_per_match": national_hands,
        "matches_per_opponent": national_matches,
        "expected_net_chips_samples": expected_gate_samples,
        "expected_strength_net_chips_samples": expected_strength_samples,
        "opponents": all_opponents,
        "matchups": matchups,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "total_draws": total_draws,
        "passed": passed,
        "blockers": blockers,
        "paired_bootstrap": paired_bootstrap_payload,
        "precommit_gate_order": precommit_gate_order,
        "strength_order": strength_order,
        "outcome_order": outcome_order,
        "precommit_evidence_authority": (
            "first_strict_bootstrap_regression_v1"
            if system_control_plan
            else "local_precommit_strength"
        ),
        "precommit_gate_admitted": True,
        "strength_admitted": not system_control_plan,
        "rating_eligible": not system_control_plan,
        "official_opponent_eligible": not system_control_plan,
        "first_strict_control_gate": control_gate,
        "primary_70_hand_match_score": outcome_order.get("primary_match_score"),
        "secondary_net_chips_total": strength_order.get("secondary_net_chips_total"),
        "secondary_net_chips_mean": strength_order.get("secondary_net_chips_mean"),
        "precommit_gate_net_chips_total": precommit_gate_order.get(
            "secondary_net_chips_total"
        ),
        "precommit_gate_net_chips_mean": precommit_gate_order.get(
            "secondary_net_chips_mean"
        ),
        "national": national_result,
        "official_platform": official_platform_result,
        "code_fingerprint": code_fingerprint,
        "precommit_eval_plan": precommit_plan,
        "precommit_eval_contract": evaluation_contract,
        "precommit_eval_contract_digest": evaluation_contract.get("contract_digest"),
        "control_execution_scope": (
            national_result.get("control_execution_scope")
            if system_control_plan
            else None
        ),
    }

    scorecard = ScoreCard(
        name="precommit_eval",
        primary_score=outcome_order.get("primary_match_score"),
        metrics={
            "evaluation_protocol": execution_protocol,
            "national_execution_mode": "native_tcp",
            "total_wins": total_wins,
            "total_losses": total_losses,
            "total_draws": total_draws,
            "n_opponents": len(all_opponents),
            "hands_per_match": national_hands,
            "matches_per_opponent": national_matches,
            "primary_70_hand_match_score": outcome_order.get("primary_match_score"),
            "secondary_net_chips_mean": strength_order.get("secondary_net_chips_mean"),
            "precommit_evidence_authority": (
                "first_strict_bootstrap_regression_v1"
                if system_control_plan
                else "local_precommit_strength"
            ),
            "strength_admitted": not system_control_plan,
            "rating_eligible": not system_control_plan,
            "official_opponent_eligible": not system_control_plan,
        },
    )
    scorecard.add(GateResult.from_bool(
        "national_precommit_regression",
        passed,
        metrics=paired_bootstrap_payload,
        failures=[str(b)[:500] for b in blockers],
    ))
    if official_platform_result:
        official_status = str(official_platform_result.get("status") or "")
        official_issues = official_platform_result.get("issues", []) or []
        try:
            from official_certification import official_compliance_verdict as _official_compliance_verdict
            official_verdict = _official_compliance_verdict(official_platform_result)
        except Exception:
            official_verdict = {
                "ok": True,
                "blocking": False,
                "inconclusive": True,
                "classification": "inconclusive",
            }
        scorecard.add(GateResult.from_bool(
            "official_platform_compliance",
            bool(official_verdict.get("ok")),
            metrics={
                "status": official_status,
                "mode": official_platform_result.get("mode"),
                "queued": official_platform_result.get("queued"),
                "cache_hit": official_platform_result.get("cache_hit"),
                "blocking": official_verdict.get("blocking"),
                "inconclusive": official_verdict.get("inconclusive"),
                "classification": official_verdict.get("classification"),
                **(official_platform_result.get("summary", {}) or {}),
            },
            failures=official_issues[:5] if bool(official_verdict.get("blocking")) else [],
            artifacts={"report": official_platform_result},
            blocking=False,
        ))
    result["scorecard"] = scorecard.model_dump()

    if passed:
        result["failure_class"] = "passed"
        result["intent"] = make_intent(
            "continue",
            next_tool="commit_bot",
            authority="tool:precommit_eval",
            safe_to_auto_execute=True,
        )
    else:
        worst_opponent = _worst_precommit_opponent(matchups, blockers)
        worst_wins, worst_losses = _worst_wins_losses(matchups, worst_opponent)
        if system_control_plan:
            result["directive"] = (
                f"First-strict system-control precommit FAILED for v{v} "
                f"({worst_wins}W-{worst_losses}L). Do not invoke an ordinary "
                "Worker repair and do not retry unchanged code. Abandon this "
                "generation, revise the checked-in deterministic blueprint/control "
                "contract, and restart from a fresh empty-pool authority receipt."
            )
            result["failure_class"] = "system_bootstrap_regression"
            result["intent"] = make_intent(
                "pause",
                failure_class="system_bootstrap_regression",
                authority="tool:precommit_eval",
                safe_to_auto_execute=False,
                reason="first_strict_control_regression",
            )
        else:
            result["directive"] = (
                f"National precommit FAILED (attempt {precommit_attempt}/{MAX_PRECOMMIT_RETRIES}) — "
                f"the final gate now uses {'native TCP ' if native_tcp_mode else ''}national 70-hand rules, not local mirror battle. "
                f"Do NOT call run_precommit_eval again on unchanged code. Rework the bot against "
                f"{worst_opponent} ({worst_wins}W-{worst_losses}L) and the listed blockers."
            )
            result["failure_class"] = "regression"
            result["intent"] = make_intent(
                "rework",
                next_tool="execute_workers",
                failure_class="regression",
                authority="tool:precommit_eval",
                safe_to_auto_execute=True,
                reason="national_precommit_regression",
            )

    checkpoint_stage = "verified" if passed else "precommit_failed"
    checkpoint_feedback = None if passed else result.get("directive")
    checkpoint_recorded = _record_gate(
        v,
        source_v,
        "precommit_eval",
        _gate_payload(
            v,
            source_v,
            passed,
            **{k: val for k, val in result.items() if k not in {"version", "source_v", "passed"}},
        ),
        stage=checkpoint_stage,
        reviewer_feedback=checkpoint_feedback,
    )
    result["checkpoint_recorded"] = checkpoint_recorded

    if append_candidate_event:
        try:
            append_candidate_event(
                "precommit_finished",
                version=v,
                source_v=source_v,
                candidate_id=candidate_id,
                profile_id=workflow_profile.profile_id,
                workflow_profile_id=workflow_profile.profile_id,
                run_id=f"{v}#0",
                stage="verified" if passed else "precommit_failed",
                parent_ids=[active_bot_name(source_v)],
                gate="precommit_eval",
                scorecard=scorecard,
                gate_results=scorecard.gates,
                metrics={
                    "passed": passed,
                    "evaluation_protocol": execution_protocol,
                    "national_execution_mode": "native_tcp",
                    "total_wins": total_wins,
                    "total_losses": total_losses,
                    "total_draws": total_draws,
                    "net_chips_mean": paired_bootstrap_payload.get("net_chips_mean"),
                },
                failures=[str(b)[:500] for b in blockers],
                failure_class=(
                    ""
                    if passed
                    else "system_bootstrap_regression"
                    if system_control_plan
                    else "national_precommit_regression"
                ),
            )
        except Exception as e:
            log.warning("candidate ledger national precommit_finished write failed: %s", e)
    if system_control_plan and not passed:
        return await _abandon_first_strict_generation(
            result,
            reason="first_strict_control_precommit_rejected",
        )
    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Precommit Eval
# ──────────────────────────────────────────────


def _worst_precommit_opponent(matchups, blockers):
    """Return the opponent name most responsible for a precommit failure.

    Priority: the first blocker that names a regression opponent
    (lost_to_parent / lost_to_opponent), else the matchup with the most losses,
    else the matchup with the worst W-L margin. Returns "unknown" if there are
    no matchups and no named blockers.
    """
    if blockers:
        for b in blockers:
            reason = b.get("reason") if isinstance(b, dict) else None
            if reason in ("lost_to_parent", "lost_to_opponent"):
                opp = b.get("opponent")
                if opp:
                    return opp
    if matchups:
        best = None
        best_key = None
        for m in matchups:
            # Typed non-gate matchups are not valid failure-attribution targets.
            if m.get("precommit_gate_admitted") is False:
                continue
            opp = m.get("opponent")
            losses = int(m.get("losses", 0) or 0)
            wins = int(m.get("wins", 0) or 0)
            # Sort by (most losses, then worst margin) so the heaviest defeat wins.
            key = (losses, losses - wins)
            if best_key is None or key > best_key:
                best_key = key
                best = opp
        if best is not None:
            return best
    return "unknown"


def _worst_wins_losses(matchups, opponent):
    """Return (wins, losses) for the given opponent across matchups, else (0, 0)."""
    if not opponent or opponent == "unknown" or not matchups:
        return 0, 0
    for m in matchups:
        if m.get("opponent") == opponent:
            return int(m.get("wins", 0) or 0), int(m.get("losses", 0) or 0)
    return 0, 0


@tool("run_precommit_eval", "Run the final native national-TCP regression check before commit.", {"version": int, "source_v": int, "n_games": int})
async def run_precommit_eval(args):
    _t0 = time.time()
    v, source_v = _resolve_version_args(args)
    if v is None or source_v is None:
        return _json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)
    # Cap n_games: precommit eval is a quick regression check, NOT a full evaluation.
    # Default is PRECOMMIT_DEFAULT_N_GAMES (8), clamped to
    # [PRECOMMIT_MIN_N_GAMES, PRECOMMIT_MAX_N_GAMES]. The regression gate now uses paired net-chip
    # bootstrap CIs, which are much less noisy than binary W/L at the same n_games.
    requested = int(args.get("n_games", PRECOMMIT_DEFAULT_N_GAMES) or PRECOMMIT_DEFAULT_N_GAMES)
    n_games = min(max(PRECOMMIT_MIN_N_GAMES, requested), PRECOMMIT_MAX_N_GAMES)

    candidate_name = active_bot_name(v)
    parent_name = active_bot_name(source_v)
    candidate_dir = get_bot_dir(v)
    try:
        from tool_gates import _bot_code_fingerprint
        code_fingerprint = _bot_code_fingerprint(candidate_dir)
    except Exception:
        code_fingerprint = ""

    workflow_profile = get_workflow_profile()
    if (
        getattr(workflow_profile, "national_execution_mode", None) != "native_tcp"
        or getattr(workflow_profile, "evaluation_protocol", None) != "national"
    ):
        return _json_tool_result({"error": "only national native_tcp precommit is supported"})
    native_tcp_mode = True
    expected_execution_mode = "native_tcp"
    evaluation_protocol = "national"
    national_evaluation = True
    candidate_entry = candidate_dir / "national_bot.py"

    # Idempotency guard: skip if precommit eval already passed for the same code snapshot
    # under the same workflow profile and national execution mode.
    _precommit_ckpt = _matching_checkpoint(v, source_v)
    stored_plan = (
        ((_precommit_ckpt.get("audit_context") or {}).get("precommit_eval_plan"))
        if _precommit_ckpt
        else None
    )
    stored_plan_issues = (
        validate_precommit_plan(
            stored_plan,
            candidate_version=v,
            source_version=source_v,
            profile_id=workflow_profile.profile_id,
            execution_mode=expected_execution_mode,
            evaluation_protocol=evaluation_protocol,
        )
        if national_evaluation and stored_plan is not None
        else []
    )
    current_evaluation_contract = (
        build_evaluation_contract(
            stored_plan,
            candidate_code_fingerprint=code_fingerprint,
        )
        if national_evaluation and stored_plan is not None and not stored_plan_issues
        else None
    )
    profile_refresh = _prepare_official_profile_refresh(_precommit_ckpt, "run_precommit_eval")
    if not profile_refresh.get("ok"):
        return _state_blocked(
            str(profile_refresh.get("error") or "official profile refresh preparation failed"),
            v,
            source_v,
            _precommit_ckpt,
        )
    if _precommit_ckpt and _precommit_ckpt.get("stage") in (
        "verified", "archived"
    ):
        precommit_gate = _precommit_ckpt.get("gate_results", {}).get("precommit_eval", {})
        cached_fingerprint = precommit_gate.get("code_fingerprint")
        cached_profile_id = str(precommit_gate.get("workflow_profile_id") or precommit_gate.get("profile_id") or "")
        cached_execution_mode = str(precommit_gate.get("national_execution_mode") or "")
        cached_contract = precommit_gate.get("precommit_eval_contract")
        if workflow_profile.profile_id == "default":
            cache_profile_matches = (
                cached_profile_id in {"", "default"}
                and cached_execution_mode in {"", expected_execution_mode}
            )
        else:
            cache_profile_matches = (
                cached_profile_id == workflow_profile.profile_id
                and cached_execution_mode == expected_execution_mode
            )
        contract_matches = (
            not national_evaluation
            or (
                current_evaluation_contract is not None
                and not validate_evaluation_contract(
                    cached_contract,
                    stored_plan,
                    candidate_code_fingerprint=code_fingerprint,
                )
            )
        )
        if (
            precommit_gate.get("passed") is True
            and cached_fingerprint == code_fingerprint
            and cache_profile_matches
            and contract_matches
        ):
            precommit_gate["idempotent_cache"] = True
            precommit_gate["directive"] = (
                "Precommit eval ALREADY PASSED. Do NOT re-run. "
                "Call commit_bot(version, source_v, strategy, review_approved=true) next."
            )
            return _json_tool_result(precommit_gate)
        if precommit_gate.get("passed") is True:
            log_system_event(
                "pipeline.precommit_cache_stale",
                "warn",
                f"Precommit cache stale for v{v}; cached code/profile does not match active eval requirements.",
                {
                    "version": v,
                    "source_v": source_v,
                    "cached_fingerprint": cached_fingerprint,
                    "current_fingerprint": code_fingerprint,
                    "cached_workflow_profile_id": cached_profile_id,
                    "active_workflow_profile_id": workflow_profile.profile_id,
                    "cached_execution_mode": cached_execution_mode,
                    "active_execution_mode": expected_execution_mode,
                    "precommit_plan_issues": stored_plan_issues,
                    "cached_contract_digest": precommit_gate.get("precommit_eval_contract_digest"),
                    "active_contract_digest": (
                        current_evaluation_contract.get("contract_digest")
                        if current_evaluation_contract
                        else None
                    ),
                },
            )

    _set_pipeline_status(f"Pre-commit eval for v{v}")

    candidate_id = f"{candidate_name}_from_v{source_v}"
    if append_candidate_event:
        try:
            append_candidate_event(
                "precommit_started",
                version=v,
                source_v=source_v,
                candidate_id=candidate_id,
                profile_id=workflow_profile.profile_id,
                workflow_profile_id=workflow_profile.profile_id,
                run_id=f"{v}#0",
                stage="precommit_eval",
                parent_ids=[active_bot_name(source_v)],
                gate="precommit_eval",
                metrics={"n_games": n_games},
            )
        except Exception as e:
            log.warning("candidate ledger precommit_started write failed: %s", e)
    blockers = []
    matchups = []

    ckpt = _matching_checkpoint(v, source_v)
    if not _quality_gate_ok(ckpt) or not _review_gate_ok(ckpt) or not _critic_gate_ok(ckpt):
        return _state_blocked(
            "run_precommit_eval requires passing quality/reviewer gates and a completed advisory critic role for the same version/source_v.",
            v,
            source_v,
            ckpt,
        )

    try:
        from system_strict_bootstrap import is_declared_native_bootstrap

        declared_first_strict = is_declared_native_bootstrap(ckpt)
    except Exception:
        declared_first_strict = False
    planned_system_control = bool(
        stored_plan
        and any(
            str(item.get("authority") or "") == "system_first_strict_control"
            for item in (stored_plan.get("opponents") or [])
            if isinstance(item, dict)
        )
    )
    first_strict_control_receipt = None
    if declared_first_strict:
        from first_strict_control import validate_control_receipt

        first_strict_control_receipt = (
            ((ckpt.get("gate_results") or {}).get("quality") or {}).get(
                "first_strict_control_receipt"
            )
        )
        control_issues = validate_control_receipt(
            first_strict_control_receipt,
            checkpoint=ckpt,
            candidate_version=v,
            source_version=source_v,
        )
        if stored_plan is not None and not planned_system_control:
            control_issues.append(
                "first_strict_control_declared_plan_authority_mismatch"
            )
        if planned_system_control:
            planned_receipt = (
                ((stored_plan.get("opponents") or [{}])[0]).get(
                    "control_receipt"
                )
            )
            if planned_receipt != first_strict_control_receipt:
                control_issues.append(
                    "first_strict_control_quality_plan_receipt_mismatch"
                )
        if control_issues:
            return await _abandon_first_strict_generation({
                "error": "FIRST_STRICT_CONTROL_AUTHORITY_INVALID",
                "version": v,
                "source_v": source_v,
                "passed": False,
                "action": "abandon_generation",
                "failure_class": "control_plane",
                "validation_errors": list(dict.fromkeys(control_issues))[:20],
                "intent": make_intent(
                    "pause",
                    failure_class="control_plane",
                    authority="tool:precommit_eval",
                    safe_to_auto_execute=False,
                    reason="first_strict_control_replan_required",
                ),
                "directive": (
                    "The empty-pool/system-control authority drifted. A newly "
                    "published strict bot or changed control/runtime invalidates "
                    "this plan; abandon it and create a fresh opponent plan."
                ),
            }, reason="first_strict_control_authority_invalid")
    elif planned_system_control:
        return await _abandon_first_strict_generation({
            "error": "UNDECLARED_FIRST_STRICT_CONTROL_PLAN",
            "version": v,
            "source_v": source_v,
            "passed": False,
            "action": "abandon_generation",
            "failure_class": "control_plane",
            "intent": make_intent(
                "pause",
                failure_class="control_plane",
                authority="tool:precommit_eval",
                safe_to_auto_execute=False,
                reason="undeclared_first_strict_control_plan",
            ),
        }, reason="undeclared_first_strict_control_plan")

    if not candidate_entry.exists():
        result = {
            "version": v,
            "source_v": source_v,
            "n_games": n_games,
            "code_fingerprint": code_fingerprint,
            "passed": False,
            "blockers": [{"reason": "candidate_missing", "details": str(candidate_entry)}],
            "opponents": [],
            "matchups": [],
        }
        gate_extra = {k: val for k, val in result.items() if k not in {"version", "source_v", "passed"}}
        _record_gate(v, source_v, "precommit_eval", _gate_payload(v, source_v, False, **gate_extra), stage=None)
        if declared_first_strict:
            return await _abandon_first_strict_generation(
                result,
                reason="first_strict_control_candidate_missing",
            )
        return _json_tool_result(result)

    # compile/smoke already verified by quality gates (required by _quality_gate_ok above)

    if national_evaluation and stored_plan is not None:
        if stored_plan_issues:
            payload = {
                "error": "PRECOMMIT CONTRACT DRIFT: restart the generation from a fresh repository baseline.",
                "version": v,
                "source_v": source_v,
                "passed": False,
                "blockers": [{
                    "reason": "precommit_contract_drift",
                    "details": "; ".join(stored_plan_issues[:12]),
                }],
                "precommit_eval_plan": stored_plan,
                "failure_class": "infrastructure",
                "intent": make_intent(
                    "pause",
                    failure_class="infrastructure",
                    authority="tool:precommit_eval",
                    safe_to_auto_execute=False,
                    reason="precommit_contract_drift",
                ),
            }
            if declared_first_strict:
                return await _abandon_first_strict_generation(
                    payload,
                    reason="first_strict_control_plan_drift",
                )
            return _json_tool_result(payload)
        opponents = opponents_from_plan(stored_plan)
        frozen_settings = stored_plan.get("settings") or {}
        n_games = int(frozen_settings.get("matches_per_opponent") or n_games)
    elif national_evaluation and declared_first_strict:
        try:
            from first_strict_control import opponent_from_receipt

            opponents = [opponent_from_receipt(first_strict_control_receipt)]
        except Exception as exc:
            return await _abandon_first_strict_generation({
                "error": "FIRST_STRICT_CONTROL_OPPONENT_INVALID",
                "version": v,
                "source_v": source_v,
                "passed": False,
                "action": "abandon_generation",
                "failure_class": "control_plane",
                "message": f"{type(exc).__name__}: {str(exc)[:800]}",
                "intent": make_intent(
                    "pause",
                    failure_class="control_plane",
                    authority="tool:precommit_eval",
                    safe_to_auto_execute=False,
                    reason="first_strict_control_opponent_invalid",
                ),
            }, reason="first_strict_control_opponent_invalid")
    else:
        opponents = _select_precommit_opponents(v, source_v)
    # Add crossover parent_b if applicable
    if (
        stored_plan is None
        and not declared_first_strict
        and ckpt
        and ckpt.get("parent2_v")
    ):
        parent2_name = active_bot_name(ckpt["parent2_v"])
        parent2_path = get_bot_dir(parse_bot_version(parent2_name))
        if parent2_path.exists() and not any(o["name"] == parent2_name for o in opponents):
            opponents.append({"name": parent2_name, "reason": "crossover_parent_b"})

    if not opponents:
        blockers.append({"reason": "no_opponents", "details": "No eligible current-epoch native TCP opponents found."})
    all_opponents = list(opponents)  # preserve full list for result reporting

    precommit_plan = stored_plan
    evaluation_contract = current_evaluation_contract
    if national_evaluation and precommit_plan is None and opponents:
        national_hands, national_matches = _national_precommit_shape(workflow_profile, n_games)
        try:
            precommit_plan = create_precommit_plan(
                candidate_version=v,
                source_version=source_v,
                profile_id=workflow_profile.profile_id,
                execution_mode=expected_execution_mode,
                evaluation_protocol=evaluation_protocol,
                opponents=opponents,
                hands_per_match=national_hands,
                matches_per_opponent=national_matches,
                path_resolver=lambda item: (
                    item.get("path")
                    or get_bot_dir(parse_bot_version(item["name"]))
                ),
                require_published_opponents=True,
            )
        except PrecommitEvalContractError as exc:
            payload = {
                "error": f"PRECOMMIT PLAN CREATION FAILED: {exc}",
                "version": v,
                "source_v": source_v,
                "passed": False,
                "blockers": [{
                    "reason": "precommit_plan_creation_failed",
                    "details": str(exc)[:800],
                }],
                "failure_class": "infrastructure",
                "intent": make_intent(
                    "pause",
                    failure_class="infrastructure",
                    authority="tool:precommit_eval",
                    safe_to_auto_execute=False,
                    reason="precommit_plan_creation_failed",
                ),
            }
            if declared_first_strict:
                return await _abandon_first_strict_generation(
                    payload,
                    reason="first_strict_control_plan_creation_failed",
                )
            return _json_tool_result(payload)
        current_stage = ckpt.get("stage", "critic_checked") if ckpt else "critic_checked"
        if not write_pipeline_checkpoint(
            v,
            source_v,
            current_stage,
            audit_context={"precommit_eval_plan": precommit_plan},
        ):
            if declared_first_strict:
                return await _abandon_first_strict_generation({
                    "error": "Failed to persist immutable precommit evaluation plan.",
                    "version": v,
                    "source_v": source_v,
                    "passed": False,
                    "failure_class": "control_plane",
                }, reason="first_strict_control_plan_persist_failed")
            return _state_blocked(
                "Failed to persist immutable precommit evaluation plan.",
                v,
                source_v,
                ckpt,
            )
        evaluation_contract = build_evaluation_contract(
            precommit_plan,
            candidate_code_fingerprint=code_fingerprint,
        )
        opponents = opponents_from_plan(precommit_plan)
        all_opponents = list(opponents)
        n_games = int((precommit_plan.get("settings") or {}).get("matches_per_opponent") or n_games)

    if national_evaluation and precommit_plan is None:
        payload = {
            "error": "PRECOMMIT PLAN UNAVAILABLE: no immutable opponent set could be created.",
            "version": v,
            "source_v": source_v,
            "passed": False,
            "blockers": blockers or [{
                "reason": "precommit_plan_unavailable",
                "details": "No eligible national opponent was available.",
            }],
            "failure_class": "infrastructure",
            "intent": make_intent(
                "pause",
                failure_class="infrastructure",
                authority="tool:precommit_eval",
                safe_to_auto_execute=False,
                reason="precommit_plan_unavailable",
            ),
        }
        if declared_first_strict:
            return await _abandon_first_strict_generation(
                payload,
                reason="first_strict_control_plan_unavailable",
            )
        return _json_tool_result(payload)

    # Increment precommit_attempt only when a real precommit battle round is
    # about to start. Idempotent already-verified calls, missing prerequisite
    # gates, missing candidates, and no-opponent preflight exits must not spend
    # an attempt because they do not evaluate the current bot code.
    precommit_attempt = int(ckpt.get("precommit_attempt", 0) or 0) if ckpt else 0
    if opponents:
        current_stage = ckpt.get("stage", "critic_checked") if ckpt else "critic_checked"
        precommit_attempt += 1
        write_pipeline_checkpoint(
            v,
            source_v,
            current_stage,
            precommit_attempt=precommit_attempt,
        )

    execution_ckpt = _matching_checkpoint(v, source_v) if opponents else ckpt

    if national_evaluation:
        return await _run_national_precommit_backend(
            v=v,
            source_v=source_v,
            requested_n_games=requested,
            effective_n_games=n_games,
            candidate_name=candidate_name,
            parent_name=parent_name,
            candidate_entry=candidate_entry,
            code_fingerprint=code_fingerprint,
            workflow_profile=workflow_profile,
            candidate_id=candidate_id,
            opponents=opponents,
            all_opponents=all_opponents,
            precommit_attempt=precommit_attempt,
            initial_blockers=blockers,
            started_at=_t0,
            precommit_plan=precommit_plan,
            evaluation_contract=evaluation_contract,
            workflow_run_id=str(
                (execution_ckpt or {}).get("workflow_run_id") or ""
            ),
            checkpoint_revision=int(
                (execution_ckpt or {}).get("checkpoint_revision") or 0
            ),
        )

# ──────────────────────────────────────────────
# Inline Eval
# ──────────────────────────────────────────────

@tool("run_inline_eval", "Run a non-authoritative diagnostic evaluation without modifying Glicko/H2H. The rating daemon is the only authoritative rating writer.", {"version": int, "n_games": int})
async def run_inline_eval(args):
    _inline_eval_start = time.time()
    v, _source_v = _resolve_version_args(args)
    if v is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Missing version and no active pipeline checkpoint"})}]}
    v = int(v)
    n_games = args.get("n_games", 5)
    bot_name = active_bot_name(v)

    _set_pipeline_status(f"Running inline eval for v{v}")

    bot_dir = get_bot_dir(v)

    from workflow_profiles import get_workflow_profile

    profile = get_workflow_profile()
    if getattr(profile, "national_execution_mode", None) != "native_tcp":
        return _json_tool_result({"error": "only native_tcp inline evaluation is supported"})
    expected_entry = bot_dir / "national_bot.py"
    if not expected_entry.exists():
        return {"content": [{"type": "text", "text": json.dumps({
            "error": f"Bot v{v} entry not found: {expected_entry.name}"
        })}]}

    # Guard: refuse to run while daemon is active (read-modify-write race on ratings)
    from daemon_management import daemon_proc, _daemon_lock
    with _daemon_lock:
        _dp = daemon_proc
    if _dp is not None and _dp.poll() is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Daemon is running. Stop it first with stop_daemon to avoid ratings race condition."})}]}

    active_bots = get_active_bots()
    opponents = [b for b in active_bots if b != bot_name]

    if getattr(profile, "national_execution_mode", None) == "native_tcp":
        from national_native import run_native_acceptance_for_candidate
        from evolution_infra import RESULTS_DIR
        from evaluation_data_identity import current_evaluation_digest
        from datetime import datetime as _dt

        acceptance = await run_native_acceptance_for_candidate(
            bot_dir,
            opponent_tokens=[get_bot_dir(int(name.removeprefix("national_v"))) for name in opponents],
            hands=70,
            max_opponents=max(1, len(opponents)),
        )
        payload = acceptance.model_dump()
        payload.update({
            "authoritative": False,
            "ratings_updated": False,
            "h2h_updated": False,
            "evaluation_identity_digest": current_evaluation_digest(RESULTS_DIR),
            "source": "inline_native_diagnostic",
        })
        diagnostic_dir = RESULTS_DIR / "inline_eval_diagnostics"
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        diagnostic_path = diagnostic_dir / f"v{v}-{_dt.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
        diagnostic_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["diagnostic_path"] = str(diagnostic_path)
        return {"content": [{"type": "text", "text": json.dumps(payload, indent=2, ensure_ascii=False)}]}
