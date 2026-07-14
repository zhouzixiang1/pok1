"""Fail-closed HTTP observability for the strict national TCP epoch."""

from __future__ import annotations

import hashlib
import json

import checkpoint_schema
import evaluation_bundle
from server.routes import _helpers
from system_strict_bootstrap import build_fresh_bootstrap_receipt


IDENTITY = "d" * 64


def _canonical_digest(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _reset_receipt() -> dict:
    archive_root = (
        "archive/evolution_epochs/national_native_v1/"
        "runtime_legacy_untrusted/20260714_000000_000000"
    )
    claim_payload = {
        "schema_version": 1,
        "kind": "national_tcp_policy_epoch_reset_claim",
        "epoch": "national_tcp_policy_v1",
        "created_at": "2026-07-14T00:00:00.000000",
        "git_head": "a" * 40,
        "archive_root": archive_root,
        "first_target_version": 143,
        "checkout_role": "autonomous_evolution_runtime",
        "one_time": True,
    }
    claim_digest = _canonical_digest(claim_payload)
    payload = {
        "schema_version": 2,
        "kind": "national_tcp_policy_epoch_reset",
        "epoch": "national_tcp_policy_v1",
        "created_at": "2026-07-14T00:00:00",
        "mode": "execute",
        "git_head": "a" * 40,
        "archive_root": archive_root,
        "execution_scope": {
            "checkout_role": "autonomous_evolution_runtime",
            "one_time": True,
            "prior_reset_evidence_required_empty": True,
            "claim_digest": claim_digest,
        },
        "archived_version_high_water": 142,
        "version_authority_high_water": 142,
        "first_target_version": 143,
        "source_code_inherited": False,
        "seed_bot": None,
        "active_namespace": {
            "bot": "national_v143",
            "protocol": "official-national-raw-tcp-v1",
            "policy_abi": "national-tcp-policy-runtime-v1",
        },
        "archived_runtime": [],
        "archived_bot_debris": [],
    }
    return {**payload, "receipt_digest": _canonical_digest(payload)}


def _write_reset_authority(results_dir, receipt):
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "policy_epoch_reset_receipt.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )
    claim_payload = {
        "schema_version": 1,
        "kind": "national_tcp_policy_epoch_reset_claim",
        "epoch": receipt["epoch"],
        "created_at": "2026-07-14T00:00:00.000000",
        "git_head": receipt["git_head"],
        "archive_root": receipt["archive_root"],
        "first_target_version": receipt["first_target_version"],
        "checkout_role": "autonomous_evolution_runtime",
        "one_time": True,
    }
    claim = {
        **claim_payload,
        "claim_digest": _canonical_digest(claim_payload),
    }
    archive = results_dir / receipt["archive_root"]
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "reset_claim.json").write_text(
        json.dumps(claim), encoding="utf-8"
    )
    (archive / "reset_receipt.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )


def _fresh_checkpoint(receipt: dict, *, workflow: str = "generation:143:test") -> dict:
    bootstrap = build_fresh_bootstrap_receipt(
        active_bots=(),
        epoch_reset_receipt_digest=receipt["receipt_digest"],
    )
    audit_context = {
        "protocol_bootstrap": bootstrap,
        "selection": {"strategy": "fresh_policy_bootstrap"},
    }
    binding = checkpoint_schema.build_checkpoint_epoch_binding(
        next_v=143,
        source_v=142,
        audit_context=audit_context,
    )
    return {
        "checkpoint_schema_version": checkpoint_schema.CHECKPOINT_SCHEMA_VERSION,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": binding,
        "next_v": 143,
        "source_v": 142,
        "parent2_v": None,
        "stage": "direction_audited",
        "workflow_run_id": workflow,
        "checkpoint_revision": 1,
        "audit_context": audit_context,
    }


def _match(match_id: str, *, bot0: str = "national_v143", epoch: str = "national_tcp_policy_v1") -> dict:
    return {
        "id": match_id,
        "execution_mode": "native_tcp",
        "evaluation_epoch": epoch,
        "evaluation_identity_digest": IDENTITY,
        "bot0": bot0,
        "bot1": "national_v144",
        "strength_sample_unit": "70_hand_match",
        "hands_per_strength_sample": 70,
        "strength_admitted": True,
        "strength_complete": True,
        "strength_compliance_passed": True,
        "strength_sample_count": 1,
        "net_chips_bot0": [100],
    }


def _bundle(*, active=None) -> dict:
    active = active or ["national_v143", "national_v144"]
    old_history = {
        "period": 1,
        "evaluation_identity_digest": "e" * 64,
        "ratings": {"national_v142": {"r": 9999, "rd": 1}},
    }
    current_history = {
        "period": 2,
        "evaluation_identity_digest": IDENTITY,
        "ratings": {
            "national_v143": {"r": 1500, "rd": 80},
            "national_v155": {"r": 9999, "rd": 1},
        },
        "win_rates": {
            "national_v143": {"games": 1},
            "national_v155": {"games": 999},
        },
    }
    matches = [
        _match("current.json"),
        _match("old-epoch.json", epoch="national_native_v1"),
        _match("unpublished.json", bot0="national_v155"),
    ]
    return {
        "available": True,
        "manifest": {
            "active_bots": active,
            "evaluation_identity_digest": IDENTITY,
        },
        "manifest_digest": "f" * 64,
        "ratings": {name: {"r": 1500, "rd": 80} for name in active},
        "h2h": {
            "national_v143 vs national_v144": {
                "games": 1,
                "a_wins": 1,
                "b_wins": 0,
                "draws": 0,
            }
        },
        "bot_stats": {name: {"games": 1} for name in active},
        "daemon_stats": {"total_games": 1},
        "selection": {
            "rows": [
                {"name": name, "rank": index + 1, "selection_score": 0.5}
                for index, name in enumerate(active)
            ]
        },
        "raw_append_logs": {
            "rating_history": (
                json.dumps(old_history) + "\n" + json.dumps(current_history) + "\n"
            ).encode(),
            "match_history": b"".join(
                (json.dumps(row) + "\n").encode() for row in matches
            ),
        },
    }


def _current_bundle(*, active=None) -> dict:
    active = active or ["national_v143", "national_v144"]
    return {
        **_bundle(active=active),
        "active_bots": active,
        "epoch_reset_receipt": _reset_receipt(),
    }


def test_strength_snapshot_stops_before_old_files_when_reset_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        evaluation_bundle,
        "load_current_strict_evaluation_bundle",
        lambda _root: {
            "available": False,
            "reason": "policy_epoch_reset_unavailable",
        },
    )

    snapshot = _helpers.load_strict_strength_snapshot(tmp_path)

    assert snapshot == {
        "available": False,
        "reason": "policy_epoch_reset_unavailable",
    }


