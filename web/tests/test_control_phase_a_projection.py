"""Phase A control-status projection: multi-slot + slice2b + cert/eval blocks."""

from __future__ import annotations

import types

import pytest

from conftest import STRICT_TARGET_V, bot_name


def _minimal_epoch(**overrides):
    epoch = {
        "current_v": STRICT_TARGET_V,
        "next_v": STRICT_TARGET_V + 1,
        "strict_generation_count": 1,
        "active_generation": None,
        "active_generations": [],
        "evaluation_epoch": "national_tcp_policy_v1",
        "state": "strict_published",
        "initialized": True,
        "version_authority_high_water": STRICT_TARGET_V,
        "strict_published_versions": [STRICT_TARGET_V],
        "strict_published_bot_identities": [],
        "active_bots": [bot_name(STRICT_TARGET_V)],
        "reset_receipt_valid": True,
        "reset_receipt_digest": "a" * 64,
        "reset_receipt_issues": [],
        "operator_action": None,
        "operator_command": None,
        "ignored_checkpoint": None,
        "runtime_reconciliation_claimed": False,
        "runtime_reconciliation_kind": None,
        "runtime_reconciliation_claim_digest": None,
        "runtime_reconciliation_claim_valid": False,
        "runtime_reconciliation_claim_issues": [],
        "publication_recovery_ready": False,
        "unpaired_completion_versions": [],
        "unpaired_high_water_versions": [],
        "operator_transition": None,
    }
    epoch.update(overrides)
    return epoch


def _patch_stable_epoch(monkeypatch, epoch):
    import epoch_authority
    import post_publication_handoff
    import stability_observation
    import server.routes.control as control

    monkeypatch.setattr(
        epoch_authority,
        "strict_epoch_projection",
        lambda **_kwargs: dict(epoch),
    )
    monkeypatch.setattr(
        epoch_authority,
        "unpublished_candidate_versions",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        epoch_authority,
        "epoch_stream_authority_digest",
        lambda _epoch: "b" * 64,
    )
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {"status": "none", "records": [], "issues": []},
    )
    monkeypatch.setattr(
        stability_observation,
        "stability_observation_cached_projection",
        lambda **_kwargs: {
            "continuity_valid": True,
            "count": 0,
            "target": 10,
            "remaining": 10,
            "complete": False,
            "verification": {
                "state": "fresh",
                "checked_at": 1.0,
                "fresh_until": 9_999_999_999.0,
                "error": None,
                "authority": {
                    "evaluation_epoch": "national_tcp_policy_v1",
                    "epoch_stream_authority_digest": "b" * 64,
                    "repository_head": "c" * 40,
                    "repository_branch": "tencent-cloud-runtime",
                },
            },
        },
    )
    return control


