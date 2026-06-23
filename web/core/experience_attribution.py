"""Experience-pool attribution — Ratchet retire for lessons (fix-12).

fix-12 wires the experience pool into the SAME outcome-driven retirement model
that research_governance applies to web-retrieved candidates (Ratchet,
arxiv 2605.19576). The goal: lessons that are repeatedly tried across
generations but never produce a rating lift should be retired so the Master
stops re-injecting them.

## Why a separate sidecar JSON (not experience_pool.md)?

The experience pool markdown is a free-form LLM-authored document: the
consolidator rewrites it every 3 gens. We cannot reliably parse attribution
counters out of prose. Instead we keep a structured sidecar
(results/experience_attribution.json) keyed by lesson_id, which the
consolidator reads to inform retire decisions.

## Why rating_delta (not precommit_passed)?

The original plan hooked record_lesson_outcome into the commit-time
archivist with won=precommit_passed. That is structurally INERT: commit_bot's
gate ledger REQUIRES precommit.passed==True before commit is allowed, so by
the time the hook runs, won is ALWAYS True -> attributed_hurt never
accumulates -> nothing ever retires. (verify caught this fatal flaw.)

The fix mirrors fix-2's reconcile_critic_calibration: the daemon's save_cycle
backfills a CONTINUOUS outcome (rating_delta = r_bot - r_source) once the
bot converges. rating_delta < 0 = hurt, > 0 = help. This is the same
post-convergence reconciliation pattern, applied to lessons.

## Reuse over rebuild

This module imports research_governance's score_candidate (the ĉ formula)
and RETIRE_N_MIN / RETIRE_TAU constants directly, rather than reimplementing
them. N_min=30 (NOT the ablated 20, which showed -0.019 active harm).

All writes are fcntl-locked via evolution_infra helpers. Best-effort: a
governance failure must never block the evolution pipeline or daemon.
"""

import time

import evolution_infra
from evolution_infra import read_locked_json, write_locked_json


def _results_dir():
    """Resolve RESULTS_DIR via the module attribute at call time so test
    monkeypatch of evolution_infra.RESULTS_DIR takes effect. (A module-level
    `from evolution_infra import RESULTS_DIR` would bind the original value
    and break test isolation — conftest patches the module attribute.)"""
    return evolution_infra.RESULTS_DIR


# Sidecar: {lesson_id: {source_gen, attributed_hurt, attributed_help, trials,
#                       status, retired_reason, last_rating_delta, last_reconciled_at}}
def _attribution_file():
    """Resolve the sidecar path at call time so test monkeypatch of
    evolution_infra.RESULTS_DIR takes effect (the archivist imports it lazily
    inside functions for the same reason)."""
    return _results_dir() / "experience_attribution.json"


def _load_attribution():
    """Load the lesson attribution sidecar. Missing/corrupt -> empty dict."""
    data = read_locked_json(_attribution_file(), default=None)
    if isinstance(data, dict):
        return data
    return {}


def _save_attribution(attrib):
    """Persist the attribution sidecar (best-effort, fcntl-locked)."""
    try:
        rd = _results_dir()
        rd.mkdir(parents=True, exist_ok=True)
        write_locked_json(_attribution_file(), attrib)
    except Exception:
        pass  # never block the pipeline


def _lesson_id(lesson):
    """Derive a stable id for a lesson dict. Prefer an explicit id, else
    synthesize from source_gen + a text fingerprint."""
    if not isinstance(lesson, dict):
        return None
    lid = lesson.get("lesson_id") or lesson.get("id")
    if lid:
        return str(lid)
    gen = lesson.get("source_gen")
    text = str(lesson.get("text") or lesson.get("lesson") or "")[:120]
    if gen is None and not text:
        return None
    return f"gen{gen}:{text[:60]}"


def score_lesson(lesson):
    """Reuse research_governance's score_candidate ĉ formula on a lesson's
    attribution counters.

    ĉ = (attributed_help - attributed_hurt) / max(trials, 1). Higher is better.
    """
    from research_governance import score_candidate
    return score_candidate(lesson)


def record_lesson_outcome(lesson_id, hurt_verdict=None, rating_delta=None,
                          source_gen=None, n=1):
    """Accumulate an outcome observation into a lesson's attribution counters.

    The outcome signal is rating_delta (continuous), NOT a binary precommit
    flag (which is structurally always-True at commit time and would make the
    retire mechanism INERT — see module docstring).

    Args:
        lesson_id: stable lesson identifier (from _lesson_id).
        hurt_verdict: optional 'helped'|'hurt'|'neutral'. When rating_delta is
            absent this is the fallback signal.
        rating_delta: r_bot - r_source. < 0 => hurt, > 0 => help. Continuous
            signal preferred over the binary verdict.
        source_gen: the generation this lesson was born from / applied to.
        n: number of trials this observation represents (default 1).
    """
    if not lesson_id:
        return
    attrib = _load_attribution()
    entry = attrib.get(lesson_id) or {
        "attributed_hurt": 0,
        "attributed_help": 0,
        "trials": 0,
        "status": "active",
    }
    if source_gen is not None and "source_gen" not in entry:
        entry["source_gen"] = source_gen

    entry["trials"] = int(entry.get("trials", 0)) + max(int(n or 0), 1)

    # Continuous rating_delta is the authoritative signal (mirrors fix-2).
    if rating_delta is not None:
        entry["last_rating_delta"] = round(float(rating_delta), 2)
        if float(rating_delta) < 0:
            entry["attributed_hurt"] = int(entry.get("attributed_hurt", 0)) + 1
        elif float(rating_delta) > 0:
            entry["attributed_help"] = int(entry.get("attributed_help", 0)) + 1
    elif hurt_verdict == "helped":
        entry["attributed_help"] = int(entry.get("attributed_help", 0)) + 1
    elif hurt_verdict == "hurt":
        entry["attributed_hurt"] = int(entry.get("attributed_hurt", 0)) + 1

    attrib[lesson_id] = entry
    _save_attribution(attrib)


