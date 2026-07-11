"""Generation policy for national-native runtime architecture debt.

The detector reports facts for one bot. This module turns those facts into a
stable parent-to-candidate contract: preserve every capability the parent has
and close one coherent debt bundle selected by the system, not by the LLM.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

from national_capability_contract import (
    NATIONAL_CAPABILITY_DETECTOR_VERSION,
    evaluate_national_capabilities,
)


RUNTIME_ARCHITECTURE_POLICY_VERSION = "3.4.0"
RUNTIME_ARCHITECTURE_POLICY_SCHEMA_VERSION = 9
RUNTIME_CONTRACT_LEDGER_SCHEMA_VERSION = 1
PREPARED_CAPABILITY_SNAPSHOT_SCHEMA_VERSION = 1
ARCHITECTURE_TRANSITION_PHASE_FINAL = "final"
ARCHITECTURE_TRANSITION_PHASE_PREPLAN = "preplan"
ARCHITECTURE_TRANSITION_PHASES = frozenset({
    ARCHITECTURE_TRANSITION_PHASE_FINAL,
    ARCHITECTURE_TRANSITION_PHASE_PREPLAN,
})
OFFICIAL_FULL_POLICY_ID = "official-full-v5"
OFFICIAL_ORACLE_DOC_DIGESTS: dict[str, str] = {
    "docs/official-raise-boundary-oracle-2026-07-11.md": (
        "a83a1ec2680577d71ddb985ddba00c5bcda40817ef2fb92c0c41938dccef3756"
    ),
    "docs/official-terminal-settlement-oracle-2026-07-11.md": (
        "ad96bc4fbe7939597b7a86ff6f9193ed2e50891be9b6b9c074883f5750c23bd9"
    ),
}
RUNTIME_CORRECTNESS_FLOOR_CHECKS: tuple[str, ...] = (
    "decision_time_budget_visible",
    "fast_strategy_baseline",
    "killable_decision_runtime",
    "decision_path_no_full_history_scan",
    "decision_path_no_large_runtime_tables",
    "persistent_match_memory",
    "terminal_response_memory",
    "showdown_range_posterior",
    "authoritative_hand_context",
)
# Backward-compatible exported name. Its semantics are now deliberately narrow:
# only system-provider/correctness guarantees are universal hard floors. Strategy
# consumption mechanisms are selected one at a time by RuntimeContract.state_learning.
RUNTIME_FLOOR_CHECKS = RUNTIME_CORRECTNESS_FLOOR_CHECKS
STATE_LEARNING_INNOVATION_CHECKS: tuple[str, ...] = (
    "incremental_refinement_protocol",
    "budget_scaled_refinement",
    "precompute_lookup_path",
    "precompute_runtime_influence",
    "incremental_opponent_model",
    "terminal_response_adaptation",
    "showdown_range_adaptation",
    "semantic_line_reachability",
)
NATIVE_TEMPLATE_PROVIDED_CHECKS: tuple[str, ...] = (
    "decision_time_budget_visible",
    "killable_decision_runtime",
    "persistent_match_memory",
    "terminal_response_memory",
    "showdown_range_posterior",
    "authoritative_hand_context",
)


def _verified_official_oracle_identity() -> dict[str, str]:
    project_root = Path(__file__).resolve().parents[2]
    observed: dict[str, str] = {}
    for relative, expected_digest in OFFICIAL_ORACLE_DOC_DIGESTS.items():
        path = project_root / relative
        if not path.is_file():
            raise RuntimeError(f"official oracle document missing: {relative}")
        actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_digest != expected_digest:
            raise RuntimeError(
                "official oracle document digest mismatch: "
                f"{relative} expected={expected_digest} actual={actual_digest}"
            )
        observed[relative] = actual_digest
    return observed


def _strategy_reference_pack_digest() -> str:
    """Pin the local recipe registry into the architecture-policy identity."""
    from strategy_reference_pack import reference_pack_registry_digest

    return reference_pack_registry_digest()

_FOCUS_SPECS: tuple[dict[str, Any], ...] = (
    {
        "focus_id": "national_runtime_v4_state_learning",
        "title": "Complete event-state and anytime-learning migration",
        "selection_checks": [
            "killable_decision_runtime",
            "fast_strategy_baseline",
            "incremental_refinement_protocol",
            "budget_scaled_refinement",
            "decision_path_no_full_history_scan",
            "decision_path_no_large_runtime_tables",
            "precompute_lookup_path",
            "precompute_runtime_influence",
            "persistent_match_memory",
            "terminal_response_memory",
            "showdown_range_posterior",
            "authoritative_hand_context",
            "incremental_opponent_model",
            "terminal_response_adaptation",
            "showdown_range_adaptation",
            "semantic_line_reachability",
        ],
        "required_checks": list(RUNTIME_CORRECTNESS_FLOOR_CHECKS),
        "innovation_checks": list(STATE_LEARNING_INNOVATION_CHECKS),
        "accepted_skill_layers": [
            "runtime_architecture",
            "precompute",
            "match_memory",
            "opponent_model",
            "line_template",
        ],
        "suggested_files": [
            "strategy.py",
            "simulation.py",
            "precompute.py",
            "opponent.py",
            "donk_probe.py",
        ],
        "required_terms": ["sanitized action", "telemetry"],
        "rationale": (
            "Keep wrapper-owned event state and protocol safety as universal correctness "
            "floors, then choose exactly one typed primary strategy innovation per generation. "
            "Only that work primitive, profile dimension, or line control becomes a new hard "
            "consumer gate; other dimensions remain shadow evidence unless the parent already "
            "passed them, in which case normal regression preservation remains blocking."
        ),
    },
    {
        "focus_id": "incremental_match_model",
        "title": "Incremental 70-hand opponent model",
        "required_checks": [
            "persistent_match_memory",
            "incremental_opponent_model",
            "decision_path_no_full_history_scan",
        ],
        "accepted_skill_layers": ["match_memory", "opponent_model", "runtime_architecture"],
        "suggested_files": ["national_bot.py", "strategy.py", "opponent.py", "state.py"],
        "required_terms": ["opponent_runtime", "confidence", "incremental"],
        "rationale": (
            "Use the uninterrupted 70-hand connection: update bounded facts on every action, "
            "settlement, and showdown; consume the snapshot directly instead of rebuilding from requests."
        ),
    },
    {
        "focus_id": "reusable_precompute",
        "title": "Bounded precomputed decision facts",
        "required_checks": ["precompute_lookup_path", "precompute_runtime_influence"],
        "accepted_skill_layers": ["precompute", "runtime_architecture"],
        "suggested_files": ["strategy.py", "simulation.py", "card_utils.py", "constants.py"],
        "required_terms": ["precompute", "bounded", "lookup"],
        "rationale": (
            "Spend memory once on pure poker facts or a bounded cache and prove that the live decision path consumes it."
        ),
    },
    {
        "focus_id": "deadline_refinement",
        "title": "Deadline-aware bounded refinement",
        "required_checks": ["decision_time_budget_visible"],
        "accepted_skill_layers": ["runtime_architecture", "precompute"],
        "suggested_files": ["strategy.py", "simulation.py", "constants.py"],
        "required_terms": ["deadline", "fallback", "budget"],
        "rationale": (
            "Return a legal baseline first, refine only while a monotonic deadline has budget, and preserve a deterministic fallback."
        ),
    },
    {
        "focus_id": "bounded_runtime_enumeration",
        "title": "Remove repeated combinatorial decision work",
        "required_checks": ["decision_path_no_large_runtime_tables"],
        "accepted_skill_layers": ["precompute", "runtime_architecture"],
        "suggested_files": ["simulation.py", "card_utils.py", "strategy.py"],
        "required_terms": ["bounded", "cache", "decision path"],
        "rationale": (
            "Replace repeated range/table construction in the live call graph with bounded reusable facts or incremental updates."
        ),
    },
    {
        "focus_id": "decision_path_purity",
        "title": "Decision path without external I/O",
        "required_checks": ["decision_path_no_external_io"],
        "accepted_skill_layers": ["runtime_architecture", "telemetry"],
        "suggested_files": ["strategy.py", "postflop.py", "opponent.py", "national_bot.py"],
        "required_terms": ["decision path", "telemetry", "I/O"],
        "rationale": "Move diagnostics and external I/O out of get_action and its reachable helpers.",
    },
)


def architecture_focus_specs() -> list[dict[str, Any]]:
    return deepcopy(list(_FOCUS_SPECS))


def _canonical_json_digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _runtime_contract_entry(task: dict[str, Any], index: int) -> dict[str, Any] | None:
    raw_contract = task.get("runtime_contract")
    if not isinstance(raw_contract, dict):
        return None
    from output_schema import RuntimeContract

    contract = RuntimeContract.model_validate(raw_contract).model_dump(mode="json")
    identity = {
        "worker_id": str(task.get("worker_id", index)),
        "skill_layer": str(task.get("skill_layer") or ""),
        "architecture_focus_id": str(task.get("architecture_focus_id") or ""),
        "runtime_contract": contract,
    }
    return {
        **identity,
        "contract_digest": _canonical_json_digest(identity),
    }


def _ledger_payload(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": RUNTIME_CONTRACT_LEDGER_SCHEMA_VERSION,
        "entries": entries,
    }


def build_runtime_contract_ledger(
    plan: dict[str, Any],
    *,
    inherited_ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an immutable contract ledger from accepted and inherited tasks.

    Repair rounds replace the executable task list, but they must not erase the
    contracts that were accepted with the original Master plan. The ledger is
    system-owned: callers attach it only after schema/hard validation.
    """
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    if isinstance(inherited_ledger, dict):
        inherited_errors = validate_runtime_contract_ledger(inherited_ledger)
        if inherited_errors:
            raise ValueError("invalid inherited runtime contract ledger: " + "; ".join(inherited_errors))
        for item in inherited_ledger.get("entries") or []:
            digest = str(item.get("contract_digest") or "")
            if digest and digest not in seen:
                entries.append(deepcopy(item))
                seen.add(digest)
    for index, task in enumerate(plan.get("tasks") or [], start=1):
        if not isinstance(task, dict):
            continue
        entry = _runtime_contract_entry(task, index)
        if entry is None or entry["contract_digest"] in seen:
            continue
        entries.append(entry)
        seen.add(entry["contract_digest"])
    payload = _ledger_payload(entries)
    return {**payload, "ledger_digest": _canonical_json_digest(payload)}