def test_sync_copies_active_generations_and_phase_a_blocks(monkeypatch):
    primary = {
        "slot_id": "primary",
        "next_v": STRICT_TARGET_V + 1,
        "source_v": STRICT_TARGET_V,
        "parent2_v": None,
        "stage": "workers_done",
        "run_id": "run-1",
        "workflow_run_id": "wf-1",
        "checkpoint_revision": 3,
        "attempt": {"generation": 1, "audit": 1, "precommit": 1},
        "generation_ordinal": 1,
        "canonical_version": STRICT_TARGET_V + 1,
        "canonical_bot_name": bot_name(STRICT_TARGET_V + 1),
        "canonical_tag": f"national-cloud-bot-v{STRICT_TARGET_V + 1}",
    }
    draft = {
        "slot_id": "draft",
        "next_v": STRICT_TARGET_V + 2,
        "source_v": STRICT_TARGET_V,
        "parent2_v": None,
        "stage": "direction_audited",
        "workflow_run_id": "wf-draft",
        "checkpoint_revision": 1,
        "is_draft": True,
    }
    epoch = _minimal_epoch(
        active_generation=dict(primary),
        active_generations=[primary, draft],
    )
    control = _patch_stable_epoch(monkeypatch, epoch)
    monkeypatch.setattr(
        control,
        "_version_authority_projection",
        lambda _epoch: {
            "high_water": STRICT_TARGET_V,
            "paired_versions": [STRICT_TARGET_V],
            "certified_versions": [],
            "unpaired_completion_versions": [],
            "unpaired_high_water_versions": [],
        },
    )
    monkeypatch.setattr(
        control,
        "_pipeline_mode_projection",
        lambda: {
            "enabled": True,
            "consumer_parked": True,
            "producer_may_prepare_next": True,
            "producer_may_advance": False,
            "in_flight_count": 1,
            "sealed_candidates": ["cand-1"],
        },
    )
    monkeypatch.setattr(
        control,
        "_feature_flags_projection",
        lambda: {
            "slice2b_enabled": True,
            "staging_as_parent": True,
            "certified_tag_prefix": "national-cloud-certified-v",
            "tag_prefix": "national-cloud-bot-v",
        },
    )
    monkeypatch.setattr(
        control,
        "_daemon_health_snapshot",
        lambda: {"alive": True, "configured": True},
    )
    monkeypatch.setattr(
        control,
        "_eval_wait_projection",
        lambda **_kwargs: {
            "waiting": False,
            "bot": None,
            "games": None,
            "min_games": 24,
            "rd": None,
            "rd_threshold": 110.0,
            "rd_min_games": 12,
            "daemon_alive": True,
            "consecutive_prep_fails": None,
            "degraded": False,
        },
    )

    snapshot = control._sync_evolution_fields({})

    assert snapshot["active_generations"] == [primary, draft]
    assert snapshot["pipeline_mode"]["enabled"] is True
    assert snapshot["pipeline_mode"]["sealed_candidates"] == ["cand-1"]
    assert snapshot["feature_flags"]["slice2b_enabled"] is True
    assert snapshot["version_authority"]["certified_versions"] == []
    assert snapshot["async_certification"]["any_pending"] is True
    assert snapshot["async_certification"]["items"][0]["state"] == "pending"
    assert snapshot["eval_wait"]["waiting"] is False


def test_sync_error_path_clears_active_generations(monkeypatch):
    import epoch_authority
    import post_publication_handoff
    import stability_observation
    import server.routes.control as control

    monkeypatch.setattr(
        epoch_authority,
        "strict_epoch_projection",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {"status": "none", "records": [], "issues": []},
    )
    monkeypatch.setattr(
        stability_observation,
        "stability_observation_cached_projection",
        lambda **_kwargs: {},
    )

    snapshot = control._sync_evolution_fields({})

    assert snapshot["active_generations"] == []
    assert snapshot["pipeline_mode"]["enabled"] is False
    assert snapshot["async_certification"] == {"items": [], "any_pending": False}
    assert snapshot["version_authority"]["high_water"] == 0
    assert "feature_flags" in snapshot
    assert "eval_wait" in snapshot


def test_health_mirrors_active_generations(monkeypatch):
    import server.routes.control as control

    monkeypatch.setattr(
        control,
        "_daemon_health_snapshot",
        lambda: {
            "configured": False,
            "alive": False,
            "heartbeat_status": "not_applicable",
            "configured_workers": 1,
            "configured_pairs": 5,
            "env_workers": None,
            "env_pairs": None,
            "effective_workers": None,
            "effective_pairs": None,
            "pairs_drift": False,
        },
    )
    monkeypatch.setattr(
        control,
        "_read_pipeline_health",
        lambda _status: {"exists": False, "stage": None},
    )
    slots = [{"slot_id": "primary", "next_v": STRICT_TARGET_V + 1, "stage": "selected"}]
    health = control._health_summary({
        "running": False,
        "daemon_enabled": False,
        "epoch_initialized": True,
        "active_generation": slots[0],
        "active_generations": slots,
        "stability_observation": {
            "continuity_valid": True,
            "verification": {"state": "fresh", "fresh_until": 9_999_999_999.0},
        },
    })
    assert health["active_generations"] == slots
    assert health["daemon"]["pairs_drift"] is False


def test_pipeline_mode_disabled_by_default(monkeypatch):
    import server.routes.control as control

    monkeypatch.delenv("POK_SLICE2B_ENABLED", raising=False)
    projection = control._pipeline_mode_projection()
    assert projection == {
        "enabled": False,
        "consumer_parked": False,
        "producer_may_prepare_next": False,
        "producer_may_advance": False,
        "in_flight_count": 0,
        "sealed_candidates": [],
    }


