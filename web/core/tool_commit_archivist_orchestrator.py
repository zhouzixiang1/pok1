"""Post-publication archivist orchestrator extracted from tool_commit.

This companion module hosts the three orchestrator-level pieces of the
post-publication archivist pipeline that were originally inline in
``web/core/tool_commit.py``: the strict log-tree manifest hasher, the
reaped-target convergence verifier, and the durable top-level archivist
coroutine.  Moving them here reunites the orchestrator with its helper
cluster (which already lives in ``tool_commit_archivist``).

The main module re-exports these three symbols at the very bottom of
``tool_commit.py`` and replaces the original definitions with thin delegate
shells, so external importers and the test-suite continue to resolve them as
``tool_commit.<name>`` and ``monkeypatch.setattr(tool_commit, ...)`` keeps
working.

IMPORTANT -- shared-symbol access model (mirrors ``tool_commit_archivist``):

Names that are defined in ``tool_commit`` and that tests may monkeypatch on
``tool_commit`` are read off the *live* ``tool_commit`` module attribute via
``_tc.<name>`` rather than imported at the top of this file.  This covers:

* Module constants (``RESULTS_DIR``, ``FIRST_STRICT_POLICY_VERSION``,
  ``EVOLUTION_BRANCH``) and the ``LLMAvailabilityBlocked`` sentinel.
* Utility helpers (``_git``, ``_git_ensure_main_branch``, ``git_push_refs``,
  ``bot_tag``, ``bot_name``, ``get_bot_dir``, ``get_active_bots``,
  ``log_system_event``, ``_set_pipeline_status``, ``_get_ui``,
  ``build_archive_rotation_plan``, ``archive_rotate_files``).
* The archivist helpers re-exported by the parent from
  ``tool_commit_archivist`` (``_build_pool_reap_plan``,
  ``_execute_pool_reap_plan``, ``_validate_pool_reap_plan``,
  ``_build_strict_log_cleanup_plan``, ``_execute_strict_log_cleanup``,
  ``_revalidate_strict_log_archives``, ``_verify_post_publication_worktree``,
  ``_handoff_publication_result``, ``_durable_archivist_state_write``,
  ``_git_dirty_paths``, ``_remote_ref_snapshot``).  Routing these through
  ``_tc.<name>`` ensures test monkeypatches on ``tool_commit`` propagate.

Intra-cluster calls (``_run_durable_post_publication_archivist`` calling
``_converge_and_verify_reaped_target``) stay bare, since both live in this
module.  Names imported inline inside a function body stay inline.
"""

from __future__ import annotations

import hashlib
import os
import stat
import time
from pathlib import Path

import tool_commit as _tc