def test_strength_snapshot_fails_closed_on_malformed_core_result(monkeypatch, tmp_path):
    monkeypatch.setattr(
        evaluation_bundle,
        "load_current_strict_evaluation_bundle",
        lambda _root: None,
    )

    assert _helpers.load_strict_strength_snapshot(tmp_path) == {
        "available": False,
        "reason": "evaluation_bundle_unavailable",
    }


def test_strength_snapshot_stops_when_published_pool_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(
        evaluation_bundle,
        "load_current_strict_evaluation_bundle",
        lambda _root: {
            "available": False,
            "reason": "strict_published_active_pool_empty",
            "active_bots": [],
        },
    )

    snapshot = _helpers.load_strict_strength_snapshot(tmp_path)

    assert snapshot["available"] is False
    assert snapshot["reason"] == "strict_published_active_pool_empty"


def test_strength_snapshot_rejects_singleton_default_rating(monkeypatch, tmp_path):
    singleton = _current_bundle(active=["national_v143"])
    monkeypatch.setattr(
        evaluation_bundle,
        "load_current_strict_evaluation_bundle",
        lambda _root: singleton,
    )

    snapshot = _helpers.load_strict_strength_snapshot(tmp_path)

    assert snapshot == {
        "available": False,
        "reason": "active_pool_singleton",
        "active_bots": ["national_v143"],
    }


def test_strength_snapshot_rejects_two_bot_zero_sample_cycle(monkeypatch, tmp_path):
    empty = _current_bundle()
    empty["raw_append_logs"]["match_history"] = b""
    monkeypatch.setattr(
        evaluation_bundle,
        "load_current_strict_evaluation_bundle",
        lambda _root: empty,
    )

    snapshot = _helpers.load_strict_strength_snapshot(tmp_path)

    assert snapshot == {
        "available": False,
        "reason": "awaiting_first_complete_cycle",
        "active_bots": ["national_v143", "national_v144"],
    }


