import json
import os
import subprocess
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
        target_dir=tmp_path / "national_v300",
        project_root=tmp_path,
    )

    task = compiled["tasks"][0]
    assert meta["compiled"] is True
    assert task["worker_prompt_compiled"] is True
    assert len(task["worker_prompt"]) < plan_compiler.HARD_WORKER_PROMPT_CHARS
    assert "task_brief_file" in task
    assert (tmp_path / task["task_brief_file"]).exists()
    assert compiled["plan_compiler"]["compiled_tasks"][0]["original_chars"] == len(long_prompt)


def test_plan_compiler_clears_stale_task_context_for_short_plan(tmp_path):
    import plan_compiler

    target = tmp_path / "national_v301"
    stale_dir = target / ".task_context"
    stale_dir.mkdir(parents=True)
    (stale_dir / "w1.md").write_text("stale next_v: 290", encoding="utf-8")

    plan = {
        "tasks": [
            {
                "worker_id": 1,
                "role": "Hyperparameter Tuner",
                "target_files": ["constants.py"],
                "worker_prompt": "Tune one constant.",
            }
        ]
    }

    compiled, meta = plan_compiler.compile_master_plan(
        plan,
        next_v=301,
        target_dir=target,
        project_root=tmp_path,
    )

    assert meta["compiled"] is False
    assert not stale_dir.exists()
    assert "task_brief_file" not in compiled["tasks"][0]


def test_plan_compiler_clears_stale_task_context_for_invalid_task_shape(tmp_path):
    import plan_compiler

    target = tmp_path / "national_v302"
    stale_dir = target / ".task_context"
    stale_dir.mkdir(parents=True)
    (stale_dir / "w1.md").write_text("stale next_v: 290", encoding="utf-8")

    compiled, meta = plan_compiler.compile_master_plan(
        {"tasks": {"not": "a list"}},
        next_v=302,
        target_dir=target,
        project_root=tmp_path,
    )

    assert meta["compiled"] is False
    assert compiled["tasks"] == {"not": "a list"}
    assert not stale_dir.exists()


def test_candidate_copy_excludes_task_context_and_parent_artifacts(tmp_path):
    import evolution_infra

    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (source / ".completed").write_text("", encoding="utf-8")
    (source / "old.pyc").write_bytes(b"pyc")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "main.cpython.pyc").write_bytes(b"pyc")
    (source / ".task_context").mkdir()
    (source / ".task_context" / "w1.md").write_text("stale next_v: 290", encoding="utf-8")
    (source / "sub").mkdir()
    (source / "sub" / "keep.py").write_text("x = 1\n", encoding="utf-8")
    (source / "sub" / ".task_context").mkdir()
    (source / "sub" / ".task_context" / "w2.md").write_text("stale", encoding="utf-8")

    target = tmp_path / "target"
    evolution_infra.copy_bot_tree_for_candidate(source, target)

    assert (target / "main.py").exists()
    assert (target / "sub" / "keep.py").exists()
    assert not (target / ".completed").exists()
    assert not (target / "old.pyc").exists()
    assert not (target / "__pycache__").exists()
    assert not (target / ".task_context").exists()
    assert not (target / "sub" / ".task_context").exists()


def test_incremental_reset_removes_stale_task_context(tmp_path):
    import tool_planning

    source = tmp_path / "source"
    next_dir = tmp_path / "next"
    source.mkdir()
    next_dir.mkdir()
    (source / "main.py").write_text("source\n", encoding="utf-8")
    (source / ".task_context").mkdir()
    (source / ".task_context" / "w1.md").write_text("parent stale", encoding="utf-8")
    (next_dir / "main.py").write_text("worker edit\n", encoding="utf-8")
    (next_dir / "new_helper.py").write_text("new file\n", encoding="utf-8")
    (next_dir / ".task_context").mkdir()
    (next_dir / ".task_context" / "w1.md").write_text("old next_v: 290", encoding="utf-8")

    preserved = tool_planning._incremental_reset_next_dir(next_dir, source)

    assert preserved == ["new_helper.py"]
    assert (next_dir / "main.py").read_text(encoding="utf-8") == "source\n"
    assert (next_dir / "new_helper.py").exists()
    assert not (next_dir / ".task_context").exists()


def test_master_prompt_disallows_manual_task_context_files():
    text = (CORE / "prompts" / "master_prompt.md").read_text(encoding="utf-8")

    assert "Do not manually create, copy, or reference `.task_context`" in text
    assert "write it to `.task_context" not in text


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


def test_oversized_source_does_not_inflate_child_limit(tmp_path):
    """Regression: source > base_limit must NOT relax the child limit beyond source_lines.

    Previously _get_adaptive_limit computed max(2000, source*1.15), so a 2147-line
    source gave the child a 2469-line limit, letting the snowball grow every
    generation. Now the limit for an oversized source is exactly source_lines.
    """
    import code_verification

    source = tmp_path / "source"
    child = tmp_path / "child"
    source.mkdir()
    child.mkdir()
    (source / "strategy.py").write_text("x = 1\n" * 2147, encoding="utf-8")
    (child / "strategy.py").write_text("x = 1\n" * 2178, encoding="utf-8")

    _total, oversized = code_verification.check_code_size(child, source_dir=source)

    # limit must be 2147 (source_lines), NOT 2469 (source*1.15)
    assert oversized == [("strategy.py", 2178, 2147)]


def test_oversized_source_allows_child_to_match_or_shrink(tmp_path):
    """A child matching or shrinking an oversized source must pass the size gate.

    This lets descendants of an inherited-oversize parent (e.g. v103 strategy.py
    at 2147 lines) survive the gate while still being nudged toward compliance,
    rather than immediately blocking every future generation.
    """
    import code_verification

    source = tmp_path / "source"
    child = tmp_path / "child"
    source.mkdir()
    child.mkdir()
    (source / "strategy.py").write_text("x = 1\n" * 2147, encoding="utf-8")

    # Exactly match source
    (child / "strategy.py").write_text("x = 1\n" * 2147, encoding="utf-8")
    _total, oversized = code_verification.check_code_size(child, source_dir=source)
    assert oversized == []

    # Shrink below source
    (child / "strategy.py").write_text("x = 1\n" * 2100, encoding="utf-8")
    _total, oversized = code_verification.check_code_size(child, source_dir=source)
    assert oversized == []


def test_compliant_source_keeps_growth_budget(tmp_path):
    """source <= base_limit still gets the 15% LINE_GROWTH_BUDGET."""
    import code_verification

    source = tmp_path / "source"
    child = tmp_path / "child"
    source.mkdir()
    child.mkdir()
    (source / "strategy.py").write_text("x = 1\n" * 1900, encoding="utf-8")
    # 2050 < max(2000, 1900*1.15=2185) -> within budget
    (child / "strategy.py").write_text("x = 1\n" * 2050, encoding="utf-8")

    _total, oversized = code_verification.check_code_size(child, source_dir=source)
    assert oversized == []

    # 2200 > 2185 -> over budget
    (child / "strategy.py").write_text("x = 1\n" * 2200, encoding="utf-8")
    _total, oversized = code_verification.check_code_size(child, source_dir=source)
    assert oversized == [("strategy.py", 2200, 2185)]


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
        " M sever/server/tcp_server.py",
        " M sever/国赛平台/通信协议.docx",
        "?? bots/national_v245/",
        "?? web/logs/restart.log",
        "?? bots/neural_national_lab/data/run.jsonl",
    ])

    def _fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(_args[0], 0, stdout=status, stderr="")

    monkeypatch.setattr(repo_state.subprocess, "run", _fake_run)

    snapshot = repo_state.git_worktree_snapshot(tmp_path)

    assert snapshot["ok"] is True
    assert snapshot["branch"] == "codex/test...origin/main"
    assert snapshot["dirty_count"] == 3
    assert snapshot["untracked_count"] == 3
    assert snapshot["generated_bot_dirs"] == ["?? bots/national_v245/"]
    assert snapshot["protected_entries"] == [
        " M web/core/tool_gates.py",
        " M sever/server/tcp_server.py",
    ]
    assert " M sever/国赛平台/通信协议.docx" in snapshot["ignored_entries"]
    assert "?? web/logs/restart.log" in snapshot["ignored_entries"]
    assert "?? bots/neural_national_lab/data/run.jsonl" in snapshot["ignored_entries"]


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
            "dirty_count": 4,
            "untracked_count": 1,
            "entry_count": 5,
            "entries": [
                " M web/core/tool_gates.py",
                " M docs/notes.md",
                " M sever/国赛平台/通信协议.docx",
                " M sever/server/tcp_server.py",
                "?? bots/national_v251/",
            ],
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
    assert worktree_event[1] == "warn"
    assert " M web/core/tool_gates.py" in worktree_event[3]["new_dirty_entries"]
    assert " M sever/server/tcp_server.py" in worktree_event[3]["new_protected_entries"]
    assert " M docs/notes.md" in worktree_event[3]["new_ignored_entries"]
    assert " M sever/国赛平台/通信协议.docx" in worktree_event[3]["new_ignored_entries"]
    assert "?? bots/national_v251/" in worktree_event[3]["new_generated_bot_dirs"]
    assert "?? bots/national_v251/" not in worktree_event[3]["new_protected_entries"]


