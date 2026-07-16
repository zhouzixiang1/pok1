import ast
import asyncio
from dataclasses import replace
import hashlib
import importlib
import json
from pathlib import Path

import pytest
from claude_agent_sdk import ResultMessage


ROOT = Path(__file__).resolve().parents[2]


class _UI:
    def __init__(self):
        self.gen_cost_total = 0.0

    def log_history(self, *_args, **_kwargs):
        return None

    def log_io(self, *_args, **_kwargs):
        return None

    def emit_tool_call(self, *_args, **_kwargs):
        return None

    def update_cost(self, *_args, **_kwargs):
        return None

    def set_status(self, *_args, **_kwargs):
        return None


# Independent expectations: these values are deliberately not derived from the
# production registry under test.
CASES = (
    ("MASTER PROPOSAL mechanism", "master_proposal", ["Read"], "agent_master", "_render_master_proposal_provider_prompt", "web/core/agent_master.py", "master_planning_context", "agent_master.py::_run_master_proposal_ensemble/proposal_renderer"),
    ("MASTER PROPOSAL CRITIC mechanism", "master_proposal_critic", [], "agent_master", "_render_master_proposal_critic_provider_prompt", "web/core/agent_master.py", "frozen_proposal_packet", "agent_master.py::_run_master_proposal_ensemble/critic_renderer"),
    ("MASTER (Try 1)", "master_final", [], "agent_master", "_render_master_final_provider_prompt", "web/core/agent_master.py", "compiled_master_context", "prompts/master_prompt.md+master_context_contract.py"),
    ("WORKER_COT_CHECK_W1", "worker_cot_audit", [], "audit_agents", "_render_worker_cot_provider_prompt", "web/core/audit_agents.py", "worker_output_diff", "prompts/worker_cot_check.md::_run_worker_cot_check"),
    ("WORKER W1 (logic)", "worker", ["Bash", "Read", "Edit"], "agent_workers", "_render_worker_provider_prompt", "web/core/agent_workers.py", "compiled_worker_task", "prompts/worker_prompt.md+prompts/worker_profile_national_native.md"),
    ("DEBUG AGENT (v150)", "debug_agent", ["Read"], "agent_workers", "_render_debug_provider_prompt", "web/core/agent_workers.py", "worker_gate_failure", "prompts/debug_worker_prompt.md::_run_debug_agent"),
    ("LEAD CODE REVIEWER", "lead_code_reviewer", ["Bash", "Read"], "tool_gates", "_render_reviewer_provider_prompt", "web/core/tool_gates.py", "review_candidate_pair", "prompts/reviewer_prompt.md::run_review"),
    ("STRATEGY CRITIC", "strategy_critic", ["Read"], "agent_review", "_render_critic_provider_prompt", "web/core/agent_review.py", "critic_plan_candidate_snapshot", "prompts/critic_prompt.md::_run_critic"),
    ("CROSSOVER_COMPAT_143x144", "crossover_compatibility", [], "audit_agents", "_render_crossover_compat_provider_prompt", "web/core/audit_agents.py", "crossover_parent_compatibility", "prompts/crossover_compatibility.md::_run_crossover_compatibility_audit"),
    ("CROSSOVER v143×v144→v145 [call]", "crossover", ["Bash", "Read", "Edit"], "agent_review", "_render_crossover_provider_prompt", "web/core/agent_review.py", "crossover_frozen_parents", "prompts/crossover_prompt.md::_run_crossover"),
    ("DIRECTION AUDITOR", "direction_auditor", [], "direction_auditor", "_render_direction_provider_prompt", "web/core/direction_auditor.py", "annotated_completion_direction_history", "prompts/direction_auditor_prompt.md::_run_direction_audit"),
    ("LITERATURE_PROBE (v145)", "literature_probe", ["WebSearch"], "tool_planning", "_render_literature_provider_prompt", "web/core/tool_planning.py", "governed_literature_brief", "prompts/literature_probe_prompt.md::run_literature_probe"),
    ("CYCLE ARCHIVIST", "cycle_archivist", [], "cycle_archivist", "_render_cycle_archivist_provider_prompt", "web/core/cycle_archivist.py", "content_bound_cycle_snapshot", "prompts/cycle_archivist.md::run_cycle_archivist_analysis"),
    ("MASTER_PLAN_AUDIT", "master_plan_audit", [], "audit_agents", "_render_master_plan_audit_provider_prompt", "web/core/audit_agents.py", "compiled_plan_completion_history", "prompts/master_plan_audit.md::_run_master_plan_audit"),
    ("DEGENERATION_DIAGNOSIS", "degeneration_diagnosis", [], "audit_agents", "_render_degeneration_provider_prompt", "web/core/audit_agents.py", "frozen_degeneration_window", "prompts/degeneration_diagnosis.md::_run_degeneration_diagnosis"),
    ("COMBINED ANALYST", "combined_analyst", [], "combined_analyst", "_render_combined_provider_prompt", "web/core/combined_analyst.py", "immutable_generation_evaluation_bundle", "prompts/combined_analyst.md::_run_combined_analysis"),
    ("OFFICIAL PLATFORM COMPLIANCE ANALYST", "official_platform_analysis", [], "official_llm_analysis", "_render_official_provider_prompt", "web/core/official_llm_analysis.py", "compact_official_compliance_evidence", "prompts/official_platform_analysis.md::build_official_analysis_prompt"),
    ("OPERATOR SDK PROBE", "operator_sdk_probe", ["Read", "Bash"], "operator_sdk_probe", "_render_operator_probe_provider_prompt", "web/core/operator_sdk_probe.py", "operator_exact_file_probe", "operator_sdk_probe.py::build_probe_prompt"),
)


