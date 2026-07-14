"""Bot lifecycle management: MCP reaping/abandonment and guarded cleanup."""

import fcntl
import json
import shutil
import time
from typing import Annotated, TypedDict

from bot_namespace import bot_name, parse_bot_version
from tool_runtime_guard import tool

from evolution_core import (
    get_active_bots, get_bot_dir, find_current_v, find_latest_active_v, load_ratings,
    clear_pipeline_checkpoint, git_has_tag, git_dir_is_committed,
    find_max_committed_v, find_abandoned_version_floor, compute_next_generation_v,
    MAX_ACTIVE_BOTS, RESULTS_DIR, REPLAY_DIR,
    Glicko2Player,
)
from tool_helpers import (
    _get_ui, load_h2h_avg_winrates, load_strength_scores, PROJECT_ROOT,
)
from system_log import log_system_event

from evolution_infra import (
    MAX_PRECOMMIT_RETRIES,
    BOTS_DIR,
    append_locked_jsonl,
    read_pipeline_checkpoint,
    record_reaped_bot,
)
from pipeline_state import generic_abandon_block

# A4 (2026-06-30): rate-limit state for abandon_generation. [timestamp, reason].
_LAST_ABANDON_TS = [0.0, ""]


def _generic_abandon_stage_block(checkpoint, reason):
    """Return a state-machine refusal payload for unsafe generic abandons."""
    return generic_abandon_block(
        checkpoint,
        reason=reason,
        max_precommit_retries=MAX_PRECOMMIT_RETRIES,
    )


class ReapWeakestInput(TypedDict):
    pass


