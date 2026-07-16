import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "web" / "core" / "prompts"


def test_orchestrator_prompt_uses_checkpoint_attempt_contract():
    prompt = (PROMPTS / "orchestrator.md").read_text(encoding="utf-8")

    assert "Do NOT keep a private `intra_gen_attempts` counter" in prompt
    assert "tool return fields are authoritative" in prompt
    assert "`generation_attempt`" in prompt
    assert "`precommit_attempt`" in prompt
    assert "Track `intra_gen_attempts`" not in prompt
    assert "Total intra_gen_attempts must not exceed" not in prompt


def test_orchestrator_prompt_keeps_critic_advisory_before_native_precommit():
    prompt = (PROMPTS / "orchestrator.md").read_text(encoding="utf-8")

    assert "Critic score is advisory" in prompt
    assert "always call `run_precommit_eval`" in prompt
    assert "native-TCP precommit" in prompt
    assert "Critic rejection is a hard strategy gate" not in prompt


def test_orchestrator_prompt_treats_master_error_as_blocking():
    prompt = (PROMPTS / "orchestrator.md").read_text(encoding="utf-8")
    normalized = " ".join(prompt.split())

    assert 'If the result contains `"error"` → Master FAILED' in prompt
    assert 'contains `"plan"` key and NO `"error"` key' in prompt
    assert "`worker_prompt` hard-size violations are BLOCKING" in normalized
    assert 'If the result contains `"plan"` key → Master SUCCEEDED' not in prompt
    assert "worker_prompt size warnings are ADVISORY" not in prompt


def test_orchestrator_prompt_crossover_commit_contract_is_consistent():
    prompt = (PROMPTS / "orchestrator.md").read_text(encoding="utf-8")

    assert "including a crossover generation" in prompt
    assert "supplies only the `prepared` baseline" in prompt
    assert "it never substitutes for Master or\n   Worker execution" in prompt
    assert "crossover performs\nno independent strategy mutation" in prompt
    assert "checkpoint is at `workers_done`" not in prompt


def test_orchestrator_prompt_separates_scheduler_selection_from_mcp_execution():
    prompt = (PROMPTS / "orchestrator.md").read_text(encoding="utf-8")
    normalized = " ".join(prompt.split())

    assert "outer code-layer scheduler exclusively owns `prepare_generation`" in normalized
    assert "It is not an MCP tool" in normalized
    assert "no checkpoint -> validated `selected` checkpoint" in normalized
    assert "`stage='selected'`" in normalized
    assert "`stage='preparing'` for exact idempotent recovery" in normalized
    assert "`next_tool=prepare_next_gen`" in normalized
    assert "`stage='timed_out'` checkpoint does not restart preparation" in normalized
    assert "`stage='infra_timed_out'` checkpoint" in normalized
    assert "A GenerationContext, version number, candidate directory" in normalized
    assert "provider_action=end_stream" in normalized
    assert "`end_stream` is a provider action, not a tool" in normalized
    assert "outer deterministic recovery path alone owns `run_archivist`" in normalized


def test_orchestrator_prompt_requires_exact_canonical_abandon_terminal_proof():
    prompt = (PROMPTS / "orchestrator.md").read_text(encoding="utf-8")

    for field in (
        "workflow_run_id",
        "abandoned=true",
        "cleared_checkpoint=true",
        "abandon_transaction_id",
        "abandon_receipt_digest",
        "finalize_receipt_digest",
        "abandon_checkpoint_identity",
    ):
        assert field in prompt
    normalized = " ".join(prompt.split())
    assert "exactly one canonical result returned by that owner tool" in normalized
    assert "flattened or nested" in normalized
    assert "current authorized owner tool performs canonical abandon" in normalized
    assert "checkpoint absence alone are not proof" in normalized


def test_orchestrator_role_registry_is_mcp_only_checkpoint_projection():
    import llm_query

    contract = llm_query.resolve_llm_role_contract("Orchestrator")

    assert contract.renderer == "prompts/orchestrator.md::_build_context"
    assert contract.template_paths == ("web/core/prompts/orchestrator.md",)
    assert contract.scope_policy == "orchestrator_mcp_only"
    assert contract.allowed_tool_sets == (frozenset(),)
    assert contract.allowed_mcp_servers == frozenset({"evolution"})
    assert contract.evidence_policy == "checkpoint_bound_typed_mcp_only"
    assert contract.history_policy == (
        "fresh_provider_session_from_checkpoint_projection_only"
    )


def _generation_context():
    return SimpleNamespace(
        current_v=143,
        next_v=144,
        source_v=143,
        strategy="single_parent",
        crossover_parents=(),
        stagnation_info="",
        match_analysis="",
        replay_spotlight="",
        performance_verification="",
    )


