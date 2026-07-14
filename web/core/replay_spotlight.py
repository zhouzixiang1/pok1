"""Critical-hand spotlight for strict native TCP rating replays.

Only identity-bound ``national_tcp_policy_v1`` hand records are considered.
Rejected, stale, or retired replay formats produce no prompt material and no
manifest citation.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterator

from bot_namespace import EVALUATION_EPOCH
from replay_analysis import validate_native_replay


SPOTLIGHT_MANIFEST_SCHEMA_VERSION = 2


def _cards(cards: Any) -> str:
    if not isinstance(cards, list):
        return ""
    return " ".join(str(card) for card in cards)


def _action_text(action: dict[str, Any] | None) -> str:
    if not isinstance(action, dict):
        return "none"
    value = str(action.get("action") or "none")
    amount = action.get("amount")
    return f"{value} {int(amount)}" if value == "raise" and isinstance(amount, int) else value


def _last_action(record: dict[str, Any], player_idx: int) -> dict[str, Any] | None:
    for action in reversed(record.get("actions") or []):
        if action.get("player_idx") == player_idx:
            return action
    return None


def _decision_pots(
    record: dict[str, Any], action: dict[str, Any] | None
) -> tuple[int, int]:
    if not isinstance(action, dict):
        settlement = record.get("settlement") or {}
        start = int(record.get("starting_pot", 150) or 150)
        return start, int(settlement.get("pot", start) or start)
    before = action.get("pot_before")
    after = action.get("pot_after")
    start = int(before if isinstance(before, int) else record.get("starting_pot", 150) or 150)
    end = int(after if isinstance(after, int) else start)
    return start, end


def _assessment(record: dict[str, Any], bot_idx: int, action: dict[str, Any] | None) -> str:
    settlement = record.get("settlement") or {}
    if settlement.get("is_showdown") is True:
        earnings = settlement.get("earnings") or [0, 0]
        return "showdown_win" if int(earnings[bot_idx]) > 0 else (
            "showdown_loss" if int(earnings[bot_idx]) < 0 else "showdown_split"
        )
    if isinstance(action, dict) and action.get("action") == "fold":
        return "bot_fold"
    winner = settlement.get("winner_idx")
    return "opponent_fold" if winner == bot_idx else "non_showdown_loss"


def _summarize_native_hand(
    record: dict[str, Any],
    bot_idx: int,
    opp_idx: int,
    game_num: int,
    replay_file: str,
    *,
    evaluation_identity_digest: str,
) -> dict[str, Any]:
    """Convert one already-validated native hand into a citation candidate."""

    settlement = record["settlement"]
    earnings = settlement["earnings"]
    bot_action = _last_action(record, bot_idx)
    opp_action = _last_action(record, opp_idx)
    before, after = _decision_pots(record, bot_action)
    stage = str((bot_action or opp_action or {}).get("stage") or "preflop")
    hole_cards = record.get("hole_cards") or [[], []]
    return {
        "game_num": game_num,
        "hand_num": int(record["hand"]),
        "stage": stage,
        "board": _cards(record.get("board") or []),
        "bot_cards": _cards(hole_cards[bot_idx]),
        "bot_action": _action_text(bot_action),
        "opp_action": _action_text(opp_action),
        "pot_before": before,
        "pot_after": after,
        "chip_delta": int(earnings[bot_idx]),
        "swing": abs(int(earnings[bot_idx])),
        "assessment": _assessment(record, bot_idx, bot_action),
        "replay_file": replay_file,
        "evaluation_identity_digest": evaluation_identity_digest,
    }


def _iter_native_hands(
    replay: dict[str, Any],
    bot_idx: int,
    opp_idx: int,
    replay_file: str,
) -> Iterator[dict[str, Any]]:
    digest = str(replay["evaluation_identity_digest"])
    for game_num, game in enumerate(replay["games"], start=1):
        for record in game["hand_records"]:
            yield _summarize_native_hand(
                record,
                bot_idx,
                opp_idx,
                game_num,
                replay_file,
                evaluation_identity_digest=digest,
            )


def build_critical_hands_evidence(
    bot_name: str,
    replays_dir: str | os.PathLike[str],
    max_hands: int = 10,
    recent_n_files: int = 20,
    *,
    allowed_replay_ids: list[str] | set[str] | tuple[str, ...] | None = None,
    expected_evaluation_identity_digest: str,
) -> dict[str, Any]:
    """Build deterministic replay evidence without writing shared state.

    Planning callers must provide the evaluation identity captured by their
    immutable cycle.  The returned text, citations, and source replay hashes
    can then be stored as one digest-bound evidence-snapshot payload.
    """

    expected_digest = str(expected_evaluation_identity_digest or "")
    empty = {
        "schema_version": SPOTLIGHT_MANIFEST_SCHEMA_VERSION,
        "epoch": EVALUATION_EPOCH,
        "execution_mode": "native_tcp",
        "evaluation_identity_digest": expected_digest or "unavailable",
        "bot": bot_name,
        "text": "",
        "citations": [],
        "source_replays": {},
    }
    if len(expected_digest) != 64:
        return empty
    root = Path(replays_dir)
    if not root.is_dir() or root.is_symlink():
        return empty
    allowed = None if allowed_replay_ids is None else {str(value) for value in allowed_replay_ids}
    files = [
        path for path in root.iterdir()
        if path.is_file()
        and not path.is_symlink()
        and path.suffix == ".json"
        and not path.name.startswith(".")
        and (allowed is None or path.name in allowed)
    ]
    files.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    files = files[: max(0, int(recent_n_files))]

    candidates: list[dict[str, Any]] = []
    replay_digests: dict[str, str] = {}
    artifact_identities: dict[str, dict[str, str]] = {}
    for path in files:
        try:
            raw = path.read_bytes()
            replay = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        validation = validate_native_replay(
            replay,
            expected_evaluation_identity_digest=expected_digest,
            expected_replay_id=path.name,
        )
        if not validation.accepted:
            continue
        if replay.get("bot0") == bot_name:
            bot_idx, opp_idx = 0, 1
        elif replay.get("bot1") == bot_name:
            bot_idx, opp_idx = 1, 0
        else:
            continue
        replay_digests[path.name] = hashlib.sha256(raw).hexdigest()
        first_execution = replay["games"][0]["artifact_execution"]["by_player"]
        artifact_identities[path.name] = {
            label: str(identity["identity_digest"])
            for label, identity in first_execution.items()
        }
        candidates.extend(
            hand for hand in _iter_native_hands(replay, bot_idx, opp_idx, str(path))
            if hand["swing"] > 0
        )
    if not candidates:
        return empty

    candidates.sort(
        key=lambda hand: (
            -hand["swing"],
            Path(hand["replay_file"]).name,
            hand["game_num"],
            hand["hand_num"],
        )
    )
    top = candidates[: max(0, int(max_hands))]
    citations: list[dict[str, Any]] = []
    lines = [f"Strict native critical hands for {bot_name}:"]
    for hand in top:
        replay_name = Path(hand["replay_file"]).name
        replay_sha = replay_digests[replay_name]
        base = f"G{hand['game_num']}H{hand['hand_num']}"
        anchor = replay_sha[:8]
        citations.append({
            "id": base,
            "id_anchored": f"{base}#{anchor}",
            "bot": bot_name,
            "game": hand["game_num"],
            "hand": hand["hand_num"],
            "replay_file": replay_name,
            "replay_sha256": replay_sha,
            "anchor": anchor,
            "evaluation_identity_digest": expected_digest,
            "artifact_identity_digests": artifact_identities[replay_name],
        })
        lines.append(
            f"{base}#{anchor} {hand['stage']}: board=[{hand['board']}] "
            f"bot=[{hand['bot_cards']}] act={hand['bot_action']} "
            f"opp={hand['opp_action']} pot={hand['pot_before']}->{hand['pot_after']} "
            f"delta={hand['chip_delta']:+d} ({hand['assessment']})"
        )
    lines.append(
        f"Summary: {len(top)} hands, average absolute swing="
        f"{sum(hand['swing'] for hand in top) / max(1, len(top)):.0f}"
    )
    rendered = "\n".join(lines)
    rendered = rendered if len(rendered) <= 4000 else rendered[:3997] + "..."
    return {
        **empty,
        "text": rendered,
        "citations": citations,
        "source_replays": {
            replay_id: {
                "sha256": digest,
                "artifact_identity_digests": artifact_identities[replay_id],
            }
            for replay_id, digest in sorted(replay_digests.items())
        },
    }


__all__ = ["build_critical_hands_evidence"]
