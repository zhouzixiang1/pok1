"""Master proposal-packet trust-boundary companion.

Extracted from agent_master_validation.py as a single business responsibility:
the final-Master trust boundary.  This module re-validates the durable proposal
packet, binds the provider-selected immutable proposal, compiles its system-owned
worker contract, projects the strict-final accepted plan, and records the Master
invocation evidence.

The parent module owns the proposal schema constants
(``_PROPOSAL_PACKET_SCHEMA_VERSION``, ``_PROPOSAL_SCHEMA_VERSION``,
``_PROPOSAL_CRITIC_CRITERIA``) and the proposal-critic/measurement/identity
helpers; this companion reaches them via ``_amv.<name>``.  All public symbols
are re-exported by agent_master_validation.py (as thin delegate shells) and then
by agent_master.py for backward compatibility.
"""

from __future__ import annotations

import hashlib
import json
import re

from output_schema import (
    STATE_LEARNING_PRIMARY_CHECKS,
    STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS,
    WORKER_PROMPT_MAX_CHARS,
    WORKER_PROMPT_MIN_CHARS,
)

import agent_master_validation as _amv


def _proposal_packet_error(
    reason: str,
    *,
    context_digest: str = "",
    source_code_digest: str = "",
) -> str:
    return json.dumps({
        "schema_version": _amv._PROPOSAL_PACKET_SCHEMA_VERSION,
        "valid": False,
        "reason": str(reason)[:500],
        "context_digest": context_digest,
        "source_code_digest": source_code_digest,
        "proposal_count": 0,
        "valid_critic_count": 0,
        "allowed_proposal_ids": [],
        "ordered_proposals": [],
        "proposal_source_symbol_digests": {},
        "proposal_invocations": {},
        "critic_reviews": [],
    }, ensure_ascii=False, sort_keys=True)


