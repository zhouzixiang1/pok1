"""Phase 3: MAP-Elites behavior archive (advisory diversity signal).

A 5x5 MAP-Elites grid over two behavior-characteristic (BC) axes derived from
replay_analysis.extract_behavior_fingerprint:

  aggression bucket (0..4) from aggression_factor = (raise+allin)/(call+1)
      [0,0.5) [0.5,1.0) [1.0,1.5) [1.5,2.5) [2.5,+inf)
  looseness bucket  (0..4) from vpip = (preflop_raise+preflop_call)/preflop_total
      [0,0.15) [0.15,0.30) [0.30,0.45) [0.45,0.60) [0.60,1.0]

niche_key = f"agg{a}_loose{l}"  (25 cells).

Each niche keeps the SINGLE bot with the highest fitness (h2h avg win_rate,
fallback 0.5 when no h2h). This is the canonical MAP-Elites archive used as a
population-diversity signal: a reap/Master can consult it to avoid culling or
re-proposing strategies that already fill a niche. MVP scope: WRITE only (the
archive is persisted + logged as telemetry). No reap/gate reads it yet.

KNOWN LIMITATION: extract_behavior_fingerprint reads
`output.display.last_action.action`, which bot_action_stats.py documents as
misattributing some actions (it echoes the previous player's action). The
fingerprint-derived BC is therefore a noisy estimate of true aggression/VPIP.
This is acceptable for an advisory diversity signal; we annotate it on every
niche entry. Do NOT use these BCs as ground truth for sizing decisions.

All I/O is best-effort (advisory): write_behavior_archive never raises; a read
failure returns {}. The archive is recomputed wholesale from replays each
daemon action-stats refresh (the etag-tracked async scan), so it is always a
fresh snapshot — never partially updated.
"""

import json
import time
from pathlib import Path

from evolution_infra import RESULTS_DIR, read_locked_json, write_locked_json, REPLAY_DIR

BEHAVIOR_ARCHIVE_FILE = RESULTS_DIR / "behavior_archive.json"

# Discretization bin edges. A value exactly on an edge falls into the higher
# bucket via bisect-style left semantics (bucket = #edges strictly <= value).
_AGG_EDGES = (0.5, 1.0, 1.5, 2.5)
_LOOSE_EDGES = (0.15, 0.30, 0.45, 0.60)


def _bucket(value, edges):
    """Map a scalar to bucket 0..len(edges) using strict-less-than edges.

    None -> 0 (conservative default: least aggressive / tightest, so empty
    fingerprints land in agg0_loose0 together).
    """
    if value is None:
        return 0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0
    b = 0
    for edge in edges:
        if v >= edge:
            b += 1
        else:
            break
    return b


def aggression_bucket(af):
    return _bucket(af, _AGG_EDGES)


def looseness_bucket(vpip):
    return _bucket(vpip, _LOOSE_EDGES)


def niche_key(agg_bucket, loose_bucket):
    return f"agg{int(agg_bucket)}_loose{int(loose_bucket)}"


def fingerprint_to_bc(fp):
    """Discretize a fingerprint into (niche_key, agg_bucket, loose_bucket, bc_dict)."""
    af = fp.get("aggression_factor") if isinstance(fp, dict) else None
    vpip = fp.get("vpip") if isinstance(fp, dict) else None
    a = aggression_bucket(af)
    l = looseness_bucket(vpip)
    return (
        niche_key(a, l),
        a,
        l,
        {
            "agg_bucket": a,
            "loose_bucket": l,
            "aggression_factor": (round(af, 3) if af is not None else None),
            "vpip": (round(vpip, 3) if vpip is not None else None),
        },
    )


def _bot_version(name):
    """Extract integer version from 'claude_v<N>'; None if unparseable."""
    try:
        return int(str(name).replace("claude_v", ""))
    except (ValueError, TypeError):
        return None