EXPECTED_PROVENANCE_FIELDS = {
    "orchestrator": ("context_digest", "dry_run"),
    "master_proposal_critic": (
        "proposal_packet_digest", "proposal_name", "criteria_digest",
        "planning_context_digest", "lens_digest", "evidence_mode", "schema_retry", "invocation_id",
    ),
    "master_proposal": (
        "planning_context_digest", "direction", "source_v", "next_v",
        "source_symbol_index_digest", "directive_digest",
        "protocol_bootstrap_prepared_only", "singleton_no_strength",
        "evidence_mode", "repair_kind", "projection_hints",
        "invocation_id",
    ),
    "master_final": (
        "master_context_digest", "proposal_packet_digest", "source_v", "next_v",
        "template_values_digest", "schema_repair_digest", "invocation_id",
    ),
    "worker_cot_audit": (
        "task_digest", "diff_digest", "worker_output_digest", "worker_role_digest",
        "worker_task_digest", "diff_metadata_digest",
        "worker_output_binding_digest", "worker_effect_id",
        "worker_lease_epoch", "worker_dispatch_receipt_digest",
    ),
    "worker": (
        "task", "next_v", "candidate_path", "allowed_files",
        "renderer_inputs_digest",
    ),
    "debug_agent": (
        "candidate_path", "error_digest", "changed_diff_digest", "target_file", "next_v",
    ),
    "lead_code_reviewer": ("source_v", "next_v", "review_prompt_digest"),
    "strategy_critic": (
        "source_v", "next_v", "master_plan_digest", "code_evidence_digest",
        "h2h_snapshot_digest", "previous_critic_digest", "invocation_id",
    ),
    "crossover_compatibility": (
        "parent_a_v", "parent_b_v", "parent_code_digest", "rating_context_digest",
        "h2h_context_digest", "architecture_context_digest",
        "parent_snapshot_receipt_digest",
    ),
    "crossover": (
        "parent_a_v", "parent_b_v", "target_v", "parent_artifacts",
        "compatibility_receipt_digest", "renderer_inputs_digest",
    ),
    "direction_auditor": ("source_v", "generation_history_digest"),
    "literature_probe": ("source_v", "next_v", "brief_digest"),
    "cycle_archivist": ("version", "source_v", "snapshot_digest"),
    "master_plan_audit": (
        "source_v", "next_v", "plan_digest", "history_digest",
        "direction_audit_digest", "h2h_snapshot_digest",
    ),
    "degeneration_diagnosis": (
        "source_v", "history_digest", "rating_digest", "strategy_changes_digest",
    ),
    "combined_analyst": ("source_v", "frozen_bundle_digest"),
    "official_platform_analysis": ("evidence_id", "compact_evidence_digest"),
    "operator_sdk_probe": ("repo_root", "local_evidence_digest"),
}


def _renderer_inputs(role_id, marker):
    combined_values = {
        "bot_name": marker,
        "opp_eval": "1",
        "opp_total": "1",
        "opp_coverage": "100%",
        "rd_warning": "",
        "top_bots": "none",
        "generation_trend": "none",
        "lineage": "none",
        "daemon_history": "none",
        "bot_stats": "none",
        "h2h_results": "none",
    }
    values = {
        "master_proposal": {"planning_context": marker, "direction": "mechanism", "directive": "structural mechanism", "source_v": 143, "next_v": 145, "protocol_bootstrap_prepared_only": False, "singleton_no_strength": False, "source_symbol_index": "policy.py:decide", "repair_kind": "", "projection_hints": [], "invocation_id": "1" * 32},
        "master_proposal_critic": {"proposal_name": "mechanism", "lens": marker, "planning_context_digest": "1" * 64, "proposals": [{"proposal_id": "p1"}], "criteria": {"falsifiability": "required"}, "evidence_mode": "frozen_strength_snapshot", "schema_retry": False, "invocation_id": "2" * 32},
        "master_final": {"template_values": {}, "master_context": marker, "proposal_ensemble": "{}", "source_v": 143, "next_v": 145, "invocation_id": "", "schema_repair_suffix": ""},
        "worker_cot_audit": {
            "task": {"target_files": ["policy.py"]},
            "worker_role": "logic",
            "worker_task": "edit policy",
            "worker_output_evidence": __import__("audit_agents").bind_fenced_worker_output(
                task={"target_files": ["policy.py"]},
                worker_id="W1",
                next_v=145,
                source_v=143,
                worker_effect_identity={
                    "workflow_run_id": "generation:145:workflow-v1",
                    "envelope_digest": "3" * 64,
                    "effect_id": "effect-worker-cot",
                    "lease_epoch": 1,
                },
                attempt=1,
                dispatch_receipt_digest="4" * 64,
                output=marker,
            ).payload,
            "code_diff": "+change",
            "diff_metadata": "policy.py changed",
        },
        "worker": {"task": {"target_files": ["policy.py"]}, "next_v": 145, "source_v": 143, "candidate_path": str(ROOT / "web/core/results/workflow/artifacts/workspaces" / ("a" * 64)), "allowed_files": ["policy.py"], "reviewer_feedback": marker, "attempt_note": "", "retry_guidance": "", "role": "logic"},
        "debug_agent": {"error_output": marker, "changed_diff": "+change", "target_file": "policy.py", "next_v": 145, "candidate_path": str(ROOT / "web/core/results/workflow/artifacts/workspaces" / ("a" * 64))},
        "lead_code_reviewer": {"master_plan": {"analysis": marker}, "source_v": 143, "next_v": 145, "strict_bootstrap": False, "invocation_id": "", "focus_areas": []},
        "strategy_critic": {"source_v": 143, "next_v": 145, "master_plan": marker, "code_evidence": {"lineage_contract": "lineage", "evaluation_steps": "steps", "prompt_section": "diff"}, "h2h_snapshot_contract": "snapshot", "previous_critic": None, "invocation_id": ""},
        "crossover_compatibility": {"parent_a_v": 143, "parent_b_v": 144, "parent_a_code": {"policy.py": marker}, "parent_b_code": {"policy.py": "b"}, "parent_a_rating": "unknown", "parent_b_rating": "unknown", "h2h_context": "unknown", "architecture_context": {}, "parent_snapshot_receipt": {"receipt_digest": "d" * 64}},
        "crossover": {"parent_a_v": 143, "parent_b_v": 144, "target_v": 145, "parent_artifacts": ["b" * 64, "c" * 64], "compatibility_receipt": {"compatible": True}, "capability_context": {}, "h2h_snapshot_contract": marker, "architecture_policy": {}, "frozen_parent_a_dir": str(ROOT / "web/core/results/workflow/artifacts" / ("b" * 64)), "frozen_parent_b_dir": str(ROOT / "web/core/results/workflow/artifacts" / ("c" * 64)), "retry_feedback": ""},
        "direction_auditor": {"generation_history": marker, "source_v": 143},
        "literature_probe": {"source_v": 143, "next_v": 145, "weakness": marker, "stagnation_info": "stagnant"},
        "cycle_archivist": {
            "snapshot": {
                "schema_version": 1,
                "kind": "national-policy-cycle-archivist-prompt-projection",
                "evaluation_epoch": "national_tcp_policy_v1",
                "version": 145,
                "source_v": 143,
                "bot_name": "national_v145",
                "git_tag": "national-bot-v145",
                "publication": {
                    "publication_id": "a" * 64,
                    "commit_oid": "b" * 40,
                    "candidate_artifact_hash": "c" * 64,
                },
                "strength_evidence_identity": {"marker": marker},
                "gate_summary": {
                    "review_score": 9,
                    "critic_score": 8,
                    "precommit_passed": True,
                },
                "post_publication_handoff": {
                    "identity_digest": "d" * 64,
                    "publication_id": "a" * 64,
                },
            },
            "version": 145,
            "source_v": 143,
        },
        "master_plan_audit": {"source_v": 143, "next_v": 145, "master_plan": {"analysis": marker}, "recent_commits": "none", "direction_audit": "none", "h2h_snapshot_contract": "snapshot"},
        "degeneration_diagnosis": {"source_v": 143, "recent_commits": marker, "strategy_changes": "change", "rating_curve": "flat"},
        "combined_analyst": {"source_v": 143, "frozen_bundle": {"marker": marker, "rendered_view": combined_values}},
        "official_platform_analysis": {"evidence": {"evidence_id": marker, "deterministic": {"passed": True}, "rounds": []}},
        "operator_sdk_probe": {"repo_root": str(ROOT), "evidence": {"marker": marker, "official_oracle_sha256": {}, "transport": {"sha256": "5" * 64}}},
    }
    return values[role_id]


