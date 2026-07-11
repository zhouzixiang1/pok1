"""Role-based official eligibility and transitional grandfather policy."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import threading
import time
from typing import Any

from bot_artifact import canonical_digest, published_bot_identity
from national_epoch_registry import (
    DEFAULT_LEGACY_LEDGER,
    effective_target_version,
    load_registry_state,
)


ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "web" / "core" / "official_grandfathering.json"
ALLOWED_ROLES = {"parent_source", "rating_pool", "official_opponent"}
_REGISTRY_CACHE_TTL_SEC = 2.0
_REGISTRY_CACHE_LOCK = threading.Lock()
_REGISTRY_CACHE: dict[str, Any] = {"loaded_at": 0.0, "head": "", "state": None}


def load_grandfather_policy(path: str | Path | None = None) -> dict[str, Any]:
    policy_path = Path(path) if path is not None else POLICY_PATH
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    schema_version = int(payload.get("schema_version", 0) or 0)
    if schema_version not in {1, 2, 3}:
        raise ValueError("unsupported official grandfather policy schema")
    if not str(payload.get("policy_id") or "").strip():
        raise ValueError("official grandfather policy is missing policy_id")
    if not isinstance(payload.get("grants"), list):
        raise ValueError("official grandfather policy grants must be a list")
    labels: set[str] = set()
    for grant in payload["grants"]:
        if not isinstance(grant, dict):
            raise ValueError("official grandfather grant must be an object")
        label = str(grant.get("bot") or "")
        if not label or label in labels:
            raise ValueError(f"duplicate or missing official grandfather grant: {label!r}")
        labels.add(label)
        roles = grant.get("roles")
        if not isinstance(roles, list) or not roles or not set(map(str, roles)) <= ALLOWED_ROLES:
            raise ValueError(f"invalid roles in official grandfather grant: {label}")
        artifact_hash = str(grant.get("artifact_hash") or "")
        if len(artifact_hash) != 64:
            raise ValueError(f"invalid artifact hash in official grandfather grant: {label}")
        if schema_version >= 3:
            for field in ("tag_object", "completion_tree_oid"):
                value = str(grant.get(field) or "")
                if len(value) != 40 or any(char not in "0123456789abcdef" for char in value.lower()):
                    raise ValueError(f"invalid {field} in official grandfather grant: {label}")
    return payload


def _current_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _registry_state():
    now = time.monotonic()
    head = _current_head()
    with _REGISTRY_CACHE_LOCK:
        cached = _REGISTRY_CACHE.get("state")
        if (
            cached is not None
            and _REGISTRY_CACHE.get("head") == head
            and now - float(_REGISTRY_CACHE.get("loaded_at") or 0.0) < _REGISTRY_CACHE_TTL_SEC
        ):
            return cached
    state = load_registry_state(
        ROOT,
        legacy_ledger=DEFAULT_LEGACY_LEDGER,
        include_history=True,
    )
    with _REGISTRY_CACHE_LOCK:
        _REGISTRY_CACHE.update({"loaded_at": now, "head": head, "state": state})
    return state


def clear_registry_state_cache() -> None:
    with _REGISTRY_CACHE_LOCK:
        _REGISTRY_CACHE.update({"loaded_at": 0.0, "head": "", "state": None})


def epoch_lifecycle_eligibility(version: int) -> dict[str, Any]:
    try:
        state = _registry_state()
    except Exception as exc:
        return {
            "eligible": False,
            "reason": "national_epoch_registry_error",
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }
    if not state.available:
        return {
            "eligible": False,
            "reason": "national_epoch_registry_unavailable",
            "diagnostics": list(state.diagnostics),
        }
    if int(version) in state.reaped_versions:
        return {
            "eligible": False,
            "reason": "national_bot_reaped",
            "version": int(version),
            "registry_source": state.source,
        }
    return {
        "eligible": True,
        "reason": "national_epoch_active",
        "version": int(version),
        "registry_source": state.source,
    }


def current_target_version(requested: int | None = None) -> int:
    state = _registry_state()
    baseline = max(1, int(requested or 1))
    return effective_target_version(
        baseline,
        repo_root=ROOT,
        state=state,
    )


def grandfather_eligibility(
    candidate: str | Path,
    role: str,
    *,
    target_version: int | None = None,
    policy: dict[str, Any] | None = None,
    readiness: dict[str, Any] | None = None,
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

    try:
        identity = published_bot_identity(candidate)
    except Exception as exc:
        return {
            "eligible": False,
            "reason": "artifact_identity_error",
            "role": role,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }
    if not identity.get("published"):
        return {
            "eligible": False,
            "reason": "not_published_artifact",
            "role": role,
            "identity": identity,
        }
    cutoff = int(active_policy.get("new_candidate_cutoff", 0) or 0)
    version = int(identity.get("version") or 0)
    lifecycle = epoch_lifecycle_eligibility(version)
    if not lifecycle.get("eligible"):
        return {**lifecycle, "role": role}
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
    if int(active_policy.get("schema_version", 0) or 0) >= 3:
        if grant.get("tag_object") != identity.get("tag_object"):
            return {
                "eligible": False,
                "reason": "grandfather_tag_object_mismatch",
                "role": role,
            }
        if grant.get("completion_tree_oid") != identity.get("completion_tree_oid"):
            return {
                "eligible": False,
                "reason": "grandfather_completion_tree_mismatch",
                "role": role,
            }
    roles = {str(item) for item in grant.get("roles", [])}
    if role not in roles:
        return {"eligible": False, "reason": "role_not_granted", "role": role}

    readiness_rules = active_policy.get("readiness_rules") or {}
    readiness_rule = (
        readiness_rules.get(role)
        if isinstance(readiness_rules, dict)
        else None
    )
    readiness_count = int((readiness or {}).get("certified_alternatives", 0) or 0)
    readiness_minimum = int(
        (readiness_rule or {}).get("minimum_certified_alternatives", 0) or 0
    )
    if readiness_minimum and readiness_count >= readiness_minimum:
        return {
            "eligible": False,
            "reason": "grandfather_readiness_satisfied",
            "role": role,
            "certified_alternatives": readiness_count,
            "minimum_certified_alternatives": readiness_minimum,
        }

    try:
        target = current_target_version(target_version)
    except Exception as exc:
        return {
            "eligible": False,
            "reason": "national_epoch_target_unavailable",
            "role": role,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }
    role_sunsets = grant.get("role_sunset_versions") or {}
    sunset = int(
        role_sunsets.get(
            role,
            grant.get("sunset_version", active_policy.get("sunset_version", 0)),
        )
        or 0
    )
    if sunset and target > sunset and not readiness_minimum:
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
        "certified_alternatives": readiness_count,
        "minimum_certified_alternatives": readiness_minimum,
        "policy_id": active_policy.get("policy_id"),
        "policy_digest": canonical_digest(active_policy),
        "grant": grant,
        "identity": identity,
    }
