"""Pool-ratchet publication model: configuration and selection pipeline.

This module deliberately contains NO gate-decision bytes.  It resolves the
publication mode (``POK_PUBLISH_MODE``), the pool target capacity
(``POK_POOL_RATCHET_TARGET_SIZE``), the reap selection policy / protection
profile for the active mode, and selects the current pool-weakest member from
one frozen generation-evidence snapshot by MEASURED strength (Wilson-95%
lower bound of the observed win rate once ``games >= 600``; conservative
Glicko with rd capped at 150 below that sample floor — 2026-10-16
calibration, see ``POOL_WEAKEST_WILSON_MIN_GAMES``).  Tuning bytes live here
precisely so they stay
OUT of the precommit evaluator identity (``pool_ratchet_gate.py`` is the
replay-sensitive member bound into ``precommit_eval_contract.SEMANTIC_PATHS``;
this module is not in any identity list).

The mode is read only at the two freeze moments (precommit plan creation and
the pool_reap handoff plan build); an in-flight generation's gate semantics
are decided by its frozen plan, never by a live env flip.

Rollback: unset/empty ``POK_PUBLISH_MODE`` falls back to ``legacy`` in code
(no env key required), and the legacy path is byte-equivalent to the
pre-ratchet behavior except that per-plan deck-seed nonces stay enabled (a
correctness fix, not a model switch).
"""

from __future__ import annotations

import math
import os

PUBLISH_MODE_ENV = "POK_PUBLISH_MODE"
POOL_TARGET_SIZE_ENV = "POK_POOL_RATCHET_TARGET_SIZE"

LEGACY_PUBLISH_MODE = "legacy"
POOL_RATCHET_PUBLISH_MODE = "pool_ratchet"
VALID_PUBLISH_MODES = (LEGACY_PUBLISH_MODE, POOL_RATCHET_PUBLISH_MODE)

# Lower bound for the ratchet target size.  ``_MIN_CROSSOVER_POOL_SIZE`` = 12
# in generation_scheduler_source_selection.py is the crossover-diversity hard
# floor: a pool smaller than 12 silently disables crossover preparation, so a
# configured N < 12 is rejected outright instead of accepted and quietly
# degrading the evolution search.  The absolute backstop remains
# ``evolution_infra.MAX_ACTIVE_BOTS`` (30).
POOL_TARGET_MIN_SIZE = 12
POOL_TARGET_MAX_SIZE = 30
# Observed production pool floor (12 completed tags at design time); used
# only when the ratchet mode is active and the env key is unset.
POOL_TARGET_DEFAULT_SIZE = 12

REAP_SELECTION_POLICY_LEGACY = "conservative_glicko_v1"
REAP_SELECTION_POLICY_POOL_RATCHET = "conservative_glicko_pool_ratchet_v1"
REAP_PROTECTION_PROFILE_LEGACY = "legacy"
REAP_PROTECTION_PROFILE_POOL_RATCHET = "pool_ratchet"

# Pool-weakest ordering calibration (2026-10-16).  The former key was the bare
# conservative rating ``r - 2*rd``; Glicko rd inflates for every bot that has
# not played recently (observed snapshot rd up to 298.8), so the key nominated
# a strong-but-rusty bot as "weakest" (v11: 993 games, 0.5156 observed win
# rate — the pool's 3rd strongest by measurement, 45W-50L vs its own parent)
# instead of the genuinely weak v173 (0.4651 over 645) / v83 (0.4668).  The
# replacement key is measured strength with an explicit confidence floor:
#
# * ``games >= POOL_WEAKEST_WILSON_MIN_GAMES`` (600) with a numeric
#   ``win_rate``: tier 0, ordered by the Wilson-95% LOWER bound of the
#   observed win rate (snapshot ``win_rate`` / ``games`` fields).
# * every other row (under-sampled, or a row without a usable ``win_rate``):
#   tier 1, ordered by the conservative score with rd capped at
#   ``POOL_WEAKEST_FALLBACK_RD_CAP`` (``rating - 2*min(rd, 150)``). The cap
#   shrinks the OLD bug shape (an inflated rd dominating the ordering) but
#   does not eliminate it: when the plain-rating gap between two tier-1
#   members is smaller than twice their capped-rd difference, the discount
#   still decides the order (red-team 2026-10-16: with the audit period's
#   clustered ratings — v11 1487.6/298.8 vs v173 1490.2/120 — the fallback
#   still orders v11 weakest). The residual is fail-safe: the gate then
#   targets a too-strong member, lowering the admission rate, never
#   nominating a measured-weaker member as the bar.
#
# Tier 0 always sorts before tier 1: the admission gate tests against
# statistically established members first, and a row whose measured strength
# is unknown is never nominated as weakest while any measured row exists.
POOL_WEAKEST_WILSON_MIN_GAMES = 600
POOL_WEAKEST_FALLBACK_RD_CAP = 150.0

