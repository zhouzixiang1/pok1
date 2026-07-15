"""Tests for /api/logs/* endpoints."""

import json
from pathlib import Path

import pytest


class TestGenerationLogs:
    def test_list_generations(self, client, tmp_path, monkeypatch):
        from server.routes import logs
        v_dir = tmp_path / "v30" / "logs"
        v_dir.mkdir(parents=True)
        (v_dir / "master_io.txt").write_text("log line\n")
        monkeypatch.setattr(logs, "RESULTS_DIR", tmp_path)
        resp = client.get("/api/logs/generations")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert data == []

    def test_get_log_content(self, client, tmp_path, monkeypatch):
        from server.routes import logs
        from server.routes import _helpers

        v_dir = tmp_path / "v143" / "logs"
        v_dir.mkdir(parents=True)
        (v_dir / "master_io.txt").write_text("line1\nline2\nline3\n")
        monkeypatch.setattr(logs, "RESULTS_DIR", tmp_path)
        monkeypatch.setattr(
            _helpers,
            "strict_observable_generation_versions",
            lambda *_args, **_kwargs: {143},
        )
        resp = client.get("/api/logs/generations/v143/master_io.txt")
        assert resp.status_code == 200
        data = resp.json()
        assert "content" in data
        assert data["version"] == "v143"
        assert data["filename"] == "master_io.txt"

    def test_get_log_tail(self, client, tmp_path, monkeypatch):
        from server.routes import logs
        from server.routes import _helpers

        v_dir = tmp_path / "v143" / "logs"
        v_dir.mkdir(parents=True)
        (v_dir / "worker_io.txt").write_text("line1\nline2\nline3\nline4\nline5\n")
        monkeypatch.setattr(logs, "RESULTS_DIR", tmp_path)
        monkeypatch.setattr(
            _helpers,
            "strict_observable_generation_versions",
            lambda *_args, **_kwargs: {143},
        )
        resp = client.get("/api/logs/generations/v143/worker_io.txt?tail=2")
        assert resp.status_code == 200
        data = resp.json()
        content = data["content"]
        lines = content.strip().split("\n")
        assert len(lines) <= 2

    def test_strict_invocation_log_has_opaque_list_and_read_identity(
        self,
        client,
        tmp_path,
        monkeypatch,
    ):
        from server.routes import _helpers, logs

        invocation_id = "1" * 32
        logs_dir = tmp_path / "v143" / "logs"
        strict_log = (
            logs_dir
            / "strict_invocations"
            / invocation_id
            / "master_proposal_mechanism_io.txt"
        )
        strict_log.parent.mkdir(parents=True)
        strict_log.write_text("strict line 1\nstrict line 2\n")
        (logs_dir / "master_io.txt").write_text("flat log\n")
        identifier = (
            f"strict@{invocation_id}@master_proposal_mechanism_io.txt"
        )
        monkeypatch.setattr(logs, "RESULTS_DIR", tmp_path)
        monkeypatch.setattr(
            _helpers,
            "strict_observable_generation_versions",
            lambda *_args, **_kwargs: {143},
        )

        listed = client.get("/api/logs/generations")
        assert listed.status_code == 200
        assert listed.json() == [{
            "version": "v143",
            "files": ["master_io.txt", identifier],
        }]
        response = client.get(
            f"/api/logs/generations/v143/{identifier}?tail=1"
        )
        assert response.status_code == 200
        assert response.json() == {
            "version": "v143",
            "filename": identifier,
            "content": "strict line 2\n",
        }
        assert not Path(str(strict_log) + ".lock").exists()

    def test_strict_log_identity_rejects_symlink_and_traversal(
        self,
        client,
        tmp_path,
        monkeypatch,
    ):
        from server.routes import _helpers, logs

        invocation_id = "2" * 32
        logs_dir = tmp_path / "v143" / "logs"
        invocation_dir = logs_dir / "strict_invocations" / invocation_id
        invocation_dir.mkdir(parents=True)
        outside = tmp_path / "outside.txt"
        outside.write_text("must not be exposed\n")
        (invocation_dir / "critic_io.txt").symlink_to(outside)
        monkeypatch.setattr(logs, "RESULTS_DIR", tmp_path)
        monkeypatch.setattr(
            _helpers,
            "strict_observable_generation_versions",
            lambda *_args, **_kwargs: {143},
        )
        identifier = f"strict@{invocation_id}@critic_io.txt"

        listed = client.get("/api/logs/generations")
        assert listed.status_code == 200
        assert listed.json() == [{"version": "v143", "files": []}]
        assert client.get(
            f"/api/logs/generations/v143/{identifier}"
        ).status_code == 400
        assert client.get(
            "/api/logs/generations/v143/"
            f"strict@{invocation_id}@..%2Foutside.txt"
        ).status_code in {400, 404}

    def test_generation_log_rejects_symlinked_version_parent(
        self,
        client,
        tmp_path,
        monkeypatch,
    ):
        from server.routes import _helpers, logs

        outside = tmp_path / "outside"
        (outside / "logs").mkdir(parents=True)
        (outside / "logs" / "master_io.txt").write_text("outside\n")
        results = tmp_path / "results"
        results.mkdir()
        (results / "v143").symlink_to(outside, target_is_directory=True)
        monkeypatch.setattr(logs, "RESULTS_DIR", results)
        monkeypatch.setattr(
            _helpers,
            "strict_observable_generation_versions",
            lambda *_args, **_kwargs: {143},
        )

        assert client.get("/api/logs/generations").json() == []
        assert client.get(
            "/api/logs/generations/v143/master_io.txt"
        ).status_code == 400

    @pytest.mark.parametrize("swap_level", ["version", "logs", "invocation"])
    def test_generation_log_parent_swap_cannot_escape_results_tree(
        self,
        client,
        tmp_path,
        monkeypatch,
        swap_level,
    ):
        from server.routes import _helpers, logs

        invocation_id = "3" * 32
        identifier = f"strict@{invocation_id}@critic_io.txt"
        results = tmp_path / "results"
        invocation_dir = (
            results
            / "v143"
            / "logs"
            / "strict_invocations"
            / invocation_id
        )
        invocation_dir.mkdir(parents=True)
        (invocation_dir / "critic_io.txt").write_text(
            "safe in-tree bytes\n",
            encoding="utf-8",
        )
        outside = tmp_path / f"outside-{swap_level}"
        if swap_level == "version":
            outside_target = (
                outside / "logs" / "strict_invocations" / invocation_id
            )
            swap_path = results / "v143"
        elif swap_level == "logs":
            outside_target = outside / "strict_invocations" / invocation_id
            swap_path = results / "v143" / "logs"
        else:
            outside_target = outside
            swap_path = invocation_dir
        outside_target.mkdir(parents=True)
        (outside_target / "critic_io.txt").write_text(
            "outside-secret-must-not-be-returned\n",
            encoding="utf-8",
        )

        original_resolver = _helpers.generation_log_path
        swapped = False

        def resolve_then_swap(logs_dir, filename):
            nonlocal swapped
            path = original_resolver(logs_dir, filename)
            if path is not None and not swapped:
                held = swap_path.with_name(swap_path.name + ".held")
                swap_path.rename(held)
                swap_path.symlink_to(outside, target_is_directory=True)
                swapped = True
            return path

        monkeypatch.setattr(logs, "RESULTS_DIR", results)
        monkeypatch.setattr(
            _helpers,
            "strict_observable_generation_versions",
            lambda *_args, **_kwargs: {143},
        )
        monkeypatch.setattr(_helpers, "generation_log_path", resolve_then_swap)

        response = client.get(f"/api/logs/generations/v143/{identifier}")

        assert swapped is True
        assert response.status_code == 409
        assert "outside-secret-must-not-be-returned" not in response.text

    def test_get_log_missing(self, client):
        resp = client.get("/api/logs/generations/v99999/nonexistent.txt")
        assert resp.status_code == 404

    def test_path_traversal_blocked(self, client):
        resp = client.get("/api/logs/generations/../../etc/passwd")
        assert resp.status_code in (400, 404, 422)


