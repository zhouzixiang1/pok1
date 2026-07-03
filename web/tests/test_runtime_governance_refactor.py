import json
import sys
from pathlib import Path


CORE = Path(__file__).resolve().parents[1] / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))


def test_battle_experience_prompt_compaction(monkeypatch):
    import battle_experience as be

    monkeypatch.setattr(be, "BATTLE_PROMPT_CURRENT_BUDGET", 1200)
    monkeypatch.setattr(be, "BATTLE_PROMPT_NEW_DATA_BUDGET", 1400)
    monkeypatch.setattr(be, "BATTLE_PROMPT_MATCH_SECTION_BUDGET", 500)

    current = "## OLD\n" + ("old line\n" * 1000)
    new = "\n\n---\n\n".join("match section " + ("x" * 1000) for _ in range(8))

    current_prompt, new_prompt = be._prepare_prompt_inputs(current, new, mode="test")

    assert len(current_prompt) <= 1200
    assert len(new_prompt) <= 1400
    assert "omitted" in current_prompt
    assert "omitted" in new_prompt


def test_battle_experience_skips_oversized_llm_prompt(monkeypatch):
    import battle_experience as be

    monkeypatch.setattr(be, "BATTLE_EXPERIENCE_LLM_ENABLED", True)
    monkeypatch.setattr(be, "BATTLE_PROMPT_CURRENT_BUDGET", 1200)
    monkeypatch.setattr(be, "BATTLE_PROMPT_NEW_DATA_BUDGET", 1400)
    monkeypatch.setattr(be, "BATTLE_PROMPT_MATCH_SECTION_BUDGET", 500)
    monkeypatch.setattr(be, "BATTLE_PROMPT_MAX_CHARS", 1000)

    def _should_not_call(_prompt):
        raise AssertionError("oversized battle_experience prompt should not call LLM")

    monkeypatch.setattr(be, "_run_sync_llm_call", _should_not_call)

    result = be._run_llm_incremental(
        "## OLD\n" + ("old line\n" * 1000),
        "\n\n---\n\n".join("match section " + ("x" * 1000) for _ in range(8)),
    )

    assert result is be._NO_EXPERIENCE_UPDATE


def test_battle_experience_llm_is_opt_in(monkeypatch):
    import battle_experience as be

    monkeypatch.setattr(be, "BATTLE_EXPERIENCE_LLM_ENABLED", False)
    monkeypatch.setattr(be, "BATTLE_PROMPT_MAX_CHARS", 100000)

    def _should_not_call(_prompt):
        raise AssertionError("battle_experience LLM should be opt-in")

    monkeypatch.setattr(be, "_run_sync_llm_call", _should_not_call)

    result = be._run_llm_incremental("short", "short match summary")

    assert result is be._NO_EXPERIENCE_UPDATE


def test_literature_probe_cache_roundtrip(tmp_path, monkeypatch):
    import evolution_infra
    import tool_planning

    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    payload = {
        "next_v": 300,
        "source_v": 299,
        "proposal": {"claim": "c", "target_fn": "f", "numeric_claim": "+1", "source_url": "u"},
        "candidate_id": "research-1",
        "gated_out": False,
        "reason": "completed",
        "weakness": "vs station",
        "stagnation_info": "flat WR",
    }

    tool_planning._write_literature_probe_cache(300, payload)
    cached = tool_planning._read_literature_probe_cache(
        300,
        source_v=299,
        h2h_weakness="vs station",
        stagnation_info="flat WR",
    )

    assert cached["cached"] is True
    assert cached["candidate_id"] == "research-1"
    assert cached["context_fingerprint"]
    assert "Research Proposal" in cached["inject_text"]


