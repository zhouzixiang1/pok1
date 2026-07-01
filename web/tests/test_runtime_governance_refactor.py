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
    assert normalized["raw_missing_count"] == 3


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
    assert len(snapshot["protected_entries"]) == 2


def test_repo_state_log_emits_structured_event(monkeypatch):
    import repo_state

    events = []
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