def _parse_valid_proposal_packet_impl(
    packet_text: str,
) -> tuple[dict | None, list[str]]:
    """Validate the machine packet again at the final-Master trust boundary."""
    try:
        packet = json.loads(packet_text)
    except (TypeError, json.JSONDecodeError):
        return None, ["proposal_packet_not_json"]
    if not isinstance(packet, dict):
        return None, ["proposal_packet_not_object"]
    errors = []
    if packet.get("schema_version") != _amv._PROPOSAL_PACKET_SCHEMA_VERSION:
        errors.append("proposal_packet_schema_mismatch")
    if packet.get("valid") is not True:
        errors.append(f"proposal_packet_invalid:{packet.get('reason', 'unknown')}")
        # Error packets intentionally contain only the primary rejection and
        # safe identity fields. Do not feed that reduced shape through the
        # success-packet validator and obscure the actual cause with secondary
        # field, evidence-mode, and critic diagnostics.
        return None, errors
    expected_packet_fields = {
        "schema_version",
        "valid",
        "authority",
        "context_digest",
        "source_code_digest",
        "evidence_mode",
        "critic_criteria",
        "proposal_count",
        "valid_critic_count",
        "allowed_proposal_ids",
        "ordered_proposals",
        "proposal_source_symbol_digests",
        "proposal_invocations",
        "critic_reviews",
    }
    if set(packet) != expected_packet_fields:
        errors.append("proposal_packet_fields_mismatch")
    evidence_mode = str(packet.get("evidence_mode") or "")
    expected_execution_mode = {
        "frozen_strength_snapshot": "strategy_implementation",
        "singleton_parent_no_strength": "strategy_implementation",
        "fresh_strict_control_no_strength": "fixed_blueprint_capability_audit",
    }.get(evidence_mode)
    if expected_execution_mode is None:
        errors.append("proposal_packet_evidence_mode_invalid")
    proposals = packet.get("ordered_proposals")
    allowed = packet.get("allowed_proposal_ids")
    if not isinstance(proposals, list) or len(proposals) != 3:
        errors.append("proposal_packet_requires_exactly_three_proposals")
        proposals = []
    if packet.get("proposal_count") != 3:
        errors.append("proposal_packet_count_must_be_three")
    proposal_ids = [
        str(item.get("proposal_id") or "")
        for item in proposals
        if isinstance(item, dict)
    ]
    if (
        len(proposal_ids) != len(proposals)
        or len(set(proposal_ids)) != len(proposal_ids)
        or not isinstance(allowed, list)
        or not 1 <= len(allowed) <= len(proposal_ids)
        or len(set(map(str, allowed))) != len(allowed)
        or not set(map(str, allowed)).issubset(set(proposal_ids))
    ):
        errors.append("proposal_packet_id_set_mismatch")
    # Cross-proposal change_symbol uniqueness (2026-08-16 evolution audit:
    # v172/v186/v187 scout triples were near-identical — same symbol, three
    # rewordings — which collapses the ensemble into one direction). The
    # ensemble gatherer rejects duplicates first; this is the packet-level
    # backstop for anything that slips through (e.g. journal replay).
    change_symbols = [
        str(item.get("change_symbol") or "")
        for item in proposals
        if isinstance(item, dict)
    ]
    if len(change_symbols) == len(proposals) and len(set(change_symbols)) != len(change_symbols):
        errors.append("proposal_packet_change_symbols_not_distinct")
    required_proposal_fields = {
        "schema_version",
        "direction",
        "proposal_id",
        "targeted_failure",
        "structural_change",
        "counterfactual",
        "measurement",
        "why_not_threshold_tuning",
        "mechanism_target",
        "expected_diff",
        "target_files",
        "source_symbols",
        "change_symbol",
        "reachable_chain",
        "falsifier",
        "evidence_refs",
        "snapshot_evidence",
        "execution_mode",
        "risks",
    }
    for item in proposals:
        if not isinstance(item, dict):
            continue
        if set(item) != required_proposal_fields:
            errors.append(f"proposal_packet_fields_missing:{item.get('proposal_id', '')}")
            continue
        proposal_id = item.get("proposal_id")
        malformed_shape = False
        if (
            not isinstance(proposal_id, str)
            or re.fullmatch(r"[0-9a-f]{16}", proposal_id) is None
        ):
            errors.append("proposal_id_invalid")
            malformed_shape = True
        scalar_minimums = {
            "targeted_failure": 20,
            "structural_change": 20,
            "counterfactual": 20,
            "measurement": 20,
            "why_not_threshold_tuning": 20,
            "expected_diff": 20,
            "risks": 20,
        }
        for field, minimum in scalar_minimums.items():
            value = item.get(field)
            if not isinstance(value, str) or len(value.strip()) < minimum:
                errors.append(f"proposal_packet_{field}_invalid:{proposal_id or ''}")
                malformed_shape = True
        if not isinstance(item.get("change_symbol"), str):
            errors.append(
                f"proposal_packet_change_symbol_invalid:{proposal_id or ''}"
            )
            malformed_shape = True
        collection_contracts = {
            "target_files": (1, 3),
            "source_symbols": (1, 8),
            "reachable_chain": (2, 8),
            "evidence_refs": (1, 10),
            "snapshot_evidence": (0, 3),
        }
        for field, (minimum, maximum) in collection_contracts.items():
            value = item.get(field)
            if (
                not isinstance(value, list)
                or not minimum <= len(value) <= maximum
                or (
                    field != "snapshot_evidence"
                    and any(not isinstance(entry, str) for entry in value)
                )
            ):
                errors.append(
                    f"proposal_packet_{field}_shape_invalid:{proposal_id or ''}"
                )
                malformed_shape = True
        if malformed_shape:
            continue
        if item.get("schema_version") != _amv._PROPOSAL_SCHEMA_VERSION:
            errors.append(f"proposal_schema_mismatch:{item.get('proposal_id', '')}")
        if item.get("execution_mode") != expected_execution_mode:
            errors.append(
                f"proposal_execution_mode_mismatch:{item.get('proposal_id', '')}"
            )
        if not _amv._proposal_measurement_contract_valid(
            str(item.get("measurement") or ""),
            evidence_mode,
        ):
            errors.append(
                f"proposal_measurement_contract_invalid:{item.get('proposal_id', '')}"
            )
        if item.get("mechanism_target") not in set(
            STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS.values()
        ):
            errors.append(
                f"proposal_mechanism_target_invalid:{item.get('proposal_id', '')}"
            )
        change_symbol = _amv._normalize_source_symbol(item.get("change_symbol"))
        source_symbols = list(map(str, item.get("source_symbols") or []))
        reachable_chain = list(map(str, item.get("reachable_chain") or []))
        target_files = list(map(str, item.get("target_files") or []))
        if change_symbol != item.get("change_symbol"):
            errors.append(
                f"proposal_change_symbol_invalid:{item.get('proposal_id', '')}"
            )
        elif change_symbol not in source_symbols:
            errors.append(
                "proposal_change_symbol_not_in_source_symbols:"
                f"{item.get('proposal_id', '')}"
            )
        else:
            if change_symbol.rsplit(":", 1)[0] not in target_files:
                errors.append(
                    "proposal_change_symbol_not_in_target_files:"
                    f"{item.get('proposal_id', '')}"
                )
            if not reachable_chain or reachable_chain[-1] != change_symbol:
                errors.append(
                    "proposal_change_symbol_not_chain_terminal:"
                    f"{item.get('proposal_id', '')}"
                )
        falsifier = item.get("falsifier")
        falsifier_fields = {
            "test_name",
            "state_learning_primary",
            "intervention_target",
            "control",
            "intervention",
            "expected_observation",
        }
        if (
            not isinstance(falsifier, dict)
            or set(falsifier) != falsifier_fields
            or any(not isinstance(falsifier.get(key), str) for key in falsifier_fields)
        ):
            errors.append(
                f"proposal_falsifier_invalid:{item.get('proposal_id', '')}"
            )
        else:
            test_name = falsifier["test_name"].strip()
            primary = _amv._proposal_falsifier_primary(test_name)
            if (
                primary is None
                or test_name not in STATE_LEARNING_PRIMARY_CHECKS[primary]
            ):
                errors.append(
                    "proposal_falsifier_primary_mapping_invalid:"
                    f"{item.get('proposal_id', '')}"
                )
            elif falsifier["state_learning_primary"].strip() != primary:
                errors.append(
                    "proposal_falsifier_state_learning_primary_mismatch:"
                    f"{item.get('proposal_id', '')}:expected={primary}:actual="
                    f"{falsifier['state_learning_primary'].strip()}"
                )
            elif falsifier["intervention_target"].strip() != (
                STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]
            ):
                errors.append(
                    "proposal_falsifier_intervention_target_mismatch:"
                    f"{item.get('proposal_id', '')}:expected="
                    f"{STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]}:"
                    f"actual={falsifier['intervention_target'].strip()}"
                )
            else:
                errors.extend(
                    error + f":{item.get('proposal_id', '')}"
                    for error in _amv._proposal_mechanism_target_errors(
                        item,
                        falsifier,
                    )
                )
        try:
            bindability_error = _proposal_worker_bindability_error(item)
        except Exception:
            errors.append(
                f"proposal_worker_binding_invalid:{item.get('proposal_id', '')}"
            )
        else:
            if bindability_error:
                errors.append(bindability_error)
        snapshot_evidence = item.get("snapshot_evidence")
        if not isinstance(snapshot_evidence, list):
            errors.append(
                f"proposal_snapshot_evidence_not_list:{item.get('proposal_id', '')}"
            )
            snapshot_evidence = []
        if evidence_mode == "frozen_strength_snapshot" and not snapshot_evidence:
            errors.append(
                f"proposal_snapshot_evidence_missing:{item.get('proposal_id', '')}"
            )
        if evidence_mode != "frozen_strength_snapshot" and snapshot_evidence:
            errors.append(
                f"proposal_snapshot_evidence_forbidden:{item.get('proposal_id', '')}"
            )
        snapshot_refs = []
        required_binding_keys = {
            "reference",
            "node_sha256",
            "resolved_projection",
            "projection_sha256",
            "projection_truncated",
        }
        # Optional statistical-evidence scalars (games/a_wins/b_wins/draws)
        # ride along since the two-tier evidence bar (2026-08-16); the digest
        # fields remain the binding authority.
        allowed_binding_keys = required_binding_keys | {
            "games", "a_wins", "b_wins", "draws",
        }
        for binding in snapshot_evidence:
            if (
                not isinstance(binding, dict)
                or not required_binding_keys.issubset(binding)
                or not allowed_binding_keys.issuperset(binding)
            ):
                errors.append(
                    f"proposal_snapshot_binding_invalid:{item.get('proposal_id', '')}"
                )
                continue
            projection = str(binding.get("resolved_projection") or "")
            reference = str(binding.get("reference") or "")
            if (
                not reference.startswith("snapshot:")
                or reference not in (item.get("evidence_refs") or [])
                or not re.fullmatch(r"[0-9a-f]{64}", str(binding.get("node_sha256") or ""))
                or binding.get("projection_sha256")
                != hashlib.sha256(projection.encode("utf-8")).hexdigest()
                or not isinstance(binding.get("projection_truncated"), bool)
                or len(projection) < 20
                or len(projection) > 1600
            ):
                errors.append(
                    f"proposal_snapshot_binding_invalid:{item.get('proposal_id', '')}"
                )
            snapshot_refs.append(reference)
        expected_snapshot_refs = [
            str(ref)
            for ref in (item.get("evidence_refs") or [])
            if str(ref).startswith("snapshot:")
        ]
        if snapshot_refs != expected_snapshot_refs:
            errors.append(
                f"proposal_snapshot_binding_set_mismatch:{item.get('proposal_id', '')}"
            )
        if (
            evidence_mode == "frozen_strength_snapshot"
            and not _amv._measurement_target_bound_to_snapshot(
                str(item.get("measurement") or ""),
                snapshot_evidence,
            )
        ):
            errors.append(
                f"proposal_measurement_target_not_snapshot_bound:"
                f"{item.get('proposal_id', '')}"
            )
        if item.get("proposal_id") != _amv._proposal_identity(item):
            errors.append(f"proposal_identity_mismatch:{item.get('proposal_id', '')}")
    source_symbol_digests = packet.get("proposal_source_symbol_digests")
    if (
        not isinstance(source_symbol_digests, dict)
        or set(source_symbol_digests) != set(proposal_ids)
    ):
        errors.append("proposal_source_symbol_digest_set_mismatch")
        source_symbol_digests = {}
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        proposal_id = str(proposal.get("proposal_id") or "")
        row = source_symbol_digests.get(proposal_id)
        symbols = proposal.get("source_symbols")
        if (
            not isinstance(row, dict)
            or not isinstance(symbols, list)
            or set(row) != set(map(str, symbols))
            or any(
                re.fullmatch(r"[0-9a-f]{64}", str(value or "")) is None
                for value in (row or {}).values()
            )
        ):
            errors.append(
                f"proposal_source_symbol_digest_invalid:{proposal_id}"
            )
    proposal_invocations = packet.get("proposal_invocations")
    if (
        not isinstance(proposal_invocations, dict)
        or set(proposal_invocations) != set(proposal_ids)
    ):
        errors.append("proposal_invocation_set_mismatch")
        proposal_invocations = {}
    invocation_ids: list[str] = []
    try:
        from bot_artifact import canonical_digest
        from system_strict_bootstrap import validate_llm_invocation_evidence

        for proposal in proposals:
            if not isinstance(proposal, dict):
                continue
            proposal_id = str(proposal.get("proposal_id") or "")
            evidence = proposal_invocations.get(proposal_id)
            direction = str(proposal.get("direction") or "")
            evidence_errors = validate_llm_invocation_evidence(
                evidence,
                expected_purpose=f"master_proposal_scout:{direction}",
            )
            errors.extend(
                f"proposal_invocation_invalid:{proposal_id}:{item}"
                for item in evidence_errors
            )
            if isinstance(evidence, dict):
                invocation_ids.append(str(evidence.get("invocation_id") or ""))
                role = str(evidence.get("role") or "")
                if not role.startswith(f"MASTER PROPOSAL {direction}"):
                    errors.append(
                        f"proposal_invocation_role_mismatch:{proposal_id}"
                    )
                if evidence.get("role_result_digest") != canonical_digest(proposal):
                    errors.append(
                        f"proposal_invocation_result_mismatch:{proposal_id}"
                    )
    except Exception as exc:
        errors.append(
            f"proposal_invocation_validation_error:{type(exc).__name__}"
        )
    if packet.get("valid_critic_count") != 2:
        errors.append("proposal_packet_requires_two_valid_critics")
    reviews = packet.get("critic_reviews")
    expected_critic_ids = {"falsification", "scope"}
    if not isinstance(reviews, list) or len(reviews) != 2:
        errors.append("proposal_packet_requires_two_critic_reviews")
        reviews = []
    critic_ids = {
        str(review.get("critic_id") or "")
        for review in reviews
        if isinstance(review, dict)
    }
    if critic_ids != expected_critic_ids:
        errors.append("proposal_critic_identity_set_mismatch")
    try:
        from bot_artifact import canonical_digest
        from system_strict_bootstrap import validate_llm_invocation_evidence

        reject_counts = {proposal_id: 0 for proposal_id in proposal_ids}
        for review in reviews:
            if not isinstance(review, dict):
                errors.append("proposal_critic_review_not_object")
                continue
            critic_id = str(review.get("critic_id") or "")
            if set(review) != {
                "critic_id",
                "ranking",
                "reject",
                "ballots",
                "invocation_evidence",
            }:
                errors.append(f"proposal_critic_review_fields_mismatch:{critic_id}")
            ballots = review.get("ballots")
            if not isinstance(ballots, list) or len(ballots) != len(proposal_ids):
                errors.append(f"proposal_critic_ballot_count_mismatch:{critic_id}")
                ballots = []
            seen_ballots: set[str] = set()
            normalized_ballots = []
            for ballot in ballots:
                if not isinstance(ballot, dict) or set(ballot) != {
                    "proposal_id",
                    "scores",
                    "total_score",
                    "reject",
                    "reason",
                }:
                    errors.append(
                        f"proposal_critic_ballot_fields_mismatch:{critic_id}"
                    )
                    continue
                proposal_id = ballot.get("proposal_id")
                scores = ballot.get("scores")
                reject = ballot.get("reject")
                reason = ballot.get("reason")
                if (
                    not isinstance(proposal_id, str)
                    or proposal_id not in set(proposal_ids)
                    or proposal_id in seen_ballots
                    or not isinstance(scores, dict)
                    or set(scores) != set(_amv._PROPOSAL_CRITIC_CRITERIA)
                    or not isinstance(reject, bool)
                    or not isinstance(reason, str)
                    or len(reason.strip()) < 12
                ):
                    errors.append(
                        f"proposal_critic_ballot_schema_invalid:{critic_id}"
                    )
                    continue
                if any(
                    isinstance(score, bool)
                    or not isinstance(score, int)
                    or not 1 <= score <= 5
                    for score in scores.values()
                ):
                    errors.append(
                        f"proposal_critic_ballot_score_invalid:{critic_id}"
                    )
                    continue
                total = sum(scores.values())
                if ballot.get("total_score") != total:
                    errors.append(
                        f"proposal_critic_ballot_total_mismatch:{critic_id}"
                    )
                seen_ballots.add(proposal_id)
                normalized_ballots.append(ballot)
                if reject:
                    reject_counts[proposal_id] += 1
            if seen_ballots != set(proposal_ids):
                errors.append(f"proposal_critic_ballot_set_mismatch:{critic_id}")
            expected_ranking = [
                item["proposal_id"]
                for item in sorted(
                    normalized_ballots,
                    key=lambda item: (
                        item["reject"],
                        -item["total_score"],
                        item["proposal_id"],
                    ),
                )
            ]
            expected_reject = [
                item["proposal_id"]
                for item in normalized_ballots
                if item["reject"]
            ]
            if review.get("ranking") != expected_ranking:
                errors.append(f"proposal_critic_ranking_mismatch:{critic_id}")
            if review.get("reject") != expected_reject:
                errors.append(f"proposal_critic_reject_mismatch:{critic_id}")
            evidence = review.get("invocation_evidence")
            evidence_errors = validate_llm_invocation_evidence(
                evidence,
                expected_purpose=f"master_proposal_critic:{critic_id}",
            )
            errors.extend(
                f"proposal_critic_invocation_invalid:{critic_id}:{item}"
                for item in evidence_errors
            )
            if isinstance(evidence, dict):
                invocation_ids.append(str(evidence.get("invocation_id") or ""))
                role = str(evidence.get("role") or "")
                if not role.startswith(f"MASTER PROPOSAL CRITIC {critic_id}"):
                    errors.append(
                        f"proposal_critic_invocation_role_mismatch:{critic_id}"
                    )
                role_result = {
                    key: value
                    for key, value in review.items()
                    if key not in {"critic_id", "invocation_evidence"}
                }
                if evidence.get("role_result_digest") != canonical_digest(
                    role_result
                ):
                    errors.append(
                        f"proposal_critic_invocation_result_mismatch:{critic_id}"
                    )
        expected_allowed = [
            proposal_id
            for proposal_id in proposal_ids
            if reject_counts.get(proposal_id, 0) < 2
        ]
        if list(map(str, allowed or [])) != expected_allowed:
            errors.append("proposal_packet_allowed_ids_veto_mismatch")
        if not expected_allowed:
            errors.append("proposal_packet_all_proposals_unanimously_rejected")
    except Exception as exc:
        errors.append(
            f"proposal_critic_invocation_validation_error:{type(exc).__name__}"
        )
    if len(invocation_ids) != 5 or len(set(invocation_ids)) != 5:
        errors.append("proposal_packet_invocations_not_independent")
    if packet.get("critic_criteria") != _amv._PROPOSAL_CRITIC_CRITERIA:
        errors.append("proposal_critic_criteria_mismatch")
    context_digest = str(packet.get("context_digest") or "")
    source_digest = str(packet.get("source_code_digest") or "")
    if (
        len(context_digest) != 64
        or len(source_digest) != 64
        or any(char not in "0123456789abcdef" for char in context_digest + source_digest)
    ):
        errors.append("proposal_packet_digest_invalid")
    return (None, errors) if errors else (packet, [])


