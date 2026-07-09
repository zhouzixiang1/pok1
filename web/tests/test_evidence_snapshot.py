import asyncio
import json
from pathlib import Path


class _UI:
    def __init__(self):
        self.history = []

    def clear_io(self):
        pass

    def log_history(self, msg, level="info"):
        self.history.append((level, msg))

    def get_output(self):
        return ""

    def log_io(self, *_args, **_kwargs):
        pass


def _patch_h2h_paths(monkeypatch, tmp_path, payload):
    import evolution_infra

    results = tmp_path / "web" / "core" / "results"
    results.mkdir(parents=True)
    h2h_file = results / "head_to_head.json"
    h2h_file.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(evolution_infra, "H2H_FILE", h2h_file)
    return h2h_file


def test_generation_h2h_snapshot_freezes_live_file(monkeypatch, tmp_path):
    import evidence_snapshot

    first = {
        "national_v17 vs national_v20": {
            "games": 45,
            "a_wins": 29,
            "b_wins": 16,
            "draws": 0,
            "win_rate": 0.6444,
        }
    }
    live = _patch_h2h_paths(monkeypatch, tmp_path, first)

    snapshot = evidence_snapshot.ensure_generation_h2h_snapshot(24)
    live.write_text(
        json.dumps({
            "national_v17 vs national_v20": {
                "games": 50,
                "a_wins": 31,
                "b_wins": 19,
                "draws": 0,
                "win_rate": 0.62,
            }
        }),
        encoding="utf-8",
    )
    reused = evidence_snapshot.ensure_generation_h2h_snapshot(24)

    assert reused["reused"] is True
    assert reused["h2h_relpath"] == "web/core/results/v24/evidence_snapshot/head_to_head.json"
    frozen = json.loads(Path(snapshot["h2h_path"]).read_text(encoding="utf-8"))
    assert frozen["national_v17 vs national_v20"]["games"] == 45
    assert frozen["national_v17 vs national_v20"]["a_wins"] == 29
    assert frozen["national_v17 vs national_v20"]["b_wins"] == 16


def test_h2h_citation_validation_uses_snapshot_not_live(monkeypatch, tmp_path):
    import evidence_snapshot

    live = _patch_h2h_paths(monkeypatch, tmp_path, {
        "national_v17 vs national_v20": {
            "games": 45,
            "a_wins": 29,
            "b_wins": 16,
            "draws": 0,
            "win_rate": 0.6444,
        }
    })
    evidence_snapshot.ensure_generation_h2h_snapshot(24)
    live.write_text(
        json.dumps({
            "national_v17 vs national_v20": {
                "games": 50,
                "a_wins": 31,
                "b_wins": 19,
                "draws": 0,
                "win_rate": 0.62,
            }
        }),
        encoding="utf-8",
    )

    good_plan = {
        "analysis": "national_v17 vs national_v20 = 45g, a_wins=29, b_wins=16",
    }
    bad_plan = {
        "analysis": "national_v17 vs national_v20 = 50g, a_wins=31, b_wins=19",
    }

    assert evidence_snapshot.validate_h2h_citations_against_snapshot(good_plan, 24) == []
    errors = evidence_snapshot.validate_h2h_citations_against_snapshot(bad_plan, 24)
    assert "snapshot has games=45" in "; ".join(errors)
    assert "snapshot has a_wins=29" in "; ".join(errors)
    assert "snapshot has b_wins=16" in "; ".join(errors)