def test_literature_probe_cache_rejects_context_mismatch(tmp_path, monkeypatch):
    import evolution_infra
    import tool_planning

    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    payload = {
        "next_v": 300,
        "source_v": 299,
        "proposal": {"claim": "c", "target_fn": "f", "numeric_claim": "+1", "source_url": "u"},
        "candidate_id": "research-1",
        "gated_out": False,
        "reason": "completed",
        "weakness": "vs station",
        "stagnation_info": "flat WR",
    }

    tool_planning._write_literature_probe_cache(300, payload)

    assert tool_planning._read_literature_probe_cache(
        300,
        source_v=298,
        h2h_weakness="vs station",
        stagnation_info="flat WR",
    ) is None
    assert tool_planning._read_literature_probe_cache(
        300,
        source_v=299,
        h2h_weakness="different weakness",
        stagnation_info="flat WR",
    ) is None


def test_literature_probe_checkpoint_reused_on_resume(tmp_path, monkeypatch):
    import asyncio
    import evolution_infra
    import tool_planning

    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", tmp_path / "pipeline_state.json")
    payload = {
        "next_v": 300,
        "source_v": 299,
        "proposal": {"claim": "c", "target_fn": "f", "numeric_claim": "+1", "source_url": "u"},
        "candidate_id": "research-1",
        "reason": "completed",
        "weakness": "original weakness",
        "stagnation_info": "original stagnation",
        "context_fingerprint": tool_planning._literature_probe_context_fingerprint(
            299, "original weakness", "original stagnation"
        ),
    }
    assert evolution_infra.write_pipeline_checkpoint(
        300, 299, "direction_audited", literature_probe=payload
    )

    result = asyncio.run(tool_planning.run_literature_probe.handler({
        "next_v": 300,
        "source_v": 299,
        "h2h_weakness": "slightly different resumed weakness",
        "stagnation_info": "slightly different resumed stagnation",
    }))
    data = json.loads(result["content"][0]["text"])

    assert data["cached"] is True
    assert data["cache_source"] == "checkpoint"
    assert data["candidate_id"] == "research-1"
    assert data["context_mismatch_reused"] is True


def test_aggregate_negative_ev_blocks_small_wl_edge():
    import tool_eval

    samples = [-1000.0] * 30 + [400.0] * 4
    blockers, payload = tool_eval._aggregate_ev_risk_blockers(
        total_wins=33,
        total_losses=31,
        total_draws=0,
        aggregate_net_chips=samples,
        agg_ci_lower=-1200.0,
        agg_ci_upper=300.0,
    )

    assert any(b["reason"] == "aggregate_negative_chip_ev" for b in blockers)
    assert payload["mean"] < 0


def test_admission_strength_blocks_negative_chip_ev():
    import tool_eval

    samples = [-500.0] * 24
    blockers, payload = tool_eval._admission_strength_blockers(
        n_games=tool_eval.PRECOMMIT_DEFAULT_N_GAMES,
        total_wins=12,
        total_losses=12,
        total_draws=0,
        aggregate_net_chips=samples,
    )

    assert any(b["reason"] == "admission_negative_chip_ev" for b in blockers)
    assert payload["mean"] == -500.0


def test_admission_strength_blocks_low_sample_precommit():
    import tool_eval

    blockers, payload = tool_eval._admission_strength_blockers(
        n_games=tool_eval.PRECOMMIT_DEFAULT_N_GAMES - 1,
        precommit_attempt=2,
        total_wins=8,
        total_losses=0,
        total_draws=0,
        aggregate_net_chips=[1000.0] * 24,
    )

    assert any(b["reason"] == "provisional_low_sample_precommit" for b in blockers)
    assert payload["n_games"] == tool_eval.PRECOMMIT_DEFAULT_N_GAMES - 1