def _parse_valid_proposal_packet(packet_text: str) -> tuple[dict | None, list[str]]:
    """Total fail-closed wrapper around durable proposal-packet validation."""

    try:
        return _parse_valid_proposal_packet_impl(packet_text)
    except Exception as exc:
        return None, [
            "proposal_packet_validation_error:"
            f"{type(exc).__name__}:{str(exc)[:200]}"
        ]


def _proposal_binding_error(code: str, payload: dict) -> str:
    return code + ":" + json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _provider_prompt_reserved_markers(prompt: str) -> tuple[str, ...]:
    """Return system-owned delimiters a provider is never allowed to emit."""

    from plan_compiler import (
        SELECTED_PROPOSAL_BEGIN,
        SELECTED_PROPOSAL_END,
        SYSTEM_OWNED_CONTRACT_BEGIN,
        SYSTEM_OWNED_CONTRACT_END,
    )

    return tuple(
        marker
        for marker in (
            SELECTED_PROPOSAL_BEGIN,
            SELECTED_PROPOSAL_END,
            SYSTEM_OWNED_CONTRACT_BEGIN,
            SYSTEM_OWNED_CONTRACT_END,
        )
        if marker in prompt
    )


def _canonical_provider_worker_prompt(prompt: str) -> str:
    """Return exactly the provider text that selected-contract binding uses.

    The binder has always removed a trailing Unicode-whitespace suffix before
    appending system-owned blocks.  Validate the same canonical text so a
    prompt at a selected cap has one arithmetic meaning in the model repair
    error, the final bind, and the later compiler.  This is whitespace
    normalization only: provider-authored non-whitespace text is never
    shortened to make a plan fit.
    """

    return prompt.rstrip()


