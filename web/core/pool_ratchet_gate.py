"""Pool-ratchet precommit gate semantics and deck-seed derivation.

EVERY byte in this module decides publication or derives the frozen deck
seed, so it is a member of ``precommit_eval_contract.SEMANTIC_PATHS``: any
edit (a flipped comparison, a moved branch, a changed derivation formula)
changes the precommit ``evaluator_identity`` and pauses every in-flight
frozen plan with ``PRECOMMIT CONTRACT DRIFT`` instead of silently re-deciding
a gate.  Tuning/mode-resolution bytes deliberately live in
``pool_ratchet.py``, which is NOT in any identity list.

Contents:
* ``POOL_GATE_MIN_SAMPLES`` / ``POOL_GATE_MAX_SCORE`` /
  ``POOL_GATE_TIE_CI_LOWER_MIN`` — pool-gate thresholds (env-overridable, read
  ONCE at the plan-freeze moment and frozen into the plan settings; a live env
  flip never re-decides an in-flight generation).
* ``pool_gate_blockers`` — the pool admission gate: the candidate must
  statistically beat the current pool-weakest member over complete 70-hand
  matches.  The structure replicates the parent gate
  (``strength_order.precommit_outcome_blockers``): majority score strictly
  above the threshold over at least ``min_samples`` complete matches, with an
  exact tie admitted only when the paired net-chip bootstrap 95% CI LOWER
  bound is positive.
* ``generate_deck_seed_nonce`` / ``derive_deck_seed_base`` — the per-plan
  one-time 128-bit nonce and its deterministic deck-seed-base derivation.
  The derivation formula is re-proven by ``validate_precommit_plan``, which is
  exactly why it lives in this identity-bound module.
"""

from __future__ import annotations

import hashlib
import math
import os
import secrets

POOL_GATE_MIN_SAMPLES_ENV = "POK_POOL_GATE_MIN_SAMPLES"
POOL_GATE_MAX_SCORE_ENV = "POK_POOL_GATE_MAX_SCORE"
POOL_GATE_TIE_CI_LOWER_MIN_ENV = "POK_POOL_GATE_TIE_CI_LOWER_MIN"

# Code defaults; optional per-plan-frozen env overrides.  Intentionally NOT
# committed to deploy/tencent-cloud/env.runtime so the thresholds cannot drift
# unnoticed — see the comment block at the end of that file.
POOL_GATE_MIN_SAMPLES = 6
POOL_GATE_MAX_SCORE = 0.50
POOL_GATE_TIE_CI_LOWER_MIN = 0.0

SEED_DERIVATION_NONCE = "nonce_sha256_v1"
SEED_DERIVATION_EXPLICIT = "explicit_base"

POOL_GATE_SETTINGS_KEYS = (
    "label",
    "min_samples",
    "max_score",
    "tie_ci_lower_min",
)


class PoolGateConfigError(RuntimeError):
    """Raised when a pool-gate threshold override is invalid."""


def _log_config_error(key: str, value: str, message: str) -> None:
    try:
        from system_log import log_system_event

        log_system_event(
            "pipeline.pool_ratchet_config_invalid",
            "error",
            message,
            {"key": key, "value": value},
        )
    except Exception:
        pass


def pool_gate_min_samples() -> int:
    raw = str(os.environ.get(POOL_GATE_MIN_SAMPLES_ENV) or "").strip()
    if not raw:
        return POOL_GATE_MIN_SAMPLES
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        message = (
            f"Invalid {POOL_GATE_MIN_SAMPLES_ENV}={raw!r}; expected an "
            "integer >= 1"
        )
        _log_config_error(POOL_GATE_MIN_SAMPLES_ENV, raw, message)
        raise PoolGateConfigError(f"pool_gate_min_samples_invalid:{raw}")
    return value


def pool_gate_max_score() -> float:
    raw = str(os.environ.get(POOL_GATE_MAX_SCORE_ENV) or "").strip()
    if not raw:
        return POOL_GATE_MAX_SCORE
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not math.isfinite(value) or not (0.0 < value < 1.0):
        message = (
            f"Invalid {POOL_GATE_MAX_SCORE_ENV}={raw!r}; expected a finite "
            "fraction strictly between 0 and 1"
        )
        _log_config_error(POOL_GATE_MAX_SCORE_ENV, raw, message)
        raise PoolGateConfigError(f"pool_gate_max_score_invalid:{raw}")
    return value


