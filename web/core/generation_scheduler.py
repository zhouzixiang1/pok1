"""Generation scheduler — three-phase evolution cycle.

Phase 1 (prepare_generation): Code-layer analysis and strategy decision.
Phase 2 (run_one_generation): LLM-driven pipeline execution (in orchestrator.py).
Phase 3 (post_generation_cleanup): Code-layer cleanup and maintenance.

Phase 1 is disposable (interrupt → re-run with fresh data).
Phase 2 preserves state on interrupt (session + checkpoint files).
Phase 3 is idempotent (interrupt → re-run safely).
"""

import asyncio
import json
import logging
import os
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from bot_namespace import ACTIVE_BOT_PREFIX, FIRST_STRICT_POLICY_VERSION, bot_name, bot_tag, parse_bot_version
from strength_order import match_score
from system_log import log_system_event
from blocking_runtime import run_blocking_isolated
import generation_scheduler_source_selection as _gs

log = logging.getLogger("pok.scheduler")

OSCILLATION_BREAKOUT_SCORE_TOLERANCE = 0.02
OSCILLATION_BREAKOUT_MIN_MARGIN = 0.01


def _default_publication_tier(*, next_v: int) -> str:
    """Resolve the checkpoint publication_tier for a newly selected generation.

    The certification/staging split has been removed: every generation now
    publishes through a single native tier.  The function is retained because
    many call sites still invoke it.
    """
    return "native"


def _relocate_draft_candidate_to_live(provisional_v: int, formal_v: int) -> None:
    """Move an isolated draft candidate tree onto the live bots/ path.

    Best-effort: missing source or occupied destination is a no-op so promotion
    can still rewrite the primary checkpoint when workers already wrote live.
    """
    import shutil

    from evolution_infra import BOTS_DIR, RESULTS_DIR

    src_name = bot_name(provisional_v)
    dst_name = bot_name(formal_v)
    src = RESULTS_DIR / "draft_candidates" / src_name
    dst = BOTS_DIR / dst_name
    if not src.is_dir():
        return
    if dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if provisional_v != formal_v:
        # Remap version directory name when shadow provisional != formal.
        tmp = RESULTS_DIR / "draft_candidates" / dst_name
        if src != tmp and not tmp.exists():
            src.rename(tmp)
            src = tmp
    shutil.move(str(src), str(dst))


def _maybe_promote_draft_to_primary():
    """Promote a one-ahead draft checkpoint to the primary slot if eligible.

    Called at the top of the primary ``prepare_generation`` (after gen N has
    fully published and the primary loop returns to prepare gen N+1).  If a
    draft sits at ``workers_done`` in ``pipeline_state_draft.json``, remap its
    shadow provisional ``next_v`` onto the canonical live successor
    (``published_high_water + 1``), move any isolated draft candidate tree onto
    ``bots/``, write the primary slot, and clear the draft.  The canonical CAS
    refuses if the primary is still active, so this is naturally safe and
    idempotent.  Returns True iff a promotion happened.  Best-effort: callers
    swallow exceptions.
    """
    try:
        from evolution_infra import (
            read_pipeline_checkpoint,
            write_pipeline_checkpoint,
            clear_pipeline_checkpoint,
            no_slot_override,
        )
    except Exception:
        return False
    draft = read_pipeline_checkpoint(slot_id="draft")
    if not isinstance(draft, dict) or not draft:
        return False
    if draft.get("stage") != "workers_done":
        return False
    try:
        draft_next_v = int(draft.get("next_v") or 0)
    except (TypeError, ValueError):
        return False
    # Confirm the formal live successor by consulting the epoch projection
    # (override-bypassed so it reads the real published high-water).
    try:
        from epoch_authority import strict_epoch_projection

        with no_slot_override():
            projection = strict_epoch_projection()
        formal_next_v = int(projection.get("next_v") or 0)
    except Exception:
        formal_next_v = 0
    if formal_next_v <= 0:
        return False
    # Shadow drafts may hold a provisional next_v (historically floor+2 while
    # primary owned floor+1).  After the primary publishes, remap onto the
    # formal successor.  Refuse only when a non-shadow draft targets a
    # different live version.
    if (
        draft.get("is_draft") is not True
        and draft_next_v != formal_next_v
    ):
        return False
    try:
        _relocate_draft_candidate_to_live(draft_next_v, formal_next_v)
    except Exception:
        pass
    promote_fields = {
        "next_v": formal_next_v,
        "source_v": int(draft.get("source_v") or 0),
        "stage": "workers_done",
        "master_plan": draft.get("master_plan"),
        "parent2_v": draft.get("parent2_v"),
        "direction_audit": draft.get("direction_audit"),
        "audit_context": draft.get("audit_context"),
        "gate_results": draft.get("gate_results"),
        "worker_failure_count": draft.get("worker_failure_count"),
        "worker_invocation_count": draft.get("worker_invocation_count"),
        "reviewer_feedback": draft.get("reviewer_feedback") or "",
        "charter_digest": draft.get("charter_digest"),
        "candidate_artifact_hash": draft.get("candidate_artifact_hash"),
        "candidate_manifest_digest": draft.get("candidate_manifest_digest"),
        "workflow_run_id": draft.get("workflow_run_id"),
        "audit_attempt": draft.get("audit_attempt"),
        "precommit_attempt": draft.get("precommit_attempt"),
        "precommit_rework_count": draft.get("precommit_rework_count"),
        "official_rework_count": draft.get("official_rework_count"),
        "timeout_extensions": draft.get("timeout_extensions"),
        "literature_probe": draft.get("literature_probe"),
        "prepare_scope_files": draft.get("prepare_scope_files"),
        "official_job": draft.get("official_job"),
        "repair_baseline_artifact_hash": draft.get(
            "repair_baseline_artifact_hash"
        ),
        "review_attempt_journal": draft.get("review_attempt_journal"),
        "identity_replan_history": draft.get("identity_replan_history"),
        "publication_tier": draft.get("publication_tier"),
    }
    with no_slot_override():
        ok = bool(write_pipeline_checkpoint(**promote_fields))
    if not ok:
        return False
    clear_pipeline_checkpoint(slot_id="draft")
    try:
        log_system_event(
            "pipeline.draft_promoted_to_primary",
            "info",
            f"One-ahead draft promoted to primary at v{formal_next_v} "
            f"(skipping re-prepare; provisional was v{draft_next_v})",
            {"next_v": formal_next_v, "provisional_next_v": draft_next_v},
        )
    except Exception:
        pass
    return True


def _wilson_lower_bound(points, games, z=1.96):
    """95% lower confidence bound on the true win rate (Wilson score interval).

    Used by the H2H anomaly detector (prepare_generation) so small-sample
    matchups (e.g. n=20 games) do not manufacture fake regressions from pure
    binomial noise. Under the null (true wr=0.5), n=20 has ~12% chance of a
    point estimate |wr-0.5|>0.15; the Wilson lower bound raises the bar to
    "statistically confident below even".
    """
    if games <= 0:
        return 0.0
    p = points / games
    denom = 1.0 + z * z / games
    center = (p + z * z / (2 * games)) / denom
    margin = z * ((p * (1 - p) + z * z / (4 * games)) / games) ** 0.5 / denom
    return max(0.0, center - margin)


@dataclass
class GenerationContext:
    """Pre-computed context for one generation."""
    current_v: int
    next_v: int
    strategy: str              # "master" | "crossover"
    source_v: int              # branch_from or current_v
    crossover_parents: tuple = ()  # (parent_a, parent_b) if crossover
    stagnation_info: str = ""
    match_analysis: str = ""
    performance_verification: str = ""
    replay_spotlight: str = ""
    gen_count: int = 0         # legacy alias for current_v; use next_v for post-generation triggers


def _bind_prepare_generation_cost_scope(
    next_v: int,
    ui=None,
    *,
    workflow_attempt: int = 1,
) -> str:
    """Bind cost accounting before any prepare-stage LLM can run."""

    from orchestrator_cost_policy import (
        activate_generation_cost_scope,
        assert_operator_cost_limit_available,
        claim_generation_cost_notice,
        generation_cost_status,
        generation_workflow_id,
        runtime_cost_policy,
    )

    workflow_run_id = generation_workflow_id(
        next_v,
        attempt=workflow_attempt,
    )
    scope = activate_generation_cost_scope(workflow_run_id, runtime_cost_policy())
    status = generation_cost_status(scope)
    receipt = scope.receipt(
        spent_before_usd=float(status.get("spent_usd") or 0.0),
        ledger_errors=tuple(status.get("accounting_errors") or ()),
    )
    begin_cost = getattr(ui, "begin_generation_cost", None) if ui else None
    if callable(begin_cost):
        begin_cost(workflow_run_id, status.get("spent_usd", 0.0), receipt)
    if claim_generation_cost_notice(scope, "prepare_scope_bound"):
        log_system_event(
            "pipeline.generation_prepare_cost_scope_bound",
            "info" if status.get("accounting_ok") else "warn",
            f"Prepare cost scope bound for {workflow_run_id}",
            {
                **receipt,
                "spent_usd": status.get("spent_usd"),
                "accounting_ok": status.get("accounting_ok"),
            },
        )
    assert_operator_cost_limit_available(scope)
    return workflow_run_id


@dataclass(frozen=True)
class EvaluationEvidence:
    """One coherent, post-wait rating/stat cutoff used for source selection."""

    active_bots: tuple[str, ...]
    ratings: dict
    bot_stats: dict
    h2h: dict
    selection_rows: tuple[dict, ...]
    rating_history_tail: tuple[dict, ...]
    games: int
    rd: float
    readiness_reason: str
    cutoffs: dict


@dataclass(frozen=True)
class SelectionView:
    """Deeply immutable source/parent selection facts for one generation."""

    active_bots: tuple[str, ...]
    active_versions: frozenset[int]
    rows: tuple
    metrics: MappingProxyType
    selection_scores: MappingProxyType
    order_keys: MappingProxyType
    rating_values: MappingProxyType
    h2h: MappingProxyType
    source_history: tuple[int, ...]
    digest: str


def _deep_freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze(item) for item in value)
    return value


