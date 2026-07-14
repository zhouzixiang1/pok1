"""Authority for executing and publishing strict national TCP policy bots.

The active epoch has one system-owned TCP runtime and a typed policy namespace
starting at ``national_v143``.  This module intentionally knows nothing about
archived bot versions, migration seeds, or Botzone launchers.  All checks are
static and fail closed; candidate code is never imported while deciding
whether it may cross an execution boundary.
"""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path
from typing import Any, Callable

from bot_namespace import (
    FIRST_STRICT_POLICY_VERSION,
    NATIONAL_ENTRYPOINT,
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


class NationalRuntimeAuthorityError(RuntimeError):
    """Raised when current runtime or publication authority is unavailable."""


def current_system_native_runtime_identity() -> dict[str, Any]:
    """Return the byte identity of the sole executable TCP wire runtime."""

    from national_native import NATIVE_BOT_TEMPLATE

    content = NATIVE_BOT_TEMPLATE.encode("utf-8")
    return {
        "schema_version": 1,
        "kind": "system-owned-national-tcp-runtime",
        "entry": NATIONAL_ENTRYPOINT,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def current_system_native_runtime_errors(bot_dir: str | Path) -> list[str]:
    """Validate the system entrypoint without executing candidate code."""

    root = Path(bot_dir)
    from managed_bot_socket import stdlib_shadow_errors

    shadow_errors = stdlib_shadow_errors(root)
    if shadow_errors:
        return list(dict.fromkeys(shadow_errors))
    entry = root / NATIONAL_ENTRYPOINT
    try:
        metadata = entry.lstat()
    except OSError as exc:
        return [
            "system_owned_native_runtime_unreadable:"
            f"{type(exc).__name__}:{str(exc)[:160]}"
        ]
    if not stat.S_ISREG(metadata.st_mode) or entry.is_symlink():
        return ["system_owned_native_runtime_entry_not_regular"]
    try:
        content = entry.read_bytes()
    except OSError as exc:
        return [
            "system_owned_native_runtime_unreadable:"
            f"{type(exc).__name__}:{str(exc)[:160]}"
        ]
    identity = current_system_native_runtime_identity()
    actual = hashlib.sha256(content).hexdigest()
    if len(content) != int(identity["size"]) or actual != identity["sha256"]:
        return [
            "system_owned_native_runtime_identity_mismatch:"
            f"expected={identity['sha256']}:actual={actual}"
        ]
    return []


def strict_published_bot_names(
    *,
    bots_dir: str | Path | None = None,
    publication_resolver: Callable[[Path], dict[str, Any]] | None = None,
    certificate_resolver: Callable[[Path], dict[str, Any]] | None = None,
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
    "strict_published_bot_names",
]