async def _do_reap_weakest(quiet: bool = False) -> dict:
    """Core reaping logic — callable directly (not via MCP)."""
    active_bots = get_active_bots()
    if len(active_bots) <= MAX_ACTIVE_BOTS:
        return {"reaped": False, "pool_size": len(active_bots)}

    ratings = load_ratings()
    h2h_winrates = load_h2h_avg_winrates()
    strength_scores = load_strength_scores()
    current_bot = bot_name(find_latest_active_v())

    # Load bot stats to protect untested bots from reaping
    from tool_helpers import _read_json
    bot_stats = _read_json(PROJECT_ROOT / "web" / "core" / "results" / "bot_stats.json", {})

    # Exclude the current/latest source and the newest few active bots; they are
    # either being evolved from or still need fresh evaluation.
    protected_recent = set()
    if len(active_bots) > MAX_ACTIVE_BOTS + 3:
        protected_recent = set(sorted(active_bots, key=lambda name: parse_bot_version(name) or -1)[-3:])
    protected_names = {current_bot, *protected_recent}
    try:
        priority_data = _read_json(PROJECT_ROOT / "web" / "core" / "results" / "priority_eval.json", {})
        if priority_data.get("bot"):
            protected_names.add(priority_data["bot"])
    except Exception:
        pass

    evaluated_candidates = []
    zero_game_candidates = []
    for b in active_bots:
        if b in protected_names:
            continue
        games = int(bot_stats.get(b, {}).get("games", 0) or 0)
        row = (b, ratings.get(b, Glicko2Player()), games)
        if games == 0:
            zero_game_candidates.append(row)
            continue
        evaluated_candidates.append(row)

    # Soft overflow: avoid culling untested bots. Hard overflow: old zero-game
    # bots are safer cull targets than the only evaluated baseline.
    if len(active_bots) <= MAX_ACTIVE_BOTS + 3:
        candidates = evaluated_candidates
    else:
        candidates = evaluated_candidates + zero_game_candidates
    if not candidates:
        return {
            "reaped": False,
            "reason": "All remaining bots are current, recent, priority, or protected untested",
            "protected": sorted(protected_names),
        }

    # Protect bots with insufficient evaluation. Previously this also gated on
    # `rd > 100`, but that clause existed only to compensate for the buggy
    # decay_rd that snapped idle bots' RD up to 150 every cycle (collapsing their
    # conservative_rating). Now that decay_rd follows the official Glicko-2
    # formula, an idle veteran's RD stays low and its conservative_rating (r-2*rd)
    # reflects real strength — so reaping it when it is genuinely the weakest is
    # correct. Protection is therefore sample-based only: a bot with <600 games
    # has too little data for its rating to be trusted as a reap verdict.
    protected = set()
    for name, rating, n_total in candidates:
        if n_total < 600:
            protected.add(name)
    # Apply protection EXCEPT when pool overflow forces reap (avoid unbounded growth)
    if len(active_bots) <= MAX_ACTIVE_BOTS + 3:  # soft cap, allow protection
        filtered = [c for c in candidates if c[0] not in protected]
        if not filtered:
            return {"reaped": False, "reason": "all_protected",
                    "remaining": len(active_bots), "protected_count": len(protected)}
        candidates = filtered

    # Sort by conservative rating (r - 2*rd) as PRIMARY key. Glicko conservative
    # rating is implicitly weighted by opponent strength, far less noisy than
    # per-opponent h2h_avg_wr at low game counts.
    candidates.sort(key=lambda x: (x[1].r - 2 * x[1].rd, x[2], parse_bot_version(x[0]) or 0))
    weakest = candidates[0]
    culled_name = weakest[0]
    conservative = weakest[1].r - 2 * weakest[1].rd

    # Serialize concurrent reaps via file lock
    reap_lock = RESULTS_DIR / ".reap.lock"
    with open(reap_lock, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            bot_src = PROJECT_ROOT / "bots" / culled_name
            if not bot_src.exists():
                return {"reaped": False, "reason": f"{culled_name} already moved"}
            # Publish the durable tombstone before mutating runtime metadata.
            # A failed tag/push must leave the sentinel intact so the operator
            # can retry without an ambiguous half-reaped state.
            record_reaped_bot(
                culled_name,
                reason="max_active_bots",
                data={
                    "selection_key": "conservative_glicko",
                    "conservative_rating": round(conservative, 1),
                    "leaderboard_score": round(strength_scores.get(culled_name, 0.0), 4),
                    "h2h_avg_wr": round(h2h_winrates.get(culled_name, 0.0), 4),
                    "quiet": quiet,
                },
            )
            sentinel = bot_src / ".completed"
            if sentinel.exists():
                sentinel.unlink()
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)

    try:
        if REPLAY_DIR.exists():
            prefix = f"_{culled_name}_"
            for f in list(REPLAY_DIR.iterdir()):
                if prefix in f.name or f.name.endswith(f"_{culled_name}.json"):
                    f.unlink()
    except Exception:
        pass

    reap_signal = RESULTS_DIR / ".reap_signal"
    reap_signal.write_text(str(time.time()))

    log_system_event(
        "bot.reaped",
        "info" if quiet else "warn",
        (
            f"{'Auto-reaped' if quiet else 'Reaped'} {culled_name} by conservative Glicko "
            f"(r-2rd={conservative:.1f}, leaderboard={strength_scores.get(culled_name, 0.0):.4f}, "
            f"h2h_wr={h2h_winrates.get(culled_name, 0.0):.2%})"
        ),
        {
            "culled": culled_name,
            "remaining": len(active_bots) - 1,
            "selection_key": "conservative_glicko",
            "conservative_rating": round(conservative, 1),
            "leaderboard_score": round(strength_scores.get(culled_name, 0.0), 4),
            "h2h_avg_wr": round(h2h_winrates.get(culled_name, 0.0), 4),
            "quiet": quiet,
        },
    )

    return {
        "reaped": True,
        "culled": culled_name,
        "selection_key": "conservative_glicko",
        "conservative_rating": round(conservative, 1),
        "leaderboard_score": round(strength_scores.get(culled_name, 0.0), 4),
        "h2h_avg_wr": round(h2h_winrates.get(culled_name, 0.0), 4),
        "rating": {"r": round(weakest[1].r, 1), "rd": round(weakest[1].rd, 1)},
        "remaining": len(active_bots) - 1,
        "reap_mode": "deactivate_completed_sentinel",
    }


def _mcp_result(data: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data)}]}


@tool("reap_weakest", f"Check if bot pool exceeds MAX_ACTIVE_BOTS and cull the weakest bot by conservative rating, reporting unified strength.", {})
async def reap_weakest(args):
    result = await _do_reap_weakest(quiet=args.get("quiet", False) if isinstance(args, dict) else False)
    return _mcp_result(result)