def _build_selection_view(evidence: EvaluationEvidence) -> SelectionView:
    """Compile every source/parent decision input from the frozen bundle once."""
    from bot_artifact import canonical_digest
    from strength_order import strength_order_key
    from tool_helpers import strength_row_to_analysis_view

    active_bots = tuple(sorted(evidence.active_bots))
    active_versions = frozenset(
        version
        for version in (parse_bot_version(name) for name in active_bots)
        if version is not None
    )
    rows_by_name = {str(row.get("name")): dict(row) for row in evidence.selection_rows}
    if set(rows_by_name) != set(active_bots):
        raise ValueError("selection rows do not exactly match the frozen active pool")
    rows = tuple(rows_by_name[name] for name in active_bots)
    metrics_raw = {
        name: strength_row_to_analysis_view(rows_by_name[name]) for name in active_bots
    }
    scores = {
        name: float(
            rows_by_name[name].get(
                "selection_score",
                rows_by_name[name].get("leaderboard_score", 0.0),
            )
        )
        for name in active_bots
    }
    order_keys = {
        name: tuple(strength_order_key(rows_by_name[name])) for name in active_bots
    }
    rating_values = {}
    for name in active_bots:
        player = evidence.ratings[name]
        rating_values[name] = (
            float(player.r),
            float(player.rd),
            float(getattr(player, "sigma", 0.06)),
            float(player.conservative_rating()),
        )

    source_history = tuple(_read_source_v_history())
    digest_payload = {
        "active_bots": active_bots,
        "rows": rows,
        "source_history": source_history,
        "evaluation_cutoffs": evidence.cutoffs,
    }
    return SelectionView(
        active_bots=active_bots,
        active_versions=active_versions,
        rows=tuple(_deep_freeze(row) for row in rows),
        metrics=MappingProxyType({
            name: _deep_freeze(metrics_raw[name]) for name in active_bots
        }),
        selection_scores=MappingProxyType(scores),
        order_keys=MappingProxyType(order_keys),
        rating_values=MappingProxyType(rating_values),
        h2h=_deep_freeze(evidence.h2h),
        source_history=source_history,
        digest=canonical_digest(digest_payload),
    )


def _load_post_wait_evaluation_evidence(
    *,
    active_v: int,
    active_bot_name: str,
    min_games: int,
    rd_threshold: float,
    rd_min_games: int,
    expected_active_bots: list[str] | tuple[str, ...],
    snapshot_bundle: dict,
    allow_stale_readiness: bool = False,
) -> EvaluationEvidence | None:
    """Validate one manifest-bound daemon cycle after the async eval wait.

    ``allow_stale_readiness`` (True for speculative draft prepares): tolerate an
    incomplete rating rebuild (fewer than ``min_games`` and RD still high).  The
    draft is designed to run on the existing — possibly stale — snapshot so the
    LLM stays busy during the eval-wait window; if the rating later drifts
    enough to change parent selection, the draft is reaped (LLM-token cost only).
    Without this flag the post-wait readiness gate makes every eval_wait draft
    prepare return ``None``, leaving the LLM idle for the entire rating rebuild.
    """
    from evolution_infra import (
        Glicko2Player,
        find_latest_active_v,
        find_latest_rating_eligible_active_v,
        get_active_bots,
    )

    _raw_active_bots_before = tuple(sorted(get_active_bots()))
    active_bots_before = tuple(
        sorted(b for b in _raw_active_bots_before if _is_rating_pool_eligible_bot(b))
    )
    # Re-derive with the same rating-eligible selector used to bind the eval
    # source, so the "did the source change?" check is consistent with binding.
    refreshed_active_v = find_latest_rating_eligible_active_v()
    issues = []
    if refreshed_active_v != active_v:
        issues.append(
            f"active_source_changed:v{active_v}->v{refreshed_active_v}"
        )
    if active_bot_name not in active_bots_before:
        issues.append("active_source_missing_from_pool")
    expected_pool = tuple(sorted(str(name) for name in expected_active_bots))
    if active_bots_before != expected_pool:
        issues.append("active_pool_changed_during_eval_wait")
    if not snapshot_bundle.get("available"):
        issues.append("generation_evaluation_snapshot_unavailable")
    manifest = snapshot_bundle.get("manifest") or {}
    cycle = manifest.get("cycle") or {}
    cycle_pool = tuple(sorted(str(name) for name in (cycle.get("active_bots") or [])))
    if cycle_pool != expected_pool:
        issues.append("cycle_active_pool_mismatch")

    ratings_raw = snapshot_bundle.get("ratings") or {}
    bot_stats = snapshot_bundle.get("bot_stats") or {}
    h2h = snapshot_bundle.get("h2h") or {}
    selection = snapshot_bundle.get("selection") or {}
    try:
        ratings = {
            str(name): Glicko2Player.from_dict(payload)
            for name, payload in ratings_raw.items()
            if isinstance(payload, dict)
        }
    except Exception:
        ratings = {}
        issues.append("snapshot_ratings_invalid")
    rows = selection.get("rows")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        rows = []
        issues.append("snapshot_selection_rows_invalid")
    row_names = tuple(sorted(str(row.get("name")) for row in rows))
    rating_names = tuple(sorted(ratings))
    if row_names != expected_pool:
        issues.append("selection_row_pool_mismatch")
    if rating_names != expected_pool:
        issues.append("rating_pool_mismatch")
    history_tail = selection.get("rating_history_tail") or []
    if not isinstance(history_tail, list) or not all(
        isinstance(item, dict) for item in history_tail
    ):
        history_tail = []
        issues.append("snapshot_rating_history_invalid")

    player = ratings.get(active_bot_name) if isinstance(ratings, dict) else None
    if player is None:
        issues.append("active_source_rating_missing")
        rd = 350.0
    else:
        try:
            rd = float(player.rd)
        except (AttributeError, TypeError, ValueError):
            issues.append("active_source_rating_invalid")
            rd = 350.0
    try:
        games = int((bot_stats or {}).get(active_bot_name, {}).get("games", 0) or 0)
    except (AttributeError, TypeError, ValueError):
        games = 0
        issues.append("active_source_games_invalid")

    if games >= int(min_games):
        readiness_reason = "min_games"
    elif games >= int(rd_min_games) and rd < float(rd_threshold):
        readiness_reason = "rd_threshold"
    else:
        readiness_reason = "not_ready"
        # A speculative draft (allow_stale_readiness=True) is *designed* to run
        # on the existing snapshot during the eval-wait window, so an incomplete
        # rating rebuild is expected and non-fatal. The primary lane still
        # requires full readiness (the wait exists to guarantee a correct parent
        # choice for publication-authority work).
        if not allow_stale_readiness:
            issues.append("post_wait_readiness_not_reproducible")

    _raw_active_bots_after = tuple(sorted(get_active_bots()))
    active_bots_after = tuple(
        sorted(b for b in _raw_active_bots_after if _is_rating_pool_eligible_bot(b))
    )
    if active_bots_after != active_bots_before:
        issues.append("active_pool_changed_while_loading_snapshot")
    cutoffs = {
        "generation_snapshot_manifest_digest": manifest.get("manifest_digest"),
        "cycle_manifest_digest": cycle.get("manifest_digest"),
        "save_num": cycle.get("save_num"),
        "daemon_run_id": cycle.get("daemon_run_id"),
    }
    if issues:
        log_system_event(
            "pipeline.eval_evidence_incoherent",
            "warn",
            f"Post-wait evidence for {active_bot_name} is not coherent; prepare will retry",
            {
                "bot": active_bot_name,
                "active_v": active_v,
                "issues": issues,
                "games": games,
                "rd": round(rd, 2),
                "cutoffs": cutoffs,
            },
        )
        return None

    evidence = EvaluationEvidence(
        active_bots=active_bots_before,
        ratings=ratings,
        bot_stats=bot_stats or {},
        h2h=h2h,
        selection_rows=tuple(dict(row) for row in rows),
        rating_history_tail=tuple(dict(item) for item in history_tail),
        games=games,
        rd=rd,
        readiness_reason=readiness_reason,
        cutoffs=cutoffs,
    )
    log_system_event(
        "pipeline.eval_evidence_frozen",
        "success",
        f"Frozen coherent post-wait evidence for {active_bot_name}",
        {
            "bot": active_bot_name,
            "active_v": active_v,
            "games": games,
            "rd": round(rd, 2),
            "reason": readiness_reason,
            "active_bot_count": len(active_bots_before),
            "cutoffs": cutoffs,
        },
    )
    return evidence


def _bind_prepare_log_context(current_v: int, allocation_floor: int) -> int:
    """Bind structured logs emitted during disposable Phase-1 prepare."""
    planned_next_v = int(allocation_floor) + 1
    attempt = {"generation": 0, "audit": 0, "precommit": 0}
    try:
        from event_bus import update_last_known, invalidate_ckpt_cache
        update_last_known(run_id=f"{planned_next_v}#0", stage="preparing", attempt=attempt)
        invalidate_ckpt_cache()
    except Exception:
        pass
    try:
        log_system_event(
            "pipeline.prepare_context_bound",
            "info",
            f"Prepare log context bound for v{planned_next_v}",
            {"next_v": planned_next_v, "current_v": current_v,
             "allocation_floor": allocation_floor, "stage": "preparing"},
        )
    except Exception:
        pass
    return planned_next_v


def _mechanical_urgent_intervention_eligible(
    source_bot_name: str,
    rating_history_tail,
    selection_view,
) -> bool:
    """Require frozen numeric evidence before an LLM can request recovery."""
    points = []
    run_ids = []
    for row in rating_history_tail or ():
        try:
            value = (row.get("ratings") or {}).get(source_bot_name, {}).get("r")
            if value is not None:
                points.append((int(row.get("period")), float(value)))
                run_ids.append(str(row.get("daemon_run_id") or ""))
        except (AttributeError, TypeError, ValueError):
            continue
    if len(points) < 4:
        return False
    recent = points[-4:]
    if any(recent[index][0] != recent[index - 1][0] + 1 for index in range(1, 4)):
        return False
    if len(set(run_ids[-4:])) != 1:
        return False
    values = [value for _period, value in recent]
    recent_deltas = [values[index] - values[index - 1] for index in range(len(values) - 2, len(values))]
    # Include the third most-recent period delta.
    recent_deltas.insert(0, values[-3] - values[-4])
    if not all(delta <= -40.0 for delta in recent_deltas[-3:]):
        return False
    if not isinstance(selection_view, SelectionView):
        return False
    leader_v = _get_unified_leader_v({}, selection_view)
    if leader_v is None or bot_name(leader_v) == source_bot_name:
        return False
    leader_score = float(
        selection_view.selection_scores.get(bot_name(leader_v), 0.0)
    )
    source_score = float(selection_view.selection_scores.get(source_bot_name, 0.0))
    return source_score <= leader_score - 0.05


def _is_rating_pool_eligible_bot(bot_name: str) -> bool:
    """True iff the bot resolves as ROLE_RATING_POOL-eligible.

    Used to filter the expected_active_bots passed to the post-eval-wait
    evidence check so it matches the daemon's filtered rating pool (the daemon
    only rates eligible bots). Without this filter the raw active pool (incl
    staging-uncertified bots) is compared against the cycle's eligible-only
    pool → cycle_active_pool_mismatch → prepare retries forever.
    """
    try:
        from bot_namespace import resolve_national_bot_spec, ROLE_RATING_POOL
        return bool(resolve_national_bot_spec(bot_name, ROLE_RATING_POOL).eligible)
    except Exception:
        return False


