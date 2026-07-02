"""Pipeline tools: pre-commit evaluation and inline evaluation (battle-based)."""

import asyncio
import json
import logging
import os
import statistics
import sys
import threading
import time
import uuid

from claude_agent_sdk import tool

from evolution_core import (
    get_bot_dir,
    get_active_bots,
    load_ratings,
    CORE_DIR,
)
from glicko2 import Glicko2Player, update_rating_period

from eval_stats import (
    paired_bootstrap_ci,
    confidence_sequence_ci,
    sequential_decision,
    NET_CHIPS_RANGE,
)

from tool_helpers import (
    _json_tool_result, _get_ui,
    _matching_checkpoint, _record_gate, _gate_payload, _state_blocked,
    _quality_gate_ok, _review_gate_ok, _critic_gate_ok,
    _select_precommit_opponents, _bot_main, _resolve_version_args,
    _set_pipeline_status,
)
from evolution_infra import write_pipeline_checkpoint, MAX_PRECOMMIT_RETRIES
from system_log import log_system_event
from daemon_management import is_daemon_scheduler_capable
from pipeline_schema import GateResult, ScoreCard
from workflow_profiles import get_workflow_profile

try:
    from candidate_store import append_candidate_event
except Exception:  # pragma: no cover
    append_candidate_event = None

from logging_config import get_logger
log = get_logger("tool_eval")


# Phase 4: opponent reasons whose matchups are TELEMETRY-ONLY (never block a
# commit, never contribute to the aggregate net-chips regression gate). The
# nemesis probe (Phase 3) and the PSRO MixtureBot meta-opponent (Phase 4) both
# degrade non-blocking so an early MixtureBot collapse cannot strand a commit.
_NONBLOCKING_REASONS = {"nemesis_probe", "psro_meta_opponent"}

# H1 (2026-06-29): thread-safe shutdown flag for precommit-eval cancellation.
# `loop.run_in_executor` submits mirror battles to the default ThreadPool; once
# running, the executor Future cannot be cancelled and the in-thread subprocess
# keeps spawning battles for up to per_game_timeout (observed: 49 stale battle
# procs, 5h daemon stall after CYCLE_TIMEOUT). This Event is checked between
# mirror games inside the drain functions (thread-safe `is_set()`) so an
# orchestrator CYCLE_TIMEOUT can abort the in-flight precommit promptly.
# Set by orchestrator via set_precommit_shutdown() on timeout/cancel.
_PRECOMMIT_SHUTDOWN = threading.Event()


def set_precommit_shutdown():
    """Signal in-flight precommit mirror battles to abort ASAP.

    Called by the orchestrator's CYCLE_TIMEOUT / CancelledError handler so
    subprocess-spawning drain loops break out instead of running to completion.
    Idempotent; reset_precommit_shutdown() clears it before the next cycle.
    """
    _PRECOMMIT_SHUTDOWN.set()


def reset_precommit_shutdown():
    """Clear the precommit shutdown flag (call at the start of each cycle)."""
    _PRECOMMIT_SHUTDOWN.clear()


def is_precommit_shutdown() -> bool:
    """True if precommit battles have been signalled to abort."""
    return _PRECOMMIT_SHUTDOWN.is_set()


def _is_nonblocking_reason(reason):
    """True if a matchup with this reason is telemetry-only (never blocks)."""
    return reason in _NONBLOCKING_REASONS


# Group A (root-cause-audit follow-up 2026-06-22): blocker reasons that indicate
# INFRASTRUCTURE failure (daemon crash / CPU contention / slow battle-MC), NOT a
# bot regression. These must NOT force the Orchestrator to rework worker code
# (which is unchanged and would give the same result) — they trigger an
# infra-aware retry with lower n_games instead. v147 timed out on attempt 1/2,
# passed on attempt 3 at n_games=6: the bot was fine, the infra wasn't.
_INFRA_BLOCKER_REASONS = {
    "match_timeout",           # mirror battle exceeded per_game_timeout
    "incomplete_or_timeout",   # n_played < n_games (partial completion)
    "scheduler_error",         # daemon returned error for this job
    "match_exception",         # battle raised (subprocess crash etc)
}


def _is_infra_blocker(reason):
    """True if this blocker reason is an infrastructure failure, not a bot
    regression. Infra blockers trigger retry-with-lower-n_games; regression
    blockers (lost_to_parent / aggregate_precommit_regression / semantic_regression)
    still hard-fail the gate."""
    return reason in _INFRA_BLOCKER_REASONS


# ──────────────────────────────────────────────
# Precommit eval tuning constants
# ──────────────────────────────────────────────
# Default and max n_games per opponent for precommit eval. 8 gives enough paired
# net-chip observations for the bootstrap gate; 16 is the hard ceiling so
# precommit eval still fits within the cycle budget.
PRECOMMIT_DEFAULT_N_GAMES = 8
PRECOMMIT_MIN_N_GAMES = 4
PRECOMMIT_MAX_N_GAMES = 16

# Per-opponent parent gate: only block a losing W/L sample when paired net-chip
# bootstrap shows a severe candidate loss. Negative thresholds are chip means
# for bot0 (candidate) per completed mirror pair.
PARENT_NET_CHIPS_LOSS_THRESHOLD = -2000.0
AGGREGATE_NET_CHIPS_LOSS_THRESHOLD = -2000.0
NEGATIVE_EV_MIN_SAMPLES = int(os.environ.get("POK_PRECOMMIT_NEG_EV_MIN_SAMPLES", "24"))
NEGATIVE_EV_MEAN_THRESHOLD = float(os.environ.get("POK_PRECOMMIT_NEG_EV_MEAN", "-250"))
NEGATIVE_EV_WIN_MARGIN_TOLERANCE = int(os.environ.get("POK_PRECOMMIT_NEG_EV_WIN_MARGIN", "2"))
CATASTROPHIC_LOSS_THRESHOLD = float(os.environ.get("POK_PRECOMMIT_CATASTROPHIC_LOSS", "-15000"))
CATASTROPHIC_LOSS_RATE_THRESHOLD = float(os.environ.get("POK_PRECOMMIT_CATASTROPHIC_RATE", "0.20"))

# ── Phase 2: Confidence Sequence sequential early-stop ──
# When True, the serial/gather fallback path consumes mirror_battle_generator
# incrementally and applies an anytime-valid Confidence Sequence to stop as soon
# as the parent-gate verdict (reject/continue) is confident — saving battle time
# on obvious regressions. When False, the path is byte-for-byte equivalent to the
# previous fixed-collect + mirror_battle implementation (zero-regression fallback).
PRECOMMIT_SEQUENTIAL_EARLY_STOP = True
CS_ALPHA = 0.05
# CS value range is imported from eval_stats (mirror net-chips ∈ [-40000, +40000]).
_CS_R = NET_CHIPS_RANGE


# ──────────────────────────────────────────────
# Phase 4: PSRO MixtureBot opponent (feature-flagged)
# ──────────────────────────────────────────────
MIXTURE_BOT_NAME = "mixture_main"
MIXTURE_MAX_POPULATION = 5  # max sub-bots in the mixture meta-distribution


def _maybe_add_mixture_opponent(v: int, source_v: int):
    """Build + persist a PSRO mixture_config.json and return the opponent entry,
    or None if anything is unavailable (PSRO MVP degrades silently to no-op).

    Returns a dict {"name": "mixture_main", "reason": "psro_meta_opponent"} so
    _bot_main("mixture_main") resolves the engine subprocess path. The
    matchup is telemetry-only (reason handled by _is_nonblocking_reason).
    """
    try:
        from pathlib import Path
        from evolution_infra import BOTS_DIR
        import psro_meta_solver as psro
        mixture_main = BOTS_DIR / MIXTURE_BOT_NAME / "main.py"
        if not mixture_main.exists():
            log.info("PSRO: mixture_main/main.py missing, skipping meta-opponent")
            return None
        # Load H2H + pick a small top population (excluding the candidate v).
        from tool_helpers import _load_h2h_data
        h2h = _load_h2h_data() or {}
        try:
            active = get_active_bots() or []
        except Exception:
            active = []
        candidate = f"claude_v{v}"
        population = [b for b in active if b != candidate][:MIXTURE_MAX_POPULATION]
        if len(population) < 2:
            log.info("PSRO: population < 2, skipping meta-opponent")
            return None
        # Resolve absolute main.py paths.
        bot_paths = {}
        for b in population:
            try:
                bv = int(str(b).replace("claude_v", ""))
            except (ValueError, TypeError):
                continue
            d = get_bot_dir(bv)
            m = d / "main.py"
            if m.exists():
                bot_paths[b] = str(m)
        if len(bot_paths) < 2:
            log.info("PSRO: <2 resolvable sub-bots, skipping meta-opponent")
            return None
        cfg = psro.build_mixture_config(
            h2h, list(bot_paths.keys()), bot_paths, method="fp", iterations=2000
        )
        if not cfg.get("strategy_weights"):
            log.info("PSRO: empty meta weights, skipping meta-opponent")
            return None
        # Persist config beside mixture_main/main.py (best-effort; failure -> skip).
        cfg_path = BOTS_DIR / MIXTURE_BOT_NAME / "mixture_config.json"
        try:
            from evolution_infra import write_locked_json
            write_locked_json(cfg_path, cfg)
        except Exception as e:
            log.warning("PSRO: mixture_config.json write failed: %s", e)
            return None
        log_system_event(
            "pipeline.psro_mixture_opponent_added", "info",
            f"v{v}: PSRO MixtureBot meta-opponent injected "
            f"({len(cfg['strategy_weights'])} sub-bots, weights={cfg['strategy_weights']})",
            {"version": v, "source_v": source_v,
             "weights": cfg["strategy_weights"]},
        )
        return {"name": MIXTURE_BOT_NAME, "reason": "psro_meta_opponent"}
    except Exception as e:
        log.warning("PSRO: mixture opponent setup failed: %s", e)
        return None


