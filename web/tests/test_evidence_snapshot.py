import asyncio
import json
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from bot_namespace import bot_name


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


def _valid_proposal_packet(
    agent_master,
    selected_proposal,
    log_dir,
    *,
    source_dir=None,
):
    import hashlib

    from system_strict_bootstrap import record_llm_invocation_evidence

    directions = ("mechanism", "counterfactual", "compute_memory")
    structural_changes = (
        selected_proposal["structural_change"],
        "Add a bounded state accumulator before the same reachable decision consumer.",
        "Add a deterministic paired-feature path into the same reachable decision consumer.",
    )
    snapshot_projection = json.dumps(
        {"games": 36, "wins": 14, "losses": 20, "draws": 2},
        sort_keys=True,
        separators=(",", ":"),
    )
    snapshot_binding = {
        "reference": f"snapshot:head_to_head.json#/{bot_name(1)} vs {bot_name(2)}",
        "node_sha256": hashlib.sha256(snapshot_projection.encode()).hexdigest(),
        "resolved_projection": snapshot_projection,
        "projection_sha256": hashlib.sha256(snapshot_projection.encode()).hexdigest(),
        "projection_truncated": False,
    }
    proposals = []
    for index, (direction, structural_change) in enumerate(
        zip(directions, structural_changes), start=1
    ):
        proposal = json.loads(json.dumps(selected_proposal))
        proposal["execution_mode"] = "strategy_implementation"
        proposal["snapshot_evidence"] = [snapshot_binding]
        proposal.setdefault("evidence_refs", []).append(
            snapshot_binding["reference"]
        )
        proposal["direction"] = direction
        proposal["structural_change"] = structural_change
        if index > 1:
            proposal["expected_diff"] = (
                f"Independent alternative {index} reaches the existing decision consumer."
            )
            proposal["falsifier"]["test_name"] = (
                "incremental_opponent_model"
                if index == 2
                else "showdown_range_adaptation"
            )
            if index == 2:
                proposal["mechanism_target"] = "opponent.rates"
                proposal["structural_change"] += " Route only through opponent.rates."
                proposal["expected_diff"] += " The consumer reads opponent.rates."
                proposal["falsifier"].update({
                    "state_learning_primary": "action_profile",
                    "intervention_target": "opponent.rates",
                    "control": "Hold the decision context and opponent action_profile at its prior.",
                    "intervention": "Change only opponent.rates action_profile in that decision context.",
                    "expected_observation": "The typed intent changes only with the opponent action_profile intervention.",
                })
            else:
                proposal["mechanism_target"] = "opponent.showdown_range"
                proposal["structural_change"] += (
                    " Route only through opponent.showdown_range."
                )
                proposal["expected_diff"] += " The consumer reads opponent.showdown_range."
                proposal["falsifier"].update({
                    "state_learning_primary": "showdown_range",
                    "intervention_target": "opponent.showdown_range",
                    "control": "Hold showdown_range confidence at its prior in the paired context.",
                    "intervention": "Change only opponent.showdown_range confidence in the paired context.",
                    "expected_observation": "The typed intent changes only with the showdown_range confidence intervention.",
                })
        proposal["proposal_id"] = agent_master._proposal_identity(proposal)
        proposals.append(proposal)
    proposal_ids = [proposal["proposal_id"] for proposal in proposals]
    log_dir.mkdir(parents=True, exist_ok=True)

    def invocation(index, *, purpose, role, role_result):
        return record_llm_invocation_evidence(
            invocation_id=f"{index:032x}",
            purpose=purpose,
            role=role,
            prompt_digest=hashlib.sha256(f"prompt:{index}".encode()).hexdigest(),
            raw_output_digest=hashlib.sha256(f"output:{index}".encode()).hexdigest(),
            result_digest=hashlib.sha256(f"result:{index}".encode()).hexdigest(),
            role_result=role_result,
            log_file=log_dir / f"invocation_{index}.txt",
        )

    proposal_invocations = {
        proposal["proposal_id"]: invocation(
            index,
            purpose=f"master_proposal_scout:{proposal['direction']}",
            role=f"MASTER PROPOSAL {proposal['direction']}",
            role_result=proposal,
        )
        for index, proposal in enumerate(proposals, start=1)
    }
    reviews = []
    proposal_id_set = set(proposal_ids)
    for index, critic_id in enumerate(("falsification", "scope"), start=4):
        raw_review = {
            "ballots": [
                {
                    "proposal_id": proposal_id,
                    "scores": {
                        criterion: 5
                        for criterion in agent_master._PROPOSAL_CRITIC_CRITERIA
                    },
                    "reject": False,
                    "reason": "The proposal is traceable, reachable, bounded, and falsifiable.",
                }
                for proposal_id in proposal_ids
            ]
        }
        review = agent_master._validated_proposal_critique(
            json.dumps(raw_review), proposal_id_set
        )
        assert review is not None
        review["critic_id"] = critic_id
        review["invocation_evidence"] = invocation(
            index,
            purpose=f"master_proposal_critic:{critic_id}",
            role=f"MASTER PROPOSAL CRITIC {critic_id}",
            role_result={key: value for key, value in review.items() if key != "critic_id"},
        )
        reviews.append(review)
    source_symbol_digests = (
        agent_master._proposal_source_symbol_digests(proposals, source_dir)
        if source_dir is not None
        else {
            proposal["proposal_id"]: {
                symbol: hashlib.sha256(
                    f"test-baseline:{symbol}".encode("utf-8")
                ).hexdigest()
                for symbol in proposal["source_symbols"]
            }
            for proposal in proposals
        }
    )
    return {
        "schema_version": "master-proposal-packet-v6",
        "valid": True,
        "authority": "ballots_rank_and_unanimous_reject_vetoes",
        "context_digest": "c" * 64,
        "source_code_digest": "d" * 64,
        "evidence_mode": "frozen_strength_snapshot",
        "proposal_count": 3,
        "valid_critic_count": 2,
        "critic_criteria": agent_master._PROPOSAL_CRITIC_CRITERIA,
        "allowed_proposal_ids": proposal_ids,
        "ordered_proposals": proposals,
        "proposal_source_symbol_digests": source_symbol_digests,
        "proposal_invocations": proposal_invocations,
        "critic_reviews": reviews,
    }


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
    import rating_snapshot
    from bot_namespace import ACTIVE_BOT_PREFIX
    from evaluation_bundle import publish_evaluation_cycle_manifest

    # This helper builds synthetic H2H/evaluation files for snapshot behavior
    # tests.  Raw replay-byte admission itself is exercised by the strict
    # replay integration tests, so keep this fixture focused on snapshot
    # ordering/cutoffs rather than duplicating a 70-hand raw envelope.
    monkeypatch.setattr(
        rating_snapshot,
        "_load_verified_history_replay",
        lambda entry, **_kwargs: dict(entry),
    )

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
        if name.startswith(ACTIVE_BOT_PREFIX)
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
        enriched.setdefault("evaluation_epoch", "national_tcp_policy_v1")
        enriched.setdefault("execution_mode", "native_tcp")
        enriched.setdefault("evaluation_identity_digest", identity_digest)
        enriched_match_rows.append(enriched)
    # Snapshot/prompt tests exercise frozen-view mechanics rather than the
    # raw-replay parser (which has dedicated integration coverage).  Still
    # construct a full *history admission projection* for every stored H2H
    # row, so the cycle now proves exact raw-history W/L/D instead of relying
    # on a cache-only fixture.
    if not enriched_match_rows:
        from national_native import build_native_match_timing_plan

        timing_plan = build_native_match_timing_plan(
            hands=70,
            requested_timeout_sec=None,
        )
        for index, (key, h2h_row) in enumerate(sorted(payload.items()), start=1):
            parts = [part.strip() for part in str(key).split(" vs ")]
            if len(parts) != 2 or not isinstance(h2h_row, dict):
                continue
            a_wins = int(h2h_row.get("a_wins", 0) or 0)
            b_wins = int(h2h_row.get("b_wins", 0) or 0)
            draws = int(h2h_row.get("draws", 0) or 0)
            if min(a_wins, b_wins, draws) < 0:
                continue
            samples = [1] * a_wins + [-1] * b_wins + [0] * draws
            enriched_match_rows.append({
                "id": f"synthetic-{index:04d}.json",
                "bot0": parts[0],
                "bot1": parts[1],
                "bot0_wins": a_wins,
                "bot1_wins": b_wins,
                "draws": draws,
                "strength_sample_unit": "70_hand_match",
                "hands_per_strength_sample": 70,
                "strength_admitted": True,
                "strength_complete": True,
                "strength_compliance_passed": True,
                "strength_sample_count": len(samples),
                "net_chips_bot0": samples,
                "native_match_timing_plan": timing_plan.snapshot(),
                "native_match_timing_plan_digest": timing_plan.digest(),
                "evaluation_epoch": "national_tcp_policy_v1",
                "execution_mode": "native_tcp",
                "evaluation_identity_digest": identity_digest,
            })
    (results / "match_history.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in enriched_match_rows),
        encoding="utf-8",
    )
    enriched_rating_rows = []
    for row in rating_history_rows:
        enriched = dict(row)
        enriched.setdefault("evaluation_epoch", "national_tcp_policy_v1")
        enriched.setdefault("execution_mode", "native_tcp")
        enriched.setdefault("evaluation_identity_digest", identity_digest)
        enriched_rating_rows.append(enriched)
    (results / "rating_history.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in enriched_rating_rows),
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

    key = f"{bot_name(17)} vs {bot_name(20)}"
    first = {
        key: {
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
            key: {
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
    assert frozen[key]["games"] == 45
    assert frozen[key]["a_wins"] == 29
    assert frozen[key]["b_wins"] == 16


def test_snapshot_freezes_replay_spotlight_and_anchor_map(monkeypatch, tmp_path):
    import evidence_snapshot
    import replay_spotlight
    import tool_planning

    _patch_h2h_paths(monkeypatch, tmp_path, {
        f"{bot_name(143)} vs {bot_name(144)}": {
            "games": 20,
            "a_wins": 10,
            "b_wins": 10,
            "draws": 0,
        }
    })
    identity = evidence_snapshot._evaluation_identity_digest(
        evidence_snapshot._infra().RESULTS_DIR
    )
    frozen_payload = {
        "schema_version": 2,
        "epoch": "national_tcp_policy_v1",
        "execution_mode": "native_tcp",
        "evaluation_identity_digest": identity,
        "bot": bot_name(144),
        "text": f"Strict native critical hands for {bot_name(144)}:\nG1H2#1234abcd",
        "citations": [{"id": "G1H2", "anchor": "1234abcd"}],
        "source_replays": {"match.json": {"sha256": "b" * 64}},
    }
    monkeypatch.setattr(
        replay_spotlight,
        "build_critical_hands_evidence",
        lambda *_args, **_kwargs: dict(frozen_payload),
    )

    created = evidence_snapshot.ensure_generation_h2h_snapshot(
        145, spotlight_bot=bot_name(144)
    )
    loaded = evidence_snapshot.load_generation_evaluation_snapshot(145)

    assert created["available"] is True
    assert loaded["replay_spotlight"] == frozen_payload
    assert tool_planning._load_replay_anchor_map(145) == {"G1H2": "1234abcd"}

    payload_path = (
        evidence_snapshot._infra().RESULTS_DIR
        / "v145"
        / "evidence_snapshot"
        / evidence_snapshot.REPLAY_SPOTLIGHT_FILENAME
    )
    payload_path.write_text("{}", encoding="utf-8")
    rejected = evidence_snapshot.load_generation_evaluation_snapshot(145)
    assert rejected["available"] is False
    assert "snapshot_replay_spotlight_digest_mismatch" in rejected["issues"]


def test_generation_snapshot_accepts_only_bounded_same_identity_action_stats_lag(
    monkeypatch, tmp_path
):
    import evidence_snapshot
    from evaluation_bundle import publish_evaluation_cycle_manifest

    live = _patch_h2h_paths(monkeypatch, tmp_path, {
        f"{bot_name(1)} vs {bot_name(2)}": {
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
        f"{bot_name(1)} vs {bot_name(2)}": {
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
        f"{bot_name(1)} vs {bot_name(2)}": {
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
        f"{bot_name(1)} vs {bot_name(2)}": {
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

    key = f"{bot_name(17)} vs {bot_name(20)}"
    live = _patch_h2h_paths(monkeypatch, tmp_path, {
        key: {
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
            key: {
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
        "analysis": f"{key} = 45g, a_wins=29, b_wins=16",
    }
    bad_plan = {
        "analysis": f"{key} = 50g, a_wins=31, b_wins=19",
    }

    assert evidence_snapshot.validate_h2h_citations_against_snapshot(good_plan, 24) == []
    errors = evidence_snapshot.validate_h2h_citations_against_snapshot(bad_plan, 24)
    assert "snapshot has games=45" in "; ".join(errors)
    assert "snapshot has a_wins=29" in "; ".join(errors)
    assert "snapshot has b_wins=16" in "; ".join(errors)


def test_h2h_citation_validation_rejects_abbreviated_wl_sample(monkeypatch, tmp_path):
    import evidence_snapshot

    _patch_h2h_paths(monkeypatch, tmp_path, {
        f"{bot_name(59)} vs {bot_name(73)}": {
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
    assert f"snapshot has games=25 (key {bot_name(59)} vs {bot_name(73)})" in joined
    assert "v59 vs v73 cited a_wins=1" in joined
    assert "v59 vs v73 cited b_wins=4" in joined


def test_h2h_citation_validation_accepts_reversed_abbreviated_wl(monkeypatch, tmp_path):
    import evidence_snapshot

    _patch_h2h_paths(monkeypatch, tmp_path, {
        f"{bot_name(59)} vs {bot_name(73)}": {
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
        f"{bot_name(120)} vs {bot_name(98)}": {
            "games": 30,
            "a_wins": 9,
            "b_wins": 21,
            "draws": 0,
            "win_rate": 0.30,
        },
        f"{bot_name(31)} vs {bot_name(120)}": {
            "games": 5,
            "a_wins": 4,
            "b_wins": 1,
            "draws": 0,
            "win_rate": 0.80,
        },
    })
    evidence_snapshot.ensure_generation_h2h_snapshot(121)
    live.write_text(json.dumps({
        f"{bot_name(120)} vs {bot_name(98)}": {
            "games": 100,
            "a_wins": 90,
            "b_wins": 10,
            "draws": 0,
            "win_rate": 0.90,
        }
    }), encoding="utf-8")

    summary = evidence_snapshot.build_h2h_prompt_summary(121, source_v=120)

    assert f"{bot_name(120)} vs {bot_name(98)}: games=30, a_wins=9, b_wins=21" in summary
    assert "class=confirmed_weakness" in summary
    assert "source_wr=0.3000" in summary
    assert f"{bot_name(31)} vs {bot_name(120)}: games=5" in summary
    assert "class=sparse" in summary
    assert f"canonical_citation=\"{bot_name(31)} vs {bot_name(120)}: games=5, a_wins=4, b_wins=1" in summary
    assert "games=100" not in summary


def test_one_complete_70_hand_match_remains_valid_sparse_h2h_evidence(
    monkeypatch, tmp_path
):
    import evidence_snapshot
    from national_native import build_native_match_timing_plan

    timing_plan = build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=None,
    )

    key = f"{bot_name(123)} vs {bot_name(74)}"
    history_row = {
        "id": "replay-low-sample",
        "bot0": bot_name(123),
        "bot1": bot_name(74),
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
        # A historical strength row is only admissible if its full-match
        # timing contract is immutable and independently digest-bound.
        "native_match_timing_plan": timing_plan.snapshot(),
        "native_match_timing_plan_digest": timing_plan.digest(),
    }
    _patch_h2h_paths(
        monkeypatch,
        tmp_path,
        {
            key: {
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
    assert frozen["h2h"][key]["games"] == 1
    assert [row["id"] for row in frozen["match_history_index"]["entries"]] == [
        "replay-low-sample"
    ]
    assert f"{key}: games=1, a_wins=1, b_wins=0" in summary
    assert "class=sparse" in summary


def test_cleanup_retains_replay_referenced_only_by_verified_evidence_snapshot(
    monkeypatch, tmp_path
):
    """Cycle retention must not orphan a still-valid generation snapshot."""

    import elo_daemon
    import evidence_snapshot

    live = _patch_h2h_paths(monkeypatch, tmp_path, {
        f"{bot_name(143)} vs {bot_name(144)}": {
            "games": 1,
            "a_wins": 1,
            "b_wins": 0,
            "draws": 0,
        }
    })
    results = live.parent
    replay_dir = results / "match_replay"
    replay_dir.mkdir(exist_ok=True)
    referenced = replay_dir / "synthetic-0001.json"
    disposable = replay_dir / "newer-unreferenced.json"
    referenced.write_text("{}", encoding="utf-8")
    disposable.write_text("{}", encoding="utf-8")
    created = evidence_snapshot.ensure_generation_h2h_snapshot(145)
    assert created["available"] is True

    # Remove live/cycle references to isolate the durable generation snapshot
    # as the only owner of this raw replay name.
    (results / "match_history.jsonl").write_text("", encoding="utf-8")
    shutil.rmtree(results / "evaluation_cycles")
    monkeypatch.setattr(elo_daemon, "RESULTS_DIR", results)
    monkeypatch.setattr(elo_daemon, "REPLAY_DIR", replay_dir)
    monkeypatch.setattr(elo_daemon, "MATCH_HISTORY_FILE", results / "match_history.jsonl")
    monkeypatch.setattr(elo_daemon, "MAX_REPLAY_FILES", 1)

    elo_daemon.cleanup_old_replays()

    assert referenced.exists()
    assert not disposable.exists()


def test_cleanup_does_not_trust_tampered_evidence_snapshot_references(
    monkeypatch, tmp_path
):
    """A detached JSON claim cannot pin a replay after its manifest drifts."""

    import elo_daemon
    import evidence_snapshot

    live = _patch_h2h_paths(monkeypatch, tmp_path, {
        f"{bot_name(143)} vs {bot_name(144)}": {
            "games": 1,
            "a_wins": 1,
            "b_wins": 0,
            "draws": 0,
        }
    })
    results = live.parent
    replay_dir = results / "match_replay"
    replay_dir.mkdir(exist_ok=True)
    referenced = replay_dir / "synthetic-0001.json"
    newer = replay_dir / "zz-unreferenced.json"
    referenced.write_text("{}", encoding="utf-8")
    newer.write_text("{}", encoding="utf-8")
    created = evidence_snapshot.ensure_generation_h2h_snapshot(145)
    manifest_path = Path(created["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["manifest_digest"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    (results / "match_history.jsonl").write_text("", encoding="utf-8")
    shutil.rmtree(results / "evaluation_cycles")
    monkeypatch.setattr(elo_daemon, "RESULTS_DIR", results)
    monkeypatch.setattr(elo_daemon, "REPLAY_DIR", replay_dir)
    monkeypatch.setattr(elo_daemon, "MATCH_HISTORY_FILE", results / "match_history.jsonl")
    monkeypatch.setattr(elo_daemon, "MAX_REPLAY_FILES", 1)

    elo_daemon.cleanup_old_replays()

    assert not referenced.exists()
    assert newer.exists()


def test_h2h_prompt_summary_keeps_all_source_rows_before_other_rows(monkeypatch, tmp_path):
    import evidence_snapshot

    payload = {}
    for opp in range(1, 35):
        payload[f"{bot_name(123)} vs {bot_name(opp)}"] = {
            "games": 30,
            "a_wins": 15,
            "b_wins": 15,
            "draws": 0,
            "win_rate": 0.5,
        }
    payload[f"{bot_name(123)} vs {bot_name(74)}"] = {
        "games": 5,
        "a_wins": 3,
        "b_wins": 2,
        "draws": 0,
        "win_rate": 0.6,
    }
    for idx in range(200, 260):
        payload[f"{bot_name(idx)} vs {bot_name(idx + 1)}"] = {
            "games": 200,
            "a_wins": 100,
            "b_wins": 100,
            "draws": 0,
            "win_rate": 0.5,
        }

    _patch_h2h_paths(monkeypatch, tmp_path, payload)
    evidence_snapshot.ensure_generation_h2h_snapshot(124)

    summary = evidence_snapshot.build_h2h_prompt_summary(124, source_v=123, max_rows=35)

    assert f"{bot_name(123)} vs {bot_name(74)}: games=5, a_wins=3, b_wins=2" in summary
    assert f"canonical_citation=\"{bot_name(123)} vs {bot_name(74)}: games=5, a_wins=3, b_wins=2, draws=0, win_rate=0.6000\"" in summary
    assert "source_record=3W/2L" in summary
    assert f"{bot_name(200)} vs {bot_name(201)}" not in summary


def test_h2h_citation_repair_guidance_returns_canonical_snapshot_rows(monkeypatch, tmp_path):
    import evidence_snapshot

    _patch_h2h_paths(monkeypatch, tmp_path, {
        f"{bot_name(123)} vs {bot_name(74)}": {
            "games": 5,
            "a_wins": 3,
            "b_wins": 2,
            "draws": 0,
            "win_rate": 0.6,
        }
    })
    evidence_snapshot.ensure_generation_h2h_snapshot(124)
    key = f"{bot_name(123)} vs {bot_name(74)}"
    rev_key = f"{bot_name(74)} vs {bot_name(123)}"
    errors = [
        f"{rev_key} cited games=10, snapshot has games=5 (key {key})",
        f"{rev_key} cited a_wins=2, snapshot has a_wins=3 (key {key})",
    ]

    guidance = evidence_snapshot.h2h_citation_repair_guidance(124, errors, source_v=123)

    assert f"canonical_citation: {bot_name(123)} vs {bot_name(74)}: games=5, a_wins=3, b_wins=2, draws=0, win_rate=0.6000" in guidance
    assert "v123 perspective: 3W/2L, wr=0.6000" in guidance
    assert "Do not replace them with live H2H" in guidance


def test_master_citation_failure_feedback_binds_path_errors_and_guidance():
    import tool_planning

    feedback = tool_planning._h2h_citation_audit_feedback(
        124,
        ["games mismatch", "a_wins mismatch"],
        "canonical row: 3W/2L",
    )

    assert "v124/evidence_snapshot/head_to_head.json" in feedback
    assert "games mismatch; a_wins mismatch" in feedback
    assert feedback.endswith("canonical row: 3W/2L")


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
        f"{bot_name(1)} vs {bot_name(2)}": {"games": 2, "a_wins": 1, "b_wins": 1, "draws": 0}
    })
    baseline = tmp_path / bot_name(20)
    target = tmp_path / bot_name(24)
    for root in (baseline, target):
        root.mkdir()
        (root / "policy.py").write_text(
            "def get_baseline_decision(context):\n"
            "    return iter_decisions(context)\n\n"
            "def iter_decisions(context):\n"
            "    return context\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(
        agent_master,
        "get_bot_dir",
        lambda version: baseline if int(version) == 20 else target,
    )
    assert evidence_snapshot.ensure_generation_h2h_snapshot(24)["available"] is True
    captured = {}
    targeted_failure = "The selected frozen-evidence mechanism fixes one reachable leak."
    proposal = {
        "schema_version": "master-proposal-v4",
        "targeted_failure": targeted_failure,
        "structural_change": "Replace one reachable frozen-evidence branch with a deadline-bounded mechanism.",
        "counterfactual": "Hold cards, state, seed, and legality fixed while toggling only this mechanism.",
        "measurement": (
            f"target={bot_name(2)}; primary=complete_70_hand_wld; "
            "expected_delta=0.03; samples=>=30_complete_matches; "
            "uncertainty=wilson_wld_interval; secondary=net_chip_ci"
        ),
        "why_not_threshold_tuning": "The mechanism replaces reachable state flow instead of changing one cutoff.",
        "mechanism_target": "deadline",
        "expected_diff": "Change policy.py:iter_decisions so the strategy decision path consumes the selected structural mechanism before the deadline.",
        "target_files": ["policy.py"],
        "source_symbols": [
            "policy.py:get_baseline_decision",
            "policy.py:iter_decisions",
        ],
        "change_symbol": "policy.py:iter_decisions",
        "reachable_chain": [
            "policy.py:get_baseline_decision",
            "policy.py:iter_decisions",
        ],
        "falsifier": {
            "test_name": "fast_policy_baseline",
            "state_learning_primary": "sample_counted_candidate_batch",
            "intervention_target": "deadline",
            "control": "The frozen parent preserves the original paired decision with sample_count=1 before the deadline.",
            "intervention": "Only the selected frozen-evidence deadline mechanism is enabled.",
            "expected_observation": "The intervention changes the target action while control does not.",
        },
        "evidence_refs": [
            "source:policy.py:get_baseline_decision",
            "source:policy.py:iter_decisions",
        ],
        "risks": "Frozen evidence may be sparse, so the fallback and implementation remain bounded.",
    }
    proposal_id = agent_master._proposal_identity(proposal)
    proposal["proposal_id"] = proposal_id
    from tests.test_master_success_return import _strict_prompt_plan

    worker_task = _strict_prompt_plan()["tasks"][0]
    worker_task["worker_prompt"] = (
        "Change policy.py:iter_decisions in the target bot. Preserve the typed "
        "runtime contract and execute all declared checks."
    )
    valid_plan = {
        "analysis": "use stable snapshot",
        "targeted_failure": targeted_failure,
        "expected_behavior_change": "one changed decision",
        "do_not_touch": ["national_bot.py"],
        "measurement_plan": proposal["measurement"],
        "tasks": [worker_task],
        "selected_proposal_id": proposal_id,
    }

    async def fake_run_claude_query(prompt, *_args, **_kwargs):
        captured["prompt"] = prompt
        return "```json\n" + json.dumps(valid_plan) + "\n```", 0.0, {}

    monkeypatch.setattr(agent_master, "run_claude_query", fake_run_claude_query)

    async def fake_ensemble(*_args, **_kwargs):
        packet = _valid_proposal_packet(
            agent_master,
            proposal,
            tmp_path / "master_proposal_invocations",
            source_dir=baseline,
        )
        valid_plan["selected_proposal_id"] = packet["ordered_proposals"][0][
            "proposal_id"
        ]
        return json.dumps(packet)

    monkeypatch.setattr(agent_master, "_run_master_proposal_ensemble", fake_ensemble)

    result = asyncio.run(agent_master._run_master_analysis(20, 24, "flat", _UI()))

    assert result is not None
    assert "web/core/results/v24/evidence_snapshot/head_to_head.json" in captured["prompt"]
    assert "Do not read live H2H for matchup counts during planning" in captured["prompt"]
    assert "do not reopen live glicko_ratings.json, bot_stats.json, rating_history.jsonl" in captured["prompt"]
    assert "Selection evidence snapshot:" in captured["prompt"]
    assert "web/core/results/glicko_ratings.json` —" not in captured["prompt"]


def test_master_plan_audit_prompt_includes_snapshot_json(monkeypatch, tmp_path):
    import audit_agents
    import evidence_snapshot

    _patch_h2h_paths(monkeypatch, tmp_path, {
        f"{bot_name(11)} vs {bot_name(20)}": {
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
                "evidence_alignment": "aligned",
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
    assert f"{bot_name(11)} vs {bot_name(20)}" in captured["prompt"]
    assert "live-file drift after snapshot creation is not" in captured["prompt"]


def test_hard_critic_uses_generation_snapshot_not_live_h2h(monkeypatch, tmp_path):
    import agent_review
    import evidence_snapshot

    _patch_h2h_paths(monkeypatch, tmp_path, {
        f"{bot_name(11)} vs {bot_name(20)}": {
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
    monkeypatch.setattr(
        agent_review,
        "_critic_code_evidence",
        lambda *_args, **_kwargs: {
            "lineage_contract": "exact current-epoch source and target",
            "evaluation_steps": "use the exact injected diff",
            "prompt_section": "# SYSTEM-SUPPLIED EXACT POLICY DIFF\n(no changes)",
        },
    )

    result = asyncio.run(agent_review._run_critic(24, 20, "{}", _UI()))

    assert result["approved"] is True
    assert "Stable H2H Snapshot Contract" in captured["prompt"]
    assert f"{bot_name(11)} vs {bot_name(20)}" in captured["prompt"]
    assert "Never read live `web/core/results/`" in captured["prompt"]
    assert "another checkout's results" in captured["prompt"]
    assert "there is no live fallback" in captured["prompt"]
