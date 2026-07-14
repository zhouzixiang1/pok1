"""Strict role eligibility for the national TCP policy epoch.

Every active parent, rating-pool member and formal opponent must resolve
through the strict policy ABI and its published signed full official-EXE
certificate.  Archived transition authorization is never loaded here.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import threading
import time
from typing import Any

from bot_namespace import (
    ACTIVE_PUBLISHED_ROLES,
    FIRST_STRICT_POLICY_VERSION,
    parse_bot_version,
    resolve_national_bot_spec,
)
from national_epoch_registry import (
    DEFAULT_LEGACY_LEDGER,
    effective_target_version,
    load_registry_state,
)


ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "web" / "core" / "official_role_policy.json"
ALLOWED_ROLES = set(ACTIVE_PUBLISHED_ROLES)
_REGISTRY_CACHE_TTL_SEC = 2.0
_REGISTRY_CACHE_LOCK = threading.Lock()
_REGISTRY_CACHE: dict[str, Any] = {"loaded_at": 0.0, "head": "", "state": None}


def load_official_role_policy(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate the grant-free strict active-role policy."""

    policy_path = Path(path) if path is not None else POLICY_PATH
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported official role policy schema")
    if payload.get("policy_id") != "national-tcp-policy-active-roles-v1":
        raise ValueError("official role policy id mismatch")
    if payload.get("epoch") != "national_tcp_policy_v1":
        raise ValueError("official role policy epoch mismatch")
    if payload.get("transitional_grants") != "forbidden":
        raise ValueError("official role policy must forbid transitional grants")
    roles = payload.get("roles")
    if not isinstance(roles, dict) or set(roles) != ALLOWED_ROLES:
        raise ValueError("official role policy role set mismatch")
    required = {
        "strict_national_bot_spec",
        "annotated_completion_publication",
        "signed_official-full-v5",
    }
    for role, contract in roles.items():
        if not isinstance(contract, dict) or set(contract.get("required") or []) != required:
            raise ValueError(f"official role requirements mismatch: {role}")
    if roles["official_opponent"].get("strength_weight") != 0:
        raise ValueError("official opponent must have zero strength weight")
    historical_root = payload.get("historical_signed_ledger_root")
    if historical_root != {
        "status": "retired",
        "active_role_authority": False,
        "executable": False,
    }:
        raise ValueError("historical signed-ledger root must be retired")
    first_control = payload.get("first_strict_control")
    if first_control != {
        "control_id": "first_strict_control_v1",
        "authority": "system_first_strict_control",
        "formal_bootstrap_scope": "first_policy_bot_empty_pool_only",
        "normal_official_opponent": False,
        "one_time": True,
        "strength_weight": 0,
        "rating_weight": 0,
    }:
        raise ValueError("first strict formal control policy mismatch")
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
            and now - float(_REGISTRY_CACHE.get("loaded_at") or 0.0)
            < _REGISTRY_CACHE_TTL_SEC
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
    """Check lifecycle only; strict ABI/publication checks live in the resolver."""

    try:
        normalized = int(version)
    except (TypeError, ValueError):
        return {"eligible": False, "reason": "invalid_national_bot_version"}
    if normalized < FIRST_STRICT_POLICY_VERSION:
        return {
            "eligible": False,
            "reason": "pre_policy_epoch_archived",
            "version": normalized,
            "first_strict_version": FIRST_STRICT_POLICY_VERSION,
        }
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
    if normalized in state.reaped_versions:
        return {
            "eligible": False,
            "reason": "national_bot_reaped",
            "version": normalized,
            "registry_source": state.source,
        }
    return {
        "eligible": True,
        "reason": "national_tcp_policy_epoch_active",
        "version": normalized,
        "registry_source": state.source,
    }


def current_target_version(requested: int | None = None) -> int:
    state = _registry_state()
    baseline = max(FIRST_STRICT_POLICY_VERSION, int(requested or 1))
    return effective_target_version(baseline, repo_root=ROOT, state=state)


def strict_role_eligibility(candidate: str | Path, role: str) -> dict[str, Any]:
    """Return the single strict resolver result for an active published role."""

    if role not in ALLOWED_ROLES:
        return {"eligible": False, "reason": "unknown_eligibility_role", "role": role}
    version = parse_bot_version(Path(candidate).name)
    lifecycle = (
        epoch_lifecycle_eligibility(version)
        if version is not None
        else {"eligible": False, "reason": "invalid_national_bot_label"}
    )
    if not lifecycle.get("eligible"):
        return {
            "eligible": False,
            "reason": lifecycle.get("reason") or "national_epoch_ineligible",
            "role": role,
            "lifecycle": lifecycle,
        }
    spec = resolve_national_bot_spec(candidate, role, repo_root=ROOT)
    result = spec.as_dict()
    if not spec.eligible:
        result["reason"] = "strict_national_bot_spec_rejected"
    else:
        result["reason"] = "strict_policy_bot_signed_full_certified"
    return result
