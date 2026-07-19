"""Tests for /api/pipeline/agents — structured agent activity projection."""

import json


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
        assert master["stage_reached"] is True
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
