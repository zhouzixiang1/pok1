import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import precommit_eval_contract as contract
import tool_eval
import national_native
import national_runtime_probe
from bot_namespace import bot_name, bot_tag, parse_bot_version
from web.tests.runtime_probe_fixtures import passing_runtime_probe


def _tool_payload(result):
    return json.loads(result["content"][0]["text"])


def _published(path: Path, *, artifact_hash: str = "a" * 64) -> dict:
    return {
        "published": True,
        "artifact_hash": artifact_hash,
        "tag": bot_tag(parse_bot_version(str(path.name))),
        "tag_type": "tag",
        "tag_object": "b" * 40,
        "commit_oid": "c" * 40,
        "completion_tree_oid": "d" * 40,
        "main_tree_oid": "d" * 40,
        "issues": [],
    }


def _bn(version: int) -> str:
    """Branch-portable bot directory name for a version."""
    return bot_name(version)


def _make_plan(tmp_path, monkeypatch, *, matches=3):
    opponents = []
    identities = {}
    for version in (8, 5):
        path = tmp_path / _bn(version)
        path.mkdir()
        (path / "national_bot.py").write_text("# native\n", encoding="utf-8")
        opponents.append({"name": path.name, "reason": "parent" if version == 8 else "top"})
        identities[str(path)] = _published(path, artifact_hash=str(version) * 64)

    monkeypatch.setattr(
        contract,
        "published_bot_identity",
        lambda path: dict(identities[str(Path(path))]),
    )
    plan = contract.create_precommit_plan(
        candidate_version=9,
        source_version=8,
        profile_id="national_native",
        execution_mode="native_tcp",
        evaluation_protocol="national",
        opponents=opponents,
        hands_per_match=70,
        matches_per_opponent=matches,
        path_resolver=lambda item: tmp_path / item["name"],
        require_published_opponents=True,
    )
    return plan, identities


def _infra_timeout_checkpoint(
    candidate: Path,
    *,
    stage="infra_timed_out",
    fingerprint=None,
):
    from tool_gates import _bot_code_fingerprint

    artifact_fingerprint = (
        _bot_code_fingerprint(candidate)
        if fingerprint is None
        else str(fingerprint)
    )
    gate_identity = {"version": 9, "source_v": 8, "passed": True}
    return {
        "next_v": 9,
        "source_v": 8,
        "stage": stage,
        "checkpoint_revision": 7,
        "precommit_attempt": 1,
        "workflow_run_id": "generation:9:infra-retry",
        "workflow_profile_id": "national_native",
        "repair_baseline_artifact_hash": artifact_fingerprint,
        "gate_results": {
            "quality": {
                **gate_identity,
                "all_passed": True,
                "critical_scenarios_passed": True,
                "workflow_profile_id": "national_native",
                "national_execution_mode": "native_tcp",
                "national_native_contract_ok": True,
                "code_fingerprint": artifact_fingerprint,
                "national_capability_contract": {
                    "dynamic_runtime_probe": passing_runtime_probe(),
                },
                **national_runtime_probe.runtime_probe_native_template_evidence(),
            },
            "review": {
                **gate_identity,
                "approved": True,
                "llm_invoked": True,
                "reviewer_llm_executed": True,
                "schema_valid": True,
            },
            "critic": {
                **gate_identity,
                "approved": True,
                "llm_invoked": True,
                "critic_llm_executed": True,
                "schema_valid": True,
            },
        },
    }


def test_plan_freezes_order_identity_and_all_sample_seeds(tmp_path, monkeypatch):
    plan, _ = _make_plan(tmp_path, monkeypatch, matches=3)

    assert [row["name"] for row in plan["opponents"]] == [_bn(8), _bn(5)]
    assert len(plan["sample_plan"]) == 6
    assert plan["sample_plan"][0] == {
        "opponent": _bn(8),
        "opponent_index": 0,
        "repeat": 1,
        "deck_seed_base": 91_000,
        "bot_seed_base": 1_000_091_000,
        "native_match_timing_plan_digest": plan["settings"][
            "native_match_timing_plan_digest"
        ],
    }
    assert plan["sample_plan"][-1]["deck_seed_base"] == 193_000
    assert contract.validate_precommit_plan(
        plan,
        candidate_version=9,
        source_version=8,
        profile_id="national_native",
        execution_mode="native_tcp",
        evaluation_protocol="national",
    ) == []