def test_strength_snapshot_exposes_only_current_identity_and_active_pool(monkeypatch, tmp_path):
    monkeypatch.setattr(
        evaluation_bundle,
        "load_current_strict_evaluation_bundle",
        lambda _root: _current_bundle(),
    )

    snapshot = _helpers.load_strict_strength_snapshot(tmp_path)

    assert snapshot["available"] is True
    assert [row["period"] for row in snapshot["rating_history"]] == [2]
    assert set(snapshot["rating_history"][0]["ratings"]) == {"national_v143"}
    assert set(snapshot["rating_history"][0]["win_rates"]) == {"national_v143"}
    assert [row["id"] for row in snapshot["match_history"]] == ["current.json"]


def test_strength_snapshot_rejects_cycle_from_another_active_pool(monkeypatch, tmp_path):
    monkeypatch.setattr(
        evaluation_bundle,
        "load_current_strict_evaluation_bundle",
        lambda _root: {
            "available": False,
            "reason": "evaluation_active_pool_mismatch",
        },
    )

    snapshot = _helpers.load_strict_strength_snapshot(tmp_path)

    assert snapshot["available"] is False
    assert snapshot["reason"] == "evaluation_active_pool_mismatch"


def test_core_bundle_authority_stops_before_rating_data_without_reset(
    monkeypatch, tmp_path
):
    import evolution_infra
    import system_strict_bootstrap

    monkeypatch.setattr(
        system_strict_bootstrap,
        "load_policy_epoch_reset_receipt",
        lambda _root: (None, ["missing"]),
    )
    monkeypatch.setattr(
        evolution_infra,
        "get_published_active_bots_read_only",
        lambda: (_ for _ in ()).throw(AssertionError("active pool reopened")),
    )
    monkeypatch.setattr(
        evaluation_bundle,
        "load_published_evaluation_bundle",
        lambda _root: (_ for _ in ()).throw(AssertionError("rating data reopened")),
    )

    observed = evaluation_bundle.load_current_strict_evaluation_bundle(tmp_path)

    assert observed["available"] is False
    assert observed["reason"] == "policy_epoch_reset_unavailable"


def test_core_bundle_authority_rejects_cycle_active_pool_mismatch(
    monkeypatch, tmp_path
):
    import evolution_infra
    import system_strict_bootstrap

    monkeypatch.setattr(
        system_strict_bootstrap,
        "load_policy_epoch_reset_receipt",
        lambda _root: (_reset_receipt(), []),
    )
    monkeypatch.setattr(
        evolution_infra,
        "get_published_active_bots_read_only",
        lambda: ["national_v143", "national_v144"],
    )
    monkeypatch.setattr(
        evaluation_bundle,
        "load_published_evaluation_bundle",
        lambda _root: _bundle(active=["national_v143", "national_v155"]),
    )

    observed = evaluation_bundle.load_current_strict_evaluation_bundle(tmp_path)

    assert observed["available"] is False
    assert observed["reason"] == "evaluation_active_pool_mismatch"


def test_core_bundle_authority_fails_closed_when_cycle_loader_breaks(
    monkeypatch, tmp_path
):
    import evolution_infra
    import system_strict_bootstrap

    monkeypatch.setattr(
        system_strict_bootstrap,
        "load_policy_epoch_reset_receipt",
        lambda _root: (_reset_receipt(), []),
    )
    monkeypatch.setattr(
        evolution_infra,
        "get_published_active_bots_read_only",
        lambda: ["national_v143"],
    )
    monkeypatch.setattr(
        evaluation_bundle,
        "load_published_evaluation_bundle",
        lambda _root: (_ for _ in ()).throw(OSError("cycle vanished")),
    )

    observed = evaluation_bundle.load_current_strict_evaluation_bundle(tmp_path)

    assert observed == {
        "available": False,
        "reason": "evaluation_bundle_unavailable",
    }