def test_repo_state_external_worktree_delta_is_info(monkeypatch):
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
            "branch": "main...origin/main",
            "head": "aaa111",
            "dirty_count": 2,
            "untracked_count": 2,
            "entry_count": 4,
            "entries": [
                " M docs/notes.md",
                " M sever/国赛平台/通信协议.docx",
                "?? bots/neural_national_lab/data/run.jsonl",
                "?? bots/national_v251/",
            ],
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

    worktree_event = next(event for event in events if event[0] == "repo.worktree_changed")
    assert worktree_event[1] == "info"
    assert worktree_event[3]["new_protected_entries"] == []
    assert " M docs/notes.md" in worktree_event[3]["new_ignored_entries"]
    assert " M sever/国赛平台/通信协议.docx" in worktree_event[3]["new_ignored_entries"]
    assert "?? bots/neural_national_lab/data/run.jsonl" in worktree_event[3]["new_ignored_entries"]


def test_runtime_code_has_no_local_absolute_project_paths():
    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        text=False,
        timeout=30,
    )
    assert proc.returncode == 0

    code_suffixes = (".py", ".sh", ".js", ".ts", ".tsx", ".json")
    excluded_prefixes = (
        "archive/",
        "docs/",
        "web/tests/",
        "bots/neural_national_lab/data/",
        "ladder_results/",
        "results/",
    )
    forbidden_tokens = (
        "/home/zzx/project/pok",
        "pok_evolution_run",
        "pok_national_native_run_clone",
    )

    hits = []
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", errors="replace")
        if not rel.endswith(code_suffixes):
            continue
        if rel.startswith(excluded_prefixes):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for token in forbidden_tokens:
            if token in text:
                hits.append(f"{rel}: {token}")
                break

    assert hits == []


def test_runtime_guard_allows_current_candidate_dir(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "abc123",
            "entries": ["?? bots/national_v300/"],
        },
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "abc123",
            "entries": ["?? bots/national_v300/"],
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


def test_runtime_guard_blocks_master_before_direction_audit(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "abc123",
            "entries": ["?? bots/national_v300/"],
        },
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "abc123",
            "entries": ["?? bots/national_v300/"],
        },
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: {
        "next_v": 300,
        "source_v": 299,
        "stage": "prepared",
        "repo_baseline": {
            "head": "abc123",
            "branch": "main...origin/main",
            "captured_stage": "prepared",
        },
    })

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_master",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["error"] == "pipeline_route_guard_blocked"
    assert payload["reason"] == "wrong_pipeline_stage"
    assert payload["checkpoint_stage"] == "prepared"
    assert payload["next_tool"] == "run_direction_audit"
    assert payload["allowed_tools"] == ["run_direction_audit"]


def test_runtime_guard_allows_pre_master_literature_probe(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "abc123",
            "entries": ["?? bots/national_v300/"],
        },
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "abc123",
            "entries": ["?? bots/national_v300/"],
        },
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: {
        "next_v": 300,
        "source_v": 299,
        "stage": "direction_audited",
        "repo_baseline": {
            "head": "abc123",
            "branch": "main...origin/main",
            "captured_stage": "direction_audited",
        },
    })

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_literature_probe",
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
            "entries": ["?? bots/national_v301/"],
        },
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "abc123",
            "entries": ["?? bots/national_v301/"],
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
        "entries": [" M web/core/tool_gates.py", "?? bots/national_v300/"],
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


def test_runtime_guard_allows_unrelated_inplace_dirty_entries(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "abc123",
        "entries": [
            " M docs/notes.md",
            " M sever/国赛平台/通信协议.docx",
            "?? bots/neural_national_lab/data/run.jsonl",
            "?? bots/national_v300/",
        ],
    }
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "execute_workers",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["ignored_count"] == 3
    assert " M docs/notes.md" in payload["ignored_entries"]
    assert " M sever/国赛平台/通信协议.docx" in payload["ignored_entries"]


def test_runtime_guard_allows_noncritical_web_core_dirty_entries(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "abc123",
        "entries": [
            " M web/core/replay_spotlight.py",
            "?? bots/national_v300/",
        ],
    }
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "execute_workers",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is True
    assert " M web/core/replay_spotlight.py" in payload["ignored_entries"]


def test_runtime_guard_blocks_foreign_national_bot_dir(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "abc123",
        "entries": ["?? bots/national_v299/", "?? bots/national_v300/"],
    }
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "execute_workers",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["reason"] == "unexpected_worktree_entries"
    assert "?? bots/national_v299/" in payload["unexpected_entries"]


def test_runtime_guard_blocks_truncated_snapshot(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "abc123",
        "entries": ["?? bots/national_v300/"] * 40,
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
        "entries": ["?? bots/national_v300/"],
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


def test_runtime_guard_allows_unrelated_head_drift(monkeypatch):
    import evaluation_contract
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "old123"})
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: ["docs/experiment-notes.md", "bots/neural_national_lab/data/run.jsonl"],
    )

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_quality_gates",
        {"version": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["head_drift_unrelated_allowed"] is True
    assert "docs/experiment-notes.md" in "\n".join(payload["head_changed_paths"])


def test_runtime_guard_blocks_source_bot_contract_head_drift(monkeypatch):
    import evaluation_contract
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "old123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: ["bots/national_v299/main.py"],
    )

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_quality_gates",
        {"version": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["reason"] == "head_changed_during_generation"
    assert payload["evaluation_contract_unchanged"] is False
    assert "bots/national_v299/main.py" in payload["head_contract_paths"]


def test_evaluation_contract_classifies_dynamic_bot_versions(monkeypatch):
    import evaluation_contract

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "default")
    contract = evaluation_contract.build_evaluation_contract(
        Path.cwd(),
        candidate_v=300,
        source_v=299,
        checkpoint={
            "gate_results": {
                "precommit_eval": {
                    "opponents": [
                        {"name": "national_v45"},
                        {"name": "bots/neural_national_lab/versions/v058"},
                    ]
                }
            },
            "official_job": {"opponent": "national_v142"},
        },
    )
    scope = evaluation_contract.classify_contract_paths(
        [
            "engine/battle.py",
            "web/core/replay_spotlight.py",
            "bots/national_v300/main.py",
            "bots/national_v299/main.py",
            "bots/national_v45/main.py",
            "official_certificates/national_v45.json",
            "official_certificates/national_v142.json",
            "bots/neural_national_lab/data/run.json",
            "sever/server/tcp_server.py",
            "sever/国赛平台/通信协议.docx",
            "docs/notes.md",
        ],
        contract,
    )

    assert "engine/battle.py" in scope["contract_paths"]
    assert "bots/national_v300/main.py" in scope["contract_paths"]
    assert "bots/national_v299/main.py" in scope["contract_paths"]
    assert "bots/national_v45/main.py" in scope["contract_paths"]
    assert "official_certificates/national_v45.json" in scope["contract_paths"]
    assert "official_certificates/national_v142.json" in scope["contract_paths"]
    assert "sever/server/tcp_server.py" in scope["contract_paths"]
    assert "web/core/replay_spotlight.py" in scope["external_paths"]
    assert "sever/国赛平台/通信协议.docx" in scope["external_paths"]
    assert "bots/neural_national_lab/data/run.json" in scope["external_paths"]
    assert "docs/notes.md" in scope["external_paths"]


def test_evaluation_contract_excludes_local_engine_under_native_profile(monkeypatch):
    import evaluation_contract

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native")
    contract = evaluation_contract.build_evaluation_contract(
        Path.cwd(),
        candidate_v=300,
        source_v=299,
        checkpoint={"stage": "workers_done", "next_v": 300, "source_v": 299},
    )
    scope = evaluation_contract.classify_contract_paths(
        [
            "engine/battle.py",
            "web/core/engine/battle.py",
            "web/core/smoke_tester.py",
            "sever/bot_adapter.py",
            "sever/tests/test_national_alignment.py",
            "scripts/national_acceptance_matrix.py",
            "sever/server/tcp_server.py",
            "sever/tests/test_national_platform_alignment.py",
            "web/core/national_acceptance.py",
            "web/core/national_eval.py",
            "web/core/national_native.py",
            "bots/national_v300/main.py",
            "bots/national_v299/main.py",
        ],
        contract,
    )

    assert contract["national_execution_mode"] == "native_tcp"
    assert "engine/battle.py" in scope["external_paths"]
    assert "web/core/engine/battle.py" in scope["external_paths"]
    assert "web/core/smoke_tester.py" in scope["external_paths"]
    assert "sever/bot_adapter.py" in scope["external_paths"]
    assert "sever/tests/test_national_alignment.py" in scope["external_paths"]
    assert "scripts/national_acceptance_matrix.py" in scope["external_paths"]
    assert "sever/server/tcp_server.py" in scope["contract_paths"]
    assert "sever/tests/test_national_platform_alignment.py" in scope["contract_paths"]
    assert "web/core/national_acceptance.py" in scope["external_paths"]
    assert "web/core/national_eval.py" in scope["external_paths"]
    assert "web/core/national_native.py" in scope["contract_paths"]
    assert "bots/national_v300/main.py" in scope["contract_paths"]
    assert "bots/national_v299/main.py" in scope["contract_paths"]