def test_native_batch_plan_binds_ordered_samples_and_execution_phases(
    tmp_path,
    monkeypatch,
):
    plan, _ = _make_plan(tmp_path, monkeypatch, matches=3)
    timing = national_native.require_native_match_timing_plan(
        plan["settings"]["native_match_timing_plan"],
        hands=70,
        requested_timeout_sec=national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
    )
    batch = plan["settings"]["native_precommit_batch_plan"]

    assert batch["schema_version"] == 1
    assert batch["ordered_samples"] == plan["sample_plan"]
    assert batch["sample_count"] == 6
    assert batch["max_new_samples_per_invocation"] == 6
    assert batch["timing_plan_digest"] == timing.digest()
    assert batch["per_sample_execution_timeout_us"] == (
        timing.execution_timeout_us
    )
    assert batch["batch_execution_timeout_us"] == (
        6 * timing.execution_timeout_us
    )
    assert batch["batch_plan_digest"] == plan["settings"][
        "native_precommit_batch_plan_digest"
    ]

    plan["settings"]["native_precommit_batch_plan"][
        "max_new_samples_per_invocation"
    ] = 1
    issues = contract.validate_precommit_plan(
        plan,
        candidate_version=9,
        source_version=8,
        profile_id="national_native",
        execution_mode="native_tcp",
        evaluation_protocol="national",
    )
    assert "precommit_plan_digest_mismatch" in issues
    assert "precommit_native_batch_plan_mismatch" in issues


def test_first_strict_batch_advances_one_new_sample_per_provider_invocation():
    timing = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
    )
    rows = [
        {
            "opponent": "first_strict_control_v1",
            "opponent_index": 0,
            "repeat": repeat,
            "deck_seed_base": 91_000 + (repeat - 1) * 1_000,
            "bot_seed_base": 1_000_091_000 + (repeat - 1) * 1_000,
            "native_match_timing_plan_digest": timing.digest(),
        }
        for repeat in range(1, 9)
    ]
    batch = contract.build_native_precommit_batch_plan(
        rows,
        native_timing_plan=timing,
        first_strict_control=True,
    )

    assert batch["sample_count"] == 8
    assert batch["max_new_samples_per_invocation"] == 1
    assert batch["batch_execution_timeout_us"] == (
        8 * timing.execution_timeout_us
    )
    assert batch["batch_effect_lease_timeout_us"] == (
        8 * timing.first_strict_lease_timeout_us
    )


def test_native_plan_rejects_shortened_strength_matches(tmp_path, monkeypatch):
    opponent = tmp_path / _bn(8)
    opponent.mkdir()
    (opponent / "national_bot.py").write_text("# native\n", encoding="utf-8")
    monkeypatch.setattr(
        contract,
        "published_bot_identity",
        lambda _path: _published(opponent),
    )

    with pytest.raises(
        contract.PrecommitEvalContractError,
        match="exactly 70 hands",
    ):
        contract.create_precommit_plan(
            candidate_version=9,
            source_version=8,
            profile_id="national_native",
            execution_mode="native_tcp",
            evaluation_protocol="national",
            opponents=[{"name": _bn(8), "reason": "parent"}],
            hands_per_match=3,
            matches_per_opponent=8,
            path_resolver=lambda _item: opponent,
            require_published_opponents=True,
        )


def test_plan_fails_closed_when_published_opponent_identity_drifts(tmp_path, monkeypatch):
    plan, identities = _make_plan(tmp_path, monkeypatch)
    identities[str(tmp_path / _bn(8))]["artifact_hash"] = "f" * 64

    issues = contract.validate_precommit_plan(
        plan,
        candidate_version=9,
        source_version=8,
        profile_id="national_native",
        execution_mode="native_tcp",
        evaluation_protocol="national",
    )

    assert "precommit_opponent_" + _bn(8) + "_identity_drift" in issues


def test_evaluation_contract_binds_candidate_code_and_frozen_plan(tmp_path, monkeypatch):
    plan, _ = _make_plan(tmp_path, monkeypatch)
    first = contract.build_evaluation_contract(
        plan,
        candidate_code_fingerprint="candidate-a",
    )
    assert contract.validate_evaluation_contract(
        first,
        plan,
        candidate_code_fingerprint="candidate-a",
    ) == []
    assert contract.validate_evaluation_contract(
        first,
        plan,
        candidate_code_fingerprint="candidate-b",
    ) == ["precommit_evaluation_contract_mismatch"]


@pytest.mark.asyncio
async def test_infra_timeout_precommit_retry_requires_exact_cas_restore(
    tmp_path,
    monkeypatch,
):
    candidate = tmp_path / _bn(9)
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    checkpoint = _infra_timeout_checkpoint(candidate)
    writes = []
    profile = SimpleNamespace(
        profile_id="national_native",
        evaluation_protocol="national",
        national_execution_mode="native_tcp",
    )

    monkeypatch.setattr(tool_eval, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tool_eval, "get_workflow_profile", lambda: profile)
    monkeypatch.setattr(tool_eval, "_matching_checkpoint", lambda *_: checkpoint)

    def reject_restore(*args, **kwargs):
        writes.append((args, kwargs))
        return False

    monkeypatch.setattr(tool_eval, "write_pipeline_checkpoint", reject_restore)

    result = await tool_eval.run_precommit_eval.handler({
        "version": 9,
        "source_v": 8,
    })
    payload = _tool_payload(result)

    assert writes == [
        (
            (9, 8, "critic_checked"),
            {
                "expected_checkpoint_revision": 7,
                "expected_checkpoint_stage": "infra_timed_out",
                "expected_workflow_run_id": "generation:9:infra-retry",
            },
        )
    ]
    assert payload["error"].startswith(
        "STATE BLOCKED: Failed to restore infra_timed_out"
    )


