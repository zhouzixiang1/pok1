"""Native TCP smoke + national acceptance + official smoke subsystem.

Extracted from tool_gates.py: the raw-TCP smoke gate, native national
acceptance evidence normalization/binding, official certification smoke
status requests, and the decision-test backend selector. These are the
execution-evidence helpers invoked by run_quality_gates; the monolith stays
in the parent and calls these via its delegate shells.

All public symbols are re-exported by tool_gates.py for backward compatibility."""

from __future__ import annotations

import tool_gates as _tg  # parent; respects test monkeypatches


def _run_workflow_decision_tests(
    bot_dir: Path,
    *,
    native_tcp_mode: bool,
    extra_scenarios=None,
) -> tuple[dict, dict]:
    """Select exactly one protocol-matched decision backend.

    Fixtures use only the raw national TCP boundary and system reducer.
    """

    if not native_tcp_mode:
        raise RuntimeError("only native_tcp decision fixtures are supported")
    from national_decision_tester import run_national_decision_tests

    detail = run_national_decision_tests(bot_dir)
    return detail, {
        "assertion_backed_count": int(detail.get("total", 0) or 0),
        "coverage_only_count": int(detail.get("coverage_only_count", 0) or 0),
        "external_scenario_sidecars_loaded": bool(
            detail.get("external_scenario_sidecars_loaded", False)
        ),
    }


def _national_acceptance_not_run(reason: str) -> tuple[bool, list[str], dict]:
    """Return a truthful fail-closed projection for an unexecuted hard gate."""

    normalized = str(reason or "national_acceptance_not_run")
    return False, [normalized], {
        "executed": False,
        "skipped": True,
        "passed": False,
        "conclusive": False,
        "outcome": "not_run",
        "reason": normalized,
        "issues": [normalized],
    }


def _national_acceptance_executed(
    report: dict,
    *,
    expected_hands: int | None = None,
    expected_timing_plan=None,
) -> tuple[bool, list[str], dict]:
    """Normalize native acceptance evidence without upgrading infra to pass."""

    payload = dict(report or {})
    outcome = str(payload.get("outcome") or "")
    issues = [str(item) for item in (payload.get("issues") or [])]
    coverage_ok = True
    timing_ok = True
    timing_plan_digests: list[str] = []
    timeout_phases: list[str] = []
    terminal_aborts: list[dict] = []
    observed_hands: list[int] = []
    if expected_hands is not None:
        expected_hands = int(expected_hands)
        if payload.get("acceptance_kind") == "first_strict_self_play_compliance":
            result = payload.get("result") or {}
            observed_hands = [int(result.get("hands_played") or 0)]
            coverage_ok = (
                int(payload.get("hands") or 0) == expected_hands
                and observed_hands == [expected_hands]
            )
        else:
            nested_report = payload.get("report") or {}
            results = nested_report.get("results") or []
            observed_hands = [
                int(item.get("hands_played") or 0)
                for item in results
                if isinstance(item, dict)
            ]
            coverage_ok = bool(
                int(payload.get("hands_per_pair") or 0) == expected_hands
                and payload.get("opponents")
                and observed_hands
                and all(item == expected_hands for item in observed_hands)
            )
        if not coverage_ok:
            issues.append(
                "national_acceptance_incomplete_hand_coverage:"
                f"expected={expected_hands}:observed={observed_hands}"
            )
    if expected_timing_plan is not None:
        try:
            from national_native import validate_native_match_timing_evidence

            if payload.get("acceptance_kind") == "first_strict_self_play_compliance":
                timing_results = [payload.get("result") or {}]
            else:
                timing_results = list(
                    ((payload.get("report") or {}).get("results") or [])
                )
            if not timing_results:
                timing_ok = False
                issues.append("national_acceptance_timing_evidence_missing")
            for index, result in enumerate(timing_results, start=1):
                if isinstance(result, dict):
                    digest = result.get("native_match_timing_plan_digest")
                    if isinstance(digest, str):
                        timing_plan_digests.append(digest)
                    phase = result.get("native_match_timeout_phase")
                    if phase is not None:
                        timeout_phases.append(str(phase))
                    abort = result.get("native_terminal_abort")
                    if isinstance(abort, dict):
                        terminal_aborts.append(dict(abort))
                timing_issues = validate_native_match_timing_evidence(
                    result,
                    timing_plan=expected_timing_plan,
                )
                if timing_issues:
                    timing_ok = False
                    issues.extend(
                        f"national_acceptance_timing_{index}:{item}"
                        for item in timing_issues
                    )
        except Exception as exc:
            timing_ok = False
            issues.append(
                "national_acceptance_timing_validation_error:"
                f"{type(exc).__name__}"
            )
    reported_passed = payload.get("passed") is True
    report_consistent = bool(
        outcome == "passed" and reported_passed and not issues
        or outcome == "candidate_failure" and not reported_passed and issues
        or outcome == "infrastructure_failure" and not reported_passed
    )
    if not report_consistent:
        issues.append(
            "national_acceptance_report_inconsistent:"
            f"outcome={outcome or 'missing'}:"
            f"reported_passed={reported_passed}:issues={len(issues)}"
        )
    passed = bool(
        reported_passed
        and outcome == "passed"
        and coverage_ok
        and timing_ok
        and report_consistent
    )
    payload.update({
        "executed": True,
        "skipped": False,
        "passed": passed,
        "conclusive": (
            coverage_ok
            and timing_ok
            and report_consistent
            and outcome in {"passed", "candidate_failure"}
        ),
        "expected_hands": expected_hands,
        "observed_hands": observed_hands,
        "coverage_ok": coverage_ok,
        "timing_ok": timing_ok,
        "native_match_timing_plan_digest": (
            timing_plan_digests[0]
            if len(set(timing_plan_digests)) == 1
            else None
        ),
        "native_match_timeout_phase": (
            timeout_phases[0] if len(set(timeout_phases)) == 1 else None
        ),
        "native_terminal_abort": (
            terminal_aborts[0] if len(terminal_aborts) == 1 else None
        ),
        "report_consistent": report_consistent,
        "issues": issues,
    })
    return passed, issues, payload


