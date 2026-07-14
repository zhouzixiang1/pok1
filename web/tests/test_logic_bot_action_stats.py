"""Native-only action diagnostic tests."""

from __future__ import annotations

from copy import deepcopy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from bot_action_stats import (
    compute_all_bot_stats,
    compute_bot_action_stats,
    extract_actions_from_replay,
    get_global_stats,
)
from test_logic_replay_analysis import IDENTITY, make_strict_replay


def _write_replay(root: Path, name: str = "strict.json") -> dict:
    replay = make_strict_replay(name)
    root.joinpath(name).write_text(json.dumps(replay), encoding="utf-8")
    return replay


def test_extracts_only_native_hand_record_actions():
    replay = make_strict_replay()
    actions = extract_actions_from_replay(
        replay, expected_evaluation_identity_digest=IDENTITY
    )
    assert len(actions) == 6
    assert {row["action"] for row in actions} == {"raise", "fold", "allin", "call"}
    assert all(row["match_id"] == "strict.json" for row in actions)
    assert all(row["street"] in {"preflop", "river"} for row in actions)
    assert all(isinstance(row["player_idx"], int) for row in actions)


def test_rejects_alternate_replay_shape_without_fallback():
    alternate = {
        "bot0": "national_v143",
        "bot1": "national_v144",
        "games": [{"logs": [], "bot0_chips": 1}],
    }
    assert extract_actions_from_replay(alternate) == []


def test_per_opponent_stats_and_terminal_showdown_learning(tmp_path):
    _write_replay(tmp_path)
    stats = compute_all_bot_stats(
        ["national_v143", "national_v144"],
        tmp_path,
        force_full=True,
        expected_evaluation_identity_digest=IDENTITY,
    )
    hero = stats["national_v143"]["national_v144"]
    villain = stats["national_v144"]["national_v143"]
    assert hero["preflop"]["total"] == 2
    assert hero["preflop"]["allin"] == 1
    assert hero["preflop"]["raise"] == 2
    assert hero["preflop"]["open_raise"] == 2
    assert hero["total_hands"] == 70
    assert villain["preflop"]["fold"] == 1
    assert villain["preflop"]["call"] == 1
    assert villain["preflop"]["bb_defend"] == 1

    tracker = villain["opponent_tracker"]
    assert tracker["source"] == "national_native_opponent_tracker"
    assert tracker["evidence_source"] == "national_tcp_policy_hand_records"
    terminal = tracker["terminal_response"]
    assert terminal["fold_to_raise"] == 0.5
    assert terminal["facing_raise"]["opportunities"] == 2
    assert terminal["fold_to_jam"] == 0.0
    assert terminal["facing_allin"]["opportunities"] == 1
    assert terminal["river_overcall"] == 1.0
    assert terminal["river_overcall_samples"] == 1
    assert tracker["showdown_range"]["samples"] == 2
    assert tracker["showdown_range"]["bucket_counts"] == {"broadway_offsuit": 2}


def test_global_stats_preserve_identity_derived_tracker(tmp_path):
    _write_replay(tmp_path)
    per_opponent = compute_all_bot_stats(
        ["national_v143", "national_v144"],
        tmp_path,
        expected_evaluation_identity_digest=IDENTITY,
    )
    flat = get_global_stats(per_opponent, "national_v144")
    assert flat["total_hands"] == 70
    assert flat["preflop"]["total"] == 2
    assert set(flat["opponent_trackers"]) == {"national_v143"}
    assert flat["opponent_trackers"]["national_v143"]["epoch"] == "national_tcp_policy_v1"


def test_single_bot_api_keeps_opponent_context(tmp_path):
    _write_replay(tmp_path)
    flat = compute_bot_action_stats(
        "national_v143",
        tmp_path,
        expected_evaluation_identity_digest=IDENTITY,
    )
    assert flat["preflop"]["total"] == 2
    assert flat["total_hands"] == 70


def test_wrong_identity_and_pre_policy_bot_fail_closed(tmp_path):
    _write_replay(tmp_path)
    wrong = compute_all_bot_stats(
        ["national_v143", "national_v144"],
        tmp_path,
        expected_evaluation_identity_digest="f" * 64,
    )
    assert wrong == {"national_v143": {}, "national_v144": {}}
    archived = compute_all_bot_stats(
        ["national_v142"],
        tmp_path,
        expected_evaluation_identity_digest=IDENTITY,
    )
    assert archived == {"national_v142": {}}


def test_allowed_replay_ids_are_an_exact_input_window(tmp_path):
    _write_replay(tmp_path, "allowed.json")
    _write_replay(tmp_path, "excluded.json")
    stats = compute_all_bot_stats(
        ["national_v143", "national_v144"],
        tmp_path,
        allowed_replay_ids={"allowed.json"},
        expected_evaluation_identity_digest=IDENTITY,
    )
    assert stats["national_v143"]["national_v144"]["total_hands"] == 70


def test_identity_bound_cache_reuses_unchanged_contribution(tmp_path, monkeypatch):
    replay = _write_replay(tmp_path)
    replay_path = tmp_path / replay["id"]
    cache_path = tmp_path / ".stats_etag.json"
    kwargs = {
        "active_bots": ["national_v143", "national_v144"],
        "replays_dir": tmp_path,
        "etag_path": cache_path,
        "allowed_replay_ids": {replay["id"]},
        "expected_evaluation_identity_digest": IDENTITY,
    }
    first = compute_all_bot_stats(**kwargs)
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache["schema_version"] == 3
    assert cache["epoch"] == "national_tcp_policy_v1"
    assert cache["evaluation_identity_digest"] == IDENTITY
    assert len(cache["files"][replay["id"]]["replay_sha256"]) == 64

    original = Path.read_bytes

    def guarded(path: Path):
        if path.resolve() == replay_path.resolve():
            raise AssertionError("unchanged replay was reparsed")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    second = compute_all_bot_stats(**kwargs)
    assert second == first


def test_cache_from_other_identity_is_not_reused(tmp_path):
    _write_replay(tmp_path)
    cache_path = tmp_path / ".stats_etag.json"
    cache_path.write_text(json.dumps({
        "schema_version": 3,
        "epoch": "national_tcp_policy_v1",
        "execution_mode": "native_tcp",
        "evaluation_identity_digest": "f" * 64,
        "files": {"strict.json": {"etag": "forged", "contribution": {}}},
    }), encoding="utf-8")
    stats = compute_all_bot_stats(
        ["national_v143", "national_v144"],
        tmp_path,
        etag_path=cache_path,
        expected_evaluation_identity_digest=IDENTITY,
    )
    assert stats["national_v143"]["national_v144"]["preflop"]["total"] == 2
    refreshed = json.loads(cache_path.read_text(encoding="utf-8"))
    assert refreshed["evaluation_identity_digest"] == IDENTITY
