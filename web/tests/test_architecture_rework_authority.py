"""Regression guard for the v12/v13 death-loop root cause.

v12 and v13 both terminal-abandoned at ``quality_failed`` with reason
``worker_terminal_abandon_rework_task_authority_invalid`` because the
``_ARCHITECTURE_CHECK_FILES`` allowlist was missing two policy.py-repairable
check ids. The ``_architecture_contracts`` ``worker_repairable`` guard returned
``[]``, ``_quality_repair_contracts`` returned empty, and
``_synthesize_rework_tasks_from_checkpoint`` hit its ``elif reviewer_feedback:
return []`` dead-end -- producing ``system_repair_task_synthesis_empty`` and a
terminal abandon instead of entering a rework cycle.

These tests lock the fix: when ``national_capability_contract`` fails on
``typed_runtime_probe`` (policy baseline deadline miss) or
``precompute_runtime_influence`` (advisory regression whose evidence points at
policy.py), the contract synthesizer MUST return a non-empty, policy.py-scoped
repair task so the generation can rework instead of abandoning.
"""

from conftest import STRICT_TARGET_V

import tool_planning_quality_contracts as _qc
from tool_planning import _synthesize_rework_tasks_from_checkpoint
from tool_planning_quality_repair_targets import _architecture_contracts


def _transition(failing_ids):
    """Build a minimal failing ``national_architecture_transition`` payload.

    Mirrors the shape ``run_quality_gates`` publishes for v12/v13: a non-ok
    transition with ``candidate_capabilities.required_failures`` listing the
    failing check ids and ``checks_by_id`` carrying their evidence/guidance.
    """
    checks_by_id = {}
    required_failures = []
    for check_id in failing_ids:
        required_failures.append({"check_id": check_id})
        checks_by_id[check_id] = {
            "required": True,
            "skill_layer": "runtime_architecture",
            "guidance": "Keep policy.py on decision_context v1 and typed intents.",
            "evidence": {
                "summary": f"{check_id} failed under the deterministic typed probe",
                "locations": ["policy.py"],
            },
        }
    return {
        "ok": False,
        "runtime_probe_infra": False,
        "policy_identity_errors": [],
        "candidate_capabilities": {
            "checks_by_id": checks_by_id,
            "required_failures": required_failures,
        },
        "policy": {},
        "selected_focus": {"focus_id": "runtime_architecture"},
    }


def _quality(transition):
    return {"national_architecture_transition": transition}


def _checkpoint(failing_ids, *, reviewer_feedback=None):
    next_v = STRICT_TARGET_V + 1
    ckpt = {
        "stage": "quality_failed",
        "next_v": next_v,
        "source_v": STRICT_TARGET_V,
        "gate_results": {
            "quality": _quality(_transition(failing_ids)),
        },
    }
    if reviewer_feedback is not None:
        ckpt["reviewer_feedback"] = reviewer_feedback
    return ckpt


# --- Unit-level: the guard itself no longer short-circuits ---------------


def test_check_files_map_admits_typed_runtime_probe():
    """typed_runtime_probe MUST be in the map and map to policy.py only."""
    assert _qc._ARCHITECTURE_CHECK_FILES.get("typed_runtime_probe") == ["policy.py"]


def test_check_files_map_admits_precompute_runtime_influence():
    """precompute_runtime_influence MUST be in the map and map to policy.py."""
    assert _qc._ARCHITECTURE_CHECK_FILES.get("precompute_runtime_influence") == ["policy.py"]


def test_typed_runtime_probe_failure_yields_policy_contract():
    """The v12/v13 signature: typed_runtime_probe alone must synthesize a repair."""
    contracts = _architecture_contracts(
        _quality(_transition(["typed_runtime_probe"])),
        {"next_v": STRICT_TARGET_V + 1},
    )
    assert len(contracts) == 1, (
        "typed_runtime_probe failure must produce exactly one architecture "
        "repair contract; returning empty re-triggers the v12/v13 abandon"
    )
    contract = contracts[0]
    assert contract["file"] == "policy.py"
    assert contract["must_change_files"] == ["policy.py"]
    assert "typed_runtime_probe" in contract["required_checks"]
    # Write scope stays policy.py-only; the worker cannot touch system files.
    assert "national_bot.py" not in contract.get("files", [])
    assert "precompute.py" not in contract.get("files", [])


def test_precompute_runtime_influence_failure_yields_policy_contract():
    """The other v12/v13 signature: advisory precompute regression is repairable."""
    contracts = _architecture_contracts(
        _quality(_transition(["precompute_runtime_influence"])),
        {"next_v": STRICT_TARGET_V + 1},
    )
    assert len(contracts) == 1
    assert contracts[0]["file"] == "policy.py"
    assert "precompute_runtime_influence" in contracts[0]["required_checks"]


def test_combined_failure_yields_single_coherent_contract():
    """Both failing ids collapse into one coherent architecture repair task."""
    contracts = _architecture_contracts(
        _quality(_transition(["typed_runtime_probe", "precompute_runtime_influence"])),
        {"next_v": STRICT_TARGET_V + 1},
    )
    assert len(contracts) == 1, (
        "architecture repair is deliberately one coherent task, not split per check"
    )
    required = contracts[0]["required_checks"]
    assert "typed_runtime_probe" in required
    assert "precompute_runtime_influence" in required


# --- Integration-level: the synthesizer no longer dead-ends -------------


