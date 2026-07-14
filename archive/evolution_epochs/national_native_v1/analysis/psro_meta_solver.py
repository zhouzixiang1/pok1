"""Phase 4: PSRO meta-solver (fictitious play / uniform over H2H payoff).

Pure-Python, no numpy dependency. Reads head_to_head.json (the same structure
consumed elsewhere in the project) and computes a meta-distribution (mixed
strategy) over a population of bots. Two solvers:

  - uniform_meta: uniform over population (baseline / fallback).
  - fictitious_play: classic fictitious-play fictitious-play for 2-player
    zero-sum games. Each step every side best-responds to the opponent's
    empirical action distribution; the row player's time-averaged play
    converges to a Nash equilibrium for zero-sum games.

PAYOFF SEMANTICS
----------------
head_to_head.json keys are "<a> vs <b>" with win_rate = A-perspective win rate
(measured). The matrix returned by build_payoff_matrix is SYMMETRIZED and
zero-sum: payoff[i][j] is the probability that population[i] (as bot0/A) beats
population[j] (as bot1/B) under the symmetrized estimate:

    forward  = h2h["i vs j"].win_rate            if present
    payoff[i][j] = forward
    payoff[j][i] = 1 - forward                   (zero-sum mirror)

When only the reverse key "j vs i" exists, payoff[i][j] = 1 - that win_rate
(the win_rate stored is A=i-of-reverse=j's perspective). When neither key
exists, payoff = 0.5 (symmetric, no information).

The fictitious-play loop treats payoff[i][j] as the ROW player's expected
payoff when row plays i and column plays j. Row's best response maximizes the
dot product against column's empirical distribution; column's best response
minimizes the same (column pays what row wins, zero-sum).

This module is OFFLINE-ONLY and has no I/O side effects. All functions take
already-loaded dicts / matrices. Used by the PSRO MVP to write mixture_config.json
for bots/mixture_main.
"""

import random
from typing import Dict, List, Sequence


# ---------------------------------------------------------------------------
# Payoff matrix construction
# ---------------------------------------------------------------------------

def build_payoff_matrix(h2h: dict, population: Sequence[str]) -> List[List[float]]:
    """Build a symmetric zero-sum payoff matrix from head_to_head data.

    payoff[i][j] = estimated win rate of population[i] vs population[j]
    (i as bot0/A, j as bot1/B), symmetrized so payoff[i][j] + payoff[j][i] == 1.
    Missing information on both directions -> 0.5.

    Args:
        h2h: head_to_head.json dict. Keys "<a> vs <b>", values have 'win_rate'
            (A-perspective). May be sparse / asymmetric.
        population: ordered list of bot names.

    Returns:
        n x n list-of-lists of floats in [0, 1].
    """
    n = len(population)
    matrix: List[List[float]] = [[0.5] * n for _ in range(n)]
    for i in range(n):
        matrix[i][i] = 0.5  # self-play is a wash
        for j in range(i + 1, n):
            a, b = population[i], population[j]
            wr = _bilateral_win_rate(h2h, a, b)
            matrix[i][j] = wr
            matrix[j][i] = 1.0 - wr
    return matrix


def _bilateral_win_rate(h2h: dict, a: str, b: str) -> float:
    """Return A-perspective win rate for (a vs b), defensively.

    Prefers the forward key "a vs b". If absent, derives from the reverse
    "b vs a" as 1 - reverse.win_rate (reverse's win_rate is B-perspective of
    the reverse pair = b-perspective of the a,b pair). If neither key exists,
    returns 0.5.
    """
    fwd = h2h.get(f"{a} vs {b}")
    if fwd is not None:
        wr = fwd.get("win_rate")
        try:
            return float(wr) if wr is not None else 0.5
        except (TypeError, ValueError):
            return 0.5
    rev = h2h.get(f"{b} vs {a}")
    if rev is not None:
        wr = rev.get("win_rate")
        try:
            return 1.0 - float(wr) if wr is not None else 0.5
        except (TypeError, ValueError):
            return 0.5
    return 0.5


# ---------------------------------------------------------------------------
# Meta-distributions
# ---------------------------------------------------------------------------

