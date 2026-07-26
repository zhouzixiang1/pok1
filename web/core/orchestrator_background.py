"""Background-coroutine helpers extracted from ``orchestrator``.

This companion module hosts two background coroutines that were originally
inline in ``web/core/orchestrator.py``:

* ``_run_post_generation_cleanup_with_timeout``
* ``_runtime_branch_guard_coroutine``

They are split out purely to keep the main entry module small; the main module
re-exports both names at the very bottom of the file for backward
compatibility, covering tests and external importers that still reach them as
``orchestrator.<name>``.

IMPORTANT -- shared-symbol access model:

Many names referenced by these coroutines remain in ``orchestrator`` because
they are part of the module's monkeypatch surface (e.g.
``_runtime_git_identity``, ``_runtime_head_drift_unrelated_allowed``,
``_set_runtime_expected_head``, ``_clear_orchestrator_session``,
``log_system_event``, ``log``).  The test suite patches these on the
``orchestrator`` module object and expects the running coroutine to observe the
patched values.  Binding them at import time would freeze the pre-patch value
and silently break the audit.

All such references in this file are written ``_o.<name>`` so they resolve
against the *live* ``orchestrator`` module attribute at call time, matching the
proven pattern used by ``tool_commit_archivist`` (``import tool_commit as _tc``,
``_tc.<name>``).

Constants used as default-argument values (``RUNTIME_BRANCH_GUARD_INTERVAL``,
``POST_GENERATION_CLEANUP_TIMEOUT``) and the
``OperatorGenerationCostLimitExceeded`` exception class are read via
``_o.<name>`` / direct import respectively; none of these are monkeypatched by
the test suite, and reading them off the live module keeps a single source of
truth.
"""

from __future__ import annotations

import asyncio
import os
import time

import orchestrator as _o
from orchestrator_cost_policy import OperatorGenerationCostLimitExceeded


async def _run_post_generation_cleanup_with_timeout(shutdown_mgr, ui, gen_ctx, gen_count=None):
    """Run post-generation housekeeping without letting it block evolution forever."""
    from generation_scheduler import post_generation_cleanup

    version = getattr(gen_ctx, "next_v", None)
    source_v = getattr(gen_ctx, "source_v", None)
    started = time.time()
    _o.log_system_event(
        "orchestrator.post_cleanup_start",
        "info",
        f"Post-generation cleanup starting for v{version}",
        {
            "version": version,
            "source_v": source_v,
            "gen_count": gen_count,
            "timeout_s": _o.POST_GENERATION_CLEANUP_TIMEOUT,
        },
    )
    try:
        await asyncio.wait_for(
            post_generation_cleanup(shutdown_mgr, ui, gen_ctx),
            timeout=_o.POST_GENERATION_CLEANUP_TIMEOUT,
        )
    except asyncio.TimeoutError:
        elapsed = time.time() - started
        msg = (
            f"Post-generation cleanup timed out for v{version} after "
            f"{_o.POST_GENERATION_CLEANUP_TIMEOUT}s; stopping before successor "
            "scheduling because the checkpoint-free boundary remains blocked."
        )
        _o.log.warning(msg)
        if ui:
            ui.log_history(msg, "warn")
        _o.log_system_event(
            "orchestrator.post_cleanup_timeout",
            "warn",
            msg,
            {
                "version": version,
                "source_v": source_v,
                "gen_count": gen_count,
                "elapsed_sec": round(elapsed, 2),
                "timeout_s": _o.POST_GENERATION_CLEANUP_TIMEOUT,
            },
        )
        return False
    except OperatorGenerationCostLimitExceeded:
        # Archivist/consolidation calls are part of the same generation.  Do not
        # translate an operator stop into best-effort cleanup and then start a
        # fresh generation with a reset scope.
        raise
    except Exception as e:
        elapsed = time.time() - started
        msg = f"Post-generation cleanup failed for v{version}: {str(e)[:180]}"
        _o.log.exception(msg)
        if ui:
            ui.log_history(msg, "warn")
        _o.log_system_event(
            "orchestrator.post_cleanup_failed",
            "error",
            msg,
            {
                "version": version,
                "source_v": source_v,
                "gen_count": gen_count,
                "elapsed_sec": round(elapsed, 2),
                "error": str(e)[:500],
            },
        )
        return False

    elapsed = time.time() - started
    _o.log_system_event(
        "orchestrator.post_cleanup_done",
        "info",
        f"Post-generation cleanup finished for v{version} in {elapsed:.1f}s",
        {
            "version": version,
            "source_v": source_v,
            "gen_count": gen_count,
            "elapsed_sec": round(elapsed, 2),
        },
    )
    return True


