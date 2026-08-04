"""Cycle-body companion to ``orchestrator._run_one_cycle`` (phase-decomposed).

The 1709-line ``_run_one_cycle`` body was split into two contiguous module-level
phase sub-functions orchestrated by the thin ``_run_one_cycle`` wrapper that
remains in ``orchestrator.py``:

- ``_cycle_phase_a_setup``         : prompt render + checkpoint observation +
                                      cost-policy gate + ClaudeAgentOptions
                                      build + per-cycle state init. Returns
                                      either an early-exit cost (bare value) or
                                      a 1-tuple ``(ctx,)`` carrying the shared
                                      session context dict.
- ``_cycle_phase_b_stream_session``: the provider streaming ``with``/``try``/
                                      ``except`` block -- ``_stream_response``
                                      (tool-result binding + actionable-stage
                                      handoff detection), the signature-retry /
                                      cycle-timeout / 529-rate-limit / 429-quota
                                      dispatch, and every exception handler.
                                      Returns either an early-exit cost (bare
                                      value) or a result dict for the cycle-level
                                      final cost accounting.

Continuation protocol (mirrors ``tool_planning_worker_phases``): every historic
``return`` inside the moved body is preserved VERBATIM. A bare non-dict return is
an early exit; a ``dict`` return is the continuation signal that hands the final
``{total_cost, cycle_completed, auth_error, cycle_failed, infra_error,
shutdown_cancelled}`` state back to ``_run_one_cycle`` so its final cost-account
block (which stays in ``orchestrator.py``) can compute the cycle return.

``_orch`` is the parent ``orchestrator`` module. All moved code reaches module
globals (helpers, constants, exception classes, SDK types, cost sentinels) via
``_orch.<name>`` so that test monkeypatches on ``orchestrator.<name>`` keep
working at call time -- exactly the pattern used by
``tool_planning_master_ensemble`` and ``tool_planning_worker_phases``. The
``_render_orchestrator_provider_prompt`` producer stays in ``orchestrator.py``
and is reached here as ``_orch._render_orchestrator_provider_prompt`` (the LLM
role-contract registry binds the producer_file to that path).
"""

from __future__ import annotations

import orchestrator as _orch

async def _cycle_phase_a_setup(ui, log_file, one_gen, dry_run, max_turns,
                               gen_ctx, _cost_policy):
    """Phase A: prompt render + checkpoint observation + cost-policy gate +
    options/state init. Returns (ctx,) to continue, or a bare cost to early-exit."""
    """Run one Orchestrator cycle (one LLM agent session). Returns total cost."""
    _orch.set_cycle_start_time(_orch.time.time())
    context = _orch._build_context(one_gen=one_gen, dry_run=dry_run, gen_ctx=gen_ctx)
    from llm_query import render_llm_prompt, _llm_thinking_options

    rendered_prompt = render_llm_prompt(
        "Orchestrator",
        producer=_orch._render_orchestrator_provider_prompt,
        renderer_inputs={"context": context, "dry_run": bool(dry_run)},
        mcp_servers={"evolution": _orch.evolution_server},
    )

    # Orchestrator owns a streaming MCP session and therefore cannot delegate
    # transport to ``run_claude_query``.  It still consumes the same fail-closed
    # role registry and receives the same final provider prompt boundary as all
    # sub-agent roles.  The only provider-visible capability is the typed
    # evolution MCP server; built-in filesystem/shell tools remain absent.
    prompt, _orchestrator_role_contract = _orch.bind_llm_role_provider_prompt(
        rendered_prompt,
        "Orchestrator",
        tools=[],
        provider_path="orchestrator_sdk",
        mcp_servers={"evolution": _orch.evolution_server},
        model="sonnet",
    )

    # Pipeline recovery is checkpoint-driven.  Provider session IDs are opaque
    # server-side history capabilities and are never loaded into SDK ``resume``.
    checkpoint_observation = _orch._pipeline_checkpoint_observation()
    if checkpoint_observation.get("error"):
        issue = str(checkpoint_observation["error"])
        msg = (
            "Refusing to open an Orchestrator provider stream because "
            f"checkpoint authority is unreadable or invalid: {issue}."
        )
        if ui:
            ui.log_history(msg, "error")
            ui.set_status("Stopped: checkpoint authority invalid", is_working=False)
        _orch.log.error(msg)
        try:
            _orch.log_system_event(
                "orchestrator.checkpoint_authority_blocked",
                "error",
                msg,
                {
                    "issue": issue,
                    "checkpoint_path_exists": checkpoint_observation.get(
                        "path_exists"
                    ),
                },
            )
        except Exception:
            pass
        return _orch.ORCH_RECOVERY_BLOCKED_COST
    checkpoint = checkpoint_observation.get("checkpoint")
    baseline_checkpoint = (
        _orch.json.loads(_orch.json.dumps(checkpoint))
        if isinstance(checkpoint, dict)
        else None
    )
    baseline_checkpoint_identity = _orch._checkpoint_actionable_identity(checkpoint)
    baseline_owned_route_identity = _orch._checkpoint_stream_owned_route_identity(
        checkpoint
    )
    _orch._bind_generation_cost_runtime(
        checkpoint,
        gen_ctx=gen_ctx,
        ui=ui,
        policy=_cost_policy,
    )
    try:
        _orch._check_generation_cost_policy(ui)
    except _orch.OperatorGenerationCostLimitExceeded:
        _orch._clear_orchestrator_session(reason="operator_generation_cost_limit")
        if ui:
            ui.set_status("Stopped: operator generation cost limit", is_working=False)
        return _orch.ORCH_OPERATOR_COST_LIMIT_COST
    _orch._load_orchestrator_session()  # removes any pre-policy legacy sidecar

    from evolution_core import _BLOCKED_MCP_TOOLS
    # P1 (2026-06-29): merge PreCompact hook (state preservation) with the
    # bot_dir_guard PreToolUse hook (blocks LLM from hand-editing bot code via
    # Bash/Edit/Write, which bypassed the H6 circuit breaker in v218).
    _hooks = {**_orch._make_precompact_hook(), **_orch._make_bot_dir_guard_hook()}
    options = _orch.ClaudeAgentOptions(
        model="sonnet",
        permission_mode="bypassPermissions",
        cwd=str(_orch.PROJECT_ROOT),
        mcp_servers={"evolution": _orch.evolution_server},
        strict_mcp_config=True,
        # The main planner needs only the typed evolution MCP server. Removing
        # built-ins closes dynamic Python/shell import routes to operator-owned
        # pause, official-bootstrap, and strict-authority state.
        tools=[],
        disallowed_tools=_BLOCKED_MCP_TOOLS,
        hooks=_hooks,
        max_turns=max_turns,
        # CRITICAL: use the centralized _llm_thinking_options() (POK_LLM_THINKING_MODE),
        # NOT a hardcoded {"type": "adaptive"}.  GLM-5.2 + adaptive is a KNOWN
        # DEATH-LOOP: it emits 16k-19k+ thinking tokens without ever producing
        # visible output, wedging the orchestrator's own provider stream for
        # 50+ minutes per cycle (observed 2026-08-05: PID 3943696 ran 52 min on
        # --thinking adaptive, never converging).  The documented reliable mode
        # is {"type": "enabled", "budget_tokens": <large>} (soft target, GLM
        # reasons deeply then converges).  See AGENTS.md "LLM provider and
        # extended thinking" and llm_role_observability._llm_thinking_options.
        **_llm_thinking_options(),
    )

    total_cost = 0.0
    cycle_completed = False
    auth_error = False
    cycle_failed = False  # P1: generic-exception path must not return partial cost (fake success)
    infra_error = False  # P2: SDK signature/timeout/connection — distinct from real auth (-0.5 vs -1.0)
    shutdown_cancelled = False
    stream_invocation_count = 0
    # Snapshot sub-agent costs at start to compute delta on return.
    # ui.gen_cost_total tracks ALL sub-agent costs (Master, Workers, etc.)
    # via llm_query.py. The orchestrator's own ResultMessage is projected at the
    # same durable-record boundary, before a hard-limit exception can unwind.
    _cost_at_start = ui.gen_cost_total if ui else 0.0

    return ({
        'prompt': prompt,
        'checkpoint': checkpoint,
        'baseline_checkpoint': baseline_checkpoint,
        'baseline_checkpoint_identity': baseline_checkpoint_identity,
        'baseline_owned_route_identity': baseline_owned_route_identity,
        'options': options,
        '_cost_at_start': _cost_at_start,
    },)