async def cleanup_incomplete(args: dict | None = None):
    """Fail closed instead of scanning arbitrary incomplete bot directories.

    The old helper enumerated ``bots/`` and inferred deletion authority from a
    raw checkpoint.  That made retired v155 debris actionable.  The only safe
    cleanup is now the normal fenced abandon transaction for an explicitly
    named, currently validated strict workflow.  The helper is deliberately
    not registered in either the MCP or HTTP tool catalogs; these checks remain
    as defence in depth for direct/internal calls.
    """

    try:
        from epoch_authority import require_policy_epoch_initialized

        epoch = require_policy_epoch_initialized("cleanup_incomplete")
    except Exception as exc:
        state = getattr(exc, "state", None)
        return _mcp_result({
            "cleaned": False,
            "error": "policy_epoch_not_initialized",
            "epoch": state if isinstance(state, dict) else None,
        })

    checkpoint = read_pipeline_checkpoint()
    if not isinstance(checkpoint, dict):
        return _mcp_result({
            "cleaned": False,
            "error": "strict_checkpoint_required",
        })
    next_v = checkpoint.get("next_v")
    revision = checkpoint.get("checkpoint_revision")
    workflow_run_id = checkpoint.get("workflow_run_id")
    if (
        type(next_v) is not int
        or type(revision) is not int
        or not isinstance(workflow_run_id, str)
        or not workflow_run_id.strip()
    ):
        return _mcp_result({
            "cleaned": False,
            "error": "strict_checkpoint_identity_missing",
        })

    try:
        from checkpoint_schema import strict_checkpoint_event_identity

        strict_checkpoint_event_identity(
            checkpoint,
            expected_gen=next_v,
            project_root=PROJECT_ROOT,
        )
    except Exception as exc:
        return _mcp_result({
            "cleaned": False,
            "error": "strict_checkpoint_invalid",
            "detail": f"{type(exc).__name__}: {str(exc)[:300]}",
        })

    request = args if isinstance(args, dict) else {}
    requested_identity = (
        request.get("workflow_run_id"),
        request.get("next_v"),
        request.get("checkpoint_revision"),
    )
    current_identity = (workflow_run_id, next_v, revision)
    if requested_identity != current_identity:
        return _mcp_result({
            "cleaned": False,
            "error": "explicit_cleanup_identity_mismatch",
            "requested": {
                "workflow_run_id": request.get("workflow_run_id"),
                "next_v": request.get("next_v"),
                "checkpoint_revision": request.get("checkpoint_revision"),
            },
            "current": {
                "workflow_run_id": workflow_run_id,
                "next_v": next_v,
                "checkpoint_revision": revision,
            },
        })

    candidate = get_bot_dir(next_v)
    bot_root = BOTS_DIR
    expected_candidate = bot_root / bot_name(next_v)
    try:
        candidate_parent = candidate.parent.resolve(strict=True)
        bot_root_resolved = bot_root.resolve(strict=True)
    except OSError as exc:
        return _mcp_result({
            "cleaned": False,
            "error": "candidate_scope_unavailable",
            "detail": f"{type(exc).__name__}: {str(exc)[:300]}",
        })
    if (
        candidate != expected_candidate
        or candidate_parent != bot_root_resolved
        or candidate.name != bot_name(next_v)
    ):
        return _mcp_result({
            "cleaned": False,
            "error": "candidate_outside_current_workflow_scope",
        })
    if not candidate.exists():
        return _mcp_result({
            "cleaned": False,
            "reason": "current_candidate_absent",
            "candidate": candidate.name,
            "workflow_run_id": workflow_run_id,
            "epoch": epoch.get("evaluation_epoch"),
        })
    if candidate.is_symlink() or not candidate.is_dir():
        return _mcp_result({
            "cleaned": False,
            "error": "current_candidate_path_unsafe",
        })
    if (candidate / ".completed").exists() or git_has_tag(next_v):
        return _mcp_result({
            "cleaned": False,
            "error": "current_candidate_is_published_or_completed",
        })
    if git_dir_is_committed(next_v):
        return _mcp_result({
            "cleaned": False,
            "error": "current_candidate_is_git_tracked",
        })

    result = await _do_abandon_generation(
        reason="cleanup_incomplete_exact_workflow",
        expected_workflow_run_id=workflow_run_id,
        expected_next_v=next_v,
        expected_source_v=checkpoint.get("source_v"),
        expected_checkpoint_revision=revision,
    )
    return _mcp_result({
        "cleaned": bool(
            result.get("abandoned") is True
            and result.get("removed_directory") == candidate.name
        ),
        "candidate": candidate.name,
        "workflow_run_id": workflow_run_id,
        "epoch": epoch.get("evaluation_epoch"),
        "abandon_result": result,
    })


