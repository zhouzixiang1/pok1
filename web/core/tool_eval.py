"""Pipeline tools: pre-commit evaluation and inline evaluation (battle-based)."""

import asyncio
import json
import logging
import math
import os
import threading
import time

from bot_namespace import bot_name as active_bot_name, parse_bot_version
from tool_runtime_guard import tool

from evolution_core import (
    get_bot_dir,
    get_active_bots,
)

from tool_helpers import (
    _json_tool_result, _get_ui,
    _matching_checkpoint, _record_gate, _gate_payload, _state_blocked,
    _quality_gate_ok, _review_gate_ok, _critic_gate_ok,
    _select_precommit_opponents, _resolve_version_args,
    _set_pipeline_status,
    _prepare_official_profile_refresh,
)
from evolution_infra import write_pipeline_checkpoint, MAX_PRECOMMIT_RETRIES
from system_log import log_system_event
from pipeline_schema import GateResult, ScoreCard
from workflow_profiles import get_workflow_profile
from failure_classification import (
    INFRA_BLOCKER_REASONS,
    classify_precommit_gate,
    is_infra_blocker,
)
from pipeline_intents import make_intent
from precommit_eval_contract import (
    PrecommitEvalContractError,
    build_evaluation_contract,
    create_precommit_plan,
    opponents_from_plan,
    plan_seed_round,
    rebuild_precommit_plan_for_seed_round,
    validate_evaluation_contract,
    validate_precommit_plan,
)
from strength_order import (
    PRECOMMIT_AGGREGATE_MIN_LOSS_MARGIN,
    PRECOMMIT_AGGREGATE_MIN_SAMPLES,
    PRECOMMIT_PARENT_MIN_SAMPLES,
    is_precommit_gate_matchup,
    is_strength_matchup,
)

try:
    from candidate_store import (
        append_candidate_event,
        candidate_observability_identity,
    )
except Exception:  # pragma: no cover
    append_candidate_event = None
    candidate_observability_identity = None

from logging_config import get_logger
log = get_logger("tool_eval")

# The official EXE certification / first-strict-control companion module
# (tool_eval_first_strict_scope) was removed in Phases 3-4. Its non-cert
# functions (precommit shutdown-token cluster, first-strict abandon helper,
# national precommit shape/sample-contract, and first-strict control
# execution-scope build/validate) are inlined below. The only cert-backed
# function (_request_official_precommit_status) is stubbed.
import tool_eval_failure_attribution as _fa  # noqa: E402,F401  (failure-attribution/infra-retry cluster)

_FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY = (
    "first_strict_control_execution_scope"
)
_FIRST_STRICT_CONTROL_BATCH_PROGRESS_KEY = (
    "first_strict_control_batch_progress"
)

# native match loop.  A timeout sets the captured Event permanently; the next
# cycle rotates to a new Event instead of clearing the old one, so starting a
# retry can never revive a detached attempt from the previous cycle.
_PRECOMMIT_SHUTDOWN_LOCK = threading.Lock()
_PRECOMMIT_SHUTDOWN = threading.Event()

# ──────────────────────────────────────────────
# Precommit eval tuning constants
# ──────────────────────────────────────────────
# Default n_games per opponent for precommit eval. 8 per opponent keeps the
# parent gate reachable: strength_order.PRECOMMIT_PARENT_MIN_SAMPLES=6
# requires >=6 samples vs THE parent (the 2026-08-16 audit raised the default
# from a structurally vacuous 4 for exactly this reason; 8 stays above the
# floor with margin). The exact-tie CI escape hatch (paired_bootstrap_ci
# upper>0) handles the 4W-4L case; 8 (down from 12) reduces precommit
# wall-clock ~33% to free CPU for LLM work (operator priority 2026-10-21).
# 16 is the hard ceiling.
PRECOMMIT_DEFAULT_N_GAMES = 8
PRECOMMIT_MIN_N_GAMES = 4
PRECOMMIT_MAX_N_GAMES = 16

# Pool-ratchet admission-leg sample depth (2026-10-16 calibration).  The
# pool gate's bare-majority threshold at the historical 12-leg admitted at
# W>=7/12; deepening ONLY the pool-weakest leg to 24 with the derived
# threshold (see _pool_gate_max_score_for_matches) moves the boundary to
# W>=14/24 (parity false-admission 0.2706).  The parent and v1 legs keep
# PRECOMMIT_DEFAULT_N_GAMES — the parent gate semantics are unchanged.
# Read ONCE at the plan-freeze moment and frozen into the plan (per-opponent
# ``matches`` + pool-gate ``max_score``); a live env flip never re-decides an
# in-flight generation.
PRECOMMIT_POOL_WEAKEST_N_GAMES_ENV = "POK_PRECOMMIT_POOL_WEAKEST_N_GAMES"
PRECOMMIT_POOL_WEAKEST_N_GAMES = 24
# The floor is the pool gate's own minimum sample depth
# (pool_ratchet_gate.POOL_GATE_MIN_SAMPLES = 6): below it the gate's blocker
# branch never engages and a 0W-nL pool leg would pass silently (red-team
# 2026-10-16 config hole). The plan-wide PRECOMMIT_MAX_N_GAMES=16 ceiling
# deliberately does NOT bound this leg — the pool gate carries its own cost
# ceiling above.
PRECOMMIT_POOL_WEAKEST_MIN_N_GAMES = 6
PRECOMMIT_POOL_WEAKEST_MAX_N_GAMES = 48


def precommit_pool_weakest_n_games() -> int:
    """Resolve the pool-weakest admission-leg match count (freeze moment)."""

    raw = str(os.environ.get(PRECOMMIT_POOL_WEAKEST_N_GAMES_ENV) or "").strip()
    if not raw:
        return PRECOMMIT_POOL_WEAKEST_N_GAMES
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if not (
        PRECOMMIT_POOL_WEAKEST_MIN_N_GAMES
        <= value
        <= PRECOMMIT_POOL_WEAKEST_MAX_N_GAMES
    ):
        raise ValueError(
            f"pool_weakest_n_games_invalid:{raw}:expected integer in "
            f"[{PRECOMMIT_POOL_WEAKEST_MIN_N_GAMES}, "
            f"{PRECOMMIT_POOL_WEAKEST_MAX_N_GAMES}]"
        )
    return value


# Parent-leg sample depth (2026-10-22 power calibration).  The parent gate is
# a strict-majority screen: at p=0.5 its per-sample false-admission is 0.387
# at n=12 (need >=7W) and 0.419 at n=24 (need >=13W), so it is deliberately
# NOT a 5% test.  What a deeper parent leg buys is a lower score variance
# (SD 0.144 -> 0.102) and a higher pass rate for a genuinely stronger
# candidate (a true 0.55 edge passes ~0.56 at n=12 vs ~0.61 at n=24), at the
# cost of 12 extra 70-hand matches (~24 min at the measured ~2 min/match).
# Unset = inherit the plan-wide count (historical behavior).  When set, the
# value is read ONCE at the plan-freeze moment and frozen as the parent
# opponent's ``matches``; the plan-wide parent gate thresholds are unchanged.
PRECOMMIT_PARENT_N_GAMES_ENV = "POK_PRECOMMIT_PARENT_N_GAMES"
# Must stay >= strength_order.PRECOMMIT_PARENT_MIN_SAMPLES (6): below it the
# parent gate's blocker branch never engages.
PRECOMMIT_PARENT_MIN_N_GAMES = 6
PRECOMMIT_PARENT_MAX_N_GAMES = 48


