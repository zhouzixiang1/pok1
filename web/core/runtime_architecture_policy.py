"""Architecture policy for the ``national_tcp_policy_v1`` epoch.

The policy treats raw TCP and state reconstruction as a system capability and
poker decisions as a typed candidate policy capability.  Historical bot code
is lineage only: it is never a baseline capability and is never opened by this
module when the source directory is absent or does not implement the policy ABI.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

from national_capability_contract import (
    ADVISORY_CHECKS,
    CAPABILITY_SCHEMA_VERSION,
    NATIONAL_CAPABILITY_DETECTOR_VERSION,
    REQUIRED_CHECKS,
    evaluate_national_capabilities,
)
from output_schema import (
    NATIONAL_POLICY_FOCUS_ID,
    POLICY_CONTEXT_SCHEMA_VERSION,
    POLICY_CONTEXT_TOP_LEVEL_FIELDS,
    POLICY_ENTRYPOINTS,
    POLICY_INTENT_KINDS,
    STATE_LEARNING_ORACLE_REFS,
)


ACTIVE_EPOCH = "national_tcp_policy_v1"
RUNTIME_ARCHITECTURE_POLICY_VERSION = "5.1.0"
RUNTIME_ARCHITECTURE_POLICY_SCHEMA_VERSION = 14
RUNTIME_CONTRACT_LEDGER_SCHEMA_VERSION = 3
PREPARED_CAPABILITY_SNAPSHOT_SCHEMA_VERSION = 3
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

RUNTIME_CORRECTNESS_FLOOR_CHECKS: tuple[str, ...] = tuple(REQUIRED_CHECKS)
RUNTIME_FLOOR_CHECKS = RUNTIME_CORRECTNESS_FLOOR_CHECKS
STATE_LEARNING_INNOVATION_CHECKS: tuple[str, ...] = tuple(ADVISORY_CHECKS)
NATIVE_TEMPLATE_PROVIDED_CHECKS: tuple[str, ...] = (
    "system_runtime_current",
    "socket_owner_action_mapping",
    "raw_tcp_stream_decoder",
    "exact_raise_to_boundary",
    "decision_time_budget_visible",
    "killable_decision_runtime",
    "persistent_match_memory",
    "terminal_response_memory",
    "showdown_range_posterior",
    "authoritative_hand_context",
)

_FOCUS_SPECS: tuple[dict[str, Any], ...] = (
    {
        "focus_id": NATIONAL_POLICY_FOCUS_ID,
        "title": "Typed policy state learning",
        "selection_checks": ["incremental_opponent_model"],
        "required_checks": ["incremental_opponent_model"],
        "innovation_checks": list(STATE_LEARNING_INNOVATION_CHECKS),
        "accepted_skill_layers": ["opponent_model", "match_memory", "runtime_architecture"],
        "suggested_files": ["policy.py"],
        "required_terms": ["decision_context", "typed intent", "opponent", "confidence"],
        "rationale": (
            "Use the system tracker snapshot directly from decision_context.opponent "
            "and prove one reachable typed-intent counterfactual."
        ),
    },
    {
        "focus_id": "deadline_refinement",
        "title": "Deadline-bounded refinement",
        "selection_checks": ["incremental_refinement_protocol", "budget_scaled_refinement"],
        "required_checks": ["incremental_refinement_protocol", "budget_scaled_refinement"],
        "innovation_checks": ["incremental_refinement_protocol", "budget_scaled_refinement"],
        "accepted_skill_layers": ["runtime_architecture"],
        "suggested_files": ["policy.py"],
        "required_terms": ["baseline", "deadline", "fallback", "typed intent"],
        "rationale": "Return a fast baseline, then yield bounded improvements before the system deadline.",
    },
)


def architecture_focus_specs() -> list[dict[str, Any]]:
    return deepcopy(list(_FOCUS_SPECS))


def _canonical_json_digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _verified_official_oracle_identity() -> dict[str, str]:
    project_root = Path(__file__).resolve().parents[2]
    observed: dict[str, str] = {}
    for relative, expected in OFFICIAL_ORACLE_DOC_DIGESTS.items():
        path = project_root / relative
        if not path.is_file():
            raise RuntimeError(f"official oracle document missing: {relative}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError(
                f"official oracle digest mismatch:{relative}:expected={expected}:actual={actual}"
            )
        observed[relative] = actual
    return observed


def _strategy_reference_pack_digest() -> str:
    from strategy_reference_pack import reference_pack_registry_digest

    return reference_pack_registry_digest()


def native_policy_runtime_contract() -> dict[str, Any]:
    """Return the exact closed ABI floor used by deterministic planning paths."""

    return {
        "policy_abi": {
            "module": "policy.py",
            "context_schema_version": POLICY_CONTEXT_SCHEMA_VERSION,
            "context_fields": list(POLICY_CONTEXT_TOP_LEVEL_FIELDS),
            "entrypoints": list(POLICY_ENTRYPOINTS),
            "intent_kinds": list(POLICY_INTENT_KINDS),
            "raise_field": "raise_to",
            "pass_mapping": "socket_owner_call_or_check",
        },
        "decision": {
            "clock": "time.monotonic",
            "hard_deadline_ms": 55_000,
            "baseline_target_ms": 250,
            "refinement_budget_ms": 54_000,
            "baseline_path": "policy.get_baseline_decision(context) returns a typed intent",
            "fallback_action": "socket owner maps a legal pass/fold fallback",
            "refinement_bound": "iter_decisions stops before context deadline and a finite cap",
            "max_samples": 4_096,
        },
        "precompute_artifacts": [],
        "match_memory": {
            "tracker_class": "OpponentTracker",
            "owner_file": "national_bot.py",
            "reset_boundary": "tcp_connection",
            "update_events": [
                "hand_start",
                "street_start",
                "opponent_action",
                "settlement",
                "showdown",
            ],
            "snapshot_field": "opponent",
            "max_recent_hands": 8,
            "prior_rule": "beta_prior_weight_8",
            "confidence_rule": (
                "global_actions_over_actions_plus_24_and_context_samples_over_samples_plus_8"
            ),
            "adaptation_cap": 0.65,
            "consumer": "policy.get_baseline_decision",
        },
        "state_learning": None,
        "reference_pack_id": "",
        "official_feedback_refs": [],
        "forbidden_runtime_work": [
            "protocol state outside decision_context",
            "file, network, or subprocess I/O inside candidate policy",
            "unbounded combinatorial construction per decision",
        ],
    }


def _runtime_contract_entry(task: dict[str, Any], index: int) -> dict[str, Any] | None:
    raw = task.get("runtime_contract")
    if not isinstance(raw, dict):
        return None
    from output_schema import RuntimeContract

    contract = RuntimeContract.model_validate(raw).model_dump(mode="json")
    identity = {
        "worker_id": str(task.get("worker_id", index)),
        "skill_layer": str(task.get("skill_layer") or ""),
        "architecture_focus_id": str(task.get("architecture_focus_id") or ""),
        "runtime_contract": contract,
    }
    return {**identity, "contract_digest": _canonical_json_digest(identity)}


def _ledger_payload(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": RUNTIME_CONTRACT_LEDGER_SCHEMA_VERSION,
        "epoch": ACTIVE_EPOCH,
        "entries": entries,
    }


def build_runtime_contract_ledger(
    plan: dict[str, Any],
    *,
    inherited_ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    if inherited_ledger is not None:
        errors = validate_runtime_contract_ledger(inherited_ledger)
        if errors:
            raise ValueError("invalid inherited runtime contract ledger: " + "; ".join(errors))
        for item in inherited_ledger.get("entries") or []:
            digest = str(item.get("contract_digest") or "")
            if digest and digest not in seen:
                entries.append(deepcopy(item))
                seen.add(digest)
    for index, task in enumerate(plan.get("tasks") or [], start=1):
        if not isinstance(task, dict):
            continue
        entry = _runtime_contract_entry(task, index)
        if entry is not None and entry["contract_digest"] not in seen:
            entries.append(entry)
            seen.add(entry["contract_digest"])
    payload = _ledger_payload(entries)
    return {**payload, "ledger_digest": _canonical_json_digest(payload)}


def validate_runtime_contract_ledger(ledger: dict[str, Any] | None) -> list[str]:
    if not isinstance(ledger, dict):
        return ["runtime_contract_ledger_missing_or_not_object"]
    errors: list[str] = []
    if ledger.get("schema_version") != RUNTIME_CONTRACT_LEDGER_SCHEMA_VERSION:
        errors.append("runtime_contract_ledger_schema_mismatch")
    if ledger.get("epoch") != ACTIVE_EPOCH:
        errors.append("runtime_contract_ledger_epoch_mismatch")
    raw_entries = ledger.get("entries")
    if not isinstance(raw_entries, list):
        return [*errors, "runtime_contract_ledger_entries_not_list"]
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_entries, start=1):
        if not isinstance(item, dict):
            errors.append(f"runtime_contract_ledger_entry_{index}_not_object")
            continue
        try:
            rebuilt = _runtime_contract_entry(item, index)
        except Exception as exc:
            errors.append(
                f"runtime_contract_ledger_entry_{index}_schema_error:{type(exc).__name__}:{str(exc)[:180]}"
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
        entries.append(rebuilt)
    expected = _ledger_payload(entries)
    if ledger.get("ledger_digest") != _canonical_json_digest(expected):
        errors.append("runtime_contract_ledger_digest_mismatch")
    return list(dict.fromkeys(errors))


def runtime_contract_ledger_digest(value: dict[str, Any] | None) -> str:
    if isinstance(value, dict) and "runtime_contract_ledger" in value:
        value = value.get("runtime_contract_ledger")
    if validate_runtime_contract_ledger(value):
        return ""
    return str((value or {}).get("ledger_digest") or "")


def attach_runtime_contract_ledger(
    plan: dict[str, Any],
    *,
    replace: bool = False,
) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return plan
    inherited = None if replace else plan.get("runtime_contract_ledger")
    if inherited is not None and validate_runtime_contract_ledger(inherited):
        return plan
    return {
        **plan,
        "runtime_contract_ledger": build_runtime_contract_ledger(
            plan,
            inherited_ledger=inherited,
        ),
    }


def _ledger_contracts(plan: dict[str, Any]) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    ledger = plan.get("runtime_contract_ledger") if isinstance(plan, dict) else None
    errors = validate_runtime_contract_ledger(ledger)
    if errors:
        return [], errors
    return [
        (f"ledger_{index}", item["runtime_contract"])
        for index, item in enumerate(ledger.get("entries") or [], start=1)
    ], []


def _check_state(capabilities: dict[str, Any] | None) -> dict[str, bool]:
    if not isinstance(capabilities, dict):
        return {}
    return {
        str(item.get("check_id") or item.get("name")): bool(item.get("passed"))
        for item in capabilities.get("checks") or []
        if isinstance(item, dict) and (item.get("check_id") or item.get("name"))
    }


def _state_digest(state: dict[str, bool]) -> str:
    return _canonical_json_digest({
        "epoch": ACTIVE_EPOCH,
        "detector_version": NATIONAL_CAPABILITY_DETECTOR_VERSION,
        "checks": {key: bool(state[key]) for key in sorted(state)},
    })


def _epoch_compatible(capabilities: dict[str, Any] | None) -> bool:
    return bool(
        isinstance(capabilities, dict)
        and capabilities.get("epoch") == ACTIVE_EPOCH
        and capabilities.get("conclusive") is True
        and (capabilities.get("checks_by_id") or {}).get("national_policy_module", {}).get("passed") is True
    )


def lineage_only_capabilities() -> dict[str, Any]:
    """Return the synthetic empty capability set for numeric-only lineage.

    The first strict generation has a completion-tag high-water identity but no
    source artifact.  This constructor is deliberately path-free: callers must
    not prove that absence by resolving or probing ``bots/national_v142``.
    """

    return {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "detector_version": NATIONAL_CAPABILITY_DETECTOR_VERSION,
        "epoch": ACTIVE_EPOCH,
        "conclusive": True,
        "ok": True,
        "outcome": "lineage_only",
        "checks": [],
        "checks_by_id": {},
        "required_failures": [],
        "advisory_warnings": [],
        "infrastructure_failures": [],
        "lineage_only": True,
    }


def _lineage_bot_identity(value: Any) -> str:
    """Validate a bot label without interpreting it as a filesystem path."""

    text = str(value or "").strip()
    prefix = "national_v"
    suffix = text[len(prefix):] if text.startswith(prefix) else ""
    if not suffix.isdigit() or int(suffix) <= 0 or any(char in text for char in "/\\"):
        raise ValueError("lineage_bot_identity_invalid")
    return text


def _lineage_capabilities(path: Path) -> dict[str, Any]:
    """Return policy capabilities only for active-epoch artifacts.

    Missing and archived/retired sources intentionally become an empty lineage
    state.  No archived source file is opened to establish a baseline.
    """

    if not path.is_dir() or not (path / "policy.py").is_file():
        return lineage_only_capabilities()
    capabilities = evaluate_national_capabilities(path)
    capabilities, _probe, _infrastructure = _apply_typed_runtime_probe(
        capabilities,
        path,
        runtime_contract_ledger=None,
    )
    return capabilities


def _runtime_probe_check(
    *,
    passed: bool,
    probe: dict[str, Any],
) -> dict[str, Any]:
    return {
        "check_id": "typed_runtime_probe",
        "name": "typed_runtime_probe",
        "passed": bool(passed),
        "required": True,
        "skill_layer": "runtime_architecture",
        "guidance": (
            "Keep policy.py on decision_context v1 and typed intents; the "
            "system runtime must reconstruct official transcripts, persist "
            "terminal/showdown memory, and emit delimiter-free legal actions."
        ),
        "evidence": {
            "summary": (
                "managed typed runtime probe passed"
                if passed
                else "managed typed runtime probe failed"
            ),
            "locations": ["policy.py", "national_bot.py"],
            "probe_identity_digest": probe.get("probe_identity_digest"),
            "managed_isolation_digest": probe.get("managed_isolation_digest"),
            "issues": list(probe.get("issues") or [])[:20],
        },
    }


def _causal_wire_counterfactual_passed(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    left_wire = value.get("left_wire")
    right_wire = value.get("right_wire")
    negative_left_wire = value.get("negative_left_wire")
    negative_right_wire = value.get("negative_right_wire")
    positive_wire_effect = bool(
        left_wire
        and right_wire
        and left_wire != right_wire
    )
    negative_control_stable = bool(
        negative_left_wire
        and negative_right_wire
        and negative_left_wire == negative_right_wire
    )
    return bool(
        value.get("causal_passed") is True
        and value.get("positive_wire_effect") is True
        and value.get("negative_control_stable") is True
        and value.get("socket_validated") is True
        and positive_wire_effect
        and negative_control_stable
    )


def _dynamic_probe_states(probe: dict[str, Any]) -> dict[str, bool]:
    rows = [
        row
        for row in probe.get("official_transcript_decisions") or []
        if isinstance(row, dict)
    ]
    counterfactuals = (
        (probe.get("policy_counterfactuals") or {}).get("dimensions") or {}
    )
    lines = (probe.get("line_reachability") or {}).get("dimensions") or {}
    scaling = probe.get("budget_scaled_refinement") or {}
    return {
        "incremental_refinement_protocol": bool(
            (rows and any(
                int((row.get("runtime") or {}).get("refinement_messages") or 0)
                > 0
                for row in rows
            ))
            or int((scaling.get("long") or {}).get("refinement_messages") or 0)
            > 0
        ),
        "budget_scaled_refinement": scaling.get("ok") is True,
        # Importing a system fact table is not policy influence.  The typed
        # probe currently has no digest-bound same-shape/different-value
        # precompute variant, so this claim stays fail-closed when selected.
        "precompute_runtime_influence": False,
        "incremental_opponent_model": _causal_wire_counterfactual_passed(
            counterfactuals.get("action_profile")
        ),
        "terminal_response_adaptation": _causal_wire_counterfactual_passed(
            counterfactuals.get("terminal_response")
        ),
        "showdown_range_adaptation": _causal_wire_counterfactual_passed(
            counterfactuals.get("showdown_range")
        ),
        "donk_line_reachability": bool(
            (lines.get("donk") or {}).get("ok")
            and (lines.get("donk") or {}).get("policy_changed")
            and (lines.get("donk") or {}).get("socket_validated")
        ),
        "delayed_probe_line_reachability": bool(
            (lines.get("delayed_probe") or {}).get("ok")
            and (lines.get("delayed_probe") or {}).get("policy_changed")
            and (lines.get("delayed_probe") or {}).get("socket_validated")
        ),
    }


def _apply_typed_runtime_probe(
    capabilities: dict[str, Any],
    candidate: Path,
    *,
    runtime_contract_ledger: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Bind managed dynamic evidence without weakening the static parser.

    Exact system-runtime drift is already candidate debt and is not executed.
    A probe failure inside exact system bytes is infrastructure only when the
    worker classifies the transcript/runtime side as the owner.
    """

    from national_runtime_probe import (
        RUNTIME_PROBE_IDENTITY_DIGEST,
        RUNTIME_PROBE_LIMITS_DIGEST,
        RUNTIME_PROBE_ORCHESTRATOR_VERSION,
        RUNTIME_PROBE_SCENARIO_DIGEST,
        RUNTIME_PROBE_SCHEMA_VERSION,
        run_national_runtime_probe,
    )

    static_checks = capabilities.get("checks_by_id") or {}
    static_ready = all(
        (static_checks.get(check_id) or {}).get("passed") is True
        for check_id in (
            "national_policy_module",
            "system_runtime_current",
            "policy_baseline_entrypoint",
            "policy_refinement_entrypoint",
        )
    )
    if static_ready:
        try:
            probe = run_national_runtime_probe(candidate)
        except Exception as exc:
            probe = {
                "schema_version": RUNTIME_PROBE_SCHEMA_VERSION,
                "orchestrator_version": RUNTIME_PROBE_ORCHESTRATOR_VERSION,
                "scenario_digest": RUNTIME_PROBE_SCENARIO_DIGEST,
                "limits_digest": RUNTIME_PROBE_LIMITS_DIGEST,
                "probe_identity_digest": RUNTIME_PROBE_IDENTITY_DIGEST,
                "ok": False,
                "failure_class": "probe_infra",
                "issues": [
                    f"typed_runtime_probe_exception:{type(exc).__name__}:"
                    f"{str(exc)[:180]}"
                ],
            }
    else:
        probe = {
            "schema_version": RUNTIME_PROBE_SCHEMA_VERSION,
            "orchestrator_version": RUNTIME_PROBE_ORCHESTRATOR_VERSION,
            "scenario_digest": RUNTIME_PROBE_SCENARIO_DIGEST,
            "limits_digest": RUNTIME_PROBE_LIMITS_DIGEST,
            "probe_identity_digest": RUNTIME_PROBE_IDENTITY_DIGEST,
            "ok": False,
            "failure_class": "candidate_contract",
            "issues": ["typed_runtime_probe_blocked_by_static_contract"],
            "repeatability_ok": False,
            "evidence_integrity_ok": False,
            "managed_isolation_digest": "",
        }
    probe = deepcopy(probe)
    probe["runtime_contract_ledger_digest"] = runtime_contract_ledger_digest(
        runtime_contract_ledger
    )

    merged = deepcopy(capabilities)
    merged["dynamic_runtime_probe"] = probe
    infrastructure_failures: list[dict[str, Any]] = []
    probe_is_infrastructure = probe.get("failure_class") == "probe_infra"
    if probe_is_infrastructure:
        infrastructure_failures.append({
            "side": "system",
            "component": "national_runtime_probe",
            "failure_class": "internal_infrastructure",
            "issues": [str(item) for item in (probe.get("issues") or [])[:20]],
            "probe_identity_digest": probe.get("probe_identity_digest"),
            "managed_isolation_digest": probe.get("managed_isolation_digest"),
        })
        merged.setdefault("infrastructure_failures", []).extend(
            infrastructure_failures
        )
        merged["conclusive"] = False
        merged["ok"] = False
        merged["outcome"] = "infrastructure_failure"
        return merged, probe, infrastructure_failures

    probe_check = _runtime_probe_check(
        passed=probe.get("ok") is True,
        probe=probe,
    )
    checks = [
        item
        for item in merged.get("checks") or []
        if item.get("check_id") != "typed_runtime_probe"
    ]
    checks.append(probe_check)
    dynamic_states = _dynamic_probe_states(probe)
    for item in checks:
        check_id = str(item.get("check_id") or "")
        if check_id not in dynamic_states:
            continue
        item["passed"] = dynamic_states[check_id]
        evidence = dict(item.get("evidence") or {})
        evidence.update({
            "summary": "managed typed-policy counterfactual evidence",
            "probe_identity_digest": probe.get("probe_identity_digest"),
            "managed_isolation_digest": probe.get("managed_isolation_digest"),
            "dynamic_passed": dynamic_states[check_id],
        })
        item["evidence"] = evidence
    merged["checks"] = checks
    merged["checks_by_id"] = {
        str(item.get("check_id")): item for item in checks
    }
    merged["required_checks"] = list(dict.fromkeys([
        *(merged.get("required_checks") or []),
        "typed_runtime_probe",
    ]))
    merged["required_failures"] = [
        item for item in checks
        if item.get("required") and item.get("passed") is not True
    ]
    merged["advisory_warnings"] = [
        item for item in checks
        if not item.get("required") and item.get("passed") is not True
    ]
    merged["passed_checks"] = [
        str(item.get("check_id")) for item in checks
        if item.get("passed") is True
    ]
    merged["conclusive"] = True
    merged["ok"] = not merged["required_failures"]
    merged["outcome"] = "passed" if merged["ok"] else "failed"
    return merged, probe, infrastructure_failures


