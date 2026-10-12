"""Pool-ratchet publication model: configuration and selection pipeline.

This module deliberately contains NO gate-decision bytes.  It resolves the
publication mode (``POK_PUBLISH_MODE``), the pool target capacity
(``POK_POOL_RATCHET_TARGET_SIZE``), the reap selection policy / protection
profile for the active mode, and selects the current pool-weakest member from
one frozen generation-evidence snapshot using the SAME ordering key as the
reap victim selection (``tool_bot_management_reap``'s conservative Glicko
``r - 2*rd`` ascending sort).  Tuning bytes live here precisely so they stay
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


def select_pool_weakest(rows, exclude_names=None) -> dict | None:
    """Return the weakest eligible row from one frozen snapshot row set.

    The ordering key is exactly the reap victim key
    (``tool_bot_management_reap._select_reap_candidate_from_snapshot``):
    ``(conservative_rating, games, version)`` ascending — the position a
    candidate must beat to publish is the position the reap replaces.

    ``rows`` are frozen generation-evidence selection rows (``name``,
    ``conservative_rating``, ``games``).  Rows whose name is in
    ``exclude_names`` are skipped.  Returns the full weakest row dict (a
    shallow copy) or ``None`` when no eligible row remains (pool smaller than
    two, or every row excluded — the bootstrap degeneration).  A non-excluded
    row without a numeric ``conservative_rating`` raises
    ``PoolRatchetSelectionError`` (fail-closed; never silently downgrade the
    ordering to a stale field).
    """

    from bot_namespace import parse_bot_version

    excluded = {str(name) for name in (exclude_names or set())}
    eligible: list[tuple[tuple[float, int, int], dict]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        if not name or name in excluded:
            continue
        raw_rating = row.get("conservative_rating")
        try:
            rating = float(raw_rating)
        except (TypeError, ValueError):
            raise PoolRatchetSelectionError(
                "pool_snapshot_conservative_rating_missing:"
                f"{name or 'unknown'}:{raw_rating!r}"
            ) from None
        try:
            games = int(row.get("games", 0) or 0)
        except (TypeError, ValueError):
            raise PoolRatchetSelectionError(
                f"pool_snapshot_games_invalid:{name}"
            ) from None
        version = parse_bot_version(name)
        eligible.append(((rating, games, version or 0), dict(row)))
    if not eligible:
        return None
    eligible.sort(key=lambda item: item[0])
    return eligible[0][1]
