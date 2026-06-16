"""Pipeline tools: pre-commit evaluation and inline evaluation (battle-based)."""

import asyncio
import json
import logging
import os
import statistics
import sys
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

from logging_config import get_logger
log = get_logger("tool_eval")


# ──────────────────────────────────────────────
# Precommit eval tuning constants
# ──────────────────────────────────────────────
# Default and max n_games per opponent for precommit eval. 8 gives enough paired
# net-chip observations for the bootstrap gate; 16 is the hard ceiling so
# precommit eval still fits within the cycle budget.
PRECOMMIT_DEFAULT_N_GAMES = 8
PRECOMMIT_MAX_N_GAMES = 16

# Per-opponent parent gate: only block a losing W/L sample when paired net-chip
# bootstrap shows a severe candidate loss. Negative thresholds are chip means
# for bot0 (candidate) per completed mirror pair.
PARENT_NET_CHIPS_LOSS_THRESHOLD = -2000.0
AGGREGATE_NET_CHIPS_LOSS_THRESHOLD = -2000.0

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
    # Default is PRECOMMIT_DEFAULT_N_GAMES (8), clamped to [1,
    # PRECOMMIT_MAX_N_GAMES] (16). The regression gate now uses paired net-chip
    # bootstrap CIs, which are much less noisy than binary W/L at the same n_games.
    requested = int(args.get("n_games", PRECOMMIT_DEFAULT_N_GAMES) or PRECOMMIT_DEFAULT_N_GAMES)
    n_games = min(max(1, requested), PRECOMMIT_MAX_N_GAMES)

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

        try:
            submitted_ids = await scheduler_client.submit(jobs)
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
            poll_interval = 2.0
            collected_results = {}

            while time.time() < deadline:
                partial = await scheduler_client.collect(submitted_ids)
                collected_results.update(partial)
                if len(collected_results) >= len(submitted_ids):
                    break
                await asyncio.sleep(poll_interval)

            # Build matchups from scheduler results
            missing_opponents = []
            for job_id, item in job_id_to_opponent.items():
                opponent = item["name"]
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
                        blockers.append({
                            "reason": "scheduler_error",
                            "opponent": opponent,
                            "details": res["error"],
                        })
                    total_wins += matchup["wins"]
                    total_losses += matchup["losses"]
                    total_draws += matchup["draws"]
                    net_chips = list(matchup.get("net_chips") or [])
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
            """
            opponent = item["name"]
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
                            local = [int(net[0] if isinstance(net, tuple) else net) for net in mirror_battle_generator(
                                _cm, _om,
                                n_games=n_games,
                                verbose=False,
                                save_log=False,
                            )]
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
                    # LOWER bound is below the loss threshold (candidate loses
                    # >= PARENT_NET_CHIPS_LOSS_THRESHOLD chips per mirror pair).
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
    if agg_ci_upper is not None and agg_ci_upper < AGGREGATE_NET_CHIPS_LOSS_THRESHOLD:
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
    try:
        log_system_event("pipeline.precommit_eval", "info" if passed else "warn",
            f"Precommit eval {'passed' if passed else 'FAILED'} for v{v}: "
            f"{total_wins}W-{total_losses}L-{total_draws}D vs {len(all_opponents)} opponents",
            {"version": v, "source_v": source_v, "passed": passed,
             "total_wins": total_wins, "total_losses": total_losses,
             "total_draws": total_draws, "blockers": blockers,
             "paired_bootstrap": {
                 "aggregate_ci_lower": round(agg_ci_lower, 1) if agg_ci_lower is not None else None,
                 "aggregate_ci_upper": round(agg_ci_upper, 1) if agg_ci_upper is not None else None,
                 "aggregate_threshold": AGGREGATE_NET_CHIPS_LOSS_THRESHOLD,
                 "net_chips_samples": len(aggregate_net_chips),
                 "gate_degraded": len(aggregate_net_chips) == 0,
                 "net_chips_mean": round(sum(aggregate_net_chips)/len(aggregate_net_chips), 1) if aggregate_net_chips else None,
                 "net_chips_std": round(statistics.pstdev(aggregate_net_chips), 1) if len(aggregate_net_chips) > 1 else None,
                 "net_chips_min": round(min(aggregate_net_chips), 1) if aggregate_net_chips else None,
                 "net_chips_max": round(max(aggregate_net_chips), 1) if aggregate_net_chips else None,
             },
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
    }

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
        if precommit_attempt >= MAX_PRECOMMIT_RETRIES:
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
        entry["win_rate"] = round(entry.get("a_wins", 0) / g, 4) if g > 0 else 0.5
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
