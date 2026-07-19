"""Authority for executing and publishing strict national TCP policy bots.

The active epoch has one system-owned TCP runtime and a typed policy namespace
starting at ``national_v143``.  This module intentionally knows nothing about
archived bot versions, migration seeds, or Botzone launchers.  All checks are
static and fail closed; candidate code is never imported while deciding
whether it may cross an execution boundary.
"""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Any, Callable

from bot_namespace import (
    FIRST_STRICT_POLICY_VERSION,
    NATIONAL_ENTRYPOINT,
    PRECOMPUTE_ENTRYPOINT,
    ROLE_PARENT_SOURCE,
    bot_tag,
    parse_bot_version,
    resolve_national_bot_spec,
    version_sort_key,
)


ROOT = Path(__file__).resolve().parents[2]
BOTS_DIR = ROOT / "bots"
PENDING_PUBLICATION_SCHEMA_VERSION = 1
PENDING_PUBLICATION_KIND = "national-tcp-policy-pending-local-publication"
SYSTEM_NATIVE_RUNTIME_IDENTITY_SCHEMA_VERSION = 2
SYSTEM_NATIVE_RUNTIME_IDENTITY_KIND = "system-owned-national-tcp-runtime"
SYSTEM_NATIVE_RUNTIME_FILES = (
    NATIONAL_ENTRYPOINT,
    PRECOMPUTE_ENTRYPOINT,
)


class NationalRuntimeAuthorityError(RuntimeError):
    """Raised when current runtime or publication authority is unavailable."""


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _template_identity(content: bytes) -> dict[str, Any]:
    return {
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def system_native_runtime_identity_structure_issues(identity: Any) -> list[str]:
    """Validate the canonical two-file system-runtime identity shape.

    ``national_bot.py`` owns TCP/worker orchestration and ``precompute.py``
    owns the system evaluator used by the policy.  They are one execution
    subject: a cache or formal admission that binds only the former can reuse
    evidence after a decision-changing precompute edit.  This helper checks
    structure and internal digest consistency; callers that need *current*
    bytes compare the full record returned by
    :func:`current_system_native_runtime_identity`.
    """

    if not isinstance(identity, dict):
        return ["system_native_runtime_identity_missing"]
    expected_keys = {
        "schema_version",
        "kind",
        "entry",
        "sha256",
        "size",
        "artifacts",
        "combined_digest",
    }
    if set(identity) != expected_keys:
        return ["system_native_runtime_identity_fields_mismatch"]
    if identity.get("schema_version") != SYSTEM_NATIVE_RUNTIME_IDENTITY_SCHEMA_VERSION:
        return ["system_native_runtime_identity_schema_mismatch"]
    if identity.get("kind") != SYSTEM_NATIVE_RUNTIME_IDENTITY_KIND:
        return ["system_native_runtime_identity_kind_mismatch"]
    if identity.get("entry") != NATIONAL_ENTRYPOINT:
        return ["system_native_runtime_identity_entry_mismatch"]
    artifacts = identity.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(SYSTEM_NATIVE_RUNTIME_FILES):
        return ["system_native_runtime_identity_artifacts_mismatch"]
    for relative in SYSTEM_NATIVE_RUNTIME_FILES:
        artifact = artifacts.get(relative)
        if not isinstance(artifact, dict) or set(artifact) != {"sha256", "size"}:
            return [f"system_native_runtime_identity_artifact_shape_invalid:{relative}"]
        digest = artifact.get("sha256")
        size = artifact.get("size")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest.lower())
            or type(size) is not int
            or size < 0
        ):
            return [f"system_native_runtime_identity_artifact_invalid:{relative}"]
    entry_identity = artifacts[NATIONAL_ENTRYPOINT]
    if (
        identity.get("sha256") != entry_identity["sha256"]
        or identity.get("size") != entry_identity["size"]
    ):
        return ["system_native_runtime_identity_entry_projection_mismatch"]
    combined_payload = {
        "schema_version": SYSTEM_NATIVE_RUNTIME_IDENTITY_SCHEMA_VERSION,
        "kind": SYSTEM_NATIVE_RUNTIME_IDENTITY_KIND,
        "artifacts": artifacts,
    }
    if identity.get("combined_digest") != _canonical_digest(combined_payload):
        return ["system_native_runtime_identity_combined_digest_mismatch"]
    return []


def current_system_native_runtime_identity() -> dict[str, Any]:
    """Return the byte identity of the full system-owned policy runtime.

    The legacy ``entry``/``sha256``/``size`` projection is retained for the
    socket entrypoint, while ``artifacts`` and ``combined_digest`` bind both
    system-owned ABI files.  A precompute-only edit must therefore invalidate
    every probe, quality, precommit, commit, and formal-admission receipt.
    """

    from national_native import NATIVE_BOT_TEMPLATE, NATIVE_PRECOMPUTE_TEMPLATE

    artifacts = {
        NATIONAL_ENTRYPOINT: _template_identity(NATIVE_BOT_TEMPLATE.encode("utf-8")),
        PRECOMPUTE_ENTRYPOINT: _template_identity(
            NATIVE_PRECOMPUTE_TEMPLATE.encode("utf-8")
        ),
    }
    payload = {
        "schema_version": SYSTEM_NATIVE_RUNTIME_IDENTITY_SCHEMA_VERSION,
        "kind": SYSTEM_NATIVE_RUNTIME_IDENTITY_KIND,
        "artifacts": artifacts,
    }
    return {
        "schema_version": SYSTEM_NATIVE_RUNTIME_IDENTITY_SCHEMA_VERSION,
        "kind": SYSTEM_NATIVE_RUNTIME_IDENTITY_KIND,
        "entry": NATIONAL_ENTRYPOINT,
        "sha256": artifacts[NATIONAL_ENTRYPOINT]["sha256"],
        "size": artifacts[NATIONAL_ENTRYPOINT]["size"],
        "artifacts": artifacts,
        "combined_digest": _canonical_digest(payload),
    }


