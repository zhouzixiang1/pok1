"""Crossover infrastructure helpers, extracted from tool_commit.

Holds the neutral crossover infrastructure-failure recorder used by
``run_crossover``.  The parent module (``tool_commit``) keeps a thin delegate
shell so test-suite monkeypatching of
``tool_commit._record_crossover_infrastructure`` and direct calls such as
``tool_commit._record_crossover_infrastructure(...)`` continue to resolve
against the parent module namespace.

Implemented as a delegate back into the parent module's already-imported
helpers (``get_bot_dir``, ``_record_infrastructure_failure``,
``_json_tool_result``) via ``_tc.`` so the companion never re-imports what the
parent already bound, matching the established ``tool_commit_publication``
companion pattern.
"""

from __future__ import annotations

import tool_commit as _tc


async def _record_crossover_infrastructure(
    target_v,
    parent_a,
    parent_b,
    *,
    component,
    code,
    issues,
    architecture_policy=None,
    metadata=None,
):
    """Persist a neutral crossover retry without consuming an LLM retry."""
    from bot_artifact import hash_path
    from national_runtime_probe import RUNTIME_PROBE_IDENTITY_DIGEST
    from pipeline_infrastructure import infrastructure_attempt_key

    target_dir = _tc.get_bot_dir(target_v)
    parent_dir = _tc.get_bot_dir(parent_a)
    parent2_dir = _tc.get_bot_dir(parent_b)
    candidate_fingerprint = (
        hash_path(target_dir) if target_dir.is_dir() else "missing"
    )
    source_fingerprint = hash_path(parent_dir)
    parent2_fingerprint = hash_path(parent2_dir)
    attempt_key = infrastructure_attempt_key(
        component=str(component or "crossover_preplan"),
        candidate_fingerprint=candidate_fingerprint,
        source_fingerprint=source_fingerprint,
        harness_identity=RUNTIME_PROBE_IDENTITY_DIGEST,
        contract_identity=str(
            (architecture_policy or {}).get("policy_digest") or ""
        ),
        extra={
            "target_v": int(target_v),
            "parent_a": int(parent_a),
            "parent_b": int(parent_b),
            "parent2_fingerprint": parent2_fingerprint,
            "phase": "crossover_preplan",
        },
    )
    result = await _tc._record_infrastructure_failure(
        target_v,
        parent_a,
        owner_tool="run_crossover",
        resume_stage="crossover_running",
        component=str(component or "crossover_preplan"),
        code=str(code),
        attempt_key=attempt_key,
        issues=[str(item)[:500] for item in issues or []],
        max_attempts=3,
        cumulative_attempt_field="crossover_generation_attempt",
        metadata={
            "parent2_v": int(parent_b),
            "candidate_fingerprint": candidate_fingerprint,
            "source_fingerprint": source_fingerprint,
            "parent2_fingerprint": parent2_fingerprint,
            "architecture_policy_digest": str(
                (architecture_policy or {}).get("policy_digest") or ""
            ),
            **(metadata or {}),
        },
    )
    return _tc._json_tool_result({
        **result,
        "error": "CROSSOVER_INFRASTRUCTURE_INCONCLUSIVE",
        "success": False,
        "target_v": target_v,
        "parent_a": parent_a,
        "parent_b": parent_b,
        "directive": (
            "Crossover infrastructure exhausted and the generation was abandoned."
            if result.get("abandoned")
            else "Retry run_crossover for the same checkpoint. The prepared child is "
                 "preserved and only the inconclusive deterministic probe will rerun."
        ),
    })