_SNAPSHOT_KEYS = frozenset({
    "schema_version",
    "epoch",
    "detector_version",
    "parent_bot",
    "prepared_bot",
    "parent_epoch_compatible",
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


def _snapshot_payload(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(value.get(key))
        for key in sorted(_SNAPSHOT_KEYS - {"snapshot_digest"})
    }


def _build_prepared_capability_snapshot(
    *,
    parent_bot: str,
    parent_capabilities: dict[str, Any],
    prepared_bot_dir: str | Path,
    prepared_capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prepared = Path(prepared_bot_dir)
    if prepared_capabilities is None:
        prepared_cap = evaluate_national_capabilities(prepared)
        prepared_cap, _probe, _infrastructure = _apply_typed_runtime_probe(
            prepared_cap,
            prepared,
            runtime_contract_ledger=None,
        )
    else:
        prepared_cap = prepared_capabilities
    if prepared_cap.get("conclusive") is not True:
        raise RuntimeError("prepared capability assessment is inconclusive")
    compatible = _epoch_compatible(parent_capabilities)
    parent_state = _check_state(parent_capabilities) if compatible else {}
    prepared_state = _check_state(prepared_cap)
    payload = {
        "schema_version": PREPARED_CAPABILITY_SNAPSHOT_SCHEMA_VERSION,
        "epoch": ACTIVE_EPOCH,
        "detector_version": NATIONAL_CAPABILITY_DETECTOR_VERSION,
        "parent_bot": parent_bot,
        "prepared_bot": prepared.name,
        "parent_epoch_compatible": compatible,
        "parent_checks": parent_state,
        "prepared_checks": prepared_state,
        "parent_capability_digest": _state_digest(parent_state),
        "prepared_capability_digest": _state_digest(prepared_state),
        "parent_passed_checks": sorted(key for key, passed in parent_state.items() if passed),
        "prepared_passed_checks": sorted(key for key, passed in prepared_state.items() if passed),
        "protected_passed_checks": sorted({
            key for key, passed in {**parent_state, **prepared_state}.items() if passed
        }),
        "acquired_checks": sorted(
            key for key, passed in prepared_state.items()
            if passed and not parent_state.get(key, False)
        ),
    }
    return {**payload, "snapshot_digest": _canonical_json_digest(payload)}


def build_prepared_capability_snapshot(
    parent_bot_dir: str | Path,
    prepared_bot_dir: str | Path,
    *,
    parent_capabilities: dict[str, Any] | None = None,
    prepared_capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind a real active-epoch parent and the prepared candidate."""

    parent = Path(parent_bot_dir)
    return _build_prepared_capability_snapshot(
        parent_bot=parent.name,
        parent_capabilities=(
            parent_capabilities or _lineage_capabilities(parent)
        ),
        prepared_bot_dir=prepared_bot_dir,
        prepared_capabilities=prepared_capabilities,
    )


def build_lineage_only_prepared_capability_snapshot(
    parent_bot: str,
    prepared_bot_dir: str | Path,
    *,
    prepared_capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind numeric lineage to a live prepared bot without a parent path."""

    return _build_prepared_capability_snapshot(
        parent_bot=_lineage_bot_identity(parent_bot),
        parent_capabilities=lineage_only_capabilities(),
        prepared_bot_dir=prepared_bot_dir,
        prepared_capabilities=prepared_capabilities,
    )


def validate_prepared_capability_snapshot(
    snapshot: dict[str, Any] | None,
    *,
    parent_bot_dir: str | Path | None = None,
    lineage_parent_bot: str | None = None,
    prepared_bot_dir: str | Path | None = None,
    parent_capabilities: dict[str, Any] | None = None,
    prepared_capabilities: dict[str, Any] | None = None,
) -> list[str]:
    if not isinstance(snapshot, dict):
        return ["prepared_capability_snapshot_missing_or_not_object"]
    errors: list[str] = []
    if set(snapshot) != _SNAPSHOT_KEYS:
        errors.append("prepared_capability_snapshot_fields_mismatch")
    if snapshot.get("schema_version") != PREPARED_CAPABILITY_SNAPSHOT_SCHEMA_VERSION:
        errors.append("prepared_capability_snapshot_schema_mismatch")
    if snapshot.get("epoch") != ACTIVE_EPOCH:
        errors.append("prepared_capability_snapshot_epoch_mismatch")
    if snapshot.get("detector_version") != NATIONAL_CAPABILITY_DETECTOR_VERSION:
        errors.append("prepared_capability_snapshot_detector_mismatch")
    payload = _snapshot_payload(snapshot)
    if snapshot.get("snapshot_digest") != _canonical_json_digest(payload):
        errors.append("prepared_capability_snapshot_digest_mismatch")
    parent_state = snapshot.get("parent_checks")
    prepared_state = snapshot.get("prepared_checks")
    if not isinstance(parent_state, dict) or not isinstance(prepared_state, dict):
        errors.append("prepared_capability_snapshot_checks_invalid")
        return errors
    if snapshot.get("parent_capability_digest") != _state_digest(parent_state):
        errors.append("prepared_capability_snapshot_parent_digest_mismatch")
    if snapshot.get("prepared_capability_digest") != _state_digest(prepared_state):
        errors.append("prepared_capability_snapshot_prepared_digest_mismatch")
    if parent_bot_dir is not None and lineage_parent_bot is not None:
        errors.append("prepared_capability_snapshot_parent_authority_ambiguous")
    lineage_identity = None
    if lineage_parent_bot is not None:
        try:
            lineage_identity = _lineage_bot_identity(lineage_parent_bot)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            if snapshot.get("parent_bot") != lineage_identity:
                errors.append("prepared_capability_snapshot_lineage_identity_mismatch")
            if snapshot.get("parent_epoch_compatible") is not False:
                errors.append("prepared_capability_snapshot_lineage_must_be_incompatible")
            if parent_state:
                errors.append("prepared_capability_snapshot_lineage_checks_not_empty")
    # Rebuild live prepared state only when the caller supplies its exact
    # directory.  Numeric-only lineage uses the path-free constructor and must
    # never reinterpret the parent label as a path relative to cwd.
    if prepared_bot_dir is not None:
        try:
            if lineage_identity is not None:
                if parent_capabilities is not None:
                    raise ValueError("lineage_parent_capabilities_forbidden")
                rebuilt = build_lineage_only_prepared_capability_snapshot(
                    lineage_identity,
                    prepared_bot_dir,
                    prepared_capabilities=prepared_capabilities,
                )
            else:
                rebuilt = build_prepared_capability_snapshot(
                    parent_bot_dir or snapshot.get("parent_bot") or "",
                    prepared_bot_dir,
                    parent_capabilities=parent_capabilities,
                    prepared_capabilities=prepared_capabilities,
                )
        except Exception as exc:
            errors.append(f"prepared_capability_snapshot_rebuild_error:{type(exc).__name__}")
        else:
            if rebuilt != snapshot:
                errors.append("prepared_capability_snapshot_current_state_mismatch")
    return list(dict.fromkeys(errors))


def prepared_capability_snapshot_digest(snapshot: dict[str, Any] | None) -> str:
    return "" if validate_prepared_capability_snapshot(snapshot) else str(snapshot.get("snapshot_digest") or "")


def _select_architecture_focus_from_state(state: dict[str, bool]) -> dict[str, Any] | None:
    for spec in _FOCUS_SPECS:
        unresolved = [
            check_id
            for check_id in spec.get("selection_checks") or spec["required_checks"]
            if not state.get(check_id, False)
        ]
        if unresolved:
            selected = deepcopy(spec)
            selected["source_unresolved_checks"] = unresolved
            return selected
    return None


def select_architecture_focus(capabilities: dict[str, Any]) -> dict[str, Any] | None:
    return _select_architecture_focus_from_state(_check_state(capabilities))


def _policy_payload(policy: dict[str, Any]) -> dict[str, Any]:
    return {key: deepcopy(value) for key, value in policy.items() if key != "policy_digest"}


def _policy_contract_digest(policy: dict[str, Any]) -> str:
    return _canonical_json_digest(_policy_payload(policy))


def _build_architecture_policy_payload(
    source_bot: str,
    capabilities: dict[str, Any],
    prepared_capability_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    if capabilities.get("conclusive") is not True:
        raise RuntimeError("source capability assessment is inconclusive")
    source_compatible = _epoch_compatible(capabilities)
    source_state = _check_state(capabilities) if source_compatible else {}
    snapshot = None
    effective_state = dict(source_state)
    effective_bot = source_bot
    if prepared_capability_snapshot is not None:
        snapshot = deepcopy(prepared_capability_snapshot)
        effective_state = dict(snapshot["prepared_checks"])
        if snapshot.get("parent_epoch_compatible"):
            effective_state.update({
                key: bool(value or effective_state.get(key, False))
                for key, value in snapshot["parent_checks"].items()
            })
        effective_bot = str(snapshot["prepared_bot"])
    focus = _select_architecture_focus_from_state(effective_state)
    floor_failures = [key for key in RUNTIME_FLOOR_CHECKS if not effective_state.get(key, False)]
    policy = {
        "schema_version": RUNTIME_ARCHITECTURE_POLICY_SCHEMA_VERSION,
        "policy_version": RUNTIME_ARCHITECTURE_POLICY_VERSION,
        "epoch": ACTIVE_EPOCH,
        "official_policy_id": OFFICIAL_FULL_POLICY_ID,
        "official_oracle_digests": _verified_official_oracle_identity(),
        "strategy_reference_pack_digest": _strategy_reference_pack_digest(),
        "detector_version": NATIONAL_CAPABILITY_DETECTOR_VERSION,
        "source_bot": source_bot,
        "source_epoch_compatible": source_compatible,
        "source_capability_digest": _state_digest(source_state),
        "source_checks": source_state,
        "prepared_capability_snapshot": snapshot,
        "prepared_capability_snapshot_digest": snapshot.get("snapshot_digest") if snapshot else None,
        "effective_baseline_bot": effective_bot,
        "effective_baseline_capability_digest": _state_digest(effective_state),
        "effective_baseline_checks": effective_state,
        "baseline_passed_checks": sorted(key for key, passed in effective_state.items() if passed),
        "runtime_floor_checks": list(RUNTIME_FLOOR_CHECKS),
        "strategy_innovation_checks": list(STATE_LEARNING_INNOVATION_CHECKS),
        "source_floor_failures": [key for key in RUNTIME_FLOOR_CHECKS if not source_state.get(key, False)],
        "baseline_floor_failures": floor_failures,
        "native_template_provided_checks": list(NATIVE_TEMPLATE_PROVIDED_CHECKS),
        "plan_required_floor_checks": [
            key for key in floor_failures if key not in NATIVE_TEMPLATE_PROVIDED_CHECKS
        ],
        "selected_focus": focus,
        "policy_abi": native_policy_runtime_contract()["policy_abi"],
    }
    policy["policy_digest"] = _policy_contract_digest(policy)
    return policy


def build_architecture_policy(
    source_bot_dir: str | Path,
    *,
    source_capabilities: dict[str, Any] | None = None,
    prepared_capability_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = Path(source_bot_dir)
    capabilities = source_capabilities or _lineage_capabilities(source)
    if prepared_capability_snapshot is not None:
        snapshot_errors = validate_prepared_capability_snapshot(
            prepared_capability_snapshot,
            parent_bot_dir=source,
            parent_capabilities=capabilities,
        )
        if snapshot_errors:
            raise ValueError(
                "invalid prepared capability snapshot: "
                + "; ".join(snapshot_errors)
            )
    return _build_architecture_policy_payload(
        source.name,
        capabilities,
        prepared_capability_snapshot,
    )


def build_lineage_only_architecture_policy(
    source_bot: str,
    *,
    prepared_capability_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Build the first-strict policy from numeric lineage and prepared facts."""

    source_identity = _lineage_bot_identity(source_bot)
    snapshot_errors = validate_prepared_capability_snapshot(
        prepared_capability_snapshot,
        lineage_parent_bot=source_identity,
    )
    if snapshot_errors:
        raise ValueError(
            "invalid lineage-only prepared capability snapshot: "
            + "; ".join(snapshot_errors)
        )
    return _build_architecture_policy_payload(
        source_identity,
        lineage_only_capabilities(),
        prepared_capability_snapshot,
    )


def _policy_identity_errors(expected: dict[str, Any], current: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in (
        "schema_version",
        "policy_version",
        "epoch",
        "official_policy_id",
        "official_oracle_digests",
        "strategy_reference_pack_digest",
        "detector_version",
        "source_bot",
        "source_epoch_compatible",
        "source_capability_digest",
        "prepared_capability_snapshot_digest",
        "effective_baseline_bot",
        "effective_baseline_capability_digest",
        "policy_abi",
    ):
        if expected.get(key) != current.get(key):
            errors.append(f"architecture_policy_{key}_mismatch")
    for label, policy in (("expected", expected), ("current", current)):
        if policy.get("policy_digest") != _policy_contract_digest(policy):
            errors.append(f"architecture_policy_{label}_content_digest_mismatch")
    if expected.get("policy_digest") != current.get("policy_digest"):
        errors.append("architecture_policy_digest_mismatch")
    return errors


def _selected_dynamic_probe_checks(
    ledger: dict[str, Any] | None,
) -> tuple[set[str], list[str]]:
    if ledger is None:
        return set(), []
    contracts, errors = _ledger_contracts({"runtime_contract_ledger": ledger})
    if errors:
        return set(), errors
    from output_schema import RuntimeContract

    selected: set[str] = set()
    for label, raw in contracts:
        try:
            contract = RuntimeContract.model_validate(raw)
        except Exception as exc:
            errors.append(
                f"{label}:runtime_contract_schema:{type(exc).__name__}:"
                f"{str(exc)[:180]}"
            )
            continue
        if contract.state_learning is not None:
            selected.update(contract.state_learning.primary_checks())
    return selected, list(dict.fromkeys(errors))


def evaluate_architecture_transition(
    source_bot_dir: str | Path | None,
    candidate_bot_dir: str | Path,
    *,
    expected_policy: dict[str, Any] | None = None,
    evaluation_phase: str = ARCHITECTURE_TRANSITION_PHASE_FINAL,
    runtime_contract_ledger: dict[str, Any] | None = None,
    lineage_source_bot: str | None = None,
) -> dict[str, Any]:
    if evaluation_phase not in ARCHITECTURE_TRANSITION_PHASES:
        raise ValueError(f"unknown architecture transition phase: {evaluation_phase}")
    candidate = Path(candidate_bot_dir)
    if lineage_source_bot is not None:
        if source_bot_dir is not None:
            raise ValueError("architecture_transition_source_authority_ambiguous")
        source_identity = _lineage_bot_identity(lineage_source_bot)
        source_cap = lineage_only_capabilities()
    else:
        if source_bot_dir is None:
            raise ValueError("architecture_transition_source_missing")
        source = Path(source_bot_dir)
        source_identity = source.name
        source_cap = _lineage_capabilities(source)
    candidate_cap = evaluate_national_capabilities(candidate)
    candidate_cap, runtime_probe, probe_infrastructure = _apply_typed_runtime_probe(
        candidate_cap,
        candidate,
        runtime_contract_ledger=runtime_contract_ledger,
    )
    infrastructure_failures: list[dict[str, Any]] = []
    if candidate_cap.get("conclusive") is not True:
        infrastructure_failures.extend(candidate_cap.get("infrastructure_failures") or [])

    snapshot = expected_policy.get("prepared_capability_snapshot") if isinstance(expected_policy, dict) else None
    if lineage_source_bot is not None:
        if not isinstance(snapshot, dict):
            raise ValueError(
                "lineage_only_transition_requires_prepared_capability_snapshot"
            )
        current_policy = build_lineage_only_architecture_policy(
            source_identity,
            prepared_capability_snapshot=snapshot,
        )
    else:
        current_policy = build_architecture_policy(
            source,
            source_capabilities=source_cap,
            prepared_capability_snapshot=snapshot,
        )
    policy = expected_policy or current_policy
    policy_identity_errors = (
        _policy_identity_errors(expected_policy, current_policy)
        if isinstance(expected_policy, dict)
        else []
    )
    source_state = dict(policy.get("effective_baseline_checks") or {})
    candidate_state = _check_state(candidate_cap)
    regressions = [
        {
            "check_id": check_id,
            "guidance": (candidate_cap.get("checks_by_id") or {}).get(check_id, {}).get("guidance", "restore baseline capability"),
        }
        for check_id in policy.get("baseline_passed_checks") or []
        if not candidate_state.get(check_id, False)
    ]
    runtime_floor_failures = [
        {
            "check_id": check_id,
            "guidance": (candidate_cap.get("checks_by_id") or {}).get(check_id, {}).get("guidance", "satisfy national policy runtime floor"),
        }
        for check_id in RUNTIME_FLOOR_CHECKS
        if not candidate_state.get(check_id, False)
    ]
    focus = policy.get("selected_focus") or None
    unresolved_focus_checks = [
        check_id
        for check_id in (focus or {}).get("required_checks") or []
        if not candidate_state.get(check_id, False)
    ]
    selected_dynamic_checks, ledger_errors = _selected_dynamic_probe_checks(
        runtime_contract_ledger
    )
    # The architecture policy may advertise the next useful direction, but an
    # advisory counterfactual becomes acceptance-critical only after Master has
    # frozen that exact primary in the digest-bound RuntimeContract ledger.
    blocking_focus = (
        [
            check_id
            for check_id in unresolved_focus_checks
            if check_id in selected_dynamic_checks
        ]
        if evaluation_phase == ARCHITECTURE_TRANSITION_PHASE_FINAL
        else []
    )
    selected_dynamic_failures = [
        check_id
        for check_id in sorted(selected_dynamic_checks)
        if not candidate_state.get(check_id, False)
    ]
    typed_runtime_failures = [
        str(item)
        for item in (
            (candidate_cap.get("checks_by_id") or {})
            .get("typed_runtime_probe", {})
            .get("evidence", {})
            .get("issues", [])
        )
    ] if candidate_state.get("typed_runtime_probe") is False else []
    policy_identity_errors.extend(
        f"runtime_contract_ledger:{item}" for item in ledger_errors
    )
    ok = not any((
        infrastructure_failures,
        policy_identity_errors,
        regressions,
        runtime_floor_failures,
        blocking_focus,
        typed_runtime_failures,
        selected_dynamic_failures,
    ))
    return {
        "schema_version": 2,
        "policy_version": RUNTIME_ARCHITECTURE_POLICY_VERSION,
        "detector_version": NATIONAL_CAPABILITY_DETECTOR_VERSION,
        "epoch": ACTIVE_EPOCH,
        "ok": ok,
        "conclusive": not infrastructure_failures,
        "outcome": "passed" if ok else "infrastructure_failure" if infrastructure_failures else "failed",
        "failure_class": "" if ok else "infrastructure" if infrastructure_failures else "candidate",
        "evaluation_phase": evaluation_phase,
        "source_capabilities": source_cap,
        "candidate_capabilities": candidate_cap,
        "source_checks": source_state,
        "candidate_checks": candidate_state,
        "policy": policy,
        "selected_focus": focus,
        "regressions": regressions,
        "runtime_floor_failures": runtime_floor_failures,
        "unresolved_focus_checks": unresolved_focus_checks,
        "blocking_focus_checks": blocking_focus,
        "policy_identity_errors": policy_identity_errors,
        "infrastructure_failures": infrastructure_failures,
        "runtime_probe_infra": infrastructure_failures,
        "runtime_probe": runtime_probe,
        "runtime_probe_identity_digest": runtime_probe.get(
            "probe_identity_digest"
        ),
        "runtime_probe_managed_isolation_digest": runtime_probe.get(
            "managed_isolation_digest"
        ),
        "runtime_contract_ledger_digest": runtime_probe.get(
            "runtime_contract_ledger_digest"
        ),
        "typed_runtime_failures": typed_runtime_failures,
        "selected_dynamic_checks": sorted(selected_dynamic_checks),
        "selected_dynamic_failures": selected_dynamic_failures,
    }


def validate_plan_architecture_focus(plan: dict[str, Any]) -> list[str]:
    if not isinstance(plan, dict):
        return ["architecture_plan_missing_or_not_object"]
    policy = plan.get("architecture_policy")
    if not isinstance(policy, dict):
        return []
    errors: list[str] = []
    if policy.get("epoch") != ACTIVE_EPOCH:
        errors.append("architecture_policy_epoch_mismatch")
    if policy.get("policy_digest") != _policy_contract_digest(policy):
        errors.append("architecture_policy_content_digest_mismatch")
    tasks = [task for task in plan.get("tasks") or [] if isinstance(task, dict)]
    floor_checks = set(policy.get("plan_required_floor_checks") or [])
    declared = {
        str(check)
        for task in tasks
        for check in task.get("checks_required") or []
    }
    missing_floor = sorted(floor_checks.difference(declared))
    if missing_floor:
        errors.append(f"architecture_plan_missing_floor_checks:{missing_floor}")
    focus = policy.get("selected_focus") or None
    focus_tasks = [
        task for task in tasks
        if str(task.get("architecture_focus_id") or "") == str((focus or {}).get("focus_id") or "")
        and str((focus or {}).get("focus_id") or "")
    ]
    if focus:
        if len(focus_tasks) != 1:
            errors.append(f"architecture_focus_task_count:{len(focus_tasks)}")
        else:
            task = focus_tasks[0]
            if task.get("skill_layer") not in set(focus.get("accepted_skill_layers") or []):
                errors.append("architecture_focus_skill_layer_mismatch")
            missing = sorted(set(focus.get("required_checks") or []).difference(task.get("checks_required") or []))
            if missing:
                errors.append(f"architecture_focus_required_checks_missing:{missing}")
            prompt = str(task.get("worker_prompt") or "").lower()
            missing_terms = [term for term in focus.get("required_terms") or [] if str(term).lower() not in prompt]
            if missing_terms:
                errors.append(f"architecture_focus_prompt_terms_missing:{missing_terms}")
    elif any(str(task.get("architecture_focus_id") or "").strip() for task in tasks):
        errors.append("architecture_focus_declared_without_selected_focus")
    for index, task in enumerate(tasks, start=1):
        writable = {
            Path(str(value)).name
            for value in [*(task.get("target_files") or []), *(task.get("files_allowed") or [])]
        }
        forbidden = sorted(writable.intersection({"national_bot.py", "precompute.py", "main.py", "state.py", "strategy.py"}))
        if forbidden:
            errors.append(f"architecture_task_{index}_forbidden_writable_files:{forbidden}")
    return errors


def validate_runtime_contract_implementation(
    plan: dict[str, Any],
    candidate_capabilities: dict[str, Any],
) -> list[str]:
    contracts, errors = _ledger_contracts(plan)
    if errors:
        return errors
    checks = _check_state(candidate_capabilities)
    expected_abi = native_policy_runtime_contract()["policy_abi"]
    from output_schema import RuntimeContract

    for label, raw in contracts:
        try:
            contract = RuntimeContract.model_validate(raw)
        except Exception as exc:
            errors.append(f"{label}:runtime_contract_schema:{type(exc).__name__}:{str(exc)[:180]}")
            continue
        dumped = contract.model_dump(mode="json")
        if dumped.get("policy_abi") != expected_abi:
            errors.append(f"{label}:policy_abi_mismatch")
        if contract.decision is not None:
            for check_id in ("fast_policy_baseline", "incremental_refinement_protocol"):
                if check_id in checks and not checks[check_id]:
                    errors.append(f"{label}:decision_contract_lacks_{check_id}")
        if contract.match_memory is not None:
            if contract.match_memory.owner_file != "national_bot.py":
                errors.append(f"{label}:match_memory_owner_must_be_system_runtime")
            if contract.match_memory.consumer not in {
                "policy.get_baseline_decision",
                "policy.iter_decisions",
            }:
                errors.append(f"{label}:match_memory_consumer_not_policy")
            for check_id in (
                "persistent_match_memory",
                "terminal_response_memory",
                "showdown_range_posterior",
            ):
                if not checks.get(check_id, False):
                    errors.append(f"{label}:match_memory_lacks_{check_id}")
        state_learning = contract.state_learning
        if state_learning is not None:
            for check_id in state_learning.primary_checks():
                if not checks.get(check_id, False):
                    errors.append(f"{label}:selected_primary_lacks_{check_id}")
    return list(dict.fromkeys(errors))


def architecture_policy_prompt(policy: dict[str, Any]) -> str:
    focus = policy.get("selected_focus") or None
    lines = [
        "System-owned national_tcp_policy_v1 architecture policy:",
        f"- policy_version={policy.get('policy_version')}",
        f"- official_policy_id={policy.get('official_policy_id')}",
        "- official_oracle_digests=" + ", ".join(
            f"{path}:{digest}"
            for path, digest in sorted((policy.get("official_oracle_digests") or {}).items())
        ),
        "- candidate decisions and all writable strategy code live only in policy.py",
        "- national_bot.py and precompute.py are exact system-owned read-only bytes",
        "- input is decision_context v1; output is pass/fold/allin/raise(raise_to)",
        "- the socket owner alone maps pass to the official call/check token and emits bytes",
        "- preserve every baseline_passed_checks item; regressions are blocking",
        f"- plan_required_floor_checks={', '.join(policy.get('plan_required_floor_checks') or []) or 'none'}",
    ]
    if focus:
        lines.extend([
            f"- selected_focus={focus.get('focus_id')}: {focus.get('title')}",
            f"- required_checks={', '.join(focus.get('required_checks') or [])}",
            f"- accepted_skill_layers={', '.join(focus.get('accepted_skill_layers') or [])}",
            f"- suggested_files={', '.join(focus.get('suggested_files') or [])}",
            f"- required_worker_prompt_terms={', '.join(focus.get('required_terms') or [])}",
            "- exactly one matching task declares one typed state_learning primary",
        ])
    else:
        lines.append("- selected_focus=none")
    return "\n".join(lines)


def crossover_architecture_policy_prompt(policy: dict[str, Any]) -> str:
    return "\n".join([
        "System-owned national_tcp_policy_v1 crossover baseline:",
        f"- policy_version={policy.get('policy_version')}",
        "- crossover may combine policy.py only; helpers/assets are not writable ABI",
        "- preserve exact national_bot.py, precompute.py, national_runtime_manifest.json, and policy_epoch_receipt.json",
        "- source inputs are the two frozen parent policy.py artifacts and digest-bound evidence",
        "- every candidate action remains a typed intent over decision_context v1",
    ])


__all__ = [
    "ACTIVE_EPOCH",
    "ARCHITECTURE_TRANSITION_PHASE_FINAL",
    "ARCHITECTURE_TRANSITION_PHASE_PREPLAN",
    "NATIVE_TEMPLATE_PROVIDED_CHECKS",
    "OFFICIAL_FULL_POLICY_ID",
    "OFFICIAL_ORACLE_DOC_DIGESTS",
    "PREPARED_CAPABILITY_SNAPSHOT_SCHEMA_VERSION",
    "RUNTIME_ARCHITECTURE_POLICY_SCHEMA_VERSION",
    "RUNTIME_ARCHITECTURE_POLICY_VERSION",
    "RUNTIME_CORRECTNESS_FLOOR_CHECKS",
    "RUNTIME_FLOOR_CHECKS",
    "STATE_LEARNING_INNOVATION_CHECKS",
    "architecture_focus_specs",
    "architecture_policy_prompt",
    "attach_runtime_contract_ledger",
    "build_architecture_policy",
    "build_lineage_only_architecture_policy",
    "build_lineage_only_prepared_capability_snapshot",
    "build_prepared_capability_snapshot",
    "build_runtime_contract_ledger",
    "crossover_architecture_policy_prompt",
    "evaluate_architecture_transition",
    "lineage_only_capabilities",
    "native_policy_runtime_contract",
    "prepared_capability_snapshot_digest",
    "runtime_contract_ledger_digest",
    "select_architecture_focus",
    "validate_plan_architecture_focus",
    "validate_prepared_capability_snapshot",
    "validate_runtime_contract_implementation",
    "validate_runtime_contract_ledger",
]