def _aggregate_ev_risk_blockers(
    *,
    total_wins: int,
    total_losses: int,
    total_draws: int,
    aggregate_net_chips: list,
    agg_ci_lower,
    agg_ci_upper,
    severe_regression_already: bool = False,
) -> tuple[list[dict], dict]:
    """Detect chip-EV regressions that binary W/L can hide."""
    samples = [float(x) for x in (aggregate_net_chips or [])]
    n = len(samples)
    total_decided = int(total_wins) + int(total_losses)
    mean = sum(samples) / n if n else None
    catastrophic = sum(1 for x in samples if x <= CATASTROPHIC_LOSS_THRESHOLD)
    catastrophic_rate = catastrophic / n if n else 0.0
    win_margin = int(total_wins) - int(total_losses)
    payload = {
        "samples": n,
        "mean": round(mean, 1) if mean is not None else None,
        "ci_lower": round(agg_ci_lower, 1) if agg_ci_lower is not None else None,
        "ci_upper": round(agg_ci_upper, 1) if agg_ci_upper is not None else None,
        "win_margin": win_margin,
        "total_decided": total_decided,
        "negative_ev_mean_threshold": NEGATIVE_EV_MEAN_THRESHOLD,
        "negative_ev_min_samples": NEGATIVE_EV_MIN_SAMPLES,
        "negative_ev_win_margin_tolerance": NEGATIVE_EV_WIN_MARGIN_TOLERANCE,
        "catastrophic_loss_threshold": CATASTROPHIC_LOSS_THRESHOLD,
        "catastrophic_loss_count": catastrophic,
        "catastrophic_loss_rate": round(catastrophic_rate, 3),
        "catastrophic_loss_rate_threshold": CATASTROPHIC_LOSS_RATE_THRESHOLD,
    }
    if severe_regression_already or n < NEGATIVE_EV_MIN_SAMPLES or mean is None:
        return [], payload

    blockers = []
    if mean < NEGATIVE_EV_MEAN_THRESHOLD and win_margin <= NEGATIVE_EV_WIN_MARGIN_TOLERANCE:
        ci_text = (
            f"CI=[{agg_ci_lower:.0f}, {agg_ci_upper:.0f}]"
            if agg_ci_lower is not None and agg_ci_upper is not None
            else "CI=unavailable"
        )
        blockers.append({
            "reason": "aggregate_negative_chip_ev",
            "details": (
                f"Aggregate W/L {total_wins}-{total_losses}-{total_draws} has only "
                f"{win_margin:+d} win margin but mean net chips {mean:.0f} per mirror pair "
                f"over {n} samples ({ci_text})."
            ),
        })
    if catastrophic_rate >= CATASTROPHIC_LOSS_RATE_THRESHOLD and mean < 0 and win_margin <= max(4, NEGATIVE_EV_WIN_MARGIN_TOLERANCE):
        blockers.append({
            "reason": "catastrophic_loss_rate",
            "details": (
                f"{catastrophic}/{n} mirror pairs ({catastrophic_rate:.0%}) lost "
                f"<={CATASTROPHIC_LOSS_THRESHOLD:.0f} chips while aggregate chip EV is negative."
            ),
        })
    return blockers, payload


# ──────────────────────────────────────────────
# Battle Scheduler Client
# ──────────────────────────────────────────────

class BattleSchedulerClient:
    """Async wrapper around the file-based battle_scheduler module.

    All blocking file operations are run in the default executor so that
    the event loop stays responsive.
    """

    def __init__(self):
        self._loop = asyncio.get_running_loop()

    async def is_available(self) -> bool:
        """Return True if the daemon was started with scheduler capability."""
        return await self._loop.run_in_executor(None, is_daemon_scheduler_capable)

    async def submit(self, jobs: list) -> list[str]:
        """Submit battle jobs to the scheduler queue.

        Returns the list of job_ids that were accepted.
        """
        import battle_scheduler
        return await self._loop.run_in_executor(
            None, lambda: battle_scheduler.submit_jobs(jobs)
        )

    async def collect(self, job_ids: list[str]) -> dict[str, dict]:
        """Collect results for the given job_ids.

        Returns a dict mapping job_id -> result dict.
        """
        import battle_scheduler
        return await self._loop.run_in_executor(
            None, lambda: battle_scheduler.collect_results(job_ids)
        )

    async def status(self, job_ids: list[str]) -> dict:
        """Peek scheduler queue state for the given job_ids without consuming results."""
        import battle_scheduler
        return await self._loop.run_in_executor(
            None, lambda: battle_scheduler.get_job_status(job_ids)
        )


def _job_attr(job, name: str, default=None):
    if isinstance(job, dict):
        return job.get(name, default)
    return getattr(job, name, default)


def _precommit_scheduler_job_details(
    submitted_ids: list[str],
    job_id_to_opponent: dict,
    jobs_by_id: dict,
    scheduler_status: dict | None,
    collected_results: dict | None,
    now: float | None = None,
) -> list[dict]:
    """Build per-job diagnostic details for scheduler wait/stall events."""
    now = time.time() if now is None else now
    scheduler_status = scheduler_status or {}
    collected_results = collected_results or {}
    state_by_id = {}
    for state in ("pending", "claimed", "completed", "missing"):
        for job_id in scheduler_status.get(state, []) or []:
            state_by_id[str(job_id)] = state

    details = []
    for job_id in submitted_ids:
        item = job_id_to_opponent.get(job_id, {}) or {}
        job = jobs_by_id.get(job_id)
        submitted_at = _job_attr(job, "submitted_at")
        age_sec = None
        if isinstance(submitted_at, (int, float)) and submitted_at > 0:
            age_sec = round(max(0.0, now - float(submitted_at)), 1)

        state = "collected" if job_id in collected_results else state_by_id.get(job_id, "unknown")
        detail = {
            "job_id": job_id,
            "opponent": item.get("name") or _job_attr(job, "bot_b_name"),
            "reason": item.get("reason"),
            "state": state,
            "age_sec": age_sec,
            "timeout_sec": _job_attr(job, "timeout_sec"),
            "n_games": _job_attr(job, "n_pairs"),
        }
        if job_id in collected_results:
            result = collected_results.get(job_id) or {}
            detail.update({
                "wins": result.get("wins_a"),
                "losses": result.get("wins_b"),
                "draws": result.get("draws"),
                "total": result.get("total"),
                "error": result.get("error"),
            })
        details.append(detail)
    return details


def _scheduler_status_excluding_collected(
    submitted_ids: list[str],
    scheduler_status: dict | None,
    collected_results: dict | None,
) -> dict:
    """Return scheduler status counts for jobs still awaiting collection.

    battle_scheduler.collect_results() removes collected records from
    battle_results.jsonl. A later get_job_status() therefore sees those job ids
    as "missing" unless the precommit caller subtracts its in-memory collected
    set. This normalizer keeps aggregate fields aligned with jobs[] details.
    """
    status = dict(scheduler_status or {})
    collected = {str(job_id) for job_id in (collected_results or {}).keys()}
    requested = [str(job_id) for job_id in submitted_ids]
    normalized = {}
    for state in ("pending", "claimed", "completed", "missing"):
        ids = [str(job_id) for job_id in (status.get(state, []) or []) if str(job_id) not in collected]
        normalized[state] = sorted(ids)
        normalized[f"{state}_count"] = len(ids)
    accounted = set(normalized["pending"]) | set(normalized["claimed"]) | set(normalized["completed"]) | set(normalized["missing"]) | collected
    truly_missing = sorted(job_id for job_id in requested if job_id not in accounted)
    if truly_missing:
        merged = sorted(set(normalized["missing"]) | set(truly_missing))
        normalized["missing"] = merged
        normalized["missing_count"] = len(merged)
    normalized["collected_count"] = len(collected)
    normalized["raw_missing_count"] = int(status.get("missing_count", 0) or 0)
    normalized["missing_unaccounted_count"] = normalized["missing_count"]
    normalized["raw_missing_before_collected_count"] = normalized["raw_missing_count"]
    return normalized