def test_evaluation_contract_tracks_adapter_files_in_adapter_profile(monkeypatch):
    import evaluation_contract

    contract = evaluation_contract.build_evaluation_contract(
        Path.cwd(),
        candidate_v=300,
        source_v=299,
        checkpoint={"stage": "workers_done", "next_v": 300, "source_v": 299},
        national_execution_mode="adapter",
    )
    scope = evaluation_contract.classify_contract_paths(
        [
            "sever/bot_adapter.py",
            "sever/tests/test_national_alignment.py",
            "scripts/national_acceptance_matrix.py",
            "web/core/national_acceptance.py",
            "web/core/national_eval.py",
            "sever/tests/test_national_platform_alignment.py",
        ],
        contract,
    )

    assert contract["national_execution_mode"] == "adapter"
    assert scope["contract_paths"] == [
        "scripts/national_acceptance_matrix.py",
        "sever/bot_adapter.py",
        "sever/tests/test_national_alignment.py",
        "sever/tests/test_national_platform_alignment.py",
        "web/core/national_acceptance.py",
        "web/core/national_eval.py",
    ]
    assert scope["external_paths"] == []


def test_worktree_scope_uses_native_evaluation_contract_for_dirty_paths(monkeypatch):
    import evaluation_contract
    import evolution_scope

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native")
    checkpoint = {"stage": "workers_done", "next_v": 300, "source_v": 299}
    contract = evaluation_contract.build_evaluation_contract(
        Path.cwd(),
        candidate_v=300,
        source_v=299,
        checkpoint=checkpoint,
    )

    scope = evolution_scope.classify_status_entries(
        [
            " M engine/battle.py",
            " M web/core/smoke_tester.py",
            " M sever/bot_adapter.py",
            " M sever/server/tcp_server.py",
            " M web/core/national_acceptance.py",
            " M bots/national_v299/main.py",
            "?? bots/national_v300/",
        ],
        300,
        contract_bot_versions=evaluation_contract.contract_bot_versions(
            candidate_v=300,
            checkpoint=checkpoint,
        ),
        evaluation_contract=contract,
    )

    assert " M engine/battle.py" in scope["external_entries"]
    assert " M web/core/smoke_tester.py" in scope["external_entries"]
    assert " M sever/bot_adapter.py" in scope["external_entries"]
    assert " M sever/server/tcp_server.py" in scope["critical_entries"]
    assert " M web/core/national_acceptance.py" in scope["external_entries"]
    assert " M bots/national_v299/main.py" in scope["foreign_bot_entries"]
    assert "?? bots/national_v300/" in scope["candidate_entries"]
    assert scope["blocking_entries"] == [
        " M sever/server/tcp_server.py",
        " M bots/national_v299/main.py",
    ]


def test_evaluation_contract_allows_noncritical_web_core_head_drift(monkeypatch):
    import evaluation_contract

    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: ["web/core/replay_spotlight.py"],
    )

    allowed, payload = evaluation_contract.evaluate_head_drift(
        Path.cwd(),
        "old123",
        "new456",
        candidate_v=300,
        source_v=299,
    )

    assert allowed is True
    assert payload["evaluation_contract_unchanged"] is True
    assert payload["head_contract_paths"] == []
    assert payload["head_external_paths"] == ["web/core/replay_spotlight.py"]


def test_evaluation_contract_allows_observability_and_launcher_head_drift(monkeypatch):
    import evaluation_contract

    non_contract_paths = [
        "sever/main.py",
        "web/main.py",
        "web/core/api_concurrency.py",
        "web/core/event_bus.py",
        "web/core/observe_policy.py",
        "web/core/rate_limiter.py",
        "web/core/system_log.py",
        "web/core/web_ui.py",
    ]
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: list(non_contract_paths),
    )

    allowed, payload = evaluation_contract.evaluate_head_drift(
        Path.cwd(),
        "old123",
        "new456",
        candidate_v=300,
        source_v=299,
    )

    assert allowed is True
    assert payload["evaluation_contract_unchanged"] is True
    assert payload["head_contract_paths"] == []
    assert payload["head_external_paths"] == sorted(non_contract_paths)


def test_evaluation_contract_blocks_gate_and_prompt_head_drift(monkeypatch):
    import evaluation_contract

    contract_paths = [
        "web/core/eval_stats.py",
        "web/core/national_native.py",
        "web/core/prompts/worker_prompt.md",
        "web/core/tool_eval.py",
        "web/core/worker_boundary.py",
    ]
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: list(contract_paths),
    )

    allowed, payload = evaluation_contract.evaluate_head_drift(
        Path.cwd(),
        "old123",
        "new456",
        candidate_v=300,
        source_v=299,
    )

    assert allowed is False
    assert payload["evaluation_contract_unchanged"] is False
    assert payload["head_contract_paths"] == sorted(contract_paths)
    assert payload["head_external_paths"] == []


def test_evaluation_contract_uses_stage_scoped_post_worker_contract(monkeypatch):
    import evaluation_contract

    changed_paths = [
        "web/core/agent_master.py",
        "web/core/eval_stats.py",
        "web/core/prompts/master_prompt.md",
        "web/core/prompts/worker_prompt.md",
        "sever/server/tcp_server.py",
    ]
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: list(changed_paths),
    )

    allowed, payload = evaluation_contract.evaluate_head_drift(
        Path.cwd(),
        "old123",
        "new456",
        candidate_v=300,
        source_v=299,
        checkpoint={"stage": "workers_done", "next_v": 300, "source_v": 299},
    )

    assert allowed is False
    assert payload["evaluation_contract"]["stage"] == "workers_done"
    assert payload["head_contract_paths"] == [
        "sever/server/tcp_server.py",
        "web/core/eval_stats.py",
    ]
    assert payload["head_external_paths"] == [
        "web/core/agent_master.py",
        "web/core/prompts/master_prompt.md",
        "web/core/prompts/worker_prompt.md",
    ]


def test_evaluation_contract_defers_gate_files_until_gate_stage(monkeypatch):
    import evaluation_contract

    changed_paths = [
        "sever/server/tcp_server.py",
        "web/core/prompts/master_prompt.md",
        "web/core/tool_gates.py",
    ]
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: list(changed_paths),
    )

    allowed, payload = evaluation_contract.evaluate_head_drift(
        Path.cwd(),
        "old123",
        "new456",
        candidate_v=300,
        source_v=299,
        checkpoint={"stage": "direction_audited", "next_v": 300, "source_v": 299},
    )

    assert allowed is False
    assert payload["evaluation_contract"]["stage"] == "direction_audited"
    assert payload["head_contract_paths"] == ["web/core/prompts/master_prompt.md"]
    assert payload["head_external_paths"] == [
        "sever/server/tcp_server.py",
        "web/core/tool_gates.py",
    ]

    allowed, payload = evaluation_contract.evaluate_head_drift(
        Path.cwd(),
        "old123",
        "new456",
        candidate_v=300,
        source_v=299,
        checkpoint={"stage": "workers_done", "next_v": 300, "source_v": 299},
    )

    assert allowed is False
    assert payload["evaluation_contract"]["stage"] == "workers_done"
    assert payload["head_contract_paths"] == [
        "sever/server/tcp_server.py",
        "web/core/tool_gates.py",
    ]
    assert payload["head_external_paths"] == ["web/core/prompts/master_prompt.md"]


def test_evaluation_contract_tracks_worker_files_for_repair_stage(monkeypatch):
    import evaluation_contract

    changed_paths = [
        "web/core/agent_master.py",
        "web/core/prompts/worker_prompt.md",
    ]
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: list(changed_paths),
    )

    allowed, payload = evaluation_contract.evaluate_head_drift(
        Path.cwd(),
        "old123",
        "new456",
        candidate_v=300,
        source_v=299,
        checkpoint={"stage": "precommit_failed", "next_v": 300, "source_v": 299},
    )

    assert allowed is False
    assert payload["evaluation_contract"]["stage"] == "precommit_failed"
    assert payload["head_contract_paths"] == ["web/core/prompts/worker_prompt.md"]
    assert payload["head_external_paths"] == ["web/core/agent_master.py"]


def test_evaluation_contract_uses_stage_specific_worker_contract(monkeypatch):
    import evaluation_contract

    changed_paths = [
        "web/core/agent_master.py",
        "web/core/agent_workers.py",
        "web/core/eval_stats.py",
        "web/core/prompts/master_prompt.md",
        "web/core/prompts/worker_prompt.md",
    ]
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: list(changed_paths),
    )

    allowed, payload = evaluation_contract.evaluate_head_drift(
        Path.cwd(),
        "old123",
        "new456",
        candidate_v=300,
        source_v=299,
        checkpoint={"stage": "master_planned", "next_v": 300, "source_v": 299},
    )

    assert allowed is False
    assert payload["evaluation_contract"]["stage"] == "master_planned"
    assert payload["head_contract_paths"] == [
        "web/core/agent_workers.py",
        "web/core/prompts/worker_prompt.md",
    ]
    assert payload["head_external_paths"] == [
        "web/core/agent_master.py",
        "web/core/eval_stats.py",
        "web/core/prompts/master_prompt.md",
    ]