def test_pipeline_mode_reads_activation_registry(monkeypatch):
    import producer_consumer_slice2b_activation as activation_mod
    import server.routes.control as control

    monkeypatch.setenv("POK_SLICE2B_ENABLED", "1")

    class _Coordinator:
        def in_flight(self):
            return {"cand-a": "deadbeef"}

    class _Activation:
        coordinator = _Coordinator()

        def producer_may_prepare_next(self):
            return True

        def producer_may_advance(self):
            return False

    monkeypatch.setattr(
        activation_mod,
        "activation_registry",
        lambda action, **_kwargs: _Activation() if action == "get" else None,
    )
    projection = control._pipeline_mode_projection()
    assert projection["enabled"] is True
    assert projection["consumer_parked"] is True
    assert projection["producer_may_prepare_next"] is True
    assert projection["producer_may_advance"] is False
    assert projection["in_flight_count"] == 1
    assert projection["sealed_candidates"] == ["cand-a"]


def test_eval_wait_waiting_when_under_threshold(monkeypatch, tmp_path):
    import evolution_infra
    import server.routes.control as control

    bot = bot_name(STRICT_TARGET_V)
    stats = tmp_path / "bot_stats.json"
    ratings = tmp_path / "glicko_ratings.json"
    stats.write_text('{"%s": {"games": 3}}' % bot)
    ratings.write_text('{"%s": {"r": 1500, "rd": 200, "vol": 0.06}}' % bot)
    monkeypatch.setattr(evolution_infra, "BOT_STATS_FILE", stats)
    monkeypatch.setattr(evolution_infra, "RATINGS_FILE", ratings)
    monkeypatch.setattr(
        evolution_infra,
        "read_locked_json",
        lambda path, default=None: __import__("json").loads(path.read_text())
        if path.exists()
        else (default if default is not None else {}),
    )

    projection = control._eval_wait_projection(
        active_generation=None,
        active_bots=[bot],
        daemon_alive=True,
    )
    assert projection["waiting"] is True
    assert projection["bot"] == bot
    assert projection["games"] == 3
    assert projection["rd"] == 200.0
    assert projection["consecutive_prep_fails"] is None


def test_eval_wait_not_waiting_with_active_generation():
    import server.routes.control as control

    projection = control._eval_wait_projection(
        active_generation={"next_v": STRICT_TARGET_V + 1, "stage": "selected"},
        active_bots=[bot_name(STRICT_TARGET_V)],
        daemon_alive=True,
    )
    assert projection["waiting"] is False
    assert projection["bot"] is None


def test_daemon_pairs_drift_from_env(monkeypatch):
    import server.routes.control as control
    from server.state import app_state

    app_state.override_runtime_config(daemon_pairs=5, daemon_workers=4)
    monkeypatch.setenv("POK_DAEMON_PAIRS", "3")
    monkeypatch.delenv("POK_DAEMON_WORKERS", raising=False)
    fields = control._daemon_config_drift_fields(
        {"alive": False, "pid": None}
    )
    assert fields["configured_pairs"] == 5
    assert fields["env_pairs"] == 3
    assert fields["pairs_drift"] is True
    assert fields["effective_pairs"] is None


def test_observer_authority_key_watches_draft_checkpoint(monkeypatch, tmp_path):
    import evolution_infra
    import server.routes.control as control

    results = tmp_path / "results"
    results.mkdir()
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", results / "pipeline_state.json")
    monkeypatch.setattr(
        evolution_infra,
        "ABANDONED_VERSIONS_FILE",
        results / "abandoned_versions.jsonl",
    )
    monkeypatch.setattr(evolution_infra, "REAPED_BOTS_FILE", results / "reaped_bots.jsonl")
    monkeypatch.setattr(
        evolution_infra,
        "POST_PUBLICATION_HANDOFF_DIR",
        results / "post_publication_handoffs",
    )
    monkeypatch.setattr(evolution_infra, "BOTS_DIR", tmp_path / "bots")
    (tmp_path / "bots").mkdir()
    monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", tmp_path)

    def fake_namespace():
        return types.SimpleNamespace(
            high_water=0,
            paired_versions=(),
            paired_commits=(),
            unpaired_completion_versions=(),
            unpaired_high_water_versions=(),
            certified_versions=(),
        )

    monkeypatch.setattr(evolution_infra, "version_namespace_authority", fake_namespace)

    before = control._observer_authority_content_key()
    draft = evolution_infra.pipeline_state_path("draft")
    draft.write_text('{"stage":"direction_audited","next_v":2,"is_draft":true}')
    after = control._observer_authority_content_key()
    assert before != after
