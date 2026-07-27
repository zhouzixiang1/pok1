"""Weakest-bot reaping subsystem for tool_bot_management.

Extracted as a cohesive business cluster; ``tool_bot_management.py`` retains
thin delegate shells so external ``from tool_bot_management import <name>``
and ``monkeypatch.setattr(tool_bot_management, "<name>", ...)`` keep
resolving.

Business responsibility (single cohesive domain):
* ``ReapWeakestInput`` typed-dict.
* Conservative-Glicko reap selection snapshot capture/validate/decode
  (``_finite_float_hex``, ``_decode_finite_float_hex``,
  ``_is_strict_canonical_bot_name``, ``_validate_reap_selection_snapshot``,
  ``_capture_reap_selection_snapshot``,
  ``_select_reap_candidate_from_snapshot``, ``_select_reap_candidate``).
* Async reap execution (``_do_reap_weakest``) and its MCP entry point
  (``reap_weakest``).
* MCP result envelope helper (``_mcp_result``).
* Strict-workflow incomplete-cleanup entry point (``cleanup_incomplete``).

Cross-references to symbols that remain in ``tool_bot_management`` (the
``REAP_SELECTION_POLICY`` / ``_REAP_SNAPSHOT_KEYS`` / ``_REAP_BOT_INPUT_KEYS``
constants, the ``get_active_bots`` / ``get_bot_dir`` / ``git_dir_is_committed``
/ ``git_has_tag`` / ``load_ratings`` / ``MAX_ACTIVE_BOTS`` / ``RESULTS_DIR``
/ ``BOTS_DIR`` / ``Glicko2Player`` evolution-core helpers, the
``bot_name`` / ``parse_bot_version`` bot-namespace helpers, the
``load_h2h_avg_winrates`` / ``load_strength_scores`` / ``PROJECT_ROOT``
tool-helpers, the ``canonical_digest`` / ``read_pipeline_checkpoint`` /
``record_reaped_bot`` / ``log_system_event`` imports, the ``tool`` decorator,
and the ``_do_abandon_generation`` async helper) are reached through
``_tbm.<name>`` so that test monkeypatches on ``tool_bot_management.<name>``
propagate.

CRITICAL (wave-3 lesson): EVERY intra-companion call to a moved symbol ALSO
routes through ``_tbm.<name>(...)`` so monkeypatches on
``tool_bot_management.<name>`` propagate even when both call sites now live
in this companion.  ``_do_reap_weakest`` is async; callers ``await`` its
delegate.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import stat
import time
from typing import TypedDict

from bot_namespace import FIRST_STRICT_POLICY_VERSION

import tool_bot_management as _tbm  # for cross-refs


class ReapWeakestInput(TypedDict):
    pass



def _finite_float_hex(value, *, field: str) -> str:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"reap_selection_{field}_invalid") from exc
    if not math.isfinite(normalized):
        raise RuntimeError(f"reap_selection_{field}_non_finite")
    return normalized.hex()


def _decode_finite_float_hex(value, *, field: str) -> float:
    if not isinstance(value, str):
        raise RuntimeError(f"reap_selection_{field}_not_hex")
    try:
        normalized = float.fromhex(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"reap_selection_{field}_not_hex") from exc
    if not math.isfinite(normalized) or normalized.hex() != value:
        raise RuntimeError(f"reap_selection_{field}_not_canonical")
    return normalized


def _is_strict_canonical_bot_name(value) -> bool:
    if not isinstance(value, str):
        return False
    version = _tbm.parse_bot_version(value)
    return (
        version is not None
        and version >= FIRST_STRICT_POLICY_VERSION
        and value == _tbm.bot_name(version)
    )


def _validate_reap_selection_snapshot(snapshot: dict) -> dict[str, dict]:
    """Validate and decode one immutable conservative-Glicko preimage."""

    if not isinstance(snapshot, dict) or set(snapshot) != _tbm._REAP_SNAPSHOT_KEYS:
        raise RuntimeError("reap_selection_snapshot_keys_invalid")
    if (
        type(snapshot.get("schema_version")) is not int
        or snapshot["schema_version"] != 1
        or snapshot.get("kind") != "strict-active-pool-selection-snapshot"
        or snapshot.get("selection_policy") != _tbm.REAP_SELECTION_POLICY
        or type(snapshot.get("max_active_bots")) is not int
        or snapshot["max_active_bots"] < 1
        or not isinstance(snapshot.get("active_bots"), list)
        or not isinstance(snapshot.get("bot_inputs"), list)
    ):
        raise RuntimeError("reap_selection_snapshot_contract_invalid")
    active_bots = snapshot["active_bots"]
    if (
        active_bots != sorted(active_bots)
        or len(active_bots) != len(set(active_bots))
        or any(not _tbm._is_strict_canonical_bot_name(name) for name in active_bots)
        or snapshot.get("active_pool_digest") != _tbm.canonical_digest(active_bots)
    ):
        raise RuntimeError("reap_selection_snapshot_pool_invalid")
    priority_bot = snapshot.get("priority_bot")
    if priority_bot is not None and (
        not isinstance(priority_bot, str) or priority_bot not in active_bots
    ):
        raise RuntimeError("reap_selection_snapshot_priority_invalid")
    rows = snapshot["bot_inputs"]
    if (
        len(rows) != len(active_bots)
        or snapshot.get("bot_inputs_digest") != _tbm.canonical_digest(rows)
    ):
        raise RuntimeError("reap_selection_snapshot_inputs_invalid")
    decoded: dict[str, dict] = {}
    for expected_name, row in zip(active_bots, rows):
        if not isinstance(row, dict) or set(row) != _tbm._REAP_BOT_INPUT_KEYS:
            raise RuntimeError("reap_selection_snapshot_input_keys_invalid")
        if row.get("bot") != expected_name:
            raise RuntimeError("reap_selection_snapshot_input_order_invalid")
        if type(row.get("games")) is not int or row["games"] < 0:
            raise RuntimeError("reap_selection_snapshot_games_invalid")
        decoded[expected_name] = {
            "r": _tbm._decode_finite_float_hex(
                row.get("rating_r_hex"), field="rating_r"
            ),
            "rd": _tbm._decode_finite_float_hex(
                row.get("rating_rd_hex"), field="rating_rd"
            ),
            "games": row["games"],
            "leaderboard_score": _tbm._decode_finite_float_hex(
                row.get("leaderboard_score_hex"), field="leaderboard_score"
            ),
            "h2h_avg_wr": _tbm._decode_finite_float_hex(
                row.get("h2h_avg_wr_hex"), field="h2h_avg_wr"
            ),
        }
    unsigned = {
        key: value for key, value in snapshot.items() if key != "snapshot_digest"
    }
    if snapshot.get("snapshot_digest") != _tbm.canonical_digest(unsigned):
        raise RuntimeError("reap_selection_snapshot_digest_invalid")
    return decoded


def _capture_reap_selection_snapshot(
    active_bots=None,
    *,
    max_active_bots: int | None = None,
) -> dict:
    """Freeze every input used by the active conservative-Glicko policy."""

    from evaluation_bundle import evaluation_cycle_lock
    from tool_helpers import _read_json

    cap = _tbm.MAX_ACTIVE_BOTS if max_active_bots is None else max_active_bots
    if type(cap) is not int or cap < 1:
        raise RuntimeError("reap_selection_max_active_bots_invalid")
    with evaluation_cycle_lock(_tbm.RESULTS_DIR, exclusive=False):
        names = sorted(
            _tbm.get_active_bots() if active_bots is None else list(active_bots)
        )
        if (
            len(names) != len(set(names))
            or any(not _tbm._is_strict_canonical_bot_name(name) for name in names)
        ):
            raise RuntimeError("reap_selection_active_pool_invalid")
        ratings = _tbm.load_ratings()
        h2h_winrates = _tbm.load_h2h_avg_winrates()
        strength_scores = _tbm.load_strength_scores()
        bot_stats = _read_json(_tbm.RESULTS_DIR / "bot_stats.json", {})
        priority_data = _read_json(_tbm.RESULTS_DIR / "priority_eval.json", {})
        priority_bot = (
            priority_data.get("bot")
            if isinstance(priority_data, dict)
            else None
        )
        if priority_bot not in names:
            priority_bot = None
        rows = []
        for name in names:
            rating = ratings.get(name, _tbm.Glicko2Player())
            try:
                games = int((bot_stats.get(name) or {}).get("games", 0) or 0)
            except (AttributeError, TypeError, ValueError) as exc:
                raise RuntimeError("reap_selection_games_invalid") from exc
            if games < 0:
                raise RuntimeError("reap_selection_games_invalid")
            rows.append({
                "bot": name,
                "rating_r_hex": _tbm._finite_float_hex(
                    getattr(rating, "r", None), field="rating_r"
                ),
                "rating_rd_hex": _tbm._finite_float_hex(
                    getattr(rating, "rd", None), field="rating_rd"
                ),
                "games": games,
                "leaderboard_score_hex": _tbm._finite_float_hex(
                    strength_scores.get(name, 0.0), field="leaderboard_score"
                ),
                "h2h_avg_wr_hex": _tbm._finite_float_hex(
                    h2h_winrates.get(name, 0.0), field="h2h_avg_wr"
                ),
            })
    snapshot = {
        "schema_version": 1,
        "kind": "strict-active-pool-selection-snapshot",
        "selection_policy": _tbm.REAP_SELECTION_POLICY,
        "max_active_bots": cap,
        "active_bots": names,
        "active_pool_digest": _tbm.canonical_digest(names),
        "priority_bot": priority_bot,
        "bot_inputs": rows,
        "bot_inputs_digest": _tbm.canonical_digest(rows),
    }
    snapshot["snapshot_digest"] = _tbm.canonical_digest(snapshot)
    _tbm._validate_reap_selection_snapshot(snapshot)
    return snapshot


def _select_reap_candidate_from_snapshot(
    snapshot: dict,
    active_bots=None,
) -> dict:
    """Purely select the next target from one validated frozen preimage."""

    inputs = _tbm._validate_reap_selection_snapshot(snapshot)
    active_bots = list(
        snapshot["active_bots"] if active_bots is None else active_bots
    )
    if (
        len(active_bots) != len(set(active_bots))
        or not set(active_bots).issubset(inputs)
    ):
        raise RuntimeError("reap_selection_runtime_pool_invalid")
    cap = snapshot["max_active_bots"]
    if len(active_bots) <= cap:
        return {"candidate": None, "pool_size": len(active_bots)}

    current_bot = max(
        active_bots,
        key=lambda name: _tbm.parse_bot_version(name) or -1,
    )

    # Exclude the current/latest source and the newest few active bots; they are
    # either being evolved from or still need fresh evaluation.
    protected_recent = set()
    if len(active_bots) > cap + 3:
        protected_recent = set(sorted(
            active_bots,
            key=lambda name: _tbm.parse_bot_version(name) or -1,
        )[-3:])
    protected_names = {current_bot, *protected_recent}
    if snapshot["priority_bot"] in active_bots:
        protected_names.add(snapshot["priority_bot"])

    evaluated_candidates = []
    zero_game_candidates = []
    for name in active_bots:
        if name in protected_names:
            continue
        row = inputs[name]
        candidate = (name, row["r"], row["rd"], row["games"])
        if row["games"] == 0:
            zero_game_candidates.append(candidate)
            continue
        evaluated_candidates.append(candidate)

    # Soft overflow avoids untested candidates; hard overflow selects old
    # zero-game candidates before allowing the pool to grow without bound.
    if len(active_bots) <= cap + 3:
        candidates = evaluated_candidates
    else:
        candidates = evaluated_candidates + zero_game_candidates
    if not candidates:
        return {
            "candidate": None,
            "reason": "All remaining bots are current, recent, priority, or protected untested",
            "protected": sorted(protected_names),
        }

    protected = {name for name, _r, _rd, games in candidates if games < 600}
    if len(active_bots) <= cap + 3:
        candidates = [row for row in candidates if row[0] not in protected]
        if not candidates:
            return {
                "candidate": None,
                "reason": "all_protected",
                "remaining": len(active_bots),
                "protected_count": len(protected),
            }

    candidates.sort(key=lambda row: (
        row[1] - 2 * row[2],
        row[3],
        _tbm.parse_bot_version(row[0]) or 0,
    ))
    name, rating_r, rating_rd, _games = candidates[0]
    frozen = inputs[name]
    return {
        "candidate": name,
        "selection_key": "conservative_glicko",
        "conservative_rating": round(rating_r - 2 * rating_rd, 1),
        "leaderboard_score": round(frozen["leaderboard_score"], 4),
        "h2h_avg_wr": round(frozen["h2h_avg_wr"], 4),
        "rating": {"r": round(rating_r, 1), "rd": round(rating_rd, 1)},
        "active_pool": sorted(active_bots),
    }


def _select_reap_candidate(active_bots=None) -> dict:
    """Return the exact next reap target without performing a side effect."""

    snapshot = _tbm._capture_reap_selection_snapshot(active_bots)
    return _tbm._select_reap_candidate_from_snapshot(snapshot, active_bots)


async def _do_reap_weakest(
    quiet: bool = False,
    *,
    expected_culled: str | None = None,
    selection_snapshot: dict | None = None,
) -> dict:
    """Core reaping logic, optionally fenced to a preplanned target."""

    active_bots = _tbm.get_active_bots()
    snapshot = (
        _tbm._capture_reap_selection_snapshot(active_bots)
        if selection_snapshot is None
        else selection_snapshot
    )
    selection = _tbm._select_reap_candidate_from_snapshot(snapshot, active_bots)
    culled_name = selection.get("candidate")
    if not culled_name:
        return {
            "reaped": False,
            **{key: value for key, value in selection.items() if key != "candidate"},
        }
    if expected_culled is not None and culled_name != expected_culled:
        return {
            "reaped": False,
            "reason": "planned_reap_target_mismatch",
            "expected_culled": expected_culled,
            "actual_culled": culled_name,
        }
    conservative = float(selection["conservative_rating"])

    # Serialize concurrent reaps on a stable sidecar; a mutable data inode is
    # not a valid lock authority when atomic replacement is allowed elsewhere.
    from evolution_infra import _locked_state_sidecar

    with _locked_state_sidecar(
        _tbm.RESULTS_DIR / ".reap-transaction",
        lock_type=fcntl.LOCK_EX,
    ):
        locked_active = _tbm.get_active_bots()
        locked_selection = _tbm._select_reap_candidate_from_snapshot(
            snapshot, locked_active
        )
        locked_culled = locked_selection.get("candidate")
        if locked_culled != culled_name or (
            expected_culled is not None and locked_culled != expected_culled
        ):
            return {
                "reaped": False,
                "reason": "planned_reap_target_changed_under_lock",
                "expected_culled": expected_culled or culled_name,
                "actual_culled": locked_culled,
            }
        try:
            bot_src = _tbm.PROJECT_ROOT / "bots" / culled_name
            if not bot_src.exists():
                return {"reaped": False, "reason": f"{culled_name} already moved"}
            # Publish the durable tombstone before mutating runtime metadata.
            # A failed tag/push must leave the sentinel intact so the operator
            # can retry without an ambiguous half-reaped state.
            _tbm.record_reaped_bot(
                culled_name,
                reason="max_active_bots",
                data={
                    "selection_key": "conservative_glicko",
                    "conservative_rating": selection["conservative_rating"],
                    "leaderboard_score": selection["leaderboard_score"],
                    "h2h_avg_wr": selection["h2h_avg_wr"],
                    "quiet": quiet,
                },
            )
            sentinel = bot_src / ".completed"
            if os.path.lexists(sentinel):
                metadata = os.lstat(sentinel)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)
                    or metadata.st_nlink != 1
                ):
                    raise RuntimeError("reap_completed_sentinel_unsafe")
                sentinel.unlink()
                from evolution_infra import _fsync_directory

                _fsync_directory(sentinel.parent)
        finally:
            pass

    reap_signal = _tbm.RESULTS_DIR / ".reap_signal"
    from evolution_infra import _atomic_publish_state_text

    with _locked_state_sidecar(reap_signal, lock_type=fcntl.LOCK_EX):
        _atomic_publish_state_text(reap_signal, f"{time.time():.6f}\n")

    _tbm.log_system_event(
        "bot.reaped",
        "info" if quiet else "warn",
        (
            f"{'Auto-reaped' if quiet else 'Reaped'} {culled_name} by conservative Glicko "
            f"(r-2rd={conservative:.1f}, leaderboard={selection['leaderboard_score']:.4f}, "
            f"h2h_wr={selection['h2h_avg_wr']:.2%})"
        ),
        {
            "culled": culled_name,
            "remaining": len(active_bots) - 1,
            "selection_key": "conservative_glicko",
            "conservative_rating": round(conservative, 1),
            "leaderboard_score": selection["leaderboard_score"],
            "h2h_avg_wr": selection["h2h_avg_wr"],
            "quiet": quiet,
        },
    )

    return {
        "reaped": True,
        "culled": culled_name,
        "selection_key": "conservative_glicko",
        "conservative_rating": selection["conservative_rating"],
        "leaderboard_score": selection["leaderboard_score"],
        "h2h_avg_wr": selection["h2h_avg_wr"],
        "rating": selection["rating"],
        "remaining": len(active_bots) - 1,
        "reap_mode": "deactivate_completed_sentinel",
    }


def _mcp_result(data: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data)}]}


@_tbm.tool("_tbm.reap_weakest", "Check if bot pool exceeds _tbm.MAX_ACTIVE_BOTS and cull the weakest bot by conservative rating, reporting unified strength.", {})
async def reap_weakest(args):
    result = await _tbm._do_reap_weakest(quiet=args.get("quiet", False) if isinstance(args, dict) else False)
    return _tbm._mcp_result(result)


async def cleanup_incomplete(args: dict | None = None):
    """Fail closed instead of scanning arbitrary incomplete bot directories.

    The old helper enumerated ``bots/`` and inferred deletion authority from a
    raw checkpoint.  That made retired v155 debris actionable.  The only safe
    cleanup is now the normal fenced abandon transaction for an explicitly
    named, currently validated strict workflow.  The helper is deliberately
    not registered in either the MCP or HTTP _tbm.tool catalogs; these checks remain
    as defence in depth for direct/internal calls.
    """

    try:
        from epoch_authority import require_policy_epoch_initialized

        epoch = require_policy_epoch_initialized("_tbm.cleanup_incomplete")
    except Exception as exc:
        state = getattr(exc, "state", None)
        return _tbm._mcp_result({
            "cleaned": False,
            "error": "policy_epoch_not_initialized",
            "epoch": state if isinstance(state, dict) else None,
        })

    checkpoint = _tbm.read_pipeline_checkpoint()
    if not isinstance(checkpoint, dict):
        return _tbm._mcp_result({
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
        return _tbm._mcp_result({
            "cleaned": False,
            "error": "strict_checkpoint_identity_missing",
        })

    try:
        from checkpoint_schema import strict_checkpoint_event_identity

        strict_checkpoint_event_identity(
            checkpoint,
            expected_gen=next_v,
            project_root=_tbm.PROJECT_ROOT,
        )
    except Exception as exc:
        return _tbm._mcp_result({
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
        return _tbm._mcp_result({
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

    candidate = _tbm.get_bot_dir(next_v)
    bot_root = _tbm.BOTS_DIR
    expected_candidate = bot_root / _tbm.bot_name(next_v)
    try:
        candidate_parent = candidate.parent.resolve(strict=True)
        bot_root_resolved = bot_root.resolve(strict=True)
    except OSError as exc:
        return _tbm._mcp_result({
            "cleaned": False,
            "error": "candidate_scope_unavailable",
            "detail": f"{type(exc).__name__}: {str(exc)[:300]}",
        })
    if (
        candidate != expected_candidate
        or candidate_parent != bot_root_resolved
        or candidate.name != _tbm.bot_name(next_v)
    ):
        return _tbm._mcp_result({
            "cleaned": False,
            "error": "candidate_outside_current_workflow_scope",
        })
    if not candidate.exists():
        return _tbm._mcp_result({
            "cleaned": False,
            "reason": "current_candidate_absent",
            "candidate": candidate.name,
            "workflow_run_id": workflow_run_id,
            "epoch": epoch.get("evaluation_epoch"),
        })
    if candidate.is_symlink() or not candidate.is_dir():
        return _tbm._mcp_result({
            "cleaned": False,
            "error": "current_candidate_path_unsafe",
        })
    if (candidate / ".completed").exists() or _tbm.git_has_tag(next_v):
        return _tbm._mcp_result({
            "cleaned": False,
            "error": "current_candidate_is_published_or_completed",
        })
    if _tbm.git_dir_is_committed(next_v):
        return _tbm._mcp_result({
            "cleaned": False,
            "error": "current_candidate_is_git_tracked",
        })

    result = await _tbm._do_abandon_generation(
        reason="cleanup_incomplete_exact_workflow",
        expected_workflow_run_id=workflow_run_id,
        expected_next_v=next_v,
        expected_source_v=checkpoint.get("source_v"),
        expected_checkpoint_revision=revision,
        expected_checkpoint_stage=checkpoint.get("stage"),
    )
    return _tbm._mcp_result({
        "cleaned": bool(
            result.get("abandoned") is True
            and result.get("removed_directory") == candidate.name
        ),
        "candidate": candidate.name,
        "workflow_run_id": workflow_run_id,
        "epoch": epoch.get("evaluation_epoch"),
        "abandon_result": result,
    })