async def _abandon_crossover_effect_conflict(
    parent_a,
    parent_b,
    target_v,
    success,
    ui,
):
    """Abandon a crossover whose synthesis effect-id namespace is unrecoverable.

    Bug B (v160): the effect-id namespace is poisoned (bound to a different
    input_digest in a prior re-entry). Synthesis can never succeed
    idempotently, so a "preserved candidate" retry overlay would loop forever:
    the overlay claims "preserved" but no candidate exists. Abandon directly;
    the next preparation rebinds a clean effect namespace.
    (crossover_effect_prepare_conflict: matches the crossover_ forced-rule
    prefix, disposable at selected/crossover_running.)
    """
    issue = success.get("issue")
    try:
        _tc.log_system_event(
            "pipeline.crossover_effect_prepare_conflict_unrecoverable",
            "error",
            f"Crossover v{parent_a}xv{parent_b} -> v{target_v} synthesis effect "
            f"namespace is unrecoverable: {issue}",
            {
                "target_v": target_v,
                "parent_a": parent_a,
                "parent_b": parent_b,
                "component": success.get("component"),
                "issue": str(issue)[:500],
            },
        )
    except Exception:
        pass
    from tool_bot_management import (
        _do_abandon_generation,
        expected_abandon_identity,
    )
    try:
        abandon_result = await _do_abandon_generation(
            reason=f"crossover_effect_prepare_conflict:v{parent_a}xv{parent_b}",
            **expected_abandon_identity(_tc.read_pipeline_checkpoint()),
        )
    except Exception as abandon_exc:
        abandon_result = {
            "abandoned": False,
            "reason": f"{type(abandon_exc).__name__}: {abandon_exc}",
        }
    try:
        _tc.log_system_event(
            "pipeline.crossover_effect_prepare_conflict_abandoned",
            "warn" if abandon_result.get("abandoned") else "error",
            f"Crossover v{parent_a}xv{parent_b} effect-id conflict; "
            f"{'abandoned generation' if abandon_result.get('abandoned') else 'abandon did not complete'}.",
            {
                "target_v": target_v,
                "parent_a": parent_a,
                "parent_b": parent_b,
                "issue": str(issue)[:500],
                "abandon_result": abandon_result,
            },
        )
    except Exception:
        pass
    return _tc._json_tool_result({
        "error": "CROSSOVER_LLM_EXHAUSTED",
        "success": False,
        "abandoned": bool(abandon_result.get("abandoned")),
        "failure_class": "infrastructure",
        "abandon_reason": "crossover_effect_prepare_conflict",
        "target_v": target_v,
        "parent_a": parent_a,
        "parent_b": parent_b,
        "directive": (
            f"Crossover v{parent_a}xv{parent_b} -> v{target_v} has an unrecoverable "
            "synthesis-effect id conflict (the effect was previously bound to a different "
            "input). The generation was abandoned; let prepare_generation select a fresh generation."
        ),
        "message": f"Crossover v{parent_a}xv{parent_b} effect-id conflict abandoned.",
        "abandon_result": abandon_result,
        "logs": ui.get_output(),
    })


async def _finish_crossover_llm_exhausted(parent_a, parent_b, target_v, ui):
    """Abandon a generation whose crossover LLM retries were exhausted.

    B1 (2026-07-09): when the crossover LLM retries are exhausted (e.g.
    repeated idle timeouts / SDK stream stalls) WITHOUT a compatibility
    rejection, the checkpoint stays at "crossover_running". Previously
    run_crossover returned a bare {"success": False} with no "error", so the
    orchestrator deterministic router fell through to "route done, re-enter
    loop" and re-routed to run_crossover again — an infinite deadlock.
    Mirror the CROSSOVER_INCOMPATIBLE contract: abandon the generation and
    return a distinct CROSSOVER_LLM_EXHAUSTED token so the orchestrator
    recognizes the abandon instead of looping.
    """
    try:
        log_system_event = _tc.log_system_event
        log_system_event('pipeline.crossover_failed', 'error',
            f'Crossover v{parent_a}×v{parent_b} → v{target_v} failed',
            {'target_v': target_v, 'parent_a': parent_a, 'parent_b': parent_b})
    except Exception:
        pass
    try:
        from tool_bot_management import (
            _do_abandon_generation,
            expected_abandon_identity,
        )
        abandon_result = await _do_abandon_generation(
            reason=f"crossover_llm_exhausted:v{parent_a}xv{parent_b}",
            **expected_abandon_identity(_tc.read_pipeline_checkpoint()),
        )
    except Exception as abandon_exc:
        abandon_result = {
            "abandoned": False,
            "reason": f"{type(abandon_exc).__name__}: {abandon_exc}",
        }
        _tc._log.warning("Failed to abandon crossover-LLM-exhausted generation: %s", abandon_result)

    _tc.log_system_event(
        "pipeline.crossover_llm_exhausted_abandoned",
        "warn" if abandon_result.get("abandoned") else "error",
        f"Crossover v{parent_a}×v{parent_b} → v{target_v} exhausted all LLM retries; "
        f"{'generation abandoned' if abandon_result.get('abandoned') else 'abandon did not complete'}.",
        {
            "target_v": target_v,
            "parent_a": parent_a,
            "parent_b": parent_b,
            "abandon_result": abandon_result,
        },
    )
    return _tc._json_tool_result({
        "error": "CROSSOVER_LLM_EXHAUSTED",
        "success": False,
        "abandoned": bool(abandon_result.get("abandoned")),
        "directive": (
            f"Crossover v{parent_a}×v{parent_b} exhausted all LLM retries "
            f"(repeated timeout/SDK stream stall). The generation was abandoned; "
            "let prepare_generation select a fresh generation."
        ),
        "message": f"Crossover v{parent_a}×v{parent_b} failed after exhausting all LLM retries.",
        "abandon_result": abandon_result,
        "logs": ui.get_output(),
    })