@pytest.mark.asyncio
async def test_infra_timeout_precommit_retry_reproves_restored_stage(
    tmp_path,
    monkeypatch,
):
    candidate = tmp_path / _bn(9)
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    checkpoint = _infra_timeout_checkpoint(candidate)
    profile = SimpleNamespace(
        profile_id="national_native",
        evaluation_protocol="national",
        national_execution_mode="native_tcp",
    )

    monkeypatch.setattr(tool_eval, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tool_eval, "get_workflow_profile", lambda: profile)
    monkeypatch.setattr(tool_eval, "_matching_checkpoint", lambda *_: checkpoint)
    monkeypatch.setattr(
        tool_eval,
        "write_pipeline_checkpoint",
        lambda *_args, **_kwargs: True,
    )

    result = await tool_eval.run_precommit_eval.handler({
        "version": 9,
        "source_v": 8,
    })
    payload = _tool_payload(result)

    assert payload["error"].startswith(
        "STATE BLOCKED: Infra-timeout checkpoint restoration could not be re-proven"
    )


@pytest.mark.asyncio
async def test_infra_timeout_retry_missing_candidate_preserves_overlay(
    tmp_path,
    monkeypatch,
):
    candidate = tmp_path / _bn(9)
    checkpoint = _infra_timeout_checkpoint(candidate, fingerprint="a" * 64)
    profile = SimpleNamespace(
        profile_id="national_native",
        evaluation_protocol="national",
        national_execution_mode="native_tcp",
    )
    writes = []

    monkeypatch.setattr(tool_eval, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tool_eval, "get_workflow_profile", lambda: profile)
    monkeypatch.setattr(tool_eval, "_matching_checkpoint", lambda *_: checkpoint)
    monkeypatch.setattr(
        tool_eval,
        "write_pipeline_checkpoint",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )

    async def backend(**_kwargs):
        pytest.fail("missing infra-timeout candidate reached precommit backend")

    monkeypatch.setattr(tool_eval, "_run_national_precommit_backend", backend)

    result = await tool_eval.run_precommit_eval.handler({
        "version": 9,
        "source_v": 8,
    })
    payload = _tool_payload(result)

    assert payload["error"].startswith(
        "STATE BLOCKED: Infra-timeout retry candidate artifact is missing"
    )
    assert payload["checkpoint_stage"] == "infra_timed_out"
    assert checkpoint["stage"] == "infra_timed_out"
    assert writes == []


@pytest.mark.asyncio
async def test_infra_timeout_retry_candidate_drift_preserves_overlay(
    tmp_path,
    monkeypatch,
):
    candidate = tmp_path / _bn(9)
    candidate.mkdir()
    entry = candidate / "national_bot.py"
    entry.write_text("# reviewed native bytes\n", encoding="utf-8")
    checkpoint = _infra_timeout_checkpoint(candidate)
    entry.write_text("# unreviewed changed bytes\n", encoding="utf-8")
    profile = SimpleNamespace(
        profile_id="national_native",
        evaluation_protocol="national",
        national_execution_mode="native_tcp",
    )
    writes = []

    monkeypatch.setattr(tool_eval, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tool_eval, "get_workflow_profile", lambda: profile)
    monkeypatch.setattr(tool_eval, "_matching_checkpoint", lambda *_: checkpoint)
    monkeypatch.setattr(
        tool_eval,
        "write_pipeline_checkpoint",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )

    async def backend(**_kwargs):
        pytest.fail("drifted infra-timeout candidate reached precommit backend")

    monkeypatch.setattr(tool_eval, "_run_national_precommit_backend", backend)

    result = await tool_eval.run_precommit_eval.handler({
        "version": 9,
        "source_v": 8,
    })
    payload = _tool_payload(result)

    assert payload["error"].startswith(
        "STATE BLOCKED: Infra-timeout retry candidate artifact drifted"
    )
    assert payload["checkpoint_stage"] == "infra_timed_out"
    assert checkpoint["stage"] == "infra_timed_out"
    assert writes == []


