"""Git operations and publication-commit lifecycle for evolution_infra.

Extracted as a cohesive business cluster from evolution_infra.py: every git
wrapper, the push enable/required flags, branch ensurance, tag/commit/ref
checks, ``git_commit_bot``, the intent-bound publication commit creation/
validation/ref verification flow, ``ensure_bot_git_publication``,
``verify_remote_bot_publication`` and the parent/tag discovery helpers.

evolution_infra.py retains thin delegate shells so external
``from evolution_infra import <name>`` and
``monkeypatch.setattr(evolution_infra, "<name>", ...)`` keep resolving.

IMPORTANT -- shared-symbol access model
---------------------------------------
Many names referenced by these bodies remain in ``evolution_infra`` because
they are part of that module's monkeypatch surface -- the test suite patches
``evolution_infra._git``, ``evolution_infra.PROJECT_ROOT``,
``evolution_infra.RESULTS_DIR``, ``evolution_infra.get_bot_dir``,
``evolution_infra.evolution_git_push_required``,
``evolution_infra.git_push_refs``, ``evolution_infra._git_ensure_main_branch``,
``evolution_infra._require_national_epoch_registry_for_commit`` and reads them
back through the git/publication code paths.  Binding them at import time
would freeze the pre-patch value and silently break those tests.

Every such reference in this file is written ``_ei.<name>`` so it resolves
against the live module attribute at call time.  References between members of
*this* module that are NOT in the monkeypatch surface (e.g.
``_create_publication_commit`` calling ``_git_with_index``) are written as
bare globals, exactly as they were inline.  A small number of moved symbols
that ARE in the monkeypatch surface AND are called from another moved body
(``_git``, ``_git_command_succeeds``, ``_git_ensure_main_branch``,
``_require_national_epoch_registry_for_commit``, ``git_push_refs``,
``evolution_git_push_enabled``, ``evolution_git_push_required``,
``git_publish_status``) are also routed through ``_ei.<name>`` so the patches
keep taking effect when the call originates from a body that now lives here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path

import fcntl

import evolution_infra as _ei

# Constants and helpers re-exported by evolution_infra.  Imported here directly
# (they are pure functions / configuration constants, not monkeypatch surface)
# so the moved bodies can keep referencing them as bare globals.
from bot_namespace import (
    ACTIVE_BOT_PREFIX,
    ACTIVE_TAG_PREFIX,
    ARCHIVED_VERSION_HIGH_WATER,
    EVOLUTION_BRANCH,
    FIRST_STRICT_POLICY_VERSION,
    active_bot_glob,
    bot_name,
    bot_relpath,
    bot_tag,
    bot_tag_glob,
    format_version,
    high_water_tag,
    parse_bot_version,
    parse_tag_version,
)
from evolution_scope import classify_status_entries
from publish_reconcile import reconcile_push_refs

# Branch refs are module-level constants in evolution_infra; mirror them here
# using the same EVOLUTION_BRANCH value so the moved code keeps reading the
# frozen local/remote publication refs exactly as before.  These are NOT
# monkeypatched by any test.
_LOCAL_PUB_REF = f"refs/heads/{EVOLUTION_BRANCH}"
_REMOTE_PUB_REF = f"refs/remotes/origin/{EVOLUTION_BRANCH}"


def _git(*args, check=True):
    """Run git command, return stdout.

    This is the actual implementation; ``evolution_infra._git`` is a thin delegate
    that calls this. Test monkeypatches of ``evolution_infra._git`` propagate into
    the moved bodies because every internal call site here routes through
    ``_ei._git(...)`` rather than calling this function directly.
    """
    try:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=str(_ei.PROJECT_ROOT),
            capture_output=True, text=True,
            timeout=30
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {args[0]}: timed out after 30s")
    if check and result.returncode != 0:
        raise RuntimeError(f"git {args[0]}: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_explicit_presence(*args):
    """Return a destructive Git predicate only for rc=0/explicit rc=1.

    Actual implementation; ``evolution_infra._git_explicit_presence`` delegates
    here. Internal callers route through ``_ei._git_explicit_presence`` so test
    monkeypatches propagate.
    """

    try:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=str(_ei.PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {args[0]}: timed out after 30s") from exc
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise RuntimeError(
        f"git {args[0]} unavailable (rc={result.returncode}): "
        f"{result.stderr.strip()}"
    )

    try:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=str(_ei.PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {args[0]}: timed out after 30s") from exc
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise RuntimeError(
        f"git {args[0]} unavailable (rc={result.returncode}): "
        f"{result.stderr.strip()}"
    )


def evolution_git_push_enabled() -> bool:
    """Return whether evolution-owned commits should be pushed immediately."""
    return _ei._env_flag("EVOLUTION_GIT_PUSH", False)


def evolution_git_push_required() -> bool:
    """Return whether a new generation requires a synchronized remote baseline."""
    return _ei._env_flag(
        "POK_REQUIRE_EVOLUTION_PUSH", _ei._env_flag("POK_EVOLUTION_RUNTIME", False)
    )


def git_publish_status() -> dict:
    """Return branch publication state relative to the configured upstream."""
    branch = _ei._git("rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
    head = _ei._git("rev-parse", "--short=12", "HEAD", check=False).strip()
    upstream = _ei._git(
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False
    ).strip() or f"origin/{EVOLUTION_BRANCH}"
    upstream_head = _ei._git("rev-parse", "--short=12", upstream, check=False).strip()
    if not upstream_head:
        return {
            "ok": False,
            "reason": "upstream_missing",
            "branch": branch,
            "head": head,
            "upstream": upstream,
            "upstream_head": "",
            "ahead": None,
            "behind": None,
        }
    raw_counts = _ei._git("rev-list", "--left-right", "--count", f"HEAD...{upstream}", check=False)
    parts = (raw_counts or "").split()
    if len(parts) != 2:
        return {
            "ok": False,
            "reason": "ahead_behind_unavailable",
            "branch": branch,
            "head": head,
            "upstream": upstream,
            "upstream_head": upstream_head,
            "ahead": None,
            "behind": None,
            "raw_counts": raw_counts,
        }
    try:
        ahead, behind = int(parts[0]), int(parts[1])
    except ValueError:
        return {
            "ok": False,
            "reason": "ahead_behind_parse_failed",
            "branch": branch,
            "head": head,
            "upstream": upstream,
            "upstream_head": upstream_head,
            "ahead": None,
            "behind": None,
            "raw_counts": raw_counts,
        }
    return {
        "ok": True,
        "branch": branch,
        "head": head,
        "upstream": upstream,
        "upstream_head": upstream_head,
        "ahead": ahead,
        "behind": behind,
    }


def ensure_publish_ready_for_new_generation() -> tuple[bool, dict]:
    """Block new generations when required evolution commits are not published."""
    push_enabled = _ei.evolution_git_push_enabled()
    push_required = _ei.evolution_git_push_required()
    status = _ei.git_publish_status()
    payload = {
        "push_enabled": push_enabled,
        "push_required": push_required,
        **status,
    }
    if not push_required:
        return True, payload
    if not push_enabled:
        payload.update({
            "blocked": True,
            "reason": "evolution_git_push_disabled",
            "directive": (
                "Long-running evolution requires EVOLUTION_GIT_PUSH=1 so bot "
                "commits and tags publish through origin/main."
            ),
        })
        return False, payload
    if not status.get("ok"):
        payload.update({
            "blocked": True,
            "reason": status.get("reason") or "publish_status_unavailable",
            "directive": "Cannot verify origin/main synchronization before starting a new generation.",
        })
        return False, payload
    if int(status.get("ahead") or 0) > 0:
        payload.update({
            "blocked": True,
            "reason": "unpublished_local_commits",
            "directive": "Push local evolution commits/tags before starting the next generation.",
        })
        return False, payload
    if int(status.get("behind") or 0) > 0:
        payload.update({
            "blocked": True,
            "reason": "remote_main_ahead",
            "directive": "Fetch and fast-forward or reconcile origin/main before starting the next generation.",
        })
        return False, payload
    return True, payload


def git_push_refs(*refs: str) -> bool:
    """Push refs to origin and return the real aggregate result.

    If origin/main advanced with evaluation-contract-neutral changes, reconcile
    by merging origin/main and retrying the push once.
    """
    if not refs:
        return True
    checkpoint = _ei.read_pipeline_checkpoint()
    candidate_v = None
    source_v = None
    if isinstance(checkpoint, dict):
        candidate_v = checkpoint.get("next_v")
        source_v = checkpoint.get("source_v")
    if candidate_v is None:
        for ref in refs:
            parsed = parse_tag_version(ref) if isinstance(ref, str) else None
            if parsed is not None:
                candidate_v = parsed
                break

    def _log_event(event_type, severity, message, data):
        try:
            from system_log import log_system_event
            log_system_event(event_type, severity, message, data)
        except Exception:
            pass

    result = reconcile_push_refs(
        tuple(refs),
        root=_ei.PROJECT_ROOT,
        git=_git,
        checkpoint=checkpoint if isinstance(checkpoint, dict) else None,
        candidate_v=candidate_v,
        source_v=source_v,
        log_event=_log_event,
    )
    ok = bool(result.get("ok"))
    errors = list(result.get("errors") or [])
    try:
        from system_log import log_system_event
        log_system_event(
            "repo.git_push_done" if ok else "repo.git_push_failed",
            "success" if ok else "error",
            (
                f"Git push succeeded for {', '.join(refs)}"
                if ok else
                f"Git push failed for {', '.join(item['ref'] for item in errors)}"
            ),
            {"refs": list(refs), "ok": ok, **result},
        )
    except Exception:
        pass
    return ok


def _git_ensure_main_branch():
    """Require the canonical evolution branch before an evolution commit.

    Do not stash/pop changes across branches here. Moving a dirty worktree from
    an accidental side branch onto main can mix unrelated edits into evolution
    commits. Runtime guard paths should stop the generation earlier; this is
    the final mutation boundary.
    """
    current = _ei._git("rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
    if current == EVOLUTION_BRANCH:
        return
    try:
        from system_log import log_system_event
        log_system_event(
            "repo.branch_commit_blocked",
            "error",
            f"Evolution commit blocked on non-{EVOLUTION_BRANCH} branch: {current}",
            {"current_branch": current, "target_branch": EVOLUTION_BRANCH},
        )
    except Exception:
        pass
    raise RuntimeError(
        f"Refusing evolution commit on branch '{current}'; expected '{EVOLUTION_BRANCH}'."
    )


def git_has_tag(version):
    """Check for a strict-policy completion tag.

    Pre-policy tags are audit/version-authority records only.
    """
    if int(version) < FIRST_STRICT_POLICY_VERSION:
        return False
    return bool(_ei._git("tag", "-l", bot_tag(version), check=False).strip())


def git_has_publication_ref(version):
    """Return whether either create-only publication tag already exists.

    This is a preservation predicate, not completion authority: an interrupted
    publication may have only the high-water tag.  Cleanup paths must retain
    candidate bytes for either partial ref instead of relying on the usual
    tracked-tree or ``.completed`` implications of a finished publication.
    """

    target = int(version)
    names = (bot_tag(target), high_water_tag(target))
    return any(
        _ei._git_explicit_presence(
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/tags/{name}",
        )
        for name in names
    )


def git_dir_is_committed(version):
    """True if the active bot directory has any git-tracked file.

    Detects BARE COMMITS — code that landed in git via a direct `git commit`
    (e.g. an LLM running git in Bash) but was never finalized through commit_bot,
    so it lacks both the active-epoch tag and the .completed sentinel. This is the
    root-cause signal of the v117 repeated-regeneration loop (2026-06-18): v117
    was bare-committed twice (f6bcccf/f6c4eb7) without a tag, so find_current_v()
    kept returning 116 and the orchestrator regenerated v117 five times until
    commit_bot finally tagged it (20db34c, 22:02). 'git ls-files' is the test:
    a directory with on-disk files but no tracked files is an untracked scratch
    dir (safe to overwrite); a directory with tracked files is committed state.
    """
    return _ei._git_explicit_presence(
        "ls-files",
        "--error-unmatch",
        "--",
        bot_relpath(version) + "/",
    )


def find_max_committed_v():
    """Diagnostic max version whose strict receipt is git-tracked.

    This legacy diagnostic can expose an untagged direct commit for operator
    reconciliation, but it is never completion authority and is never an input
    to version allocation.  Only annotated completion/high-water tags advance
    the published namespace; only checkpoint-bound abandonment receipts may
    reserve a consumed label inside the current epoch.

    Implementation: a SINGLE `git ls-files bots/{active prefix}*` call (not one
    subprocess per directory) keeps this O(1 git call)/generation regardless
    of how many direct strict bot artifacts exist.
    """
    try:
        out = _ei._git("ls-files", "--", f"bots/{active_bot_glob()}", check=False)
    except Exception:
        return 0
    max_v = ARCHIVED_VERSION_HIGH_WATER
    for line in out.splitlines():
        # line like "bots/national_v001/card_utils.py" — extract the dir version
        parts = line.split("/")
        if len(parts) < 2 or not parts[1].startswith(ACTIVE_BOT_PREFIX):
            continue
        v = parse_bot_version(parts[1])
        if v is None or v < FIRST_STRICT_POLICY_VERSION:
            continue
        # A tracked strict epoch receipt is the minimum proof that this is a
        # consumed policy version rather than unrelated/stale source debris.
        receipt_path = f"bots/{bot_name(v)}/policy_epoch_receipt.json"
        if not _ei._git("ls-files", "--error-unmatch", receipt_path, check=False).strip():
            continue
        if v > max_v:
            max_v = v
    return max_v


def publish_runtime_expected_head(reason: str = "", version=None) -> str:
    """Publish the current HEAD as the validated runtime baseline.

    The background runtime guard owns the final stop/continue decision, but
    pipeline-owned commits must be able to tell it that the new HEAD is expected
    instead of being an external drift.
    """
    head = _ei._git("rev-parse", "--short=12", "HEAD", check=False).strip()
    if not head:
        return ""
    previous = os.environ.get("POK_RUNTIME_EXPECTED_HEAD", "").strip()
    os.environ["POK_RUNTIME_EXPECTED_HEAD"] = head
    if previous != head:
        try:
            from system_log import log_system_event
            log_system_event(
                "repo.runtime_expected_head_published",
                "info",
                f"Published runtime expected HEAD {previous or '<none>'} -> {head}",
                {
                    "previous_expected_head": previous,
                    "expected_head": head,
                    "reason": reason,
                    "version": version,
                },
            )
        except Exception:
            pass
    return head


def _require_national_epoch_registry_for_commit():
    from national_epoch_registry import load_registry_state

    state = load_registry_state(
        _ei.PROJECT_ROOT,
        legacy_ledger=_ei.REAPED_BOTS_FILE,
        include_history=True,
    )
    if not state.available or not state.migration_marker:
        diagnostics = "; ".join(state.diagnostics) or "migration marker missing"
        raise RuntimeError(
            "national epoch registry is not durably migrated; " + diagnostics
        )
    return state


def _advance_national_epoch_high_water(version):
    from national_epoch_registry import advance_high_water

    return advance_high_water(
        int(version),
        repo_root=_ei.PROJECT_ROOT,
        legacy_ledger=_ei.REAPED_BOTS_FILE,
    )


def git_commit_bot(
    version,
    source_v,
    strategy_tag,
    rating_info="",
    parent2_v=None,
    *,
    official_certificate,
):
    """Commit a completed bot generation.

    Always commits on EVOLUTION_BRANCH (main). Calls _ei._git_ensure_main_branch()
    first so that LLM-created side-branches never pollute the evolution history.
    Stage only the evolved bot and curated learning notes; daemon/result churn
    must not leak into evolution commits.
    """
    certificate = dict(official_certificate or {})
    certificate_digest = str(certificate.get("certificate_digest") or "")
    expected_bot_hash = str(certificate.get("candidate_hash") or "")
    certificate_policy = str(certificate.get("policy_id") or "")
    if not certificate:
        raise RuntimeError(
            "official full certificate is required for national-bot commit/tag"
        )
    if not (certificate_digest and expected_bot_hash and certificate_policy):
        raise RuntimeError("official certificate metadata is incomplete")
    # Import lazily so the foundational infrastructure module does not create a
    # module-load cycle with official certification.  The policy identifier has
    # one owner; commit/tag code must not drift from certificate issuance.
    from official_certification import FULL_POLICY_ID

    if certificate_policy != FULL_POLICY_ID:
        raise RuntimeError(
            f"unsupported official certificate policy: {certificate_policy or '<missing>'}"
        )

    from bot_artifact import hash_path, validate_staged_artifact

    current_bot_hash = hash_path(_ei.get_bot_dir(version))
    if current_bot_hash != expected_bot_hash:
        raise RuntimeError(
            "candidate changed after official certification: "
            f"expected {expected_bot_hash}, current {current_bot_hash}"
        )

    _ei._require_national_epoch_registry_for_commit()

    _ei._git_ensure_main_branch()
    parent_line = f"parent: {bot_name(source_v)}"
    if parent2_v is not None:
        parent_line += f"\nparent2: {bot_name(parent2_v)}"
    msg = (
        f"evolve: v{source_v} → v{version}\n\n"
        f"{parent_line}\n"
        f"strategy: {strategy_tag}\n"
        f"{rating_info}"
    )
    msg += (
        f"\nofficial-certificate: {certificate_digest}"
        f"\nofficial-candidate-hash: {expected_bot_hash}"
        f"\nofficial-policy: {certificate_policy}"
    )
    bot_path = bot_relpath(version)
    preexisting_staged = [
        p for p in _ei._git("diff", "--cached", "--name-only", check=False).splitlines()
        if p
    ]
    preexisting_scope = classify_status_entries(
        [f"?? {path}" for path in preexisting_staged],
        int(version),
    )
    preexisting_blocking = (
        list(preexisting_scope.get("critical_entries") or [])
        + list(preexisting_scope.get("foreign_bot_entries") or [])
    )
    if preexisting_blocking:
        try:
            from system_log import log_system_event
            log_system_event(
                "pipeline.git_commit_blocked_preexisting_staged",
                "error",
                f"v{version}: refusing commit because blocking staged files already exist",
                {
                    "version": version,
                    "staged_files": preexisting_staged[:40],
                    "blocking_staged": preexisting_blocking[:40],
                },
            )
        except Exception:
            pass
        raise RuntimeError(
            "Refusing git_commit_bot with pre-existing blocking staged files: "
            + ", ".join(preexisting_blocking[:10])
        )

    from official_certification import publish_certificate_attestation

    publication = publish_certificate_attestation(certificate, _ei.get_bot_dir(version))
    if publication.get("certificate_digest") != certificate_digest:
        raise RuntimeError("published official attestation changed certificate digest")
    certificate_path = str(publication.get("relative_path") or "")
    if not certificate_path:
        raise RuntimeError("published official attestation path is missing")

    # LOG GAP FIX (2026-06-29): record what gets staged so a hand-edit bypass
    # (orchestrator LLM mutating bot code outside execute_workers) is visible.
    _staged = _ei._git("add", "--", bot_path, certificate_path, check=False)
    staged_bot_hash = hash_path(_ei.get_bot_dir(version))
    if staged_bot_hash != expected_bot_hash:
        _ei._git("restore", "--staged", "--", bot_path, certificate_path, check=False)
        raise RuntimeError(
            "candidate changed while staging official-certified artifact: "
            f"expected {expected_bot_hash}, current {staged_bot_hash}"
        )
    try:
        staged_artifact = validate_staged_artifact(
            _ei.get_bot_dir(version),
            repo_root=_ei.PROJECT_ROOT,
        )
    except Exception as exc:
        _ei._git("restore", "--staged", "--", bot_path, certificate_path, check=False)
        raise RuntimeError(
            "staged bot artifact validation failed: "
            f"{type(exc).__name__}: {str(exc)[:500]}"
        ) from exc
    if (
        not staged_artifact.get("valid")
        or staged_artifact.get("working_hash") != expected_bot_hash
        or staged_artifact.get("staged_hash") != expected_bot_hash
    ):
        _ei._git("restore", "--staged", "--", bot_path, certificate_path, check=False)
        working_files = {
            str(item.get("path") or "")
            for item in (staged_artifact.get("working_manifest") or {}).get("entries") or []
            if item.get("type") == "file"
        }
        staged_files = {
            str(item.get("path") or "")
            for item in (staged_artifact.get("staged_manifest") or {}).get("entries") or []
            if item.get("type") == "file"
        }
        raise RuntimeError(
            "staged Git blobs do not reproduce the certified bot artifact: "
            f"working_hash={staged_artifact.get('working_hash')} "
            f"staged_hash={staged_artifact.get('staged_hash')} "
            f"missing={sorted(working_files - staged_files)[:10]} "
            f"extra={sorted(staged_files - working_files)[:10]}"
        )
    allowed_paths = [bot_path, certificate_path]
    # Capture the staged file list right before commit for auditability.
    _staged_files = _ei._git("diff", "--cached", "--name-only", check=False).strip().splitlines()
    allowed_exact = set(allowed_paths)
    allowed_prefixes = [p.rstrip("/") + "/" for p in allowed_paths if p.endswith(bot_name(version))]
    commit_staged_files = [
        p for p in _staged_files
        if p in allowed_exact or any(p.startswith(prefix) for prefix in allowed_prefixes)
    ]
    outside_staged = [
        p for p in _staged_files
        if p not in allowed_exact and not any(p.startswith(prefix) for prefix in allowed_prefixes)
    ]
    outside_scope = classify_status_entries([f"?? {path}" for path in outside_staged], int(version))
    unexpected_staged = (
        list(outside_scope.get("critical_entries") or [])
        + list(outside_scope.get("foreign_bot_entries") or [])
    )
    if not commit_staged_files:
        raise RuntimeError(f"Refusing git_commit_bot with no staged files under {bot_path}")
    if certificate_path not in _staged_files:
        for path in allowed_paths:
            _ei._git("restore", "--staged", "--", path, check=False)
        raise RuntimeError(
            f"Refusing git_commit_bot without staged official attestation {certificate_path}"
        )
    if unexpected_staged:
        for path in allowed_paths:
            _ei._git("restore", "--staged", "--", path, check=False)
        try:
            from system_log import log_system_event
            log_system_event(
                "pipeline.git_commit_blocked_unexpected_staged",
                "error",
                f"v{version}: refusing commit because unrelated staged files appeared",
                {
                    "version": version,
                    "unexpected_staged": unexpected_staged[:40],
                    "allowed_paths": allowed_paths,
                    "outside_staged": outside_staged[:40],
                },
            )
        except Exception:
            pass
        raise RuntimeError(
            "Refusing git_commit_bot with unexpected staged files: "
            + ", ".join(unexpected_staged[:10])
        )
    try:
        from system_log import log_system_event
        log_system_event(
            "pipeline.git_commit_staged", "info",
            f"v{version}: staging {len(commit_staged_files)} file(s) for commit",
            {"version": version, "source_v": source_v,
             "staged_files": commit_staged_files[:30],
             "external_staged_preserved": outside_staged[:30]},
        )
    except Exception:
        pass
    _ei._git("commit", "-m", msg, "--", *allowed_paths)
    _commit_hash = _ei._git("rev-parse", "HEAD", check=False).strip()[:12]
    _ei.publish_runtime_expected_head("bot_commit", version=version)
    high_water_mutation = _ei._advance_national_epoch_high_water(version)
    high_water_refs = list(high_water_mutation.created_tags)
    tag = bot_tag(version)
    if _ei._git("tag", "-l", tag, check=False).strip():
        raise RuntimeError(
            f"Refusing to delete or recreate immutable completion tag {tag}; "
            "resume through the durable publication transaction"
        )
    tag_message = f"National bot v{format_version(version)}: {strategy_tag}"
    tag_message += (
        f"\n\nofficial-certificate: {certificate_digest}"
        f"\nofficial-candidate-hash: {expected_bot_hash}"
        f"\nofficial-policy: {certificate_policy}"
    )
    _ei._git("tag", "-a", tag, "HEAD", "-m", tag_message)
    from bot_artifact import validate_completion_tag

    tag_validation = validate_completion_tag(
        _ei.get_bot_dir(version),
        expected_metadata={
            "official-certificate": certificate_digest,
            "official-candidate-hash": expected_bot_hash,
            "official-policy": certificate_policy,
        },
        certificate_path=certificate_path,
    )
    if not tag_validation.get("valid"):
        raise RuntimeError(
            "new completion tag failed structural validation: "
            + ", ".join(tag_validation.get("issues") or [])
        )
    try:
        from official_eligibility import clear_registry_state_cache

        clear_registry_state_cache()
    except Exception:
        pass
    try:
        from system_log import log_system_event
        log_system_event(
            "pipeline.git_commit_done", "success",
            f"v{version}: committed {_commit_hash} + tag {tag}",
            {"version": version, "commit_hash": _commit_hash, "tag": tag},
        )
    except Exception:
        pass

    push_ok = False
    if _ei.evolution_git_push_enabled() or _ei.evolution_git_push_required():
        push_ok = _ei.git_push_refs(EVOLUTION_BRANCH, tag, *high_water_refs)
        _ei.publish_runtime_expected_head("bot_commit_push", version=version)
    return push_ok


def _git_command_succeeds(*args: str) -> bool:
    """Run a Git predicate while retaining its return code."""

    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(_ei.PROJECT_ROOT),
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _git_blob_bytes(ref: str, relative_path: str) -> bytes:
    result = subprocess.run(
        ["git", "cat-file", "blob", f"{ref}:{relative_path}"],
        cwd=str(_ei.PROJECT_ROOT),
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Git blob unavailable at {ref}:{relative_path}: "
            + result.stderr.decode("utf-8", "replace")[:300]
        )
    return bytes(result.stdout)


def _publication_commit_paths(intent: dict) -> tuple[str, str]:
    cert_path = str(intent.get("certificate_relative_path") or "")
    return (
        bot_relpath(int(intent["version"])),
        cert_path,
    )


def _validate_publication_certificate_file(intent: dict) -> None:
    from publication_transaction import file_sha256

    relative = str(intent.get("certificate_relative_path") or "")
    path = _ei.PROJECT_ROOT / relative
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("publication certificate attestation is missing or not regular")
    if file_sha256(path) != intent.get("certificate_file_sha256"):
        raise RuntimeError("publication certificate attestation bytes drifted")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"publication certificate attestation is unreadable: {type(exc).__name__}"
        ) from exc
    if payload.get("attestation_digest") != intent.get(
        "certificate_attestation_digest"
    ):
        raise RuntimeError("publication certificate attestation digest drifted")
    if payload.get("certificate_digest") != intent.get(
        "official_certificate_digest"
    ):
        raise RuntimeError("publication certificate digest drifted")


def _validate_existing_publication_commit(intent: dict, commit_oid: str) -> None:
    """Prove a recovered commit is the sole scoped effect after the intent."""

    from bot_artifact import canonical_digest, git_tree_artifact_manifest
    from publication_transaction import file_sha256

    baseline = str(intent.get("baseline_head") or "")
    bot_path, certificate_path = _ei._publication_commit_paths(intent)
    if not _ei._git_command_succeeds(
        "merge-base", "--is-ancestor", baseline, commit_oid
    ):
        raise RuntimeError("publication commit is not descended from intent baseline")
    if not _ei._git_command_succeeds(
        "merge-base", "--is-ancestor", commit_oid, _LOCAL_PUB_REF
    ):
        raise RuntimeError("publication commit is not reachable from local main")
    changed = [
        item
        for item in _ei._git(
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-r",
            commit_oid,
            check=False,
        ).splitlines()
        if item
    ]
    bot_prefix = bot_path.rstrip("/") + "/"
    if (
        certificate_path not in changed
        or not any(item.startswith(bot_prefix) for item in changed)
        or any(
            item != certificate_path and not item.startswith(bot_prefix)
            for item in changed
        )
    ):
        raise RuntimeError(
            "publication commit changed paths outside candidate/certificate scope: "
            + ", ".join(changed[:20])
        )
    manifest = git_tree_artifact_manifest(
        _ei.get_bot_dir(int(intent["version"])),
        commit_oid,
        repo_root=_ei.PROJECT_ROOT,
    )
    if canonical_digest(manifest) != intent.get("candidate_artifact_hash"):
        raise RuntimeError("publication commit candidate tree hash mismatch")
    certificate_bytes = _ei._git_blob_bytes(commit_oid, certificate_path)
    if hashlib.sha256(certificate_bytes).hexdigest() != intent.get(
        "certificate_file_sha256"
    ):
        raise RuntimeError("publication commit certificate blob hash mismatch")
    try:
        certificate_payload = json.loads(certificate_bytes.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"publication commit certificate blob is invalid: {type(exc).__name__}"
        ) from exc
    if certificate_payload.get("attestation_digest") != intent.get(
        "certificate_attestation_digest"
    ):
        raise RuntimeError("publication commit attestation digest mismatch")
    if certificate_payload.get("certificate_digest") != intent.get(
        "official_certificate_digest"
    ):
        raise RuntimeError("publication commit official certificate mismatch")
    message = _ei._git("show", "-s", "--format=%B", commit_oid, check=False).strip()
    if message != str(intent.get("commit_message") or "").strip():
        raise RuntimeError("publication commit message does not match frozen intent")


def _resolve_existing_publication_commit(intent: dict) -> str:
    baseline = str(intent.get("baseline_head") or "")
    bot_path, certificate_path = _ei._publication_commit_paths(intent)
    for relative in (bot_path, certificate_path):
        if _ei._git_command_succeeds(
            "cat-file", "-e", f"{baseline}:{relative}"
        ):
            raise RuntimeError(
                f"publication path already existed at intent baseline: {relative}"
            )
    commits = [
        item
        for item in _ei._git(
            "rev-list",
            "--reverse",
            f"{baseline}..{_LOCAL_PUB_REF}",
            "--",
            bot_path,
            certificate_path,
            check=False,
        ).splitlines()
        if item
    ]
    if len(commits) > 1:
        raise RuntimeError(
            "multiple commits touched frozen publication paths after intent"
        )
    if not commits:
        return ""
    commit_oid = commits[0]
    _ei._validate_existing_publication_commit(intent, commit_oid)
    return commit_oid


def _git_with_index(index_path: Path, *args: str) -> str:
    """Run one Git index operation against a transaction-private index."""

    environment = os.environ.copy()
    environment["GIT_INDEX_FILE"] = str(index_path)
    result = subprocess.run(
        ["git", *args],
        cwd=str(_ei.PROJECT_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {args[0]} with publication index failed: "
            + result.stderr.strip()[:500]
        )
    return result.stdout.strip()


def _publication_commit_object(tree_oid: str, parent_oid: str, message: str) -> str:
    """Create an immutable commit object without consulting the worktree."""

    result = subprocess.run(
        ["git", "commit-tree", tree_oid, "-p", parent_oid],
        cwd=str(_ei.PROJECT_ROOT),
        input=message,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    commit_oid = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit_oid):
        raise RuntimeError(
            "git commit-tree failed for frozen publication tree: "
            + result.stderr.strip()[:500]
        )
    return commit_oid


def _validate_frozen_publication_tree(
    intent: dict,
    *,
    tree_oid: str,
    parent_oid: str,
) -> None:
    """Prove the private-index tree contains only the frozen publication."""

    from bot_artifact import canonical_digest, git_tree_artifact_manifest

    bot_path, certificate_path = _ei._publication_commit_paths(intent)
    changed = [
        item
        for item in _ei._git(
            "diff",
            "--name-only",
            parent_oid,
            tree_oid,
            "--",
            check=False,
        ).splitlines()
        if item
    ]
    bot_prefix = bot_path.rstrip("/") + "/"
    if (
        certificate_path not in changed
        or not any(item.startswith(bot_prefix) for item in changed)
        or any(
            item != certificate_path and not item.startswith(bot_prefix)
            for item in changed
        )
    ):
        raise RuntimeError(
            "frozen publication tree changed paths outside candidate/certificate scope: "
            + ", ".join(changed[:20])
        )
    manifest = git_tree_artifact_manifest(
        _ei.get_bot_dir(int(intent["version"])),
        tree_oid,
        repo_root=_ei.PROJECT_ROOT,
    )
    if canonical_digest(manifest) != intent.get("candidate_artifact_hash"):
        raise RuntimeError("private-index candidate tree differs from frozen intent")
    certificate_bytes = _ei._git_blob_bytes(tree_oid, certificate_path)
    if hashlib.sha256(certificate_bytes).hexdigest() != intent.get(
        "certificate_file_sha256"
    ):
        raise RuntimeError("private-index certificate differs from frozen intent")


def _create_publication_commit(intent: dict) -> str:
    """CAS a commit built from an immutable private-index tree onto main."""

    from bot_artifact import hash_path, validate_staged_artifact

    version = int(intent["version"])
    bot_path, certificate_path = _ei._publication_commit_paths(intent)
    expected_hash = str(intent["candidate_artifact_hash"])
    preexisting_staged = [
        item
        for item in _ei._git("diff", "--cached", "--name-only", check=False).splitlines()
        if item
    ]
    preexisting_scope = classify_status_entries(
        [f"?? {path}" for path in preexisting_staged],
        version,
    )
    blocking = [
        *list(preexisting_scope.get("critical_entries") or []),
        *list(preexisting_scope.get("foreign_bot_entries") or []),
    ]
    if blocking:
        raise RuntimeError(
            "Refusing publication commit with pre-existing blocking staged files: "
            + ", ".join(blocking[:10])
        )
    add_targets = [bot_path]
    if certificate_path:
        add_targets.append(certificate_path)
    _ei._git("add", "--", *add_targets, check=False)
    ref_updated = False
    index_path = _ei.RESULTS_DIR / (
        f".publication-index.{os.getpid()}.{uuid.uuid4().hex}"
    )
    index_lock_path = Path(str(index_path) + ".lock")
    try:
        if hash_path(_ei.get_bot_dir(version)) != expected_hash:
            raise RuntimeError("candidate changed while staging publication intent")
        staged = validate_staged_artifact(
            _ei.get_bot_dir(version),
            repo_root=_ei.PROJECT_ROOT,
        )
        if (
            staged.get("valid") is not True
            or staged.get("working_hash") != expected_hash
            or staged.get("staged_hash") != expected_hash
        ):
            raise RuntimeError(
                "staged Git blobs do not reproduce frozen publication candidate"
            )
        staged_files = [
            item
            for item in _ei._git(
                "diff", "--cached", "--name-only", check=False
            ).splitlines()
            if item
        ]
        bot_prefix = bot_path.rstrip("/") + "/"
        scoped = [
            item
            for item in staged_files
            if item == certificate_path or item.startswith(bot_prefix)
        ]
        outside = [item for item in staged_files if item not in scoped]
        outside_scope = classify_status_entries(
            [f"?? {path}" for path in outside], version
        )
        unexpected = [
            *list(outside_scope.get("critical_entries") or []),
            *list(outside_scope.get("foreign_bot_entries") or []),
        ]
        if not any(item.startswith(bot_prefix) for item in scoped):
            raise RuntimeError("publication commit has no staged candidate files")
        if certificate_path not in scoped:
            raise RuntimeError("publication commit has no staged certificate")
        if unexpected:
            raise RuntimeError(
                "publication commit observed unexpected staged files: "
                + ", ".join(unexpected[:10])
            )
        _ei.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        parent_oid = _ei._git("rev-parse", _LOCAL_PUB_REF).strip()
        _ei._git_with_index(index_path, "read-tree", parent_oid)
        _ei._git_with_index(
            index_path,
            "add",
            "-A",
            "--",
            bot_path,
            certificate_path,
        )
        tree_oid = _ei._git_with_index(index_path, "write-tree")
        if not re.fullmatch(r"[0-9a-f]{40}", tree_oid):
            raise RuntimeError("private publication index did not produce a tree")
        _ei._validate_frozen_publication_tree(
            intent,
            tree_oid=tree_oid,
            parent_oid=parent_oid,
        )
        commit_oid = _ei._publication_commit_object(
            tree_oid,
            parent_oid,
            str(intent["commit_message"]),
        )
        # The tree and parent are now fixed objects. update-ref is the only
        # branch mutation and refuses a concurrently moved main ref.
        _ei._git(
            "update-ref",
            _LOCAL_PUB_REF,
            commit_oid,
            parent_oid,
        )
        ref_updated = True
    except Exception:
        if not ref_updated:
            _ei._git(
                "restore",
                "--staged",
                "--",
                bot_path,
                certificate_path,
                check=False,
            )
        raise
    finally:
        index_path.unlink(missing_ok=True)
        index_lock_path.unlink(missing_ok=True)
    _ei.publish_runtime_expected_head("bot_publication_commit", version=version)
    _ei._validate_existing_publication_commit(intent, commit_oid)
    return commit_oid


def _validate_local_publication_refs(intent: dict, commit_oid: str) -> dict:
    tag = str(intent["completion_tag"])
    high_water = str(intent["high_water_tag"])
    issues = []
    refs: dict[str, dict[str, str]] = {}
    for name in (tag, high_water):
        ref = f"refs/tags/{name}"
        tag_type = _ei._git("cat-file", "-t", ref, check=False).strip()
        object_oid = _ei._git("rev-parse", ref, check=False).strip()
        peeled_oid = _ei._git("rev-parse", f"{ref}^{{commit}}", check=False).strip()
        refs[name] = {
            "type": tag_type,
            "object_oid": object_oid,
            "peeled_commit_oid": peeled_oid,
        }
        if tag_type != "tag":
            issues.append(f"local_ref_not_annotated:{name}")
        if peeled_oid != commit_oid:
            issues.append(f"local_ref_commit_mismatch:{name}")
    contents = _ei._git(
        "for-each-ref",
        "--format=%(contents)",
        f"refs/tags/{tag}",
        check=False,
    ).strip()
    if contents != str(intent.get("tag_message") or "").strip():
        issues.append("completion_tag_message_mismatch")
    if issues:
        raise RuntimeError("invalid local publication refs: " + "; ".join(issues))
    return refs


def remote_completion_ref_snapshot() -> dict[str, str]:
    """Return the exact remote active-epoch completion-tag namespace."""

    raw = _ei._git("ls-remote", "origin", f"refs/tags/{bot_tag_glob()}")
    refs: dict[str, str] = {}
    for line in raw.splitlines():
        oid, separator, ref = line.partition("\t")
        if (
            separator
            and len(oid) == 40
            and ref.startswith(f"refs/tags/{ACTIVE_TAG_PREFIX}")
        ):
            refs[ref] = oid
    return dict(sorted(refs.items()))


@contextmanager
def _publication_checkpoint_linearization_lock():
    """Fence publishing checkpoint writers across authority-check + push."""

    with _ei._locked_state_sidecar(
        _ei.PIPELINE_STATE_FILE,
        lock_type=fcntl.LOCK_EX,
    ):
        yield


def _push_first_strict_publication(
    intent: dict,
    commit_oid: str,
    local_refs: dict,
    *,
    pre_push_authority,
) -> bool:
    """CAS the first strict publication without any reconcile/merge window.

    The first strict bot changes the authority regime from the migration seed
    to the normal strict pool.  Its frozen ``origin/main`` is therefore a
    compare-and-swap precondition, not a merge base.  The completion and
    high-water tags use ordinary create-only refspecs; no force option ever
    applies to a tag.  ``--atomic`` makes a concurrent main/tag change reject
    the complete ref set with no partial remote effects.
    """

    if list(intent.get("prepublication_strict_bots") or []):
        raise RuntimeError("first-strict publication CAS used for a non-first bot")
    if not callable(pre_push_authority):
        raise RuntimeError("first-strict publication requires a pre-push authority check")
    baseline = str(intent.get("baseline_remote_main") or "")
    completion = str(intent.get("completion_tag") or "")
    high_water = str(intent.get("high_water_tag") or "")
    local_main = _ei._git("rev-parse", _LOCAL_PUB_REF, check=False).strip()
    if not (
        len(baseline) == 40
        and len(local_main) == 40
        and _ei._git_command_succeeds("merge-base", "--is-ancestor", baseline, local_main)
        and _ei._git_command_succeeds("merge-base", "--is-ancestor", commit_oid, local_main)
    ):
        raise RuntimeError(
            "first-strict publication local main is not a fast-forward of the frozen remote baseline"
        )

    # Fetch is read-only with respect to the remote.  It makes a strict bot
    # published since intent creation visible to the authority callback, while
    # the later main lease closes the fetch/check/push race.
    _ei._git("fetch", "origin", "--prune", "--tags")
    wanted = (
        _LOCAL_PUB_REF,
        f"refs/tags/{bot_tag_glob()}",
        f"refs/tags/{high_water}",
    )
    raw = _ei._git("ls-remote", "origin", *wanted)
    remote_refs: dict[str, str] = {}
    for line in raw.splitlines():
        oid, separator, ref = line.partition("\t")
        if separator and oid and ref:
            remote_refs[ref] = oid
    if remote_refs.get(_LOCAL_PUB_REF) != baseline:
        raise RuntimeError(
            "first-strict publication blocked: origin/main changed after intent baseline"
        )
    remote_completion_refs = dict(sorted(
        (ref, oid)
        for ref, oid in remote_refs.items()
        if ref.startswith(f"refs/tags/{ACTIVE_TAG_PREFIX}")
    ))
    if remote_completion_refs != dict(
        intent.get("baseline_remote_completion_refs") or {}
    ):
        raise RuntimeError(
            "first-strict publication blocked: remote strict completion refs "
            "changed after intent baseline"
        )
    occupied = [
        ref for ref in (
            f"refs/tags/{completion}",
            f"refs/tags/{high_water}",
        )
        if remote_refs.get(ref)
    ]
    if occupied:
        raise RuntimeError(
            "first-strict publication blocked: create-only remote tag already exists: "
            + ", ".join(occupied)
        )

    with _ei._publication_checkpoint_linearization_lock():
        # The callback re-reads the checkpoint while this stable sidecar lock
        # excludes every normal checkpoint writer.  Keep the lock until the
        # atomic remote ref transaction has linearized.
        pre_push_authority()

        # A callback must not be able to move any frozen local source ref.
        # Remote races are handled by the leases below.
        if _ei._git("rev-parse", _LOCAL_PUB_REF, check=False).strip() != local_main:
            raise RuntimeError("first-strict publication local main changed during authority check")
        for name in (completion, high_water):
            expected = str((local_refs.get(name) or {}).get("object_oid") or "")
            current = _ei._git("rev-parse", f"refs/tags/{name}", check=False).strip()
            if not expected or current != expected:
                raise RuntimeError(
                    f"first-strict publication local tag changed during authority check: {name}"
                )

        refspecs = (
            f"{_LOCAL_PUB_REF}:{_LOCAL_PUB_REF}",
            f"refs/tags/{completion}:refs/tags/{completion}",
            f"refs/tags/{high_water}:refs/tags/{high_water}",
        )
        try:
            _ei._git(
                "push",
                "--atomic",
                f"--force-with-lease={_LOCAL_PUB_REF}:{baseline}",
                "origin",
                *refspecs,
            )
        except Exception as exc:
            raise RuntimeError(
                "first-strict publication atomic lease failed; no publication refs were accepted: "
                f"{type(exc).__name__}: {str(exc)[:300]}"
            ) from exc
    return True


def ensure_bot_git_publication(
    publication_intent: dict,
    *,
    official_certificate: dict,
    pre_push_authority=None,
) -> dict:
    """Idempotently converge intent_recorded -> committed -> local refs -> push.

    Existing completion tags are immutable: this function never deletes,
    recreates, or force-updates one.  Recovery reconstructs progress from Git
    rather than trusting a caller-supplied phase.
    """

    from bot_artifact import hash_path, validate_completion_tag
    from official_certification import FULL_POLICY_ID
    from publication_transaction import (
        publication_intent_structure_errors,
        PUBLICATION_INTENT_KIND_STAGING,
    )

    intent = dict(publication_intent or {})
    errors = publication_intent_structure_errors(intent)
    if errors:
        raise RuntimeError("invalid publication intent: " + "; ".join(errors))
    version = int(intent["version"])
    is_staging = intent.get("kind") == PUBLICATION_INTENT_KIND_STAGING
    certificate = dict(official_certificate or {})
    if is_staging:
        # Staging tier: no official certificate exists yet (cert runs async).
        # Only the candidate hash must match; skip all cert-bound validation.
        if hash_path(_ei.get_bot_dir(version)) != intent.get("candidate_artifact_hash"):
            raise RuntimeError("candidate changed after publication intent was recorded")
    else:
        expected_certificate = {
            "certificate_digest": intent.get("official_certificate_digest"),
            "candidate_hash": intent.get("candidate_artifact_hash"),
            "policy_id": intent.get("official_policy_id"),
        }
        for field, expected in expected_certificate.items():
            if certificate.get(field) != expected:
                raise RuntimeError(f"official certificate {field} differs from publication intent")
        if certificate.get("policy_id") != FULL_POLICY_ID:
            raise RuntimeError("publication intent is not bound to the full official policy")
        if hash_path(_ei.get_bot_dir(version)) != intent.get("candidate_artifact_hash"):
            raise RuntimeError("candidate changed after publication intent was recorded")
        _ei._validate_publication_certificate_file(intent)
    _ei._require_national_epoch_registry_for_commit()
    _ei._git_ensure_main_branch()

    with _ei.bot_publication_lock():
        commit_oid = _ei._resolve_existing_publication_commit(intent)
        if not commit_oid:
            commit_oid = _ei._create_publication_commit(intent)

        _ei._advance_national_epoch_high_water(version)
        tag = str(intent["completion_tag"])
        if _ei._git("tag", "-l", tag, check=False).strip():
            # Create-only semantics: an existing tag is evidence to validate,
            # never mutable state to repair in place.
            existing_target = _ei._git(
                "rev-parse", f"refs/tags/{tag}^{{commit}}", check=False
            ).strip()
            if existing_target != commit_oid:
                raise RuntimeError("existing completion tag points at a different commit")
        else:
            _ei._git(
                "tag",
                "-a",
                tag,
                commit_oid,
                "-m",
                str(intent["tag_message"]),
            )

        local_refs = _ei._validate_local_publication_refs(intent, commit_oid)
        if is_staging:
            # Staging tag annotation carries staging-candidate-hash, not
            # official-certificate lines, and has no certificate file.
            tag_validation = validate_completion_tag(
                _ei.get_bot_dir(version),
                expected_metadata={
                    "staging-candidate-hash": str(
                        intent["candidate_artifact_hash"]
                    ),
                },
                certificate_path="",
            )
        else:
            tag_validation = validate_completion_tag(
                _ei.get_bot_dir(version),
                expected_metadata={
                    "official-certificate": str(
                        intent["official_certificate_digest"]
                    ),
                    "official-candidate-hash": str(
                        intent["candidate_artifact_hash"]
                    ),
                    "official-policy": str(intent["official_policy_id"]),
                },
                certificate_path=str(intent["certificate_relative_path"]),
            )
        if tag_validation.get("valid") is not True:
            raise RuntimeError(
                "completion tag failed frozen publication validation: "
                + ", ".join(tag_validation.get("issues") or [])
            )
        push_attempted = bool(
            intent.get("remote_publication_enabled")
            or intent.get("remote_publication_required")
        )
        push_ok = False
        already_remote = False
        if push_attempted:
            provisional_state = {
                "publication_id": intent["publication_id"],
                "version": version,
                "commit_oid": commit_oid,
                "local_refs": local_refs,
            }
            existing_remote = _ei.verify_remote_bot_publication(
                intent,
                local_state=provisional_state,
            )
            already_remote = existing_remote.get("valid") is True
            if already_remote:
                push_ok = True
            elif not list(intent.get("prepublication_strict_bots") or []):
                push_ok = _ei._push_first_strict_publication(
                    intent,
                    commit_oid,
                    local_refs,
                    pre_push_authority=pre_push_authority,
                )
            else:
                if not callable(pre_push_authority):
                    raise RuntimeError(
                        "publication requires a pre-push authority check"
                    )
                with _ei._publication_checkpoint_linearization_lock():
                    pre_push_authority()
                    push_ok = _ei.git_push_refs(
                        EVOLUTION_BRANCH,
                        tag,
                        str(intent["high_water_tag"]),
                    )
            _ei.publish_runtime_expected_head(
                "bot_publication_push", version=version
            )
            _ei._clear_remote_publication_cache()
        return {
            "publication_id": intent["publication_id"],
            "version": version,
            "commit_oid": commit_oid,
            "local_refs": local_refs,
            "local_valid": True,
            "push_attempted": push_attempted,
            "push_ok": bool(push_ok),
            "already_remote": already_remote,
        }


def verify_remote_bot_publication(
    publication_intent: dict,
    *,
    local_state: dict | None = None,
) -> dict:
    """Independently prove remote tag objects, peeled commits, and main reachability."""

    intent = dict(publication_intent or {})
    version = int(intent.get("version") or -1)
    commit_oid = str((local_state or {}).get("commit_oid") or "")
    if not commit_oid:
        try:
            commit_oid = _ei._git(
                "rev-parse",
                f"refs/tags/{intent['completion_tag']}^{{commit}}",
                check=False,
            ).strip()
        except Exception:
            commit_oid = ""
    tag_names = [
        str(intent.get("completion_tag") or ""),
        str(intent.get("high_water_tag") or ""),
    ]
    wanted = [_LOCAL_PUB_REF]
    for name in tag_names:
        wanted.extend((f"refs/tags/{name}", f"refs/tags/{name}^{{}}"))
    try:
        raw = _ei._git("ls-remote", "origin", *wanted)
    except Exception as exc:
        return {
            "valid": False,
            "version": version,
            "issues": [f"remote_refs_unavailable:{type(exc).__name__}"],
        }
    remote_refs: dict[str, str] = {}
    for line in raw.splitlines():
        oid, separator, ref = line.partition("\t")
        if separator and oid and ref:
            remote_refs[ref] = oid
    issues: list[str] = []
    remote_main = remote_refs.get(_LOCAL_PUB_REF, "")
    if len(remote_main) != 40:
        issues.append("remote_main_missing")
    local_refs = (local_state or {}).get("local_refs") or {}
    for name in tag_names:
        local_object = str(
            (local_refs.get(name) or {}).get("object_oid")
            or _ei._git("rev-parse", f"refs/tags/{name}", check=False).strip()
        )
        local_peeled = str(
            (local_refs.get(name) or {}).get("peeled_commit_oid")
            or _ei._git(
                "rev-parse", f"refs/tags/{name}^{{commit}}", check=False
            ).strip()
        )
        if remote_refs.get(f"refs/tags/{name}") != local_object:
            issues.append(f"remote_tag_object_mismatch:{name}")
        if remote_refs.get(f"refs/tags/{name}^{{}}") != local_peeled:
            issues.append(f"remote_tag_peeled_mismatch:{name}")
        if local_peeled != commit_oid:
            issues.append(f"local_tag_commit_mismatch:{name}")
    if not issues:
        try:
            _ei._git(
                "fetch",
                "--no-tags",
                "origin",
                f"{_LOCAL_PUB_REF}:{_REMOTE_PUB_REF}",
            )
        except Exception as exc:
            issues.append(f"remote_main_fetch_failed:{type(exc).__name__}")
        else:
            fetched = _ei._git("rev-parse", _REMOTE_PUB_REF, check=False).strip()
            if fetched != remote_main:
                issues.append("remote_main_fetch_identity_mismatch")
            elif not _ei._git_command_succeeds(
                "merge-base", "--is-ancestor", commit_oid, remote_main
            ):
                issues.append("publication_commit_not_on_remote_main")
    return {
        "valid": not issues,
        "version": version,
        "publication_id": intent.get("publication_id"),
        "commit_oid": commit_oid,
        "remote_main_oid": remote_main,
        "remote_refs": remote_refs,
        "issues": list(dict.fromkeys(issues)),
    }


def git_get_parent(version):
    """从 tag/commit message 解析 parent。"""
    tag = bot_tag(version)
    tags = _ei._git("tag", "-l", tag, check=False)
    if tags:
        commit_hash = _ei._git("rev-list", "-n", "1", tag, check=False).strip()
        if not commit_hash:
            return None
        msg = _ei._git("show", "-s", "--format=%B", commit_hash, check=False)
    else:
        log = _ei._git("log", "--diff-filter=A", "--oneline", "-1", "--",
                    bot_relpath(version) + "/", check=False)
        if not log:
            return None
        commit_hash = log.split()[0]
        msg = _ei._git("show", "-s", "--format=%B", commit_hash, check=False)
    for line in (msg or "").split("\n"):
        if line.strip().startswith("parent:"):
            parent = line.split(":", 1)[1].strip()
            parsed = parse_bot_version(parent)
            return parsed if parsed is not None else parent
    return None
