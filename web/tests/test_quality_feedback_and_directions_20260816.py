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
        "issues": ["runtime_probe_non_repeatable", "x" * 300],
        "capability_issues": ["refinement_never_changes_sanitized_decision"],
        "differing_path_count": 18,
        "differing_paths": ["/line_reachability/dimensions/donk/decision"],
        "strata": {"short": {"trusted_steps": 2}, "long": {"trusted_steps": 9}},
        "changes_sanitized_decision": False,
    })
    assert "issues=['runtime_probe_non_repeatable'" in extras
    assert "capability_issues=['refinement_never_changes_sanitized_decision']" in extras
    assert "non_repeatable_paths[18]" in extras
    assert "strata=" in extras
    assert "changes_sanitized_decision=False" in extras
    # Bounded: the 300-char issue is included but the line stays sane.
    assert len(extras) < 600

    assert repair_targets._bounded_evidence_extras({}) == ""
    assert repair_targets._bounded_evidence_extras({"issues": []}) == ""


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
