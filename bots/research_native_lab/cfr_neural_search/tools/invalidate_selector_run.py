"""Content-derive and atomically publish an invalidated selector-run marker."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from ..blueprint.hunl_training import HUNL_CHECKPOINT_SCHEMA, HUNL_TRAINING_FORMAT
from ..blueprint.mccfr import SolverState
from ..core.identity import payload_sha256, require_sha256
from ..core.selector_invalidation import (
    INVALIDATION_AUTHORITY,
    INVALIDATION_NAME,
    INVALIDATION_SCHEMA,
    m4_authority_lock,
    register_invalidation,
)
from ..core.run_journal import utc_now
from ..core.strict_io import (
    atomic_json_create,
    load_hashed_json,
    read_regular_bytes,
    strict_json_loads,
    validate_real_directory,
)
from .select_hunl_scale import SELECTION_EVIDENCE_SCHEMA
from .train_hunl_blueprint import RUNTIME_ROOT_NAME, _route_root

def _read_hashed_payload_and_file_digest(
    path: Path,
    root: Path,
) -> tuple[dict[str, Any], str]:
    raw = read_regular_bytes(path, root=root)
    envelope = strict_json_loads(raw, context="selector invalidation input")
    if type(envelope) is not dict or set(envelope) != {"payload", "sha256"}:
        raise ValueError("selector invalidation input is not a hashed envelope")
    payload = envelope["payload"]
    if type(payload) is not dict:
        raise TypeError("selector invalidation payload must be an exact object")
    require_sha256(envelope["sha256"], "selector invalidation payload digest")
    if payload_sha256(payload) != envelope["sha256"]:
        raise ValueError("selector invalidation input payload digest mismatch")
    return payload, hashlib.sha256(raw).hexdigest()


def invalidate_selector_run(
    workspace: str | Path,
    reason: str,
    *,
    registry_root: str | Path | None = None,
) -> dict[str, Any]:
    route = _route_root()
    with m4_authority_lock(route):
        return _invalidate_selector_run_locked(
            route,
            workspace,
            reason,
            registry_root=registry_root,
        )


def _invalidate_selector_run_locked(
    route: Path,
    workspace: str | Path,
    reason: str,
    *,
    registry_root: str | Path | None,
) -> dict[str, Any]:
    if type(reason) is not str or not reason or not reason.isascii():
        raise ValueError("invalidation reason must be a nonempty ASCII string")
    runtime = validate_real_directory(route / RUNTIME_ROOT_NAME)
    root = validate_real_directory(workspace)
    try:
        root.relative_to(runtime)
    except ValueError as exc:
        raise ValueError("selector invalidation workspace must be under runtime_outputs") from exc
    checkpoint_path = root / "selector_checkpoint.json"
    trace_path = root / "selection.json"
    checkpoint, checkpoint_file_sha256 = _read_hashed_payload_and_file_digest(
        checkpoint_path,
        root,
    )
    if type(checkpoint) is not dict or set(checkpoint) != {
        "schema",
        "format_version",
        "contract",
        "contract_sha256",
        "solver",
        "solver_sha256",
        "resources",
    }:
        raise ValueError("selector checkpoint differs from strict HUNL schema")
    if (
        checkpoint["schema"] != HUNL_CHECKPOINT_SCHEMA
        or checkpoint["format_version"] != HUNL_TRAINING_FORMAT
        or type(checkpoint["solver"]) is not dict
    ):
        raise ValueError("selector checkpoint format is unsupported")
    state = SolverState.from_payload(checkpoint["solver"])
    if checkpoint["solver_sha256"] != state.digest:
        raise ValueError("selector checkpoint solver digest mismatch")
    require_sha256(checkpoint["contract_sha256"], "selector checkpoint contract")
    resources = checkpoint["resources"]
    if type(resources) is not dict or resources.get("batches") != state.batch_index:
        raise ValueError("selector checkpoint batch resources disagree with solver")
    checkpoint_payload_sha256 = payload_sha256(checkpoint)

    trace, trace_file_sha256 = _read_hashed_payload_and_file_digest(trace_path, root)
    if (
        type(trace) is not dict
        or trace.get("schema") != SELECTION_EVIDENCE_SCHEMA
        or trace.get("checkpoint_sha256") != checkpoint_payload_sha256
    ):
        raise ValueError("selector trace is not bound to the durable checkpoint")
    observations = trace.get("observations")
    if type(observations) is not list or any(type(row) is not dict for row in observations):
        raise ValueError("selector trace observations are invalid")
    observed_batches = [row.get("batches") for row in observations]
    if any(type(value) is not int or value <= 0 for value in observed_batches) or any(
        left >= right for left, right in zip(observed_batches, observed_batches[1:])
    ):
        raise ValueError("selector trace candidate observations are not strictly ordered")
    candidates = trace.get("candidate_batches")
    if (
        type(candidates) is not list
        or observed_batches != candidates[: len(observed_batches)]
    ):
        raise ValueError("selector trace observations are not its exact candidate prefix")
    last_observed = observed_batches[-1] if observed_batches else None
    if last_observed is not None and last_observed > state.batch_index:
        raise ValueError("selector trace observes a candidate beyond the checkpoint")
    derived = {
        "schema": INVALIDATION_SCHEMA,
        "invalidated_reason": reason,
        "authority": INVALIDATION_AUTHORITY,
        "last_complete_batch": state.batch_index,
        "last_observed_candidate": last_observed,
        "checkpoint": {
            "relative_path": "selector_checkpoint.json",
            "payload_sha256": checkpoint_payload_sha256,
            "file_sha256": checkpoint_file_sha256,
            "solver_sha256": state.digest,
        },
        "trace": {
            "relative_path": "selection.json",
            "payload_sha256": payload_sha256(trace),
            "file_sha256": trace_file_sha256,
            "status": trace.get("status"),
        },
    }
    marker = root / INVALIDATION_NAME
    if marker.exists() or marker.is_symlink():
        if marker.is_symlink() or not marker.is_file():
            raise ValueError("selector invalidation marker is not a real regular file")
        payload = load_hashed_json(marker, root=root)
        if type(payload) is not dict:
            raise ValueError("selector invalidation marker payload must be an object")
        without_time = dict(payload)
        invalidated_at = without_time.pop("invalidated_at_utc", None)
        if (
            type(invalidated_at) is not str
            or not invalidated_at.endswith("Z")
            or without_time != derived
        ):
            raise ValueError(
                "existing selector invalidation marker has different derived facts"
            )
    else:
        payload = {**derived, "invalidated_at_utc": utc_now()}
        try:
            atomic_json_create(marker, payload, root=root)
        except ValueError:
            if marker.is_symlink() or not marker.is_file():
                raise
            concurrent = load_hashed_json(marker, root=root)
            without_time = dict(concurrent)
            invalidated_at = without_time.pop("invalidated_at_utc", None)
            if (
                type(invalidated_at) is not str
                or not invalidated_at.endswith("Z")
                or without_time != derived
            ):
                raise
            payload = concurrent
    loaded = load_hashed_json(marker, root=root)
    if loaded != payload:
        raise RuntimeError("selector invalidation marker failed atomic readback")
    register_invalidation(
        route,
        root,
        payload,
        registry_root=registry_root,
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--reason", required=True)
    arguments = parser.parse_args(argv)
    result = invalidate_selector_run(arguments.workspace, arguments.reason)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