def test_generation_context_without_checkpoint_commands_end_stream(monkeypatch):
    import evolution_core
    import orchestrator_context
    import post_publication_handoff

    monkeypatch.setattr(evolution_core, "get_active_bots", lambda: ["national_v143"])
    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {"status": "none"},
    )

    context = orchestrator_context._build_context(gen_ctx=_generation_context())

    assert "NO ACTIVE PIPELINE CHECKPOINT" in context
    assert "PROVIDER ACTION: end_stream" in context
    assert "Make no MCP call" in context
    assert "outer-scheduler-owned" in context
    assert "Never call prepare_next_gen without that exact checkpoint" in context


def test_selected_context_authorizes_first_prepare_next_gen_materialization(monkeypatch):
    import evolution_core
    import orchestrator_context
    import post_publication_handoff

    checkpoint = {
        "workflow_run_id": "generation:144:workflow-v1",
        "checkpoint_revision": 3,
        "next_v": 144,
        "source_v": 143,
        "stage": "selected",
    }
    monkeypatch.setattr(evolution_core, "get_active_bots", lambda: ["national_v143"])
    monkeypatch.setattr(
        evolution_core,
        "read_pipeline_checkpoint",
        lambda: checkpoint,
    )
    monkeypatch.setattr(
        orchestrator_context,
        "route_policy",
        lambda _checkpoint: {
            "next_tool": "prepare_next_gen",
            "intent": "prepare",
            "directive": "Call exact selected route.",
        },
    )
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {"status": "none"},
    )

    context = orchestrator_context._build_context(gen_ctx=_generation_context())

    assert "PREPARE ROUTE AUTHORIZATION" in context
    assert "first materialization of the already-selected candidate" in context
    assert "exact runtime-validated checkpoint identity" in context
    assert "NO ACTIVE PIPELINE CHECKPOINT" not in context


def test_preparing_context_authorizes_only_fenced_prepare_recovery(monkeypatch):
    import evolution_core
    import orchestrator_context
    import post_publication_handoff

    checkpoint = {
        "workflow_run_id": "generation:144:workflow-v1",
        "checkpoint_revision": 4,
        "next_v": 144,
        "source_v": 143,
        "stage": "preparing",
    }
    monkeypatch.setattr(evolution_core, "get_active_bots", lambda: ["national_v143"])
    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(
        orchestrator_context,
        "route_policy",
        lambda _checkpoint: {
            "next_tool": "prepare_next_gen",
            "intent": "prepare_recovery",
            "directive": "Resume exact preparing route.",
        },
    )
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {"status": "none"},
    )

    context = orchestrator_context._build_context(gen_ctx=_generation_context())

    assert "PREPARE ROUTE AUTHORIZATION" in context
    assert "recovery of the interrupted preparation only while no" in context
    assert "unbound target preimage exists" in context
    assert "canonically abandon/quarantine" in context
    assert "adopting, deleting, or continuing" in context
    assert "prepare_next_gen is NOT authorized" not in context


def test_nonselected_context_explicitly_denies_prepare_next_gen(monkeypatch):
    import orchestrator_context

    monkeypatch.setattr(
        orchestrator_context,
        "route_policy",
        lambda _checkpoint: {
            "next_tool": "run_direction_audit",
            "intent": "pipeline",
            "directive": "Call direction audit.",
        },
    )
    lines = []
    orchestrator_context._format_checkpoint_info(
        {"next_v": 144, "source_v": 143, "stage": "prepared"},
        lines,
    )

    assert "prepare_next_gen is NOT authorized at this checkpoint stage" in "\n".join(lines)


def test_abandon_route_context_requires_current_head_terminal_proof(monkeypatch):
    import orchestrator_context

    monkeypatch.setattr(
        orchestrator_context,
        "route_policy",
        lambda _checkpoint: {
            "next_tool": "abandon_generation",
            "intent": "system_bootstrap_abandon",
            "directive": "Call the canonical owner.",
        },
    )
    lines = []
    orchestrator_context._format_checkpoint_info(
        {"next_v": 143, "source_v": 142, "stage": "precommit_failed"},
        lines,
    )
    context = "\n".join(lines)

    assert "CANONICAL ABANDON ROUTE" in context
    assert "current authorized owner tool" in context
    assert "flattened or nested" in context
    assert "workflow_run_id" in context
    assert "finalize_receipt_digest" in context
    assert "Duplicate flattened/nested results" in context


