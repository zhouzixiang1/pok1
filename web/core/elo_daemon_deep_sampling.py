"""Deep-sampling priority lane for daemon match selection (P1, 2026-10-06).

Extracted companion of ``elo_daemon.pick_matches`` holding the master-demand
scheduling lane.  The delivery blocker: the master statistical-evidence gate
rejects plans whose cited matchup rows sit under the 30-game tier
(``cited_sample_too_small``, 68 rejections in 8h, top blocker) while the
daemon's breadth objective spreads games wide — 0 of 65 H2H pairs reach 30
games and 60 sit under the 15-game annealed floor.  Master plans act on the
CURRENT generation's parent matchups (``source_v`` / crossover ``parent2_v``),
so the scheduler needs a lane that deep-samples exactly those pairs.

Contract
--------
* Selection-only change: this module never touches the rating identity
  ``runtime_profile`` (``_rating_protocol_config``), ``n_pairs``, the worker
  governor, or env knobs.  ``web/tests/test_elo_daemon_deep_sampling.py``
  proves the production-built ``runtime_profile`` bytes are unchanged.
* Data source (read per scheduling pass, no caching of authority): the live
  pipeline checkpoint ``RESULTS_DIR/pipeline_state.json`` —
  ``active_generation.source_v`` first, top-level ``source_v`` as the on-disk
  shape — plus crossover ``parent2_v``.  Unreadable/invalid/absent source
  falls back to the newest published pool bot.
* Priority set: every pool pair with a focus member, in BOTH wire directions.
  An ordered direction stays in the lane while it holds fewer than
  ``DEEP_SAMPLING_DIRECTION_TARGET`` admitted 70-hand samples (counted from
  the authoritative append-only ``match_history.jsonl`` rows, seat order =
  ``(bot0, bot1)``).  A pair leaves the lane when both directions reach the
  target; when every focus pair is saturated the lane empties and
  ``pick_matches`` degenerates byte-for-byte to the legacy breadth loop.
* Breadth is never starved: when at least two non-focus bots exist, at least
  ``max(1, n_picks // 4)`` picks per round are reserved for pairs with no
  unsaturated focus membership, and ``pick_matches`` orders those pairs ahead
  of focus pairs in the breadth fill.
* Observability: explicit ``daemon.deep_sampling_lane_*`` events on phase
  transition only (enabled / saturated / source fallback) plus in-memory
  counters merged into the daemon stats file by ``save_cycle``.
* Crash safety: NO new must-persist state.  Directional counts are re-derived
  from ``match_history.jsonl`` (existing durable authority) via an in-memory
  ``(mtime_ns, size)``-keyed cache; transition/stats state is write-only
  observability and rebuilds as ``disabled`` after a restart.

This module is NOT a ``SEMANTIC_PATHS`` member; the one-line seam it needs in
``elo_daemon.py`` is (that file's evaluator-identity hash is flagged by
``scripts/check_rating_identity_predeploy.py`` — deploy of the seam follows
the documented operator archive-and-restart decision, identical to the
2026-10-05 governor deploy).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from bot_namespace import ACTIVE_BOT_PREFIX, bot_name
from evolution_infra import pair_key, read_locked_json

DEEP_SAMPLING_DIRECTION_TARGET = 30
DEEP_SAMPLING_EVENT_PREFIX = "daemon.deep_sampling_lane"

# Write-only observability (rebuilds after crash; never scheduling input).
_LANE_STATS = {
    "selections": 0,
    "lane_picks": 0,
    "enabled_events": 0,
    "saturated_events": 0,
    "fallback_events": 0,
}
_LANE_TRANSITION_STATE = {}
_DIRECTION_COUNT_CACHE = {"key": None, "counts": {}}


def reset_lane_state_for_tests():
    """Drop in-memory lane caches/counters (test/crash-simulation helper).

    Production never calls this: every cache is keyed by input identity, so a
    restart simply rebuilds from the same durable files.
    """
    _LANE_STATS.update({
        "selections": 0,
        "lane_picks": 0,
        "enabled_events": 0,
        "saturated_events": 0,
        "fallback_events": 0,
    })
    _LANE_TRANSITION_STATE.clear()
    _DIRECTION_COUNT_CACHE["key"] = None
    _DIRECTION_COUNT_CACHE["counts"] = {}


def lane_stats_snapshot():
    """Copy of the lane counters for the daemon stats merge."""
    return dict(_LANE_STATS)


def _emit_lane_event(event_type, severity, message, data):
    """Structured-event sink (tests monkeypatch this indirection)."""
    try:
        from system_log import log_system_event

        log_system_event(event_type, severity, message, data)
    except Exception:
        pass


@dataclass(frozen=True)
class LaneSelection:
    """One scheduling pass's lane outcome.

    ``pairs`` are ORDERED ``(seat0_bot, seat1_bot)`` tuples — the reverse of a
    pool-order pair is a distinct, legitimate lane pick (the legacy breadth
    selection only ever emitted pool-order tuples).  ``focus_pair_keys`` holds
    the normalized ``pair_key`` of every unsaturated focus pair so the caller
    can order non-focus breadth candidates ahead of focus pairs.
    """

    pairs: tuple
    focus_pair_keys: frozenset
    focus_bots: tuple
    phase: str


# ─── directional sample counts (existing durable authority) ─────────────────


def _direction_counts(match_history_file: Path) -> dict:
    """Admitted-sample count per ordered seat direction.

    One match_history row is one admitted complete 70-hand strength sample
    (the same unit the H2H ``games`` tier counts).  The scan is bounded by the
    file's append-only size and memoized on ``(mtime_ns, size)`` so repeated
    scheduling passes within one file state cost nothing.
    """
    path = Path(match_history_file)
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return {}
    if _DIRECTION_COUNT_CACHE.get("key") == key:
        return _DIRECTION_COUNT_CACHE["counts"]
    counts: dict = {}
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                a = row.get("bot0")
                b = row.get("bot1")
                if not isinstance(a, str) or not isinstance(b, str):
                    continue
                if not a or not b or a == b:
                    continue
                counts[(a, b)] = counts.get((a, b), 0) + 1
    except OSError:
        return {}
    _DIRECTION_COUNT_CACHE["key"] = key
    _DIRECTION_COUNT_CACHE["counts"] = counts
    return counts


# ─── focus resolution (pipeline checkpoint -> pool bots) ────────────────────


def _read_pipeline_state(results_dir: Path):
    state = read_locked_json(Path(results_dir) / "pipeline_state.json", default=None)
    return state if isinstance(state, dict) else None


def _checkpoint_field(state, name):
    """Read a checkpoint field: ``active_generation.<name>`` projection first,
    top-level key as the on-disk shape."""
    generation = state.get("active_generation")
    if isinstance(generation, dict):
        value = generation.get(name)
        if value is not None:
            return value
    return state.get(name)


def _valid_version(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 1 else None


def _newest_pool_bot(pool):
    newest_name = None
    newest_version = None
    prefix = str(ACTIVE_BOT_PREFIX)
    for name in pool:
        if not isinstance(name, str) or not name.startswith(prefix):
            continue
        suffix = name[len(prefix):]
        # The namespace prefix already carries the trailing ``v`` (e.g.
        # ``national_cloud_v11``); tolerate a doubled ``vv`` defensively.
        if suffix.startswith("v"):
            suffix = suffix[1:]
        if not suffix.isdigit():
            continue
        version = int(suffix)
        if newest_version is None or version > newest_version:
            newest_version = version
            newest_name = name
    return newest_name


def _resolve_focus(active_bots, results_dir):
    """(focus_names_tuple, fallback_flag, fallback_reason)."""
    pool = list(active_bots)
    if len(pool) < 2:
        return (), False, None
    pool_set = set(pool)
    state = _read_pipeline_state(results_dir)
    focus: list = []
    fallback = False
    reason = None
    if state is None:
        fallback = True
        reason = "pipeline_state.json unreadable"
    else:
        source_v = _valid_version(_checkpoint_field(state, "source_v"))
        if source_v is None or bot_name(source_v) not in pool_set:
            fallback = True
            reason = "source_v invalid or not in rating pool"
        else:
            focus.append(bot_name(source_v))
            parent2_v = _valid_version(_checkpoint_field(state, "parent2_v"))
            if (
                parent2_v is not None
                and parent2_v != source_v
                and bot_name(parent2_v) in pool_set
            ):
                focus.append(bot_name(parent2_v))
    if fallback:
        newest = _newest_pool_bot(pool)
        focus = [newest] if newest is not None else []
    return tuple(focus), fallback, reason


def _note_transition(phase, focus, *, fallback, reason, unsaturated_pairs, picks):
    """Emit lane events on phase transition only (never per selection).

    ``disabled`` (pool too small / no resolvable focus) emits nothing: there
    is no lane to announce, and an empty-focus ``enabled`` event would be a
    lie (observed in the operator event ledger during bring-up).
    """
    key = (phase, tuple(sorted(focus)))
    if _LANE_TRANSITION_STATE.get("key") == key:
        return
    _LANE_TRANSITION_STATE["key"] = key
    if phase == "saturated":
        _LANE_STATS["saturated_events"] += 1
        _emit_lane_event(
            f"{DEEP_SAMPLING_EVENT_PREFIX}_saturated",
            "info",
            "deep-sampling lane saturated: every focus-pair direction has "
            f">= {DEEP_SAMPLING_DIRECTION_TARGET} samples; breadth objective resumed",
            {"focus_bots": sorted(focus)},
        )
        return
    if phase != "active":
        return
    if fallback:
        _LANE_STATS["fallback_events"] += 1
        _emit_lane_event(
            f"{DEEP_SAMPLING_EVENT_PREFIX}_source_fallback",
            "info",
            "deep-sampling lane source unreadable/invalid; "
            f"focus fell back to newest published bot ({', '.join(sorted(focus)) or 'none'}): {reason}",
            {"focus_bots": sorted(focus), "reason": reason},
        )
    _LANE_STATS["enabled_events"] += 1
    _emit_lane_event(
        f"{DEEP_SAMPLING_EVENT_PREFIX}_enabled",
        "info",
        "deep-sampling lane enabled for focus bots "
        f"({', '.join(sorted(focus))}): {unsaturated_pairs} unsaturated pair(s), "
        f"{picks} lane pick(s) this pass",
        {
            "focus_bots": sorted(focus),
            "unsaturated_pairs": unsaturated_pairs,
            "picks": picks,
            "direction_target": DEEP_SAMPLING_DIRECTION_TARGET,
            "source_fallback": bool(fallback),
        },
    )


# ─── the lane ────────────────────────────────────────────────────────────────


def lane_selection(
    *,
    active_bots,
    n_picks,
    base_max,
    coverage,
    priority_fn,
    priority_bot,
):
    """Compute this pass's deep-sampling lane picks.

    Mirrors ``pick_matches`` cap discipline: focus bots (and the priority-eval
    bot) are exempt from per-bot caps inside the lane; non-focus partners use
    the same ``base_max``/coverage-loosened caps as the breadth loop, so one
    opponent cannot be hammered every round.
    """
    _LANE_STATS["selections"] += 1
    pool = list(active_bots)
    if len(pool) < 2 or n_picks <= 0:
        return LaneSelection((), frozenset(), (), "disabled")

    # Late import: the companion reads daemon globals through the parent
    # namespace so tests (and any future path override) stay authoritative.
    import elo_daemon as _ed

    counts = _direction_counts(_ed.MATCH_HISTORY_FILE)
    focus, fallback, reason = _resolve_focus(pool, _ed.RESULTS_DIR)
    if not focus:
        _note_transition(
            "disabled", (), fallback=fallback, reason=reason,
            unsaturated_pairs=0, picks=0,
        )
        return LaneSelection((), frozenset(), (), "disabled")

    focus_set = set(focus)
    target = DEEP_SAMPLING_DIRECTION_TARGET
    ordered = sorted(pool)
    scored = []
    focus_pair_keys = set()
    for index, a in enumerate(ordered):
        for b in ordered[index + 1:]:
            if a not in focus_set and b not in focus_set:
                continue
            forward = counts.get((a, b), 0)
            reverse = counts.get((b, a), 0)
            if forward >= target and reverse >= target:
                continue  # both wire directions at target: pair is saturated
            focus_pair_keys.add(pair_key(a, b))
            if forward < target:
                scored.append((-(target - forward), -priority_fn(a, b), a, b))
            if reverse < target:
                scored.append((-(target - reverse), -priority_fn(a, b), b, a))
    scored.sort()

    # Breadth floor: reserve at least max(1, n_picks // 4) picks for pairs
    # with no unsaturated focus membership whenever such pairs can exist.
    non_focus = [b for b in pool if b not in focus_set]
    breadth_floor = max(1, int(n_picks) // 4) if len(non_focus) >= 2 else 0
    max_lane = max(0, int(n_picks) - breadth_floor)

    cap_base = max(2, int(base_max))

    def _cap(bot):
        if bot in focus_set or bot == priority_bot:
            return int(n_picks)
        return cap_base * 3 if coverage.get(bot, 0.0) < 0.8 else cap_base

    selected = []
    bot_counts = Counter()
    for _deficit, _score, a, b in scored:
        if len(selected) >= max_lane:
            break
        if bot_counts[a] < _cap(a) and bot_counts[b] < _cap(b):
            selected.append((a, b))
            bot_counts[a] += 1
            bot_counts[b] += 1

    phase = "saturated" if not focus_pair_keys else "active"
    _note_transition(
        phase,
        focus,
        fallback=fallback,
        reason=reason,
        unsaturated_pairs=len(focus_pair_keys),
        picks=len(selected),
    )
    _LANE_STATS["lane_picks"] += len(selected)
    return LaneSelection(
        tuple(selected), frozenset(focus_pair_keys), tuple(focus), phase
    )
