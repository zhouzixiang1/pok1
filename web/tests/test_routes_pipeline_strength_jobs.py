"""Tests for /api/pipeline/strength-jobs — 70-hand background job projection."""

import json


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
            "active_bots": ["national_v143", "national_v144"],
            "match_history": [
                {
                    "id": "match_001",
                    "timestamp": "2026-07-19T00:00:00Z",
                    "bot0": "national_v143",
                    "bot1": "national_v144",
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
            lambda *_args, **_kwargs: {"available": False},
        )

        resp = client.get("/api/pipeline/strength-jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert data["evaluation_identity_digest"] == "a" * 64
        assert data["active_bots"] == ["national_v143", "national_v144"]

        assert len(data["admitted_samples"]) == 1
        sample = data["admitted_samples"][0]
        assert sample["id"] == "match_001"
        assert sample["bot0_wins"] == 40
        assert sample["strength_sample_count"] == 70
        assert sample["replay_sha256"] == "d" * 64

        assert data["staged_pending"] == []
        assert data["inadmissible_diagnostics"] == []
        assert data["daemon_stats"]["pairs"]["national_v143 vs national_v144"] == 1

    def test_inadmissible_diagnostics_explain_rejection(self, client, monkeypatch):
        from server.routes import pipeline

        identity = "a" * 64
        snapshot = {
            "available": True,
            "evaluation_identity_digest": identity,
            "evaluation_manifest_digest": "b" * 64,
            "active_bots": ["national_v143", "national_v144"],
            "match_history": [
                {
                    "id": "good",
                    "execution_mode": "native_tcp",
                    "evaluation_epoch": "national_tcp_policy_v1",
                    "evaluation_identity_digest": identity,
                    "bot0": "national_v143",
                    "bot1": "national_v144",
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
            "bot0": "national_v143",
            "bot1": "national_v144",
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
