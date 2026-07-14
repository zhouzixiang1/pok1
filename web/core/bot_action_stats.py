"""Action diagnostics from strict national TCP policy hand records.

The module consumes only complete ``national_tcp_policy_v1`` replay envelopes
validated by :mod:`replay_analysis`.  Full native ``hand_records`` are the sole
action authority; diagnostic stderr, bot-owned telemetry, and alternate replay
shapes are never consulted.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable

from bot_namespace import (
    EVALUATION_EPOCH,
    FIRST_STRICT_POLICY_VERSION,
    parse_bot_version,
)
from evolution_infra import RESULTS_DIR, write_locked_json
from replay_analysis import STREETS, validate_native_replay


MAX_ACTION_STATS_CYCLE_LAG = 5
ACTION_STATS_CACHE_SCHEMA_VERSION = 3
_ETAG_FILENAME = ".stats_etag.json"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ACTIONS = ("fold", "call", "check", "raise", "allin")


def _strict_label(value: Any) -> bool:
    version = parse_bot_version(value if isinstance(value, str) else None)
    return version is not None and version >= FIRST_STRICT_POLICY_VERSION


def _expected_identity(explicit: str | None) -> str | None:
    if isinstance(explicit, str) and _HEX64.fullmatch(explicit):
        return explicit
    try:
        from evaluation_data_identity import current_evaluation_digest

        value = current_evaluation_digest(RESULTS_DIR)
    except Exception:
        return None
    return value if isinstance(value, str) and _HEX64.fullmatch(value) else None


def _empty_street_stats() -> dict[str, int]:
    return {
        "total": 0,
        "fold": 0,
        "call": 0,
        "raise": 0,
        "check": 0,
        "allin": 0,
        "fold_to_bet": 0,
        "cbet": 0,
        "barrel": 0,
        "open_raise": 0,
        "three_bet": 0,
        "bb_defend": 0,
        "river_call_down": 0,
    }


def _facing_bet(action: dict[str, Any], player_idx: int) -> bool:
    bets = action.get("player_bets_before")
    return bool(
        isinstance(bets, list)
        and len(bets) == 2
        and isinstance(bets[player_idx], int)
        and isinstance(bets[1 - player_idx], int)
        and bets[player_idx] < bets[1 - player_idx]
    )


def _extract_validated_actions(replay: dict[str, Any]) -> list[dict[str, Any]]:
    labels = (str(replay["bot0"]), str(replay["bot1"]))
    rows: list[dict[str, Any]] = []
    replay_id = str(replay["id"])
    for game_index, game in enumerate(replay["games"], start=1):
        for hand in game["hand_records"]:
            raised_by_street = {0: set(), 1: set()}
            preflop_raise_count = 0
            pending_aggressor: dict[str, int | None] = {
                street: None for street in STREETS
            }
            sb_idx, bb_idx = int(hand["sb_idx"]), int(hand["bb_idx"])
            for action_index, action in enumerate(hand["actions"]):
                player_idx = int(action["player_idx"])
                street = str(action["stage"])
                action_name = str(action["action"])
                street_index = STREETS.index(street)
                is_aggressive = action_name in {"raise", "allin"}
                facing = (
                    pending_aggressor[street] == 1 - player_idx
                    or _facing_bet(action, player_idx)
                )
                is_open = street == "preflop" and is_aggressive and preflop_raise_count == 0
                is_three_bet = street == "preflop" and is_aggressive and preflop_raise_count >= 1
                cbet = street == "flop" and is_aggressive and 0 in raised_by_street[player_idx]
                barrel = (
                    street_index >= 2
                    and is_aggressive
                    and street_index - 1 in raised_by_street[player_idx]
                )
                bb_defend = (
                    street == "preflop"
                    and player_idx == bb_idx
                    and facing
                    and action_name in {"call", "raise", "allin"}
                )
                rows.append({
                    "bot": labels[player_idx],
                    "opponent": labels[1 - player_idx],
                    "player_idx": player_idx,
                    "position": "SB" if player_idx == sb_idx else "BB",
                    "street": street,
                    "action": action_name,
                    "match_id": replay_id,
                    "game": game_index,
                    "hand": int(hand["hand"]),
                    "action_index": action_index,
                    "facing_bet": facing,
                    "fold_to_bet": action_name == "fold" and facing,
                    "cbet": cbet,
                    "barrel": barrel,
                    "open_raise": is_open,
                    "three_bet": is_three_bet,
                    "bb_defend": bb_defend,
                    "river_call_down": street == "river" and action_name == "call" and facing,
                    "raise_to": int(action["amount"]) if action_name == "raise" else None,
                    "pot_before": action.get("pot_before"),
                    "pot_after": action.get("pot_after"),
                })
                if is_aggressive:
                    raised_by_street[player_idx].add(street_index)
                    pending_aggressor[street] = player_idx
                    if street == "preflop":
                        preflop_raise_count += 1
                elif action_name in {"call", "fold"} and facing:
                    pending_aggressor[street] = None
    return rows


def extract_actions_from_replay(
    replay_json: Any,
    *,
    expected_evaluation_identity_digest: str | None = None,
) -> list[dict[str, Any]]:
    """Return authoritative native action rows, or ``[]`` when rejected."""

    validation = validate_native_replay(
        replay_json,
        expected_evaluation_identity_digest=expected_evaluation_identity_digest,
    )
    if not validation.accepted:
        return []
    return _extract_validated_actions(replay_json)


def _hole_bucket(cards: Any) -> str:
    if not isinstance(cards, list) or len(cards) != 2:
        return "unknown"
    try:
        first_suit, first_rank = (int(value) for value in cards[0][1:-1].split(","))
        second_suit, second_rank = (int(value) for value in cards[1][1:-1].split(","))
    except (ValueError, TypeError, IndexError):
        return "unknown"
    if first_rank == second_rank:
        return "pair_high" if first_rank >= 8 else "pair_low"
    high, low = max(first_rank, second_rank), min(first_rank, second_rank)
    suited = first_suit == second_suit
    if high >= 10 and low >= 8:
        return "broadway_suited" if suited else "broadway_offsuit"
    if suited and high - low <= 3:
        return "suited_connected"
    if high == 12:
        return "ace_x_suited" if suited else "ace_x_offsuit"
    return "other_suited" if suited else "other_offsuit"


def _counter_template() -> dict[str, int]:
    return {"opportunities": 0, **{action: 0 for action in _ACTIONS}}


def _tracker_template() -> dict[str, Any]:
    return {
        "hands_started": 0,
        "hands_completed": 0,
        "showdowns": 0,
        "raw_street_actions": {street: Counter() for street in STREETS},
        "semantic_street_actions": {street: Counter() for street in STREETS},
        "facing_raise": Counter(_counter_template()),
        "facing_allin": Counter(_counter_template()),
        "facing_raise_by_street": {
            street: Counter(_counter_template()) for street in STREETS
        },
        "facing_allin_by_street": {
            street: Counter(_counter_template()) for street in STREETS
        },
        "showdown_bucket_counts": Counter(),
        "showdown_range_samples": 0,
    }


def _build_trackers(replay: dict[str, Any]) -> dict[str, dict[str, Any]]:
    labels = (str(replay["bot0"]), str(replay["bot1"]))
    trackers = {label: _tracker_template() for label in labels}
    for game in replay["games"]:
        for hand in game["hand_records"]:
            actions = hand["actions"]
            for label in labels:
                trackers[label]["hands_started"] += 1
                trackers[label]["hands_completed"] += 1
            for action in actions:
                player_idx = int(action["player_idx"])
                tracker = trackers[labels[player_idx]]
                tracker["raw_street_actions"][action["stage"]][action["action"]] += 1
                tracker["semantic_street_actions"][action["stage"]][action["action"]] += 1
            for index, aggressive in enumerate(actions):
                if aggressive["action"] not in {"raise", "allin"}:
                    continue
                aggressor = int(aggressive["player_idx"])
                response = next((
                    row for row in actions[index + 1:]
                    if row["stage"] == aggressive["stage"]
                    and int(row["player_idx"]) == 1 - aggressor
                ), None)
                if response is None:
                    continue
                responder = labels[1 - aggressor]
                category = "facing_allin" if aggressive["action"] == "allin" else "facing_raise"
                action_name = str(response["action"])
                trackers[responder][category]["opportunities"] += 1
                trackers[responder][category][action_name] += 1
                by_street = trackers[responder][f"{category}_by_street"][aggressive["stage"]]
                by_street["opportunities"] += 1
                by_street[action_name] += 1
            settlement = hand["settlement"]
            if settlement.get("is_showdown") is True:
                for player_idx, label in enumerate(labels):
                    trackers[label]["showdowns"] += 1
                    trackers[label]["showdown_range_samples"] += 1
                    trackers[label]["showdown_bucket_counts"][
                        _hole_bucket(hand["hole_cards"][player_idx])
                    ] += 1
    return trackers


def _serialize_tracker(tracker: dict[str, Any]) -> dict[str, Any]:
    return {
        "hands_started": int(tracker["hands_started"]),
        "hands_completed": int(tracker["hands_completed"]),
        "showdowns": int(tracker["showdowns"]),
        "raw_street_actions": {
            street: dict(tracker["raw_street_actions"][street]) for street in STREETS
        },
        "semantic_street_actions": {
            street: dict(tracker["semantic_street_actions"][street]) for street in STREETS
        },
        "facing_raise": dict(tracker["facing_raise"]),
        "facing_allin": dict(tracker["facing_allin"]),
        "facing_raise_by_street": {
            street: dict(tracker["facing_raise_by_street"][street]) for street in STREETS
        },
        "facing_allin_by_street": {
            street: dict(tracker["facing_allin_by_street"][street]) for street in STREETS
        },
        "showdown_bucket_counts": dict(tracker["showdown_bucket_counts"]),
        "showdown_range_samples": int(tracker["showdown_range_samples"]),
    }


def _build_contribution(replay: dict[str, Any]) -> dict[str, Any]:
    actions = _extract_validated_actions(replay)
    trackers = {
        label: _serialize_tracker(tracker)
        for label, tracker in _build_trackers(replay).items()
    }
    hands = []
    for game_index, game in enumerate(replay["games"], start=1):
        for hand in game["hand_records"]:
            hands.append([str(replay["id"]), game_index, int(hand["hand"])])
    return {
        "bot0": replay["bot0"],
        "bot1": replay["bot1"],
        "actions": actions,
        "trackers": trackers,
        "hands": hands,
    }


def _new_bot_totals() -> dict[str, Any]:
    return {"_hands": set()}


def _ensure_bucket(bot_totals: dict[str, Any], opponent: str) -> dict[str, Any]:
    bucket = bot_totals.get(opponent)
    if bucket is None:
        bucket = {
            **{street: _empty_street_stats() for street in STREETS},
            "_hands": set(),
            "_tracker": _tracker_template(),
        }
        bot_totals[opponent] = bucket
    return bucket


def _merge_counter(target: Counter, source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            target[str(key)] += value


def _merge_tracker(target: dict[str, Any], source: dict[str, Any]) -> None:
    for field in ("hands_started", "hands_completed", "showdowns", "showdown_range_samples"):
        target[field] += int(source.get(field, 0) or 0)
    for field in ("raw_street_actions", "semantic_street_actions"):
        values = source.get(field) or {}
        for street in STREETS:
            _merge_counter(target[field][street], values.get(street) or {})
    for field in ("facing_raise", "facing_allin", "showdown_bucket_counts"):
        _merge_counter(target[field], source.get(field) or {})
    for field in ("facing_raise_by_street", "facing_allin_by_street"):
        values = source.get(field) or {}
        for street in STREETS:
            _merge_counter(target[field][street], values.get(street) or {})


def _aggregate_action(totals: dict[str, Any], action: dict[str, Any]) -> None:
    bot, opponent = str(action["bot"]), str(action["opponent"])
    bucket = _ensure_bucket(totals[bot], opponent)
    street_stats = bucket[action["street"]]
    action_name = action["action"]
    street_stats["total"] += 1
    street_stats[action_name] += 1
    if action_name == "allin":
        street_stats["raise"] += 1
    for flag in (
        "fold_to_bet", "cbet", "barrel", "open_raise", "three_bet",
        "bb_defend", "river_call_down",
    ):
        if action.get(flag):
            street_stats[flag] += 1
    hand_id = (action["match_id"], int(action["game"]), int(action["hand"]))
    bucket["_hands"].add(hand_id)
    totals[bot]["_hands"].add(hand_id)


def _apply_contribution(
    totals: dict[str, Any],
    bot_set: set[str],
    contribution: dict[str, Any],
) -> None:
    bot0, bot1 = contribution.get("bot0"), contribution.get("bot1")
    if bot0 not in bot_set and bot1 not in bot_set:
        return
    for action in contribution.get("actions") or []:
        if action.get("bot") in bot_set:
            _aggregate_action(totals, action)
    for raw_hand in contribution.get("hands") or []:
        if not isinstance(raw_hand, list) or len(raw_hand) != 3:
            continue
        hand_id = (str(raw_hand[0]), int(raw_hand[1]), int(raw_hand[2]))
        for bot, opponent in ((bot0, bot1), (bot1, bot0)):
            if bot not in bot_set:
                continue
            bucket = _ensure_bucket(totals[bot], opponent)
            bucket["_hands"].add(hand_id)
            totals[bot]["_hands"].add(hand_id)
    trackers = contribution.get("trackers") or {}
    for bot, opponent in ((bot0, bot1), (bot1, bot0)):
        if bot not in bot_set:
            continue
        source = trackers.get(bot)
        if isinstance(source, dict):
            _merge_tracker(_ensure_bucket(totals[bot], opponent)["_tracker"], source)


def _rate(counter: dict[str, Any], numerator: str) -> float | None:
    opportunities = int(counter.get("opportunities", 0) or 0)
    return int(counter.get(numerator, 0) or 0) / opportunities if opportunities else None


def _finalize_tracker(tracker: dict[str, Any]) -> dict[str, Any]:
    facing_raise = dict(tracker["facing_raise"])
    facing_allin = dict(tracker["facing_allin"])
    river_raise = tracker["facing_raise_by_street"]["river"]
    river_allin = tracker["facing_allin_by_street"]["river"]
    river_opportunities = river_raise["opportunities"] + river_allin["opportunities"]
    river_calls = river_raise["call"] + river_allin["call"] + river_raise["allin"] + river_allin["allin"]
    showdown_samples = int(tracker["showdown_range_samples"])
    buckets = dict(tracker["showdown_bucket_counts"])
    return {
        "source": "national_native_opponent_tracker",
        "evidence_source": "national_tcp_policy_hand_records",
        "epoch": EVALUATION_EPOCH,
        "hands_started": int(tracker["hands_started"]),
        "hands_completed": int(tracker["hands_completed"]),
        "showdowns": int(tracker["showdowns"]),
        "raw_street_actions": {
            street: dict(tracker["raw_street_actions"][street]) for street in STREETS
        },
        "semantic_street_actions": {
            street: dict(tracker["semantic_street_actions"][street]) for street in STREETS
        },
        "terminal_response": {
            "facing_raise": facing_raise,
            "facing_allin": facing_allin,
            "facing_raise_by_street": {
                street: dict(tracker["facing_raise_by_street"][street]) for street in STREETS
            },
            "facing_allin_by_street": {
                street: dict(tracker["facing_allin_by_street"][street]) for street in STREETS
            },
            "fold_to_raise": _rate(facing_raise, "fold"),
            "fold_to_jam": _rate(facing_allin, "fold"),
            "river_overcall": river_calls / river_opportunities if river_opportunities else None,
            "river_overcall_samples": int(river_opportunities),
        },
        "showdown_range": {
            "samples": showdown_samples,
            "bucket_counts": buckets,
            "bucket_rates": {
                key: int(value) / showdown_samples if showdown_samples else 0.0
                for key, value in buckets.items()
            },
            "class_counts": {},
        },
        "latest_contexts": {},
    }


def _finalize_totals(totals: dict[str, Any], active_bots: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for bot in active_bots:
        output: dict[str, Any] = {}
        for opponent, bucket in totals[bot].items():
            if opponent == "_hands":
                continue
            action_total = sum(bucket[street]["total"] for street in STREETS)
            if action_total <= 0:
                continue
            output[opponent] = {
                **{street: dict(bucket[street]) for street in STREETS},
                "total_hands": len(bucket["_hands"]),
                "opponent_tracker": _finalize_tracker(bucket["_tracker"]),
            }
        result[bot] = output
    return result


def get_global_stats(stats: dict[str, Any], bot: str) -> dict[str, Any]:
    """Collapse a bot's strict per-opponent diagnostics into one view."""

    per_opponent = stats.get(bot) if isinstance(stats, dict) else None
    if not isinstance(per_opponent, dict) or not per_opponent:
        return {}
    aggregate = {street: _empty_street_stats() for street in STREETS}
    total_hands = 0
    trackers: dict[str, Any] = {}
    for opponent, row in per_opponent.items():
        if not isinstance(row, dict):
            continue
        total_hands += int(row.get("total_hands", 0) or 0)
        for street in STREETS:
            source = row.get(street) or {}
            for key in aggregate[street]:
                aggregate[street][key] += int(source.get(key, 0) or 0)
        if isinstance(row.get("opponent_tracker"), dict):
            trackers[opponent] = row["opponent_tracker"]
    if not any(aggregate[street]["total"] for street in STREETS):
        return {}
    return {
        **{street: aggregate[street] for street in STREETS},
        "total_hands": total_hands,
        "aggression_factor": {
            street: (
                aggregate[street]["raise"] / aggregate[street]["call"]
                if aggregate[street]["call"] else None
            )
            for street in STREETS
        },
        "opponent_trackers": trackers,
    }