def test_timeout_context_routes_abandon_and_infra_retry_without_prepare(monkeypatch):
    import orchestrator_context

    route_by_stage = {
        "timed_out": {
            "next_tool": "abandon_generation",
            "intent": "timeout_abandon",
            "directive": "Abandon the timed-out generation.",
        },
        "infra_timed_out": {
            "next_tool": "run_precommit_eval",
            "intent": "infra_precommit_recovery",
            "directive": "Retry exact precommit evaluation.",
        },
    }
    monkeypatch.setattr(
        orchestrator_context,
        "route_policy",
        lambda checkpoint: route_by_stage[checkpoint["stage"]],
    )

    timed_out_lines = []
    orchestrator_context._format_checkpoint_info(
        {"next_v": 144, "source_v": 143, "stage": "timed_out"},
        timed_out_lines,
    )
    infra_lines = []
    orchestrator_context._format_checkpoint_info(
        {"next_v": 144, "source_v": 143, "stage": "infra_timed_out"},
        infra_lines,
    )

    timed_out_context = "\n".join(timed_out_lines)
    infra_context = "\n".join(infra_lines)
    assert "TIMEOUT ACTIVE LEASE" in timed_out_context
    assert "canonical abandon_generation" in timed_out_context
    assert "Never call prepare_next_gen" in timed_out_context
    assert "not a dead/restartable checkpoint" in timed_out_context
    assert "INFRASTRUCTURE TIMEOUT ACTIVE LEASE" in infra_context
    assert "retry only run_precommit_eval" in infra_context
    assert "quality/review/critic gate identities" in infra_context
    assert "quality fingerprint = repair baseline = live bytes" in infra_context
    assert "do not prepare or strategically rework" in infra_context


def test_checkpoint_without_authorized_route_commands_end_stream(monkeypatch):
    import orchestrator_context

    monkeypatch.setattr(
        orchestrator_context,
        "route_policy",
        lambda _checkpoint: {
            "next_tool": None,
            "intent": "blocked",
            "directive": "Checkpoint validation failed.",
        },
    )
    lines = []
    orchestrator_context._format_checkpoint_info(
        {"next_v": 144, "source_v": 143, "stage": "selected"},
        lines,
    )

    assert "NO AUTHORIZED CHECKPOINT ROUTE" in "\n".join(lines)
    assert "PROVIDER ACTION: end_stream" in "\n".join(lines)


def test_archivist_route_is_outer_owned_provider_end_stream(monkeypatch):
    import orchestrator_context

    monkeypatch.setattr(
        orchestrator_context,
        "route_policy",
        lambda _checkpoint: {
            "next_tool": "run_archivist",
            "intent": "post_publication_cleanup",
            "directive": "Resume cleanup.",
        },
    )
    lines = []
    orchestrator_context._format_checkpoint_info(
        {"next_v": 144, "source_v": 143, "stage": "archived"},
        lines,
    )
    context = "\n".join(lines)

    assert "PROVIDER ACTION: end_stream" in context
    assert "outer deterministic recovery path alone owns run_archivist" in context
    assert "Next MCP tool: run_archivist" not in context
    assert "make no MCP call" in context


def test_precompact_without_checkpoint_preserves_end_stream_action(monkeypatch):
    import epoch_authority
    import evolution_core
    import orchestrator_context
    import post_publication_handoff

    monkeypatch.setattr(
        epoch_authority,
        "strict_epoch_projection",
        lambda: {
            "current_v": 143,
            "active_generation": False,
            "ignored_checkpoint": None,
            "initialized": True,
        },
    )
    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {"status": "none"},
    )
    hook = orchestrator_context._make_precompact_hook()["PreCompact"][0].hooks[0]

    output = asyncio.run(hook({}, "compact-1", None))

    assert "PROVIDER ACTION: end_stream" in output["reason"]
    assert (
        "outer scheduler alone may later call non-MCP prepare_generation"
        in output["reason"]
    )


def test_generation_context_prioritizes_postpublication_handoff_over_prepare(monkeypatch):
    import evolution_core
    import orchestrator_context
    import post_publication_handoff

    monkeypatch.setattr(evolution_core, "get_active_bots", lambda: ["national_v143"])
    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {
            "status": "pending",
            "version": 144,
            "source_v": 143,
            "state": "running",
        },
    )

    context = orchestrator_context._build_context(gen_ctx=_generation_context())

    assert "POST-PUBLICATION HANDOFF ACTIVE" in context
    assert "PROVIDER ACTION: end_stream" in context
    assert "outer deterministic recovery path alone owns run_archivist" in context
    assert "NO ACTIVE PIPELINE CHECKPOINT" not in context
    assert "may later call non-MCP prepare_generation" not in context