def _bind_quality_native_timing_plan(
    checkpoint: dict | None,
    timing_plan,
) -> dict | None:
    """Persist the quality match plan before it can emit a liveness sidecar.

    A runtime heartbeat may only prolong the provider cycle when its digest is
    already part of the active checkpoint.  This intentionally does not
    rewrite a prior binding: a config/code drift becomes a hard quality error
    instead of quietly changing an in-flight 70-hand evaluation.
    """

    if not isinstance(checkpoint, dict):
        return None
    existing = (checkpoint.get("audit_context") or {})
    snapshot = timing_plan.snapshot()
    digest = timing_plan.digest()
    recorded = existing.get("quality_native_match_timing_plan")
    recorded_digest = existing.get("quality_native_match_timing_plan_digest")
    if recorded is not None or recorded_digest is not None:
        if recorded != snapshot or recorded_digest != digest:
            raise RuntimeError("quality_native_match_timing_plan_drift")
        return checkpoint
    if str(checkpoint.get("stage") or "") != "workers_done":
        # No active gate-stage checkpoint means the runner may still execute
        # its own bounded match, but it receives no orchestrator extension.
        return checkpoint
    from evolution_infra import write_pipeline_checkpoint

    if not write_pipeline_checkpoint(
        int(checkpoint["next_v"]),
        int(checkpoint["source_v"]),
        "workers_done",
        audit_context={
            "quality_native_match_timing_plan": snapshot,
            "quality_native_match_timing_plan_digest": digest,
        },
        expected_checkpoint_revision=int(checkpoint.get("checkpoint_revision") or 0),
        expected_checkpoint_stage="workers_done",
        expected_workflow_run_id=str(checkpoint.get("workflow_run_id") or ""),
    ):
        raise RuntimeError("quality_native_match_timing_plan_bind_failed")
    refreshed = _tg._matching_checkpoint(
        int(checkpoint["next_v"]),
        int(checkpoint["source_v"]),
    )
    if not isinstance(refreshed, dict):
        raise RuntimeError("quality_native_match_timing_plan_checkpoint_missing")
    refreshed_context = refreshed.get("audit_context") or {}
    if (
        refreshed_context.get("quality_native_match_timing_plan") != snapshot
        or refreshed_context.get("quality_native_match_timing_plan_digest")
        != digest
    ):
        raise RuntimeError("quality_native_match_timing_plan_checkpoint_drift")
    return refreshed


