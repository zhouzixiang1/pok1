"""Runtime git integrity guard -- branch / HEAD drift coroutine and helpers.

This module hosts the *complete* branch-guard business cluster that was
originally split between ``orchestrator.py`` (the helpers / identity probes)
and the old catch-all ``orchestrator_background.py`` companion (the
long-running coroutine).  They are co-located here so the whole business
concern -- "stop in-place evolution if another actor mutates this worktree's
branch or HEAD" -- reads as a single cohesive unit.

Members moved here:

* ``RUNTIME_BRANCH_GUARD_INTERVAL``  -- default polling cadence (seconds).
* ``_runtime_branch_guard_coroutine``  -- the asyncio watchdog loop.
* ``_runtime_branch_guard_enabled``    -- gate (env / pytest aware).
* ``_branch_name``                     -- parse ``git status -b`` first cell.
* ``_runtime_git_identity``            -- read-only branch+HEAD probe.
* ``_runtime_head_drift_unrelated_allowed``  -- classify HEAD delta as benign.
* ``_set_runtime_expected_head``       -- publish the validated baseline HEAD.

IMPORTANT -- shared-symbol access model
---------------------------------------
Many names referenced by these bodies remain in ``orchestrator`` because they
are part of the module's monkeypatch surface (``log``, ``log_system_event``,
``_clear_orchestrator_session``, ``evaluate_head_drift``, ``bot_relpath``,
``PROJECT_ROOT``, etc.).  The test suite patches some of these *and* patches
members of this very module (``_runtime_git_identity``,
``_runtime_head_drift_unrelated_allowed``, ``_set_runtime_expected_head``,
``_runtime_branch_guard_enabled``) on the ``orchestrator`` module object.

To keep both patch surfaces live:

* References to symbols that live in ``orchestrator`` are written
  ``_o.<name>`` (live attribute access; reflects any monkeypatch on
  ``orchestrator``).
* References between members of *this* module: helper functions that the
  test suite patches on ``orchestrator`` (``_runtime_git_identity``,
  ``_runtime_head_drift_unrelated_allowed``, ``_set_runtime_expected_head``)
  are also reached via ``_o.<name>`` from inside the coroutine, so the
  patches stay effective.  Pure-internal helpers that are never patched
  (``_branch_name``) are called directly.
"""

from __future__ import annotations

import asyncio
import os
import subprocess

import orchestrator as _o


# Default polling cadence for the branch-guard coroutine (seconds).  Read once
# at import time; the coroutine accepts an explicit ``check_interval`` kwarg so
# tests can override per-task without mutating this constant.
RUNTIME_BRANCH_GUARD_INTERVAL = float(os.environ.get("POK_RUNTIME_BRANCH_GUARD_INTERVAL", "5"))


def _runtime_branch_guard_enabled() -> bool:
    if os.environ.get("POK_DISABLE_RUNTIME_BRANCH_GUARD") == "1":
        return False
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("POK_FORCE_RUNTIME_BRANCH_GUARD") != "1":
        return False
    return True


def _branch_name(branch_status: str | None) -> str:
    parts = (branch_status or "").split("...", 1)[0].split()
    return parts[0] if parts else ""


def _runtime_git_identity() -> dict:
    """Read the current branch and HEAD without mutating the worktree."""
    branch_status = ""
    head = ""
    try:
        status = subprocess.run(
            ["git", "status", "--short", "--branch", "--untracked-files=no"],
            cwd=str(_o.PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if status.returncode == 0:
            lines = [line for line in (status.stdout or "").splitlines() if line.strip()]
            if lines and lines[0].startswith("## "):
                branch_status = lines[0].replace("## ", "", 1)
    except Exception:
        branch_status = ""
    if not branch_status:
        try:
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(_o.PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if branch.returncode == 0:
                branch_status = (branch.stdout or "").strip()
        except Exception:
            branch_status = ""
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(_o.PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if rev.returncode == 0:
            head = (rev.stdout or "").strip()
    except Exception:
        head = ""
    return {
        "branch": _branch_name(branch_status),
        "branch_status": branch_status,
        "head": head,
    }


def _runtime_head_drift_unrelated_allowed(expected_head: str, current_head: str) -> tuple[bool, dict]:
    if not expected_head or not current_head or expected_head == current_head:
        return False, {}
    try:
        from evolution_core import read_pipeline_checkpoint
        checkpoint = read_pipeline_checkpoint()
    except Exception:
        checkpoint = None
    candidate_v = None
    if isinstance(checkpoint, dict):
        try:
            candidate_v = int(checkpoint.get("next_v"))
        except Exception:
            candidate_v = None
    allowed, payload = _o.evaluate_head_drift(
        _o.PROJECT_ROOT,
        expected_head,
        current_head,
        candidate_v=candidate_v,
        checkpoint=checkpoint if isinstance(checkpoint, dict) else None,
    )
    contract_paths = list(payload.get("head_contract_paths") or [])
    candidate_prefix = _o.bot_relpath(candidate_v) + "/" if candidate_v is not None else ""
    payload.update({
        "candidate_v": candidate_v,
        "head_candidate_entries": [
            f"?? {path}" for path in contract_paths
            if candidate_prefix and path.startswith(candidate_prefix)
        ][:40],
        "head_blocking_entries": [
            f"?? {path}" for path in contract_paths
            if not candidate_prefix or not path.startswith(candidate_prefix)
        ][:40],
    })
    return allowed, payload


def _set_runtime_expected_head(head: str) -> str:
    """Publish the current safe runtime HEAD for tool-level guards."""
    clean_head = (head or "").strip()
    if clean_head:
        os.environ["POK_RUNTIME_EXPECTED_HEAD"] = clean_head
    else:
        os.environ.pop("POK_RUNTIME_EXPECTED_HEAD", None)
    return clean_head


async def _runtime_branch_guard_coroutine(
    ui,
    shutdown_mgr,
    *,
    expected_branch: str,
    expected_head: str,
    owner_task=None,
    hard_stop_event=None,
    check_interval: float = RUNTIME_BRANCH_GUARD_INTERVAL,
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