def _scheduler_stall_reason(
    *,
    collected_count: int,
    submitted_count: int,
    rounds_since_progress: int,
    pending_stall_rounds: int,
    missing_stall_rounds: int,
    pending_count: int,
    claimed_count: int,
    completed_count: int,
    scheduler_stall_rounds: int,
    claimed_job_stall_rounds: int,
) -> str:
    """Return the scheduler stall reason, or an empty string if it should keep waiting."""
    if collected_count >= submitted_count:
        return ""
    if missing_stall_rounds >= 3:
        return "jobs_missing_from_scheduler_files"
    if pending_count > 0 and pending_stall_rounds >= scheduler_stall_rounds:
        return "jobs_never_claimed"
    if (
        claimed_count == 0
        and pending_count == 0
        and completed_count == 0
        and rounds_since_progress >= scheduler_stall_rounds
    ):
        return "no_scheduler_activity"
    if claimed_count > 0 and rounds_since_progress >= claimed_job_stall_rounds:
        return "claimed_jobs_exceeded_grace"
    return ""


def _claimed_job_stall_rounds(n_games: int, poll_interval: float,
                              per_game_timeout: float,
                              poll_budget: float) -> int:
    """Grace window for claimed scheduler jobs before fallback.

    Claimed jobs are actively owned by the daemon, so the precommit caller should
    not duplicate them before the configured job timeout has a chance to elapse.
    Use the smaller of per-job timeout and the global poll budget, with a small
    cushion for file-lock and scheduling jitter.
    """
    grace_sec = min(float(per_game_timeout), float(poll_budget)) + max(
        60.0, float(n_games) * 15.0
    )
    return max(1, int(grace_sec / max(float(poll_interval), 0.1)))


# ──────────────────────────────────────────────
# Precommit Eval
# ──────────────────────────────────────────────


def _worst_precommit_opponent(matchups, blockers):
    """Return the opponent name most responsible for a precommit failure.

    Priority: the first blocker that names a regression opponent
    (lost_to_parent / lost_to_opponent), else the matchup with the most losses,
    else the matchup with the worst W-L margin. Returns "unknown" if there are
    no matchups and no named blockers.
    """
    if blockers:
        for b in blockers:
            reason = b.get("reason") if isinstance(b, dict) else None
            if reason in ("lost_to_parent", "lost_to_opponent"):
                opp = b.get("opponent")
                if opp:
                    return opp
    if matchups:
        best = None
        best_key = None
        for m in matchups:
            # Phase 3/4: skip telemetry-only matchups (nemesis_probe,
            # psro_meta_opponent) when attributing a failure — their losses are
            # telemetry-only and must not be surfaced as the "worst opponent" the
            # worker should target.
            if _is_nonblocking_reason(m.get("reason")):
                continue
            opp = m.get("opponent")
            losses = int(m.get("losses", 0) or 0)
            wins = int(m.get("wins", 0) or 0)
            # Sort by (most losses, then worst margin) so the heaviest defeat wins.
            key = (losses, losses - wins)
            if best_key is None or key > best_key:
                best_key = key
                best = opp
        if best is not None:
            return best
    return "unknown"


def _worst_wins_losses(matchups, opponent):
    """Return (wins, losses) for the given opponent across matchups, else (0, 0)."""
    if not opponent or opponent == "unknown" or not matchups:
        return 0, 0
    for m in matchups:
        if m.get("opponent") == opponent:
            return int(m.get("wins", 0) or 0), int(m.get("losses", 0) or 0)
    return 0, 0