class TestOrchestratorLogs:
    def test_list(self, client, tmp_path, monkeypatch):
        from server.routes import logs
        (tmp_path / "orchestrator_20260601_120000.txt").write_text("log\n")
        monkeypatch.setattr(logs, "ORCHESTRATOR_LOGS_DIR", tmp_path)
        monkeypatch.setattr(logs, "_current_log_epoch_identity", lambda: {
            "evaluation_epoch": "national_tcp_policy_v1",
            "policy_epoch_reset_receipt_digest": "a" * 64,
        })
        resp = client.get("/api/logs/orchestrator")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert "filename" in data[0]
        assert "size_bytes" in data[0]

    def test_get_log(self, client, tmp_path, monkeypatch):
        from server.routes import logs
        fname = "orchestrator_20260601_120000.txt"
        (tmp_path / fname).write_text("orchestrator log content here\n")
        monkeypatch.setattr(logs, "ORCHESTRATOR_LOGS_DIR", tmp_path)
        monkeypatch.setattr(logs, "_current_log_epoch_identity", lambda: {
            "evaluation_epoch": "national_tcp_policy_v1",
            "policy_epoch_reset_receipt_digest": "a" * 64,
        })
        resp = client.get(f"/api/logs/orchestrator/{fname}")
        assert resp.status_code == 200
        assert len(resp.text) > 0

    def test_invalid_filename(self, client):
        resp = client.get("/api/logs/orchestrator/../../etc/passwd")
        assert resp.status_code in (400, 404)

    def test_non_matching_filename(self, client):
        resp = client.get("/api/logs/orchestrator/random.txt")
        assert resp.status_code == 400

    def test_not_found(self, client):
        resp = client.get("/api/logs/orchestrator/orchestrator_29990101_000000.txt")
        assert resp.status_code == 404