def _ensure_priority_eval_signal(bot: str, min_games: int) -> None:
    """Ask the daemon to prioritize the bot that prepare is about to wait for."""
    try:
        from evolution_infra import RESULTS_DIR, locked_file

        priority_file = RESULTS_DIR / "priority_eval.json"
        payload = {
            "bot": bot,
            "min_games": max(1, int(min_games)),
            "since": time.time(),
            "source": "prepare_eval_wait",
        }
        with locked_file(priority_file, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        log_system_event(
            "pipeline.eval_wait_priority_set",
            "info",
            f"Priority evaluation queued for {bot}",
            payload,
        )
    except Exception as exc:
        log.warning("Failed to write priority eval signal for %s: %s", bot, exc)
        log_system_event(
            "pipeline.eval_wait_priority_failed",
            "warn",
            f"Failed to queue priority evaluation for {bot}: {str(exc)[:180]}",
            {"bot": bot, "min_games": min_games},
        )


def _prepare_protocol_bootstrap_generation(
    *,
    active_bots: list[str],
    current_v: int,
    next_v: int,
    allocation_floor: int,
    abandoned_receipt_floor: int,
    workflow_run_id: str,
    ui=None,
    slot_id=None,
) -> GenerationContext | None:
    """Select a zero/one-strict-bot generation without fabricated ratings."""

    from evolution_infra import write_pipeline_checkpoint
    from master_context_contract import build_master_context
    from bot_artifact import canonical_digest
    from bot_namespace import (
        ARCHIVED_VERSION_HIGH_WATER,
        EVALUATION_EPOCH,
        FIRST_STRICT_POLICY_VERSION,
        ROLE_PARENT_SOURCE,
        parse_bot_version,
        resolve_national_bot_spec,
    )

    active_bots = sorted(set(map(str, active_bots)))
    if not active_bots:
        from system_strict_bootstrap import (
            build_fresh_bootstrap_receipt,
            load_policy_epoch_reset_receipt,
        )

        if int(next_v) != FIRST_STRICT_POLICY_VERSION:
            log_system_event(
                "pipeline.policy_bootstrap_version_mismatch",
                "error",
                f"Fresh national policy bootstrap must create v{FIRST_STRICT_POLICY_VERSION}",
                {"next_v": next_v, "expected": FIRST_STRICT_POLICY_VERSION},
            )
            return None
        source_v = ARCHIVED_VERSION_HIGH_WATER
        reset_receipt, reset_errors = load_policy_epoch_reset_receipt()
        if reset_receipt is None:
            log_system_event(
                "pipeline.policy_epoch_reset_required",
                "error",
                f"Fresh v{FIRST_STRICT_POLICY_VERSION} is blocked until the one-time policy epoch reset is executed",
                {"issues": reset_errors[:10], "next_v": int(next_v)},
            )
            if ui:
                ui.log_history(
                    f"Fresh v{FIRST_STRICT_POLICY_VERSION} requires scripts/reset_national_tcp_policy_epoch.py "
                    "--execute --acknowledge-runtime-checkout from the stopped "
                    ".evolution_pok checkout",
                    "error",
                )
            return None
        receipt = build_fresh_bootstrap_receipt(
            active_bots=(),
            epoch_reset_receipt_digest=str(reset_receipt["receipt_digest"]),
        )
        mode = "fresh_national_policy_bootstrap"
    elif len(active_bots) == 1:
        source_v = parse_bot_version(active_bots[0])
        if source_v is None or source_v < FIRST_STRICT_POLICY_VERSION:
            return None
        spec = resolve_national_bot_spec(active_bots[0], role=ROLE_PARENT_SOURCE)
        if not spec.eligible:
            log_system_event(
                "pipeline.singleton_policy_bootstrap_unavailable",
                "error",
                "The sole policy bot failed strict parent-source resolution",
                {"bot": active_bots[0], "issues": list(spec.issues)[:10]},
            )
            return None
        subject = {
            "schema_version": 1,
            "kind": "national-tcp-policy-singleton-bootstrap-v1",
            "mode": "singleton_strict_bootstrap",
            "epoch": EVALUATION_EPOCH,
            "source_v": source_v,
            "next_v": int(next_v),
            "source_artifact_inherited": True,
            "active_bots": list(active_bots),
            "source_runtime_manifest_digest": canonical_digest(spec.runtime_manifest),
            "source_epoch_receipt_digest": canonical_digest(spec.epoch_receipt),
            "source_publication_identity": spec.publication_identity,
            "source_certificate_digest": spec.certificate_digest,
        }
        receipt = {**subject, "receipt_digest": canonical_digest(subject)}
        mode = "singleton_strict_bootstrap"
    else:
        log_system_event(
            "pipeline.protocol_bootstrap_unavailable",
            "error",
            "Policy bootstrap is only valid with zero or one active bot",
            {
                "next_v": next_v,
                "active_bots": list(active_bots),
                "reason": "active_policy_pool_not_bootstrap_sized",
            },
        )
        if ui:
            ui.log_history(
                "策略 epoch 启动被拒绝：活跃池必须恰为 0 或 1 个 bot",
                "error",
            )
        return None
    strategy = (
        "fresh_policy_bootstrap"
        if mode == "fresh_national_policy_bootstrap"
        else "singleton_strict_bootstrap"
    )
    evidence_note = (
        f"No policy-epoch bot is published yet. v{ARCHIVED_VERSION_HIGH_WATER} is version/tag/tree authority "
        "only: do not open, copy, import, execute, or mine its archived source. "
        f"Prepare v{FIRST_STRICT_POLICY_VERSION} from the current system runtime and a fresh typed policy. "
        "No pre-policy rating, replay, or strategy evidence is admissible."
        if mode == "fresh_national_policy_bootstrap"
        else
        "Exactly one policy-epoch bot is published, so peer rating evidence cannot "
        "exist. Build the second policy bot from that strict parent only; archived "
        "pre-policy artifacts and derived evidence remain inadmissible."
    )
    master_context = build_master_context(
        next_v=next_v,
        source_v=source_v,
        stagnation_info=evidence_note,
        match_analysis="No two-bot strict pool exists; match analysis intentionally absent.",
        performance_verification=(
            "Bootstrap authority is protocol publication identity, not Glicko/H2H. "
            f"receipt={receipt['receipt_digest']}"
        ),
    )
    ok = write_pipeline_checkpoint(
        next_v,
        source_v,
        "selected",
        audit_context={
            "protocol_bootstrap": receipt,
            "selection": {
                "strategy": strategy,
                "current_v": current_v,
                "published_high_water": current_v,
                "allocation_floor": allocation_floor,
                "abandoned_receipt_floor": abandoned_receipt_floor,
                "parent_a": None if mode == "fresh_national_policy_bootstrap" else source_v,
                "parent_b": None,
                "bootstrap_without_strength_evidence": True,
                "protocol_bootstrap_receipt_digest": receipt["receipt_digest"],
                "evaluation_evidence": {
                    "bot": None,
                    "games": 0,
                    "rd": None,
                    "readiness_reason": mode,
                    "cutoffs": {},
                },
            },
            "master_context": master_context,
        },
        workflow_run_id=workflow_run_id,
        publication_tier=_default_publication_tier(next_v=next_v),
        slot_id=slot_id,
    )
    if not ok:
        raise RuntimeError(f"protocol bootstrap checkpoint refused for v{next_v}")
    log_system_event(
        "pipeline.protocol_bootstrap_selected",
        "warn",
        f"Selected {strategy} v{next_v} from v{source_v}",
        {
            "next_v": next_v,
            "source_v": source_v,
            "mode": mode,
            "active_bots": list(active_bots),
            "receipt_digest": receipt["receipt_digest"],
            "rating_wait_bypassed": True,
        },
    )
    return GenerationContext(
        current_v=current_v,
        next_v=next_v,
        strategy=strategy,
        source_v=source_v,
        crossover_parents=(),
        stagnation_info=evidence_note,
        match_analysis="No two-bot strict pool exists; match analysis intentionally absent.",
        performance_verification=master_context["performance_verification"],
        replay_spotlight="",
        gen_count=current_v,
    )


def _h2h_freeze_force(slot_id) -> bool:
    """Only the PRIMARY lane force-refreshes the frozen evidence cycle.

    The snapshot dir is per-VERSION (not per-lane), and up to max_ahead draft
    slots can prepare the same version concurrently — every force freeze
    rmtree+rebuilds it with a new created_at (=> new manifest_digest), which
    invalidated the primary checkpoint's bound digests and killed every
    generation at Master (v177/v179/v182, 2026-08-15: 33 re-freezes of v179
    in one hour). Draft slots must REUSE a valid existing snapshot (they are
    explicitly designed to run on stale evidence,
    ``allow_stale_readiness=slot_id is not None``); ``force=False`` still
    creates the snapshot when absent and validates integrity + spotlight_bot
    when reusing.
    """
    return slot_id is None


async def prepare_generation(shutdown_mgr, ui=None, min_games=None, *, slot_id=None) -> GenerationContext | None:
    """Phase 1: Analyze state, decide strategy. Disposable on interrupt.

    ``slot_id`` (Phase 5b one-ahead draft) routes the two selected-checkpoint
    writes to a per-slot file instead of the primary.  When ``slot_id`` is not
    None the caller is the one-ahead draft task: it builds a *shadow* identity
    (``is_draft=True``) with a provisional ``next_v`` label derived from the
    primary target + 1.  That provisional is **not** a live allocation claim
    (floor+1 CAS / bots/national_cloud_v{floor+2} are skipped); the candidate
    worktree is isolated under ``draft_candidates/``, and promotion remaps the
    draft onto the formal live successor after the primary publishes.
    """
    from evolution_infra import (
        MAX_ACTIVE_BOTS, find_latest_active_v, get_active_bots,
        wait_for_daemon_eval, ensure_publish_ready_for_new_generation,
        MIN_GAMES_FOR_EVAL, find_latest_rating_eligible_active_v,
    )

    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        return None

    # Phase 5b one-ahead draft promotion (primary path only).  When the
    # primary loop returns here after gen N fully publishes, a one-ahead draft
    # for gen N+1 may be sitting at workers_done in the draft slot.  Promote
    # it to the primary slot instead of re-preparing from scratch: the draft
    # already ran direction_audit/Master/Workers, so the primary loop's
    # deterministic recovery picks up at run_quality_gates.  Best-effort and
    # non-fatal: any refusal or mismatch falls through to the canonical
    # prepare.  This is the reliable promotion point because gen N's tag now
    # exists (published_high_water == N) and the primary checkpoint is clear.
    # Offload: _maybe_promote_draft_to_primary reads the draft checkpoint and
    # re-runs strict_epoch_projection (git/ssh-keygen subprocesses) on the
    # event loop otherwise (AGENTS.md blocking boundary).
    if slot_id is None:
        try:
            await run_blocking_isolated(
                _maybe_promote_draft_to_primary,
                thread_name_prefix="prepare-draft-promote",
            )
        except Exception:
            pass

    # A published bot is not a completed evolution cycle until its durable
    # Archivist journal finishes.  This check precedes epoch reads, cost scope,
    # daemon waits, and every planning LLM call.
    try:
        from post_publication_handoff import pending_handoff_route

        # pending_handoff_route scans durable handoff journal JSON records from
        # disk (same AGENTS.md blocking boundary as the epoch projection below).
        handoff_route = await run_blocking_isolated(
            pending_handoff_route,
            thread_name_prefix="prepare-handoff-discovery",
        )
    except Exception as exc:
        handoff_route = {
            "status": "blocked",
            "issues": [f"handoff_discovery_failed:{type(exc).__name__}"],
        }
    if handoff_route.get("status") != "none":
        log_system_event(
            "pipeline.prepare_blocked_post_publication_handoff",
            "error" if handoff_route.get("status") == "blocked" else "warn",
            "Prepare blocked until the durable post-publication Archivist handoff finishes",
            {
                "status": handoff_route.get("status"),
                "version": handoff_route.get("version"),
                "source_v": handoff_route.get("source_v"),
                "issues": handoff_route.get("issues") or [],
                "next_tool": "run_archivist",
            },
        )
        return None

    # Resolve an existing generation only through the canonical epoch
    # projection. A raw checkpoint filename/JSON object cannot reserve a target
    # or become resumable without its digest-bound strict-epoch envelope.
    # strict_epoch_projection() resolves find_current_v / policy_epoch
    # initialization / abandoned-version authority / read_pipeline_checkpoint,
    # each of which walks git + ssh-keygen subprocesses (30s timeouts) to
    # signature-verify the published namespace. Running it inline blocks the
    # ASGI event loop for minutes (same defect as the pool-resolution calls
    # below), so it MUST run in an owned worker thread (AGENTS.md blocking
    # boundary). Import locally so the blocking resolution runs entirely off
    # the loop.
    try:
        from epoch_authority import strict_epoch_projection

        _epoch_projection = await run_blocking_isolated(
            strict_epoch_projection,
            thread_name_prefix="prepare-epoch-projection",
        )
    except Exception as exc:
        log_system_event(
            "pipeline.prepare_blocked_version_authority",
            "error",
            "Prepare generation could not verify version allocation authority",
            {"error": f"{type(exc).__name__}: {str(exc)[:500]}"},
        )
        if ui:
            ui.log_history(f"Prepare blocked by version authority: {exc}", "error")
        return None
    if _epoch_projection.get("initialized") is not True:
        log_system_event(
            "pipeline.prepare_blocked_epoch_uninitialized",
            "error",
            "Prepare generation refused an uninitialized strict policy epoch",
            {
                "state": _epoch_projection.get("state"),
                "operator_action": _epoch_projection.get("operator_action"),
                "reset_receipt_issues": _epoch_projection.get(
                    "reset_receipt_issues"
                ) or [],
            },
        )
        return None
    if _epoch_projection.get("ignored_checkpoint"):
        # Only the PRIMARY prepare path may be refused on the basis of an
        # unreadable/incompatible checkpoint.  A one-ahead draft prepare
        # (slot_id set) runs concurrently with the parked primary; its OWN
        # draft slot legitimately has no checkpoint yet, and it must not be
        # refused because of the primary's checkpoint state.  Mirrors the
        # draft bypass at the ``slot_id is None`` guard below on
        # ``_existing_generation``.
        if slot_id is None:
            log_system_event(
                "pipeline.prepare_blocked_checkpoint_authority",
                "error",
                "Prepare generation refused an unreadable or incompatible checkpoint",
                dict(_epoch_projection["ignored_checkpoint"]),
            )
            if ui:
                ui.log_history(
                    "Prepare blocked: active checkpoint requires operator reconciliation.",
                    "error",
                )
            return None
    _existing_generation = _epoch_projection.get("active_generation")
    # Only the primary prepare path may early-return an existing in-flight
    # generation.  A draft prepare (slot_id set) must continue so it can build
    # its shadow identity even while the primary checkpoint is past preparing.
    if slot_id is None and isinstance(_existing_generation, dict):
        if _existing_generation.get("stage") not in (
            None, "selected", "preparing",
        ):
            _next_v = _existing_generation.get("next_v")
            _source_v = _existing_generation.get("source_v")
            if _next_v is not None and _source_v is not None:
                _parent2_v = _existing_generation.get("parent2_v")
                _strategy = "crossover" if _parent2_v else "master"
                log_system_event(
                    "pipeline.prepare_skipped_existing_checkpoint",
                    "info",
                    "Prepare skipped: canonical checkpoint already at "
                    f"{_existing_generation.get('stage')}",
                    {
                        "next_v": _next_v,
                        "source_v": _source_v,
                        "stage": _existing_generation.get("stage"),
                    },
                )
                return GenerationContext(
                    current_v=int(_epoch_projection["current_v"]),
                    next_v=_next_v,
                    strategy=_strategy,
                    source_v=_source_v,
                    crossover_parents=(_source_v, _parent2_v) if _parent2_v else (),
                    gen_count=0,
                )

    try:
        from workflow_profiles import get_workflow_profile

        workflow_profile = get_workflow_profile()
        workflow_id = str(getattr(workflow_profile, "profile_id", "") or "")
        execution_mode = str(
            getattr(workflow_profile, "national_execution_mode", "") or ""
        )
    except Exception as exc:
        log_system_event(
            "pipeline.prepare_blocked_workflow_contract",
            "error",
            "Prepare generation could not resolve the national-native workflow contract",
            {"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
        )
        return None
    if workflow_id != "national_native" or execution_mode != "native_tcp":
        log_system_event(
            "pipeline.prepare_blocked_workflow_contract",
            "error",
            "Formal evolution requires the national_native/native_tcp workflow",
            {
                "workflow_profile_id": workflow_id,
                "national_execution_mode": execution_mode,
                "required_profile_id": "national_native",
                "required_execution_mode": "native_tcp",
            },
        )
        if ui:
            ui.log_history(
                "Evolution blocked: formal bot output requires national_native/native_tcp.",
                "error",
            )
        return None

    try:
        from tool_runtime_guard import ensure_runtime_git_guard
        # ensure_runtime_git_guard runs a git worktree/status subprocess to
        # verify branch cleanliness (AGENTS.md blocking boundary).
        guard_ok, guard_payload = await run_blocking_isolated(
            ensure_runtime_git_guard,
            "prepare_generation",
            {},
            thread_name_prefix="prepare-runtime-git-guard",
        )
        if not guard_ok:
            log_system_event(
                "pipeline.prepare_blocked_runtime_guard",
                "error",
                "Prepare generation blocked by runtime git guard",
                guard_payload,
            )
            if ui:
                ui.log_history(
                    f"Prepare blocked by runtime git guard: {guard_payload.get('reason')}",
                    "error",
                )
            return None
    except Exception as exc:
        log_system_event(
            "pipeline.prepare_runtime_guard_failed_closed",
            "error",
            "Prepare generation could not verify the runtime Git guard",
            {"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
        )
        return None

    try:
        # ensure_publish_ready_for_new_generation checks origin/main sync via
        # git subprocess (AGENTS.md blocking boundary).
        publish_ok, publish_payload = await run_blocking_isolated(
            ensure_publish_ready_for_new_generation,
            thread_name_prefix="prepare-publish-sync",
        )
        if not publish_ok:
            log_system_event(
                "pipeline.prepare_blocked_publish_sync",
                "error",
                "Prepare generation blocked by unpublished or stale git state",
                publish_payload,
            )
            if ui:
                ui.log_history(
                    f"Prepare blocked by publish sync guard: {publish_payload.get('reason')}",
                    "error",
                )
            return None
    except Exception as exc:
        log_system_event(
            "pipeline.prepare_publish_sync_check_failed",
            "error",
            "Prepare generation could not verify publish synchronization",
            {"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
        )
        if ui:
            ui.log_history(f"Prepare publish sync check failed: {exc}", "error")
        return None

    current_v = int(_epoch_projection["published_high_water"])
    _abandoned_floor = int(_epoch_projection["abandoned_receipt_floor"])
    allocation_floor = int(_epoch_projection["allocation_floor"])
    _planned_next_v = int(_epoch_projection["next_v"])
    # Phase 5b one-ahead draft: shadow provisional label only.  Derive
    # primary_next_v + 1 so planning/logs have a stable intended successor, but
    # do NOT treat it as a live floor+2 allocation (checkpoint write sets
    # is_draft=True, skips floor+1 CAS, and isolates the candidate worktree).
    # Promotion remaps onto the formal next_v after the primary publishes.
    if slot_id is not None:
        from evolution_infra import no_slot_override, read_pipeline_checkpoint

        def _read_primary_ckpt_sync():
            with no_slot_override():
                return read_pipeline_checkpoint()

        # read_pipeline_checkpoint reads + parses the primary checkpoint JSON
        # from disk (AGENTS.md blocking boundary).
        _primary_ckpt = await run_blocking_isolated(
            _read_primary_ckpt_sync,
            thread_name_prefix="prepare-draft-ckpt-read",
        )
        _draft_floor = max(allocation_floor, current_v)
        if isinstance(_primary_ckpt, dict):
            try:
                _primary_next_v = int(_primary_ckpt.get("next_v") or 0)
            except (TypeError, ValueError):
                _primary_next_v = 0
            if _primary_next_v > _draft_floor:
                _draft_floor = _primary_next_v
        else:
            # No primary checkpoint exists yet (the primary is parked in
            # eval_wait, before prepare creates its checkpoint). The primary
            # will claim allocation_floor+1, so a speculative draft launched
            # during this window must target allocation_floor+2 to avoid a
            # version collision with the primary's imminent next_v. Without
            # this, the draft reserves the SAME version the primary will claim
            # and stalls at ``selected checkpoint refused``.
            _draft_floor = max(_draft_floor, allocation_floor + 1)
        # Multi-ahead: reserve a DISTINCT next_v for this draft slot through the
        # persisted version-reservation registry, so N>1 in-flight drafts never
        # collide on the same version (the floor+1 projection alone would give
        # them all the same value).  Falls back to the legacy floor+1 when the
        # registry is unavailable (slice2b off / single-ahead / tests).
        _registry_floor = _draft_floor + 1
        try:
            from producer_consumer_slice2b_activation import slice2b_active

            if slice2b_active():
                from orchestrator_deterministic_route import (
                    _slice2b_ensure_activation,
                )

                _activation = _slice2b_ensure_activation()
                if _activation is not None:
                    _registry_floor = _activation.ledger.reserve_draft_version(
                        slot_id=slot_id,
                        floor_next_v=_draft_floor,
                        candidate_id=str(checkpoint.get("candidate_id") or "")
                        if isinstance(checkpoint, dict)
                        else None,
                    )
        except Exception:
            pass
        _planned_next_v = _registry_floor
        allocation_floor = _planned_next_v - 1
        log.info(
            "Ahead draft slot=%s shadow provisional v%d (primary high-water v%d)",
            slot_id,
            _planned_next_v,
            current_v,
        )
    # No directory, log filename, direct commit or runtime counter participates
    # here. A valid active checkpoint may hold its bound target; otherwise the
    # next label is tag high-water / durable abandon-receipt floor + one.
    if _epoch_projection.get("next_v_authority") == "active_checkpoint_epoch_binding":
        log.info(
            "Resuming checkpoint-bound allocation v%d (published high-water v%d)",
            _planned_next_v,
            current_v,
        )
    elif _abandoned_floor > current_v:
        log.info(
            "Next label v%d reserved after validated abandon receipt floor v%d",
            _planned_next_v,
            _abandoned_floor,
        )
    # Allocate the final workflow identity before Combined/Match/degeneration
    # analysis.  The selected checkpoint below adopts this exact id, so prepare
    # retries, SDK session replacement, and process restart cannot split or
    # leak one generation's bill into another.
    #
    # The attempt suffix is advanced for EVERY re-selected version, not only the
    # first-strict bootstrap.  A re-selection happens when this label has prior
    # validated abandon receipts but the allocation floor has not advanced past
    # it (the high-water/floor window leaves the same label allocatable again).
    # `abandoned_version_attempt_count` returns the greatest prior attempt for
    # this exact version (0 when it was never abandoned), so:
    #   * a never-abandoned version keeps workflow-v1 (v1/v11 history, and every
    #     normal first attempt, is byte-identical);
    #   * a version abandoned once is re-prepared under workflow-v2 with a fresh
    #     Worker/strict journal, instead of reusing the dead `workflow-v1`
    #     instance -- which previously surfaced as either `WorkflowConflict:
    #     workflow instance is not running` (crossover effect prepare) or an
    #     infinite `frozen_rework_*` state-guard loop (terminal journal replayed
    #     while the outer checkpoint was re-created at master_planned).  This is
    #     the durable per-version retry that the ledger's `workflow-vK` naming
    #     already encodes (see test_failed_reserved_v143_attempt_is_audited_but_
    #     does_not_burn_label in test_epoch_authority.py).
    from evolution_infra import abandoned_version_attempt_count

    # abandoned_version_attempt_count scans the durable abandoned-version
    # receipt ledger from disk (AGENTS.md blocking boundary).
    _workflow_attempt = (
        await run_blocking_isolated(
            abandoned_version_attempt_count,
            _planned_next_v,
            thread_name_prefix="prepare-abandon-ledger",
        )
    ) + 1
    _prepare_workflow_run_id = _bind_prepare_generation_cost_scope(
        _planned_next_v,
        ui,
        workflow_attempt=_workflow_attempt,
    )
    _bind_prepare_log_context(current_v, _planned_next_v - 1)
    try:
        from repo_state import log_git_worktree_snapshot

        # log_git_worktree_snapshot runs a git worktree/status subprocess
        # (AGENTS.md blocking boundary).
        await run_blocking_isolated(
            log_git_worktree_snapshot,
            "repo.worktree_snapshot",
            f"Worktree snapshot before preparing v{_planned_next_v}",
            next_v=_planned_next_v,
            current_v=current_v,
            published_high_water=current_v,
            allocation_floor=allocation_floor,
            abandoned_receipt_floor=_abandoned_floor,
            emit_delta=True,
            thread_name_prefix="prepare-worktree-snapshot",
        )
    except Exception:
        pass
    # Pick the eval source from COMPLETED bots: a staging-published master that
    # has not yet closed the two-tier gap (no signed full certificate) is
    # structurally excluded from the rating pool and can never accrue the
    # strength sample this generation waits on. find_latest_rating_eligible_active_v
    # returns the newest rating-pool-eligible (fully certified) active bot,
    # falling back past any not-yet-certified higher versions. 1A's
    # EvalSourceRatingIneligible precheck below stays as a fail-closed backstop
    # for the case where NO active bot is rating-eligible.
    # Pool resolution (get_active_bots / find_latest_rating_eligible_active_v)
    # walks every active bot through certificate_validation -> git/ssh-keygen
    # subprocesses with 30s timeouts. Running it inline blocks the ASGI event
    # loop for minutes (8 published bots + redundant calls) and starves every
    # HTTP request, so it MUST run in an owned worker thread via the same
    # single-wakeup boundary the HTTP handlers use (AGENTS.md blocking boundary).
    active_v = await run_blocking_isolated(
        find_latest_rating_eligible_active_v,
        thread_name_prefix="prepare-pool-resolve",
    )
    active_bots = await run_blocking_isolated(
        get_active_bots,
        thread_name_prefix="prepare-pool-resolve",
    )
    # Re-open the namespace after active-pool discovery.  A paired tag/reset/
    # abandon transaction racing the first projection invalidates every source,
    # target, and bootstrap decision from that projection.  Offloaded to a
    # worker thread: same blocking git/ssh-keygen resolution as the first
    # projection above.
    try:
        _epoch_projection_second = await run_blocking_isolated(
            strict_epoch_projection,
            thread_name_prefix="prepare-epoch-projection",
        )
    except Exception as exc:
        log_system_event(
            "pipeline.prepare_namespace_second_read_failed",
            "error",
            "Prepare generation could not revalidate namespace authority",
            {"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
        )
        return None
    _namespace_fields = (
        "initialized",
        "published_high_water",
        "allocation_floor",
        "next_v",
        "abandoned_receipt_floor",
        "abandoned_receipt_head_digest",
        "active_bots",
        "active_generation",
        "ignored_checkpoint",
    )
    if any(
        _epoch_projection_second.get(field) != _epoch_projection.get(field)
        for field in _namespace_fields
    ) or sorted(active_bots) != sorted(_epoch_projection_second.get("active_bots") or []):
        log_system_event(
            "pipeline.prepare_namespace_drift",
            "warn",
            "Prepare generation stopped because namespace authority changed during discovery",
            {
                "first": {
                    field: _epoch_projection.get(field) for field in _namespace_fields
                },
                "second": {
                    field: _epoch_projection_second.get(field)
                    for field in _namespace_fields
                },
                "observed_active_bots": list(active_bots),
            },
        )
        return None
    if len(active_bots) <= 1:
        _cleanup_incomplete()
        if shutdown_mgr and shutdown_mgr.is_shutting_down:
            return None
        # _prepare_protocol_bootstrap_generation resolves the sole/zero active
        # bot via resolve_national_bot_spec (git/ssh-keygen subprocesses) and
        # writes the bootstrap checkpoint, so run it off the event loop
        # (AGENTS.md blocking boundary).
        return await run_blocking_isolated(
            _prepare_protocol_bootstrap_generation,
            active_bots=list(active_bots),
            current_v=current_v,
            next_v=_planned_next_v,
            allocation_floor=allocation_floor,
            abandoned_receipt_floor=_abandoned_floor,
            workflow_run_id=_prepare_workflow_run_id,
            ui=ui,
            slot_id=slot_id,
            thread_name_prefix="prepare-protocol-bootstrap",
        )
    if active_v <= 0 or not active_bots:
        log_system_event(
            "pipeline.prepare_no_active_source",
            "error",
            "Prepare generation found no tagged active bot source; skipping eval wait.",
            {"active_v": active_v, "active_bots": active_bots, "planned_next_v": _planned_next_v},
        )
        if ui:
            ui.log_history("没有可用的 tagged active bot，跳过本轮 prepare，避免等待 national_v000。", "error")
        return None
    active_bot_name = bot_name(active_v)   # 等待活跃 bot 的 eval（核心 fix）

    # Reap bots if pool exceeds limit — reduces starvation in match selection
    if len(active_bots) > MAX_ACTIVE_BOTS:
        from tool_bot_management import _do_reap_weakest
        reap_count = 0
        while (await run_blocking_isolated(
            lambda: len(get_active_bots()) > MAX_ACTIVE_BOTS,
            thread_name_prefix="prepare-pool-resolve",
        )) and reap_count < 10:
            try:
                result = await _do_reap_weakest(quiet=True)
                if not result.get("reaped"):
                    break
                if ui:
                    ui.log_history(f"淘汰 {result['culled']} (池 {result['remaining']}/{MAX_ACTIVE_BOTS})", "info")
            except Exception as e:
                log.warning("Pre-eval reap failed: %s\n%s", e, traceback.format_exc())
                if ui:
                    ui.log_history(f"淘汰失败: {e}", "warn")
                break
            reap_count += 1

    # Reaping is allowed to change both the pool and its latest active version.
    # Bind the wait target only after that mutation has finished; the post-wait
    # evidence loader checks the same identity again before planning.
    refreshed_active_bots = await run_blocking_isolated(
        get_active_bots,
        thread_name_prefix="prepare-pool-resolve",
    )
    refreshed_active_v = await run_blocking_isolated(
        find_latest_rating_eligible_active_v,
        thread_name_prefix="prepare-pool-resolve",
    )
    if refreshed_active_v <= 0 or not refreshed_active_bots:
        log_system_event(
            "pipeline.prepare_no_active_source",
            "error",
            "No tagged active bot remains after pre-evaluation reaping",
            {"planned_next_v": _planned_next_v},
        )
        return None
    if refreshed_active_v != active_v or refreshed_active_bots != active_bots:
        log_system_event(
            "pipeline.eval_wait_source_refreshed",
            "info",
            f"Evaluation source refreshed after reaping: v{active_v} -> v{refreshed_active_v}",
            {
                "before_active_v": active_v,
                "after_active_v": refreshed_active_v,
                "before_pool_size": len(active_bots),
                "after_pool_size": len(refreshed_active_bots),
            },
        )
    active_v = refreshed_active_v
    active_bots = refreshed_active_bots
    active_bot_name = bot_name(active_v)

    # Wait for sufficient evaluation
    eval_kwargs = {"ui": ui, "shutdown_event": shutdown_mgr}
    eval_kwargs["rd_threshold"] = workflow_profile.eval_wait_rd_threshold
    eval_kwargs["rd_min_games"] = workflow_profile.eval_wait_rd_min_games
    if min_games is None:
        eval_kwargs["min_games"] = workflow_profile.eval_wait_min_games
    if min_games is not None:
        eval_kwargs["min_games"] = min_games
    _ensure_priority_eval_signal(active_bot_name, eval_kwargs.get("min_games", MIN_GAMES_FOR_EVAL))
    # Deep-parallelism "LLM never stops" mode (2026-08-10): the primary lane
    # ALSO skips the blocking wait_for_daemon_eval, exactly like drafts. The
    # daemon continues rating in the background (for final delivery / pool
    # selection), but it no longer blocks the LLM evolution line. This is safe
    # because eval_wait was degrading 54/54 times anyway (it NEVER reached
    # min_games before timeout) — the primary was already using stale ratings.
    # Skipping the wait removes the multi-minute LLM-idle window before every
    # prepare. Controlled by POK_SKIP_PRIMARY_EVAL_WAIT (default 1 = skip on
    # this cloud runtime; set 0 to restore the blocking wait for debugging).
    import os as _os_eval
    _skip_primary_eval_wait = _os_eval.environ.get(
        "POK_SKIP_PRIMARY_EVAL_WAIT", "1"
    ).strip().lower() in {"1", "true", "yes", "on"}
    if slot_id is not None or _skip_primary_eval_wait:
        # Draft OR primary-in-skip-mode: don't block on daemon eval.
        eval_ok = True
        if ui:
            ui.log_history(
                "Speculative draft prepare: skipping daemon eval wait "
                f"(slot={slot_id}); using existing rating for {active_bot_name}",
                "info",
            )
    else:
        # Primary lane only: before blocking on the eval wait, verify the
        # selected source bot can ever accrue rating-pool games. A staging
        # master without a full signed certificate is structurally excluded
        # from the daemon's match queue (0 games), so wait_for_daemon_eval
        # would loop forever on an unreachable games floor. Fail closed with a
        # typed signal instead of silently degrading the floor. Drafts skip
        # this (they intentionally use stale ratings and run no matches).
        from bot_namespace import resolve_national_bot_spec, ROLE_RATING_POOL
        eval_spec = await run_blocking_isolated(
            resolve_national_bot_spec,
            active_bot_name,
            ROLE_RATING_POOL,
            thread_name_prefix="prepare-pool-resolve",
        )
        if not eval_spec.eligible:
            from orchestrator_cost_policy import EvalSourceRatingIneligible
            raise EvalSourceRatingIneligible(
                bot_name=active_bot_name,
                version=active_v,
                issues=tuple(eval_spec.issues),
                publication_tier=getattr(eval_spec, "publication_tier", None),
            )
        eval_ok = await wait_for_daemon_eval(active_bot_name, **eval_kwargs)
    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        return None
    if not eval_ok:
        if ui:
            ui.log_history("Waiting for evaluation (insufficient games)...", "info")
        return None

    # Cleanup incomplete bot dirs from previous interrupted cycles
    _cleanup_incomplete()
    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        return None

    # Freeze one daemon-published evaluation cycle before any planning role
    # runs. H2H, bot stats, ratings, and derived selection rows share the same
    # save_num and digest manifest.  Both snapshot calls read/parse/write JSON
    # from the results dir (and may rmtree + re-freeze on force=True), so they
    # run together in an owned worker thread (AGENTS.md blocking boundary).
    try:
        from evidence_snapshot import (
            ensure_generation_h2h_snapshot,
            load_generation_evaluation_snapshot,
        )

        def _freeze_h2h_sync():
            snapshot = ensure_generation_h2h_snapshot(
                _planned_next_v,
                force=_h2h_freeze_force(slot_id),
                spotlight_bot=active_bot_name,
            )
            bundle = (
                load_generation_evaluation_snapshot(_planned_next_v)
                if snapshot.get("available")
                else None
            )
            return snapshot, bundle

        h2h_snapshot, frozen_bundle = await run_blocking_isolated(
            _freeze_h2h_sync,
            thread_name_prefix="prepare-h2h-snapshot",
        )
        if not h2h_snapshot.get("available"):
            log_system_event(
                "pipeline.h2h_snapshot_unavailable",
                "error",
                f"Cannot freeze H2H evidence for v{_planned_next_v}",
                {
                    "next_v": _planned_next_v,
                    "reason": h2h_snapshot.get("reason"),
                    "issues": h2h_snapshot.get("issues", [])[:10],
                },
            )
            if ui:
                ui.log_history(
                    f"H2H evidence snapshot unavailable: {h2h_snapshot.get('reason')}",
                    "error",
                )
            return None
        if not frozen_bundle or not frozen_bundle.get("available"):
            raise RuntimeError(
                f"generation evaluation snapshot unavailable: "
                f"{(frozen_bundle or {}).get('reason')}"
            )
    except Exception as exc:
        log_system_event(
            "pipeline.h2h_snapshot_failed",
            "error",
            f"H2H evidence snapshot failed for v{_planned_next_v}",
            {"next_v": _planned_next_v, "error": f"{type(exc).__name__}: {str(exc)[:300]}"},
        )
        return None

    # _load_post_wait_evaluation_evidence is synchronous and internally
    # re-resolves the active pool and per-bot rating eligibility (git/ssh-keygen
    # subprocesses) 3+ times. Compute the expected_active_bots filter AND the
    # evidence load together in the worker thread so none of it touches the loop.
    _expected_min_games = int(eval_kwargs.get("min_games", MIN_GAMES_FOR_EVAL))
    _expected_rd_threshold = float(eval_kwargs.get("rd_threshold", 90.0))
    _expected_rd_min_games = int(eval_kwargs.get("rd_min_games", 30))

    def _freeze_eval_evidence_sync():
        expected = [b for b in active_bots if _is_rating_pool_eligible_bot(b)]
        return _load_post_wait_evaluation_evidence(
            active_v=active_v,
            active_bot_name=active_bot_name,
            min_games=_expected_min_games,
            rd_threshold=_expected_rd_threshold,
            rd_min_games=_expected_rd_min_games,
            expected_active_bots=expected,
            snapshot_bundle=frozen_bundle,
            allow_stale_readiness=slot_id is not None,
        )

    evidence = await run_blocking_isolated(
        _freeze_eval_evidence_sync,
        thread_name_prefix="prepare-eval-evidence",
    )
    if evidence is None:
        return None
    active_bots = list(evidence.active_bots)
    ratings = evidence.ratings
    frozen_h2h = evidence.h2h
    frozen_bot_stats = evidence.bot_stats
    try:
        selection_view = _build_selection_view(evidence)
    except Exception as exc:
        log_system_event(
            "pipeline.selection_view_failed",
            "error",
            f"Cannot compile frozen selection view for v{_planned_next_v}",
            {
                "next_v": _planned_next_v,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            },
        )
        return None

    # Combined analysis is compiled exclusively from the immutable generation
    # evidence bundle. Hand-level context is already a deterministic payload in
    # that same bundle; no second LLM may reopen live replay files here.
    from combined_analyst import _run_combined_analysis

    from llm_availability import LLMAvailabilityBlocked, gather_llm_fail_fast

    (combined_result,) = await gather_llm_fail_fast(
        _run_combined_analysis(
            active_v,
            active_bots,
            ratings,
            ui,
            frozen_h2h,
            frozen_bot_stats,
            list(evidence.selection_rows),
            list(evidence.rating_history_tail),
        ),
    )

    from orchestrator_cost_policy import (
        OperatorGenerationCostLimitExceeded,
        assert_operator_cost_limit_available,
    )
    for prepare_result in (combined_result,):
        if isinstance(prepare_result, OperatorGenerationCostLimitExceeded):
            raise prepare_result
    # A role may have translated an operator stop into a structured failure.
    # Re-read the system-owned ledger before any checkpoint is selected.
    assert_operator_cost_limit_available()

    if isinstance(combined_result, asyncio.CancelledError):
        log_system_event(
            "pipeline.prepare_llm_cancelled",
            "info",
            "Prepare-stage LLM analysis cancelled; prepare cycle will retry later",
            {
                "version": active_v,
                "combined_cancelled": isinstance(combined_result, asyncio.CancelledError),
            },
        )
        return None

    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        return None

    # Unpack results, treating exceptions as failures
    combined = combined_result if not isinstance(combined_result, BaseException) else None
    match_analysis = ""

    if isinstance(combined_result, BaseException):
        log.warning("Combined analysis failed: %s", combined_result)

    # Strategy decision (code-layer, deterministic)
    strategy, source_v, parents = _decide_strategy(
        combined,
        active_v,
        ratings,
        selection_view=selection_view,
    )

    # --- P1-1: Continuous Degeneration Diagnosis ---
    mechanical_urgent = _mechanical_urgent_intervention_eligible(
        bot_name(active_v),
        evidence.rating_history_tail,
        selection_view,
    )
    # A frozen numeric emergency is system-owned control flow.  Apply it before
    # asking an advisory model to explain the decline; an LLM timeout must not
    # suppress a mechanically proven recovery action.
    if mechanical_urgent:
        log_system_event(
            "pipeline.urgent_degeneration",
            "error",
            f"Mechanically proven urgent degeneration for v{active_v}",
            {
                "source_v": active_v,
                "trigger": "frozen_three_period_decline_and_leader_gap",
                "selection_view_digest": selection_view.digest,
            },
        )
        if strategy != "crossover":
            recovery_parents = _pick_crossover_parents(
                ratings,
                active_v,
                selection_view=selection_view,
            )
            if recovery_parents:
                strategy = "crossover"
                parents = recovery_parents
                source_v = recovery_parents[0]
                log_system_event(
                    "pipeline.degeneration_strategy_override",
                    "warn",
                    "Overriding strategy to crossover due to mechanical degeneration",
                    {
                        "parent_a": parents[0],
                        "parent_b": parents[1],
                        "selection_view_digest": selection_view.digest,
                    },
                )
            else:
                log_system_event(
                    "pipeline.degeneration_override_deferred",
                    "warn",
                    "Urgent degeneration found, but no frozen eligible crossover pair exists",
                    {"selection_view_digest": selection_view.digest},
                )
    if mechanical_urgent or (combined and combined.get("trend") == "declining"):
        try:
            from audit_agents import (
                _run_degeneration_diagnosis,
                _strict_completion_commit_history,
            )
            # Completion history is an exact annotated strict-tag allowlist.
            # A generic ``git log <tag> -5`` window can cross into ordinary
            # infrastructure, failed attempts, or the retired bot epoch and is
            # therefore inadmissible prompt evidence.
            try:
                recent_commits_text = _strict_completion_commit_history(limit=5)
            except Exception:
                recent_commits_text = ""

            # Build rating curve
            rating_curve_text = "\n".join(
                json.dumps(row, ensure_ascii=False)
                for row in evidence.rating_history_tail[-10:]
            )[:2000]

            diag = await _run_degeneration_diagnosis(
                active_v,
                recent_commits_text,
                (
                    "system_mechanical_urgent_intervention="
                    f"{str(bool(mechanical_urgent)).lower()}; "
                    "strategy changes are limited to the supplied strict commit window"
                ),
                rating_curve_text,
                ui,
            )
            if mechanical_urgent:
                log_system_event(
                    "pipeline.degeneration_diagnosis_attached",
                    "info",
                    "Advisory diagnosis attached to mechanical degeneration decision",
                    {"source_v": active_v, "diagnosis": diag},
                )
            elif diag.get("urgent_intervention"):
                log_system_event(
                    "pipeline.degeneration_override_rejected",
                    "warn",
                    "LLM urgent degeneration request lacked frozen mechanical evidence",
                    {
                        "source_v": active_v,
                        "diagnosis": diag,
                        "selection_view_digest": selection_view.digest,
                    },
                )
            elif diag.get("is_degenerating"):
                log_system_event("pipeline.degeneration_detected", "warn",
                                 f"Degeneration detected for v{active_v}: {diag.get('recommendation', '')}",
                                 {"source_v": active_v, "diagnosis": diag})
        except LLMAvailabilityBlocked:
            # The advisory role has already persisted the global provider
            # pause.  Never turn it into a safe-default and then publish the
            # selected checkpoint for a generation that was not fully prepared.
            raise
        except OperatorGenerationCostLimitExceeded:
            raise
        except Exception as e:
            log.warning("Degeneration diagnosis error (skipping): %s", e)

    stagnation_text = json.dumps(combined, ensure_ascii=False) if combined else ""
    if combined and combined.get("is_stagnant"):
        stagnation_text = ("STAGNATION_DETECTED (is_stagnant=true): You MUST call run_literature_probe BEFORE run_master "
                           "(governance-gated; if it returns skipped:true, proceed to run_master).\n" + stagnation_text)
    perf_text = stagnation_text  # Combined result serves as both
    match_text = match_analysis or ""

    # --- Replay Spotlight Analysis ---
    # Text and citations were derived while the evidence snapshot was created
    # and are now covered by its file hash and manifest digest.
    spotlight_payload = frozen_bundle.get("replay_spotlight") or {}
    spotlight_text = ""
    if (
        isinstance(spotlight_payload, dict)
        and spotlight_payload.get("bot") == active_bot_name
        and spotlight_payload.get("evaluation_identity_digest")
        == h2h_snapshot.get("evaluation_identity_digest")
    ):
        spotlight_text = str(spotlight_payload.get("text") or "")

    # --- P1-2: H2H Anomaly Root Cause Analysis ---
    # NOTE (root-cause-audit 2026-06-17): the stored `win_rate` field is the
    # win rate of the lexicographically-FIRST bot in the pair_key (see
    # elo_daemon pair_key: "a vs b" if a < b), NOT necessarily active_v's win
    # rate. Reading it directly inverts the sign when active_v is the "b" side
    # (e.g. "national_v104 vs national_v114" stores 0.35 = v104's rate, which was
    # mis-attributed to v114 as a fake regression). Fix: recompute active_v's
    # win rate from a_wins/b_wins by pair position — the same perspective
    # correction compute_h2h_avg_winrate (tool_helpers) already applies.
    if combined:
        try:
            if frozen_h2h:
                h2h_data = frozen_h2h
                regressions = []    # active_v LOSING — genuine concern for Master
                dominations = []    # active_v WINNING — informational only, NOT "attention"
                v_key = bot_name(active_v)
                active_bot_set = set(active_bots)
                for pair_key, pair_data in h2h_data.items():
                    parts = pair_key.split(" vs ")
                    if len(parts) != 2 or v_key not in parts:
                        continue
                    games = pair_data.get("games", 0)
                    if games < 20:
                        continue
                    # Perspective-correct win rate for active_v.
                    if parts[0] == v_key:
                        bot_wins = pair_data.get("a_wins", 0)
                        opp = parts[1]
                    else:
                        bot_wins = pair_data.get("b_wins", 0)
                        opp = parts[0]
                    if opp not in active_bot_set:
                        continue
                    draws = int(pair_data.get("draws", 0) or 0)
                    wr = match_score(bot_wins, draws, games)
                    if wr is None:
                        continue
                    delta = wr - 0.5
                    lb = _wilson_lower_bound(bot_wins + 0.5 * draws, games)
                    entry = {
                        "opponent": opp,
                        "win_rate": round(wr, 3),
                        "games": games,
                        "delta": round(delta, 2),
                        "ci_lower": round(lb, 3),
                    }
                    # Single-sided regression: only flag matchups active_v is
                    # genuinely LOSING (wr < 0.40, i.e. delta < -0.10). The old
                    # abs(wr-0.5)>0.15 two-sided threshold treated "crushing a
                    # weak opponent" as a problem and, combined with the
                    # perspective bug, manufactured fake regressions. The Wilson
                    # lower bound is kept as a confidence column but not a hard
                    # gate (n=20 makes it too wide to discriminate — a 0.50
                    # point estimate already has lb≈0.30).
                    if wr < 0.40:
                        regressions.append(entry)
                    elif delta > 0.15:
                        dominations.append(entry)
                # Most-severe regressions first (lowest win rate), so the
                # top-5 surfaced to Master are the most diagnostic.
                regressions.sort(key=lambda a: a["win_rate"])
                if regressions:
                    log_system_event("pipeline.h2h_anomaly", "warn",
                                     f"H2H regressions for v{active_v}: {len(regressions)} "
                                     f"matchups with win rate < 40% (genuine losses); "
                                     f"{len(dominations)} strong dominations (informational)",
                                     {"source_v": active_v, "regressions": regressions[:5],
                                      "domination_count": len(dominations)})
                    # Inject into stagnation_text for Master context
                    anomaly_text = "\n\n## H2H Regression Alert\n"
                    for a in regressions[:5]:
                        anomaly_text += (
                            f"- vs {a['opponent']}: WR={a['win_rate']:.0%} "
                            f"(delta={a['delta']:+.0%}, {a['games']}g, "
                            f"95% CI lower={a['ci_lower']:.0%})\n"
                        )
                    anomaly_text += (
                        "These matchups show genuine regression — "
                        "prioritize fixing them in the next generation.\n"
                    )
                    stagnation_text += anomaly_text
        except Exception as e:
            log.warning("H2H anomaly check error (skipping): %s", e)

    # Record the final next_v decision with the exact authoritative inputs. This
    # is scheduler selection, not publication proof or directory authority.
    _final_next_v = _planned_next_v
    try:
        log_system_event(
            "pipeline.generation_selected", "info",
            f"Selected v{_final_next_v} from v{source_v} (strategy={strategy[:40]})",
            {"next_v": _final_next_v, "current_v": current_v,
             "published_high_water": current_v,
             "allocation_floor": allocation_floor,
             "abandoned_receipt_floor": _abandoned_floor,
             "next_v_authority": _epoch_projection.get("next_v_authority"),
             "source_v": source_v, "strategy": strategy[:80],
             "selection_stage": "selected",
             "next_step": "prepare_next_gen_or_run_crossover"},
        )
    except Exception:
        pass

    try:
        from evolution_infra import write_pipeline_checkpoint
        from master_context_contract import build_master_context

        parent2_v = parents[1] if parents and len(parents) > 1 else None
        master_context = build_master_context(
            next_v=_final_next_v,
            source_v=source_v,
            stagnation_info=stagnation_text,
            match_analysis=match_text,
            performance_verification=perf_text,
        )
        ok = write_pipeline_checkpoint(
            _final_next_v,
            source_v,
            "selected",
            workflow_run_id=_prepare_workflow_run_id,
            parent2_v=parent2_v,
            audit_context={
                "selection": {
                    "strategy": strategy[:80],
                    "current_v": current_v,
                    "published_high_water": current_v,
                    "allocation_floor": allocation_floor,
                    "abandoned_receipt_floor": _abandoned_floor,
                    "next_v_authority": _epoch_projection.get("next_v_authority"),
                    "parent_a": parents[0] if parents else None,
                    "parent_b": parent2_v,
                    "h2h_snapshot_manifest_digest": h2h_snapshot.get("manifest_digest"),
                    "h2h_snapshot_sha256": h2h_snapshot.get("sha256"),
                    "selection_view_source_history": list(
                        selection_view.source_history
                    ),
                    "evaluation_evidence": {
                        "bot": active_bot_name,
                        "games": evidence.games,
                        "rd": round(evidence.rd, 2),
                        "readiness_reason": evidence.readiness_reason,
                        "cutoffs": evidence.cutoffs,
                        "selection_view_digest": selection_view.digest,
                    },
                },
                # System-owned exact handoff.  The outer orchestrator may show
                # these strings to the user/model, but run_master reloads this
                # digest-bound copy instead of trusting an LLM transcription.
                "master_context": master_context,
            },
            publication_tier=_default_publication_tier(next_v=_final_next_v),
            slot_id=slot_id,
        )
    except Exception as exc:
        log_system_event(
            "pipeline.generation_selection_checkpoint_failed",
            "error",
            f"Failed to persist selected checkpoint for v{_final_next_v}: {exc}",
            {"next_v": _final_next_v, "source_v": source_v, "strategy": strategy[:80]},
        )
        raise
    if not ok:
        log_system_event(
            "pipeline.generation_selection_checkpoint_failed",
            "error",
            f"Checkpoint write refused after selecting v{_final_next_v}",
            {"next_v": _final_next_v, "source_v": source_v, "strategy": strategy[:80]},
        )
        raise RuntimeError(f"selected checkpoint refused for v{_final_next_v}")

    return GenerationContext(
        current_v=current_v,
        next_v=_final_next_v,
        strategy=strategy,
        source_v=source_v,
        crossover_parents=parents,
        stagnation_info=stagnation_text,
        match_analysis=match_text,
        performance_verification=perf_text,
        replay_spotlight=spotlight_text,
        gen_count=current_v,
    )


def _log_crossover_decision(
    trigger,
    source_v,
    parents,
    cons_a=None,
    cons_b=None,
    ratings=None,
    selection_view=None,
):
    """LOG GAP FIX (2026-06-30): record WHY crossover was chosen + which parents,
    so the parent-selection rationale is auditable (previously only the result was
    logged via pipeline.generation_selected's strategy field)."""
    try:
        parent_a_metrics = _strength_payload(
            parents[0], ratings=ratings, selection_view=selection_view
        )
        parent_b_metrics = _strength_payload(
            parents[1], ratings=ratings, selection_view=selection_view
        )
        log_system_event(
            "pipeline.crossover_decided", "info",
            f"Crossover decided (trigger={trigger}): v{parents[0]}×v{parents[1]} "
            f"(source v{source_v})",
            {"trigger": trigger, "source_v": source_v,
             "parent_a": parents[0], "parent_b": parents[1],
             "version_gap": abs(parents[0] - parents[1]),
             "conservative_a": round(cons_a, 0) if cons_a else None,
             "conservative_b": round(cons_b, 0) if cons_b else None,
             "parent_a_metrics": parent_a_metrics,
             "parent_b_metrics": parent_b_metrics},
        )
    except Exception:
        pass


def _strength_payload(version, ratings=None, selection_view=None):
    name = bot_name(version)
    payload = {"bot": name}
    if isinstance(selection_view, SelectionView):
        rating = selection_view.rating_values.get(name)
        if rating is not None:
            payload.update({
                "glicko_r": round(float(rating[0]), 1),
                "glicko_rd": round(float(rating[1]), 1),
                "conservative_rating": round(float(rating[3]), 1),
            })
        metrics = selection_view.metrics.get(name)
        if metrics is not None:
            h2h = metrics.get("h2h_avg_wr")
            if h2h is not None:
                payload["h2h_avg_wr"] = round(float(h2h), 4)
            payload["h2h_games"] = int(metrics.get("h2h_games", 0) or 0)
            payload["h2h_opponents"] = int(
                metrics.get("opponents_evaluated", 0) or 0
            )
            payload["selection_score"] = round(
                float(selection_view.selection_scores.get(name, 0.0)), 4
            )
        return payload
    rating = (ratings or {}).get(name) if isinstance(ratings, dict) else None
    if rating is not None:
        try:
            payload["conservative_rating"] = round(float(rating.conservative_rating()), 1)
        except Exception:
            pass
        r = getattr(rating, "r", None)
        rd = getattr(rating, "rd", None)
        if r is not None:
            try:
                payload["glicko_r"] = round(float(r), 1)
            except Exception:
                pass
        if rd is not None:
            try:
                payload["glicko_rd"] = round(float(rd), 1)
            except Exception:
                pass
    try:
        from tool_helpers import _load_h2h_data, compute_h2h_avg_winrate
        h2h_data = _load_h2h_data()
        h2h = compute_h2h_avg_winrate(name, h2h_data)
        if h2h is not None:
            payload["h2h_avg_wr"] = round(float(h2h), 4)
        h2h_games = 0
        opponents = set()
        for key, value in h2h_data.items():
            parts = key.split(" vs ")
            if len(parts) != 2 or name not in parts:
                continue
            games = int(value.get("games", 0) or 0)
            h2h_games += games
            opponents.update(part for part in parts if part != name)
        payload["h2h_games"] = h2h_games
        payload["h2h_opponents"] = len(opponents)
        return payload
    except Exception:
        return payload


def _log_source_selection_decision(
    trigger,
    selected_v,
    current_v,
    combined=None,
    ratings=None,
    selection_view=None,
):
    try:
        log_system_event(
            "pipeline.source_selection_decided",
            "info",
            f"Source selected by {trigger}: v{selected_v} (latest v{current_v})",
            {
                "trigger": trigger,
                "selected_source_v": selected_v,
                "current_v": current_v,
                "llm_recommended_source": (combined or {}).get("recommended_source"),
                "source_rationale": (combined or {}).get("source_rationale"),
                "selected_metrics": _strength_payload(
                    selected_v, ratings=ratings, selection_view=selection_view
                ),
                "current_metrics": _strength_payload(
                    current_v, ratings=ratings, selection_view=selection_view
                ),
            },
        )
    except Exception:
        pass


def _active_source_versions(selection_view=None) -> set[int]:
    """Return active source versions backed by normal completion discovery."""
    if isinstance(selection_view, SelectionView):
        return set(selection_view.active_versions)
    try:
        from evolution_infra import get_active_bots
        versions = set()
        for bot_name in get_active_bots():
            version = _parse_branch_from(bot_name)
            if version is not None:
                versions.add(version)
        return versions
    except Exception:
        return set()


def _log_source_selection_rejected(trigger, requested_v, current_v, reason, combined=None):
    try:
        log_system_event(
            "pipeline.source_selection_rejected",
            "warn",
            f"Rejected source selected by {trigger}: v{requested_v} ({reason}); latest v{current_v} remains fallback",
            {
                "trigger": trigger,
                "requested_source_v": requested_v,
                "current_v": current_v,
                "reason": reason,
                "llm_recommended_source": (combined or {}).get("recommended_source"),
                "branch_from": (combined or {}).get("branch_from"),
                "source_rationale": (combined or {}).get("source_rationale"),
            },
        )
    except Exception:
        pass


_COMBINED_CONFIDENCE = {"low", "medium", "high"}
_COMBINED_TRENDS = {"improving", "stagnant", "declining", "unknown"}
_COMBINED_RECOMMENDATIONS = {
    "continue", "crossover", "branch", "branch_from", "force_exploration",
}


def _normalize_combined_control(combined):
    """Delegate to generation_scheduler_source_selection."""
    return _gs._normalize_combined_control(combined)


def _deterministic_fallback_source(current_v, ratings, selection_view):
    """Delegate to generation_scheduler_source_selection."""
    return _gs._deterministic_fallback_source(current_v, ratings, selection_view)


def _llm_source_eligibility(requested_v, selection_view):
    """Delegate to generation_scheduler_source_selection."""
    return _gs._llm_source_eligibility(requested_v, selection_view)


def _decide_strategy(combined, current_v, ratings, *, selection_view=None):
    """Delegate to generation_scheduler_source_selection."""
    return _gs._decide_strategy(combined, current_v, ratings, selection_view=selection_view)


def _parse_branch_from(branch_str: str) -> int | None:
    """Delegate to generation_scheduler_source_selection."""
    return _gs._parse_branch_from(branch_str)


def _read_source_v_history():
    """Delegate to generation_scheduler_source_selection."""
    return _gs._read_source_v_history()


def _detect_source_loop(n=3):
    """Delegate to generation_scheduler_source_selection."""
    return _gs._detect_source_loop(n=n)


def _source_loop_from_history(sources, *, n=3):
    """Delegate to generation_scheduler_source_selection."""
    return _gs._source_loop_from_history(sources, n=n)


def _detect_source_oscillation(n=8, max_unique=3):
    """Delegate to generation_scheduler_source_selection."""
    return _gs._detect_source_oscillation(n=n, max_unique=max_unique)


def _source_oscillation_from_history(sources, *, n=8, max_unique=3):
    """Delegate to generation_scheduler_source_selection."""
    return _gs._source_oscillation_from_history(sources, n=n, max_unique=max_unique)


def _get_unified_leader_v(ratings, selection_view=None):
    """Delegate to generation_scheduler_source_selection."""
    return _gs._get_unified_leader_v(ratings, selection_view)


def _pick_oscillation_breakout_source(oscillating, current_v, selection_view=None):
    """Delegate to generation_scheduler_source_selection."""
    return _gs._pick_oscillation_breakout_source(oscillating, current_v, selection_view=selection_view)


def _pick_crossover_parents(ratings, current_v, selection_view=None):
    """Delegate to generation_scheduler_source_selection."""
    return _gs._pick_crossover_parents(ratings, current_v, selection_view=selection_view)


def _bare_commit_gate_ledger_ok(v, ckpt):
    """Return (ok, reason) for recovering an interrupted commit_bot run."""
    if not ckpt:
        return False, "missing_checkpoint"
    if int(ckpt.get("next_v") or -1) != int(v):
        return False, "checkpoint_version_mismatch"
    if ckpt.get("stage") not in {"verified", "archived"}:
        return False, f"stage_not_verified:{ckpt.get('stage')}"

    source_v = ckpt.get("source_v")
    if source_v is None:
        return False, "missing_source_v"

    try:
        from tool_commit import get_bot_dir, validate_commit_gate_ledger
        ledger = validate_commit_gate_ledger(v, source_v, ckpt, bot_dir=get_bot_dir(v))
    except Exception as exc:
        return False, f"gate_ledger_validation_error:{type(exc).__name__}:{str(exc)[:120]}"

    if not ledger.get("ok"):
        missing = ",".join(ledger.get("missing_gates") or [])
        failed = ",".join(
            f"{item.get('gate')}:{item.get('reason')}" if isinstance(item, dict) else str(item)
            for item in (ledger.get("failed_gates") or [])
        )
        return False, f"gate_ledger_failed:missing={missing};failed={failed}"
    # Certification system removed: the official-full gate / certificate-stale
    # precheck no longer runs.  Bare-commit recovery relies solely on the gate
    # ledger validated above plus the durable publication intent.
    return True, ""


def _finalize_bare_commit(v, ckpt=None):
    """Resume only a content-bound durable publication transaction.

    Historical "bare commit" inference is intentionally retired.  A tracked bot
    without an intent does not prove which certificate/gates authorized that
    commit.  New publication always records ``publishing`` before Git mutation,
    so restart recovery can invoke the same idempotent transaction without
    inventing authority from repository shape.
    """
    try:
        from evolution_infra import git_has_tag
        from tool_commit import _resume_publication_transaction, get_bot_dir
    except Exception as e:
        log.warning("_finalize_bare_commit imports failed for v%d: %s", v, e)
        return False
    bot_dir = get_bot_dir(v)
    if not bot_dir.exists():
        return False
    if (
        not isinstance(ckpt, dict)
        or ckpt.get("stage") != "publishing"
        or not isinstance(ckpt.get("publication_intent"), dict)
        or int(ckpt.get("next_v") or -1) != int(v)
    ):
        log.warning(
            "bare-commit v%d has no durable publication intent — leaving dir intact.",
            v,
        )
        try:
            log_system_event(
                "pipeline.bare_commit_finalize_blocked",
                "warn",
                f"Bare-commit recovery blocked for v{v}: missing publication intent",
                {"version": v, "reason": "missing_publication_intent", "checkpoint_stage": (ckpt or {}).get("stage")},
            )
        except Exception:
            pass
        return False
    source_v = ckpt.get("source_v")
    try:
        result = _resume_publication_transaction(v, source_v, ckpt)
        if result.get("committed") is not True:
            log.warning(
                "publication recovery for v%d remains pending: %s",
                v,
                result.get("error") or result,
            )
            return False
        log.info("Recovered durable publication v%d (source v%d)", v, source_v)
        try:
            log_system_event("pipeline.bare_commit_finalized", "success",
                             f"Recovered durable publication v{v} (source v{source_v})",
                             {"version": v, "source_v": source_v,
                              "publication_id": result.get("publication_id")})
        except Exception:
            pass
        return True
    except Exception as e:
        log.warning("H3: finalize failed for v%d (%s) — leaving dir intact", v, e)
        return False


def _cleanup_incomplete():
    """Report incomplete directories; mutation requires canonical abandon CAS.

    Directory names, missing tags, and a best-effort checkpoint read are not
    cleanup authority.  The former direct ``rmtree`` path raced publication and
    could delete a partial-tag or just-committed candidate.  Active cleanup is
    now exclusively owned by ``_do_abandon_generation`` under the shared
    publication lock and durable claim transaction.
    """
    from evolution_infra import PROJECT_ROOT

    bots_dir = PROJECT_ROOT / "bots"
    if not bots_dir.exists():
        return []
    observed = []
    for d in sorted(bots_dir.iterdir()):
        if d.is_dir() and d.name.startswith(ACTIVE_BOT_PREFIX):
            if not (d / ".completed").exists():
                v = parse_bot_version(d.name)
                if v is None:
                    continue
                observed.append(v)
                log.warning(
                    "Preserving incomplete v%d until canonical checkpoint-bound "
                    "abandon or publication reconciliation proves authority.",
                    v,
                )
    return observed


async def post_generation_cleanup(shutdown_mgr, ui, ctx: GenerationContext):
    """Phase 3: Idempotent post-generation cleanup."""
    from evolution_infra import MAX_ACTIVE_BOTS, get_active_bots, git_has_tag

    cleanup_started = time.time()

    def _finish(status: str = "done", reason: str = ""):
        elapsed = time.time() - cleanup_started
        severity = "info" if status in {"done", "skipped"} else "warn"
        log_system_event(
            "pipeline.post_cleanup_done",
            severity,
            f"Post-cleanup {status} for v{ctx.next_v} in {elapsed:.1f}s",
            {
                "version": ctx.next_v,
                "source_v": ctx.source_v,
                "status": status,
                "reason": reason,
                "elapsed_sec": round(elapsed, 2),
            },
        )

    log_system_event(
        "pipeline.post_cleanup_start",
        "info",
        f"Post-cleanup starting for v{ctx.next_v}",
        {"version": ctx.next_v, "source_v": ctx.source_v, "strategy": ctx.strategy},
    )

    if shutdown_mgr and shutdown_mgr.is_shutting_down:
        _finish("skipped", "shutdown_before_start")
        return

    from post_publication_handoff import pending_handoff_route

    handoff = pending_handoff_route()
    if handoff.get("status") != "none":
        _finish("blocked", "post_publication_handoff_incomplete")
        raise RuntimeError(
            "post_generation_cleanup_requires_completed_archivist_handoff:"
            + ";".join(handoff.get("issues") or [str(handoff.get("status"))])
        )
    committed_generation = ctx.next_v > 0 and git_has_tag(ctx.next_v)
    if not committed_generation:
        _finish("skipped", "not_committed_or_abandoned")
        return
    # All reap, rotation, log archival, annotation, stability, and signal
    # effects are journaled by run_archivist. Phase 3 only verifies that the
    # obligation is complete; duplicating them here would make resume behavior
    # differ from the initial publication path.
    _finish("done")
    return
