"""Content identity helpers for published national bots."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from bot_namespace import bot_tag, parse_bot_version


ROOT = Path(__file__).resolve().parents[2]


def hash_path(path_or_token: str | Path) -> str:
    """Hash a bot artifact while excluding runtime-only files."""
    path = Path(path_or_token).expanduser().resolve()
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.name.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        return digest.hexdigest()
    if not path.exists():
        digest.update(f"missing:{path}".encode("utf-8", "surrogateescape"))
        return digest.hexdigest()

    files: list[tuple[str, Path]] = []
    for item in path.rglob("*"):
        if not item.is_file() or item.is_symlink():
            continue
        relative = item.relative_to(path)
        if "__pycache__" in relative.parts:
            continue
        if item.name == ".completed" or item.suffix in {".pyc", ".pyo"}:
            continue
        if item.name.startswith("."):
            continue
        files.append((str(relative).replace(os.sep, "/"), item))
    for relative, item in sorted(files):
        digest.update(relative.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def canonical_digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _tag_metadata(tag: str) -> dict[str, str]:
    result = _git("for-each-ref", "--format=%(contents)", f"refs/tags/{tag}")
    metadata: dict[str, str] = {}
    if result.returncode != 0:
        return metadata
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(":")
        normalized = key.strip().lower().replace("_", "-")
        if separator and normalized.startswith("official-"):
            metadata[normalized] = value.strip()
    return metadata


def published_bot_identity(path_or_token: str | Path) -> dict[str, Any]:
    """Resolve the immutable Git publication identity for a national bot."""
    path = Path(path_or_token).expanduser().resolve()
    version = parse_bot_version(path.name)
    issues: list[str] = []
    try:
        relative = path.relative_to(ROOT)
    except ValueError:
        relative = None
        issues.append("bot_path_outside_project")

    tag = bot_tag(version) if version is not None else ""
    tag_type = ""
    tag_object = ""
    commit_oid = ""
    completion_tree_oid = ""
    current_tree_oid = ""
    tracked_tree_matches = False
    untracked_files: list[str] = []
    if version is None:
        issues.append("invalid_national_bot_label")
    elif relative is not None:
        ref = f"refs/tags/{tag}"
        type_result = _git("cat-file", "-t", ref)
        tag_type = type_result.stdout.strip() if type_result.returncode == 0 else ""
        if tag_type != "tag":
            issues.append("missing_annotated_completion_tag")
        object_result = _git("rev-parse", ref)
        tag_object = object_result.stdout.strip() if object_result.returncode == 0 else ""
        commit_result = _git("rev-list", "-n", "1", tag)
        commit_oid = commit_result.stdout.strip() if commit_result.returncode == 0 else ""
        tree_result = _git("rev-parse", f"{tag}^{{commit}}:{relative.as_posix()}")
        completion_tree_oid = tree_result.stdout.strip() if tree_result.returncode == 0 else ""
        current_tree_result = _git("rev-parse", f"HEAD:{relative.as_posix()}")
        current_tree_oid = (
            current_tree_result.stdout.strip()
            if current_tree_result.returncode == 0
            else ""
        )
        if not commit_oid or not completion_tree_oid:
            issues.append("completion_tag_missing_bot_tree")
        if not current_tree_oid:
            issues.append("current_head_missing_bot_tree")
        diff_result = _git("diff", "--quiet", "HEAD", "--", relative.as_posix())
        tracked_tree_matches = diff_result.returncode == 0
        if not tracked_tree_matches:
            issues.append("working_bot_differs_from_current_head")
        untracked_result = _git(
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            relative.as_posix(),
        )
        if untracked_result.returncode == 0:
            untracked_files = [line for line in untracked_result.stdout.splitlines() if line]
        if untracked_files:
            issues.append("working_bot_has_untracked_files")

    artifact_hash = hash_path(path)
    published = bool(
        path.is_dir()
        and tag_type == "tag"
        and commit_oid
        and completion_tree_oid
        and current_tree_oid
        and tracked_tree_matches
        and not untracked_files
    )
    return {
        "label": path.name,
        "version": version,
        "path": str(path),
        "artifact_hash": artifact_hash,
        "tag": tag,
        "tag_type": tag_type,
        "tag_object": tag_object,
        "commit_oid": commit_oid,
        "completion_tree_oid": completion_tree_oid,
        "current_tree_oid": current_tree_oid,
        "migrated_since_completion": bool(
            completion_tree_oid
            and current_tree_oid
            and completion_tree_oid != current_tree_oid
        ),
        "tag_metadata": _tag_metadata(tag) if tag else {},
        "published": published,
        "issues": list(dict.fromkeys(issues)),
    }