def test_plan_compiler_externalizes_oversized_worker_prompt(tmp_path):
    import plan_compiler

    long_prompt = "Implement this carefully.\n" + ("detail " * 2500)
    plan = {
        "tasks": [
            {
                "worker_id": 1,
                "role": "Algorithmic Logic Architect",
                "target_files": ["strategy.py"],
                "worker_prompt": long_prompt,
            }
        ]
    }

    compiled, meta = plan_compiler.compile_master_plan(
        plan,
        next_v=300,
        target_dir=tmp_path / "claude_v300",
        project_root=tmp_path,
    )

    task = compiled["tasks"][0]
    assert meta["compiled"] is True
    assert task["worker_prompt_compiled"] is True
    assert len(task["worker_prompt"]) < plan_compiler.HARD_WORKER_PROMPT_CHARS
    assert "task_brief_file" in task
    assert (tmp_path / task["task_brief_file"]).exists()
    assert compiled["plan_compiler"]["compiled_tasks"][0]["original_chars"] == len(long_prompt)


def test_scheduler_status_excludes_collected_from_missing():
    import tool_eval

    status = {
        "pending": [],
        "claimed": [],
        "completed": [],
        "missing": ["j1", "j2", "j3"],
        "missing_count": 3,
    }
    normalized = tool_eval._scheduler_status_excluding_collected(
        ["j1", "j2", "j3"],
        status,
        {"j1": {"total": 1}, "j2": {"total": 1}},
    )

    assert normalized["collected_count"] == 2
    assert normalized["missing"] == ["j3"]
    assert normalized["missing_count"] == 1
    assert normalized["missing_unaccounted_count"] == 1
    assert normalized["raw_missing_count"] == 3
    assert normalized["raw_missing_before_collected_count"] == 3


def test_near_cap_core_file_cannot_grow(tmp_path):
    import code_verification

    source = tmp_path / "source"
    child = tmp_path / "child"
    source.mkdir()
    child.mkdir()
    (source / "strategy.py").write_text("x = 1\n" * 2486, encoding="utf-8")
    (child / "strategy.py").write_text("x = 1\n" * 2493, encoding="utf-8")

    _total, oversized = code_verification.check_code_size(child, source_dir=source)

    assert oversized == [("strategy.py", 2493, 2486)]


def test_exhausted_positive_text_ignores_prohibitions():
    import tool_planning

    task = {
        "worker_prompt": (
            "Do NOT reopen choose_raise constant tuning. "
            "Add a new river blocker telemetry hook in postflop.py."
        ),
        "behavior_hypothesis": "Improve blocker telemetry reachability.",
        "prohibited_files": ["constants.py"],
    }
    text = tool_planning._positive_execution_text_from_task(task)

    assert "choose_raise constant tuning" not in text
    assert "blocker telemetry" in text


def test_exhausted_positive_text_strips_refactor_away_clause():
    import tool_planning

    task = {
        "worker_prompt": (
            "TASK: Wire _tier_opp_sizing_directive into choose_raise, "
            "REPLACING rigid plan-label-driven sizing caps. "
            "CHANGE: use per-street opponent bet-size tendency sizing."
        )
    }

    text = tool_planning._positive_execution_text_from_task(task)

    assert "rigid plan-label-driven sizing caps" not in text
    assert "_tier_opp_sizing_directive" in text
    assert "opponent bet-size tendency sizing" in text


def test_repo_state_snapshot_classifies_dirty_untracked_and_protected(monkeypatch, tmp_path):
    import subprocess
    import repo_state

    status = "\n".join([
        "## codex/test...origin/main",
        " M web/core/tool_gates.py",
        "?? bots/claude_v245/",
        "?? web/logs/restart.log",
    ])

    def _fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(_args[0], 0, stdout=status, stderr="")

    monkeypatch.setattr(repo_state.subprocess, "run", _fake_run)

    snapshot = repo_state.git_worktree_snapshot(tmp_path)

    assert snapshot["ok"] is True
    assert snapshot["branch"] == "codex/test...origin/main"
    assert snapshot["dirty_count"] == 1
    assert snapshot["untracked_count"] == 2
    assert snapshot["generated_bot_dirs"] == ["?? bots/claude_v245/"]
    assert len(snapshot["protected_entries"]) == 2