def _official_gate_enabled(name: str, *, include_required: bool = True) -> bool:
    return (include_required and _tg._env_enabled("POK_OFFICIAL_REQUIRED")) or _tg._env_enabled(name)


async def _request_official_smoke_status(bot_dir: Path) -> dict:
    """Request smoke evidence using only a policy-eligible official opponent."""
    from official_certification import (
        STATUS_FAILED,
        STATUS_INCONCLUSIVE,
        STATUS_PENDING,
        build_spec,
        official_compliance_verdict,
        select_official_opponent,
    )
    from official_certification_job import start_or_poll_job

    preferred = _tg.os.environ.get("POK_OFFICIAL_OPPONENT", "").strip() or None
    selection = select_official_opponent(
        bot_dir,
        preferred=preferred,
        allow_bootstrap_grandfather=False,
    )
    if not selection.get("selected"):
        return {
            "status": STATUS_INCONCLUSIVE,
            "mode": "smoke",
            "issues": ["official_smoke_no_eligible_opponent"],
            "blocking": False,
            "inconclusive": True,
            "classification": "inconclusive",
            "opponent_selection": selection,
        }

    opponent = selection["opponent"]["path"]
    spec = build_spec("smoke", bot_dir, opponent=opponent)
    job = await _tg.run_blocking_isolated(
        start_or_poll_job,
        spec,
        thread_name_prefix="official-smoke",
        opponent_selection=selection,
    )
    status = (
        job.get("status")
        if job.get("state") == "completed" and isinstance(job.get("status"), dict)
        else {
            "status": STATUS_PENDING,
            "mode": "smoke",
            "queued": job.get("state") == "queued",
            "pending": bool(job.get("pending")),
            "issues": list(job.get("issues") or []),
            "official_job": job,
            "summary": {
                "self_play_rounds": (
                    spec.get("self_play_rounds") if isinstance(spec, dict) else spec.self_play_rounds
                ),
                "opponent_rounds": (
                    spec.get("opponent_rounds") if isinstance(spec, dict) else spec.opponent_rounds
                ),
                "target_hands": (
                    spec.get("target_hands") if isinstance(spec, dict) else spec.target_hands
                ),
            },
        }
    )

    verdict = official_compliance_verdict(status)
    return {
        **status,
        "blocking": bool(verdict.get("blocking")),
        "inconclusive": bool(verdict.get("inconclusive")),
        "classification": str(verdict.get("classification") or "passed_or_pending"),
        "opponent_selection": status.get("opponent_selection") or selection,
        "request_opponent_selection": selection,
        "official_job": job,
    }


