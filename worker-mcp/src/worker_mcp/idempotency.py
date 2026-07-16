"""Stable submit request fingerprints."""

from __future__ import annotations

import hashlib
import json

from .schemas import TaskEnvelope


EXECUTION_PROFILE_VERSION = "worker-mcp-execution-v2"


def request_fingerprint(request: TaskEnvelope) -> str:
    # Bind the exact execution-relevant envelope.  Whitespace and list order can
    # change a model prompt, so treating those variants as equivalent would be
    # an unsafe idempotent replay.  The key itself identifies the replay slot,
    # while trace_id is observability metadata and may legitimately differ on a
    # transport retry.
    payload = request.model_dump(
        mode="json",
        exclude={"idempotency_key", "trace_id"},
    )
    payload["execution_profile_version"] = EXECUTION_PROFILE_VERSION
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