def _task_proposal_scope_paths(task: dict) -> tuple[set[str], tuple[dict, ...]]:
    """Parse proposal-writable task paths without iterating provider scalars."""

    paths: set[str] = set()
    invalid: list[dict] = []
    for field in ("target_files", "files_allowed"):
        if field not in task:
            continue
        values = task.get(field)
        if not isinstance(values, list):
            invalid.append({
                "field": field,
                "expected_type": "list",
                "actual_type": type(values).__name__,
            })
            continue
        for index, value in enumerate(values):
            if not isinstance(value, str):
                invalid.append({
                    "field": field,
                    "index": index,
                    "expected_type": "str",
                    "actual_type": type(value).__name__,
                })
                continue
            path = _amv._safe_relative_python_path(value)
            if path is not None:
                paths.add(path)
    return paths, tuple(invalid)


def _resolve_allowed_selected_proposal(
    data: dict,
    packet: dict,
) -> tuple[dict | None, list[str]]:
    """Resolve the one provider-selected immutable proposal, or fail closed."""

    if not isinstance(data, dict):
        return None, ["master_output_not_object"]
    selected = data.get("selected_proposal_id")
    if not isinstance(selected, str):
        return None, ["selected_proposal_id_must_be_one_string"]
    proposals = {
        item["proposal_id"]: item
        for item in packet.get("ordered_proposals", [])
        if isinstance(item, dict) and isinstance(item.get("proposal_id"), str)
    }
    proposal = proposals.get(selected)
    if (
        proposal is None
        or selected not in set(map(str, packet.get("allowed_proposal_ids") or []))
    ):
        return None, [f"selected_proposal_id_not_allowed:{selected}"]
    return proposal, []