def test_repo_state_log_emits_structured_event(monkeypatch):
    import repo_state

    events = []
    monkeypatch.setattr(repo_state, "_LAST_SNAPSHOT", None)
    monkeypatch.setattr(repo_state, "git_worktree_snapshot", lambda: {"ok": True, "entries": []})

    import system_log

    def _fake_log(event_type, severity, message, data):
        events.append((event_type, severity, message, data))

    monkeypatch.setattr(system_log, "log_system_event", _fake_log)

    payload = repo_state.log_git_worktree_snapshot(
        "repo.worktree_snapshot",
        "snapshot",
        next_v=300,
    )

    assert payload["next_v"] == 300
    assert events == [("repo.worktree_snapshot", "info", "snapshot", payload)]


def test_repo_state_delta_emits_branch_and_worktree_events(monkeypatch):
    import repo_state

    events = []
    snapshots = iter([
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "aaa111",
            "dirty_count": 0,
            "untracked_count": 0,
            "entry_count": 0,
            "entries": [],
        },
        {
            "ok": True,
            "branch": "codex/refactor",
            "head": "bbb222",
            "dirty_count": 1,
            "untracked_count": 1,
            "entry_count": 2,
            "entries": [" M web/core/tool_gates.py", "?? bots/claude_v251/"],
        },
    ])
    monkeypatch.setattr(repo_state, "_LAST_SNAPSHOT", None)
    monkeypatch.setattr(repo_state, "git_worktree_snapshot", lambda: next(snapshots))

    import system_log

    def _fake_log(event_type, severity, message, data):
        events.append((event_type, severity, message, data))

    monkeypatch.setattr(system_log, "log_system_event", _fake_log)

    repo_state.log_git_worktree_snapshot("repo.worktree_snapshot", "before", emit_delta=True)
    repo_state.log_git_worktree_snapshot("repo.worktree_snapshot", "after", emit_delta=True)

    event_types = [event[0] for event in events]
    assert "repo.worktree_baseline" in event_types
    assert "repo.branch_changed" in event_types
    assert "repo.head_changed" in event_types
    assert "repo.worktree_changed" in event_types
    branch_event = next(event for event in events if event[0] == "repo.branch_changed")
    assert branch_event[3]["previous_branch"] == "main...origin/main"
    assert branch_event[3]["current_branch"] == "codex/refactor"
    worktree_event = next(event for event in events if event[0] == "repo.worktree_changed")
    assert " M web/core/tool_gates.py" in worktree_event[3]["new_dirty_entries"]
    assert "?? bots/claude_v251/" in worktree_event[3]["new_generated_bot_dirs"]
    assert "?? bots/claude_v251/" not in worktree_event[3]["new_protected_entries"]


def test_runtime_guard_allows_current_candidate_dir(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "abc123",
            "entries": ["?? bots/claude_v300/"],
        },
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "abc123",
            "entries": ["?? bots/claude_v300/"],
        },
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_master",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["guard"] == "ok"


def test_runtime_guard_cleanup_tools_infer_authoritative_next_v(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "abc123",
            "entries": ["?? bots/claude_v301/"],
        },
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "abc123",
            "entries": ["?? bots/claude_v301/"],
        },
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: None)

    import evolution_infra
    monkeypatch.setattr(evolution_infra, "compute_next_generation_v", lambda: 301)

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard("cleanup_incomplete", {})

    assert ok is True
    assert payload["candidate_v"] == 301


def test_runtime_guard_blocks_unexpected_system_dirty(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "abc123",
        "entries": [" M web/core/tool_gates.py", "?? bots/claude_v300/"],
    }
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "execute_workers",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["reason"] == "unexpected_worktree_entries"
    assert " M web/core/tool_gates.py" in payload["unexpected_entries"]


def test_runtime_guard_blocks_truncated_snapshot(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "abc123",
        "entries": ["?? bots/claude_v300/"] * 40,
        "entry_count": 41,
        "truncated": True,
    }
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "execute_workers",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["reason"] == "worktree_snapshot_truncated"
    assert payload["entry_count"] == 41


