"""Phase 3: FAMOU nemesis archive — persistent nemesis / champion relationships.

The nemesis archive is a rolling snapshot of "who beats whom" derived from the
on-disk head_to_head.json. It supports the FAMOU-style co-evolution pressure we
apply via the precommit nemesis probe (tool_helpers._select_precommit_opponents):

  nemesis_of[X]   = the active opponent with the lowest h2h win_rate vs X
                    (X's toughest matchup; the weakness we probe in the
                    candidate that inherits X = the parent).
  champions[Y]    = reverse index: the bots for whom Y is the nemesis, plus a
                    defeat count. Lets a future reap/Master rank bots by how
                    many populations they dominate (weakness pressure).

WRITES are best-effort (advisory): every entry point is wrapped in try/except
and never blocks the commit pipeline. The archive is a *convenience snapshot*
— the canonical source is always head_to_head.json, recomputed live in
_select_precommit_opponents. The archive only serves as a fallback when the
live scan finds no qualifying nemesis (e.g. the parent has not played enough
games against any single active opponent yet but a prior commit recorded one).

Schema (results/nemesis_archive.json):

    {
      "version": 1,
      "updated_at": "<iso>",
      "nemesis_of": {
        "<bot>": {"nemesis": "<opp>", "win_rate": <float>, "games": <int>, "since": "<iso>"}
      },
      "champions": {
        "<bot>": {"defeats": ["<bot>", ...], "as_nemesis_count": <int>}
      }
    }

Only bots with a qualifying nemesis (win_rate < threshold, games >= min_games)
get a nemesis_of entry. Bots that are nobody's nemesis get no champions entry.
"""

import time

from evolution_infra import RESULTS_DIR, read_locked_json, write_locked_json, H2H_FILE

NEMESIS_ARCHIVE_FILE = RESULTS_DIR / "nemesis_archive.json"

# A nemesis must represent a real weakness (win_rate below this) backed by
# enough games to be above the H2H noise floor. Mirrors the probe thresholds in
# tool_helpers so the archive and the live scan agree on what counts as a nemesis.
NEMESIS_WINRATE_THRESHOLD = 0.40
NEMESIS_MIN_GAMES = 4


def _h2h_winrate(bot_name, opponent, h2h):
    """Return (win_rate, games) for bot_name vs opponent from the h2h dict, else None."""
    for key, value in h2h.items():
        parts = key.split(" vs ")
        if len(parts) != 2 or bot_name not in parts or opponent not in parts:
            continue
        a, b = parts
        games = value.get("games", 0)
        if games <= 0:
            return None
        bot_wins = value.get("a_wins", 0) if bot_name == a else value.get("b_wins", 0)
        return (bot_wins / games, games)
    return None


def compute_nemesis_relationships(active_bots, h2h,
                                  winrate_threshold=NEMESIS_WINRATE_THRESHOLD,
                                  min_games=NEMESIS_MIN_GAMES):
    """Compute nemesis_of + champions from h2h for the given active bots.

    Returns (nemesis_of, champions) dicts ready for serialization. Pure data —
    no I/O. Exposed for unit testing.
    """
    nemesis_of = {}
    for bot in active_bots:
        best = None  # (win_rate, games, opp)
        for opp in active_bots:
            if opp == bot:
                continue
            rec = _h2h_winrate(bot, opp, h2h)
            if rec is None:
                continue
            wr, games = rec
            if games < min_games:
                continue
            if best is None or wr < best[0]:
                best = (wr, games, opp)
        if best is not None and best[0] < winrate_threshold:
            nemesis_of[bot] = {
                "nemesis": best[2],
                "win_rate": round(best[0], 4),
                "games": int(best[1]),
            }

    # Reverse index: champions[Y] = bots for whom Y is the nemesis.
    champions = {}
    for bot, rec in nemesis_of.items():
        nemesis = rec["nemesis"]
        entry = champions.setdefault(
            nemesis, {"defeats": [], "as_nemesis_count": 0}
        )
        entry["defeats"].append(bot)
        entry["as_nemesis_count"] += 1

    return nemesis_of, champions


def write_nemesis_archive(active_bots, h2h=None):
    """Recompute and persist the nemesis archive.

    Best-effort: any failure is swallowed. Caller (commit_bot) wraps this in
    its own try/except too, but this never raises so it is safe to call
    directly. Reads head_to_head.json under a shared lock unless `h2h` is
    supplied (caller already holds a snapshot).
    """
    try:
        if h2h is None:
            h2h = read_locked_json(H2H_FILE, default={})
        nemesis_of, champions = compute_nemesis_relationships(active_bots, h2h)
        archive = {
            "version": 1,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "nemesis_of": nemesis_of,
            "champions": champions,
        }
        write_locked_json(NEMESIS_ARCHIVE_FILE, archive)
        return archive
    except Exception:
        return None


def read_nemesis_archive():
    """Read the nemesis archive, returning {} on any failure (advisory)."""
    return read_locked_json(NEMESIS_ARCHIVE_FILE, default={}) or {}
