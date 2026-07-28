"""Precommit failure attribution and infra-timeout retry validation.

Extracted from ``tool_eval``.  Holds the pure helper cluster that attributes
a national precommit failure to its worst opponent (used by the failure
directive/intent projection) and the validator that decides whether an
``infra_timed_out`` candidate may reuse its already-passed quality/review/critic
evidence for an exact precommit retry.

The parent module (``tool_eval``) keeps thin delegate shells so any future
monkeypatching of ``tool_eval.<name>`` and internal module-global call sites
continue to resolve against the parent namespace.  Companions reach the parent's
already-bound imports (``_quality_gate_ok``, ``opponents_from_plan``,
``_validate_first_strict_control_execution_scope``, etc.) via ``_te.``, matching
the established ``tool_eval_first_strict_scope`` companion pattern.
"""

from __future__ import annotations

from pathlib import Path

import tool_eval as _te


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


def _infra_timeout_retry_authority_error(
    checkpoint,
    *,
    candidate_dir,
    code_fingerprint,
    version,
    source_v,
):
    """Return why an infra-timeout candidate cannot reuse passed gate evidence.

    ``infra_timed_out`` is only a transport/evaluation overlay.  Removing it is
    safe only while the complete candidate artifact is still the byte identity
    that passed the active quality -> review -> critic chain.  The quality gate
    owns the complete-artifact fingerprint and ``repair_baseline_artifact_hash``
    carries that same frozen identity through the later non-mutating gates.
    """

    candidate_dir = Path(candidate_dir)
    candidate_entry = candidate_dir / "national_bot.py"
    if (
        not candidate_dir.is_dir()
        or not candidate_entry.is_file()
        or not isinstance(code_fingerprint, str)
        or len(code_fingerprint) != 64
        or any(char not in "0123456789abcdef" for char in code_fingerprint)
    ):
        return "Infra-timeout retry candidate artifact is missing or unreadable."

    if not (
        _te._quality_gate_ok(checkpoint)
        and _te._review_gate_ok(checkpoint)
        and _te._critic_gate_ok(checkpoint)
    ):
        return (
            "Infra-timeout retry quality/review/critic gate chain is incomplete "
            "or invalid."
        )

    precommit_attempt = checkpoint.get("precommit_attempt")
    if type(precommit_attempt) is not int or precommit_attempt < 1:
        return (
            "Infra-timeout retry checkpoint is missing its frozen logical "
            "precommit attempt identity."
        )

    gates = checkpoint.get("gate_results") or {}
    for gate_name in ("quality", "review", "critic"):
        gate = gates.get(gate_name) or {}
        if (
            gate.get("version") != int(version)
            or gate.get("source_v") != int(source_v)
        ):
            return (
                "Infra-timeout retry quality/review/critic gate identity does "
                "not match the active generation."
            )

    quality_fingerprint = str(
        ((gates.get("quality") or {}).get("code_fingerprint")) or ""
    )
    frozen_fingerprint = str(
        checkpoint.get("repair_baseline_artifact_hash") or ""
    )
    for fingerprint in (quality_fingerprint, frozen_fingerprint):
        if (
            len(fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in fingerprint)
        ):
            return (
                "Infra-timeout retry checkpoint is missing its frozen candidate "
                "artifact identity."
            )
    if quality_fingerprint != frozen_fingerprint:
        return (
            "Infra-timeout retry quality gate and checkpoint artifact bindings "
            "disagree."
        )
    if code_fingerprint != quality_fingerprint:
        return (
            "Infra-timeout retry candidate artifact drifted from the passed "
            "quality/review/critic evidence."
        )

    audit_context = checkpoint.get("audit_context") or {}
    stored_plan = audit_context.get("precommit_eval_plan")
    plan_opponents = (
        stored_plan.get("opponents")
        if isinstance(stored_plan, dict)
        else []
    )
    system_control_plan = any(
        isinstance(item, dict)
        and str(item.get("authority") or "")
        == "system_first_strict_control"
        for item in (plan_opponents or [])
    )
    if system_control_plan:
        try:
            frozen_opponents = _te.opponents_from_plan(stored_plan)
            frozen_contract = _te.build_evaluation_contract(
                stored_plan,
                candidate_code_fingerprint=code_fingerprint,
            )
        except Exception as exc:
            return (
                "Infra-timeout retry first-strict plan identity is invalid: "
                f"{type(exc).__name__}: {str(exc)[:240]}"
            )
        _scope, scope_error = _te._validate_first_strict_control_execution_scope(
            audit_context.get(
                _te._FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY
            ),
            v=int(version),
            candidate_name=_te.active_bot_name(int(version)),
            code_fingerprint=code_fingerprint,
            opponents=frozen_opponents,
            precommit_plan=stored_plan,
            evaluation_contract=frozen_contract,
            workflow_run_id=str(checkpoint.get("workflow_run_id") or ""),
            precommit_attempt=int(precommit_attempt),
        )
        if scope_error:
            return "Infra-timeout retry cannot re-prove its journal: " + scope_error
    return None
