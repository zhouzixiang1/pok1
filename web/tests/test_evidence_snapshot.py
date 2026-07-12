import asyncio
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor


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


def _patch_h2h_paths(
    monkeypatch,
    tmp_path,
    payload,
    *,
    match_history_rows=(),
    rating_history_rows=(),
):
    import evaluation_data_identity
    import evolution_infra
    from evaluation_bundle import publish_evaluation_cycle_manifest

    results = tmp_path / "web" / "core" / "results"
    results.mkdir(parents=True)
    monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(evolution_infra, "H2H_FILE", results / "head_to_head.json")
    monkeypatch.setattr(evolution_infra, "BOT_STATS_FILE", results / "bot_stats.json")
    monkeypatch.setattr(evolution_infra, "RATINGS_FILE", results / "glicko_ratings.json")
    monkeypatch.setattr(evolution_infra, "STATS_FILE", results / "elo_daemon_stats.json")
    monkeypatch.setattr(evolution_infra, "MATCH_HISTORY_FILE", results / "match_history.jsonl")
    monkeypatch.setattr(evolution_infra, "RATING_HISTORY_FILE", results / "rating_history.jsonl")
    identity_manifest = evaluation_data_identity.ensure_evaluation_data_identity(results)
    identity_digest = identity_manifest["manifest_digest"]
    h2h_file = results / "head_to_head.json"
    h2h_file.write_text(json.dumps(payload), encoding="utf-8")
    active = sorted({
        name
        for key in payload
        for name in key.split(" vs ")
        if name.startswith("national_v")
    })
    h2h_games = {
        name: sum(
            int((row or {}).get("games", 0) or 0)
            for key, row in payload.items()
            if name in [part.strip() for part in key.split(" vs ")]
        )
        for name in active
    }
    h2h_opponents = {
        name: sum(
            1
            for key, row in payload.items()
            if name in [part.strip() for part in key.split(" vs ")]
            and int((row or {}).get("games", 0) or 0) > 0
        )
        for name in active
    }
    (results / "bot_stats.json").write_text(
        json.dumps({name: {"games": 20, "win_rate": 0.5} for name in active}),
        encoding="utf-8",
    )
    (results / "glicko_ratings.json").write_text(
        json.dumps({
            name: {"r": 1500.0, "rd": 90.0, "sigma": 0.06}
            for name in active
        }),
        encoding="utf-8",
    )
    (results / "selection_snapshot.json").write_text(
        json.dumps({
            "schema_version": 1,
            "save_num": 1,
            "daemon_run_id": "test-run",
            "active_bots": active,
            "rows": [{
                "name": name,
                "selection_score": 0.5,
                "leaderboard_score": 0.5,
                "h2h_avg_wr": 0.5,
                "h2h_games": h2h_games[name],
                "h2h_opponents": h2h_opponents[name],
                "h2h_opponents_total": max(0, len(active) - 1),
                "h2h_coverage": 1.0,
                "strength_confidence": "medium",
            } for name in active],
            "rating_history_tail": [],
        }),
        encoding="utf-8",
    )
    (results / "elo_daemon_stats.json").write_text(
        json.dumps({
            "total_games": sum(
                int((row or {}).get("games", 0) or 0) for row in payload.values()
            ),
            "pairs": {
                key: int((row or {}).get("games", 0) or 0)
                for key, row in payload.items()
            },
        }),
        encoding="utf-8",
    )
    enriched_match_rows = []
    for row in match_history_rows:
        enriched = dict(row)
        enriched.setdefault("evaluation_identity_digest", identity_digest)
        enriched_match_rows.append(enriched)
    (results / "match_history.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in enriched_match_rows),
        encoding="utf-8",
    )
    (results / "rating_history.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rating_history_rows),
        encoding="utf-8",
    )
    (results / "bot_action_stats.json").write_text(
        json.dumps({name: {"total_actions": 1} for name in active}),
        encoding="utf-8",
    )
    (results / "bot_action_stats_per_opp.json").write_text("{}", encoding="utf-8")
    manifest = publish_evaluation_cycle_manifest(
        save_num=1,
        daemon_run_id="test-run",
        active_bots=active,
        results_dir=results,
        evaluation_identity_digest=identity_digest,
        _test_only_allow_unleased=True,
    )
    (results / "bot_action_stats_source.json").write_text(
        json.dumps({
            "evaluation_identity_digest": identity_digest,
            "source": "test-committed-replays",
            "source_cycle_manifest_digest": manifest["manifest_digest"],
            "source_cycle_save_num": 1,
            "published_against_cycle_manifest_digest": manifest["manifest_digest"],
            "published_against_cycle_save_num": 1,
            "cycle_lag_at_publish": 0,
            "max_cycle_lag": 5,
        }),
        encoding="utf-8",
    )
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


