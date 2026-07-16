"""Deterministic analysis for authoritative national TCP policy replays.

The active analyzer accepts one replay contract only: a complete
``national_tcp_policy_v1`` rating replay produced by the native TCP runner.
Any other input shape is rejected before replay-derived text can reach an LLM
prompt.

The replay carries two independent identities:

* ``evaluation_identity_digest`` binds the current evaluation semantics; and
* ``artifact_execution`` binds every player to the exact policy artifact that
  was executed for each 70-hand strength sample.

Callers that possess a frozen evaluation bundle should pass its digest as
``expected_evaluation_identity_digest``.  Omitting it still performs complete
structural and self-consistency validation, but prompt-producing callers in
this repository always supply or resolve the expected digest.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Iterable, Iterator

from bot_artifact import canonical_digest
from bot_namespace import (
    EVALUATION_EPOCH,
    FIRST_STRICT_POLICY_VERSION,
    parse_bot_version,
)


REPLAY_SCHEMA_VERSION = 1
EXECUTION_MODE = "native_tcp"
ARTIFACT_EXECUTION_MODE = "direct_content_bound_policy_artifact"
STREETS = ("preflop", "flop", "turn", "river")
ACTION_CATEGORIES = ("fold", "raise", "call", "check", "allin")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CARD = re.compile(r"^<[0-3],(?:[0-9]|1[0-2])>$")


@dataclass(frozen=True)
class ReplayValidation:
    """Result of validating one replay without executing candidate code."""

    accepted: bool
    reason: str = ""
    evaluation_identity_digest: str = ""
    artifact_hashes: tuple[tuple[str, str], ...] = ()


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _num_public_cards_to_street(count: int) -> str:
    """Map an official board-card count to a canonical street label."""

    return {0: "preflop", 3: "flop", 4: "turn", 5: "river"}.get(
        int(count), "invalid"
    )


def _strict_bot_label(value: Any) -> bool:
    version = parse_bot_version(value if isinstance(value, str) else None)
    return version is not None and version >= FIRST_STRICT_POLICY_VERSION


def _valid_execution_identity(identity: Any, label: str) -> bool:
    if not isinstance(identity, dict):
        return False
    unsigned = {key: value for key, value in identity.items() if key != "identity_digest"}
    required = {
        "schema_version",
        "mode",
        "label",
        "artifact_hash",
        "entrypoint",
        "entry_digest",
        "policy_digest",
        "precompute_digest",
        "runtime_manifest_digest",
        "artifact_contract_digest",
        "epoch_receipt_digest",
    }
    if set(unsigned) != required:
        return False
    if identity.get("schema_version") != 1:
        return False
    if identity.get("mode") != ARTIFACT_EXECUTION_MODE:
        return False
    if identity.get("label") != label or identity.get("entrypoint") != "national_bot.py":
        return False
    for key in (
        "artifact_hash",
        "entry_digest",
        "policy_digest",
        "precompute_digest",
        "runtime_manifest_digest",
        "artifact_contract_digest",
        "epoch_receipt_digest",
    ):
        if not isinstance(identity.get(key), str) or not _HEX64.fullmatch(identity[key]):
            return False
    return identity.get("identity_digest") == canonical_digest(unsigned)


def _artifact_execution_hashes(
    payload: Any,
    labels: tuple[str, str],
) -> dict[str, str] | None:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "mode", "by_player"
    }:
        return None
    if payload.get("schema_version") != 1 or payload.get("mode") != ARTIFACT_EXECUTION_MODE:
        return None
    by_player = payload.get("by_player")
    if not isinstance(by_player, dict) or set(by_player) != set(labels):
        return None
    hashes: dict[str, str] = {}
    for label in labels:
        identity = by_player.get(label)
        if not _valid_execution_identity(identity, label):
            return None
        hashes[label] = str(identity["artifact_hash"])
    return hashes


def _valid_action(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    if set(row) - {
        "player_idx",
        "stage",
        "action",
        "amount",
        "pot_before",
        "pot_after",
        "player_bets_before",
        "decision_wait_sec",
        "timeout_budget_sec",
    }:
        return False
    player_idx = _as_int(row.get("player_idx"))
    if player_idx not in (0, 1):
        return False
    if row.get("stage") not in STREETS or row.get("action") not in ACTION_CATEGORIES:
        return False
    amount = row.get("amount")
    if amount is not None and _as_int(amount) is None:
        return False
    for key in ("pot_before", "pot_after"):
        if row.get(key) is not None:
            value = _as_int(row[key])
            if value is None or value < 0:
                return False
    bets = row.get("player_bets_before")
    if bets is not None:
        if not isinstance(bets, list) or len(bets) != 2:
            return False
        if any(_as_int(value) is None or int(value) < 0 for value in bets):
            return False
    return True


def _settlement_earnings(settlement: Any) -> tuple[int, int] | None:
    if not isinstance(settlement, dict):
        return None
    earnings = settlement.get("earnings")
    if not isinstance(earnings, list) or len(earnings) != 2:
        return None
    first, second = (_as_int(earnings[0]), _as_int(earnings[1]))
    if first is None or second is None or first + second != 0:
        return None
    return first, second


def _valid_hand_record(record: Any, expected_hand: int) -> bool:
    if not isinstance(record, dict) or _as_int(record.get("hand")) != expected_hand:
        return False
    if _as_int(record.get("sb_idx")) not in (0, 1) or _as_int(record.get("bb_idx")) not in (0, 1):
        return False
    if int(record["sb_idx"]) == int(record["bb_idx"]):
        return False
    hole_cards = record.get("hole_cards")
    if not isinstance(hole_cards, list) or len(hole_cards) != 2:
        return False
    if any(not isinstance(cards, list) or len(cards) != 2 for cards in hole_cards):
        return False
    board = record.get("board")
    if not isinstance(board, list) or len(board) > 5:
        return False
    all_cards = [card for cards in hole_cards for card in cards] + list(board)
    if (
        any(not isinstance(card, str) or not _CARD.fullmatch(card) for card in all_cards)
        or len(all_cards) != len(set(all_cards))
    ):
        return False
    actions = record.get("actions")
    if not isinstance(actions, list) or not all(_valid_action(row) for row in actions):
        return False
    if _settlement_earnings(record.get("settlement")) is None:
        return False
    return True


def _valid_game(
    game: Any,
    labels: tuple[str, str],
    *,
    timing_plan: Any,
) -> tuple[bool, str, dict[str, str]]:
    if not isinstance(game, dict):
        return False, "game_not_object", {}
    if game.get("execution_mode") != EXECUTION_MODE:
        return False, "game_execution_mode_mismatch", {}
    if _as_int(game.get("hands_played")) != 70 or _as_int(game.get("hands_requested")) != 70:
        return False, "game_not_complete_70_hands", {}
    if game.get("passed_compliance") is not True or game.get("issues") not in ([], None):
        return False, "game_compliance_failed", {}
    try:
        from national_native import validate_native_match_timing_evidence

        timing_issues = validate_native_match_timing_evidence(
            game,
            timing_plan=timing_plan,
        )
    except Exception:
        timing_issues = ["validator_failed"]
    if timing_issues:
        return False, "game_timing_evidence_invalid", {}
    if game.get("bot_a") != labels[0] or game.get("bot_b") != labels[1]:
        return False, "game_player_order_mismatch", {}
    net_a, net_b = _as_int(game.get("net_chips_a")), _as_int(game.get("net_chips_b"))
    if net_a is None or net_b is None or net_a + net_b != 0:
        return False, "game_net_chips_invalid", {}
    hashes = _artifact_execution_hashes(game.get("artifact_execution"), labels)
    if hashes is None:
        return False, "artifact_execution_invalid", {}
    records = game.get("hand_records")
    if not isinstance(records, list) or len(records) != 70:
        return False, "hand_records_incomplete", {}
    for expected_hand, record in enumerate(records, start=1):
        if not _valid_hand_record(record, expected_hand):
            return False, f"hand_record_invalid:{expected_hand}", {}
    settlements = game.get("settlements")
    if not isinstance(settlements, list) or len(settlements) != 70:
        return False, "settlements_incomplete", {}
    settlement_sum = [0, 0]
    for expected_hand, settlement in enumerate(settlements, start=1):
        if not isinstance(settlement, dict) or _as_int(settlement.get("hand")) != expected_hand:
            return False, f"settlement_invalid:{expected_hand}", {}
        earnings = _settlement_earnings(settlement)
        if earnings is None:
            return False, f"settlement_earnings_invalid:{expected_hand}", {}
        settlement_sum[0] += earnings[0]
        settlement_sum[1] += earnings[1]
        record_earnings = _settlement_earnings(records[expected_hand - 1].get("settlement"))
        if record_earnings != earnings:
            return False, f"settlement_record_mismatch:{expected_hand}", {}
    if settlement_sum != [net_a, net_b]:
        return False, "settlement_total_mismatch", {}
    return True, "", hashes


def validate_native_replay(
    replay_data: Any,
    *,
    expected_evaluation_identity_digest: str | None = None,
    expected_replay_id: str | None = None,
) -> ReplayValidation:
    """Validate a replay before analysis or prompt construction.

    Validation is intentionally fail-closed.  A missing field is not inferred
    from filenames, current bot directories, or retired runtime conventions.
    """

    if not isinstance(replay_data, dict):
        return ReplayValidation(False, "replay_not_object")
    if replay_data.get("replay_schema_version") != REPLAY_SCHEMA_VERSION:
        return ReplayValidation(False, "replay_schema_mismatch")
    if replay_data.get("execution_mode") != EXECUTION_MODE:
        return ReplayValidation(False, "replay_execution_mode_mismatch")
    if replay_data.get("evaluation_epoch") != EVALUATION_EPOCH:
        return ReplayValidation(False, "replay_epoch_mismatch")
    digest = replay_data.get("evaluation_identity_digest")
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
        return ReplayValidation(False, "evaluation_identity_invalid")
    if expected_evaluation_identity_digest is not None and digest != expected_evaluation_identity_digest:
        return ReplayValidation(False, "evaluation_identity_mismatch", digest)
    if expected_replay_id is not None and replay_data.get("id") != expected_replay_id:
        return ReplayValidation(False, "replay_id_mismatch", digest)
    try:
        from national_native import require_native_match_timing_plan

        timing_plan = require_native_match_timing_plan(
            replay_data.get("native_match_timing_plan"),
            hands=70,
            requested_timeout_sec=None,
        )
        if replay_data.get("native_match_timing_plan_digest") != timing_plan.digest():
            return ReplayValidation(False, "replay_timing_plan_digest_mismatch", digest)
    except Exception:
        return ReplayValidation(False, "replay_timing_plan_invalid", digest)

    bot0, bot1 = replay_data.get("bot0"), replay_data.get("bot1")
    if not _strict_bot_label(bot0) or not _strict_bot_label(bot1) or bot0 == bot1:
        return ReplayValidation(False, "strict_bot_labels_invalid", digest)
    labels = (str(bot0), str(bot1))
    games = replay_data.get("games")
    if not isinstance(games, list) or not games:
        return ReplayValidation(False, "games_missing", digest)
    expected_samples = _as_int(replay_data.get("strength_sample_count"))
    if expected_samples != len(games):
        return ReplayValidation(False, "strength_sample_count_mismatch", digest)
    if (
        replay_data.get("strength_sample_unit") != "70_hand_match"
        or _as_int(replay_data.get("hands_per_strength_sample")) != 70
        or replay_data.get("strength_admitted") is not True
        or replay_data.get("strength_complete") is not True
        or replay_data.get("strength_compliance_passed") is not True
    ):
        return ReplayValidation(False, "strength_admission_invalid", digest)

    sample_values = replay_data.get("net_chips_bot0")
    if not isinstance(sample_values, list) or len(sample_values) != len(games):
        return ReplayValidation(False, "net_chip_samples_invalid", digest)
    artifact_hashes: dict[str, str] | None = None
    nets: list[int] = []
    for index, game in enumerate(games, start=1):
        accepted, reason, hashes = _valid_game(
            game,
            labels,
            timing_plan=timing_plan,
        )
        if not accepted:
            return ReplayValidation(False, f"game_{index}:{reason}", digest)
        if artifact_hashes is None:
            artifact_hashes = hashes
        elif hashes != artifact_hashes:
            return ReplayValidation(False, "artifact_identity_drift", digest)
        net = _as_int(game.get("net_chips_a"))
        sample = _as_int(sample_values[index - 1])
        if net is None or sample != net:
            return ReplayValidation(False, f"game_{index}:net_sample_mismatch", digest)
        nets.append(net)
    wins = sum(value > 0 for value in nets)
    losses = sum(value < 0 for value in nets)
    draws = sum(value == 0 for value in nets)
    if (
        _as_int(replay_data.get("bot0_wins")) != wins
        or _as_int(replay_data.get("bot1_wins")) != losses
        or _as_int(replay_data.get("draws")) != draws
    ):
        return ReplayValidation(False, "outcome_counts_mismatch", digest)
    return ReplayValidation(
        True,
        evaluation_identity_digest=digest,
        artifact_hashes=tuple(sorted((artifact_hashes or {}).items())),
    )


def _replay_bot_indices(replay_data: dict[str, Any], bot_name: str) -> tuple[int | None, int | None]:
    if replay_data.get("bot0") == bot_name:
        return 0, 1
    if replay_data.get("bot1") == bot_name:
        return 1, 0
    return None, None


def _iter_hand_records(replay_data: dict[str, Any]) -> Iterator[tuple[int, dict[str, Any]]]:
    for game_index, game in enumerate(replay_data.get("games") or [], start=1):
        for record in game.get("hand_records") or []:
            yield game_index, record


def _iter_bot_actions(replay_data: dict[str, Any], bot_idx: int) -> Iterator[dict[str, Any]]:
    for game_index, hand in _iter_hand_records(replay_data):
        for action_index, action in enumerate(hand.get("actions") or []):
            if action.get("player_idx") != bot_idx:
                continue
            yield {
                **action,
                "game": game_index,
                "hand": hand.get("hand"),
                "action_index": action_index,
            }


def extract_street_patterns(
    replay_data: dict[str, Any],
    bot_name: str,
    *,
    expected_evaluation_identity_digest: str | None = None,
) -> str:
    """Return bounded per-street frequencies from a validated replay."""

    validation = validate_native_replay(
        replay_data,
        expected_evaluation_identity_digest=expected_evaluation_identity_digest,
    )
    bot_idx, _ = _replay_bot_indices(replay_data, bot_name)
    if not validation.accepted or bot_idx is None:
        return ""
    counts = {street: Counter() for street in STREETS}
    raise_ratios = {street: [] for street in STREETS}
    for action in _iter_bot_actions(replay_data, bot_idx):
        street, category = action["stage"], action["action"]
        counts[street][category] += 1
        amount, pot = _as_float(action.get("amount")), _as_float(action.get("pot_before"))
        if category == "raise" and amount is not None and pot is not None and pot > 0:
            raise_ratios[street].append(amount / pot)
    lines: list[str] = []
    for street in STREETS:
        total = sum(counts[street].values())
        if not total:
            continue
        parts = [
            f"{category}={100 * counts[street][category] / total:.1f}%"
            for category in ACTION_CATEGORIES
            if counts[street][category]
        ]
        if raise_ratios[street]:
            parts.append(f"avg_raise_to/pot={sum(raise_ratios[street]) / len(raise_ratios[street]):.2f}")
        lines.append(f"  {street}: " + ", ".join(parts))
    return "\n".join(lines)


def _empty_fingerprint() -> dict[str, Any]:
    return {
        "epoch": EVALUATION_EPOCH,
        "execution_mode": EXECUTION_MODE,
        "per_street_freq": {
            street: {action: 0.0 for action in ACTION_CATEGORIES}
            for street in STREETS
        },
        "per_street_avg_raise_to_pot": {street: None for street in STREETS},
        "aggression_factor": None,
        "vpip": None,
        "river_continue_rate": None,
        "total_actions": 0,
    }


def extract_behavior_fingerprint(
    replay_data: dict[str, Any],
    bot_name: str,
    *,
    expected_evaluation_identity_digest: str | None = None,
) -> dict[str, Any]:
    """Build a native-action fingerprint, or an empty fingerprint if rejected."""

    validation = validate_native_replay(
        replay_data,
        expected_evaluation_identity_digest=expected_evaluation_identity_digest,
    )
    bot_idx, _ = _replay_bot_indices(replay_data, bot_name)
    if not validation.accepted or bot_idx is None:
        return _empty_fingerprint()
    counts = {street: Counter() for street in STREETS}
    ratios = {street: [] for street in STREETS}
    for action in _iter_bot_actions(replay_data, bot_idx):
        counts[action["stage"]][action["action"]] += 1
        amount, pot = _as_float(action.get("amount")), _as_float(action.get("pot_before"))
        if action["action"] == "raise" and amount is not None and pot is not None and pot > 0:
            ratios[action["stage"]].append(amount / pot)
    fp = _empty_fingerprint()
    total_actions = 0
    aggressive = calls = 0
    for street in STREETS:
        total = sum(counts[street].values())
        total_actions += total
        if total:
            fp["per_street_freq"][street] = {
                action: counts[street][action] / total for action in ACTION_CATEGORIES
            }
        if ratios[street]:
            fp["per_street_avg_raise_to_pot"][street] = sum(ratios[street]) / len(ratios[street])
        aggressive += counts[street]["raise"] + counts[street]["allin"]
        calls += counts[street]["call"]
    fp["total_actions"] = total_actions
    fp["aggression_factor"] = aggressive / calls if calls else (float(aggressive) if aggressive else None)
    preflop = sum(counts["preflop"].values())
    fp["vpip"] = (
        (counts["preflop"]["raise"] + counts["preflop"]["call"] + counts["preflop"]["allin"]) / preflop
        if preflop else None
    )
    river = sum(counts["river"].values())
    fp["river_continue_rate"] = (
        (counts["river"]["raise"] + counts["river"]["call"] + counts["river"]["allin"]) / river
        if river else None
    )
    return fp


def _fp_vector(fp: dict[str, Any]) -> list[float]:
    return [
        float((fp.get("per_street_freq") or {}).get(street, {}).get(action, 0.0) or 0.0)
        for street in STREETS
        for action in ACTION_CATEGORIES
    ]


def fingerprint_distance(first: dict[str, Any], second: dict[str, Any]) -> float:
    """Return a normalized distance between two native fingerprints."""

    a, b = _fp_vector(first), _fp_vector(second)
    norm_a = math.sqrt(sum(value * value for value in a))
    norm_b = math.sqrt(sum(value * value for value in b))
    cosine = 0.0 if not norm_a and not norm_b else (
        1.0 if not norm_a or not norm_b
        else 1.0 - sum(x * y for x, y in zip(a, b)) / (norm_a * norm_b)
    )
    scalar_distances = []
    for key in ("aggression_factor", "vpip", "river_continue_rate"):
        x, y = first.get(key), second.get(key)
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            scalar_distances.append(min(1.0, abs(float(x) - float(y))))
    scalar = sum(scalar_distances) / len(scalar_distances) if scalar_distances else 0.0
    return max(0.0, min(1.0, 0.8 * cosine + 0.2 * scalar))


def _showdown_bucket(cards: Any) -> str:
    if not isinstance(cards, list) or len(cards) != 2 or not all(isinstance(card, str) for card in cards):
        return "unknown"
    try:
        parsed = [tuple(int(value) for value in card[1:-1].split(",")) for card in cards]
        (first_suit, first), (second_suit, second) = parsed
    except (ValueError, IndexError, TypeError):
        return "unknown"
    if first == second:
        return "pair_high" if first >= 8 else "pair_low"
    high, low = max(first, second), min(first, second)
    suited = first_suit == second_suit
    if high >= 10 and low >= 8:
        return "broadway_suited" if suited else "broadway_offsuit"
    if suited and high - low <= 3:
        return "suited_connected"
    if high == 12:
        return "ace_x_suited" if suited else "ace_x_offsuit"
    return "other_suited" if suited else "other_offsuit"


def _opponent_observations(
    replay_data: dict[str, Any], bot_idx: int, opp_idx: int
) -> dict[str, Any]:
    terminal = Counter()
    showdown = Counter()
    showdown_samples = 0
    for _game_index, hand in _iter_hand_records(replay_data):
        actions = hand.get("actions") or []
        for index, action in enumerate(actions):
            if action.get("player_idx") != bot_idx or action.get("action") not in {"raise", "allin"}:
                continue
            sample_key = "jam" if action["action"] == "allin" else "raise"
            response = next(
                (
                    row for row in actions[index + 1:]
                    if row.get("stage") == action.get("stage") and row.get("player_idx") == opp_idx
                ),
                None,
            )
            if response is not None:
                terminal[f"{sample_key}_samples"] += 1
                if response.get("action") == "fold":
                    terminal[f"fold_to_{sample_key}"] += 1
                if action.get("stage") == "river":
                    terminal["river_overcall_samples"] += 1
                    if response.get("action") in {"call", "allin"}:
                        terminal["river_overcall"] += 1
        settlement = hand.get("settlement") or {}
        if settlement.get("is_showdown") is True:
            cards = (hand.get("hole_cards") or [[], []])[opp_idx]
            showdown[_showdown_bucket(cards)] += 1
            showdown_samples += 1
    def rate(numerator: str, denominator: str) -> float | None:
        return terminal[numerator] / terminal[denominator] if terminal[denominator] else None
    return {
        "terminal": {
            "fold_to_raise": rate("fold_to_raise", "raise_samples"),
            "fold_to_raise_samples": terminal["raise_samples"],
            "fold_to_jam": rate("fold_to_jam", "jam_samples"),
            "fold_to_jam_samples": terminal["jam_samples"],
            "river_overcall": rate("river_overcall", "river_overcall_samples"),
            "river_overcall_samples": terminal["river_overcall_samples"],
        },
        "showdown_range": {
            "samples": showdown_samples,
            "bucket_counts": dict(sorted(showdown.items())),
        },
    }


def extract_replay_evidence_for_analysis(
    replay_data: dict[str, Any],
    bot_name: str,
    match_id: str = "",
    *,
    expected_evaluation_identity_digest: str | None = None,
) -> dict[str, Any] | None:
    """Return one identity-bound evidence row, or ``None`` if rejected."""

    validation = validate_native_replay(
        replay_data,
        expected_evaluation_identity_digest=expected_evaluation_identity_digest,
        expected_replay_id=match_id or None,
    )
    bot_idx, opp_idx = _replay_bot_indices(replay_data, bot_name)
    if not validation.accepted or bot_idx is None or opp_idx is None:
        return None
    opponent = replay_data["bot1" if bot_idx == 0 else "bot0"]
    deltas = [
        int(game["net_chips_a"]) * (1 if bot_idx == 0 else -1)
        for game in replay_data["games"]
    ]
    wins = sum(delta > 0 for delta in deltas)
    losses = sum(delta < 0 for delta in deltas)
    draws = sum(delta == 0 for delta in deltas)
    observations = _opponent_observations(replay_data, bot_idx, opp_idx)
    fp = extract_behavior_fingerprint(
        replay_data,
        bot_name,
        expected_evaluation_identity_digest=validation.evaluation_identity_digest,
    )
    identity_by_label = {
        label: identity
        for label, identity in (
            replay_data["games"][0]["artifact_execution"]["by_player"]
        ).items()
    }
    payload = {
        "schema_version": 2,
        "epoch": EVALUATION_EPOCH,
        "execution_mode": EXECUTION_MODE,
        "evaluation_identity_digest": validation.evaluation_identity_digest,
        "match_id": match_id or str(replay_data.get("id") or ""),
        "bot": bot_name,
        "opponent": opponent,
        "artifact_identity_digest": identity_by_label[bot_name]["identity_digest"],
        "opponent_artifact_identity_digest": identity_by_label[opponent]["identity_digest"],
        "sample_n": len(deltas),
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "win_rate": (wins + 0.5 * draws) / len(deltas),
        "total_delta": sum(deltas),
        "avg_delta": sum(deltas) / len(deltas),
        "behavior_fingerprint": fp,
        "opponent_terminal": observations["terminal"],
        "showdown_range": observations["showdown_range"],
    }
    payload["evidence_id"] = "native_ev_" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    return payload


def summarize_replay_for_analysis(
    replay_data: dict[str, Any],
    bot_name: str,
    *,
    expected_evaluation_identity_digest: str | None = None,
) -> str:
    """Render bounded, validated evidence for advisory analysis."""

    evidence = extract_replay_evidence_for_analysis(
        replay_data,
        bot_name,
        match_id=str(replay_data.get("id") or ""),
        expected_evaluation_identity_digest=expected_evaluation_identity_digest,
    )
    if evidence is None:
        return ""
    terminal = evidence["opponent_terminal"]
    showdown = evidence["showdown_range"]
    def percent(value: Any) -> str:
        return "unknown" if value is None else f"{100 * float(value):.1f}%"
    lines = [
        f"Native replay {evidence['match_id']}: {evidence['bot']} vs {evidence['opponent']}",
        f"contract: epoch={EVALUATION_EPOCH} mode={EXECUTION_MODE} evaluation={evidence['evaluation_identity_digest']}",
        (
            f"70-hand samples={evidence['sample_n']} W/L/D="
            f"{evidence['wins']}/{evidence['losses']}/{evidence['draws']} "
            f"score={evidence['win_rate']:.3f} avg_net={evidence['avg_delta']:+.1f}"
        ),
    ]
    street_text = extract_street_patterns(
        replay_data,
        bot_name,
        expected_evaluation_identity_digest=evidence["evaluation_identity_digest"],
    )
    if street_text:
        lines.append("native actions:\n" + street_text)
    lines.append(
        "opponent terminal: "
        f"fold_to_raise={percent(terminal['fold_to_raise'])} (n={terminal['fold_to_raise_samples']}), "
        f"fold_to_jam={percent(terminal['fold_to_jam'])} (n={terminal['fold_to_jam_samples']}), "
        f"river_overcall={percent(terminal['river_overcall'])} (n={terminal['river_overcall_samples']})"
    )
    lines.append(
        f"opponent showdown range: n={showdown['samples']} "
        f"buckets={json.dumps(showdown['bucket_counts'], sort_keys=True, separators=(',', ':'))}"
    )
    return "\n".join(lines)[:6000]


__all__ = [
    "ACTION_CATEGORIES",
    "EXECUTION_MODE",
    "REPLAY_SCHEMA_VERSION",
    "ReplayValidation",
    "STREETS",
    "_num_public_cards_to_street",
    "extract_behavior_fingerprint",
    "extract_replay_evidence_for_analysis",
    "extract_street_patterns",
    "fingerprint_distance",
    "summarize_replay_for_analysis",
    "validate_native_replay",
]
