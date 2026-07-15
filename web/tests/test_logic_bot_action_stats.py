"""Native-only action diagnostic tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
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


def _digest(value) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _resign_cache(payload: dict) -> None:
    for row in payload["files"].values():
        unsigned = {key: value for key, value in row.items() if key != "row_digest"}
        row["row_digest"] = _digest(unsigned)
    unsigned = {key: value for key, value in payload.items() if key != "payload_digest"}
    payload["payload_digest"] = _digest(unsigned)


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


def test_identity_bound_cache_is_exact_digest_metadata_not_action_authority(tmp_path):
    replay = _write_replay(tmp_path)
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
    assert set(cache) == {
        "schema_version",
        "epoch",
        "execution_mode",
        "evaluation_identity_digest",
        "files",
        "payload_digest",
    }
    assert cache["schema_version"] == 4
    assert cache["epoch"] == "national_tcp_policy_v1"
    assert cache["evaluation_identity_digest"] == IDENTITY
    row = cache["files"][replay["id"]]
    assert set(row) == {
        "etag", "replay_sha256", "contribution_digest", "row_digest",
    }
    assert len(row["replay_sha256"]) == 64
    assert "contribution" not in row
    _resign_cache(cache)
    assert json.loads(cache_path.read_text(encoding="utf-8")) == cache

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
    assert refreshed["schema_version"] == 4
    assert refreshed["evaluation_identity_digest"] == IDENTITY


def test_resigned_forged_cache_cannot_inject_contribution(tmp_path):
    replay = _write_replay(tmp_path)
    cache_path = tmp_path / ".stats_etag.json"
    kwargs = {
        "active_bots": ["national_v143", "national_v144"],
        "replays_dir": tmp_path,
        "etag_path": cache_path,
        "expected_evaluation_identity_digest": IDENTITY,
    }
    expected = compute_all_bot_stats(**kwargs)
    forged = json.loads(cache_path.read_text(encoding="utf-8"))
    forged_row = forged["files"][replay["id"]]
    forged_row["contribution_digest"] = "f" * 64
    _resign_cache(forged)
    cache_path.write_text(json.dumps(forged), encoding="utf-8")

    actual = compute_all_bot_stats(**kwargs)

    assert actual == expected
    refreshed = json.loads(cache_path.read_text(encoding="utf-8"))
    assert refreshed["files"][replay["id"]]["contribution_digest"] != "f" * 64


def test_forged_cache_cannot_mask_replay_identity_drift(tmp_path):
    replay = _write_replay(tmp_path)
    replay_path = tmp_path / replay["id"]
    cache_path = tmp_path / ".stats_etag.json"
    kwargs = {
        "active_bots": ["national_v143", "national_v144"],
        "replays_dir": tmp_path,
        "etag_path": cache_path,
        "expected_evaluation_identity_digest": IDENTITY,
    }
    compute_all_bot_stats(**kwargs)
    forged = json.loads(cache_path.read_text(encoding="utf-8"))
    replay["evaluation_identity_digest"] = "b" * 64
    drifted_raw = json.dumps(replay).encode("utf-8")
    replay_path.write_bytes(drifted_raw)
    forged_row = forged["files"][replay["id"]]
    forged_row["replay_sha256"] = hashlib.sha256(drifted_raw).hexdigest()
    _resign_cache(forged)
    cache_path.write_text(json.dumps(forged), encoding="utf-8")

    actual = compute_all_bot_stats(**kwargs)

    assert actual == {"national_v143": {}, "national_v144": {}}
    refreshed = json.loads(cache_path.read_text(encoding="utf-8"))
    assert refreshed["files"] == {}


def test_same_etag_replay_rewrite_is_rehashed_and_rederived(tmp_path):
    replay = _write_replay(tmp_path)
    replay_path = tmp_path / replay["id"]
    cache_path = tmp_path / ".stats_etag.json"
    kwargs = {
        "active_bots": ["national_v143", "national_v144"],
        "replays_dir": tmp_path,
        "etag_path": cache_path,
        "expected_evaluation_identity_digest": IDENTITY,
    }
    before = compute_all_bot_stats(**kwargs)
    before_cache = json.loads(cache_path.read_text(encoding="utf-8"))
    before_stat = replay_path.stat()
    before_bytes = replay_path.read_bytes()

    replay["games"][0]["hand_records"][0]["actions"][0]["action"] = "allin"
    after_bytes = json.dumps(replay).encode("utf-8")
    assert len(after_bytes) == len(before_bytes)
    replay_path.write_bytes(after_bytes)
    os.utime(
        replay_path,
        ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns),
    )
    assert replay_path.stat().st_size == before_stat.st_size
    assert replay_path.stat().st_mtime_ns == before_stat.st_mtime_ns

    after = compute_all_bot_stats(**kwargs)

    assert before["national_v143"]["national_v144"]["preflop"]["allin"] == 1
    assert after["national_v143"]["national_v144"]["preflop"]["allin"] == 2
    after_cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert (
        after_cache["files"][replay["id"]]["replay_sha256"]
        != before_cache["files"][replay["id"]]["replay_sha256"]
    )


def test_old_same_identity_contribution_cache_is_recomputed(tmp_path):
    _write_replay(tmp_path)
    cache_path = tmp_path / ".stats_etag.json"
    cache_path.write_text(json.dumps({
        "schema_version": 3,
        "epoch": "national_tcp_policy_v1",
        "execution_mode": "native_tcp",
        "evaluation_identity_digest": IDENTITY,
        "files": {
            "strict.json": {
                "etag": "0:0",
                "replay_sha256": "0" * 64,
                "contribution": {
                    "bot0": "national_v143",
                    "bot1": "national_v144",
                    "actions": [],
                    "trackers": {},
                    "hands": [],
                },
            },
        },
    }), encoding="utf-8")

    stats = compute_all_bot_stats(
        ["national_v143", "national_v144"],
        tmp_path,
        etag_path=cache_path,
        expected_evaluation_identity_digest=IDENTITY,
    )

    assert stats["national_v143"]["national_v144"]["preflop"]["total"] == 2
    refreshed = json.loads(cache_path.read_text(encoding="utf-8"))
    assert refreshed["schema_version"] == 4
    assert "contribution" not in refreshed["files"]["strict.json"]


def test_replay_symlink_and_hardlink_are_not_evidence(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
    outside.write_text(json.dumps(make_strict_replay("linked.json")), encoding="utf-8")
    try:
        (tmp_path / "linked.json").symlink_to(outside)
        symlink_stats = compute_all_bot_stats(
            ["national_v143", "national_v144"],
            tmp_path,
            allowed_replay_ids={"linked.json"},
            expected_evaluation_identity_digest=IDENTITY,
        )
        assert symlink_stats == {"national_v143": {}, "national_v144": {}}

        (tmp_path / "linked.json").unlink()
        os.link(outside, tmp_path / "linked.json")
        hardlink_stats = compute_all_bot_stats(
            ["national_v143", "national_v144"],
            tmp_path,
            allowed_replay_ids={"linked.json"},
            expected_evaluation_identity_digest=IDENTITY,
        )
        assert hardlink_stats == {"national_v143": {}, "national_v144": {}}
    finally:
        outside.unlink(missing_ok=True)


def test_unsafe_cache_links_are_ignored_without_mutating_their_targets(tmp_path):
    _write_replay(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-forged-cache.json"
    forged_bytes = b'{"forged":true}'
    outside.write_bytes(forged_bytes)
    cache_path = tmp_path / ".stats_etag.json"
    kwargs = {
        "active_bots": ["national_v143", "national_v144"],
        "replays_dir": tmp_path,
        "etag_path": cache_path,
        "expected_evaluation_identity_digest": IDENTITY,
    }
    try:
        cache_path.symlink_to(outside)
        stats = compute_all_bot_stats(**kwargs)
        assert stats["national_v143"]["national_v144"]["preflop"]["total"] == 2
        assert outside.read_bytes() == forged_bytes

        cache_path.unlink()
        os.link(outside, cache_path)
        stats = compute_all_bot_stats(**kwargs)
        assert stats["national_v143"]["national_v144"]["preflop"]["total"] == 2
        assert outside.read_bytes() == forged_bytes
    finally:
        outside.unlink(missing_ok=True)


def test_incremental_and_concurrent_calls_remain_deterministic(tmp_path):
    _write_replay(tmp_path, "first.json")
    kwargs = {
        "active_bots": ["national_v143", "national_v144"],
        "replays_dir": tmp_path,
        "etag_path": tmp_path / ".stats_etag.json",
        "expected_evaluation_identity_digest": IDENTITY,
    }
    first = compute_all_bot_stats(**kwargs)
    assert first["national_v143"]["national_v144"]["total_hands"] == 70
    _write_replay(tmp_path, "second.json")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _index: compute_all_bot_stats(**kwargs), range(8)))

    assert all(result == results[0] for result in results)
    assert results[0]["national_v143"]["national_v144"]["total_hands"] == 140
    cache = json.loads((tmp_path / ".stats_etag.json").read_text(encoding="utf-8"))
    assert set(cache["files"]) == {"first.json", "second.json"}
    _resign_cache(cache)