def pool_gate_tie_ci_lower_min() -> float:
    raw = str(os.environ.get(POOL_GATE_TIE_CI_LOWER_MIN_ENV) or "").strip()
    if not raw:
        return POOL_GATE_TIE_CI_LOWER_MIN
    try:
        value = float(raw)
    except ValueError:
        value = math.inf
    if not math.isfinite(value):
        message = (
            f"Invalid {POOL_GATE_TIE_CI_LOWER_MIN_ENV}={raw!r}; expected a "
            "finite float"
        )
        _log_config_error(POOL_GATE_TIE_CI_LOWER_MIN_ENV, raw, message)
        raise PoolGateConfigError(f"pool_gate_tie_ci_lower_min_invalid:{raw}")
    return value


def build_pool_gate_settings(label: str) -> dict:
    """Resolve the freezable pool-gate settings at the plan-freeze moment."""

    text = str(label or "").strip()
    if not text:
        raise PoolGateConfigError("pool_gate_label_empty")
    return {
        "label": text,
        "min_samples": pool_gate_min_samples(),
        "max_score": pool_gate_max_score(),
        "tie_ci_lower_min": pool_gate_tie_ci_lower_min(),
    }


def validate_pool_gate_settings(pool_gate) -> dict:
    """Normalize one pool-gate spec or raise ``ValueError`` (contract layer)."""

    if not isinstance(pool_gate, dict):
        raise ValueError("pool_gate_not_object")
    if set(pool_gate) != set(POOL_GATE_SETTINGS_KEYS):
        raise ValueError("pool_gate_keys_invalid")
    label = pool_gate.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("pool_gate_label_invalid")
    min_samples = pool_gate.get("min_samples")
    if type(min_samples) is not int or min_samples < 1:
        raise ValueError("pool_gate_min_samples_invalid")
    max_score = pool_gate.get("max_score")
    if isinstance(max_score, bool) or not isinstance(max_score, (int, float)):
        raise ValueError("pool_gate_max_score_invalid")
    max_score = float(max_score)
    if not math.isfinite(max_score) or not (0.0 < max_score < 1.0):
        raise ValueError("pool_gate_max_score_invalid")
    tie_min = pool_gate.get("tie_ci_lower_min")
    if isinstance(tie_min, bool) or not isinstance(tie_min, (int, float)):
        raise ValueError("pool_gate_tie_ci_lower_min_invalid")
    tie_min = float(tie_min)
    if not math.isfinite(tie_min):
        raise ValueError("pool_gate_tie_ci_lower_min_invalid")
    return {
        "label": label,
        "min_samples": int(min_samples),
        "max_score": max_score,
        "tie_ci_lower_min": tie_min,
    }


