from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest


def _write_pair(module, *, version: int = 143, source_v: int = 142):
    publication_id = f"{version:064x}"
    commit_oid = f"{version:040x}"
    completion_object = f"{version + 1:040x}"
    high_water_object = f"{version + 2:040x}"
    artifact_hash = f"{version + 3:064x}"
    certificate_digest = f"{version + 4:064x}"
    completion_tree_oid = f"{version + 5:040x}"
    completion_name = module.bot_tag(version)
    high_water_name = f"national-high-water-v{version}"
    proof_payload = {
        "schema_version": 1,
        "kind": "national-tcp-policy-pending-local-publication",
        "bot": module.bot_name(version),
        "version": version,
        "artifact_hash": artifact_hash,
        "tag": completion_name,
        "tag_object": completion_object,
        "commit_oid": commit_oid,
        "completion_tree_oid": completion_tree_oid,
        "main_commit_oid": commit_oid,
    }
    local_proof = {
        **proof_payload,
        "proof_digest": module.canonical_digest(proof_payload),
    }
    epoch_binding = {"fixture": version}
    projection = {
        "checkpoint_schema_version": 2,
        "evaluation_epoch": module.EVALUATION_EPOCH,
        "epoch_binding": epoch_binding,
        "next_v": version,
        "source_v": source_v,
        "parent2_v": None,
        "generation_mode": "master",
        "workflow_run_id": f"generation:{version}:handoff-test",
        "checkpoint_revision": 11,
        "stage": "publishing",
        "workflow_profile_id": "national-policy-v1",
        "national_execution_mode": "native_tcp",
        "generation_attempt": 0,
        "precommit_rework_count": 0,
        "official_rework_count": 0,
        "repair_baseline_artifact_hash": None,
        "audit_context": {},
        "publication_intent": {
            "publication_id": publication_id,
            "candidate_artifact_hash": artifact_hash,
            "official_certificate_digest": certificate_digest,
        },
    }
    source_binding = {
        "source_v": source_v,
        "parent2_v": None,
        "epoch_binding": epoch_binding,
        "protocol_bootstrap_receipt_digest": None,
    }
    publication_identity = {
        "schema_version": module.SCHEMA_VERSION,
        "evaluation_epoch": module.EVALUATION_EPOCH,
        "version": version,
        "source_v": source_v,
        "workflow_run_id": projection["workflow_run_id"],
        "checkpoint_revision": projection["checkpoint_revision"],
        "publishing_checkpoint_digest": module.canonical_digest(projection),
        "publication_id": publication_id,
        "commit_oid": commit_oid,
        "candidate_artifact_hash": artifact_hash,
        "source_binding_digest": module.canonical_digest(source_binding),
        "local_paired_refs": {
            completion_name: {
                "object_oid": completion_object,
                "peeled_commit_oid": commit_oid,
            },
            high_water_name: {
                "object_oid": high_water_object,
                "peeled_commit_oid": commit_oid,
            },
        },
        "local_publication_proof": local_proof,
        "certificate_digest": certificate_digest,
        "remote_publication": {
            "required": False,
            "explicit_test_mode": True,
            "remote_main_oid": None,
            "paired_refs": {},
        },
    }
    base = {
        "schema_version": 2,
        "kind": "national-policy-generation-archive",
        "evaluation_epoch": module.EVALUATION_EPOCH,
        "version": version,
        "source_v": source_v,
        "bot_name": module.bot_name(version),
        "git_tag": module.bot_tag(version),
        "publication_identity": publication_identity,
        "publishing_checkpoint_projection": projection,
        "publishing_checkpoint_digest": module.canonical_digest(projection),
        "strength_evidence_identity": {"fixture": version},
        "review_score": 0,
        "reviewer_change_summary": "",
        "reviewer_risk_areas": [],
        "critic_score": 0,
        "precommit_passed": True,
    }
    base_digest = module.canonical_digest(base)
    record = module._base_record(publication_identity, base_digest)
    archive = {
        **base,
        "base_snapshot_digest": base_digest,
        "post_publication_handoff": {
            "identity_digest": record["identity_digest"],
            "publication_id": publication_id,
            "state": "pending",
        },
        "finalization": {"state": "pending"},
    }
    record_path = module._handoff_path(version, publication_id)
    module._atomic_write(module._archive_path(version), archive)
    module._atomic_write(record_path, record)
    module._atomic_write(
        module._active_pointer_path(),
        module._active_pointer(record, record_path),
    )
    return record_path, record, archive


