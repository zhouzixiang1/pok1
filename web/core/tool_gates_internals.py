"""Quality-gate internal helpers: identity binding, proposal evidence,
fingerprint, idempotency, and quality-failure recording.

Extracted from tool_gates.py: the LLM-gate infrastructure identity binder,
strict-reviewer harness identity, selected-proposal quality evidence,
strict-blueprint rejection finalizer, code fingerprint, transient task
context checks, idempotency cache, and the worker_failures.jsonl recorder.
These are pure helpers invoked by run_quality_gates and run_review/run_critic.

All public symbols are re-exported by tool_gates.py for backward compatibility."""

from __future__ import annotations

import tool_gates as _tg  # parent; respects test monkeypatches


def _canonical_digest(value):
    return _tg.hashlib.sha256(
        _tg.json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _llm_gate_infrastructure_identity(
    *,
    component,
    role,
    candidate_dir,
    source_dir,
    prompt_text,
    checkpoint,
    source_fingerprint_override=None,
    harness_identity_override=None,
):
    """Bind LLM-gate retries to code, prompt, backend, and runtime contract."""
    prompt_digest = _tg.hashlib.sha256(str(prompt_text).encode("utf-8")).hexdigest()
    harness_identity = (
        prompt_digest
        if harness_identity_override is None
        else str(harness_identity_override)
    )
    if not _tg.re.fullmatch(r"[0-9a-f]{64}", harness_identity):
        raise ValueError("LLM gate harness identity must be a SHA-256 digest")
    ledger = (checkpoint or {}).get("runtime_contract_ledger") or {}
    contract_digest = str(ledger.get("ledger_digest") or "")
    backend_contract = {
        key: _tg.os.environ.get(key, "")
        for key in (
            "ANTHROPIC_MODEL",
            "CLAUDE_MODEL",
            "POK_LLM_MODEL",
            "ANTHROPIC_BASE_URL",
        )
    }
    candidate_fingerprint = _tg._bot_code_fingerprint(candidate_dir)
    source_fingerprint = str(source_fingerprint_override or "")
    if not source_fingerprint:
        source_fingerprint = _tg._bot_code_fingerprint(source_dir)
    attempt_key = _tg.infrastructure_attempt_key(
        component=component,
        candidate_fingerprint=candidate_fingerprint,
        source_fingerprint=source_fingerprint,
        harness_identity=harness_identity,
        contract_identity=contract_digest,
        extra={"role": role, "backend_contract": backend_contract},
    )
    return attempt_key, {
        "role": role,
        "prompt_digest": prompt_digest,
        "attempt_harness_identity": harness_identity,
        "attempt_harness_identity_mode": (
            "stable_override_v1"
            if harness_identity_override is not None
            else "rendered_prompt_sha256_v1"
        ),
        "candidate_fingerprint": candidate_fingerprint,
        "source_fingerprint": source_fingerprint,
        "runtime_contract_ledger_digest": contract_digest,
        "backend_contract": backend_contract,
    }


def _strict_review_infrastructure_harness_identity(call):
    """Bind infra retries to strict semantics while excluding invocation nonce.

    The strict renderer places the random invocation id in provider-visible
    text.  Hashing that final text makes every real provider retry look like a
    new infrastructure identity and resets its bounded retry budget.  The
    durable descriptor already owns a nonce-free generation/context identity;
    use exactly that phase identity plus the Reviewer authority slot instead.
    """

    if not isinstance(call, dict):
        raise ValueError("Strict Reviewer call descriptor is missing")
    slot = str(call.get("slot") or "")
    expected_purpose = f"system_strict_bootstrap_gate:{slot}"
    revision = call.get("checkpoint_revision")
    if (
        slot not in {"review", "review:retry"}
        or call.get("purpose") != expected_purpose
        or call.get("checkpoint_stage") != "quality_passed"
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
    ):
        raise ValueError("Strict Reviewer infrastructure descriptor is invalid")
    for field in ("generation_binding_digest", "context_binding_digest"):
        if not _tg.re.fullmatch(r"[0-9a-f]{64}", str(call.get(field) or "")):
            raise ValueError(
                f"Strict Reviewer infrastructure {field} is invalid"
            )
    return _tg._canonical_digest({
        "schema_version": 1,
        "kind": "strict-reviewer-infrastructure-harness-v1",
        "slot": slot,
        "purpose": expected_purpose,
        "generation_binding_digest": call["generation_binding_digest"],
        "checkpoint_stage": "quality_passed",
        "checkpoint_revision": int(revision),
        "context_binding_digest": call["context_binding_digest"],
    })


def _selected_proposal_quality_evidence(
    master_plan,
    architecture_transition,
    *,
    candidate_dir=None,
):
    """Bind a proposal to a changed live chain and one executed typed check.

    This is mechanism-scope acceptance evidence, not a claim that the prose
    counterfactual was fully executed or that 70-hand strength improved.
    """

    selected_checks = list(
        (architecture_transition or {}).get("selected_dynamic_checks") or []
    )
    # Proposal-bearing generations must always prove their selected falsifier.
    # Deriving ``required`` from the transition output would let a missing or
    # malformed runtime-contract ledger silently turn this acceptance gate off.
    required = bool(
        isinstance(master_plan, dict)
        and isinstance(master_plan.get("proposal_binding"), dict)
    )
    result = {
        "required": required,
        "ok": not required,
        "check_id": "",
        "check_evidence_digest": "",
        "proposal_contract_digest": "",
        "evidence_scope": (
            "reachable_symbol_delta_plus_typed_capability_only;"
            "not_full_counterfactual_or_strength_proof"
        ),
        "reachable_symbol_diff_required": False,
        "reachable_symbol_diff_ok": True,
        "changed_reachable_symbols": [],
        "reachable_symbol_diff_digest": "",
        "errors": [],
    }
    if not required:
        return result
    if not isinstance(master_plan, dict):
        result["errors"] = ["proposal_quality_master_plan_missing"]
        return result
    try:
        from agent_master import (
            _parse_valid_proposal_packet,
            _selected_proposal_binding,
            _source_symbol_ast_digest,
        )

        packet, packet_errors = _parse_valid_proposal_packet(_tg.json.dumps(
            master_plan.get("proposal_ensemble"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ))
    except Exception as exc:
        result["errors"] = [
            f"proposal_quality_packet_validation_error:{type(exc).__name__}:"
            f"{str(exc)[:180]}"
        ]
        return result
    if packet_errors or packet is None:
        result["errors"] = [
            "proposal_quality_packet:" + str(item)
            for item in packet_errors[:20]
        ]
        return result
    selected_id = str(master_plan.get("selected_proposal_id") or "")
    selected = next(
        (
            item
            for item in packet.get("ordered_proposals") or []
            if isinstance(item, dict) and item.get("proposal_id") == selected_id
        ),
        None,
    )
    binding = master_plan.get("proposal_binding")
    if not isinstance(selected, dict) or not isinstance(binding, dict):
        result["errors"] = ["proposal_quality_selected_binding_missing"]
        return result
    expected_binding = _selected_proposal_binding(selected, packet)
    result["proposal_contract_digest"] = expected_binding["contract_digest"]
    errors = []
    if binding.get("selected_proposal_id") != selected_id:
        errors.append("proposal_quality_selected_id_mismatch")
    if binding.get("contract_digest") != expected_binding["contract_digest"]:
        errors.append("proposal_quality_contract_digest_mismatch")
    if binding != expected_binding:
        errors.append("proposal_quality_binding_projection_mismatch")
    strategy_implementation = (
        expected_binding.get("execution_mode") == "strategy_implementation"
    )
    change_symbol = str(expected_binding.get("change_symbol") or "")
    result["reachable_symbol_diff_required"] = strategy_implementation
    if strategy_implementation:
        result["reachable_symbol_diff_ok"] = False
        baseline_rows = (
            (packet.get("proposal_source_symbol_digests") or {}).get(selected_id)
            or {}
        )
        diff_rows = []
        if candidate_dir is None:
            errors.append("proposal_quality_candidate_dir_missing")
        else:
            for symbol in selected.get("reachable_chain") or []:
                baseline_digest = str(baseline_rows.get(symbol) or "")
                candidate_digest = _source_symbol_ast_digest(
                    _tg.Path(candidate_dir),
                    str(symbol),
                )
                if (
                    _tg.re.fullmatch(r"[0-9a-f]{64}", baseline_digest) is None
                    or candidate_digest is None
                ):
                    errors.append(
                        f"proposal_quality_reachable_symbol_missing:{symbol}"
                    )
                    continue
                row = {
                    "symbol": str(symbol),
                    "baseline_ast_sha256": baseline_digest,
                    "candidate_ast_sha256": candidate_digest,
                    "changed": candidate_digest != baseline_digest,
                }
                diff_rows.append(row)
            changed_symbols = [
                row["symbol"] for row in diff_rows if row["changed"]
            ]
            result["changed_reachable_symbols"] = changed_symbols
            change_symbol_changed = change_symbol in changed_symbols
            if change_symbol_changed and len(diff_rows) == len(
                selected.get("reachable_chain") or []
            ):
                from bot_artifact import canonical_digest

                result["reachable_symbol_diff_ok"] = True
                result["reachable_symbol_diff_digest"] = canonical_digest(
                    diff_rows
                )
            else:
                if not changed_symbols:
                    errors.append("proposal_quality_reachable_chain_unchanged")
                if not change_symbol_changed:
                    errors.append(
                        "proposal_quality_change_symbol_unchanged:"
                        + change_symbol
                    )
    check_id = str((selected.get("falsifier") or {}).get("test_name") or "")
    result["check_id"] = check_id
    if check_id not in selected_checks:
        errors.append("proposal_quality_selected_check_not_executed")
    if check_id in set(
        (architecture_transition or {}).get("selected_dynamic_failures") or []
    ):
        errors.append("proposal_quality_selected_check_failed")
    check_row = (
        (((architecture_transition or {}).get("candidate_capabilities") or {})
        .get("checks_by_id") or {})
        .get(check_id)
    )
    if not isinstance(check_row, dict) or check_row.get("passed") is not True:
        errors.append("proposal_quality_selected_check_evidence_missing")
    else:
        from bot_artifact import canonical_digest

        result["check_evidence_digest"] = canonical_digest(check_row)
    result["errors"] = list(dict.fromkeys(errors))
    result["ok"] = not result["errors"]
    return result


async def _finalize_strict_blueprint_quality_rejection(
    *,
    required: bool,
    infrastructure_active: bool,
    all_passed: bool,
    checkpoint,
    result: dict,
) -> dict:
    """Terminate immutable bytes on business failure; preserve infra retries."""

    if not required or infrastructure_active or all_passed:
        return result
    from system_strict_bootstrap import abandon_rejected_blueprint

    quality_gate = _tg.deepcopy(
        (((checkpoint or {}).get("gate_results") or {}).get("quality") or {})
    )
    if not quality_gate:
        return {
            **result,
            "abandoned": False,
            "blocked": True,
            "error": "SYSTEM_STRICT_BOOTSTRAP_QUALITY_GATE_MISSING",
            "directive": (
                "The failed quality gate was not durably projected; preserve "
                "the candidate and checkpoint for operator reconciliation."
            ),
        }

    return await abandon_rejected_blueprint(
        checkpoint,
        reason="system_strict_bootstrap_quality_rejected",
        result={
            **result,
            "failure_class": "quality_gate",
            "terminal_gate_name": "quality",
            "terminal_reason_code": "quality_gate_rejected",
            "terminal_gate_payload": quality_gate,
            "directive": (
                "A real quality gate rejected the immutable strict blueprint; "
                "the tool layer has terminated this generation."
            ),
        },
    )


def _quality_source_dir(source_v, *, numeric_lineage_only: bool):
    """Resolve a real parent only; fresh v143 has numeric lineage instead."""

    if source_v is None or numeric_lineage_only:
        return None
    return _tg.get_bot_dir(source_v)


def _bot_code_fingerprint(bot_dir):
    """Content hash of the complete decision artifact for gate cache validity.

    The persisted field name predates data/model-backed bots, but its value must
    cover every authorized source file that can affect a decision. ``hash_path``
    uses the shared deterministic manifest; completion/control/cache products do
    not contribute identity. Strict layout gates reject executable caches, and
    managed launch mounts only the sealed content-bound source projection.
    """
    root = _tg.Path(bot_dir)
    if not root.exists():
        return ""
    try:
        from bot_artifact import hash_path

        return hash_path(root)
    except Exception:
        # Callers treat an empty fingerprint as unavailable and final commit
        # fails closed.  Do not bless a partial manifest after an I/O race or an
        # unsafe artifact entry.
        return ""


def _transient_task_context_errors(bot_dir):
    """Reject unpublished compiler briefs before quality/certification."""
    from candidate_hygiene import transient_control_artifact_errors

    return transient_control_artifact_errors(bot_dir)


def _record_quality_failure(gen, worker_id, role, error, **extra):
    """Record an identity-bound gate rejection to worker_failures.jsonl.

    RC5: category="gate" separates these strategic rejections from real
    worker-exec failures (_record_worker_failure writes category="worker") so
    the Worker Failures view can filter out the 49 critic / 9 reviewer noise
    and surface only genuine compile/timeout crashes.

    New rows are authorized by the exact current strict checkpoint.  Missing,
    retired, cross-generation, or reset-receipt-incompatible authority raises
    before the JSONL file is opened; historical unbound rows are never upgraded
    from their generation number.
    """
    from checkpoint_schema import (
        CheckpointSchemaError,
        strict_checkpoint_event_identity,
    )
    from evolution_infra import WORKER_FAILURES_FILE, append_locked_jsonl

    identity = strict_checkpoint_event_identity(
        _tg.read_pipeline_checkpoint(),
        expected_gen=gen,
        project_root=_tg.PROJECT_ROOT,
    )
    entry = {
        **identity,
        "worker_id": worker_id,
        "role": role,
        "error": error,
        "timestamp": _tg.time.time(),
        "category": "gate",
    }
    collisions = sorted(set(extra).intersection(entry))
    if collisions:
        raise CheckpointSchemaError(
            [
                "quality_failure_reserved_identity_override:"
                + ",".join(collisions)
            ]
        )
    entry.update(
        {k: v for k, v in extra.items() if v is not None and v is not False}
    )
    append_locked_jsonl(WORKER_FAILURES_FILE, entry)
    return entry


def _idempotency_check(v, source_v, stage_set, gate_name, approval_key="approved",
                       extra_ok_keys=(), directive="", cache_validator=None):
    """Check if a pipeline stage has already been completed; return cached result or None.

    Args:
        v: Bot version.
        source_v: Parent version.
        stage_set: Tuple/list of stage strings that mean "this stage passed".
        gate_name: Key inside gate_results (e.g. "quality", "review", "critic").
        approval_key: The key to check for truthiness (default "approved").
        extra_ok_keys: Additional keys that count as truthy (e.g. ("force_advanced",)).
        directive: Message to include when returning cached result.
        cache_validator: Optional callable receiving the exact complete
            ``(checkpoint, gate)`` pair fetched by this helper.

    Returns:
        An MCP-formatted result dict if the stage already passed, or None.
    """
    ckpt = _tg._matching_checkpoint(v, source_v)
    if not ckpt or ckpt.get("stage") not in stage_set:
        return None
    gate = ckpt.get("gate_results", {}).get(gate_name, {})
    if gate.get(approval_key) is True or any(gate.get(k) is True for k in extra_ok_keys):
        # Cache authority is the complete checkpoint.  Passing only the gate
        # made strict Review/Critic receipts impossible to validate without a
        # synthetic partial checkpoint; capturing an earlier checkpoint in a
        # closure also risked validating generation A after this helper fetched
        # generation B.  Validate the exact checkpoint/gate pair read above.
        if cache_validator is not None and not cache_validator(ckpt, gate):
            return None
        gate["idempotent_cache"] = True
        gate["checkpoint_recorded"] = True
        gate["directive"] = directive
        return _tg._json_tool_result(gate)
    return None