def precommit_parent_n_games() -> int | None:
    """Resolve the parent-leg match count override (freeze moment).

    ``None`` means inherit ``matches_per_opponent`` — the historical shape.
    """

    raw = str(os.environ.get(PRECOMMIT_PARENT_N_GAMES_ENV) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if not (
        PRECOMMIT_PARENT_MIN_N_GAMES
        <= value
        <= PRECOMMIT_PARENT_MAX_N_GAMES
    ):
        raise ValueError(
            f"parent_n_games_invalid:{raw}:expected integer in "
            f"[{PRECOMMIT_PARENT_MIN_N_GAMES}, "
            f"{PRECOMMIT_PARENT_MAX_N_GAMES}]"
        )
    return value


def _pool_gate_max_score_for_matches(matches: int) -> float:
    """Derive the pool-leg score threshold from its frozen sample count.

    Pass requires a primary match score STRICTLY greater than
    ``(n//2 + 1) / n`` over n complete matches, i.e. at least
    ``n//2 + 2`` decisive wins (W>=14 at n=24; parity false-admission
    P(W>=14 | p=0.5, n=24) = 0.2706).  An exact score at the threshold
    (13W-0D-11L at n=24) keeps the existing exact-tie net-chip CI rescue.
    """

    n = max(2, int(matches))
    return (n // 2 + 1) / n


# ──────────────────────────────────────────────
# Pre-registered independent-seed repair round
# ──────────────────────────────────────────────
# One generation gets at most TWO precommit samples with independent deck
# seeds: the original frozen plan (round 1) and, after a Workers repair round
# changed the candidate, exactly one rebuild under a fresh one-time nonce
# (round 2).  Gates, alpha and per-opponent sample depths are carried over
# verbatim; a pass is only ever the unchanged native precommit gate passing.
# There is no retry-until-pass: a third independent sample is refused, and
# infrastructure retries keep the SAME plan (a harness stall is not a policy
# result and must never buy a fresh sample).
PRECOMMIT_SEED_REPAIR_ROUNDS_MAX = 1
PRECOMMIT_SEED_ROUNDS_MAX = 1 + PRECOMMIT_SEED_REPAIR_ROUNDS_MAX
PRECOMMIT_ATTEMPT_HISTORY_KEY = "precommit_eval_attempts"
PRECOMMIT_ATTEMPT_HISTORY_MAX = 4


def _precommit_rework_seed_round(ckpt) -> int:
    """The seed round the checkpoint's current repair state requires."""

    rework_count = int((ckpt or {}).get("precommit_rework_count", 0) or 0)
    return max(0, rework_count) + 1


def _precommit_attempt_record(
    *,
    result: dict,
    precommit_plan: dict,
    precommit_attempt: int,
) -> dict:
    """Bound one attempt's durable record (complete scorecard included)."""

    settings = precommit_plan.get("settings") or {}
    blockers = []
    for blocker in result.get("blockers") or []:
        if not isinstance(blocker, dict):
            continue
        blockers.append({
            key: blocker.get(key)
            for key in ("reason", "opponent", "details")
            if blocker.get(key) is not None
        })
    return {
        "schema_version": 1,
        "attempt": int(precommit_attempt),
        "seed_round": int(plan_seed_round(precommit_plan)),
        "plan_digest": str(precommit_plan.get("plan_digest") or ""),
        "deck_seed_base": settings.get("deck_seed_base"),
        "matches_by_opponent": dict(result.get("matches_by_opponent") or {}),
        "total_wins": int(result.get("total_wins") or 0),
        "total_losses": int(result.get("total_losses") or 0),
        "total_draws": int(result.get("total_draws") or 0),
        "passed": bool(result.get("passed")),
        "failure_class": str(result.get("failure_class") or ""),
        "blockers": blockers[:12],
        "scorecard": result.get("scorecard"),
        "recorded_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        ),
    }


def _record_precommit_attempt_history(
    v: int,
    source_v: int,
    *,
    result: dict,
    precommit_plan: dict,
    precommit_attempt: int,
) -> bool:
    """Persist the attempt's complete scorecard into the checkpoint history.

    Both independent samples must stay inspectable after the later stage
    transition prunes ``gate_results.precommit_eval``.  The append-only
    candidate ledger also keeps every precommit row; this checkpoint copy is
    the content-bound evaluation record the repair rule is re-proved from.
    """

    checkpoint = _matching_checkpoint(v, source_v)
    if not isinstance(checkpoint, dict):
        return False
    context = checkpoint.get("audit_context") or {}
    records = [
        item
        for item in (context.get(PRECOMMIT_ATTEMPT_HISTORY_KEY) or [])
        if isinstance(item, dict)
    ]
    record = _precommit_attempt_record(
        result=result,
        precommit_plan=precommit_plan,
        precommit_attempt=precommit_attempt,
    )
    records = [
        item
        for item in records
        if item.get("seed_round") != record["seed_round"]
    ]
    records.append(record)
    return bool(
        write_pipeline_checkpoint(
            v,
            source_v,
            checkpoint.get("stage"),
            audit_context={
                PRECOMMIT_ATTEMPT_HISTORY_KEY: (
                    records[-PRECOMMIT_ATTEMPT_HISTORY_MAX:]
                ),
            },
            expected_checkpoint_revision=checkpoint.get(
                "checkpoint_revision"
            ),
            expected_checkpoint_stage=checkpoint.get("stage"),
            expected_workflow_run_id=checkpoint.get("workflow_run_id"),
        )
    )


def _rotate_precommit_plan_for_repair_round(
    *,
    v: int,
    source_v: int,
    checkpoint: dict,
    stored_plan: dict,
    workflow_profile,
) -> tuple[dict | None, dict | None]:
    """Return ``(rotated_plan, None)`` or ``(None, error_payload)``.

    Rotation happens exactly once per Workers repair round: the checkpoint's
    ``precommit_rework_count`` is the pre-registered trigger, and the rebuilt
    plan carries the same opponents, per-opponent sample depths, evaluator
    identity and gate thresholds with a fresh independent nonce.
    """

    try:
        current_round = plan_seed_round(stored_plan)
    except PrecommitEvalContractError as exc:
        return None, {
            "error": f"PRECOMMIT PLAN SEED ROUND INVALID: {exc}",
            "version": v,
            "source_v": source_v,
            "passed": False,
            "failure_class": "control_plane",
            "intent": make_intent(
                "pause",
                failure_class="control_plane",
                authority="tool:precommit_eval",
                safe_to_auto_execute=False,
                reason="precommit_seed_round_invalid",
            ),
        }
    target_round = _precommit_rework_seed_round(checkpoint)
    if target_round <= current_round:
        return None, None
    settings = stored_plan.get("settings") or {}
    if any(
        str(item.get("authority") or "") == "system_first_strict_control"
        for item in (stored_plan.get("opponents") or [])
        if isinstance(item, dict)
    ):
        # The empty-pool system-control plan carries no strength gate and is
        # governed by its own execution journal; it is never re-sampled.
        return None, None
    if target_round > PRECOMMIT_SEED_ROUNDS_MAX:
        return None, {
            "error": (
                "PRECOMMIT INDEPENDENT REPAIR ROUND EXHAUSTED: one generation "
                "gets at most one fresh-seed repair sample "
                f"(seed rounds {current_round}/{PRECOMMIT_SEED_ROUNDS_MAX}, "
                f"rework_count={int(checkpoint.get('precommit_rework_count') or 0)})."
            ),
            "version": v,
            "source_v": source_v,
            "passed": False,
            "failure_class": "candidate",
            "blockers": [{
                "reason": "precommit_repair_round_exhausted",
                "details": (
                    "The pre-registered decision rule allows the original "
                    "sample plus exactly one independent-seed repair sample; "
                    "no retry-until-pass is permitted."
                ),
            }],
            "intent": make_intent(
                "abandon",
                next_tool=None,
                failure_class="candidate",
                authority="tool:precommit_eval",
                safe_to_auto_execute=True,
                reason="precommit_repair_round_exhausted",
            ),
        }
    try:
        rotated = rebuild_precommit_plan_for_seed_round(
            stored_plan,
            seed_round=target_round,
            path_resolver=lambda item: (
                item.get("path")
                or get_bot_dir(parse_bot_version(item["name"]))
            ),
        )
    except Exception as exc:
        return None, {
            "error": (
                "PRECOMMIT SEED ROTATION FAILED: "
                f"{type(exc).__name__}: {str(exc)[:400]}"
            ),
            "version": v,
            "source_v": source_v,
            "passed": False,
            "failure_class": "control_plane",
            "intent": make_intent(
                "pause",
                failure_class="control_plane",
                authority="tool:precommit_eval",
                safe_to_auto_execute=False,
                reason="precommit_seed_rotation_failed",
            ),
        }
    if rotated.get("plan_digest") == stored_plan.get("plan_digest"):
        return None, {
            "error": (
                "PRECOMMIT SEED ROTATION DID NOT CHANGE THE PLAN: the repair "
                "sample would replay the failed sample plan."
            ),
            "version": v,
            "source_v": source_v,
            "passed": False,
            "failure_class": "control_plane",
            "intent": make_intent(
                "pause",
                failure_class="control_plane",
                authority="tool:precommit_eval",
                safe_to_auto_execute=False,
                reason="precommit_seed_rotation_noop",
            ),
        }
    context = checkpoint.get("audit_context") or {}
    prior_records = [
        item
        for item in (context.get(PRECOMMIT_ATTEMPT_HISTORY_KEY) or [])
        if isinstance(item, dict)
    ]
    # Best-effort backfill for a first attempt that ran before the history
    # writer existed (or whose write was interrupted) while its gate receipt
    # is still on the checkpoint.  The later rework stage prunes that receipt,
    # so this is the only remaining moment to keep its scorecard bound.
    if not any(
        int(item.get("seed_round") or 0) == current_round
        for item in prior_records
    ):
        prior_gate = ((checkpoint.get("gate_results") or {}).get(
            "precommit_eval"
        ))
        if isinstance(prior_gate, dict):
            try:
                prior_records.append(
                    _precommit_attempt_record(
                        result=prior_gate,
                        precommit_plan=stored_plan,
                        precommit_attempt=int(
                            checkpoint.get("precommit_attempt") or 0
                        ),
                    )
                )
            except Exception:
                pass
    persisted = write_pipeline_checkpoint(
        v,
        source_v,
        checkpoint.get("stage"),
        audit_context={
            "precommit_eval_plan": rotated,
            PRECOMMIT_ATTEMPT_HISTORY_KEY: (
                prior_records[-PRECOMMIT_ATTEMPT_HISTORY_MAX:]
            ),
        },
        expected_checkpoint_revision=checkpoint.get("checkpoint_revision"),
        expected_checkpoint_stage=checkpoint.get("stage"),
        expected_workflow_run_id=checkpoint.get("workflow_run_id"),
    )
    if not persisted:
        return None, {
            "error": (
                "STATE BLOCKED: failed to persist the independent-seed "
                "repair plan for the precommit repair round."
            ),
            "version": v,
            "source_v": source_v,
            "passed": False,
            "checkpoint_stage": checkpoint.get("stage"),
            "failure_class": "control_plane",
            "intent": make_intent(
                "pause",
                failure_class="control_plane",
                authority="tool:precommit_eval",
                safe_to_auto_execute=False,
                reason="precommit_seed_rotation_persist_failed",
            ),
        }
    try:
        log_system_event(
            "pipeline.precommit_seed_rotated",
            "info",
            (
                f"Precommit deck seed rotated for v{v}: round "
                f"{current_round} -> {target_round} "
                f"(deck_seed_base={(rotated.get('settings') or {}).get('deck_seed_base')})"
            ),
            {
                "version": v,
                "source_v": source_v,
                "precommit_rework_count": int(
                    checkpoint.get("precommit_rework_count") or 0
                ),
                "from_seed_round": current_round,
                "to_seed_round": target_round,
                "prior_plan_digest": stored_plan.get("plan_digest"),
                "plan_digest": rotated.get("plan_digest"),
                "seed_derivation": (rotated.get("settings") or {}).get(
                    "seed_derivation"
                ),
                "deck_seed_base": (rotated.get("settings") or {}).get(
                    "deck_seed_base"
                ),
                "deck_seed_nonce": (rotated.get("settings") or {}).get(
                    "deck_seed_nonce"
                ),
                "seed_rounds_max": PRECOMMIT_SEED_ROUNDS_MAX,
            },
        )
    except Exception:
        pass
    return rotated, None


# ──────────────────────────────────────────────
# Ambiguous failure: pre-register the fresh-seed sample instead of repairing
# ──────────────────────────────────────────────
# The frozen plan already pre-registers two independent deck seeds per
# generation (round 1 + the single fresh-seed sample purchased by
# ``precommit_rework_count``).  A non-decisive failure carries almost no
# information about the candidate, so the one available second sample is
# strictly more informative than an unguided Worker rework — the 2026-10-27
# two-round evidence (6 samples, 4 repairs worse, 1 unchanged, 1 better;
# mean primary score 0.475 -> 0.443) shows the repair path walks a candidate
# away from its parent on noise.  A decisive regression keeps the historical
# in-place repair.  Either way the generation still gets at most two
# independent samples: this path consumes the same ``precommit_rework_count``
# unit the repair path consumes, so a second failure follows the ordinary
# repair/circuit-breaker route.
def _precommit_aggregate_evidence(result) -> tuple[int, int, float | None, float | None]:
    """Aggregate W/L and the paired net-chip CI from a precommit gate result.

    Returns ``(wins, losses, ci_lower, ci_upper)``.  Tolerates the three shapes
    the same evidence takes in this codebase: the live gate result
    (``total_wins``/``total_losses`` + ``paired_bootstrap``), the persisted
    round report (``aggregate``), and a bare matchup list.
    """

    def _int(value) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _float(value) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    if isinstance(result, (list, tuple)):
        wins = losses = 0
        for matchup in result:
            if not isinstance(matchup, dict) or not is_precommit_gate_matchup(matchup):
                continue
            wins += _int(matchup.get("wins"))
            losses += _int(matchup.get("losses"))
        return wins, losses, None, None
    if not isinstance(result, dict):
        return 0, 0, None, None

    aggregate = result.get("aggregate")
    if isinstance(aggregate, dict):
        wins = _int(aggregate.get("wins"))
        losses = _int(aggregate.get("losses"))
        ci = aggregate.get("net_chips_ci") or []
        if isinstance(ci, (list, tuple)) and len(ci) == 2:
            return wins, losses, _float(ci[0]), _float(ci[1])
        return wins, losses, None, None

    wins = _int(result.get("total_wins"))
    losses = _int(result.get("total_losses"))
    if "total_wins" not in result:
        wins = losses = 0
        for matchup in result.get("matchups") or []:
            if not isinstance(matchup, dict) or not is_precommit_gate_matchup(matchup):
                continue
            wins += _int(matchup.get("wins"))
            losses += _int(matchup.get("losses"))
    bootstrap = result.get("paired_bootstrap") or {}
    if isinstance(bootstrap, dict):
        return (
            wins,
            losses,
            _float(bootstrap.get("aggregate_ci_lower")),
            _float(bootstrap.get("aggregate_ci_upper")),
        )
    return wins, losses, None, None


def _binomial_tail_p(losses: int, decided: int) -> float:
    """Exact one-sided P(wins <= decided - losses) for a fair coin."""

    if decided <= 0:
        return 1.0
    wins = max(0, decided - losses)
    if wins >= decided:
        return 1.0
    return sum(math.comb(decided, k) for k in range(0, wins + 1)) / float(
        2 ** decided
    )


def _pool_leg_tail_p(result) -> "float | None":
    """One-sided binomial tail for the pool-weakest admission leg, or None.

    Finds the ``did_not_beat_pool_weakest`` blocker's opponent, reads that
    matchup's W/L and returns P(losses >= observed | p=0.5, n=decided) — i.e.
    how far below 50% the candidate sits against the pool's OWN weakest
    member. The aggregate tail dilutes this signal with parent/bootstrap
    legs, so a candidate measurably worse than the weakest can look ambiguous
    in aggregate while the admission leg is already a regression the
    fresh-seed round cannot fix (2026-10-28 audit: the pool leg drifted WORSE
    after resample in v630 12W→8W tail 0.0758 and v634 10W→8W tail 0.0758,
    each costing a full 48-match native round).

    Calibrated numbers (2026-10-29 second-round audit, deployed function
    recomputation — an earlier draft of this docstring misstated the 10W-14L
    tail as 0.09; the true value at n=24 is P(W<=10)=0.2706, and the 0.10
    decisive threshold therefore only catches round-1 legs of 8W-16L or
    worse; whether to tighten toward 0.2-0.3 is gated on forward v638+
    samples because rescue-ability is unobservable at n=5):
      pool leg at r1: v630 12W-12L 0.5806 / v631 9W-15L 0.1537 /
                      v634 10W-14L 0.2706 / v635 13W-11L 0.7294 /
                      v637 10W-14L 0.2706
    """
    if not isinstance(result, dict):
        return None
    label = None
    for blocker in result.get("blockers") or []:
        if (
            isinstance(blocker, dict)
            and blocker.get("reason") == "did_not_beat_pool_weakest"
        ):
            label = str(blocker.get("opponent") or "") or None
            break
    if label is None:
        return None
    for matchup in result.get("matchups") or []:
        if not isinstance(matchup, dict) or str(matchup.get("opponent")) != label:
            continue
        wins = int(matchup.get("wins") or 0)
        losses = int(matchup.get("losses") or 0)
        decided = wins + losses
        if decided < 8:
            return None
        return _binomial_tail_p(losses, decided)
    return None


def _precommit_failure_is_decisive(result) -> bool:
    """Whether the failed sample proves a regression the repair must address.

    Decisive means one of the gate's independent evidence channels shows a
    regression that noise cannot explain; all are computed by the gate
    itself, so no new judgement is introduced here:

    * the aggregate paired net-chip bootstrap CI lies entirely below zero, or
    * the aggregate W/L record is a one-sided exact binomial outlier
      (``p < 0.05``), or
    * the pool-weakest admission leg is itself a binomial outlier
      (``p < 0.10``, 2026-10-28 audit; deliberately looser than the aggregate
      because the leg alone gates admission — a candidate that loses to the
      pool's weakest by a significant margin will lose that leg under any
      deck seed, so the F1 fresh-seed round is guaranteed native-time waste).

    A deficit that is merely a strict-inequality loss (the 20W/20L sample
    that consumed a whole 92-minute round) is *not* decisive: it is the
    definition of an ambiguous sample, and it gets the fresh-seed second
    sample instead of an unguided repair.
    """

    wins, losses, ci_lower, ci_upper = _precommit_aggregate_evidence(result)
    decided = wins + losses
    if ci_upper is not None and ci_lower is not None:
        if ci_upper < 0.0:
            return True
    if decided >= PRECOMMIT_AGGREGATE_MIN_SAMPLES:
        if _binomial_tail_p(losses, decided) < 0.05:
            return True
    pool_tail = _pool_leg_tail_p(result)
    if pool_tail is not None and pool_tail < 0.10:
        return True
    return False


def _precommit_seed_resample_preferred(
    *,
    result: dict,
    precommit_plan: dict | None,
    checkpoint: dict | None,
    gate_failure_class: str,
) -> bool:
    """True when an ambiguous round-1 failure should buy the fresh-seed sample."""

    if not isinstance(result, dict) or result.get("passed") is not False:
        return False
    if gate_failure_class not in {"regression", "failed_unknown"}:
        # ``infra_timeout`` (including the exhausted-attempt rewrite to
        # ``regression``) is a harness failure, never a policy result: it must
        # keep its own retry path and must not consume the rework unit.
        return False
    ckpt = checkpoint if isinstance(checkpoint, dict) else {}
    if int(ckpt.get("precommit_rework_count", 0) or 0) != 0:
        # The single rework unit is already spent (either by the fresh-seed
        # resample or by an in-place repair): no third sample exists.
        return False
    if not isinstance(precommit_plan, dict):
        return False
    try:
        if plan_seed_round(precommit_plan) != 1:
            return False
    except PrecommitEvalContractError:
        return False
    return not _precommit_failure_is_decisive(result)


def _pre_register_precommit_seed_resample(
    *, v: int, source_v: int, prior_rework_count: int
) -> bool:
    """Restore the unchanged candidate to ``critic_checked`` for seed round 2.

    One exact-CAS checkpoint write consumes the single rework counter unit
    (``precommit_rework_count`` -> 1).  The next ``run_precommit_eval`` then
    rebuilds the frozen plan under seed round 2
    (``_rotate_precommit_plan_for_repair_round``), and because the write's
    stage gate allowlist prunes ``gate_results.precommit_eval``,
    ``route_policy`` routes straight back to ``run_precommit_eval`` instead of
    ``execute_workers``.  Candidate bytes, opponents, per-opponent sample
    depths, gate thresholds and the evaluation contract are untouched.
    """

    checkpoint = _matching_checkpoint(v, source_v)
    if (
        not isinstance(checkpoint, dict)
        or str(checkpoint.get("stage") or "") != "precommit_failed"
    ):
        return False
    feedback = (
        "Precommit round 1 failed without a decisive regression. The candidate "
        "bytes are UNCHANGED: do NOT rework. The checkpoint was restored to "
        "critic_checked and the single independent-seed sample round 2 was "
        "pre-registered (precommit_rework_count=1). Call run_precommit_eval to "
        "re-sample the unchanged candidate under the same frozen opponents, "
        "sample depths, gate thresholds and evaluation contract with a fresh "
        "deck seed. A second failure abandons this generation; no third sample "
        "exists."
    )
    persisted = write_pipeline_checkpoint(
        v,
        source_v,
        "critic_checked",
        reviewer_feedback=feedback,
        precommit_rework_count=int(prior_rework_count) + 1,
        expected_checkpoint_revision=checkpoint.get("checkpoint_revision"),
        expected_checkpoint_stage="precommit_failed",
        expected_workflow_run_id=checkpoint.get("workflow_run_id"),
    )
    if not persisted:
        return False
    try:
        log_system_event(
            "pipeline.precommit_seed_resample_instead_of_repair",
            "info",
            (
                f"Precommit round 1 failed ambiguously for v{v}; skipping the "
                "in-place Worker repair and pre-registering the single "
                "independent-seed resample (round 2)."
            ),
            {
                "version": v,
                "source_v": source_v,
                "prior_precommit_rework_count": int(prior_rework_count),
                "precommit_rework_count": int(prior_rework_count) + 1,
                "restored_stage": "critic_checked",
                "expected_checkpoint_stage": "precommit_failed",
                "seed_round": 2,
                "seed_rounds_max": PRECOMMIT_SEED_ROUNDS_MAX,
            },
        )
    except Exception:
        pass
    return True


def _apply_precommit_seed_resample_instead_of_repair(
    *,
    v: int,
    source_v: int,
    result: dict,
    precommit_plan: dict | None,
    checkpoint: dict | None,
    gate_failure_class: str,
) -> bool:
    """Restore ``critic_checked`` and rewrite the failure payload for round 2.

    Must run only after the failed gate and its durable attempt record are both
    on disk.  A failed restore write is side-effect free and keeps the
    historical ``precommit_failed -> execute_workers`` repair route.
    """

    if not _precommit_seed_resample_preferred(
        result=result,
        precommit_plan=precommit_plan,
        checkpoint=checkpoint,
        gate_failure_class=gate_failure_class,
    ):
        return False
    prior_rework_count = int(
        (checkpoint or {}).get("precommit_rework_count", 0) or 0
    )
    if not _pre_register_precommit_seed_resample(
        v=v,
        source_v=source_v,
        prior_rework_count=prior_rework_count,
    ):
        return False
    result["precommit_seed_resample_pre_registered"] = True
    result["precommit_rework_count"] = prior_rework_count + 1
    result["precommit_resample_seed_round"] = 2
    result["directive"] = (
        f"National precommit round 1 FAILED without a decisive regression "
        f"(seed round 1/{PRECOMMIT_SEED_ROUNDS_MAX}). The candidate bytes are "
        "UNCHANGED and the checkpoint is restored to critic_checked with the "
        "single independent-seed sample round 2 pre-registered "
        "(precommit_rework_count=1). Do NOT call execute_workers and do NOT "
        "rework the bot. Call run_precommit_eval now: it re-samples the "
        "unchanged candidate under the same frozen opponents, sample depths, "
        "gate thresholds and evaluation contract with a fresh deck seed. A "
        "second failure abandons this generation; no third sample exists."
    )
    result["intent"] = make_intent(
        "retry_same_tool",
        next_tool="run_precommit_eval",
        failure_class="regression",
        authority="tool:precommit_eval",
        safe_to_auto_execute=True,
        reason="precommit_seed_resample_pre_registered",
    )
    return True


def set_precommit_shutdown(token: threading.Event | None = None):
    """Permanently cancel every precommit holding the current attempt token.

    Called by the orchestrator's CYCLE_TIMEOUT / CancelledError handler so
    subprocess-spawning loops stop before starting another complete match.
    Idempotent; ``reset_precommit_shutdown`` rotates rather than clears it.
    """
    if token is None:
        with _PRECOMMIT_SHUTDOWN_LOCK:
            token = _PRECOMMIT_SHUTDOWN
    token.set()


def reset_precommit_shutdown():
    """Rotate a cancelled token; never detach a still-live current attempt."""

    global _PRECOMMIT_SHUTDOWN
    with _PRECOMMIT_SHUTDOWN_LOCK:
        if _PRECOMMIT_SHUTDOWN.is_set():
            _PRECOMMIT_SHUTDOWN = threading.Event()


def current_precommit_shutdown_token() -> threading.Event:
    """Return the immutable-by-convention cancellation token for one attempt."""

    with _PRECOMMIT_SHUTDOWN_LOCK:
        return _PRECOMMIT_SHUTDOWN


def begin_precommit_shutdown_attempt() -> threading.Event:
    """Claim a live token, rotating only after the prior attempt was cancelled.

    Deterministic ``infra_timed_out`` recovery can call ``run_precommit_eval``
    before another provider cycle reaches the orchestrator's reset hook.  The
    handler therefore performs this atomic cancelled-to-fresh handoff itself.
    Concurrent calls while the token is live share it, so one timeout still
    fences every duplicate attempt owned by that cycle.
    """

    global _PRECOMMIT_SHUTDOWN
    with _PRECOMMIT_SHUTDOWN_LOCK:
        if _PRECOMMIT_SHUTDOWN.is_set():
            _PRECOMMIT_SHUTDOWN = threading.Event()
        return _PRECOMMIT_SHUTDOWN


async def _abandon_first_strict_generation(payload: dict, *, reason: str):
    """Fence and remove a rejected deterministic first-migration candidate.

    Returning an ``action`` string is not an execution boundary.  This helper
    invokes the existing actor-fenced abandon implementation before the tool
    returns, while retaining a deterministic pause/abandon route if cleanup is
    temporarily rate-limited or otherwise cannot complete.
    """

    try:
        from system_strict_bootstrap import abandon_rejected_blueprint

        checkpoint = _matching_checkpoint(
            payload.get("version"),
            payload.get("source_v"),
        )
        result = await abandon_rejected_blueprint(
            checkpoint,
            reason=str(reason),
            result=dict(payload),
        )
    except Exception as exc:
        result = {
            **dict(payload),
            "action": "abandon_generation",
            "abandoned": False,
            "abandon_error": (
                f"abandon_exception:{type(exc).__name__}:{str(exc)[:300]}"
            ),
        }
    result["abandon_reason"] = str(reason)
    result["intent"] = make_intent(
        "abandoned" if result["abandoned"] else "abandon",
        next_tool=(
            None if result["abandoned"] else "abandon_generation"
        ),
        failure_class=str(result.get("failure_class") or "control_plane"),
        authority="tool:precommit_eval",
        safe_to_auto_execute=not result["abandoned"],
        reason=str(reason),
    )
    return _json_tool_result(result)


def is_precommit_shutdown() -> bool:
    """True if the current precommit attempt has been signalled to abort."""

    return current_precommit_shutdown_token().is_set()



def _official_bot_token(value) -> str:
    path = Path(value)
    if path.name == "national_bot.py":
        return str(path.parent)
    return str(path)


def _request_official_precommit_status(
    *,
    candidate,
    self_play_rounds: int,
    opponent_rounds: int,
    target_hands: int,
) -> dict:
    """Stubbed: official compliance precommit queue was backed by the
    removed official_certification system (Phases 3-4). Compliance is now
    always reported as non-blocking/inconclusive so precommit never stalls.
    """
    return {
        "status": "inconclusive",
        "mode": "compliance",
        "passed": False,
        "blocking": False,
        "inconclusive": True,
        "classification": "inconclusive",
        "issues": ["official_precommit_system_removed"],
    }



def _national_sample_contract_blockers(
    paired_bootstrap: dict,
    *,
    expected_samples: int,
) -> list[dict]:
    sample_count = int(paired_bootstrap.get("net_chips_samples", 0) or 0)
    blockers: list[dict] = []
    if int(paired_bootstrap.get("hands_per_match", 0) or 0) != 70:
        blockers.append({
            "reason": "national_strength_hands_not_70",
            "details": "Every production national strength sample must be one complete 70-hand match.",
        })
    if sample_count > 0 and sample_count < expected_samples:
        blockers.append({
            "reason": "national_sample_shortfall",
            "details": (
                f"National precommit completed {sample_count}/{expected_samples} "
                "required full-match samples."
            ),
        })
    return blockers


def _national_precommit_shape(workflow_profile, sample_target: int) -> tuple[int, int]:
    if getattr(workflow_profile, "national_execution_mode", None) != "native_tcp":
        raise RuntimeError("precommit supports only national native_tcp evaluation")
    hands = 70
    configured_matches = int(os.environ.get(
        "POK_NATIONAL_PRECOMMIT_MATCHES",
        str(getattr(workflow_profile, "national_precommit_matches", 1)),
    ))
    return (
        max(1, min(70, hands)),
        max(
            2,
            configured_matches,
            min(PRECOMMIT_MAX_N_GAMES, max(PRECOMMIT_MIN_N_GAMES, sample_target)),
        ),
    )


def _observed_native_sample_plan(result: dict) -> list[dict]:
    rows: list[dict] = []
    for opponent_index, matchup in enumerate(result.get("matchups") or []):
        for repeat in matchup.get("repeats") or []:
            rows.append({
                "opponent": str(matchup.get("opponent") or ""),
                "opponent_index": opponent_index,
                "repeat": int(repeat.get("repeat") or 0),
                "deck_seed_base": repeat.get("deck_seed_base"),
                "bot_seed_base": repeat.get("bot_seed_base"),
                "native_match_timing_plan_digest": (
                    ((repeat.get("local_runtime_budget") or {}).get(
                        "timing_plan_digest"
                    ))
                ),
            })
    return rows


def _build_first_strict_control_execution_scope(
    *,
    v: int,
    candidate_name: str,
    code_fingerprint: str,
    opponents: list,
    precommit_plan: dict,
    evaluation_contract: dict,
    workflow_run_id: str,
    checkpoint_revision: int,
    precommit_attempt: int,
) -> dict:
    """Build the immutable identity for one first-strict match journal."""

    control = next(
        (
            item
            for item in opponents
            if str(item.get("authority") or "")
            == "system_first_strict_control"
        ),
        {},
    )
    control_receipt = control.get("control_receipt") or {}
    return {
        "workflow_run_id": str(workflow_run_id),
        "checkpoint_revision": int(checkpoint_revision),
        "candidate_version": int(v),
        "candidate_label": str(candidate_name),
        "candidate_artifact_hash": str(code_fingerprint),
        "control_id": str(control.get("name") or ""),
        "control_artifact_hash": str(
            ((control_receipt.get("control") or {}).get("artifact_hash"))
            or ""
        ),
        "control_receipt_digest": str(
            control_receipt.get("receipt_digest") or ""
        ),
        "precommit_plan_digest": str(
            precommit_plan.get("plan_digest") or ""
        ),
        "evaluation_contract_digest": str(
            evaluation_contract.get("contract_digest") or ""
        ),
        "native_match_timing_plan_digest": str(
            ((precommit_plan.get("settings") or {}).get(
                "native_match_timing_plan_digest"
            ))
            or ""
        ),
        "precommit_attempt": int(precommit_attempt),
    }


def _validate_first_strict_control_execution_scope(
    scope,
    *,
    v: int,
    candidate_name: str,
    code_fingerprint: str,
    opponents: list,
    precommit_plan: dict,
    evaluation_contract: dict,
    workflow_run_id: str,
    precommit_attempt: int,
) -> tuple[dict | None, str | None]:
    """Re-prove a frozen scope while retaining its attempt-origin revision."""

    try:
        from first_strict_execution_journal import normalize_execution_scope

        normalized = normalize_execution_scope(scope)
    except Exception as exc:
        return None, (
            "First-strict precommit execution scope is missing or invalid: "
            f"{type(exc).__name__}: {str(exc)[:240]}"
        )
    expected = _build_first_strict_control_execution_scope(
        v=v,
        candidate_name=candidate_name,
        code_fingerprint=code_fingerprint,
        opponents=opponents,
        precommit_plan=precommit_plan,
        evaluation_contract=evaluation_contract,
        workflow_run_id=workflow_run_id,
        checkpoint_revision=int(normalized["checkpoint_revision"]),
        precommit_attempt=precommit_attempt,
    )
    if normalized != expected:
        return None, (
            "First-strict precommit execution scope no longer binds the exact "
            "workflow, artifact, plan, control, and logical attempt."
        )
    return normalized, None


def _first_strict_live_lease_pending_result(
    *,
    v: int,
    source_v: int,
    candidate_name: str,
    precommit_attempt: int,
    control_execution_scope: dict,
    pending_execution: object,
    batch_progress: dict | None = None,
    batch_checkpoint_recorded: bool = False,
) -> dict:
    """Return a non-terminal recovery response for one active journal lease.

    A first-strict control match is physical evidence, not a pure function that
    may be retried in parallel.  The durable journal has already bound this
    scope to the quality/reviewer/critic chain.  While its matching effect is
    live, retain that chain and ask the operator/orchestrator to retry the
    exact precommit call only after the recorded lease ends or the original
    owner has written its completion receipt.
    """

    from first_strict_execution_journal import (
        FirstStrictExecutionJournalError,
        normalize_pending_control_execution,
        read_pending_control_execution,
    )

    normalized_scope = control_execution_scope
    try:
        pending = read_pending_control_execution(
            pending_execution,
            expected_scope=normalized_scope,
        )
        retryable_now = False
        pending_validation = "live_lease"
    except FirstStrictExecutionJournalError as exc:
        # A lease which expires between the native layer's observation and this
        # projection is safe to reclaim on the next exact invocation.  It is
        # still not a candidate/gate failure and must never enter abandon.
        if str(exc) != "first_strict_execution_pending_lease_not_live":
            raise RuntimeError(
                "first_strict_control_pending_journal_invalid:"
                f"{type(exc).__name__}:{str(exc)}"
            ) from exc
        pending = normalize_pending_control_execution(
            pending_execution,
            expected_scope=normalized_scope,
        )
        retryable_now = True
        pending_validation = "lease_expired_before_projection"

    checkpoint = _matching_checkpoint(v, source_v) or {}
    return _json_tool_result({
        "version": int(v),
        "source_v": int(source_v),
        "candidate": str(candidate_name),
        "passed": False,
        "pending": True,
        "failure_class": "infrastructure_pending",
        "checkpoint_recorded": False,
        "checkpoint_stage": checkpoint.get("stage"),
        "precommit_attempt": int(precommit_attempt),
        # The complete scope includes the control receipt digest and the
        # checkpoint revision which fenced the critic-approved artifact.
        "control_execution_scope": normalized_scope,
        "control_receipt_digest": normalized_scope.get(
            "control_receipt_digest"
        ),
        "preserved_gate_evidence": ["quality", "review", "critic"],
        "control_execution_pending": pending,
        "retry_not_before_epoch_s": (
            None if retryable_now else pending["lease_until"]
        ),
        "retryable_now": retryable_now,
        "directive": (
            "First-strict native precommit is already owned by a matching "
            "live journal lease. Preserve the frozen scope, control receipt, "
            "and quality/reviewer/critic evidence; do not abandon, rework, or "
            "launch a parallel match. Retry the exact precommit only after "
            "the lease expires or the durable receipt is recovered."
        ),
        "intent": make_intent(
            "wait",
            next_tool="run_precommit_eval",
            failure_class="infrastructure_pending",
            authority="tool:precommit_eval",
            safe_to_auto_execute=False,
            reason=(
                "first_strict_execution_lease_expired_retry"
                if retryable_now
                else "first_strict_execution_lease_active"
            ),
        ),
        "pending_validation": pending_validation,
        "first_strict_batch_pending": batch_progress,
        "batch_checkpoint_recorded": bool(batch_checkpoint_recorded),
    })


def _control_execution_pending_from_native_result(
    national_result: object,
    *,
    control_execution_scope: dict | None,
) -> dict | None:
    """Return only the explicit live-lease signal; generic pending is unsafe.

    Batch continuation and other infrastructure paths may expose their own
    pending state.  This narrow adapter intentionally recognizes neither a
    generic ``pending`` flag nor arbitrary blocker text, because treating
    either as a live first-strict lease could suppress a genuine gate failure.
    """

    if not isinstance(national_result, dict):
        return None
    pending = national_result.get("control_execution_pending")
    if pending is None:
        return None
    if control_execution_scope is None:
        raise RuntimeError(
            "first_strict_control_pending_without_execution_scope"
        )
    from first_strict_execution_journal import normalize_pending_control_execution

    return normalize_pending_control_execution(
        pending,
        expected_scope=control_execution_scope,
    )


def _validate_first_strict_batch_progress(
    progress: object,
    *,
    precommit_plan: dict,
    control_execution_scope: dict,
) -> list[str]:
    """Verify the checkpoint projection against plan and journal authority.

    ``national_native`` produces this projection, but a control-plane checkpoint
    must not trust a returned dictionary merely because it came from an in
    process call.  Each completed reference is re-read from the fenced journal
    before it becomes durable checkpoint state.
    """

    if not isinstance(progress, dict):
        return ["first_strict_batch_progress_missing"]
    settings = precommit_plan.get("settings") or {}
    expected_batch = settings.get("native_precommit_batch_plan")
    if not isinstance(expected_batch, dict):
        return ["first_strict_batch_plan_missing"]
    from first_strict_execution_journal import (
        execution_scope_digest,
        normalize_execution_scope,
        read_control_execution_receipt,
    )

    issues: list[str] = []
    try:
        scope = normalize_execution_scope(control_execution_scope)
    except Exception:
        return ["first_strict_batch_scope_invalid"]
    expected_rows = expected_batch.get("ordered_samples")
    if not isinstance(expected_rows, list) or len(expected_rows) != 8:
        return ["first_strict_batch_plan_rows_invalid"]
    expected_digest = expected_batch.get("batch_plan_digest")
    if (
        progress.get("schema_version") != 1
        or progress.get("kind") != "first-strict-native-precommit-batch-progress"
        or progress.get("state") not in {
            "pending_next_sample",
            "waiting_live_lease",
        }
        or progress.get("batch_plan_digest") != expected_digest
        or progress.get("sample_plan_digest")
        != expected_batch.get("sample_plan_digest")
        or progress.get("scope_digest") != execution_scope_digest(scope)
        or progress.get("candidate_artifact_hash")
        != scope.get("candidate_artifact_hash")
        or progress.get("control_artifact_hash")
        != scope.get("control_artifact_hash")
        or progress.get("timing_plan_digest")
        != scope.get("native_match_timing_plan_digest")
        or progress.get("sample_count") != 8
        or progress.get("max_new_samples_per_invocation") != 1
    ):
        issues.append("first_strict_batch_progress_binding_invalid")
    planned = progress.get("planned_samples")
    if not isinstance(planned, list) or len(planned) != len(expected_rows):
        issues.append("first_strict_batch_progress_planned_shape_invalid")
        planned = []
    for index, (expected, observed) in enumerate(
        zip(expected_rows, planned), start=1
    ):
        if not isinstance(expected, dict) or not isinstance(observed, dict):
            issues.append(f"first_strict_batch_progress_planned_{index}_invalid")
            continue
        if (
            observed.get("repeat") != expected.get("repeat")
            or observed.get("deck_seed_base") != expected.get("deck_seed_base")
            or observed.get("bot_seed_base") != expected.get("bot_seed_base")
            or not isinstance(observed.get("match_run_id"), str)
            or not observed.get("match_run_id")
        ):
            issues.append(f"first_strict_batch_progress_planned_{index}_mismatch")

    completed = progress.get("completed_samples")
    if not isinstance(completed, list):
        issues.append("first_strict_batch_progress_completed_shape_invalid")
        completed = []
    completed_repeats: list[int] = []
    for entry in completed:
        if not isinstance(entry, dict) or type(entry.get("repeat")) is not int:
            issues.append("first_strict_batch_progress_completed_entry_invalid")
            continue
        repeat = int(entry["repeat"])
        if not 1 <= repeat <= len(expected_rows):
            issues.append("first_strict_batch_progress_completed_repeat_invalid")
            continue
        completed_repeats.append(repeat)
        expected = expected_rows[repeat - 1]
        planned_row = planned[repeat - 1] if len(planned) >= repeat else {}
        evidence, receipt_issues = read_control_execution_receipt(
            entry.get("execution_receipt"),
            expected_scope=scope,
        )
        if receipt_issues or not isinstance(evidence, dict):
            issues.append("first_strict_batch_progress_receipt_invalid")
            continue
        input_payload = evidence.get("input") or {}
        result_payload = evidence.get("result") or {}
        receipt = entry.get("execution_receipt") or {}
        if (
            entry.get("deck_seed_base") != expected.get("deck_seed_base")
            or entry.get("bot_seed_base") != expected.get("bot_seed_base")
            or input_payload.get("repeat") != repeat
            or input_payload.get("deck_seed_base") != expected.get("deck_seed_base")
            or input_payload.get("bot_seed_base") != expected.get("bot_seed_base")
            or entry.get("match_run_id") != planned_row.get("match_run_id")
            or input_payload.get("match_run_id") != entry.get("match_run_id")
            or result_payload.get("match_run_id") != entry.get("match_run_id")
            or receipt.get("match_run_id") != entry.get("match_run_id")
        ):
            issues.append("first_strict_batch_progress_receipt_binding_invalid")
    if completed_repeats != list(range(1, len(completed_repeats) + 1)):
        issues.append("first_strict_batch_progress_completed_order_invalid")
    next_repeat = progress.get("next_repeat")
    if type(next_repeat) is not int or next_repeat != len(completed_repeats) + 1:
        issues.append("first_strict_batch_progress_next_repeat_invalid")
    if progress.get("state") == "pending_next_sample" and not completed_repeats:
        issues.append("first_strict_batch_progress_empty_boundary_invalid")
    return list(dict.fromkeys(issues))


def _persist_first_strict_batch_progress(
    *,
    v: int,
    source_v: int,
    precommit_plan: dict,
    control_execution_scope: dict,
    batch_progress: object,
) -> tuple[bool, dict | None]:
    """CAS-persist a verified batch boundary without recording a gate result."""

    issues = _validate_first_strict_batch_progress(
        batch_progress,
        precommit_plan=precommit_plan,
        control_execution_scope=control_execution_scope,
    )
    checkpoint = _matching_checkpoint(v, source_v)
    if issues:
        return False, _state_blocked(
            "First-strict batch progress cannot be re-proven: "
            + ";".join(issues[:8]),
            v,
            source_v,
            checkpoint,
        )
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("stage") != "critic_checked"
        or (checkpoint.get("audit_context") or {}).get(
            _FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY
        ) != control_execution_scope
    ):
        return False, _state_blocked(
            "First-strict batch continuation lost its critic-approved "
            "checkpoint scope.",
            v,
            source_v,
            checkpoint,
        )
    written = write_pipeline_checkpoint(
        v,
        source_v,
        "critic_checked",
        audit_context={
            _FIRST_STRICT_CONTROL_BATCH_PROGRESS_KEY: batch_progress,
        },
        expected_checkpoint_revision=checkpoint.get("checkpoint_revision"),
        expected_checkpoint_stage="critic_checked",
        expected_workflow_run_id=checkpoint.get("workflow_run_id"),
    )
    after = _matching_checkpoint(v, source_v)
    if not written or not isinstance(after, dict) or (
        after.get("stage") != "critic_checked"
        or (after.get("audit_context") or {}).get(
            _FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY
        ) != control_execution_scope
        or (after.get("audit_context") or {}).get(
            _FIRST_STRICT_CONTROL_BATCH_PROGRESS_KEY
        ) != batch_progress
    ):
        return False, _state_blocked(
            "First-strict batch continuation checkpoint CAS could not be "
            "re-proven.",
            v,
            source_v,
            after,
        )
    return True, None


def _first_strict_batch_pending_result(
    *,
    v: int,
    source_v: int,
    candidate_name: str,
    precommit_attempt: int,
    control_execution_scope: dict,
    batch_progress: dict,
) -> dict:
    """Return a fresh-provider boundary after exactly one durable sample."""

    checkpoint = _matching_checkpoint(v, source_v) or {}
    return _json_tool_result({
        "version": int(v),
        "source_v": int(source_v),
        "candidate": str(candidate_name),
        "passed": False,
        "pending": True,
        "failure_class": "infrastructure_pending",
        "checkpoint_recorded": False,
        "batch_checkpoint_recorded": True,
        "checkpoint_stage": checkpoint.get("stage"),
        "precommit_attempt": int(precommit_attempt),
        "control_execution_scope": control_execution_scope,
        "control_receipt_digest": control_execution_scope.get(
            "control_receipt_digest"
        ),
        "preserved_gate_evidence": ["quality", "review", "critic"],
        "first_strict_batch_pending": batch_progress,
        "directive": (
            "One frozen first-strict native control sample is durably journaled "
            "and indexed. End this provider cycle; a fresh provider cycle must "
            "re-prove the same scope and call run_precommit_eval to request only "
            "the next ordered sample. Do not abandon, rework, or skip ahead."
        ),
        "intent": make_intent(
            "continue",
            next_tool="run_precommit_eval",
            failure_class="infrastructure_pending",
            authority="tool:precommit_eval",
            safe_to_auto_execute=False,
            reason="first_strict_batch_next_sample",
        ),
    })



def _build_pool_gate_spec(opponents, *, first_strict: bool) -> tuple[dict | None, str | None]:
    """Resolve the pool-ratchet gate spec at the plan-freeze moment.

    Returns ``(spec, error)``.  The spec is resolved exactly once, here, from
    the live publication mode and the selected opponents; it is then frozen
    into the plan settings and every retry/commit re-proves it from the frozen
    copy (never from live env).  ``error`` is a fail-fast configuration
    failure (invalid ``POK_PUBLISH_MODE`` or gate overrides) that must pause
    the pipeline before any plan is frozen.  The first-strict system control
    never carries a pool gate.
    """

    if first_strict:
        return None, None
    from pool_ratchet import publish_mode

    try:
        mode = publish_mode()
    except Exception as exc:
        return None, (
            f"publish_mode_invalid:{type(exc).__name__}:{str(exc)[:240]}"
        )
    if mode != "pool_ratchet":
        return None, None
    gate_label = next(
        (
            str(item.get("name") or "")
            for item in opponents
            if isinstance(item, dict)
            and str(item.get("reason") or "") == "pool_weakest_gate"
        ),
        "",
    )
    if not gate_label:
        return None, None
    from pool_ratchet_gate import (
        POOL_GATE_MAX_SCORE_ENV,
        build_pool_gate_settings,
    )

    try:
        spec = build_pool_gate_settings(gate_label)
    except Exception as exc:
        return None, (
            f"pool_gate_settings_invalid:{type(exc).__name__}:{str(exc)[:240]}"
        )
    # Pool-leg threshold derivation (2026-10-16 calibration): the pool leg
    # freezes a deeper sample (opponent item ``matches``, default 24), and
    # its code-default score threshold scales with that count so the pass
    # boundary is W >= n//2 + 2 (14/24) instead of bare majority (13/24).
    # An explicit POK_POOL_GATE_MAX_SCORE keeps full operator authority —
    # the derivation only replaces the unset code default.
    pool_leg_matches = 0
    for item in opponents:
        if (
            isinstance(item, dict)
            and str(item.get("reason") or "") == "pool_weakest_gate"
        ):
            raw = item.get("matches")
            if type(raw) is int and raw >= 1:
                pool_leg_matches = raw
            break
    if pool_leg_matches and not str(
        os.environ.get(POOL_GATE_MAX_SCORE_ENV) or ""
    ).strip():
        spec = {
            **spec,
            "max_score": _pool_gate_max_score_for_matches(pool_leg_matches),
        }
    return spec, None


def _precommit_repair_symbol_domain_clause(v, source_v):
    """P0-A (2026-10-20): lock the precommit repair to the planned symbols.

    Repair workers kept drifting outside the generation's planned
    change_symbol set (v599 edited three unrelated ``_bluff_allowed``
    parameters, v600 deleted its river-air branch), burning review
    rejection after review rejection.  The clause is interpolated from the
    live checkpoint's frozen ``proposal_binding``; ledger/checkpoint read
    failures degrade to an empty string, never to a fabricated symbol.
    """
    ckpt = _matching_checkpoint(v, source_v) or {}
    try:
        from tool_planning_quality_contracts import (
            _repair_symbol_domain_clause,
        )

        return _repair_symbol_domain_clause(ckpt)
    except Exception:
        return ""


async def _run_national_precommit_backend(
    *,
    v: int,
    source_v: int,
    requested_n_games: int,
    effective_n_games: int | None = None,
    candidate_name: str,
    parent_name: str,
    candidate_entry,
    code_fingerprint: str,
    workflow_profile,
    candidate_id: str,
    opponents: list,
    all_opponents: list,
    precommit_attempt: int,
    initial_blockers: list,
    started_at: float,
    precommit_plan: dict,
    evaluation_contract: dict,
    workflow_run_id: str = "",
    checkpoint_revision: int = 0,
    shutdown_token: threading.Event | None = None,
    control_execution_scope: dict | None = None,
):
    """Run the sole active 70-hand native TCP precommit backend."""
    candidate_observability = (
        candidate_observability_identity(v, source_v)
        if candidate_observability_identity is not None
        else {
            "candidate_id": candidate_id,
            "parent_ids": [],
            "lineage_kind": "unavailable",
        }
    )
    candidate_id = str(candidate_observability["candidate_id"])
    candidate_parent_ids = list(candidate_observability["parent_ids"])
    candidate_lineage_metrics = {
        key: candidate_observability[key]
        for key in (
            "lineage_kind",
            "numeric_high_water_version",
            "source_artifact_inherited",
        )
        if key in candidate_observability
    }
    settings = precommit_plan.get("settings") or {}
    national_hands = int(settings.get("hands_per_match") or 0)
    national_matches = int(settings.get("matches_per_opponent") or 0)
    # Pre-registered independent-sample round carried by the frozen plan
    # (1 = original plan, 2 = the single fresh-seed repair sample).
    try:
        precommit_seed_round = int(plan_seed_round(precommit_plan))
    except PrecommitEvalContractError:
        precommit_seed_round = 0
    try:
        from national_native import (
            LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
            require_native_match_timing_plan,
        )

        native_match_timing_plan = require_native_match_timing_plan(
            settings.get("native_match_timing_plan"),
            hands=national_hands,
            requested_timeout_sec=LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
        )
        if settings.get("native_match_timing_plan_digest") != (
            native_match_timing_plan.digest()
        ):
            raise RuntimeError("precommit native timing plan digest mismatch")
    except Exception as exc:
        raise RuntimeError(
            "precommit native timing plan unavailable:"
            f"{type(exc).__name__}: {str(exc)[:240]}"
        ) from exc

    if getattr(workflow_profile, "national_execution_mode", None) != "native_tcp":
        raise RuntimeError("precommit supports only national native_tcp evaluation")
    try:
        from national_runtime_probe import runtime_probe_native_template_evidence

        native_template_evidence = runtime_probe_native_template_evidence()
    except Exception as exc:
        raise RuntimeError(
            "precommit native runtime identity unavailable:"
            f"{type(exc).__name__}:{str(exc)[:200]}"
        ) from exc
    native_tcp_mode = True
    system_control_plan = any(
        str(item.get("authority") or "") == "system_first_strict_control"
        for item in opponents
    )
    native_precommit_batch_plan = None
    if system_control_plan:
        try:
            from precommit_eval_contract import build_native_precommit_batch_plan

            native_precommit_batch_plan = (
                build_native_precommit_batch_plan(
                    list(precommit_plan.get("sample_plan") or []),
                    native_timing_plan=native_match_timing_plan,
                    first_strict_control=True,
                )
            )
            if (
                settings.get("native_precommit_batch_plan")
                != native_precommit_batch_plan
                or settings.get("native_precommit_batch_plan_digest")
                != native_precommit_batch_plan.get("batch_plan_digest")
            ):
                raise RuntimeError("first strict batch plan digest mismatch")
        except Exception as exc:
            raise RuntimeError(
                "first strict native batch plan unavailable:"
                f"{type(exc).__name__}: {str(exc)[:240]}"
            ) from exc
    opponents_with_paths = []
    for item in opponents:
        copied = dict(item)
        if str(item.get("authority") or "") != "system_first_strict_control":
            try:
                copied["path"] = str(
                    get_bot_dir(parse_bot_version(item["name"]))
                )
            except Exception:
                pass
        opponents_with_paths.append(copied)

    blockers = list(initial_blockers or [])
    native_precommit_progress_callback = None
    try:
        from pipeline_state import make_native_match_heartbeat_reporter

        progress_checkpoint = _matching_checkpoint(v, source_v)
        if (
            isinstance(progress_checkpoint, dict)
            and str(progress_checkpoint.get("workflow_run_id") or "")
            == str(workflow_run_id or progress_checkpoint.get("workflow_run_id") or "")
        ):
            native_precommit_progress_callback = (
                make_native_match_heartbeat_reporter(
                    progress_checkpoint,
                    owner_tool="run_precommit_eval",
                )
            )
    except Exception:
        native_precommit_progress_callback = None
    execution_protocol = "national_native_tcp" if native_tcp_mode else "national"
    if system_control_plan:
        if control_execution_scope is None:
            control_execution_scope = (
                _build_first_strict_control_execution_scope(
                    v=v,
                    candidate_name=candidate_name,
                    code_fingerprint=code_fingerprint,
                    opponents=opponents_with_paths,
                    precommit_plan=precommit_plan,
                    evaluation_contract=evaluation_contract,
                    workflow_run_id=workflow_run_id,
                    checkpoint_revision=checkpoint_revision,
                    precommit_attempt=precommit_attempt,
                )
            )
        control_execution_scope, scope_error = (
            _validate_first_strict_control_execution_scope(
                control_execution_scope,
                v=v,
                candidate_name=candidate_name,
                code_fingerprint=code_fingerprint,
                opponents=opponents_with_paths,
                precommit_plan=precommit_plan,
                evaluation_contract=evaluation_contract,
                workflow_run_id=workflow_run_id,
                precommit_attempt=precommit_attempt,
            )
        )
        if scope_error:
            raise RuntimeError(scope_error)
    if national_hands != 70:
        blockers.append({
            "reason": "national_strength_hands_not_70",
            "details": f"Production precommit requires 70 hands per match; plan requested {national_hands}.",
        })
    if not blockers and opponents_with_paths:
        try:
            from national_native import run_native_precommit

            national_result = await run_native_precommit(
                str(candidate_entry),
                opponents_with_paths,
                hands=national_hands,
                matches_per_opponent=national_matches,
                parent_label=parent_name,
                sample_plan=list(precommit_plan.get("sample_plan") or []),
                batch_plan=native_precommit_batch_plan,
                control_execution_scope=control_execution_scope,
                cancel_token=shutdown_token,
                timing_plan=native_match_timing_plan,
                progress_callback=native_precommit_progress_callback,
            )
            live_pending = _control_execution_pending_from_native_result(
                national_result,
                control_execution_scope=control_execution_scope,
            )
            batch_pending = (
                national_result.get("first_strict_batch_pending")
                if isinstance(national_result, dict)
                else None
            )
            if live_pending is not None or batch_pending is not None:
                if (
                    not system_control_plan
                    or control_execution_scope is None
                    or not isinstance(batch_pending, dict)
                ):
                    raise RuntimeError(
                        "first_strict_batch_pending_outside_bound_control_plan"
                    )
                persisted, blocked_result = _persist_first_strict_batch_progress(
                    v=v,
                    source_v=source_v,
                    precommit_plan=precommit_plan,
                    control_execution_scope=control_execution_scope,
                    batch_progress=batch_pending,
                )
                if not persisted:
                    return blocked_result
            if live_pending is not None:
                return _first_strict_live_lease_pending_result(
                    v=v,
                    source_v=source_v,
                    candidate_name=candidate_name,
                    precommit_attempt=precommit_attempt,
                    control_execution_scope=control_execution_scope,
                    pending_execution=live_pending,
                    batch_progress=batch_pending,
                    batch_checkpoint_recorded=True,
                )
            if batch_pending is not None:
                return _first_strict_batch_pending_result(
                    v=v,
                    source_v=source_v,
                    candidate_name=candidate_name,
                    precommit_attempt=precommit_attempt,
                    control_execution_scope=control_execution_scope,
                    batch_progress=batch_pending,
                )
            blockers.extend(national_result.get("blockers") or [])
            if system_control_plan:
                # first_strict_control module removed; skip control-receipt
                # validation (no longer enforced).
                try:
                    from first_strict_control import validate_control_receipt
                except ImportError:
                    validate_control_receipt = None

                if validate_control_receipt is not None:
                    control_receipt = opponents_with_paths[0].get("control_receipt")
                    control_issues = validate_control_receipt(
                        control_receipt,
                        candidate_version=v,
                        source_version=source_v,
                    )
                    if control_issues:
                        blockers.append({
                            "reason": "first_strict_control_contract_drift",
                            "details": ";".join(control_issues[:12]),
                        })
            if native_tcp_mode and _observed_native_sample_plan(national_result) != list(
                precommit_plan.get("sample_plan") or []
            ):
                blockers.append({
                    "reason": "native_precommit_sample_plan_mismatch",
                    "details": "Native precommit did not execute the frozen deck/bot seed schedule.",
                })
        except Exception as exc:
            # The journal may be used directly by a native runner which
            # propagates this typed state rather than returning the structured
            # ``control_execution_pending`` result above.  It has the same
            # scope/lease proof and is never a strategy or control regression.
            try:
                from first_strict_execution_journal import FirstStrictExecutionPending

                live_pending_exception = isinstance(
                    exc,
                    FirstStrictExecutionPending,
                )
            except Exception:
                live_pending_exception = False
            if live_pending_exception:
                if not system_control_plan or control_execution_scope is None:
                    raise RuntimeError(
                        "control_execution_pending_outside_first_strict_plan"
                    ) from exc
                return _first_strict_live_lease_pending_result(
                    v=v,
                    source_v=source_v,
                    candidate_name=candidate_name,
                    precommit_attempt=precommit_attempt,
                    control_execution_scope=control_execution_scope,
                    pending_execution=exc.pending,
                )
            national_result = {
                "evaluation_protocol": execution_protocol,
                "candidate": candidate_name,
                "opponents": opponents_with_paths,
                "matchups": [],
                "total_wins": 0,
                "total_losses": 0,
                "total_draws": 0,
                "paired_bootstrap": {
                    "protocol": execution_protocol,
                    "hands_per_match": national_hands,
                    "matches_per_opponent": national_matches,
                    "net_chips_samples": 0,
                    "net_chips_mean": None,
                    "gate_degraded": True,
                },
                "blockers": [{
                    "reason": "native_precommit_exception" if native_tcp_mode else "national_precommit_exception",
                    "details": f"{type(exc).__name__}: {str(exc)[:500]}",
                }],
                "passed": False,
            }
            blockers.extend(national_result["blockers"])
    else:
        national_result = {
            "evaluation_protocol": execution_protocol,
            "candidate": candidate_name,
            "opponents": opponents_with_paths,
            "matchups": [],
            "total_wins": 0,
            "total_losses": 0,
            "total_draws": 0,
            "paired_bootstrap": {
                "protocol": execution_protocol,
                "hands_per_match": national_hands,
                "matches_per_opponent": national_matches,
                "net_chips_samples": 0,
                "net_chips_mean": None,
                "gate_degraded": True,
            },
            "blockers": blockers,
            "passed": False,
        }

    official_platform_result = {}
    # Lazy import: ``_official_gate_enabled`` lives in tool_gates_native_smoke
    # (re-exported by tool_gates). The official EXE certification system was
    # removed, so this gate is disabled by default (returns False unless
    # POK_OFFICIAL_PRECOMMIT_GATE/POK_OFFICIAL_REQUIRED is set); the block is
    # an optional diagnostic. Without this import the precommit path raised
    # NameError (v171, 2026-08-13) — latent for 5 days because no generation
    # reached precommit during the v170 stall.
    try:
        from tool_gates_native_smoke import _official_gate_enabled
    except Exception:
        _official_gate_enabled = lambda *a, **k: False
    if native_tcp_mode and _official_gate_enabled("POK_OFFICIAL_PRECOMMIT_GATE") and not blockers:
        # The official Windows platform is a protocol/compliance oracle here.
        # Strength and long-run tracking stay on the local native TCP harness.
        official_self_rounds = max(0, _env_int("POK_OFFICIAL_PRECOMMIT_SELF_ROUNDS", 1))
        official_opponent_rounds = max(0, _env_int("POK_OFFICIAL_PRECOMMIT_OPPONENT_ROUNDS", 1))
        official_hands = max(1, min(70, _env_int("POK_OFFICIAL_PRECOMMIT_TARGET_HANDS", 10)))
        try:
            official_platform_result = _request_official_precommit_status(
                candidate=candidate_entry,
                self_play_rounds=official_self_rounds,
                opponent_rounds=official_opponent_rounds,
                target_hands=official_hands,
            )
            national_result["official_platform"] = official_platform_result
        except Exception as exc:
            official_platform_result = {
                "passed": False,
                "blocking": False,
                "issues": [f"official_platform_compliance_exception: {type(exc).__name__}: {str(exc)[:500]}"],
            }
            national_result["official_platform"] = official_platform_result

    total_wins = int(national_result.get("total_wins", 0) or 0)
    total_losses = int(national_result.get("total_losses", 0) or 0)
    total_draws = int(national_result.get("total_draws", 0) or 0)
    matchups = list(national_result.get("matchups") or [])
    paired_bootstrap_payload = dict(national_result.get("paired_bootstrap") or {})
    from strength_order import summarize_70_hand_net_chips, summarize_match_outcomes

    gate_samples = [
        int(value)
        for matchup in matchups
        if is_precommit_gate_matchup(matchup)
        for value in (matchup.get("net_chips") or [])
    ]
    strength_samples = [
        int(value)
        for matchup in matchups
        if is_strength_matchup(matchup)
        for value in (matchup.get("net_chips") or [])
    ]
    precommit_gate_order = summarize_70_hand_net_chips(gate_samples)
    strength_order = summarize_70_hand_net_chips(strength_samples)
    outcome_order = summarize_match_outcomes(total_wins, total_losses, total_draws)
    gate_sample_count = int(
        paired_bootstrap_payload.get("net_chips_samples", 0) or 0
    )
    strength_sample_count = int(
        paired_bootstrap_payload.get(
            "strength_net_chips_samples",
            gate_sample_count,
        )
        or 0
    )
    # Per-opponent frozen match counts (2026-10-16 pool-leg calibration):
    # ``matches_per_opponent`` is the plan-wide default (parent/top legs);
    # an opponent item carrying its own ``matches`` (pool-weakest admission
    # leg) freezes a different depth.  Recorded in every event/receipt so
    # the reported games always match the executed games.
    matches_by_opponent = {
        str(item.get("name") or ""): max(
            1, int(item.get("matches") or national_matches)
        )
        for item in opponents_with_paths
    }
    expected_gate_samples = sum(
        matches_by_opponent[str(item.get("name") or "")]
        for item in opponents_with_paths
        if is_precommit_gate_matchup(item)
    )
    expected_strength_samples = sum(
        matches_by_opponent[str(item.get("name") or "")]
        for item in opponents_with_paths
        if is_strength_matchup(item)
    )
    strength_evidence_required = bool(
        settings.get("strength_evidence_required", True)
    )
    minimum_gate_samples = int(
        settings.get("control_min_samples") or 2
    )
    if gate_sample_count <= 0 and not blockers:
        blockers.append({
            "reason": "national_no_samples",
            "details": "National precommit produced zero completed match samples.",
        })
    blockers.extend(
        _national_sample_contract_blockers(
            paired_bootstrap_payload,
            expected_samples=expected_gate_samples,
        )
    )
    if gate_sample_count != outcome_order["samples"]:
        blockers.append({
            "reason": "national_outcome_sample_mismatch",
            "details": (
                f"Outcome counts describe {outcome_order['samples']} samples but "
                f"the admitted precommit vector contains {gate_sample_count}."
            ),
        })
    if precommit_gate_order["samples"] != gate_sample_count or (
        precommit_gate_order["positive_matches"] != total_wins
        or precommit_gate_order["negative_matches"] != total_losses
        or precommit_gate_order["zero_matches"] != total_draws
    ):
        blockers.append({
            "reason": "national_precommit_sign_mismatch",
            "details": "Admitted precommit net-chip signs disagree with the recorded W/L/D outcomes.",
        })
    if strength_order["samples"] != strength_sample_count:
        blockers.append({
            "reason": "national_strength_sample_mismatch",
            "details": (
                f"Strength vector contains {strength_order['samples']} samples but "
                f"the runtime declared {strength_sample_count}."
            ),
        })
    if strength_evidence_required and (
        strength_order["positive_matches"] != total_wins
        or strength_order["negative_matches"] != total_losses
        or strength_order["zero_matches"] != total_draws
    ):
        blockers.append({
            "reason": "national_strength_sign_mismatch",
            "details": "Admitted strength signs disagree with the recorded W/L/D outcomes.",
        })
    if system_control_plan:
        # first_strict_control module removed; skip control-gate blocker
        # derivation and control-receipt validation (no longer enforced).
        try:
            from first_strict_control import (
                control_gate_blockers,
                validate_control_receipt,
            )
        except ImportError:
            control_gate_blockers = None
            validate_control_receipt = None

        if control_gate_blockers is not None:
            control_blockers, control_gate = control_gate_blockers(
                national_result,
                expected_sample_plan=list(precommit_plan.get("sample_plan") or []),
                expected_execution_scope=control_execution_scope,
            )
            existing_reasons = {
                str(item.get("reason") or "")
                for item in blockers
                if isinstance(item, dict)
            }
            blockers.extend(
                item for item in control_blockers
                if str(item.get("reason") or "") not in existing_reasons
            )
        else:
            control_gate = None
        if validate_control_receipt is not None:
            final_control_issues = validate_control_receipt(
                opponents_with_paths[0].get("control_receipt"),
                candidate_version=v,
                source_version=source_v,
            )
            if final_control_issues and not any(
                isinstance(item, dict)
                and item.get("reason") == "first_strict_control_contract_drift"
                for item in blockers
            ):
                blockers.append({
                    "reason": "first_strict_control_contract_drift",
                    "details": ";".join(final_control_issues[:12]),
                })
    else:
        control_gate = None
    # Pool admission gate (pool-ratchet mode): the frozen plan settings carry
    # the gate spec (label + thresholds); execution reads ONLY the frozen
    # settings, so an operator flipping the live mode/thresholds mid-flight
    # never re-decides an in-flight generation's gate semantics.
    pool_gate_spec = settings.get("pool_gate")
    pool_gate_spec = (
        pool_gate_spec if isinstance(pool_gate_spec, dict) else None
    )
    pool_gate_summary = None
    if pool_gate_spec and pool_gate_spec.get("label"):
        from pool_ratchet_gate import pool_gate_blockers

        pool_blockers, pool_gate_summary = pool_gate_blockers(
            matchups,
            str(pool_gate_spec.get("label")),
            settings,
        )
        blockers.extend(pool_blockers)
    passed = (
        bool(national_result.get("passed"))
        and len(blockers) == 0
        and gate_sample_count == expected_gate_samples
        and strength_sample_count == expected_strength_samples
        and gate_sample_count >= minimum_gate_samples
        and (not strength_evidence_required or strength_sample_count >= 2)
    )

    first_strict_execution_terminal_receipt = None
    if system_control_plan and passed:
        # Close the physical-execution authority before projecting a verified
        # checkpoint.  A crash after this atomic SQLite transition is safe:
        # the next exact precommit invocation replays all eight receipts and
        # reaches this idempotent transition without launching another match.
        from first_strict_execution_journal import succeed_control_execution

        execution_receipts = [
            repeat.get("execution_receipt")
            for matchup in matchups
            for repeat in (matchup.get("repeats") or [])
        ]
        first_strict_execution_terminal_receipt = succeed_control_execution(
            control_execution_scope,
            expected_receipts=execution_receipts,
        )

    try:
        log_system_event(
            "pipeline.precommit_eval.national",
            "info" if passed else "warn",
            f"National {'native TCP ' if native_tcp_mode else ''}precommit "
            f"{'passed' if passed else 'FAILED'} for v{v}: "
            f"{total_wins}W-{total_losses}L-{total_draws}D vs {len(all_opponents)} opponents",
            {
                "version": v,
                "source_v": source_v,
                "passed": passed,
                "evaluation_protocol": execution_protocol,
                "execution_mode": "native_tcp",
                "hands_per_match": national_hands,
                "matches_per_opponent": national_matches,
                "matches_by_opponent": matches_by_opponent,
                "blockers": blockers,
                "paired_bootstrap": paired_bootstrap_payload,
                "pool_gate": pool_gate_summary,
                "elapsed_sec": round(time.time() - started_at, 2),
            },
        )
    except Exception:
        pass

    result = {
        "version": v,
        "source_v": source_v,
        "n_games": national_matches,
        "requested_n_games": requested_n_games,
        "workflow_profile_id": workflow_profile.profile_id,
        "evaluation_protocol": execution_protocol,
        "national_execution_mode": "native_tcp",
        **native_template_evidence,
        "hands_per_match": national_hands,
        "matches_per_opponent": national_matches,
        "matches_by_opponent": matches_by_opponent,
        "expected_net_chips_samples": expected_gate_samples,
        "expected_strength_net_chips_samples": expected_strength_samples,
        "opponents": all_opponents,
        "matchups": matchups,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "total_draws": total_draws,
        "passed": passed,
        "blockers": blockers,
        "paired_bootstrap": paired_bootstrap_payload,
        "precommit_gate_order": precommit_gate_order,
        "strength_order": strength_order,
        "outcome_order": outcome_order,
        "precommit_evidence_authority": (
            "first_strict_bootstrap_regression_v1"
            if system_control_plan
            else "local_precommit_strength"
        ),
        "precommit_gate_admitted": True,
        "strength_admitted": not system_control_plan,
        "rating_eligible": not system_control_plan,
        "official_opponent_eligible": not system_control_plan,
        "first_strict_control_gate": control_gate,
        "primary_70_hand_match_score": outcome_order.get("primary_match_score"),
        "secondary_net_chips_total": strength_order.get("secondary_net_chips_total"),
        "secondary_net_chips_mean": strength_order.get("secondary_net_chips_mean"),
        "precommit_gate_net_chips_total": precommit_gate_order.get(
            "secondary_net_chips_total"
        ),
        "precommit_gate_net_chips_mean": precommit_gate_order.get(
            "secondary_net_chips_mean"
        ),
        "national": national_result,
        "official_platform": official_platform_result,
        "code_fingerprint": code_fingerprint,
        # Non-secret independent-sample identity: the seed round that produced
        # this scorecard, the repair round it belongs to, and the fixed
        # pre-registered decision rule.  The plan (with its nonce) itself
        # remains only in the checkpoint audit_context.
        "precommit_seed_round": precommit_seed_round,
        "precommit_repair_round": max(0, precommit_seed_round - 1),
        "precommit_seed_rounds_max": PRECOMMIT_SEED_ROUNDS_MAX,
        "precommit_decision_rule": {
            "independent_samples_max": PRECOMMIT_SEED_ROUNDS_MAX,
            "fresh_seed_on_repair": True,
            "thresholds_unchanged": True,
            "pass_requires_native_precommit_pass": True,
        },
        # Leak-surface guard (2026-10-11): the frozen plan (which now carries
        # the deck-seed nonce) is deliberately NOT copied into the result
        # payload / checkpoint gate_results.  Its two remaining carriers are
        # the PRECOMMIT CONTRACT DRIFT pause payload and the checkpoint
        # audit_context copy that commit re-proves — every result-side
        # consumer was audited to read neither (rework feedback extraction
        # reads directive/reviewer_feedback/blockers/failures/matchups only;
        # the commit ledger reads the audit_context copy; the sole historical
        # result-side reader was the one-time first-strict control compare).
        "precommit_eval_contract": evaluation_contract,
        "precommit_eval_contract_digest": evaluation_contract.get("contract_digest"),
        "control_execution_scope": (
            national_result.get("control_execution_scope")
            if system_control_plan
            else None
        ),
        "first_strict_execution_terminal_receipt": (
            first_strict_execution_terminal_receipt
            if system_control_plan
            else None
        ),
    }

    scorecard = ScoreCard(
        name="precommit_eval",
        primary_score=outcome_order.get("primary_match_score"),
        metrics={
            "evaluation_protocol": execution_protocol,
            "national_execution_mode": "native_tcp",
            "total_wins": total_wins,
            "total_losses": total_losses,
            "total_draws": total_draws,
            "n_opponents": len(all_opponents),
            "hands_per_match": national_hands,
            "matches_per_opponent": national_matches,
            "matches_by_opponent": matches_by_opponent,
            "primary_70_hand_match_score": outcome_order.get("primary_match_score"),
            "secondary_net_chips_mean": strength_order.get("secondary_net_chips_mean"),
            "precommit_evidence_authority": (
                "first_strict_bootstrap_regression_v1"
                if system_control_plan
                else "local_precommit_strength"
            ),
            "strength_admitted": not system_control_plan,
            "rating_eligible": not system_control_plan,
            "official_opponent_eligible": not system_control_plan,
            "precommit_seed_round": precommit_seed_round,
            "precommit_seed_rounds_max": PRECOMMIT_SEED_ROUNDS_MAX,
        },
    )
    scorecard.add(GateResult.from_bool(
        "national_precommit_regression",
        passed,
        metrics=paired_bootstrap_payload,
        failures=[str(b)[:500] for b in blockers],
    ))
    if official_platform_result:
        official_status = str(official_platform_result.get("status") or "")
        official_issues = official_platform_result.get("issues", []) or []
        try:
            from official_certification import official_compliance_verdict as _official_compliance_verdict
            official_verdict = _official_compliance_verdict(official_platform_result)
        except Exception:
            official_verdict = {
                "ok": True,
                "blocking": False,
                "inconclusive": True,
                "classification": "inconclusive",
            }
        scorecard.add(GateResult.from_bool(
            "official_platform_compliance",
            bool(official_verdict.get("ok")),
            metrics={
                "status": official_status,
                "mode": official_platform_result.get("mode"),
                "queued": official_platform_result.get("queued"),
                "cache_hit": official_platform_result.get("cache_hit"),
                "blocking": official_verdict.get("blocking"),
                "inconclusive": official_verdict.get("inconclusive"),
                "classification": official_verdict.get("classification"),
                **(official_platform_result.get("summary", {}) or {}),
            },
            failures=official_issues[:5] if bool(official_verdict.get("blocking")) else [],
            artifacts={"report": official_platform_result},
            blocking=False,
        ))
    result["scorecard"] = scorecard.model_dump()

    # Gate classification of this failed sample (empty when it passed or when
    # the empty-pool first-strict control owns its own journal).  The
    # ambiguous-failure resample rule reads the same value the failure branch
    # below used to choose infra-retry vs repair.
    precommit_failure_class = ""
    if passed:
        result["failure_class"] = "passed"
        result["intent"] = make_intent(
            "continue",
            next_tool="commit_bot",
            authority="tool:precommit_eval",
            safe_to_auto_execute=True,
        )
    else:
        worst_opponent = _worst_precommit_opponent(matchups, blockers)
        worst_wins, worst_losses = _worst_wins_losses(matchups, worst_opponent)
        if system_control_plan:
            result["directive"] = (
                f"First-strict system-control precommit FAILED for v{v} "
                f"({worst_wins}W-{worst_losses}L). Do not invoke an ordinary "
                "Worker repair and do not retry unchanged code. Abandon this "
                "generation, revise the checked-in deterministic blueprint/control "
                "contract, and restart from a fresh empty-pool authority receipt."
            )
            result["failure_class"] = "system_bootstrap_regression"
            result["intent"] = make_intent(
                "pause",
                failure_class="system_bootstrap_regression",
                authority="tool:precommit_eval",
                safe_to_auto_execute=False,
                reason="first_strict_control_regression",
            )
        else:
            _symbol_domain_clause = _precommit_repair_symbol_domain_clause(
                v, source_v
            )
            result["directive"] = (
                f"National precommit FAILED (attempt {precommit_attempt}/{MAX_PRECOMMIT_RETRIES}, "
                f"seed round {precommit_seed_round}/{PRECOMMIT_SEED_ROUNDS_MAX}) — "
                f"the final gate now uses {'native TCP ' if native_tcp_mode else ''}national 70-hand rules, not local mirror battle. "
                f"Do NOT call run_precommit_eval again on unchanged code. Rework the bot against "
                f"{worst_opponent} ({worst_wins}W-{worst_losses}L) and the listed blockers. "
                f"{_symbol_domain_clause} "
                "A repaired candidate is re-sampled exactly once with a fresh independent "
                "deck seed under the same unchanged gates (pre-registered; no retry-until-pass)."
            )
            # Infrastructure attribution: when every blocker is infra-class
            # (a native run killed mid-match by a startup-watchdog kill,
            # transport stall, launch-latency spike, or a runner exception),
            # the failure is harness-side, not a candidate regression.  The
            # bot runtime is system-owned and byte-identical across every
            # candidate and baseline opponent, so such a stall can never be a
            # policy defect.  Classifying it as ``regression`` here would make
            # the Slice-2b consumer reject the candidate permanently with zero
            # retries (the same class of bug as the smoke-gate single-shot
            # abandon).  Instead emit ``infrastructure`` + ``retry_same_tool``
            # so the consumer's bounded infra retry budget absorbs the
            # transient stall and re-runs precommit.  A genuine regression
            # (e.g. ``aggregate_native_regression`` / ``did_not_beat_parent``)
            # keeps the ``regression`` / rework path below.
            _precommit_gate_class = classify_precommit_gate(
                {"passed": False, "blockers": blockers}
            )
            precommit_failure_class = _precommit_gate_class
            if _precommit_gate_class == "infra_timeout" and precommit_attempt < MAX_PRECOMMIT_RETRIES:
                result["directive"] = (
                    f"National precommit could not reach a conclusive verdict "
                    f"(attempt {precommit_attempt}/{MAX_PRECOMMIT_RETRIES}): "
                    f"every blocker is infrastructure-class "
                    f"(startup-watchdog kill, transport stall, or runner "
                    f"exception).  The candidate policy is unaffected by a "
                    f"native TCP harness stall; retrying precommit."
                )
                result["failure_class"] = "infrastructure"
                result["intent"] = make_intent(
                    "retry_same_tool",
                    next_tool="run_precommit_eval",
                    failure_class="infrastructure",
                    authority="tool:precommit_eval",
                    safe_to_auto_execute=True,
                    reason="native_precommit_infra_blocker",
                )
            else:
                # Either a genuine regression, OR an infra_timeout that has
                # exhausted MAX_PRECOMMIT_RETRIES.  The infra path used to retry
                # unconditionally, which caused 80+ min zero-LLM loops when
                # native TCP matches hit persistent transport stalls.  Once the
                # attempt budget is exhausted, the consumer cannot rework
                # (execute_workers is not a gate-chain tool), so emit a
                # candidate_failure that the consumer rejects immediately —
                # the system auto-abandons and moves to the next generation.
                result["failure_class"] = "regression"
                result["intent"] = make_intent(
                    "abandon",
                    next_tool=None,
                    failure_class="candidate",
                    authority="tool:precommit_eval",
                    safe_to_auto_execute=True,
                    reason="national_precommit_regression_exhausted",
                )

    checkpoint_stage = "verified" if passed else "precommit_failed"
    checkpoint_feedback = None if passed else result.get("directive")
    checkpoint_recorded = _record_gate(
        v,
        source_v,
        "precommit_eval",
        _gate_payload(
            v,
            source_v,
            passed,
            **{k: val for k, val in result.items() if k not in {"version", "source_v", "passed"}},
        ),
        stage=checkpoint_stage,
        reviewer_feedback=checkpoint_feedback,
    )
    result["checkpoint_recorded"] = checkpoint_recorded

    # Durable two-sample evaluation record.  The later rework stage prunes
    # gate_results.precommit_eval, so a failed attempt's complete scorecard
    # must be captured now for the pre-registered repair-round rule to stay
    # inspectable.  On failure this record is load-bearing: an unrecorded
    # attempt would let a repair sample run without its pre-registered
    # predecessor, so the tool fails closed instead of reworking anyway.
    attempt_history_recorded = _record_precommit_attempt_history(
        v,
        source_v,
        result=result,
        precommit_plan=precommit_plan,
        precommit_attempt=precommit_attempt,
    )
    result["precommit_attempt_history_recorded"] = attempt_history_recorded
    if not attempt_history_recorded and not passed and not system_control_plan:
        return _state_blocked(
            "Failed to persist the durable precommit attempt record; the "
            "pre-registered independent-seed repair round cannot start.",
            v,
            source_v,
            _matching_checkpoint(v, source_v),
        )

    # Pre-registered independent-seed resample instead of an in-place repair.
    # A non-decisive round-1 failure (no gate matchup proves a
    # ``losses >= wins + margin`` regression) carries almost no information
    # about the candidate, so the single available fresh-seed sample is
    # strictly more informative than an unguided Worker rework.  The restore
    # runs only after BOTH the failed gate and its durable attempt record are
    # on disk; a failed restore write keeps the historical
    # ``precommit_failed -> execute_workers`` repair route.
    if (
        not passed
        and not system_control_plan
        and checkpoint_recorded
        and attempt_history_recorded
    ):
        _apply_precommit_seed_resample_instead_of_repair(
            v=v,
            source_v=source_v,
            result=result,
            precommit_plan=precommit_plan,
            checkpoint=_matching_checkpoint(v, source_v),
            gate_failure_class=precommit_failure_class,
        )

    if append_candidate_event:
        try:
            append_candidate_event(
                "precommit_finished",
                version=v,
                source_v=source_v,
                candidate_id=candidate_id,
                profile_id=workflow_profile.profile_id,
                workflow_profile_id=workflow_profile.profile_id,
                run_id=f"{v}#0",
                stage="verified" if passed else "precommit_failed",
                parent_ids=candidate_parent_ids,
                gate="precommit_eval",
                scorecard=scorecard,
                gate_results=scorecard.gates,
                metrics={
                    **candidate_lineage_metrics,
                    "passed": passed,
                    "evaluation_protocol": execution_protocol,
                    "national_execution_mode": "native_tcp",
                    "total_wins": total_wins,
                    "total_losses": total_losses,
                    "total_draws": total_draws,
                    "net_chips_mean": paired_bootstrap_payload.get("net_chips_mean"),
                    "precommit_seed_round": precommit_seed_round,
                    "precommit_repair_round": max(0, precommit_seed_round - 1),
                },
                failures=[str(b)[:500] for b in blockers],
                failure_class=(
                    ""
                    if passed
                    else "system_bootstrap_regression"
                    if system_control_plan
                    else "national_precommit_regression"
                ),
            )
        except Exception as e:
            log.warning("candidate ledger national precommit_finished write failed: %s", e)
    if system_control_plan and not passed:
        return await _abandon_first_strict_generation(
            result,
            reason="first_strict_control_precommit_rejected",
        )
    return _json_tool_result(result)


async def _run_national_precommit_attempt(
    shutdown_token: threading.Event,
    **kwargs,
):
    """Run one backend attempt and fence its exact token on task cancellation."""

    try:
        return await _run_national_precommit_backend(
            shutdown_token=shutdown_token,
            **kwargs,
        )
    except asyncio.CancelledError:
        # Deterministic checkpoint routes can be cancelled outside
        # ``_run_one_cycle``.  Set the locally captured identity rather than the
        # module's possibly-rotated current token.
        set_precommit_shutdown(shutdown_token)
        raise


# ──────────────────────────────────────────────
# Precommit Eval
# ──────────────────────────────────────────────


def _worst_precommit_opponent(*args, **kwargs):
    """Delegate to tool_eval_failure_attribution."""
    return _fa._worst_precommit_opponent(*args, **kwargs)


def _worst_wins_losses(*args, **kwargs):
    """Delegate to tool_eval_failure_attribution."""
    return _fa._worst_wins_losses(*args, **kwargs)


def _infra_timeout_retry_authority_error(*args, **kwargs):
    """Delegate to tool_eval_failure_attribution."""
    return _fa._infra_timeout_retry_authority_error(*args, **kwargs)


@tool("run_precommit_eval", "Run the final native national-TCP regression check before commit.", {"version": int, "source_v": int, "n_games": int})
async def run_precommit_eval(args):
    _t0 = time.time()
    # Capture before any await or checkpoint transition.  The outer cycle may
    # rotate the module's current token as soon as this attempt is cancelled;
    # this local reference must remain bound to the old, permanently-set token.
    precommit_shutdown_token = begin_precommit_shutdown_attempt()
    v, source_v = _resolve_version_args(args)
    if v is None or source_v is None:
        return _json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)
    # Cap n_games: precommit eval is a quick regression check, NOT a full evaluation.
    # Default is PRECOMMIT_DEFAULT_N_GAMES (12), clamped to
    # [PRECOMMIT_MIN_N_GAMES, PRECOMMIT_MAX_N_GAMES]. The regression gate uses paired net-chip
    # bootstrap CIs (the parent-gate tie-break and the aggregate gate), which are far less
    # noisy than binary W/L at the same n_games.
    requested = int(args.get("n_games", PRECOMMIT_DEFAULT_N_GAMES) or PRECOMMIT_DEFAULT_N_GAMES)
    n_games = min(max(PRECOMMIT_MIN_N_GAMES, requested), PRECOMMIT_MAX_N_GAMES)

    candidate_name = active_bot_name(v)
    parent_name = active_bot_name(source_v)
    candidate_dir = get_bot_dir(v)
    try:
        from tool_gates import _bot_code_fingerprint
        code_fingerprint = _bot_code_fingerprint(candidate_dir)
    except Exception:
        code_fingerprint = ""

    workflow_profile = get_workflow_profile()
    if (
        getattr(workflow_profile, "national_execution_mode", None) != "native_tcp"
        or getattr(workflow_profile, "evaluation_protocol", None) != "national"
    ):
        return _json_tool_result({"error": "only national native_tcp precommit is supported"})
    native_tcp_mode = True
    expected_execution_mode = "native_tcp"
    evaluation_protocol = "national"
    national_evaluation = True
    candidate_entry = candidate_dir / "national_bot.py"
    try:
        from national_runtime_probe import (
            runtime_probe_native_template_evidence,
            runtime_probe_native_template_evidence_matches,
        )

        native_template_evidence = runtime_probe_native_template_evidence()
    except Exception as exc:
        # Never replace a missing current system runtime identity with a
        # best-effort cache hit.  Retry only after the operator restores the
        # checked-in native runtime authority.
        return _json_tool_result({
            "error": "native_runtime_identity_unavailable",
            "version": v,
            "source_v": source_v,
            "passed": False,
            "failure_class": "infrastructure",
            "details": f"{type(exc).__name__}: {str(exc)[:200]}",
        })

    # Idempotency guard: skip if precommit eval already passed for the same code snapshot
    # under the same workflow profile and national execution mode.
    _precommit_ckpt = _matching_checkpoint(v, source_v)
    infra_timeout_retry = bool(
        _precommit_ckpt
        and _precommit_ckpt.get("stage") == "infra_timed_out"
    )
    if infra_timeout_retry:
        authority_error = _infra_timeout_retry_authority_error(
            _precommit_ckpt,
            candidate_dir=candidate_dir,
            code_fingerprint=code_fingerprint,
            version=v,
            source_v=source_v,
        )
        if authority_error:
            return _state_blocked(
                authority_error,
                v,
                source_v,
                _precommit_ckpt,
            )
        # The timeout overlay preserves the already-approved candidate and gate
        # evidence but is not itself a legal predecessor of verified/failed.
        # Restore the exact critic_checked state by checkpoint CAS before any
        # expensive retry so the normal precommit transitions remain valid.
        restored = write_pipeline_checkpoint(
            v,
            source_v,
            "critic_checked",
            expected_checkpoint_revision=_precommit_ckpt.get(
                "checkpoint_revision"
            ),
            expected_checkpoint_stage="infra_timed_out",
            expected_workflow_run_id=_precommit_ckpt.get("workflow_run_id"),
        )
        if not restored:
            return _state_blocked(
                "Failed to restore infra_timed_out checkpoint for exact "
                "precommit retry.",
                v,
                source_v,
                _precommit_ckpt,
            )
        _precommit_ckpt = _matching_checkpoint(v, source_v)
        if (
            not isinstance(_precommit_ckpt, dict)
            or _precommit_ckpt.get("stage") != "critic_checked"
        ):
            return _state_blocked(
                "Infra-timeout checkpoint restoration could not be re-proven.",
                v,
                source_v,
                _precommit_ckpt,
            )
    stored_plan = (
        ((_precommit_ckpt.get("audit_context") or {}).get("precommit_eval_plan"))
        if _precommit_ckpt
        else None
    )
    stored_plan_issues = (
        validate_precommit_plan(
            stored_plan,
            candidate_version=v,
            source_version=source_v,
            profile_id=workflow_profile.profile_id,
            execution_mode=expected_execution_mode,
            evaluation_protocol=evaluation_protocol,
        )
        if national_evaluation and stored_plan is not None
        else []
    )
    current_evaluation_contract = (
        build_evaluation_contract(
            stored_plan,
            candidate_code_fingerprint=code_fingerprint,
        )
        if national_evaluation and stored_plan is not None and not stored_plan_issues
        else None
    )
    profile_refresh = _prepare_official_profile_refresh(_precommit_ckpt, "run_precommit_eval")
    if not profile_refresh.get("ok"):
        return _state_blocked(
            str(profile_refresh.get("error") or "official profile refresh preparation failed"),
            v,
            source_v,
            _precommit_ckpt,
        )
    if _precommit_ckpt and _precommit_ckpt.get("stage") in (
        "verified", "archived"
    ):
        precommit_gate = _precommit_ckpt.get("gate_results", {}).get("precommit_eval", {})
        cached_fingerprint = precommit_gate.get("code_fingerprint")
        cached_profile_id = str(precommit_gate.get("workflow_profile_id") or precommit_gate.get("profile_id") or "")
        cached_execution_mode = str(precommit_gate.get("national_execution_mode") or "")
        cached_contract = precommit_gate.get("precommit_eval_contract")
        if workflow_profile.profile_id == "default":
            cache_profile_matches = (
                cached_profile_id in {"", "default"}
                and cached_execution_mode in {"", expected_execution_mode}
            )
        else:
            cache_profile_matches = (
                cached_profile_id == workflow_profile.profile_id
                and cached_execution_mode == expected_execution_mode
            )
        contract_matches = (
            not national_evaluation
            or (
                current_evaluation_contract is not None
                and not validate_evaluation_contract(
                    cached_contract,
                    stored_plan,
                    candidate_code_fingerprint=code_fingerprint,
                )
            )
        )
        first_strict_terminal_matches = True
        cached_plan_opponents = (
            stored_plan.get("opponents")
            if isinstance(stored_plan, dict)
            else []
        )
        if any(
            isinstance(item, dict)
            and item.get("authority") == "system_first_strict_control"
            for item in (cached_plan_opponents or [])
        ):
            try:
                from first_strict_execution_journal import (
                    read_succeeded_control_execution,
                )

                cached_national = precommit_gate.get("national") or {}
                cached_receipts = [
                    repeat.get("execution_receipt")
                    for matchup in (cached_national.get("matchups") or [])
                    for repeat in (matchup.get("repeats") or [])
                ]
                read_succeeded_control_execution(
                    precommit_gate.get("control_execution_scope"),
                    expected_receipts=cached_receipts,
                    expected_terminal_receipt=precommit_gate.get(
                        "first_strict_execution_terminal_receipt"
                    ),
                )
            except Exception:
                first_strict_terminal_matches = False
        if (
            precommit_gate.get("passed") is True
            and cached_fingerprint == code_fingerprint
            and cache_profile_matches
            and contract_matches
            and runtime_probe_native_template_evidence_matches(precommit_gate)
            # A historical precommit result is only reusable while the whole
            # gate chain remains reusable.  In particular, the quality gate
            # owns the runtime-probe repeatability receipt; checking just the
            # precommit's native-template projection would otherwise allow a
            # direct caller to bypass a now-missing or malformed receipt.
            and _quality_gate_ok(_precommit_ckpt)
            and _review_gate_ok(_precommit_ckpt)
            and _critic_gate_ok(_precommit_ckpt)
            and first_strict_terminal_matches
        ):
            precommit_gate["idempotent_cache"] = True
            precommit_gate["directive"] = (
                "Precommit eval ALREADY PASSED. Do NOT re-run. "
                "Call commit_bot(version, source_v, strategy, review_approved=true) next."
            )
            return _json_tool_result(precommit_gate)
        if precommit_gate.get("passed") is True:
            log_system_event(
                "pipeline.precommit_cache_stale",
                "warn",
                f"Precommit cache stale for v{v}; cached code/profile does not match active eval requirements.",
                {
                    "version": v,
                    "source_v": source_v,
                    "cached_fingerprint": cached_fingerprint,
                    "current_fingerprint": code_fingerprint,
                    "cached_workflow_profile_id": cached_profile_id,
                    "active_workflow_profile_id": workflow_profile.profile_id,
                    "cached_execution_mode": cached_execution_mode,
                    "active_execution_mode": expected_execution_mode,
                    "cached_native_runtime_template_digest": precommit_gate.get(
                        "native_runtime_template_digest"
                    ),
                    "active_native_runtime_template_digest": (
                        native_template_evidence[
                            "native_runtime_template_digest"
                        ]
                    ),
                    "precommit_plan_issues": stored_plan_issues,
                    "cached_contract_digest": precommit_gate.get("precommit_eval_contract_digest"),
                    "active_contract_digest": (
                        current_evaluation_contract.get("contract_digest")
                        if current_evaluation_contract
                        else None
                    ),
                },
            )

    _set_pipeline_status(f"Pre-commit eval for v{v}")

    candidate_observability = (
        candidate_observability_identity(v, source_v)
        if candidate_observability_identity is not None
        else {
            "candidate_id": candidate_name,
            "parent_ids": [],
            "lineage_kind": "unavailable",
        }
    )
    candidate_id = str(candidate_observability["candidate_id"])
    candidate_parent_ids = list(candidate_observability["parent_ids"])
    candidate_lineage_metrics = {
        key: candidate_observability[key]
        for key in (
            "lineage_kind",
            "numeric_high_water_version",
            "source_artifact_inherited",
        )
        if key in candidate_observability
    }
    if append_candidate_event:
        try:
            append_candidate_event(
                "precommit_started",
                version=v,
                source_v=source_v,
                candidate_id=candidate_id,
                profile_id=workflow_profile.profile_id,
                workflow_profile_id=workflow_profile.profile_id,
                run_id=f"{v}#0",
                stage="precommit_eval",
                parent_ids=candidate_parent_ids,
                gate="precommit_eval",
                metrics={
                    **candidate_lineage_metrics,
                    "n_games": n_games,
                },
            )
        except Exception as e:
            log.warning("candidate ledger precommit_started write failed: %s", e)
    blockers = []
    matchups = []

    ckpt = _matching_checkpoint(v, source_v)
    if not _quality_gate_ok(ckpt) or not _review_gate_ok(ckpt) or not _critic_gate_ok(ckpt):
        return _state_blocked(
            "run_precommit_eval requires passing quality/reviewer gates and a completed advisory critic role for the same version/source_v.",
            v,
            source_v,
            ckpt,
        )

    try:
        from system_strict_bootstrap import is_declared_native_bootstrap

        declared_first_strict = is_declared_native_bootstrap(ckpt)
    except Exception:
        declared_first_strict = False
    planned_system_control = bool(
        stored_plan
        and any(
            str(item.get("authority") or "") == "system_first_strict_control"
            for item in (stored_plan.get("opponents") or [])
            if isinstance(item, dict)
        )
    )
    first_strict_control_receipt = None
    if declared_first_strict:
        # first_strict_control module removed; skip control-receipt
        # validation (no longer enforced).
        try:
            from first_strict_control import validate_control_receipt
        except ImportError:
            validate_control_receipt = None

        first_strict_control_receipt = (
            ((ckpt.get("gate_results") or {}).get("quality") or {}).get(
                "first_strict_control_receipt"
            )
        )
        if validate_control_receipt is None:
            control_issues = []
        else:
            control_issues = validate_control_receipt(
                first_strict_control_receipt,
                checkpoint=ckpt,
                candidate_version=v,
                source_version=source_v,
            )
        if stored_plan is not None and not planned_system_control:
            control_issues.append(
                "first_strict_control_declared_plan_authority_mismatch"
            )
        if planned_system_control:
            planned_receipt = (
                ((stored_plan.get("opponents") or [{}])[0]).get(
                    "control_receipt"
                )
            )
            if planned_receipt != first_strict_control_receipt:
                control_issues.append(
                    "first_strict_control_quality_plan_receipt_mismatch"
                )
        if control_issues:
            return await _abandon_first_strict_generation({
                "error": "FIRST_STRICT_CONTROL_AUTHORITY_INVALID",
                "version": v,
                "source_v": source_v,
                "passed": False,
                "action": "abandon_generation",
                "failure_class": "control_plane",
                "validation_errors": list(dict.fromkeys(control_issues))[:20],
                "intent": make_intent(
                    "pause",
                    failure_class="control_plane",
                    authority="tool:precommit_eval",
                    safe_to_auto_execute=False,
                    reason="first_strict_control_replan_required",
                ),
                "directive": (
                    "The empty-pool/system-control authority drifted. A newly "
                    "published strict bot or changed control/runtime invalidates "
                    "this plan; abandon it and create a fresh opponent plan."
                ),
            }, reason="first_strict_control_authority_invalid")
    elif planned_system_control:
        return await _abandon_first_strict_generation({
            "error": "UNDECLARED_FIRST_STRICT_CONTROL_PLAN",
            "version": v,
            "source_v": source_v,
            "passed": False,
            "action": "abandon_generation",
            "failure_class": "control_plane",
            "intent": make_intent(
                "pause",
                failure_class="control_plane",
                authority="tool:precommit_eval",
                safe_to_auto_execute=False,
                reason="undeclared_first_strict_control_plan",
            ),
        }, reason="undeclared_first_strict_control_plan")

    if not candidate_entry.exists():
        result = {
            "version": v,
            "source_v": source_v,
            "n_games": n_games,
            "code_fingerprint": code_fingerprint,
            "passed": False,
            "blockers": [{"reason": "candidate_missing", "details": str(candidate_entry)}],
            "opponents": [],
            "matchups": [],
        }
        gate_extra = {k: val for k, val in result.items() if k not in {"version", "source_v", "passed"}}
        _record_gate(v, source_v, "precommit_eval", _gate_payload(v, source_v, False, **gate_extra), stage=None)
        if declared_first_strict:
            return await _abandon_first_strict_generation(
                result,
                reason="first_strict_control_candidate_missing",
            )
        return _json_tool_result(result)

    # compile/smoke already verified by quality gates (required by _quality_gate_ok above)

    if national_evaluation and stored_plan is not None:
        if stored_plan_issues:
            payload = {
                "error": "PRECOMMIT CONTRACT DRIFT: restart the generation from a fresh repository baseline.",
                "version": v,
                "source_v": source_v,
                "passed": False,
                "blockers": [{
                    "reason": "precommit_contract_drift",
                    "details": "; ".join(stored_plan_issues[:12]),
                }],
                "precommit_eval_plan": stored_plan,
                "failure_class": "infrastructure",
                "intent": make_intent(
                    "pause",
                    failure_class="infrastructure",
                    authority="tool:precommit_eval",
                    safe_to_auto_execute=False,
                    reason="precommit_contract_drift",
                ),
            }
            if declared_first_strict:
                return await _abandon_first_strict_generation(
                    payload,
                    reason="first_strict_control_plan_drift",
                )
            return _json_tool_result(payload)
        if not declared_first_strict:
            # Pre-registered independent-seed repair round: a Workers repair
            # round increments ``precommit_rework_count``, and the next
            # precommit must NOT replay the failed plan's deck schedule.  The
            # rotated plan keeps every opponent, per-opponent sample depth,
            # evaluator identity and gate threshold; only the one-time nonce
            # changes.  Infrastructure retries never increment the counter and
            # therefore keep the exact same plan.
            rotated_plan, rotation_error = (
                _rotate_precommit_plan_for_repair_round(
                    v=v,
                    source_v=source_v,
                    checkpoint=ckpt,
                    stored_plan=stored_plan,
                    workflow_profile=workflow_profile,
                )
            )
            if rotation_error is not None:
                return _json_tool_result(rotation_error)
            if rotated_plan is not None:
                stored_plan = rotated_plan
                stored_plan_issues = []
                current_evaluation_contract = build_evaluation_contract(
                    rotated_plan,
                    candidate_code_fingerprint=code_fingerprint,
                )
        opponents = opponents_from_plan(stored_plan)
        frozen_settings = stored_plan.get("settings") or {}
        n_games = int(frozen_settings.get("matches_per_opponent") or n_games)
    elif national_evaluation and declared_first_strict:
        try:
            from first_strict_control import opponent_from_receipt

            opponents = [opponent_from_receipt(first_strict_control_receipt)]
        except Exception as exc:
            return await _abandon_first_strict_generation({
                "error": "FIRST_STRICT_CONTROL_OPPONENT_INVALID",
                "version": v,
                "source_v": source_v,
                "passed": False,
                "action": "abandon_generation",
                "failure_class": "control_plane",
                "message": f"{type(exc).__name__}: {str(exc)[:800]}",
                "intent": make_intent(
                    "pause",
                    failure_class="control_plane",
                    authority="tool:precommit_eval",
                    safe_to_auto_execute=False,
                    reason="first_strict_control_opponent_invalid",
                ),
            }, reason="first_strict_control_opponent_invalid")
    else:
        opponents = _select_precommit_opponents(
            v,
            source_v,
            checkpoint=ckpt,
        )
    # Add crossover parent_b if applicable
    if (
        stored_plan is None
        and not declared_first_strict
        and ckpt
        and ckpt.get("parent2_v")
    ):
        parent2_name = active_bot_name(ckpt["parent2_v"])
        parent2_path = get_bot_dir(parse_bot_version(parent2_name))
        if parent2_path.exists() and not any(o["name"] == parent2_name for o in opponents):
            opponents.append({"name": parent2_name, "reason": "crossover_parent_b"})

    # Fail-closed opponent-selection blockers (pool-ratchet mode): the frozen
    # snapshot must be able to name a pool-weakest member.  A missing
    # conservative_rating (or an invalid publish-mode config) is an
    # infrastructure boundary that requires operator attention — it must never
    # degrade into a silent legacy selection, and never blame the candidate.
    selection_blockers = []
    clean_opponents = []
    for item in opponents:
        if isinstance(item, dict) and isinstance(
            item.get("selection_blocker"), dict
        ):
            selection_blockers.append(dict(item["selection_blocker"]))
        else:
            clean_opponents.append(item)
    if selection_blockers:
        blockers.extend(selection_blockers)
        opponents = clean_opponents
        return _json_tool_result({
            "error": (
                "PRECOMMIT OPPONENT SELECTION FAILED: "
                + "; ".join(
                    str(item.get("reason") or "selection_failed")
                    for item in selection_blockers
                )
            ),
            "version": v,
            "source_v": source_v,
            "passed": False,
            "blockers": blockers,
            "opponents": opponents,
            "matchups": [],
            "failure_class": "infrastructure",
            "intent": make_intent(
                "pause",
                failure_class="infrastructure",
                authority="tool:precommit_eval",
                safe_to_auto_execute=False,
                reason="precommit_opponent_selection_failed",
            ),
        })

    if not opponents:
        blockers.append({"reason": "no_opponents", "details": "No eligible current-epoch native TCP opponents found."})
    all_opponents = list(opponents)  # preserve full list for result reporting

    precommit_plan = stored_plan
    evaluation_contract = current_evaluation_contract
    if national_evaluation and precommit_plan is None and opponents:
        national_hands, national_matches = _national_precommit_shape(workflow_profile, n_games)
        # Pool-leg sample depth (2026-10-16 calibration): freeze a deeper
        # per-opponent leg for the pool-weakest admission opponent (default
        # POK_PRECOMMIT_POOL_WEAKEST_N_GAMES=24).  Every other leg (parent,
        # top-strength, h2h-weakness) keeps the plan-wide count, so the
        # parent gate semantics are unchanged.
        try:
            pool_weakest_matches = precommit_pool_weakest_n_games()
        except ValueError as exc:
            payload = {
                "error": f"PRECOMMIT PLAN CREATION FAILED: {exc}",
                "version": v,
                "source_v": source_v,
                "passed": False,
                "blockers": [{
                    "reason": "precommit_plan_creation_failed",
                    "details": str(exc)[:800],
                }],
                "failure_class": "infrastructure",
                "intent": make_intent(
                    "pause",
                    failure_class="infrastructure",
                    authority="tool:precommit_eval",
                    safe_to_auto_execute=False,
                    reason="precommit_plan_creation_failed",
                ),
            }
            return _json_tool_result(payload)
        for item in opponents:
            if (
                isinstance(item, dict)
                and str(item.get("reason") or "") == "pool_weakest_gate"
            ):
                item["matches"] = pool_weakest_matches
        # Parent-leg sample depth (2026-10-22 power calibration): freeze the
        # parent opponent's own count (POK_PRECOMMIT_PARENT_N_GAMES, default
        # 12 = historical behavior).  The parent gate threshold and every
        # other leg are untouched; only the number of fresh 70-hand parent
        # samples changes.
        try:
            parent_matches = precommit_parent_n_games()
        except ValueError as exc:
            return _json_tool_result({
                "error": f"PRECOMMIT PLAN CREATION FAILED: {exc}",
                "version": v,
                "source_v": source_v,
                "passed": False,
                "blockers": [{
                    "reason": "precommit_plan_creation_failed",
                    "details": str(exc)[:800],
                }],
                "failure_class": "infrastructure",
                "intent": make_intent(
                    "pause",
                    failure_class="infrastructure",
                    authority="tool:precommit_eval",
                    safe_to_auto_execute=False,
                    reason="precommit_plan_creation_failed",
                ),
            })
        for item in opponents:
            if (
                isinstance(item, dict)
                and str(item.get("reason") or "") == "parent"
                and parent_matches is not None
            ):
                item["matches"] = parent_matches
        # Pool-ratchet admission gate spec: resolved once at this plan-freeze
        # moment from the live publication mode and the selected opponents,
        # then frozen into the plan settings (retries re-prove the frozen
        # copy; a mid-flight mode flip cannot change this generation's gate).
        pool_gate_spec, pool_gate_error = _build_pool_gate_spec(
            opponents,
            first_strict=declared_first_strict,
        )
        if pool_gate_error:
            payload = {
                "error": f"PRECOMMIT PLAN CREATION FAILED: {pool_gate_error}",
                "version": v,
                "source_v": source_v,
                "passed": False,
                "blockers": [{
                    "reason": "precommit_plan_creation_failed",
                    "details": pool_gate_error,
                }],
                "failure_class": "infrastructure",
                "intent": make_intent(
                    "pause",
                    failure_class="infrastructure",
                    authority="tool:precommit_eval",
                    safe_to_auto_execute=False,
                    reason="precommit_plan_creation_failed",
                ),
            }
            return _json_tool_result(payload)
        try:
            # A plan frozen after a Workers repair round is the pre-registered
            # repair sample (round 2), not a second "original" sample.
            new_plan_seed_round = 1
            if not declared_first_strict:
                new_plan_seed_round = min(
                    PRECOMMIT_SEED_ROUNDS_MAX,
                    _precommit_rework_seed_round(ckpt),
                )
            precommit_plan = create_precommit_plan(
                candidate_version=v,
                source_version=source_v,
                profile_id=workflow_profile.profile_id,
                execution_mode=expected_execution_mode,
                evaluation_protocol=evaluation_protocol,
                opponents=opponents,
                hands_per_match=national_hands,
                matches_per_opponent=national_matches,
                path_resolver=lambda item: (
                    item.get("path")
                    or get_bot_dir(parse_bot_version(item["name"]))
                ),
                require_published_opponents=True,
                pool_gate=pool_gate_spec,
                seed_round=new_plan_seed_round,
            )
        except PrecommitEvalContractError as exc:
            payload = {
                "error": f"PRECOMMIT PLAN CREATION FAILED: {exc}",
                "version": v,
                "source_v": source_v,
                "passed": False,
                "blockers": [{
                    "reason": "precommit_plan_creation_failed",
                    "details": str(exc)[:800],
                }],
                "failure_class": "infrastructure",
                "intent": make_intent(
                    "pause",
                    failure_class="infrastructure",
                    authority="tool:precommit_eval",
                    safe_to_auto_execute=False,
                    reason="precommit_plan_creation_failed",
                ),
            }
            if declared_first_strict:
                return await _abandon_first_strict_generation(
                    payload,
                    reason="first_strict_control_plan_creation_failed",
                )
            return _json_tool_result(payload)
        current_stage = ckpt.get("stage", "critic_checked") if ckpt else "critic_checked"
        if not write_pipeline_checkpoint(
            v,
            source_v,
            current_stage,
            audit_context={"precommit_eval_plan": precommit_plan},
        ):
            if declared_first_strict:
                return await _abandon_first_strict_generation({
                    "error": "Failed to persist immutable precommit evaluation plan.",
                    "version": v,
                    "source_v": source_v,
                    "passed": False,
                    "failure_class": "control_plane",
                }, reason="first_strict_control_plan_persist_failed")
            return _state_blocked(
                "Failed to persist immutable precommit evaluation plan.",
                v,
                source_v,
                ckpt,
            )
        evaluation_contract = build_evaluation_contract(
            precommit_plan,
            candidate_code_fingerprint=code_fingerprint,
        )
        # Record the seed provenance for audit replay: the structured event
        # payload (journal side) carries the nonce original bound to the plan
        # digest; UI-facing text renders only the derived base and digest.
        try:
            frozen_seed_settings = (
                (precommit_plan.get("settings") or {})
            )
            log_system_event(
                "pipeline.precommit_seed_frozen",
                "info",
                (
                    f"Precommit deck seed frozen for v{v}: "
                    f"derivation={frozen_seed_settings.get('seed_derivation')} "
                    f"deck_seed_base={frozen_seed_settings.get('deck_seed_base')}"
                ),
                {
                    "version": v,
                    "source_v": source_v,
                    "plan_digest": precommit_plan.get("plan_digest"),
                    "seed_derivation": frozen_seed_settings.get(
                        "seed_derivation"
                    ),
                    "seed_round": frozen_seed_settings.get(
                        "precommit_seed_round"
                    ),
                    "deck_seed_base": frozen_seed_settings.get(
                        "deck_seed_base"
                    ),
                    "deck_seed_nonce": frozen_seed_settings.get(
                        "deck_seed_nonce"
                    ),
                },
            )
        except Exception:
            pass
        opponents = opponents_from_plan(precommit_plan)
        all_opponents = list(opponents)
        n_games = int((precommit_plan.get("settings") or {}).get("matches_per_opponent") or n_games)

    if national_evaluation and precommit_plan is None:
        payload = {
            "error": "PRECOMMIT PLAN UNAVAILABLE: no immutable opponent set could be created.",
            "version": v,
            "source_v": source_v,
            "passed": False,
            "blockers": blockers or [{
                "reason": "precommit_plan_unavailable",
                "details": "No eligible national opponent was available.",
            }],
            "failure_class": "infrastructure",
            "intent": make_intent(
                "pause",
                failure_class="infrastructure",
                authority="tool:precommit_eval",
                safe_to_auto_execute=False,
                reason="precommit_plan_unavailable",
            ),
        }
        if declared_first_strict:
            return await _abandon_first_strict_generation(
                payload,
                reason="first_strict_control_plan_unavailable",
            )
        return _json_tool_result(payload)

    # A normal battle round gets a new logical attempt.  An interrupted attempt
    # is different: its frozen system-control journal scope (and, explicitly,
    # ``infra_timed_out``) resumes the exact same identity, so a completed match
    # can be recovered instead of relaunched under a new revision/attempt scope.
    attempt_ckpt = _matching_checkpoint(v, source_v) if opponents else ckpt
    precommit_attempt = (
        int((attempt_ckpt or {}).get("precommit_attempt", 0) or 0)
    )
    system_control_plan = any(
        str(item.get("authority") or "") == "system_first_strict_control"
        for item in opponents
    )
    control_execution_scope = None
    execution_ckpt = attempt_ckpt
    resume_control_attempt = False
    if opponents and system_control_plan and precommit_attempt >= 1:
        frozen_scope = (
            ((attempt_ckpt or {}).get("audit_context") or {}).get(
                _FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY
            )
        )
        control_execution_scope, scope_error = (
            _validate_first_strict_control_execution_scope(
                frozen_scope,
                v=v,
                candidate_name=candidate_name,
                code_fingerprint=code_fingerprint,
                opponents=opponents,
                precommit_plan=precommit_plan,
                evaluation_contract=evaluation_contract,
                workflow_run_id=str(
                    (attempt_ckpt or {}).get("workflow_run_id") or ""
                ),
                precommit_attempt=precommit_attempt,
            )
        )
        if scope_error:
            return _state_blocked(
                scope_error,
                v,
                source_v,
                attempt_ckpt,
            )
        # A persisted scope with no terminal precommit stage is the durable
        # in-flight marker.  Reuse it after process cancellation/crash as well
        # as after an explicit infra_timed_out overlay.
        resume_control_attempt = True
    if opponents and not infra_timeout_retry and not resume_control_attempt:
        current_stage = (
            (attempt_ckpt or {}).get("stage", "critic_checked")
        )
        precommit_attempt += 1
        audit_context_update = None
        predicted_scope = None
        if system_control_plan:
            current_revision = (attempt_ckpt or {}).get(
                "checkpoint_revision"
            )
            workflow_run_id = str(
                (attempt_ckpt or {}).get("workflow_run_id") or ""
            )
            if (
                type(current_revision) is not int
                or current_revision < 1
                or not workflow_run_id
            ):
                return _state_blocked(
                    "First-strict precommit cannot freeze an invalid checkpoint "
                    "execution identity.",
                    v,
                    source_v,
                    attempt_ckpt,
                )
            predicted_scope = _build_first_strict_control_execution_scope(
                v=v,
                candidate_name=candidate_name,
                code_fingerprint=code_fingerprint,
                opponents=opponents,
                precommit_plan=precommit_plan,
                evaluation_contract=evaluation_contract,
                workflow_run_id=workflow_run_id,
                checkpoint_revision=current_revision + 1,
                precommit_attempt=precommit_attempt,
            )
            audit_context_update = {
                _FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY: predicted_scope,
            }
        persisted_attempt = write_pipeline_checkpoint(
            v,
            source_v,
            current_stage,
            precommit_attempt=precommit_attempt,
            audit_context=audit_context_update,
            expected_checkpoint_revision=(
                (attempt_ckpt or {}).get("checkpoint_revision")
            ),
            expected_checkpoint_stage=(attempt_ckpt or {}).get("stage"),
            expected_workflow_run_id=(
                (attempt_ckpt or {}).get("workflow_run_id")
            ),
        )
        if not persisted_attempt:
            return _state_blocked(
                "Failed to persist the exact precommit attempt identity.",
                v,
                source_v,
                attempt_ckpt,
            )
        execution_ckpt = _matching_checkpoint(v, source_v)
        if system_control_plan:
            stored_scope = (
                ((execution_ckpt or {}).get("audit_context") or {}).get(
                    _FIRST_STRICT_CONTROL_EXECUTION_SCOPE_KEY
                )
            )
            if (
                not isinstance(execution_ckpt, dict)
                or execution_ckpt.get("checkpoint_revision")
                != predicted_scope["checkpoint_revision"]
                or stored_scope != predicted_scope
            ):
                return _state_blocked(
                    "First-strict precommit execution identity persistence "
                    "could not be re-proven.",
                    v,
                    source_v,
                    execution_ckpt,
                )
            control_execution_scope = predicted_scope

    if national_evaluation:
        return await _run_national_precommit_attempt(
            precommit_shutdown_token,
            v=v,
            source_v=source_v,
            requested_n_games=requested,
            effective_n_games=n_games,
            candidate_name=candidate_name,
            parent_name=parent_name,
            candidate_entry=candidate_entry,
            code_fingerprint=code_fingerprint,
            workflow_profile=workflow_profile,
            candidate_id=candidate_id,
            opponents=opponents,
            all_opponents=all_opponents,
            precommit_attempt=precommit_attempt,
            initial_blockers=blockers,
            started_at=_t0,
            precommit_plan=precommit_plan,
            evaluation_contract=evaluation_contract,
            workflow_run_id=str(
                (execution_ckpt or {}).get("workflow_run_id") or ""
            ),
            checkpoint_revision=int(
                (execution_ckpt or {}).get("checkpoint_revision") or 0
            ),
            control_execution_scope=control_execution_scope,
        )

