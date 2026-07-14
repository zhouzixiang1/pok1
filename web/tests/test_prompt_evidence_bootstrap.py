"""Protocol-bootstrap prompt evidence must not reopen historical sidecars."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace


POISON = {
    "strength": "POISON_WR_91_H2H_v991",
    "lesson": "POISON_EXHAUSTED_LESSON_v992",
    "failure": "POISON_WORKER_FAILURE_v993",
    "guardian": "POISON_GUARDIAN_v994",
    "eval": "POISON_EVAL_ROUND_v995",
    "spotlight": "POISON_SPOTLIGHT_G9H9",
    "official": "POISON_OFFICIAL_CERT_PROSE_v996",
}


class _UI:
    def clear_io(self):
        pass

    def log_history(self, *_args, **_kwargs):
        pass

    def log_io(self, *_args, **_kwargs):
        pass

    def update_cost(self, *_args, **_kwargs):
        pass


def _bootstrap(next_v=155, source_v=142):
    from prompt_evidence import build_protocol_bootstrap_prompt_evidence

    receipt = {"receipt_digest": "a" * 64, "mode": "legacy_strategy_migration"}
    envelope = build_protocol_bootstrap_prompt_evidence(
        next_v=next_v,
        source_v=source_v,
        protocol_bootstrap_receipt=receipt,
    )
    checkpoint = {
        "next_v": next_v,
        "source_v": source_v,
        "stage": "direction_audited",
        "workflow_run_id": f"generation-v{next_v}",
        "checkpoint_revision": 3,
        "audit_context": {
            "protocol_bootstrap": receipt,
            "prompt_evidence": envelope,
        },
    }
    return receipt, envelope, checkpoint


def _assert_no_poison(text):
    rendered = str(text)
    for marker in POISON.values():
        assert marker not in rendered


def test_bootstrap_envelope_is_empty_digest_bound_and_tamper_fails_closed():
    from prompt_evidence import (
        PROMPT_EVIDENCE_SECTIONS,
        is_protocol_bootstrap_prompt_evidence,
        resolve_prompt_evidence,
        validate_prompt_evidence_envelope,
    )

    receipt, envelope, checkpoint = _bootstrap()
    assert validate_prompt_evidence_envelope(
        envelope,
        next_v=155,
        source_v=142,
        protocol_bootstrap_receipt=receipt,
    ) == []
    assert set(envelope["sections"]) == set(PROMPT_EVIDENCE_SECTIONS)
    assert set(envelope["sections"].values()) == {""}

    tampered = json.loads(json.dumps(envelope))
    tampered["sections"]["lessons"] = POISON["lesson"]
    checkpoint["audit_context"]["prompt_evidence"] = tampered
    repaired = resolve_prompt_evidence(checkpoint=checkpoint)
    assert is_protocol_bootstrap_prompt_evidence(repaired)
    assert repaired["sections"]["lessons"] == ""
    assert repaired["envelope_digest"] == envelope["envelope_digest"]


def test_orchestrator_bootstrap_context_ignores_all_historical_inputs(monkeypatch):
    import eval_rounds
    import evolution_core
    import orchestrator_context

    _receipt, envelope, checkpoint = _bootstrap()
    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(evolution_core, "get_active_bots", lambda: ["national_v142"])
    monkeypatch.setattr(
        orchestrator_context,
        "_load_guardian_insights",
        lambda **_kwargs: POISON["guardian"],
    )

    class _ForbiddenEvalRound:
        def __init__(self):
            raise AssertionError("eval_rounds must not be opened during bootstrap")

    monkeypatch.setattr(eval_rounds, "EvalRoundManager", _ForbiddenEvalRound)
    ctx = SimpleNamespace(
        current_v=142,
        next_v=155,
        strategy="protocol_migration_bootstrap",
        source_v=142,
        crossover_parents=(),
        stagnation_info=POISON["strength"],
        match_analysis=POISON["eval"],
        replay_spotlight=POISON["spotlight"],
        performance_verification=POISON["official"],
        battle_experience=POISON["lesson"],
        prompt_evidence=envelope,
    )
    rendered = orchestrator_context._build_context(gen_ctx=ctx)
    _assert_no_poison(rendered)
    assert "PROTOCOL BOOTSTRAP PROMPT EVIDENCE POLICY" in rendered


def test_worker_bootstrap_context_never_reads_lessons_or_failures(monkeypatch):
    import agent_workers

    _receipt, envelope, checkpoint = _bootstrap()
    monkeypatch.setattr(
        agent_workers,
        "_extract_exhausted_block",
        lambda: POISON["lesson"],
    )
    monkeypatch.setattr(
        agent_workers,
        "_load_recent_failures",
        lambda _n=5: [{"error": POISON["failure"]}],
    )
    context = agent_workers.build_worker_execution_context(
        prompt_evidence=envelope,
        checkpoint=checkpoint,
    )
    assert context["exhausted_block"] == ""
    assert context["recent_failures"] == []
    assert context["prompt_evidence"] == envelope
    _assert_no_poison(context)


def test_bootstrap_auditors_return_before_historical_loaders(monkeypatch):
    import audit_agents
    import direction_auditor

    _receipt, envelope, _checkpoint = _bootstrap()

    async def forbidden_query(*_args, **_kwargs):
        raise AssertionError("bootstrap auditor must not call the LLM")

    monkeypatch.setattr(direction_auditor, "run_claude_query", forbidden_query)
    monkeypatch.setattr(audit_agents, "run_claude_query", forbidden_query)
    direction = asyncio.run(
        direction_auditor._run_direction_audit(
            142, _UI(), prompt_evidence=envelope
        )
    )
    plan = asyncio.run(
        audit_agents._run_master_plan_audit(
            {"analysis": POISON["lesson"], "tasks": []},
            142,
            _UI(),
            next_v=155,
            prompt_evidence=envelope,
        )
    )
    pool = asyncio.run(
        audit_agents._run_experience_pool_audit(
            POISON["lesson"],
            {"national_v991": {"r": 9999}},
            _UI(),
            prompt_evidence=envelope,
        )
    )
    guardian = asyncio.run(
        audit_agents._run_regression_guardian(
            155,
            142,
            {"history": POISON["guardian"]},
            POISON["failure"],
            _UI(),
            prompt_evidence=envelope,
        )
    )
    assert direction["protocol_bootstrap_no_strength"] is True
    assert plan["protocol_bootstrap_no_strength"] is True
    assert pool["protocol_bootstrap_no_strength"] is True
    assert guardian["protocol_bootstrap_no_strength"] is True
    _assert_no_poison([direction, plan, pool, guardian])


def test_bootstrap_critic_prompt_omits_calibration_history_and_sidecars(
    monkeypatch,
    tmp_path,
):
    import agent_review

    _receipt, envelope, _checkpoint = _bootstrap()
    captured = {}

    async def fake_query(prompt, *_args, **kwargs):
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return json.dumps({
            "score": 6,
            "approved": True,
            "strategic_assessment": "current diff only",
            "evidence": {
                "h2h_weaknesses": [],
                "experience_pool_refs": [],
                "diff_refs": ["strategy.py"],
            },
            "feedback": "",
            "local_optima_warning": False,
        }), 0.0, {}

    monkeypatch.setattr(agent_review, "run_claude_query", fake_query)
    monkeypatch.setattr(agent_review, "get_logs_dir", lambda _v: tmp_path)
    monkeypatch.setattr(agent_review, "RESULTS_DIR", tmp_path)
    (tmp_path / "critic_calibration.jsonl").write_text(
        json.dumps({"rating_delta": -999, "note": POISON["strength"]}) + "\n"
    )
    result = asyncio.run(agent_review._run_critic(
        155,
        142,
        json.dumps({"tasks": [{"worker_prompt": "current change"}]}),
        _UI(),
        prompt_evidence=envelope,
    ))
    assert result["score"] == 6
    assert captured["kwargs"]["deny_live_prompt_evidence"] is True
    assert "BOOTSTRAP SCORING OVERRIDE" in captured["prompt"]
    assert "do not inspect historical git commits" in captured["prompt"]
    _assert_no_poison(captured["prompt"])


def test_bootstrap_archivist_sanitizes_snapshot_and_skips_pool_consolidation(
    monkeypatch,
    tmp_path,
):
    import experience_archivist

    _receipt, envelope, _checkpoint = _bootstrap()
    captured = {}

    async def fake_query(prompt, *_args, **kwargs):
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return json.dumps({
            "generation_assessment": "neutral",
            "archive_notes": "current diff archived",
            "experience_updates": [],
            "strategic_advice": "",
        }), 0.0, {}

    monkeypatch.setattr(experience_archivist, "run_claude_query", fake_query)
    monkeypatch.setattr(experience_archivist, "get_logs_dir", lambda _v: tmp_path)
    monkeypatch.setattr(experience_archivist, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(experience_archivist, "EXPERIENCE_FILE", tmp_path / "experience_pool.md")
    monkeypatch.setattr(experience_archivist, "PROMPTS_DIR", Path(__file__).parents[1] / "core" / "prompts")
    experience_archivist.ARCHIVE_DIR.mkdir()
    (experience_archivist.ARCHIVE_DIR / "v154.json").write_text(
        json.dumps({"rating": POISON["strength"]})
    )
    experience_archivist.EXPERIENCE_FILE.write_text(POISON["lesson"])
    snapshot = {
        "version": 155,
        "source_v": 142,
        "diff_stats_raw": "1 file changed",
        "rating": POISON["strength"],
        "critic_data": POISON["guardian"],
        "precommit_eval": POISON["eval"],
        "official_feedback": POISON["official"],
        "prompt_evidence": envelope,
    }
    result = asyncio.run(experience_archivist._run_archivist_analysis(
        155,
        142,
        snapshot,
        _UI(),
        prompt_evidence=envelope,
    ))
    assert result["generation_assessment"] == "neutral"
    assert captured["kwargs"]["deny_live_prompt_evidence"] is True
    _assert_no_poison(captured["prompt"])

    before = experience_archivist.EXPERIENCE_FILE.read_text()
    asyncio.run(experience_archivist._consolidate_experience_pool(
        _UI(), prompt_evidence=envelope
    ))
    assert experience_archivist.EXPERIENCE_FILE.read_text() == before


def test_prompt_evidence_read_guard_blocks_global_sidecars(tmp_path):
    import llm_query

    assert llm_query._master_live_evidence_read_violation(
        "Read", {"file_path": "web/core/experience_pool.md"}
    )
    assert llm_query._master_live_evidence_read_violation(
        "Bash", {"command": "sed -n '1,20p' web/core/results/regression_guardian.jsonl"}
    )
    assert llm_query._master_live_evidence_read_violation(
        "Read", {"file_path": "official_certificates/national_v99.json"}
    )
    assert llm_query._master_live_evidence_read_violation(
        "Bash",
        {"command": "python -c \"open('web/core/experience_pool.md').read()\""},
        deny_all_prompt_evidence=True,
    )


def test_archive_sort_is_numeric_and_empty_spotlight_clears_manifest(
    monkeypatch,
    tmp_path,
):
    import generation_scheduler
    import replay_spotlight

    paths = [Path("v98.json"), Path("v142.json"), Path("v9.json")]
    ordered = sorted(
        paths,
        key=generation_scheduler._archive_version_sort_key,
        reverse=True,
    )
    assert [path.name for path in ordered] == ["v142.json", "v98.json", "v9.json"]

    fake_core = tmp_path / "core"
    results = fake_core / "results"
    replays = tmp_path / "replays"
    results.mkdir(parents=True)
    replays.mkdir()
    manifest = results / "spotlight_manifest.json"
    manifest.write_text(json.dumps({
        "bot": "national_v121",
        "citations": [{"id": POISON["spotlight"]}],
    }))
    monkeypatch.setattr(replay_spotlight, "__file__", str(fake_core / "replay_spotlight.py"))
    assert replay_spotlight.find_critical_hands(
        "national_v155",
        str(replays),
        allowed_replay_ids=[],
    ) == "No replay files found."
    published = json.loads(manifest.read_text())
    assert published == {"bot": "national_v155", "citations": []}