def validate_runtime_contract_ledger(ledger: dict[str, Any] | None) -> list[str]:
    if not isinstance(ledger, dict):
        return ["runtime_contract_ledger_missing_or_not_object"]
    errors: list[str] = []
    if ledger.get("schema_version") != RUNTIME_CONTRACT_LEDGER_SCHEMA_VERSION:
        errors.append(
            "runtime_contract_ledger_schema_mismatch: "
            f"expected={RUNTIME_CONTRACT_LEDGER_SCHEMA_VERSION} "
            f"actual={ledger.get('schema_version')!r}"
        )
    entries = ledger.get("entries")
    if not isinstance(entries, list):
        return [*errors, "runtime_contract_ledger_entries_not_list"]
    normalized_entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(entries, start=1):
        if not isinstance(item, dict):
            errors.append(f"runtime_contract_ledger_entry_{index}_not_object")
            continue
        try:
            rebuilt = _runtime_contract_entry(
                {
                    "worker_id": item.get("worker_id"),
                    "skill_layer": item.get("skill_layer"),
                    "architecture_focus_id": item.get("architecture_focus_id"),
                    "runtime_contract": item.get("runtime_contract"),
                },
                index,
            )
        except Exception as exc:
            errors.append(
                f"runtime_contract_ledger_entry_{index}_schema_error:"
                f"{type(exc).__name__}:{str(exc)[:180]}"
            )
            continue
        if rebuilt is None:
            errors.append(f"runtime_contract_ledger_entry_{index}_missing_contract")
            continue
        if item.get("contract_digest") != rebuilt["contract_digest"]:
            errors.append(f"runtime_contract_ledger_entry_{index}_digest_mismatch")
        if rebuilt["contract_digest"] in seen:
            errors.append(f"runtime_contract_ledger_entry_{index}_duplicate")
        seen.add(rebuilt["contract_digest"])
        normalized_entries.append(rebuilt)
    payload = _ledger_payload(normalized_entries)
    if ledger.get("ledger_digest") != _canonical_json_digest(payload):
        errors.append("runtime_contract_ledger_digest_mismatch")
    return errors


def runtime_contract_ledger_digest(ledger_or_plan: dict[str, Any] | None) -> str:
    value = ledger_or_plan
    if isinstance(value, dict) and "runtime_contract_ledger" in value:
        value = value.get("runtime_contract_ledger")
    if validate_runtime_contract_ledger(value):
        return ""
    return str(value.get("ledger_digest") or "")


def attach_runtime_contract_ledger(
    plan: dict[str, Any],
    *,
    replace: bool = False,
) -> dict[str, Any]:
    """Attach or extend the system-owned ledger without dropping old entries."""
    if not isinstance(plan, dict):
        return plan
    inherited = None if replace else plan.get("runtime_contract_ledger")
    if inherited is not None and validate_runtime_contract_ledger(inherited):
        # Preserve corrupt evidence so the next hard gate fails closed instead
        # of silently blessing a rewritten checkpoint.
        return plan
    ledger = build_runtime_contract_ledger(plan, inherited_ledger=inherited)
    return {**plan, "runtime_contract_ledger": ledger}


def _ledger_contracts(plan: dict[str, Any]) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    ledger = plan.get("runtime_contract_ledger") if isinstance(plan, dict) else None
    if ledger is not None:
        errors = validate_runtime_contract_ledger(ledger)
        if errors:
            return [], errors
        return [
            (f"ledger_{index}", item["runtime_contract"])
            for index, item in enumerate(ledger.get("entries") or [], start=1)
        ], []
    # Backward compatibility for checkpoints created before ledger schema v1.
    contracts = []
    for index, task in enumerate(plan.get("tasks") or [], start=1):
        if isinstance(task, dict) and isinstance(task.get("runtime_contract"), dict):
            contracts.append((f"task_{index}", task["runtime_contract"]))
    return contracts, []


def _check_state(capabilities: dict[str, Any]) -> dict[str, bool]:
    return {
        str(item.get("check_id") or item.get("name")): bool(item.get("passed"))
        for item in capabilities.get("checks") or []
        if item.get("check_id") or item.get("name")
    }


def _system_owned_external_io_only(capabilities: dict[str, Any]) -> bool:
    check = (capabilities.get("checks_by_id") or {}).get(
        "decision_path_no_external_io"
    ) or {}
    locations = [
        str(item) for item in (check.get("evidence") or {}).get("locations") or []
    ]
    return bool(
        locations
        and all(location.startswith("national_bot.py:") for location in locations)
    )


def _focus_check_state(capabilities: dict[str, Any]) -> dict[str, bool]:
    state = _check_state(capabilities)
    if _system_owned_external_io_only(capabilities):
        state["decision_path_no_external_io"] = True
    return state


def _task_writable_filenames(task: dict[str, Any]) -> set[str]:
    return {
        Path(str(item)).name
        for item in [
            *(task.get("target_files") or []),
            *(task.get("files_allowed") or []),
        ]
        if str(item).strip()
    }


def _is_explicit_official_protocol_repair(task: dict[str, Any]) -> bool:
    """Recognize the one system route allowed to edit the native entrypoint.

    Master output cannot declare these repair-only fields because its Pydantic
    schema forbids extras.  Requiring the deterministic task kind, blocker, and
    exact protocol role prevents an ordinary strategy task from self-labeling a
    writable system provider.  State-learning work is never an exception.
    """
    runtime_contract = task.get("runtime_contract")
    state_learning = (
        runtime_contract.get("state_learning")
        if isinstance(runtime_contract, dict)
        else None
    )
    target_names = {
        Path(str(item)).name for item in task.get("target_files") or []
    }
    must_change_names = {
        Path(str(item)).name for item in task.get("must_change_files") or []
    }
    return bool(
        str(task.get("worker_id") or "") == "auto_official_full_repair"
        and str(task.get("task_kind") or "") == "official_repair"
        and str(task.get("repair_blocker") or "") == "official_full"
        and str(task.get("role") or "") == "Protocol Integration Architect"
        and not str(task.get("architecture_focus_id") or "").strip()
        and not state_learning
        and target_names == {"national_bot.py"}
        and must_change_names == target_names
        and not (task.get("files_allowed") or [])
    )


def _capability_infrastructure_failures(
    capabilities: dict[str, Any] | None,
    *,
    side: str,
) -> list[dict[str, Any]]:
    """Normalize detector/probe failures without turning unknown checks false."""
    if not isinstance(capabilities, dict):
        return [{
            "side": side,
            "component": "national_capability_contract",
            "failure_class": "internal_infrastructure",
            "issues": ["capability_result_missing_or_not_object"],
        }]
    failures = capabilities.get("infrastructure_failures") or []
    normalized = []
    for item in failures:
        if not isinstance(item, dict):
            item = {"issues": [str(item)]}
        normalized.append({
            "side": side,
            "component": str(item.get("component") or "national_capability_contract"),
            "failure_class": str(item.get("failure_class") or "infrastructure"),
            "issues": [str(issue) for issue in (item.get("issues") or [])[:8]]
            or ["unspecified_capability_infrastructure_failure"],
        })
    if not normalized and capabilities.get("outcome") == "infrastructure_failure":
        normalized.append({
            "side": side,
            "component": "national_capability_contract",
            "failure_class": "internal_infrastructure",
            "issues": ["capability_contract_reported_infrastructure_failure_without_detail"],
        })
    return normalized