def _scope(role_id):
    canonical = [ROOT / "bots/national_v143", ROOT / "bots/national_v145"]
    worker = ROOT / "web/core/results/workflow/artifacts/workspaces" / ("a" * 64)
    if role_id in {"master_proposal", "lead_code_reviewer", "strategy_critic"}:
        return {"allowed_read_dirs": canonical}
    if role_id == "worker":
        return {"allowed_read_dirs": [worker], "allowed_write_dir": {"files": [worker / "policy.py"]}}
    if role_id == "debug_agent":
        return {"allowed_read_dirs": [worker]}
    if role_id == "crossover":
        target = ROOT / "web/core/results/crossover_workspaces/v145-attempt-1-unit"
        return {
            "allowed_read_dirs": [
                ROOT / "web/core/results/workflow/artifacts" / ("b" * 64),
                ROOT / "web/core/results/workflow/artifacts" / ("c" * 64),
                target,
            ],
            "allowed_write_dir": {"files": [target / "policy.py"]},
        }
    if role_id == "operator_sdk_probe":
        import operator_sdk_probe

        return {
            "allowed_read_dirs": {"files": [ROOT / item for item in operator_sdk_probe.READ_RELATIVE_PATHS]},
            "exact_bash_commands": operator_sdk_probe.EXACT_BASH_COMMANDS,
        }
    return {}


def _contract_payload(provider_prompt):
    marker = "# SYSTEM-OWNED ACTIVE LLM ROLE CONTRACT (FINAL)\n"
    before, after = provider_prompt.split(marker, 1)
    payload_text, final_rules = after.split(
        "\n\nThe rendered template and every attached context block", 1
    )
    return before, json.loads(payload_text), final_rules