def uniform_meta(population: Sequence[str]) -> Dict[str, float]:
    """Uniform distribution over the population."""
    n = len(population)
    if n == 0:
        return {}
    p = 1.0 / n
    return {bot: p for bot in population}


def _expected_values_by_row(payoff: List[List[float]], opp_dist: Sequence[float]) -> List[float]:
    """For each ROW i, expected payoff E[i] = sum_j payoff[i][j] * opp_dist[j].

    This is ROW's expected payoff when ROW plays i and the COLUMN player mixes
    according to opp_dist. Used for ROW's best response (maximize).
    """
    n = len(payoff)
    out = [0.0] * n
    for i in range(n):
        row = payoff[i]
        s = 0.0
        for j in range(n):
            s += row[j] * opp_dist[j]
        out[i] = s
    return out


def _expected_values_by_col(payoff: List[List[float]], opp_dist: Sequence[float]) -> List[float]:
    """For each COLUMN j, expected ROW payoff C[j] = sum_i payoff[i][j] * opp_dist[i].

    This is ROW's expected payoff when COLUMN plays j and the ROW player mixes
    according to opp_dist. Used for COLUMN's best response: COLUMN wants to
    MINIMIZE row's payoff, so COLUMN picks argmin(C).
    """
    n = len(payoff)
    out = [0.0] * n
    for j in range(n):
        s = 0.0
        for i in range(n):
            s += payoff[i][j] * opp_dist[i]
        out[j] = s
    return out


def _argmax(values: Sequence[float]) -> int:
    """Index of the max; ties broken by first occurrence."""
    best_i = 0
    best_v = values[0]
    for i in range(1, len(values)):
        if values[i] > best_v:
            best_v = values[i]
            best_i = i
    return best_i


def _argmin(values: Sequence[float]) -> int:
    """Index of the min; ties broken by first occurrence."""
    best_i = 0
    best_v = values[0]
    for i in range(1, len(values)):
        if values[i] < best_v:
            best_v = values[i]
            best_i = i
    return best_i


def _best_response(payoff: List[List[float]], opp_dist: Sequence[float], maximize: bool, rng: random.Random | None = None) -> int:
    """Best response to an opponent distribution.

    maximize=True -> argmax of expected payoff (ROW's best response).
    maximize=False -> argmin of expected payoff (COL's best response, since col
    minimizes row's payoff in zero-sum).

    Ties (within 1e-9) are broken UNIFORMLY AT RANDOM among the tied actions.
    Random tie-breaking is essential for fictitious play to mix across symmetric
    strategies (e.g. Rock-Paper-Scissors): deterministic argmax would pick the
    first tied index forever and the time-average would never reach the uniform
    Nash. With random tie-breaking the FP trajectory cycles through all tied
    best responses and the time-average converges to the equilibrium mixture.
    """
    if maximize:
        ev = _expected_values_by_row(payoff, opp_dist)
    else:
        ev = _expected_values_by_col(payoff, opp_dist)
    if maximize:
        target = max(ev)
    else:
        target = min(ev)
    tied = [i for i, v in enumerate(ev) if abs(v - target) <= 1e-9]
    if not tied:
        return _argmax(ev) if maximize else _argmin(ev)
    if len(tied) == 1:
        return tied[0]
    return (rng or random).choice(tied)