class TestSystemEvents:
    def test_structured_source_filters_run_id_and_stage(self, client, tmp_path, monkeypatch):
        from server.routes import logs

        identity = {
            "evaluation_epoch": "national_tcp_policy_v1",
            "epoch_reset_receipt_digest": "a" * 64,
        }

        events_file = tmp_path / "events.jsonl"
        events_file.write_text(
            json.dumps({
                "ts": 1.0,
                "type": "pipeline.master_done",
                "severity": "info",
                "message": "done",
                "data": {
                    **identity,
                    "category": "pipeline.master_done",
                    "run_id": "231#0",
                    "stage": "master_planned",
                },
            }) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(logs, "RESULTS_DIR", tmp_path)
        monkeypatch.setattr(logs, "_current_event_epoch_identity", lambda: identity)

        resp = client.get(
            "/api/logs/system-events?source=structured&run_id=231%230&stage=master_planned"
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["events"][0]["type"] == "pipeline.master_done"

    def test_structured_source_normalizes_legacy_sigterm_misclassification(self, client, tmp_path, monkeypatch):
        from server.routes import logs

        identity = {
            "evaluation_epoch": "national_tcp_policy_v1",
            "epoch_reset_receipt_digest": "a" * 64,
        }

        events_file = tmp_path / "events.jsonl"
        events_file.write_text(
            json.dumps({
                "ts": 1.0,
                "type": "pipeline.llm_role_shutdown_cancelled",
                "severity": "info",
                "message": "LLM process received SIGTERM",
                "data": {
                    **identity,
                    "category": "pipeline.llm_role_shutdown_cancelled",
                    "shutdown_requested": False,
                    "run_id": "257#0",
                    "stage": "reviewed",
                },
            }) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(logs, "RESULTS_DIR", tmp_path)
        monkeypatch.setattr(logs, "_current_event_epoch_identity", lambda: identity)

        resp = client.get(
            "/api/logs/system-events?source=structured&type=pipeline.llm_role_process_terminated"
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        event = data["events"][0]
        assert event["type"] == "pipeline.llm_role_process_terminated"
        assert event["severity"] == "error"
        assert event["data"]["original_type"] == "pipeline.llm_role_shutdown_cancelled"
        assert event["data"]["legacy_misclassified"] is True

    def test_system_events_rejects_unknown_source(self, client):
        resp = client.get("/api/logs/system-events?source=unknown")
        assert resp.status_code == 400

    def test_system_events_fail_empty_before_epoch_reset(
        self, client, tmp_path, monkeypatch
    ):
        from server.routes import logs

        (tmp_path / "events.jsonl").write_text(
            json.dumps({
                "ts": 1.0,
                "type": "battle_exp.old",
                "severity": "info",
                "message": "retired experience",
                "data": {},
            }) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(logs, "RESULTS_DIR", tmp_path)
        monkeypatch.setattr(logs, "_current_event_epoch_identity", lambda: None)

        response = client.get("/api/logs/system-events")

        assert response.status_code == 200
        assert response.json() == {
            "events": [],
            "total": 0,
            "authority_status": "policy_epoch_not_initialized",
        }

    def test_system_events_rejects_rows_from_another_reset_identity(
        self, client, tmp_path, monkeypatch
    ):
        from server.routes import logs

        current = {
            "evaluation_epoch": "national_tcp_policy_v1",
            "epoch_reset_receipt_digest": "a" * 64,
        }
        (tmp_path / "events.jsonl").write_text(
            json.dumps({
                "ts": 1.0,
                "type": "pipeline.old",
                "severity": "info",
                "message": "wrong reset",
                "data": {
                    "evaluation_epoch": "national_tcp_policy_v1",
                    "epoch_reset_receipt_digest": "b" * 64,
                },
            }) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(logs, "RESULTS_DIR", tmp_path)
        monkeypatch.setattr(logs, "_current_event_epoch_identity", lambda: current)

        response = client.get("/api/logs/system-events")

        assert response.status_code == 200
        assert response.json()["events"] == []
        assert response.json()["total"] == 0