@pytest.mark.parametrize(
    ("invalid_proof", "expected_error"),
    [
        (
            "gate_chain",
            "gate chain is incomplete or invalid",
        ),
        (
            "gate_identity",
            "gate identity does not match the active generation",
        ),
        (
            "quality_baseline_binding",
            "quality gate and checkpoint artifact bindings disagree",
        ),
    ],
)
@pytest.mark.asyncio
async def test_infra_timeout_retry_rejects_invalid_gate_reproof(
    tmp_path,
    monkeypatch,
    invalid_proof,
    expected_error,
):
    candidate = tmp_path / _bn(9)
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    checkpoint = _infra_timeout_checkpoint(candidate)
    if invalid_proof == "gate_chain":
        checkpoint["gate_results"]["review"]["approved"] = False
    elif invalid_proof == "gate_identity":
        checkpoint["gate_results"]["critic"]["source_v"] = 7
    else:
        checkpoint["repair_baseline_artifact_hash"] = "b" * 64

    profile = SimpleNamespace(
        profile_id="national_native",
        evaluation_protocol="national",
        national_execution_mode="native_tcp",
    )
    writes = []

    monkeypatch.setattr(tool_eval, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tool_eval, "get_workflow_profile", lambda: profile)
    monkeypatch.setattr(tool_eval, "_matching_checkpoint", lambda *_: checkpoint)
    monkeypatch.setattr(
        tool_eval,
        "write_pipeline_checkpoint",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )

    async def backend(**_kwargs):
        pytest.fail("invalid infra-timeout proof reached precommit backend")

    monkeypatch.setattr(tool_eval, "_run_national_precommit_backend", backend)

    result = await tool_eval.run_precommit_eval.handler({
        "version": 9,
        "source_v": 8,
    })
    payload = _tool_payload(result)

    assert expected_error in payload["error"]
    assert payload["checkpoint_stage"] == "infra_timed_out"
    assert checkpoint["stage"] == "infra_timed_out"
    assert writes == []


