"""Tests for /api/pipeline/strength-jobs — 70-hand background job projection."""

import asyncio
from contextlib import contextmanager
import json
import time

from bot_namespace import bot_name


def _write_preflight_cycle(root, *, match_history: bytes = b"", extra_file_bytes: int = 0):
    """Create only the filesystem shape consumed by the observer preflight."""

    from evaluation_bundle import APPEND_LOGS, BUNDLE_FILES

    cycle_name = "20260719-test-" + ("a" * 24)
    cycle = root / "evaluation_cycles" / cycle_name
    cycle.mkdir(parents=True)
    file_contracts = {}
    append_contracts = {}
    for index, (role, filename) in enumerate(BUNDLE_FILES.items()):
        payload = b"{}" + (b"x" * extra_file_bytes if index == 0 else b"")
        (cycle / filename).write_bytes(payload)
        file_contracts[role] = {"filename": filename, "bytes": len(payload)}
    for role, filename in APPEND_LOGS.items():
        payload = match_history if role == "match_history" else b""
        (cycle / filename).write_bytes(payload)
        append_contracts[role] = {
            "filename": filename,
            "committed_bytes": len(payload),
        }
    manifest = {
        "cycle_dir": f"evaluation_cycles/{cycle_name}",
        "files": file_contracts,
        "append_logs": append_contracts,
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode("utf-8")
    (root / "evaluation_cycle_manifest.json").write_bytes(manifest_bytes)
    total_bytes = len(manifest_bytes)
    total_bytes += sum(path.stat().st_size for path in cycle.iterdir())
    return total_bytes


class TestPipelineStrengthJobs:
    def test_no_bundle_returns_unavailable(self, client, monkeypatch):
        from server.routes import pipeline

        monkeypatch.setattr(
            pipeline,
            "load_strict_strength_snapshot",
            lambda *_args, **_kwargs: {"available": False, "reason": "active_pool_empty"},
        )
        resp = client.get("/api/pipeline/strength-jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is False
        assert data["reason"] == "active_pool_empty"
        assert data["evaluation_epoch"] == "national_tcp_policy_v1"
        assert "daemon" in data

    def test_projection_with_admitted_samples(self, client, monkeypatch):
        from server.routes import pipeline

        snapshot = {
            "available": True,
            "evaluation_identity_digest": "a" * 64,
            "evaluation_manifest_digest": "b" * 64,
            "epoch_reset_receipt_digest": "c" * 64,
            "active_bots": [bot_name(143), bot_name(144)],
            "match_history": [
                {
                    "id": "match_001",
                    "timestamp": "2026-07-19T00:00:00Z",
                    "bot0": bot_name(143),
                    "bot1": bot_name(144),
                    "bot0_wins": 40,
                    "bot1_wins": 30,
                    "draws": 0,
                    "strength_sample_count": 70,
                    "hands_per_strength_sample": 70,
                    "replay_sha256": "d" * 64,
                }
            ],
            "daemon_stats": {"pairs": {"national_v143 vs national_v144": 1}},
        }
        monkeypatch.setattr(
            pipeline,
            "load_strict_strength_snapshot",
            lambda *_args, **_kwargs: snapshot,
        )
        # Avoid touching the real bundle for inadmissible diagnostics in tests.
        monkeypatch.setattr(
            pipeline,
            "load_current_strict_evaluation_bundle",
            lambda *_args, **_kwargs: {
                "available": True,
                "raw_append_logs": {"match_history": b""},
            },
        )

        resp = client.get("/api/pipeline/strength-jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert data["evaluation_identity_digest"] == "a" * 64
        assert data["active_bots"] == [bot_name(143), bot_name(144)]
        assert data["capabilities"] == {
            "durable_job_lifecycle": False,
            "queued_running_leases": False,
            "producer_consumer_dispatch": False,
        }
        assert data["authority_binding"] == {
            "evaluation_epoch": "national_tcp_policy_v1",
            "active_bots": [bot_name(143), bot_name(144)],
            "epoch_reset_receipt_digest": "c" * 64,
            "evaluation_identity_digest": "a" * 64,
            "evaluation_manifest_digest": "b" * 64,
            "complete": True,
        }

        assert len(data["admitted_samples"]) == 1
        sample = data["admitted_samples"][0]
        assert sample["id"] == "match_001"
        assert sample["bot0_wins"] == 40
        assert sample["strength_sample_count"] == 70
        assert sample["replay_sha256"] == "d" * 64

        assert data["staged_pending"] == []
        assert data["inadmissible_diagnostics"] == []
        assert data["pagination"]["admitted_total"] == 1
        assert data["pagination"]["limit"] == 50
        assert data["observer"]["complete"] is True
        assert data["daemon_stats"]["pairs"]["national_v143 vs national_v144"] == 1

    def test_inadmissible_diagnostics_explain_rejection(self, client, monkeypatch):
        from server.routes import pipeline

        identity = "a" * 64
        snapshot = {
            "available": True,
            "evaluation_identity_digest": identity,
            "evaluation_manifest_digest": "b" * 64,
            "active_bots": [bot_name(143), bot_name(144)],
            "match_history": [
                {
                    "id": "good",
                    "execution_mode": "native_tcp",
                    "evaluation_epoch": "national_tcp_policy_v1",
                    "evaluation_identity_digest": identity,
                    "bot0": bot_name(143),
                    "bot1": bot_name(144),
                    "strength_sample_unit": "70_hand_match",
                    "hands_per_strength_sample": 70,
                    "strength_admitted": True,
                    "strength_complete": True,
                    "strength_compliance_passed": True,
                    "strength_sample_count": 70,
                    "net_chips_bot0": [1] * 70,
                }
            ],
            "daemon_stats": {},
        }
        # A 69-hand row that was rejected by admission.
        rejected_row = {
            "id": "bad69",
            "execution_mode": "native_tcp",
            "evaluation_epoch": "national_tcp_policy_v1",
            "evaluation_identity_digest": identity,
            "bot0": bot_name(143),
            "bot1": bot_name(144),
            "strength_sample_unit": "70_hand_match",
            "hands_per_strength_sample": 69,  # rejected: not 70
            "strength_admitted": True,
            "strength_complete": True,
            "strength_compliance_passed": True,
            "strength_sample_count": 69,
            "net_chips_bot0": [1] * 69,
        }
        raw_match_history = (
            json.dumps(snapshot["match_history"][0]) + "\n" + json.dumps(rejected_row) + "\n"
        ).encode("utf-8")
        bundle = {
            "available": True,
            "raw_append_logs": {"match_history": raw_match_history},
        }

        import server.routes.pipeline as pipeline_mod

        monkeypatch.setattr(
            pipeline_mod,
            "load_strict_strength_snapshot",
            lambda *_args, **_kwargs: snapshot,
        )
        # load_current_strict_evaluation_bundle is a module-level import in
        # pipeline.py; patch it there so the endpoint picks up the stub.
        monkeypatch.setattr(
            pipeline_mod,
            "load_current_strict_evaluation_bundle",
            lambda *_args, **_kwargs: bundle,
        )

        resp = client.get("/api/pipeline/strength-jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert len(data["admitted_samples"]) == 1
        assert data["admitted_samples"][0]["id"] == "good"

        diag = data["inadmissible_diagnostics"]
        assert len(diag) == 1
        assert diag[0]["id"] == "bad69"
        assert "hands_per_strength_sample_not_70" in diag[0]["rejection_reasons"]

    def test_daemon_health_failure_is_fail_closed(self, client, monkeypatch):
        from server.routes import pipeline

        monkeypatch.setattr(
            pipeline,
            "load_strict_strength_snapshot",
            lambda *_args, **_kwargs: {"available": False, "reason": "active_pool_empty"},
        )

        def _boom():
            raise RuntimeError("daemon reader broken")

        monkeypatch.setattr(pipeline, "_daemon_health_snapshot", _boom)
        resp = client.get("/api/pipeline/strength-jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is False
        assert data["daemon"]["alive"] is False
        assert data["daemon"]["health_error"] == "daemon_health_unavailable"

    def test_staged_pending_requires_current_identity_pool_artifacts_and_full_replay(
        self,
        client,
        monkeypatch,
        tmp_path,
    ):
        from replay_analysis import ReplayValidation
        from server.routes import pipeline
        import bot_artifact
        import replay_analysis

        identity = "a" * 64
        active = [bot_name(143), bot_name(144)]
        snapshot = {
            "available": True,
            "evaluation_identity_digest": identity,
            "evaluation_manifest_digest": "b" * 64,
            "epoch_reset_receipt_digest": "c" * 64,
            "active_bots": active,
            "match_history": [],
            "daemon_stats": {},
        }
        pending = tmp_path / "match_replay" / ".pending"
        pending.mkdir(parents=True)

        def payload(name, **overrides):
            value = {
                "id": name,
                "timestamp": "2026-07-19T00:00:00Z",
                "evaluation_epoch": "national_tcp_policy_v1",
                "execution_mode": "native_tcp",
                "evaluation_identity_digest": identity,
                "bot0": active[0],
                "bot1": active[1],
                "strength_sample_unit": "70_hand_match",
                "hands_per_strength_sample": 70,
                "strength_sample_count": 1,
                "strength_admitted": True,
                "strength_complete": True,
                "strength_compliance_passed": True,
            }
            value.update(overrides)
            return value

        good = payload("good.json")
        old = payload("old.json", evaluation_identity_digest="9" * 64)
        offpool = payload("offpool.json", bot1=bot_name(999))
        short = payload("short.json", hands_per_strength_sample=69)
        false_claim = payload("false.json", strength_complete=False)
        for name, value in (("good.json", good), ("old.json", old), ("offpool.json", offpool), ("short.json", short), ("false.json", false_claim)):
            (pending / name).write_text(json.dumps(value), encoding="utf-8")
        (pending / "target.json").write_text("{}", encoding="utf-8")
        (pending / "symlink.json").symlink_to(pending / "target.json")

        def validate(value, *, expected_evaluation_identity_digest, expected_replay_id):
            if (
                value.get("evaluation_identity_digest") != expected_evaluation_identity_digest
                or value.get("id") != expected_replay_id
                or value.get("hands_per_strength_sample") != 70
                or value.get("strength_complete") is not True
            ):
                return ReplayValidation(False, "strict_70_hand_contract_failed")
            hashes = tuple(sorted((
                (str(value["bot0"]), "h" * 64),
                (str(value["bot1"]), "h" * 64),
            )))
            return ReplayValidation(True, artifact_hashes=hashes)

        monkeypatch.setattr(pipeline, "RESULTS_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "load_strict_strength_snapshot", lambda *_a, **_k: snapshot)
        monkeypatch.setattr(pipeline, "load_current_strict_evaluation_bundle", lambda *_a, **_k: {"available": False})
        monkeypatch.setattr(replay_analysis, "validate_native_replay", validate)
        monkeypatch.setattr(bot_artifact, "hash_path", lambda _path: "h" * 64)

        data = client.get("/api/pipeline/strength-jobs").json()
        assert [row["filename"] for row in data["staged_pending"]] == ["good.json"]
        reasons = {
            row.get("filename"): row["rejection_reasons"]
            for row in data["inadmissible_diagnostics"]
        }
        assert "evaluation_identity_digest_mismatch" in reasons["old.json"]
        assert "bot1_not_in_active_pool" in reasons["offpool.json"]
        assert any(reason.startswith("staged_replay_invalid:") for reason in reasons["short.json"])
        assert any(reason.startswith("staged_replay_invalid:") for reason in reasons["false.json"])
        assert reasons["symlink.json"] == ["staged_pending_symlink_rejected"]

    def test_pending_reader_rejects_files_over_observer_cap(self, monkeypatch, tmp_path):
        from server.routes import pipeline

        pending = tmp_path / ".pending"
        pending.mkdir()
        (pending / "large.json").write_bytes(b"x" * 17)
        monkeypatch.setattr(pipeline, "_MAX_STAGED_REPLAY_BYTES", 16)
        directory_fd = __import__("os").open(pending, __import__("os").O_RDONLY)
        try:
            try:
                pipeline._read_pending_bytes(directory_fd, "large.json")
            except OSError as exc:
                assert "observer cap" in str(exc)
            else:
                raise AssertionError("oversized pending replay was accepted")
        finally:
            __import__("os").close(directory_fd)

    def test_rows_are_paginated_with_authoritative_totals(self, client, monkeypatch):
        from server.routes import pipeline

        snapshot = {
            "available": True,
            "evaluation_identity_digest": "a" * 64,
            "evaluation_manifest_digest": "b" * 64,
            "epoch_reset_receipt_digest": "c" * 64,
            "active_bots": [bot_name(143), bot_name(144)],
            "match_history": [
                {
                    "id": f"match-{index}",
                    "timestamp": None,
                    "bot0": bot_name(143),
                    "bot1": bot_name(144),
                    "bot0_wins": 1,
                    "bot1_wins": 0,
                    "draws": 0,
                    "strength_sample_count": 1,
                    "hands_per_strength_sample": 70,
                    "replay_sha256": f"{index:x}".rjust(64, "0"),
                }
                for index in range(5)
            ],
            "daemon_stats": {},
        }
        monkeypatch.setattr(pipeline, "load_strict_strength_snapshot", lambda *_a, **_k: snapshot)
        monkeypatch.setattr(
            pipeline,
            "load_current_strict_evaluation_bundle",
            lambda *_a, **_k: {"available": True, "raw_append_logs": {"match_history": b""}},
        )
        pipeline._STRENGTH_OBSERVER_CACHE.clear()

        data = client.get("/api/pipeline/strength-jobs?offset=2&limit=2").json()

        assert [row["id"] for row in data["admitted_samples"]] == ["match-2", "match-3"]
        assert data["pagination"] == {
            "offset": 2,
            "limit": 2,
            "admitted_total": 5,
            "staged_pending_total": 0,
            "inadmissible_total": 0,
            "admitted_has_more": True,
            "staged_pending_has_more": False,
            "inadmissible_has_more": False,
        }

    def test_directory_entry_budget_fails_closed_without_partial_rows(
        self, client, monkeypatch, tmp_path
    ):
        from server.routes import pipeline

        pending = tmp_path / "match_replay" / ".pending"
        pending.mkdir(parents=True)
        for index in range(4):
            (pending / f"entry-{index}.txt").write_text("ignored", encoding="utf-8")
        snapshot = {
            "available": True,
            "evaluation_identity_digest": "a" * 64,
            "evaluation_manifest_digest": "b" * 64,
            "epoch_reset_receipt_digest": "c" * 64,
            "active_bots": [bot_name(143), bot_name(144)],
            "match_history": [],
            "daemon_stats": {},
        }
        monkeypatch.setattr(pipeline, "RESULTS_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "_MAX_STRENGTH_DIRECTORY_ENTRIES", 2)
        monkeypatch.setattr(pipeline, "load_strict_strength_snapshot", lambda *_a, **_k: snapshot)
        monkeypatch.setattr(
            pipeline,
            "load_current_strict_evaluation_bundle",
            lambda *_a, **_k: {"available": True, "raw_append_logs": {"match_history": b""}},
        )
        pipeline._STRENGTH_OBSERVER_CACHE.clear()

        data = client.get("/api/pipeline/strength-jobs").json()

        assert data["observer"]["complete"] is False
        assert data["staged_pending"] == []
        assert data["inadmissible_diagnostics"][0]["rejection_reasons"] == [
            "strength_observer_directory_entry_budget_exceeded"
        ]

    def test_total_byte_budget_fails_closed_without_partial_validation(
        self, client, monkeypatch, tmp_path
    ):
        from server.routes import pipeline

        pending = tmp_path / "match_replay" / ".pending"
        pending.mkdir(parents=True)
        (pending / "oversized-total.json").write_bytes(b"{" + b"x" * 40 + b"}")
        snapshot = {
            "available": True,
            "evaluation_identity_digest": "a" * 64,
            "evaluation_manifest_digest": "b" * 64,
            "epoch_reset_receipt_digest": "c" * 64,
            "active_bots": [bot_name(143), bot_name(144)],
            "match_history": [],
            "daemon_stats": {},
        }
        monkeypatch.setattr(pipeline, "RESULTS_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "_MAX_STRENGTH_TOTAL_READ_BYTES", 16)
        monkeypatch.setattr(pipeline, "load_strict_strength_snapshot", lambda *_a, **_k: snapshot)
        monkeypatch.setattr(
            pipeline,
            "load_current_strict_evaluation_bundle",
            lambda *_a, **_k: {"available": True, "raw_append_logs": {"match_history": b""}},
        )
        pipeline._STRENGTH_OBSERVER_CACHE.clear()

        data = client.get("/api/pipeline/strength-jobs").json()

        assert data["observer"]["complete"] is False
        assert data["staged_pending"] == []
        assert data["inadmissible_diagnostics"][0]["rejection_reasons"] == [
            "strength_observer_byte_budget_exceeded"
        ]

    def test_file_and_cpu_budgets_are_explicit_fail_closed_contracts(
        self, client, monkeypatch, tmp_path
    ):
        from server.routes import pipeline

        pending = tmp_path / "match_replay" / ".pending"
        pending.mkdir(parents=True)
        (pending / "one.json").write_text("{}", encoding="utf-8")
        (pending / "two.json").write_text("{}", encoding="utf-8")
        snapshot = {
            "available": True,
            "evaluation_identity_digest": "a" * 64,
            "evaluation_manifest_digest": "b" * 64,
            "epoch_reset_receipt_digest": "c" * 64,
            "active_bots": [bot_name(143), bot_name(144)],
            "match_history": [],
            "daemon_stats": {},
        }
        monkeypatch.setattr(pipeline, "RESULTS_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "_MAX_STAGED_REPLAY_FILES", 1)
        monkeypatch.setattr(pipeline, "load_strict_strength_snapshot", lambda *_a, **_k: snapshot)
        monkeypatch.setattr(
            pipeline,
            "load_current_strict_evaluation_bundle",
            lambda *_a, **_k: {"available": True, "raw_append_logs": {"match_history": b""}},
        )
        pipeline._STRENGTH_OBSERVER_CACHE.clear()
        file_limited = client.get("/api/pipeline/strength-jobs").json()
        assert file_limited["observer"]["complete"] is False
        assert "strength_observer_file_budget_exceeded" in file_limited["observer"]["issues"]

        monkeypatch.setattr(pipeline, "_MAX_STRENGTH_CPU_SECONDS", 0.0)
        pipeline._STRENGTH_OBSERVER_CACHE.clear()
        cpu_limited = client.get("/api/pipeline/strength-jobs").json()
        assert cpu_limited["available"] is False
        assert cpu_limited["reason"] == "strength_observer_cpu_budget_exceeded"
        assert cpu_limited["observer"]["complete"] is False

    def test_strength_scan_runs_off_event_loop_so_control_health_stays_responsive(
        self, monkeypatch
    ):
        from server.routes import control, pipeline

        def slow_snapshot(*_args, **_kwargs):
            time.sleep(0.20)
            return {"available": False, "reason": "active_pool_empty"}

        monkeypatch.setattr(pipeline, "load_strict_strength_snapshot", slow_snapshot)
        monkeypatch.setattr(pipeline, "_daemon_health_snapshot", lambda: {"alive": False})
        monkeypatch.setattr(control, "_control_health_snapshot", lambda: {"overall": "healthy"})
        pipeline._STRENGTH_OBSERVER_CACHE.clear()

        async def exercise():
            strength = asyncio.create_task(pipeline.pipeline_strength_jobs(offset=0, limit=50))
            await asyncio.sleep(0.01)
            started = time.monotonic()
            health = await asyncio.wait_for(control.control_health(), timeout=0.15)
            elapsed = time.monotonic() - started
            result = await strength
            return health, elapsed, result

        health, elapsed, result = asyncio.run(exercise())

        assert health == {"overall": "healthy"}
        assert elapsed < 0.15
        assert result["available"] is False

    def test_unavailable_projection_still_binds_reset_pool_and_capabilities(
        self, client, monkeypatch
    ):
        from server.routes import pipeline

        reset_digest = "c" * 64
        monkeypatch.setattr(
            pipeline,
            "load_current_strict_evaluation_bundle",
            lambda *_a, **_k: {
                "available": True,
                "active_bots": [bot_name(143)],
                "epoch_reset_receipt": {"receipt_digest": reset_digest},
                "manifest_digest": "b" * 64,
                "manifest": {"evaluation_identity_digest": "a" * 64},
                "raw_files": {},
                "raw_append_logs": {"match_history": b""},
            },
        )
        monkeypatch.setattr(
            pipeline,
            "load_strict_strength_snapshot",
            lambda *_a, **_k: {
                "available": False,
                "reason": "active_pool_singleton",
                "active_bots": [bot_name(143)],
            },
        )
        pipeline._STRENGTH_OBSERVER_CACHE.clear()

        data = client.get("/api/pipeline/strength-jobs").json()

        assert data["available"] is False
        assert data["active_bots"] == [bot_name(143)]
        assert data["epoch_reset_receipt_digest"] == reset_digest
        assert data["authority_binding"] == {
            "evaluation_epoch": "national_tcp_policy_v1",
            "active_bots": [bot_name(143)],
            "epoch_reset_receipt_digest": reset_digest,
            "evaluation_identity_digest": "a" * 64,
            "evaluation_manifest_digest": "b" * 64,
            "complete": True,
        }
        assert data["capabilities"]["durable_job_lifecycle"] is False
        assert data["capabilities"]["queued_running_leases"] is False

    def test_authority_binding_fails_closed_on_pool_or_reset_source_mismatch(self):
        from server.routes import pipeline

        binding = pipeline._strength_authority_binding(
            {
                "active_bots": [bot_name(143), bot_name(144)],
                "epoch_reset_receipt_digest": "a" * 64,
                "evaluation_identity_digest": "b" * 64,
                "evaluation_manifest_digest": "c" * 64,
            },
            {
                "active_bots": [bot_name(143), bot_name(145)],
                "epoch_reset_receipt": {"receipt_digest": "d" * 64},
                "manifest": {"evaluation_identity_digest": "e" * 64},
                "manifest_digest": "f" * 64,
            },
        )

        assert binding["complete"] is False
        assert binding["epoch_reset_receipt_digest"] is None
        assert binding["evaluation_identity_digest"] is None
        assert binding["evaluation_manifest_digest"] is None

        malformed = pipeline._strength_authority_binding(
            {
                "active_bots": ["national_vx"],
                "epoch_reset_receipt_digest": "a" * 64,
            },
            {},
        )
        assert malformed["active_bots"] == ["national_vx"]
        assert malformed["complete"] is False

    def test_manifest_budget_rejects_before_authority_bundle_loader(
        self, client, monkeypatch, tmp_path
    ):
        from server.routes import pipeline

        total_bytes = _write_preflight_cycle(tmp_path, extra_file_bytes=256)
        loader_calls = []

        def must_not_load(*_args, **_kwargs):
            loader_calls.append(True)
            raise AssertionError("heavy authority loader ran after preflight overflow")

        monkeypatch.setattr(pipeline, "RESULTS_DIR", tmp_path)
        monkeypatch.setattr(
            pipeline,
            "_MAX_STRENGTH_TOTAL_READ_BYTES",
            total_bytes - 1,
        )
        monkeypatch.setattr(
            pipeline,
            "load_current_strict_evaluation_bundle",
            must_not_load,
        )
        pipeline._STRENGTH_OBSERVER_CACHE.clear()

        data = client.get("/api/pipeline/strength-jobs").json()

        assert loader_calls == []
        assert data["available"] is False
        assert data["reason"] == "strength_observer_byte_budget_exceeded"
        assert data["observer"]["complete"] is False
        assert data["capabilities"]["producer_consumer_dispatch"] is False

    def test_bounded_manifest_preflight_accounts_files_before_loader(self, tmp_path):
        from server.routes import pipeline

        _write_preflight_cycle(tmp_path)
        budget = pipeline._StrengthObserverBudget()

        assert pipeline._preflight_strength_bundle_budget(tmp_path, budget) is True
        receipt = budget.projection(complete=True, issues=[])
        assert receipt["usage"]["files_read"] == 8  # manifest + 5 JSON + 2 logs
        assert receipt["usage"]["directory_entries"] == 7
        assert receipt["usage"]["rows_seen"] == 0
        assert receipt["usage"]["total_read_bytes"] > 0

    def test_preflight_and_authority_loader_share_one_cycle_lock(
        self, monkeypatch, tmp_path
    ):
        from server.routes import pipeline

        state = {"locked": False}
        events = []

        @contextmanager
        def cycle_lock(root, *, exclusive):
            assert root == tmp_path
            assert exclusive is False
            assert state["locked"] is False
            state["locked"] = True
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")
                state["locked"] = False

        def preflight(root, _budget):
            assert root == tmp_path
            assert state["locked"] is True
            events.append("preflight")
            return False

        def bundle_loader(root, **_kwargs):
            assert root == tmp_path
            assert state["locked"] is True
            events.append("bundle")
            return {"available": False, "reason": "cycle_manifest_missing"}

        def snapshot_loader(*_args, **_kwargs):
            assert state["locked"] is True
            events.append("snapshot")
            return {
                "available": False,
                "reason": "active_pool_singleton",
                "active_bots": [bot_name(143)],
                "epoch_reset_receipt_digest": "a" * 64,
            }

        monkeypatch.setattr(pipeline, "RESULTS_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "evaluation_cycle_lock", cycle_lock)
        monkeypatch.setattr(pipeline, "_preflight_strength_bundle_budget", preflight)
        monkeypatch.setattr(pipeline, "load_current_strict_evaluation_bundle", bundle_loader)
        monkeypatch.setattr(pipeline, "load_strict_strength_snapshot", snapshot_loader)

        _authority, projection = pipeline._read_strength_projection()

        assert projection["available"] is False
        assert events == ["enter", "preflight", "bundle", "snapshot", "exit"]

    def test_row_budget_rejects_before_authority_bundle_loader(
        self, client, monkeypatch, tmp_path
    ):
        from server.routes import pipeline

        match_history = b"".join(
            json.dumps({"id": f"row-{index}", "strength_admitted": False}).encode()
            + b"\n"
            for index in range(3)
        )
        _write_preflight_cycle(tmp_path, match_history=match_history)
        loader_calls = []

        def must_not_load(*_args, **_kwargs):
            loader_calls.append(True)
            raise AssertionError("heavy authority loader ran after row overflow")

        monkeypatch.setattr(pipeline, "RESULTS_DIR", tmp_path)
        monkeypatch.setattr(pipeline, "_MAX_STRENGTH_ROWS", 2)
        monkeypatch.setattr(
            pipeline,
            "load_current_strict_evaluation_bundle",
            must_not_load,
        )
        pipeline._STRENGTH_OBSERVER_CACHE.clear()

        data = client.get("/api/pipeline/strength-jobs").json()

        assert loader_calls == []
        assert data["available"] is False
        assert data["reason"] == "strength_observer_row_budget_exceeded"

    def test_different_pages_share_one_heavy_identity_observation(
        self, client, monkeypatch
    ):
        from server.routes import pipeline

        calls = []
        bundle = {
            "available": True,
            "raw_files": {},
            "raw_append_logs": {"match_history": b""},
        }
        snapshot = {
            "available": True,
            "evaluation_identity_digest": "a" * 64,
            "evaluation_manifest_digest": "b" * 64,
            "epoch_reset_receipt_digest": "c" * 64,
            "active_bots": [bot_name(143), bot_name(144)],
            "match_history": [
                {
                    "id": f"match-{index}",
                    "bot0": bot_name(143),
                    "bot1": bot_name(144),
                    "strength_sample_count": 70,
                    "hands_per_strength_sample": 70,
                }
                for index in range(5)
            ],
            "daemon_stats": {},
        }

        def load_bundle(*_args, **_kwargs):
            calls.append("bundle")
            return bundle

        monkeypatch.setattr(
            pipeline,
            "load_current_strict_evaluation_bundle",
            load_bundle,
        )
        monkeypatch.setattr(
            pipeline,
            "load_strict_strength_snapshot",
            lambda *_a, **_k: snapshot,
        )
        pipeline._STRENGTH_OBSERVER_CACHE.clear()

        first = client.get("/api/pipeline/strength-jobs?offset=0&limit=2").json()
        second = client.get("/api/pipeline/strength-jobs?offset=2&limit=2").json()

        assert calls == ["bundle"]
        assert [row["id"] for row in first["admitted_samples"]] == [
            "match-0",
            "match-1",
        ]
        assert [row["id"] for row in second["admitted_samples"]] == [
            "match-2",
            "match-3",
        ]
        assert first["pagination"]["admitted_total"] == 5
        assert second["pagination"]["admitted_total"] == 5

    def test_page_bounds_are_rejected_before_heavy_observation(
        self, client, monkeypatch
    ):
        from server.routes import pipeline

        loader_calls = []
        monkeypatch.setattr(
            pipeline,
            "load_current_strict_evaluation_bundle",
            lambda *_a, **_k: loader_calls.append(True),
        )
        pipeline._STRENGTH_OBSERVER_CACHE.clear()

        too_far = client.get(
            f"/api/pipeline/strength-jobs?offset={pipeline._MAX_STRENGTH_ROWS + 1}&limit=1"
        )
        too_wide = client.get(
            f"/api/pipeline/strength-jobs?offset=0&limit={pipeline._MAX_STRENGTH_PAGE_LIMIT + 1}"
        )

        assert too_far.status_code == 422
        assert too_wide.status_code == 422
        assert loader_calls == []