def test_generation_snapshot_accepts_only_bounded_same_identity_action_stats_lag(
    monkeypatch, tmp_path
):
    import evidence_snapshot
    from evaluation_bundle import publish_evaluation_cycle_manifest

    live = _patch_h2h_paths(monkeypatch, tmp_path, {
        "national_v1 vs national_v2": {
            "games": 10,
            "a_wins": 5,
            "b_wins": 5,
            "draws": 0,
        }
    })
    results = live.parent
    selection_path = results / "selection_snapshot.json"
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    active = selection["active_bots"]

    selection["save_num"] = 2
    selection_path.write_text(json.dumps(selection), encoding="utf-8")
    publish_evaluation_cycle_manifest(
        save_num=2,
        daemon_run_id="test-run",
        active_bots=active,
        results_dir=results,
        _test_only_allow_unleased=True,
    )
    created = evidence_snapshot.ensure_generation_h2h_snapshot(25)
    frozen = evidence_snapshot.load_generation_evaluation_snapshot(25)

    assert created["available"] is True
    assert frozen["action_stats"]
    assert frozen["action_stats_source"]["bounded_stale"] is True
    assert frozen["action_stats_source"]["snapshot_cycle_lag"] == 1

    selection["save_num"] = 7
    selection_path.write_text(json.dumps(selection), encoding="utf-8")
    publish_evaluation_cycle_manifest(
        save_num=7,
        daemon_run_id="test-run",
        active_bots=active,
        results_dir=results,
        _test_only_allow_unleased=True,
    )
    evidence_snapshot.ensure_generation_h2h_snapshot(26)
    too_old = evidence_snapshot.load_generation_evaluation_snapshot(26)

    assert too_old["action_stats"] == {}
    assert too_old["action_stats_per_opp"] == {}
    assert too_old["action_stats_source"]["reason"] == (
        "no_bounded_same_identity_committed_action_scan"
    )


def test_generation_h2h_snapshot_rejects_payload_tampering(monkeypatch, tmp_path):
    import evidence_snapshot

    _patch_h2h_paths(monkeypatch, tmp_path, {
        "national_v1 vs national_v2": {
            "games": 10,
            "a_wins": 6,
            "b_wins": 4,
            "draws": 0,
        }
    })
    created = evidence_snapshot.ensure_generation_h2h_snapshot(3)
    Path(created["h2h_path"]).write_text("{}", encoding="utf-8")

    reused = evidence_snapshot.ensure_generation_h2h_snapshot(3)

    assert reused["available"] is False
    assert reused["reason"] == "snapshot_integrity_failure"
    assert "snapshot_h2h_digest_mismatch" in reused["issues"]
    assert evidence_snapshot.load_generation_h2h_snapshot(3) == {}
    contract = evidence_snapshot.h2h_snapshot_contract_text(3)
    assert "Do not read live H2H" in contract


def test_generation_snapshot_rejects_rating_or_stats_tampering(monkeypatch, tmp_path):
    import evidence_snapshot

    _patch_h2h_paths(monkeypatch, tmp_path, {
        "national_v1 vs national_v2": {
            "games": 10,
            "a_wins": 5,
            "b_wins": 5,
            "draws": 0,
        }
    })
    created = evidence_snapshot.ensure_generation_h2h_snapshot(3)
    snapshot_dir = Path(created["manifest_path"]).parent
    (snapshot_dir / "bot_stats.json").write_text("{}", encoding="utf-8")

    reused = evidence_snapshot.ensure_generation_h2h_snapshot(3)

    assert reused["available"] is False
    assert "snapshot_bot_stats_digest_mismatch" in reused["issues"]


def test_generation_h2h_snapshot_rejects_manifest_tampering(monkeypatch, tmp_path):
    import evidence_snapshot

    _patch_h2h_paths(monkeypatch, tmp_path, {})
    created = evidence_snapshot.ensure_generation_h2h_snapshot(4)
    manifest_path = Path(created["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"] = 99
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    reused = evidence_snapshot.ensure_generation_h2h_snapshot(4)

    assert reused["available"] is False
    assert "snapshot_manifest_digest_mismatch" in reused["issues"]


def test_generation_h2h_snapshot_concurrent_creation_is_single_identity(
    monkeypatch, tmp_path
):
    import evidence_snapshot

    _patch_h2h_paths(monkeypatch, tmp_path, {
        "national_v1 vs national_v2": {
            "games": 2,
            "a_wins": 1,
            "b_wins": 1,
            "draws": 0,
        }
    })
    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(
            lambda _index: evidence_snapshot.ensure_generation_h2h_snapshot(5),
            range(4),
        ))

    assert all(row["available"] is True for row in rows)
    assert len({row["manifest_digest"] for row in rows}) == 1
    assert sum(row["reused"] is False for row in rows) == 1


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


