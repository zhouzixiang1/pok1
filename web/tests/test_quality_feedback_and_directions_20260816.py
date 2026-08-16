"""Quality-repair feedback transmission + direction-diversity advisory.

v187 (2026-08-16) burned five repair rounds on the identical
``budget_scaled_refinement`` / ``typed_runtime_probe`` failure: the repair
prompt carried only ``summary; required=guidance; locations`` and DROPPED the
probe diagnostics (capability_issues, differing paths, strata) that name the
actual behavioral criterion, so the worker could only guess. And Master
proposals recycled the same handful of policy.py symbols (v170-v187: 63%
opponent.terminal_response) because nothing told planning what recent
generations had already targeted.
"""

import os
import sys
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parents[1]
CORE_DIR = WEB_DIR / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import runtime_architecture_policy as rap  # noqa: E402
import tool_planning_quality_contracts  # noqa: E402,F401  (import order: breaks a repair_targets<->contracts cycle)
import tool_planning_quality_repair_targets as repair_targets  # noqa: E402


def test_bounded_evidence_extras_renders_diagnostics():
    extras = repair_targets._bounded_evidence_extras({
        "summary": "managed typed runtime probe failed",
        "locations": ["policy.py"],
        "issues": ["runtime_probe_non_repeatable"],
        "capability_issues": ["refinement_never_changes_sanitized_decision"],
        "differing_path_count": 18,
        "differing_paths": ["/line_reachability/dimensions/donk/decision"],
        "strata": {"short": {"trusted_steps": 2}, "long": {"trusted_steps": 9}},
        "changes_sanitized_decision": False,
    })
    assert "issues=" in extras and "runtime_probe_non_repeatable" in extras
    assert "capability_issues=" in extras
    assert "refinement_never_changes_sanitized_decision" in extras
    assert "differing_path_count=18" in extras
    assert "strata=" in extras
    assert "changes_sanitized_decision=false" in extras
    # Identifying digests and the summary are NOT re-rendered as extras.
    assert "managed typed runtime probe failed" not in extras

    assert repair_targets._bounded_evidence_extras({}) == ""
    assert repair_targets._bounded_evidence_extras({"issues": []}) == ""


def test_bounded_evidence_extras_renders_unknown_future_fields():
    """Class-kill assertion: a future gate check that grows a NEW diagnostic
    field must reach the repair worker without this renderer being touched
    (the v187 failure class was exactly this silent drop)."""
    extras = repair_targets._bounded_evidence_extras({
        "future_probe_metric": {"edge_count": 7, "mode": "strict"},
    })
    assert "future_probe_metric=" in extras
    assert "edge_count" in extras


def test_runtime_probe_check_carries_repeatability_and_determinism_guidance():
    check = rap._runtime_probe_check(
        passed=False,
        probe={
            "probe_identity_digest": "a" * 64,
            "managed_isolation_digest": "b" * 64,
            "issues": ["runtime_probe_non_repeatable"],
            "repeatability": {
                "repeat_count": 2,
                "differing_path_count": 18,
                "differing_paths": ["/x/decision", "/y/wire"],
            },
        },
    )
    evidence = check["evidence"]
    assert evidence["repeat_count"] == 2
    assert evidence["differing_path_count"] == 18
    assert evidence["differing_paths"] == ["/x/decision", "/y/wire"]
    # The determinism criterion must reach the worker.
    assert "time_budget" in check["guidance"]
    assert "measured elapsed time" in check["guidance"]


def test_budget_scaling_evidence_packs_behavioral_diagnostics():
    evidence = rap._budget_scaling_evidence({
        "capability_issues": ["refinement_never_changes_sanitized_decision"],
        "changes_sanitized_decision": False,
        "bounded_work": True,
        "scaled_or_exhausted": True,
        "short": {"trusted_steps": 2, "refinement_messages": 1, "action_changes": 0},
        "long": {"trusted_steps": 9, "refinement_messages": 2, "action_changes": 0},
    })
    assert evidence["changes_sanitized_decision"] is False
    assert evidence["capability_issues"] == [
        "refinement_never_changes_sanitized_decision"
    ]
    assert evidence["strata"]["long"]["trusted_steps"] == 9
    assert evidence["strata"]["long"]["action_changes"] == 0


