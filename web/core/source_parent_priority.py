"""Persistent source-parent priority-eval writer (master-stage H2H deep sampling).

The rating daemon's native priority channel (``RESULTS_DIR/priority_eval.json``,
read by ``elo_daemon._load_priority_eval``) has two official writers: the
prepare-time eval_wait intent (targets the current candidate ``next_v`` while
prepare waits for its strength sample) and the post-publication archivist step
(targets the newly published bot with ``min_games=500``). While a generation
grinds through the master phase neither is active — the eval_wait intent has
already been consumed and the archivist step only runs after commit — so the
H2H pairings a Master plan needs to cite starve below the statistical evidence
floor (``cited_sample_too_small``; matchups commonly < 15 games).

This module persistently re-asserts the current generation's SOURCE PARENT
into that channel on every master entry, so the daemon keeps deep-sampling
that bot's pairings throughout plan/audit/repair cycles. It is a control-plane
aid only: fail-soft everywhere, never raises, and always defers to a stronger
official intent.

Arbitration rules (checked in order, under the priority file's EX sidecar):

a) file missing            → write ``{bot: <source parent>, min_games:
                              <source parent current total games> + 300}``;
b) incumbent bot is the
   current candidate        → defer (the eval_wait intent is authoritative
                              while prepare still waits on this bot);
c) incumbent bot is the
   source parent            → defer without rewriting (the intent is already
                              effective; a rewrite would reset ``min_games``
                              and disturb the daemon's games-reached expiry);
d) anything else (stale
   foreign / unreadable)   → overwrite with the source parent.

The write mirrors the existing archivist writer byte-for-byte in mechanism:
EX sidecar lock + temp file + ``fsync`` + ``os.replace`` via
``evolution_infra._atomic_publish_state_text``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from bot_namespace import bot_name
from daemon_management import log_system_event

PRIORITY_EVAL_FILENAME = "priority_eval.json"
STATS_FILENAME = "elo_daemon_stats.json"
# Deep-sampling increment added on top of the source parent's current total
# games. 300 keeps the daemon scheduling that bot's pairings well past the
# point where master citations stop starving, while still expiring naturally
# through the daemon's own games-reached rule.
MIN_GAMES_INCREMENT = 300
# Marker carried in the payload for observability (the daemon reads only
# ``bot`` / ``min_games``; the prepare writer uses "prepare_eval_wait" and the
# archivist writes "publication_id" the same way).
PAYLOAD_SOURCE = "master_source_parent"

DECISION_WRITTEN_MISSING = "written_missing_file"
DECISION_OVERWRITTEN_STALE = "overwritten_stale_intent"
DECISION_DEFERRED_CANDIDATE = "deferred_candidate_eval_wait"
DECISION_DEFERRED_SOURCE = "deferred_source_parent_effective"
DECISION_FAILED = "failed"


def _emit(
    event_type: str,
    severity: str,
    message: str,
    data: dict[str, Any],
) -> None:
    """Persist one assertion event; logging must never raise."""
    try:
        log_system_event(event_type, severity, message, data)
    except Exception:
        pass


def _bot_total_games(results_dir: Path, bot: str) -> int:
    """Total games involving ``bot`` from ``elo_daemon_stats.json`` pairs.

    Pair keys are ``"A vs B"``; both wire directions count, and the split
    mirrors the daemon's own ``key.split(" vs ")`` convention so a bot whose
    name is a substring of another bot's name can never be miscounted.
    Unreadable/malformed stats read as 0.
    """
    try:
        data = json.loads(
            (Path(results_dir) / STATS_FILENAME).read_text(encoding="utf-8")
        )
    except Exception:
        return 0
    pairs = data.get("pairs") if isinstance(data, dict) else None
    if not isinstance(pairs, dict):
        return 0
    total = 0
    for key, value in pairs.items():
        try:
            if bot in str(key).split(" vs "):
                total += int(value or 0)
        except (TypeError, ValueError):
            continue
    return max(0, total)


def _incumbent_bot(path: Path) -> str | None:
    """Best-effort read of the current priority intent's bot name."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict):
        bot = data.get("bot")
        if isinstance(bot, str) and bot:
            return bot
    return None