def _state_digest(state: dict[str, bool]) -> str:
    payload = {
        "detector_version": NATIONAL_CAPABILITY_DETECTOR_VERSION,
        "checks": {key: bool(state[key]) for key in sorted(state)},
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


_PREPARED_CAPABILITY_SNAPSHOT_KEYS = frozenset({
    "schema_version",
    "detector_version",
    "parent_bot",
    "prepared_bot",
    "parent_checks",
    "prepared_checks",
    "parent_capability_digest",
    "prepared_capability_digest",
    "parent_passed_checks",
    "prepared_passed_checks",
    "protected_passed_checks",
    "acquired_checks",
    "snapshot_digest",
})


def _prepared_capability_snapshot_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable, serializable part of a prepared baseline snapshot."""
    return {
        key: deepcopy(snapshot.get(key))
        for key in sorted(_PREPARED_CAPABILITY_SNAPSHOT_KEYS - {"snapshot_digest"})
    }


def _normalized_snapshot_state(value: Any) -> dict[str, bool] | None:
    if not isinstance(value, dict):
        return None
    normalized: dict[str, bool] = {}
    for raw_key, raw_passed in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            return None
        if not isinstance(raw_passed, bool):
            return None
        normalized[raw_key] = raw_passed
    return {key: normalized[key] for key in sorted(normalized)}


def build_prepared_capability_snapshot(
    parent_bot_dir: str | Path,
    prepared_bot_dir: str | Path,
    *,
    parent_capabilities: dict[str, Any] | None = None,
    prepared_capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze the accepted pre-Worker child capability state.

    A crossover child is subsequently edited in place by Workers, so its
    pre-Worker detector result cannot safely be reconstructed at the final
    gate.  This snapshot records only normalized detector facts and their
    derived preservation set.  The complete payload is digest-bound and can be
    embedded into the system-owned architecture policy.
    """
    parent_bot_dir = Path(parent_bot_dir)
    prepared_bot_dir = Path(prepared_bot_dir)
    parent_result = (
        parent_capabilities
        if parent_capabilities is not None
        else evaluate_national_capabilities(parent_bot_dir)
    )
    prepared_result = (
        prepared_capabilities
        if prepared_capabilities is not None
        else evaluate_national_capabilities(prepared_bot_dir)
    )
    infrastructure_failures = [
        *_capability_infrastructure_failures(parent_result, side="parent"),
        *_capability_infrastructure_failures(prepared_result, side="prepared"),
    ]
    if infrastructure_failures:
        details = "; ".join(
            f"{item['side']}/{item['component']}: {', '.join(item['issues'])}"
            for item in infrastructure_failures
        )
        raise RuntimeError(
            "prepared capability snapshot is inconclusive because infrastructure failed: "
            + details[:1000]
        )
    parent_detector = str(parent_result.get("detector_version") or "")
    prepared_detector = str(prepared_result.get("detector_version") or "")
    if not parent_detector or parent_detector != prepared_detector:
        raise RuntimeError(
            "prepared capability snapshot detector mismatch: "
            f"parent={parent_detector!r} prepared={prepared_detector!r}"
        )

    # Normalize the wrapper-owned external-I/O exception before freezing the
    # state, exactly as architecture focus selection does.  This prevents the
    # system's killable worker transport from becoming artificial child debt.
    parent_state = _focus_check_state(parent_result)
    prepared_state = _focus_check_state(prepared_result)
    parent_passed = sorted(key for key, passed in parent_state.items() if passed)
    prepared_passed = sorted(key for key, passed in prepared_state.items() if passed)
    protected_passed = sorted(set(parent_passed) | set(prepared_passed))
    snapshot = {
        "schema_version": PREPARED_CAPABILITY_SNAPSHOT_SCHEMA_VERSION,
        "detector_version": parent_detector,
        "parent_bot": parent_bot_dir.name,
        "prepared_bot": prepared_bot_dir.name,
        "parent_checks": {key: bool(parent_state[key]) for key in sorted(parent_state)},
        "prepared_checks": {
            key: bool(prepared_state[key]) for key in sorted(prepared_state)
        },
        "parent_capability_digest": _state_digest(parent_state),
        "prepared_capability_digest": _state_digest(prepared_state),
        "parent_passed_checks": parent_passed,
        "prepared_passed_checks": prepared_passed,
        "protected_passed_checks": protected_passed,
        "acquired_checks": sorted(set(prepared_passed) - set(parent_passed)),
    }
    snapshot["snapshot_digest"] = _canonical_json_digest(
        _prepared_capability_snapshot_payload(snapshot)
    )
    return snapshot


def validate_prepared_capability_snapshot(
    snapshot: dict[str, Any] | None,
    *,
    parent_bot_dir: str | Path | None = None,
    prepared_bot_dir: str | Path | None = None,
    parent_capabilities: dict[str, Any] | None = None,
    prepared_capabilities: dict[str, Any] | None = None,
) -> list[str]:
    """Validate structure, derived state, digest, and optional live identities."""
    if not isinstance(snapshot, dict):
        return ["prepared_capability_snapshot_missing_or_not_object"]
    errors: list[str] = []
    unexpected = sorted(set(snapshot) - _PREPARED_CAPABILITY_SNAPSHOT_KEYS)
    missing = sorted(_PREPARED_CAPABILITY_SNAPSHOT_KEYS - set(snapshot))
    if unexpected:
        errors.append(f"prepared_capability_snapshot_unexpected_fields:{unexpected}")
    if missing:
        errors.append(f"prepared_capability_snapshot_missing_fields:{missing}")
    if snapshot.get("schema_version") != PREPARED_CAPABILITY_SNAPSHOT_SCHEMA_VERSION:
        errors.append(
            "prepared_capability_snapshot_schema_mismatch: "
            f"expected={PREPARED_CAPABILITY_SNAPSHOT_SCHEMA_VERSION} "
            f"actual={snapshot.get('schema_version')!r}"
        )
    detector_version = snapshot.get("detector_version")
    if not isinstance(detector_version, str) or not detector_version:
        errors.append("prepared_capability_snapshot_detector_version_invalid")
    for field in ("parent_bot", "prepared_bot"):
        value = snapshot.get(field)
        if not isinstance(value, str) or not value:
            errors.append(f"prepared_capability_snapshot_{field}_invalid")

    parent_state = _normalized_snapshot_state(snapshot.get("parent_checks"))
    prepared_state = _normalized_snapshot_state(snapshot.get("prepared_checks"))
    if parent_state is None:
        errors.append("prepared_capability_snapshot_parent_checks_invalid")
        parent_state = {}
    if prepared_state is None:
        errors.append("prepared_capability_snapshot_prepared_checks_invalid")
        prepared_state = {}

    expected_parent_passed = sorted(key for key, passed in parent_state.items() if passed)
    expected_prepared_passed = sorted(
        key for key, passed in prepared_state.items() if passed
    )
    expected_protected = sorted(set(expected_parent_passed) | set(expected_prepared_passed))
    expected_acquired = sorted(set(expected_prepared_passed) - set(expected_parent_passed))
    for field, expected in (
        ("parent_passed_checks", expected_parent_passed),
        ("prepared_passed_checks", expected_prepared_passed),
        ("protected_passed_checks", expected_protected),
        ("acquired_checks", expected_acquired),
    ):
        if snapshot.get(field) != expected:
            errors.append(
                f"prepared_capability_snapshot_{field}_mismatch: "
                f"expected={expected!r} actual={snapshot.get(field)!r}"
            )
    expected_parent_digest = _state_digest(parent_state)
    expected_prepared_digest = _state_digest(prepared_state)
    if snapshot.get("parent_capability_digest") != expected_parent_digest:
        errors.append("prepared_capability_snapshot_parent_capability_digest_mismatch")
    if snapshot.get("prepared_capability_digest") != expected_prepared_digest:
        errors.append("prepared_capability_snapshot_prepared_capability_digest_mismatch")
    expected_snapshot_digest = _canonical_json_digest(
        _prepared_capability_snapshot_payload(snapshot)
    )
    if snapshot.get("snapshot_digest") != expected_snapshot_digest:
        errors.append("prepared_capability_snapshot_digest_mismatch")

    if parent_bot_dir is not None and snapshot.get("parent_bot") != Path(parent_bot_dir).name:
        errors.append(
            "prepared_capability_snapshot_parent_bot_mismatch: "
            f"expected={Path(parent_bot_dir).name!r} actual={snapshot.get('parent_bot')!r}"
        )
    if (
        prepared_bot_dir is not None
        and snapshot.get("prepared_bot") != Path(prepared_bot_dir).name
    ):
        errors.append(
            "prepared_capability_snapshot_prepared_bot_mismatch: "
            f"expected={Path(prepared_bot_dir).name!r} actual={snapshot.get('prepared_bot')!r}"
        )

    for label, capabilities, expected_state in (
        ("parent", parent_capabilities, parent_state),
        ("prepared", prepared_capabilities, prepared_state),
    ):
        if capabilities is None:
            continue
        infrastructure = _capability_infrastructure_failures(capabilities, side=label)
        if infrastructure:
            errors.append(f"prepared_capability_snapshot_{label}_infrastructure_failure")
            continue
        live_state = _focus_check_state(capabilities)
        if live_state != expected_state:
            errors.append(f"prepared_capability_snapshot_{label}_checks_mismatch")
        live_detector = str(capabilities.get("detector_version") or "")
        if live_detector != detector_version:
            errors.append(
                f"prepared_capability_snapshot_{label}_detector_version_mismatch"
            )
    return errors


def prepared_capability_snapshot_digest(snapshot: dict[str, Any] | None) -> str:
    """Return a validated snapshot digest, or an empty string for bad evidence."""
    if validate_prepared_capability_snapshot(snapshot):
        return ""
    return str(snapshot.get("snapshot_digest") or "")


def _policy_contract_payload(policy: dict[str, Any]) -> dict[str, Any]:
    """Return the complete immutable contract represented by a policy."""
    return {
        "schema_version": policy.get("schema_version"),
        "policy_version": policy.get("policy_version"),
        "official_policy_id": policy.get("official_policy_id"),
        "official_oracle_digests": policy.get("official_oracle_digests") or {},
        "strategy_reference_pack_digest": policy.get("strategy_reference_pack_digest"),
        "detector_version": policy.get("detector_version"),
        "source_bot": policy.get("source_bot"),
        "source_capability_digest": policy.get("source_capability_digest"),
        "source_checks": policy.get("source_checks") or {},
        "prepared_capability_snapshot": policy.get("prepared_capability_snapshot"),
        "prepared_capability_snapshot_digest": policy.get(
            "prepared_capability_snapshot_digest"
        ),
        "effective_baseline_bot": policy.get("effective_baseline_bot"),
        "effective_baseline_capability_digest": policy.get(
            "effective_baseline_capability_digest"
        ),
        "effective_baseline_checks": policy.get("effective_baseline_checks") or {},
        "baseline_passed_checks": policy.get("baseline_passed_checks") or [],
        "runtime_floor_checks": policy.get("runtime_floor_checks") or [],
        "strategy_innovation_checks": policy.get("strategy_innovation_checks") or [],
        "source_floor_failures": policy.get("source_floor_failures") or [],
        "baseline_floor_failures": policy.get("baseline_floor_failures") or [],
        "native_template_provided_checks": policy.get("native_template_provided_checks") or [],
        "plan_required_floor_checks": policy.get("plan_required_floor_checks") or [],
        "selected_focus": policy.get("selected_focus"),
    }


def _policy_contract_digest(policy: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            _policy_contract_payload(policy),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _select_architecture_focus_from_state(
    state: dict[str, bool],
) -> dict[str, Any] | None:
    for spec in _FOCUS_SPECS:
        selection_checks = spec.get("selection_checks") or spec["required_checks"]
        unresolved = [
            check_id for check_id in selection_checks
            if not state.get(check_id, False)
        ]
        if unresolved:
            selected = deepcopy(spec)
            selected["source_unresolved_checks"] = unresolved
            return selected
    return None


def select_architecture_focus(capabilities: dict[str, Any]) -> dict[str, Any] | None:
    return _select_architecture_focus_from_state(_focus_check_state(capabilities))


def build_architecture_policy(
    source_bot_dir: str | Path,
    *,
    source_capabilities: dict[str, Any] | None = None,
    prepared_capability_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_bot_dir = Path(source_bot_dir)
    capabilities = source_capabilities or evaluate_national_capabilities(source_bot_dir)
    infrastructure_failures = _capability_infrastructure_failures(
        capabilities,
        side="source",
    )
    if infrastructure_failures:
        details = "; ".join(
            f"{item['component']}: {', '.join(item['issues'])}"
            for item in infrastructure_failures
        )
        raise RuntimeError(
            "source capability assessment is inconclusive because infrastructure failed: "
            + details[:1000]
        )
    state = _check_state(capabilities)
    snapshot = None
    if prepared_capability_snapshot is not None:
        snapshot_errors = validate_prepared_capability_snapshot(
            prepared_capability_snapshot,
            parent_bot_dir=source_bot_dir,
            parent_capabilities=capabilities,
        )
        if snapshot_errors:
            raise ValueError(
                "invalid prepared capability snapshot: " + "; ".join(snapshot_errors)
            )
        snapshot = deepcopy(prepared_capability_snapshot)
        parent_snapshot_state = dict(snapshot["parent_checks"])
        prepared_state = dict(snapshot["prepared_checks"])
        effective_state = {
            check_id: bool(
                parent_snapshot_state.get(check_id, False)
                or prepared_state.get(check_id, False)
            )
            for check_id in sorted(set(parent_snapshot_state) | set(prepared_state))
        }
        effective_baseline_bot = str(snapshot["prepared_bot"])
        focus_state = effective_state
    else:
        effective_state = dict(state)
        effective_baseline_bot = source_bot_dir.name
        focus_state = _focus_check_state(capabilities)
    focus = _select_architecture_focus_from_state(focus_state)
    source_floor_failures = [
        check_id for check_id in RUNTIME_FLOOR_CHECKS if not state.get(check_id, False)
    ]
    baseline_floor_failures = [
        check_id
        for check_id in RUNTIME_FLOOR_CHECKS
        if not effective_state.get(check_id, False)
    ]
    policy = {
        "schema_version": RUNTIME_ARCHITECTURE_POLICY_SCHEMA_VERSION,
        "policy_version": RUNTIME_ARCHITECTURE_POLICY_VERSION,
        "official_policy_id": OFFICIAL_FULL_POLICY_ID,
        "official_oracle_digests": _verified_official_oracle_identity(),
        "strategy_reference_pack_digest": _strategy_reference_pack_digest(),
        "detector_version": capabilities.get("detector_version"),
        "source_bot": source_bot_dir.name,
        "source_capability_digest": _state_digest(state),
        "source_checks": state,
        "prepared_capability_snapshot": snapshot,
        "prepared_capability_snapshot_digest": (
            str(snapshot["snapshot_digest"]) if snapshot is not None else None
        ),
        "effective_baseline_bot": effective_baseline_bot,
        "effective_baseline_capability_digest": _state_digest(effective_state),
        "effective_baseline_checks": effective_state,
        "baseline_passed_checks": sorted(
            check_id for check_id, passed in effective_state.items() if passed
        ),
        "runtime_floor_checks": list(RUNTIME_FLOOR_CHECKS),
        "strategy_innovation_checks": list(STATE_LEARNING_INNOVATION_CHECKS),
        "source_floor_failures": source_floor_failures,
        "baseline_floor_failures": baseline_floor_failures,
        "native_template_provided_checks": list(NATIVE_TEMPLATE_PROVIDED_CHECKS),
        "plan_required_floor_checks": [
            check_id
            for check_id in baseline_floor_failures
            if check_id not in NATIVE_TEMPLATE_PROVIDED_CHECKS
        ],
        "selected_focus": focus,
    }
    policy["policy_digest"] = _policy_contract_digest(policy)
    return policy


def _policy_identity_errors(
    expected_policy: dict[str, Any],
    current_policy: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    for key in (
        "schema_version",
        "policy_version",
        "official_policy_id",
        "official_oracle_digests",
        "strategy_reference_pack_digest",
        "detector_version",
        "source_bot",
        "source_capability_digest",
        "prepared_capability_snapshot_digest",
        "effective_baseline_bot",
        "effective_baseline_capability_digest",
    ):
        if expected_policy.get(key) != current_policy.get(key):
            errors.append(
                f"architecture_policy_{key}_mismatch: expected={expected_policy.get(key)!r} "
                f"current={current_policy.get(key)!r}"
            )
    for label, policy in (("expected", expected_policy), ("current", current_policy)):
        snapshot = policy.get("prepared_capability_snapshot")
        if snapshot is None:
            continue
        for error in validate_prepared_capability_snapshot(snapshot):
            errors.append(f"architecture_policy_{label}_{error}")
    expected_stored_digest = str(expected_policy.get("policy_digest") or "")
    expected_content_digest = _policy_contract_digest(expected_policy)
    current_stored_digest = str(current_policy.get("policy_digest") or "")
    current_content_digest = _policy_contract_digest(current_policy)
    if expected_stored_digest != expected_content_digest:
        errors.append(
            "architecture_policy_expected_content_digest_mismatch: "
            f"stored={expected_stored_digest!r} computed={expected_content_digest!r}"
        )
    if current_stored_digest != current_content_digest:
        errors.append(
            "architecture_policy_current_content_digest_mismatch: "
            f"stored={current_stored_digest!r} computed={current_content_digest!r}"
        )
    if expected_content_digest != current_content_digest:
        errors.append(
            "architecture_policy_contract_digest_mismatch: "
            f"expected={expected_content_digest!r} current={current_content_digest!r}"
        )
    return errors


def evaluate_architecture_transition(
    source_bot_dir: str | Path,
    candidate_bot_dir: str | Path,
    *,
    expected_policy: dict[str, Any] | None = None,
    evaluation_phase: str = ARCHITECTURE_TRANSITION_PHASE_FINAL,
) -> dict[str, Any]:
    if evaluation_phase not in ARCHITECTURE_TRANSITION_PHASES:
        raise ValueError(
            "unknown architecture transition evaluation phase: "
            f"{evaluation_phase!r}"
        )
    source_capabilities = evaluate_national_capabilities(source_bot_dir)
    candidate_capabilities = evaluate_national_capabilities(candidate_bot_dir)
    infrastructure_failures = [
        *_capability_infrastructure_failures(source_capabilities, side="source"),
        *_capability_infrastructure_failures(candidate_capabilities, side="candidate"),
    ]
    if infrastructure_failures:
        return {
            "schema_version": 1,
            "policy_version": RUNTIME_ARCHITECTURE_POLICY_VERSION,
            "ok": False,
            "conclusive": False,
            "outcome": "infrastructure_failure",
            "failure_class": "infrastructure",
            "evaluation_phase": evaluation_phase,
            "policy": expected_policy if isinstance(expected_policy, dict) else None,
            "policy_identity_errors": [],
            "infrastructure_failures": infrastructure_failures,
            # Compatibility alias for existing quality telemetry. This is not a
            # candidate blocker and must never be routed into repair workers.
            "runtime_probe_infra": infrastructure_failures,
            "candidate_failures": [],
            "regressions": [],
            "system_provided_deltas": [],
            "runtime_floor_checks": list(RUNTIME_FLOOR_CHECKS),
            "runtime_floor_failures": [],
            "deferred_runtime_floor_checks": [],
            "deferred_runtime_floor_failures": [],
            "strategy_shadow_checks": [],
            "selected_focus": None,
            "unresolved_focus_checks": [],
            "full_unresolved_focus_checks": [],
            "deferred_unresolved_focus_checks": [],
            "source_capabilities": source_capabilities,
            "candidate_capabilities": candidate_capabilities,
        }
    expected_snapshot = (
        expected_policy.get("prepared_capability_snapshot")
        if isinstance(expected_policy, dict)
        else None
    )
    snapshot_identity_errors: list[str] = []
    if expected_snapshot is not None:
        snapshot_identity_errors = [
            f"architecture_policy_{error}"
            for error in validate_prepared_capability_snapshot(
                expected_snapshot,
                parent_bot_dir=source_bot_dir,
                prepared_bot_dir=candidate_bot_dir,
                parent_capabilities=source_capabilities,
            )
        ]
    current_policy = build_architecture_policy(
        source_bot_dir,
        source_capabilities=source_capabilities,
        prepared_capability_snapshot=(
            expected_snapshot if expected_snapshot is not None and not snapshot_identity_errors else None
        ),
    )
    identity_errors = snapshot_identity_errors
    if isinstance(expected_policy, dict):
        identity_errors.extend(_policy_identity_errors(expected_policy, current_policy))
    policy = expected_policy if isinstance(expected_policy, dict) and not identity_errors else current_policy
    source_state = _check_state(source_capabilities)
    candidate_state = _check_state(candidate_capabilities)
    regressions = []
    system_provided_deltas = []
    candidate_checks = candidate_capabilities.get("checks_by_id") or {}
    protected_passed_checks = {
        str(check_id)
        for check_id in policy.get("baseline_passed_checks") or []
        if str(check_id)
    }
    prepared_checks = (
        (policy.get("prepared_capability_snapshot") or {}).get("prepared_checks") or {}
    )
    for check_id in sorted(protected_passed_checks):
        if candidate_state.get(check_id, False):
            continue
        check = candidate_checks.get(check_id) or {}
        locations = [
            str(item) for item in (check.get("evidence") or {}).get("locations") or []
        ]
        if (
            check_id == "decision_path_no_external_io"
            and _system_owned_external_io_only(candidate_capabilities)
        ):
            # The current system wrapper deliberately owns a killable subprocess
            # worker. Do not misclassify that provider mechanism as strategy I/O;
            # any strategy/helper location still takes the normal regression path.
            system_provided_deltas.append({
                "check_id": check_id,
                "reason": "killable_strategy_worker_owned_by_national_bot",
                "locations": locations[:8],
            })
            continue
        regressions.append({
            "check_id": check_id,
            "source_passed": True,
            "baseline_origin": (
                "parent_and_prepared"
                if source_state.get(check_id, False)
                and prepared_checks.get(check_id, False)
                else "prepared_child"
                if prepared_checks.get(check_id, False)
                else "parent"
            ),
            "candidate_passed": False,
            "guidance": check.get("guidance", ""),
        })
    focus = policy.get("selected_focus") or None
    full_unresolved_focus = []
    if focus:
        full_unresolved_focus = [
            check_id
            for check_id in focus.get("required_checks") or []
            if not candidate_state.get(check_id, False)
        ]
    # Crossover is a preparation operator, not the generation's implementation
    # worker.  The system wrapper must already provide its owned correctness
    # checks, but source debt explicitly assigned to plan_required_floor_checks
    # is closed only after direction audit -> Master -> Workers.  Final quality
    # evaluation keeps the historic fail-closed behavior and defers nothing.
    deferred_floor_checks = (
        [
            str(check_id)
            for check_id in policy.get("plan_required_floor_checks") or []
            if str(check_id) in RUNTIME_FLOOR_CHECKS
        ]
        if evaluation_phase == ARCHITECTURE_TRANSITION_PHASE_PREPLAN
        else []
    )
    deferred_floor_set = set(deferred_floor_checks)
    unresolved_focus = [
        check_id
        for check_id in full_unresolved_focus
        if check_id not in deferred_floor_set
    ]
    deferred_unresolved_focus = [
        check_id
        for check_id in full_unresolved_focus
        if check_id in deferred_floor_set
    ]
    required_failures = candidate_capabilities.get("required_failures") or []
    floor_failures = [
        {
            "check_id": check_id,
            "guidance": (
                (candidate_capabilities.get("checks_by_id") or {}).get(check_id, {}).get("guidance", "")
            ),
        }
        for check_id in RUNTIME_FLOOR_CHECKS
        if check_id not in deferred_floor_set
        if not candidate_state.get(check_id, False)
    ]
    deferred_floor_failures = [
        {
            "check_id": check_id,
            "guidance": (
                (candidate_capabilities.get("checks_by_id") or {})
                .get(check_id, {})
                .get("guidance", "")
            ),
        }
        for check_id in deferred_floor_checks
        if not candidate_state.get(check_id, False)
    ]
    strategy_shadow_checks = [
        {"check_id": check_id, "passed": bool(candidate_state.get(check_id, False))}
        for check_id in STATE_LEARNING_INNOVATION_CHECKS
    ]
    return {
        "schema_version": 1,
        "policy_version": RUNTIME_ARCHITECTURE_POLICY_VERSION,
        "evaluation_phase": evaluation_phase,
        "ok": (
            not identity_errors
            and not required_failures
            and not floor_failures
            and not regressions
            and not unresolved_focus
        ),
        "conclusive": True,
        "outcome": (
            "passed"
            if not identity_errors
            and not required_failures
            and not floor_failures
            and not regressions
            and not unresolved_focus
            else "candidate_failure"
        ),
        "failure_class": (
            "none"
            if not identity_errors
            and not required_failures
            and not floor_failures
            and not regressions
            and not unresolved_focus
            else "candidate"
        ),
        "policy": policy,
        "policy_identity_errors": identity_errors,
        "infrastructure_failures": [],
        "runtime_probe_infra": [],
        "regressions": regressions,
        "system_provided_deltas": system_provided_deltas,
        "runtime_floor_checks": list(RUNTIME_FLOOR_CHECKS),
        "runtime_floor_failures": floor_failures,
        "deferred_runtime_floor_checks": deferred_floor_checks,
        "deferred_runtime_floor_failures": deferred_floor_failures,
        "strategy_shadow_checks": strategy_shadow_checks,
        "selected_focus": focus,
        "unresolved_focus_checks": unresolved_focus,
        "full_unresolved_focus_checks": full_unresolved_focus,
        "deferred_unresolved_focus_checks": deferred_unresolved_focus,
        "source_capabilities": source_capabilities,
        "candidate_capabilities": candidate_capabilities,
    }


def validate_plan_architecture_focus(
    plan: dict[str, Any],
    policy: dict[str, Any] | None = None,
) -> list[str]:
    policy = policy or (plan.get("architecture_policy") if isinstance(plan, dict) else None)
    if not isinstance(policy, dict):
        return []
    focus = policy.get("selected_focus") or None
    tasks = [task for task in (plan.get("tasks") or []) if isinstance(task, dict)]
    errors: list[str] = []
    writable_native_tasks = [
        task for task in tasks
        if "national_bot.py" in _task_writable_filenames(task)
        and not _is_explicit_official_protocol_repair(task)
    ]
    for task in writable_native_tasks:
        errors.append(
            "system-provided national_bot.py is read-only for ordinary strategy/"
            "state-learning tasks; writable access requires the deterministic "
            "official_repair/official_full Protocol Integration Architect route "
            f"(worker_id={task.get('worker_id')!r})."
        )

    state_learning_tasks = []
    for task in tasks:
        raw_contract = task.get("runtime_contract")
        if not isinstance(raw_contract, dict) or raw_contract.get("state_learning") is None:
            continue
        state_learning_tasks.append(task)
        if str(task.get("architecture_focus_id") or "") != "national_runtime_v4_state_learning":
            errors.append(
                "runtime_contract.state_learning may appear only on the single "
                "national_runtime_v4_state_learning primary task "
                f"(worker_id={task.get('worker_id')!r})."
            )

    required_floor = [str(item) for item in policy.get("plan_required_floor_checks") or []]
    for check_id in required_floor:
        matching_floor = [
            task for task in tasks
            if check_id in {str(item) for item in task.get("checks_required") or []}
        ]
        if not matching_floor:
            errors.append(
                f"Runtime floor check {check_id!r} is unresolved in the source and must be "
                "declared in checks_required by at least one worker task."
            )
    if not focus:
        return errors
    focus_id = str(focus.get("focus_id") or "")
    matching = [task for task in tasks if str(task.get("architecture_focus_id") or "") == focus_id]
    if not matching:
        errors.append(
            f"Architecture focus {focus_id!r} is mandatory for this generation; "
            "one worker task must declare the same architecture_focus_id."
        )
        return errors
    if len(matching) != 1:
        errors.append(
            f"Architecture focus {focus_id!r} requires exactly one primary worker task; "
            f"found {len(matching)}."
        )
    if focus_id == "national_runtime_v4_state_learning" and len(state_learning_tasks) != 1:
        errors.append(
            "Architecture focus 'national_runtime_v4_state_learning' requires "
            "exactly one state_learning primary across the entire generation; "
            f"found {len(state_learning_tasks)}."
        )

    accepted_layers = set(focus.get("accepted_skill_layers") or [])
    suggested_files = {Path(str(path)).name for path in focus.get("suggested_files") or []}
    required_terms = [str(term).lower() for term in focus.get("required_terms") or []]
    for task in matching:
        layer = str(task.get("skill_layer") or "")
        if layer not in accepted_layers:
            errors.append(
                f"Architecture focus {focus_id!r} task uses skill_layer={layer!r}; "
                f"expected one of {sorted(accepted_layers)}."
            )
        targets = {
            Path(str(path)).name
            for path in [*(task.get("target_files") or []), *(task.get("files_allowed") or [])]
        }
        if suggested_files and not targets.intersection(suggested_files):
            errors.append(
                f"Architecture focus {focus_id!r} task targets {sorted(targets)} but none of "
                f"the relevant files {sorted(suggested_files)}."
            )
        prompt = str(task.get("worker_prompt") or "").lower()
        missing_terms = [term for term in required_terms if term not in prompt]
        if missing_terms:
            errors.append(
                f"Architecture focus {focus_id!r} task prompt is missing execution terms {missing_terms}."
            )
        if focus_id == "national_runtime_v4_state_learning":
            try:
                from output_schema import RuntimeContract

                contract = RuntimeContract.model_validate(task.get("runtime_contract") or {})
            except Exception as exc:
                errors.append(
                    f"Architecture focus {focus_id!r} has invalid runtime_contract: "
                    f"{type(exc).__name__}: {str(exc)[:180]}"
                )
                continue
            state_learning = contract.state_learning
            if state_learning is None:
                errors.append(
                    f"Architecture focus {focus_id!r} requires typed state_learning."
                )
                continue
            declared_checks = {
                str(item) for item in task.get("checks_required") or []
            }
            missing_primary = sorted(
                set(state_learning.primary_checks()).difference(declared_checks)
            )
            if missing_primary:
                errors.append(
                    f"Architecture focus {focus_id!r} primary innovation "
                    f"{state_learning.primary_innovation()!r} requires checks_required "
                    f"{missing_primary}."
                )
            unresolved_innovations = {
                str(item)
                for item in focus.get("source_unresolved_checks") or []
                if str(item) in STATE_LEARNING_INNOVATION_CHECKS
            }
            if (
                unresolved_innovations
                and not unresolved_innovations.intersection(
                    state_learning.primary_checks()
                )
            ):
                errors.append(
                    f"Architecture focus {focus_id!r} primary innovation "
                    f"{state_learning.primary_innovation()!r} does not close a source "
                    f"shadow debt; choose one of {sorted(unresolved_innovations)}."
                )
    return errors


def validate_runtime_contract_implementation(
    plan: dict[str, Any],
    candidate_capabilities: dict[str, Any],
) -> list[str]:
    """Bind Master RuntimeContract declarations to detector evidence."""
    try:
        from output_schema import RuntimeContract
    except Exception as exc:
        raise RuntimeError(
            f"runtime_contract_validator_import_error:{type(exc).__name__}:{str(exc)[:160]}"
        ) from exc

    checks = {
        str(item.get("check_id")): bool(item.get("passed"))
        for item in candidate_capabilities.get("checks") or []
    }
    decision = candidate_capabilities.get("decision_time_evidence") or {}
    precompute = candidate_capabilities.get("precompute_evidence") or {}
    incremental = candidate_capabilities.get("incremental_model_evidence") or {}
    provider = incremental.get("provider") or {}
    risks = candidate_capabilities.get("decision_path_risks") or {}
    artifacts = precompute.get("consumed_artifacts") or []
    contract_items, ledger_errors = _ledger_contracts(plan)
    errors: list[str] = [f"runtime_contract_ledger:{item}" for item in ledger_errors]

    for label, raw_contract in contract_items:
        prefix = f"runtime_contract_{label}"
        try:
            contract = RuntimeContract.model_validate(raw_contract)
        except Exception as exc:
            errors.append(
                f"{prefix}_schema_error:{type(exc).__name__}:{str(exc)[:200]}"
            )
            continue

        state_learning = contract.state_learning
        primary_innovation = (
            state_learning.primary_innovation()
            if state_learning is not None
            else ""
        )
        if state_learning is not None and state_learning.work_primitive is not None:
            from strategy_reference_pack import get_reference_card

            card = get_reference_card(contract.reference_pack_id)
            if card is None:
                # RuntimeContract normally catches this first. Keep the
                # implementation gate fail-closed if a serialized legacy plan
                # bypasses schema validation in a caller.
                errors.append(
                    f"{prefix}: selected work primitive has no valid local strategy reference card"
                )
            else:
                decision_fields = incremental.get("decision_field_locations") or {}
                decision_field_functions = (
                    incremental.get("decision_field_function_locations") or {}
                )
                # v4.3+ capability evidence contains exact request-rooted
                # paths which reach an action sink.  A dead tuple of strings
                # such as ('street', 'spr', 'confidence') must never satisfy a
                # reference card simply because it shares field names with the
                # live schema.  Retain the former normalized-literal heuristic
                # only for persisted pre-v4.3 capability reports which do not
                # carry this key at all.
                has_source_rooted_paths = (
                    "source_rooted_live_access_paths" in incremental
                )
                source_rooted_paths = (
                    incremental.get("source_rooted_live_access_paths")
                    if has_source_rooted_paths
                    else None
                )
                if has_source_rooted_paths:
                    if not isinstance(source_rooted_paths, dict):
                        source_rooted_paths = {}
                    missing_hand_fields = [
                        field
                        for field in card.required_hand_runtime_fields
                        if not source_rooted_paths.get(field)
                    ]
                    if missing_hand_fields:
                        errors.append(
                            f"{prefix}: reference card {card.reference_id} lacks source-rooted "
                            f"live hand_runtime action consumption for {missing_hand_fields}"
                        )
                    if not any(
                        source_rooted_paths.get(path)
                        for path in card.required_any_opponent_runtime_fields
                    ):
                        errors.append(
                            f"{prefix}: reference card {card.reference_id} lacks a source-rooted "
                            "confidence-scaled terminal/showdown opponent_runtime action consumer"
                        )
                else:
                    missing_hand_fields = [
                        field.rsplit(".", 1)[-1]
                        for field in card.required_hand_runtime_fields
                        if not decision_fields.get(field.rsplit(".", 1)[-1])
                    ]
                    if missing_hand_fields:
                        errors.append(
                            f"{prefix}: reference card {card.reference_id} lacks live "
                            f"hand_runtime decision consumption for {missing_hand_fields}"
                        )

                    def _opponent_path_consumed(path: str) -> bool:
                        parts = path.split(".")[1:]
                        # A disconnected mention of ``confidence`` elsewhere in
                        # strategy code is not evidence that the terminal or
                        # showdown posterior controls this decision.  Require all
                        # path components to meet in at least one decision
                        # location, which keeps the legacy fallback meaningful.
                        normalized_locations = decision_field_functions or decision_fields
                        locations = [
                            {
                                str(item)
                                for item in (normalized_locations.get(part) or [])
                            }
                            for part in parts
                        ]
                        return bool(parts) and bool(locations) and bool(
                            set.intersection(*locations)
                        )

                    if not any(
                        _opponent_path_consumed(path)
                        for path in card.required_any_opponent_runtime_fields
                    ):
                        errors.append(
                            f"{prefix}: reference card {card.reference_id} lacks a required "
                            "confidence-scaled terminal/showdown opponent_runtime consumer"
                        )

        if contract.decision is not None:
            if not checks.get("decision_time_budget_visible"):
                errors.append(
                    f"{prefix}: decision contract has no proven deadline/baseline/fallback path"
                )
            declared_actual = (
                (
                    "hard_deadline_ms",
                    contract.decision.hard_deadline_ms,
                    "default_hard_deadline_ms",
                    "DEFAULT_DECISION_HARD_DEADLINE_SEC",
                ),
                (
                    "baseline_target_ms",
                    contract.decision.baseline_target_ms,
                    "default_baseline_target_ms",
                    "DEFAULT_DECISION_BASELINE_TARGET_SEC",
                ),
                (
                    "refinement_budget_ms",
                    contract.decision.refinement_budget_ms,
                    "default_refinement_budget_ms",
                    "DEFAULT_DECISION_REFINEMENT_BUDGET_SEC",
                ),
            )
            for field_name, declared_value, evidence_key, constant_name in declared_actual:
                actual_value = decision.get(evidence_key)
                if actual_value is None:
                    errors.append(
                        f"{prefix}: {constant_name} is not statically provable"
                    )
                elif int(actual_value) != int(declared_value):
                    errors.append(
                        f"{prefix}: declared {field_name}={declared_value} "
                        f"but implementation default is {actual_value}"
                    )
            strategy_external_io = [
                location
                for location in risks.get("external_io") or []
                if not str(location).startswith("national_bot.py:")
            ]
            if strategy_external_io:
                errors.append(
                    f"{prefix}: decision path performs external I/O at "
                    f"{strategy_external_io[0]}"
                )
            if primary_innovation == "sample_counted_candidate_batch":
                for check_id in (
                    "fast_strategy_baseline",
                    "incremental_refinement_protocol",
                    "budget_scaled_refinement",
                ):
                    if not checks.get(check_id):
                        errors.append(
                            f"{prefix}: selected sample-counted work primitive lacks "
                            f"proven {check_id}"
                        )

        for declared in contract.precompute_artifacts:
            matching = [
                item
                for item in artifacts
                if item.get("name") == declared.name
                and str(item.get("location") or "").startswith(f"{declared.owner_file}:")
            ]
            if not matching:
                errors.append(
                    f"{prefix}: precompute artifact "
                    f"{declared.owner_file}:{declared.name} is not proven built-before-decision and consumed"
                )
                continue
            detected_bound = max(int(item.get("bound_entries", 0) or 0) for item in matching)
            if detected_bound > declared.max_entries:
                errors.append(
                    f"{prefix}: precompute artifact {declared.name} "
                    f"uses {detected_bound} entries above declared max_entries={declared.max_entries}"
                )
            detected_phases = {str(item.get("build_phase") or "") for item in matching}
            if declared.build_phase not in detected_phases:
                errors.append(
                    f"{prefix}: precompute artifact {declared.name} declares "
                    f"build_phase={declared.build_phase} but detector found {sorted(detected_phases)}"
                )
            consumer_module, declared_consumer = declared.consumer.rsplit(".", 1)
            consumer_token = f"{consumer_module}.py:{declared_consumer}"
            consumer_locations = [
                str(location)
                for item in matching
                for location in item.get("consumer_locations") or []
            ]
            if not any(
                consumer_token in location.split("->")
                for location in consumer_locations
            ):
                errors.append(
                    f"{prefix}: precompute artifact {declared.name} has no proven declared "
                    f"consumer {declared.consumer}"
                )
            try:
                from national_runtime_probe import validate_dynamic_precompute_contract

                dynamic_errors = validate_dynamic_precompute_contract(
                    candidate_capabilities.get("dynamic_runtime_probe") or {},
                    name=declared.name,
                    owner_file=declared.owner_file,
                    build_phase=declared.build_phase,
                    max_build_ms=declared.max_build_ms,
                    max_entries=declared.max_entries,
                    max_bytes=declared.max_bytes,
                    key_shape=declared.key_shape,
                    fallback=declared.fallback,
                    require_action_influence=(
                        primary_innovation == "bounded_precompute_lookup"
                    ),
                    require_key_variation=(
                        primary_innovation == "bounded_precompute_lookup"
                    ),
                )
            except Exception as exc:
                dynamic_errors = [
                    f"dynamic_precompute_validator_error:{type(exc).__name__}:{str(exc)[:160]}"
                ]
            for error in dynamic_errors:
                errors.append(f"{prefix}: precompute artifact {declared.name}: {error}")
        if contract.precompute_artifacts and risks.get("large_runtime_tables"):
            errors.append(
                f"{prefix}: live decision path still constructs a large table at "
                f"{risks['large_runtime_tables'][0]}"
            )

        memory = contract.match_memory
        if memory is not None:
            if not incremental.get("provider_complete") or not incremental.get("consumed_by_decision"):
                errors.append(
                    f"{prefix}: match-memory provider/consumer evidence is incomplete"
                )
            consumer_module, consumer_function = memory.consumer.rsplit(".", 1)
            consumer_token = f"{consumer_module}.py:{consumer_function}"
            consumer_locations = [
                str(location) for location in incremental.get("consumer_locations") or []
            ]
            if not any(
                consumer_token in location.split("->")
                for location in consumer_locations
            ):
                errors.append(
                    f"{prefix}: match-memory contract has no proven declared consumer "
                    f"{memory.consumer}"
                )
            if memory.tracker_class != "OpponentTracker" or memory.owner_file != "national_bot.py":
                errors.append(
                    f"{prefix}: detector found OpponentTracker in national_bot.py, "
                    f"not {memory.owner_file}:{memory.tracker_class}"
                )
            actual_recent = provider.get("recent_state_maxlen")
            if actual_recent is None or int(actual_recent) > int(memory.max_recent_hands):
                errors.append(
                    f"{prefix}: recent-hand bound {actual_recent!r} exceeds "
                    f"declared max_recent_hands={memory.max_recent_hands}"
                )
            actual_cap = provider.get("adaptation_cap")
            if actual_cap is None or float(actual_cap) > float(memory.adaptation_cap) + 1e-12:
                errors.append(
                    f"{prefix}: adaptation cap {actual_cap!r} exceeds "
                    f"declared adaptation_cap={memory.adaptation_cap}"
                )
            event_evidence = {
                "hand_start": "hand_lifecycle",
                "opponent_action": "action_updates",
                "hero_action": "action_updates",
                "settlement": "settlement_updates",
                "showdown": "showdown_updates",
                "street_start": "street_lifecycle",
            }
            missing_events = [
                event
                for event in memory.update_events
                if not provider.get(event_evidence[event])
            ]
            if missing_events:
                errors.append(
                    f"{prefix}: update events lack mutation/call evidence {missing_events}"
                )
            if risks.get("history_scans"):
                errors.append(
                    f"{prefix}: decision path rescans full match history at "
                    f"{risks['history_scans'][0]}"
                )
            dynamic_tracker = incremental.get("dynamic_tracker") or {}
            if not dynamic_tracker.get("ok"):
                errors.append(
                    f"{prefix}: dynamic OpponentTracker probe failed: "
                    f"{(dynamic_tracker.get('issues') or ['unknown'])[:4]}"
                )
            for check_id in (
                "terminal_response_memory",
                "showdown_range_posterior",
                "authoritative_hand_context",
            ):
                if not checks.get(check_id):
                    errors.append(
                        f"{prefix}: match-memory contract lacks proven {check_id}"
                    )

        if state_learning is None:
            continue
        if primary_innovation == "sample_counted_candidate_batch":
            if contract.decision is None:
                errors.append(
                    f"{prefix}: selected sample_counted_candidate_batch has no decision contract"
                )
            else:
                decision_runtime = (
                    candidate_capabilities.get("decision_runtime_evidence") or {}
                )
                long_tier = (
                    (decision_runtime.get("budget_scaling") or {}).get("long") or {}
                )
                trusted_steps = long_tier.get("trusted_steps")
                if (
                    not isinstance(trusted_steps, int)
                    or isinstance(trusted_steps, bool)
                    or trusted_steps < 8
                ):
                    errors.append(
                        f"{prefix}: selected sample-counted work primitive has "
                        f"long trusted_steps={trusted_steps!r}; expected at least 8 "
                        "system-observed iterator steps"
                    )
                elif (
                    contract.decision.max_samples is not None
                    and trusted_steps > contract.decision.max_samples
                ):
                    errors.append(
                        f"{prefix}: long trusted_steps={trusted_steps} exceeds declared "
                        f"decision.max_samples={contract.decision.max_samples}"
                    )
        elif primary_innovation == "bounded_precompute_lookup":
            if not contract.precompute_artifacts:
                errors.append(
                    f"{prefix}: selected bounded_precompute_lookup has no declared artifact"
                )
            if not checks.get("precompute_lookup_path"):
                errors.append(
                    f"{prefix}: selected bounded_precompute_lookup is not proven consumed"
                )
            if not checks.get("precompute_runtime_influence"):
                errors.append(
                    f"{prefix}: selected bounded_precompute_lookup has no proven "
                    "value-sensitive final-wire counterfactual"
                )
        elif primary_innovation in {
            "action_profile",
            "terminal_response",
            "showdown_range",
        }:
            check_id = {
                "action_profile": "incremental_opponent_model",
                "terminal_response": "terminal_response_adaptation",
                "showdown_range": "showdown_range_adaptation",
            }[primary_innovation]
            if not checks.get(check_id):
                errors.append(
                    f"{prefix}: selected profile dimension {primary_innovation!r} "
                    f"lacks proven {check_id}"
                )
        elif primary_innovation in {"donk", "delayed_probe"}:
            decision_fields = incremental.get("decision_field_locations") or {}
            required_field = (
                "can_donk" if primary_innovation == "donk" else "can_delayed_probe"
            )
            if not decision_fields.get("hand_runtime") or not decision_fields.get(required_field):
                errors.append(
                    f"{prefix}: selected line control {primary_innovation!r} is not "
                    f"statically consumed from hand_runtime.{required_field}"
                )
            dynamic_probe = candidate_capabilities.get("dynamic_runtime_probe") or {}
            strategy_influence = dynamic_probe.get("strategy_influence") or {}
            dimensions = strategy_influence.get("dimensions") or {}
            rows = (dimensions.get("semantic_lines") or {}).get("rows") or []
            matching_rows = [
                row for row in rows
                if isinstance(row, dict) and row.get("dimension") == primary_innovation
            ]
            dynamic_ok = False
            for row in matching_rows:
                tiers = row.get("tiers") or {}
                if isinstance(tiers, dict) and any(
                    isinstance(tier, dict) and tier.get("changed") is True
                    for tier in tiers.values()
                ):
                    dynamic_ok = True
                    break
                # Backward compatibility for a persisted v4 probe payload from
                # before baseline/short/long tier evidence was introduced.
                positive = row.get("positive")
                negative = row.get("negative")
                if (
                    not tiers
                    and isinstance(positive, dict)
                    and isinstance(negative, dict)
                    and "error" not in positive
                    and "error" not in negative
                    and positive.get("wire") != negative.get("wire")
                ):
                    dynamic_ok = True
                    break
            if not dynamic_ok:
                errors.append(
                    f"{prefix}: selected line control {primary_innovation!r} has no "
                    "positive/control sanitized-action difference"
                )
    return errors


def architecture_policy_prompt(policy: dict[str, Any]) -> str:
    focus = policy.get("selected_focus") or None
    lines = [
        "System-owned national runtime architecture policy:",
        f"- policy_version={policy.get('policy_version')}",
        f"- official_policy_id={policy.get('official_policy_id')}",
        "- official_oracle_digests="
        + ", ".join(
            f"{path}:{digest}"
            for path, digest in sorted(
                (policy.get("official_oracle_digests") or {}).items()
            )
        ),
        f"- source_capability_digest={policy.get('source_capability_digest')}",
        f"- effective_baseline_bot={policy.get('effective_baseline_bot')}",
        "- prepared_capability_snapshot_digest="
        f"{policy.get('prepared_capability_snapshot_digest') or 'none'}",
        "- preserve every check listed in baseline_passed_checks; candidate regressions are blocking",
        "- every check in plan_required_floor_checks must appear in at least one task checks_required and be closed in this generation",
        "- state_learning declares exactly one primary strategy innovation; only its mapped consumer check is newly blocking, while other strategy dimensions stay shadow/advisory",
        f"- native_template_provided_checks={', '.join(policy.get('native_template_provided_checks') or []) or 'none'}",
        f"- plan_required_floor_checks={', '.join(policy.get('plan_required_floor_checks') or []) or 'none'}",
    ]
    if not focus:
        lines.append("- selected_focus=none; all architecture debt bundles currently pass")
        return "\n".join(lines)
    lines.extend([
        f"- selected_focus={focus.get('focus_id')}: {focus.get('title')}",
        f"- required_checks={', '.join(focus.get('required_checks') or [])}",
        f"- innovation_shadow_checks={', '.join(focus.get('innovation_checks') or [])}",
        f"- accepted_skill_layers={', '.join(focus.get('accepted_skill_layers') or [])}",
        f"- suggested_files={', '.join(focus.get('suggested_files') or [])}",
        f"- required_worker_prompt_terms={', '.join(focus.get('required_terms') or [])}",
        f"- rationale={focus.get('rationale')}",
        "- exactly one task MUST set architecture_focus_id exactly to selected_focus and declare one typed state_learning primary innovation",
        "- the matching task worker_prompt MUST literally contain every required_worker_prompt_terms value",
        "- a label is not proof: quality gates re-run AST/dynamic evidence for correctness floors, parent regressions, and the one selected primary innovation",
    ])
    return "\n".join(lines)


def crossover_architecture_policy_prompt(policy: dict[str, Any]) -> str:
    """Render the pre-Master architecture contract for crossover preparation.

    Crossover deliberately has no WorkerTask schema, runtime-contract ledger, or
    authority to close the generation's selected innovation focus.  Keeping its
    prompt vocabulary separate prevents a weaker model from interpreting Master
    requirements as a request to rewrite the entire strategy during recombination.
    """

    focus = policy.get("selected_focus") or None
    lines = [
        "System-owned crossover baseline architecture policy:",
        f"- policy_version={policy.get('policy_version')}",
        f"- official_policy_id={policy.get('official_policy_id')}",
        f"- source_capability_digest={policy.get('source_capability_digest')}",
        "- this stage prepares a recombination baseline; it is NOT Master or Worker execution",
        "- preserve every check in baseline_passed_checks; any regression is blocking",
        "- preserve the installed system-owned national_bot.py and precompute.py providers",
        "- do not emit or simulate downstream planning objects at this stage",
        "- every strategic diff must be a traceable Parent B component; crossover makes no independent strategic innovation",
        "- direction audit, literature probe, Master, and Workers run after this baseline; Master/Workers own the generation's exactly-one innovation",
        "- native_template_provided_checks must pass before the baseline is accepted: "
        + (", ".join(policy.get("native_template_provided_checks") or []) or "none"),
        "- plan_required_floor_checks are deliberately deferred to Master/Workers: "
        + (", ".join(policy.get("plan_required_floor_checks") or []) or "none"),
    ]
    if focus:
        lines.extend([
            f"- downstream_selected_focus={focus.get('focus_id')}: {focus.get('title')}",
            "- the downstream focus is context only; do not attempt to close it during crossover",
        ])
    else:
        lines.append("- downstream_selected_focus=none")
    lines.append(
        "- final quality gates will re-run the full architecture transition after Workers"
    )
    return "\n".join(lines)
