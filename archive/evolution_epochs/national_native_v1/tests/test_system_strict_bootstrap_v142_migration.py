"""ARCHIVED: content/execution tests for the retired v142 migration bootstrap.

These tests deliberately use the real, published v142 bytes.  The bootstrap is
allowed only because that exact artifact, the current system-owned native
runtime, both official-oracle documents, and the four checked-in consumer
files form one recomputable authority chain.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import py_compile
import runpy
import shutil

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = PROJECT_ROOT / "bots" / "national_v142"
MIGRATION_CHECKS = (
    "terminal_response_adaptation",
    "showdown_range_adaptation",
    "donk_line_reachability",
    "delayed_probe_line_reachability",
)
CORRECTNESS_CHECKS = (
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


class _UI:
    def log_history(self, *_args, **_kwargs):
        return None

    def get_output(self):
        return ""


def _observed_provider_result(label: str, call: dict, output: str):
    from claude_agent_sdk import ResultMessage
    from strict_authority_workflow import _observe_provider_result

    result = ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=10,
        is_error=False,
        num_turns=1,
        session_id=f"strict-test-session-{label}",
        total_cost_usd=0.0,
        usage={},
        result=output,
    )
    _observe_provider_result(
        result,
        invocation_id=call["invocation_id"],
        effect_id=call["effect_id"],
    )
    return result


@pytest.fixture(autouse=True)
def _isolated_strict_authority_store(monkeypatch, tmp_path):
    import strict_authority_workflow as authority
    from workflow_kernel import WorkflowStore

    store = WorkflowStore(tmp_path / "strict-authority.sqlite3")
    monkeypatch.setattr(authority, "_store", lambda: store)
    # These end-to-end bootstrap tests build already-normalized role payloads.
    # The role-specific parser/projection is covered independently by the
    # strict-authority and Master tests; keep this fixture focused on the
    # durable authority/receipt chain.
    real_project_role_result = authority._project_role_result

    def project_normalized_fixture(call, raw_output):
        if call.get("slot") in {"review", "critic"}:
            return real_project_role_result(call, raw_output)
        return json.loads(raw_output)

    monkeypatch.setattr(
        authority,
        "_project_role_result",
        project_normalized_fixture,
    )


def _prepare_checkpoint(tmp_path: Path, *, directory: str = "case"):
    """Recreate the exact post-prepare migration boundary."""

    from national_native import ensure_native_entry
    from national_protocol_quarantine import select_protocol_bootstrap_source
    from prepared_baseline_contract import build_prepared_artifact_contract
    from runtime_architecture_policy import build_architecture_policy
    from system_strict_bootstrap import load_blueprint_manifest

    candidate = tmp_path / directory / "national_v150"
    candidate.parent.mkdir(parents=True)
    shutil.copytree(SOURCE_DIR, candidate)
    ensure_native_entry(candidate, overwrite=True)

    manifest = load_blueprint_manifest()
    prepared = build_prepared_artifact_contract(
        candidate,
        source_v=142,
        next_v=150,
    )
    assert prepared["prepared_artifact_hash"] == manifest["prepared_artifact_hash"]

    bootstrap = select_protocol_bootstrap_source([])
    assert bootstrap["available"] is True
    receipt = bootstrap["receipt"]
    checkpoint = {
        "workflow_run_id": f"strict-bootstrap-test-{directory}",
        "checkpoint_revision": 10,
        "next_v": 150,
        "source_v": 142,
        "stage": "direction_audited",
        "audit_context": {
            "protocol_bootstrap": receipt,
            "protocol_bootstrap_prepare": {
                "receipt_digest": receipt["receipt_digest"],
                "mode": "legacy_strategy_migration",
                "system_runtime_replaced": True,
                "national_bot_sha256": hashlib.sha256(
                    (candidate / "national_bot.py").read_bytes()
                ).hexdigest(),
            },
            "prepared_artifact_contract": prepared,
            "selection": {
                "bootstrap_without_strength_evidence": True,
                "strategy": "protocol_migration_bootstrap",
                "parent_a": 142,
            },
        },
        "direction_audit": {
            "protocol_bootstrap_no_strength": True,
            "protocol_bootstrap_receipt_digest": receipt["receipt_digest"],
            "prepared_artifact_hash": prepared["prepared_artifact_hash"],
        },
        "gate_results": {},
    }
    policy = build_architecture_policy(SOURCE_DIR)
    return candidate, checkpoint, policy


def _complete_mocked_strict_query(strict_call, prompt: str, output: str, label: str):
    """A mocked SDK must still provide a real fenced provider completion."""

    from strict_authority_workflow import complete_provider_call, dispatch_call

    dispatch_call(
        strict_call,
        full_prompt=prompt,
        tools=["Read"],
        owner="pytest-mocked-provider",
    )
    complete_provider_call(
        strict_call,
        raw_output=output,
        provider_results=[_observed_provider_result(
            f"{label}-{strict_call['invocation_id']}",
            strict_call,
            output,
        )],
    )


def _bind_master(candidate: Path, checkpoint: dict, policy: dict):
    import agent_master
    from plan_compiler import (
        bind_system_owned_legacy_consumer_migration,
        bind_system_owned_worker_contract_terms,
        compile_master_plan,
    )
    from runtime_architecture_policy import attach_runtime_contract_ledger
    from system_strict_bootstrap import (
        EXECUTOR_ID,
        build_master_receipt,
        llm_result_digest,
        new_llm_invocation_id,
        record_llm_invocation_evidence,
        system_worker_backend_contract,
    )
    from tool_planning import _validate_master_plan

    source_graph, source_code_digest = agent_master._source_symbol_graph(candidate)
    proposals = []
    directions = ("mechanism", "counterfactual", "compute_memory")
    for direction, check_id in zip(directions, MIGRATION_CHECKS):
        raw = {
            "targeted_failure": (
                f"{check_id} is absent from a verified reachable final-action path."
            ),
            "structural_change": (
                f"Replace the legacy request rescan with the bounded {check_id} "
                "consumer inside the universal four-file runtime migration."
            ),
            "counterfactual": (
                f"Toggle only the authoritative {check_id} input while holding cards, "
                "betting state, seed, and legal-action bounds fixed."
            ),
            "measurement": (
                f"Run the exact {check_id} positive/control capability pair and require "
                "different sanitized final actions."
            ),
            "why_not_threshold_tuning": (
                "The change replaces producer-to-consumer state flow and a reachable "
                "decision path rather than changing a numeric threshold."
            ),
            "expected_diff": (
                "Replace the bounded strategy consumer graph while preserving the "
                "system-owned socket runtime and official protocol behavior."
            ),
            "target_files": ["strategy.py"],
            "source_symbols": [
                "strategy.py:get_action",
                "strategy.py:choose_preflop_spot_action",
            ],
            "reachable_chain": [
                "strategy.py:get_action",
                "strategy.py:choose_preflop_spot_action",
            ],
            "falsifier": {
                "test_name": check_id,
                "control": (
                    "Run the frozen prepared baseline with the authoritative signal "
                    "disabled on one canonical legal state."
                ),
                "intervention": (
                    "Enable only the authoritative runtime signal on the identical "
                    "cards, state, deterministic seed, and legality bounds."
                ),
                "expected_observation": (
                    "The sanitized final action differs only under the intervention; "
                    "an unchanged action falsifies the proposed consumer path."
                ),
            },
            "evidence_refs": [
                "source:strategy.py:get_action",
                "source:strategy.py:choose_preflop_spot_action",
            ],
            "risks": (
                "Sparse opponent evidence may over-adapt, so confidence and adaptation "
                "weights remain bounded and the legal baseline remains available."
            ),
        }
        proposal = agent_master._validated_master_proposal(
            json.dumps(raw),
            direction,
            source_graph=source_graph,
            snapshot_dir=candidate.parent,
            legacy_migration_only=True,
        )
        assert proposal is not None
        proposals.append(proposal)

    proposal_ids = {proposal["proposal_id"] for proposal in proposals}
    invocation_root = candidate.parent / "strict-master-invocations"
    proposal_invocations = {}
    for proposal in proposals:
        direction = proposal["direction"]
        raw_output = json.dumps(proposal, ensure_ascii=False, sort_keys=True)
        proposal_invocations[proposal["proposal_id"]] = (
            record_llm_invocation_evidence(
                invocation_id=new_llm_invocation_id(),
                purpose=f"master_proposal_scout:{direction}",
                role=f"MASTER PROPOSAL {direction}",
                prompt_digest=hashlib.sha256(
                    f"test-scout:{direction}".encode("utf-8")
                ).hexdigest(),
                raw_output_digest=hashlib.sha256(
                    raw_output.encode("utf-8")
                ).hexdigest(),
                result_digest=llm_result_digest(0.0, {}),
                role_result=proposal,
                log_file=invocation_root / f"scout-{direction}.txt",
            )
        )
    reviews = []
    for critic_id, reverse in (("falsification", False), ("scope", True)):
        ordered = list(reversed(proposals)) if reverse else list(proposals)
        raw_review = {
            "ballots": [
                {
                    "proposal_id": proposal["proposal_id"],
                    "scores": {
                        criterion: 5 - index
                        for criterion in agent_master._PROPOSAL_CRITIC_CRITERIA
                    },
                    "reject": False,
                    "reason": (
                        "The evidence, direct call edge, falsifier, and bounded "
                        "migration scope are explicit."
                    ),
                }
                for index, proposal in enumerate(ordered)
            ]
        }
        review = agent_master._validated_proposal_critique(
            json.dumps(raw_review),
            proposal_ids,
        )
        assert review is not None
        review_result = dict(review)
        review["critic_id"] = critic_id
        review["invocation_evidence"] = record_llm_invocation_evidence(
            invocation_id=new_llm_invocation_id(),
            purpose=f"master_proposal_critic:{critic_id}",
            role=f"MASTER PROPOSAL CRITIC {critic_id}",
            prompt_digest=hashlib.sha256(
                f"test-critic:{critic_id}".encode("utf-8")
            ).hexdigest(),
            raw_output_digest=hashlib.sha256(
                json.dumps(raw_review, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            result_digest=llm_result_digest(0.0, {}),
            role_result=review_result,
            log_file=invocation_root / f"critic-{critic_id}.txt",
        )
        reviews.append(review)

    ensemble = {
        "schema_version": agent_master._PROPOSAL_PACKET_SCHEMA_VERSION,
        "valid": True,
        "authority": "advisory_only; canonical runtime and gate contracts remain binding",
        "context_digest": hashlib.sha256(b"strict-bootstrap-test-context").hexdigest(),
        "source_code_digest": source_code_digest,
        "critic_criteria": agent_master._PROPOSAL_CRITIC_CRITERIA,
        "proposal_count": 3,
        "valid_critic_count": 2,
        "allowed_proposal_ids": [proposal["proposal_id"] for proposal in proposals],
        "ordered_proposals": proposals,
        "proposal_invocations": proposal_invocations,
        "critic_reviews": reviews,
    }
    selected = proposals[0]
    selected_contract = agent_master._selected_proposal_contract(selected)
    master_prompt = (PROJECT_ROOT / "web/core/prompts/master_prompt.md").read_text(
        encoding="utf-8"
    )
    example_start = master_prompt.index(
        '{\n  "analysis": "Strategic analysis as a single string.'
    )
    example_end = master_prompt.index(
        "\n\n- Do NOT include `branch_from`", example_start
    )
    # Start with the public Master schema example, just as the real Master role
    # does, then bind the exact selected proposal and strict migration policy.
    plan = json.loads(master_prompt[example_start:example_end])
    plan["targeted_failure"] = selected["targeted_failure"]
    plan["selected_proposal_id"] = selected["proposal_id"]
    plan["proposal_binding"] = {
        "schema_version": agent_master._PROPOSAL_PACKET_SCHEMA_VERSION,
        "selected_proposal_id": selected["proposal_id"],
        "contract_digest": selected_contract["contract_digest"],
        "context_digest": ensemble["context_digest"],
        "source_code_digest": source_code_digest,
        "target_files": list(selected["target_files"]),
        "source_symbols": list(selected["source_symbols"]),
        "reachable_chain": list(selected["reachable_chain"]),
        "falsifier": dict(selected["falsifier"]),
        "evidence_refs": list(selected["evidence_refs"]),
        "structural_change": selected_contract["structural_change"],
        "expected_diff": selected_contract["expected_diff"],
        "why_not_threshold_tuning": selected_contract["why_not_threshold_tuning"],
        "selected_proposal": {
            key: value for key, value in selected.items() if key != "direction"
        },
        "proposal_packet_digest": hashlib.sha256(
            json.dumps(
                ensemble,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    plan["proposal_ensemble"] = ensemble
    plan["tasks"][0]["worker_prompt"] += (
        "\n\n" + agent_master._selected_proposal_worker_block(selected)
    )
    plan, _migration_binding = bind_system_owned_legacy_consumer_migration(
        plan,
        policy=policy,
    )
    plan, _contract_binding = bind_system_owned_worker_contract_terms(plan)
    accepted_master_plan = deepcopy(plan)
    plan["architecture_policy"] = policy
    plan, compile_meta = compile_master_plan(
        plan,
        next_v=150,
        target_dir=candidate,
        project_root=PROJECT_ROOT,
    )
    if compile_meta.get("compiled"):
        assert plan.get("plan_compiler") == compile_meta
    else:
        assert "plan_compiler" not in plan
    assert all(
        row.get("context_trimmed") is False
        for row in compile_meta.get("compiled_tasks") or []
    )
    errors, warnings = _validate_master_plan(
        plan,
        next_v=150,
        precomputed_exhausted_keywords=[],
    )
    assert errors == []
    assert warnings == []
    plan = attach_runtime_contract_ledger(plan, replace=True)
    checkpoint["master_plan"] = plan
    from strict_authority_workflow import (
        MASTER_SLOTS,
        SLOT_TOOLS,
        accept_role_result,
        complete_provider_call,
        dispatch_call,
        expected_master_contexts,
        expected_master_role_results,
        new_call,
    )

    authority_results = expected_master_role_results(plan)
    authority_results["master:final"] = accepted_master_plan
    contexts = expected_master_contexts(plan)
    for index, slot in enumerate(MASTER_SLOTS):
        call = new_call(
            checkpoint,
            slot=slot,
            context_binding=contexts[slot],
        )
        dispatch_call(
            call,
            full_prompt=f"test strict provider prompt {slot}",
            tools=SLOT_TOOLS[slot],
            owner="pytest",
        )
        complete_provider_call(
            call,
            raw_output=json.dumps(authority_results[slot], sort_keys=True),
            provider_results=[_observed_provider_result(
                str(index),
                call,
                json.dumps(authority_results[slot], sort_keys=True),
            )],
        )
        accept_role_result(
            call,
            role_result=authority_results[slot],
            parse_contract={
                **{slot: "master-proposal-v2" for slot in MASTER_SLOTS[:3]},
                **{
                    slot: "master-proposal-ballot-v1"
                    for slot in MASTER_SLOTS[3:5]
                },
                "master:final": "master-plan-schema-v1",
            }[slot],
        )
    master_receipt = build_master_receipt(
        checkpoint,
        plan,
        architecture_policy=policy,
        candidate_dir=candidate,
    )
    checkpoint["audit_context"]["system_strict_bootstrap"] = master_receipt
    envelope = {
        "execution_policy": {"executor": EXECUTOR_ID},
        "prepared_artifact_hash": (
            checkpoint["audit_context"]["prepared_artifact_contract"]
            ["prepared_artifact_hash"]
        ),
        "projection_plan": plan,
        "backend_contract": system_worker_backend_contract(master_receipt),
        "envelope_digest": "e" * 64,
    }
    return plan, compile_meta, master_receipt, envelope


def _apply_and_bind_worker(candidate: Path, checkpoint: dict, envelope: dict):
    from bot_artifact import hash_path
    from system_strict_bootstrap import apply_blueprint, bind_worker_effect_receipt

    snapshots, focus, worker_receipt = apply_blueprint(
        candidate,
        checkpoint=checkpoint,
        envelope=envelope,
    )
    worker_receipt = bind_worker_effect_receipt(
        worker_receipt,
        effect_id="effect-1",
        lease_epoch=1,
    )
    output_hash = hash_path(candidate)
    checkpoint["audit_context"]["system_strict_bootstrap_worker"] = worker_receipt
    checkpoint["audit_context"]["durable_worker_output"] = {
        "artifact_hash": output_hash,
        "snapshot_hash": output_hash,
        "envelope_digest": envelope["envelope_digest"],
        "effect_id": "effect-1",
        "lease_epoch": 1,
    }
    checkpoint["gate_results"]["quality"] = {
        "all_passed": True,
        "critical_scenarios_passed": True,
        "code_fingerprint": output_hash,
        "diff_hash": "d" * 64,
    }
    return snapshots, focus, worker_receipt


def _llm_gate(name: str):
    gate = {
        "approved": True,
        "passed": True,
        "version": 150,
        "source_v": 142,
        "timestamp": "2026-07-13T00:00:00+08:00",
        "llm_invoked": True,
        "schema_valid": True,
    }
    if name == "review":
        gate.update({
            "reviewer_llm_executed": True,
            "quality_score": 8,
            "feedback": "",
            "change_summary": "Verified the selected migration mechanism.",
            "risk_areas": [],
        })
    else:
        gate.update({
            "critic_llm_executed": True,
            "score": 8.0,
            "advisory_score": 8.0,
            "raw_approved": True,
            "advisory_approved": True,
            "feedback": "",
            "strategic_assessment": "The selected consumer path is causal.",
            "local_optima_warning": False,
            "force_advanced": False,
        })
    return gate


def _embed_gate(checkpoint: dict, candidate: Path, name: str):
    from system_strict_bootstrap import (
        build_system_gate_receipt,
        llm_result_digest,
        new_llm_invocation_id,
        record_llm_invocation_evidence,
    )

    from strict_authority_workflow import (
        accept_role_result,
        complete_provider_call,
        dispatch_call,
        gate_call_context,
        new_call,
    )

    checkpoint["stage"] = "quality_passed" if name == "review" else "reviewed"
    checkpoint["checkpoint_revision"] = 20 if name == "review" else 30
    gate = _llm_gate(name)
    from output_schema import validate_agent_output

    role_input = (
        {
            key: gate[key]
            for key in (
                "approved",
                "feedback",
                "quality_score",
                "change_summary",
                "risk_areas",
            )
        }
        if name == "review"
        else {
            "score": int(gate["score"]),
            "approved": bool(gate["raw_approved"]),
            "strategic_assessment": gate["strategic_assessment"],
            "evidence": {},
            "feedback": gate["feedback"],
            "local_optima_warning": gate["local_optima_warning"],
        }
    )
    role_result, role_errors = validate_agent_output(
        "reviewer" if name == "review" else "critic",
        role_input,
    )
    assert role_errors == []
    raw_role_output = json.dumps(role_input, sort_keys=True)
    role = "LEAD CODE REVIEWER" if name == "review" else "STRATEGY CRITIC"
    call = new_call(
        checkpoint,
        slot=name,
        context_binding=gate_call_context(
            checkpoint,
            gate_name=name,
            candidate_dir=candidate,
        ),
    )
    dispatch_call(
        call,
        full_prompt=f"test strict gate prompt {name}",
        tools=["Read"],
        owner="pytest",
    )
    complete_provider_call(
        call,
        raw_output=raw_role_output,
        provider_results=[_observed_provider_result(
            name,
            call,
            raw_role_output,
        )],
    )
    gate["llm_role_result"] = role_result
    gate["llm_authority_receipt"] = accept_role_result(
        call,
        role_result=role_result,
        parse_contract=(
            "reviewer-output-schema-v1"
            if name == "review"
            else "critic-output-schema-v1"
        ),
    )
    gate["llm_execution_evidence"] = record_llm_invocation_evidence(
        invocation_id=new_llm_invocation_id(),
        purpose=f"system_strict_bootstrap_gate:{name}",
        role=role,
        prompt_digest=hashlib.sha256(
            f"test-{name}-prompt".encode("utf-8")
        ).hexdigest(),
        raw_output_digest=hashlib.sha256(
            raw_role_output.encode("utf-8")
        ).hexdigest(),
        result_digest=llm_result_digest(0.0, {}),
        role_result=role_result,
        log_file=candidate.parent / (
            "reviewer_io.txt" if name == "review" else "critic_io.txt"
        ),
    )
    gate["system_verifier_receipt"] = build_system_gate_receipt(
        checkpoint,
        gate_name=name,
        candidate_dir=candidate,
        llm_gate=gate,
    )
    checkpoint["gate_results"][name] = gate
    checkpoint["stage"] = "reviewed" if name == "review" else "critic_checked"
    return gate


def test_blueprint_package_binds_manifest_assets_oracles_and_rejects_extras(
    tmp_path, monkeypatch
):
    import system_strict_bootstrap as bootstrap

    manifest = bootstrap.load_blueprint_manifest()
    assert bootstrap.validate_blueprint_package(manifest) == []
    assert set(manifest["files"]) == {
        "strategy.py",
        "opponent.py",
        "simulation.py",
        "donk_probe.py",
    }
    assert manifest["official_oracles"] == {
        "docs/official-raise-boundary-oracle-2026-07-11.md": (
            "a83a1ec2680577d71ddb985ddba00c5bcda40817ef2fb92c0c41938dccef3756"
        ),
        "docs/official-terminal-settlement-oracle-2026-07-11.md": (
            "ad96bc4fbe7939597b7a86ff6f9193ed2e50891be9b6b9c074883f5750c23bd9"
        ),
    }
    assert set(manifest["output_capability_contracts"]) == set(MIGRATION_CHECKS)
    assert manifest["output_capability_contracts"][
        "terminal_response_adaptation"
    ]["output_consumer_chain"] == [
        "strategy.py:get_action",
        "strategy.py:get_baseline_action",
        "opponent.py:terminal_fold_pressure",
    ]

    copied = tmp_path / "strict_v1"
    shutil.copytree(bootstrap.BLUEPRINT_DIR, copied)
    monkeypatch.setattr(bootstrap, "BLUEPRINT_DIR", copied)
    monkeypatch.setattr(bootstrap, "BLUEPRINT_MANIFEST", copied / "manifest.json")

    for source in copied.glob("*.py"):
        py_compile.compile(str(source), doraise=True)
    assert bootstrap.validate_blueprint_package() == []

    cache_payload = copied / "__pycache__" / "undeclared.txt"
    cache_payload.write_text("not bytecode\n", encoding="utf-8")
    errors = bootstrap.validate_blueprint_package()
    assert any("package_entries_mismatch" in error for error in errors)
    cache_payload.unlink()

    obsolete_bytecode = copied / "__pycache__" / "strategy.pyo"
    obsolete_bytecode.write_bytes(b"not a supported Python cache artifact")
    errors = bootstrap.validate_blueprint_package()
    assert any("package_entries_mismatch" in error for error in errors)
    obsolete_bytecode.unlink()

    (copied / "undeclared.bin").write_bytes(b"not part of the ABI")
    errors = bootstrap.validate_blueprint_package()
    assert any("package_entries_mismatch" in error for error in errors)

    (copied / "undeclared.bin").unlink()
    (copied / "empty-extra-directory").mkdir()
    errors = bootstrap.validate_blueprint_package()
    assert any("package_entries_mismatch" in error for error in errors)
    (copied / "empty-extra-directory").rmdir()

    manifest_path = copied / "manifest.json"
    original_manifest = manifest_path.read_bytes()
    output_tamper = json.loads(original_manifest)
    output_contract = output_tamper["output_capability_contracts"][
        "terminal_response_adaptation"
    ]
    output_contract["output_consumer_chain"][-1] = "opponent.py:invented_sink"
    output_contract["contract_digest"] = bootstrap._canonical_digest(
        bootstrap._output_contract_subject(output_contract)
    )
    manifest_path.write_text(
        json.dumps(output_tamper, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    errors = bootstrap.validate_blueprint_package()
    assert any(
        "system_bootstrap_output_consumer_symbol_missing" in error
        for error in errors
    )
    manifest_path.write_bytes(original_manifest)

    linked_bytes = (copied / "strategy.py").read_bytes()
    link_target = tmp_path / "linked-strategy.py"
    link_target.write_bytes(linked_bytes)
    (copied / "strategy.py").unlink()
    (copied / "strategy.py").symlink_to(link_target)
    errors = bootstrap.validate_blueprint_package()
    assert "system_bootstrap_package_node_not_regular:strategy.py" in errors
    assert "system_bootstrap_asset_not_regular:strategy.py" in errors

    (copied / "strategy.py").unlink()
    (copied / "strategy.py").write_text("# drift\n", encoding="utf-8")
    errors = bootstrap.validate_blueprint_package()
    assert "system_bootstrap_asset_hash_mismatch:strategy.py" in errors


def test_master_plan_compiles_as_one_complete_system_owned_migration(tmp_path):
    import system_strict_bootstrap as bootstrap

    candidate, checkpoint, policy = _prepare_checkpoint(tmp_path)
    plan, meta, master_receipt, envelope = _bind_master(
        candidate,
        checkpoint,
        policy,
    )

    task = plan["tasks"][0]
    assert len(plan["tasks"]) == 1
    assert task["architecture_focus_id"] == (
        "national_runtime_v4_legacy_consumer_migration"
    )
    assert set(task["target_files"] + task["files_allowed"]) == {
        "strategy.py",
        "opponent.py",
        "simulation.py",
        "donk_probe.py",
    }
    assert set(MIGRATION_CHECKS).issubset(task["checks_required"])
    ensemble = plan["proposal_ensemble"]
    binding = plan["proposal_binding"]
    assert ensemble["proposal_count"] == 3
    assert len(ensemble["ordered_proposals"]) == 3
    assert ensemble["valid_critic_count"] == 2
    assert len(ensemble["critic_reviews"]) == 2
    assert plan["selected_proposal_id"] in ensemble["allowed_proposal_ids"]
    assert binding["selected_proposal_id"] == plan["selected_proposal_id"]
    assert f"proposal_id={plan['selected_proposal_id']}" in task["worker_prompt"]
    assert (
        f'"test_name":"{binding["falsifier"]["test_name"]}"'
        in task["worker_prompt"]
    )
    assert meta["migration_binding"]["bound"] is True
    assert master_receipt["executor"] == "system_strict_bootstrap_v1"
    assert master_receipt["proposal_contract_digest"] == binding["contract_digest"]
    output_evidence = master_receipt["output_capability_evidence"]
    assert output_evidence["selected_proposal_id"] == plan["selected_proposal_id"]
    assert output_evidence["proposal_contract_digest"] == binding["contract_digest"]
    assert output_evidence["capability_id"] == binding["falsifier"]["test_name"]
    assert output_evidence["causal_binding_digest"] == output_evidence[
        "evidence_digest"
    ]
    assert envelope["execution_policy"]["executor"] == (
        "system_strict_bootstrap_v1"
    )
    assert bootstrap.validate_selected_proposal_for_blueprint(
        plan,
        prepared_baseline_dir=candidate,
    ) == []

    digest_tamper = deepcopy(plan)
    digest_tamper["proposal_binding"]["source_code_digest"] = "0" * 64
    digest_errors = bootstrap.validate_selected_proposal_for_blueprint(
        digest_tamper,
        prepared_baseline_dir=candidate,
    )
    assert "system_bootstrap_prepared_source_code_digest_mismatch" in digest_errors


def test_blueprint_application_is_byte_deterministic_and_exactly_four_files(
    tmp_path,
):
    first = _prepare_checkpoint(tmp_path, directory="first")
    second = _prepare_checkpoint(tmp_path, directory="second")
    outputs = []
    for candidate, checkpoint, policy in (first, second):
        _plan, _meta, _master, envelope = _bind_master(
            candidate,
            checkpoint,
            policy,
        )
        snapshots, focus, receipt = _apply_and_bind_worker(
            candidate,
            checkpoint,
            envelope,
        )
        outputs.append((candidate, snapshots, focus, receipt))

    left, right = outputs
    assert left[1] == right[1]
    assert left[2] == right[2]
    assert {
        key: value
        for key, value in left[3].items()
        if key not in {"master_receipt_digest", "receipt_digest"}
    } == {
        key: value
        for key, value in right[3].items()
        if key not in {"master_receipt_digest", "receipt_digest"}
    }
    assert set(left[1]) == {
        (0, "strategy.py"),
        (0, "opponent.py"),
        (0, "simulation.py"),
        (0, "donk_probe.py"),
    }
    assert left[3]["changed_files"] == [
        "donk_probe.py",
        "opponent.py",
        "simulation.py",
        "strategy.py",
    ]
    assert left[3]["output_capability_validation"]["passed_checks"] == sorted(
        MIGRATION_CHECKS
    )
    assert left[3]["output_capability_validation"] == right[3][
        "output_capability_validation"
    ]
    assert left[3]["selected_output_capability"]["selected_proposal_id"] == (
        first[1]["master_plan"]["selected_proposal_id"]
    )
    assert left[3]["selected_output_capability"]["capability_id"] == (
        first[1]["master_plan"]["proposal_binding"]["falsifier"]["test_name"]
    )
    for relative in left[3]["changed_files"]:
        assert (left[0] / relative).read_bytes() == (right[0] / relative).read_bytes()


def test_checkpoint_master_worker_and_candidate_tampering_fail_closed(tmp_path):
    from bot_artifact import hash_path
    from system_strict_bootstrap import (
        SystemStrictBootstrapError,
        apply_blueprint,
        validate_bootstrap_checkpoint,
        validate_master_receipt,
        validate_system_worker_envelope,
    )

    candidate, checkpoint, policy = _prepare_checkpoint(tmp_path)
    _plan, _meta, _master, envelope = _bind_master(
        candidate,
        checkpoint,
        policy,
    )
    assert validate_bootstrap_checkpoint(
        checkpoint,
        architecture_policy=policy,
        candidate_dir=candidate,
        active_bots=[],
        require_direction_audit=True,
    ) == []
    assert validate_master_receipt(checkpoint, candidate_dir=candidate) == []
    assert validate_system_worker_envelope(
        checkpoint,
        envelope,
        candidate_dir=candidate,
    ) == []

    receipt_tamper = deepcopy(checkpoint)
    receipt_tamper["audit_context"]["protocol_bootstrap"]["source"][
        "artifact_hash"
    ] = "0" * 64
    assert validate_bootstrap_checkpoint(
        receipt_tamper,
        candidate_dir=candidate,
        active_bots=[],
    )

    master_tamper = deepcopy(checkpoint)
    master_tamper["audit_context"]["system_strict_bootstrap"][
        "plan_digest"
    ] = "0" * 64
    assert "system_bootstrap_receipt_digest_mismatch" in validate_master_receipt(
        master_tamper,
        candidate_dir=candidate,
    )

    proposal_tamper = deepcopy(checkpoint)
    proposal_tamper["master_plan"]["proposal_binding"]["structural_change"] += (
        " Undeclared threshold substitution."
    )
    proposal_errors = validate_master_receipt(
        proposal_tamper,
        candidate_dir=candidate,
    )
    assert "system_bootstrap_selected_structural_change_binding_mismatch" in (
        proposal_errors
    )
    assert "system_bootstrap_proposal_contract_digest_mismatch" in proposal_errors

    ensemble_tamper = deepcopy(checkpoint)
    ensemble_tamper["master_plan"]["proposal_ensemble"]["ordered_proposals"].pop()
    ensemble_errors = validate_master_receipt(
        ensemble_tamper,
        candidate_dir=candidate,
    )
    assert "system_bootstrap_proposal_packet_digest_mismatch" in ensemble_errors
    assert "system_bootstrap_three_proposal_ensemble_invalid" in ensemble_errors

    final_plan_tamper = deepcopy(checkpoint)
    final_plan_tamper["master_plan"]["analysis"] += " forged after LLM acceptance"
    final_plan_errors = validate_master_receipt(
        final_plan_tamper,
        candidate_dir=candidate,
    )
    assert any(
        "strict_authority_master_final_projection_mismatch" in item
        for item in final_plan_errors
    )

    # Simulate an attacker who changes the selected baseline chain and then
    # recomputes every unkeyed plan/receipt digest.  Publication validation must
    # still rebuild pinned v142+runtime and reject the invented AST evidence.
    import agent_master
    import system_strict_bootstrap as bootstrap
    from runtime_architecture_policy import attach_runtime_contract_ledger

    resigned = deepcopy(checkpoint)
    plan = resigned["master_plan"]
    binding = plan["proposal_binding"]
    ensemble = plan["proposal_ensemble"]
    old_id = plan["selected_proposal_id"]
    selected_index = next(
        index
        for index, proposal in enumerate(ensemble["ordered_proposals"])
        if proposal["proposal_id"] == old_id
    )
    selected = deepcopy(ensemble["ordered_proposals"][selected_index])
    selected["source_symbols"] = [
        "strategy.py:invented_source",
        "strategy.py:invented_sink",
    ]
    selected["reachable_chain"] = list(selected["source_symbols"])
    selected["evidence_refs"] = [
        "source:strategy.py:invented_source",
        "source:strategy.py:invented_sink",
    ]
    selected["proposal_id"] = agent_master._proposal_identity(selected)
    new_id = selected["proposal_id"]
    ensemble["ordered_proposals"][selected_index] = selected
    ensemble["allowed_proposal_ids"] = [
        new_id if proposal_id == old_id else proposal_id
        for proposal_id in ensemble["allowed_proposal_ids"]
    ]
    for review in ensemble["critic_reviews"]:
        review["ranking"] = [
            new_id if proposal_id == old_id else proposal_id
            for proposal_id in review["ranking"]
        ]
        review["reject"] = [
            new_id if proposal_id == old_id else proposal_id
            for proposal_id in review["reject"]
        ]
        for ballot in review["ballots"]:
            if ballot["proposal_id"] == old_id:
                ballot["proposal_id"] = new_id
    plan["selected_proposal_id"] = new_id
    binding["selected_proposal_id"] = new_id
    for field in (
        "source_symbols",
        "reachable_chain",
        "evidence_refs",
    ):
        binding[field] = deepcopy(selected[field])
    binding["selected_proposal"] = {
        key: value for key, value in selected.items() if key != "direction"
    }
    selected_contract = agent_master._selected_proposal_contract(selected)
    binding["contract_digest"] = selected_contract["contract_digest"]
    binding["proposal_packet_digest"] = hashlib.sha256(
        json.dumps(
            ensemble,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    prompt = plan["tasks"][0]["worker_prompt"]
    begin = prompt.index("[[SELECTED_PROPOSAL_CONTRACT:BEGIN]]")
    end_marker = "[[SELECTED_PROPOSAL_CONTRACT:END]]"
    end = prompt.index(end_marker, begin) + len(end_marker)
    plan["tasks"][0]["worker_prompt"] = (
        prompt[:begin]
        + agent_master._selected_proposal_worker_block(selected)
        + prompt[end:]
    )
    plan = attach_runtime_contract_ledger(plan, replace=True)
    resigned["master_plan"] = plan

    old_receipt = resigned["audit_context"]["system_strict_bootstrap"]
    receipt_payload = {
        key: deepcopy(value)
        for key, value in old_receipt.items()
        if key != "receipt_digest"
    }
    prepared_hash = resigned["audit_context"]["prepared_artifact_contract"][
        "prepared_artifact_hash"
    ]
    receipt_payload.update({
        "runtime_contract_ledger_digest": plan["runtime_contract_ledger"][
            "ledger_digest"
        ],
        "plan_digest": bootstrap._canonical_digest(plan),
        "selected_proposal_id": new_id,
        "proposal_contract_digest": binding["contract_digest"],
        "proposal_binding_digest": bootstrap._canonical_digest(binding),
        "baseline_proposal_evidence": bootstrap._baseline_proposal_evidence(
            plan,
            prepared_artifact_hash=prepared_hash,
        ),
        "output_capability_evidence": bootstrap._output_capability_evidence(
            plan,
            bootstrap.load_blueprint_manifest(),
        ),
    })
    resigned["audit_context"]["system_strict_bootstrap"] = bootstrap._receipt(
        receipt_payload
    )
    resigned_errors = validate_master_receipt(
        resigned,
        candidate_dir=candidate,
    )
    assert (
        "system_bootstrap_selected_source_symbol_missing_from_prepared"
        in resigned_errors
    )
    assert (
        "system_bootstrap_selected_chain_symbol_missing_from_prepared"
        in resigned_errors
    )

    envelope_tamper = deepcopy(envelope)
    envelope_tamper["backend_contract"]["manifest_sha256"] = "0" * 64
    before = hash_path(candidate)
    with pytest.raises(SystemStrictBootstrapError):
        apply_blueprint(
            candidate,
            checkpoint=checkpoint,
            envelope=envelope_tamper,
        )
    assert hash_path(candidate) == before

    (candidate / "strategy.py").write_text("# prepared drift\n", encoding="utf-8")
    assert any(
        "workspace_hash_mismatch" in error
        for error in validate_system_worker_envelope(
            checkpoint,
            envelope,
            candidate_dir=candidate,
        )
    )


def test_review_and_critic_receipts_bind_worker_quality_and_prior_review(tmp_path):
    from system_strict_bootstrap import (
        validate_system_gate_receipt,
    )

    candidate, checkpoint, policy = _prepare_checkpoint(tmp_path)
    _plan, _meta, _master, envelope = _bind_master(
        candidate,
        checkpoint,
        policy,
    )
    _apply_and_bind_worker(candidate, checkpoint, envelope)

    review_gate = _embed_gate(checkpoint, candidate, "review")
    review = review_gate["system_verifier_receipt"]
    assert validate_system_gate_receipt(
        checkpoint,
        gate_name="review",
        candidate_dir=candidate,
    ) == []

    critic_gate = _embed_gate(checkpoint, candidate, "critic")
    critic = critic_gate["system_verifier_receipt"]
    assert validate_system_gate_receipt(
        checkpoint,
        gate_name="critic",
        candidate_dir=candidate,
    ) == []

    quality_tamper = deepcopy(checkpoint)
    quality_tamper["gate_results"]["quality"]["diff_hash"] = "a" * 64
    assert validate_system_gate_receipt(
        quality_tamper,
        gate_name="review",
        candidate_dir=candidate,
    )

    review_tamper = deepcopy(checkpoint)
    review_tamper["gate_results"]["review"]["system_verifier_receipt"][
        "receipt_digest"
    ] = "0" * 64
    critic_errors = validate_system_gate_receipt(
        review_tamper,
        gate_name="critic",
        candidate_dir=candidate,
    )
    assert any("prior_review" in error for error in critic_errors)

    marker_tamper = deepcopy(checkpoint)
    marker_tamper["gate_results"]["review"]["reviewer_llm_executed"] = False
    marker_errors = validate_system_gate_receipt(
        marker_tamper,
        gate_name="review",
        candidate_dir=candidate,
    )
    assert "system_gate_reviewer_llm_executed_missing" in marker_errors

    evidence_tamper = deepcopy(checkpoint)
    evidence_tamper["gate_results"]["review"]["llm_execution_evidence"][
        "raw_output_digest"
    ] = "0" * 64
    evidence_errors = validate_system_gate_receipt(
        evidence_tamper,
        gate_name="review",
        candidate_dir=candidate,
    )
    assert "system_bootstrap_receipt_digest_mismatch" in evidence_errors

    import system_strict_bootstrap as bootstrap

    resigned_evidence = deepcopy(checkpoint)
    evidence = resigned_evidence["gate_results"]["review"][
        "llm_execution_evidence"
    ]
    unsigned = {
        key: value for key, value in evidence.items() if key != "receipt_digest"
    }
    unsigned["raw_output_digest"] = "0" * 64
    resigned_evidence["gate_results"]["review"][
        "llm_execution_evidence"
    ] = bootstrap._receipt(unsigned)
    resigned_errors = validate_system_gate_receipt(
        resigned_evidence,
        gate_name="review",
        candidate_dir=candidate,
    )
    assert (
        "system_bootstrap_llm_invocation_log_trailer_mismatch"
        in resigned_errors
    )


def test_real_review_and_critic_roles_publish_adjunct_receipts(
    tmp_path,
    monkeypatch,
):
    """The deterministic Worker never waives either mandatory LLM role."""

    import tool_gates
    from system_strict_bootstrap import validate_system_gate_receipt

    candidate, checkpoint, policy = _prepare_checkpoint(tmp_path)
    _plan, _meta, _master, envelope = _bind_master(
        candidate,
        checkpoint,
        policy,
    )
    _apply_and_bind_worker(candidate, checkpoint, envelope)
    checkpoint["stage"] = "quality_passed"
    role_calls = {"review": 0, "critic": 0}

    async def no_exhausted_failure(*_args, **_kwargs):
        return None

    async def review_query(*_args, **_kwargs):
        role_calls["review"] += 1
        output = json.dumps({
            "approved": True,
            "quality_score": 8,
            "feedback": "",
            "change_summary": "The selected proposal reaches live actions.",
            "risk_areas": [],
        })
        _complete_mocked_strict_query(
            _kwargs["strict_authority"],
            _args[0],
            output,
            "review",
        )
        return output, None, None

    async def critic_query(*_args, **_kwargs):
        role_calls["critic"] += 1
        from system_strict_bootstrap import llm_result_digest

        result = {
            "score": 8,
            "approved": True,
            "feedback": "",
            "strategic_assessment": "The migration is causal and bounded.",
            "evidence": {},
            "local_optima_warning": False,
        }
        raw_output = json.dumps(result, sort_keys=True)
        result["_llm_execution_material"] = {
            "invocation_id": _kwargs["execution_invocation_id"],
            "purpose": "system_strict_bootstrap_gate:critic",
            "role": "STRATEGY CRITIC",
            "prompt_digest": hashlib.sha256(b"test-critic-prompt").hexdigest(),
            "raw_output_digest": hashlib.sha256(
                raw_output.encode("utf-8")
            ).hexdigest(),
            "result_digest": llm_result_digest(0.0, {}),
            "log_file": str(tmp_path / "critic_io.txt"),
        }
        _complete_mocked_strict_query(
            _kwargs["strict_authority"],
            "mocked critic prompt",
            raw_output,
            "critic",
        )
        return result

    def bot_dir(version):
        return candidate if int(version) == 150 else SOURCE_DIR

    def record_gate(_version, _source_v, name, gate, **kwargs):
        checkpoint["gate_results"][name] = deepcopy(gate)
        checkpoint["stage"] = kwargs.get("stage", checkpoint.get("stage"))
        return True

    monkeypatch.setattr(tool_gates, "_matching_checkpoint", lambda *_a: checkpoint)
    monkeypatch.setattr(
        tool_gates,
        "_owned_infrastructure_failure",
        lambda *_a, **_kw: (None, None),
    )
    monkeypatch.setattr(
        tool_gates,
        "_execute_exhausted_infrastructure_failure",
        no_exhausted_failure,
    )
    monkeypatch.setattr(tool_gates, "_idempotency_check", lambda *_a, **_kw: None)
    monkeypatch.setattr(tool_gates, "_quality_gate_ok", lambda *_a, **_kw: True)
    monkeypatch.setattr(tool_gates, "_review_gate_ok", lambda *_a, **_kw: True)
    monkeypatch.setattr(tool_gates, "_set_pipeline_status", lambda *_a, **_kw: None)
    monkeypatch.setattr(tool_gates, "log_system_event", lambda *_a, **_kw: None)
    monkeypatch.setattr(tool_gates, "get_bot_dir", bot_dir)
    monkeypatch.setattr(tool_gates, "get_logs_dir", lambda *_a: tmp_path)
    monkeypatch.setattr(tool_gates, "_get_ui", lambda: _UI())
    monkeypatch.setattr(
        tool_gates,
        "_llm_gate_infrastructure_identity",
        lambda **_kw: ("strict-bootstrap-test-attempt", {}),
    )
    monkeypatch.setattr(tool_gates, "_record_gate", record_gate)
    monkeypatch.setattr(tool_gates, "_record_quality_failure", lambda *_a, **_kw: None)
    monkeypatch.setattr(tool_gates, "run_claude_query", review_query)
    monkeypatch.setattr(tool_gates, "_run_critic", critic_query)

    raw_review = asyncio.run(tool_gates.run_review.handler({
        "version": 150,
        "source_v": 142,
        "plan": [],
    }))
    review_result = json.loads(raw_review["content"][0]["text"])
    review_gate = checkpoint["gate_results"]["review"]
    assert role_calls["review"] == 1
    assert review_result["llm_invoked"] is True
    assert review_result["reviewer_llm_executed"] is True
    assert review_gate["schema_valid"] is True
    assert review_gate["system_verifier_receipt"]["llm_invoked"] is True
    assert validate_system_gate_receipt(
        checkpoint,
        gate_name="review",
        candidate_dir=candidate,
    ) == []

    raw_critic = asyncio.run(tool_gates.run_critic.handler({
        "version": 150,
        "source_v": 142,
        "plan": [],
        "reviewer_feedback": "",
    }))
    critic_result = json.loads(raw_critic["content"][0]["text"])
    critic_gate = checkpoint["gate_results"]["critic"]
    assert role_calls["critic"] == 1
    assert critic_result["llm_invoked"] is True
    assert critic_result["critic_llm_executed"] is True
    assert critic_gate["schema_valid"] is True
    assert critic_gate["system_verifier_receipt"]["llm_invoked"] is True
    assert validate_system_gate_receipt(
        checkpoint,
        gate_name="critic",
        candidate_dir=candidate,
    ) == []


def test_real_critic_role_exports_exact_call_material(tmp_path, monkeypatch):
    import agent_review
    from system_strict_bootstrap import llm_result_digest

    captured = {}
    raw_output = json.dumps({
        "score": 8,
        "approved": True,
        "strategic_assessment": "The selected mechanism reaches live actions.",
        "evidence": {},
        "feedback": "The causal intervention and bounded fallback are explicit.",
        "local_optima_warning": False,
    })

    async def query(prompt, _ctx, _ui, role, log_file, **_kwargs):
        captured.update({
            "prompt": prompt,
            "role": role,
            "log_file": str(log_file),
        })
        return raw_output, 0.25, {"input_tokens": 12, "output_tokens": 7}

    monkeypatch.setattr(agent_review, "run_claude_query", query)
    monkeypatch.setattr(agent_review, "get_logs_dir", lambda *_a: tmp_path)
    result = asyncio.run(agent_review._run_critic(
        150,
        142,
        "{}",
        _UI(),
        execution_invocation_id="a" * 32,
    ))
    material = result.pop("_llm_execution_material")

    assert captured["role"] == "STRATEGY CRITIC"
    assert material == {
        "invocation_id": "a" * 32,
        "purpose": "system_strict_bootstrap_gate:critic",
        "role": "STRATEGY CRITIC",
        "prompt_digest": hashlib.sha256(
            captured["prompt"].encode("utf-8")
        ).hexdigest(),
        "raw_output_digest": hashlib.sha256(
            raw_output.encode("utf-8")
        ).hexdigest(),
        "result_digest": llm_result_digest(
            0.25,
            {"input_tokens": 12, "output_tokens": 7},
        ),
        "log_file": str(tmp_path / "critic_io.txt"),
    }


@pytest.mark.parametrize(
    ("tool_name", "gate_name", "validator_name", "stage"),
    (
        ("run_review", "review", "_review_gate_ok", "reviewed"),
        ("run_critic", "critic", "_critic_gate_ok", "critic_checked"),
    ),
)
def test_llm_role_idempotency_validates_the_complete_checkpoint(
    monkeypatch,
    tool_name,
    gate_name,
    validator_name,
    stage,
):
    import tool_gates

    current_checkpoint = {
        "next_v": 150,
        "source_v": 142,
        "stage": stage,
        "checkpoint_revision": 2,
        "audit_context": {
            "protocol_bootstrap": {"mode": "legacy_strategy_migration"},
            "selection": {
                "strategy": "protocol_migration_bootstrap",
                "parent_a": 142,
            },
        },
        "gate_results": {gate_name: {"approved": True}},
    }
    initially_read_checkpoint = deepcopy(current_checkpoint)
    initially_read_checkpoint["checkpoint_revision"] = 1
    observed = []

    async def no_exhausted_failure(*_args, **_kwargs):
        return None

    def complete_checkpoint_validator(value):
        observed.append(value)
        assert value is current_checkpoint
        return True

    checkpoint_reads = iter((initially_read_checkpoint, current_checkpoint))
    monkeypatch.setattr(
        tool_gates,
        "_matching_checkpoint",
        lambda *_a: next(checkpoint_reads),
    )
    monkeypatch.setattr(
        tool_gates,
        "_owned_infrastructure_failure",
        lambda *_a, **_kw: (None, None),
    )
    monkeypatch.setattr(
        tool_gates,
        "_execute_exhausted_infrastructure_failure",
        no_exhausted_failure,
    )
    monkeypatch.setattr(tool_gates, "_set_pipeline_status", lambda *_a, **_kw: None)
    monkeypatch.setattr(tool_gates, validator_name, complete_checkpoint_validator)

    args = {"version": 150, "source_v": 142, "plan": []}
    if tool_name == "run_critic":
        args["reviewer_feedback"] = ""
    raw = asyncio.run(getattr(tool_gates, tool_name).handler(args))
    result = json.loads(raw["content"][0]["text"])

    assert result["idempotent_cache"] is True
    assert observed == [current_checkpoint]


def test_immutable_blueprint_rejection_is_tool_layer_terminal(
    tmp_path,
    monkeypatch,
):
    import system_strict_bootstrap as bootstrap
    import orchestrator_session
    import tool_bot_management
    import tool_gates

    _candidate, checkpoint, _policy = _prepare_checkpoint(tmp_path)
    checkpoint.update(
        workflow_run_id="strict-workflow-a",
        checkpoint_revision=7,
    )
    calls = []
    cleared_sessions = []

    async def abandon(*, reason, _bypass_rate_limit=False, **_kwargs):
        calls.append((reason, _bypass_rate_limit, deepcopy(_kwargs)))
        return {
            "abandoned": True,
            "cleared_checkpoint": True,
            "removed_dir": "national_v150",
        }

    monkeypatch.setattr(tool_bot_management, "_do_abandon_generation", abandon)
    monkeypatch.setattr(
        orchestrator_session,
        "_clear_orchestrator_session",
        lambda: cleared_sessions.append("cleared"),
    )
    result = asyncio.run(bootstrap.abandon_rejected_blueprint(
        checkpoint,
        reason="system_strict_bootstrap_test_rejected",
        result={"error": "BLUEPRINT_REJECTED", "passed": False},
    ))
    assert calls == [(
        "system_strict_bootstrap_test_rejected",
        True,
        {
            "expected_workflow_run_id": "strict-workflow-a",
            "expected_next_v": 150,
            "expected_source_v": 142,
            "expected_checkpoint_revision": 7,
        },
    )]
    assert result["abandoned"] is True
    assert result["checkpoint_stage"] == "abandoned"
    assert result["abandon_result"]["cleared_checkpoint"] is True
    assert cleared_sessions == ["cleared"]

    async def stale_abandon(**_kwargs):
        return {
            "abandoned": False,
            "reason": "expected_checkpoint_identity_mismatch",
            "action": "stale_rejection_ignored",
            "current_checkpoint": {
                "workflow_run_id": "strict-workflow-b",
                "next_v": 151,
                "source_v": 150,
                "checkpoint_revision": 1,
            },
        }

    monkeypatch.setattr(
        tool_bot_management,
        "_do_abandon_generation",
        stale_abandon,
    )
    stale = asyncio.run(bootstrap.abandon_rejected_blueprint(
        checkpoint,
        reason="late_review_rejection_for_a",
        result={"error": "BLUEPRINT_REJECTED", "passed": False},
    ))
    assert stale["abandoned"] is False
    assert stale["action"] == "stale_rejection_ignored"
    assert stale["abandon_result"]["current_checkpoint"]["next_v"] == 151
    assert cleared_sessions == ["cleared"]

    quality_calls = []

    async def quality_abandon(checkpoint_arg, *, reason, result):
        quality_calls.append((checkpoint_arg, reason, deepcopy(result)))
        return {**result, "abandoned": True, "checkpoint_stage": "abandoned"}

    monkeypatch.setattr(bootstrap, "abandon_rejected_blueprint", quality_abandon)
    business_failure = asyncio.run(
        tool_gates._finalize_strict_blueprint_quality_rejection(
            required=True,
            infrastructure_active=False,
            all_passed=False,
            checkpoint=checkpoint,
            result={"all_passed": False},
        )
    )
    assert business_failure["abandoned"] is True
    assert quality_calls[0][1] == "system_strict_bootstrap_quality_rejected"

    retryable = {"all_passed": False, "action": "retry_same_tool"}
    assert asyncio.run(
        tool_gates._finalize_strict_blueprint_quality_rejection(
            required=True,
            infrastructure_active=True,
            all_passed=False,
            checkpoint=checkpoint,
            result=retryable,
        )
    ) is retryable
    assert len(quality_calls) == 1


def test_review_rejection_and_gate_receipt_drift_invoke_terminal_abandon(
    tmp_path,
    monkeypatch,
):
    import system_strict_bootstrap as bootstrap
    import tool_gates

    candidate, checkpoint, policy = _prepare_checkpoint(tmp_path)
    _plan, _meta, _master, envelope = _bind_master(
        candidate,
        checkpoint,
        policy,
    )
    _apply_and_bind_worker(candidate, checkpoint, envelope)
    checkpoint["stage"] = "quality_passed"

    active = [deepcopy(checkpoint)]
    active_candidate = [candidate]
    review_approved = [False]
    abandoned_reasons = []

    async def no_exhausted_failure(*_args, **_kwargs):
        return None

    async def review_query(*_args, **_kwargs):
        output = json.dumps({
            "approved": review_approved[0],
            "quality_score": 8 if review_approved[0] else 3,
            "feedback": "immutable consumer path rejected",
            "change_summary": "strict blueprint",
            "risk_areas": ["consumer path"],
        })
        # Rejected roles abandon before acceptance but still need provider
        # provenance; approved roles continue to the schema receipt.
        _complete_mocked_strict_query(
            _kwargs["strict_authority"],
            _args[0],
            output,
            "terminal-review",
        )
        return output, None, None

    async def critic_query(*_args, **_kwargs):
        from system_strict_bootstrap import llm_result_digest

        result = {
            "score": 8,
            "approved": True,
            "feedback": "",
            "strategic_assessment": "schema-valid advisory",
            "evidence": {},
            "local_optima_warning": False,
        }
        raw_output = json.dumps(result, sort_keys=True)
        result["_llm_execution_material"] = {
            "invocation_id": _kwargs["execution_invocation_id"],
            "purpose": "system_strict_bootstrap_gate:critic",
            "role": "STRATEGY CRITIC",
            "prompt_digest": hashlib.sha256(b"test-critic-prompt").hexdigest(),
            "raw_output_digest": hashlib.sha256(
                raw_output.encode("utf-8")
            ).hexdigest(),
            "result_digest": llm_result_digest(0.0, {}),
            "log_file": str(tmp_path / "critic_io.txt"),
        }
        _complete_mocked_strict_query(
            _kwargs["strict_authority"],
            "mocked critic prompt",
            raw_output,
            "terminal-critic",
        )
        return result

    async def terminal_abandon(checkpoint_arg, *, reason, result):
        assert checkpoint_arg is active[0]
        abandoned_reasons.append(reason)
        return {
            **result,
            "abandoned": True,
            "checkpoint_stage": "abandoned",
            "abandon_result": {"abandoned": True},
        }

    def bot_dir(version):
        return active_candidate[0] if int(version) == 150 else SOURCE_DIR

    def unexpected_record(*_args, **_kwargs):
        raise AssertionError("terminal rejection must not record a retryable gate")

    monkeypatch.setattr(tool_gates, "_matching_checkpoint", lambda *_a: active[0])
    monkeypatch.setattr(
        tool_gates,
        "_owned_infrastructure_failure",
        lambda *_a, **_kw: (None, None),
    )
    monkeypatch.setattr(
        tool_gates,
        "_execute_exhausted_infrastructure_failure",
        no_exhausted_failure,
    )
    monkeypatch.setattr(tool_gates, "_idempotency_check", lambda *_a, **_kw: None)
    monkeypatch.setattr(tool_gates, "_quality_gate_ok", lambda *_a, **_kw: True)
    monkeypatch.setattr(tool_gates, "_review_gate_ok", lambda *_a, **_kw: True)
    monkeypatch.setattr(tool_gates, "_set_pipeline_status", lambda *_a, **_kw: None)
    monkeypatch.setattr(tool_gates, "log_system_event", lambda *_a, **_kw: None)
    monkeypatch.setattr(tool_gates, "get_bot_dir", bot_dir)
    monkeypatch.setattr(tool_gates, "get_logs_dir", lambda *_a: tmp_path)
    monkeypatch.setattr(tool_gates, "_get_ui", lambda: _UI())
    monkeypatch.setattr(
        tool_gates,
        "_llm_gate_infrastructure_identity",
        lambda **_kw: ("strict-bootstrap-terminal-test", {}),
    )
    monkeypatch.setattr(tool_gates, "_record_gate", unexpected_record)
    monkeypatch.setattr(tool_gates, "_record_quality_failure", lambda *_a, **_kw: None)
    monkeypatch.setattr(tool_gates, "run_claude_query", review_query)
    monkeypatch.setattr(tool_gates, "_run_critic", critic_query)
    monkeypatch.setattr(bootstrap, "abandon_rejected_blueprint", terminal_abandon)

    raw = asyncio.run(tool_gates.run_review.handler({
        "version": 150,
        "source_v": 142,
        "plan": [],
    }))
    rejected = json.loads(raw["content"][0]["text"])
    assert rejected["error"] == "SYSTEM_STRICT_BOOTSTRAP_REVIEW_REJECTED"
    assert rejected["abandoned"] is True

    def invalid_receipt(*_args, **_kwargs):
        raise bootstrap.SystemStrictBootstrapError(["content_chain_drift"])

    real_build_system_gate_receipt = bootstrap.build_system_gate_receipt
    monkeypatch.setattr(bootstrap, "build_system_gate_receipt", invalid_receipt)
    # This section models an independent generation branch.  A completed,
    # schema-valid but business-rejected Reviewer output in the first branch is
    # now intentionally recoverable after a pre-accept crash, so reusing its
    # authority store would replay that exact result instead of calling the
    # provider again.
    import strict_authority_workflow as authority
    from workflow_kernel import WorkflowStore

    receipt_drift_store = WorkflowStore(
        tmp_path / "receipt-drift-strict-authority.sqlite3"
    )
    monkeypatch.setattr(authority, "_store", lambda: receipt_drift_store)
    active[0] = deepcopy(checkpoint)
    review_approved[0] = True
    raw = asyncio.run(tool_gates.run_review.handler({
        "version": 150,
        "source_v": 142,
        "plan": [],
    }))
    invalid_review = json.loads(raw["content"][0]["text"])
    assert invalid_review["error"] == (
        "SYSTEM_STRICT_BOOTSTRAP_REVIEW_RECEIPT_INVALID"
    )
    assert invalid_review["abandoned"] is True

    # The Critic terminal-receipt case is an independent generation branch.
    # Reusing the Review branch's workflow id would intentionally collide with
    # its already accepted Review authority slot and mask the Critic behavior
    # this section is exercising.
    monkeypatch.setattr(
        bootstrap,
        "build_system_gate_receipt",
        real_build_system_gate_receipt,
    )
    candidate, valid_review_checkpoint, policy = _prepare_checkpoint(
        tmp_path,
        directory="critic-case",
    )
    _plan, _meta, _master, envelope = _bind_master(
        candidate,
        valid_review_checkpoint,
        policy,
    )
    _apply_and_bind_worker(candidate, valid_review_checkpoint, envelope)
    valid_review_checkpoint["stage"] = "quality_passed"
    _embed_gate(valid_review_checkpoint, candidate, "review")
    valid_review_checkpoint["stage"] = "reviewed"

    active_candidate[0] = candidate
    monkeypatch.setattr(bootstrap, "build_system_gate_receipt", invalid_receipt)

    active[0] = valid_review_checkpoint
    raw = asyncio.run(tool_gates.run_critic.handler({
        "version": 150,
        "source_v": 142,
        "plan": [],
        "reviewer_feedback": "",
    }))
    invalid_critic = json.loads(raw["content"][0]["text"])
    assert invalid_critic["error"] == (
        "SYSTEM_STRICT_BOOTSTRAP_CRITIC_RECEIPT_INVALID"
    )
    assert invalid_critic["abandoned"] is True
    assert abandoned_reasons == [
        "system_strict_bootstrap_review_rejected",
        "system_strict_bootstrap_review_receipt_invalid",
        "system_strict_bootstrap_critic_receipt_invalid",
    ]


def test_strict_blueprint_capability_probe_closes_the_runtime_consumer_floor(
    tmp_path,
):
    from national_capability_contract import evaluate_national_capabilities

    candidate, checkpoint, policy = _prepare_checkpoint(tmp_path)
    _plan, _meta, _master, envelope = _bind_master(
        candidate,
        checkpoint,
        policy,
    )
    _apply_and_bind_worker(candidate, checkpoint, envelope)
    capabilities = evaluate_national_capabilities(candidate)

    assert capabilities["conclusive"] is True
    for check_id in (*CORRECTNESS_CHECKS, *MIGRATION_CHECKS):
        assert capabilities["checks_by_id"][check_id]["passed"] is True, check_id
    samples = capabilities["decision_runtime_evidence"]["baseline_samples_ms"]
    assert samples and max(samples) <= 250.0
    assert capabilities["decision_path_risks"]["external_io"] == []
    assert capabilities["decision_path_risks"]["history_scans"] == []
    assert capabilities["decision_path_risks"]["large_runtime_tables"] == []

    facts = runpy.run_path(str(candidate / "simulation.py"))
    assert len(facts["PREFLOP_CLASS_LOOKUP_169"]) == 169
    assert len(facts["STRAIGHT_HIGH_BY_RANK_MASK_8192"]) == 8192


def test_output_capability_probe_requires_detector_identity_and_conclusion():
    import system_strict_bootstrap as bootstrap
    from national_capability_contract import (
        CAPABILITY_SCHEMA_VERSION,
        NATIONAL_CAPABILITY_DETECTOR_VERSION,
    )

    valid = {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "detector_version": NATIONAL_CAPABILITY_DETECTOR_VERSION,
        "conclusive": True,
        "ok": True,
        "infrastructure_failures": [],
        "checks_by_id": {
            check_id: {"passed": True}
            for check_id in MIGRATION_CHECKS
        },
    }
    assert bootstrap._output_capability_probe_errors(
        valid,
        required_check_ids=MIGRATION_CHECKS,
    ) == []

    mutations = (
        (
            {"schema_version": CAPABILITY_SCHEMA_VERSION + 1},
            "system_bootstrap_output_capability_probe_schema_mismatch",
        ),
        (
            {"detector_version": "stale-detector"},
            "system_bootstrap_output_capability_probe_detector_mismatch",
        ),
        (
            {"conclusive": False},
            "system_bootstrap_output_capability_probe_inconclusive",
        ),
        (
            {"ok": False},
            "system_bootstrap_output_capability_probe_not_ok",
        ),
        (
            {"infrastructure_failures": [{"reason": "probe_timeout"}]},
            "system_bootstrap_output_capability_probe_infrastructure_failure",
        ),
    )
    for replacement, expected_error in mutations:
        observed = {**deepcopy(valid), **replacement}
        errors = bootstrap._output_capability_probe_errors(
            observed,
            required_check_ids=MIGRATION_CHECKS,
        )
        assert expected_error in errors


def test_blueprint_application_rejects_inconclusive_all_true_probe(
    tmp_path,
    monkeypatch,
):
    import national_capability_contract
    from system_strict_bootstrap import SystemStrictBootstrapError, apply_blueprint

    candidate, checkpoint, policy = _prepare_checkpoint(tmp_path)
    _plan, _meta, _master, envelope = _bind_master(
        candidate,
        checkpoint,
        policy,
    )

    monkeypatch.setattr(
        national_capability_contract,
        "evaluate_national_capabilities",
        lambda _workspace: {
            "schema_version": national_capability_contract.CAPABILITY_SCHEMA_VERSION,
            "detector_version": (
                national_capability_contract.NATIONAL_CAPABILITY_DETECTOR_VERSION
            ),
            "conclusive": False,
            "ok": True,
            "infrastructure_failures": [{"reason": "runtime_probe_timeout"}],
            "checks_by_id": {
                check_id: {"passed": True}
                for check_id in MIGRATION_CHECKS
            },
        },
    )

    with pytest.raises(SystemStrictBootstrapError) as raised:
        apply_blueprint(
            candidate,
            checkpoint=checkpoint,
            envelope=envelope,
        )
    assert "system_bootstrap_output_capability_probe_inconclusive" in raised.value.errors
    assert (
        "system_bootstrap_output_capability_probe_infrastructure_failure"
        in raised.value.errors
    )
