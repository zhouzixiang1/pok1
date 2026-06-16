"""Phase 4: QD k=3 fitness — median over k independent mirror-battle evaluations.

For CANDIDATE commit versions (the bot just committed by the pipeline), we
re-evaluate fitness k=3 times against each opponent with independent seeds and
take the MEDIAN per-sample win rate. Median (not mean) down-weights a single
lucky/unlucky mirror pair — a cheap variance-reduction tailored to the QD
archive's max-fitness retention (a niche's recorded fitness should reflect
typical performance, not the best-of-k outlier).

Cost discipline:
  - Candidate commit version -> eval_mode="k3" (this module).
  - Normal daemon background mirror battles -> eval_mode="single" (the existing
    map_elites.build_behavior_archive path). The archive merge compares like
    with like: k3 fitness vs k3 fitness, single vs single.

This module is PURE DATA + engine calls: it returns fitness_samples and a
median; it does NOT write the archive. The async worker in qd_async_eval.py
calls evaluate_commit_version_k and merges the result into behavior_archive.json.

mirror_battle returns (match_wins, draws, n_played, all_logs, net_chips_list).
Per-sample fitness = candidate (bot0) match wins / n_played (a win rate in
[0,1], same units as the daemon's h2h avg win rate that populates
map_elites.build_behavior_archive fitness).
"""

import random
import statistics
from typing import List, Optional, Sequence

QD_K = 3
QD_REEVAL_EVERY = 5          # generations between 10%-elite reeval sweeps
QD_ELITE_FRACTION = 0.10     # fraction of niches re-evaluated each sweep


def _import_mirror_battle():
    """Lazy import of the web core engine mirror_battle (engine contract: do
    NOT modify battle.py; this only calls it)."""
    import sys
    from pathlib import Path
    _core = Path(__file__).resolve().parent / "engine"
    if str(_core) not in sys.path:
        sys.path.insert(0, str(_core))
    from engine.battle import mirror_battle  # noqa: E402
    return mirror_battle


def _run_single_eval(bot_main: str, opponent_main: str, n_games: int, seed: int) -> Optional[float]:
    """One mirror_battle run. Returns candidate (bot0) win rate in [0,1], or
    None if the battle produced no completed mirror pairs (engine error /
    both bots crashed).

    NOTE: ``seed`` is accepted for API regularity, but the engine's judge
    reseeds with OS entropy at match init (engine/judge.py random.seed() with
    no argument), so the k runs are NOT reproducible per-seed. They remain
    INDEPENDENT samples (each mirror pair draws a fresh deck from OS entropy),
    which is all the median needs. (Known engine non-determinism, documented in
    the Phase 0 memory.)
    """
    mirror_battle = _import_mirror_battle()
    random.seed(seed)
    try:
        match_wins, draws, n_played, all_logs, net_chips_list = mirror_battle(
            bot_main, opponent_main, n_games=n_games, verbose=False, save_log=False
        )
    except Exception:
        return None
    if n_played <= 0:
        return None
    # match_wins[0] = candidate (bot0) mirror-pair wins.
    return float(match_wins[0]) / float(n_played)


def evaluate_commit_version_k(
    bot_main: str,
    opponents: Sequence[str],
    k: int = QD_K,
    n_games: int = 8,
    seed_base: int = 0,
    cancel_check=None,
) -> dict:
    """Evaluate a candidate bot against each opponent k times, return per-opponent
    fitness_samples + an overall median fitness.

    Args:
        bot_main: absolute path to the candidate bot's main.py.
        opponents: sequence of absolute opponent main.py paths.
        k: number of independent mirror-battle evaluations per opponent.
        n_games: mirror pairs per evaluation (small; matches precommit default).
        seed_base: offset for per-sample seeds (so the same opponent across k
            runs uses distinct seeds: seed_base + run_idx).
        cancel_check: optional zero-arg callable returning True to abort the
            eval early (checked at every opponent AND run boundary, so a
            shutdown / watchdog timer can break the loop promptly without
            waiting for the full k x opponents batch). Partial results are
            returned (median over whatever completed).

    Returns:
        {
          "fitness_samples": [per-opponent-averaged win rate, len k],  # one
                                                                        # sample = mean win rate across opponents for that run
          "fitness_median": float,            # median(fitness_samples)
          "per_opponent": {opponent_path: [k win rates]},
          "k": k, "n_games": n_games,
          "eval_mode": "k3",
          "completed": int,                  # # of runs that returned a value
        }

    Per-sample fitness is the MEAN win rate across opponents for that run (so
    fitness_samples has exactly k entries regardless of opponent count). This
    keeps the median well-defined and comparable to the single-eval h2h avg
    win rate used elsewhere.
    """
    k = max(1, int(k))
    opponents = list(opponents)
    per_run_rates: List[List[float]] = [[] for _ in range(k)]  # run -> list of opp win rates
    per_opponent: dict = {}
    for opp in opponents:
        if cancel_check is not None and cancel_check():
            break
        opp_rates = []
        for run_idx in range(k):
            if cancel_check is not None and cancel_check():
                break
            seed = seed_base + run_idx
            r = _run_single_eval(bot_main, opp, n_games=n_games, seed=seed)
            if r is not None:
                opp_rates.append(r)
                per_run_rates[run_idx].append(r)
        per_opponent[opp] = opp_rates

    # Per-sample fitness: mean win rate across opponents that completed for that run.
    fitness_samples: List[float] = []
    for run_idx in range(k):
        rates = per_run_rates[run_idx]
        if rates:
            fitness_samples.append(sum(rates) / len(rates))

    fitness_median = statistics.median(fitness_samples) if fitness_samples else 0.5
    completed = sum(1 for run_idx in range(k) if per_run_rates[run_idx])
    return {
        "fitness_samples": [round(x, 4) for x in fitness_samples],
        "fitness_median": round(float(fitness_median), 4),
        "per_opponent": per_opponent,
        "k": k,
        "n_games": n_games,
        "eval_mode": "k3",
        "completed": completed,
    }


def reevaluate_top_elites(archive: dict, fraction: float = QD_ELITE_FRACTION) -> List[str]:
    """Select the top-fraction elites from a behavior archive and return their
    bot names for re-evaluation.

    "Elite" = the niche occupants with the highest fitness. We take
    ceil(niches * fraction) elites by fitness. The caller marks these for
    priority re-evaluation by the daemon (single-eval) — the next
    build_behavior_archive sweep then refreshes their fitness from fresh h2h
    data, preventing stale-elite lock-in.

    Args:
        archive: behavior_archive.json dict ({niches: {key: {bot, fitness, ...}}}).
        fraction: fraction of niches to mark (default 0.10).

    Returns:
        List of bot names (deduplicated) to re-evaluate. Empty if archive is
        empty/invalid. Pure data — does NOT mutate the archive.
    """
    if not archive or not isinstance(archive, dict):
        return []
    niches = archive.get("niches")
    if not niches or not isinstance(niches, dict):
        return []
    import math
    entries = list(niches.values())
    # Sort by fitness desc (fallback 0.5 for malformed entries).
    def _fit(e):
        try:
            return float(e.get("fitness", 0.5))
        except (TypeError, ValueError):
            return 0.5
    entries.sort(key=_fit, reverse=True)
    n = len(entries)
    take = max(1, math.ceil(n * max(0.0, min(1.0, fraction)))) if n else 0
    elites = []
    seen = set()
    for e in entries[:take]:
        bot = e.get("bot")
        if bot and bot not in seen:
            seen.add(bot)
            elites.append(bot)
    return elites