def _safe_log_tree_manifest(root: Path, *, version: int) -> dict:
    """Hash one exact strict-generation log tree without following links."""

    from bot_artifact import canonical_digest

    if int(version) < _tc.FIRST_STRICT_POLICY_VERSION:
        raise RuntimeError("legacy_log_tree_forbidden")
    expected_parent = _tc.RESULTS_DIR / f"v{int(version)}"
    if root.parent != expected_parent:
        raise RuntimeError("log_tree_root_outside_strict_generation")
    for directory in (_tc.RESULTS_DIR, expected_parent):
        directory_stat = os.lstat(directory)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or stat.S_ISLNK(directory_stat.st_mode)
        ):
            raise RuntimeError("log_tree_parent_unsafe")
    root_stat = os.lstat(root)
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise RuntimeError("log_tree_root_unsafe")
    entries: list[dict] = []
    pending = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        before = os.lstat(directory)
        if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise RuntimeError("log_tree_directory_unsafe")
        with os.scandir(directory) as scanner:
            children = sorted(scanner, key=lambda item: item.name)
        if len(entries) + len(children) > 4096:
            raise RuntimeError("log_tree_member_limit")
        for child in children:
            if (
                not child.name
                or child.name in {".", ".."}
                or "/" in child.name
                or "\\" in child.name
                or any(ord(char) < 32 for char in child.name)
            ):
                raise RuntimeError("log_tree_member_name_unsafe")
            relative = f"{prefix}/{child.name}" if prefix else child.name
            if len(relative.encode("utf-8")) > 1024:
                raise RuntimeError("log_tree_member_path_too_long")
            metadata = child.stat(follow_symlinks=False)
            child_path = Path(child.path)
            if stat.S_ISDIR(metadata.st_mode):
                entries.append({"path": relative, "kind": "directory"})
                pending.append((child_path, relative))
                continue
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise RuntimeError("log_tree_member_unsafe")
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(child_path, flags)
            hasher = hashlib.sha256()
            size = 0
            try:
                opened = os.fstat(descriptor)
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    size += len(chunk)
                live = os.lstat(child_path)
            finally:
                os.close(descriptor)
            if (
                opened.st_nlink != 1
                or live.st_nlink != 1
                or (opened.st_dev, opened.st_ino)
                != (metadata.st_dev, metadata.st_ino)
                or (live.st_dev, live.st_ino)
                != (metadata.st_dev, metadata.st_ino)
                or opened.st_size != size
                or live.st_size != size
                or opened.st_mtime_ns != live.st_mtime_ns
                or opened.st_ctime_ns != live.st_ctime_ns
            ):
                raise RuntimeError("log_tree_member_changed")
            entries.append({
                "path": relative,
                "kind": "file",
                "size": size,
                "sha256": hasher.hexdigest(),
            })
        after = os.lstat(directory)
        if (
            (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise RuntimeError("log_tree_directory_changed")
    entries.sort(key=lambda row: (row["path"], row["kind"]))
    payload = {
        "schema_version": 1,
        "kind": "strict-generation-log-tree",
        "version": int(version),
        "source_relative_path": f"v{int(version)}/logs",
        "entries": entries,
    }
    return {**payload, "tree_digest": canonical_digest(payload)}



def _converge_and_verify_reaped_target(name: str, record: dict) -> dict:
    """Finish a planned tombstone crash and return exact local/remote proof."""

    from bot_namespace import parse_bot_version
    from evolution_infra import load_reaped_bot_versions
    from national_epoch_registry import REAPED_TAG_PREFIX

    version = parse_bot_version(name)
    if version is None or version < _tc.FIRST_STRICT_POLICY_VERSION:
        raise RuntimeError("planned_reap_target_invalid")
    completion_ref = f"refs/tags/{_tc.bot_tag(version)}"
    tombstone_name = f"{REAPED_TAG_PREFIX}{version}"
    tombstone_ref = f"refs/tags/{tombstone_name}"
    if _tc._git("cat-file", "-t", completion_ref, check=False).strip() != "tag":
        raise RuntimeError("reap_completion_tag_missing")
    if _tc._git("cat-file", "-t", tombstone_ref, check=False).strip() != "tag":
        raise RuntimeError("reap_tombstone_tag_missing")
    completion_commit = _tc._git(
        "rev-parse", f"{completion_ref}^{{commit}}", check=False
    ).strip()
    tombstone_object = _tc._git("rev-parse", tombstone_ref, check=False).strip()
    tombstone_commit = _tc._git(
        "rev-parse", f"{tombstone_ref}^{{commit}}", check=False
    ).strip()
    if (
        len(tombstone_object) != 40
        or len(completion_commit) != 40
        or tombstone_commit != completion_commit
    ):
        raise RuntimeError("reap_tombstone_identity_mismatch")

    remote = record["identity"]["remote_publication"]
    remote_proof = {"required": remote.get("required") is True}
    if remote.get("required") is True:
        wanted = (
            f"refs/heads/{_tc.EVOLUTION_BRANCH}",
            tombstone_ref,
            f"{tombstone_ref}^{{}}",
        )
        refs = _tc._remote_ref_snapshot(*wanted)
        if (
            refs.get(tombstone_ref) != tombstone_object
            or refs.get(f"{tombstone_ref}^{{}}") != completion_commit
        ):
            if not _tc.git_push_refs(tombstone_name):
                raise RuntimeError("reap_tombstone_remote_push_failed")
            refs = _tc._remote_ref_snapshot(*wanted)
        if (
            refs.get(tombstone_ref) != tombstone_object
            or refs.get(f"{tombstone_ref}^{{}}") != completion_commit
        ):
            raise RuntimeError("reap_remote_proof_mismatch")
        remote_main = str(refs.get(f"refs/heads/{_tc.EVOLUTION_BRANCH}") or "")
        if len(remote_main) != 40 or any(
            char not in "0123456789abcdef" for char in remote_main
        ):
            raise RuntimeError("reap_remote_main_invalid")
        from evolution_infra import _git_command_succeeds

        tracking = _tc._git(
            "rev-parse", f"refs/remotes/origin/{_tc.EVOLUTION_BRANCH}", check=False
        ).strip()
        if tracking != remote_main:
            _tc._git(
                "fetch",
                "--no-tags",
                "origin",
                f"refs/heads/{_tc.EVOLUTION_BRANCH}:refs/remotes/origin/{_tc.EVOLUTION_BRANCH}",
            )
        if not _git_command_succeeds(
            "merge-base",
            "--is-ancestor",
            record["identity"]["commit_oid"],
            remote_main,
        ):
            raise RuntimeError("reap_publication_not_on_remote_main")
        remote_proof.update({
            "publication_remote_main_oid": remote.get("remote_main_oid"),
            "verified_remote_main_oid": remote_main,
            "publication_commit_is_ancestor": True,
            "tombstone_object_oid": refs[tombstone_ref],
            "tombstone_commit_oid": refs[f"{tombstone_ref}^{{}}"],
        })
    elif remote.get("explicit_test_mode") is not True:
        raise RuntimeError("reap_local_only_mode_unproven")

    # Once the durable local/required-remote tombstone is proven, removing the
    # ignored completion capability is the idempotent final half of reaping.
    sentinel = _tc.get_bot_dir(version) / ".completed"
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

    # Durably prove the directory entry absence even on a crash retry where
    # the original reaper already removed the sentinel before its own fsync.
    _fsync_directory(sentinel.parent)
    if os.path.lexists(sentinel):
        raise RuntimeError("reap_completed_sentinel_still_present")
    if version not in load_reaped_bot_versions():
        raise RuntimeError("reap_registry_projection_missing")
    if name in set(_tc.get_active_bots()):
        raise RuntimeError("reaped_target_still_active")
    if _tc._git("rev-parse", "HEAD").strip() != record["identity"]["commit_oid"]:
        raise RuntimeError("reap_changed_head")
    return {
        "bot": name,
        "version": version,
        "completion_commit_oid": completion_commit,
        "tombstone_tag": tombstone_name,
        "tombstone_object_oid": tombstone_object,
        "tombstone_commit_oid": tombstone_commit,
        "completed_sentinel_absent": True,
        "registry_projection_present": True,
        "remote_proof": remote_proof,
    }



async def _run_durable_post_publication_archivist(v: int, source_v: int):
    """Execute or resume every post-publication effect through one journal."""

    from bot_artifact import canonical_digest
    from post_publication_handoff import (
        claim_post_publication_handoff,
        complete_handoff_step,
        complete_post_publication_handoff,
        load_archive_snapshot,
        plan_handoff_step,
        release_post_publication_handoff_claim,
        write_archive_annotation,
    )

    claim_id = ""
    try:
        record, claim_id = claim_post_publication_handoff(v, source_v)
        if record.get("state") == "completed":
            return {
                "version": v,
                "source_v": source_v,
                "archivist_completed": True,
                "idempotent_replay": True,
            }
        _tc._set_pipeline_status(f"Archiving v{v}")
        _tc._git_ensure_main_branch()
        if _tc._git("rev-parse", "HEAD").strip() != record["identity"]["commit_oid"]:
            raise RuntimeError("post_publication_head_not_publication_commit")
        if _tc._git_dirty_paths():
            raise RuntimeError("post_publication_worktree_not_clean")
        snapshot = load_archive_snapshot(v)
        publishing_checkpoint = snapshot["publishing_checkpoint_projection"]
        publication_result = _tc._handoff_publication_result(record)

        def done(name):
            return record["steps"][name].get("status") == "completed"

        if not done("stability_observation"):
            from stability_observation import record_published_generation

            row = record["steps"]["stability_observation"]
            if row.get("status") == "pending":
                plan = {
                    "schema_version": 1,
                    "kind": "stability-observation-plan",
                    "publication_id": record["identity"]["publication_id"],
                    "publishing_checkpoint_digest": record["identity"][
                        "publishing_checkpoint_digest"
                    ],
                    "strength_evidence_identity_digest": canonical_digest(
                        snapshot["strength_evidence_identity"]
                    ),
                }
                record = plan_handoff_step(
                    v, source_v, claim_id, "stability_observation", plan
                )
                row = record["steps"]["stability_observation"]
            projection = record_published_generation(
                version=v,
                publication_result=publication_result,
                publishing_checkpoint=publishing_checkpoint,
            )
            record = complete_handoff_step(
                v,
                source_v,
                claim_id,
                "stability_observation",
                {
                    "plan_digest": row["plan_digest"],
                    "publication_id": record["identity"]["publication_id"],
                    "continuity_id": projection.get("continuity_id"),
                    "count": projection.get("count"),
                    "target": projection.get("target"),
                    "complete": projection.get("complete"),
                },
            )

        if not done("reap_signal"):
            row = record["steps"]["reap_signal"]
            if row.get("status") == "pending":
                signal_text = f"{time.time():.6f}\n"
                plan = {
                    "schema_version": 1,
                    "kind": "rating-daemon-refresh-plan",
                    "publication_id": record["identity"]["publication_id"],
                    "signal_text": signal_text,
                    "signal_sha256": hashlib.sha256(
                        signal_text.encode("utf-8")
                    ).hexdigest(),
                }
                record = plan_handoff_step(
                    v, source_v, claim_id, "reap_signal", plan
                )
                row = record["steps"]["reap_signal"]
            plan = row["plan"]
            signal_path = _tc.RESULTS_DIR / ".reap_signal"
            signal_text = str(plan["signal_text"])
            # The daemon may consume this file before the receipt write. A
            # retry safely reissues the same refresh capability.
            from evolution_infra import _atomic_publish_state_text, _locked_state_sidecar
            import fcntl

            with _locked_state_sidecar(signal_path, lock_type=fcntl.LOCK_EX):
                _atomic_publish_state_text(signal_path, signal_text)
            record = complete_handoff_step(
                v, source_v, claim_id, "reap_signal", {
                    "plan_digest": row["plan_digest"],
                    "publication_id": record["identity"]["publication_id"],
                    "signal_sha256": hashlib.sha256(
                        signal_text.encode("utf-8")
                    ).hexdigest(),
                }
            )

        if not done("priority_eval"):
            row = record["steps"]["priority_eval"]
            if row.get("status") == "pending":
                priority = {
                    "bot": _tc.bot_name(v),
                    "min_games": 500,
                    "since": time.time(),
                    "publication_id": record["identity"]["publication_id"],
                }
                record = plan_handoff_step(
                    v,
                    source_v,
                    claim_id,
                    "priority_eval",
                    {
                        "schema_version": 1,
                        "kind": "priority-evaluation-plan",
                        "payload": priority,
                    },
                )
                row = record["steps"]["priority_eval"]
            priority = row["plan"]["payload"]
            priority_sha = _tc._durable_archivist_state_write(
                _tc.RESULTS_DIR / "priority_eval.json", priority
            )
            record = complete_handoff_step(
                v, source_v, claim_id, "priority_eval", {
                    "plan_digest": row["plan_digest"],
                    "bot": _tc.bot_name(v),
                    "min_games": 500,
                    "publication_id": record["identity"]["publication_id"],
                    "payload_sha256": priority_sha,
                }
            )

        if not done("archive_rotation"):
            row = record["steps"]["archive_rotation"]
            if row.get("status") == "pending":
                rotation_plan = _tc.build_archive_rotation_plan(
                    v,
                    record["identity"]["publication_id"],
                )
                record = plan_handoff_step(
                    v,
                    source_v,
                    claim_id,
                    "archive_rotation",
                    rotation_plan,
                )
                row = record["steps"]["archive_rotation"]
            rotations = _tc.archive_rotate_files(v, row["plan"])
            if any(
                not isinstance(item, dict)
                or item.get("source_preserved_append_only") is not True
                or len(str(item.get("rotation_id") or "")) != 64
                or len(str(item.get("archive_sha256") or "")) != 64
                for item in rotations
            ):
                raise RuntimeError("archive_rotation_receipt_invalid")
            record = complete_handoff_step(
                v, source_v, claim_id, "archive_rotation", {
                    "plan_digest": row["plan_digest"],
                    "version": v,
                    "rotations": rotations,
                    "rotation_set_digest": canonical_digest(rotations),
                }
            )

        if not done("log_cleanup"):
            row = record["steps"]["log_cleanup"]
            if row.get("status") == "pending":
                log_plan = _tc._build_strict_log_cleanup_plan(v)
                log_plan["publication_id"] = record["identity"]["publication_id"]
                record = plan_handoff_step(
                    v, source_v, claim_id, "log_cleanup", log_plan
                )
                row = record["steps"]["log_cleanup"]
            log_archives = _tc._execute_strict_log_cleanup(
                row["plan"],
                expected_handoff_version=v,
                expected_publication_id=record["identity"]["publication_id"],
            )
            record = complete_handoff_step(
                v, source_v, claim_id, "log_cleanup", {
                    "plan_digest": row["plan_digest"],
                    "version": v,
                    "archives": log_archives,
                    "archive_set_digest": canonical_digest(log_archives),
                }
            )

        reap_row = record["steps"]["pool_reap"]
        if reap_row.get("status") != "completed":
            if reap_row.get("status") == "pending":
                reap_plan = _tc._build_pool_reap_plan(record)
                record = plan_handoff_step(
                    v, source_v, claim_id, "pool_reap", reap_plan
                )
                reap_row = record["steps"]["pool_reap"]
            reap_plan = reap_row["plan"]
            reap_output = await _tc._execute_pool_reap_plan(reap_plan, record)
            record = complete_handoff_step(
                v,
                source_v,
                claim_id,
                "pool_reap",
                {**reap_output, "plan_digest": reap_row["plan_digest"]},
            )

        if not done("cycle_annotation"):
            snapshot = load_archive_snapshot(v)
            row = record["steps"]["cycle_annotation"]
            if row.get("status") == "pending":
                unannotated = dict(snapshot)
                unannotated.pop("archivist_notes", None)
                record = plan_handoff_step(
                    v,
                    source_v,
                    claim_id,
                    "cycle_annotation",
                    {
                        "schema_version": 1,
                        "kind": "cycle-archivist-annotation-plan",
                        "publication_id": record["identity"]["publication_id"],
                        "archive_pre_annotation_digest": canonical_digest(
                            unannotated
                        ),
                    },
                )
                row = record["steps"]["cycle_annotation"]
            unannotated = dict(snapshot)
            unannotated.pop("archivist_notes", None)
            if canonical_digest(unannotated) != row["plan"].get(
                "archive_pre_annotation_digest"
            ):
                raise RuntimeError("cycle_annotation_archive_preimage_changed")
            from post_publication_handoff import local_handoff_identity_errors

            local_cycle_issues = local_handoff_identity_errors(record)
            if local_cycle_issues:
                raise RuntimeError(
                    "cycle_annotation_local_identity_invalid:"
                    + ";".join(local_cycle_issues[:30])
                )
            existing_annotation = snapshot.get("archivist_notes")
            if existing_annotation is not None:
                from cycle_archivist import (
                    _offline_cycle_input_errors,
                    annotation_identity_errors,
                )

                issues = _offline_cycle_input_errors(
                    snapshot,
                    record,
                    version=v,
                    source_v=source_v,
                )
                issues.extend(annotation_identity_errors(
                    existing_annotation,
                    snapshot,
                    version=v,
                    source_v=source_v,
                ))
                if issues:
                    raise RuntimeError("existing_cycle_annotation_invalid")
                annotation = existing_annotation
            else:
                from cycle_archivist import run_cycle_archivist_analysis

                annotation = await run_cycle_archivist_analysis(
                    v,
                    source_v,
                    snapshot,
                    _tc._get_ui(),
                    handoff_record=record,
                )
                if annotation.get("status") != "annotated":
                    raise RuntimeError(
                        "cycle_archivist_required_analysis_unavailable:"
                        + ";".join(annotation.get("issues") or [])
                    )
            annotation_receipt = write_archive_annotation(
                v, source_v, claim_id, annotation
            )
            record = complete_handoff_step(
                v,
                source_v,
                claim_id,
                "cycle_annotation",
                {**annotation_receipt, "plan_digest": row["plan_digest"]},
            )

        if not done("housekeeping"):
            row = record["steps"]["housekeeping"]
            dependency_receipts = {
                name: record["steps"][name]["receipt"]["receipt_digest"]
                for name in (
                    "archive_rotation", "log_cleanup", "pool_reap",
                    "cycle_annotation",
                )
            }
            if row.get("status") == "pending":
                record = plan_handoff_step(
                    v,
                    source_v,
                    claim_id,
                    "housekeeping",
                    {
                        "schema_version": 1,
                        "kind": "post-publication-worktree-verification-plan",
                        "expected_head_oid": record["identity"]["commit_oid"],
                        "expected_dirty_paths": [],
                        "tracked_housekeeping_commit_allowed": False,
                        "dependency_receipts": dependency_receipts,
                    },
                )
                row = record["steps"]["housekeeping"]
            if row["plan"].get("dependency_receipts") != dependency_receipts:
                raise RuntimeError("post_publication_dependency_receipt_changed")
            rotation_output = record["steps"]["archive_rotation"][
                "receipt"
            ]["output"]
            recorded_rotations = rotation_output.get("rotations")
            if canonical_digest(recorded_rotations) != rotation_output.get(
                "rotation_set_digest"
            ):
                raise RuntimeError("archive_rotation_final_reproof_mismatch")
            from evolution_infra import validate_archive_rotation_receipts

            if validate_archive_rotation_receipts(
                v,
                recorded_rotations,
                rotation_plan=record["steps"]["archive_rotation"]["plan"],
            ) != recorded_rotations:
                raise RuntimeError("archive_rotation_final_receipt_mismatch")
            log_row = record["steps"]["log_cleanup"]
            _tc._revalidate_strict_log_archives(
                log_row["plan"],
                log_row["receipt"]["output"].get("archives"),
                expected_handoff_version=v,
                expected_publication_id=record["identity"]["publication_id"],
            )
            pool_output = record["steps"]["pool_reap"]["receipt"]["output"]
            _initial_pool, target_names, _selection_snapshot = (
                _tc._validate_pool_reap_plan(
                    record["steps"]["pool_reap"]["plan"],
                    record,
                )
            )
            if (
                pool_output.get("required_reaps") != len(target_names)
                or pool_output.get("removed_bots") != sorted(target_names)
            ):
                raise RuntimeError("pool_reap_final_target_set_mismatch")
            prior_reap_proofs = {
                proof.get("bot"): proof
                for proof in pool_output.get("reap_proofs") or []
                if isinstance(proof, dict)
            }
            final_reap_proofs = []
            for name in target_names:
                proof = _converge_and_verify_reaped_target(name, record)
                prior = prior_reap_proofs.get(name) or {}
                for field in (
                    "version", "completion_commit_oid", "tombstone_tag",
                    "tombstone_object_oid", "tombstone_commit_oid",
                ):
                    if proof.get(field) != prior.get(field):
                        raise RuntimeError("pool_reap_final_reproof_mismatch")
                final_reap_proofs.append(proof)
            housekeeping = _tc._verify_post_publication_worktree(
                expected_head=row["plan"]["expected_head_oid"],
                expected_dirty=set(row["plan"]["expected_dirty_paths"]),
            )
            housekeeping.update({
                "archive_rotation_revalidated": True,
                "strict_log_archives_revalidated": True,
                "reap_proofs": final_reap_proofs,
                "reap_proof_set_digest": canonical_digest(final_reap_proofs),
            })
            record = complete_handoff_step(
                v,
                source_v,
                claim_id,
                "housekeeping",
                {**housekeeping, "plan_digest": row["plan_digest"]},
            )

        completed = complete_post_publication_handoff(v, source_v, claim_id)
        claim_id = ""
        _tc._set_pipeline_status(f"Archived v{v}", is_working=False)
        result = {
            "version": v,
            "source_v": source_v,
            "archivist_completed": True,
            "handoff_identity_digest": completed["identity_digest"],
            "publication_id": completed["identity"]["publication_id"],
            "steps": completed["steps"],
            "next_tool": "prepare_generation",
        }
        # Completion telemetry is deliberately downstream of the durable
        # archive/record linearization.  It has no marker file, is not a
        # required handoff step, and failure cannot reopen the generation.
        try:
            _tc.log_system_event(
                "pipeline.archivist_done",
                "success",
                f"Archivist completed required effects for v{v}",
                {
                    "version": v,
                    "source_v": source_v,
                    "publication_id": completed["identity"]["publication_id"],
                    "handoff_identity_digest": completed["identity_digest"],
                },
            )
        except Exception:
            pass
        return result
    except _tc.LLMAvailabilityBlocked:
        if claim_id:
            release_post_publication_handoff_claim(
                v, source_v, claim_id, error="llm_availability_blocked"
            )
        raise
    except Exception as exc:
        if claim_id:
            release_post_publication_handoff_claim(
                v,
                source_v,
                claim_id,
                error=f"{type(exc).__name__}: {str(exc)[:500]}",
            )
        return {
            "error": "POST_PUBLICATION_ARCHIVIST_PENDING",
            "version": v,
            "source_v": source_v,
            "archivist_completed": False,
            "checkpoint_cleared_by_archivist": False,
            "detail": f"{type(exc).__name__}: {str(exc)[:500]}",
            "directive": (
                "Repair the required effect and retry run_archivist for the same "
                "durable handoff; do not prepare another generation."
            ),
        }