# Queue #3 (2026-10-30): the tier-0 measured-strength sample floor is now
# env-tunable.  Why: at 600 games the pool's tier-0 population was a SINGLE
# member for 29 consecutive generations (v173 monopoly — block-30 pool audit),
# because 600 daemon games accrue at ~4.5 days per new entrant while the
# ratchet keeps the pool young — every fresh publication enters tier 1 and
# stays there for weeks.  A permanent monopoly means the admission gate tests
# one fixed opponent forever: no cross-member discrimination, and v173's
# measured 0.4651 becomes a standing bar candidates can specialize against.
#
# Bounds 100..600, code default 600 (unset = byte-identical to the 2026-10-16
# calibration).  The floor is 100, NOT lower, deliberately: at ~100 games the
# Wilson-95% lower bound is ±0.098 wide at p≈.47 — a measured-STRONG member
# can draw a bound below a genuinely weak one, re-opening the exact
# rd-inflation-style mis-nomination (v11 nominated weakest over 993 games)
# this floor was introduced to kill.  A production 300 halves the accrual
# wait at ±0.055 — still a conservative draw-risk band, and the fail-safe
# direction of a wrong pick is unchanged (gate targets a too-strong member,
# lowering the admission rate, never the reverse).
POOL_WEAKEST_WILSON_MIN_GAMES_ENV = "POK_POOL_WEAKEST_MIN_GAMES"
POOL_WEAKEST_WILSON_MIN_GAMES_FLOOR = 100
POOL_WEAKEST_WILSON_MIN_GAMES_CEIL = 600


def pool_weakest_min_games() -> int:
    """Effective tier-0 sample floor for the pool-weakest ordering key.

    Read at the same freeze moments as the rest of the ratchet configuration
    (precommit plan build, pool_reap handoff plan build), never mid-deck: an
    in-flight generation's gate target is already bound into its frozen plan.
    Unset/empty returns ``POOL_WEAKEST_WILSON_MIN_GAMES`` (600).  An
    out-of-range or non-integer value fails fast with a system event, exactly
    like ``pool_target_size`` — a mis-set floor must not silently degrade the
    measured-strength tiering.
    """

    raw = str(os.environ.get(POOL_WEAKEST_WILSON_MIN_GAMES_ENV) or "").strip()
    if not raw:
        return POOL_WEAKEST_WILSON_MIN_GAMES
    try:
        games = int(raw)
    except ValueError:
        games = None
    if (
        games is None
        or not (
            POOL_WEAKEST_WILSON_MIN_GAMES_FLOOR
            <= games
            <= POOL_WEAKEST_WILSON_MIN_GAMES_CEIL
        )
    ):
        message = (
            f"Invalid {POOL_WEAKEST_WILSON_MIN_GAMES_ENV}={raw!r}; expected an "
            f"integer in [{POOL_WEAKEST_WILSON_MIN_GAMES_FLOOR}, "
            f"{POOL_WEAKEST_WILSON_MIN_GAMES_CEIL}] "
            f"({POOL_WEAKEST_WILSON_MIN_GAMES_FLOOR} = the Wilson half-width "
            "where a measured-strong member can be mis-nominated weakest; "
            "600 = the 2026-10-16 calibration)"
        )
        _log_config_error(
            "pipeline.pool_ratchet_config_invalid",
            POOL_WEAKEST_WILSON_MIN_GAMES_ENV,
            raw,
            message,
        )
        raise PoolRatchetConfigError(f"pool_weakest_min_games_invalid:{raw}")
    return games


# Two-sided 95% normal quantile for the Wilson score interval.
WILSON_95_Z = 1.959963984540054