async def _run_workflow_smoke_gate(
    *,
    bot_dir: Path,
    source_v: int | None,
    native_tcp_mode: bool,
    compile_errors: list,
    import_errors: list,
    protected_contract_errors: list,
    native_contract_errors: list,
    embedded_selftest_errors: list,
    opponent_token=None,
    self_play: bool = False,
) -> tuple[list[str], dict]:
    """Run the sole active raw-TCP smoke gate."""
    if not native_tcp_mode:
        raise RuntimeError("only native_tcp smoke is supported")

    blocking_prereqs = (
        list(compile_errors or [])
        + list(import_errors or [])
        + list(protected_contract_errors or [])
        + list(native_contract_errors or [])
        + list(embedded_selftest_errors or [])
    )
    if blocking_prereqs:
        return [], {
            "execution_mode": "native_tcp",
            "skipped": True,
            "reason": "prerequisite_gate_failed",
        }

    hands = int(_tg.os.environ.get("POK_NATIVE_SMOKE_HANDS", "1"))
    timeout_sec = float(_tg.os.environ.get("POK_NATIVE_SMOKE_TIMEOUT_SEC", "90"))
    # Bounded retry for transient infrastructure flakiness.  A single shaky
    # native TCP run (startup-watchdog kill, transport stall, launch-latency
    # spike) must not permanently abandon a generation that already spent its
    # full Master+Worker LLM budget.  Only retries infrastructure-class
    # failures; a genuine candidate defect is reported immediately.
    max_attempts = max(1, int(_tg.os.environ.get("POK_NATIVE_SMOKE_MAX_ATTEMPTS", "3")))
    report: dict = {}
    for attempt in range(1, max_attempts + 1):
        try:
            from national_native import run_native_tcp_smoke

            # An unpublished candidate (no completion tag / certificate yet)
            # sits under ``bots/`` (materialized by the Worker phase) but is
            # NOT a valid published strict artifact — resolve_bot rejects it
            # with ``invalid_national_bot_label``.  The smoke must run against
            # the in-flight workspace via ``in_flight_candidate_dir`` so the
            # namespace bypass path applies.  Detect this by checking whether
            # resolve_bot accepts the bot_dir; if not, pass it as in_flight.
            _smoke_kwargs = dict(
                source_v=source_v,
                opponent_token=opponent_token,
                self_play=self_play,
                hands=hands,
                timeout_sec=timeout_sec,
            )
            try:
                from national_native import resolve_bot as _resolve_bot_check

                _resolve_bot_check(str(bot_dir))
            except Exception:
                # Unpublished candidate: use the in_flight bypass path.
                _smoke_kwargs["in_flight_candidate_dir"] = str(bot_dir)

            attempt_report = await run_native_tcp_smoke(str(bot_dir), **_smoke_kwargs)
        except Exception as exc:
            attempt_report = {
                "passed": False,
                "execution_mode": "native_tcp",
                "failure_class": "infrastructure",
                "outcome": "infrastructure_failure",
                "failure_side": "harness",
                "issues": [
                    f"native_smoke_exception={type(exc).__name__}: {str(exc)[:500]}"
                ],
            }
        report = attempt_report
        if report.get("passed"):
            break
        # Retry only on transient infrastructure-class failures.  A real
        # candidate defect (illegal_actions, artifact_changed_during_execution,
        # handshake_malformed, ...) is deterministic and must not burn retries.
        if attempt < max_attempts and _smoke_outcome_is_retryable(report):
            import asyncio
            import random

            backoff = float(attempt) + random.uniform(0.0, 1.0)
            await asyncio.sleep(backoff)
            continue
        break
    errors = list(report.get("issues") or []) if not report.get("passed") else []
    return errors, report


def _smoke_outcome_is_retryable(report: dict) -> bool:
    """A smoke failure is retryable iff it is infrastructure-class.

    The native smoke may fail transiently from launch-latency spikes
    (startup-watchdog kill, no process output), transport stalls
    (finalizing-cleanup timeout), or harness exceptions — all of which are
    independent of the candidate's policy bytes (the system-owned bot runtime
    is byte-identical across every candidate and baseline opponent).  A genuine
    candidate defect (``candidate_failure``) is deterministic and must not be
    retried.
    """
    if not isinstance(report, dict):
        return False
    outcome = str(report.get("outcome") or "")
    if outcome in ("infrastructure_failure", ""):
        # An explicit infrastructure outcome is always retryable.  An empty
        # outcome (e.g. a watchdog kill reported only via timeout_phase) is
        # treated as infrastructure unless candidate-side issues prove
        # otherwise.
        return True
    if outcome != "candidate_failure":
        return True
    # Even a candidate_failure is retryable when the underlying run never
    # produced a hand: that means a process was killed during startup before
    # any policy decision was made, which cannot be a policy defect.  The
    # startup_watchdog / finalizing_cleanup phases are likewise harness-side.
    timeout_phase = str(report.get("native_match_timeout_phase") or "")
    if timeout_phase in ("startup_watchdog", "finalizing_cleanup"):
        return True
    try:
        hands_played = int(report.get("hands_played") or 0)
    except (TypeError, ValueError):
        hands_played = 0
    if hands_played <= 0:
        return True
    # Fall back to the embedded per-run result if present (run_native_tcp_smoke
    # nests the raw pair result under "result").  Only treat an ABSENT raw
    # result as non-retryable here: a genuine candidate_failure with
    # hands_played>=1 and no startup-phase signal has already returned False
    # above; this branch only adds evidence when a raw result exists.
    raw = report.get("result")
    if isinstance(raw, dict):
        raw_phase = str(raw.get("native_match_timeout_phase") or "")
        if raw_phase in ("startup_watchdog", "finalizing_cleanup"):
            return True
        try:
            raw_hands = int(raw.get("hands_played") or 0)
        except (TypeError, ValueError):
            raw_hands = 0
        if raw_hands <= 0:
            return True
    return False