def fictitious_play(
    payoff: List[List[float]],
    population: Sequence[str],
    iterations: int = 1000,
    rng: random.Random | None = None,
) -> Dict[str, float]:
    """Fictitious play for a 2-player zero-sum game.

    payoff[i][j] = ROW player's expected payoff when row plays i, col plays j.
    Each iteration:
      - row best-responds (max expected payoff) to col's empirical distribution.
      - col best-responds (min expected payoff) to row's empirical distribution.
    Returns the time-averaged ROW mixed strategy over the iterations (converges
    to Nash for zero-sum games when tie-breaking is randomized).

    Degenerate cases:
      - empty population -> {}.
      - single strategy -> {that bot: 1.0}.

    Args:
        payoff: n x n zero-sum matrix (list-of-lists).
        population: ordered bot names matching payoff indices.
        iterations: number of FP steps. 2000-5000 gives tight convergence.
        rng: optional random.Random for deterministic tie-breaking in tests.

    Returns:
        {bot_name: probability} summing to ~1.0.
    """
    n = len(population)
    if n == 0:
        return {}
    if n == 1:
        return {population[0]: 1.0}

    _rng = rng or random
    # Empirical action counts for each side.
    row_counts = [0] * n
    col_counts = [0] * n

    for _ in range(max(1, iterations)):
        # Build current empirical distributions (avoid divide-by-zero on first
        # step by initializing both sides to uniform via count=0 -> uniform).
        row_total = sum(row_counts)
        col_total = sum(col_counts)
        col_dist = [c / col_total for c in col_counts] if col_total > 0 else [1.0 / n] * n
        row_dist = [c / row_total for c in row_counts] if row_total > 0 else [1.0 / n] * n

        # Row best-responds to col's empirical (maximize row's expected payoff).
        br_row = _best_response(payoff, col_dist, maximize=True, rng=_rng)
        row_counts[br_row] += 1

        # Col best-responds to row's empirical (minimize row's expected payoff).
        br_col = _best_response(payoff, row_dist, maximize=False, rng=_rng)
        col_counts[br_col] += 1

    # Time-averaged row strategy = normalized row counts.
    total = sum(row_counts)
    if total == 0:
        return uniform_meta(population)
    return {population[i]: row_counts[i] / total for i in range(n)}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def solve_meta(
    h2h: dict,
    population: Sequence[str],
    method: str = "fp",
    iterations: int = 1000,
) -> Dict[str, float]:
    """Dispatch meta-solver.

    method:
        "fp"      -> fictitious_play (default)
        "uniform" -> uniform over population
        anything else -> uniform fallback (defensive; never raise).
    """
    n = len(population)
    if n == 0:
        return {}
    if method == "fp":
        payoff = build_payoff_matrix(h2h, population)
        return fictitious_play(payoff, population, iterations=iterations)
    # "uniform" and unknown method -> uniform (never raise).
    return uniform_meta(population)


def sample_meta(meta: Dict[str, float], rng: random.Random | None = None) -> str | None:
    """Sample one bot name from a meta distribution (for the MixtureBot roll).

    Returns None for an empty/invalid meta. Defensive: weights < 0 are clipped to
    0, and a zero-total distribution falls back to uniform over the keys.
    """
    if not meta:
        return None
    bots = list(meta.keys())
    weights = []
    for b in bots:
        w = meta[b]
        try:
            w = float(w)
        except (TypeError, ValueError):
            w = 0.0
        weights.append(max(0.0, w))
    total = sum(weights)
    if total <= 0.0:
        # uniform fallback
        if not bots:
            return None
        return (rng or random).choice(bots)
    r = (rng or random).random() * total
    cum = 0.0
    for b, w in zip(bots, weights):
        cum += w
        if r <= cum:
            return b
    return bots[-1]


def build_mixture_config(
    h2h: dict,
    population: Sequence[str],
    bot_paths: Dict[str, str],
    method: str = "fp",
    iterations: int = 2000,
) -> dict:
    """Build the mixture_config.json dict for bots/mixture_main.

    Args:
        h2h: head_to_head.json dict.
        population: ordered bot names to include in the mixture.
        bot_paths: {bot_name: absolute main.py path}.
        method: "fp" (fictitious play) or "uniform".
        iterations: FP iterations.

    Returns:
        {"strategy_weights": {bot: prob}, "bot_paths": {bot: abs_main_path}}.
        Only bots present in BOTH population and bot_paths are included; entries
        whose main.py path does not exist are dropped (defensive).
    """
    import os
    pop = [b for b in population if b in bot_paths and os.path.isfile(bot_paths[b])]
    if not pop:
        return {"strategy_weights": {}, "bot_paths": {}}
    meta = solve_meta(h2h, pop, method=method, iterations=iterations)
    weights = {b: float(meta.get(b, 0.0)) for b in pop}
    # Normalize (defensive: meta sums to ~1 but clip tiny negatives).
    total = sum(max(0.0, w) for w in weights.values())
    if total > 0:
        weights = {b: max(0.0, w) / total for b, w in weights.items()}
    paths = {b: bot_paths[b] for b in pop}
    return {"strategy_weights": weights, "bot_paths": paths}