def current_system_native_runtime_errors(bot_dir: str | Path) -> list[str]:
    """Validate both system-owned runtime files without executing candidate code."""

    root = Path(bot_dir)
    from managed_bot_socket import stdlib_shadow_errors

    shadow_errors = stdlib_shadow_errors(root)
    if shadow_errors:
        return list(dict.fromkeys(shadow_errors))
    identity = current_system_native_runtime_identity()
    identity_issues = system_native_runtime_identity_structure_issues(identity)
    if identity_issues:
        return identity_issues
    artifacts = identity["artifacts"]
    issues: list[str] = []
    for relative in SYSTEM_NATIVE_RUNTIME_FILES:
        path = root / relative
        try:
            metadata = path.lstat()
        except OSError as exc:
            issues.append(
                "system_owned_native_runtime_unreadable:"
                f"{relative}:{type(exc).__name__}:{str(exc)[:160]}"
            )
            continue
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            issues.append(f"system_owned_native_runtime_file_not_regular:{relative}")
            continue
        try:
            content = path.read_bytes()
        except OSError as exc:
            issues.append(
                "system_owned_native_runtime_unreadable:"
                f"{relative}:{type(exc).__name__}:{str(exc)[:160]}"
            )
            continue
        expected = artifacts[relative]
        actual = hashlib.sha256(content).hexdigest()
        if len(content) != expected["size"] or actual != expected["sha256"]:
            issues.append(
                "system_owned_native_runtime_identity_mismatch:"
                f"{relative}:expected={expected['sha256']}:actual={actual}"
            )
    return list(dict.fromkeys(issues))


def strict_published_bot_names(
    *,
    bots_dir: str | Path | None = None,
    publication_resolver: Callable[[Path], dict[str, Any]] | None = None,
    certificate_resolver: Callable[[Path], dict[str, Any]] | None = None,
    ledger_fresh: bool = True,
) -> tuple[str, ...]:
    """Return direct v143+ children satisfying the complete published ABI."""

    root = Path(bots_dir) if bots_dir is not None else BOTS_DIR
    if root.is_symlink() or not root.is_dir():
        return ()
    repo_root = root.parent
    strict: list[str] = []
    for path in root.iterdir():
        version = parse_bot_version(path.name)
        if (
            version is None
            or version < FIRST_STRICT_POLICY_VERSION
            or path.is_symlink()
            or not path.is_dir()
        ):
            continue
        try:
            if current_system_native_runtime_errors(path):
                continue
            spec = resolve_national_bot_spec(
                path,
                ROLE_PARENT_SOURCE,
                repo_root=repo_root,
                publication_resolver=publication_resolver,
                certificate_resolver=certificate_resolver,
                ledger_fresh=ledger_fresh,
            )
        except Exception:
            continue
        if spec.eligible:
            strict.append(path.name)
    return tuple(sorted(strict, key=version_sort_key))


def build_pending_local_publication_proof(bot_dir: str | Path) -> dict[str, Any]:
    """Bind the single local publication that crash recovery may observe."""

    from bot_artifact import canonical_digest, published_bot_identity

    path = Path(bot_dir)
    version = parse_bot_version(path.name)
    if version is None or version < FIRST_STRICT_POLICY_VERSION:
        raise NationalRuntimeAuthorityError(
            "pending publication is outside the strict policy namespace"
        )
    runtime_errors = current_system_native_runtime_errors(path)
    if runtime_errors:
        raise NationalRuntimeAuthorityError(
            "pending publication runtime is invalid:" + ";".join(runtime_errors[:8])
        )
    identity = published_bot_identity(path)
    if identity.get("published") is not True:
        raise NationalRuntimeAuthorityError(
            "pending local publication is not immutable:"
            + ";".join(str(item) for item in (identity.get("issues") or [])[:8])
        )
    if identity.get("version") != version or identity.get("tag") != bot_tag(version):
        raise NationalRuntimeAuthorityError("pending publication identity mismatch")
    payload = {
        "schema_version": PENDING_PUBLICATION_SCHEMA_VERSION,
        "kind": PENDING_PUBLICATION_KIND,
        "bot": path.name,
        "version": version,
        "artifact_hash": str(identity.get("artifact_hash") or ""),
        "tag": str(identity.get("tag") or ""),
        "tag_object": str(identity.get("tag_object") or ""),
        "commit_oid": str(identity.get("commit_oid") or ""),
        "completion_tree_oid": str(identity.get("completion_tree_oid") or ""),
        "main_commit_oid": str(identity.get("main_commit_oid") or ""),
    }
    return {**payload, "proof_digest": canonical_digest(payload)}


__all__ = [
    "NationalRuntimeAuthorityError",
    "build_pending_local_publication_proof",
    "current_system_native_runtime_errors",
    "current_system_native_runtime_identity",
    "system_native_runtime_identity_structure_issues",
    "strict_published_bot_names",
]