def test_evaluation_contract_always_tracks_guard_files_after_workers(monkeypatch):
    import evaluation_contract

    changed_paths = [
        "web/core/evaluation_contract.py",
        "web/core/prompts/worker_prompt.md",
    ]
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: list(changed_paths),
    )

    allowed, payload = evaluation_contract.evaluate_head_drift(
        Path.cwd(),
        "old123",
        "new456",
        candidate_v=300,
        source_v=299,
        checkpoint={"stage": "critic_checked", "next_v": 300, "source_v": 299},
    )

    assert allowed is False
    assert payload["head_contract_paths"] == ["web/core/evaluation_contract.py"]
    assert payload["head_external_paths"] == ["web/core/prompts/worker_prompt.md"]


def test_evaluation_contract_tracks_shared_native_and_official_authority_files(monkeypatch):
    import evaluation_contract

    contract_paths = [
        "web/core/national_transport.py",
        "web/core/national_game_runtime.py",
        "web/core/national_bot_launcher.py",
        "web/core/national_runtime_telemetry.py",
        "web/core/runtime_capacity.py",
        "web/core/blocking_runtime.py",
        "web/core/official_platform_resource.py",
        "web/core/official_execution_profile.json",
        "web/core/official_verdict_ledger.py",
    ]
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: [
            *contract_paths,
            "web/server/routes/national_arena.py",
        ],
    )

    allowed, payload = evaluation_contract.evaluate_head_drift(
        Path.cwd(),
        "old123",
        "new456",
        candidate_v=300,
        source_v=299,
        checkpoint={"stage": "master_planned", "next_v": 300, "source_v": 299},
    )

    assert allowed is False
    assert payload["head_contract_paths"] == sorted(contract_paths)
    assert payload["head_external_paths"] == ["web/server/routes/national_arena.py"]


def test_worktree_scope_keeps_observability_nonblocking_but_gate_logic_blocking():
    import evolution_scope

    scope = evolution_scope.classify_status_entries(
        [
            " M web/core/event_bus.py",
            " M web/core/system_log.py",
            " M web/core/web_ui.py",
            " M web/core/eval_stats.py",
            " M web/core/worker_boundary.py",
        ],
        candidate_v=300,
    )

    assert " M web/core/eval_stats.py" in scope["critical_entries"]
    assert " M web/core/worker_boundary.py" in scope["critical_entries"]
    assert " M web/core/event_bus.py" in scope["external_entries"]
    assert " M web/core/system_log.py" in scope["external_entries"]
    assert " M web/core/web_ui.py" in scope["external_entries"]
    assert scope["blocking_entries"] == [
        " M web/core/eval_stats.py",
        " M web/core/worker_boundary.py",
    ]


def test_evolution_scope_is_exact_file_scoped():
    import evolution_scope

    assert evolution_scope.CRITICAL_PREFIXES == ()
    assert evolution_scope.classify_path("web/core/replay_spotlight.py", candidate_v=300) == "external"
    assert evolution_scope.classify_path("web/core/eval_stats.py", candidate_v=300) == "critical"
    assert evolution_scope.classify_path("sever/main.py", candidate_v=300) == "external"
    assert evolution_scope.classify_path("sever/server/tcp_server.py", candidate_v=300) == "critical"
    assert evolution_scope.classify_path("sever/国赛平台/通信协议.docx", candidate_v=300) == "external"
    assert evolution_scope.classify_path("bots/national_v300/postflop.py", candidate_v=300) == "candidate"
    assert evolution_scope.classify_path("bots/national_v299/postflop.py", candidate_v=300) == "foreign_active_bot"


def test_evolution_scope_can_limit_foreign_bot_blocking_to_contract_versions():
    import evolution_scope

    assert (
        evolution_scope.classify_path(
            "bots/national_v299/postflop.py",
            candidate_v=300,
            contract_bot_versions=[300, 299],
        )
        == "foreign_active_bot"
    )
    assert (
        evolution_scope.classify_path(
            "bots/national_v123/postflop.py",
            candidate_v=300,
            contract_bot_versions=[300, 299],
        )
        == "external"
    )

    scope = evolution_scope.classify_status_entries(
        [
            " M bots/national_v299/main.py",
            " M bots/national_v123/main.py",
            "?? bots/national_v300/",
        ],
        candidate_v=300,
        contract_bot_versions=[300, 299],
    )

    assert scope["blocking_entries"] == [" M bots/national_v299/main.py"]
    assert scope["external_entries"] == [" M bots/national_v123/main.py"]
    assert scope["candidate_entries"] == ["?? bots/national_v300/"]


def test_evaluation_contract_hash_ignores_non_contract_national_docs(tmp_path, monkeypatch):
    import evaluation_contract

    server_file = tmp_path / "sever" / "server" / "tcp_server.py"
    docs_file = tmp_path / "sever" / "国赛平台" / "通信协议.docx"
    server_file.parent.mkdir(parents=True)
    docs_file.parent.mkdir(parents=True)
    server_file.write_text("server-v1\n", encoding="utf-8")
    docs_file.write_text("doc-v1\n", encoding="utf-8")

    monkeypatch.setattr(
        evaluation_contract,
        "_git_ls_files",
        lambda _root, _pathspecs: [
            "sever/server/tcp_server.py",
            "sever/国赛平台/通信协议.docx",
        ],
    )
    contract = evaluation_contract.build_evaluation_contract(tmp_path)

    before = evaluation_contract.evaluation_contract_hash(tmp_path, contract)
    docs_file.write_text("doc-v2\n", encoding="utf-8")
    after_doc_change = evaluation_contract.evaluation_contract_hash(tmp_path, contract)
    server_file.write_text("server-v2\n", encoding="utf-8")
    after_server_change = evaluation_contract.evaluation_contract_hash(tmp_path, contract)

    assert after_doc_change == before
    assert after_server_change != before


def test_runtime_guard_uses_persisted_checkpoint_baseline_after_restart(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
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
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
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


def test_runtime_guard_allows_execute_workers_after_master_planned_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: {
        "next_v": 300,
        "source_v": 299,
        "stage": "master_planned",
        "master_plan": {"tasks": [{"worker_id": "w1", "target_files": ["strategy.py"]}]},
        "repo_baseline": {"head": "old123", "branch": "main...origin/main", "captured_stage": "selected"},
    })

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "execute_workers",
        {"version": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["head_drift_resume_allowed"] is True
    assert payload["head_drift_repair_allowed"] is False
    assert payload["resume_kind"] == "initial_workers"
    assert payload["stage"] == "master_planned"
    assert payload["baseline_head"] == "old123"
    assert payload["current_head"] == "new456"


def test_runtime_guard_allows_direction_audit_after_prepared_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: {
        "next_v": 300,
        "source_v": 299,
        "stage": "prepared",
        "repo_baseline": {"head": "old123", "branch": "main...origin/main", "captured_stage": "selected"},
    })

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_direction_audit",
        {"version": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["head_drift_resume_allowed"] is True
    assert payload["head_drift_repair_allowed"] is False
    assert payload["resume_kind"] == "pre_master"
    assert payload["stage"] == "prepared"
    assert payload["baseline_head"] == "old123"
    assert payload["current_head"] == "new456"


def test_runtime_guard_allows_master_after_direction_audited_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: {
        "next_v": 300,
        "source_v": 299,
        "stage": "direction_audited",
        "repo_baseline": {"head": "old123", "branch": "main...origin/main", "captured_stage": "prepared"},
    })

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_master",
        {"version": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["head_drift_resume_allowed"] is True
    assert payload["head_drift_repair_allowed"] is False
    assert payload["resume_kind"] == "pre_master"
    assert payload["stage"] == "direction_audited"
    assert payload["baseline_head"] == "old123"
    assert payload["current_head"] == "new456"


def test_runtime_guard_allows_crossover_after_selected_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": []},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": []},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: {
        "next_v": 300,
        "source_v": 250,
        "stage": "selected",
        "parent2_v": 240,
        "repo_baseline": {"head": "old123", "branch": "main...origin/main", "captured_stage": "selected"},
    })

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_crossover",
        {"next_v": 300, "source_v": 250, "parent2_v": 240},
    )

    assert ok is True
    assert payload["head_drift_resume_allowed"] is True
    assert payload["head_drift_repair_allowed"] is False
    assert payload["resume_kind"] == "selected"
    assert payload["stage"] == "selected"
    assert payload["baseline_head"] == "old123"
    assert payload["current_head"] == "new456"


def test_runtime_guard_allows_crossover_running_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: {
        "next_v": 300,
        "source_v": 250,
        "stage": "crossover_running",
        "parent2_v": 240,
        "repo_baseline": {"head": "old123", "branch": "main...origin/main", "captured_stage": "selected"},
    })

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_crossover",
        {"target_v": 300, "parent_a": 250, "parent_b": 240},
    )

    assert ok is True
    assert payload["head_drift_resume_allowed"] is True
    assert payload["head_drift_repair_allowed"] is False
    assert payload["resume_kind"] == "crossover"
    assert payload["stage"] == "crossover_running"


def test_runtime_guard_allows_quality_after_workers_done_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
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
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
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


def test_runtime_guard_allows_review_repair_workers_after_post_quality_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
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
        "execute_workers",
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
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
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
    monkeypatch.delenv("POK_RUNTIME_EXPECTED_HEAD", raising=False)
    snapshot = {"ok": True, "branch": "codex/refactor", "head": "abc123", "entries": ["?? bots/national_v300/"]}
    commands = []
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "_run_git", lambda *args: commands.append(args))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_literature_probe",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["reason"] == "branch_drift"
    assert payload["expected_branch"] == "main"
    assert commands == []


