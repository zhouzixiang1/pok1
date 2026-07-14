"""The strict TCP epoch ignores every retired free-form strategy-memory file."""

from pathlib import Path
from types import SimpleNamespace


def test_retired_markdown_facility_has_no_active_module_or_prompt():
    import evolution_infra
    import tool_planning

    core = Path(evolution_infra.CORE_DIR)
    for relative in (
        "experience_pool.md",
        "experience_pool.py",
        "experience_archivist.py",
        "experience_attribution.py",
        "battle_experience.py",
        "prompts/experience_consolidator.md",
        "prompts/experience_pool_audit.md",
        "prompts/battle_experience_incremental.md",
        "prompts/battle_experience_update.md",
    ):
        assert not (core / relative).exists()

    assert not hasattr(evolution_infra, "EXPERIENCE_FILE")
    assert not hasattr(evolution_infra, "CROSS_GEN_EXHAUSTED_HISTORY")
    assert not hasattr(tool_planning, "_extract_exhausted_keywords")
    assert not hasattr(tool_planning, "_build_cross_gen_constraint_block")


def test_active_scope_runtime_and_ui_have_no_retired_experience_surface():
    import inspect

    import agent_master
    import evaluation_contract
    import evolution_scope
    import generation_scheduler

    retired_paths = (
        "web/core/battle_experience.py",
        "web/core/prompts/battle_experience_incremental.md",
        "web/core/prompts/battle_experience_update.md",
        "web/core/experience_pool.md",
        "web/core/experience_pool.py",
    )
    for module in (evaluation_contract, evolution_scope):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for retired in retired_paths:
            assert retired not in source

    project = Path(evolution_scope.__file__).resolve().parents[2]
    assert not (project / "web/frontend/src/pages/ExperiencePool.tsx").exists()
    for relative in (
        "web/frontend/src/App.tsx",
        "web/frontend/src/layout/AppSidebar.tsx",
        "web/frontend/src/api/client.ts",
        "web/frontend/src/api/types.ts",
        "web/server/routes/ratings.py",
    ):
        source = (project / relative).read_text(encoding="utf-8")
        assert "/experience" not in source
        assert "ExperiencePool" not in source
        assert "NativeExperience" not in source

    for relative in (
        "web/core/generation_scheduler.py",
        "web/core/tool_planning.py",
        "web/core/elo_daemon.py",
        "web/core/observe_policy.py",
        "web/core/api_concurrency.py",
    ):
        assert "battle_experience" not in (
            project / relative
        ).read_text(encoding="utf-8")

    assert "battle_experience" not in inspect.signature(
        agent_master._run_master_analysis
    ).parameters
    assert "battle_experience" not in generation_scheduler.GenerationContext.__annotations__

    # Do not leave an unwired replacement facility in the active tree. Current
    # identity-bound evidence is published through the frozen action-stat
    # snapshot; a future lesson store needs a complete reachable call chain.
    assert not (project / "web/core/battle_memory.py").exists()


def test_legacy_files_cannot_enter_worker_or_orchestrator_prompt(tmp_path, monkeypatch):
    import agent_workers
    import evolution_core
    import evolution_infra
    import orchestrator_context

    sentinel = "LEGACY_FREE_FORM_STRATEGY_MUST_NOT_APPEAR"
    (tmp_path / "experience_pool.md").write_text(sentinel, encoding="utf-8")
    results = tmp_path / "results"
    results.mkdir()
    (results / "regression_guardian.jsonl").write_text(
        '{"diagnosis":"' + sentinel + '"}\n', encoding="utf-8"
    )
    (results / "battle_experience.md").write_text(sentinel, encoding="utf-8")
    (results / "battle_lessons.jsonl").write_text(
        '{"text":"' + sentinel + '"}\n', encoding="utf-8"
    )
    (results / "worker_failures.jsonl").write_text(
        '{"error":"' + sentinel + '"}\n', encoding="utf-8"
    )
    (results / "eval_rounds.jsonl").write_text(
        '{"summary":"' + sentinel + '"}\n', encoding="utf-8"
    )

    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: None)

    assert not hasattr(agent_workers, "build_worker_execution_context")
    assert not hasattr(agent_workers, "_load_recent_failures")

    generation = SimpleNamespace(
        current_v=143,
        next_v=144,
        source_v=143,
        strategy="master",
        crossover_parents=None,
        stagnation_info="",
        match_analysis="",
        replay_spotlight="",
        performance_verification="",
    )
    prompt_context = orchestrator_context._build_context(gen_ctx=generation)
    assert sentinel not in prompt_context