def test_quality_failed_with_reviewer_feedback_synthesizes_rework_task():
    """The exact v12/v13 dead-end: reviewer_feedback present + empty contracts.

    Before the fix, ``_synthesize_rework_tasks_from_checkpoint`` hit
    ``elif reviewer_feedback: return []`` and the orchestrator emitted
    ``system_repair_task_synthesis_empty`` -> terminal abandon. With the two
    check ids admitted, the architecture contract is non-empty and a real
    rework task is produced.
    """
    ckpt = _checkpoint(
        ["typed_runtime_probe", "precompute_runtime_influence"],
        reviewer_feedback=(
            "national_capability_contract failed: typed_runtime_probe "
            "(policy_baseline_deadline_missed on turn_delayed_probe_vs_opponent_pfr) "
            "and architecture_regression:precompute_runtime_influence."
        ),
    )
    tasks = _synthesize_rework_tasks_from_checkpoint(ckpt, ckpt["reviewer_feedback"])
    assert len(tasks) >= 1, (
        "with the fix applied, a typed_runtime_probe/precompute_runtime_influence "
        "quality failure MUST synthesize a rework task instead of returning [] "
        "(which would re-trigger worker_terminal_abandon_rework_task_authority_invalid)"
    )
    task = tasks[0]
    assert task["target_files"] == ["policy.py"]
    assert task["must_change_files"] == ["policy.py"]
    # The task carries a repair contract, not a bare feedback echo.
    assert task.get("repair_contract") is not None


# --- Defect-C hardening: capability-only fallback when transition is absent ---


def _capability(failing_ids):
    """Build a failing flat ``national_capability_contract`` (no transition)."""
    checks_by_id = {}
    required_failures = []
    for check_id in failing_ids:
        required_failures.append({"check_id": check_id})
        checks_by_id[check_id] = {
            "required": True,
            "skill_layer": "runtime_architecture",
            "guidance": "Satisfy this capability with code consumed by the decision path.",
            "evidence": {
                "summary": f"{check_id} failed under the deterministic typed probe",
                "locations": ["policy.py"],
            },
        }
    return {
        "ok": False,
        "checks_by_id": checks_by_id,
        "required_failures": required_failures,
        "policy": {},
    }


def test_capability_only_fallback_synthesizes_policy_contract_when_transition_absent():
    """Defect-C hardening: a flat capability failure with NO transition object
    must still synthesize a policy.py repair contract instead of dead-ending on
    ``system_repair_task_synthesis_empty``."""
    quality = {"national_capability_contract": _capability(["typed_runtime_probe"])}
    contracts = _architecture_contracts(quality, {"next_v": STRICT_TARGET_V + 1})
    assert len(contracts) == 1, (
        "a flat typed_runtime_probe policy.py failure must synthesize a repair "
        "contract even when national_architecture_transition is absent"
    )
    contract = contracts[0]
    assert contract["file"] == "policy.py"
    assert contract["must_change_files"] == ["policy.py"]
    assert "typed_runtime_probe" in contract["required_checks"]


def test_capability_only_fallback_synthesizes_policy_contract_when_transition_ok():
    """The fallback also fires when the transition is present but ok/skipped."""
    quality = {
        "national_capability_contract": _capability(["precompute_runtime_influence"]),
        "national_architecture_transition": {"ok": True, "skipped": True},
    }
    contracts = _architecture_contracts(quality, {"next_v": STRICT_TARGET_V + 1})
    assert len(contracts) == 1
    assert contracts[0]["file"] == "policy.py"
    assert "precompute_runtime_influence" in contracts[0]["required_checks"]


def test_capability_only_fallback_is_fail_closed_for_non_policy_py_failure():
    """A failing check that is NOT policy.py-repairable must still return [].

    Fail-closed: a check_id absent from _ARCHITECTURE_CHECK_FILES, or one whose
    flat-contract evidence.locations do not include policy.py, must NOT be
    repaired via the fallback -- it stays a terminal abandon (unchanged
    behavior for genuinely non-repairable failures)."""
    capability = {
        "ok": False,
        "checks_by_id": {
            "some_socket_reducer_failure": {
                "required": True,
                "skill_layer": "reducer",
                "guidance": "x",
                "evidence": {"summary": "failed", "locations": ["national_bot.py"]},
            },
        },
        "required_failures": [{"check_id": "some_socket_reducer_failure"}],
        "policy": {},
    }
    quality = {"national_capability_contract": capability}
    contracts = _architecture_contracts(quality, {"next_v": STRICT_TARGET_V + 1})
    assert contracts == [], (
        "a non-policy.py failure must NOT be repaired by the fallback; terminal "
        "abandon is the correct behavior for reducer/socket/tracker failures"
    )


def test_capability_only_fallback_requires_flat_evidence_corroboration():
    """Even a mapped check_id must corroborate policy.py via its own flat
    evidence.locations; a mapped check whose evidence points elsewhere is
    fail-closed (returns [])."""
    capability = {
        "ok": False,
        "checks_by_id": {
            # Mapped to policy.py in _ARCHITECTURE_CHECK_FILES, but its flat
            # evidence.locations do NOT include policy.py -> fail closed.
            "typed_runtime_probe": {
                "required": True,
                "skill_layer": "runtime_architecture",
                "guidance": "x",
                "evidence": {"summary": "failed", "locations": ["national_bot.py"]},
            },
        },
        "required_failures": [{"check_id": "typed_runtime_probe"}],
        "policy": {},
    }
    quality = {"national_capability_contract": capability}
    contracts = _architecture_contracts(quality, {"next_v": STRICT_TARGET_V + 1})
    assert contracts == [], (
        "the fallback must require BOTH the static map AND the flat contract's "
        "own evidence.locations to corroborate policy.py"
    )