def pool_gate_blockers(matchups, weakest_label: str, frozen_settings) -> tuple:
    """Apply the pool admission gate to complete-match outcomes.

    Structural mirror of the parent gate in
    ``strength_order.precommit_outcome_blockers``: over the matchup row for
    ``weakest_label``, the candidate must reach a primary match score strictly
    greater than ``max_score`` with at least ``min_samples`` complete
    70-hand matches.  An exact tie (score == max_score) is reclassified as
    "not a regression" only when the paired net-chip bootstrap 95% CI LOWER
    bound exceeds ``tie_ci_lower_min`` (same 2026-08-16 tightening as the
    parent gate).  A genuine loss is a hard block regardless of chips.

    Every threshold comes from the FROZEN plan settings (``frozen_settings``
    is the plan's ``settings`` dict; the gate spec is read from its
    ``pool_gate`` member, falling back to this module's code defaults only
    when the plan predates the spec) — a live env change never re-decides an
    in-flight generation.
    """

    from strength_order import (
        is_precommit_gate_matchup,
        summarize_match_outcomes,
    )

    gate_spec = (
        frozen_settings.get("pool_gate")
        if isinstance(frozen_settings, dict)
        else None
    )
    gate_spec = gate_spec if isinstance(gate_spec, dict) else {}
    label = str(weakest_label or "")
    min_samples = int(gate_spec.get("min_samples") or POOL_GATE_MIN_SAMPLES)
    max_score = float(gate_spec.get("max_score") or POOL_GATE_MAX_SCORE)
    tie_ci_lower_min = float(
        gate_spec.get("tie_ci_lower_min")
        if gate_spec.get("tie_ci_lower_min") is not None
        else POOL_GATE_TIE_CI_LOWER_MIN
    )

    blockers: list[dict] = []
    per_matchup: list[dict] = []
    matched = False
    for matchup in matchups or []:
        if not isinstance(matchup, dict):
            continue
        if not is_precommit_gate_matchup(matchup):
            continue
        opponent = str(matchup.get("opponent") or "")
        if opponent != label:
            continue
        matched = True
        summary = summarize_match_outcomes(
            matchup.get("wins", 0),
            matchup.get("losses", 0),
            matchup.get("draws", 0),
        )
        per_matchup.append({"opponent": opponent, **summary})
        if not (
            summary["samples"] >= min_samples
            and summary["primary_match_score"] <= max_score
        ):
            continue
        score = summary["primary_match_score"]
        is_exact_tie = abs(score - max_score) < 1e-9
        tie_ci_lower = None
        if is_exact_tie:
            tie_chips = [int(x) for x in (matchup.get("net_chips") or [])]
            if len(tie_chips) >= min_samples:
                from eval_stats import paired_bootstrap_ci

                tie_ci_lower, _tie_hi = paired_bootstrap_ci(tie_chips)
        tie_not_regression = (
            tie_ci_lower is not None and tie_ci_lower > tie_ci_lower_min
        )
        if tie_not_regression:
            continue
        ci_note = (
            f" net-chip CI lower={tie_ci_lower:.1f} <= "
            f"{tie_ci_lower_min:.1f}; tie still a regression."
            if is_exact_tie and tie_ci_lower is not None
            else (
                " exact tie but no net-chip samples to break it."
                if is_exact_tie
                else ""
            )
        )
        blockers.append({
            "reason": "did_not_beat_pool_weakest",
            "opponent": opponent,
            "details": (
                f"70-hand outcomes {summary['wins']}W-{summary['losses']}L-"
                f"{summary['draws']}D score={summary['primary_match_score']:.3f}; "
                f"pool gate requires score>{max_score:.2f} (candidate must "
                f"beat pool weakest) with n>={min_samples}."
                + ci_note
            ),
        })

    if not matched:
        blockers.append({
            "reason": "pool_gate_label_missing",
            "opponent": label,
            "details": (
                f"No complete-matchup row for the frozen pool-weakest label "
                f"{label!r}; the pool admission gate cannot be decided."
            ),
        })

    summary = {
        "label": label,
        "matched": matched,
        "per_matchup": per_matchup,
        "min_samples": min_samples,
        "max_score": max_score,
        "tie_ci_lower_min": tie_ci_lower_min,
        "gate_basis": "complete_70_hand_wld_plus_tie_chip_ci",
        "draw_score": 0.5,
    }
    return blockers, summary


def generate_deck_seed_nonce() -> str:
    """One-time 128-bit evaluation seed nonce (plan-freeze moment only)."""

    return secrets.token_hex(16)


def derive_deck_seed_base(nonce: str) -> int:
    """Deterministically derive the deck seed base from a nonce.

    sha256(nonce), first 6 bytes, big-endian unsigned integer (48 bits).
    ``validate_precommit_plan`` re-proves this exact formula against the
    frozen settings, which is why it must stay inside this identity-bound
    module.
    """

    text = str(nonce)
    if not text or len(text) != 32 or any(
        char not in "0123456789abcdef" for char in text
    ):
        raise ValueError("deck_seed_nonce_invalid")
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:6], "big")


def derive_bot_seed_root(nonce: str) -> int:
    """Derive the BOT-side RNG seed root — domain-separated from the deck.

    Red-team fix (2026-10-12, attack "seed predictability at runtime"): the
    bot process receives its own seed through POK_NATIVE_BOT_SEED, and the
    historical linear relation ``bot_seed == deck_seed + 1e9`` handed the
    evaluated candidate a value from which the deck seed was directly
    computable (environ read via the ``posix`` module reproduced every
    dealt card byte-exactly).  The nonce path therefore derives the bot-side
    root from a DOMAIN-SEPARATED hash of the same nonce: observing the bot's
    own seed (via env or via its seeded RNG stream) no longer reveals
    anything about the deck.  The explicit/diagnostic path keeps the
    historical linear relation for first-strict journal compatibility.
    """

    text = str(nonce)
    if not text or len(text) != 32 or any(
        char not in "0123456789abcdef" for char in text
    ):
        raise ValueError("deck_seed_nonce_invalid")
    digest = hashlib.sha256(
        text.encode("utf-8") + b":bot_seed_v1"
    ).digest()
    return int.from_bytes(digest[:6], "big")