def reconcile_lesson_outcomes(ratings, bot_stats, rd_threshold=60, min_games=100):
    """Backfill rating_delta outcomes into lesson attribution counters.

    Called from the daemon's save_cycle (parallel to fix-2's
    reconcile_critic_calibration). For each lesson row that has a source_gen
    whose bot has converged (rd < rd_threshold AND games >= min_games) and
    has not yet been reconciled for that gen, compute the real delta =
    r_bot - r_source and feed it to record_lesson_outcome.

    The hurt signal therefore comes from rating_delta < 0, NOT from
    precommit_passed (which is always True at commit time).

    Args:
        ratings: dict of bot_name -> Glicko2Player (current daemon ratings).
        bot_stats: dict of bot_name -> stats dict (must have 'games' key).
        rd_threshold: max rd to consider a bot converged (default 60).
        min_games: min games to consider a bot converged (default 100).
    """
    attrib = _load_attribution()
    if not attrib:
        return False

    changed = False
    for lesson_id, entry in list(attrib.items()):
        if entry.get("status") != "active":
            continue
        gen = entry.get("source_gen")
        if gen is None:
            continue
        # Skip gens already reconciled (idempotent — never recompute).
        if entry.get("last_reconciled_gen") == gen:
            continue

        bot_name = f"claude_v{gen}"
        bot_player = ratings.get(bot_name)
        bot_games = bot_stats.get(bot_name, {}).get("games", 0)
        if bot_player is None or bot_games < min_games:
            continue
        if bot_player.rd >= rd_threshold:
            continue

        # Converged — compute real rating delta vs a reference.
        # Prefer the gen's own source_v if recorded; else vs baseline 1500.
        source_v = entry.get("source_v")
        source_name = f"claude_v{source_v}" if source_v is not None else None
        source_player = ratings.get(source_name) if source_name else None
        if source_player is not None:
            delta = bot_player.r - source_player.r
        else:
            delta = bot_player.r - 1500.0

        # Feed the continuous signal in (record_lesson_outcome reloads+writes
        # atomically, keeping counters consistent).
        record_lesson_outcome(lesson_id, rating_delta=delta, source_gen=gen)

        # Mark this gen reconciled so we never recompute it.
        fresh = _load_attribution()
        fresh_lesson = fresh.get(lesson_id)
        if fresh_lesson is not None:
            fresh_lesson["last_reconciled_gen"] = gen
            fresh_lesson["last_reconciled_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            _save_attribution(fresh)
        changed = True

    return changed


def retire_lessons(min_trials=None, tau=None):
    """Retire active lessons with ĉ <= tau after >= min_trials observations.

    Mirrors research_governance's retire threshold (RETIRE_N_MIN=30,
    RETIRE_TAU=-0.10). The ablated N_min=20 showed -0.019 active harm, so the
    floor is 30 — do NOT lower it.

    Returns the list of newly-retired lesson_ids (for the consolidator to
    surface as stale / drop from the active pool).
    """
    from research_governance import RETIRE_N_MIN, RETIRE_TAU
    if min_trials is None:
        min_trials = RETIRE_N_MIN
    if tau is None:
        tau = RETIRE_TAU
    # Use the stricter of the passed min_trials and the governance floor.
    min_trials = max(int(min_trials), RETIRE_N_MIN)

    attrib = _load_attribution()
    retired = []
    for lesson_id, entry in attrib.items():
        if entry.get("status") != "active":
            continue
        trials = int(entry.get("trials", 0))
        if trials < min_trials:
            continue
        if score_lesson(entry) <= tau:
            entry["status"] = "retired"
            entry["retired_reason"] = (
                f"low_score_after_{trials}_trials_ĉ={score_lesson(entry):.3f}"
            )
            retired.append(lesson_id)
    if retired:
        _save_attribution(attrib)
    return retired


def active_lesson_ids():
    """Return the set of lesson_ids that are still active (not retired)."""
    attrib = _load_attribution()
    return {lid for lid, e in attrib.items() if e.get("status") == "active"}


def retired_lesson_ids():
    """Return the set of retired lesson_ids (for consolidator to drop)."""
    attrib = _load_attribution()
    return {lid for lid, e in attrib.items() if e.get("status") == "retired"}
