from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path


SYSTEM_BOOTSTRAP_FILES = {
    "scripts/reset_national_tcp_policy_epoch.py",
    "web/core/system_strict_bootstrap.py",
    "web/core/strict_authority_workflow.py",
    "web/core/bootstrap_assets/strict_v1/manifest.json",
    "web/core/bootstrap_assets/strict_v1/policy.py",
    "web/core/bootstrap_assets/strict_v1/prepared_policy.py",
}
FIRST_STRICT_CONTROL_FILES = {
    "web/core/first_strict_execution_journal.py",
    "web/core/first_strict_control.py",
    "web/core/bootstrap_assets/first_strict_control_v1/manifest.json",
    "web/core/bootstrap_assets/first_strict_control_v1/policy.py",
}
FORMAL_BOOTSTRAP_FILES = {
    "web/core/official_bootstrap.py",
    "web/core/official_bootstrap_control.json",
}
LLM_CONTROL_FILES = {
    "web/core/llm_availability.py",
    "web/core/llm_availability_store.py",
}
ORCHESTRATOR_ROOT_GUARD_FILES = {
    "web/core/orchestrator_context.py",
    "web/core/epoch_authority.py",
}


def test_system_control_plane_is_exact_and_restart_critical_at_every_stage():
    import evaluation_contract
    import evolution_scope

    expected = (
        SYSTEM_BOOTSTRAP_FILES
        | FIRST_STRICT_CONTROL_FILES
        | LLM_CONTROL_FILES
        | ORCHESTRATOR_ROOT_GUARD_FILES
        | FORMAL_BOOTSTRAP_FILES
    )
    assert evaluation_contract.CONTRACT_VERSION == 26
    assert SYSTEM_BOOTSTRAP_FILES == set(
        evolution_scope.CRITICAL_SYSTEM_BOOTSTRAP_EXACT
    )
    assert FIRST_STRICT_CONTROL_FILES == set(
        evolution_scope.CRITICAL_FIRST_STRICT_CONTROL_EXACT
    )
    assert LLM_CONTROL_FILES == set(evolution_scope.CRITICAL_LLM_CONTROL_EXACT)
    assert ORCHESTRATOR_ROOT_GUARD_FILES <= (
        evolution_scope.CRITICAL_GENERATION_EXACT
    )
    assert expected <= evaluation_contract.ALWAYS_CRITICAL_EXACT
    assert SYSTEM_BOOTSTRAP_FILES | FIRST_STRICT_CONTROL_FILES <= (
        evolution_scope.CRITICAL_EVALUATION_GATE_EXACT
    )
    assert FORMAL_BOOTSTRAP_FILES <= (
        evolution_scope.CRITICAL_EVALUATION_GATE_EXACT
    )
    assert expected <= evolution_scope.CRITICAL_GENERATION_EXACT

    for stage in (
        "prepared",
        "direction_audited",
        "master_planned",
        "workers_done",
        "quality_passed",
        "reviewed",
        "critic_checked",
        "verified",
        "official_bootstrap_required",
    ):
        assert expected <= evaluation_contract.critical_exact_for_stage(
            stage,
            national_execution_mode="native_tcp",
        )

    assert evolution_scope.classify_path(
        "web/core/results/llm_availability_pause.json",
        candidate_v=300,
    ) == "runtime"


def test_blueprint_manifest_is_bound_to_runtime_policy_and_complete_oracle_set():
    import runtime_architecture_policy
    import system_strict_bootstrap

    manifest = system_strict_bootstrap.load_blueprint_manifest()
    assert manifest["official_policy_id"] == (
        runtime_architecture_policy.OFFICIAL_FULL_POLICY_ID
    )
    assert manifest["official_oracles"] == (
        runtime_architecture_policy.OFFICIAL_ORACLE_DOC_DIGESTS
    )
    assert system_strict_bootstrap.validate_blueprint_package(
        manifest,
        verify_source=False,
    ) == []

    incomplete = deepcopy(manifest)
    incomplete["official_oracles"].pop(
        "docs/official-terminal-settlement-oracle-2026-07-11.md"
    )
    errors = system_strict_bootstrap.validate_blueprint_package(
        incomplete,
        verify_source=False,
    )
    assert "system_bootstrap_official_oracle_set_mismatch" in errors


def test_evaluation_hash_tracks_blueprint_but_not_mutable_pause_record(
    tmp_path: Path,
    monkeypatch,
):
    import evaluation_contract

    manifest = tmp_path / "web/core/bootstrap_assets/strict_v1/manifest.json"
    pause = tmp_path / "web/core/results/llm_availability_pause.json"
    manifest.parent.mkdir(parents=True)
    pause.parent.mkdir(parents=True)
    manifest.write_text("blueprint-v1\n", encoding="utf-8")
    pause.write_text("pause-v1\n", encoding="utf-8")

    monkeypatch.setattr(
        evaluation_contract,
        "_git_ls_files",
        lambda _root, _pathspecs: [
            "web/core/bootstrap_assets/strict_v1/manifest.json",
            "web/core/results/llm_availability_pause.json",
        ],
    )
    contract = evaluation_contract.build_evaluation_contract(
        tmp_path,
        stage="verified",
        national_execution_mode="native_tcp",
    )
    assert evaluation_contract.is_contract_path(
        "web/core/bootstrap_assets/strict_v1/manifest.json",
        contract,
    )
    assert not evaluation_contract.is_contract_path(
        "web/core/results/llm_availability_pause.json",
        contract,
    )

    before = evaluation_contract.evaluation_contract_hash(tmp_path, contract)
    pause.write_text("pause-v2\n", encoding="utf-8")
    after_pause = evaluation_contract.evaluation_contract_hash(tmp_path, contract)
    manifest.write_text("blueprint-v2\n", encoding="utf-8")
    after_blueprint = evaluation_contract.evaluation_contract_hash(tmp_path, contract)

    assert after_pause == before
    assert after_blueprint != before
    assert len(after_blueprint) == hashlib.sha256().digest_size * 2


def test_pinned_official_oracle_bytes_remain_exact():
    import runtime_architecture_policy

    root = Path(__file__).resolve().parents[2]
    observed = {
        relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
        for relative in runtime_architecture_policy.OFFICIAL_ORACLE_DOC_DIGESTS
    }
    assert observed == runtime_architecture_policy.OFFICIAL_ORACLE_DOC_DIGESTS