def test_runtime_guard_allows_non_commit_tool_on_same_head_branch_alias(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    monkeypatch.setenv("POK_RUNTIME_EXPECTED_HEAD", "abc123")
    snapshots = iter([
        {"ok": True, "branch": "codex/refactor", "head": "abc123", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "codex/refactor", "head": "abc123", "entries": ["?? bots/national_v300/"]},
    ])
    events = []
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "_log_guard_event", lambda *args: events.append(args))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_literature_probe",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["branch_alias_allowed"] is True
    assert events[0][0] == "repo.runtime_guard_branch_alias_allowed"


def test_runtime_guard_allows_pre_master_head_resume_on_same_head_branch_alias(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    monkeypatch.setenv("POK_RUNTIME_EXPECTED_HEAD", "new456")
    snapshots = iter([
        {"ok": True, "branch": "codex/refactor", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "codex/refactor", "head": "new456", "entries": ["?? bots/national_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "_unrelated_head_drift_allowed", lambda **_kwargs: (False, {}))
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: {
        "next_v": 300,
        "source_v": 299,
        "stage": "direction_audited",
        "repo_baseline": {"head": "old123", "branch": "main...origin/main", "captured_stage": "prepared"},
    })

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_master",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["head_drift_resume_allowed"] is True
    assert payload["resume_kind"] == "pre_master"
    assert payload["branch_alias_allowed"] is True


def test_runtime_guard_blocks_commit_on_same_head_branch_alias(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    monkeypatch.setenv("POK_RUNTIME_EXPECTED_HEAD", "abc123")
    snapshot = {"ok": True, "branch": "codex/refactor", "head": "abc123", "entries": ["?? bots/national_v300/"]}
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: None)

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "commit_bot",
        {"version": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["reason"] == "branch_drift"
    assert payload["expected_branch"] == "main"


def test_runtime_guard_allows_non_commit_tool_on_branch_with_unrelated_head_drift(monkeypatch):
    import evaluation_contract
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    monkeypatch.setenv("POK_RUNTIME_EXPECTED_HEAD", "old123")
    snapshots = iter([
        {"ok": True, "branch": "codex/docs", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "codex/docs", "head": "new456", "entries": ["?? bots/national_v300/"]},
    ])
    events = []
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "old123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args: ["docs/notes.md", "bots/neural_national_lab/data/run.json"],
    )
    monkeypatch.setattr(tool_runtime_guard, "_log_guard_event", lambda *args: events.append(args))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_review",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["head_drift_unrelated_allowed"] is True
    assert payload["head_changed_paths"] == [
        "docs/notes.md",
        "bots/neural_national_lab/data/run.json",
    ]
    assert any(event[0] == "repo.runtime_guard_branch_head_drift_unrelated_allowed" for event in events)


def test_runtime_guard_blocks_commit_on_branch_with_unrelated_head_drift(monkeypatch):
    import evaluation_contract
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    monkeypatch.setenv("POK_RUNTIME_EXPECTED_HEAD", "old123")
    snapshot = {"ok": True, "branch": "codex/docs", "head": "new456", "entries": ["?? bots/national_v300/"]}
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "old123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(evaluation_contract, "changed_paths_between_heads", lambda *_args: ["docs/notes.md"])

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "commit_bot",
        {"version": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["reason"] == "branch_drift"
    assert payload["expected_branch"] == "main"


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
        "entries": ["?? bots/national_v300/"],
        "truncated": False,
    })

    assert evolution_infra.write_pipeline_checkpoint(300, 299, "prepared") is True
    state = evolution_infra.read_pipeline_checkpoint()

    assert state["repo_baseline"]["head"] == "abc123"
    assert state["repo_baseline"]["branch"] == "main...origin/main"
    assert state["repo_baseline"]["captured_stage"] == "prepared"


def test_write_pipeline_checkpoint_refreshes_baseline_after_planning_handoff(tmp_path, monkeypatch):
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
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
        {
            "branch": "main...origin/main",
            "head": "mid456",
            "entry_count": 1,
            "dirty_count": 0,
            "untracked_count": 1,
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
        {
            "branch": "main...origin/main",
            "head": "new789",
            "entry_count": 1,
            "dirty_count": 0,
            "untracked_count": 1,
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
    ])
    monkeypatch.setattr(repo_state, "git_worktree_snapshot", lambda: next(snapshots))

    assert evolution_infra.write_pipeline_checkpoint(300, 299, "prepared") is True
    assert evolution_infra.write_pipeline_checkpoint(300, 299, "direction_audited") is True
    state = evolution_infra.read_pipeline_checkpoint()
    assert state["repo_baseline"]["head"] == "mid456"
    assert state["repo_baseline"]["captured_stage"] == "direction_audited"

    assert evolution_infra.write_pipeline_checkpoint(
        300,
        299,
        "master_planned",
        master_plan={"tasks": [{"worker_id": "w1", "target_files": ["strategy.py"]}]},
    ) is True
    state = evolution_infra.read_pipeline_checkpoint()
    assert state["repo_baseline"]["head"] == "new789"
    assert state["repo_baseline"]["captured_stage"] == "master_planned"


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
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
        {
            "branch": "main...origin/main",
            "head": "new456",
            "entry_count": 1,
            "dirty_count": 0,
            "untracked_count": 1,
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
    ])
    monkeypatch.setattr(repo_state, "git_worktree_snapshot", lambda: next(snapshots))

    assert evolution_infra.write_pipeline_checkpoint(300, 299, "quality_failed") is True
    assert evolution_infra.write_pipeline_checkpoint(300, 299, "repair_planned") is True
    state = evolution_infra.read_pipeline_checkpoint()

    assert state["repo_baseline"]["head"] == "new456"
    assert state["repo_baseline"]["captured_stage"] == "repair_planned"


def test_write_pipeline_checkpoint_refreshes_baseline_after_quality_gate(tmp_path, monkeypatch):
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
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
        {
            "branch": "main...origin/main",
            "head": "new456",
            "entry_count": 1,
            "dirty_count": 0,
            "untracked_count": 1,
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
    ])
    monkeypatch.setattr(repo_state, "git_worktree_snapshot", lambda: next(snapshots))

    assert evolution_infra.write_pipeline_checkpoint(300, 299, "workers_done") is True
    assert evolution_infra.write_pipeline_checkpoint(
        300,
        299,
        "quality_passed",
        gate_results={"quality": {"all_passed": True}},
    ) is True
    state = evolution_infra.read_pipeline_checkpoint()

    assert state["repo_baseline"]["head"] == "new456"
    assert state["repo_baseline"]["captured_stage"] == "quality_passed"


def test_write_pipeline_checkpoint_refreshes_same_validation_stage_with_gate_result(tmp_path, monkeypatch):
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
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
        {
            "branch": "main...origin/main",
            "head": "new456",
            "entry_count": 1,
            "dirty_count": 0,
            "untracked_count": 1,
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
    ])
    monkeypatch.setattr(repo_state, "git_worktree_snapshot", lambda: next(snapshots))

    assert evolution_infra.write_pipeline_checkpoint(
        300,
        299,
        "quality_passed",
        gate_results={"quality": {"all_passed": True}},
    ) is True
    assert evolution_infra.write_pipeline_checkpoint(
        300,
        299,
        "quality_passed",
        gate_results={"quality": {"all_passed": True, "rerun": True}},
    ) is True
    state = evolution_infra.read_pipeline_checkpoint()

    assert state["repo_baseline"]["head"] == "new456"
    assert state["repo_baseline"]["captured_stage"] == "quality_passed"


def test_write_pipeline_checkpoint_refreshes_baseline_after_precommit_gate(tmp_path, monkeypatch):
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
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
        {
            "branch": "main...origin/main",
            "head": "new456",
            "entry_count": 1,
            "dirty_count": 0,
            "untracked_count": 1,
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
    ])
    monkeypatch.setattr(repo_state, "git_worktree_snapshot", lambda: next(snapshots))

    assert evolution_infra.write_pipeline_checkpoint(
        300,
        299,
        "critic_checked",
        precommit_attempt=2,
        gate_results={
            "quality": {"all_passed": True},
            "review": {"approved": True},
            "critic": {"approved": True},
        },
    ) is True
    assert evolution_infra.write_pipeline_checkpoint(
        300,
        299,
        "verified",
        gate_results={"precommit_eval": {"passed": True}},
    ) is True
    state = evolution_infra.read_pipeline_checkpoint()

    assert state["repo_baseline"]["head"] == "new456"
    assert state["repo_baseline"]["captured_stage"] == "verified"
    assert state["precommit_attempt"] == 2


def test_publish_ready_blocks_required_push_disabled(monkeypatch):
    import evolution_infra

    monkeypatch.setenv("POK_REQUIRE_EVOLUTION_PUSH", "1")
    monkeypatch.delenv("EVOLUTION_GIT_PUSH", raising=False)
    monkeypatch.setattr(evolution_infra, "git_publish_status", lambda: {
        "ok": True,
        "branch": "main",
        "head": "abc123",
        "upstream": "origin/main",
        "upstream_head": "abc123",
        "ahead": 0,
        "behind": 0,
    })

    ok, payload = evolution_infra.ensure_publish_ready_for_new_generation()

    assert ok is False
    assert payload["reason"] == "evolution_git_push_disabled"