def _file_etags(root: Path, allowed: set[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        entries = list(root.iterdir())
    except OSError:
        return result
    for path in entries:
        if (
            not path.is_file()
            or path.is_symlink()
            or path.suffix != ".json"
            or path.name.startswith(".")
            or (allowed is not None and path.name not in allowed)
        ):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        result[path.name] = f"{stat.st_mtime_ns}:{stat.st_size}"
    return result


def _load_cache(path: Path, identity: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != ACTION_STATS_CACHE_SCHEMA_VERSION
        or payload.get("epoch") != EVALUATION_EPOCH
        or payload.get("execution_mode") != "native_tcp"
        or payload.get("evaluation_identity_digest") != identity
        or not isinstance(payload.get("files"), dict)
    ):
        return {}
    return payload["files"]


def _save_cache(path: Path, identity: str, files: dict[str, Any]) -> None:
    write_locked_json(path, {
        "schema_version": ACTION_STATS_CACHE_SCHEMA_VERSION,
        "epoch": EVALUATION_EPOCH,
        "execution_mode": "native_tcp",
        "evaluation_identity_digest": identity,
        "files": files,
    })


def compute_all_bot_stats(
    active_bots: Iterable[str],
    replays_dir: str | os.PathLike[str],
    force_full: bool = False,
    etag_path: str | os.PathLike[str] | None = None,
    *,
    allowed_replay_ids: Iterable[str] | None = None,
    expected_evaluation_identity_digest: str | None = None,
) -> dict[str, Any]:
    """Aggregate complete current-identity native hand records in one pass."""

    bots = list(dict.fromkeys(str(bot) for bot in active_bots))
    if not bots or any(not _strict_label(bot) for bot in bots):
        return {bot: {} for bot in bots}
    identity = _expected_identity(expected_evaluation_identity_digest)
    if identity is None:
        return {bot: {} for bot in bots}
    root = Path(replays_dir)
    if not root.is_dir() or root.is_symlink():
        return {bot: {} for bot in bots}
    allowed = None if allowed_replay_ids is None else {str(value) for value in allowed_replay_ids}
    current = _file_etags(root, allowed)
    cache_path = Path(etag_path) if etag_path is not None else root / _ETAG_FILENAME
    cached = {} if force_full else _load_cache(cache_path, identity)
    next_cache: dict[str, Any] = {}
    totals = {bot: _new_bot_totals() for bot in bots}
    bot_set = set(bots)
    for filename in sorted(current):
        cached_row = cached.get(filename)
        contribution = None
        if (
            isinstance(cached_row, dict)
            and cached_row.get("etag") == current[filename]
            and isinstance(cached_row.get("replay_sha256"), str)
            and isinstance(cached_row.get("contribution"), dict)
        ):
            contribution = cached_row["contribution"]
            next_cache[filename] = cached_row
        else:
            path = root / filename
            try:
                raw = path.read_bytes()
                replay = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            validation = validate_native_replay(
                replay,
                expected_evaluation_identity_digest=identity,
                expected_replay_id=filename,
            )
            if not validation.accepted:
                continue
            if replay["bot0"] not in bot_set and replay["bot1"] not in bot_set:
                continue
            contribution = _build_contribution(replay)
            next_cache[filename] = {
                "etag": current[filename],
                "replay_sha256": hashlib.sha256(raw).hexdigest(),
                "contribution": contribution,
            }
        _apply_contribution(totals, bot_set, contribution)
    _save_cache(cache_path, identity, next_cache)
    return _finalize_totals(totals, bots)


def compute_bot_action_stats(
    bot_name: str,
    replays_dir: str | os.PathLike[str],
    *,
    expected_evaluation_identity_digest: str | None = None,
) -> dict[str, Any]:
    per_opponent = compute_all_bot_stats(
        [bot_name],
        replays_dir,
        expected_evaluation_identity_digest=expected_evaluation_identity_digest,
    )
    return get_global_stats(per_opponent, bot_name)


__all__ = [
    "MAX_ACTION_STATS_CYCLE_LAG",
    "compute_all_bot_stats",
    "compute_bot_action_stats",
    "extract_actions_from_replay",
    "get_global_stats",
]