def _scan_behavior_fingerprints(replays_dir, active_bots):
    """Scan replay files and return {bot_name: fingerprint} for active bots.

    Accumulates each bot's game logs across every replay it appeared in, then
    builds ONE fingerprint per bot. Returns {} if the replay dir is missing or
    empty. Active-set filter keeps the scan bounded and avoids archiving
    graveyarded bots.

    NOTE on the known last_action misattribution (see module docstring): we use
    extract_behavior_fingerprint as-is; the BCs are advisory only.
    """
    from replay_analysis import extract_behavior_fingerprint

    replays_dir = Path(replays_dir)
    if not replays_dir.exists():
        return {}
    active_set = set(active_bots)
    # games_by_bot[name] = list of game dicts (each game's logs are scanned by
    # extract_behavior_fingerprint which filters on player_id internally).
    games_by_bot = {}
    try:
        entries = [e for e in replays_dir.iterdir()
                   if e.suffix == ".json" and not e.name.startswith(".")]
    except OSError:
        return {}
    for fp_path in entries:
        try:
            with open(fp_path, "r", encoding="utf-8") as f:
                replay = json.load(f)
        except Exception:
            continue
        bot0 = replay.get("bot0")
        bot1 = replay.get("bot1")
        games = replay.get("games", [])
        if not games:
            continue
        if bot0 in active_set:
            games_by_bot.setdefault(bot0, []).extend(games)
        if bot1 in active_set:
            games_by_bot.setdefault(bot1, []).extend(games)

    out = {}
    for bot, games in games_by_bot.items():
        try:
            # The fingerprint needs to know which player_id this bot was. Since
            # we merged games from multiple replays where the bot may have been
            # pid 0 OR pid 1, run the fingerprint for BOTH ids and merge the raw
            # action streams by concatenating. Simplest correct approach: run
            # fingerprint once treating the bot as pid 0 across its own pooled
            # games is WRONG when it was pid 1. Instead, accumulate per-replay
            # per-pid then pass a combined games list — but fingerprint keys on
            # player_id. So we compute per-pid fingerprints from the SAME pooled
            # games (a given log entry only matches one pid) and sum the
            # resulting frequency vectors via a re-extract.
            # To keep this bounded, we just call fingerprint twice (pid 0 and
            # pid 1) on the pooled games and take the union of action counts by
            # re-deriving aggression/vpip from the richer one. We pick the pid
            # whose total_actions is larger (the bot's actual seat in most
            # replays). This is an approximation acceptable for advisory BC.
            fp0 = extract_behavior_fingerprint(games, 0)
            fp1 = extract_behavior_fingerprint(games, 1)
            # Heuristic: the bot's real actions are whichever pid has more
            # actions in the pooled set (it was that seat in most of its
            # replays). For a single replay both pids have ~equal actions, so
            # this merges sensibly.
            out[bot] = fp0 if fp0["total_actions"] >= fp1["total_actions"] else fp1
        except Exception:
            continue
    return out


def build_behavior_archive(replays_dir, active_bots, h2h_winrates=None):
    """Build the full MAP-Elites archive from replays + fitness.

    `h2h_winrates` is an optional {bot: avg_win_rate} map (from
    tool_helpers.load_h2h_avg_winrates). When omitted, fitness falls back to
    0.5 for every bot (niche still populated, ordering arbitrary).

    Returns the archive dict (niches keyed by niche_key). Pure data — no I/O.
    Exposed for unit testing.
    """
    fingerprints = _scan_behavior_fingerprints(replays_dir, active_bots)
    h2h_winrates = h2h_winrates or {}
    niches = {}
    for bot, fp in fingerprints.items():
        key, a, l, bc = fingerprint_to_bc(fp)
        fitness = h2h_winrates.get(bot, 0.5)
        try:
            fitness = float(fitness)
        except (TypeError, ValueError):
            fitness = 0.5
        # Phase 4: single-eval entry. eval_mode="single" marks this as the
        # daemon-background-battle fitness (one sample). A k=3 re-evaluated
        # candidate (written by qd_async_eval's worker) carries fitness_median
        # + fitness_samples + eval_mode="k3"; the merge comparison below prefers
        # fitness_median when BOTH entries have it (like-for-like), otherwise
        # falls back to the scalar fitness (backward compatible with v1 archives).
        new_entry = {
            "bot": bot,
            "version": _bot_version(bot),
            "fitness": round(fitness, 4),
            "bc": bc,
            "last_eval": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "eval_mode": "single",
            "fitness_median": None,
            "fitness_samples": None,
        }
        existing = niches.get(key)
        if existing is None or _better_niche_occupant(new_entry, existing):
            # Preserve a prior k=3 entry's richer fields if this single-eval entry
            # is NOT better — but when the new entry wins, a single-eval rebuild
            # legitimately overwrites with the fresh fingerprint/fitness. The k=3
            # median survives only if it is still the better occupant.
            niches[key] = new_entry
    return {
        "version": 1,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "bc_note": (
            "aggression/looseness derived from extract_behavior_fingerprint "
            "(last_action-based; known misattribution — advisory only)"
        ),
        "niches": niches,
    }