def test_publish_ready_blocks_unpublished_local_commits(monkeypatch):
    import evolution_infra

    monkeypatch.setenv("POK_REQUIRE_EVOLUTION_PUSH", "1")
    monkeypatch.setenv("EVOLUTION_GIT_PUSH", "1")
    monkeypatch.setattr(evolution_infra, "git_publish_status", lambda: {
        "ok": True,
        "branch": "main",
        "head": "def456",
        "upstream": "origin/main",
        "upstream_head": "abc123",
        "ahead": 2,
        "behind": 0,
    })

    ok, payload = evolution_infra.ensure_publish_ready_for_new_generation()

    assert ok is False
    assert payload["reason"] == "unpublished_local_commits"


def test_publish_ready_allows_synchronized_runtime(monkeypatch):
    import evolution_infra

    monkeypatch.setenv("POK_EVOLUTION_RUNTIME", "1")
    monkeypatch.setenv("EVOLUTION_GIT_PUSH", "1")
    monkeypatch.delenv("POK_REQUIRE_EVOLUTION_PUSH", raising=False)
    monkeypatch.setattr(evolution_infra, "git_publish_status", lambda: {
        "ok": True,
        "branch": "main",
        "head": "abc123",
        "upstream": "origin/main",
        "upstream_head": "abc123",
        "ahead": 0,
        "behind": 0,
    })

    ok, payload = evolution_infra.ensure_publish_ready_for_new_generation()

    assert ok is True
    assert payload["push_required"] is True
    assert payload["push_enabled"] is True


def test_checkpoint_recovery_diagnostics_allows_workers_done_head_mismatch(tmp_path):
    import pipeline_recovery

    (tmp_path / "bots" / "national_v257").mkdir(parents=True)
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