class PoolRatchetConfigError(RuntimeError):
    """Raised when a pool-ratchet env configuration value is invalid."""


class PoolRatchetSelectionError(RuntimeError):
    """Raised when the frozen snapshot cannot support a weakest-pool pick."""


def _log_config_error(event: str, key: str, value: str, message: str) -> None:
    try:
        from system_log import log_system_event

        log_system_event(
            event,
            "error",
            message,
            {"key": key, "value": value},
        )
    except Exception:
        pass


def publish_mode() -> str:
    """Resolve the publication model; unset/empty falls back to ``legacy``.

    An unrecognized value is a fail-fast configuration error: it emits a
    system event and raises ``PoolRatchetConfigError`` so callers pause
    instead of guessing a publication model.
    """

    raw = str(os.environ.get(PUBLISH_MODE_ENV) or "").strip()
    if not raw:
        return LEGACY_PUBLISH_MODE
    if raw not in VALID_PUBLISH_MODES:
        message = (
            f"Invalid {PUBLISH_MODE_ENV}={raw!r}; expected one of "
            f"{list(VALID_PUBLISH_MODES)} (unset = legacy code fallback)"
        )
        _log_config_error(
            "pipeline.pool_ratchet_config_invalid",
            PUBLISH_MODE_ENV,
            raw,
            message,
        )
        raise PoolRatchetConfigError(f"publish_mode_invalid:{raw}")
    return raw


def pool_target_size() -> int:
    """Pool target capacity N for the active mode.

    ``legacy`` always resolves to the absolute backstop
    ``evolution_infra.MAX_ACTIVE_BOTS`` (30) — byte-equivalent to the
    pre-ratchet reap cap.  ``pool_ratchet`` reads
    ``POK_POOL_RATCHET_TARGET_SIZE`` (default 12) and validates
    ``12 <= N <= 30``; an out-of-range or non-integer value fails fast with a
    system event.
    """

    mode = publish_mode()
    if mode != POOL_RATCHET_PUBLISH_MODE:
        from evolution_infra import MAX_ACTIVE_BOTS

        return int(MAX_ACTIVE_BOTS)
    raw = str(os.environ.get(POOL_TARGET_SIZE_ENV) or "").strip()
    if not raw:
        return POOL_TARGET_DEFAULT_SIZE
    try:
        size = int(raw)
    except ValueError:
        size = None
    if size is None or not (POOL_TARGET_MIN_SIZE <= size <= POOL_TARGET_MAX_SIZE):
        message = (
            f"Invalid {POOL_TARGET_SIZE_ENV}={raw!r}; expected an integer in "
            f"[{POOL_TARGET_MIN_SIZE}, {POOL_TARGET_MAX_SIZE}] "
            f"({POOL_TARGET_MIN_SIZE} = crossover diversity floor "
            "_MIN_CROSSOVER_POOL_SIZE)"
        )
        _log_config_error(
            "pipeline.pool_ratchet_config_invalid",
            POOL_TARGET_SIZE_ENV,
            raw,
            message,
        )
        raise PoolRatchetConfigError(f"pool_target_size_invalid:{raw}")
    return size


def reap_selection_policy_for_mode(mode: str | None = None) -> str:
    """Frozen reap selection policy label for the given publication mode."""

    resolved = str(mode) if mode is not None else publish_mode()
    if resolved == POOL_RATCHET_PUBLISH_MODE:
        return REAP_SELECTION_POLICY_POOL_RATCHET
    if resolved in VALID_PUBLISH_MODES or mode is None:
        return REAP_SELECTION_POLICY_LEGACY
    raise PoolRatchetConfigError(f"publish_mode_invalid:{resolved}")


def reap_protection_profile_for_mode(mode: str | None = None) -> str:
    """Reap protection profile for the given publication mode.

    ``pool_ratchet`` protects only the latest entrant and the priority bot;
    ``legacy`` keeps the historical protection rules (newest-3 under hard
    overflow, <600-game soft protection).
    """

    resolved = str(mode) if mode is not None else publish_mode()
    if resolved == POOL_RATCHET_PUBLISH_MODE:
        return REAP_PROTECTION_PROFILE_POOL_RATCHET
    if resolved in VALID_PUBLISH_MODES or mode is None:
        return REAP_PROTECTION_PROFILE_LEGACY
    raise PoolRatchetConfigError(f"publish_mode_invalid:{resolved}")