async def _cycle_phase_b_stream_session(ctx, ui, log_file, gen_ctx,
                                        shutdown_mgr, max_turns):
    """Phase B: the provider streaming with/try/except session.

    Returns a bare value (early-exit cost sentinel) or a dict with the final
    cycle state for the caller's cost accounting.
    """
    from evolution_core import _BLOCKED_MCP_TOOLS
    prompt = ctx['prompt']
    checkpoint = ctx['checkpoint']
    baseline_checkpoint = ctx['baseline_checkpoint']
    baseline_checkpoint_identity = ctx['baseline_checkpoint_identity']
    baseline_owned_route_identity = ctx['baseline_owned_route_identity']
    options = ctx['options']
    _cost_at_start = ctx['_cost_at_start']
    total_cost = 0.0
    cycle_completed = False
    auth_error = False
    cycle_failed = False
    infra_error = False
    shutdown_cancelled = False
    stream_invocation_count = 0
    with open(log_file, "a") as lf:
        lf.write(f"\n{'='*60}\n[ORCHESTRATOR CYCLE] {_orch.time.strftime('%Y-%m-%d %H:%M:%S')}\n{'='*60}\n")
        lf.write(f"[PROMPT]\n{prompt}\n\n[OUTPUT]\n")

        async def _stream_response(opts, max_retries=3):
            """Run a single streaming query. Returns (full_text, cost, cycle_ok, gen, auth_error)."""
            nonlocal stream_invocation_count
            stream_invocation_count += 1
            stream_invocation_id = stream_invocation_count
            texts = []
            cost = 0.0
            ok = False
            gen = None
            auth_err = False
            _tool_call_counts = {}
            _pending_tool_uses = {}
            _seen_tool_use_ids = set()
            _ignored_user_tool_use_ids = set()
            _terminal_tool_result_for_batch = None
            availability_trace = _orch.LLMAvailabilityTrace()

            def _canonical_json_bytes(value):
                try:
                    return _orch.json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                except (TypeError, ValueError):
                    return None

            def _current_verified_terminal_cache():
                """Read the active attempt's guarded-handler-only cache."""

                try:
                    from llm_query import current_provider_verified_terminal_abandon

                    return current_provider_verified_terminal_abandon()
                except Exception:
                    return None

            def _validated_cached_terminal_for_owner(owner, tool_use_id):
                """Reprove one cache entry before it can replace an SDK result."""

                record = _current_verified_terminal_cache()
                if record is None:
                    return None
                if not isinstance(record, dict):
                    _raise_stream_result_binding_blocked(
                        "provider_terminal_cache_shape_invalid"
                    )
                if owner not in _orch._TERMINAL_ABANDON_RESULT_OWNER_TOOLS:
                    _raise_stream_result_binding_blocked(
                        "provider_terminal_cache_owner_not_terminal:"
                        f"{owner or 'unknown'}"
                    )
                if str(record.get("owner_tool") or "") != str(owner or ""):
                    _raise_stream_result_binding_blocked(
                        "provider_terminal_cache_owner_mismatch:"
                        f"{owner or 'unknown'}"
                    )
                if str(record.get("tool_use_id") or "") != str(tool_use_id or ""):
                    _raise_stream_result_binding_blocked(
                        "provider_terminal_cache_tool_use_id_mismatch"
                    )
                if not isinstance(record.get("arguments"), str):
                    _raise_stream_result_binding_blocked(
                        "provider_terminal_cache_arguments_invalid"
                    )
                cached_result = _orch._completed_abandon_tool_result(
                    record.get("terminal_result")
                )
                if cached_result is None or not isinstance(
                    baseline_checkpoint, dict
                ):
                    _raise_stream_result_binding_blocked(
                        "provider_terminal_cache_material_invalid"
                    )
                try:
                    from tool_bot_management import validate_completed_abandon_handoff

                    proof = validate_completed_abandon_handoff(
                        baseline_checkpoint,
                        cached_result,
                    )
                except Exception as exc:
                    _raise_stream_result_binding_blocked(
                        "provider_terminal_cache_proof_invalid:"
                        f"{str(exc)[:160]}"
                    )
                if _canonical_json_bytes(proof) != _canonical_json_bytes(
                    record.get("terminal_proof")
                ):
                    _raise_stream_result_binding_blocked(
                        "provider_terminal_cache_proof_mismatch"
                    )
                return cached_result

            def _settle_missing_terminal_result_from_cache():
                """Settle exactly one pending ToolUse only from a proved cache."""

                if not _pending_tool_uses:
                    return None
                record = _current_verified_terminal_cache()
                if record is None:
                    return None
                if len(_pending_tool_uses) != 1:
                    _raise_stream_result_binding_blocked(
                        "provider_terminal_cache_pending_tool_uses_ambiguous"
                    )
                tool_use_id, pending_name = next(iter(_pending_tool_uses.items()))
                owner = _orch._normalized_provider_tool_name(pending_name)
                cached_result = _validated_cached_terminal_for_owner(
                    owner,
                    tool_use_id,
                )
                if cached_result is None:
                    return None
                _pending_tool_uses.pop(tool_use_id, None)
                return cached_result

            def _register_pending_tool_use(block, *, source):
                """Bind either SDK message placement of ToolUseBlock identically."""

                nonlocal _terminal_tool_result_for_batch
                tool_use_id = str(getattr(block, "id", "") or "")
                if not tool_use_id or tool_use_id in _seen_tool_use_ids:
                    _raise_stream_result_binding_blocked(
                        "assistant_tool_use_id_missing_or_duplicate"
                    )
                _seen_tool_use_ids.add(tool_use_id)
                raw_name = str(getattr(block, "name", "") or "")
                if not raw_name.startswith("mcp__evolution__"):
                    if source == "user":
                        # SDK may surface non-MCP/User-side tool annotations in
                        # a UserMessage.  They have no Evolution dispatch
                        # authority and must not create a pending gate lease.
                        _ignored_user_tool_use_ids.add(tool_use_id)
                        return False
                    _raise_stream_result_binding_blocked(
                        "assistant_tool_use_not_evolution_mcp"
                    )
                if not _pending_tool_uses:
                    _terminal_tool_result_for_batch = None
                _pending_tool_uses[tool_use_id] = raw_name
                try:
                    from llm_query import register_current_provider_evolution_tool_use

                    register_current_provider_evolution_tool_use(
                        tool_use_id,
                        raw_name,
                        getattr(block, "input", None),
                    )
                except Exception:
                    # Local binding remains sufficient for the normal SDK
                    # result path.  Only the transport-loss fallback depends
                    # on the stricter attempt-scoped registration.
                    pass
                if ui:
                    ui.log_history(
                        f"[Orchestrator] Calling tool: {raw_name}", "info"
                    )
                    ui.log_io(
                        f"\n[tool: {raw_name}]", "tool", "Orchestrator"
                    )
                    ui.emit_tool_call(raw_name, block.input, "Orchestrator")
                else:
                    _orch.log.info("Calling tool: %s", raw_name)
                args_str = _orch.json.dumps(
                    block.input, ensure_ascii=False, indent=2
                )[:2000]
                lf.write(f"\n[tool: {raw_name}]\n[args] {args_str}\n")
                tool_name = (
                    raw_name.split("__")[-1]
                    if "__" in raw_name
                    else raw_name
                )
                _tool_call_counts[tool_name] = (
                    _tool_call_counts.get(tool_name, 0) + 1
                )
                threshold = (
                    _orch._REDUNDANT_NOISY_THRESHOLD
                    if tool_name in _orch._NOISY_TOOLS
                    else _orch._REDUNDANT_STRICT_THRESHOLD
                )
                if _tool_call_counts[tool_name] != threshold:
                    return
                allowed_repeat = _orch._classify_allowed_repeated_pipeline_tool(
                    tool_name, block.input
                )
                if allowed_repeat:
                    _orch.log.info(
                        "Tool '%s' called %d times on a corrective route: %s",
                        tool_name,
                        _tool_call_counts[tool_name],
                        allowed_repeat.get("reason"),
                    )
                    try:
                        _orch.log_system_event(
                            "pipeline.repeated_tool_call_allowed",
                            "info",
                            f"Orchestrator called {tool_name} "
                            f"{_tool_call_counts[tool_name]}x on corrective route "
                            f"{allowed_repeat.get('reason')}",
                            {
                                "tool": tool_name,
                                "count": _tool_call_counts[tool_name],
                                "threshold": threshold,
                                **allowed_repeat,
                            },
                        )
                    except Exception:
                        pass
                    return
                _orch.log.warning(
                    "Tool '%s' called %d times (possible redundant call)",
                    tool_name,
                    _tool_call_counts[tool_name],
                )
                try:
                    _orch.log_system_event(
                        "pipeline.redundant_tool_call",
                        "warn",
                        f"Orchestrator called {tool_name} "
                        f"{_tool_call_counts[tool_name]}x in one cycle",
                        {
                            "tool": tool_name,
                            "count": _tool_call_counts[tool_name],
                            "threshold": threshold,
                        },
                    )
                except Exception:
                    pass

                return True

            def _settle_registered_evolution_tool_use(tool_use_id):
                try:
                    from llm_query import settle_current_provider_evolution_tool_use

                    settle_current_provider_evolution_tool_use(tool_use_id)
                except Exception:
                    pass

            def _raise_actionable_handoff_if_ready(terminal_tool_result=None):
                handoff = _orch._detect_actionable_stage_handoff(
                    baseline_checkpoint_identity=baseline_checkpoint_identity,
                    baseline_checkpoint=baseline_checkpoint,
                    terminal_tool_result=terminal_tool_result,
                )
                if not handoff:
                    return
                next_v = handoff.get("next_v")
                stage = handoff.get("stage")
                if handoff.get("recovery_blocked"):
                    issues = ", ".join(map(str, handoff.get("issues") or ()))
                    msg = (
                        "Checkpoint-free recovery is blocked by durable handoff "
                        f"diagnostics ({issues or 'unknown'}); ending the current "
                        "Orchestrator stream so outer recovery can fail closed."
                    )
                elif handoff.get("operator_action_required"):
                    msg = (
                        f"Checkpoint parked at '{stage}' for v{next_v}; ending the "
                        "Orchestrator stream and stopping automatic recovery until "
                        "the explicit operator bootstrap succeeds."
                    )
                elif handoff.get("scheduler_handoff_required"):
                    msg = (
                        f"Generation workflow for v{next_v} reached its canonical "
                        "terminal boundary; ending the current Orchestrator stream "
                        "so the outer scheduler can call prepare_generation in a "
                        "fresh cycle."
                    )
                else:
                    next_tool = handoff.get("next_tool") or "unknown"
                    msg = (
                        f"Checkpoint reached actionable stage '{stage}' for v{next_v}; "
                        f"handing off current Orchestrator stream so recovery can call "
                        f"{next_tool} deterministically."
                    )
                try:
                    _orch.log_system_event(
                        "pipeline.actionable_stage_handoff",
                        "info",
                        msg,
                        handoff,
                    )
                except Exception:
                    pass
                raise _orch._OrchActionableStageHandoff(msg, handoff)

            def _raise_stream_result_binding_blocked(issue):
                handoff = {
                    "next_v": (
                        baseline_checkpoint.get("next_v")
                        if isinstance(baseline_checkpoint, dict)
                        else None
                    ),
                    "source_v": (
                        baseline_checkpoint.get("source_v")
                        if isinstance(baseline_checkpoint, dict)
                        else None
                    ),
                    "stage": "provider_tool_result_binding_blocked",
                    "next_tool": None,
                    "recovery_blocked": True,
                    "issues": [str(issue)],
                    "directive": (
                        "End the provider stream. A tool result could not be "
                        "bound to exactly one stream-owned tool invocation."
                    ),
                }
                msg = (
                    "Provider tool-result identity failed closed: "
                    f"{issue}."
                )
                try:
                    _orch.log_system_event(
                        "pipeline.provider_tool_result_binding_blocked",
                        "error",
                        msg,
                        handoff,
                    )
                except Exception:
                    pass
                raise _orch._OrchActionableStageHandoff(msg, handoff)

            def _terminal_result_for_bound_tool(tool_use_id, content):
                """Accept terminal proof only from its exact mutating owner call."""

                owner = _orch._normalized_provider_tool_name(
                    _pending_tool_uses.get(tool_use_id)
                )
                terminal = _orch._completed_abandon_tool_result(content)
                if (
                    terminal is not None
                    and owner not in _orch._TERMINAL_ABANDON_RESULT_OWNER_TOOLS
                ):
                    _raise_stream_result_binding_blocked(
                        "terminal_abandon_result_owner_mismatch:"
                        f"{owner or 'unknown'}"
                    )
                if terminal is not None:
                    cached_result = _validated_cached_terminal_for_owner(
                        owner,
                        tool_use_id,
                    )
                    if (
                        cached_result is not None
                        and _canonical_json_bytes(cached_result)
                        != _canonical_json_bytes(terminal)
                    ):
                        _raise_stream_result_binding_blocked(
                            "provider_terminal_cache_sdk_result_mismatch"
                        )
                return terminal

            if getattr(opts, "resume", None) is not None:
                raise RuntimeError("orchestrator_provider_session_resume_forbidden")
            provider_attempt = _orch.create_owned_provider_attempt(prompt, opts)
            _attempt_ref[0] = provider_attempt
            provider_token = _orch.activate_owned_provider_attempt(provider_attempt)
            native_match_dispatch_token = None
            _orch._orchestrator_provider_stream_active = True
            try:
                # Bind native liveness to this exact SDK transport before any
                # MCP tool handler is allowed to inherit the task context.
                # This task resets only its ContextVar.  The outer bounded
                # stream owner retains registry authority through final
                # live/terminal proof, then revokes it on every return/error/
                # cancellation path.
                from pipeline_state import activate_native_match_dispatch_nonce

                native_match_dispatch_token = (
                    activate_native_match_dispatch_nonce(
                        str(provider_attempt.get("attempt_id") or "")
                    )
                )
                gen = _orch.claude_query(
                    prompt=prompt,
                    options=opts,
                    transport=_orch.owned_provider_attempt_transport(provider_attempt),
                )
                _gen_ref[0] = gen
                _stream_iter = gen.__aiter__()
                _first_activity_seen = False
                _stream_started_at = _orch.time.time()
                _last_message_at = _stream_started_at  # D: for stall ceiling
                while True:
                    try:
                        if not _first_activity_seen:
                            message = await _orch.await_provider_stream_next_bounded(
                                _stream_iter,
                                _orch.ORCH_FIRST_ACTIVITY_TIMEOUT,
                            )
                            _first_activity_seen = True
                            _last_message_at = _orch.time.time()
                            _first_latency = _orch.time.time() - _stream_started_at
                            if _first_latency >= 60:
                                try:
                                    _orch.log_system_event(
                                        "pipeline.first_activity_delayed", "warn",
                                        f"Orchestrator first stream activity after {_first_latency:.1f}s",
                                        {"latency_s": round(_first_latency, 1),
                                         "timeout_s": _orch.ORCH_FIRST_ACTIVITY_TIMEOUT},
                                    )
                                except Exception:
                                    pass
                        else:
                            message = await _orch._await_next_stream_message(
                                _stream_iter,
                                last_message_at=_last_message_at,
                                stream_started_at=_stream_started_at,
                                baseline_owned_route_identity=(
                                    baseline_owned_route_identity
                                ),
                            )
                            _last_message_at = _orch.time.time()
                    except StopAsyncIteration:
                        if _pending_tool_uses:
                            cached_terminal = (
                                _settle_missing_terminal_result_from_cache()
                            )
                            if _pending_tool_uses:
                                _raise_stream_result_binding_blocked(
                                    "provider_stream_ended_with_pending_tool_results"
                                )
                            _terminal_tool_result_for_batch = (
                                cached_terminal
                                or _terminal_tool_result_for_batch
                            )
                        _raise_actionable_handoff_if_ready(
                            terminal_tool_result=_terminal_tool_result_for_batch
                        )
                        _terminal_tool_result_for_batch = None
                        break
                    except _orch.asyncio.TimeoutError as e:
                        if not _first_activity_seen:
                            msg = (
                                f"Orchestrator LLM produced no first stream message within "
                                f"{_orch.ORCH_FIRST_ACTIVITY_TIMEOUT}s"
                            )
                            if ui:
                                ui.log_history(
                                    f"[Orchestrator] {msg} — treating as infrastructure stall; "
                                    "checkpoint will be preserved for retry.",
                                    "warn",
                                )
                            try:
                                _orch.log_system_event(
                                    "pipeline.first_activity_timeout", "warn", msg,
                                    {"timeout_s": _orch.ORCH_FIRST_ACTIVITY_TIMEOUT,
                                     "session_present": bool(_orch._load_orchestrator_session())},
                                )
                            except Exception:
                                pass
                            raise _orch._OrchFirstActivityTimeout(msg) from e
                        raise
                    if isinstance(message, _orch.AssistantMessage):
                        assistant_has_tool_use = False
                        assistant_terminal_result = None
                        for block in message.content:
                            if isinstance(block, _orch.TextBlock):
                                availability_trace.observe_text(block.text)
                                texts.append(block.text)
                                if ui:
                                    ui.log_io(block.text, "claude", "Orchestrator")
                                else:
                                    _orch.log.debug("%s", block.text.rstrip())
                                lf.write(block.text)
                                # Detect embedded API errors in text output (mid-stream visibility)
                                _text_lower = block.text.lower()
                                if 'internal server error' in _text_lower or 'api error' in _text_lower or 'internal network failure' in _text_lower:
                                    _orch.log.warning("Embedded API error detected in LLM output: %s", block.text[:200])
                                    if ui:
                                        ui.log_history("[Orchestrator] Mid-stream API error detected", "warning")
                            elif isinstance(block, _orch.ToolUseBlock):
                                assistant_has_tool_use = True
                                _register_pending_tool_use(
                                    block,
                                    source="assistant",
                                )
                            elif isinstance(block, _orch.ThinkingBlock):
                                thinking = block.thinking or "[thinking...]"
                                if ui:
                                    ui.log_io(thinking, "thinking", "Orchestrator")
                                else:
                                    _orch.log.debug("[thinking...]")
                                lf.write(f"\n[THINKING] {thinking[:2000]}\n")
                            elif isinstance(block, _orch.ToolResultBlock):
                                content = block.content if isinstance(block.content, str) else (
                                    _orch.json.dumps(block.content, ensure_ascii=False) if block.content is not None else ""
                                )
                                if content:
                                    lf.write(f"\n[tool_result] {content[:500]}\n")
                                    if ui:
                                        ui.log_io(content[:3000], "tool_result", "Orchestrator")
                                _orch._raise_for_llm_availability_tool_result(
                                    block.content
                                )
                                tool_use_id = str(
                                    getattr(block, "tool_use_id", "") or ""
                                )
                                if tool_use_id not in _pending_tool_uses:
                                    _raise_stream_result_binding_blocked(
                                        "assistant_tool_result_id_unknown_or_duplicate"
                                    )
                                assistant_terminal_result = (
                                    _terminal_result_for_bound_tool(
                                        tool_use_id,
                                        block.content,
                                    )
                                    or assistant_terminal_result
                                )
                                _terminal_tool_result_for_batch = (
                                    assistant_terminal_result
                                    or _terminal_tool_result_for_batch
                                )
                                _pending_tool_uses.pop(tool_use_id, None)
                                _settle_registered_evolution_tool_use(
                                    tool_use_id
                                )
                                # A nested role may have converted the typed
                                # exception into its legacy infra payload. The
                                # shared run_claude_query boundary persists the
                                # pause first, so stop this parent stream before
                                # the model can call another tool and consume a
                                # second gate attempt.
                                nested_pause = _orch.active_llm_pause()
                                if nested_pause is not None:
                                    raise _orch.blocked_from_pause_state(
                                        nested_pause,
                                        role="Orchestrator",
                                    )
                        # Sub-agent costs have settled in the durable generation
                        # ledger by this point.  Default mode only emits telemetry;
                        # an explicit operator hard limit stops the stream.
                        _orch._check_generation_cost_policy(ui)
                        if not assistant_has_tool_use and not _pending_tool_uses:
                            _raise_actionable_handoff_if_ready(
                                terminal_tool_result=(
                                    assistant_terminal_result
                                    or _terminal_tool_result_for_batch
                                ),
                            )
                            _terminal_tool_result_for_batch = None
                    elif isinstance(message, _orch.UserMessage):
                        nested_pause = _orch.active_llm_pause()
                        if nested_pause is not None:
                            raise _orch.blocked_from_pause_state(
                                nested_pause,
                                role="Orchestrator",
                            )
                        saw_tool_result = False
                        terminal_tool_result = None
                        if isinstance(message.content, list):
                            for block in message.content:
                                # The SDK parser permits ToolUseBlock in a
                                # UserMessage.  Treat it identically to the
                                # AssistantMessage placement; otherwise the
                                # real handler can mutate/abandon a generation
                                # while the parent has no pending id to bind its
                                # eventual result to.
                                if isinstance(block, _orch.ToolUseBlock):
                                    _register_pending_tool_use(
                                        block,
                                        source="user",
                                    )
                                    continue
                                if not isinstance(block, _orch.ToolResultBlock):
                                    continue
                                saw_tool_result = True
                                content = block.content
                                rendered = (
                                    content
                                    if isinstance(content, str)
                                    else _orch.json.dumps(content, ensure_ascii=False)
                                    if content is not None
                                    else ""
                                )
                                if rendered:
                                    lf.write(f"\n[tool_result] {rendered[:500]}\n")
                                    if ui:
                                        ui.log_io(
                                            rendered[:3000],
                                            "tool_result",
                                            "Orchestrator",
                                )
                                _orch._raise_for_llm_availability_tool_result(content)
                                tool_use_id = str(
                                    getattr(block, "tool_use_id", "") or ""
                                )
                                if tool_use_id in _ignored_user_tool_use_ids:
                                    _ignored_user_tool_use_ids.discard(tool_use_id)
                                    continue
                                if tool_use_id not in _pending_tool_uses:
                                    _raise_stream_result_binding_blocked(
                                        "user_tool_result_id_unknown_or_duplicate"
                                    )
                                terminal_tool_result = (
                                    _terminal_result_for_bound_tool(
                                        tool_use_id,
                                        content,
                                    )
                                    or terminal_tool_result
                                )
                                _terminal_tool_result_for_batch = (
                                    terminal_tool_result
                                    or _terminal_tool_result_for_batch
                                )
                                _pending_tool_uses.pop(tool_use_id, None)
                                _settle_registered_evolution_tool_use(
                                    tool_use_id
                                )
                        tool_use_result = getattr(
                            message,
                            "tool_use_result",
                            None,
                        )
                        if tool_use_result is not None:
                            if not saw_tool_result:
                                _orch._raise_for_llm_availability_tool_result(
                                    tool_use_result
                                )
                            fallback_tool_use_id = ""
                            if isinstance(tool_use_result, dict):
                                fallback_tool_use_id = str(
                                    tool_use_result.get("tool_use_id") or ""
                                )
                            if not fallback_tool_use_id:
                                fallback_tool_use_id = str(
                                    getattr(
                                        message,
                                        "parent_tool_use_id",
                                        "",
                                    )
                                    or ""
                                )
                            if (
                                not fallback_tool_use_id
                                and len(_pending_tool_uses) == 1
                            ):
                                fallback_tool_use_id = next(
                                    iter(_pending_tool_uses)
                                )
                            if fallback_tool_use_id in _ignored_user_tool_use_ids:
                                # Keep the legacy ``tool_use_result`` shortcut
                                # consistent with ToolResultBlock handling:
                                # a non-Evolution annotation carried by a
                                # UserMessage has no gate lease and cannot be
                                # promoted into a missing-result failure.
                                _ignored_user_tool_use_ids.discard(
                                    fallback_tool_use_id
                                )
                            elif fallback_tool_use_id in _pending_tool_uses:
                                terminal_tool_result = (
                                    _terminal_result_for_bound_tool(
                                        fallback_tool_use_id,
                                        tool_use_result,
                                    )
                                    or terminal_tool_result
                                )
                                _terminal_tool_result_for_batch = (
                                    terminal_tool_result
                                    or _terminal_tool_result_for_batch
                                )
                                _pending_tool_uses.pop(
                                    fallback_tool_use_id,
                                    None,
                                )
                                _settle_registered_evolution_tool_use(
                                    fallback_tool_use_id
                                )
                            elif not saw_tool_result:
                                _raise_stream_result_binding_blocked(
                                    "user_tool_use_result_id_unknown_or_duplicate"
                                )
                        nested_pause = _orch.active_llm_pause()
                        if nested_pause is not None:
                            raise _orch.blocked_from_pause_state(
                                nested_pause,
                                role="Orchestrator",
                            )
                        _orch._check_generation_cost_policy(ui)
                        if not _pending_tool_uses:
                            _raise_actionable_handoff_if_ready(
                                terminal_tool_result=(
                                    terminal_tool_result
                                    or _terminal_tool_result_for_batch
                                ),
                            )
                            _terminal_tool_result_for_batch = None
                    elif isinstance(message, _orch.ResultMessage):
                        availability_trace.observe_result(message)
                        billing_status = _orch.record_generation_cost(
                            "Orchestrator",
                            message.total_cost_usd,
                            getattr(message, "usage", None),
                            source="orchestrator_result",
                            event_id=_orch.sdk_result_event_id(
                                message,
                                source="orchestrator_result",
                                attempt=stream_invocation_id,
                            ),
                        )
                        # A resumed SDK stream may replay the same Result.
                        # Count/UI-project only the first durable occurrence.
                        billing_new = (
                            not billing_status.get("active")
                            or billing_status.get("recorded")
                            or billing_status.get("pending_only")
                        )
                        if billing_new:
                            if message.total_cost_usd is not None:
                                cost += message.total_cost_usd
                            if ui:
                                ui.update_cost(
                                    "Orchestrator",
                                    float(message.total_cost_usd or 0.0),
                                    getattr(message, "usage", None),
                                )
                        _orch._check_generation_cost_policy(ui)
                        if not message.is_error:
                            if _pending_tool_uses:
                                cached_terminal = (
                                    _settle_missing_terminal_result_from_cache()
                                )
                                if _pending_tool_uses:
                                    _raise_stream_result_binding_blocked(
                                        "provider_result_with_pending_tool_results"
                                    )
                                _terminal_tool_result_for_batch = (
                                    cached_terminal
                                    or _terminal_tool_result_for_batch
                                )
                            _raise_actionable_handoff_if_ready(
                                terminal_tool_result=(
                                    _terminal_tool_result_for_batch
                                ),
                            )
                            _terminal_tool_result_for_batch = None
                            ok = True
                            if message.session_id:
                                _orch._save_orchestrator_session(message.session_id)
                        else:
                            error_text = _orch.extract_result_error(message)
                            lf.write(f"\n[API ERROR] {error_text}\n")
                            if ui:
                                ui.log_history(f"[Orchestrator] API error: {error_text[:200]}", "error")
                            # Parse a provider-declared quota reset.  The next
                            # attempt is a fresh stream over the same validated
                            # checkpoint; opaque provider history is not retained.
                            is_429 = "429" in error_text or ("已达到" in error_text and "使用上限" in error_text)
                            if is_429:
                                from rate_limiter import rate_limiter
                                rate_limiter.parse_429(error_text)
                            else:
                                _orch._clear_orchestrator_session()
                            # Detect real auth failures. Match status tokens, NOT bare substrings —
                            # otherwise cost strings like "$0.4017"/"$0.4031" and token counts falsely
                            # trip auth_err, misrouting signature/infra failures into the 300s auth
                            # backoff path (which itself never fires on a real 401/403 in practice).
                            import re as _re
                            if _re.search(r"\b40[13]\b", error_text) or \
                                    "invalid x-api-key" in error_text.lower() or \
                                    "authentication" in error_text.lower():
                                auth_err = True
                            availability_block = availability_trace.blocked(
                                role="Orchestrator"
                            )
                            if availability_block is not None:
                                raise availability_block
            except _orch.LLMAvailabilityBlocked:
                raise
            except (
                _orch._OrchActionableStageHandoff,
                _orch.OperatorGenerationCostLimitExceeded,
            ):
                raise
            except (_orch.CLINotFoundError, _orch.ProcessError) as e:
                availability_block = availability_trace.blocked(
                    role="Orchestrator",
                    exception=e,
                )
                if availability_block is not None:
                    raise availability_block from e
                if ui:
                    ui.log_io(f"[ERROR] {e}", "error", "Orchestrator")
                else:
                    _orch.log.error("LLM error: %s", e)
                # Propagate to the outer `except Exception` so is_llm_infra_error
                # classification -> -0.5 infra sentinel applies, instead of falling
                # through and returning cost=0/ok=False (fake $0 success that masks
                # exit-143 / ProcessError crashes). The OUTER except (commit 0295d2b)
                # was fixed but this INNER one was missed (same shape as v84 deadlock).
                raise
            except _orch.ClaudeSDKError as _sig_err:
                availability_block = availability_trace.blocked(
                    role="Orchestrator",
                    exception=_sig_err,
                )
                if availability_block is not None:
                    raise availability_block from _sig_err
                # Signature-field stream errors: transient SDK deserialization bug
                # (ThinkingBlock.signature missing). Retryable ONLY when no MCP tool
                # has executed yet — otherwise tool side-effects would be duplicated.
                # When retryable, convert to _OrchSignatureRetryable for the retry
                # loop in _run_one_cycle; otherwise propagate (-> infra -0.5 backoff).
                _sig_s = str(_sig_err).lower()
                if ("signature" in _sig_s or "missing required field" in _sig_s) and not _tool_call_counts:
                    raise _orch._OrchSignatureRetryable(str(_sig_err)) from _sig_err
                raise
            except Exception as e:
                availability_block = availability_trace.blocked(
                    role="Orchestrator",
                    exception=e,
                )
                if availability_block is not None:
                    raise availability_block from e
                raise
            finally:
                try:
                    if gen is not None:
                        await _orch.cleanup_owned_provider_attempt(
                            gen,
                            provider_attempt,
                            "ORCHESTRATOR",
                            log_file,
                        )
                finally:
                    _orch._orchestrator_provider_stream_active = False
                    if native_match_dispatch_token is not None:
                        try:
                            from pipeline_state import reset_native_match_dispatch_nonce

                            reset_native_match_dispatch_nonce(
                                native_match_dispatch_token
                            )
                        except Exception:
                            pass
                    _orch.reset_owned_provider_attempt(provider_token)
            if _tool_call_counts:
                _orch.log.info("Tool call summary: %s", dict(sorted(_tool_call_counts.items())))
            return "".join(texts), cost, ok, gen, auth_err

        CYCLE_TIMEOUT = 14400  # 240 minutes (4h) max per LLM cycle. Generous for GLM-5.2 variable output speed: during peak provider load a single Scout can take 15-20min. Full pipeline (Scout ensemble + Critics + final + workers + review + critic + precommit) can reach 2-3h under load. 14400s gives ample room without being unbounded.
        # Sentinel returned by the timeout-extension path (stage=verified, first extension).
        # Must be DISTINCT from every other cost signal: -0.5 (infra), -1.0 (generic crash),
        # and the auth clamp -max(abs(total_cost), 1.0) which can reach any negative value
        # ≥1.0 in magnitude. -99999.0 is unreachable in practice (a single cycle cannot
        # spend $99999) so it cannot collide with the auth clamp even if a future cycle's
        # total_cost grew large. This fixes the v101 death-loop's latent collision risk.
        _TIMEOUT_EXTENSION_SENTINEL = -99999.0
        query_gen = None
        # Mutable container to track the async generator across scope boundaries.
        # The bounded task owner raises before tuple unpacking on timeout, so the
        # exact generator is published here from inside _stream_response.
        _gen_ref = [None]
        # The complete owned-attempt record is published before SDK dispatch so
        # the cycle-level timeout can terminate and verify this exact transport.
        _attempt_ref = [None]
        # H1: rotate a previously-cancelled precommit attempt token at cycle
        # start; never clear or revive the detached old attempt.
        try:
            from tool_eval import reset_precommit_shutdown
            reset_precommit_shutdown()
        except Exception:
            pass
        try:
            try:
                # Signature-retry loop for the orchestrator's own SDK stream.
                # _stream_response raises _OrchSignatureRetryable when a transient
                # signature-field error occurs AND no MCP tool has executed yet
                # (no side-effects to duplicate). Bounded to _ORCH_SIG_MAX_ATTEMPTS.
                _ORCH_SIG_MAX_ATTEMPTS = 2
                full_output, total_cost, cycle_completed, query_gen, auth_error = (
                    None, 0.0, False, None, False
                )
                # NOTE: each retry attempt is individually wrapped by CYCLE_TIMEOUT
                # below, so worst-case wall-clock is (N+1)*CYCLE_TIMEOUT. This is
                # acceptable because (a) signature errors fire early in the stream
                # (ThinkingBlock deserialization), so each attempt burns little of
                # the 3600s budget, and (b) the watchdog/stuck-pipeline detector
                # remains the hard backstop. Signature retries only happen when no
                # MCP tool has executed yet (no side-effects to duplicate).
                for _sig_attempt in range(_ORCH_SIG_MAX_ATTEMPTS + 1):
                    try:
                        full_output, total_cost, cycle_completed, query_gen, auth_error = (
                            await _orch._await_orchestrator_stream_response_bounded(
                                _stream_response(options),
                                timeout=CYCLE_TIMEOUT,
                                attempt_ref=_attempt_ref,
                                gen_ref=_gen_ref,
                                log_file_path=log_file,
                            )
                        )
                        break
                    except _orch._OrchSignatureRetryable as _sr:
                        if _sig_attempt < _ORCH_SIG_MAX_ATTEMPTS:
                            _backoff = min(5 * (2 ** _sig_attempt), 20)
                            _orch.log.warning(
                                "Orchestrator SDK signature stream error (attempt %d/%d), "
                                "retrying in %ds (no tool side-effects yet): %s",
                                _sig_attempt + 1, _ORCH_SIG_MAX_ATTEMPTS + 1, _backoff, _sr,
                            )
                            if ui:
                                ui.log_history(
                                    f"[Orchestrator] SDK signature stream error — retrying "
                                    f"(attempt {_sig_attempt + 1}/{_ORCH_SIG_MAX_ATTEMPTS + 1})...",
                                    "warn",
                                )
                            try:
                                _orch.log_system_event("pipeline.orch_signature_retry", "warn",
                                    f"Orchestrator signature stream retry (attempt {_sig_attempt + 1})",
                                    {"attempt": _sig_attempt + 1, "error": str(_sr)[:200]})
                            except Exception:
                                pass
                            await _orch.asyncio.sleep(_backoff)
                            continue
                        # Exhausted — re-raise the ORIGINAL ClaudeSDKError (stored
                        # as __cause__ when _OrchSignatureRetryable was raised) so
                        # the outer except classifies it as infra (-0.5 backoff)
                        # via type-based is_llm_infra_error, not only the keyword
                        # fallback (which would silently break if the keyword
                        # guard were ever tightened).
                        raise (_sr.__cause__ or _sr) from None
                if full_output is None:
                    # All signature retries exhausted and re-raised above; defensive.
                    raise RuntimeError("orchestrator signature retry loop exited without result")
            except _orch.asyncio.TimeoutError:
                # Signal the exact in-flight native precommit attempt to stop.
                # The owned provider stream is already cancelled, but a complete
                # 70-hand subprocess-backed match is the smallest interruptible
                # evidence unit.  The monotonic token is checked after that unit
                # and before another sample can launch or reach a terminal gate.
                try:
                    from tool_eval import set_precommit_shutdown
                    set_precommit_shutdown()
                except Exception as _se:
                    _orch.log.debug("set_precommit_shutdown failed: %s", _se)

                # Stage-aware timeout skip: if pipeline is at the "verified" stage,
                # commit is the next gate (idempotent) — grant ONE extension.
                # Only "verified" (precommit passed) qualifies — "critic_checked" still
                # has verified + archived before commit, so granting there produced the
                # v101 false-complete death loop.
                try:
                    from evolution_core import read_pipeline_checkpoint as _read_ckpt
                    _ckpt = _read_ckpt()
                    if _ckpt and _ckpt.get("stage") == "publishing":
                        # Publication crossed a durable one-way boundary. A
                        # provider/session timeout cannot rewrite it to
                        # ``timed_out`` or abandon it; the next fresh session
                        # must reconcile the same immutable intent.
                        _orch._clear_orchestrator_session()
                        try:
                            _orch._write_timeout_checkpoint_from_exact_snapshot(
                                _ckpt,
                                "publishing",
                                touch_stage_timestamp=True,
                            )
                        except Exception:
                            pass
                        lf.write(
                            "\n[TIMEOUT] Durable publication preserved; "
                            "resuming the same intent.\n"
                        )
                        return _TIMEOUT_EXTENSION_SENTINEL
                    if _ckpt and _ckpt.get("stage") == "verified":
                        # ONE extension only: a per-version counter persisted in the
                        # checkpoint prevents every timeout at this stage re-granting.
                        _ext_count = _ckpt.get("timeout_extensions", 0)
                        if _ext_count >= 1:
                            # Already used the single extension — fall through to normal
                            # timeout handling below (marks timed_out, restarts cycle).
                            _orch.log.warning(
                                "Cycle timeout at stage=verified but timeout_extensions=%d already used — NOT granting (one extension limit).",
                                _ext_count,
                            )
                            if ui:
                                ui.log_history(
                                    f"[Orchestrator] Cycle timeout at stage=verified but the single "
                                    f"timeout extension was already granted (count={_ext_count}). "
                                    f"Not granting again.",
                                    "warn",
                                )
                            lf.write(f"\n[TIMEOUT] Stage=verified, extension already used ({_ext_count}) — not granting.\n")
                        else:
                            _orch.log.warning(
                                "Cycle timeout at stage=%s — commit is imminent, granting ONE extension (idempotent recovery)",
                                _ckpt.get("stage"),
                            )
                            if ui:
                                ui.log_history(
                                    f"[Orchestrator] Cycle timeout at stage={_ckpt.get('stage')} — "
                                    f"commit imminent, granting ONE extension.",
                                    "warn",
                                )
                            lf.write(f"\n[TIMEOUT] Stage={_ckpt.get('stage')} — granting ONE extension (commit imminent)\n")
                            # The owned stream boundary has confirmed or fenced
                            # generator/process cleanup. The disposable session
                            # cannot be resumed; restart from the checkpoint.
                            _orch._clear_orchestrator_session()
                            # Refresh checkpoint timestamp so the watchdog does not immediately
                            # re-trigger on the next cycle (elapsed > WATCHDOG_TIMEOUT), AND
                            # record the single granted extension (timeout_extensions=1).
                            try:
                                _orch._write_timeout_checkpoint_from_exact_snapshot(
                                    _ckpt,
                                    _ckpt.get("stage"),
                                    master_plan=_ckpt.get("master_plan"),
                                    reviewer_feedback=_ckpt.get("reviewer_feedback", ""),
                                    generation_attempt=_ckpt.get("generation_attempt", 0),
                                    gate_results=_ckpt.get("gate_results", {}) or {},
                                    parent2_v=_ckpt.get("parent2_v"),
                                    direction_audit=_ckpt.get("direction_audit"),
                                    audit_context=_ckpt.get("audit_context", {}) or {},
                                    audit_attempt=_ckpt.get("audit_attempt", 0),
                                    precommit_attempt=_ckpt.get("precommit_attempt", 0),
                                    timeout_extensions=1,
                                    touch_stage_timestamp=True,
                                )
                            except Exception:
                                pass  # Non-fatal: watchdog may trigger, but checkpoint is preserved
                            # Sentinel _TIMEOUT_EXTENSION_SENTINEL = "timeout extension granted,
                            # cycle NOT complete". The main loop treats it distinctly: NO
                            # post_generation_cleanup, NO 'gen complete' log, NO backoff — just
                            # resume from the checkpoint. Value -99999.0 is mathematically
                            # distinct from -0.5 infra / -1.0 generic / auth clamp (≥1.0 magnitude).
                            return _TIMEOUT_EXTENSION_SENTINEL
                except Exception:
                    pass  # If checkpoint read fails, fall through to normal timeout handling

                if ui:
                    ui.log_history(
                        f"[Orchestrator] Cycle timed out after {CYCLE_TIMEOUT}s — killing stuck session.",
                        "error",
                    )
                else:
                    _orch.log.error("Cycle timed out after %ss", CYCLE_TIMEOUT)
                lf.write(f"\n[TIMEOUT] Cycle killed after {CYCLE_TIMEOUT}s\n")
                _orch._clear_orchestrator_session()
                # Mark pipeline checkpoint as timed_out so next cycle doesn't repeat
                # the same stuck state (e.g., repeatedly failing run_precommit_eval)
                ckpt = None
                try:
                    from evolution_core import read_pipeline_checkpoint
                    ckpt = read_pipeline_checkpoint()
                    if ckpt and ckpt.get("stage") not in ("timed_out", "archived"):
                        # B3 (v125 retry-storm fix): if Master repeatedly failed this
                        # cycle (audit_attempt >= MAX_MASTER_TOTAL_FAILURES=2), the
                        # timeout was caused by the Master retry-storm itself — abandon
                        # now instead of marking timed_out + resuming into the same
                        # stuck Master loop. "verified" is excluded (it has its own
                        # extension path above; commit is the imminent idempotent step).
                        _b3_audit = int(ckpt.get("audit_attempt") or 0)
                        _b3_stage = ckpt.get("stage")
                        _B3_MASTER_FAIL_THRESHOLD = 2  # mirrors MAX_MASTER_TOTAL_FAILURES (tool_planning.py)
                        if (
                            _b3_audit >= _B3_MASTER_FAIL_THRESHOLD
                            and _b3_stage == "direction_audited"
                        ):
                            _orch.log.warning(
                                "Cycle timed out with Master fail count=%d (stage=%s) — "
                                "abandoning stuck generation instead of marking timed_out.",
                                _b3_audit, _b3_stage,
                            )
                            _orch.log_system_event(
                                "pipeline.cycle_timeout_abandon", "error",
                                f"Cycle timed out after {CYCLE_TIMEOUT}s with Master fail count={_b3_audit} — abandoning",
                                {"timeout_sec": CYCLE_TIMEOUT, "pipeline_stage": _b3_stage,
                                 "master_fail_count": _b3_audit},
                            )
                            if ui:
                                ui.log_history(
                                    f"[Orchestrator] Cycle timed out after Master failed "
                                    f"{_b3_audit}× — abandoning stuck generation "
                                    f"(instead of marking timed_out + resuming).",
                                    "error",
                                )
                            try:
                                from tool_bot_management import (
                                    _do_abandon_generation,
                                    expected_abandon_identity,
                                )
                                abandon_result = await _do_abandon_generation(
                                    reason=f"cycle_timeout_master_stuck ({_b3_audit} fails)",
                                    _bypass_rate_limit=True,
                                    **expected_abandon_identity(ckpt),
                                )
                                terminal_result = _orch._completed_abandon_tool_result(
                                    abandon_result
                                )
                                if terminal_result is None:
                                    raise RuntimeError(
                                        "cycle_timeout_master_abandon_not_completed"
                                    )
                                from tool_bot_management import (
                                    validate_completed_abandon_handoff,
                                )

                                terminal_proof = validate_completed_abandon_handoff(
                                    ckpt,
                                    terminal_result,
                                )
                                if (
                                    gen_ctx is not None
                                    and not _orch._remember_verified_canonical_abandon(
                                        gen_ctx,
                                        terminal_proof,
                                    )
                                ):
                                    raise RuntimeError(
                                        "cycle_timeout_master_abandon_proof_"
                                        "context_mismatch"
                                    )
                                return _orch.ORCH_GENERATION_ABANDONED_COST
                            except Exception as _ae:
                                _orch.log.error(
                                    "B3 canonical abandon failed closed: %s",
                                    _ae,
                                )
                                return _orch.ORCH_RECOVERY_BLOCKED_COST
                        else:
                            # v193 root-cause-audit (2026-06-26): distinguish an
                            # INFRA-only timeout from a real regression. When the
                            # cycle timed out during precommit (i.e. quality +
                            # review + critic already passed, stage=critic_checked)
                            # AND precommit produced no regression blocker (the bot
                            # code itself is fine — the timeout was caused by the
                            # daemon/scheduler failing to deliver battle results),
                            # mark `infra_timed_out` instead of `timed_out`. The
                            # recovery handler then RETRIES precommit on the SAME
                            # code instead of clearing the checkpoint and discarding
                            # the generation (which wasted v193's already-passed
                            # gates). If any regression blocker exists, fall back to
                            # plain timed_out (a real regression should not be retried).
                            _gate_results = ckpt.get("gate_results", {}) or {}
                            _is_precommit_stage = _b3_stage == "critic_checked"
                            _has_precommit_regression = False
                            _pc = _gate_results.get("precommit_eval") or {}
                            if isinstance(_pc, dict):
                                for _b in (_pc.get("blockers") or []):
                                    _reason = (_b.get("reason") if isinstance(_b, dict) else _b) or ""
                                    if _reason not in _orch._INFRA_BLOCKER_REASONS_SET:
                                        _has_precommit_regression = True
                                        break
                            _infra_only = (
                                _is_precommit_stage
                                and _gate_results.get("quality", {}).get("passed")
                                and _gate_results.get("review", {}).get("passed")
                                and _gate_results.get("critic", {}).get("passed")
                                and not _has_precommit_regression
                            )
                            if _infra_only:
                                marked_timeout = _orch._write_timeout_checkpoint_from_exact_snapshot(
                                    ckpt,
                                    "infra_timed_out",
                                    master_plan=ckpt.get("master_plan"),
                                )
                                timeout_message = (
                                    "Infra-only precommit timeout recorded; the exact "
                                    "candidate/gates will be re-proven before retry."
                                    if marked_timeout
                                    else "Infra-timeout overlay lost its checkpoint CAS; "
                                    "newer checkpoint authority was preserved."
                                )
                                _orch.log.warning(timeout_message)
                                _orch.log_system_event(
                                    "pipeline.cycle_timeout_infra"
                                    if marked_timeout
                                    else "pipeline.cycle_timeout_stage_preserved",
                                    "warn",
                                    timeout_message,
                                    {
                                        "timeout_sec": CYCLE_TIMEOUT,
                                        "pipeline_stage": _b3_stage,
                                        "precommit_attempt": ckpt.get(
                                            "precommit_attempt", 0
                                        ),
                                        "timeout_overlay_applied": marked_timeout,
                                    },
                                )
                                if ui:
                                    ui.log_history(
                                        f"[Orchestrator] {timeout_message}",
                                        "warn",
                                    )
                            else:
                                # LOG GAP FIX (2026-06-30): plain timed_out (the most
                                # common timeout path) previously had NO structured
                                # event — only cycle_timeout_abandon/infra logged.
                                # Record stage + reason so timeouts are auditable.
                                marked_timeout = _orch._write_timeout_checkpoint_from_exact_snapshot(
                                    ckpt,
                                    "timed_out",
                                    master_plan=ckpt.get("master_plan"),
                                )
                                timeout_message = (
                                    f"Cycle timed out after {CYCLE_TIMEOUT}s at "
                                    f"disposable stage={_b3_stage}; canonical "
                                    "abandon is now required."
                                    if marked_timeout
                                    else f"Cycle timed out after {CYCLE_TIMEOUT}s at "
                                    f"stage={_b3_stage}; exact stage/newer checkpoint "
                                    "authority was preserved."
                                )
                                try:
                                    _orch.log_system_event(
                                        "pipeline.cycle_timeout_plain"
                                        if marked_timeout
                                        else "pipeline.cycle_timeout_stage_preserved",
                                        "error" if marked_timeout else "warn",
                                        timeout_message,
                                        {
                                            "timeout_sec": CYCLE_TIMEOUT,
                                            "pipeline_stage": _b3_stage,
                                            "next_v": ckpt.get("next_v"),
                                            "source_v": ckpt.get("source_v"),
                                            "precommit_attempt": ckpt.get(
                                                "precommit_attempt", 0
                                            ),
                                            "master_fail_count": _b3_audit,
                                            "timeout_overlay_applied": marked_timeout,
                                        },
                                    )
                                except Exception:
                                    pass
                                if ui:
                                    ui.log_history(
                                        f"[Orchestrator] {timeout_message}",
                                        "error" if marked_timeout else "warn",
                                    )
                except Exception as e:
                    _orch.log.warning("Failed to mark checkpoint timed_out: %s", e)
                try:
                    _orch.log_system_event("pipeline.cycle_timeout", "error",
                        f"Orchestrator cycle timed out after {CYCLE_TIMEOUT}s",
                        {"timeout_sec": CYCLE_TIMEOUT,
                         "pipeline_stage": ckpt.get("stage") if ckpt else "unknown"})
                except Exception:
                    pass
                if ui:
                    return ui.gen_cost_total - _cost_at_start
                return total_cost

            # 529 rate-limit retry with exponential backoff
            if (
                not cycle_completed
                and _orch.looks_like_provider_error_envelope(full_output)
                and _orch._is_rate_limited(full_output)
            ):
                # Retry from the same sealed prompt and typed checkpoint/MCP
                # projection, but never from opaque provider conversation history.
                _orch._clear_orchestrator_session()
                retry_opts = _orch.ClaudeAgentOptions(
                    model="sonnet",
                    permission_mode="bypassPermissions",
                    cwd=str(_orch.PROJECT_ROOT),
                    mcp_servers={"evolution": _orch.evolution_server},
                    strict_mcp_config=True,
                    tools=[],
                    disallowed_tools=_BLOCKED_MCP_TOOLS,
                    hooks={**_orch._make_precompact_hook(), **_orch._make_bot_dir_guard_hook()},
                    max_turns=max_turns,
                    # CRITICAL: same as the main loop above — do NOT hardcode
                    # {"type": "adaptive"} (GLM death-loop).  Use the centralized
                    # _llm_thinking_options() so POK_LLM_THINKING_MODE controls
                    # this stream too.
                    **_llm_thinking_options(),
                )
                for backoff in [30, 60, 120]:
                    if ui:
                        ui.log_history(f"Orchestrator rate limited (529). Retrying in {backoff}s...", "warn")
                    lf.write(f"\n[529 RETRY] backing off {backoff}s\n")
                    if shutdown_mgr:
                        try:
                            await _orch.asyncio.wait_for(shutdown_mgr.wait_for_shutdown(), timeout=backoff)
                            return total_cost
                        except _orch.asyncio.TimeoutError:
                            pass
                    else:
                        await _orch.asyncio.sleep(backoff)
                    try:
                        full_output, retry_cost, cycle_completed, query_gen, auth_error = (
                            await _orch._await_orchestrator_stream_response_bounded(
                                _stream_response(retry_opts),
                                timeout=CYCLE_TIMEOUT,
                                attempt_ref=_attempt_ref,
                                gen_ref=_gen_ref,
                                log_file_path=log_file,
                            )
                        )
                    except _orch.asyncio.TimeoutError:
                        raise  # Re-raise to outer timeout handler
                    total_cost += retry_cost
                    if not (
                        _orch.looks_like_provider_error_envelope(full_output)
                        and _orch._is_rate_limited(full_output)
                    ):
                        break
                else:
                    # Every attempt used the same sealed checkpoint/prompt
                    # projection but a fresh provider stream.  Exhaustion is
                    # an infrastructure failure, never a successful cycle and
                    # never a reason to recover opaque provider history.
                    cycle_failed = True
                    infra_error = True
                    _orch.log.warning(
                        "529 retries exhausted; checkpoint preserved and "
                        "provider history discarded"
                    )
                    if ui:
                        ui.log_history(
                            "[Orchestrator] 529 retries exhausted. Checkpoint "
                            "preserved; the next attempt will use a fresh "
                            "checkpoint-bound provider stream.",
                            "warn",
                        )
                    lf.write(
                        "\n[529 RETRIES EXHAUSTED] checkpoint preserved; "
                        "provider history discarded\n"
                    )

            # 429 quota detected — exit cycle cleanly so orchestrator_loop can block
            from rate_limiter import rate_limiter
            if rate_limiter.is_blocked() and not cycle_completed:
                if ui:
                    ui.log_history(
                        "[Orchestrator] 429 配额耗尽。Checkpoint 保留；"
                        "provider history 已丢弃，恢复后使用新 stream 继续。",
                        "warn",
                    )
                return (ui.gen_cost_total - _cost_at_start) if ui else total_cost

            if ui:
                total_cost = ui.gen_cost_total - _cost_at_start
            if not cycle_failed:
                lf.write(f"\n[CYCLE DONE] cost=${total_cost:.4f}\n")

        except KeyboardInterrupt:
            if ui:
                ui.log_history("[Orchestrator] Interrupted by user.", "warn")
            else:
                _orch.log.warning("Interrupted by user.")
            lf.write("\n[INTERRUPTED]\n")

        except _orch.asyncio.CancelledError:
            # Signal in-flight native precommit work to stop after its current
            # complete 70-hand evidence unit.
            try:
                from tool_eval import set_precommit_shutdown
                set_precommit_shutdown()
            except Exception:
                pass
            # The validated checkpoint is preserved; provider history is not.
            if ui:
                ui.log_history("[Orchestrator] Cancelled — checkpoint preserved for a fresh provider stream.", "warn")
            else:
                _orch.log.warning("Cancelled — checkpoint preserved for a fresh provider stream.")
            lf.write("\n[CANCELLED — checkpoint preserved; provider history discarded]\n")
            raise

        except _orch._OrchActionableStageHandoff as e:
            # Normal pipeline handoff: a tool has already persisted a checkpoint
            # whose route_policy has a deterministic next tool. Stop the current
            # SDK stream and let the main loop route directly from the checkpoint.
            # _stream_response's finally has already completed owned cleanup.
            _orch._clear_orchestrator_session(reason="actionable_stage_handoff")
            if ui:
                ui.log_history(f"[Orchestrator] {e}", "info")
            else:
                _orch.log.info("%s", e)
            lf.write(f"\n[ACTIONABLE_HANDOFF] {e}\n")
            if e.handoff.get("recovery_blocked") is True:
                return _orch.ORCH_RECOVERY_BLOCKED_COST
            if e.handoff.get("scheduler_handoff_required") is True:
                if (
                    gen_ctx is not None
                    and not _orch._remember_verified_canonical_abandon(
                        gen_ctx,
                        e.handoff.get("terminal_proof"),
                    )
                ):
                    _orch.log.error(
                        "Canonical terminal handoff lacked an exact proof "
                        "bound to the active generation context."
                    )
                    return _orch.ORCH_RECOVERY_BLOCKED_COST
                return _orch.ORCH_GENERATION_ABANDONED_COST
            if e.handoff.get("operator_action_required") is True:
                return _orch.ORCH_OPERATOR_ACTION_REQUIRED_COST
            return _orch.ORCH_ACTIONABLE_HANDOFF_COST

        except _orch.OperatorGenerationCostLimitExceeded as e:
            # This is an operator-requested stop, not an API/SDK infrastructure
            # failure.  Preserve the generation checkpoint, discard the
            # disposable Claude session, and park the outer loop instead of
            # retrying every 15 seconds and spending past the same limit.
            _orch._clear_orchestrator_session(reason="operator_generation_cost_limit")
            if ui:
                ui.set_status("Stopped: operator generation cost limit", is_working=False)
                _orch._project_generation_cost_runtime(ui)
            lf.write(f"\n[OPERATOR_COST_LIMIT] {e}\n")
            return _orch.ORCH_OPERATOR_COST_LIMIT_COST

        except _orch.LLMAvailabilityBlocked as e:
            # Provider availability is control-plane state, not a failed bot or
            # a retryable SDK signature glitch. Owned cleanup has completed;
            # persist the typed pause and preserve every generation/Worker
            # checkpoint exactly as-is.
            _orch._clear_orchestrator_session(reason="llm_availability_blocked")
            try:
                pause_state = _orch.persist_llm_pause(e)
            except Exception as pause_error:
                pause_state = None
                _orch.log.exception("Failed to persist LLM availability pause: %s", pause_error)
            issue = e.issue
            if ui:
                ui.log_history(
                    f"[Orchestrator] LLM unavailable ({issue.category}); "
                    "checkpoint preserved and retry loop stopped.",
                    "error",
                )
                ui.set_status(
                    f"LLM unavailable ({issue.category})",
                    is_working=False,
                )
            try:
                _orch.log_system_event(
                    "orchestrator.llm_availability_blocked",
                    "error",
                    f"Orchestrator paused for LLM availability: {issue.category}",
                    {
                        "availability_issue": issue.as_dict(),
                        "pause_persisted": bool(pause_state),
                        "checkpoint_preserved": True,
                        "worker_attempt_consumed": False,
                    },
                )
            except Exception:
                pass
            lf.write(
                f"\n[LLM_AVAILABILITY_BLOCKED] {issue.category}: "
                f"{issue.summary}\n"
            )
            return _orch.ORCH_LLM_AVAILABILITY_BLOCKED_COST

        except _orch.LLMAvailabilityPauseError as e:
            # The Worker was already fenced, but its global pause record could
            # not be proven. Stop the current stream and let the outer sentinel
            # path fail closed; never translate this into a generic retry.
            _orch._clear_orchestrator_session(reason="llm_availability_state_invalid")
            if ui:
                ui.log_history(f"[Orchestrator] {e}", "error")
                ui.set_status("Stopped: invalid LLM availability state", is_working=False)
            try:
                _orch.log_system_event(
                    "orchestrator.llm_availability_state_stop",
                    "error",
                    str(e),
                    {
                        "checkpoint_preserved": True,
                        "worker_attempt_consumed": False,
                        "operator_action_required": True,
                    },
                )
            except Exception:
                pass
            lf.write(f"\n[LLM_AVAILABILITY_STATE_INVALID] {e}\n")
            return _orch.ORCH_LLM_AVAILABILITY_BLOCKED_COST

        except Exception as e:
            # Every stream exit runs the single owned cleanup in
            # _stream_response.finally. Cleanup failures arrive here as typed
            # ConnectionError infrastructure failures and are never suppressed.
            cycle_failed = True
            # P2: classify infra (SDK signature/timeout/connection) vs real business failure.
            # is_llm_infra_error is type-based (ClaudeSDKError, asyncio.TimeoutError,
            # ConnectionError, OSError) — same classifier used by tool_gates/agent_review
            # for critic/reviewer infra short-circuit (commit 5c14d01). Keyword fallback
            # remains for defense-in-depth (older SDK error formats).
            is_shutdown_cancel = (
                shutdown_mgr is not None
                and shutdown_mgr.is_shutting_down
                and _orch._is_shutdown_cancel_error(e)
            )
            is_infra = (
                not is_shutdown_cancel
                and (
                    isinstance(e, (_orch._OrchFirstActivityTimeout, _orch._OrchActionableStageTimeout, _orch._OrchStreamStallTimeout))
                    or _orch._is_cycle_infra_error(e)
                )
            )
            # SDK streaming errors (missing 'signature' field on thinking blocks,
            # observed with claude_agent_sdk 0.2.91 + adaptive thinking) leave the
            # provider stream broken — a fresh stream avoids replaying the same crash (the v84
            # quality_passed→run_review infinite-retry deadlock). P0 disabled
            # thinking so this no longer fires, but the guard prevents silent
            # recurrence if thinking is ever re-enabled or another SDK stream
            # error appears.
            if is_shutdown_cancel:
                shutdown_cancelled = True
                cycle_failed = False
                if ui:
                    ui.log_history(
                        "[Orchestrator] Claude stream stopped during shutdown; checkpoint preserved for a fresh provider stream.",
                        "warn",
                    )
                else:
                    _orch.log.warning("Claude stream stopped during shutdown; checkpoint preserved: %s", e)
                try:
                    _orch.log_system_event(
                        "orchestrator.shutdown_cancelled",
                        "info",
                        "Claude stream stopped during orchestrator shutdown; checkpoint preserved and provider history discarded",
                        {"exception_type": type(e).__name__, "error": str(e)[:500]},
                    )
                except Exception:
                    pass
            elif is_infra:
                infra_error = True  # ALSO set here — signature path must trigger -0.5 sentinel, not -1.0
                _orch._clear_orchestrator_session()
                if ui:
                    ui.log_history(
                        f"[Orchestrator] LLM infrastructure error ({type(e).__name__}, NOT auth) — "
                        "provider history discarded; next cycle will use a fresh checkpoint-bound stream.", "warn",
                    )
                else:
                    _orch.log.warning(
                        "LLM infra error (%s); fresh checkpoint-bound provider retry will be attempted: %s",
                        type(e).__name__, e,
                    )
                try:
                    _orch.log_system_event("pipeline.sdk_stream_error", "warn",
                        f"Orchestrator LLM infra error ({type(e).__name__}): {e}",
                        {"provider_history_discarded": True, "exception_type": type(e).__name__})
                except Exception:
                    pass
            else:
                # Checkpoint/transaction state remains available for recovery.
                if ui:
                    ui.log_history(f"[Orchestrator] Error: {e}", "error")
                else:
                    _orch.log.error("Error: %s", e)
            lf.write(f"\n[{'SHUTDOWN_CANCELLED' if is_shutdown_cancel else 'ERROR'}] {e}\n")

    return {
        'total_cost': total_cost,
        'cycle_completed': cycle_completed,
        'auth_error': auth_error,
        'cycle_failed': cycle_failed,
        'infra_error': infra_error,
        'shutdown_cancelled': shutdown_cancelled,
    }