def test_h2h_citation_validation_rejects_abbreviated_wl_sample(monkeypatch, tmp_path):
    import evidence_snapshot

    _patch_h2h_paths(monkeypatch, tmp_path, {
        "national_v59 vs national_v73": {
            "games": 25,
            "a_wins": 11,
            "b_wins": 14,
            "draws": 0,
            "win_rate": 0.44,
        }
    })
    evidence_snapshot.ensure_generation_h2h_snapshot(74)

    plan = {
        "analysis": (
            "Replay evidence ev_abc shows v59 vs v73, 1W/4L, so v73 loses hard "
            "to v59 and should tune against that nemesis."
        ),
    }

    errors = evidence_snapshot.validate_h2h_citations_against_snapshot(plan, 74)

    joined = "; ".join(errors)
    assert "v59 vs v73 cited games=5" in joined
    assert "snapshot has games=25 (key national_v59 vs national_v73)" in joined
    assert "v59 vs v73 cited a_wins=1" in joined
    assert "v59 vs v73 cited b_wins=4" in joined


def test_h2h_citation_validation_accepts_reversed_abbreviated_wl(monkeypatch, tmp_path):
    import evidence_snapshot

    _patch_h2h_paths(monkeypatch, tmp_path, {
        "national_v59 vs national_v73": {
            "games": 25,
            "a_wins": 11,
            "b_wins": 14,
            "draws": 0,
            "win_rate": 0.44,
        }
    })
    evidence_snapshot.ensure_generation_h2h_snapshot(74)

    plan = {
        "analysis": "Stable snapshot row v73 vs v59: 25g, 14W/11L from v73 perspective.",
    }

    assert evidence_snapshot.validate_h2h_citations_against_snapshot(plan, 74) == []


def test_h2h_prompt_summary_uses_stable_source_perspective(monkeypatch, tmp_path):
    import evidence_snapshot

    live = _patch_h2h_paths(monkeypatch, tmp_path, {
        "national_v120 vs national_v98": {
            "games": 30,
            "a_wins": 9,
            "b_wins": 21,
            "draws": 0,
            "win_rate": 0.30,
        },
        "national_v31 vs national_v120": {
            "games": 5,
            "a_wins": 4,
            "b_wins": 1,
            "draws": 0,
            "win_rate": 0.80,
        },
    })
    evidence_snapshot.ensure_generation_h2h_snapshot(121)
    live.write_text(json.dumps({
        "national_v120 vs national_v98": {
            "games": 100,
            "a_wins": 90,
            "b_wins": 10,
            "draws": 0,
            "win_rate": 0.90,
        }
    }), encoding="utf-8")

    summary = evidence_snapshot.build_h2h_prompt_summary(121, source_v=120)

    assert "national_v120 vs national_v98: games=30, a_wins=9, b_wins=21" in summary
    assert "class=confirmed_weakness" in summary
    assert "source_wr=0.3000" in summary
    assert "national_v31 vs national_v120: games=5" in summary
    assert "class=sparse" in summary
    assert "canonical_citation=\"national_v31 vs national_v120: games=5, a_wins=4, b_wins=1" in summary
    assert "games=100" not in summary


def test_h2h_prompt_summary_keeps_all_source_rows_before_other_rows(monkeypatch, tmp_path):
    import evidence_snapshot

    payload = {}
    for opp in range(1, 35):
        payload[f"national_v123 vs national_v{opp}"] = {
            "games": 30,
            "a_wins": 15,
            "b_wins": 15,
            "draws": 0,
            "win_rate": 0.5,
        }
    payload["national_v123 vs national_v74"] = {
        "games": 5,
        "a_wins": 3,
        "b_wins": 2,
        "draws": 0,
        "win_rate": 0.6,
    }
    for idx in range(200, 260):
        payload[f"national_v{idx} vs national_v{idx + 1}"] = {
            "games": 200,
            "a_wins": 100,
            "b_wins": 100,
            "draws": 0,
            "win_rate": 0.5,
        }

    _patch_h2h_paths(monkeypatch, tmp_path, payload)
    evidence_snapshot.ensure_generation_h2h_snapshot(124)

    summary = evidence_snapshot.build_h2h_prompt_summary(124, source_v=123, max_rows=35)

    assert "national_v123 vs national_v74: games=5, a_wins=3, b_wins=2" in summary
    assert "canonical_citation=\"national_v123 vs national_v74: games=5, a_wins=3, b_wins=2, win_rate=0.6000\"" in summary
    assert "source_record=3W/2L" in summary
    assert "national_v200 vs national_v201" not in summary