async def _runtime_branch_guard_coroutine(
    ui,
    shutdown_mgr,
    *,
    expected_branch: str,
    expected_head: str,
    owner_task=None,
    hard_stop_event=None,
    check_interval: float = _o.RUNTIME_BRANCH_GUARD_INTERVAL,
):
    """Stop in-place evolution if another actor changes this worktree's branch.

    Dirty-path scope can be made safe, but git branch/HEAD is global to a
    worktree. If another agent switches or advances HEAD while workers are
    running, the LLM may read a different codebase than the one that passed
    gates. A branch-name change to the same HEAD is only an alias of the same
    tree, so it is recorded and tolerated until a commit/HEAD change appears.
    """
    allowed_aliases: set[tuple[str, str]] = set()
    allowed_unrelated_heads: set[tuple[str, str, str]] = set()
    runtime_expected_head = _o._set_runtime_expected_head(expected_head)
    while True:
        if shutdown_mgr and shutdown_mgr.is_shutting_down:
            return
        try:
            await asyncio.sleep(check_interval)
            if shutdown_mgr and shutdown_mgr.is_shutting_down:
                return
            current = _o._runtime_git_identity()
            current_branch = current.get("branch") or ""
            current_head = current.get("head") or ""
            reason = ""
            published_expected_head = os.environ.get("POK_RUNTIME_EXPECTED_HEAD", "").strip()
            if (
                published_expected_head
                and published_expected_head != runtime_expected_head
                and current_head == published_expected_head
            ):
                previous_expected_head = runtime_expected_head
                runtime_expected_head = published_expected_head
                _o.log_system_event(
                    "repo.runtime_expected_head_adopted",
                    "info",
                    (
                        "Runtime branch guard adopted published expected HEAD: "
                        f"{previous_expected_head or '<none>'} -> {runtime_expected_head}"
                    ),
                    {
                        "expected_branch": expected_branch,
                        "current_branch": current_branch,
                        "previous_expected_head": previous_expected_head,
                        "expected_head": runtime_expected_head,
                        "current_head": current_head,
                        "branch_status": current.get("branch_status", ""),
                        "directive": (
                            "Continuing because a pipeline-owned operation "
                            "published the current HEAD as the validated runtime baseline."
                        ),
                    },
                )
            same_expected_head = bool(
                runtime_expected_head
                and current_head
                and current_head == runtime_expected_head
            )
            branch_alias = bool(
                expected_branch
                and current_branch
                and current_branch != expected_branch
                and same_expected_head
            )
            if branch_alias:
                alias_key = (current_branch, current_head)
                if alias_key not in allowed_aliases:
                    allowed_aliases.add(alias_key)
                    _o.log_system_event(
                        "repo.runtime_branch_alias_allowed",
                        "warn",
                        (
                            "Runtime branch guard tolerated branch alias on the "
                            f"same HEAD: {expected_branch}@{runtime_expected_head} -> "
                            f"{current_branch}@{current_head}"
                        ),
                        {
                            "expected_branch": expected_branch,
                            "current_branch": current_branch,
                            "expected_head": runtime_expected_head,
                            "current_head": current_head,
                            "branch_status": current.get("branch_status", ""),
                            "directive": (
                                "Continuing because the worktree HEAD is unchanged. "
                                "A later evaluation-contract HEAD change will stop evolution; commit_bot "
                                "still requires the canonical branch."
                            ),
                        },
                    )
                continue
            if runtime_expected_head and current_head and current_head != runtime_expected_head:
                unrelated_allowed, unrelated_payload = _o._runtime_head_drift_unrelated_allowed(
                    runtime_expected_head,
                    current_head,
                )
                if unrelated_allowed:
                    previous_expected_head = runtime_expected_head
                    runtime_expected_head = _o._set_runtime_expected_head(current_head)
                    drift_key = (current_branch, previous_expected_head, current_head)
                    if drift_key not in allowed_unrelated_heads:
                        allowed_unrelated_heads.add(drift_key)
                        _o.log_system_event(
                            "repo.runtime_head_drift_unrelated_allowed",
                            "warn",
                            (
                                "Runtime branch guard tolerated unrelated HEAD drift: "
                                f"{expected_branch}@{previous_expected_head} -> "
                                f"{current_branch}@{current_head}"
                            ),
                            {
                                "expected_branch": expected_branch,
                                "current_branch": current_branch,
                                "expected_head": previous_expected_head,
                                "current_head": current_head,
                                "advanced_expected_head": runtime_expected_head,
                                "branch_status": current.get("branch_status", ""),
                                **unrelated_payload,
                                "directive": (
                                    "Continuing because the HEAD change does not touch "
                                    "evolution infrastructure, the national platform, the "
                                    "local engine, or the active candidate bot. The runtime "
                                    "baseline was advanced so later unrelated commits are "
                                    "checked incrementally. commit_bot still requires the "
                                    "canonical branch."
                                ),
                            },
                        )
                    continue
                reason = "head_drift"
            elif expected_branch and current_branch and current_branch != expected_branch:
                reason = "branch_drift"
            if not reason:
                continue

            payload = {
                "reason": reason,
                "expected_branch": expected_branch,
                "current_branch": current_branch,
                "expected_head": runtime_expected_head,
                "current_head": current_head,
                "branch_status": current.get("branch_status", ""),
                "directive": (
                    "Runtime evolution stopped because this shared worktree's "
                    "git branch/HEAD changed. Return to the expected branch and "
                    "restart so checkpoint recovery can revalidate the candidate."
                ),
            }
            msg = (
                "Runtime branch guard stopped evolution: "
                f"{reason} {expected_branch}@{runtime_expected_head} -> "
                f"{current_branch}@{current_head}"
            )
            if ui:
                ui.log_history(msg, "error")
                ui.set_status("Stopped: git branch drift", is_working=False)
            else:
                _o.log.error(msg)
            _o.log_system_event("repo.runtime_branch_drift_shutdown", "error", msg, payload)
            _o._clear_orchestrator_session(reason="runtime_branch_drift")
            if hard_stop_event is not None:
                try:
                    hard_stop_event.set()
                except Exception:
                    pass
            if shutdown_mgr:
                shutdown_mgr.request_shutdown()
            if owner_task is not None and not owner_task.done():
                owner_task.cancel()
            return
        except asyncio.CancelledError:
            return
        except Exception as e:
            _o.log.debug("Runtime branch guard check error (non-fatal): %s", e)
