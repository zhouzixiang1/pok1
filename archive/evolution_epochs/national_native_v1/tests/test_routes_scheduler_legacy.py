"""Adversarial tests for strict scheduler REST/SSE projections."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import server.routes.data_stream as data_stream
import server.routes.scheduler as scheduler


EPOCH = "national_tcp_policy_v1"
IDENTITY = "d" * 64
POOL = ("national_v143", "national_v144")


def _append_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")


def _context():
    return ({
        "evaluation_epoch": EPOCH,
        "evaluation_identity_digest": IDENTITY,
        "published_pool": POOL,
    }, "ok")


def _job(job_id: str, **changes) -> dict:
    row = {
        "job_id": job_id,
        "bot_a_name": "national_v143",
        "bot_b_name": "national_v144",
        "bot_a_path": "/secret/retired/path/a.py",
        "bot_b_path": "/secret/retired/path/b.py",
        "n_pairs": 3,
        "evaluation_epoch": EPOCH,
        "evaluation_identity_digest": IDENTITY,
    }
    row.update(changes)
    return row


def _result(job_id: str, **changes) -> dict:
    row = {
        "job_id": job_id,
        "bot_a_name": "national_v143",
        "bot_b_name": "national_v144",
        "wins_a": 1,
        "wins_b": 0,
        "draws": 0,
        "total": 1,
        "evaluation_epoch": EPOCH,
        "evaluation_identity_digest": IDENTITY,
    }
    row.update(changes)
    return row


def test_context_requires_initialized_epoch_before_identity_or_pool(monkeypatch):
    import epoch_authority
    import evaluation_bundle
    from server.routes import _helpers

    monkeypatch.setattr(
        epoch_authority,
        "policy_epoch_initialization",
        lambda: {
            "initialized": False,
            "state": "reset_required",
            "evaluation_epoch": EPOCH,
        },
    )
    monkeypatch.setattr(
        evaluation_bundle,
        "validated_evaluation_identity_digest",
        lambda: (_ for _ in ()).throw(
            AssertionError("identity must not be consulted before epoch authority")
        ),
    )
    monkeypatch.setattr(
        _helpers,
        "_strict_published_active_pool",
        lambda: (_ for _ in ()).throw(
            AssertionError("pool must not be consulted before epoch authority")
        ),
    )

    context, reason = scheduler._strict_scheduler_context()

    assert context is None
    assert reason == "reset_required"


def test_context_requires_validated_identity_and_published_active_pool(monkeypatch):
    import epoch_authority
    import evaluation_bundle
    from server.routes import _helpers

    monkeypatch.setattr(
        epoch_authority,
        "policy_epoch_initialization",
        lambda: {
            "initialized": True,
            "state": "strict_published",
            "evaluation_epoch": EPOCH,
        },
    )
    monkeypatch.setattr(
        evaluation_bundle,
        "validated_evaluation_identity_digest",
        lambda: IDENTITY,
    )
    monkeypatch.setattr(
        _helpers, "_strict_published_active_pool", lambda: list(POOL)
    )

    context, reason = scheduler._strict_scheduler_context()

    assert reason == "ok"
    assert context == {
        "evaluation_epoch": EPOCH,
        "evaluation_identity_digest": IDENTITY,
        "published_pool": POOL,
    }


def test_context_fails_closed_when_validated_identity_is_absent(monkeypatch):
    import epoch_authority
    import evaluation_bundle

    monkeypatch.setattr(
        epoch_authority,
        "policy_epoch_initialization",
        lambda: {
            "initialized": True,
            "state": "fresh_bootstrap_ready",
            "evaluation_epoch": EPOCH,
        },
    )
    monkeypatch.setattr(
        evaluation_bundle,
        "validated_evaluation_identity_digest",
        lambda: None,
    )

    context, reason = scheduler._strict_scheduler_context()

    assert context is None
    assert reason == "evaluation_identity_unavailable"


def test_status_fails_empty_before_opening_retired_queue(client, monkeypatch):
    import battle_scheduler

    monkeypatch.setattr(
        scheduler,
        "_strict_scheduler_context",
        lambda: (None, "reset_required"),
    )

    def forbidden_read(_path):
        raise AssertionError("retired scheduler bytes must not be opened")

    monkeypatch.setattr(battle_scheduler, "_read_jsonl", forbidden_read)
    response = client.get("/api/scheduler/status")

    assert response.status_code == 200
    assert response.json() == {
        "available": False,
        "reason": "reset_required",
        "evaluation_epoch": None,
        "evaluation_identity_digest": None,
        "published_pool": [],
        "pending_jobs": 0,
        "claimed_jobs": 0,
        "recent_results": 0,
        "pending_details": [],
    }


def test_missing_evaluation_identity_fails_empty(client, monkeypatch):
    monkeypatch.setattr(
        scheduler,
        "_strict_scheduler_context",
        lambda: (None, "evaluation_identity_unavailable"),
    )

    response = client.get("/api/scheduler/results")

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["reason"] == "evaluation_identity_unavailable"
    assert response.json()["results"] == []


def test_status_counts_only_exact_current_identity_and_published_pool(
    client, tmp_path, monkeypatch
):
    import battle_scheduler

    jobs_file = tmp_path / "battle_jobs.jsonl"
    claimed_file = tmp_path / "battle_jobs.claimed"
    results_file = tmp_path / "battle_results.jsonl"
    _append_jsonl(jobs_file, [
        _job("current-pending"),
        _job("old-epoch", evaluation_epoch="national_native_v1"),
        _job("old-identity", evaluation_identity_digest="e" * 64),
        _job("unpublished", bot_b_name="national_v155"),
        {"job_id": "unbound-legacy"},
    ])
    _append_jsonl(claimed_file, [
        _job("current-claimed"),
        _job("claimed-v155", bot_a_name="national_v155"),
    ])
    _append_jsonl(results_file, [
        _result("current-result"),
        _result("result-old", evaluation_identity_digest="e" * 64),
    ])
    monkeypatch.setattr(scheduler, "_strict_scheduler_context", _context)

    with patch.object(battle_scheduler, "BATTLE_JOBS_FILE", jobs_file), patch.object(
        battle_scheduler, "BATTLE_CLAIMED_FILE", claimed_file
    ), patch.object(battle_scheduler, "BATTLE_RESULTS_FILE", results_file):
        response = client.get("/api/scheduler/status")

    payload = response.json()
    assert payload["available"] is True
    assert payload["pending_jobs"] == 1
    assert payload["claimed_jobs"] == 1
    assert payload["recent_results"] == 1
    assert [row["job_id"] for row in payload["pending_details"]] == [
        "current-pending"
    ]
    assert "bot_a_path" not in payload["pending_details"][0]
    assert "bot_b_path" not in payload["pending_details"][0]


def test_result_limit_applies_after_strict_filtering(client, tmp_path, monkeypatch):
    import battle_scheduler

    results_file = tmp_path / "battle_results.jsonl"
    rows = []
    for index in range(10):
        rows.append(_result(f"r{index}"))
        rows.append(_result(f"old{index}", evaluation_epoch="national_native_v1"))
    _append_jsonl(results_file, rows)
    monkeypatch.setattr(scheduler, "_strict_scheduler_context", _context)

    with patch.object(battle_scheduler, "BATTLE_RESULTS_FILE", results_file):
        response = client.get("/api/scheduler/results?limit=3")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert [row["job_id"] for row in payload["results"]] == ["r7", "r8", "r9"]


def test_result_without_self_contained_bot_identity_is_never_joined_by_job_id(
    client, tmp_path, monkeypatch
):
    """A reused legacy job_id cannot inherit authority from a current queue row."""

    import battle_scheduler

    jobs_file = tmp_path / "battle_jobs.jsonl"
    results_file = tmp_path / "battle_results.jsonl"
    _append_jsonl(jobs_file, [_job("same-id")])
    _append_jsonl(results_file, [{
        "job_id": "same-id",
        "wins_a": 999,
        "wins_b": 0,
        "draws": 0,
        "total": 999,
    }])
    monkeypatch.setattr(scheduler, "_strict_scheduler_context", _context)

    with patch.object(battle_scheduler, "BATTLE_JOBS_FILE", jobs_file), patch.object(
        battle_scheduler, "BATTLE_RESULTS_FILE", results_file
    ):
        response = client.get("/api/scheduler/results")

    assert response.json()["results"] == []


def test_authority_change_during_queue_read_fails_empty(client, tmp_path, monkeypatch):
    import battle_scheduler

    jobs_file = tmp_path / "battle_jobs.jsonl"
    _append_jsonl(jobs_file, [_job("would-have-been-current")])
    first, _ = _context()
    changed = {
        **first,
        "evaluation_identity_digest": "f" * 64,
    }
    contexts = iter(((first, "ok"), (changed, "ok")))
    monkeypatch.setattr(
        scheduler, "_strict_scheduler_context", lambda: next(contexts)
    )

    with patch.object(battle_scheduler, "BATTLE_JOBS_FILE", jobs_file):
        response = client.get("/api/scheduler/status")

    payload = response.json()
    assert payload["available"] is False
    assert payload["reason"] == "scheduler_authority_changed_during_read"
    assert payload["pending_jobs"] == 0
    assert payload["pending_details"] == []


def test_sse_scheduler_helper_uses_the_same_projection(monkeypatch):
    expected = {
        "available": True,
        "reason": None,
        "pending_jobs": 2,
        "claimed_jobs": 1,
        "recent_results": 4,
        "pending_details": [],
        "results": ["must-not-leak-through-status"],
    }
    monkeypatch.setattr(
        scheduler,
        "strict_scheduler_projection",
        lambda *, result_limit=None: dict(expected),
    )

    payload = data_stream._get_scheduler_status()

    assert payload["pending_jobs"] == 2
    assert "results" not in payload