def test_recent_directions_block_names_recycled_symbols(tmp_path, monkeypatch):
    import evolution_infra
    import generation_scheduler as gs

    results = tmp_path / "results"
    for v, symbol in (
        (187, "_refinement_prior_equity"),
        (186, "_bluff_allowed"),
        (185, "_decision_from_equity"),
    ):
        log_dir = results / f"v{v}" / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "master_io.txt").write_text(
            'noise before\n{"plan": {"change_symbol": "policy.py:' + symbol + '"}}\n',
            encoding="utf-8",
        )
    repo = tmp_path / "repo"
    (repo / "web").mkdir(parents=True)
    import subprocess

    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(git + ["init", "-q"], cwd=str(repo), check=True)
    (repo / "web" / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(git + ["add", "-A"], cwd=str(repo), check=True)
    subprocess.run(
        git + ["commit", "-q", "-m", "init"], cwd=str(repo), check=True,
    )
    subprocess.run(
        git + ["tag", "national-cloud-bot-v186"],
        cwd=str(repo), check=True,
    )
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", str(results))
    monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", str(repo))

    block = gs._recent_directions_block()
    assert "v187 policy.py:_refinement_prior_equity (not published)" in block
    assert "v186 policy.py:_bluff_allowed (published)" in block
    assert "v185 policy.py:_decision_from_equity (not published)" in block
    assert "exhausted hypotheses" in block
    assert "preflop range construction" in block
    assert len(block) <= 2600


def test_recent_directions_block_empty_without_logs(tmp_path, monkeypatch):
    import evolution_infra
    import generation_scheduler as gs

    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", str(tmp_path / "none"))
    monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", str(tmp_path))
    assert gs._recent_directions_block() == ""


def test_saturator_housekeep_prunes_sessions_and_stale_locks(tmp_path):
    import time

    import llm_saturator

    now = time.time()
    fresh = tmp_path / "session_00002.txt"
    fresh.write_text("x", encoding="utf-8")
    fresh_touch = tmp_path / "session_00001.txt"
    fresh_touch.write_text("x", encoding="utf-8")
    os.utime(fresh_touch, (now - 3600, now - 3600))
    stale = tmp_path / "session_00000.txt"
    stale.write_text("x", encoding="utf-8")
    os.utime(stale, (now - 100000, now - 100000))

    held_lock = tmp_path / "session_00002.txt.lock"
    held_lock.write_text("", encoding="utf-8")  # fresh — a live session may hold it
    stale_lock = tmp_path / "session_00000.txt.lock"
    stale_lock.write_text("", encoding="utf-8")
    os.utime(stale_lock, (now - 100000, now - 100000))

    llm_saturator._housekeep_session_files(tmp_path, keep_sessions=2)

    assert fresh.exists() and fresh_touch.exists()
    assert not stale.exists()
    assert held_lock.exists()  # never prune a possibly-held lock
    assert not stale_lock.exists()


def test_literature_identity_drift_is_self_describing():
    import tool_planning_literature_probe as probe

    checkpoint = {
        "next_v": 2,
        "source_v": 1,
        "checkpoint_revision": 5,
        "stage": "direction_audited",
        "brand_new_mutable_key": {"unexpected": "future bookkeeping"},
        "timestamp": "2026-08-16T00:00:00",
    }
    description = probe._describe_identity_drift(checkpoint, origin_revision=5)
    assert "brand_new_mutable_key" in description
    assert "preimage_keys=" in description
    # The documented strip list itself never appears as residual key.
    assert "timestamp" not in description.split("heaviest=")[0]


def test_pipeline_priority_semaphore_counts_queue_pending():
    """v187 class-kill: pipeline queue-wait must be visible so background
    fill can yield/preempt. The wrapper counts pending demand exactly while
    the acquire waits, and only then."""
    import asyncio

    import llm_concurrency as lc

    async def scenario():
        lc._note_pipeline_pending(0)
        raw = asyncio.Semaphore(1)
        wrapped = lc._PipelinePrioritySemaphore(raw)
        await raw.acquire()  # pool fully held (e.g. by saturator sessions)
        assert lc.pipeline_pending_count() == 0

        async def queued_pipeline():
            async with wrapped:
                return "ran"

        task = asyncio.ensure_future(queued_pipeline())
        await asyncio.sleep(0.05)
        assert lc.pipeline_pending_count() == 1  # waiting, not running
        raw.release()
        assert await task == "ran"
        await asyncio.sleep(0.01)
        assert lc.pipeline_pending_count() == 0  # cleared after acquire
        assert raw._value == 1  # the wrapper released the permit back

    asyncio.run(scenario())


def test_saturator_preemption_pick_semantics():
    import llm_saturator

    # Transient pipeline queue churn must not cancel sessions...
    assert llm_saturator._pick_preemptable({"t": 1.0}, 5.0) is None
    # ...but sustained demand preempts the YOUNGEST (least invested).
    tasks = {"old": 100.0, "young": 900.0}
    assert llm_saturator._pick_preemptable(tasks, 45.0) == "young"
    assert llm_saturator._pick_preemptable({}, 45.0) is None