# ──────────────────────────────────────────────
# Inline Eval
# ──────────────────────────────────────────────

@tool("run_inline_eval", "Run a non-authoritative diagnostic evaluation without modifying Glicko/H2H. The rating daemon is the only authoritative rating writer.", {"version": int, "n_games": int})
async def run_inline_eval(args):
    _inline_eval_start = time.time()
    v, _source_v = _resolve_version_args(args)
    if v is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Missing version and no active pipeline checkpoint"})}]}
    v = int(v)
    n_games = args.get("n_games", 5)
    bot_name = active_bot_name(v)

    _set_pipeline_status(f"Running inline eval for v{v}")

    bot_dir = get_bot_dir(v)

    from workflow_profiles import get_workflow_profile

    profile = get_workflow_profile()
    if getattr(profile, "national_execution_mode", None) != "native_tcp":
        return _json_tool_result({"error": "only native_tcp inline evaluation is supported"})
    expected_entry = bot_dir / "national_bot.py"
    if not expected_entry.exists():
        return {"content": [{"type": "text", "text": json.dumps({
            "error": f"Bot v{v} entry not found: {expected_entry.name}"
        })}]}

    # Guard: refuse to run while daemon is active (read-modify-write race on ratings)
    from daemon_management import daemon_proc, _daemon_lock
    with _daemon_lock:
        _dp = daemon_proc
    if _dp is not None and _dp.poll() is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Daemon is running. Stop it first with stop_daemon to avoid ratings race condition."})}]}

    active_bots = get_active_bots()
    opponents = [b for b in active_bots if b != bot_name]

    if getattr(profile, "national_execution_mode", None) == "native_tcp":
        from national_native import run_native_acceptance_for_candidate
        from evolution_infra import RESULTS_DIR
        from evaluation_data_identity import current_evaluation_digest
        from datetime import datetime as _dt

        acceptance = await run_native_acceptance_for_candidate(
            bot_dir,
            opponent_tokens=[get_bot_dir(parse_bot_version(name)) for name in opponents],
            hands=70,
            max_opponents=max(1, len(opponents)),
        )
        payload = acceptance.model_dump()
        payload.update({
            "authoritative": False,
            "ratings_updated": False,
            "h2h_updated": False,
            "evaluation_identity_digest": current_evaluation_digest(RESULTS_DIR),
            "source": "inline_native_diagnostic",
        })
        diagnostic_dir = RESULTS_DIR / "inline_eval_diagnostics"
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        diagnostic_path = diagnostic_dir / f"v{v}-{_dt.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
        diagnostic_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["diagnostic_path"] = str(diagnostic_path)
        return {"content": [{"type": "text", "text": json.dumps(payload, indent=2, ensure_ascii=False)}]}