class AbandonGenerationInput(TypedDict):
    pass


@tool("abandon_generation", "Clear pipeline checkpoint and remove incomplete next-gen directory. Use when a generation is stuck and needs to be restarted.", {})
async def abandon_generation(args):
    result = await _do_abandon_generation(reason="abandon_generation")
    return {"content": [{"type": "text", "text": json.dumps(result)}]}


async def _do_abandon_generation(
    reason: str = "abandon_generation",
    *,
    _actor_lock_owned: bool = False,
    _bypass_rate_limit: bool = False,
    expected_workflow_run_id: str | None = None,
    expected_next_v: int | None = None,
    expected_source_v: int | None = None,
    expected_checkpoint_revision: int | None = None,
) -> dict:
    """Core abandon logic — clears the pipeline checkpoint and removes the
    incomplete next-gen directory.

    Shared by the ``abandon_generation`` MCP tool and forced-abandon paths
    (notably ``MASTER_EXHAUSTED`` in run_master, B2 v125 fix) so the latter no
    longer relies on the orchestrator LLM obeying a plain-text directive.

    ``_bypass_rate_limit`` is reserved for system-owned fail-closed paths that
    have already proved the current immutable candidate cannot be retried.  It
    does not bypass checkpoint identity, workflow fencing, or stage guards.

    Returns the abandon result dict (also written as a ``pipeline.abandoned``
    system event). The caller is responsible for clearing the orchestrator
    session BEFORE calling this if a stale session must not be resumed.
    """
    from evolution_core import PIPELINE_STATE_FILE

    def expected_identity_conflict(candidate):
        if not any(value is not None for value in (
            expected_workflow_run_id,
            expected_next_v,
            expected_source_v,
            expected_checkpoint_revision,
        )):
            return None
        if not isinstance(candidate, dict):
            current = None
            mismatch = True
        else:
            current = {
                "workflow_run_id": str(
                    candidate.get("workflow_run_id")
                    or candidate.get("run_id")
                    or ""
                ),
                "next_v": candidate.get("next_v"),
                "source_v": candidate.get("source_v"),
                "checkpoint_revision": candidate.get("checkpoint_revision"),
            }
            mismatch = bool(
                (
                    expected_workflow_run_id is not None
                    and current["workflow_run_id"]
                    != str(expected_workflow_run_id)
                )
                or (
                    expected_next_v is not None
                    and current["next_v"] != int(expected_next_v)
                )
                or (
                    expected_source_v is not None
                    and current["source_v"] != int(expected_source_v)
                )
                or (
                    expected_checkpoint_revision is not None
                    and current["checkpoint_revision"]
                    != int(expected_checkpoint_revision)
                )
            )
        if not mismatch:
            return None
        return {
            "abandoned": False,
            "reason": "expected_checkpoint_identity_mismatch",
            "action": "stale_rejection_ignored",
            "expected_checkpoint": {
                "workflow_run_id": expected_workflow_run_id,
                "next_v": expected_next_v,
                "source_v": expected_source_v,
                "checkpoint_revision": expected_checkpoint_revision,
            },
            "current_checkpoint": current,
            "directive": (
                "The rejection belongs to an older checkpoint identity. Preserve "
                "the current generation and ignore this stale cleanup request."
            ),
        }

    checkpoint_exists = PIPELINE_STATE_FILE.exists()
    checkpoint = read_pipeline_checkpoint() if checkpoint_exists else None
    if checkpoint_exists and not isinstance(checkpoint, dict):
        return {
            "abandoned": False,
            "reason": "checkpoint_corrupt",
            "action": "operator_reconcile",
            "directive": (
                "The pipeline checkpoint exists but cannot be decoded or "
                "normalized. Preserve it for diagnosis; do not infer a version "
                "or delete a candidate from directory names."
            ),
        }
    identity_conflict = expected_identity_conflict(checkpoint)
    if identity_conflict:
        return identity_conflict
    infra_failure = (
        dict(checkpoint.get("infra_failure"))
        if isinstance(checkpoint, dict) and isinstance(checkpoint.get("infra_failure"), dict)
        else None
    )
    blocked = _generic_abandon_stage_block(checkpoint, reason)
    if blocked:
        try:
            log_system_event(
                "pipeline.abandon_refused_state_guard",
                "warn",
                blocked["directive"],
                blocked,
            )
        except Exception:
            pass
        return blocked

    # A4 (2026-06-30): rate-limit abandons to prevent evolution-DoS / version-space
    # leak. A rogue or stuck LLM could spam abandon_generation, monotonically
    # incrementing next_v via the abandoned_versions floor and never letting any
    # generation reach the gates. Enforce a 60s cooldown between abandons.
    import time as _t
    now = _t.time()
    if not _bypass_rate_limit and (now - _LAST_ABANDON_TS[0]) < 60:
        try:
            log_system_event(
                "pipeline.abandon_rate_limited", "warn",
                f"abandon_generation rate-limited (cooldown {60 - (now - _LAST_ABANDON_TS[0]):.0f}s remaining). "
                f"Recent abandon was {_LAST_ABANDON_TS[1]}.",
                {"cooldown_remaining": 60 - (now - _LAST_ABANDON_TS[0]),
                 "last_abandon_reason": _LAST_ABANDON_TS[1]},
            )
        except Exception:
            pass
        return {"abandoned": False, "rate_limited": True,
                "reason": f"abandon cooldown active ({60 - (now - _LAST_ABANDON_TS[0]):.0f}s remaining)"}
    workflow_fenced = False
    workflow_run_id = None
    if isinstance(checkpoint, dict):
        try:
            from worker_workflow import (
                WorkerWorkflow,
                workflow_run_id as checkpoint_workflow_run_id,
            )

            workflow = WorkerWorkflow.for_checkpoint(checkpoint)
            workflow_run_id = workflow.run_id
            # The actor terminal event and effect cancellation happen before any
            # mutable projection or candidate cleanup. Completion/projector paths
            # use the same short lock, so a late Worker can never recreate an
            # abandoned candidate after rmtree.
            def fence_latest_checkpoint():
                latest = read_pipeline_checkpoint()
                if not isinstance(latest, dict):
                    raise RuntimeError(
                        "checkpoint disappeared or became unreadable before fence"
                    )
                latest_identity_conflict = expected_identity_conflict(latest)
                if latest_identity_conflict:
                    return latest_identity_conflict, None
                if checkpoint_workflow_run_id(latest) != workflow.run_id:
                    raise RuntimeError(
                        "checkpoint workflow identity changed before fence"
                    )
                latest_block = _generic_abandon_stage_block(latest, reason)
                if latest_block:
                    return latest_block, None
                workflow.abandon(reason)
                return None, latest

            if _actor_lock_owned:
                blocked_after_lock, latest_checkpoint = fence_latest_checkpoint()
            else:
                with workflow.store.command_lock(
                    workflow.run_id,
                    blocking=True,
                ):
                    blocked_after_lock, latest_checkpoint = (
                        fence_latest_checkpoint()
                    )
            if blocked_after_lock:
                log_system_event(
                    "pipeline.abandon_refused_state_guard",
                    "warn",
                    blocked_after_lock["directive"],
                    blocked_after_lock,
                )
                return blocked_after_lock
            checkpoint = latest_checkpoint
            infra_failure = (
                dict(checkpoint.get("infra_failure"))
                if isinstance(checkpoint.get("infra_failure"), dict)
                else None
            )
            workflow_fenced = True
        except Exception as exc:
            log_system_event(
                "pipeline.abandon_workflow_fence_failed",
                "error",
                "Refused generation cleanup because the durable actor could not be fenced",
                {
                    "reason": reason,
                    "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                    "workflow_run_id": workflow_run_id,
                },
            )
            return {
                "abandoned": False,
                "reason": "workflow_fence_failed",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                "workflow_run_id": workflow_run_id,
            }
    cleared_checkpoint = False
    removed_dir = None
    abandoned_v = checkpoint.get("next_v") if isinstance(checkpoint, dict) else None

    def record_abandoned_floor(version):
        if version is None:
            return
        append_locked_jsonl(
            RESULTS_DIR / "abandoned_versions.jsonl",
            {
                "v": version,
                "reason": reason,
                "timestamp": __import__("time").time(),
                "infra_failure": infra_failure,
                "workflow_run_id": workflow_run_id,
            },
        )

    if checkpoint:
        next_v = checkpoint.get("next_v")
        # Persist the monotonic version floor before unlink/rmtree.  A crash
        # after either cleanup step therefore cannot silently reuse this
        # generation number on restart.
        record_abandoned_floor(abandoned_v)
        cleared_checkpoint = bool(clear_pipeline_checkpoint(
            expected_workflow_run_id=(
                checkpoint.get("workflow_run_id")
                or checkpoint.get("run_id")
                or workflow_run_id
            ),
            expected_next_v=checkpoint.get("next_v"),
            expected_source_v=checkpoint.get("source_v"),
            expected_checkpoint_revision=checkpoint.get("checkpoint_revision"),
            expected_checkpoint_stage=checkpoint.get("stage"),
        ))
        if not cleared_checkpoint:
            log_system_event(
                "pipeline.abandon_checkpoint_identity_conflict",
                "error",
                "Durable actor was fenced but checkpoint identity changed before cleanup",
                {
                    "reason": reason,
                    "workflow_run_id": workflow_run_id,
                    "next_v": next_v,
                    "source_v": checkpoint.get("source_v"),
                },
            )
            return {
                "abandoned": False,
                "reason": "checkpoint_identity_conflict",
                "workflow_fenced": workflow_fenced,
                "workflow_run_id": workflow_run_id,
                "abandoned_v": abandoned_v,
            }
        if next_v is not None:
            next_dir = get_bot_dir(next_v)
            if next_dir.exists() and not (next_dir / ".completed").exists():
                if git_dir_is_committed(next_v):
                    log_system_event(
                        "pipeline.abandon_preserved_git_tracked",
                        "warn",
                        f"Preserved git-tracked incomplete v{next_v} during abandon",
                        {"version": next_v, "reason": "git_tracked_without_tag"},
                    )
                else:
                    shutil.rmtree(next_dir)
                    removed_dir = bot_name(next_v)
    else:
        # No checkpoint — clean up any incomplete dir for authoritative next
        # version. Do not reuse current_v + 1 after abandoned generations.
        current_v = find_current_v()
        next_v = compute_next_generation_v(
            current_v=current_v,
            max_committed_v=find_max_committed_v(),
            abandoned_floor=find_abandoned_version_floor(),
        )
        next_dir = get_bot_dir(next_v)
        if next_dir.exists() and not (next_dir / ".completed").exists():
            abandoned_v = next_v
            record_abandoned_floor(abandoned_v)
            if git_dir_is_committed(next_v):
                log_system_event(
                    "pipeline.abandon_preserved_git_tracked",
                    "warn",
                    f"Preserved git-tracked incomplete v{next_v} during abandon",
                    {"version": next_v, "reason": "git_tracked_without_tag"},
                )
            else:
                shutil.rmtree(next_dir)
                removed_dir = bot_name(next_v)

    log_system_event("pipeline.abandoned", "warn",
                     f"Abandoned generation ({reason}, dir={removed_dir})",
                     {"removed_dir": removed_dir, "cleared_checkpoint": cleared_checkpoint,
                      "reason": reason, "abandoned_v": abandoned_v,
                      "infra_failure": infra_failure,
                      "workflow_fenced": workflow_fenced,
                      "workflow_run_id": workflow_run_id})
    # A4: update rate-limit timestamp on successful abandon.
    _LAST_ABANDON_TS[0] = now
    _LAST_ABANDON_TS[1] = reason

    return {
        "abandoned": True,
        "cleared_checkpoint": cleared_checkpoint,
        "removed_directory": removed_dir,
        "reason": reason,
        "infra_failure": infra_failure,
        "abandoned_v": abandoned_v,
        "workflow_fenced": workflow_fenced,
        "workflow_run_id": workflow_run_id,
    }
