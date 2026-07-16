"""Stable submit request fingerprints."""

from __future__ import annotations

import hashlib
import json

from .schemas import TaskEnvelope


EXECUTION_PROFILE_VERSION = "worker-mcp-execution-v1"


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def request_fingerprint(request: TaskEnvelope) -> str:
    payload = {
        "repo": str(request.repo),
        "base_commit": request.base_commit.strip(),
        "goal": _normalize_text(request.goal),
        "allowed_paths": sorted(request.allowed_paths),
        "forbidden_paths": sorted(request.forbidden_paths),
        "constraints": sorted(_normalize_text(item) for item in request.constraints),
        "acceptance_criteria": sorted(
            _normalize_text(item) for item in request.acceptance_criteria
        ),
        "task_type": request.task_type.value,
        "execution": request.execution.model_dump(mode="json"),
        "execution_profile_version": EXECUTION_PROFILE_VERSION,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