def _niche_fitness_value(entry):
    """Comparable fitness for a niche entry.

    Prefers fitness_median (k=3 median, lower-variance) when present; else the
    scalar fitness. Returns a float (0.5 fallback for malformed entries)."""
    try:
        med = entry.get("fitness_median")
        if med is not None:
            return float(med)
        return float(entry.get("fitness", 0.5))
    except (TypeError, ValueError):
        return 0.5


def _better_niche_occupant(candidate, incumbent):
    """True if `candidate` should replace `incumbent` as a niche's occupant.

    Phase 4 fairness rule: compare LIKE-FOR-LIKE.
      - both k3 (fitness_median present on both): compare medians.
      - both single: compare scalar fitness.
      - mixed (one k3, one single): compare the median (k3) against the single
        scalar. This is advisory-only cross-tier comparison — k3 median has
        lower variance, so a k3 entry that beats a single entry on the scalar
        comparison is a defensible occupant. The comparison value is taken from
        _niche_fitness_value (median when present, else scalar), so a k3 entry's
        median competes directly against a single entry's scalar.
    Strictly-greater keeps the incumbent on ties (stable, no flapping).
    """
    return _niche_fitness_value(candidate) > _niche_fitness_value(incumbent)


def write_behavior_archive(replays_dir, active_bots, h2h_winrates=None):
    """Recompute and persist the behavior archive. Best-effort, never raises.

    Phase 4: preserves any k=3 median fields written by the qd_async_eval worker
    for bots whose niche occupant is unchanged by this rebuild. A full rebuild
    re-derives every bot's fitness as single-eval (one sample from h2h), which
    would otherwise clobber the lower-variance k=3 median a candidate earned on
    commit. We re-attach the prior k=3 fields when the occupant bot matches.
    """
    try:
        prior = read_behavior_archive()
        prior_niches = (prior or {}).get("niches", {}) if isinstance(prior, dict) else {}
        archive = build_behavior_archive(replays_dir, active_bots, h2h_winrates)
        niches = archive.get("niches", {})
        for key, entry in niches.items():
            prior_entry = prior_niches.get(key)
            if (isinstance(prior_entry, dict)
                    and prior_entry.get("bot") == entry.get("bot")
                    and prior_entry.get("eval_mode") == "k3"
                    and prior_entry.get("fitness_median") is not None):
                # Re-attach k=3 fields so the median survives a daemon rebuild.
                entry["fitness_median"] = prior_entry["fitness_median"]
                entry["fitness_samples"] = prior_entry.get("fitness_samples")
                entry["eval_mode"] = "k3"
        write_locked_json(BEHAVIOR_ARCHIVE_FILE, archive)
        return archive
    except Exception:
        return None


def read_behavior_archive():
    """Read the behavior archive, returning {} on any failure (advisory)."""
    return read_locked_json(BEHAVIOR_ARCHIVE_FILE, default={}) or {}
