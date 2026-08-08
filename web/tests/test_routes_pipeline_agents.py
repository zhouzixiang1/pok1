"""Tests for /api/pipeline/agents — structured agent activity projection."""

import asyncio
import json
import time


class TestPipelineAgents:
    def test_no_workflow_returns_unavailable(self, client):
        # No valid strict checkpoint in the isolated tmp results dir.
        resp = client.get("/api/pipeline/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is False
        assert data["reason"] == "no_strict_workflow"
        assert data["evaluation_epoch"] == "national_tcp_policy_v1"

    def test_legacy_checkpoint_not_presented_as_agent_activity(self, client):
        # A pre-policy checkpoint is not current strict authority.
        from server.routes import pipeline

        sample = {
            "next_v": 11,
            "source_v": 10,
            "stage": "master_planned",
            "gate_results": {},
        }
        pipeline.PIPELINE_STATE_FILE.write_text(json.dumps(sample))

        resp = client.get("/api/pipeline/agents")
        assert resp.status_code == 200
        assert resp.json()["available"] is False

    def test_projection_carries_identity_and_stage(self, client, monkeypatch):
        from server.routes import pipeline

        checkpoint = {
            "checkpoint_schema_version": 2,
            "evaluation_epoch": "national_tcp_policy_v1",
            "checkpoint_revision": 7,
            "next_v": 144,
            "source_v": 143,
            "parent2_v": None,
            "stage": "workers_done",
            "workflow_run_id": "workflow-v2",
            "run_id": "144#1",
            "generation_attempt": 1,
            "audit_attempt": 0,
            "precommit_attempt": 0,
            "worker_failure_count": 2,
            "master_plan": {
                "analysis": "tighten river overbet",
                "tasks": [
                    {
                        "worker_id": 1,
                        "role": "Algorithmic Logic Architect",
                        "target_files": ["policy.py"],
                        "difficulty": "medium",
                        "skill_layer": "line_template",
                    },
                ],
            },
            "gate_results": {
                "quality": {
                    "all_passed": True,
                    "critical_scenarios_passed": True,
                    "decision_pass_rate": 0.9,
                },
            },
            "reviewer_feedback": "looks good",
        }
        monkeypatch.setattr(
            pipeline,
            "load_strict_pipeline_checkpoint",
            lambda *_args, **_kwargs: checkpoint,
        )
        monkeypatch.setattr(
            pipeline,
            "read_strict_worker_failures",
            lambda *_args, **_kwargs: [
                {
                    "worker_id": 2,
                    "role": "Tuner",
                    "error": "timeout",
                    "failure_type": "llm_timeout",
                    "category": "worker",
                    "gen": 144,
                }
            ],
        )
        monkeypatch.setattr(pipeline, "_quality_complete", lambda _checkpoint: True)

        resp = client.get("/api/pipeline/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert data["next_v"] == 144
        assert data["source_v"] == 143
        assert data["parent2_v"] is None
        assert data["workflow_run_id"] == "workflow-v2"
        assert data["checkpoint_revision"] == 7
        assert data["stage"] == "workers_done"
        assert data["attempts"] == {"generation": 1, "audit": 0, "precommit": 0}
        assert data["rework_counts"]["worker_failure"] == 2

        master = data["master"]
        assert master["plan_present"] is True
        assert master["started"] is True
        assert master["completed"] is True
        assert master["analysis"] == "tighten river overbet"
        assert len(master["tasks"]) == 1
        assert master["tasks"][0]["worker_id"] == 1
        assert master["tasks"][0]["skill_layer"] == "line_template"

        assert data["gates"]["quality"]["complete"] is True
        assert data["gates"]["quality"]["fields"]["decision_pass_rate"] == 0.9
        # review/critic/precommit/official_full are absent -> None
        assert data["gates"]["review"] is None
        assert data["gates"]["critic"] is None
        assert "quality" in data["gate_keys_present"]

        assert data["orchestrator"]["reviewer_feedback"] == "looks good"
        assert len(data["worker_failures"]) == 1
        assert data["worker_failures"][0]["worker_id"] == 2
        assert data["worker_failures"][0]["record_state"] == "historical"
        assert data["worker_failures"][0]["current_blocker"] is False

    def test_critic_advisory_completeness_uses_exact_field_chain(self, client, monkeypatch):
        # critic.approved alone is NOT enough; schema_valid + llm_invoked +
        # critic_llm_executed must all be true and llm_failed/parse_failed absent.
        from server.routes import pipeline

        base = {
            "evaluation_epoch": "national_tcp_policy_v1",
            "next_v": 145,
            "source_v": 144,
            "stage": "critic_checked",
            "workflow_run_id": "workflow-v3",
            "gate_results": {},
        }

        def projection_with(critic_gate):
            ckpt = dict(base)
            ckpt["gate_results"] = {"critic": dict(critic_gate)}
            monkeypatch.setattr(
                pipeline,
                "load_strict_pipeline_checkpoint",
                lambda *_args, **_kwargs: ckpt,
            )
            monkeypatch.setattr(
                pipeline, "read_strict_worker_failures", lambda *_a, **_k: []
            )
            return client.get("/api/pipeline/agents").json()

        complete = projection_with({
            "approved": True,
            "schema_valid": True,
            "llm_invoked": True,
            "critic_llm_executed": True,
            "advisory_approved": False,
            "advisory_score": 4,
        })
        assert complete["gates"]["critic"]["complete"] is True
        # advisory verdict stays separate from completion
        assert complete["gates"]["critic"]["fields"]["advisory_approved"] is False

        incomplete = projection_with({
            "approved": True,
            "schema_valid": True,
            "llm_invoked": True,
            # critic_llm_executed missing
        })
        assert incomplete["gates"]["critic"]["complete"] is False

        failed = projection_with({
            "approved": True,
            "schema_valid": True,
            "llm_invoked": True,
            "critic_llm_executed": True,
            "parse_failed": True,
        })
        assert failed["gates"]["critic"]["complete"] is False

    def test_does_not_invent_checkpoint_when_reader_returns_none(self, client, monkeypatch):
        from server.routes import pipeline

        monkeypatch.setattr(
            pipeline,
            "load_strict_pipeline_checkpoint",
            lambda *_args, **_kwargs: None,
        )
        resp = client.get("/api/pipeline/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is False
        # No fabricated identity fields leak when the workflow is unavailable.
        assert "next_v" not in data
        assert "stage" not in data

    def test_direction_audited_master_is_started_not_completed(self):
        from server.routes import pipeline

        view = pipeline._master_view({"stage": "direction_audited"})
        assert view["started"] is True
        assert view["completed"] is False
        assert view["plan_present"] is False

    def test_timeout_lease_preserves_checkpoint_owned_master_high_water(self):
        from server.routes import pipeline

        for stage in ("timed_out", "infra_timed_out"):
            planned = pipeline._master_view({
                "stage": stage,
                "master_plan": {"analysis": "bound plan", "tasks": []},
            })
            assert planned["started"] is True
            assert planned["completed"] is True
            assert planned["plan_present"] is True

            before_master = pipeline._master_view({"stage": stage})
            assert before_master["started"] is False
            assert before_master["completed"] is False
            assert before_master["plan_present"] is False

    def test_worker_failures_use_the_same_checkpoint_object(self, client, monkeypatch):
        from server.routes import pipeline

        checkpoint = {
            "evaluation_epoch": "national_tcp_policy_v1",
            "next_v": 143,
            "source_v": 142,
            "parent2_v": None,
            "stage": "direction_audited",
            "workflow_run_id": "generation:143:same-snapshot",
            "run_id": "143#0",
            "checkpoint_revision": 3,
            "gate_results": {},
        }
        observed = []
        monkeypatch.setattr(pipeline, "load_strict_pipeline_checkpoint", lambda *_a, **_k: checkpoint)

        def failures(*_args, **kwargs):
            observed.append(kwargs.get("checkpoint_snapshot"))
            return []

        monkeypatch.setattr(pipeline, "read_strict_worker_failures", failures)
        assert client.get("/api/pipeline/agents").status_code == 200
        assert observed == [checkpoint]

    def test_gate_projection_is_fixed_allowlist_and_live_payload_is_bounded(self, monkeypatch):
        from server.routes import pipeline

        huge = "secret-receipt-output:" + ("x" * 500_000)
        checkpoint = {
            "evaluation_epoch": "national_tcp_policy_v1",
            "next_v": 144,
            "source_v": 143,
            "parent2_v": None,
            "stage": "verified",
            "workflow_run_id": "generation:144:bounded",
            "run_id": "144#1",
            "checkpoint_revision": 11,
            "master_plan": {
                "analysis": huge,
                "tasks": [
                    {
                        "worker_id": index,
                        "role": huge,
                        "target_files": [huge] * 100,
                    }
                    for index in range(100)
                ],
            },
            "direction_audit": {"status": "passed", "raw_receipt": huge},
            "infra_failure": {"component": "worker", "raw_status": huge},
            "gate_results": {
                name: {
                    "passed": True,
                    "approved": True,
                    "all_passed": True,
                    "critical_scenarios_passed": True,
                    "status": {"raw": huge},
                    "receipt": huge,
                    "stdout": huge,
                }
                for name in pipeline._GATE_FIELD_ALLOWLIST
            },
        }
        for helper in (
            "_quality_complete",
            "_review_complete",
            "_critic_advisory_complete",
            "_precommit_complete",
            "_official_full_complete",
        ):
            monkeypatch.setattr(pipeline, helper, lambda _checkpoint: True)

        projection = pipeline._build_agents_projection(
            checkpoint,
            [{"error": huge, "role": huge}] * 100,
        )
        encoded = json.dumps(projection, ensure_ascii=False).encode("utf-8")

        assert len(encoded) < pipeline._MAX_AGENT_RESPONSE_BYTES
        assert projection["master"]["task_total"] == 100
        assert len(projection["master"]["tasks"]) == pipeline._MAX_AGENT_TASKS
        assert projection["master"]["tasks_truncated"] is True
        assert len(projection["worker_failures"]) == pipeline._MAX_AGENT_FAILURES
        assert projection["worker_failures_truncated"] is True
        for name, gate in projection["gates"].items():
            assert set(gate["fields"]) <= set(pipeline._GATE_FIELD_ALLOWLIST[name])
            assert "status" not in gate["fields"]
            assert "receipt" not in gate["fields"]
            assert "stdout" not in gate["fields"]
        assert huge not in encoded.decode("utf-8")

    def test_repair_stages_invalidate_old_gates_without_revalidating_them(self):
        from server.routes import pipeline

        def must_not_run(_checkpoint):
            raise AssertionError("historical gate was revalidated as current")

        for stage in ("repair_planned", "rework_running"):
            view = pipeline._gate_view(
                {
                    "stage": stage,
                    "gate_results": {"quality": {"all_passed": True}},
                },
                "quality",
                complete_fn=must_not_run,
            )
            assert view == {
                "name": "quality",
                "present": True,
                "authority_state": "historical_invalidated",
                "complete": False,
                "fields": {"all_passed": True},
            }

    def test_agents_observer_cache_is_bounded_and_revision_invalidated(
        self, client, monkeypatch, tmp_path
    ):
        from server.routes import pipeline

        checkpoint_file = tmp_path / "pipeline_state.json"
        failures_file = tmp_path / "worker_failures.jsonl"
        checkpoint_file.write_text("revision-1", encoding="utf-8")
        failures_file.write_text("", encoding="utf-8")
        state = {"revision": 1}
        calls = []

        def load(*_args, **_kwargs):
            calls.append(state["revision"])
            return {
                "evaluation_epoch": "national_tcp_policy_v1",
                "next_v": 143,
                "source_v": 142,
                "parent2_v": None,
                "stage": "direction_audited",
                "workflow_run_id": f"generation:143:workflow-v{state['revision']}",
                "run_id": "143#0",
                "checkpoint_revision": state["revision"],
                "gate_results": {},
            }

        monkeypatch.setattr(pipeline, "PIPELINE_STATE_FILE", checkpoint_file)
        monkeypatch.setattr(pipeline, "WORKER_FAILURES_FILE", failures_file)
        monkeypatch.setattr(pipeline, "load_strict_pipeline_checkpoint", load)
        monkeypatch.setattr(pipeline, "read_strict_worker_failures", lambda *_a, **_k: [])
        pipeline._AGENTS_OBSERVER_CACHE.clear()

        first = client.get("/api/pipeline/agents").json()
        second = client.get("/api/pipeline/agents").json()
        assert first == second
        assert calls == [1]

        state["revision"] = 2
        checkpoint_file.write_text("revision-2-is-different", encoding="utf-8")
        third = client.get("/api/pipeline/agents").json()
        assert third["checkpoint_revision"] == 2
        assert calls == [1, 2]

    def test_agents_validation_is_offloaded_from_concurrent_control_health(
        self, monkeypatch
    ):
        from server.routes import control, pipeline

        def slow_checkpoint(*_args, **_kwargs):
            time.sleep(0.20)
            return None

        monkeypatch.setattr(pipeline, "load_strict_pipeline_checkpoint", slow_checkpoint)
        monkeypatch.setattr(pipeline, "read_strict_worker_failures", lambda *_a, **_k: [])
        monkeypatch.setattr(control, "_control_health_snapshot", lambda: {"overall": "healthy"})
        pipeline._AGENTS_OBSERVER_CACHE.clear()

        async def exercise():
            agents = asyncio.create_task(pipeline.pipeline_agents())
            await asyncio.sleep(0.01)
            started = time.monotonic()
            health = await asyncio.wait_for(control.control_health(), timeout=0.15)
            elapsed = time.monotonic() - started
            projection = await agents
            return health, elapsed, projection

        health, elapsed, projection = asyncio.run(exercise())
        assert health == {"overall": "healthy"}
        assert elapsed < 0.15
        assert projection["available"] is False

    def test_failure_projection_preserves_recovery_fields_but_marks_jsonl_history(self):
        from server.routes import pipeline

        checkpoint = {
            "evaluation_epoch": "national_tcp_policy_v1",
            "next_v": 144,
            "source_v": 143,
            "parent2_v": None,
            "stage": "quality_passed",
            "workflow_run_id": "generation:144:failure-fields",
            "run_id": "144#1",
            "checkpoint_revision": 12,
            "gate_results": {},
            "infra_failure": {
                "schema_version": 1,
                "failure_class": "infrastructure",
                "component": "reviewer_llm",
                "code": "reviewer_llm_unavailable",
                "owner_tool": "run_review",
                "resume_stage": "quality_passed",
                "attempt": 2,
                "max_attempts": 3,
                "retryable": True,
                "exhausted": False,
                "action": "retry_same_tool",
                "identity_digest": "a" * 64,
                # Nested/high-volume fields remain excluded.
                "issues": ["provider timeout"],
                "metadata": {"raw": "must-not-leak"},
            },
        }
        projection = pipeline._build_agents_projection(
            checkpoint,
            [{
                "worker_id": 7,
                "role": "Policy worker",
                "error": "one historical failure",
                "failure_type": "llm_timeout",
                "category": "worker",
                "gen": 144,
                "timestamp": 1_784_000_000.5,
            }],
        )

        infra = projection["orchestrator"]["infra_failure"]
        assert infra == {
            "schema_version": 1,
            "failure_class": "infrastructure",
            "component": "reviewer_llm",
            "code": "reviewer_llm_unavailable",
            "owner_tool": "run_review",
            "resume_stage": "quality_passed",
            "attempt": 2,
            "max_attempts": 3,
            "retryable": True,
            "exhausted": False,
            "action": "retry_same_tool",
            "identity_digest": "a" * 64,
        }
        row = projection["worker_failures"][0]
        assert row["timestamp"] == 1_784_000_000.5
        assert row["record_state"] == "historical"
        assert row["current_blocker"] is False

