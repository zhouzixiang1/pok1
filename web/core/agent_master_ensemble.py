"""Master proposal ensemble subsystem for agent_master.

Extracted as a cohesive business cluster; ``agent_master.py`` retains a thin
delegate shell so external ``from agent_master import _run_master_proposal_ensemble``
and ``monkeypatch.setattr(agent_master, "_run_master_proposal_ensemble", ...)``
keep resolving.

Business responsibility (single cohesive domain): the three-proposal /
two-anonymous-criterion-critic ensemble with deterministic veto/order
(``_run_master_proposal_ensemble``) and its nested helpers
(``raise_provider_failure``, ``propose``, ``proposal_actual_role``,
``critique``, plus the call-context / role-result / duplicate-rejection
plumbing that lives inside the ensemble function body).

Cross-references to symbols that remain in ``agent_master`` (the
``_canonical_proposal_primaries`` / ``_source_symbol_graph`` /
``_proposal_packet_error`` / ``_validated_master_proposal`` /
``_validated_proposal_critique`` / ``_record_master_invocation_evidence`` /
``_master_proposal_projection_hints`` / ``_proposal_source_symbol_digests`` /
``_snapshot_reference_prompt_index`` / ``_source_symbol_prompt_index``
validation helpers, the ``run_claude_query`` / ``get_bot_dir``
evolution-infra helpers, the ``gather_llm_fail_fast`` / ``LLMAvailabilityBlocked``
llm-availability helpers, the ``bot_name`` bot-namespace helper, and the
``MasterEnsembleInfrastructureParked`` / ``MasterInfrastructureError``
agent-master-errors sentinels) are reached through ``_am.<name>`` so that
test monkeypatches on ``agent_master.<name>`` propagate.

CRITICAL (wave-3 lesson): EVERY intra-companion call to a moved symbol ALSO
routes through ``_am.<name>(...)`` so monkeypatches on ``agent_master.<name>``
propagate even when both call sites now live in this companion.
``_run_master_proposal_ensemble`` is async; callers ``await`` its delegate.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path

import agent_master as _am  # for cross-refs

_CHANGE_SYMBOL_IN_OUTPUT_RE = re.compile(
    r'"change_symbol"\s*:\s*"(policy\.py:[A-Za-z_][A-Za-z0-9_]*)"'
)


def _extract_change_symbol_from_output(raw_output: object) -> str | None:
    """Best-effort last change_symbol in a rejected raw proposal output.

    Used to PIN a schema-retry to its original target: v187's schema retry
    silently switched the scout's target from _bluff_allowed to
    _refinement_prior_equity — the 'independent scout' output was
    effectively authored under retry pressure."""
    found = _CHANGE_SYMBOL_IN_OUTPUT_RE.findall(str(raw_output or ""))
    return found[-1] if found else None

_log = logging.getLogger("pok.master")


async def _run_master_proposal_ensemble(
    planning_context: str,
    *,
    source_v: int,
    next_v: int,
    ui,
    log_dir: Path,
    allowed_evidence_snapshot_dir: str,
    baseline_v: int | None = None,
    protocol_bootstrap_prepared_only: bool = False,
    singleton_no_strength: bool = False,
    strict_checkpoint: dict | None = None,
    allowed_primaries: tuple[str, ...] | None = None,
) -> str:
    """Three proposals, two anonymous criterion critics, deterministic veto/order.

    The ballots cannot alter lineage, evidence cutoffs, executable literals,
    or gates.  Their only blocking authority is a two-ballot rejection veto;
    the final plan still passes the canonical schema/compiler/validator path.
    """
    import asyncio

    if protocol_bootstrap_prepared_only and singleton_no_strength:
        raise ValueError("Master proposal planning mode is ambiguous")
    allowed_primaries = _am._canonical_proposal_primaries(allowed_primaries)

    context_digest = hashlib.sha256(planning_context.encode("utf-8")).hexdigest()
    try:
        baseline_dir = _am.get_bot_dir(
            int(baseline_v) if baseline_v is not None else int(source_v)
        )
        source_graph, source_code_digest = _am._source_symbol_graph(baseline_dir)
    except Exception as exc:
        return _am._proposal_packet_error(
            f"source_symbol_index_failed:{type(exc).__name__}:{str(exc)[:240]}",
            context_digest=context_digest,
        )
    if not source_graph:
        return _am._proposal_packet_error(
            "source_symbol_index_empty",
            context_digest=context_digest,
        )
    no_strength_snapshot = bool(
        protocol_bootstrap_prepared_only or singleton_no_strength
    )
    snapshot_dir = (
        None if no_strength_snapshot else Path(allowed_evidence_snapshot_dir)
    )
    require_snapshot_evidence = not no_strength_snapshot
    proposal_execution_mode = (
        "fixed_blueprint_capability_audit"
        if protocol_bootstrap_prepared_only
        else "strategy_implementation"
    )
    evidence_mode = (
        "fresh_strict_control_no_strength"
        if protocol_bootstrap_prepared_only
        else "singleton_parent_no_strength"
        if singleton_no_strength
        else "frozen_strength_snapshot"
    )
    source_symbol_index = _am._source_symbol_prompt_index(source_graph)
    if require_snapshot_evidence:
        snapshot_reference_index = _am._snapshot_reference_prompt_index(snapshot_dir)
        if not snapshot_reference_index:
            return _am._proposal_packet_error(
                "snapshot_reference_index_empty",
                context_digest=context_digest,
                source_code_digest=source_code_digest,
            )
        source_symbol_index += "\n\n" + snapshot_reference_index
    # Every no-strength protocol-bootstrap generation has a checkpoint-owned
    # identity and must retain successful Scout/Ballot work across transport
    # retries and process restarts. Restricting the durable authority journal
    # to the one-time fresh v143 bootstrap caused singleton successors to lose
    # two valid Scout results whenever the third provider stalled.
    strict_authority_enabled = (
        (protocol_bootstrap_prepared_only or singleton_no_strength)
        and isinstance(strict_checkpoint, dict)
    )

    def raise_provider_failure(
        phase: str,
        role_name: str,
        error: BaseException,
        *,
        slot: str,
    ) -> None:
        issue = (
            f"{phase}:{role_name}:"
            f"{type(error).__name__}:{str(error)[:300]}"
        )
        if strict_authority_enabled:
            from strict_authority_workflow import master_provider_retry_state

            retry_state = master_provider_retry_state(
                strict_checkpoint,
                failed_slot=slot,
            )
            # Only a fenced provider effect may enter the attempt-neutral park
            # path. Renderer/log/local control errors that occur before an
            # exact dispatch have no durable provider-failure proof and must
            # retain the ordinary bounded infrastructure classification.
            if not retry_state.get("failed_effect_ids"):
                raise _am.MasterInfrastructureError(
                    source_v,
                    next_v,
                    context_digest,
                    issue,
                ) from error
            raise _am.MasterEnsembleInfrastructureParked(
                source_v,
                next_v,
                context_digest,
                issue,
                slot=slot,
                retry_state=retry_state,
            ) from error
        raise _am.MasterInfrastructureError(
            source_v,
            next_v,
            context_digest,
            issue,
        ) from error
    proposal_read_dirs = (
        [_am.get_bot_dir(int(next_v))]
        if protocol_bootstrap_prepared_only
        else [_am.get_bot_dir(int(source_v)), _am.get_bot_dir(int(next_v))]
    )

    async def propose(
        direction: str,
        directive: str,
        *,
        repair: dict | None = None,
    ):
        strict_call = None
        if strict_authority_enabled:
            from strict_authority_workflow import new_call, proposal_call_context

            strict_call = new_call(
                strict_checkpoint,
                slot=f"proposal:{direction}",
                context_binding=proposal_call_context(
                    context_digest=context_digest,
                    source_code_digest=source_code_digest,
                    direction=direction,
                    allowed_primaries=allowed_primaries,
                    # Preserve byte-for-byte compatibility with published
                    # fresh-v143 receipts. Only the newly admitted singleton
                    # successor projection needs an explicit mode marker.
                    evidence_mode=(
                        "singleton_parent_no_strength"
                        if singleton_no_strength
                        else None
                    ),
                ),
            )
            invocation_id = strict_call["invocation_id"]
            if repair is None and strict_call.get("replay_provider"):
                replay_role = str(strict_call.get("actual_role") or "")
                base_role = f"MASTER PROPOSAL {direction}"
                if replay_role == base_role + " DISTINCTNESS RETRY":
                    repair = {"kind": "distinctness"}
                elif replay_role == base_role + " SCHEMA RETRY":
                    repair = {"kind": "schema"}
                elif replay_role != base_role:
                    raise RuntimeError(
                        "strict_proposal_replay_role_invalid:"
                        f"{direction}:{replay_role}"
                    )
            if repair is None and strict_call.get("schema_retry_required"):
                prior_rejection = strict_call.get("prior_schema_rejection") or {}
                repair = {
                    "kind": (
                        "distinctness"
                        if prior_rejection.get("rejection_kind")
                        == "proposal_identity_collision"
                        else "schema"
                    ),
                    "projection_hints": list(
                        prior_rejection.get("projection_errors") or ()
                    ),
                }
                _log.info(
                    "Master proposal %s schema retry triggered: "
                    "rejection_kind=%s, projection_errors=%s",
                    direction,
                    prior_rejection.get("rejection_kind"),
                    list(prior_rejection.get("projection_errors") or ()),
                )
        else:
            from system_strict_bootstrap import new_llm_invocation_id

            invocation_id = new_llm_invocation_id()
        repair_kind = str((repair or {}).get("kind") or "")
        projection_hints = list((repair or {}).get("projection_hints") or ())
        is_repair = bool(repair_kind)
        is_distinctness_repair = repair_kind == "distinctness"
        purpose = f"master_proposal_scout:{direction}"
        log_basename = (
            f"master_proposal_{direction}_{'distinctness' if is_distinctness_repair else 'schema'}_retry_io.txt"
            if is_repair
            else f"master_proposal_{direction}_io.txt"
        )
        log_file = log_dir / log_basename
        if strict_call is not None:
            from strict_authority_workflow import strict_invocation_log_path

            log_file = strict_invocation_log_path(
                strict_call,
                logs_dir=log_dir,
                basename=log_basename,
            )
        retry_label = (
            " DISTINCTNESS RETRY"
            if is_distinctness_repair
            else " SCHEMA RETRY" if is_repair else ""
        )
        proposal_role = f"MASTER PROPOSAL {direction}{retry_label}"
        from llm_query import render_llm_prompt

        rendered_prompt = render_llm_prompt(
            proposal_role,
            producer=_am._render_master_proposal_provider_prompt,
            renderer_inputs={
                "planning_context": planning_context,
                "direction": str(direction),
                "directive": str(directive),
                "source_v": int(source_v),
                "next_v": int(next_v),
                "protocol_bootstrap_prepared_only": bool(
                    protocol_bootstrap_prepared_only
                ),
                "singleton_no_strength": bool(singleton_no_strength),
                "source_symbol_index": source_symbol_index,
                "repair_kind": repair_kind,
                "projection_hints": projection_hints,
                "allowed_primaries": list(allowed_primaries or ()),
                "invocation_id": str(invocation_id),
            },
        )
        output, cost_usd, usage = await _am.run_claude_query(
            rendered_prompt,
            [],
            ui,
            proposal_role,
            log_file,
            tools=["Read"],
            allowed_evidence_snapshot_dir=allowed_evidence_snapshot_dir,
            allowed_read_dirs=proposal_read_dirs,
            strict_authority=strict_call,
        )
        return {
            "output": output,
            "cost_usd": cost_usd,
            "usage": usage,
            "invocation_id": invocation_id,
            "purpose": purpose,
            "role": (
                f"MASTER PROPOSAL {direction}"
                f"{retry_label}"
            ),
            "prompt": str(rendered_prompt),
            "log_file": str(log_file),
            "strict_call": strict_call,
        }

    proposal_results = await _am.gather_llm_fail_fast(
        *(propose(direction, directive) for direction, directive in _am._MASTER_PROPOSAL_DIRECTIONS),
    )
    proposals = []
    proposal_invocations: dict[str, dict] = {}
    seen_proposal_ids: set[str] = set()
    seen_change_symbols: dict[str, str] = {}
    proposal_provider_errors: list[tuple[str, BaseException]] = []
    invalid_proposal_specs: list[tuple[str, str, dict]] = []
    accepted_proposal_directions: dict[str, str] = {}

    def proposal_actual_role(result: object) -> str | None:
        if not isinstance(result, dict):
            return None
        strict_call = result.get("strict_call")
        if isinstance(strict_call, dict):
            # The journal-bound dispatched role is authority.  A missing value
            # may not fall back to a caller-supplied result label.
            return str(strict_call.get("actual_role") or "") or None
        return str(result.get("role") or "") or None

    for (direction, _directive), result in zip(_am._MASTER_PROPOSAL_DIRECTIONS, proposal_results):
        if isinstance(result, BaseException):
            from strict_authority_workflow import StrictAuthorityError

            if isinstance(result, StrictAuthorityError):
                raise result
            proposal_provider_errors.append((direction, result))
            continue
        output = result.get("output", "") if isinstance(result, dict) else ""
        proposal = _am._validated_master_proposal(
            output,
            direction,
            source_graph=source_graph,
            snapshot_dir=snapshot_dir,
            national_policy_only=True,
            require_snapshot_evidence=require_snapshot_evidence,
            execution_mode=proposal_execution_mode,
            evidence_mode=evidence_mode,
            expected_measurement_target=(
                _am.bot_name(int(source_v)) if singleton_no_strength else None
            ),
            forbidden_measurement_target=(
                _am.bot_name(int(next_v)) if require_snapshot_evidence else None
            ),
            allowed_primaries=allowed_primaries,
            actual_role=proposal_actual_role(result),
        )
        if proposal is None:
            repair = {"kind": "schema"}
            # Pin the schema retry to its original target family so repair
            # pressure cannot silently redirect the direction (v187).
            pinned_symbol = _extract_change_symbol_from_output(output)
            if pinned_symbol:
                repair["pinned_change_symbol"] = pinned_symbol
            repair["projection_hints"] = (
                _am._master_proposal_projection_hints(
                    output,
                    source_graph=source_graph,
                    snapshot_dir=snapshot_dir,
                    national_policy_only=True,
                    require_snapshot_evidence=require_snapshot_evidence,
                    evidence_mode=evidence_mode,
                    allowed_primaries=allowed_primaries,
                )
                or ["proposal_contract_invalid"]
            )
            if pinned_symbol:
                repair["projection_hints"] = list(repair["projection_hints"]) + [
                    f"schema_retry_keep_change_symbol.{pinned_symbol}"
                ]
            _log.warning(
                "Master proposal %s rejected (attempt 1): %s",
                direction,
                repair.get("projection_hints", []) if isinstance(repair, dict) else [],
            )
            invalid_proposal_specs.append(
                (direction, _directive, repair)
            )
            continue
        proposal_id = proposal["proposal_id"]
        proposal_symbol = str(proposal.get("change_symbol") or "")
        if proposal_id in seen_proposal_ids:
            if strict_authority_enabled:
                from strict_authority_workflow import reject_duplicate_proposal

                reject_duplicate_proposal(result["strict_call"])
            invalid_proposal_specs.append((
                direction,
                _directive,
                {
                    "kind": "distinctness",
                    "proposal_id": proposal_id,
                    "conflicting_direction": accepted_proposal_directions[
                        proposal_id
                    ],
                    "avoid_change_symbols": [
                        s for s in (proposal_symbol,) if s
                    ],
                },
            ))
            continue
        # Within-ensemble duplicate target (v172/v186/v187: scout triples on
        # the SAME symbol — one direction reworded three ways). Route to the
        # distinctness repair so the retry must pick a different symbol.
        if proposal_symbol and proposal_symbol in seen_change_symbols:
            if strict_authority_enabled:
                from strict_authority_workflow import reject_duplicate_proposal

                reject_duplicate_proposal(result["strict_call"])
            _log.warning(
                "Master proposal %s rejected: change_symbol %s already "
                "claimed by direction %s (ensemble must stay distinct)",
                direction,
                proposal_symbol,
                seen_change_symbols[proposal_symbol],
            )
            invalid_proposal_specs.append((
                direction,
                _directive,
                {
                    "kind": "distinctness",
                    "proposal_id": proposal_id,
                    "change_symbol": proposal_symbol,
                    "conflicting_direction": seen_change_symbols[proposal_symbol],
                    "avoid_change_symbols": [proposal_symbol],
                },
            ))
            continue
        if strict_authority_enabled:
            from strict_authority_workflow import accept_role_result

            accept_role_result(
                result["strict_call"],
                role_result=proposal,
                parse_contract=_am._PROPOSAL_SCHEMA_VERSION,
            )

        proposal_invocations[proposal_id] = _am._record_master_invocation_evidence(
            result,
            output=output,
            role_result=proposal,
        )
        seen_proposal_ids.add(proposal_id)
        if proposal_symbol:
            seen_change_symbols[proposal_symbol] = direction
        accepted_proposal_directions[proposal_id] = direction
        proposals.append(proposal)
    if proposal_provider_errors:
        direction, error = proposal_provider_errors[0]
        raise_provider_failure(
            "proposal_scout",
            direction,
            error,
            slot=f"proposal:{direction}",
        )
    if invalid_proposal_specs:
        retry_results = await _am.gather_llm_fail_fast(
            *(
                propose(direction, directive, repair=repair)
                for direction, directive, repair in invalid_proposal_specs
            ),
        )
        retry_provider_errors: list[tuple[str, BaseException]] = []
        for (direction, _directive, repair), result in zip(
            invalid_proposal_specs, retry_results
        ):
            if isinstance(result, _am.LLMAvailabilityBlocked):
                raise result
            if isinstance(result, BaseException):
                from strict_authority_workflow import StrictAuthorityError

                if isinstance(result, StrictAuthorityError):
                    raise result
                retry_provider_errors.append((direction, result))
                continue
            output = result.get("output", "") if isinstance(result, dict) else ""
            proposal = _am._validated_master_proposal(
                output,
                direction,
                source_graph=source_graph,
                snapshot_dir=snapshot_dir,
                national_policy_only=True,
                require_snapshot_evidence=require_snapshot_evidence,
                execution_mode=proposal_execution_mode,
                evidence_mode=evidence_mode,
                expected_measurement_target=(
                    _am.bot_name(int(source_v)) if singleton_no_strength else None
                ),
                forbidden_measurement_target=(
                    _am.bot_name(int(next_v)) if require_snapshot_evidence else None
                ),
                allowed_primaries=allowed_primaries,
                actual_role=proposal_actual_role(result),
            )
            if proposal is None:
                fresh_hints = (
                    _am._master_proposal_projection_hints(
                        output,
                        source_graph=source_graph,
                        snapshot_dir=snapshot_dir,
                        national_policy_only=True,
                        require_snapshot_evidence=require_snapshot_evidence,
                        evidence_mode=evidence_mode,
                        allowed_primaries=allowed_primaries,
                    )
                    or ["proposal_contract_invalid"]
                )
                _log.warning(
                    "Master proposal %s rejected (attempt 2): hints=%s; "
                    "retry_was_based_on=%s",
                    direction,
                    fresh_hints,
                    (repair or {}).get("projection_hints", [])
                    if isinstance(repair, dict)
                    else [],
                )
                continue
            proposal_id = proposal["proposal_id"]
            proposal_symbol = str(proposal.get("change_symbol") or "")
            if proposal_id in seen_proposal_ids:
                if strict_authority_enabled:
                    from strict_authority_workflow import reject_duplicate_proposal

                    reject_duplicate_proposal(result["strict_call"])
                continue
            # Retry pinning: a schema repair must keep its original target
            # family (v187's retry silently switched symbols); a distinctness
            # repair must avoid the symbol that caused the conflict.
            pinned_symbol = (
                str(repair.get("pinned_change_symbol") or "")
                if isinstance(repair, dict) else ""
            )
            avoid_symbols = (
                [str(s) for s in (repair.get("avoid_change_symbols") or [])]
                if isinstance(repair, dict) else []
            )
            if pinned_symbol and proposal_symbol != pinned_symbol:
                if strict_authority_enabled:
                    from strict_authority_workflow import reject_duplicate_proposal

                    reject_duplicate_proposal(result["strict_call"])
                _log.warning(
                    "Master proposal %s schema retry switched target "
                    "%s -> %s; rejected (retry must keep its symbol)",
                    direction, pinned_symbol, proposal_symbol or "?",
                )
                continue
            if proposal_symbol and proposal_symbol in avoid_symbols:
                if strict_authority_enabled:
                    from strict_authority_workflow import reject_duplicate_proposal

                    reject_duplicate_proposal(result["strict_call"])
                _log.warning(
                    "Master proposal %s distinctness retry reused the "
                    "conflicting symbol %s; rejected",
                    direction, proposal_symbol,
                )
                continue
            if proposal_symbol and proposal_symbol in seen_change_symbols:
                if strict_authority_enabled:
                    from strict_authority_workflow import reject_duplicate_proposal

                    reject_duplicate_proposal(result["strict_call"])
                _log.warning(
                    "Master proposal %s retry collided on change_symbol %s "
                    "(claimed by direction %s); rejected",
                    direction, proposal_symbol,
                    seen_change_symbols[proposal_symbol],
                )
                continue
            if strict_authority_enabled:
                from strict_authority_workflow import accept_role_result

                accept_role_result(
                    result["strict_call"],
                    role_result=proposal,
                    parse_contract=_am._PROPOSAL_SCHEMA_VERSION,
                )

            proposal_invocations[proposal_id] = _am._record_master_invocation_evidence(
                result,
                output=output,
                role_result=proposal,
            )
            seen_proposal_ids.add(proposal_id)
            if proposal_symbol:
                seen_change_symbols[proposal_symbol] = direction
            accepted_proposal_directions[proposal_id] = direction
            proposals.append(proposal)
        if retry_provider_errors:
            direction, error = retry_provider_errors[0]
            raise_provider_failure(
                "proposal_scout_repair",
                direction,
                error,
                slot=f"proposal:{direction}",
            )
    if len(proposals) != len(_am._MASTER_PROPOSAL_DIRECTIONS):
        _log.error(
            "Master ensemble insufficient: got %d valid proposals, need %d. "
            "Rejected directions: %s",
            len(proposals),
            len(_am._MASTER_PROPOSAL_DIRECTIONS),
            [
                {"direction": d, "hints": r.get("projection_hints", [])}
                for d, _, r in invalid_proposal_specs
            ],
        )
        return _am._proposal_packet_error(
            "three_distinct_schema_valid_scout_proposals_required:"
            f"got_{len(proposals)}",
            context_digest=context_digest,
            source_code_digest=source_code_digest,
        )
    try:
        proposal_source_symbol_digests = _am._proposal_source_symbol_digests(
            proposals,
            baseline_dir,
        )
    except Exception as exc:
        return _am._proposal_packet_error(
            "proposal_source_symbol_digest_failed:"
            f"{type(exc).__name__}:{str(exc)[:240]}",
            context_digest=context_digest,
            source_code_digest=source_code_digest,
        )

    async def critique(name: str, lens: str, *, schema_retry: bool = False):
        strict_call = None
        if strict_authority_enabled:
            from strict_authority_workflow import ballot_call_context, new_call

            strict_call = new_call(
                strict_checkpoint,
                slot=f"ballot:{name}",
                context_binding=ballot_call_context(
                    context_digest=context_digest,
                    source_code_digest=source_code_digest,
                    critic_id=name,
                    proposal_ids=(item["proposal_id"] for item in proposals),
                    critic_criteria=_am._PROPOSAL_CRITIC_CRITERIA,
                ),
            )
            invocation_id = strict_call["invocation_id"]
            if strict_call.get("replay_provider"):
                replay_role = str(strict_call.get("actual_role") or "")
                base_role = f"MASTER PROPOSAL CRITIC {name}"
                if replay_role == base_role + " SCHEMA RETRY":
                    schema_retry = True
                elif replay_role != base_role:
                    raise RuntimeError(
                        "strict_ballot_replay_role_invalid:"
                        f"{name}:{replay_role}"
                    )
            elif strict_call.get("schema_retry_required"):
                schema_retry = True
                _log.info(
                    "Master proposal critic %s schema retry triggered",
                    name,
                )
        else:
            from system_strict_bootstrap import new_llm_invocation_id

            invocation_id = new_llm_invocation_id()
        # No scout lens/identity is exposed.  Each critic receives a different
        # but replayable ordering derived from the immutable planning digest.
        critic_proposals = [
            {key: value for key, value in proposal.items() if key != "direction"}
            for proposal in proposals
        ]
        critic_proposals.sort(
            key=lambda item: hashlib.sha256(
                f"{context_digest}:{name}:{item['proposal_id']}".encode("utf-8")
            ).hexdigest()
        )
        purpose = f"master_proposal_critic:{name}"
        log_basename = (
            f"master_proposal_critic_{name}_schema_retry_io.txt"
            if schema_retry
            else f"master_proposal_critic_{name}_io.txt"
        )
        log_file = log_dir / log_basename
        if strict_call is not None:
            from strict_authority_workflow import strict_invocation_log_path

            log_file = strict_invocation_log_path(
                strict_call,
                logs_dir=log_dir,
                basename=log_basename,
            )
        critic_role = (
            f"MASTER PROPOSAL CRITIC {name}"
            f"{' SCHEMA RETRY' if schema_retry else ''}"
        )
        from llm_query import render_llm_prompt

        rendered_prompt = render_llm_prompt(
            critic_role,
            producer=_am._render_master_proposal_critic_provider_prompt,
            renderer_inputs={
                "proposal_name": str(name),
                "lens": str(lens),
                "planning_context_digest": context_digest,
                "proposals": critic_proposals,
                "criteria": _am._PROPOSAL_CRITIC_CRITERIA,
                "evidence_mode": evidence_mode,
                "schema_retry": bool(schema_retry),
                "invocation_id": str(invocation_id),
            },
        )
        output, cost_usd, usage = await _am.run_claude_query(
            rendered_prompt,
            [],
            ui,
            critic_role,
            log_file,
            tools=[],
            strict_authority=strict_call,
        )
        return {
            "output": output,
            "cost_usd": cost_usd,
            "usage": usage,
            "invocation_id": invocation_id,
            "purpose": purpose,
            "role": (
                f"MASTER PROPOSAL CRITIC {name}"
                f"{' SCHEMA RETRY' if schema_retry else ''}"
            ),
            "prompt": str(rendered_prompt),
            "log_file": str(log_file),
            "critic_id": name,
            "strict_call": strict_call,
        }

    critic_results = await _am.gather_llm_fail_fast(
        critique("falsification", "Counterfactual quality, causal attribution, and evidence support."),
        critique("scope", "Reachability, bounded implementation scope, and regression risk."),
    )
    proposal_ids = {item["proposal_id"] for item in proposals}
    critiques = []
    invalid_critics = []
    critic_provider_errors: list[tuple[str, BaseException]] = []
    critic_specs = (
        ("falsification", "Counterfactual quality, causal attribution, and evidence support."),
        ("scope", "Reachability, bounded implementation scope, and regression risk."),
    )
    for spec, result in zip(critic_specs, critic_results):
        if isinstance(result, BaseException):
            from strict_authority_workflow import StrictAuthorityError

            if isinstance(result, StrictAuthorityError):
                raise result
            critic_provider_errors.append((spec[0], result))
            continue
        output = result.get("output", "") if isinstance(result, dict) else ""
        critique_row = _am._validated_proposal_critique(output, proposal_ids)
        if critique_row is not None:
            critique_row["critic_id"] = result["critic_id"]
            if strict_authority_enabled:
                from strict_authority_workflow import accept_role_result

                accept_role_result(
                    result["strict_call"],
                    role_result={
                        key: value
                        for key, value in critique_row.items()
                        if key not in {"critic_id", "invocation_evidence"}
                    },
                    parse_contract="master-proposal-ballot-v1",
                )
            critique_row["invocation_evidence"] = (
                _am._record_master_invocation_evidence(
                    result,
                    output=output,
                    role_result={
                        key: value
                        for key, value in critique_row.items()
                        if key not in {"critic_id", "invocation_evidence"}
                    },
                )
            )
            critiques.append(critique_row)
        else:
            invalid_critics.append(spec)

    if critic_provider_errors:
        critic_id, error = critic_provider_errors[0]
        raise_provider_failure(
            "proposal_critic",
            critic_id,
            error,
            slot=f"ballot:{critic_id}",
        )
    if invalid_critics:
        retry_results = await _am.gather_llm_fail_fast(
            *(
                critique(name, lens, schema_retry=True)
                for name, lens in invalid_critics
            ),
        )
        retry_critic_provider_errors: list[tuple[str, BaseException]] = []
        for (critic_id, _lens), result in zip(invalid_critics, retry_results):
            if isinstance(result, _am.LLMAvailabilityBlocked):
                raise result
            if isinstance(result, BaseException):
                from strict_authority_workflow import StrictAuthorityError

                if isinstance(result, StrictAuthorityError):
                    raise result
                retry_critic_provider_errors.append((critic_id, result))
                continue
            output = result.get("output", "") if isinstance(result, dict) else ""
            critique_row = _am._validated_proposal_critique(output, proposal_ids)
            if critique_row is not None:
                critique_row["critic_id"] = result["critic_id"]
                if strict_authority_enabled:
                    from strict_authority_workflow import accept_role_result

                    accept_role_result(
                        result["strict_call"],
                        role_result={
                            key: value
                            for key, value in critique_row.items()
                            if key not in {"critic_id", "invocation_evidence"}
                        },
                        parse_contract="master-proposal-ballot-v1",
                    )
                critique_row["invocation_evidence"] = (
                    _am._record_master_invocation_evidence(
                        result,
                        output=output,
                        role_result={
                            key: value
                            for key, value in critique_row.items()
                            if key not in {"critic_id", "invocation_evidence"}
                        },
                    )
                )
                critiques.append(critique_row)
        if retry_critic_provider_errors:
            critic_id, error = retry_critic_provider_errors[0]
            raise_provider_failure(
                "proposal_critic_repair",
                critic_id,
                error,
                slot=f"ballot:{critic_id}",
            )

    if len(critiques) != 2:
        return _am._proposal_packet_error(
            f"expected_two_schema_valid_critics_got_{len(critiques)}",
            context_digest=context_digest,
            source_code_digest=source_code_digest,
        )

    # Deterministic equal-criterion aggregation. Critic prose cannot create a
    # candidate. Two independent schema-valid rejects form a narrow veto so
    # final Master cannot resurrect a proposal both ballots found concretely
    # unfalsifiable, ungrounded, or strategically irrelevant.
    order = {item["proposal_id"]: index for index, item in enumerate(proposals)}
    scores = {proposal_id: 0 for proposal_id in proposal_ids}
    rejects = {proposal_id: 0 for proposal_id in proposal_ids}
    for critique_row in critiques:
        for ballot in critique_row["ballots"]:
            scores[ballot["proposal_id"]] += ballot["total_score"]
        for proposal_id in critique_row["reject"]:
            rejects[proposal_id] += 1
    proposals.sort(
        key=lambda item: (
            rejects[item["proposal_id"]] >= 2,
            -scores[item["proposal_id"]],
            order[item["proposal_id"]],
        )
    )
    allowed_proposal_ids = [
        item["proposal_id"]
        for item in proposals
        if rejects[item["proposal_id"]] < 2
    ]
    if not allowed_proposal_ids:
        return _am._proposal_packet_error(
            "all_three_proposals_unanimously_rejected",
            context_digest=context_digest,
            source_code_digest=source_code_digest,
        )
    packet = {
        "schema_version": _am._PROPOSAL_PACKET_SCHEMA_VERSION,
        "valid": True,
        "authority": (
            "ballots_rank_and_unanimous_reject_vetoes; final Master chooses among "
            "remaining IDs under frozen lineage/evidence and canonical "
            "runtime/schema/gate contracts"
        ),
        "context_digest": context_digest,
        "source_code_digest": source_code_digest,
        "evidence_mode": evidence_mode,
        "critic_criteria": _am._PROPOSAL_CRITIC_CRITERIA,
        "proposal_count": len(proposals),
        "valid_critic_count": len(critiques),
        "allowed_proposal_ids": allowed_proposal_ids,
        "ordered_proposals": proposals,
        "proposal_source_symbol_digests": proposal_source_symbol_digests,
        "proposal_invocations": proposal_invocations,
        "critic_reviews": critiques,
    }
    return json.dumps(
        packet,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