@tool("run_precommit_eval", "Run a minimal mirror-battle regression check before commit. Tests parent, current top opponents, and source H2H weaknesses; blocks obvious crashes or collapses.", {"version": int, "source_v": int, "n_games": int})
async def run_precommit_eval(args):
    _t0 = time.time()
    v, source_v = _resolve_version_args(args)
    if v is None or source_v is None:
        return _json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)
    # Cap n_games: precommit eval is a quick regression check, NOT a full evaluation.
    # Default is PRECOMMIT_DEFAULT_N_GAMES (8), clamped to
    # [PRECOMMIT_MIN_N_GAMES, PRECOMMIT_MAX_N_GAMES]. The regression gate now uses paired net-chip
    # bootstrap CIs, which are much less noisy than binary W/L at the same n_games.
    requested = int(args.get("n_games", PRECOMMIT_DEFAULT_N_GAMES) or PRECOMMIT_DEFAULT_N_GAMES)
    n_games = min(max(PRECOMMIT_MIN_N_GAMES, requested), PRECOMMIT_MAX_N_GAMES)

    # A4: infra-aware n_games auto-reduction. If the previous precommit attempt
    # for this (v, source_v) timed out (infra blocker), halve n_games this
    # attempt so the mirror battle fits within the per_game_timeout window.
    # Floor at 4 so the paired-bootstrap CI still has >=4 observations. The
    # MAX_PRECOMMIT_RETRIES hard cap still bounds total attempts.
    _prev_ckpt_for_n = _matching_checkpoint(v, source_v)
    if _prev_ckpt_for_n:
        _prev_gate = _prev_ckpt_for_n.get("gate_results", {}).get("precommit_eval", {})
        _prev_had_timeout = any(
            _is_infra_blocker(b.get("reason"))
            for b in (_prev_gate.get("blockers") or [])
            if isinstance(b, dict)
        )
        if _prev_had_timeout and n_games > 4:
            n_games = max(4, n_games // 2)
            log.info(
                "v%s: previous precommit had infra timeout, auto-reducing n_games %d->%d",
                v, requested, n_games,
            )

    # Idempotency guard: skip if precommit eval already passed
    _precommit_ckpt = _matching_checkpoint(v, source_v)
    if _precommit_ckpt and _precommit_ckpt.get("stage") in (
        "verified", "archived"
    ):
        precommit_gate = _precommit_ckpt.get("gate_results", {}).get("precommit_eval", {})
        if precommit_gate.get("passed") is True:
            precommit_gate["idempotent_cache"] = True
            precommit_gate["directive"] = (
                "Precommit eval ALREADY PASSED. Do NOT re-run. "
                "Call commit_bot(version, source_v, strategy, review_approved=true) next."
            )
            return _json_tool_result(precommit_gate)

    _set_pipeline_status(f"Pre-commit eval for v{v}")

    candidate_name = f"claude_v{v}"
    parent_name = f"claude_v{source_v}"
    candidate_main = _bot_main(candidate_name)
    candidate_id = f"{candidate_name}_from_v{source_v}"
    workflow_profile = get_workflow_profile()
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
                parent_ids=[f"claude_v{source_v}"],
                gate="precommit_eval",
                metrics={"n_games": n_games},
            )
        except Exception as e:
            log.warning("candidate ledger precommit_started write failed: %s", e)
    blockers = []
    matchups = []

    ckpt = _matching_checkpoint(v, source_v)
    if not _quality_gate_ok(ckpt) or not _review_gate_ok(ckpt) or not _critic_gate_ok(ckpt):
        return _state_blocked(
            "run_precommit_eval requires passing quality, reviewer, and critic gates for the same version/source_v.",
            v,
            source_v,
            ckpt,
        )

    if not candidate_main.exists():
        result = {
            "version": v,
            "source_v": source_v,
            "n_games": n_games,
            "passed": False,
            "blockers": [{"reason": "candidate_missing", "details": str(candidate_main)}],
            "opponents": [],
            "matchups": [],
        }
        gate_extra = {k: val for k, val in result.items() if k not in {"version", "source_v", "passed"}}
        _record_gate(v, source_v, "precommit_eval", _gate_payload(v, source_v, False, **gate_extra), stage=None)
        return _json_tool_result(result)

    # compile/smoke already verified by quality gates (required by _quality_gate_ok above)

    opponents = _select_precommit_opponents(v, source_v)
    # Add crossover parent_b if applicable
    if ckpt and ckpt.get("parent2_v"):
        parent2_name = f"claude_v{ckpt['parent2_v']}"
        parent2_main = _bot_main(parent2_name)
        if parent2_main.exists() and not any(o["name"] == parent2_name for o in opponents):
            opponents.append({"name": parent2_name, "reason": "crossover_parent_b"})

    # Phase 4: PSRO MixtureBot meta-opponent (FEATURE FLAG, default OFF).
    # When PSRO_ENABLED, inject bots/mixture_main as a TELEMETRY-ONLY opponent
    # (reason="psro_meta_opponent", handled by _is_nonblocking_reason above so a
    # MixtureBot collapse can never block the commit). The engine sees
    # mixture_main as a standard subprocess bot on the bot1 side — ZERO change to
    # the 2-player Popen contract of engine/battle.py. OFF = this block is a
    # no-op, byte-identical precommit path. The mixture_config.json is written
    # here from a PSRO meta-solver over the top population + H2H payoff.
    try:
        from evolution_infra import PSRO_ENABLED
    except Exception:
        PSRO_ENABLED = False
    if PSRO_ENABLED:
        _mixture_opp = _maybe_add_mixture_opponent(v, source_v)
        if _mixture_opp is not None:
            opponents.append(_mixture_opp)

    if not opponents:
        blockers.append({"reason": "no_opponents", "details": "No parent/top/H2H opponents with main.py found."})
    all_opponents = list(opponents)  # preserve full list for result reporting

    # Increment precommit_attempt only when a real precommit battle round is
    # about to start. Idempotent already-verified calls, missing prerequisite
    # gates, missing candidates, and no-opponent preflight exits must not spend
    # an attempt because they do not evaluate the current bot code.
    precommit_attempt = int(ckpt.get("precommit_attempt", 0) or 0) if ckpt else 0
    if opponents:
        current_stage = ckpt.get("stage", "critic_checked") if ckpt else "critic_checked"
        precommit_attempt += 1
        write_pipeline_checkpoint(
            v,
            source_v,
            current_stage,
            precommit_attempt=precommit_attempt,
        )

    total_wins = 0
    total_losses = 0
    total_draws = 0
    aggregate_net_chips = []  # candidate net-chips per mirror pair, across all opponents
    _core = CORE_DIR  # imported unconditionally from evolution_core (line 18)
    sys.path.insert(0, str(_core.resolve()))
    from engine.battle import mirror_battle, mirror_battle_generator

    # ── Dual-path: Battle Scheduler vs Serial fallback ──
    scheduler_client = BattleSchedulerClient()
    _use_scheduler = await scheduler_client.is_available()

    if _use_scheduler and opponents:
        log_system_event(
            "pipeline.precommit_eval.scheduler_start", "info",
            f"v{v}: submitting {len(opponents)} opponent battle(s) to scheduler",
            {"version": v, "source_v": source_v, "opponents": [o['name'] for o in opponents], "n_games": n_games}
        )

        from battle_scheduler import BattleJob
        jobs = []
        job_id_to_opponent = {}
        for item in opponents:
            opponent = item["name"]
            opponent_main = _bot_main(opponent)
            job_id = str(uuid.uuid4())
            job_id_to_opponent[job_id] = item
            jobs.append(BattleJob(
                job_id=job_id,
                bot_a_name=candidate_name,
                bot_b_name=opponent,
                bot_a_path=str(candidate_main),
                bot_b_path=str(opponent_main),
                n_pairs=n_games,
                submitted_at=time.time(),
                submitted_by="precommit_eval",
                priority=1,
                timeout_sec=max(300, n_games * 120),
                update_ratings=False,
            ))
        jobs_by_id = {job.job_id: job for job in jobs}

        try:
            submitted_ids = await scheduler_client.submit(jobs)
            log_system_event(
                "pipeline.precommit_eval.scheduler_jobs_submitted", "info",
                f"v{v}: scheduler accepted {len(submitted_ids)}/{len(jobs)} precommit job(s)",
                {"version": v, "source_v": source_v,
                         "jobs": _precommit_scheduler_job_details(
                             submitted_ids,
                             job_id_to_opponent,
                             jobs_by_id,
                             {"pending": submitted_ids},
                             {},
                         ),
                         "poll_budget_sec": min(max(300, n_games * 120) * len(opponents), 1500)},
            )
        except Exception as exc:
            log_system_event(
                "pipeline.precommit_eval.scheduler_rejected", "warn",
                f"v{v}: scheduler submit failed ({exc}), falling back to serial",
                {"version": v, "source_v": source_v, "error": str(exc)[:200]}
            )
            _use_scheduler = False
            submitted_ids = []

        if _use_scheduler and submitted_ids:
            # Poll for results with deadline
            per_game_timeout = max(300, n_games * 120)
            deadline = time.time() + per_game_timeout * len(opponents)
            poll_interval = 5.0  # root-cause-audit 2026-06-21: 2s→5s 减少 fcntl 锁竞争（与 SCHEDULER_STALL_ROUNDS 联动，总等待窗不变）
            collected_results = {}

            # HARD wall-clock safety net (root-cause-audit 2026-06-26 / v193):
            # precommit previously had NO independent timeout — it relied solely
            # on the outer CYCLE_TIMEOUT (5400s/90min). When the daemon stalled
            # (process alive but scheduler not draining jobs; e.g. in_flight
            # emptied and the daemon's `while running and in_flight` loop exited),
            # `collect` returned {} forever and this loop spun for ~70min until
            # CYCLE_TIMEOUT killed the WHOLE generation — discarding code that
            # had already passed quality/review/critic. This independent budget
            # caps the scheduler-poll wall-clock so a stuck daemon degrades to
            # the parallel fallback instead of abandoning the generation.
            # Capped at 1500s (25min) so a healthy run is never cut short.
            PRECOMMIT_POLL_BUDGET = min(per_game_timeout * len(opponents), 1500)
            poll_budget_deadline = time.time() + PRECOMMIT_POLL_BUDGET

            # Circuit breaker: detect scheduler 'no progress' fast so we fall
            # back to the parallel path instead of waiting for the full budget.
            #
            # v193 root-cause (2026-06-26): the OLD breaker counted CONSECUTIVE
            # empty rounds (`consecutive_stall`), reset to 0 whenever `collect`
            # returned ANY non-empty dict. A half-dead daemon can occasionally
            # emit a stale/partial result (or `collect` can block on an fcntl
            # lock held by the daemon's heavy save_cycle I/O), which both
            # (a) never advances `len(collected_results)` and (b) keeps the old
            # counter pegged at 0 — so the breaker never tripped and the loop
            # spun until CYCLE_TIMEOUT.
            #
            # Fix: count rounds since the LAST REAL PROGRESS (`collected_results`
            # actually grew), not rounds since the last non-empty `collect`. A
            # collect() that returns already-collected job_ids, or that throws/
            # times out, counts as no-progress. The trip decision is now purely
            # progress-based and independent of daemon liveness probes.
            SCHEDULER_STALL_ROUNDS = max(24, n_games * 3)
            CLAIMED_JOB_STALL_ROUNDS = max(
                SCHEDULER_STALL_ROUNDS,
                _claimed_job_stall_rounds(
                    n_games, poll_interval, per_game_timeout, PRECOMMIT_POLL_BUDGET
                ),
            )
            rounds_since_progress = 0
            pending_stall_rounds = 0
            missing_stall_rounds = 0
            prev_collected_count = 0
            last_status_log = 0.0
            last_scheduler_status = {}
            # Cap each collect() call so a wedged fcntl lock cannot hang the
            # whole poll loop (battle_scheduler.collect_results takes an
            # exclusive LOCK_EX on battle_results.jsonl).
            COLLECT_CALL_TIMEOUT = 15.0

            while time.time() < deadline and time.time() < poll_budget_deadline:
                try:
                    partial = await asyncio.wait_for(
                        scheduler_client.collect(submitted_ids),
                        timeout=COLLECT_CALL_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    # collect() blocked (likely fcntl contention) — treat as
                    # no-progress this round rather than hanging the loop.
                    partial = None
                if partial:
                    collected_results.update(partial)
                if len(collected_results) > prev_collected_count:
                    prev_collected_count = len(collected_results)
                    rounds_since_progress = 0
                else:
                    rounds_since_progress += 1
                if len(collected_results) >= len(submitted_ids):
                    break
                try:
                    _raw_scheduler_status = await asyncio.wait_for(
                        scheduler_client.status(submitted_ids),
                        timeout=COLLECT_CALL_TIMEOUT,
                    )
                    last_scheduler_status = _scheduler_status_excluding_collected(
                        submitted_ids,
                        _raw_scheduler_status,
                        collected_results,
                    )
                except asyncio.TimeoutError:
                    last_scheduler_status = {}

                pending_count = int(last_scheduler_status.get("pending_count", 0) or 0)
                claimed_count = int(last_scheduler_status.get("claimed_count", 0) or 0)
                completed_count = int(last_scheduler_status.get("completed_count", 0) or 0)
                missing_count = int(last_scheduler_status.get("missing_count", 0) or 0)
                if claimed_count > 0:
                    pending_stall_rounds = 0
                    missing_stall_rounds = 0
                elif pending_count > 0:
                    pending_stall_rounds += 1
                    missing_stall_rounds = 0
                elif completed_count > 0:
                    pending_stall_rounds = 0
                    missing_stall_rounds = 0
                elif len(collected_results) < len(submitted_ids):
                    missing_stall_rounds += 1

                now_for_status = time.time()
                if (
                    rounds_since_progress > 0
                    and now_for_status - last_status_log >= 60
                    and len(collected_results) < len(submitted_ids)
                ):
                    job_details = _precommit_scheduler_job_details(
                        submitted_ids,
                        job_id_to_opponent,
                        jobs_by_id,
                        last_scheduler_status,
                        collected_results,
                        now=now_for_status,
                    )
                    log_system_event(
                        "pipeline.precommit_eval.scheduler_waiting", "info",
                        f"v{v}: waiting for scheduler results "
                        f"({len(collected_results)}/{len(submitted_ids)} collected; "
                        f"pending={pending_count}, claimed={claimed_count}, completed_peek={completed_count}, "
                        f"missing_unaccounted={missing_count}, raw_missing={last_scheduler_status.get('raw_missing_count')}, "
                        f"collected_mem={len(collected_results)})",
                        {"version": v, "source_v": source_v,
                         "collected": len(collected_results),
                         "submitted": len(submitted_ids),
                         "pending": pending_count,
                         "claimed": claimed_count,
                         "completed_peek": completed_count,
                         "missing": missing_count,
                         "missing_unaccounted": missing_count,
                         "raw_missing": last_scheduler_status.get("raw_missing_count"),
                         "raw_missing_before_collected": last_scheduler_status.get("raw_missing_before_collected_count"),
                         "jobs": job_details},
                    )
                    last_status_log = now_for_status

                stall_reason = _scheduler_stall_reason(
                    collected_count=len(collected_results),
                    submitted_count=len(submitted_ids),
                    rounds_since_progress=rounds_since_progress,
                    pending_stall_rounds=pending_stall_rounds,
                    missing_stall_rounds=missing_stall_rounds,
                    pending_count=pending_count,
                    claimed_count=claimed_count,
                    completed_count=completed_count,
                    scheduler_stall_rounds=SCHEDULER_STALL_ROUNDS,
                    claimed_job_stall_rounds=CLAIMED_JOB_STALL_ROUNDS,
                )
                if stall_reason:
                    # daemon liveness is logged for diagnosis only; the trip
                    # itself is decided by progress plus queue state. Claimed
                    # jobs are allowed a much larger grace window because real
                    # 8-pair mirror battles often take 7-10 minutes before the
                    # first result appears.
                    scheduler_healthy = await scheduler_client.is_available()
                    job_details = _precommit_scheduler_job_details(
                        submitted_ids,
                        job_id_to_opponent,
                        jobs_by_id,
                        last_scheduler_status,
                        collected_results,
                    )
                    log_system_event(
                        "pipeline.precommit_eval.scheduler_stall", "warn",
                        f"v{v}: scheduler no progress for {rounds_since_progress} rounds "
                        f"(~{rounds_since_progress * poll_interval:.0f}s, "
                        f"daemon_capable={scheduler_healthy}, reason={stall_reason}). "
                        f"Breaking to fallback.",
                        {"version": v, "source_v": source_v,
                         "collected": len(collected_results),
                         "submitted": len(submitted_ids),
                         "reason": stall_reason,
                         "scheduler_status": last_scheduler_status,
                         "jobs": job_details},
                    )
                    break
                await asyncio.sleep(poll_interval)

            # Hard-budget trip: time's up before all results collected → degrade
            # to fallback rather than letting CYCLE_TIMEOUT abandon the generation.
            if (time.time() >= poll_budget_deadline
                    and len(collected_results) < len(submitted_ids)):
                scheduler_healthy = await scheduler_client.is_available()
                job_details = _precommit_scheduler_job_details(
                    submitted_ids,
                    job_id_to_opponent,
                    jobs_by_id,
                    last_scheduler_status,
                    collected_results,
                )
                log_system_event(
                    "pipeline.precommit_eval.poll_budget_exceeded", "warn",
                    f"v{v}: precommit scheduler-poll hard budget ({PRECOMMIT_POLL_BUDGET:.0f}s) "
                    f"exceeded with {len(collected_results)}/{len(submitted_ids)} results "
                    f"(daemon_capable={scheduler_healthy}). Degrading to fallback.",
                    {"version": v, "source_v": source_v,
                     "collected": len(collected_results),
                     "submitted": len(submitted_ids),
                     "scheduler_status": last_scheduler_status,
                     "jobs": job_details},
                )

            # Build matchups from scheduler results
            missing_opponents = []
            for job_id, item in job_id_to_opponent.items():
                opponent = item["name"]
                _is_nemesis = _is_nonblocking_reason(item.get("reason"))
                if job_id in collected_results:
                    res = collected_results[job_id]
                    matchup = {
                        "opponent": opponent,
                        "reason": item["reason"],
                        "wins": int(res.get("wins_a", 0)),
                        "losses": int(res.get("wins_b", 0)),
                        "draws": int(res.get("draws", 0)),
                        "n_played": int(res.get("total", 0)),
                        # Scheduler results carry paired net-chips when produced by
                        # the updated daemon; default [] keeps old result records safe.
                        "net_chips": list(res.get("net_chips", [])),
                    }
                    if res.get("error"):
                        matchup["error"] = res["error"]
                        # Phase 3: nemesis probe is telemetry-only — never block.
                        if not _is_nemesis:
                            blockers.append({
                                "reason": "scheduler_error",
                                "opponent": opponent,
                                "details": res["error"],
                            })
                        else:
                            matchup["nemesis_note"] = "scheduler_error (non-blocking)"
                    # Phase 3: nemesis probe is excluded from aggregate_net_chips
                    # so a nemesis collapse cannot trip the aggregate regression
                    # gate. Its W/L still counts toward totals for telemetry.
                    if not _is_nemesis:
                        total_wins += matchup["wins"]
                        total_losses += matchup["losses"]
                        total_draws += matchup["draws"]
                    net_chips = list(matchup.get("net_chips") or [])
                    if not _is_nemesis:
                        aggregate_net_chips.extend(net_chips)
                    # P0-1 parent regression gate — keep scheduler semantics aligned
                    # with the serial path: use paired net-chips CI when available,
                    # and fall back to the older binary W/L ratio only when no
                    # net-chip observations exist.
                    if opponent == parent_name and matchup["wins"] < matchup["losses"]:
                        decided = matchup["wins"] + matchup["losses"]
                        nc_lo = None
                        nc_hi = None
                        if net_chips:
                            nc_lo, nc_hi = paired_bootstrap_ci(net_chips)
                            matchup["parent_net_chip_ci"] = [round(nc_lo, 1), round(nc_hi, 1)]
                        net_chips_block = (
                            nc_hi is not None
                            and nc_hi < PARENT_NET_CHIPS_LOSS_THRESHOLD
                        )
                        ratio_block = (
                            decided >= 4
                            and (matchup["losses"] / decided) >= 0.60
                        )
                        if net_chips_block or (not net_chips and ratio_block):
                            detail = f"{matchup['wins']}-{matchup['losses']}-{matchup['draws']} in {matchup['n_played']} games"
                            if nc_hi is not None:
                                detail += f"; net-chips CI=[{nc_lo:.0f}, {nc_hi:.0f}]"
                            blockers.append({
                                "reason": "lost_to_parent",
                                "opponent": opponent,
                                "details": detail,
                            })
                    matchups.append(matchup)
                else:
                    missing_opponents.append(item)

            if missing_opponents:
                log_system_event(
                    "pipeline.precommit_eval.scheduler_partial", "warn",
                    f"v{v}: {len(missing_opponents)}/{len(opponents)} scheduler results missing, falling back to serial",
                    {"version": v, "source_v": source_v, "missing": [o['name'] for o in missing_opponents]}
                )
                _use_scheduler = False
                opponents = missing_opponents
            else:
                log_system_event(
                    "pipeline.precommit_eval.scheduler_complete", "info",
                    f"v{v}: all {len(opponents)} scheduler results collected",
                    {"version": v, "source_v": source_v, "matchups": matchups}
                )
        else:
            _use_scheduler = False

    # ── Parallel fallback using asyncio.gather (replaces serial loop) ──
    if not _use_scheduler and opponents:
        if matchups:
            log_system_event(
                "pipeline.precommit_eval.fallback", "info",
                f"v{v}: running parallel fallback for {len(opponents)} missing opponent(s)",
                {"version": v, "source_v": source_v, "opponents": [o['name'] for o in opponents]}
            )
        else:
            log_system_event(
                "pipeline.precommit_eval.parallel_start", "info",
                f"v{v}: scheduler unavailable, running {len(opponents)} parallel mirror battle(s)",
                {"version": v, "source_v": source_v, "opponents": [o['name'] for o in opponents], "n_games": n_games}
            )

        # Semaphore caps concurrent subprocess battles to avoid CPU overwhelm.
        # Each mirror_battle spawns subprocesses, so they are truly parallel
        # (not GIL-bound), but too many concurrent battles saturate CPU.
        max_concurrent = min(len(opponents), os.cpu_count() or 8)
        _battle_sem = asyncio.Semaphore(max_concurrent)

        per_game_timeout = max(300, n_games * 120)
        loop = asyncio.get_running_loop()

        async def _run_single_mirror_battle(item):
            """Run one mirror battle in executor with per-opponent timeout.

            Returns a matchup dict with wins/losses/draws populated on success,
            or with 'error' key set on timeout/exception. Also returns any
            blockers as a list in the 'blockers' key.

            Phase 2: when PRECOMMIT_SEQUENTIAL_EARLY_STOP is True, the parent
            matchup is run via mirror_battle_generator and an anytime-valid
            Confidence Sequence gates the loop — DECIDE_REJECT (candidate
            significantly losing to the parent) breaks out early. Non-parent
            matchups use the generator too for uniform W/L reconstruction but
            do not early-stop (there is no accept-side business value and
            rejecting non-parents is not a gate). When the flag is False the
            original fixed-collect mirror_battle path runs unchanged.

            Phase 3: when item["reason"] == "nemesis_probe" the matchup is
            TELEMETRY-ONLY. Any blockers it would produce (timeout / exception /
            incomplete) are downgraded to a non-blocking 'nemesis_note' field on
            the matchup, so a nemesis collapse or scheduler hiccup cannot change
            the commit verdict. The aggregate-net-chips exclusion is applied in
            the outer aggregation loop below (which checks reason too).
            """
            opponent = item["name"]
            _is_nemesis = _is_nonblocking_reason(item.get("reason"))
            opponent_main = _bot_main(opponent)
            matchup = {
                "opponent": opponent,
                "reason": item["reason"],
                "wins": 0,
                "losses": 0,
                "draws": 0,
                "n_played": 0,
            }
            item_blockers = []
            # H1: if orchestrator signalled shutdown (CYCLE_TIMEOUT/cancel) before
            # this matchup even started, skip it entirely. Avoids spawning fresh
            # subprocesses for an already-aborted precommit round.
            if _PRECOMMIT_SHUTDOWN.is_set():
                item_blockers.append({
                    "reason": "precommit_shutdown",
                    "opponent": opponent,
                    "details": "precommit aborted by orchestrator shutdown signal",
                })
                matchup["blockers"] = item_blockers
                return matchup
            try:
                async with _battle_sem:
                    nc = []
                    cs_meta = None
                    early_stopped = False
                    if PRECOMMIT_SEQUENTIAL_EARLY_STOP and opponent == parent_name:
                        # Incremental generator + CS early-stop for the parent gate.
                        def _drain_parent(_cm=str(candidate_main), _om=str(opponent_main)):
                            local = []
                            last = None
                            for net in mirror_battle_generator(
                                _cm, _om,
                                n_games=n_games,
                                verbose=False,
                                save_log=False,
                            ):
                                # H1: abort in-flight precommit when orchestrator
                                # signalled shutdown (CYCLE_TIMEOUT/cancel). Returns
                                # partial results so the caller records a blocker
                                # instead of hanging on a dead subprocess pool.
                                if _PRECOMMIT_SHUTDOWN.is_set():
                                    break
                                # Generator yields a bare net_chips_0 int when
                                # aivat_enabled=False (the production default);
                                # a (raw, aivat) tuple when True. Unpack defensively
                                # so enabling the AIVAT stream later won't crash here.
                                local.append(int(net[0] if isinstance(net, tuple) else net))
                                last = sequential_decision(
                                    local,
                                    reject_threshold=PARENT_NET_CHIPS_LOSS_THRESHOLD,
                                    accept_threshold=None,
                                    alpha=CS_ALPHA,
                                    R=_CS_R,
                                )
                                if last["decision"] == "DECIDE_REJECT":
                                    return local, last, True
                            return local, last, False

                        battle_result = await asyncio.wait_for(
                            loop.run_in_executor(None, _drain_parent),
                            timeout=per_game_timeout,
                        )
                        nc, cs_meta, early_stopped = battle_result
                    elif PRECOMMIT_SEQUENTIAL_EARLY_STOP:
                        # Non-parent: still use the generator for W/L parity with
                        # the parent path, but run to completion (no early-stop).
                        def _drain_full(_cm=str(candidate_main), _om=str(opponent_main)):
                            # H1: explicit loop (not a list comprehension) so the
                            # thread-safe shutdown flag can break out between games.
                            local = []
                            for net in mirror_battle_generator(
                                _cm, _om,
                                n_games=n_games,
                                verbose=False,
                                save_log=False,
                            ):
                                if _PRECOMMIT_SHUTDOWN.is_set():
                                    break
                                local.append(int(net[0] if isinstance(net, tuple) else net))
                            return local, None, False

                        battle_result = await asyncio.wait_for(
                            loop.run_in_executor(None, _drain_full),
                            timeout=per_game_timeout,
                        )
                        nc, cs_meta, early_stopped = battle_result
                    else:
                        # Zero-regression fallback: identical to the original
                        # fixed-collect mirror_battle implementation.
                        battle_result = await asyncio.wait_for(
                            loop.run_in_executor(
                                None,
                                lambda _cm=str(candidate_main), _om=str(opponent_main): mirror_battle(
                                    _cm, _om,
                                    n_games=n_games,
                                    verbose=False,
                                    save_log=False,
                                ),
                            ),
                            timeout=per_game_timeout,
                        )
                        if len(battle_result) >= 5:
                            match_wins, draws, n_played, _logs, net_chips_list = battle_result
                        else:
                            # Backward-compatible only for tests that monkeypatch the old
                            # 4-tuple shape; real mirror_battle returns net_chips_list.
                            match_wins, draws, n_played, _logs = battle_result
                            net_chips_list = []
                        nc = [int(x) for x in (net_chips_list or [])]

                if PRECOMMIT_SEQUENTIAL_EARLY_STOP:
                    # Reconstruct W/L/D from the net-chips stream (generator does
                    # not return match_wins). Per mirror_battle: bot0 wins the
                    # pair iff net_chips_0 > 0, loses iff < 0, draw iff == 0.
                    wins = sum(1 for x in nc if x > 0)
                    losses = sum(1 for x in nc if x < 0)
                    draws = sum(1 for x in nc if x == 0)
                    match_wins = (wins, losses)
                    n_played = len(nc)
                    if cs_meta is not None:
                        matchup["cs_meta"] = {
                            "decision": cs_meta["decision"],
                            "ci_lo": cs_meta["ci_lo"],
                            "ci_hi": cs_meta["ci_hi"],
                            "half_width": cs_meta["half_width"],
                            "mean": cs_meta["mean"],
                            "n": cs_meta["n"],
                            "rule": cs_meta["rule"],
                            "early_stopped": early_stopped,
                        }
                    else:
                        matchup["cs_meta"] = None
                matchup.update({
                    "wins": int(match_wins[0]),
                    "losses": int(match_wins[1]),
                    "draws": int(draws),
                    "n_played": int(n_played),
                    "net_chips": nc,
                })
                # Early-stop on the parent does NOT count as incomplete: we chose
                # to stop because the verdict was already confident. Only a real
                # shortfall below n_games (timeout/crash) is incomplete.
                if (not early_stopped) and n_played < n_games:
                    item_blockers.append({
                        "reason": "incomplete_or_timeout",
                        "opponent": opponent,
                        "details": f"Only {n_played}/{n_games} mirror pairs completed.",
                    })
                if opponent == parent_name and matchup["wins"] < matchup["losses"]:
                    # Parent is the true regression baseline. Primary gate is a
                    # paired net-chips bootstrap 95% CI: block only when the CI
                    # UPPER bound is below the loss threshold, meaning the whole
                    # interval is confidently worse than the allowed loss floor.
                    # The old binary W/L ratio gate stays as a fallback when no
                    # net-chip observations are available.
                    decided = matchup["wins"] + matchup["losses"]
                    nc_lo = None
                    nc_hi = None
                    if nc:
                        nc_lo, nc_hi = paired_bootstrap_ci(nc)
                        matchup["parent_net_chip_ci"] = [round(nc_lo, 1), round(nc_hi, 1)]
                    # Block only when the CI UPPER bound is below threshold — i.e. we
                    # are 95% confident the mean per-pair net-chips deficit exceeds the
                    # threshold. Using the upper bound (not lower) avoids a single
                    # extreme all-in loss spuriously tripping the gate: NLHE net-chips
                    # are heavy-tailed, so a lower-bound test would reintroduce the
                    # noise-fail the paired-bootstrap was meant to eliminate.
                    if early_stopped and cs_meta is not None:
                        # CS already decided REJECT (ci_hi < threshold) with an
                        # anytime-valid guarantee — treat it as a confirmed regression.
                        net_chips_block = cs_meta["decision"] == "DECIDE_REJECT"
                    else:
                        net_chips_block = (
                            nc_hi is not None
                            and nc_hi < PARENT_NET_CHIPS_LOSS_THRESHOLD
                        )
                    ratio_block = (
                        decided >= 4
                        and (matchup["losses"] / decided) >= 0.60
                    )
                    if net_chips_block or (not nc and ratio_block):
                        detail = f"{matchup['wins']}-{matchup['losses']}-{matchup['draws']} in {matchup['n_played']} games"
                        if nc_hi is not None:
                            detail += f"; net-chips CI=[{nc_lo:.0f}, {nc_hi:.0f}]"
                        if early_stopped:
                            detail += f"; CS early-stop @ n={cs_meta['n']} ({cs_meta['rule']})"
                        item_blockers.append({
                            "reason": "lost_to_parent",
                            "opponent": opponent,
                            "details": detail,
                        })
                    else:
                        _get_ui().log_history(
                            f"⚠️ Lost to parent ({matchup['wins']}-{matchup['losses']}) "
                            f"but net-chips CI upper={nc_hi if nc_hi is not None else 'n/a'} "
                            f"above gate ({PARENT_NET_CHIPS_LOSS_THRESHOLD}) — not blocking",
                            "warn"
                        )
            except asyncio.TimeoutError:
                matchup["error"] = f"Mirror battle timed out ({per_game_timeout}s limit)"
                item_blockers.append({
                    "reason": "match_timeout",
                    "opponent": opponent,
                    "details": f"Mirror battle against {opponent} exceeded {per_game_timeout}s timeout",
                })
            except Exception as exc:
                matchup["error"] = str(exc)[:500]
                item_blockers.append({
                    "reason": "match_exception",
                    "opponent": opponent,
                    "details": str(exc)[:500],
                })
            # Phase 3: nemesis_probe is telemetry-only — downgrade any blockers
            # it produced into a non-blocking note so they cannot flip the commit
            # verdict. The aggregate-net-chips exclusion happens in the loop below
            # (it also keys off reason == "nemesis_probe").
            if _is_nemesis and item_blockers:
                matchup["nemesis_note"] = "; ".join(
                    f"{b.get('reason')}" for b in item_blockers
                )[:300]
                item_blockers = []
            matchup["blockers"] = item_blockers
            return matchup

        # Launch all opponents in parallel via gather
        matchup_results = await asyncio.gather(
            *[_run_single_mirror_battle(item) for item in opponents]
        )

        # Aggregate results from all parallel matchups
        for matchup in matchup_results:
            item_blockers = matchup.pop("blockers", [])
            blockers.extend(item_blockers)
            _is_nemesis = _is_nonblocking_reason(matchup.get("reason"))
            # Phase 3: nemesis W/L/net-chips stay on the matchup dict for
            # telemetry but are NOT pooled into the gate totals or the
            # aggregate regression bootstrap (a nemesis collapse must not be
            # able to trip the commit gate).
            if not _is_nemesis:
                total_wins += matchup["wins"]
                total_losses += matchup["losses"]
                total_draws += matchup["draws"]
                net_chips = list(matchup.get("net_chips") or [])
                aggregate_net_chips.extend(net_chips)
            matchups.append(matchup)

    # --- P0-4: Semantic Interpretation of Battle Results ---
    semantic_result = None
    if matchups:
        try:
            from audit_agents import _run_precommit_semantic
            ckpt_sem = _matching_checkpoint(v, source_v)
            master_plan_sem = ckpt_sem.get("master_plan", {}) if ckpt_sem else {}
            semantic_result = await _run_precommit_semantic(
                v, source_v, matchups, master_plan_sem, _get_ui()
            )
        except Exception as e:
            log.warning("Precommit semantic analysis failed: %s", e)

    # Aggregate regression gate. Primary gate is a paired net-chips bootstrap 95%
    # CI over all opponents' mirror pairs: block only when the CI UPPER bound is
    # below AGGREGATE_NET_CHIPS_LOSS_THRESHOLD (i.e. we are 95% confident the
    # mean per-pair deficit exceeds the threshold). Using upper-bound (not
    # lower) avoids heavy-tail false positives from single all-in outliers.
    # The old binary W/L margin gate stays as a fallback when net-chip
    # observations are unavailable (e.g. older daemon/scheduler results).
    total_decided = total_wins + total_losses
    agg_ci_lower = None
    agg_ci_upper = None
    if aggregate_net_chips:
        agg_ci_lower, agg_ci_upper = paired_bootstrap_ci(aggregate_net_chips)
    severe_aggregate_regression = (
        agg_ci_upper is not None
        and agg_ci_upper < AGGREGATE_NET_CHIPS_LOSS_THRESHOLD
    )
    if severe_aggregate_regression:
        blockers.append({
            "reason": "aggregate_precommit_regression",
            "details": (
                f"Aggregate mirror result {total_wins}-{total_losses}-{total_draws}; "
                f"net-chips CI=[{agg_ci_lower:.0f}, {agg_ci_upper:.0f}]."
            ),
        })
    elif (not aggregate_net_chips
          and total_decided >= 8
          and total_losses >= total_wins + 2):
        blockers.append({
            "reason": "aggregate_precommit_regression",
            "details": f"Aggregate mirror result {total_wins}-{total_losses}-{total_draws}.",
        })
    ev_blockers, ev_risk_payload = _aggregate_ev_risk_blockers(
        total_wins=total_wins,
        total_losses=total_losses,
        total_draws=total_draws,
        aggregate_net_chips=aggregate_net_chips,
        agg_ci_lower=agg_ci_lower,
        agg_ci_upper=agg_ci_upper,
        severe_regression_already=severe_aggregate_regression,
    )
    blockers.extend(ev_blockers)

    paired_bootstrap_payload = {
        "aggregate_ci_lower": round(agg_ci_lower, 1) if agg_ci_lower is not None else None,
        "aggregate_ci_upper": round(agg_ci_upper, 1) if agg_ci_upper is not None else None,
        "aggregate_threshold": AGGREGATE_NET_CHIPS_LOSS_THRESHOLD,
        "aggregate_gate_bound": round(agg_ci_upper, 1) if agg_ci_upper is not None else None,
        "aggregate_gate_rule": "block_if_ci_upper_below_threshold",
        "net_chips_samples": len(aggregate_net_chips),
        "gate_degraded": len(aggregate_net_chips) == 0,
        "net_chips_mean": round(sum(aggregate_net_chips)/len(aggregate_net_chips), 1) if aggregate_net_chips else None,
        "net_chips_std": round(statistics.pstdev(aggregate_net_chips), 1) if len(aggregate_net_chips) > 1 else None,
        "net_chips_min": round(min(aggregate_net_chips), 1) if aggregate_net_chips else None,
        "net_chips_max": round(max(aggregate_net_chips), 1) if aggregate_net_chips else None,
        "ev_risk": ev_risk_payload,
    }

    # P0-4: Semantic blocker — LLM detects regression patterns that numbers miss
    if semantic_result and semantic_result.get("recommended_action") == "block":
        blockers.append({
            "reason": "semantic_regression",
            "details": semantic_result.get("regression_semantics", "LLM detected regression pattern"),
        })
    elif semantic_result and semantic_result.get("recommended_action") == "caution":
        log_system_event("pipeline.precommit_caution", "warn",
                         f"Semantic caution for v{v}: {semantic_result.get('win_pattern_analysis', '')[:200]}",
                         {"version": v, "semantic": semantic_result})

    passed = len(blockers) == 0
    # Group A: classify blockers so the FAILED directive can distinguish
    # INFRASTRUCTURE timeouts from real bot regressions. `passed` semantics are
    # UNCHANGED (any blocker still fails the commit gate) — only the directive
    # text + next-attempt n_games auto-reduction behave differently.
    regression_blockers = [b for b in blockers if not _is_infra_blocker(b.get("reason"))]
    infra_blockers = [b for b in blockers if _is_infra_blocker(b.get("reason"))]
    infra_only_timeout = (not passed) and (not regression_blockers) and bool(infra_blockers)
    try:
        log_system_event("pipeline.precommit_eval", "info" if passed else "warn",
            f"Precommit eval {'passed' if passed else 'FAILED'} for v{v}: "
            f"{total_wins}W-{total_losses}L-{total_draws}D vs {len(all_opponents)} opponents",
            {"version": v, "source_v": source_v, "passed": passed,
             "total_wins": total_wins, "total_losses": total_losses,
             "total_draws": total_draws, "blockers": blockers,
             "paired_bootstrap": paired_bootstrap_payload,
             "n_opponents": len(all_opponents),
             "elapsed_sec": round(time.time() - _t0, 2)})
    except Exception:
        pass
    result = {
        "version": v,
        "source_v": source_v,
        "n_games": n_games,
        "opponents": all_opponents,
        "matchups": matchups,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "total_draws": total_draws,
        "passed": passed,
        "blockers": blockers,
        "paired_bootstrap": paired_bootstrap_payload,
    }
    scorecard = ScoreCard(
        name="precommit_eval",
        primary_score=paired_bootstrap_payload.get("net_chips_mean"),
        metrics={
            "total_wins": total_wins,
            "total_losses": total_losses,
            "total_draws": total_draws,
            "n_opponents": len(all_opponents),
            "n_games": n_games,
        },
    )
    scorecard.add(GateResult.from_bool(
        "precommit_regression",
        passed,
        metrics=paired_bootstrap_payload,
        failures=[str(b)[:500] for b in blockers],
    ))
    result["scorecard"] = scorecard.model_dump()

    # A6 (research_governance, evolution-plan-refresh-jun21): feed the precommit
    # outcome back into any web-derived candidates applied to this bot version, and
    # trigger a retrieval cooldown if a web-injected gen FAILED (Ratchet anti-pollution).
    # fix-5: infra_only_timeout must NOT count as a real fail — an infra timeout
    # (daemon crash / CPU contention / battle-MC) is not a bot regression. The raw
    # `passed` is False for infra timeouts, but the bot code is unproven (not weak).
    # Adjusted pass: True if passed OR if the only blockers were infra timeouts.
    _actual_pass = passed or infra_only_timeout
    try:
        from research_governance import record_precommit_outcome
        record_precommit_outcome(v, _actual_pass, next_v=v)
    except Exception:
        pass

    # ── Task B: FAILED directive ──
    # When precommit fails, tell the Orchestrator exactly what to do next:
    #   - below the retry limit → rework the bot (call execute_workers) OR
    #     abandon the generation. Retrying precommit alone is pointless because
    #     the bot code is unchanged.
    #   - at/above the retry limit → hard-stop: abandon the generation.
    # We surface the worst matchup (most losses) so the worker feedback can
    # target the specific losing line.
    if not passed:
        worst_opponent = _worst_precommit_opponent(matchups, blockers)
        worst_wins, worst_losses = _worst_wins_losses(matchups, worst_opponent)
        if infra_only_timeout:
            # Infrastructure timeout (daemon/CPU/battle-MC), NOT a bot regression.
            # Bot code is unchanged and unproven weak — retry precommit (A4
            # auto-reduces n_games next call). Do NOT rework the bot or abandon.
            result["directive"] = (
                f"Precommit TIMED OUT (attempt {precommit_attempt}/{MAX_PRECOMMIT_RETRIES}) — "
                f"this is an INFRASTRUCTURE failure (daemon/CPU/battle-MC), NOT a bot regression. "
                f"The bot code is UNCHANGED and has NOT been proven weak. "
                f"CALL run_precommit_eval AGAIN (it auto-reduces n_games). "
                f"Do NOT rework the bot or abandon the generation."
            )
            result["infra_retry"] = True
            log_system_event(
                "pipeline.precommit_infra_timeout", "warn",
                f"v{v}: precommit infra timeout (attempt {precommit_attempt}/{MAX_PRECOMMIT_RETRIES}) "
                f"— {len(infra_blockers)} infra blocker(s), 0 regression. Retry with lower n_games.",
                {"version": v, "source_v": source_v,
                 "precommit_attempt": precommit_attempt,
                 "infra_blockers": [b.get("reason") for b in infra_blockers]},
            )
        elif precommit_attempt >= MAX_PRECOMMIT_RETRIES:
            result["directive"] = (
                f"PRECOMMIT HARD LIMIT REACHED ({MAX_PRECOMMIT_RETRIES}/{MAX_PRECOMMIT_RETRIES} attempts). "
                f"The current bot cannot pass precommit. Do NOT retry precommit or workers. "
                f"Abandon this generation (the pipeline will reset on the next cycle with a new master plan)."
            )
            log_system_event(
                "pipeline.precommit_hard_limit",
                "warn",
                f"v{v}: precommit hard limit reached ({MAX_PRECOMMIT_RETRIES}/{MAX_PRECOMMIT_RETRIES}); "
                f"abandoning generation vs worst opponent {worst_opponent}",
                {
                    "version": v,
                    "source_v": source_v,
                    "precommit_attempt": precommit_attempt,
                    "max_retries": MAX_PRECOMMIT_RETRIES,
                    "worst_opponent": worst_opponent,
                    "total_wins": total_wins,
                    "total_losses": total_losses,
                    "blockers": blockers,
                },
            )
        else:
            result["directive"] = (
                f"Precommit FAILED (attempt {precommit_attempt}/{MAX_PRECOMMIT_RETRIES}) — "
                f"bot code is UNCHANGED since the last attempt, so retrying precommit will give the SAME result. "
                f"Do NOT call run_precommit_eval again. You MUST either (a) rework the bot: call execute_workers "
                f"with reviewer_feedback explaining the loss vs {worst_opponent} "
                f"({worst_wins}W-{worst_losses}L), targeting that matchup; or (b) abandon this generation "
                f"and start fresh from a different direction."
            )
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
        stage="verified" if passed else None,
    )
    result["checkpoint_recorded"] = checkpoint_recorded
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
                parent_ids=[f"claude_v{source_v}"],
                gate="precommit_eval",
                scorecard=scorecard,
                gate_results=scorecard.gates,
                metrics={
                    "passed": passed,
                    "total_wins": total_wins,
                    "total_losses": total_losses,
                    "total_draws": total_draws,
                    "net_chips_mean": paired_bootstrap_payload.get("net_chips_mean"),
                },
                failures=[str(b)[:500] for b in blockers],
                failure_class="" if passed else ("infra_timeout" if infra_only_timeout else "precommit_regression"),
            )
        except Exception as e:
            log.warning("candidate ledger precommit_finished write failed: %s", e)
    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Inline Eval