def _canonicalize_selected_proposal_metadata(
    data: dict,
    packet: dict,
) -> tuple[dict, dict | None, list[str], tuple[str, ...]]:
    """Bind duplicated display metadata to the selected immutable proposal.

    ``selected_proposal_id`` is the provider's one semantic selection.  Its
    ``targeted_failure`` and ``measurement`` are already sealed in the
    proposal packet, so letting a final-Master free-text duplicate override or
    accidentally paraphrase them only creates a non-causal retry failure.  The
    system therefore derives the two duplicate plan fields before any schema,
    Worker, or strict-authority projection consumes the plan.  Selection,
    task scope, runtime contract, and provider prompt remain independently
    validated below.
    """

    proposal, errors = _resolve_allowed_selected_proposal(data, packet)
    if errors or proposal is None:
        return data, None, errors, ()
    result = json.loads(json.dumps(data, ensure_ascii=False))
    expected = {
        "targeted_failure": str(proposal["targeted_failure"]),
        "measurement_plan": str(proposal["measurement"]),
    }
    rebound = tuple(
        key for key, value in expected.items() if result.get(key) != value
    )
    result.update(expected)
    return result, proposal, [], rebound


def _validate_final_proposal_binding(data: dict, packet: dict) -> list[str]:
    """Require one exact proposal selection and its writable-file contract."""
    proposal, selection_errors = _resolve_allowed_selected_proposal(data, packet)
    if selection_errors or proposal is None:
        return selection_errors
    selected = str(data["selected_proposal_id"])
    errors = []
    if str(data.get("targeted_failure") or "").strip() != proposal["targeted_failure"]:
        errors.append("targeted_failure_must_exactly_copy_selected_proposal")
    if str(data.get("measurement_plan") or "").strip() != proposal["measurement"]:
        errors.append("measurement_plan_must_exactly_copy_selected_proposal")
    writable: set[str] = set()
    tasks = data.get("tasks")
    task_scopes: list[tuple[dict, set[str]]] = []
    if isinstance(tasks, list):
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_files, scope_errors = _task_proposal_scope_paths(task)
            task_scopes.append((task, task_files))
            writable.update(task_files)
            for scope_error in scope_errors:
                errors.append(_proposal_binding_error(
                    "selected_proposal_worker_scope_type_invalid",
                    {
                        "worker_id": task.get("worker_id"),
                        "proposal_id": selected,
                        **scope_error,
                    },
                ))
    missing_files = sorted(set(proposal["target_files"]) - writable)
    if missing_files:
        errors.append(f"selected_proposal_target_files_not_writable:{missing_files}")
    compilation = _selected_proposal_compilation_contract(proposal)
    binding_chars = int(compilation["reserved_selected_contract_chars"])
    expected_primary = str(compilation["state_learning_primary"])
    selected_check = str(compilation["falsifier_test_name"])
    required_primary_checks = set(map(
        str,
        compilation["required_primary_checks"],
    ))
    bound_task_count = 0
    falsifier_check_bound = False
    observed_primaries: list[dict] = []
    if isinstance(tasks, list):
        for task, task_files in task_scopes:
            if not task_files.intersection(proposal["target_files"]):
                continue
            bound_task_count += 1
            try:
                from output_schema import RuntimeContract

                runtime_contract = RuntimeContract.model_validate(
                    task.get("runtime_contract")
                )
            except Exception:
                runtime_contract = None
            state_learning = (
                runtime_contract.state_learning
                if runtime_contract is not None
                else None
            )
            checks_required = task.get("checks_required") or []
            actual_primary = (
                state_learning.primary_innovation()
                if state_learning is not None
                else None
            )
            task_checks = (
                set(map(str, checks_required))
                if isinstance(checks_required, list)
                else set()
            )
            observed_primaries.append({
                "worker_id": task.get("worker_id", bound_task_count),
                "state_learning_primary": actual_primary,
                "checks_required": sorted(task_checks),
            })
            if actual_primary == expected_primary:
                missing_checks = sorted(required_primary_checks - task_checks)
                if not missing_checks and selected_check in required_primary_checks:
                    falsifier_check_bound = True
                elif missing_checks:
                    errors.append(_proposal_binding_error(
                        "selected_proposal_primary_checks_missing",
                        {
                            "worker_id": task.get("worker_id", bound_task_count),
                            "proposal_id": selected,
                            "state_learning_primary": expected_primary,
                            "proposal_falsifier": selected_check,
                            "missing_checks": missing_checks,
                            "required_primary_checks": sorted(required_primary_checks),
                        },
                    ))
            raw_prompt = task.get("worker_prompt")
            if not isinstance(raw_prompt, str):
                errors.append(_proposal_binding_error(
                    "selected_proposal_worker_prompt_type_invalid",
                    {
                        "worker_id": task.get("worker_id", bound_task_count),
                        "proposal_id": selected,
                        "expected_type": "str",
                        "actual_type": type(raw_prompt).__name__,
                    },
                ))
                continue
            prompt = _canonical_provider_worker_prompt(raw_prompt)
            if len(prompt.strip()) < WORKER_PROMPT_MIN_CHARS:
                errors.append(_proposal_binding_error(
                    "selected_proposal_worker_prompt_below_minimum",
                    {
                        "worker_id": task.get("worker_id", bound_task_count),
                        "proposal_id": selected,
                        "actual_provider_chars": len(prompt),
                        "actual_non_whitespace_chars": len(prompt.strip()),
                        "minimum_provider_chars": WORKER_PROMPT_MIN_CHARS,
                    },
                ))
            reserved_markers = _provider_prompt_reserved_markers(prompt)
            if reserved_markers:
                errors.append(_proposal_binding_error(
                    "selected_proposal_worker_prompt_reserved_marker",
                    {
                        "worker_id": task.get("worker_id", bound_task_count),
                        "proposal_id": selected,
                        "reserved_markers": list(reserved_markers),
                    },
                ))
            runtime_contract_reserve = int(
                compilation["reserved_runtime_contract_max_chars"]
            )
            combined_chars = (
                len(prompt) + binding_chars + 2 + runtime_contract_reserve
            )
            if combined_chars > WORKER_PROMPT_MAX_CHARS:
                budget_payload = {
                    "worker_id": task.get("worker_id", bound_task_count),
                    "proposal_id": selected,
                    "actual_provider_chars": len(prompt),
                    "reserved_selected_contract_chars": binding_chars,
                    "reserved_runtime_contract_max_chars": (
                        runtime_contract_reserve
                    ),
                    "separator_chars": 2,
                    "combined_chars": combined_chars,
                    "global_cap_chars": WORKER_PROMPT_MAX_CHARS,
                    "max_provider_chars": compilation["max_provider_chars"],
                    "overflow_chars": combined_chars - WORKER_PROMPT_MAX_CHARS,
                    "character_metric": compilation["character_metric"],
                }
                if len(raw_prompt) != len(prompt):
                    budget_payload["submitted_provider_chars"] = len(raw_prompt)
                    budget_payload["trimmed_trailing_whitespace_chars"] = (
                        len(raw_prompt) - len(prompt)
                    )
                errors.append(_proposal_binding_error(
                    "selected_proposal_worker_prompt_has_no_binding_budget",
                    budget_payload,
                ))
    if bound_task_count == 0 and not missing_files:
        errors.append("selected_proposal_has_no_bound_worker_task")
    elif bound_task_count and not falsifier_check_bound:
        errors.append(_proposal_binding_error(
            "selected_proposal_falsifier_not_bound_to_runtime_primary_check",
            {
                "proposal_id": selected,
                "proposal_falsifier": selected_check,
                "expected_state_learning_primary": expected_primary,
                "required_primary_checks": sorted(required_primary_checks),
                "observed_bound_tasks": observed_primaries,
            },
        ))
    from plan_compiler import selected_proposal_change_contract_errors

    if not missing_files:
        errors.extend(selected_proposal_change_contract_errors(
            data,
            change_symbol=str(proposal.get("change_symbol") or ""),
            reachable_chain=proposal.get("reachable_chain") or [],
            target_files=proposal.get("target_files") or [],
        ))
    return errors