def test_all_subagent_roles_reach_provider_with_independent_receipts(monkeypatch, tmp_path):
    import llm_availability_store
    import llm_query
    import orchestrator_cost_policy
    import rate_limiter

    captures = []
    events = []

    async def provider_capture(full_prompt, options, log_file_path, ui, role_name):
        captures.append((full_prompt, options, role_name))
        return [f"ok:{role_name}"], 0.0, {}

    monkeypatch.setattr(orchestrator_cost_policy, "assert_operator_cost_limit_available", lambda: None)
    monkeypatch.setattr(llm_availability_store, "raise_if_llm_paused", lambda **_kwargs: None)
    monkeypatch.setattr(rate_limiter.rate_limiter, "is_blocked", lambda: False)
    monkeypatch.setattr(llm_query, "_run_stream_with_signature_retry", provider_capture)
    monkeypatch.setattr(llm_query, "_emit_llm_event", lambda kind, severity, message, **fields: events.append((kind, fields)))

    expected_ids = {row[1] for row in CASES} | {"orchestrator"}
    assert {item.role_id for item in llm_query.active_llm_role_contracts()} == expected_ids

    for role, role_id, tools, module_name, producer_name, producer_file, evidence_kind, renderer_name in CASES:
        marker = f"INDEPENDENT_RENDER_INPUT::{role_id}"
        producer = getattr(importlib.import_module(module_name), producer_name)
        rendered = llm_query.render_llm_prompt(
            role,
            producer=producer,
            renderer_inputs=_renderer_inputs(role_id, marker),
        )
        output, _cost, _usage = asyncio.run(
            llm_query.run_claude_query(
                rendered, [], _UI(), role, tmp_path / f"{role_id}.log",
                tools=list(tools), **_scope(role_id),
            )
        )
        assert output == f"ok:{role}"
        provider_prompt, options, captured_role = captures[-1]
        before, payload, rules = _contract_payload(provider_prompt)
        assert captured_role == role
        assert rendered.text in before
        assert payload["role_id"] == role_id
        assert payload["model"] == "sonnet"
        assert payload["frozen_capability"]["model"] == "sonnet"
        assert payload["renderer"] == renderer_name
        assert payload["renderer_producer_file"] == producer_file
        assert payload["evidence_provenance_kind"] == evidence_kind
        assert payload["evidence_provenance_sha256"] == rendered.dispatch_receipt.evidence.provenance_sha256
        contract = llm_query.resolve_llm_role_contract(role)
        assert contract.required_evidence_fields == EXPECTED_PROVENANCE_FIELDS[role_id]
        provenance = json.loads(rendered.dispatch_receipt.evidence.provenance_json)
        assert set(contract.required_evidence_fields).issubset(provenance)
        assert payload["dispatch_receipt_digest"] == rendered.dispatch_receipt.receipt_digest
        assert payload["selected_builtin_tools"] == list(tools)
        assert payload["selected_mcp_servers"] == []
        assert payload["strength_authority"] == "zero"
        assert payload["rating_authority"] == "zero"
        assert payload["certification_authority"] == "zero"
        assert payload["historical_memory_authority"] == "zero"
        assert before.endswith("\n\n")
        prefix = before[:-2]
        assert payload["rendered_provider_prefix_sha256"] == hashlib.sha256(prefix.encode()).hexdigest()
        assert payload["frozen_capability_sha256"] == hashlib.sha256(
            json.dumps(payload["frozen_capability"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert "archive/legacy content" in rules
        assert options.tools == list(tools)
        assert options.mcp_servers == {}
        assert options.model == "sonnet"

    starts = [fields for kind, fields in events if kind == "pipeline.llm_role_start"]
    assert [item["role_contract_id"] for item in starts] == [row[1] for row in CASES]


def _combined_rendered(text="prompt"):
    import combined_analyst
    import llm_query

    return llm_query.render_llm_prompt(
        "COMBINED ANALYST",
        producer=combined_analyst._render_combined_provider_prompt,
        renderer_inputs=_renderer_inputs("combined_analyst", text),
    )


def test_all_registered_producers_reject_caller_rendered_sections_even_with_valid_hash():
    import llm_query

    arbitrary = "caller-owned complete provider prompt"
    section = [{
        "name": "correct-looking-section",
        "content": arbitrary,
        "content_sha256": hashlib.sha256(arbitrary.encode("utf-8")).hexdigest(),
    }]
    for role, role_id, _tools, module_name, producer_name, *_rest in CASES:
        producer = getattr(importlib.import_module(module_name), producer_name)
        forged_inputs = dict(_renderer_inputs(role_id, f"RAW::{role_id}"))
        forged_inputs["sections"] = section
        with pytest.raises(
            llm_query.LLMRoleContractError,
            match="production renderer failed",
        ):
            llm_query.render_llm_prompt(
                role,
                producer=producer,
                renderer_inputs=forged_inputs,
            )
    import orchestrator
    from tools import evolution_server

    with pytest.raises(
        llm_query.LLMRoleContractError,
        match="production renderer failed",
    ):
        llm_query.render_llm_prompt(
            "Orchestrator",
            producer=orchestrator._render_orchestrator_provider_prompt,
            renderer_inputs={
                "context": "typed context",
                "dry_run": False,
                "sections": section,
            },
            mcp_servers={"evolution": evolution_server},
        )


def test_typed_renderer_rejects_tampered_text_and_forged_provenance(monkeypatch):
    import llm_query

    called = False

    async def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        return ["bad"], 0.0, {}

    monkeypatch.setattr(llm_query, "_run_stream_with_signature_retry", forbidden)
    original = _combined_rendered()
    forged_text = llm_query.RenderedLLMPrompt(
        role_id=original.role_id,
        runtime_role=original.runtime_role,
        text=original.text + "\nFORGED",
        renderer_inputs_json=original.renderer_inputs_json,
        dispatch_receipt=original.dispatch_receipt,
        producer=original.producer,
        _authority=original._authority,
    )
    with pytest.raises(llm_query.LLMRoleContractError, match="replay mismatch"):
        asyncio.run(llm_query.run_claude_query(forged_text, [], _UI(), "COMBINED ANALYST", None))

    forged_evidence = replace(
        original.dispatch_receipt.evidence,
        provenance_json='{"source_v":143,"frozen_bundle_digest":"forged"}',
    )
    forged_dispatch = replace(original.dispatch_receipt, evidence=forged_evidence)
    forged_provenance = llm_query.RenderedLLMPrompt(
        role_id=original.role_id,
        runtime_role=original.runtime_role,
        text=original.text,
        renderer_inputs_json=original.renderer_inputs_json,
        dispatch_receipt=forged_dispatch,
        producer=original.producer,
        _authority=original._authority,
    )
    with pytest.raises(llm_query.LLMRoleContractError, match="provenance"):
        asyncio.run(llm_query.run_claude_query(forged_provenance, [], _UI(), "COMBINED ANALYST", None))
    assert called is False


def test_unknown_role_and_tool_scope_drift_fail_before_provider(monkeypatch):
    import llm_query

    monkeypatch.setattr(llm_query, "_run_stream_with_signature_retry", lambda *_a, **_k: pytest.fail("provider called"))
    with pytest.raises(llm_query.LLMRoleContractError, match="unregistered"):
        asyncio.run(llm_query.run_claude_query("raw", [], _UI(), "RETIRED MATCH ANALYST", None))
    with pytest.raises(llm_query.LLMRoleContractError, match="tools"):
        asyncio.run(llm_query.run_claude_query(_combined_rendered(), [], _UI(), "COMBINED ANALYST", None, tools=["Read"]))


def test_model_drift_fails_before_provider(monkeypatch):
    import llm_query

    called = False

    async def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        return ["bad"], 0.0, {}

    monkeypatch.setattr(llm_query, "_run_stream_with_signature_retry", forbidden)
    with pytest.raises(llm_query.LLMRoleContractError, match="model"):
        asyncio.run(
            llm_query.run_claude_query(
                _combined_rendered(),
                [],
                _UI(),
                "COMBINED ANALYST",
                None,
                model="opus",
            )
        )
    assert called is False


def test_combined_bundle_rejects_separate_poisoned_render_view():
    import combined_analyst
    import llm_query

    inputs = _renderer_inputs("combined_analyst", "valid frozen view")
    inputs["template_values"] = {
        "h2h_results": "POISON LIVE H2H",
    }
    with pytest.raises(llm_query.LLMRoleContractError, match="renderer failed"):
        llm_query.render_llm_prompt(
            "COMBINED ANALYST",
            producer=combined_analyst._render_combined_provider_prompt,
            renderer_inputs=inputs,
        )


def test_registered_template_path_ignores_monkeypatched_prompt_directory(
    monkeypatch, tmp_path
):
    import combined_analyst

    (tmp_path / "combined_analyst.md").write_text(
        "POISON ALTERNATE TEMPLATE {bot_name}",
        encoding="utf-8",
    )
    monkeypatch.setattr(combined_analyst, "PROMPTS_DIR", tmp_path)
    rendered = _combined_rendered("canonical template marker")
    assert "canonical template marker" in rendered.text
    assert "POISON ALTERNATE TEMPLATE" not in rendered.text


def test_template_bytes_drift_after_render_fails_before_provider(monkeypatch):
    import combined_analyst
    import llm_query

    rendered = _combined_rendered("template drift")
    template_path = (
        ROOT / "web/core/prompts/combined_analyst.md"
    ).resolve()
    original_read_text = Path.read_text

    def drifted_read_text(path, *args, **kwargs):
        value = original_read_text(path, *args, **kwargs)
        if Path(path).resolve() == template_path:
            return value + "\nDRIFTED TEMPLATE BYTES"
        return value

    monkeypatch.setattr(Path, "read_text", drifted_read_text)
    monkeypatch.setattr(
        llm_query,
        "_run_stream_with_signature_retry",
        lambda *_args, **_kwargs: pytest.fail("provider called"),
    )
    with pytest.raises(llm_query.LLMRoleContractError, match="replay mismatch"):
        asyncio.run(
            llm_query.run_claude_query(
                rendered, [], _UI(), "COMBINED ANALYST", None
            )
        )


def test_tools_and_paths_are_frozen_before_quota_wait(monkeypatch, tmp_path):
    import agent_workers
    import llm_availability_store
    import llm_query
    import orchestrator_cost_policy
    import rate_limiter

    workspace = ROOT / "web/core/results/workflow/artifacts/workspaces" / ("a" * 64)
    tools = ["Bash", "Read", "Edit"]
    read_dirs = [workspace]
    write_scope = {"files": [workspace / "policy.py"]}
    captured = {}

    async def wait_and_mutate():
        tools.append("Write")
        read_dirs.append(ROOT)
        write_scope["files"] = [ROOT / "web/core/llm_query.py"]

    async def provider(full_prompt, options, *_args):
        captured["prompt"] = full_prompt
        captured["options"] = options
        return ["ok"], 0.0, {}

    monkeypatch.setattr(orchestrator_cost_policy, "assert_operator_cost_limit_available", lambda: None)
    monkeypatch.setattr(llm_availability_store, "raise_if_llm_paused", lambda **_kwargs: None)
    monkeypatch.setattr(rate_limiter.rate_limiter, "is_blocked", lambda: True)
    monkeypatch.setattr(rate_limiter.rate_limiter, "wait_until_reset", wait_and_mutate)
    monkeypatch.setattr(rate_limiter.rate_limiter, "reset_time_str", lambda: "now")
    monkeypatch.setattr(llm_query, "_run_stream_with_signature_retry", provider)
    monkeypatch.setattr(llm_query, "_emit_llm_event", lambda *_args, **_kwargs: None)
    rendered = llm_query.render_llm_prompt(
        "WORKER W1 (logic)",
        producer=agent_workers._render_worker_provider_prompt,
        renderer_inputs=_renderer_inputs("worker", "frozen input"),
    )
    asyncio.run(
        llm_query.run_claude_query(
            rendered,
            [],
            _UI(),
            "WORKER W1 (logic)",
            tmp_path / "worker.log",
            tools=tools,
            allowed_read_dirs=read_dirs,
            allowed_write_dir=write_scope,
        )
    )
    _before, payload, _rules = _contract_payload(captured["prompt"])
    assert captured["options"].tools == ["Bash", "Read", "Edit"]
    assert payload["frozen_capability"]["read_dirs"] == [str(workspace)]
    assert payload["frozen_capability"]["write_files"] == [
        str(workspace / "policy.py")
    ]


def test_strict_authority_uses_internal_copy_after_quota_wait(
    monkeypatch, tmp_path
):
    import agent_master
    import llm_availability_store
    import llm_query
    import orchestrator_cost_policy
    import rate_limiter
    import strict_authority_workflow

    owner = {
        "schema_version": 1,
        "slot": "proposal:mechanism",
        "marker": "original",
    }
    captured = {}

    async def wait_and_mutate():
        owner["marker"] = "poison"
        owner["replay_provider"] = True

    def schema_prompt(call):
        assert call is not owner
        assert call["marker"] == "original"
        assert "replay_provider" not in call
        return ""

    def dispatch(call, **_kwargs):
        assert call is not owner
        assert call["marker"] == "original"
        assert "replay_provider" not in call
        call.update({"effect_id": "effect", "invocation_id": "internal"})
        captured["dispatch"] = dict(call)

    def complete(call, **_kwargs):
        call["provider_completed"] = True

    async def provider(*_args, **_kwargs):
        return ["ok"], 0.0, {}

    monkeypatch.setattr(orchestrator_cost_policy, "assert_operator_cost_limit_available", lambda: None)
    monkeypatch.setattr(llm_availability_store, "raise_if_llm_paused", lambda **_kwargs: None)
    monkeypatch.setattr(rate_limiter.rate_limiter, "is_blocked", lambda: True)
    monkeypatch.setattr(rate_limiter.rate_limiter, "wait_until_reset", wait_and_mutate)
    monkeypatch.setattr(rate_limiter.rate_limiter, "reset_time_str", lambda: "now")
    monkeypatch.setattr(strict_authority_workflow, "schema_retry_prompt", schema_prompt)
    monkeypatch.setattr(strict_authority_workflow, "dispatch_call", dispatch)
    monkeypatch.setattr(strict_authority_workflow, "canonical_provider_output", lambda _results: "ok")
    monkeypatch.setattr(strict_authority_workflow, "complete_provider_call", complete)
    monkeypatch.setattr(llm_query, "_run_stream_with_signature_retry", provider)
    monkeypatch.setattr(llm_query, "_emit_llm_event", lambda *_args, **_kwargs: None)
    role = "MASTER PROPOSAL mechanism"
    rendered = llm_query.render_llm_prompt(
        role,
        producer=agent_master._render_master_proposal_provider_prompt,
        renderer_inputs=_renderer_inputs("master_proposal", "strict context"),
    )
    output, _cost, _usage = asyncio.run(
        llm_query.run_claude_query(
            rendered,
            [],
            _UI(),
            role,
            tmp_path / "strict.log",
            tools=["Read"],
            allowed_read_dirs=[
                ROOT / "bots/national_v143",
                ROOT / "bots/national_v145",
            ],
            strict_authority=owner,
        )
    )
    assert output == "ok"
    assert captured["dispatch"]["marker"] == "original"
    assert owner["marker"] == "original"
    assert "replay_provider" not in owner
    assert owner["provider_completed"] is True


def test_strict_provider_replay_keeps_original_log_bytes(monkeypatch, tmp_path):
    import agent_master
    import llm_availability_store
    import llm_query
    import orchestrator_cost_policy
    import rate_limiter
    import strict_authority_workflow

    log_file = tmp_path / "master_proposal_mechanism_io.txt"
    log_file.write_bytes(b"original sealed invocation log\n")
    original_bytes = log_file.read_bytes()
    provider_called = False
    owner = {
        "schema_version": 1,
        "slot": "proposal:mechanism",
        "role": "MASTER PROPOSAL mechanism",
        "replay_provider": True,
        "replay_raw_output": '{"replayed":true}',
        "replay_cost_usd": 0.01,
        "replay_usage": {"input_tokens": 1, "output_tokens": 1},
        "provider_completed": True,
    }

    async def forbidden_provider(*_args, **_kwargs):
        nonlocal provider_called
        provider_called = True
        return ["forbidden"], 0.0, {}

    def dispatch(call, **_kwargs):
        assert call.get("replay_provider") is True
        call["dispatched"] = True

    monkeypatch.setattr(
        orchestrator_cost_policy,
        "assert_operator_cost_limit_available",
        lambda: None,
    )
    monkeypatch.setattr(
        llm_availability_store,
        "raise_if_llm_paused",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(rate_limiter.rate_limiter, "is_blocked", lambda: False)
    monkeypatch.setattr(
        strict_authority_workflow,
        "schema_retry_prompt",
        lambda _call: "",
    )
    monkeypatch.setattr(strict_authority_workflow, "dispatch_call", dispatch)
    monkeypatch.setattr(
        llm_query,
        "_run_stream_with_signature_retry",
        forbidden_provider,
    )
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda *_args, **_kwargs: None,
    )
    role = "MASTER PROPOSAL mechanism"
    rendered = llm_query.render_llm_prompt(
        role,
        producer=agent_master._render_master_proposal_provider_prompt,
        renderer_inputs=_renderer_inputs(
            "master_proposal",
            "strict replay context",
        ),
    )

    output, cost, usage = asyncio.run(llm_query.run_claude_query(
        rendered,
        [],
        _UI(),
        role,
        log_file,
        tools=["Read"],
        allowed_read_dirs=[ROOT / "bots/national_v143"],
        strict_authority=owner,
    ))

    assert output == '{"replayed":true}'
    assert cost == 0.01
    assert usage == {"input_tokens": 1, "output_tokens": 1}
    assert provider_called is False
    assert log_file.read_bytes() == original_bytes


def test_oversized_sealed_prompt_fails_without_truncation(monkeypatch):
    import evolution_infra
    import llm_query

    provider_called = False

    async def forbidden(*_args, **_kwargs):
        nonlocal provider_called
        provider_called = True
        return ["bad"], 0.0, {}

    marker = "TAIL_KEPT" + ("x" * (evolution_infra.MAX_PROMPT_CHARS + 1000))
    rendered = _combined_rendered(marker)
    assert rendered.text.endswith("```\n") or "TAIL_KEPT" in rendered.text
    monkeypatch.setattr(llm_query, "_run_stream_with_signature_retry", forbidden)
    with pytest.raises(llm_query.LLMRoleContractError, match="cannot be truncated"):
        asyncio.run(
            llm_query.run_claude_query(
                rendered, [], _UI(), "COMBINED ANALYST", None
            )
        )
    assert provider_called is False


@pytest.mark.parametrize(
    ("role_id", "factory_name"),
    (
        ("worker", "_make_subagent_write_guard"),
        ("lead_code_reviewer", "_make_subagent_readonly_guard"),
        ("strategy_critic", "_make_master_evidence_read_guard"),
        ("debug_agent", "_make_subagent_read_scope_guard"),
        ("operator_sdk_probe", "_make_exact_bash_allowlist_guard"),
    ),
)
def test_mandatory_hook_unavailability_aborts_before_provider(
    monkeypatch, tmp_path, role_id, factory_name
):
    import llm_availability_store
    import llm_query
    import orchestrator_cost_policy
    import rate_limiter

    row = next(item for item in CASES if item[1] == role_id)
    role, _role_id, tools, module_name, producer_name, *_rest = row
    producer = getattr(importlib.import_module(module_name), producer_name)
    rendered = llm_query.render_llm_prompt(
        role,
        producer=producer,
        renderer_inputs=_renderer_inputs(role_id, f"hook::{role_id}"),
    )
    provider_called = False

    async def forbidden(*_args, **_kwargs):
        nonlocal provider_called
        provider_called = True
        return ["bad"], 0.0, {}

    monkeypatch.setattr(orchestrator_cost_policy, "assert_operator_cost_limit_available", lambda: None)
    monkeypatch.setattr(llm_availability_store, "raise_if_llm_paused", lambda **_kwargs: None)
    monkeypatch.setattr(rate_limiter.rate_limiter, "is_blocked", lambda: False)
    monkeypatch.setattr(llm_query, factory_name, lambda *_args, **_kwargs: None)
    monkeypatch.setattr(llm_query, "_run_stream_with_signature_retry", forbidden)
    monkeypatch.setattr(llm_query, "_emit_llm_event", lambda *_args, **_kwargs: None)
    with pytest.raises(RuntimeError, match="unavailable"):
        asyncio.run(
            llm_query.run_claude_query(
                rendered,
                [],
                _UI(),
                role,
                tmp_path / f"{role_id}.log",
                tools=list(tools),
                **_scope(role_id),
            )
        )
    assert provider_called is False


def test_mandatory_hooks_deny_malformed_input_and_internal_errors(monkeypatch):
    import llm_query

    workspace = ROOT / "web/core/results/workflow/artifacts/workspaces" / ("a" * 64)
    factories = (
        llm_query._make_subagent_write_guard(
            {"files": [workspace / "policy.py"]}
        ),
        llm_query._make_subagent_readonly_guard("LEAD CODE REVIEWER"),
        llm_query._make_master_evidence_read_guard(
            "STRATEGY CRITIC",
            None,
            [workspace],
        ),
        llm_query._make_subagent_read_scope_guard(
            "DEBUG AGENT (v145)",
            [workspace],
        ),
        llm_query._make_exact_bash_allowlist_guard(
            "OPERATOR SDK PROBE",
            ("sha256sum fixed",),
        ),
    )

    async def invoke(handler, payload):
        return await handler(payload, "malformed", {})

    for hooks in factories:
        assert hooks
        handler = hooks["PreToolUse"][0].hooks[0]
        output = asyncio.run(invoke(handler, None))
        assert (
            (output.get("hookSpecificOutput") or {}).get("permissionDecision")
            == "deny"
        )

    read_hooks = llm_query._make_subagent_read_scope_guard(
        "DEBUG AGENT (v145)", [workspace]
    )
    read_handler = read_hooks["PreToolUse"][0].hooks[0]
    monkeypatch.setattr(
        llm_query,
        "_subagent_read_scope_violation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    output = asyncio.run(
        invoke(
            read_handler,
            {"tool_name": "Read", "tool_input": {"file_path": str(workspace)}},
        )
    )
    assert (
        (output.get("hookSpecificOutput") or {}).get("permissionDecision")
        == "deny"
    )

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    internal_cases = []
    write_hooks = llm_query._make_subagent_write_guard(
        {"files": [workspace / "policy.py"]}
    )
    internal_cases.append((
        write_hooks["PreToolUse"][0].hooks[0],
        "_subagent_bash_write_scope_violation",
        {"tool_name": "Bash", "tool_input": {"command": "touch policy.py"}},
    ))
    evidence_hooks = llm_query._make_master_evidence_read_guard(
        "STRATEGY CRITIC", None, [workspace]
    )
    internal_cases.append((
        evidence_hooks["PreToolUse"][0].hooks[0],
        "_master_live_evidence_read_violation",
        {"tool_name": "Bash", "tool_input": {"command": "cat evidence"}},
    ))
    readonly_hooks = llm_query._make_subagent_readonly_guard("LEAD CODE REVIEWER")
    internal_cases.append((
        readonly_hooks["PreToolUse"][0].hooks[0],
        "_subagent_readonly_mutation_violation",
        {"tool_name": "Bash", "tool_input": {"command": "touch x"}},
    ))
    for handler, helper_name, payload in internal_cases:
        with monkeypatch.context() as local_patch:
            local_patch.setattr(llm_query, helper_name, boom)
            output = asyncio.run(invoke(handler, payload))
        assert (
            (output.get("hookSpecificOutput") or {}).get("permissionDecision")
            == "deny"
        )

    class BadString:
        def __str__(self):
            raise RuntimeError("boom")

    exact_hooks = llm_query._make_exact_bash_allowlist_guard(
        "OPERATOR SDK PROBE", ("sha256sum fixed",)
    )
    exact_output = asyncio.run(
        invoke(
            exact_hooks["PreToolUse"][0].hooks[0],
            {"tool_name": "Bash", "tool_input": {"command": BadString()}},
        )
    )
    assert (
        (exact_output.get("hookSpecificOutput") or {}).get("permissionDecision")
        == "deny"
    )


def test_scope_normalization_rejects_outside_and_symlink_paths(
    monkeypatch, tmp_path
):
    import llm_query

    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = project / "linked"
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(llm_query, "_LLM_PROJECT_ROOT", project)

    with pytest.raises(llm_query.LLMRoleContractError, match="outside active project"):
        llm_query._project_relative_path(outside)
    with pytest.raises(llm_query.LLMRoleContractError, match="symlinked"):
        llm_query._project_relative_path(link / "policy.py")


class _ProviderVisitor(ast.NodeVisitor):
    def __init__(self, relative):
        self.relative = relative
        self.stack = []
        self.calls = set()
        self.run_aliases = {"run_claude_query"}
        self.sdk_aliases = {"claude_query"}
        self.module_aliases = set()

    def visit_ImportFrom(self, node):
        if node.module in {"llm_query", "evolution_infra", "evolution_core", "core.llm_query"}:
            for item in node.names:
                if item.name == "run_claude_query":
                    self.run_aliases.add(item.asname or item.name)
        if node.module == "claude_agent_sdk":
            for item in node.names:
                if item.name == "query":
                    self.sdk_aliases.add(item.asname or item.name)
        self.generic_visit(node)

    def visit_Import(self, node):
        for item in node.names:
            if item.name in {"llm_query", "core.llm_query"}:
                self.module_aliases.add(item.asname or item.name.split(".")[-1])
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        name = None
        if isinstance(node.func, ast.Name):
            if (
                node.func.id == "query_runner"
                and self.relative == "web/core/operator_sdk_probe.py"
            ):
                name = "run_indirect"
            elif node.func.id in self.run_aliases:
                name = "run"
            elif node.func.id in self.sdk_aliases:
                name = "sdk"
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            if node.func.value.id in self.module_aliases and node.func.attr == "run_claude_query":
                name = "run_attribute"
        if name:
            self.calls.add((self.relative, self.stack[-1] if self.stack else "<module>", name))
        self.generic_visit(node)


def test_active_tree_provider_scan_covers_aliases_attributes_subpackages_scripts_and_sdk():
    found = set()
    for surface in (ROOT / "web/core", ROOT / "scripts", ROOT / "sever"):
        for path in surface.rglob("*.py"):
            if "tests" in path.parts or "archive" in path.parts:
                continue
            relative = path.relative_to(ROOT).as_posix()
            visitor = _ProviderVisitor(relative)
            visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
            found.update(visitor.calls)

    expected_run_functions = {
        ("web/core/direction_auditor.py", "_run_direction_audit", "run"),
        ("web/core/agent_review.py", "_run_critic", "run"),
        ("web/core/agent_review.py", "_run_crossover", "run"),
        ("web/core/agent_master.py", "propose", "run"),
        ("web/core/agent_master.py", "critique", "run"),
        ("web/core/agent_master.py", "_run_master_analysis", "run"),
        ("web/core/agent_workers.py", "_run_debug_agent", "run"),
        ("web/core/agent_workers.py", "_run_single_worker", "run"),
        ("web/core/tool_gates.py", "run_review", "run"),
        ("web/core/tool_planning.py", "run_literature_probe", "run"),
        ("web/core/cycle_archivist.py", "run_cycle_archivist_analysis", "run"),
        ("web/core/audit_agents.py", "_run_master_plan_audit", "run"),
        ("web/core/audit_agents.py", "_run_worker_cot_check", "run"),
        ("web/core/audit_agents.py", "_run_degeneration_diagnosis", "run"),
        ("web/core/audit_agents.py", "_run_crossover_compatibility_audit", "run"),
        ("web/core/combined_analyst.py", "_run_combined_analysis", "run"),
        ("web/core/official_llm_analysis.py", "_default_runner", "run"),
        ("web/core/operator_sdk_probe.py", "run_operator_probe", "run_indirect"),
        ("web/core/llm_query.py", "_run_stream_with_signature_retry_attempts", "sdk"),
        ("web/core/orchestrator.py", "_stream_response", "sdk"),
    }
    assert found == expected_run_functions


def test_active_render_callers_cannot_reintroduce_caller_owned_full_prompts():
    """Keep semantic renderer inputs distinct from provider-ready prompt text."""
    forbidden_input_keys = {
        "sections",
        "ordered_sections",
        "full_prompt",
        "provider_prompt",
        "rendered_prompt",
    }
    render_calls = []
    for path in (ROOT / "web/core").rglob("*.py"):
        if path.name == "llm_query.py" or "tests" in path.parts or "archive" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        assert "ordered_sections" not in source
        assert "validate_renderer_sections" not in source
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called_name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if called_name != "render_llm_prompt":
                continue
            render_calls.append((path.relative_to(ROOT).as_posix(), node.lineno))
            renderer_keyword = next(
                (item for item in node.keywords if item.arg == "renderer_inputs"),
                None,
            )
            assert renderer_keyword is not None
            if isinstance(renderer_keyword.value, ast.Dict):
                literal_keys = {
                    key.value
                    for key in renderer_keyword.value.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
                assert not (literal_keys & forbidden_input_keys)

    # Nineteen active provider roles plus the strict gate authority's
    # descriptor-owned semantic replay helper. A new call site must be
    # registered and covered deliberately.
    assert len(render_calls) == 20


def test_durable_worker_effect_supplies_no_external_context_files():
    """The production Worker path must match its no-context-files role ABI."""
    import llm_query

    assert llm_query.resolve_llm_role_contract("WORKER 1 (logic)").allows_context_files is False
    path = ROOT / "web/core/tool_planning.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_execute_workers"
    ]
    assert len(calls) == 1
    # Positional ABI: tasks, template, workspace, version, context_files, UI.
    assert len(calls[0].args) >= 5
    assert isinstance(calls[0].args[4], (ast.List, ast.Tuple))
    assert calls[0].args[4].elts == []


@pytest.mark.asyncio
async def test_orchestrator_direct_provider_binds_real_mcp_object(monkeypatch, tmp_path):
    import evolution_core
    import orchestrator

    captured = {}
    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(orchestrator, "_build_context", lambda **_kwargs: "ORCHESTRATOR_CONTEXT")
    monkeypatch.setattr(orchestrator, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(orchestrator, "_save_orchestrator_session", lambda *_args: None)
    monkeypatch.setattr(orchestrator, "_clear_orchestrator_session", lambda **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_bind_generation_cost_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orchestrator, "_check_generation_cost_policy", lambda *_args: None)
    monkeypatch.setattr(
        orchestrator,
        "_detect_actionable_stage_handoff",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(orchestrator, "record_generation_cost", lambda *_args, **_kwargs: {"active": False, "recorded": False})
    monkeypatch.setattr(orchestrator, "log_system_event", lambda *_args, **_kwargs: None)

    async def stream():
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="receipt", total_cost_usd=0.0, usage={}, result="done")

    monkeypatch.setattr(orchestrator, "claude_query", lambda **kwargs: captured.update(kwargs) or stream())
    assert await orchestrator._run_one_cycle(ui=_UI(), log_file=tmp_path / "orch.log") == 0.0
    _before, payload, _rules = _contract_payload(captured["prompt"])
    assert payload["role_id"] == "orchestrator"
    assert payload["model"] == "sonnet"
    assert payload["selected_mcp_servers"] == ["evolution"]
    assert payload["mcp_config_sha256"] != hashlib.sha256(b"{}").hexdigest()
    assert captured["options"].mcp_servers["evolution"] is orchestrator.evolution_server
    import llm_query

    contract = llm_query.resolve_llm_role_contract("Orchestrator")
    assert contract.required_evidence_fields == EXPECTED_PROVENANCE_FIELDS["orchestrator"]
