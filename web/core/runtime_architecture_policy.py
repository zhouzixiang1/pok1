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


RUNTIME_ARCHITECTURE_POLICY_VERSION = "2.0.0"
RUNTIME_ARCHITECTURE_POLICY_SCHEMA_VERSION = 4
RUNTIME_CONTRACT_LEDGER_SCHEMA_VERSION = 1
RUNTIME_FLOOR_CHECKS: tuple[str, ...] = (
    "decision_time_budget_visible",
    "killable_decision_runtime",
    "fast_strategy_baseline",
    "incremental_refinement_protocol",
    "decision_path_no_external_io",
    "decision_path_no_full_history_scan",
    "decision_path_no_large_runtime_tables",
    "precompute_lookup_path",
    "persistent_match_memory",
    "incremental_opponent_model",
)
NATIVE_TEMPLATE_PROVIDED_CHECKS: tuple[str, ...] = (
    "decision_time_budget_visible",
    "killable_decision_runtime",
    "persistent_match_memory",
)

_FOCUS_SPECS: tuple[dict[str, Any], ...] = (
    {
        "focus_id": "national_runtime_v3_migration",
        "title": "Complete national runtime v3 migration",
        "required_checks": [
            "killable_decision_runtime",
            "fast_strategy_baseline",
            "incremental_refinement_protocol",
            "decision_path_no_full_history_scan",
            "decision_path_no_large_runtime_tables",
            "precompute_lookup_path",
            "persistent_match_memory",
            "incremental_opponent_model",
        ],
        "accepted_skill_layers": [
            "runtime_architecture",
            "precompute",
            "match_memory",
            "opponent_model",
        ],
        "suggested_files": [
            "strategy.py",
            "simulation.py",
            "precompute.py",
            "opponent.py",
        ],
        "required_terms": [
            "get_baseline_action",
            "iter_refinements",
            "precompute",
            "opponent_runtime",
        ],
        "rationale": (
            "Migrate the strategy as one executable package: a sub-250ms lookup baseline, "
            "incremental deadline-bound candidates, reusable pure facts, and bounded 70-hand "
            "opponent evidence. Partial labels do not satisfy the runtime contract."
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
        "required_checks": ["precompute_lookup_path"],
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


def _policy_contract_payload(policy: dict[str, Any]) -> dict[str, Any]:
    """Return the complete immutable contract represented by a policy."""
    return {
        "schema_version": policy.get("schema_version"),
        "policy_version": policy.get("policy_version"),
        "detector_version": policy.get("detector_version"),
        "source_bot": policy.get("source_bot"),
        "source_capability_digest": policy.get("source_capability_digest"),
        "source_checks": policy.get("source_checks") or {},
        "baseline_passed_checks": policy.get("baseline_passed_checks") or [],
        "runtime_floor_checks": policy.get("runtime_floor_checks") or [],
        "source_floor_failures": policy.get("source_floor_failures") or [],
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


def select_architecture_focus(capabilities: dict[str, Any]) -> dict[str, Any] | None:
    state = _check_state(capabilities)
    for spec in _FOCUS_SPECS:
        unresolved = [check_id for check_id in spec["required_checks"] if not state.get(check_id, False)]
        if unresolved:
            selected = deepcopy(spec)
            selected["source_unresolved_checks"] = unresolved
            return selected
    return None


def build_architecture_policy(
    source_bot_dir: str | Path,
    *,
    source_capabilities: dict[str, Any] | None = None,
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
    focus = select_architecture_focus(capabilities)
    source_floor_failures = [
        check_id for check_id in RUNTIME_FLOOR_CHECKS if not state.get(check_id, False)
    ]
    policy = {
        "schema_version": RUNTIME_ARCHITECTURE_POLICY_SCHEMA_VERSION,
        "policy_version": RUNTIME_ARCHITECTURE_POLICY_VERSION,
        "detector_version": capabilities.get("detector_version"),
        "source_bot": source_bot_dir.name,
        "source_capability_digest": _state_digest(state),
        "source_checks": state,
        "baseline_passed_checks": sorted(check_id for check_id, passed in state.items() if passed),
        "runtime_floor_checks": list(RUNTIME_FLOOR_CHECKS),
        "source_floor_failures": source_floor_failures,
        "native_template_provided_checks": list(NATIVE_TEMPLATE_PROVIDED_CHECKS),
        "plan_required_floor_checks": [
            check_id
            for check_id in source_floor_failures
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
        "detector_version",
        "source_bot",
        "source_capability_digest",
    ):
        if expected_policy.get(key) != current_policy.get(key):
            errors.append(
                f"architecture_policy_{key}_mismatch: expected={expected_policy.get(key)!r} "
                f"current={current_policy.get(key)!r}"
            )
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
) -> dict[str, Any]:
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
            "policy": expected_policy if isinstance(expected_policy, dict) else None,
            "policy_identity_errors": [],
            "infrastructure_failures": infrastructure_failures,
            # Compatibility alias for existing quality telemetry. This is not a
            # candidate blocker and must never be routed into repair workers.
            "runtime_probe_infra": infrastructure_failures,
            "candidate_failures": [],
            "regressions": [],
            "runtime_floor_checks": list(RUNTIME_FLOOR_CHECKS),
            "runtime_floor_failures": [],
            "selected_focus": None,
            "unresolved_focus_checks": [],
            "source_capabilities": source_capabilities,
            "candidate_capabilities": candidate_capabilities,
        }
    current_policy = build_architecture_policy(
        source_bot_dir,
        source_capabilities=source_capabilities,
    )
    identity_errors = (
        _policy_identity_errors(expected_policy, current_policy)
        if isinstance(expected_policy, dict)
        else []
    )
    policy = expected_policy if isinstance(expected_policy, dict) and not identity_errors else current_policy
    source_state = _check_state(source_capabilities)
    candidate_state = _check_state(candidate_capabilities)
    regressions = [
        {
            "check_id": check_id,
            "source_passed": True,
            "candidate_passed": False,
            "guidance": (candidate_capabilities.get("checks_by_id") or {}).get(check_id, {}).get("guidance", ""),
        }
        for check_id, passed in sorted(source_state.items())
        if passed and not candidate_state.get(check_id, False)
    ]
    focus = policy.get("selected_focus") or None
    unresolved_focus = []
    if focus:
        unresolved_focus = [
            check_id
            for check_id in focus.get("required_checks") or []
            if not candidate_state.get(check_id, False)
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
        if not candidate_state.get(check_id, False)
    ]
    return {
        "schema_version": 1,
        "policy_version": RUNTIME_ARCHITECTURE_POLICY_VERSION,
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
        "runtime_floor_checks": list(RUNTIME_FLOOR_CHECKS),
        "runtime_floor_failures": floor_failures,
        "selected_focus": focus,
        "unresolved_focus_checks": unresolved_focus,
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
            if risks.get("external_io"):
                errors.append(
                    f"{prefix}: decision path performs external I/O at "
                    f"{risks['external_io'][0]}"
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
            dynamic_influence = incremental.get("dynamic_strategy_influence") or {}
            if not dynamic_influence.get("ok"):
                errors.append(
                    f"{prefix}: opponent_runtime has no proven bounded action influence: "
                    f"{(dynamic_influence.get('issues') or ['unknown'])[:4]}"
                )
    return errors


def architecture_policy_prompt(policy: dict[str, Any]) -> str:
    focus = policy.get("selected_focus") or None
    lines = [
        "System-owned national runtime architecture policy:",
        f"- policy_version={policy.get('policy_version')}",
        f"- source_capability_digest={policy.get('source_capability_digest')}",
        "- preserve every check listed in baseline_passed_checks; candidate regressions are blocking",
        "- every check in plan_required_floor_checks must appear in at least one task checks_required and be closed in this generation",
        f"- native_template_provided_checks={', '.join(policy.get('native_template_provided_checks') or []) or 'none'}",
        f"- plan_required_floor_checks={', '.join(policy.get('plan_required_floor_checks') or []) or 'none'}",
    ]
    if not focus:
        lines.append("- selected_focus=none; all architecture debt bundles currently pass")
        return "\n".join(lines)
    lines.extend([
        f"- selected_focus={focus.get('focus_id')}: {focus.get('title')}",
        f"- required_checks={', '.join(focus.get('required_checks') or [])}",
        f"- accepted_skill_layers={', '.join(focus.get('accepted_skill_layers') or [])}",
        f"- suggested_files={', '.join(focus.get('suggested_files') or [])}",
        f"- required_worker_prompt_terms={', '.join(focus.get('required_terms') or [])}",
        f"- rationale={focus.get('rationale')}",
        "- one task MUST set architecture_focus_id exactly to selected_focus and implement the complete behavior",
        "- the matching task worker_prompt MUST literally contain every required_worker_prompt_terms value",
        "- a label is not proof: quality gates re-run AST evidence and block unless every required check passes",
    ])
    return "\n".join(lines)