def _selected_proposal_contract(proposal: dict) -> dict:
    falsifier = dict(proposal["falsifier"])
    state_learning_primary = _amv._proposal_falsifier_primary(
        falsifier.get("test_name")
    )
    if (
        state_learning_primary is None
        or falsifier.get("state_learning_primary") != state_learning_primary
        or falsifier.get("intervention_target")
        != STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[state_learning_primary]
        or proposal.get("mechanism_target") != falsifier.get("intervention_target")
    ):
        raise ValueError("selected proposal falsifier has no typed primary")
    contract = {
        "schema_version": 1,
        "proposal_id": str(proposal["proposal_id"]),
        "targeted_failure": str(proposal["targeted_failure"]),
        "structural_change": str(proposal["structural_change"]),
        "counterfactual": str(proposal["counterfactual"]),
        "measurement": str(proposal["measurement"]),
        "expected_diff": str(proposal["expected_diff"]),
        "target_files": list(proposal["target_files"]),
        "source_symbols": list(proposal["source_symbols"]),
        "change_symbol": str(proposal["change_symbol"]),
        "reachable_chain": list(proposal["reachable_chain"]),
        "falsifier": falsifier,
        "state_learning_primary": state_learning_primary,
        "mechanism_target": proposal["mechanism_target"],
        "intervention_target": falsifier["intervention_target"],
        "required_primary_checks": list(
            STATE_LEARNING_PRIMARY_CHECKS[state_learning_primary]
        ),
        "evidence_refs": list(proposal["evidence_refs"]),
        "snapshot_evidence": list(proposal.get("snapshot_evidence") or []),
        "execution_mode": str(
            proposal.get("execution_mode") or "strategy_implementation"
        ),
        "why_not_threshold_tuning": str(proposal["why_not_threshold_tuning"]),
        "risks": str(proposal["risks"]),
    }
    contract["contract_digest"] = hashlib.sha256(
        json.dumps(
            contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return contract


def _selected_proposal_binding(proposal: dict, packet: dict) -> dict:
    """Project the one canonical packet-to-plan binding used by every mode."""

    contract = _selected_proposal_contract(proposal)
    return {
        "schema_version": _amv._PROPOSAL_PACKET_SCHEMA_VERSION,
        "selected_proposal_id": proposal["proposal_id"],
        "contract_digest": contract["contract_digest"],
        "context_digest": packet["context_digest"],
        "source_code_digest": packet["source_code_digest"],
        "target_files": list(contract["target_files"]),
        "source_symbols": list(contract["source_symbols"]),
        "change_symbol": contract["change_symbol"],
        "reachable_chain": list(contract["reachable_chain"]),
        "falsifier": dict(contract["falsifier"]),
        "mechanism_target": contract["mechanism_target"],
        "state_learning_primary": contract["state_learning_primary"],
        "intervention_target": contract["intervention_target"],
        "required_primary_checks": list(contract["required_primary_checks"]),
        "evidence_refs": list(contract["evidence_refs"]),
        "snapshot_evidence": list(contract["snapshot_evidence"]),
        "execution_mode": contract["execution_mode"],
        "targeted_failure": contract["targeted_failure"],
        "structural_change": contract["structural_change"],
        "counterfactual": contract["counterfactual"],
        "measurement": contract["measurement"],
        "expected_diff": contract["expected_diff"],
        "why_not_threshold_tuning": contract["why_not_threshold_tuning"],
        "risks": contract["risks"],
        "selected_proposal": {
            key: value for key, value in proposal.items() if key != "direction"
        },
        "proposal_packet_digest": hashlib.sha256(
            json.dumps(
                packet,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }


def _selected_proposal_worker_block(proposal: dict) -> str:
    from plan_compiler import SELECTED_PROPOSAL_BEGIN, SELECTED_PROPOSAL_END

    contract = _selected_proposal_contract(proposal)
    execution_instruction = (
        "The checked-in fixed blueprint owns the v143 output bytes. Treat this "
        "proposal only as a capability-audit lens: do not claim that its prose "
        "caused the implementation or proves poker strength. The system quality "
        "gate must verify the named typed falsifier against the fixed blueprint."
        if contract["execution_mode"] == "fixed_blueprint_capability_audit"
        else
        "Modify the exact change_symbol through the named reachable chain. Do not "
        "substitute an unmeasured threshold-only edit, a second mechanism, or "
        "telemetry-only code. Preserve counterfactual and measurement as the "
        "generation hypothesis, and expose the named falsifier through the task "
        "RuntimeContract/checks_required so the system typed probe can execute it."
    )
    return "\n".join((
        SELECTED_PROPOSAL_BEGIN,
        "# SYSTEM-BOUND SELECTED PROPOSAL CONTRACT",
        f"proposal_id={contract['proposal_id']}",
        f"contract_digest={contract['contract_digest']}",
        f"execution_mode={contract['execution_mode']}",
        f"targeted_failure={contract['targeted_failure']}",
        f"structural_change={contract['structural_change']}",
        f"counterfactual={contract['counterfactual']}",
        f"measurement={contract['measurement']}",
        f"expected_diff={contract['expected_diff']}",
        "source_symbols=" + json.dumps(
            contract["source_symbols"], ensure_ascii=False, separators=(",", ":")
        ),
        f"change_symbol={contract['change_symbol']}",
        "reachable_chain=" + json.dumps(
            contract["reachable_chain"], ensure_ascii=False, separators=(",", ":")
        ),
        f"state_learning_primary={contract['state_learning_primary']}",
        f"mechanism_target={contract['mechanism_target']}",
        f"intervention_target={contract['intervention_target']}",
        "required_primary_checks=" + json.dumps(
            contract["required_primary_checks"],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "falsifier=" + json.dumps(
            contract["falsifier"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        "evidence_refs=" + json.dumps(
            contract["evidence_refs"], ensure_ascii=False, separators=(",", ":")
        ),
        "snapshot_evidence=" + json.dumps(
            contract["snapshot_evidence"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "not_threshold_tuning=" + contract["why_not_threshold_tuning"],
        "risks=" + contract["risks"],
        execution_instruction,
        SELECTED_PROPOSAL_END,
    ))


def _selected_proposal_compilation_contract(proposal: dict) -> dict:
    """Return the exact provider budget and typed-primary binding for a proposal."""

    from plan_compiler import SYSTEM_OWNED_CONTRACT_MAX_CHARS

    contract = _selected_proposal_contract(proposal)
    binding_chars = len(_selected_proposal_worker_block(proposal))
    separator_chars = 2
    return {
        "proposal_id": contract["proposal_id"],
        "falsifier_test_name": contract["falsifier"]["test_name"],
        "mechanism_target": contract["mechanism_target"],
        "change_symbol": contract["change_symbol"],
        "state_learning_primary": contract["state_learning_primary"],
        "intervention_target": contract["intervention_target"],
        "required_primary_checks": list(contract["required_primary_checks"]),
        "reserved_selected_contract_chars": binding_chars,
        "separator_chars": separator_chars,
        "reserved_runtime_contract_max_chars": (
            SYSTEM_OWNED_CONTRACT_MAX_CHARS
        ),
        "global_cap_chars": WORKER_PROMPT_MAX_CHARS,
        "max_provider_chars": (
            WORKER_PROMPT_MAX_CHARS
            - binding_chars
            - separator_chars
            - SYSTEM_OWNED_CONTRACT_MAX_CHARS
        ),
        "character_metric": "python_unicode_code_points",
    }


def _proposal_worker_bindability_error(proposal: dict) -> str | None:
    compilation = _selected_proposal_compilation_contract(proposal)
    if int(compilation["max_provider_chars"]) >= WORKER_PROMPT_MIN_CHARS:
        return None
    # The hint travels verbatim into the schema-repair prompt's
    # projection_hints, whose provider renderer only admits <=160 chars of
    # [a-z0-9_:.-]. The full compilation JSON used to leak here and made every
    # repair render raise, misclassifying a schema repair as
    # master_llm_unavailable and burning the whole infra retry budget. Keep the
    # binding-budget numbers (the actionable part) in a charset-safe form.
    binding = int(compilation["reserved_selected_contract_chars"])
    budget = int(compilation["max_provider_chars"])
    minimum = int(WORKER_PROMPT_MIN_CHARS)
    return (
        "proposal_worker_binding_cannot_fit_minimum_prompt."
        f"binding_chars.{binding}.provider_budget_chars.{budget}."
        f"minimum_provider_chars.{minimum}.shrink_binding_by.{minimum - budget}"
    )


def _proposal_compilation_contract_text(packet: dict) -> str:
    """Render all allowed proposal budgets before the final Master chooses one."""

    allowed = set(map(str, packet.get("allowed_proposal_ids") or []))
    rows = [
        _selected_proposal_compilation_contract(proposal)
        for proposal in packet.get("ordered_proposals") or []
        if str(proposal.get("proposal_id") or "") in allowed
    ]
    return json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _master_final_emission_guard(packet: dict) -> str:
    """Render the final, system-owned selected-plan emission limits.

    The final Master chooses an immutable proposal by id, then supplies only
    task-specific implementation reasoning.  Proposal metadata and the full
    selected contract are system-bound later, so repeating either in a Worker
    prompt wastes the very limited provider-owned prompt budget.
    """

    allowed = set(map(str, packet.get("allowed_proposal_ids") or []))
    rows = []
    for proposal in packet.get("ordered_proposals") or []:
        if not isinstance(proposal, dict):
            continue
        proposal_id = str(proposal.get("proposal_id") or "")
        if proposal_id not in allowed:
            continue
        compilation = _selected_proposal_compilation_contract(proposal)
        hard_cap = int(compilation["max_provider_chars"])
        rows.append({
            "proposal_id": proposal_id,
            "worker_prompt_hard_cap_chars": hard_cap,
            "worker_prompt_advisory_target_chars": max(
                WORKER_PROMPT_MIN_CHARS,
                hard_cap - 128,
            ),
        })
    if not rows:
        raise ValueError("Master final emission guard has no allowed proposal")
    return (
        "# SYSTEM-OWNED FINAL EMISSION GATE (highest priority)\n"
        "selected_proposal_id is your only proposal-selection field. The system "
        "binds targeted_failure and measurement_plan from that selected immutable "
        "proposal; do not paraphrase, expand, or use either duplicate field to "
        "change scope. For every task that writes a selected target file, keep "
        "worker_prompt near the listed advisory target (Unicode code points) and "
        "never exceed its hard cap. That selected row is the sole model-owned "
        "length authority: do not rely on template-wide length advice, compiler "
        "externalization, truncation, or a task brief to make it fit. Describe only "
        "task-specific implementation and checks; when the cap is small, use compact "
        "directives rather than reproducing code, the proposal, or the runtime "
        "contract. The system appends those immutable blocks after validation.\n"
        "EMISSION_CAPS="
        + json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\nReturn the single JSON object now; do not emit analysis outside it."
    )


def _bind_selected_proposal_workers(data: dict, proposal: dict) -> dict:
    """Compile the selected mechanism into every writable target task."""
    result = json.loads(json.dumps(data, ensure_ascii=False))
    block = _selected_proposal_worker_block(proposal)
    target_files = set(proposal["target_files"])
    for task in result.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        task_files, scope_errors = _task_proposal_scope_paths(task)
        if scope_errors:
            continue
        if task_files.intersection(target_files):
            provider_prompt = task.get("worker_prompt")
            if (
                not isinstance(provider_prompt, str)
                or len(provider_prompt.strip()) < WORKER_PROMPT_MIN_CHARS
                or _provider_prompt_reserved_markers(provider_prompt)
            ):
                continue
            task["worker_prompt"] = (
                _canonical_provider_worker_prompt(provider_prompt)
                + "\n\n"
                + block
            )
    return result


def _project_strict_final_master_result(
    output: str,
    *,
    proposal_packet: dict | None,
    architecture_policy: dict | None,
) -> tuple[dict | None, list[str]]:
    """Deterministically project provider text to the accepted strict plan.

    This is intentionally the same post-provider transformation used by the
    first strict Master path: proposal selection, system-owned policy ABI terms,
    canonical Master schema normalization, and the frozen proposal bindings.
    Strict authority calls this function before completing the provider effect,
    so an unrelated caller-supplied plan can never be accepted as LLM output.
    """

    if not isinstance(proposal_packet, dict):
        return None, ["proposal_packet_missing"]
    packet, packet_errors = _parse_valid_proposal_packet(json.dumps(
        proposal_packet,
        ensure_ascii=False,
        sort_keys=True,
    ))
    if packet_errors or packet is None:
        return None, ["proposal_packet_invalid:" + item for item in packet_errors]
    if not isinstance(architecture_policy, dict) or not architecture_policy:
        return None, ["architecture_policy_missing"]

    from llm_query import parse_json_output_with_mode

    data, failure_mode = parse_json_output_with_mode(output or "")
    if not isinstance(data, dict) or "tasks" not in data:
        return None, [f"master_output_invalid:{failure_mode}"]
    data, selected_proposal, selection_errors, _metadata_rebound = (
        _canonicalize_selected_proposal_metadata(data, packet)
    )
    if selection_errors or selected_proposal is None:
        return None, selection_errors
    binding_errors = _validate_final_proposal_binding(data, packet)
    if binding_errors:
        return None, binding_errors
    selected_proposal_id = data.pop("selected_proposal_id")
    data = _bind_selected_proposal_workers(data, selected_proposal)

    from plan_compiler import (
        bind_system_owned_policy_abi,
        bind_system_owned_worker_contract_terms,
    )

    data, _policy_abi = bind_system_owned_policy_abi(
        data,
        policy=architecture_policy,
    )
    data, _terms = bind_system_owned_worker_contract_terms(data)
    if _terms.get("overflow_tasks"):
        return None, [_proposal_binding_error(
            "system_owned_worker_contract_binding_overflow",
            {"tasks": _terms["overflow_tasks"]},
        )]
    if any(data.get(field) for field in (
        "branch_from",
        "source_override",
        "source_v_override",
    )):
        return None, ["master_source_override_forbidden"]

    from output_schema import validate_agent_output

    data, schema_errors = validate_agent_output("master", data)
    if schema_errors:
        return None, ["master_schema:" + item for item in schema_errors]

    data["selected_proposal_id"] = selected_proposal_id
    data["proposal_binding"] = _selected_proposal_binding(
        selected_proposal,
        packet,
    )
    data["proposal_ensemble"] = packet
    return data, []


def _record_master_invocation_evidence(
    result: dict,
    *,
    output: str,
    role_result: dict,
) -> dict:
    """Record a new invocation or reuse the exact accepted replay evidence."""

    from system_strict_bootstrap import (
        llm_result_digest,
        record_llm_invocation_evidence,
    )

    strict_call = result.get("strict_call")
    journal_bound = bool(
        isinstance(strict_call, dict)
        and strict_call.get("effect_id")
        and strict_call.get("accepted_receipt")
    )
    if journal_bound:
        from strict_authority_workflow import record_bound_invocation_evidence

        return record_bound_invocation_evidence(
            strict_call,
            log_file=result["log_file"],
        )
    evidence = record_llm_invocation_evidence(
        invocation_id=result["invocation_id"],
        purpose=result["purpose"],
        role=result["role"],
        prompt_digest=hashlib.sha256(
            result["prompt"].encode("utf-8")
        ).hexdigest(),
        raw_output_digest=hashlib.sha256(output.encode("utf-8")).hexdigest(),
        result_digest=llm_result_digest(result["cost_usd"], result["usage"]),
        role_result=role_result,
        log_file=result["log_file"],
    )
    return evidence
