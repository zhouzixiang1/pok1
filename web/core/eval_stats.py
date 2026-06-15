"""Statistical helpers for precommit eval (paired bootstrap CI).

Pure-Python implementation — no scipy/numpy dependency. Used by the precommit
regression gate to convert the per-mirror-pair net-chips vector into a 95%
confidence interval instead of the noisy binary win/loss count.
"""

import random


def paired_bootstrap_ci(values, n_resamples=1000, alpha=0.05, seed=12345):
    """Empirical bootstrap confidence interval for the mean of ``values``.

    Resamples ``values`` with replacement ``n_resamples`` times, computes the
    mean of each resample, and returns the empirical ``[alpha/2, 1-alpha/2]``
    percentile interval.

    Args:
        values: iterable of numeric paired observations (e.g. per-mirror-pair
            net chips for the candidate bot).
        n_resamples: number of bootstrap resamples (default 1000).
        alpha: two-sided significance level (default 0.05 → 95% CI).
        seed: deterministic RNG seed for reproducibility across daemon workers.

    Returns:
        (lo, hi) tuple — the lower and upper CI bounds. Returns (0.0, 0.0)
        if ``values`` is empty.
    """
    sample = list(values)
    n = len(sample)
    if n == 0:
        return (0.0, 0.0)
    if n == 1:
        v = float(sample[0])
        return (v, v)

    rng = random.Random(seed)
    means = []
    for _ in range(n_resamples):
        total = 0.0
        for _ in range(n):
            total += sample[rng.randrange(n)]
        means.append(total / n)

    means.sort()
    lo_idx = max(0, int((alpha / 2.0) * (n_resamples - 1)))
    hi_idx = min(n_resamples - 1, int((1.0 - alpha / 2.0) * (n_resamples - 1)))
    return (means[lo_idx], means[hi_idx])