def test_strict_checkpoint_and_failure_reader_require_exact_workflow_identity(
    monkeypatch, tmp_path
):
    receipt = _reset_receipt()
    _write_reset_authority(tmp_path, receipt)
    checkpoint = _fresh_checkpoint(receipt)
    checkpoint_path = tmp_path / "pipeline_state.json"
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    monkeypatch.setattr(_helpers, "_strict_published_active_pool", lambda: [])

    failures_path = tmp_path / "worker_failures.jsonl"
    rows = [
        {
            "gen": 143,
            "worker_id": 1,
            "role": "worker",
            "error": "current",
            "category": "worker",
            "evaluation_epoch": "national_tcp_policy_v1",
            "workflow_run_id": checkpoint["workflow_run_id"],
        },
        {
            "gen": 143,
            "worker_id": 2,
            "role": "worker",
            "error": "old workflow",
            "category": "worker",
            "evaluation_epoch": "national_tcp_policy_v1",
            "workflow_run_id": "generation:143:old",
        },
        {
            "gen": 143,
            "worker_id": 3,
            "role": "worker",
            "error": "unbound legacy row",
            "category": "worker",
        },
        {
            "gen": 143,
            "worker_id": 4,
            "role": "worker",
            "error": "old epoch",
            "category": "worker",
            "evaluation_epoch": "national_native_v1",
            "workflow_run_id": checkpoint["workflow_run_id"],
        },
    ]
    failures_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    observed = _helpers.load_strict_pipeline_checkpoint(tmp_path, checkpoint_path)
    failures = _helpers.read_strict_worker_failures(
        failures_path,
        results_dir=tmp_path,
        checkpoint_path=checkpoint_path,
    )

    assert observed is not None
    assert observed["next_v"] == 143
    assert [row["error"] for row in failures] == ["current"]


def test_abandoned_checkpoint_and_its_failures_are_hidden(monkeypatch, tmp_path):
    receipt = _reset_receipt()
    _write_reset_authority(tmp_path, receipt)
    checkpoint = {**_fresh_checkpoint(receipt), "stage": "abandoned"}
    checkpoint_path = tmp_path / "pipeline_state.json"
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    monkeypatch.setattr(_helpers, "_strict_published_active_pool", lambda: [])

    assert _helpers.load_strict_pipeline_checkpoint(tmp_path, checkpoint_path) is None
    assert _helpers.read_strict_worker_failures(
        tmp_path / "worker_failures.jsonl",
        results_dir=tmp_path,
        checkpoint_path=checkpoint_path,
    ) == []


def test_data_stream_has_no_mutable_alias_fallback_without_strict_snapshot(
    monkeypatch,
):
    import epoch_authority
    from server.routes import data_stream

    monkeypatch.setattr(data_stream, "_strict_snapshot", lambda: {})
    monkeypatch.setattr(
        epoch_authority,
        "strict_epoch_projection",
        lambda **_kwargs: {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "reset_required",
            "initialized": False,
        },
    )

    assert data_stream._get_ratings() == []
    assert data_stream._get_h2h() == {}
    assert data_stream._get_bot_stats() == {}
    assert data_stream._get_history() == []
    assert data_stream._get_recent_matches() == []
    assert data_stream._get_bots() == {"active": []}
    assert data_stream._get_match_matrix()["evidence_available"] is False
    daemon = data_stream._get_daemon_status()
    assert daemon["status"] == "blocked"
    assert daemon["daemon_enabled"] is False


def test_new_worker_failure_is_identity_bound_at_write_time(monkeypatch, tmp_path):
    import agent_workers
    import evolution_infra
    import system_strict_bootstrap

    receipt = _reset_receipt()
    checkpoint = _fresh_checkpoint(receipt)
    failures = tmp_path / "worker_failures.jsonl"
    monkeypatch.setattr(agent_workers, "WORKER_FAILURES_FILE", failures)
    monkeypatch.setattr(
        evolution_infra,
        "read_pipeline_checkpoint",
        lambda: checkpoint,
    )
    monkeypatch.setattr(
        system_strict_bootstrap,
        "load_policy_epoch_reset_receipt",
        lambda: (receipt, []),
    )

    agent_workers._record_worker_failure(
        143,
        1,
        "policy worker",
        "test failure",
        failure_type="compile_error",
    )

    row = json.loads(failures.read_text(encoding="utf-8"))
    assert row["evaluation_epoch"] == "national_tcp_policy_v1"
    assert row["workflow_run_id"] == checkpoint["workflow_run_id"]


def test_web_ui_uses_cycle_selection_instead_of_live_callback_payload(monkeypatch):
    from web_ui import EventBroadcaster, WebUI

    bundle = _current_bundle()
    monkeypatch.setattr(
        evaluation_bundle,
        "load_current_strict_evaluation_bundle",
        lambda: bundle,
    )
    ui = WebUI(EventBroadcaster())

    ui.update_eval_table(
        {"national_v155": {"r": 9999, "rd": 1}},
        ["national_v155"],
    )

    state = ui.get_state()
    assert [row["name"] for row in state["ratings"]] == [
        "national_v143",
        "national_v144",
    ]
    assert state["active_bots"] == ["national_v143", "national_v144"]