def test_h2h_citation_repair_guidance_returns_canonical_snapshot_rows(monkeypatch, tmp_path):
    import evidence_snapshot

    _patch_h2h_paths(monkeypatch, tmp_path, {
        "national_v123 vs national_v74": {
            "games": 5,
            "a_wins": 3,
            "b_wins": 2,
            "draws": 0,
            "win_rate": 0.6,
        }
    })
    evidence_snapshot.ensure_generation_h2h_snapshot(124)
    errors = [
        "national_v74 vs national_v123 cited games=10, snapshot has games=5 (key national_v123 vs national_v74)",
        "national_v74 vs national_v123 cited a_wins=2, snapshot has a_wins=3 (key national_v123 vs national_v74)",
    ]

    guidance = evidence_snapshot.h2h_citation_repair_guidance(124, errors, source_v=123)

    assert "canonical_citation: national_v123 vs national_v74: games=5, a_wins=3, b_wins=2, win_rate=0.6000" in guidance
    assert "v123 perspective: 3W/2L, wr=0.6000" in guidance
    assert "Do not replace them with live H2H" in guidance


def test_master_prompt_uses_generation_h2h_snapshot(monkeypatch, tmp_path):
    import agent_master

    _patch_h2h_paths(monkeypatch, tmp_path, {
        "national_v1 vs national_v2": {"games": 2, "a_wins": 1, "b_wins": 1, "draws": 0}
    })
    captured = {}
    valid_plan = {
        "analysis": "use stable snapshot",
        "targeted_failure": "one leak",
        "expected_behavior_change": "one changed decision",
        "do_not_touch": ["opponent.py"],
        "measurement_plan": "compare to parent",
        "tasks": [{
            "worker_id": 1,
            "role": "Algorithmic Logic Architect",
            "target_files": ["strategy.py"],
            "difficulty": "medium",
            "skill_layer": "spr",
            "worker_prompt": "Change strategy.py in the target bot.",
        }],
    }

    async def fake_run_claude_query(prompt, *_args, **_kwargs):
        captured["prompt"] = prompt
        return "```json\n" + json.dumps(valid_plan) + "\n```", 0.0, {}

    monkeypatch.setattr(agent_master, "run_claude_query", fake_run_claude_query)

    result = asyncio.run(agent_master._run_master_analysis(20, 24, "flat", _UI()))

    assert result is not None
    assert "web/core/results/v24/evidence_snapshot/head_to_head.json" in captured["prompt"]
    assert "Do not read live H2H for matchup counts during planning" in captured["prompt"]


def test_master_plan_audit_prompt_includes_snapshot_json(monkeypatch, tmp_path):
    import audit_agents

    _patch_h2h_paths(monkeypatch, tmp_path, {
        "national_v11 vs national_v20": {
            "games": 55,
            "a_wins": 32,
            "b_wins": 23,
            "draws": 0,
            "win_rate": 0.5818,
        }
    })
    captured = {}

    async def fake_run_claude_query(prompt, *_args, **_kwargs):
        captured["prompt"] = prompt
        return (
            "```json\n"
            + json.dumps({
                "plan_coherent": True,
                "contradiction_found": False,
                "contradictions": [],
                "experience_alignment": "aligned",
                "direction_novelty": "novel",
                "overall_pass": True,
                "feedback": "",
                "retry_recommended": False,
            })
            + "\n```"
        ), 0.0, {}

    monkeypatch.setattr(audit_agents, "run_claude_query", fake_run_claude_query)

    result = asyncio.run(audit_agents._run_master_plan_audit({"tasks": []}, 20, _UI(), next_v=24))

    assert result["overall_pass"] is True
    assert "Stable H2H Snapshot Contract" in captured["prompt"]
    assert "national_v11 vs national_v20" in captured["prompt"]
    assert "live-file drift after snapshot creation is not" in captured["prompt"]