def wilson_lower_bound(win_rate: float, games: int) -> float:
    """Wilson score interval 95% LOWER bound for an observed win rate.

    ``(p̂ + z²/2n − z·sqrt(p̂(1−p̂)/n + z²/4n²)) / (1 + z²/n)`` with the
    two-sided 95% z.  ``win_rate`` is clamped to [0, 1] and ``games`` to at
    least 1 so a malformed snapshot cell degrades to a conservative score
    instead of raising inside the sort.
    """

    n = max(1, int(games))
    p = min(1.0, max(0.0, float(win_rate)))
    if not math.isfinite(p):
        # min/max propagate NaN unpredictably; a non-finite cell degrades to
        # the most conservative measurable score.
        p = 0.0
    z2 = WILSON_95_Z * WILSON_95_Z
    center = p + z2 / (2.0 * n)
    margin = WILSON_95_Z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return (center - margin) / (1.0 + z2 / n)


def _numeric(value) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    # A NaN cell (JSON can carry one) breaks total-order transitivity and
    # would make the weakest choice depend on input row order — degrade the
    # row to the tier-1 fallback instead (red-team 2026-10-16).
    if not math.isfinite(result):
        return None
    return result


def select_pool_weakest(rows, exclude_names=None) -> dict | None:
    """Return the weakest eligible row from one frozen snapshot row set.

    The ordering key is the 2026-10-16 calibration described at
    ``POOL_WEAKEST_WILSON_MIN_GAMES`` above: rows with statistically
    sufficient measured strength (``games >= 600`` and a numeric
    ``win_rate``) are tier 0 ordered by the Wilson-95% LOWER bound of the
    observed win rate; every other row is tier 1 ordered by the conservative
    score with rd capped at 150 (``rating - 2*min(rd, 150)``).  This replaces
    the reap victim key (bare ``conservative_rating``) for pool-weakest
    nomination — the reap lane keeps its own frozen
    ``conservative_glicko_*`` policies — because rd inflation, not measured
    weakness, dominated that ordering in production snapshots.

    ``rows`` are frozen generation-evidence selection rows (``name``,
    ``rating``, ``rd``, ``conservative_rating``, ``win_rate``, ``games``).
    Rows whose name is in ``exclude_names`` are skipped.  Returns the full
    weakest row dict (a shallow copy) or ``None`` when no eligible row remains
    (pool smaller than two, or every row excluded — the bootstrap
    degeneration).  A tier-1 row without numeric ``rating``/``rd`` raises
    ``PoolRatchetSelectionError`` (fail-closed; never silently downgrade the
    ordering to a stale field).
    """

    from bot_namespace import parse_bot_version

    min_games_floor = pool_weakest_min_games()
    excluded = {str(name) for name in (exclude_names or set())}
    eligible: list[tuple[tuple[int, float, int, int], dict]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        if not name or name in excluded:
            continue
        try:
            games = int(row.get("games", 0) or 0)
        except (TypeError, ValueError):
            raise PoolRatchetSelectionError(
                f"pool_snapshot_games_invalid:{name}"
            ) from None
        version = parse_bot_version(name)
        win_rate = _numeric(row.get("win_rate"))
        if games >= min_games_floor and win_rate is not None:
            key = (
                0,
                wilson_lower_bound(win_rate, games),
                games,
                version or 0,
            )
        else:
            rating = _numeric(row.get("rating"))
            rd = _numeric(row.get("rd"))
            if rating is None:
                raise PoolRatchetSelectionError(
                    "pool_snapshot_rating_missing:"
                    f"{name or 'unknown'}:{row.get('rating')!r}"
                ) from None
            if rd is None:
                raise PoolRatchetSelectionError(
                    f"pool_snapshot_rd_missing:{name}:{row.get('rd')!r}"
                ) from None
            key = (
                1,
                rating - 2.0 * min(rd, POOL_WEAKEST_FALLBACK_RD_CAP),
                games,
                version or 0,
            )
        eligible.append((key, dict(row)))
    if not eligible:
        return None
    # (tier, key, games, version) is a total order, so equal keys resolve
    # deterministically and repeated calls on one frozen snapshot agree.
    eligible.sort(key=lambda item: item[0])
    return eligible[0][1]