@pytest.mark.asyncio
async def test_infra_timeout_matching_candidate_allows_exact_cas_restore(
    tmp_path,
    monkeypatch,
):
    candidate = tmp_path / _bn(9)
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    checkpoint = _infra_timeout_checkpoint(candidate)
    restored_checkpoint = {
        **checkpoint,
        "stage": "critic_checked",
        "checkpoint_revision": checkpoint["checkpoint_revision"] + 1,
    }
    profile = SimpleNamespace(
        profile_id="national_native",
        evaluation_protocol="national",
        national_execution_mode="native_tcp",
    )
    reads = iter((checkpoint, restored_checkpoint))
    writes = []

    monkeypatch.setattr(tool_eval, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tool_eval, "get_workflow_profile", lambda: profile)
    monkeypatch.setattr(tool_eval, "_matching_checkpoint", lambda *_: next(reads))

    def restore(*args, **kwargs):
        writes.append((args, kwargs))
        return True

    monkeypatch.setattr(tool_eval, "write_pipeline_checkpoint", restore)
    monkeypatch.setattr(
        tool_eval,
        "_prepare_official_profile_refresh",
        lambda *_args, **_kwargs: {
            "ok": False,
            "error": "stop_after_exact_infra_restore",
        },
    )

    async def backend(**_kwargs):
        pytest.fail("test sentinel should stop before precommit backend")

    monkeypatch.setattr(tool_eval, "_run_national_precommit_backend", backend)

    result = await tool_eval.run_precommit_eval.handler({
        "version": 9,
        "source_v": 8,
    })
    payload = _tool_payload(result)

    assert writes == [
        (
            (9, 8, "critic_checked"),
            {
                "expected_checkpoint_revision": 7,
                "expected_checkpoint_stage": "infra_timed_out",
                "expected_workflow_run_id": "generation:9:infra-retry",
            },
        )
    ]
    assert payload["error"] == "STATE BLOCKED: stop_after_exact_infra_restore"
    assert payload["checkpoint_stage"] == "critic_checked"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attempt_case", "checkpoint_stage"),
    [
        ("infra_timeout", "infra_timed_out"),
        ("infra_scope_drift", "infra_timed_out"),
        ("process_cancel", "critic_checked"),
        ("fresh", "critic_checked"),
    ],
)
async def test_control_attempt_freezes_or_reuses_journal_identity(
    tmp_path,
    monkeypatch,
    attempt_case,
    checkpoint_stage,
):
    import system_strict_bootstrap
    from tool_gates import _bot_code_fingerprint

    candidate = tmp_path / _bn(9)
    control_path = tmp_path / "first_strict_control_v1"
    candidate.mkdir()
    control_path.mkdir()
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    fingerprint = _bot_code_fingerprint(candidate)
    receipt = {
        "candidate_version": 9,
        "source_version": 8,
        "active_policy_bots": [],
        "receipt_digest": "d" * 64,
        "control": {
            "control_id": control_path.name,
            "path": str(control_path.absolute()),
            "artifact_hash": "c" * 64,
        },
    }
    opponent = {
        "name": control_path.name,
        "path": str(control_path.absolute()),
        "reason": "first_strict_empty_pool_control",
        "authority": "system_first_strict_control",
        "precommit_gate_admitted": True,
        "formal_bootstrap_opponent_admitted": True,
        "formal_bootstrap_scope": "first_policy_bot_empty_pool_only",
        "strength_admitted": False,
        "rating_eligible": False,
        "official_opponent_eligible": False,
        "control_receipt": receipt,
    }
    timing_plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
    )
    plan = {
        "plan_digest": "e" * 64,
        "opponents": [dict(opponent)],
        "settings": {
            "hands_per_match": 70,
            "matches_per_opponent": 1,
            "native_match_timing_plan": timing_plan.snapshot(),
            "native_match_timing_plan_digest": timing_plan.digest(),
        },
        "sample_plan": [],
    }
    evaluation_contract = contract.build_evaluation_contract(
        plan,
        candidate_code_fingerprint=fingerprint,
    )
    frozen_scope = tool_eval._build_first_strict_control_execution_scope(
        v=9,
        candidate_name=candidate.name,
        code_fingerprint=fingerprint,
        opponents=[opponent],
        precommit_plan=plan,
        evaluation_contract=evaluation_contract,
        workflow_run_id="generation:9:infra-retry",
        checkpoint_revision=7,
        precommit_attempt=1,
    )
    checkpoint = _infra_timeout_checkpoint(candidate, stage=checkpoint_stage)
    checkpoint["checkpoint_revision"] = 9
    checkpoint["precommit_attempt"] = 0 if attempt_case == "fresh" else 1
    checkpoint["audit_context"] = {"precommit_eval_plan": plan}
    if attempt_case != "fresh":
        stored_scope = dict(frozen_scope)
        if attempt_case == "infra_scope_drift":
            stored_scope["candidate_artifact_hash"] = "b" * 64
        checkpoint["audit_context"][
            tool_eval._FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY
        ] = stored_scope
    checkpoint["gate_results"]["quality"][
        "first_strict_control_receipt"
    ] = receipt
    profile = SimpleNamespace(
        profile_id="national_native",
        evaluation_protocol="national",
        national_execution_mode="native_tcp",
    )
    writes = []
    backend_calls = []

    monkeypatch.setattr(tool_eval, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tool_eval, "get_workflow_profile", lambda: profile)
    monkeypatch.setattr(tool_eval, "_matching_checkpoint", lambda *_: checkpoint)
    monkeypatch.setattr(tool_eval, "validate_precommit_plan", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(tool_eval, "opponents_from_plan", lambda _plan: [dict(opponent)])
    monkeypatch.setattr(tool_eval, "_prepare_official_profile_refresh", lambda *_: {"ok": True})
    monkeypatch.setattr(tool_eval, "candidate_observability_identity", None)
    monkeypatch.setattr(tool_eval, "append_candidate_event", None)
    monkeypatch.setattr(
        system_strict_bootstrap,
        "is_declared_native_bootstrap",
        lambda _checkpoint: True,
    )
    monkeypatch.setattr(
        system_strict_bootstrap,
        "validate_system_gate_receipt",
        lambda *_args, **_kwargs: [],
    )

    def restore(_v, _source_v, stage, **kwargs):
        writes.append((stage, dict(kwargs)))
        assert stage == "critic_checked"
        if attempt_case == "infra_timeout":
            assert "precommit_attempt" not in kwargs
        elif attempt_case == "fresh":
            assert kwargs["precommit_attempt"] == 1
            checkpoint["precommit_attempt"] = 1
            checkpoint["audit_context"].update(kwargs["audit_context"])
        else:
            pytest.fail("cancelled attempt must not write a new identity")
        checkpoint["stage"] = stage
        checkpoint["checkpoint_revision"] += 1
        return True

    async def backend(**kwargs):
        backend_calls.append(kwargs)
        return tool_eval._json_tool_result({"passed": True})

    monkeypatch.setattr(tool_eval, "write_pipeline_checkpoint", restore)
    monkeypatch.setattr(tool_eval, "_run_national_precommit_backend", backend)

    result = await tool_eval.run_precommit_eval.handler({
        "version": 9,
        "source_v": 8,
    })

    if attempt_case == "infra_scope_drift":
        payload = _tool_payload(result)
        assert "cannot re-prove its journal" in payload["error"]
        assert payload["checkpoint_stage"] == "infra_timed_out"
        assert writes == []
        assert backend_calls == []
        return

    assert _tool_payload(result) == {"passed": True}
    assert len(writes) == (0 if attempt_case == "process_cancel" else 1)
    if writes:
        assert writes[0][0] == "critic_checked"
    assert checkpoint["precommit_attempt"] == 1
    assert len(backend_calls) == 1
    call = backend_calls[0]
    assert call["precommit_attempt"] == 1
    assert call["checkpoint_revision"] == (
        9 if attempt_case == "process_cancel" else 10
    )
    expected_scope = (
        checkpoint["audit_context"][
            tool_eval._FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY
        ]
    )
    assert call["control_execution_scope"] == expected_scope
    assert call["control_execution_scope"]["checkpoint_revision"] == (
        10 if attempt_case == "fresh" else 7
    )


@pytest.mark.asyncio
async def test_national_precommit_backend_passes_exact_attempt_cancel_token(
    tmp_path,
    monkeypatch,
):
    import asyncio
    import threading
    import time

    import national_native

    candidate = tmp_path / _bn(9)
    opponent = tmp_path / _bn(8)
    candidate.mkdir()
    opponent.mkdir()
    candidate_entry = candidate / "national_bot.py"
    candidate_entry.write_text("# native\n", encoding="utf-8")
    token = threading.Event()
    observed_tokens = []
    terminal_writes = []

    async def run_native(*_args, cancel_token=None, **_kwargs):
        observed_tokens.append(cancel_token)
        raise asyncio.CancelledError("test attempt cancellation")

    monkeypatch.setattr(national_native, "run_native_precommit", run_native)
    monkeypatch.setattr(tool_eval, "candidate_observability_identity", None)
    monkeypatch.setattr(
        tool_eval,
        "_record_gate",
        lambda *_args, **_kwargs: terminal_writes.append("checkpoint"),
    )
    monkeypatch.setattr(
        tool_eval,
        "append_candidate_event",
        lambda *_args, **_kwargs: terminal_writes.append("candidate_event"),
    )
    profile = SimpleNamespace(
        profile_id="national_native",
        national_execution_mode="native_tcp",
    )
    opponents = [{
        "name": opponent.name,
        "path": str(opponent),
        "reason": "parent",
        "precommit_gate_admitted": True,
        "strength_admitted": True,
    }]

    with pytest.raises(asyncio.CancelledError):
        await tool_eval._run_national_precommit_backend(
            v=9,
            source_v=8,
            requested_n_games=2,
            effective_n_games=2,
            candidate_name=candidate.name,
            parent_name=opponent.name,
            candidate_entry=candidate_entry,
            code_fingerprint="a" * 64,
            workflow_profile=profile,
            candidate_id=candidate.name,
            opponents=opponents,
            all_opponents=list(opponents),
            precommit_attempt=1,
            initial_blockers=[],
            started_at=time.time(),
            precommit_plan={
                "settings": {
                    "hands_per_match": 70,
                    "matches_per_opponent": 2,
                    "native_match_timing_plan": (
                        national_native.build_native_match_timing_plan(
                            hands=70,
                            requested_timeout_sec=(
                                national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC
                            ),
                        ).snapshot()
                    ),
                    "native_match_timing_plan_digest": (
                        national_native.build_native_match_timing_plan(
                            hands=70,
                            requested_timeout_sec=(
                                national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC
                            ),
                        ).digest()
                    ),
                },
                "sample_plan": [],
            },
            evaluation_contract={"contract_digest": "b" * 64},
            shutdown_token=token,
        )

    assert observed_tokens == [token]
    assert terminal_writes == []


@pytest.mark.asyncio
async def test_cancelled_precommit_attempt_sets_only_its_captured_token(
    monkeypatch,
):
    import asyncio
    import threading

    tool_eval.reset_precommit_shutdown()
    current = tool_eval.current_precommit_shutdown_token()
    detached = threading.Event()

    async def cancelled_backend(**_kwargs):
        raise asyncio.CancelledError("deterministic route cancelled")

    monkeypatch.setattr(
        tool_eval,
        "_run_national_precommit_backend",
        cancelled_backend,
    )

    with pytest.raises(asyncio.CancelledError):
        await tool_eval._run_national_precommit_attempt(detached)

    assert detached.is_set() is True
    assert current.is_set() is False
    assert tool_eval.current_precommit_shutdown_token() is current


@pytest.mark.parametrize(
    ("asset_name", "before_bytes", "after_bytes"),
    [
        ("ranges.json", b'{"open": ["AA"]}\n', b'{"open": ["AA", "KK"]}\n'),
        ("policy.model", b"model-v1\x00weights", b"model-v2\x00weights"),
        ("equity_table.txt", b"AA=0.85\n", b"AA=0.81\n"),
    ],
)
def test_precommit_candidate_fingerprint_covers_non_python_decision_assets(
    tmp_path,
    asset_name,
    before_bytes,
    after_bytes,
):
    from tool_gates import _bot_code_fingerprint

    candidate = tmp_path / _bn(9)
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    asset = candidate / asset_name
    asset.write_bytes(before_bytes)
    before = _bot_code_fingerprint(candidate)
    frozen = contract.build_evaluation_contract(
        {"plan_digest": "frozen-plan"},
        candidate_code_fingerprint=before,
    )

    asset.write_bytes(after_bytes)
    after = _bot_code_fingerprint(candidate)

    assert after != before
    assert contract.validate_evaluation_contract(
        frozen,
        {"plan_digest": "frozen-plan"},
        candidate_code_fingerprint=after,
    ) == ["precommit_evaluation_contract_mismatch"]


def test_precommit_candidate_fingerprint_ignores_runtime_markers_and_caches(tmp_path):
    from tool_gates import _bot_code_fingerprint

    candidate = tmp_path / _bn(9)
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    before = _bot_code_fingerprint(candidate)

    (candidate / ".completed").write_text("runtime marker\n", encoding="utf-8")
    cache = candidate / "__pycache__"
    cache.mkdir()
    (cache / "national_bot.cpython-314.pyc").write_bytes(b"runtime cache")

    assert _bot_code_fingerprint(candidate) == before


def test_plan_digest_detects_seed_schedule_tampering(tmp_path, monkeypatch):
    plan, _ = _make_plan(tmp_path, monkeypatch)
    plan["sample_plan"][0]["deck_seed_base"] += 1

    issues = contract.validate_precommit_plan(
        plan,
        candidate_version=9,
        source_version=8,
        profile_id="national_native",
        execution_mode="native_tcp",
        evaluation_protocol="national",
    )

    assert "precommit_plan_digest_mismatch" in issues
    assert "precommit_sample_plan_mismatch" in issues


@pytest.mark.asyncio
async def test_verified_precommit_cache_rejects_missing_runtime_repeatability_receipt(
    tmp_path,
    monkeypatch,
):
    """A direct precommit caller cannot reuse a stale quality receipt.

    The normal scheduler refreshes quality before it routes to precommit, but
    ``run_precommit_eval`` is also an externally callable tool.  Its verified
    cache must independently re-check the quality/review/critic chain so a
    malformed dynamic runtime-probe repeatability receipt cannot turn into an
    ``ALREADY PASSED`` response.
    """

    candidate = tmp_path / _bn(9)
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    broken_probe = passing_runtime_probe()
    broken_probe.pop("repeatability")
    template_evidence = national_runtime_probe.runtime_probe_native_template_evidence()
    fingerprint = "a" * 64
    checkpoint = {
        "next_v": 9,
        "source_v": 8,
        "stage": "verified",
        "workflow_profile_id": "national_native",
        "audit_context": {"precommit_eval_plan": {"plan_digest": "frozen"}},
        "gate_results": {
            "quality": {
                "all_passed": True,
                "critical_scenarios_passed": True,
                "workflow_profile_id": "national_native",
                "national_execution_mode": "native_tcp",
                "national_native_contract_ok": True,
                "code_fingerprint": fingerprint,
                "national_capability_contract": {
                    "dynamic_runtime_probe": broken_probe,
                },
                **template_evidence,
            },
            "review": {
                "approved": True,
                "llm_invoked": True,
                "reviewer_llm_executed": True,
                "schema_valid": True,
            },
            "critic": {
                "approved": True,
                "llm_invoked": True,
                "critic_llm_executed": True,
                "schema_valid": True,
            },
            "precommit_eval": {
                "passed": True,
                "code_fingerprint": fingerprint,
                "workflow_profile_id": "national_native",
                "national_execution_mode": "native_tcp",
                "precommit_eval_contract": {"contract_digest": "frozen"},
                **template_evidence,
            },
        },
    }
    profile = SimpleNamespace(
        profile_id="national_native",
        evaluation_protocol="national",
        national_execution_mode="native_tcp",
    )
    backend_calls = []

    monkeypatch.setattr(tool_eval, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(tool_eval, "get_workflow_profile", lambda: profile)
    monkeypatch.setattr(tool_eval, "_matching_checkpoint", lambda *_: checkpoint)
    monkeypatch.setattr(
        tool_eval,
        "_prepare_official_profile_refresh",
        lambda *_args, **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(
        "tool_helpers._active_workflow_profile_info",
        lambda: ("national_native", "native_tcp"),
    )
    monkeypatch.setattr(
        "tool_gates._bot_code_fingerprint",
        lambda _path: fingerprint,
    )
    monkeypatch.setattr(tool_eval, "validate_precommit_plan", lambda *_a, **_k: [])
    monkeypatch.setattr(
        tool_eval,
        "build_evaluation_contract",
        lambda *_a, **_k: {"contract_digest": "frozen"},
    )
    monkeypatch.setattr(
        tool_eval,
        "validate_evaluation_contract",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(tool_eval, "append_candidate_event", None)
    monkeypatch.setattr(tool_eval, "candidate_observability_identity", None)

    async def backend(**_kwargs):
        backend_calls.append(True)
        pytest.fail("stale quality evidence reached precommit backend")

    monkeypatch.setattr(tool_eval, "_run_national_precommit_backend", backend)

    result = await tool_eval.run_precommit_eval.handler(
        {"version": 9, "source_v": 8}
    )
    payload = _tool_payload(result)

    assert payload["error"].startswith(
        "STATE BLOCKED: run_precommit_eval requires passing quality/reviewer"
    )
    assert payload.get("idempotent_cache") is not True
    assert backend_calls == []


@pytest.mark.asyncio
async def test_tool_reuses_frozen_opponents_when_live_selection_changes(tmp_path, monkeypatch):
    bots = tmp_path / "bots"
    for version in (9, 8, 5, 4):
        bot_dir = bots / _bn(version)
        bot_dir.mkdir(parents=True)
        (bot_dir / "national_bot.py").write_text("# native\n", encoding="utf-8")

    checkpoint = {
        "next_v": 9,
        "source_v": 8,
        "stage": "critic_checked",
        "audit_context": {},
        "gate_results": {
            "quality": {
                "all_passed": True,
                "critical_scenarios_passed": True,
                "workflow_profile_id": "national_native",
                "national_execution_mode": "native_tcp",
                "national_native_contract_ok": True,
                "national_capability_contract": {
                    "dynamic_runtime_probe": passing_runtime_probe(),
                },
                **national_runtime_probe.runtime_probe_native_template_evidence(),
            },
            "review": {
                "approved": True,
                "llm_invoked": True,
                "reviewer_llm_executed": True,
                "schema_valid": True,
            },
            "critic": {
                "approved": True,
                "llm_invoked": True,
                "critic_llm_executed": True,
                "schema_valid": True,
            },
        },
    }
    profile = SimpleNamespace(
        profile_id="national_native",
        evaluation_protocol="national",
        national_execution_mode="native_tcp",
        national_precommit_hands=70,
        national_precommit_matches=2,
    )
    selected = [
        {"name": _bn(8), "reason": "parent"},
        {"name": _bn(5), "reason": "top_strength"},
    ]
    backend_calls = []

    monkeypatch.setattr(tool_eval, "get_workflow_profile", lambda: profile)
    monkeypatch.setattr(tool_eval, "get_bot_dir", lambda version: bots / _bn(version))
    monkeypatch.setattr(tool_eval, "_matching_checkpoint", lambda *_: checkpoint)
    monkeypatch.setattr(tool_eval, "_prepare_official_profile_refresh", lambda *_: {"ok": True})
    monkeypatch.setattr("tool_helpers._active_workflow_profile_info", lambda: ("national_native", "native_tcp"))
    def select_opponents(*_args, **kwargs):
        assert kwargs["checkpoint"] is checkpoint
        return list(selected)

    monkeypatch.setattr(tool_eval, "_select_precommit_opponents", select_opponents)
    monkeypatch.setattr(tool_eval, "append_candidate_event", None)
    monkeypatch.setattr("tool_gates._bot_code_fingerprint", lambda _path: "candidate-code")
    monkeypatch.setattr(
        contract,
        "published_bot_identity",
        lambda path: _published(Path(path), artifact_hash=Path(path).name[-1] * 64),
    )

    def fake_write(_v, _source_v, stage, **kwargs):
        checkpoint["stage"] = stage
        if kwargs.get("audit_context"):
            checkpoint["audit_context"].update(kwargs["audit_context"])
        if kwargs.get("precommit_attempt") is not None:
            checkpoint["precommit_attempt"] = kwargs["precommit_attempt"]
        return True

    async def fake_backend(**kwargs):
        backend_calls.append(kwargs)
        return tool_eval._json_tool_result({"passed": True})

    monkeypatch.setattr(tool_eval, "write_pipeline_checkpoint", fake_write)
    monkeypatch.setattr(tool_eval, "_run_national_precommit_backend", fake_backend)

    await tool_eval.run_precommit_eval.handler({"version": 9, "source_v": 8, "n_games": 2})
    assert [row["name"] for row in backend_calls[0]["opponents"]] == [
        _bn(8),
        _bn(5),
    ]
    frozen_plan = backend_calls[0]["precommit_plan"]
    assert frozen_plan["require_published_opponents"] is True
    assert all(
        row["authority"] == "published_bot"
        and row["strength_admitted"] is True
        and row["formal_bootstrap_opponent_admitted"] is False
        and row["identity"]["published"] is True
        and row["identity"]["artifact_hash"]
        and row["identity"]["tag"]
        for row in frozen_plan["opponents"]
    )

    selected[:] = [{"name": _bn(4), "reason": "new_live_leader"}]
    await tool_eval.run_precommit_eval.handler({"version": 9, "source_v": 8, "n_games": 16})

    assert [row["name"] for row in backend_calls[1]["opponents"]] == [
        _bn(8),
        _bn(5),
    ]
    assert backend_calls[1]["precommit_plan"] == backend_calls[0]["precommit_plan"]
    assert backend_calls[1]["effective_n_games"] == 4