# ──────────────────────────────────────────────

@tool("run_inline_eval", "Run inline evaluation: battle the bot against all active opponents and update Glicko-2 ratings. Use when daemon is not running.", {"version": int, "n_games": int})
async def run_inline_eval(args):
    _inline_eval_start = time.time()
    v, _source_v = _resolve_version_args(args)
    if v is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Missing version and no active pipeline checkpoint"})}]}
    v = int(v)
    n_games = args.get("n_games", 5)
    bot_name = f"claude_v{v}"

    _set_pipeline_status(f"Running inline eval for v{v}")

    bot_dir = get_bot_dir(v)

    if not (bot_dir / "main.py").exists():
        return {"content": [{"type": "text", "text": json.dumps({"error": f"Bot v{v} main.py not found"})}]}

    # Guard: refuse to run while daemon is active (read-modify-write race on ratings)
    from daemon_management import daemon_proc, _daemon_lock
    with _daemon_lock:
        _dp = daemon_proc
    if _dp is not None and _dp.poll() is None:
        return {"content": [{"type": "text", "text": json.dumps({"error": "Daemon is running. Stop it first with stop_daemon to avoid ratings race condition."})}]}

    # Import battle engine
    _core = CORE_DIR  # imported unconditionally from evolution_core (line 18)
    sys.path.insert(0, str(_core.resolve()))
    from engine.battle import mirror_battle

    ratings = load_ratings()
    active_bots = get_active_bots()
    opponents = [b for b in active_bots if b != bot_name]

    if bot_name not in ratings:
        ratings[bot_name] = Glicko2Player()

    results_summary = []
    all_results = []

    from evolution_infra import (
        RATINGS_FILE, H2H_FILE, BOT_STATS_FILE, MATCH_HISTORY_FILE, RESULTS_DIR,
        locked_file, pair_key, read_locked_json, write_locked_json, update_h2h, update_bot_stats,
    )
    h2h = read_locked_json(H2H_FILE, default={})
    bot_stats_data = read_locked_json(BOT_STATS_FILE, default={})

    for opp in opponents:
        if opp not in ratings:
            ratings[opp] = Glicko2Player()
        loop = asyncio.get_running_loop()
        battle_result = await loop.run_in_executor(
            None,
            lambda _b=str(_bot_main(bot_name)), _o=str(_bot_main(opp)): mirror_battle(
                _b, _o, n_games=n_games, verbose=False, save_log=False,
            ),
        )
        if len(battle_result) >= 5:
            match_wins, draws, n_played, _, _net_chips_list = battle_result
        else:
            match_wins, draws, n_played, _ = battle_result
        w_a, w_b = match_wins[0], match_wins[1]
        total = w_a + w_b + draws
        results_summary.append({"opponent": opp, "wins": w_a, "losses": w_b, "draws": draws})

        # Update H2H
        update_h2h(h2h, bot_name, opp, w_a, w_b, draws=draws)

        # Update bot_stats
        update_bot_stats(bot_stats_data, bot_name, w_a, w_b, draws=draws)
        update_bot_stats(bot_stats_data, opp, w_b, w_a, draws=draws)

        # Append to match_history
        try:
            from datetime import datetime
            summary = {
                "id": f"inline_v{v}_vs_{opp}",
                "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S_%f"),
                "bot0": bot_name,
                "bot1": opp,
                "bot0_wins": w_a,
                "bot1_wins": w_b,
                "draws": draws,
            }
            with locked_file(MATCH_HISTORY_FILE, "a") as f:
                f.write(json.dumps(summary) + "\n")
        except Exception as e:
            log.warning("Match history write failed: %s", e)

        for _ in range(w_a):
            all_results.append((ratings[opp], 1.0))
        for _ in range(w_b):
            all_results.append((ratings[opp], 0.0))
        for _ in range(draws):
            all_results.append((ratings[opp], 0.5))

    if all_results:
        ratings[bot_name] = update_rating_period(ratings[bot_name], all_results)

    # Save updated ratings (atomic write — consistent with daemon)
    from datetime import datetime as _dt
    data = {}
    for name, p in ratings.items():
        d = p.to_dict()
        d["last_period"] = _dt.now().isoformat(timespec="seconds")
        data[name] = d
    write_locked_json(RATINGS_FILE, data)

    # Append rating history snapshot (consistent with daemon save_ratings)
    history_file = RESULTS_DIR / "rating_history.jsonl"
    snapshot = {
        "period": f"inline_v{v}",
        "timestamp": _dt.now().isoformat(timespec="seconds"),
        "ratings": {name: {"r": p.r, "rd": p.rd} for name, p in ratings.items()},
        "source": "inline_eval",
    }
    with locked_file(history_file, "a") as f:
        f.write(json.dumps(snapshot) + "\n")

    # Save H2H with win_rate computed
    h2h_out = {}
    for k, h2h_entry in h2h.items():
        entry = dict(h2h_entry)
        g = entry.get("games", 0)
        entry["win_rate"] = round((entry.get("a_wins", 0) + 0.5 * entry.get("draws", 0)) / g, 4) if g > 0 else 0.5
        h2h_out[k] = entry
    write_locked_json(H2H_FILE, h2h_out)

    # Save bot_stats
    write_locked_json(BOT_STATS_FILE, bot_stats_data)

    try:
        from system_log import log_system_event
        log_system_event('pipeline.inline_eval', 'info',
            f'Inline eval for v{v}',
            {'version': v, 'elapsed_sec': round(time.time() - _inline_eval_start, 1),
             'opponents_played': len(opponents), 'games_per_opponent': n_games,
             'rating': round(ratings[bot_name].r, 1), 'rd': round(ratings[bot_name].rd, 1)})
    except Exception:
        pass

    result = {
        "version": v,
        "opponents_played": len(opponents),
        "games_per_opponent": n_games,
        "results": results_summary,
        "updated_rating": {"r": round(ratings[bot_name].r, 1), "rd": round(ratings[bot_name].rd, 1)},
    }
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}
