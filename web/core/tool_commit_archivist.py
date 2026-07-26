"""Archivist internal helpers extracted from tool_commit.

This companion module hosts the post-publication archivist helpers that were
originally inline in ``web/core/tool_commit.py``.  They are split out purely
to keep the main entry module small; the main module re-exports every public
symbol from here at the very bottom of the file for backward compatibility,
covering tests and external importers that still reach them as
``tool_commit.<name>``.

IMPORTANT -- shared-symbol access model:

Some names are read off the *live* ``tool_commit`` module attribute instead of
being imported at the top of this file.  This is required for correctness in
two scenarios:

1. Module constants that tests monkeypatch on ``tool_commit`` (``RESULTS_DIR``,
   ``ARCHIVE_DIR``, ``MAX_ACTIVE_BOTS``, ``PROJECT_ROOT``) and other names that
   the test-suite mutates on ``tool_commit`` (``_safe_log_tree_manifest`` and
   ``_converge_and_verify_reaped_target`` which deliberately remain in the
   main module so the monkeypatch surface stays intact).  Binding them at
   import time would freeze the pre-patch value and silently break the audit.

2. Utility helpers (``_git``, ``_git_ensure_main_branch``, ``get_active_bots``,
   ``parse_bot_version``, ``bot_tag``, ``bot_name``, ``get_bot_dir``,
   ``git_push_refs``) that are likewise defined in ``tool_commit`` and that
   tests may patch on the main module.

All such references in this file are written ``_tc.<name>`` so they resolve
against the live module attribute at call time.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from pathlib import Path

import tool_commit as _tc

_ARCHIVIST_STORAGE_OWNER_LOCK = "web/core/national_arena/storage_owner.lock"



_ARCHIVIST_STORAGE_OWNER_LOCK_PORCELAIN = (
    f"?? {_ARCHIVIST_STORAGE_OWNER_LOCK}"
)



STRICT_LOG_KEEP_GENERATIONS = 5



_POOL_REAP_PLAN_KEYS = {
    "schema_version",
    "kind",
    "publication_id",
    "selection_policy",
    "selection_snapshot",
    "selection_snapshot_digest",
    "active_bots",
    "active_pool_digest",
    "max_active_bots",
    "required_reaps",
    "targets",
    "target_sequence_digest",
    "expected_head_oid",
    "expected_remote_main_oid",
}



def _validated_archivist_storage_owner_lock() -> bool:
    """Recognize the one system-owned untracked file Archivist may ignore.

    This is deliberately an identity check, not a path allowlist.  The raw
    porcelain entry must be untracked (checked by the caller), and the path
    below the repository root must remain the same empty, private, regular
    inode across ``lstat`` and a no-follow open.  Any ambiguity is dirty.
    """

    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        return False
    path = Path(_tc.PROJECT_ROOT) / _ARCHIVIST_STORAGE_OWNER_LOCK
    fd = -1
    try:
        before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size != 0
            or before.st_nlink != 1
        ):
            return False
        flags = os.O_RDONLY | nofollow
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_mode != before.st_mode
            or opened.st_uid != before.st_uid
            or opened.st_gid != before.st_gid
            or opened.st_size != before.st_size
            or opened.st_nlink != before.st_nlink
        ):
            return False
        return (
            stat.S_ISREG(opened.st_mode)
            and opened.st_uid == os.getuid()
            and stat.S_IMODE(opened.st_mode) == 0o600
            and opened.st_size == 0
            and opened.st_nlink == 1
        )
    except (OSError, ValueError):
        return False
    finally:
        if fd >= 0:
            os.close(fd)



def _git_dirty_paths() -> set[str]:
    """Return Archivist-relevant porcelain paths without mutating Git state."""
    out = _tc._git("status", "--porcelain", check=False)
    paths: set[str] = set()
    for line in out.splitlines():
        if not line:
            continue
        if (
            line == _ARCHIVIST_STORAGE_OWNER_LOCK_PORCELAIN
            and _validated_archivist_storage_owner_lock()
        ):
            continue
        # Porcelain v1: XY<space>path, rename: XY old -> new.
        path = line[3:] if len(line) > 3 else line
        if " -> " in path:
            old, new = path.split(" -> ", 1)
            paths.add(old.strip())
            paths.add(new.strip())
        else:
            paths.add(path.strip())
    return paths



def _verify_post_publication_worktree(
    *,
    expected_head: str,
    expected_dirty: set[str],
) -> dict:
    """Prove post-publication effects did not create a second Git mutation.

    Reaping publishes an annotated tombstone tag and removes an ignored
    ``.completed`` capability.  Log/result archiving is ignored runtime state.
    Consequently there is no authorized tracked housekeeping commit.  Any HEAD
    or porcelain change is a hard failure and remains for operator inspection.
    """

    _tc._git_ensure_main_branch()
    actual_head = _tc._git("rev-parse", "HEAD").strip()
    actual_dirty = _git_dirty_paths()
    if actual_head != expected_head:
        raise RuntimeError("post_publication_head_changed")
    if actual_dirty != set(expected_dirty):
        raise RuntimeError("post_publication_worktree_changed")
    return {
        "head_oid": actual_head,
        "worktree_status_digest": hashlib.sha256(
            "\n".join(sorted(actual_dirty)).encode("utf-8")
        ).hexdigest(),
        "tracked_housekeeping_commit": False,
    }



def _durable_archivist_state_write(path: Path, payload: dict) -> str:
    """Atomically publish one required Archivist side effect."""

    from evolution_infra import (
        _atomic_publish_state_text,
        _locked_state_sidecar,
    )
    import fcntl

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    with _locked_state_sidecar(path, lock_type=fcntl.LOCK_EX):
        _atomic_publish_state_text(path, encoded + "\n")
    return hashlib.sha256((encoded + "\n").encode("utf-8")).hexdigest()



def _build_strict_log_cleanup_plan(
    handoff_version: int,
    *,
    keep_generations: int = STRICT_LOG_KEEP_GENERATIONS,
) -> dict:
    """Plan only exact v143+ log roots owned by the current handoff."""

    if type(handoff_version) is not int or (
        handoff_version < _tc.FIRST_STRICT_POLICY_VERSION
    ):
        raise RuntimeError("strict_log_cleanup_handoff_version_invalid")
    if (
        type(keep_generations) is not int
        or keep_generations != STRICT_LOG_KEEP_GENERATIONS
    ):
        raise RuntimeError("strict_log_cleanup_retention_invalid")
    cutoff = handoff_version - keep_generations
    archives: list[dict] = []
    if cutoff >= _tc.FIRST_STRICT_POLICY_VERSION:
        for version in range(_tc.FIRST_STRICT_POLICY_VERSION, cutoff + 1):
            source = _tc.RESULTS_DIR / f"v{version}" / "logs"
            # Exact paths only: never glob or probe v1..v142 legacy history.
            if not os.path.lexists(source):
                continue
            tree = _tc._safe_log_tree_manifest(source, version=version)
            suffix = tree["tree_digest"][:20]
            archives.append({
                **tree,
                "archive_relative_path": f"v{version}_logs_{suffix}.tar.gz",
                "manifest_relative_path": (
                    f"v{version}_logs_{suffix}.manifest.json"
                ),
                # Schema-1 plans already bind this historical path.  Retain it
                # as inert identity data so persisted plans remain resumable;
                # the non-destructive executor must never probe or mutate it.
                "quarantine_relative_path": (
                    f"v{version}/.logs-archived-{tree['tree_digest']}"
                ),
            })
    return {
        "schema_version": 1,
        "kind": "strict-log-cleanup-plan",
        "handoff_version": handoff_version,
        "first_strict_version": _tc.FIRST_STRICT_POLICY_VERSION,
        "keep_generations": int(keep_generations),
        "cutoff_version": cutoff,
        "archives": archives,
    }



def _validate_strict_log_cleanup_plan(
    plan: dict,
    *,
    expected_handoff_version: int,
    expected_publication_id: str | None = None,
) -> list[dict]:
    """Validate one frozen, non-destructive strict-log archive plan.

    The caller supplies the handoff identity rather than trusting identity
    fields stored in the plan.  A forged future handoff or shortened retention
    window therefore cannot select current/recent log trees.  Schema-1 retains
    its historical quarantine path as inert identity data only; validation
    never makes that path an authorization to rename or delete live bytes.
    """

    from bot_artifact import canonical_digest

    if (
        type(expected_handoff_version) is not int
        or expected_handoff_version < _tc.FIRST_STRICT_POLICY_VERSION
    ):
        raise RuntimeError("strict_log_cleanup_expected_version_invalid")
    base_keys = {
        "schema_version",
        "kind",
        "handoff_version",
        "first_strict_version",
        "keep_generations",
        "cutoff_version",
        "archives",
    }
    if not isinstance(plan, dict) or set(plan) not in (
        base_keys,
        base_keys | {"publication_id"},
    ):
        raise RuntimeError("strict_log_cleanup_plan_invalid")
    handoff_version = plan.get("handoff_version")
    keep_generations = plan.get("keep_generations")
    cutoff = plan.get("cutoff_version")
    archives = plan.get("archives")
    if (
        plan.get("schema_version") != 1
        or plan.get("kind") != "strict-log-cleanup-plan"
        or plan.get("first_strict_version") != _tc.FIRST_STRICT_POLICY_VERSION
        or type(handoff_version) is not int
        or handoff_version != expected_handoff_version
        or type(keep_generations) is not int
        or keep_generations != STRICT_LOG_KEEP_GENERATIONS
        or type(cutoff) is not int
        or cutoff != handoff_version - keep_generations
        or not isinstance(archives, list)
    ):
        raise RuntimeError("strict_log_cleanup_plan_invalid")
    publication_id = plan.get("publication_id")
    if expected_publication_id is not None:
        if (
            not isinstance(expected_publication_id, str)
            or len(expected_publication_id) != 64
            or any(
                char not in "0123456789abcdef"
                for char in expected_publication_id
            )
            or publication_id != expected_publication_id
        ):
            raise RuntimeError("strict_log_cleanup_publication_invalid")
    elif publication_id is not None and (
        not isinstance(publication_id, str)
        or len(publication_id) != 64
        or any(char not in "0123456789abcdef" for char in publication_id)
    ):
        raise RuntimeError("strict_log_cleanup_publication_invalid")

    maximum_subjects = max(0, cutoff - _tc.FIRST_STRICT_POLICY_VERSION + 1)
    if len(archives) > maximum_subjects:
        raise RuntimeError("strict_log_cleanup_subject_invalid")
    previous_version = _tc.FIRST_STRICT_POLICY_VERSION - 1
    item_keys = {
        "schema_version",
        "kind",
        "version",
        "source_relative_path",
        "entries",
        "tree_digest",
        "archive_relative_path",
        "manifest_relative_path",
        "quarantine_relative_path",
    }
    for item in archives:
        if not isinstance(item, dict) or set(item) != item_keys:
            raise RuntimeError("strict_log_cleanup_subject_invalid")
        version = item.get("version")
        if (
            type(version) is not int
            or version < _tc.FIRST_STRICT_POLICY_VERSION
            or version > cutoff
            or version <= previous_version
        ):
            raise RuntimeError("strict_log_cleanup_subject_invalid")
        previous_version = version
        expected_source = f"v{version}/logs"
        entries = item.get("entries")
        if (
            item.get("schema_version") != 1
            or item.get("kind") != "strict-generation-log-tree"
            or item.get("source_relative_path") != expected_source
            or not isinstance(entries, list)
            or len(entries) > 4096
        ):
            raise RuntimeError("strict_log_cleanup_tree_invalid")
        seen_paths: set[str] = set()
        directory_paths: set[str] = set()
        previous_sort_key: tuple[str, str] | None = None
        for entry in entries:
            if not isinstance(entry, dict):
                raise RuntimeError("strict_log_cleanup_tree_invalid")
            relative = entry.get("path")
            kind = entry.get("kind")
            expected_keys = (
                {"path", "kind"}
                if kind == "directory"
                else {"path", "kind", "size", "sha256"}
            )
            if (
                set(entry) != expected_keys
                or kind not in {"directory", "file"}
                or not isinstance(relative, str)
                or not relative
                or relative in seen_paths
            ):
                raise RuntimeError("strict_log_cleanup_tree_invalid")
            try:
                encoded_relative = relative.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise RuntimeError("strict_log_cleanup_tree_invalid") from exc
            components = relative.split("/")
            if (
                len(encoded_relative) > 1024
                or any(
                    not component
                    or component in {".", ".."}
                    or "\\" in component
                    or any(ord(char) < 32 for char in component)
                    for component in components
                )
            ):
                raise RuntimeError("strict_log_cleanup_tree_invalid")
            sort_key = (relative, kind)
            if previous_sort_key is not None and sort_key <= previous_sort_key:
                raise RuntimeError("strict_log_cleanup_tree_invalid")
            previous_sort_key = sort_key
            for index in range(1, len(components)):
                parent = "/".join(components[:index])
                if parent not in directory_paths:
                    raise RuntimeError("strict_log_cleanup_tree_invalid")
            if kind == "directory":
                directory_paths.add(relative)
            else:
                size = entry.get("size")
                digest = entry.get("sha256")
                if (
                    type(size) is not int
                    or size < 0
                    or not isinstance(digest, str)
                    or len(digest) != 64
                    or any(char not in "0123456789abcdef" for char in digest)
                ):
                    raise RuntimeError("strict_log_cleanup_tree_invalid")
            seen_paths.add(relative)
        tree_payload = {
            "schema_version": 1,
            "kind": "strict-generation-log-tree",
            "version": version,
            "source_relative_path": expected_source,
            "entries": entries,
        }
        tree_digest = canonical_digest(tree_payload)
        suffix = tree_digest[:20]
        if (
            item.get("tree_digest") != tree_digest
            or item.get("archive_relative_path")
            != f"v{version}_logs_{suffix}.tar.gz"
            or item.get("manifest_relative_path")
            != f"v{version}_logs_{suffix}.manifest.json"
            or item.get("quarantine_relative_path")
            != f"v{version}/.logs-archived-{tree_digest}"
        ):
            raise RuntimeError("strict_log_cleanup_tree_invalid")

    # Live sources are deliberately retained, so both execution and final
    # reproof can reconstruct the entire cutoff projection.  A syntactically
    # valid subset (including a forged empty list) must not suppress required
    # immutable evidence, while a changed tree must not be laundered through
    # the previously frozen digest.
    expected_plan = _build_strict_log_cleanup_plan(
        expected_handoff_version,
        keep_generations=STRICT_LOG_KEEP_GENERATIONS,
    )
    expected_archives = expected_plan["archives"]
    if (
        [item["version"] for item in archives]
        != [item["version"] for item in expected_archives]
    ):
        raise RuntimeError("strict_log_cleanup_plan_incomplete")
    if archives != expected_archives:
        raise RuntimeError("strict_log_cleanup_preimage_changed")
    return archives



def _read_safe_json(path: Path) -> dict | None:
    from evolution_infra import _read_regular_state_text

    raw = _read_regular_state_text(path, allow_missing=True)
    if not raw.strip():
        return None
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError(f"state_not_object:{path.name}")
    return payload



def _publish_log_tar(source: Path, target: Path, plan: dict) -> None:
    """Create a normalized tar for one already-digest-bound tree."""

    import tarfile

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    expected = {row["path"]: row for row in plan["entries"]}
    try:
        with tarfile.open(temporary, "x:gz", format=tarfile.PAX_FORMAT) as writer:
            root_info = tarfile.TarInfo(f"v{plan['version']}/logs")
            root_info.type = tarfile.DIRTYPE
            root_info.mode = 0o700
            root_info.mtime = 0
            writer.addfile(root_info)
            for relative in sorted(expected):
                row = expected[relative]
                archive_name = f"v{plan['version']}/logs/{relative}"
                info = tarfile.TarInfo(archive_name)
                info.mode = 0o600
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                if row["kind"] == "directory":
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o700
                    writer.addfile(info)
                    continue
                child = source.joinpath(*relative.split("/"))
                flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(child, flags)
                try:
                    metadata = os.fstat(descriptor)
                    if (
                        metadata.st_nlink != 1
                        or metadata.st_size != row["size"]
                    ):
                        raise RuntimeError("log_tree_changed_while_archiving")
                    info.size = row["size"]
                    with os.fdopen(descriptor, "rb", closefd=False) as handle:
                        writer.addfile(info, handle)
                    live = os.lstat(child)
                finally:
                    os.close(descriptor)
                if (
                    live.st_nlink != 1
                    or (metadata.st_dev, metadata.st_ino)
                    != (live.st_dev, live.st_ino)
                    or metadata.st_size != live.st_size
                    or metadata.st_mtime_ns != live.st_mtime_ns
                    or metadata.st_ctime_ns != live.st_ctime_ns
                ):
                    raise RuntimeError("log_tree_changed_while_archiving")
        descriptor = os.open(
            temporary,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            pass
        from evolution_infra import _fsync_directory

        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)



def _validate_log_tar(
    path: Path,
    plan: dict,
    *,
    expected_nlink: int = 1,
) -> str:
    import tarfile

    metadata = os.lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != expected_nlink
    ):
        raise RuntimeError("log_archive_path_unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    archive_hash = hashlib.sha256()
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as raw_handle:
            while True:
                chunk = raw_handle.read(1024 * 1024)
                if not chunk:
                    break
                archive_hash.update(chunk)
            raw_handle.seek(0)
            with tarfile.open(fileobj=raw_handle, mode="r:gz") as reader:
                members = reader.getmembers()
                root = f"v{plan['version']}/logs"
                expected = {root: {"kind": "directory"}}
                expected.update({
                    f"{root}/{row['path']}": row for row in plan["entries"]
                })
                if len(members) != len(expected):
                    raise RuntimeError("log_archive_member_count_mismatch")
                seen = set()
                for member in members:
                    if member.name in seen or member.name not in expected:
                        raise RuntimeError("log_archive_member_name_mismatch")
                    seen.add(member.name)
                    row = expected[member.name]
                    if row["kind"] == "directory":
                        if not member.isdir():
                            raise RuntimeError("log_archive_member_type_mismatch")
                        continue
                    if not member.isfile() or member.islnk() or member.issym():
                        raise RuntimeError("log_archive_member_type_mismatch")
                    if member.size != row["size"]:
                        raise RuntimeError("log_archive_member_size_mismatch")
                    extracted = reader.extractfile(member)
                    if extracted is None:
                        raise RuntimeError("log_archive_member_unreadable")
                    hasher = hashlib.sha256()
                    size = 0
                    while True:
                        chunk = extracted.read(1024 * 1024)
                        if not chunk:
                            break
                        hasher.update(chunk)
                        size += len(chunk)
                    if size != row["size"] or hasher.hexdigest() != row["sha256"]:
                        raise RuntimeError("log_archive_member_digest_mismatch")
        opened = os.fstat(descriptor)
        live = os.lstat(path)
    finally:
        os.close(descriptor)
    if (
        opened.st_nlink != expected_nlink
        or live.st_nlink != expected_nlink
        or (opened.st_dev, opened.st_ino) != (live.st_dev, live.st_ino)
        or opened.st_size != live.st_size
        or opened.st_mtime_ns != live.st_mtime_ns
        or opened.st_ctime_ns != live.st_ctime_ns
    ):
        raise RuntimeError("log_archive_changed_while_reading")
    return archive_hash.hexdigest()



def _recover_linked_log_tar(path: Path, plan: dict) -> None:
    """Converge a crash after create-only link and before temp unlink."""

    metadata = os.lstat(path)
    if metadata.st_nlink == 1:
        return
    if metadata.st_nlink != 2:
        raise RuntimeError("log_archive_link_count_unsafe")
    prefix = f".{path.name}."
    candidates = []
    with os.scandir(path.parent) as scanner:
        for entry in scanner:
            name = entry.name
            if not name.startswith(prefix) or not name.endswith(".tmp"):
                continue
            token = name[len(prefix):-4]
            if len(token) != 32 or any(
                char not in "0123456789abcdef" for char in token
            ):
                continue
            candidate_stat = entry.stat(follow_symlinks=False)
            if (
                stat.S_ISREG(candidate_stat.st_mode)
                and not stat.S_ISLNK(candidate_stat.st_mode)
                and (candidate_stat.st_dev, candidate_stat.st_ino)
                == (metadata.st_dev, metadata.st_ino)
            ):
                candidates.append(Path(entry.path))
    if len(candidates) != 1:
        raise RuntimeError("log_archive_orphan_link_unrecoverable")
    # Validate the complete archive before removing the only evidence that the
    # second link is our exact create-only temporary, not an injected hardlink.
    _validate_log_tar(path, plan, expected_nlink=2)
    candidates[0].unlink()
    from evolution_infra import _fsync_directory

    _fsync_directory(path.parent)
    final = os.lstat(path)
    if (
        not stat.S_ISREG(final.st_mode)
        or stat.S_ISLNK(final.st_mode)
        or final.st_nlink != 1
    ):
        raise RuntimeError("log_archive_orphan_link_not_recovered")



def _execute_strict_log_cleanup(
    plan: dict,
    *,
    expected_handoff_version: int,
    expected_publication_id: str | None = None,
) -> list[dict]:
    """Publish immutable log archives while preserving every live source.

    The step name is retained for journal compatibility, but this executor is
    deliberately not a cleanup primitive.  It never probes, renames, unlinks,
    or removes the plan's quarantine path or any live generation log path.
    Crash recovery only converges create-only archive and manifest effects.
    """

    from bot_artifact import canonical_digest

    archives = _validate_strict_log_cleanup_plan(
        plan,
        expected_handoff_version=expected_handoff_version,
        expected_publication_id=expected_publication_id,
    )
    archive_root = os.lstat(_tc.ARCHIVE_DIR)
    if (
        not stat.S_ISDIR(archive_root.st_mode)
        or stat.S_ISLNK(archive_root.st_mode)
    ):
        raise RuntimeError("strict_log_archive_root_unsafe")
    receipts = []
    for item in archives:
        version = item.get("version")
        if type(version) is not int or version < _tc.FIRST_STRICT_POLICY_VERSION:
            raise RuntimeError("strict_log_cleanup_subject_invalid")
        expected_source = f"v{version}/logs"
        if item.get("source_relative_path") != expected_source:
            raise RuntimeError("strict_log_cleanup_source_invalid")
        source = _tc.RESULTS_DIR / f"v{version}" / "logs"
        archive = _tc.ARCHIVE_DIR / str(item.get("archive_relative_path"))
        manifest_path = _tc.ARCHIVE_DIR / str(item.get("manifest_relative_path"))
        if (
            source.parent != _tc.RESULTS_DIR / f"v{version}"
            or archive.parent != _tc.ARCHIVE_DIR
            or manifest_path.parent != _tc.ARCHIVE_DIR
        ):
            raise RuntimeError("strict_log_cleanup_path_escape")

        from evolution_infra import _locked_state_sidecar
        import fcntl

        with _locked_state_sidecar(archive, lock_type=fcntl.LOCK_EX):
            if not archive.exists():
                if not source.exists():
                    raise RuntimeError(
                        "strict_log_cleanup_source_missing_before_archive"
                    )
                if _tc._safe_log_tree_manifest(source, version=version) != {
                    key: item[key]
                    for key in (
                        "schema_version", "kind", "version",
                        "source_relative_path", "entries", "tree_digest",
                    )
                }:
                    raise RuntimeError("strict_log_cleanup_preimage_changed")
                _publish_log_tar(source, archive, item)
            _recover_linked_log_tar(archive, item)
            archive_sha = _validate_log_tar(archive, item)
        manifest_payload = {
            "schema_version": 1,
            "kind": "strict-generation-log-archive",
            "version": version,
            "source_relative_path": expected_source,
            "tree_digest": item["tree_digest"],
            "entries": item["entries"],
            "archive_relative_path": item["archive_relative_path"],
            "archive_sha256": archive_sha,
        }
        expected_manifest = {
            **manifest_payload,
            "manifest_digest": canonical_digest(manifest_payload),
        }
        from evolution_infra import _atomic_publish_state_text

        with _locked_state_sidecar(manifest_path, lock_type=fcntl.LOCK_EX):
            existing_manifest = _read_safe_json(manifest_path)
            if existing_manifest is None:
                encoded_manifest = json.dumps(
                    expected_manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                _atomic_publish_state_text(
                    manifest_path,
                    encoded_manifest + "\n",
                )
                existing_manifest = _read_safe_json(manifest_path)
        if existing_manifest != expected_manifest:
            raise RuntimeError("strict_log_archive_manifest_mismatch")

        # Re-open the live tree after both immutable effects are durable.  This
        # proves the receipt did not merely archive the right bytes before a
        # destructive move.  No quarantine path is even resolved here.
        current = _tc._safe_log_tree_manifest(source, version=version)
        expected_tree = {
            key: item[key]
            for key in (
                "schema_version", "kind", "version",
                "source_relative_path", "entries", "tree_digest",
            )
        }
        if current != expected_tree:
            raise RuntimeError("strict_log_cleanup_preimage_changed")
        receipts.append({
            "version": version,
            "tree_digest": item["tree_digest"],
            "archive_relative_path": item["archive_relative_path"],
            "archive_sha256": archive_sha,
            "manifest_relative_path": item["manifest_relative_path"],
            "manifest_digest": expected_manifest["manifest_digest"],
            "effect_mode": "nondestructive-immutable-archive",
            "live_source_relative_path": expected_source,
            "live_log_tree_preserved": True,
            "quarantine_log_tree_touched": False,
            "generation_siblings_preserved": True,
        })
    return receipts



def _revalidate_strict_log_archives(
    plan: dict,
    receipts: list[dict],
    *,
    expected_handoff_version: int,
    expected_publication_id: str | None = None,
) -> None:
    """Reprove immutable evidence and the still-live frozen source tree."""

    from bot_artifact import canonical_digest
    from evolution_infra import _locked_state_sidecar
    import fcntl

    items = _validate_strict_log_cleanup_plan(
        plan,
        expected_handoff_version=expected_handoff_version,
        expected_publication_id=expected_publication_id,
    )
    if not isinstance(items, list) or not isinstance(receipts, list):
        raise RuntimeError("strict_log_final_receipt_invalid")
    if len(items) != len(receipts):
        raise RuntimeError("strict_log_final_receipt_count_mismatch")
    for item, receipt in zip(items, receipts):
        if not isinstance(receipt, dict) or set(receipt) != {
            "version", "tree_digest", "archive_relative_path",
            "archive_sha256", "manifest_relative_path", "manifest_digest",
            "effect_mode", "live_source_relative_path",
            "live_log_tree_preserved", "quarantine_log_tree_touched",
            "generation_siblings_preserved",
        }:
            raise RuntimeError("strict_log_final_receipt_invalid")
        archive = _tc.ARCHIVE_DIR / str(item.get("archive_relative_path"))
        manifest_path = _tc.ARCHIVE_DIR / str(item.get("manifest_relative_path"))
        with _locked_state_sidecar(archive, lock_type=fcntl.LOCK_EX):
            _recover_linked_log_tar(archive, item)
            archive_sha = _validate_log_tar(archive, item)
        manifest = _read_safe_json(manifest_path)
        if not isinstance(manifest, dict):
            raise RuntimeError("strict_log_final_manifest_missing")
        manifest_payload = {
            "schema_version": 1,
            "kind": "strict-generation-log-archive",
            "version": item["version"],
            "source_relative_path": item["source_relative_path"],
            "tree_digest": item["tree_digest"],
            "entries": item["entries"],
            "archive_relative_path": item["archive_relative_path"],
            "archive_sha256": archive_sha,
        }
        expected_manifest = {
            **manifest_payload,
            "manifest_digest": canonical_digest(manifest_payload),
        }
        if manifest != expected_manifest:
            raise RuntimeError("strict_log_final_manifest_mismatch")
        source = _tc.RESULTS_DIR / f"v{item['version']}" / "logs"
        expected_tree = {
            key: item[key]
            for key in (
                "schema_version", "kind", "version",
                "source_relative_path", "entries", "tree_digest",
            )
        }
        if _tc._safe_log_tree_manifest(
            source, version=item["version"]
        ) != expected_tree:
            raise RuntimeError("strict_log_final_source_mismatch")
        expected_receipt = {
            "version": item["version"],
            "tree_digest": item["tree_digest"],
            "archive_relative_path": item["archive_relative_path"],
            "archive_sha256": archive_sha,
            "manifest_relative_path": item["manifest_relative_path"],
            "manifest_digest": manifest["manifest_digest"],
            "effect_mode": "nondestructive-immutable-archive",
            "live_source_relative_path": item["source_relative_path"],
            "live_log_tree_preserved": True,
            "quarantine_log_tree_touched": False,
            "generation_siblings_preserved": True,
        }
        if receipt != expected_receipt:
            raise RuntimeError("strict_log_final_receipt_mismatch")



def _handoff_publication_result(record: dict) -> dict:
    identity = record["identity"]
    remote = identity["remote_publication"]
    remote_refs = {}
    for name, row in (remote.get("paired_refs") or {}).items():
        remote_refs[f"refs/tags/{name}"] = row["object_oid"]
        remote_refs[f"refs/tags/{name}^{{}}"] = row["peeled_commit_oid"]
    return {
        "committed": True,
        "version": identity["version"],
        "source_v": identity["source_v"],
        "publication_id": identity["publication_id"],
        "commit_oid": identity["commit_oid"],
        "push_ok": remote.get("required") is True,
        "checkpoint_cleared": True,
        "completed_sentinel_written": True,
        "remote_proof": {
            "valid": remote.get("required") is True,
            "remote_main_oid": remote.get("remote_main_oid"),
            "remote_refs": remote_refs,
        },
    }



def _build_pool_reap_plan(record: dict) -> dict:
    """Freeze the complete reap sequence before publishing any tombstone."""

    from bot_artifact import canonical_digest
    from tool_bot_management import (
        REAP_SELECTION_POLICY,
        _capture_reap_selection_snapshot,
        _select_reap_candidate_from_snapshot,
    )

    initial = sorted(_tc.get_active_bots())
    if (
        len(initial) != len(set(initial))
        or any(
            (_tc.parse_bot_version(name) or -1) < _tc.FIRST_STRICT_POLICY_VERSION
            for name in initial
        )
    ):
        raise RuntimeError("pool_reap_active_namespace_invalid")
    selection_snapshot = _capture_reap_selection_snapshot(
        initial,
        max_active_bots=_tc.MAX_ACTIVE_BOTS,
    )
    if selection_snapshot["active_bots"] != initial:
        raise RuntimeError("pool_reap_selection_snapshot_pool_mismatch")
    simulated = list(initial)
    targets = []
    while len(simulated) > _tc.MAX_ACTIVE_BOTS:
        selection = _select_reap_candidate_from_snapshot(
            selection_snapshot,
            simulated,
        )
        candidate = selection.get("candidate")
        if not candidate:
            raise RuntimeError(
                "pool_reap_cannot_plan_required_target:"
                + str(selection.get("reason") or selection)
            )
        targets.append({
            key: value
            for key, value in selection.items()
            if key in {
                "candidate", "selection_key", "conservative_rating",
                "leaderboard_score", "h2h_avg_wr", "rating",
                "active_pool",
            }
        })
        simulated.remove(candidate)
    return {
        "schema_version": 2,
        "kind": "strict-active-pool-reap-plan",
        "publication_id": record["identity"]["publication_id"],
        "selection_policy": REAP_SELECTION_POLICY,
        "selection_snapshot": selection_snapshot,
        "selection_snapshot_digest": selection_snapshot["snapshot_digest"],
        "active_bots": initial,
        "active_pool_digest": canonical_digest(initial),
        "max_active_bots": _tc.MAX_ACTIVE_BOTS,
        "required_reaps": len(targets),
        "targets": targets,
        "target_sequence_digest": canonical_digest(targets),
        "expected_head_oid": record["identity"]["commit_oid"],
        "expected_remote_main_oid": (
            record["identity"]["remote_publication"].get("remote_main_oid")
        ),
    }



def _lower_hex(value, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(char in "0123456789abcdef" for char in value)
    )



def _validate_pool_reap_plan(
    reap_plan: dict,
    record: dict,
) -> tuple[list, list, dict]:
    """Purely prove the complete frozen target sequence before any effect."""

    from bot_artifact import canonical_digest
    from tool_bot_management import (
        REAP_SELECTION_POLICY,
        _select_reap_candidate_from_snapshot,
        _validate_reap_selection_snapshot,
    )

    if not isinstance(reap_plan, dict) or set(reap_plan) != _POOL_REAP_PLAN_KEYS:
        raise RuntimeError("pool_reap_plan_keys_invalid")
    if (
        type(reap_plan.get("schema_version")) is not int
        or reap_plan["schema_version"] != 2
        or reap_plan.get("kind") != "strict-active-pool-reap-plan"
        or reap_plan.get("selection_policy") != REAP_SELECTION_POLICY
        or not _lower_hex(reap_plan.get("publication_id"), 64)
        or not _lower_hex(reap_plan.get("expected_head_oid"), 40)
        or (
            reap_plan.get("expected_remote_main_oid") is not None
            and not _lower_hex(reap_plan.get("expected_remote_main_oid"), 40)
        )
        or type(reap_plan.get("max_active_bots")) is not int
        or reap_plan["max_active_bots"] != _tc.MAX_ACTIVE_BOTS
        or type(reap_plan.get("required_reaps")) is not int
        or not isinstance(reap_plan.get("active_bots"), list)
        or not isinstance(reap_plan.get("targets"), list)
    ):
        raise RuntimeError("pool_reap_plan_contract_invalid")

    identity = record.get("identity") if isinstance(record, dict) else None
    remote = (
        identity.get("remote_publication")
        if isinstance(identity, dict)
        else None
    )
    if (
        not isinstance(identity, dict)
        or not isinstance(remote, dict)
        or identity.get("publication_id") != reap_plan["publication_id"]
        or identity.get("commit_oid") != reap_plan["expected_head_oid"]
        or remote.get("remote_main_oid") != reap_plan["expected_remote_main_oid"]
    ):
        raise RuntimeError("pool_reap_plan_publication_identity_mismatch")

    initial = list(reap_plan["active_bots"])
    if (
        initial != sorted(initial)
        or len(initial) != len(set(initial))
        or reap_plan.get("active_pool_digest") != canonical_digest(initial)
        or any(
            not isinstance(name, str)
            or (_tc.parse_bot_version(name) or -1) < _tc.FIRST_STRICT_POLICY_VERSION
            or name != _tc.bot_name(_tc.parse_bot_version(name))
            for name in initial
        )
    ):
        raise RuntimeError("pool_reap_initial_pool_invalid")

    snapshot = reap_plan.get("selection_snapshot")
    _validate_reap_selection_snapshot(snapshot)
    if (
        snapshot.get("snapshot_digest")
        != reap_plan.get("selection_snapshot_digest")
        or snapshot.get("selection_policy") != reap_plan["selection_policy"]
        or snapshot.get("max_active_bots") != reap_plan["max_active_bots"]
        or snapshot.get("active_bots") != initial
        or snapshot.get("active_pool_digest") != reap_plan["active_pool_digest"]
    ):
        raise RuntimeError("pool_reap_selection_snapshot_identity_mismatch")

    expected_targets = []
    simulated = list(initial)
    while len(simulated) > reap_plan["max_active_bots"]:
        selection = _select_reap_candidate_from_snapshot(snapshot, simulated)
        candidate = selection.get("candidate")
        if not candidate:
            raise RuntimeError(
                "pool_reap_cannot_validate_required_target:"
                + str(selection.get("reason") or selection)
            )
        expected_targets.append(selection)
        simulated.remove(candidate)
    if (
        reap_plan["targets"] != expected_targets
        or reap_plan.get("target_sequence_digest")
        != canonical_digest(expected_targets)
        or reap_plan["required_reaps"] != len(expected_targets)
        or reap_plan["required_reaps"]
        != max(0, len(initial) - reap_plan["max_active_bots"])
    ):
        raise RuntimeError("pool_reap_target_sequence_invalid")
    return initial, [row["candidate"] for row in expected_targets], snapshot



def _remote_ref_snapshot(*refs: str) -> dict[str, str]:
    raw = _tc._git("ls-remote", "origin", *refs)
    result = {}
    for line in raw.splitlines():
        oid, separator, name = line.partition("\t")
        if separator and len(oid) == 40:
            result[name] = oid
    return result



async def _execute_pool_reap_plan(reap_plan: dict, record: dict) -> dict:
    """Execute an immutable multi-reap plan and converge partial tombstones."""

    from bot_artifact import canonical_digest
    initial, target_names, selection_snapshot = _validate_pool_reap_plan(
        reap_plan,
        record,
    )
    current = sorted(_tc.get_active_bots())
    if not set(current).issubset(set(initial)):
        raise RuntimeError("pool_changed_after_reap_plan")
    removed = sorted(set(initial) - set(current))
    if not set(removed).issubset(set(target_names)):
        raise RuntimeError("unplanned_pool_member_removed")
    # Prove every already-absent target before publishing a new tombstone.  A
    # partial crash is resumable, while a forged preimage cannot smuggle a
    # nonexistent pool member past the subset check and then reap a live bot.
    prior_reap_proofs = {
        target: _tc._converge_and_verify_reaped_target(target, record)
        for target in removed
    }
    reap_proofs = []
    for target in target_names:
        if target in set(_tc.get_active_bots()):
            from tool_bot_management import _do_reap_weakest

            effect = await _do_reap_weakest(
                quiet=True,
                expected_culled=target,
                selection_snapshot=selection_snapshot,
            )
            if effect.get("reaped") is not True:
                raise RuntimeError(
                    "required_pool_reap_did_not_complete:"
                    + str(effect.get("reason") or effect)
                )
        reap_proofs.append(
            prior_reap_proofs.get(target)
            or _tc._converge_and_verify_reaped_target(target, record)
        )
    current = sorted(_tc.get_active_bots())
    removed = sorted(set(initial) - set(current))
    if removed != sorted(target_names):
        raise RuntimeError("pool_reap_preimage_drift")
    return {
        "removed_bots": removed,
        "required_reaps": len(target_names),
        "reap_proofs": reap_proofs,
        "reap_proof_set_digest": canonical_digest(reap_proofs),
    }
