"""Git publish reconciliation for evolution commits.

The evolution pipeline may race with unrelated work landing on origin/main.
This module owns the policy for retrying a push safely: remote changes are
merged automatically only when they do not affect the evaluation contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from evaluation_contract import evaluate_head_drift

GitFunc = Callable[..., str]
LogFunc = Callable[[str, str, str, dict[str, Any]], None]


def _push_once(git: GitFunc, refs: tuple[str, ...]) -> tuple[bool, list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    for ref in refs:
        try:
            git("push", "origin", ref)
        except Exception as exc:
            errors.append({"ref": ref, "error": str(exc)[:500]})
    return not errors, errors


def _rev_count(git: GitFunc) -> tuple[int, int]:
    raw = git("rev-list", "--left-right", "--count", "HEAD...origin/main", check=False)
    parts = (raw or "").split()
    if len(parts) != 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


def _short_rev(git: GitFunc, ref: str) -> str:
    return git("rev-parse", "--short=12", ref, check=False).strip()


def _merge_base(git: GitFunc) -> str:
    return git("merge-base", "HEAD", "origin/main", check=False).strip()


def reconcile_push_refs(
    refs: tuple[str, ...],
    *,
    root: str | Path,
    git: GitFunc,
    checkpoint: dict[str, Any] | None = None,
    candidate_v: int | None = None,
    source_v: int | None = None,
    log_event: LogFunc | None = None,
) -> dict[str, Any]:
    """Push refs, auto-merging unrelated remote main changes before retrying."""
    ok, errors = _push_once(git, refs)
    if ok:
        return {"ok": True, "refs": list(refs), "reconciled": False, "errors": []}

    result: dict[str, Any] = {
        "ok": False,
        "refs": list(refs),
        "reconciled": False,
        "errors": errors,
    }
    if "main" not in refs:
        return result

    try:
        git("fetch", "origin", "--prune", "--tags")
        ahead, behind = _rev_count(git)
        result.update({"ahead": ahead, "behind": behind})
        if behind <= 0:
            retry_ok, retry_errors = _push_once(git, refs)
            result.update({"ok": retry_ok, "errors": retry_errors, "retried": True})
            return result

        base = _merge_base(git)
        remote_head = _short_rev(git, "origin/main")
        local_head = _short_rev(git, "HEAD")
        allowed, payload = evaluate_head_drift(
            root,
            base,
            "origin/main",
            candidate_v=candidate_v,
            source_v=source_v,
            checkpoint=checkpoint,
        )
        result.update({
            "merge_base": base[:12],
            "local_head": local_head,
            "remote_head": remote_head,
            **payload,
        })
        if not allowed:
            result["reason"] = "remote_contract_changed"
            if log_event:
                log_event(
                    "repo.git_push_reconcile_blocked",
                    "error",
                    "Git push reconcile blocked because remote main changed evaluation contract paths",
                    result,
                )
            return result

        if ahead > 0:
            git("merge", "--no-ff", "origin/main", "-m", "Merge remote main before publishing evolution refs")
        else:
            git("merge", "--ff-only", "origin/main")

        retry_ok, retry_errors = _push_once(git, refs)
        result.update({
            "ok": retry_ok,
            "errors": retry_errors,
            "reconciled": True,
            "retried": True,
        })
        if log_event:
            log_event(
                "repo.git_push_reconciled" if retry_ok else "repo.git_push_reconcile_retry_failed",
                "success" if retry_ok else "error",
                "Git push reconciled with unrelated remote main changes" if retry_ok else "Git push retry failed after reconcile",
                result,
            )
        return result
    except Exception as exc:
        result.update({"reason": "reconcile_error", "reconcile_error": str(exc)[:500]})
        if log_event:
            log_event(
                "repo.git_push_reconcile_failed",
                "error",
                f"Git push reconcile failed: {str(exc)[:180]}",
                result,
            )
        return result
