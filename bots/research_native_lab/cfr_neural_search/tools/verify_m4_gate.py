"""Publish and verify the deterministic Route-B M4 blueprint gate."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..blueprint.artifact import load_blueprint_artifact
from ..blueprint.hunl_abstraction import HUNLAbstractionConfig
from ..blueprint.hunl_game import HUNLTrainingGame, common_dependency_payload
from ..core.identity import canonical_json_bytes, file_sha256, payload_sha256
from ..core.run_journal import (
    load_event_export,
    load_event_log,
    validate_heartbeat_payload,
)
from ..core.selector_invalidation import (
    assert_workspace_not_invalidated,
    invalidation_registry_snapshot,
    m4_authority_lock,
)
from ..core.strict_io import (
    atomic_json_write,
    atomic_write_bytes,
    load_hashed_json,
    read_regular_bytes,
    remove_empty_directory,
    remove_regular_file,
    stable_flat_directory_manifest,
    validate_real_directory,
)
from ..native_runtime.local_evidence import (
    load_reproducibility_evidence,
    run_reproducibility_gate,
    save_reproducibility_evidence,
    sever_backend_hashes,
)
from .select_hunl_scale import (
    SELECTION_EVIDENCE_SCHEMA,
    _selection_run_contract,
)
from .train_hunl_blueprint import (
    FORMAL_RUN_CONTRACT_SCHEMA,
    RUNTIME_ROOT_NAME,
    SOURCE_SNAPSHOT_EXCLUDED_PATHS,
    _assert_module_provenance,
    _load_config,
    _route_root,
    build_loaded_scale_observation,
    capture_source_snapshot,
)


M4_MANIFEST_SCHEMA = "route-b-hunl-m4-gate-manifest-v2"
M4_GATE_NAME = "route_b_hunl_blueprint_native_vertical_slice"
MANIFEST_RELATIVE = "manifests/m4_gate_20260714.json"
BLUEPRINT_RELATIVE = "artifacts/m4/blueprint.rbbp"
SELECTION_RELATIVE = "artifacts/m4/training_scale_selection.json"
LOCAL_EVIDENCE_RELATIVE = "artifacts/m4/local_native_evidence.json"
SELECTOR_EVENTS_RELATIVE = "artifacts/m4/selector_events.jsonl"
SELECTOR_EVENT_DIRECTORY_RELATIVE = "artifacts/m4/selector_events"
SELECTOR_HEARTBEAT_RELATIVE = "artifacts/m4/selector_heartbeat.json"


@dataclass(frozen=True, slots=True)
class _PublishedOutputBackup:
    scalar_files: dict[str, bytes | None]
    event_directory_existed: bool
    event_files: dict[str, bytes]
    event_manifest: dict[str, str]


def _published_scalar_relatives() -> tuple[str, ...]:
    # Restore the manifest last so no old authority is temporarily paired with
    # a partially restored payload set.
    return (
        BLUEPRINT_RELATIVE,
        SELECTION_RELATIVE,
        LOCAL_EVIDENCE_RELATIVE,
        SELECTOR_EVENTS_RELATIVE,
        SELECTOR_HEARTBEAT_RELATIVE,
        MANIFEST_RELATIVE,
    )


def _capture_published_output_backup(route: Path) -> _PublishedOutputBackup:
    scalar: dict[str, bytes | None] = {}
    for relative in _published_scalar_relatives():
        path = route / relative
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"published output is not a real file: {relative}")
            scalar[relative] = read_regular_bytes(path, root=route)
        else:
            scalar[relative] = None
    event_directory = route / SELECTOR_EVENT_DIRECTORY_RELATIVE
    event_directory_existed = event_directory.exists() or event_directory.is_symlink()
    if event_directory_existed:
        if event_directory.is_symlink() or not event_directory.is_dir():
            raise ValueError("published selector event backup target is not a real directory")
        manifest = stable_flat_directory_manifest(event_directory)
        event_files = {
            name: read_regular_bytes(event_directory / name, root=route)
            for name in sorted(manifest)
        }
        if stable_flat_directory_manifest(event_directory) != manifest:
            raise ValueError("published selector event tree changed during backup")
    else:
        manifest = {}
        event_files = {}
    # Close the scalar/event gap with an exact second scalar pass.
    for relative, content in scalar.items():
        path = route / relative
        if content is None:
            if path.exists() or path.is_symlink():
                raise ValueError("published scalar appeared during backup")
        elif read_regular_bytes(path, root=route) != content:
            raise ValueError("published scalar changed during backup")
    if event_directory_existed and stable_flat_directory_manifest(
        event_directory
    ) != manifest:
        raise ValueError("published selector event tree changed across backup passes")
    return _PublishedOutputBackup(
        scalar_files=scalar,
        event_directory_existed=event_directory_existed,
        event_files=event_files,
        event_manifest=manifest,
    )


def _restore_published_output_backup(
    route: Path,
    backup: _PublishedOutputBackup,
) -> None:
    # Restore all payload scalars first, excluding the manifest authority.
    for relative in _published_scalar_relatives()[:-1]:
        content = backup.scalar_files[relative]
        path = route / relative
        if content is None:
            remove_regular_file(path, root=route, missing_ok=True)
        else:
            atomic_write_bytes(path, content, root=route)

    event_directory = route / SELECTOR_EVENT_DIRECTORY_RELATIVE
    if event_directory.exists() or event_directory.is_symlink():
        if event_directory.is_symlink() or not event_directory.is_dir():
            raise RuntimeError("cannot roll back a replaced selector event directory")
        current = stable_flat_directory_manifest(event_directory)
    else:
        current = {}
    for name, content in backup.event_files.items():
        atomic_write_bytes(event_directory / name, content, root=route)
    for name in sorted(set(current) - set(backup.event_files)):
        remove_regular_file(event_directory / name, root=route)
    if not backup.event_directory_existed:
        remove_empty_directory(event_directory, root=route, missing_ok=True)

    manifest_content = backup.scalar_files[MANIFEST_RELATIVE]
    manifest_path = route / MANIFEST_RELATIVE
    if manifest_content is None:
        remove_regular_file(manifest_path, root=route, missing_ok=True)
    else:
        atomic_write_bytes(manifest_path, manifest_content, root=route)
    _require_published_output_backup(route, backup)


def _require_published_output_backup(
    route: Path,
    backup: _PublishedOutputBackup,
) -> None:
    for relative, expected in backup.scalar_files.items():
        path = route / relative
        if expected is None:
            if path.exists() or path.is_symlink():
                raise RuntimeError(f"rollback left unexpected published file: {relative}")
        elif read_regular_bytes(path, root=route) != expected:
            raise RuntimeError(f"rollback changed published bytes: {relative}")
    event_directory = route / SELECTOR_EVENT_DIRECTORY_RELATIVE
    exists = event_directory.exists() or event_directory.is_symlink()
    if exists != backup.event_directory_existed:
        raise RuntimeError("rollback changed selector event directory existence")
    if exists:
        if event_directory.is_symlink() or not event_directory.is_dir():
            raise RuntimeError("rollback selector event directory is not real")
        manifest = stable_flat_directory_manifest(event_directory)
        if manifest != backup.event_manifest:
            raise RuntimeError("rollback changed selector event tree manifest")
        for name, expected in backup.event_files.items():
            if read_regular_bytes(event_directory / name, root=route) != expected:
                raise RuntimeError("rollback changed selector authoritative event bytes")


def _strict_keys(payload: Mapping[str, Any], expected: set[str], context: str) -> None:
    if type(payload) is not dict or set(payload) != expected:
        raise ValueError(f"{context} differs from strict schema")


def _validate_selection(
    payload: Mapping[str, Any],
    config: Mapping[str, Any],
    source_snapshot: Any,
) -> None:
    _strict_keys(
        payload,
        {
            "schema",
            "status",
            "input_authority",
            "pinned_config_sha256",
            "run_contract_sha256",
            "source_snapshot_sha256",
            "candidate_batches",
            "selection_metric",
            "selection_threshold",
            "selection_rule",
            "observations",
            "selected_batches",
            "checkpoint_sha256",
            "forbidden_inputs",
        },
        "published selector evidence",
    )
    scale = config["scale_gate"]
    expected_run_contract = _selection_run_contract(config, source_snapshot)
    if (
        payload["schema"] != SELECTION_EVIDENCE_SCHEMA
        or payload["status"] != "complete"
        or payload["input_authority"] != "training_only"
        or payload["pinned_config_sha256"] != payload_sha256(config)
        or payload["source_snapshot_sha256"] != source_snapshot.digest
        or payload["run_contract_sha256"] != payload_sha256(expected_run_contract)
        or payload["candidate_batches"] != scale["candidate_batches"]
        or payload["selection_metric"] != scale["selection_metric"]
        or payload["selection_threshold"] != scale["selection_threshold"]
        or payload["selection_rule"] != scale["selection_rule"]
        or payload["observations"] != scale["frozen_observations"]
        or payload["selected_batches"] != scale["frozen_selected_batches"]
        or payload["forbidden_inputs"] != scale["forbidden_inputs"]
    ):
        raise ValueError("published selector evidence differs from frozen config/source")
    checkpoint_sha256 = payload["checkpoint_sha256"]
    if (
        type(checkpoint_sha256) is not str
        or len(checkpoint_sha256) != 64
        or any(character not in "0123456789abcdef" for character in checkpoint_sha256)
    ):
        raise ValueError("published selector checkpoint digest is invalid")


def _paths(route: Path) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    return (
        route / MANIFEST_RELATIVE,
        route / BLUEPRINT_RELATIVE,
        route / SELECTION_RELATIVE,
        route / LOCAL_EVIDENCE_RELATIVE,
        route / SELECTOR_EVENT_DIRECTORY_RELATIVE,
        route / SELECTOR_EVENTS_RELATIVE,
        route / SELECTOR_HEARTBEAT_RELATIVE,
    )


def _selector_journal_evidence(
    *,
    route: Path,
    config: Mapping[str, Any],
    snapshot: Any,
    selection: Mapping[str, Any],
    root: Path,
    event_directory: Path,
    events_path: Path,
    heartbeat_path: Path,
) -> tuple[list[dict[str, Any]], Mapping[str, Any], dict[str, str]]:
    identity = {
        "run_contract_sha256": selection["run_contract_sha256"],
        "source_snapshot_sha256": snapshot.digest,
        "config_payload_sha256": payload_sha256(config),
        "config_file_sha256": file_sha256(
            route / "configs" / "hunl_m4_blueprint.json"
        ),
    }
    before_manifest = stable_flat_directory_manifest(event_directory)
    events = load_event_log(
        event_directory,
        root=root,
        run_identity=identity,
    )
    after_manifest = stable_flat_directory_manifest(event_directory)
    if before_manifest != after_manifest:
        raise ValueError("authoritative selector event tree changed during validation")
    expected_event_files = {
        f"{sequence:012d}.json" for sequence in range(len(events))
    }
    if set(after_manifest) != expected_event_files:
        raise ValueError(
            "formal selector event tree contains missing, extra, or orphan files"
        )
    exported = load_event_export(events_path, root=root, run_identity=identity)
    expected_export = b"".join(
        canonical_json_bytes(event) + b"\n" for event in events
    )
    if exported != events or read_regular_bytes(events_path, root=root) != expected_export:
        raise ValueError(
            "derived selector JSONL is not byte-identical to the authoritative chain"
        )
    heartbeat = load_hashed_json(heartbeat_path, root=root)
    validate_heartbeat_payload(heartbeat, identity, events)
    selected = config["scale_gate"]["frozen_selected_batches"]
    if (
        not events
        or events[-1]["event"] != "selector_completed"
        or events[-1]["completed_batches"] != selected
        or events[-1]["checkpoint_sha256"] != selection["checkpoint_sha256"]
        or heartbeat["status"] != "completed"
        or heartbeat["completed_batches"] != selected
        or heartbeat["checkpoint_sha256"] != selection["checkpoint_sha256"]
        or heartbeat["last_event_sequence"] != events[-1]["sequence"]
        or heartbeat["last_event_sha256"] != events[-1]["event_sha256"]
    ):
        raise ValueError("published selector journal is not a completed frozen run")
    return events, heartbeat, after_manifest


def _require_checkpoint_not_invalidated(
    selection: Mapping[str, Any],
    registry: Mapping[str, Any],
) -> None:
    checkpoint_sha256 = selection["checkpoint_sha256"]
    matching = [
        entry["workspace_relative"]
        for entry in registry["entries"]
        if entry["selector_checkpoint_payload_sha256"] == checkpoint_sha256
    ]
    if matching:
        raise ValueError(
            "published selector checkpoint belongs to an invalidated run: "
            + ", ".join(sorted(matching))
        )


def _require_unchanged_render_boundary(
    route: Path,
    selection: Mapping[str, Any],
    initial_snapshot: Any,
    initial_registry: Mapping[str, Any],
) -> None:
    final_snapshot = capture_source_snapshot(route)
    final_registry = invalidation_registry_snapshot(route)
    _require_checkpoint_not_invalidated(selection, final_registry)
    if final_snapshot != initial_snapshot or final_registry != initial_registry:
        raise RuntimeError(
            "Route/Common source or invalidation registry changed during M4 render"
        )


def render_m4_manifest() -> dict[str, Any]:
    route = _route_root()
    with m4_authority_lock(route):
        return _render_m4_manifest_locked(route)


def _render_m4_manifest_locked(route: Path) -> dict[str, Any]:
    _assert_module_provenance(route)
    (
        manifest_path,
        blueprint_path,
        selection_path,
        evidence_path,
        selector_event_directory,
        selector_events_path,
        selector_heartbeat_path,
    ) = _paths(route)
    snapshot = capture_source_snapshot(route)
    config = _load_config(route)
    selection = load_hashed_json(selection_path, root=route)
    _validate_selection(selection, config, snapshot)
    invalidation_registry = invalidation_registry_snapshot(route)
    _require_checkpoint_not_invalidated(selection, invalidation_registry)
    selector_events, selector_heartbeat, selector_event_manifest = (
        _selector_journal_evidence(
            route=route,
            config=config,
            snapshot=snapshot,
            selection=selection,
            root=route,
            event_directory=selector_event_directory,
            events_path=selector_events_path,
            heartbeat_path=selector_heartbeat_path,
        )
    )
    training = config["training"]
    game = HUNLTrainingGame(HUNLAbstractionConfig(**training["abstraction"]))
    blueprint = load_blueprint_artifact(blueprint_path, game, root=route)
    artifact_observation = build_loaded_scale_observation(
        blueprint,
        config["influence_gate"]["required_material_rows"],
    )
    if artifact_observation != config["scale_gate"]["frozen_observations"][-1]:
        raise ValueError("formal artifact observation differs from frozen selector")
    run_contract = dict(blueprint.contract.run_contract)
    if (
        run_contract.get("schema") != FORMAL_RUN_CONTRACT_SCHEMA
        or run_contract.get("pinned_config") != config
        or run_contract.get("pinned_config_sha256") != payload_sha256(config)
        or run_contract.get("target_batches") != training["batches"]
        or run_contract.get("source_snapshot") != snapshot.to_payload()
        or run_contract.get("source_snapshot_sha256") != snapshot.digest
    ):
        raise ValueError("blueprint formal run contract differs from frozen source/config")
    local = load_reproducibility_evidence(evidence_path, root=route)
    projection = local["runs"][0]["semantic_projection"]
    backend_files = sever_backend_hashes()
    if sever_backend_hashes() != backend_files:
        raise RuntimeError("sever backend changed across manifest hash passes")
    backend_sha = payload_sha256({"files": backend_files})
    if (
        projection["artifact_sha256"] != blueprint.artifact_sha256
        or projection["artifact_payload_sha256"] != blueprint.payload_sha256
        or projection["source_solver_sha256"] != blueprint.source_solver_sha256
        or projection["backend_sha256"] != backend_sha
        or projection["hands"] != 70
        or projection["illegal_actions"] != 0
        or projection["timeouts"] != 0
    ):
        raise ValueError("local native evidence differs from artifact/backend gate")
    generated_files = {
        BLUEPRINT_RELATIVE: file_sha256(blueprint_path),
        SELECTION_RELATIVE: file_sha256(selection_path),
        LOCAL_EVIDENCE_RELATIVE: file_sha256(evidence_path),
        SELECTOR_EVENTS_RELATIVE: file_sha256(selector_events_path),
        SELECTOR_HEARTBEAT_RELATIVE: file_sha256(selector_heartbeat_path),
        **{
            f"{SELECTOR_EVENT_DIRECTORY_RELATIVE}/{name}": digest
            for name, digest in selector_event_manifest.items()
        },
    }
    expected_scalar_outputs = SOURCE_SNAPSHOT_EXCLUDED_PATHS - {
        RUNTIME_ROOT_NAME,
        MANIFEST_RELATIVE,
        SELECTOR_EVENT_DIRECTORY_RELATIVE,
    }
    if set(generated_files) - {
        f"{SELECTOR_EVENT_DIRECTORY_RELATIVE}/{name}"
        for name in selector_event_manifest
    } != expected_scalar_outputs:
        raise AssertionError("generated output exclusions and manifest map drifted")
    rendered = {
        "schema": M4_MANIFEST_SCHEMA,
        "gate": M4_GATE_NAME,
        "audit_date": "2026-07-14",
        "status": "passed_local_diagnostic_only_not_officially_certified",
        "authority": {
            "strength_weight": 0,
            "chip_result_acceptance_weight": 0,
            "official_exe_certificate": False,
            "purpose": "M4 correctness/native vertical-slice comparison evidence only",
        },
        "root_contract": {
            "repository_relative_root": "bots/research_native_lab/cfr_neural_search",
            "source_snapshot": snapshot.to_payload(),
            "source_snapshot_sha256": snapshot.digest,
            "excluded_generated_outputs": sorted(SOURCE_SNAPSHOT_EXCLUDED_PATHS),
        },
        "common_dependency": common_dependency_payload(),
        "invalidated_selector_run_registry": {
            **invalidation_registry,
            "snapshot_payload_sha256": payload_sha256(invalidation_registry),
            "published_selector_checkpoint_absent": True,
        },
        "sever_backend": {
            "files": backend_files,
            "sha256": backend_sha,
        },
        "config": {
            "relative_path": "configs/hunl_m4_blueprint.json",
            "sha256": file_sha256(route / "configs/hunl_m4_blueprint.json"),
            "payload_sha256": payload_sha256(config),
            "selected_batches": training["batches"],
        },
        "selector": {
            "relative_path": SELECTION_RELATIVE,
            "file_sha256": generated_files[SELECTION_RELATIVE],
            "payload_sha256": payload_sha256(selection),
            "run_contract_sha256": selection["run_contract_sha256"],
            "observations": selection["observations"],
            "selected_batches": selection["selected_batches"],
            "durable_journal": {
                "events_relative_path": SELECTOR_EVENTS_RELATIVE,
                "events_file_sha256": generated_files[SELECTOR_EVENTS_RELATIVE],
                "authoritative_event_directory": SELECTOR_EVENT_DIRECTORY_RELATIVE,
                "authoritative_event_files": selector_event_manifest,
                "authoritative_event_files_sha256": payload_sha256(
                    {"files": selector_event_manifest}
                ),
                "event_count": len(selector_events),
                "event_chain_tip_sequence": selector_events[-1]["sequence"],
                "event_chain_tip_sha256": selector_events[-1]["event_sha256"],
                "durable_completed_batches": selector_events[-1][
                    "completed_batches"
                ],
                "durable_checkpoint_sha256": selector_events[-1][
                    "checkpoint_sha256"
                ],
                "heartbeat_relative_path": SELECTOR_HEARTBEAT_RELATIVE,
                "heartbeat_file_sha256": generated_files[
                    SELECTOR_HEARTBEAT_RELATIVE
                ],
                "heartbeat_payload_sha256": payload_sha256(selector_heartbeat),
                "atomic_event_files_at_runtime": True,
                "jsonl_is_atomic_derived_view": True,
                "jsonl_byte_identical_to_authoritative_chain": True,
                "completed_heartbeat_is_exact_chain_tip": True,
            },
        },
        "blueprint": {
            "relative_path": BLUEPRINT_RELATIVE,
            "file_sha256": generated_files[BLUEPRINT_RELATIVE],
            "artifact_sha256": blueprint.artifact_sha256,
            "payload_sha256": blueprint.payload_sha256,
            "source_solver_sha256": blueprint.source_solver_sha256,
            "training_contract_sha256": blueprint.contract.digest,
            "formal_run_contract_sha256": blueprint.contract.run_contract_sha256,
            "statistics": dict(blueprint.statistics),
            "resources": dict(blueprint.resources),
        },
        "local_native_reproducibility": {
            "relative_path": LOCAL_EVIDENCE_RELATIVE,
            "file_sha256": generated_files[LOCAL_EVIDENCE_RELATIVE],
            "payload_sha256": payload_sha256(local),
            "semantic_projection_sha256": local["semantic_projection_sha256"],
            "semantic_projection": projection,
            "acceptance": local["runs"][0]["acceptance"],
        },
        "generated_files": generated_files,
        "generator": {
            "relative_path": "tools/verify_m4_gate.py",
            "source_sha256": file_sha256(Path(__file__)),
            "write_is_atomic": True,
            "post_write_readback": True,
            "mixed_time_source_rejected": True,
        },
    }
    _require_unchanged_render_boundary(
        route,
        selection,
        snapshot,
        invalidation_registry,
    )
    return rendered


def verify_m4_manifest() -> dict[str, Any]:
    route = _route_root()
    with m4_authority_lock(route):
        return _verify_m4_manifest_locked(route)


def _verify_m4_manifest_locked(route: Path) -> dict[str, Any]:
    manifest_path = route / MANIFEST_RELATIVE
    stored = load_hashed_json(manifest_path, root=route)
    expected = render_m4_manifest()
    if stored != expected:
        raise ValueError("stored M4 manifest differs from current deterministic render")
    return expected


def write_m4_manifest() -> dict[str, Any]:
    route = _route_root()
    with m4_authority_lock(route):
        return _write_m4_manifest_locked(route)


def _write_m4_manifest_locked(route: Path) -> dict[str, Any]:
    manifest_path = route / MANIFEST_RELATIVE
    before = capture_source_snapshot(route)
    rendered = render_m4_manifest()
    if capture_source_snapshot(route) != before:
        raise RuntimeError("Route/Common source changed while rendering M4 manifest")
    previous = (
        read_regular_bytes(manifest_path, root=route)
        if manifest_path.is_file() and not manifest_path.is_symlink()
        else None
    )
    try:
        atomic_json_write(manifest_path, rendered, root=route)
        verified = verify_m4_manifest()
        if capture_source_snapshot(route) != before:
            raise RuntimeError("Route/Common source changed after M4 manifest write")
        return verified
    except BaseException:
        if previous is None:
            remove_regular_file(manifest_path, root=route, missing_ok=True)
        else:
            atomic_write_bytes(manifest_path, previous, root=route)
        raise


def publish_m4_outputs(training_workspace: Path, selector_workspace: Path) -> dict[str, Any]:
    route = _route_root()
    with m4_authority_lock(route):
        return _publish_m4_outputs_locked(
            route,
            training_workspace,
            selector_workspace,
        )


def _publish_m4_outputs_locked(
    route: Path,
    training_workspace: Path,
    selector_workspace: Path,
) -> dict[str, Any]:
    runtime_root = validate_real_directory(route / RUNTIME_ROOT_NAME)
    training_workspace = validate_real_directory(training_workspace)
    selector_workspace = validate_real_directory(selector_workspace)
    for workspace in (training_workspace, selector_workspace):
        try:
            workspace.relative_to(runtime_root)
        except ValueError as exc:
            raise ValueError("publish workspaces must be below runtime_outputs") from exc
    before = capture_source_snapshot(route)
    (
        _manifest_path,
        blueprint_path,
        selection_path,
        evidence_path,
        selector_event_directory,
        selector_events_path,
        selector_heartbeat_path,
    ) = _paths(route)
    assert_workspace_not_invalidated(route, training_workspace)
    assert_workspace_not_invalidated(route, selector_workspace)
    config = _load_config(route)
    selection = load_hashed_json(
        selector_workspace / "selection.json",
        root=selector_workspace,
    )
    _validate_selection(selection, config, before)
    initial_registry = invalidation_registry_snapshot(route)
    _require_checkpoint_not_invalidated(selection, initial_registry)

    def require_current_invalidation_boundary() -> None:
        assert_workspace_not_invalidated(route, training_workspace)
        assert_workspace_not_invalidated(route, selector_workspace)
        current_registry = invalidation_registry_snapshot(route)
        _require_checkpoint_not_invalidated(selection, current_registry)
        if current_registry != initial_registry:
            raise RuntimeError("invalidation registry changed during M4 publication")
        if capture_source_snapshot(route) != before:
            raise RuntimeError("Route/Common source changed during M4 publication")

    _events, _heartbeat, source_event_manifest = _selector_journal_evidence(
        route=route,
        config=config,
        snapshot=before,
        selection=selection,
        root=selector_workspace,
        event_directory=selector_workspace / "events",
        events_path=selector_workspace / "events.jsonl",
        heartbeat_path=selector_workspace / "heartbeat.json",
    )
    require_current_invalidation_boundary()
    # Freeze every runtime input into memory before touching the prior reviewed
    # publication. Runtime workspaces are excluded from the source snapshot.
    blueprint_bytes = read_regular_bytes(
        training_workspace / "blueprint.rbbp",
        root=training_workspace,
    )
    selection_bytes = read_regular_bytes(
        selector_workspace / "selection.json",
        root=selector_workspace,
    )
    selector_export_bytes = read_regular_bytes(
        selector_workspace / "events.jsonl",
        root=selector_workspace,
    )
    selector_heartbeat_bytes = read_regular_bytes(
        selector_workspace / "heartbeat.json",
        root=selector_workspace,
    )
    source_event_bytes = {
        name: read_regular_bytes(
            selector_workspace / "events" / name,
            root=selector_workspace,
        )
        for name in sorted(source_event_manifest)
    }
    if {
        name: hashlib.sha256(content).hexdigest()
        for name, content in source_event_bytes.items()
    } != source_event_manifest:
        raise RuntimeError("selector authoritative event bytes changed after validation")
    reread_selection = load_hashed_json(
        selector_workspace / "selection.json",
        root=selector_workspace,
    )
    if reread_selection != selection:
        raise RuntimeError("selector evidence changed during publication")
    if (
        read_regular_bytes(
            selector_workspace / "selection.json",
            root=selector_workspace,
        )
        != selection_bytes
    ):
        raise RuntimeError("selector evidence bytes changed during publication")
    reread_events, reread_heartbeat, reread_event_manifest = (
        _selector_journal_evidence(
            route=route,
            config=config,
            snapshot=before,
            selection=selection,
            root=selector_workspace,
            event_directory=selector_workspace / "events",
            events_path=selector_workspace / "events.jsonl",
            heartbeat_path=selector_workspace / "heartbeat.json",
        )
    )
    if (
        reread_events != _events
        or reread_heartbeat != _heartbeat
        or reread_event_manifest != source_event_manifest
    ):
        raise RuntimeError("selector journal changed during publication")
    if (
        read_regular_bytes(
            selector_workspace / "events.jsonl",
            root=selector_workspace,
        )
        != selector_export_bytes
        or read_regular_bytes(
            selector_workspace / "heartbeat.json",
            root=selector_workspace,
        )
        != selector_heartbeat_bytes
    ):
        raise RuntimeError("selector journal view bytes changed during publication")

    require_current_invalidation_boundary()
    backup = _capture_published_output_backup(route)
    try:
        require_current_invalidation_boundary()
        atomic_write_bytes(blueprint_path, blueprint_bytes, root=route)
        if selector_event_directory.exists() or selector_event_directory.is_symlink():
            if (
                selector_event_directory.is_symlink()
                or not selector_event_directory.is_dir()
            ):
                raise ValueError("published selector event target is not a real directory")
            existing_event_manifest = stable_flat_directory_manifest(
                selector_event_directory
            )
        else:
            existing_event_manifest = {}
        if any("/" in name for name in existing_event_manifest):
            raise ValueError("published selector event target contains nested files")
        for name, content in source_event_bytes.items():
            atomic_write_bytes(
                selector_event_directory / name,
                content,
                root=route,
            )
        for name in sorted(set(existing_event_manifest) - set(source_event_manifest)):
            remove_regular_file(selector_event_directory / name, root=route)
        if stable_flat_directory_manifest(
            selector_event_directory
        ) != source_event_manifest:
            raise RuntimeError("published authoritative selector event tree drifted")
        atomic_write_bytes(selection_path, selection_bytes, root=route)
        atomic_write_bytes(selector_events_path, selector_export_bytes, root=route)
        atomic_write_bytes(
            selector_heartbeat_path,
            selector_heartbeat_bytes,
            root=route,
        )
        require_current_invalidation_boundary()
        diagnostic = config["diagnostic_evidence"]
        first, second = run_reproducibility_gate(
            blueprint_path,
            deck_root_seed=diagnostic["deck_root_seed"],
            policy_seeds=tuple(diagnostic["policy_seeds"]),
        )
        save_reproducibility_evidence(
            evidence_path,
            first,
            second,
            root=route,
        )
        require_current_invalidation_boundary()
        result = write_m4_manifest()
        require_current_invalidation_boundary()
        return result
    except BaseException:
        _restore_published_output_backup(route, backup)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--training-workspace", type=Path)
    parser.add_argument("--selector-workspace", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.publish:
        if arguments.training_workspace is None or arguments.selector_workspace is None:
            parser.error("--publish requires both workspaces")
        result = publish_m4_outputs(
            arguments.training_workspace,
            arguments.selector_workspace,
        )
    elif arguments.write:
        result = write_m4_manifest()
    else:
        result = verify_m4_manifest()
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
