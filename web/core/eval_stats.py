"""Statistical helpers for precommit eval (paired bootstrap CI).

Pure-Python implementation — no scipy/numpy dependency. Used by the precommit
regression gate to convert the per-mirror-pair net-chips vector into a 95%
confidence interval instead of the noisy binary win/loss count.

Phase 2 additions:
  - confidence_sequence_ci: anytime-valid sub-Gaussian Confidence Sequence
    (Jamdagni-Goel 2024 / sequential-Hoeffding style) for bounded samples.
    Supports sequential early-stopping without alpha correction.
  - sequential_decision: per-arrival reject/accept/continue gating built on the
    anytime-valid CI. Used by tool_eval's serial/gather fallback path to break
    out of the mirror-battle generator as soon as a decision is confident.
"""

import math
import random


# net-chips per mirror pair is bounded: a completed pair (normal + mirror hand)
# moves bot0's net chips into [-2*INITIAL_CHIPS, +2*INITIAL_CHIPS] = [-40000,
# +40000], so the value range R = 80000. Using the conservative fixed R (rather
# than an empirical [min,max]) keeps the anytime-validity guarantee sound and
# makes the half-width independent of observed extremes.
NET_CHIPS_RANGE = 80000.0


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


def confidence_sequence_ci(samples, alpha=0.05, R=NET_CHIPS_RANGE):
    """Anytime-valid (1-α) confidence interval for a bounded sub-Gaussian mean.

    Uses the sub-Gaussian Confidence Sequence from Jamdagni-Goel 2024 /
    sequential-Hoeffding. After collecting t=len(samples) samples, the half-width

        h_t = R * sqrt( ln( π²·t² / (6α) ) / (2t) )

    gives an interval that covers the true mean with probability ≥ (1-α) at
    EVERY stopping time t simultaneously (anytime-valid). This is the key
    advantage over a fixed-N bootstrap CI: peeking / early-stopping does NOT
    inflate the type-I error.

    Args:
        samples: iterable of numeric observations (bounded sub-Gaussian).
        alpha: significance level (default 0.05 → 95% anytime-valid CI).
        R: sample value range (b-a). Defaults to NET_CHIPS_RANGE (80000) for
            mirror net-chips. Conservative fixed R keeps the guarantee sound.

    Returns:
        (lo, hi, half_width) tuple. (None, None, None) for empty samples.
    """
    xs = list(samples)
    t = len(xs)
    if t == 0:
        return (None, None, None)
    mean = sum(xs) / t
    # ln(π²·t²/(6α)); for t>=1 and α≤0.5 this is ln(>=9.87/3) ≈ 1.19 > 0, so the
    # half-width is always real and positive. Guard the argument just in case
    # a caller passes a huge alpha that drives the log term non-positive.
    log_arg = (math.pi ** 2) * (t ** 2) / (6.0 * alpha)
    log_val = math.log(log_arg) if log_arg > 0.0 else 0.0
    half_width = R * math.sqrt(log_val / (2.0 * t))
    return (mean - half_width, mean + half_width, half_width)


def sequential_decision(samples_so_far, reject_threshold=None, accept_threshold=None,
                        alpha=0.05, R=NET_CHIPS_RANGE, n_max=None):
    """Per-arrival sequential gate for an anytime-valid Confidence Sequence.

    Called after each new sample is appended. Returns a dict describing whether
    to reject, accept, or continue sampling. The CI uses confidence_sequence_ci
    so the resulting decision is anytime-valid (no alpha correction needed
    regardless of how many times this is called).

    Args:
        samples_so_far: list of observations collected so far (the function does
            NOT mutate it).
        reject_threshold: if the CI UPPER bound drops below this, the candidate
            is significantly below the threshold → DECIDE_REJECT. Pass None to
            disable reject-side early stopping.
        accept_threshold: if the CI LOWER bound rises above this, the candidate
            is significantly above the threshold → DECIDE_ACCEPT. Pass None to
            disable accept-side early stopping.
        alpha: significance level passed to the CI (default 0.05).
        R: sample value range passed to the CI (default NET_CHIPS_RANGE).
        n_max: optional sample cap. When provided and len(samples_so_far) >= n_max
            without a reject/accept decision, returns "UNDECIDED_AT_LIMIT" so the
            caller can fall back to a fixed-N test (e.g. paired_bootstrap_ci).

    Returns:
        {
          "decision": "CONTINUE"|"DECIDE_REJECT"|"DECIDE_ACCEPT"|"UNDECIDED_AT_LIMIT",
          "ci_lo": float|None, "ci_hi": float|None, "half_width": float|None,
          "mean": float|None, "n": int, "rule": str,
        }
        Empty samples → decision "CONTINUE" with null CI fields and rule "empty".
    """
    xs = list(samples_so_far)
    n = len(xs)
    lo, hi, hw = confidence_sequence_ci(xs, alpha=alpha, R=R)
    base = {
        "ci_lo": lo,
        "ci_hi": hi,
        "half_width": hw,
        "mean": (sum(xs) / n) if n > 0 else None,
        "n": n,
    }
    if n == 0:
        return {**base, "decision": "CONTINUE", "rule": "empty"}

    if reject_threshold is not None and hi is not None and hi < reject_threshold:
        return {**base, "decision": "DECIDE_REJECT",
                "rule": f"ci_hi<{reject_threshold}"}
    if accept_threshold is not None and lo is not None and lo > accept_threshold:
        return {**base, "decision": "DECIDE_ACCEPT",
                "rule": f"ci_lo>{accept_threshold}"}
    if n_max is not None and n >= n_max:
        return {**base, "decision": "UNDECIDED_AT_LIMIT",
                "rule": f"reached n_max={n_max}"}
    return {**base, "decision": "CONTINUE", "rule": "ci_straddles_threshold"}