def test_one_complete_70_hand_match_remains_valid_sparse_h2h_evidence(
    monkeypatch, tmp_path
):
    import evidence_snapshot

    history_row = {
        "id": "replay-low-sample",
        "bot0": "national_v123",
        "bot1": "national_v74",
        "strength_sample_unit": "70_hand_match",
        "hands_per_strength_sample": 70,
        "strength_sample_count": 1,
        "strength_admitted": True,
        "strength_complete": True,
        "strength_compliance_passed": True,
        "net_chips_bot0": [250],
        "bot0_wins": 1,
        "bot1_wins": 0,
        "draws": 0,
    }
    _patch_h2h_paths(
        monkeypatch,
        tmp_path,
        {
            "national_v123 vs national_v74": {
                "games": 1,
                "a_wins": 1,
                "b_wins": 0,
                "draws": 0,
                "win_rate": 1.0,
            }
        },
        match_history_rows=[history_row],
    )

    created = evidence_snapshot.ensure_generation_h2h_snapshot(124)
    frozen = evidence_snapshot.load_generation_evaluation_snapshot(124)
    summary = evidence_snapshot.build_h2h_prompt_summary(124, source_v=123)

    assert created["available"] is True
    assert frozen["available"] is True
    assert frozen["h2h"]["national_v123 vs national_v74"]["games"] == 1
    assert [row["id"] for row in frozen["match_history_index"]["entries"]] == [
        "replay-low-sample"
    ]
    assert "national_v123 vs national_v74: games=1, a_wins=1, b_wins=0" in summary
    assert "class=sparse" in summary


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
    assert "canonical_citation=\"national_v123 vs national_v74: games=5, a_wins=3, b_wins=2, draws=0, win_rate=0.6000\"" in summary
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

    assert "canonical_citation: national_v123 vs national_v74: games=5, a_wins=3, b_wins=2, draws=0, win_rate=0.6000" in guidance
    assert "v123 perspective: 3W/2L, wr=0.6000" in guidance
    assert "Do not replace them with live H2H" in guidance


def test_snapshot_recomputes_draw_aware_score_instead_of_stale_win_rate():
    import evidence_snapshot

    row = {
        "games": 10,
        "a_wins": 0,
        "b_wins": 0,
        "draws": 10,
        "win_rate": 0.0,
    }

    assert evidence_snapshot._row_win_rate(row) == 0.5


def test_master_prompt_uses_generation_h2h_snapshot(monkeypatch, tmp_path):
    import agent_master
    import evidence_snapshot

    _patch_h2h_paths(monkeypatch, tmp_path, {
        "national_v1 vs national_v2": {"games": 2, "a_wins": 1, "b_wins": 1, "draws": 0}
    })
    assert evidence_snapshot.ensure_generation_h2h_snapshot(24)["available"] is True
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
    assert "do not reopen live glicko_ratings.json, bot_stats.json, or rating_history.jsonl" in captured["prompt"]
    assert "Selection evidence snapshot:" in captured["prompt"]
    assert "web/core/results/glicko_ratings.json` —" not in captured["prompt"]


def test_master_plan_audit_prompt_includes_snapshot_json(monkeypatch, tmp_path):
    import audit_agents
    import evidence_snapshot

    _patch_h2h_paths(monkeypatch, tmp_path, {
        "national_v11 vs national_v20": {
            "games": 55,
            "a_wins": 32,
            "b_wins": 23,
            "draws": 0,
            "win_rate": 0.5818,
        }
    })
    assert evidence_snapshot.ensure_generation_h2h_snapshot(24)["available"] is True
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


def test_hard_critic_uses_generation_snapshot_not_live_h2h(monkeypatch, tmp_path):
    import agent_review
    import evidence_snapshot

    _patch_h2h_paths(monkeypatch, tmp_path, {
        "national_v11 vs national_v20": {
            "games": 55,
            "a_wins": 32,
            "b_wins": 23,
            "draws": 0,
            "win_rate": 0.5818,
        }
    })
    assert evidence_snapshot.ensure_generation_h2h_snapshot(24)["available"] is True
    captured = {}

    async def fake_run_claude_query(prompt, *_args, **_kwargs):
        captured["prompt"] = prompt
        return json.dumps({
            "score": 7,
            "approved": True,
            "strategic_assessment": "snapshot-backed",
            "feedback": "",
            "local_optima_warning": False,
        }), 0.0, {}

    monkeypatch.setattr(agent_review, "run_claude_query", fake_run_claude_query)

    result = asyncio.run(agent_review._run_critic(24, 20, "{}", _UI()))

    assert result["approved"] is True
    assert "Stable H2H Snapshot Contract" in captured["prompt"]
    assert "national_v11 vs national_v20" in captured["prompt"]
    assert "Do not read live `web/core/results/head_to_head.json`" in captured["prompt"]