def test_postpublication_handoff_suppresses_conflicting_checkpoint_route(monkeypatch):
    import evolution_core
    import orchestrator_context
    import post_publication_handoff

    checkpoint = {
        "workflow_run_id": "generation:144:workflow-v1",
        "checkpoint_revision": 9,
        "next_v": 144,
        "source_v": 143,
        "stage": "selected",
    }
    monkeypatch.setattr(evolution_core, "get_active_bots", lambda: ["national_v143"])
    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(
        orchestrator_context,
        "route_policy",
        lambda _checkpoint: {
            "next_tool": "prepare_next_gen",
            "intent": "prepare",
            "directive": "Prepare candidate.",
        },
    )
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {
            "status": "pending",
            "version": 143,
            "source_v": 142,
            "state": "pending",
        },
    )

    context = orchestrator_context._build_context(gen_ctx=_generation_context())

    assert "POST-PUBLICATION HANDOFF ACTIVE" in context
    assert "PROVIDER ACTION: end_stream" in context
    assert "PIPELINE CHECKPOINT" not in context
    assert "PREPARE ROUTE AUTHORIZATION" not in context


def test_precompact_projects_blocked_postpublication_before_no_checkpoint(monkeypatch):
    import epoch_authority
    import evolution_core
    import orchestrator_context
    import post_publication_handoff

    monkeypatch.setattr(
        epoch_authority,
        "strict_epoch_projection",
        lambda: {
            "current_v": 143,
            "active_generation": False,
            "ignored_checkpoint": None,
            "initialized": True,
        },
    )
    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {"status": "blocked", "issues": ["receipt drift"]},
    )
    hook = orchestrator_context._make_precompact_hook()["PreCompact"][0].hooks[0]

    output = asyncio.run(hook({}, "compact-2", None))

    assert "POST-PUBLICATION HANDOFF BLOCKED/AMBIGUOUS" in output["reason"]
    assert "receipt drift" in output["reason"]
    assert "PROVIDER ACTION: end_stream" in output["reason"]
    assert "NO ACTIVE PIPELINE CHECKPOINT" not in output["reason"]
    assert "may later call non-MCP prepare_generation" not in output["reason"]


def test_precompact_never_hands_archivist_route_to_provider(monkeypatch):
    import epoch_authority
    import evolution_core
    import orchestrator_context

    checkpoint = {
        "workflow_run_id": "generation:144:workflow-v1",
        "checkpoint_revision": 12,
        "next_v": 144,
        "source_v": 143,
        "stage": "archived",
    }
    monkeypatch.setattr(
        epoch_authority,
        "strict_epoch_projection",
        lambda: {
            "current_v": 144,
            "active_generation": True,
            "ignored_checkpoint": None,
            "initialized": True,
        },
    )
    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(
        orchestrator_context,
        "route_policy",
        lambda _checkpoint: {
            "next_tool": "run_archivist",
            "intent": "post_publication_cleanup",
            "directive": "Resume cleanup.",
        },
    )
    hook = orchestrator_context._make_precompact_hook()["PreCompact"][0].hooks[0]

    output = asyncio.run(hook({}, "compact-3", None))

    assert "ACTIVE POST-PUBLICATION CHECKPOINT" in output["reason"]
    assert "PROVIDER ACTION: end_stream" in output["reason"]
    assert "Outer deterministic recovery alone owns run_archivist" in output["reason"]
    assert "Next tool: run_archivist" not in output["reason"]


def test_llm_stages_documents_active_strict_control_contract():
    stages = (ROOT / "docs" / "llm-stages.md").read_text(encoding="utf-8")

    assert "## One active protocol" in stages
    assert "Candidate code may edit only `policy.py`" in stages
    assert "typed-intent validator" in stages
    assert "Master proposal ensemble" in stages
    assert "Precommit evaluation" in stages
    assert "Official certification" in stages


def test_llm_stages_documents_checkpoint_and_evaluation_authority():
    stages = (ROOT / "docs" / "llm-stages.md").read_text(encoding="utf-8")
    normalized = " ".join(stages.split())

    assert "immutable workflow run ID and monotonic CAS revision" in normalized
    assert "replays frozen inputs" in normalized
    assert "Critic" in normalized and "score is not an acceptance threshold" in normalized
    assert "five 70-hand self-play rounds and three 70-hand rounds" in normalized
    assert "Official chip results have zero strength weight" in normalized


def test_main_orchestrator_has_mcp_tools_but_no_builtin_tools_on_all_dispatches():
    source = (ROOT / "web" / "core" / "orchestrator.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    options_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ClaudeAgentOptions"
    ]
    # Initial dispatch and the 529 retry must have identical tool authority.
    assert len(options_calls) == 2
    for call in options_calls:
        keywords = {item.arg: item.value for item in call.keywords if item.arg}
        assert isinstance(keywords.get("tools"), ast.List)
        assert keywords["tools"].elts == []
        servers = keywords.get("mcp_servers")
        assert isinstance(servers, ast.Dict)
        assert [key.value for key in servers.keys] == ["evolution"]