@pytest.fixture
def handoff_env(tmp_path, monkeypatch):
    import evolution_infra
    import generation_evidence
    import post_publication_handoff as module
    import tool_commit

    results = tmp_path / "results"
    monkeypatch.setattr(
        evolution_infra,
        "POST_PUBLICATION_HANDOFF_DIR",
        results / "post_publication_handoffs",
    )
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(
        evolution_infra, "WORKER_FAILURES_FILE", results / "worker_failures.jsonl"
    )
    monkeypatch.setattr(
        evolution_infra, "MATCH_HISTORY_FILE", results / "match_history.jsonl"
    )
    monkeypatch.setattr(
        evolution_infra, "RATING_HISTORY_FILE", results / "rating_history.jsonl"
    )
    monkeypatch.setattr(
        evolution_infra, "LLM_COSTS_FILE", results / "llm_costs.jsonl"
    )
    monkeypatch.setattr(evolution_infra, "ARCHIVE_DIR", results / "archive")
    monkeypatch.setattr(tool_commit, "RESULTS_DIR", results)
    monkeypatch.setattr(tool_commit, "ARCHIVE_DIR", results / "archive")
    monkeypatch.setenv(
        "POK_ALLOW_LOCAL_ONLY_POST_PUBLICATION_HANDOFF_FOR_TESTS", "1"
    )
    monkeypatch.setattr(
        generation_evidence,
        "generation_evidence_identity_errors",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(module, "local_handoff_identity_errors", lambda _record: [])
    monkeypatch.setattr(
        module,
        "_test_original_reprove_operational_steps",
        module._reprove_operational_steps,
        raising=False,
    )
    monkeypatch.setattr(
        module,
        "_reprove_operational_steps",
        lambda _record: {
            "stability_observation": True,
            "reap_signal": True,
            "priority_eval": True,
        },
    )
    monkeypatch.setattr(
        module,
        "_test_original_reprove_external_steps",
        module._reprove_external_steps,
        raising=False,
    )
    monkeypatch.setattr(module, "_reprove_external_steps", lambda _record: None)
    with module._ACTIVE_CLAIMS_LOCK:
        module._ACTIVE_CLAIMS.clear()
    yield module, results
    with module._ACTIVE_CLAIMS_LOCK:
        module._ACTIVE_CLAIMS.clear()


def _complete_all_steps(
    module,
    version=143,
    source_v=142,
    *,
    finalize=True,
):
    remote_calls = []
    original_remote = module._remote_handoff_identity_errors

    def remote(record):
        remote_calls.append(record["identity_digest"])
        return original_remote(record)

    module._remote_handoff_identity_errors = remote
    try:
        record, claim_id = module.claim_post_publication_handoff(
            version, source_v
        )
        assert len(remote_calls) == 1
        for step in module.REQUIRED_STEPS:
            identity = record["identity"]
            publication_id = identity["publication_id"]
            archive = module.load_archive_snapshot(version)
            import tool_commit
            from evolution_infra import build_archive_rotation_plan
            from tool_bot_management import (
                REAP_SELECTION_POLICY,
                _capture_reap_selection_snapshot,
            )

            selection_snapshot = _capture_reap_selection_snapshot(
                [], max_active_bots=tool_commit.MAX_ACTIVE_BOTS
            )
            plans = {
                "stability_observation": {
                    "schema_version": 1,
                    "kind": "stability-observation-plan",
                    "publication_id": publication_id,
                    "publishing_checkpoint_digest": identity[
                        "publishing_checkpoint_digest"
                    ],
                    "strength_evidence_identity_digest": module.canonical_digest(
                        archive["strength_evidence_identity"]
                    ),
                },
                "reap_signal": {
                    "schema_version": 1,
                    "kind": "rating-daemon-refresh-plan",
                    "publication_id": publication_id,
                    "signal_text": "1.000000\n",
                    "signal_sha256": hashlib.sha256(
                        b"1.000000\n"
                    ).hexdigest(),
                },
                "priority_eval": {
                    "schema_version": 1,
                    "kind": "priority-evaluation-plan",
                    "payload": {
                        "bot": module.bot_name(version),
                        "min_games": 500,
                        "since": 1.0,
                        "publication_id": publication_id,
                    },
                },
                "archive_rotation": build_archive_rotation_plan(
                    version, publication_id
                ),
                "log_cleanup": {
                    "schema_version": 1,
                    "kind": "strict-log-cleanup-plan",
                    "handoff_version": version,
                    "first_strict_version": module.FIRST_STRICT_POLICY_VERSION,
                    "keep_generations": 5,
                    "cutoff_version": version - 5,
                    "archives": [],
                    "publication_id": publication_id,
                },
                "pool_reap": {
                    "schema_version": 2,
                    "kind": "strict-active-pool-reap-plan",
                    "publication_id": publication_id,
                    "selection_policy": REAP_SELECTION_POLICY,
                    "selection_snapshot": selection_snapshot,
                    "selection_snapshot_digest": selection_snapshot[
                        "snapshot_digest"
                    ],
                    "active_bots": [],
                    "active_pool_digest": module.canonical_digest([]),
                    "max_active_bots": tool_commit.MAX_ACTIVE_BOTS,
                    "required_reaps": 0,
                    "targets": [],
                    "target_sequence_digest": module.canonical_digest([]),
                    "expected_head_oid": identity["commit_oid"],
                    "expected_remote_main_oid": None,
                },
                "cycle_annotation": {
                    "schema_version": 1,
                    "kind": "cycle-archivist-annotation-plan",
                    "publication_id": publication_id,
                    "archive_pre_annotation_digest": module.canonical_digest({
                        key: value for key, value in archive.items()
                        if key != "archivist_notes"
                    }),
                },
            }
            if step == "housekeeping":
                plans[step] = {
                    "schema_version": 1,
                    "kind": "post-publication-worktree-verification-plan",
                    "expected_head_oid": identity["commit_oid"],
                    "expected_dirty_paths": [],
                    "tracked_housekeeping_commit_allowed": False,
                    "dependency_receipts": {
                        name: record["steps"][name]["receipt"]["receipt_digest"]
                        for name in (
                            "archive_rotation", "log_cleanup", "pool_reap",
                            "cycle_annotation",
                        )
                    },
                }
            record = module.plan_handoff_step(
                version, source_v, claim_id, step, plans[step]
            )
            row = record["steps"][step]
            if step == "cycle_annotation":
                import cycle_archivist

                annotation_payload = {
                    "schema_version": cycle_archivist.ARCHIVIST_SCHEMA_VERSION,
                    "kind": cycle_archivist.ARCHIVIST_KIND,
                    "subject": {
                        "epoch": module.EVALUATION_EPOCH,
                        "version": version,
                        "source_v": source_v,
                        "bot": module.bot_name(version),
                        "tag": module.bot_tag(version),
                        "artifact_hash": archive["publication_identity"][
                            "candidate_artifact_hash"
                        ],
                        "strength_evidence_identity": archive[
                            "strength_evidence_identity"
                        ],
                    },
                    "status": "annotated",
                    "issues": [],
                    "analysis": {
                        "generation_assessment": "neutral",
                        "archive_notes": "Journal contract fixture.",
                    },
                }
                annotation = {
                    **annotation_payload,
                    "annotation_digest": module.canonical_digest(
                        annotation_payload
                    ),
                }
                output = module.write_archive_annotation(
                    version, source_v, claim_id, annotation
                )
            elif step == "stability_observation":
                output = {
                    "publication_id": publication_id,
                    "continuity_id": "b" * 32,
                    "count": 1,
                    "target": 10,
                    "complete": False,
                }
            elif step == "reap_signal":
                output = {
                    "publication_id": publication_id,
                    "signal_sha256": plans[step]["signal_sha256"],
                }
            elif step == "priority_eval":
                encoded = json.dumps(
                    plans[step]["payload"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ) + "\n"
                output = {
                    "bot": module.bot_name(version),
                    "min_games": 500,
                    "publication_id": publication_id,
                    "payload_sha256": hashlib.sha256(
                        encoded.encode("utf-8")
                    ).hexdigest(),
                }
            elif step == "archive_rotation":
                output = {
                    "version": version,
                    "rotations": [],
                    "rotation_set_digest": module.canonical_digest([]),
                }
            elif step == "log_cleanup":
                output = {
                    "version": version,
                    "archives": [],
                    "archive_set_digest": module.canonical_digest([]),
                }
            elif step == "pool_reap":
                output = {
                    "removed_bots": [],
                    "required_reaps": 0,
                    "reap_proofs": [],
                    "reap_proof_set_digest": module.canonical_digest([]),
                }
            elif step == "housekeeping":
                output = {
                    "head_oid": identity["commit_oid"],
                    "worktree_status_digest": hashlib.sha256(b"").hexdigest(),
                    "tracked_housekeeping_commit": False,
                    "archive_rotation_revalidated": True,
                    "strict_log_archives_revalidated": True,
                    "reap_proofs": [],
                    "reap_proof_set_digest": module.canonical_digest([]),
                }
            else:
                raise AssertionError(step)
            output["plan_digest"] = row["plan_digest"]
            record = module.complete_handoff_step(
                version, source_v, claim_id, step, output
            )
            assert len(remote_calls) == 1
        if not finalize:
            return record, claim_id
        completed = module.complete_post_publication_handoff(
            version, source_v, claim_id
        )
        assert len(remote_calls) == 2
        return completed
    finally:
        module._remote_handoff_identity_errors = original_remote


def test_exact_suffix_marker_is_ignored_and_first_run_completes(handoff_env):
    module, results = handoff_env
    record_path, _record, _archive = _write_pair(module)
    assert record_path.name.endswith(module.RECORD_SUFFIX)
    marker = record_path.with_name(
        record_path.name.removesuffix(module.RECORD_SUFFIX)
        + ".completed-event.json"
    )
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("not a handoff record", encoding="utf-8")

    route = module.pending_handoff_route()
    assert route["status"] == "pending"
    assert Path(route["record"]["_path"]) == record_path

    completed = _complete_all_steps(module)
    assert completed["state"] == "completed"
    assert not module._active_pointer_path().exists()
    assert marker.exists()
    assert module.pending_handoff_route()["status"] == "none"
    assert (results / "archive" / "v143.json").exists()
    planned = completed["steps"]["stability_observation"]
    assert planned["plan_digest"] == planned["receipt"]["plan_digest"]


@pytest.mark.parametrize("crash_boundary", ["archive_only", "record_without_pointer"])
def test_ensure_resumes_each_pre_clear_publication_boundary(
    handoff_env, monkeypatch, crash_boundary
):
    module, _results = handoff_env
    record_path, record, archive = _write_pair(module)
    pointer_path = module._active_pointer_path()
    pointer_path.unlink()
    if crash_boundary == "archive_only":
        record_path.unlink()
    publication_identity = {
        key: value
        for key, value in record["identity"].items()
        if key != "archive_base_snapshot_digest"
    }
    base = {
        key: value
        for key, value in archive.items()
        if key not in {
            "base_snapshot_digest",
            "post_publication_handoff",
            "finalization",
            "archivist_notes",
        }
    }
    monkeypatch.setattr(
        module,
        "_publication_identity",
        lambda **_kwargs: copy.deepcopy(publication_identity),
    )
    monkeypatch.setattr(
        module,
        "_archive_base",
        lambda **_kwargs: copy.deepcopy(base),
    )

    resumed = module.ensure_post_publication_handoff(
        version=143,
        source_v=142,
        publishing_checkpoint={},
        publication_result={},
    )
    assert resumed["state"] == "pending"
    assert module._read_json(record_path)["identity_digest"] == record[
        "identity_digest"
    ]
    assert pointer_path.exists()
    assert module.pending_handoff_route()["status"] == "pending"


def test_ensure_refuses_publication_until_authority_directories_are_durable(
    handoff_env, monkeypatch
):
    module, results = handoff_env
    record_path, record, archive = _write_pair(module)
    publication_identity = {
        key: value
        for key, value in record["identity"].items()
        if key != "archive_base_snapshot_digest"
    }
    base = {
        key: value
        for key, value in archive.items()
        if key not in {
            "base_snapshot_digest",
            "post_publication_handoff",
            "finalization",
            "archivist_notes",
        }
    }
    shutil.rmtree(record_path.parent)
    shutil.rmtree(results / "archive")
    monkeypatch.setattr(
        module,
        "_publication_identity",
        lambda **_kwargs: copy.deepcopy(publication_identity),
    )
    monkeypatch.setattr(
        module,
        "_archive_base",
        lambda **_kwargs: copy.deepcopy(base),
    )
    original_fsync = module._fsync_directory
    fail_parent_fsync = {"enabled": True}

    def injected_fsync(path):
        if fail_parent_fsync["enabled"] and Path(path) == results:
            raise OSError("injected authority parent fsync failure")
        return original_fsync(path)

    monkeypatch.setattr(module, "_fsync_directory", injected_fsync)
    with pytest.raises(OSError, match="injected authority parent fsync failure"):
        module.ensure_post_publication_handoff(
            version=143,
            source_v=142,
            publishing_checkpoint={},
            publication_result={},
        )
    assert not record_path.exists()
    assert not module._active_pointer_path().exists()
    assert not module._archive_path(143).exists()

    fail_parent_fsync["enabled"] = False
    resumed = module.ensure_post_publication_handoff(
        version=143,
        source_v=142,
        publishing_checkpoint={},
        publication_result={},
    )
    assert resumed["state"] == "pending"
    assert record_path.exists()
    assert module._active_pointer_path().exists()
    assert module._archive_path(143).exists()


def test_publication_identity_requires_the_exact_local_tag_tree_proof(handoff_env):
    module, _results = handoff_env
    _path, record, archive = _write_pair(module)
    expected = {
        key: value
        for key, value in record["identity"].items()
        if key != "archive_base_snapshot_digest"
    }
    checkpoint = copy.deepcopy(archive["publishing_checkpoint_projection"])
    checkpoint["publication_intent"]["remote_publication_required"] = False
    # The projection digest in this direct producer test is rebuilt by the
    # producer itself; no stored record is being validated here.
    refs = {
        name: {**row, "type": "tag"}
        for name, row in expected["local_paired_refs"].items()
    }
    publication_result = {
        "committed": True,
        "publication_id": expected["publication_id"],
        "commit_oid": expected["commit_oid"],
        "local_refs": refs,
        "local_publication_proof": expected["local_publication_proof"],
        "remote_proof": {"valid": True, "local_only": True},
    }
    produced = module._publication_identity(
        version=143,
        source_v=142,
        checkpoint=checkpoint,
        publication_result=publication_result,
        allow_local_only=True,
    )
    assert produced["local_publication_proof"] == expected[
        "local_publication_proof"
    ]
    assert produced["certificate_digest"] == expected["certificate_digest"]

    missing = dict(publication_result)
    missing.pop("local_publication_proof")
    with pytest.raises(
        module.PostPublicationHandoffError,
        match="local_publication_proof_shape_invalid",
    ):
        module._publication_identity(
            version=143,
            source_v=142,
            checkpoint=checkpoint,
            publication_result=missing,
            allow_local_only=True,
        )


def test_released_claim_becomes_pending_and_another_claim_can_take_over(
    handoff_env,
):
    module, _results = handoff_env
    record_path, _record, _archive = _write_pair(module)
    _claimed, first_claim = module.claim_post_publication_handoff(143, 142)
    running_route = module.pending_handoff_route()
    assert running_route["status"] == "pending"
    assert running_route["state"] == "running"

    module.release_post_publication_handoff_claim(
        143, 142, first_claim, error="injected crash"
    )
    released = module._read_json(record_path)
    assert released["state"] == "pending"
    assert released["owner"] is None
    assert released["last_error"] == "injected crash"

    claimed, second_claim = module.claim_post_publication_handoff(143, 142)
    assert second_claim != first_claim
    assert claimed["state"] == "running"
    module.release_post_publication_handoff_claim(143, 142, second_claim)


def test_dead_running_owner_projects_effective_pending(handoff_env):
    module, _results = handoff_env
    record_path, record, _archive = _write_pair(module)
    record["state"] = "running"
    record["owner"] = {
        "claim_id": "d" * 32,
        "pid": 999_999_999,
        "process_start_token": "1",
        "claimed_at": 1.0,
        "heartbeat_at": 1.0,
    }
    record["revision"] += 1
    record["updated_at"] = 1.0
    record["record_digest"] = module._record_digest(record)
    module._atomic_write(record_path, record)

    route = module.pending_handoff_route()

    assert route["status"] == "pending"
    assert route["state"] == "pending"
    assert route["durable_state"] == "running"


def test_route_blocks_when_local_publication_reproof_fails(
    handoff_env,
    monkeypatch,
):
    module, _results = handoff_env
    _write_pair(module)
    monkeypatch.setattr(
        module,
        "local_handoff_identity_errors",
        lambda _record: ["handoff_live_certificate_digest_mismatch"],
    )

    route = module.pending_handoff_route()

    assert route["status"] == "blocked"
    assert any(
        "handoff_live_certificate_digest_mismatch" in issue
        for issue in route["issues"]
    )


def test_live_foreign_owner_cannot_be_stolen_even_with_old_heartbeat(
    handoff_env, monkeypatch
):
    module, _results = handoff_env
    record_path, _record, _archive = _write_pair(module)
    claimed, claim_id = module.claim_post_publication_handoff(
        143, 142, now=1.0
    )
    claimed["owner"].update({
        "pid": os.getpid() + 100_000,
        "process_start_token": "123",
        "heartbeat_at": 1.0,
    })
    claimed["record_digest"] = module._record_digest(claimed)
    module._atomic_write(record_path, claimed)
    with module._ACTIVE_CLAIMS_LOCK:
        module._ACTIVE_CLAIMS.discard(claim_id)
    monkeypatch.setattr(module, "_owner_alive", lambda _owner: True)

    with pytest.raises(
        module.PostPublicationHandoffError,
        match="already_running",
    ):
        module.claim_post_publication_handoff(143, 142, now=10_000.0)

    module.release_post_publication_handoff_claim(143, 142, claim_id)


def test_release_write_failure_drops_volatile_claim_and_allows_recovery(
    handoff_env, monkeypatch
):
    module, _results = handoff_env
    _write_pair(module)
    _record, claim_id = module.claim_post_publication_handoff(143, 142)
    original_write = module._atomic_write

    def fail_release_write(_path, _payload):
        raise OSError("injected release write failure")

    monkeypatch.setattr(module, "_atomic_write", fail_release_write)
    module.release_post_publication_handoff_claim(
        143, 142, claim_id, error="worker failed"
    )

    with module._ACTIVE_CLAIMS_LOCK:
        assert claim_id not in module._ACTIVE_CLAIMS

    monkeypatch.setattr(module, "_atomic_write", original_write)
    recovered, recovered_claim = module.claim_post_publication_handoff(143, 142)
    assert recovered["state"] == "running"
    assert recovered_claim and recovered_claim != claim_id
    module.release_post_publication_handoff_claim(143, 142, recovered_claim)


def test_completed_history_damage_is_not_scanned_without_active_pointer(
    handoff_env,
):
    module, _results = handoff_env
    record_path, _record, _archive = _write_pair(module)
    _complete_all_steps(module)
    record_path.write_text("corrupt completed history", encoding="utf-8")

    assert module.discover_post_publication_handoffs() == {
        "ok": True,
        "records": [],
        "issues": [],
    }
    assert module.pending_handoff_route()["status"] == "none"


def test_completed_record_replace_with_failed_parent_fsync_stays_blocked_until_reproved(
    handoff_env, monkeypatch
):
    import evolution_infra

    module, _results = handoff_env
    record_path, _record, _archive = _write_pair(module)
    _steps, claim_id = _complete_all_steps(module, finalize=False)
    original_fsync = evolution_infra._fsync_directory
    fail_handoff_parent = {"enabled": True}

    def injected_fsync(path):
        if fail_handoff_parent["enabled"] and Path(path) == record_path.parent:
            raise OSError("injected handoff parent fsync failure")
        return original_fsync(path)

    monkeypatch.setattr(evolution_infra, "_fsync_directory", injected_fsync)
    with pytest.raises(OSError, match="injected handoff parent fsync failure"):
        module.complete_post_publication_handoff(143, 142, claim_id)
    module.release_post_publication_handoff_claim(
        143, 142, claim_id, error="final record fsync failed"
    )

    # os.replace happened, but neither the failed call nor a status reader may
    # infer durability while the exact record/parent re-proof still fails.
    assert module._read_json(record_path)["state"] == "completed"
    blocked = module.pending_handoff_route()
    assert blocked["status"] == "blocked"
    assert module._active_pointer_path().exists()

    fail_handoff_parent["enabled"] = False
    completed, replay_claim = module.claim_post_publication_handoff(143, 142)
    assert completed["state"] == "completed"
    assert replay_claim == ""
    assert not module._active_pointer_path().exists()
    assert module.pending_handoff_route()["status"] == "none"


def test_stable_sidecar_rejects_inode_swap_and_releases_lock(handoff_env):
    module, _results = handoff_env
    target = module._handoff_path(143, "f" * 64)
    lock = module._JournalLock(target)

    with pytest.raises(OSError, match="sidecar lock"):
        with lock:
            lock.path.unlink()
            lock.path.write_text("replacement inode", encoding="utf-8")

    # The failed exit must have unlocked/closed the old descriptor and released
    # the same-process RLock.  Replacing the attacker inode permits immediate
    # acquisition instead of deadlocking this process.
    lock.path.unlink()
    with module._JournalLock(target):
        pass


def test_schema_owner_and_step_prefix_are_exact(handoff_env):
    module, _results = handoff_env
    _path, baseline, _archive = _write_pair(module)
    assert module.validate_handoff_record(baseline, reopen_archive=False) == []

    bad_type = copy.deepcopy(baseline)
    bad_type["identity"]["version"] = "143"
    bad_type["identity_digest"] = module.canonical_digest(bad_type["identity"])
    bad_type["record_digest"] = module._record_digest(bad_type)
    assert "handoff_subject_identity_invalid" in module.validate_handoff_record(
        bad_type, reopen_archive=False
    )

    bad_owner = copy.deepcopy(baseline)
    bad_owner["state"] = "running"
    bad_owner["owner"] = {"claim_id": "b" * 32}
    bad_owner["record_digest"] = module._record_digest(bad_owner)
    assert "handoff_owner_shape_invalid" in module.validate_handoff_record(
        bad_owner, reopen_archive=False
    )

    bad_prefix = copy.deepcopy(baseline)
    second = module.REQUIRED_STEPS[1]
    payload = {
        "schema_version": 1,
        "step": second,
        "publication_id": baseline["identity"]["publication_id"],
        "completed_at": 1.0,
        "plan_digest": None,
        "output": {},
    }
    bad_prefix["steps"][second] = {
        "status": "completed",
        "receipt": module._receipt(payload),
    }
    bad_prefix["record_digest"] = module._record_digest(bad_prefix)
    prefix_errors = module.validate_handoff_record(
        bad_prefix, reopen_archive=False
    )
    assert "handoff_step_completed_prefix_invalid" in prefix_errors

    bad_plan = copy.deepcopy(baseline)
    first = module.REQUIRED_STEPS[0]
    bad_plan["steps"][first] = {
        "status": "planned",
        "plan": {"target": 1},
        "plan_digest": "0" * 64,
    }
    bad_plan["record_digest"] = module._record_digest(bad_plan)
    assert (
        f"handoff_step_plan_digest_mismatch:{first}"
        in module.validate_handoff_record(bad_plan, reopen_archive=False)
    )


def test_planned_effect_receipt_must_echo_the_exact_plan_digest(handoff_env):
    module, _results = handoff_env
    _record_path, _record, archive = _write_pair(module)
    record, claim_id = module.claim_post_publication_handoff(143, 142)
    step = module.REQUIRED_STEPS[0]
    identity = record["identity"]
    record = module.plan_handoff_step(
        143,
        142,
        claim_id,
        step,
        {
            "schema_version": 1,
            "kind": "stability-observation-plan",
            "publication_id": identity["publication_id"],
            "publishing_checkpoint_digest": identity[
                "publishing_checkpoint_digest"
            ],
            "strength_evidence_identity_digest": module.canonical_digest(
                archive["strength_evidence_identity"]
            ),
        },
    )
    valid_output = {
        "plan_digest": record["steps"][step]["plan_digest"],
        "publication_id": identity["publication_id"],
        "continuity_id": "b" * 32,
        "count": 1,
        "target": 10,
        "complete": False,
    }
    with pytest.raises(
        module.PostPublicationHandoffError,
        match="output_plan_binding_mismatch",
    ):
        module.complete_handoff_step(
            143,
            142,
            claim_id,
            step,
            {**valid_output, "plan_digest": "0" * 64},
        )
    module.complete_handoff_step(
        143, 142, claim_id, step, valid_output
    )
    module.release_post_publication_handoff_claim(143, 142, claim_id)


def test_recomputed_forged_step_receipt_cannot_skip_required_effect(handoff_env):
    module, _results = handoff_env
    _write_pair(module)
    record, claim_id = _complete_all_steps(module, finalize=False)
    step = "stability_observation"
    row = record["steps"][step]
    forged_output = {
        "plan_digest": row["plan_digest"],
        "forged_skip": True,
    }
    forged_receipt = dict(row["receipt"])
    forged_receipt["output"] = forged_output
    unsigned = {
        key: value for key, value in forged_receipt.items()
        if key != "receipt_digest"
    }
    forged_receipt["receipt_digest"] = module.canonical_digest(unsigned)
    record["steps"][step]["receipt"] = forged_receipt
    record["record_digest"] = module._record_digest(record)

    issues = module.validate_handoff_record(record, reopen_archive=False)
    assert any(
        issue.startswith(
            "handoff_step_output_contract_invalid:stability_observation:"
        )
        for issue in issues
    )
    module.release_post_publication_handoff_claim(143, 142, claim_id)


def test_stability_plan_and_receipt_bind_frozen_evidence_and_ten_run_target(
    handoff_env
):
    module, _results = handoff_env
    _write_pair(module)
    record, claim_id = module.claim_post_publication_handoff(143, 142)
    identity = record["identity"]
    forged_plan = {
        "schema_version": 1,
        "kind": "stability-observation-plan",
        "publication_id": identity["publication_id"],
        "publishing_checkpoint_digest": identity[
            "publishing_checkpoint_digest"
        ],
        "strength_evidence_identity_digest": "c" * 64,
    }
    with pytest.raises(
        module.PostPublicationHandoffError,
        match="stability_evidence_digest_mismatch",
    ):
        module.plan_handoff_step(
            143, 142, claim_id, "stability_observation", forged_plan
        )
    module.release_post_publication_handoff_claim(143, 142, claim_id)


def test_operational_reproof_reopens_stability_and_reissues_exact_signals(
    handoff_env, monkeypatch
):
    import stability_observation

    module, results = handoff_env
    _write_pair(module)
    record, claim_id = _complete_all_steps(module, finalize=False)
    stability_output = record["steps"]["stability_observation"]["receipt"][
        "output"
    ]
    monkeypatch.setattr(
        stability_observation,
        "stability_observation_projection",
        lambda: {
            "continuity_id": stability_output["continuity_id"],
            "count": stability_output["count"],
            "target": stability_output["target"],
            "complete": stability_output["complete"],
            "observations": [{
                "publication_id": record["identity"]["publication_id"]
            }],
        },
    )

    result = module._test_original_reprove_operational_steps(record)

    assert result == {
        "stability_observation": True,
        "reap_signal": True,
        "priority_eval": True,
    }
    signal_plan = record["steps"]["reap_signal"]["plan"]
    assert (results / ".reap_signal").read_text(
        encoding="utf-8"
    ) == signal_plan["signal_text"]
    priority_plan = record["steps"]["priority_eval"]["plan"]
    expected_priority = json.dumps(
        priority_plan["payload"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    assert (results / "priority_eval.json").read_text(
        encoding="utf-8"
    ) == expected_priority

    monkeypatch.setattr(
        stability_observation,
        "stability_observation_projection",
        lambda: {
            "continuity_id": stability_output["continuity_id"],
            "count": 0,
            "target": 10,
            "complete": False,
            "observations": [],
        },
    )
    with pytest.raises(
        module.PostPublicationHandoffError,
        match="stability_observation_reproof_mismatch",
    ):
        module._test_original_reprove_operational_steps(record)
    module.release_post_publication_handoff_claim(143, 142, claim_id)


def test_external_reproof_reopens_housekeeping_instead_of_trusting_booleans(
    handoff_env, monkeypatch
):
    import tool_commit

    module, _results = handoff_env
    _write_pair(module)
    record, claim_id = _complete_all_steps(module, finalize=False)
    expected_worktree = {
        "head_oid": record["identity"]["commit_oid"],
        "worktree_status_digest": hashlib.sha256(b"").hexdigest(),
        "tracked_housekeeping_commit": False,
    }
    monkeypatch.setattr(
        tool_commit,
        "_verify_post_publication_worktree",
        lambda **_kwargs: dict(expected_worktree),
    )

    module._test_original_reprove_external_steps(record)

    forged = copy.deepcopy(record)
    forged["steps"]["housekeeping"]["receipt"]["output"][
        "worktree_status_digest"
    ] = "d" * 64
    with pytest.raises(
        module.PostPublicationHandoffError,
        match="housekeeping_worktree_reproof_mismatch",
    ):
        module._test_original_reprove_external_steps(forged)
    module.release_post_publication_handoff_claim(143, 142, claim_id)