def assert_source_parent_priority(
    *,
    source_v: int,
    next_v: int,
    results_dir: Path,
) -> dict | None:
    """Re-assert the source parent into the daemon's priority eval channel.

    Returns the written payload dict when this call wrote/overwrote the file
    (rules a/d), or ``None`` when it deferred to a stronger intent (rules b/c)
    or failed fail-soft (IO/permission/garbage input). Never raises.
    """
    source_bot: str | None = None
    try:
        source_v = int(source_v)
        next_v = int(next_v)
        source_bot = bot_name(source_v)
        candidate_bot = bot_name(next_v)
        results_dir = Path(results_dir)
        path = results_dir / PRIORITY_EVAL_FILENAME
        min_games = max(
            1, _bot_total_games(results_dir, source_bot) + MIN_GAMES_INCREMENT
        )
        payload = {
            "bot": source_bot,
            "min_games": min_games,
            "since": time.time(),
            "source": PAYLOAD_SOURCE,
        }
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        )

        import fcntl

        from evolution_infra import _atomic_publish_state_text, _locked_state_sidecar

        # The daemon reads (and expire-unlinks) this file under the same EX
        # sidecar, so the read-decide-write must hold it too: a cooperating
        # atomic writer runs wholly before or wholly after the daemon's
        # consume step.
        with _locked_state_sidecar(path, lock_type=fcntl.LOCK_EX):
            if path.exists():
                incumbent = _incumbent_bot(path)
                if incumbent == candidate_bot:
                    # (b) eval_wait intent for the live candidate wins.
                    _emit(
                        "pipeline.source_parent_priority_deferred",
                        "info",
                        f"source-parent priority deferred to eval_wait intent "
                        f"for candidate {incumbent}",
                        {
                            "decision": DECISION_DEFERRED_CANDIDATE,
                            "bot": source_bot,
                            "incumbent_bot": incumbent,
                            "source_v": source_v,
                            "next_v": next_v,
                        },
                    )
                    return None
                if incumbent == source_bot:
                    # (c) Already effective; rewriting would reset min_games
                    # and disturb the daemon's games-reached expiry.
                    _emit(
                        "pipeline.source_parent_priority_deferred",
                        "info",
                        f"source-parent priority already effective for "
                        f"{source_bot}",
                        {
                            "decision": DECISION_DEFERRED_SOURCE,
                            "bot": source_bot,
                            "incumbent_bot": incumbent,
                            "source_v": source_v,
                            "next_v": next_v,
                        },
                    )
                    return None
                # (d) Stale foreign (or unreadable) intent → overwrite.
                decision = DECISION_OVERWRITTEN_STALE
            else:
                # (a) No intent yet → write the source parent.
                decision = DECISION_WRITTEN_MISSING
            _atomic_publish_state_text(path, encoded)

        _emit(
            "pipeline.source_parent_priority_asserted",
            "info",
            f"source-parent priority asserted for {source_bot} "
            f"(min_games={min_games}, decision={decision})",
            {
                "decision": decision,
                "bot": source_bot,
                "min_games": min_games,
                "source_v": source_v,
                "next_v": next_v,
            },
        )
        return dict(payload)
    except Exception as exc:
        _emit(
            "pipeline.source_parent_priority_failed",
            "warn",
            f"source-parent priority assertion failed: "
            f"{type(exc).__name__}: {str(exc)[:180]}",
            {
                "decision": DECISION_FAILED,
                "bot": source_bot,
                "source_v": source_v,
                "next_v": next_v,
                "error": f"{type(exc).__name__}: {str(exc)[:180]}",
            },
        )
        return None
