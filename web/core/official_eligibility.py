"""Role-based official eligibility and transitional grandfather policy."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

from bot_artifact import canonical_digest, published_bot_identity
from bot_namespace import parse_tag_version


ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "web" / "core" / "official_grandfathering.json"
ALLOWED_ROLES = {"parent_source", "rating_pool", "official_opponent"}


def load_grandfather_policy(path: str | Path | None = None) -> dict[str, Any]:
    policy_path = Path(path) if path is not None else POLICY_PATH
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    if int(payload.get("schema_version", 0) or 0) != 1:
        raise ValueError("unsupported official grandfather policy schema")
    if not str(payload.get("policy_id") or "").strip():
        raise ValueError("official grandfather policy is missing policy_id")
    if not isinstance(payload.get("grants"), list):
        raise ValueError("official grandfather policy grants must be a list")
    return payload


def current_target_version() -> int:
    result = subprocess.run(
        ["git", "tag", "-l", "national-bot-v*"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    versions = [
        version
        for version in (parse_tag_version(line.strip()) for line in result.stdout.splitlines())
        if version is not None
    ]
    return max(versions, default=0) + 1


def grandfather_eligibility(
    candidate: str | Path,
    role: str,
    *,
    target_version: int | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if role not in ALLOWED_ROLES:
        return {"eligible": False, "reason": "unknown_eligibility_role", "role": role}
    try:
        active_policy = policy or load_grandfather_policy()
    except Exception as exc:
        return {
            "eligible": False,
            "reason": "grandfather_policy_unavailable",
            "role": role,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }

    identity = published_bot_identity(candidate)
    if not identity.get("published"):
        return {
            "eligible": False,
            "reason": "not_published_artifact",
            "role": role,
            "identity": identity,
        }
    cutoff = int(active_policy.get("new_candidate_cutoff", 0) or 0)
    version = int(identity.get("version") or 0)
    if cutoff and version >= cutoff:
        return {
            "eligible": False,
            "reason": "new_candidate_cannot_be_grandfathered",
            "role": role,
            "version": version,
            "cutoff": cutoff,
        }

    grant = next(
        (
            item
            for item in active_policy.get("grants", [])
            if isinstance(item, dict) and item.get("bot") == identity.get("label")
        ),
        None,
    )
    if not grant:
        return {"eligible": False, "reason": "no_grandfather_grant", "role": role}
    if bool(grant.get("revoked")):
        return {"eligible": False, "reason": "grandfather_grant_revoked", "role": role}
    if grant.get("artifact_hash") != identity.get("artifact_hash"):
        return {
            "eligible": False,
            "reason": "grandfather_artifact_hash_mismatch",
            "role": role,
            "expected_hash": grant.get("artifact_hash"),
            "current_hash": identity.get("artifact_hash"),
        }
    roles = {str(item) for item in grant.get("roles", [])}
    if role not in roles:
        return {"eligible": False, "reason": "role_not_granted", "role": role}

    target = int(target_version if target_version is not None else current_target_version())
    role_sunsets = grant.get("role_sunset_versions") or {}
    sunset = int(
        role_sunsets.get(
            role,
            grant.get("sunset_version", active_policy.get("sunset_version", 0)),
        )
        or 0
    )
    if sunset and target > sunset:
        return {
            "eligible": False,
            "reason": "grandfather_grant_expired",
            "role": role,
            "target_version": target,
            "sunset_version": sunset,
        }
    return {
        "eligible": True,
        "reason": "content_bound_grandfather_grant",
        "role": role,
        "priority": 1,
        "target_version": target,
        "sunset_version": sunset,
        "policy_id": active_policy.get("policy_id"),
        "policy_digest": canonical_digest(active_policy),
        "grant": grant,
        "identity": identity,
    }