def test_runtime_guard_blocks_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "new456",
        "entries": ["?? bots/claude_v300/"],
    }
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "old123"})

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_quality_gates",
        {"version": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["reason"] == "head_changed_during_generation"
    assert payload["baseline_head"] == "old123"
    assert payload["current_head"] == "new456"


def test_runtime_guard_uses_persisted_checkpoint_baseline_after_restart(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/claude_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/claude_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: {
        "next_v": 300,
        "source_v": 299,
        "repo_baseline": {"head": "old123", "branch": "main...origin/main", "captured_stage": "prepared"},
    })

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_quality_gates",
        {"version": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["reason"] == "head_changed_during_generation"
    assert payload["baseline_source"] == "checkpoint"
    assert payload["baseline_head"] == "old123"


def test_runtime_guard_allows_execute_workers_after_repair_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/claude_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/claude_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: {
        "next_v": 300,
        "source_v": 299,
        "stage": "quality_failed",
        "repo_baseline": {"head": "old123", "branch": "main...origin/main", "captured_stage": "prepared"},
    })

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "execute_workers",
        {"version": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["head_drift_repair_allowed"] is True
    assert payload["baseline_head"] == "old123"
    assert payload["current_head"] == "new456"


def test_runtime_guard_allows_quality_after_workers_done_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/claude_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/claude_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: {
        "next_v": 300,
        "source_v": 299,
        "stage": "workers_done",
        "repo_baseline": {"head": "old123", "branch": "main...origin/main", "captured_stage": "workers_done"},
    })

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_quality_gates",
        {"version": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["head_drift_resume_allowed"] is True
    assert payload["head_drift_repair_allowed"] is False
    assert payload["resume_kind"] == "gate"
    assert payload["stage"] == "workers_done"
    assert payload["baseline_head"] == "old123"
    assert payload["current_head"] == "new456"


def test_runtime_guard_allows_review_after_post_quality_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/claude_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/claude_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: {
        "next_v": 300,
        "source_v": 299,
        "stage": "quality_passed",
        "repo_baseline": {"head": "old123", "branch": "main...origin/main", "captured_stage": "quality_passed"},
    })

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_review",
        {"version": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["head_drift_resume_allowed"] is True
    assert payload["head_drift_repair_allowed"] is False
    assert payload["resume_kind"] == "post_quality"
    assert payload["baseline_head"] == "old123"
    assert payload["current_head"] == "new456"


def test_runtime_guard_allows_commit_after_verified_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/claude_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/claude_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: {
        "next_v": 300,
        "source_v": 299,
        "stage": "verified",
        "repo_baseline": {"head": "old123", "branch": "main...origin/main", "captured_stage": "quality_passed"},
    })

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "commit_bot",
        {"version": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["head_drift_resume_allowed"] is True
    assert payload["head_drift_repair_allowed"] is False
    assert payload["resume_kind"] == "post_quality"
    assert payload["stage"] == "verified"
    assert payload["baseline_head"] == "old123"
    assert payload["current_head"] == "new456"


def test_runtime_guard_blocks_clean_branch_drift_without_auto_checkout(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshot = {"ok": True, "branch": "codex/refactor", "head": "abc123", "entries": ["?? bots/claude_v300/"]}
    commands = []
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})
    monkeypatch.setattr(tool_runtime_guard, "_run_git", lambda *args: commands.append(args))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_literature_probe",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["reason"] == "branch_drift"
    assert payload["expected_branch"] == "main"
    assert commands == []


def test_write_pipeline_checkpoint_persists_repo_baseline(tmp_path, monkeypatch):
    import evolution_infra
    import repo_state

    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", tmp_path / "pipeline_state.json")
    monkeypatch.setattr(repo_state, "git_worktree_snapshot", lambda: {
        "branch": "main...origin/main",
        "head": "abc123",
        "entry_count": 1,
        "dirty_count": 0,
        "untracked_count": 1,
        "entries": ["?? bots/claude_v300/"],
        "truncated": False,
    })

    assert evolution_infra.write_pipeline_checkpoint(300, 299, "prepared") is True
    state = evolution_infra.read_pipeline_checkpoint()

    assert state["repo_baseline"]["head"] == "abc123"
    assert state["repo_baseline"]["branch"] == "main...origin/main"
    assert state["repo_baseline"]["captured_stage"] == "prepared"


def test_write_pipeline_checkpoint_refreshes_baseline_on_rework(tmp_path, monkeypatch):
    import evolution_infra
    import repo_state

    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", tmp_path / "pipeline_state.json")
    snapshots = iter([
        {
            "branch": "main...origin/main",
            "head": "old123",
            "entry_count": 1,
            "dirty_count": 0,
            "untracked_count": 1,
            "entries": ["?? bots/claude_v300/"],
            "truncated": False,
        },
        {
            "branch": "main...origin/main",
            "head": "new456",
            "entry_count": 1,
            "dirty_count": 0,
            "untracked_count": 1,
            "entries": ["?? bots/claude_v300/"],
            "truncated": False,
        },
    ])
    monkeypatch.setattr(repo_state, "git_worktree_snapshot", lambda: next(snapshots))

    assert evolution_infra.write_pipeline_checkpoint(300, 299, "quality_failed") is True
    assert evolution_infra.write_pipeline_checkpoint(300, 299, "repair_planned") is True
    state = evolution_infra.read_pipeline_checkpoint()

    assert state["repo_baseline"]["head"] == "new456"
    assert state["repo_baseline"]["captured_stage"] == "repair_planned"


def test_checkpoint_recovery_diagnostics_allows_workers_done_head_mismatch(tmp_path):
    import pipeline_recovery

    (tmp_path / "bots" / "claude_v257").mkdir(parents=True)
    checkpoint = {
        "next_v": 257,
        "source_v": 197,
        "stage": "workers_done",
        "repo_baseline": {"branch": "main", "head": "old123"},
    }
    snapshot = {"ok": True, "branch": "main", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["active"] is True
    assert diag["recoverable"] is True
    assert "repo_baseline_head_mismatch" not in diag["issues"]
    assert "repo_baseline_head_mismatch_gate_resume" in diag["warnings"]
    assert diag["repo"]["baseline_head_mismatch_allowed"] is True
    assert diag["target"]["exists"] is True


def test_checkpoint_recovery_diagnostics_blocks_early_repo_head_mismatch(tmp_path):
    import pipeline_recovery

    (tmp_path / "bots" / "claude_v257").mkdir(parents=True)
    checkpoint = {
        "next_v": 257,
        "source_v": 197,
        "stage": "prepared",
        "repo_baseline": {"branch": "main", "head": "old123"},
    }
    snapshot = {"ok": True, "branch": "main", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["active"] is True
    assert diag["recoverable"] is False
    assert "repo_baseline_head_mismatch" in diag["issues"]
    assert diag["target"]["exists"] is True


def test_checkpoint_recovery_diagnostics_allows_repair_head_mismatch(tmp_path):
    import pipeline_recovery

    (tmp_path / "bots" / "claude_v269").mkdir(parents=True)
    checkpoint = {
        "next_v": 269,
        "source_v": 237,
        "stage": "quality_failed",
        "repo_baseline": {"branch": "main", "head": "old123"},
    }
    snapshot = {"ok": True, "branch": "main...origin/main", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["active"] is True
    assert diag["recoverable"] is True
    assert "repo_baseline_head_mismatch" not in diag["issues"]
    assert "repo_baseline_head_mismatch_repair_resume" in diag["warnings"]
    assert diag["repo"]["baseline_head_mismatch_allowed"] is True


def test_checkpoint_recovery_diagnostics_allows_post_quality_head_mismatch(tmp_path):
    import pipeline_recovery

    (tmp_path / "bots" / "claude_v269").mkdir(parents=True)
    checkpoint = {
        "next_v": 269,
        "source_v": 237,
        "stage": "quality_passed",
        "repo_baseline": {"branch": "main", "head": "old123"},
    }
    snapshot = {"ok": True, "branch": "main...origin/main", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["active"] is True
    assert diag["recoverable"] is True
    assert "repo_baseline_head_mismatch" not in diag["issues"]
    assert "repo_baseline_head_mismatch_post_quality_resume" in diag["warnings"]
    assert diag["repo"]["baseline_head_mismatch_allowed"] is True


def test_checkpoint_recovery_diagnostics_allows_verified_head_mismatch(tmp_path):
    import pipeline_recovery

    (tmp_path / "bots" / "claude_v269").mkdir(parents=True)
    checkpoint = {
        "next_v": 269,
        "source_v": 237,
        "stage": "verified",
        "repo_baseline": {"branch": "main", "head": "old123"},
    }
    snapshot = {"ok": True, "branch": "main...origin/main", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["active"] is True
    assert diag["recoverable"] is True
    assert "repo_baseline_head_mismatch" not in diag["issues"]
    assert "repo_baseline_head_mismatch_post_quality_resume" in diag["warnings"]
    assert diag["repo"]["baseline_head_mismatch_allowed"] is True


def test_checkpoint_recovery_diagnostics_allows_matching_active_checkpoint(tmp_path):
    import pipeline_recovery

    (tmp_path / "bots" / "claude_v258").mkdir(parents=True)
    checkpoint = {
        "next_v": 258,
        "source_v": 254,
        "stage": "workers_done",
        "repo_baseline": {"branch": "main", "head": "same123"},
    }
    snapshot = {"ok": True, "branch": "main...origin/main", "head": "same123"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["active"] is True
    assert diag["recoverable"] is True
    assert diag["issues"] == []


def test_checkpoint_recovery_diagnostics_tracks_rework_target_dirs(tmp_path):
    import pipeline_recovery

    for stage in ("quality_failed", "repair_planned", "rework_running"):
        (tmp_path / "bots" / "claude_v259").mkdir(parents=True, exist_ok=True)
        checkpoint = {
            "next_v": 259,
            "source_v": 254,
            "stage": stage,
            "repo_baseline": {"branch": "main", "head": "same123"},
        }
        snapshot = {"ok": True, "branch": "main...origin/main", "head": "same123"}

        diag = pipeline_recovery.checkpoint_recovery_diagnostics(
            checkpoint,
            snapshot=snapshot,
            project_root=tmp_path,
        )

        assert diag["active"] is True
        assert diag["recoverable"] is True
        assert diag["target"]["exists"] is True


def test_startup_recovery_blocks_unrecoverable_checkpoint(monkeypatch):
    import sys
    from types import SimpleNamespace

    import orchestrator_session
    import pipeline_recovery

    checkpoint = {
        "next_v": 257,
        "source_v": 197,
        "stage": "workers_done",
        "repo_baseline": {"branch": "old", "head": "old123"},
    }
    cleared = []
    events = []
    fake_evolution_core = SimpleNamespace(
        read_pipeline_checkpoint=lambda: checkpoint,
        clear_pipeline_checkpoint=lambda: None,
    )
    fake_system_log = SimpleNamespace(
        log_system_event=lambda *args, **kwargs: events.append((args, kwargs))
    )

    monkeypatch.setitem(sys.modules, "evolution_core", fake_evolution_core)
    monkeypatch.setitem(sys.modules, "system_log", fake_system_log)
    monkeypatch.setattr(orchestrator_session, "_load_orchestrator_session", lambda: "session-abc")
    monkeypatch.setattr(
        orchestrator_session,
        "_clear_orchestrator_session",
        lambda reason="completed_or_reset": cleared.append(reason),
    )
    monkeypatch.setattr(
        pipeline_recovery,
        "checkpoint_recovery_diagnostics",
        lambda _checkpoint: {
            "active": True,
            "recoverable": False,
            "issues": ["repo_baseline_head_mismatch"],
        },
    )

    result = orchestrator_session._startup_recovery()

    assert result["action"] == "blocked"
    assert result["reason"] == "unrecoverable_checkpoint"
    assert cleared == ["unrecoverable_checkpoint"]
    assert any(args[0] == "orchestrator.recovery_blocked" for args, _ in events)


def test_startup_recovery_resumes_prepared_without_master_plan(monkeypatch):
    import sys
    from types import SimpleNamespace

    import evolution_infra
    import orchestrator_session
    import pipeline_recovery

    checkpoint = {
        "next_v": 260,
        "source_v": 254,
        "stage": "prepared",
        "master_plan": None,
        "repo_baseline": {"branch": "main", "head": "same123"},
    }
    cleared = []
    events = []
    fake_evolution_core = SimpleNamespace(
        read_pipeline_checkpoint=lambda: checkpoint,
        clear_pipeline_checkpoint=lambda: cleared.append("checkpoint"),
    )
    fake_system_log = SimpleNamespace(
        log_system_event=lambda *args, **kwargs: events.append((args, kwargs))
    )

    monkeypatch.setitem(sys.modules, "evolution_core", fake_evolution_core)
    monkeypatch.setitem(sys.modules, "system_log", fake_system_log)
    monkeypatch.setattr(orchestrator_session, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(
        orchestrator_session,
        "_clear_orchestrator_session",
        lambda reason="completed_or_reset": cleared.append(reason),
    )
    monkeypatch.setattr(evolution_infra, "git_has_tag", lambda _v: False)
    monkeypatch.setattr(
        pipeline_recovery,
        "checkpoint_recovery_diagnostics",
        lambda _checkpoint: {"active": True, "recoverable": True, "issues": []},
    )

    result = orchestrator_session._startup_recovery()

    assert result["action"] == "resume"
    assert result["stage"] == "prepared"
    assert result["next_v"] == 260
    assert cleared == []
    assert any(args[0] == "orchestrator.recovery_decision" for args, _ in events)


def test_startup_recovery_resumes_old_quality_failed_checkpoint(monkeypatch):
    import sys
    from types import SimpleNamespace

    import evolution_infra
    import orchestrator_session
    import pipeline_recovery

    checkpoint = {
        "next_v": 261,
        "source_v": 254,
        "stage": "quality_failed",
        "timestamp": "2000-01-01T00:00:00",
        "repo_baseline": {"branch": "main", "head": "same123"},
    }
    cleared = []
    events = []
    fake_evolution_core = SimpleNamespace(
        read_pipeline_checkpoint=lambda: checkpoint,
        clear_pipeline_checkpoint=lambda: cleared.append("checkpoint"),
    )
    fake_system_log = SimpleNamespace(
        log_system_event=lambda *args, **kwargs: events.append((args, kwargs))
    )

    monkeypatch.setitem(sys.modules, "evolution_core", fake_evolution_core)
    monkeypatch.setitem(sys.modules, "system_log", fake_system_log)
    monkeypatch.setattr(orchestrator_session, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(
        orchestrator_session,
        "_clear_orchestrator_session",
        lambda reason="completed_or_reset": cleared.append(reason),
    )
    monkeypatch.setattr(evolution_infra, "git_has_tag", lambda _v: False)
    monkeypatch.setattr(
        pipeline_recovery,
        "checkpoint_recovery_diagnostics",
        lambda _checkpoint: {"active": True, "recoverable": True, "issues": []},
    )

    result = orchestrator_session._startup_recovery()

    assert result["action"] == "resume"
    assert result["stage"] == "quality_failed"
    assert result["next_v"] == 261
    assert cleared == []
    assert any(args[0] == "orchestrator.recovery_decision" for args, _ in events)