def test_checkpoint_recovery_diagnostics_allows_same_head_branch_alias(tmp_path, monkeypatch):
    import pipeline_recovery

    monkeypatch.setenv("POK_FORCE_PIPELINE_RECOVERY_GUARD", "1")
    (tmp_path / "bots" / "national_v257").mkdir(parents=True)
    checkpoint = {
        "next_v": 257,
        "source_v": 197,
        "stage": "workers_done",
        "repo_baseline": {"branch": "main", "head": "same123"},
    }
    snapshot = {"ok": True, "branch": "codex/refactor", "head": "same123"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["recoverable"] is True
    assert "repo_not_on_evolution_branch" not in diag["issues"]
    assert "repo_baseline_branch_mismatch" not in diag["issues"]
    assert "repo_current_branch_alias_resume" in diag["warnings"]
    assert "repo_baseline_branch_alias_resume" in diag["warnings"]
    assert diag["repo"]["current_branch_alias_allowed"] is True
    assert diag["repo"]["baseline_branch_alias_allowed"] is True


def test_checkpoint_recovery_diagnostics_blocks_verified_branch_alias(tmp_path, monkeypatch):
    import pipeline_recovery

    monkeypatch.setenv("POK_FORCE_PIPELINE_RECOVERY_GUARD", "1")
    (tmp_path / "bots" / "national_v257").mkdir(parents=True)
    checkpoint = {
        "next_v": 257,
        "source_v": 197,
        "stage": "verified",
        "repo_baseline": {"branch": "main", "head": "same123"},
    }
    snapshot = {"ok": True, "branch": "codex/refactor", "head": "same123"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["recoverable"] is False
    assert "repo_not_on_evolution_branch" in diag["issues"]


def test_checkpoint_recovery_diagnostics_allows_main_resume_from_alias_after_ancestor_head_advance(
    tmp_path,
    monkeypatch,
):
    import pipeline_recovery

    monkeypatch.setenv("POK_FORCE_PIPELINE_RECOVERY_GUARD", "1")
    monkeypatch.setattr(pipeline_recovery, "_head_is_ancestor", lambda *_args: True)
    (tmp_path / "bots" / "national_v281").mkdir(parents=True)
    checkpoint = {
        "next_v": 281,
        "source_v": 279,
        "stage": "workers_done",
        "repo_baseline": {"branch": "codex/neural-work", "head": "old123"},
    }
    snapshot = {"ok": True, "branch": "main...origin/main", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["recoverable"] is True
    assert "repo_baseline_branch_mismatch" not in diag["issues"]
    assert "repo_baseline_head_mismatch" not in diag["issues"]
    assert "repo_baseline_branch_alias_resume" in diag["warnings"]
    assert "repo_baseline_head_mismatch_gate_resume" in diag["warnings"]
    assert diag["repo"]["baseline_branch_alias_allowed"] is True
    assert diag["repo"]["baseline_head_mismatch_allowed"] is True
    assert diag["repo"]["baseline_branch_alias_reason"] == "main_resume_ancestor_head_drift"


def test_checkpoint_recovery_diagnostics_blocks_main_resume_from_alias_after_non_ancestor_head_advance(
    tmp_path,
    monkeypatch,
):
    import pipeline_recovery

    monkeypatch.setenv("POK_FORCE_PIPELINE_RECOVERY_GUARD", "1")
    monkeypatch.setattr(pipeline_recovery, "_head_is_ancestor", lambda *_args: False)
    (tmp_path / "bots" / "national_v281").mkdir(parents=True)
    checkpoint = {
        "next_v": 281,
        "source_v": 279,
        "stage": "workers_done",
        "repo_baseline": {"branch": "codex/neural-work", "head": "old123"},
    }
    snapshot = {"ok": True, "branch": "main...origin/main", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["recoverable"] is False
    assert "repo_baseline_branch_mismatch" in diag["issues"]
    assert "repo_baseline_head_mismatch" in diag["issues"]
    assert diag["repo"]["baseline_branch_alias_reason"] == "non_ancestor_head_drift"


def test_checkpoint_recovery_diagnostics_allows_branch_with_unrelated_head_drift(
    tmp_path,
    monkeypatch,
):
    import evaluation_contract
    import pipeline_recovery

    monkeypatch.setenv("POK_FORCE_PIPELINE_RECOVERY_GUARD", "1")
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args: [
            "bots/neural_national_lab/data/run.json",
            "bots/neural_national_lab/versions/v026/main.py",
        ],
    )
    (tmp_path / "bots" / "national_v281").mkdir(parents=True)
    checkpoint = {
        "next_v": 281,
        "source_v": 279,
        "stage": "quality_passed",
        "repo_baseline": {"branch": "main...origin/main", "head": "old123"},
    }
    snapshot = {"ok": True, "branch": "codex/neural-work", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["recoverable"] is True
    assert "repo_not_on_evolution_branch" not in diag["issues"]
    assert "repo_baseline_branch_mismatch" not in diag["issues"]
    assert "repo_baseline_head_mismatch" not in diag["issues"]
    assert "repo_current_branch_unrelated_head_resume" in diag["warnings"]
    assert "repo_baseline_head_mismatch_post_quality_resume" in diag["warnings"]
    assert diag["repo"]["current_branch_unrelated_head_allowed"] is True


def test_checkpoint_recovery_diagnostics_blocks_branch_with_critical_head_drift(
    tmp_path,
    monkeypatch,
):
    import evaluation_contract
    import pipeline_recovery

    monkeypatch.setenv("POK_FORCE_PIPELINE_RECOVERY_GUARD", "1")
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args: ["web/core/orchestrator.py"],
    )
    (tmp_path / "bots" / "national_v281").mkdir(parents=True)
    checkpoint = {
        "next_v": 281,
        "source_v": 279,
        "stage": "quality_passed",
        "repo_baseline": {"branch": "main...origin/main", "head": "old123"},
    }
    snapshot = {"ok": True, "branch": "codex/neural-work", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["recoverable"] is False
    assert "repo_not_on_evolution_branch" in diag["issues"]
    assert "repo_baseline_branch_mismatch" in diag["issues"]
    assert "repo_baseline_head_mismatch" in diag["issues"]
    assert diag["repo"]["current_branch_head_blocking_entries"] == ["?? web/core/orchestrator.py"]


def test_checkpoint_recovery_diagnostics_allows_master_planned_head_mismatch(tmp_path):
    import pipeline_recovery

    (tmp_path / "bots" / "national_v257").mkdir(parents=True)
    checkpoint = {
        "next_v": 257,
        "source_v": 197,
        "stage": "master_planned",
        "repo_baseline": {"branch": "main", "head": "old123"},
        "master_plan": {"tasks": [{"worker_id": "w1", "target_files": ["state.py"]}]},
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
    assert "repo_baseline_head_mismatch_initial_workers_resume" in diag["warnings"]
    assert diag["repo"]["baseline_head_mismatch_allowed"] is True
    assert diag["target"]["exists"] is True


def test_checkpoint_recovery_allows_master_planned_contract_head_mismatch(
    tmp_path,
    monkeypatch,
):
    import evaluation_contract
    import pipeline_recovery

    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: ["web/core/prompts/worker_prompt.md"],
    )
    (tmp_path / "bots" / "national_v257").mkdir(parents=True)
    checkpoint = {
        "next_v": 257,
        "source_v": 197,
        "stage": "master_planned",
        "repo_baseline": {
            "branch": "main",
            "head": "old123",
            "captured_stage": "selected",
            "evaluation_contract": {"version": 2, "hash": "old"},
        },
        "master_plan": {"tasks": [{"worker_id": "w1", "target_files": ["strategy.py"]}]},
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
    assert "repo_baseline_head_mismatch_initial_workers_resume" in diag["warnings"]
    assert diag["repo"]["baseline_evaluation_contract_unchanged"] is False
    assert "web/core/prompts/worker_prompt.md" in diag["repo"]["baseline_head_contract_paths"]
    assert diag["repo"]["head_drift_requires_contract_unchanged"] is False
    assert diag["repo"]["baseline_head_mismatch_allowed"] is True


def test_checkpoint_recovery_diagnostics_allows_selected_head_mismatch_without_target(tmp_path):
    import pipeline_recovery

    checkpoint = {
        "next_v": 257,
        "source_v": 197,
        "stage": "selected",
        "parent2_v": 188,
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
    assert "repo_baseline_head_mismatch_selected_resume" in diag["warnings"]
    assert diag["repo"]["baseline_head_mismatch_allowed"] is True
    assert "target" not in diag


def test_checkpoint_recovery_diagnostics_allows_crossover_running_head_mismatch(tmp_path):
    import pipeline_recovery

    (tmp_path / "bots" / "national_v257").mkdir(parents=True)
    checkpoint = {
        "next_v": 257,
        "source_v": 197,
        "stage": "crossover_running",
        "parent2_v": 188,
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
    assert "repo_baseline_head_mismatch_crossover_resume" in diag["warnings"]
    assert diag["repo"]["baseline_head_mismatch_allowed"] is True
    assert diag["repo"]["head_drift_resume_kind"] == "crossover"
    assert diag["target"]["exists"] is True


def test_checkpoint_recovery_diagnostics_allows_reentrant_crossover_after_contract_head_drift(
    tmp_path,
    monkeypatch,
):
    import evaluation_contract
    import pipeline_recovery

    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: ["web/core/pipeline_state.py"],
    )
    (tmp_path / "bots" / "national_v257").mkdir(parents=True)
    checkpoint = {
        "next_v": 257,
        "source_v": 197,
        "stage": "crossover_running",
        "parent2_v": 188,
        "repo_baseline": {
            "branch": "main",
            "head": "old123",
            "evaluation_contract": {"version": 2, "hash": "old"},
        },
    }
    snapshot = {"ok": True, "branch": "main", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["recoverable"] is True
    assert "repo_baseline_head_mismatch" not in diag["issues"]
    assert "web/core/pipeline_state.py" in diag["repo"]["baseline_head_contract_paths"]
    assert diag["repo"]["baseline_evaluation_contract_unchanged"] is False
    assert diag["repo"]["head_drift_requires_contract_unchanged"] is False
    assert diag["repo"]["head_drift_resume_kind"] == "crossover"


def test_checkpoint_recovery_diagnostics_allows_pre_master_head_mismatch(tmp_path):
    import pipeline_recovery

    for stage in ("prepared", "direction_audited"):
        (tmp_path / "bots" / "national_v257").mkdir(parents=True, exist_ok=True)
        checkpoint = {
            "next_v": 257,
            "source_v": 197,
            "stage": stage,
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
        assert "repo_baseline_head_mismatch_pre_master_resume" in diag["warnings"]
        assert diag["repo"]["baseline_head_mismatch_allowed"] is True
        assert diag["target"]["exists"] is True


def test_checkpoint_recovery_diagnostics_blocks_preparing_repo_head_mismatch(tmp_path):
    import pipeline_recovery

    (tmp_path / "bots" / "national_v257").mkdir(parents=True)
    checkpoint = {
        "next_v": 257,
        "source_v": 197,
        "stage": "preparing",
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

    (tmp_path / "bots" / "national_v269").mkdir(parents=True)
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

    (tmp_path / "bots" / "national_v269").mkdir(parents=True)
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

    (tmp_path / "bots" / "national_v269").mkdir(parents=True)
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

    (tmp_path / "bots" / "national_v258").mkdir(parents=True)
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


def test_checkpoint_recovery_diagnostics_ignores_unrelated_dirty_entries(tmp_path):
    import pipeline_recovery

    (tmp_path / "bots" / "national_v258").mkdir(parents=True)
    checkpoint = {
        "next_v": 258,
        "source_v": 254,
        "stage": "workers_done",
        "repo_baseline": {"branch": "main", "head": "same123"},
    }
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "same123",
        "entries": [
            " M docs/notes.md",
            "?? bots/neural_national_lab/data/run.jsonl",
            "?? bots/national_v258/",
        ],
    }

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["recoverable"] is True
    assert "repo_unrelated_worktree_entries_ignored" in diag["warnings"]
    assert diag["worktree_scope"]["ignored_count"] == 2


def test_checkpoint_recovery_diagnostics_only_blocks_contract_bot_versions(tmp_path):
    import pipeline_recovery

    (tmp_path / "bots" / "national_v258").mkdir(parents=True)
    checkpoint = {
        "next_v": 258,
        "source_v": 254,
        "parent2_v": 111,
        "stage": "workers_done",
        "repo_baseline": {"branch": "main", "head": "same123"},
    }
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "same123",
        "entries": [
            " M bots/national_v254/main.py",
            " M bots/national_v999/main.py",
        ],
    }

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["recoverable"] is False
    assert "repo_blocking_worktree_entries" in diag["issues"]
    assert diag["worktree_scope"]["blocking_entries"] == [" M bots/national_v254/main.py"]
    assert diag["worktree_scope"]["ignored_entries"] == [" M bots/national_v999/main.py"]


def test_checkpoint_recovery_diagnostics_blocks_critical_dirty_entries(tmp_path):
    import pipeline_recovery

    (tmp_path / "bots" / "national_v258").mkdir(parents=True)
    checkpoint = {
        "next_v": 258,
        "source_v": 254,
        "stage": "workers_done",
        "repo_baseline": {"branch": "main", "head": "same123"},
    }
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "same123",
        "entries": [" M sever/server/tcp_server.py", "?? bots/national_v258/"],
    }

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["recoverable"] is False
    assert "repo_blocking_worktree_entries" in diag["issues"]
    assert " M sever/server/tcp_server.py" in diag["worktree_scope"]["blocking_entries"]


def test_runtime_branch_guard_requests_shutdown_on_branch_drift(monkeypatch):
    import asyncio
    import orchestrator

    monkeypatch.setenv("POK_FORCE_RUNTIME_BRANCH_GUARD", "1")
    snapshots = iter([
        {"branch": "main", "head": "abc123", "branch_status": "main"},
        {"branch": "codex/other", "head": "abc123", "branch_status": "codex/other"},
    ])
    events = []
    cleared = []
    monkeypatch.setattr(orchestrator, "_runtime_git_identity", lambda: next(snapshots))
    monkeypatch.setattr(orchestrator, "log_system_event", lambda *args: events.append(args))
    monkeypatch.setattr(orchestrator, "_clear_orchestrator_session", lambda **kwargs: cleared.append(kwargs))

    class DummyShutdown:
        def __init__(self):
            self.is_shutting_down = False
            self.requested = False

        def request_shutdown(self):
            self.requested = True
            self.is_shutting_down = True

    class DummyOwnerTask:
        def __init__(self):
            self.cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

    shutdown = DummyShutdown()
    owner = DummyOwnerTask()
    hard_stop = asyncio.Event()

    asyncio.run(orchestrator._runtime_branch_guard_coroutine(
        None,
        shutdown,
        expected_branch="main",
        expected_head="",
        owner_task=owner,
        hard_stop_event=hard_stop,
        check_interval=0.001,
    ))

    assert shutdown.requested is True
    assert owner.cancelled is True
    assert hard_stop.is_set() is True
    assert cleared == [{"reason": "runtime_branch_drift"}]
    assert events[0][0] == "repo.runtime_branch_drift_shutdown"
    assert events[0][3]["reason"] == "branch_drift"


def test_runtime_git_identity_tolerates_empty_branch_status(monkeypatch):
    import orchestrator

    calls = []

    class Result:
        def __init__(self, returncode, stdout=""):
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["git", "status", "--short"]:
            return Result(1, "")
        if args == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return Result(0, "main\n")
        if args == ["git", "rev-parse", "--short=12", "HEAD"]:
            return Result(0, "abc123def456\n")
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)

    identity = orchestrator._runtime_git_identity()

    assert orchestrator._branch_name("") == ""
    assert identity == {
        "branch": "main",
        "branch_status": "main",
        "head": "abc123def456",
    }
    assert "--untracked-files=no" in calls[0]


def test_runtime_branch_guard_tolerates_same_head_branch_alias(monkeypatch):
    import asyncio
    import orchestrator

    monkeypatch.setenv("POK_FORCE_RUNTIME_BRANCH_GUARD", "1")
    snapshots = iter([
        {"branch": "codex/other", "head": "abc123", "branch_status": "codex/other"},
    ])
    events = []
    cleared = []
    monkeypatch.setattr(orchestrator, "_runtime_git_identity", lambda: next(snapshots))
    monkeypatch.setattr(orchestrator, "_clear_orchestrator_session", lambda **kwargs: cleared.append(kwargs))

    class DummyShutdown:
        def __init__(self):
            self.is_shutting_down = False
            self.requested = False

        def request_shutdown(self):
            self.requested = True
            self.is_shutting_down = True

    class DummyOwnerTask:
        def __init__(self):
            self.cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

    shutdown = DummyShutdown()

    def _fake_log(*args):
        events.append(args)
        shutdown.is_shutting_down = True

    monkeypatch.setattr(orchestrator, "log_system_event", _fake_log)

    owner = DummyOwnerTask()
    hard_stop = asyncio.Event()

    asyncio.run(orchestrator._runtime_branch_guard_coroutine(
        None,
        shutdown,
        expected_branch="main",
        expected_head="abc123",
        owner_task=owner,
        hard_stop_event=hard_stop,
        check_interval=0.001,
    ))

    assert shutdown.requested is False
    assert owner.cancelled is False
    assert hard_stop.is_set() is False
    assert cleared == []
    assert events[0][0] == "repo.runtime_branch_alias_allowed"


def test_runtime_branch_guard_tolerates_unrelated_head_drift(monkeypatch):
    import asyncio
    import orchestrator

    monkeypatch.setenv("POK_FORCE_RUNTIME_BRANCH_GUARD", "1")
    snapshots = iter([
        {"branch": "codex/docs", "head": "def456", "branch_status": "codex/docs"},
    ])
    events = []
    cleared = []
    monkeypatch.setattr(orchestrator, "_runtime_git_identity", lambda: next(snapshots))
    monkeypatch.setattr(
        orchestrator,
        "_runtime_head_drift_unrelated_allowed",
        lambda *_args: (True, {"head_changed_paths": ["docs/notes.md"], "candidate_v": 300}),
    )
    monkeypatch.setattr(orchestrator, "_clear_orchestrator_session", lambda **kwargs: cleared.append(kwargs))

    class DummyShutdown:
        def __init__(self):
            self.is_shutting_down = False
            self.requested = False

        def request_shutdown(self):
            self.requested = True
            self.is_shutting_down = True

    shutdown = DummyShutdown()

    def _fake_log(*args):
        events.append(args)
        shutdown.is_shutting_down = True

    monkeypatch.setattr(orchestrator, "log_system_event", _fake_log)

    asyncio.run(orchestrator._runtime_branch_guard_coroutine(
        None,
        shutdown,
        expected_branch="main",
        expected_head="abc123",
        check_interval=0.001,
    ))

    assert shutdown.requested is False
    assert cleared == []
    assert events[0][0] == "repo.runtime_head_drift_unrelated_allowed"
    assert events[0][3]["head_changed_paths"] == ["docs/notes.md"]
    assert events[0][3]["advanced_expected_head"] == "def456"
    assert os.environ["POK_RUNTIME_EXPECTED_HEAD"] == "def456"


def test_runtime_branch_guard_advances_baseline_for_repeated_unrelated_head_drift(monkeypatch):
    import asyncio
    import orchestrator

    monkeypatch.setenv("POK_FORCE_RUNTIME_BRANCH_GUARD", "1")
    monkeypatch.delenv("POK_RUNTIME_EXPECTED_HEAD", raising=False)

    snapshots = iter([
        {"branch": "main", "head": "def456", "branch_status": "main"},
        {"branch": "main", "head": "ghi789", "branch_status": "main"},
    ])
    drift_checks = []
    events = []
    cleared = []
    monkeypatch.setattr(orchestrator, "_runtime_git_identity", lambda: next(snapshots))

    def _allow_unrelated(expected_head, current_head):
        drift_checks.append((expected_head, current_head))
        return True, {"head_changed_paths": [f"docs/{current_head}.md"]}

    monkeypatch.setattr(orchestrator, "_runtime_head_drift_unrelated_allowed", _allow_unrelated)
    monkeypatch.setattr(orchestrator, "_clear_orchestrator_session", lambda **kwargs: cleared.append(kwargs))

    class DummyShutdown:
        def __init__(self):
            self.is_shutting_down = False
            self.requested = False

        def request_shutdown(self):
            self.requested = True
            self.is_shutting_down = True

    shutdown = DummyShutdown()

    def _fake_log(*args):
        events.append(args)
        if len(events) == 2:
            shutdown.is_shutting_down = True

    monkeypatch.setattr(orchestrator, "log_system_event", _fake_log)

    asyncio.run(orchestrator._runtime_branch_guard_coroutine(
        None,
        shutdown,
        expected_branch="main",
        expected_head="abc123",
        check_interval=0.001,
    ))

    assert shutdown.requested is False
    assert cleared == []
    assert [event[0] for event in events] == [
        "repo.runtime_head_drift_unrelated_allowed",
        "repo.runtime_head_drift_unrelated_allowed",
    ]
    assert drift_checks == [("abc123", "def456"), ("def456", "ghi789")]
    assert events[0][3]["advanced_expected_head"] == "def456"
    assert events[1][3]["expected_head"] == "def456"
    assert events[1][3]["advanced_expected_head"] == "ghi789"
    assert os.environ["POK_RUNTIME_EXPECTED_HEAD"] == "ghi789"


def test_runtime_branch_guard_adopts_pipeline_published_expected_head(monkeypatch):
    import asyncio
    import orchestrator

    monkeypatch.setenv("POK_FORCE_RUNTIME_BRANCH_GUARD", "1")
    monkeypatch.delenv("POK_RUNTIME_EXPECTED_HEAD", raising=False)

    snapshots = iter([
        {"branch": "main", "head": "def456", "branch_status": "main"},
    ])
    events = []
    cleared = []

    def _identity_after_pipeline_publish():
        monkeypatch.setenv("POK_RUNTIME_EXPECTED_HEAD", "def456")
        return next(snapshots)

    monkeypatch.setattr(orchestrator, "_runtime_git_identity", _identity_after_pipeline_publish)

    def _unexpected_drift_check(*_args):
        raise AssertionError("published current HEAD should be adopted before drift checks")

    monkeypatch.setattr(orchestrator, "_runtime_head_drift_unrelated_allowed", _unexpected_drift_check)
    monkeypatch.setattr(orchestrator, "_clear_orchestrator_session", lambda **kwargs: cleared.append(kwargs))

    class DummyShutdown:
        def __init__(self):
            self.is_shutting_down = False
            self.requested = False

        def request_shutdown(self):
            self.requested = True
            self.is_shutting_down = True

    shutdown = DummyShutdown()

    def _fake_log(*args):
        events.append(args)
        shutdown.is_shutting_down = True

    monkeypatch.setattr(orchestrator, "log_system_event", _fake_log)

    asyncio.run(orchestrator._runtime_branch_guard_coroutine(
        None,
        shutdown,
        expected_branch="main",
        expected_head="abc123",
        check_interval=0.001,
    ))

    assert shutdown.requested is False
    assert cleared == []
    assert events[0][0] == "repo.runtime_expected_head_adopted"
    assert events[0][3]["previous_expected_head"] == "abc123"
    assert events[0][3]["expected_head"] == "def456"
    assert os.environ["POK_RUNTIME_EXPECTED_HEAD"] == "def456"


def test_publish_runtime_expected_head_updates_tool_guard_baseline(monkeypatch):
    import evolution_infra

    monkeypatch.delenv("POK_RUNTIME_EXPECTED_HEAD", raising=False)
    monkeypatch.setattr(evolution_infra, "_git", lambda *args, **_kwargs: "abc123def456\n")

    head = evolution_infra.publish_runtime_expected_head("test_publish", version=289)

    assert head == "abc123def456"
    assert os.environ["POK_RUNTIME_EXPECTED_HEAD"] == "abc123def456"


def test_runtime_branch_guard_requests_shutdown_on_head_drift(monkeypatch):
    import asyncio
    import orchestrator

    monkeypatch.setenv("POK_FORCE_RUNTIME_BRANCH_GUARD", "1")
    snapshots = iter([
        {"branch": "main", "head": "abc123", "branch_status": "main"},
        {"branch": "main", "head": "def456", "branch_status": "main"},
    ])
    events = []
    monkeypatch.setattr(orchestrator, "_runtime_git_identity", lambda: next(snapshots))
    monkeypatch.setattr(orchestrator, "log_system_event", lambda *args: events.append(args))
    monkeypatch.setattr(orchestrator, "_clear_orchestrator_session", lambda **_kwargs: None)

    class DummyShutdown:
        def __init__(self):
            self.is_shutting_down = False
            self.requested = False

        def request_shutdown(self):
            self.requested = True
            self.is_shutting_down = True

    shutdown = DummyShutdown()

    asyncio.run(orchestrator._runtime_branch_guard_coroutine(
        None,
        shutdown,
        expected_branch="main",
        expected_head="abc123",
        check_interval=0.001,
    ))

    assert shutdown.requested is True
    assert events[0][0] == "repo.runtime_branch_drift_shutdown"
    assert events[0][3]["reason"] == "head_drift"


def test_checkpoint_recovery_diagnostics_tracks_rework_target_dirs(tmp_path):
    import pipeline_recovery

    for stage in ("quality_failed", "repair_planned", "rework_running"):
        (tmp_path / "bots" / "national_v259").mkdir(parents=True, exist_ok=True)
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
