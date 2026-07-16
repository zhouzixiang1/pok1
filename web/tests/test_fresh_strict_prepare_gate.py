"""Prepare-gate separation for the one-time fresh strict bootstrap."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json


def _tool_payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _install_prepare_environment(
    tmp_path,
    monkeypatch,
    *,
    checkpoint: dict,
    active_bots: list[str],
    tagged_versions: set[int],
):
    import evolution_infra
    import repo_state
    import system_strict_bootstrap
    import tool_gates

    bots = tmp_path / "bots"
    bots.mkdir()
    state = deepcopy(checkpoint)
    writes: list[dict] = []

    def bot_dir(version: int):
        return bots / f"national_v{int(version)}"

    def read_checkpoint():
        return deepcopy(state)

    def matching(version: int, source_v: int | None = None):
        if state.get("next_v") != int(version):
            return None
        if source_v is not None and state.get("source_v") != int(source_v):
            return None
        return deepcopy(state)

    def write_checkpoint(next_v: int, source_v: int, stage: str, **kwargs):
        state.update({
            "next_v": int(next_v),
            "source_v": int(source_v),
            "stage": str(stage),
        })
        incoming_audit = kwargs.get("audit_context")
        if isinstance(incoming_audit, dict):
            merged_audit = dict(state.get("audit_context") or {})
            merged_audit.update(deepcopy(incoming_audit))
            state["audit_context"] = merged_audit
        for key, value in kwargs.items():
            if key != "audit_context":
                state[key] = deepcopy(value)
        writes.append(deepcopy(state))
        return True

    monkeypatch.setattr(tool_gates, "get_bot_dir", bot_dir)
    monkeypatch.setattr(tool_gates, "find_current_v", lambda: 142)
    monkeypatch.setattr(tool_gates, "read_pipeline_checkpoint", read_checkpoint)
    monkeypatch.setattr(tool_gates, "_matching_checkpoint", matching)
    monkeypatch.setattr(tool_gates, "_set_pipeline_status", lambda *_args: None)
    monkeypatch.setattr(tool_gates, "log_system_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(repo_state, "log_git_worktree_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        evolution_infra,
        "git_has_tag",
        lambda version: int(version) in tagged_versions,
    )
    monkeypatch.setattr(evolution_infra, "git_dir_is_committed", lambda _version: False)
    monkeypatch.setattr(evolution_infra, "get_active_bots", lambda: list(active_bots))
    monkeypatch.setattr(evolution_infra, "write_pipeline_checkpoint", write_checkpoint)
    reset_digest = str(
        (((checkpoint.get("audit_context") or {}).get("protocol_bootstrap") or {}).get(
            "epoch_reset_receipt_digest"
        ) or "")
    )
    monkeypatch.setattr(
        system_strict_bootstrap,
        "load_policy_epoch_reset_receipt",
        lambda: ({"receipt_digest": reset_digest}, []),
    )

    return tool_gates, bot_dir, state, writes


def test_real_prepare_materializes_fresh_142_to_143_without_source_tag_or_bytes(
    tmp_path,
    monkeypatch,
):
    from bot_artifact import hash_path
    from system_strict_bootstrap import (
        build_fresh_bootstrap_receipt,
        load_blueprint_manifest,
        validate_bootstrap_checkpoint,
        validate_fresh_bootstrap_receipt,
    )

    receipt = build_fresh_bootstrap_receipt(
        active_bots=(), epoch_reset_receipt_digest="a" * 64
    )
    assert validate_fresh_bootstrap_receipt(receipt, active_bots=()) == []
    checkpoint = {
        "source_v": 142,
        "next_v": 143,
        "stage": "selected",
        "audit_context": {
            "protocol_bootstrap": receipt,
            "selection": {
                "strategy": "fresh_policy_bootstrap",
                "bootstrap_without_strength_evidence": True,
            },
        },
    }
    tool_gates, bot_dir, state, writes = _install_prepare_environment(
        tmp_path,
        monkeypatch,
        checkpoint=checkpoint,
        active_bots=[],
        tagged_versions=set(),
    )

    # A historical source tree may exist locally, but no byte from it is an
    # input to the fresh strict artifact.
    source = bot_dir(142)
    source.mkdir()
    (source / ".completed").write_text("historical-only\n", encoding="utf-8")
    (source / "legacy_marker.py").write_text(
        "RAISE_BY_INCREMENT = 'must never inherit'\n",
        encoding="utf-8",
    )
    (source / "policy.py").write_text(
        "raise RuntimeError('old source opened')\n",
        encoding="utf-8",
    )

    def copying_old_source_is_forbidden(*_args, **_kwargs):
        raise AssertionError("fresh bootstrap must not copy v142")

    import evolution_infra

    monkeypatch.setattr(
        evolution_infra,
        "copy_bot_tree_for_candidate",
        copying_old_source_is_forbidden,
    )

    def resolve_target_only(version: int):
        if int(version) == 142:
            raise AssertionError(
                "fresh bootstrap must not even resolve the stale v142 path"
            )
        return bot_dir(version)

    monkeypatch.setattr(tool_gates, "get_bot_dir", resolve_target_only)

    payload = _tool_payload(asyncio.run(
        tool_gates.prepare_next_gen.handler({"source_v": 142, "next_v": 143})
    ))

    candidate = bot_dir(143)
    assert payload == {"prepared": True, "next_v": 143, "source_v": 142}
    assert sorted(path.name for path in candidate.iterdir()) == [
        "national_bot.py",
        "national_runtime_manifest.json",
        "policy.py",
        "policy_epoch_receipt.json",
        "precompute.py",
    ]
    assert not (candidate / "legacy_marker.py").exists()
    assert b"old source opened" not in (candidate / "policy.py").read_bytes()
    assert hash_path(candidate) == load_blueprint_manifest()["prepared_artifact_hash"]
    assert len(writes) == 2
    assert state["stage"] == "prepared"
    prepare_receipt = state["audit_context"]["protocol_bootstrap_prepare"]
    assert prepare_receipt["receipt_digest"] == receipt["receipt_digest"]
    assert prepare_receipt["source_artifact_inherited"] is False
    assert validate_bootstrap_checkpoint(
        state,
        candidate_dir=candidate,
        active_bots=(),
    ) == []

    # A retry may resume only from the exact checkpoint-owned prepared
    # contract.  It must not rewrite even the system blueprint bytes.
    before_retry = {
        path.name: path.read_bytes()
        for path in candidate.iterdir()
        if path.is_file()
    }
    resumed = _tool_payload(asyncio.run(
        tool_gates.prepare_next_gen.handler({"source_v": 142, "next_v": 143})
    ))
    after_retry = {
        path.name: path.read_bytes()
        for path in candidate.iterdir()
        if path.is_file()
    }
    assert resumed["success"] is True
    assert resumed["resumed"] is True
    assert resumed["stage"] == "prepared"
    assert resumed["prepared_artifact_hash"] == hash_path(candidate)
    assert before_retry == after_retry
    assert len(writes) == 2


def test_preexisting_unbound_target_is_preserved_for_canonical_abandon(
    tmp_path,
    monkeypatch,
):
    from system_strict_bootstrap import build_fresh_bootstrap_receipt

    receipt = build_fresh_bootstrap_receipt(
        active_bots=(),
        epoch_reset_receipt_digest="a" * 64,
    )
    checkpoint = {
        "source_v": 142,
        "next_v": 143,
        "stage": "selected",
        "workflow_run_id": "generation:143:workflow-v9",
        "checkpoint_revision": 4,
        "audit_context": {
            "protocol_bootstrap": receipt,
            "selection": {"strategy": "fresh_policy_bootstrap"},
        },
    }
    tool_gates, bot_dir, _state, writes = _install_prepare_environment(
        tmp_path,
        monkeypatch,
        checkpoint=checkpoint,
        active_bots=[],
        tagged_versions=set(),
    )
    target = bot_dir(143)
    target.mkdir()
    marker = target / "unbound-policy.py"
    marker.write_text("UNTRUSTED_PREIMAGE = True\n", encoding="utf-8")
    before = marker.read_bytes()
    import tool_bot_management

    abandon_calls = []

    async def canonical_abandon(*, reason, **identity):
        abandon_calls.append((reason, identity))
        return {
            "abandoned": True,
            "cleared_checkpoint": True,
            "workflow_run_id": checkpoint["workflow_run_id"],
            "abandon_transaction_id": "1" * 64,
            "abandon_receipt_digest": "2" * 64,
            "finalize_receipt_digest": "3" * 64,
            "abandon_checkpoint_identity": {
                "workflow_run_id": checkpoint["workflow_run_id"],
                "next_v": 143,
                "source_v": 142,
                "checkpoint_revision": 4,
                "stage": "selected",
            },
        }

    monkeypatch.setattr(
        tool_bot_management,
        "_do_abandon_generation",
        canonical_abandon,
    )

    payload = _tool_payload(asyncio.run(
        tool_gates.prepare_next_gen.handler({"source_v": 142, "next_v": 143})
    ))

    assert payload["error"] == "TARGET_PREIMAGE_REQUIRES_CANONICAL_ABANDON"
    assert payload["stage"] == "selected"
    assert payload["abandoned"] is True
    assert abandon_calls == [
        (
            "stale_blueprint_rejection:prepare_preimage_unbound",
            {
                "expected_workflow_run_id": checkpoint["workflow_run_id"],
                "expected_next_v": 143,
                "expected_source_v": 142,
                "expected_checkpoint_revision": 4,
                "expected_checkpoint_stage": "selected",
            },
        )
    ]
    assert marker.read_bytes() == before
    assert writes == []


def test_preexisting_target_without_checkpoint_is_never_adopted_or_deleted(
    tmp_path,
    monkeypatch,
):
    checkpoint = {"source_v": 143, "next_v": 144, "stage": "selected"}
    tool_gates, bot_dir, _state, writes = _install_prepare_environment(
        tmp_path,
        monkeypatch,
        checkpoint=checkpoint,
        active_bots=["national_v143"],
        tagged_versions={143},
    )
    source = bot_dir(143)
    source.mkdir()
    (source / ".completed").write_text("published\n", encoding="utf-8")
    target = bot_dir(144)
    target.mkdir()
    marker = target / "orphan.py"
    marker.write_text("ORPHAN = True\n", encoding="utf-8")
    before = marker.read_bytes()
    monkeypatch.setattr(tool_gates, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(tool_gates, "_matching_checkpoint", lambda *_a, **_k: None)

    payload = _tool_payload(asyncio.run(
        tool_gates.prepare_next_gen.handler({"source_v": 143, "next_v": 144})
    ))

    assert payload["error"] == "TARGET_PREIMAGE_REQUIRES_CANONICAL_ABANDON"
    assert payload["stage"] is None
    assert marker.read_bytes() == before
    assert writes == []


def test_normal_unpublished_parent_still_fails_closed_before_copy(
    tmp_path,
    monkeypatch,
):
    checkpoint = {"source_v": 143, "next_v": 144, "stage": "selected"}
    tool_gates, bot_dir, _state, writes = _install_prepare_environment(
        tmp_path,
        monkeypatch,
        checkpoint=checkpoint,
        active_bots=["national_v143"],
        tagged_versions=set(),
    )
    source = bot_dir(143)
    source.mkdir()
    (source / ".completed").write_text("local-only\n", encoding="utf-8")

    payload = _tool_payload(asyncio.run(
        tool_gates.prepare_next_gen.handler({"source_v": 143, "next_v": 144})
    ))

    assert "has no git tag 'national-bot-v143'" in payload["error"]
    assert not bot_dir(144).exists()
    assert writes == []


def test_tampered_fresh_receipt_cannot_obtain_the_v142_tag_exemption(
    tmp_path,
    monkeypatch,
):
    from system_strict_bootstrap import build_fresh_bootstrap_receipt

    receipt = build_fresh_bootstrap_receipt(
        active_bots=(), epoch_reset_receipt_digest="a" * 64
    )
    receipt["next_v"] = 144
    checkpoint = {
        "source_v": 142,
        "next_v": 143,
        "stage": "selected",
        "audit_context": {"protocol_bootstrap": receipt},
    }
    tool_gates, bot_dir, _state, writes = _install_prepare_environment(
        tmp_path,
        monkeypatch,
        checkpoint=checkpoint,
        active_bots=[],
        tagged_versions=set(),
    )

    payload = _tool_payload(asyncio.run(
        tool_gates.prepare_next_gen.handler({"source_v": 142, "next_v": 143})
    ))

    assert payload["error"] == "PROTOCOL_BOOTSTRAP_RECEIPT_INVALID"
    assert "system_bootstrap_receipt_digest_mismatch" in payload["validation_errors"]
    assert "fresh_bootstrap_receipt_subject_mismatch" in payload["validation_errors"]
    assert not bot_dir(143).exists()
    assert writes == []


def test_singleton_receipt_cannot_replace_current_parent_role_eligibility(
    tmp_path,
    monkeypatch,
):
    from bot_artifact import canonical_digest
    from system_strict_bootstrap import materialize_fresh_candidate

    subject = {
        "schema_version": 1,
        "kind": "national-tcp-policy-singleton-bootstrap-v1",
        "mode": "singleton_strict_bootstrap",
        "source_v": 143,
        "next_v": 144,
        "source_artifact_inherited": True,
        "active_bots": [],
    }
    receipt = {**subject, "receipt_digest": canonical_digest(subject)}
    checkpoint = {
        "source_v": 143,
        "next_v": 144,
        "stage": "selected",
        "audit_context": {"protocol_bootstrap": receipt},
    }
    tool_gates, bot_dir, _state, writes = _install_prepare_environment(
        tmp_path,
        monkeypatch,
        checkpoint=checkpoint,
        active_bots=[],
        tagged_versions={143},
    )
    source = bot_dir(143)
    materialize_fresh_candidate(source, version=143, final_policy=True)
    (source / ".completed").write_text("published-cache-only\n", encoding="utf-8")

    payload = _tool_payload(asyncio.run(
        tool_gates.prepare_next_gen.handler({"source_v": 143, "next_v": 144})
    ))

    assert "not eligible for the active national pool" in payload["error"]
    assert "signed full-v5 certificate" in payload["error"]
    assert not bot_dir(144).exists()
    assert writes == []
