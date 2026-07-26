"""Archive rotation plan/receipt subsystem.

Extracted from evolution_infra.py as a single business responsibility: the
schema-2 archive-rotation plan/receipt pipeline -- build/validate the
per-generation rotation plan, rotate log files into ARCHIVE_DIR, and
issue+verify digest-signed rotation receipts.

All public symbols are re-exported by evolution_infra.py (via thin delegate
shells) for backward compatibility, covering every ``from evolution_infra
import <name>`` site and every ``evolution_infra.<name>`` monkeypatch.

IMPORTANT -- shared-symbol access model
---------------------------------------
Many names referenced by these bodies remain in ``evolution_infra`` because
they are part of that module's monkeypatch surface -- the test suite patches
``evolution_infra._write_rotation_record``, ``evolution_infra._ROTATION_*``
constants, and reads them back through the rotation code paths.  Binding them
at import time would freeze the pre-patch value and silently break the audit.

Every such reference in this file is written ``_ei.<name>`` so it resolves
against the live module attribute at call time.  References between members of
*this* module (e.g. ``build_archive_rotation_plan`` calling
``validate_archive_rotation_plan``) are written as bare globals, exactly as
they were inline.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path

import evolution_infra as _ei


def _rotation_rules():
    return (
        (Path(_ei.WORKER_FAILURES_FILE), 200),
        (Path(_ei.MATCH_HISTORY_FILE), 500),
        (Path(_ei.RATING_HISTORY_FILE), 100),
        (Path(_ei.RESULTS_DIR) / "events.jsonl", 1000),
        (Path(_ei.LLM_COSTS_FILE), 200),
    )


def _rotation_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _rotation_paths(source_path: Path, version: int):
    root = Path(_ei.ARCHIVE_DIR)
    return (
        root / f"{source_path.stem}_v{int(version)}.jsonl",
        root / f"{source_path.stem}_v{int(version)}.rotation.json",
        root / f"{source_path.stem}.rotation-watermark.json",
    )


def _rotation_set_plan_path(version: int):
    return Path(_ei.ARCHIVE_DIR) / f"rotation-set-v{int(version)}.plan.json"


def _rotation_record(path: Path, *, kind: str, keys: frozenset[str]):
    from bot_artifact import canonical_digest

    with _ei._locked_state_sidecar(path, lock_type=fcntl.LOCK_SH):
        existed = os.path.lexists(path)
        raw = _ei._read_regular_state_text(path, allow_missing=True)
    if not existed:
        return None
    if not raw.strip():
        raise RuntimeError(f"archive record empty: {path.name}")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"archive record invalid JSON: {path.name}") from exc
    if not isinstance(payload, dict) or set(payload) != set(keys):
        raise RuntimeError(f"archive record fields mismatch: {path.name}")
    if payload.get("schema_version") != 2 or payload.get("kind") != kind:
        raise RuntimeError(f"archive record schema mismatch: {path.name}")
    unsigned = {key: value for key, value in payload.items() if key != "digest"}
    if payload.get("digest") != canonical_digest(unsigned):
        raise RuntimeError(f"archive record digest mismatch: {path.name}")
    return payload


def _write_rotation_record(
    path: Path,
    payload: dict,
    *,
    kind: str,
    keys: frozenset[str],
):
    from bot_artifact import canonical_digest

    unsigned = dict(payload)
    if set(unsigned) != set(keys) - {"digest"}:
        raise RuntimeError(f"archive record write fields mismatch: {path.name}")
    if unsigned.get("schema_version") != 2 or unsigned.get("kind") != kind:
        raise RuntimeError(f"archive record write schema mismatch: {path.name}")
    final = {**unsigned, "digest": canonical_digest(unsigned)}
    with _ei._locked_state_sidecar(path, lock_type=fcntl.LOCK_EX):
        _ei._atomic_publish_state_text(
            path,
            json.dumps(final, indent=2, ensure_ascii=False, sort_keys=True),
        )
    reopened = _ei._rotation_record(path, kind=kind, keys=keys)
    if reopened != final:
        raise RuntimeError(f"archive record publication mismatch: {path.name}")
    return final


def _read_rotation_archive(path: Path):
    if not os.path.lexists(path):
        return None
    with _ei.locked_file(path, "rb", lock_type=fcntl.LOCK_SH) as handle:
        return handle.read()


def _publish_rotation_archive(path: Path, raw: bytes):
    path = Path(path)
    with _ei._locked_state_sidecar(path, lock_type=fcntl.LOCK_EX):
        existing = None
        if os.path.lexists(path):
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                _ei._assert_open_regular_path(path, handle, label="rotation archive")
                existing = handle.read()
                _ei._assert_open_regular_path(path, handle, label="rotation archive")
        if existing is not None:
            if existing != raw:
                raise RuntimeError(f"archive content mismatch: {path.name}")
            _ei._fsync_regular_state_file_and_parent(path)
            return
        _ei._assert_safe_state_parent(path)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = None
        temporary_identity = None
        try:
            descriptor = os.open(temporary, flags, 0o600)
            offset = 0
            while offset < len(raw):
                written = os.write(descriptor, raw[offset:])
                if written <= 0:
                    raise OSError("rotation archive write made no progress")
                offset += written
            os.fsync(descriptor)
            temporary_identity = os.fstat(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, path)
            live = os.lstat(path)
            if (
                temporary_identity is None
                or not stat.S_ISREG(live.st_mode)
                or live.st_nlink != 1
                or (live.st_dev, live.st_ino)
                != (temporary_identity.st_dev, temporary_identity.st_ino)
                or live.st_size != len(raw)
            ):
                raise OSError("rotation archive publication inode changed")
            _ei._fsync_directory(path.parent)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)


def _base_rotation_watermark(source_path: Path):
    from bot_artifact import canonical_digest

    unsigned = {
        "schema_version": 2,
        "kind": _ei._ROTATION_WATERMARK_KIND,
        "source": source_path.name,
        "end_offset": 0,
        "prefix_sha256": _ei._rotation_digest(b""),
        "last_version": None,
        "last_rotation_id": None,
        "last_plan_digest": None,
        "previous_watermark_digest": None,
    }
    return {**unsigned, "digest": canonical_digest(unsigned)}


def _validate_rotation_plan(
    plan: dict,
    *,
    version: int,
    source_path: Path,
    raw: bytes,
    require_completed: bool,
    require_archive: bool,
):
    from bot_artifact import canonical_digest

    archive_path, _plan_path, _watermark_path = _ei._rotation_paths(
        source_path,
        version,
    )
    if (
        type(plan.get("version")) is not int
        or plan["version"] != int(version)
        or plan.get("source") != source_path.name
        or plan.get("archive") != archive_path.name
        or plan.get("state") not in {"planned", "completed"}
        or (require_completed and plan.get("state") != "completed")
    ):
        raise RuntimeError(f"archive plan identity invalid: {source_path.name}")
    start = plan.get("start_offset")
    end = plan.get("end_offset")
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start < end <= len(raw)
    ):
        raise RuntimeError(f"archive plan offsets invalid: {source_path.name}")
    for key in (
        "archive_sha256", "new_prefix_sha256",
        "previous_watermark_digest", "rotation_id", "digest",
    ):
        value = str(plan.get(key) or "")
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise RuntimeError(f"archive plan digest invalid:{source_path.name}:{key}")
    archived = raw[start:end]
    subject = {
        "version": int(version),
        "source": source_path.name,
        "start_offset": start,
        "end_offset": end,
        "archive_sha256": _ei._rotation_digest(archived),
        "new_prefix_sha256": _ei._rotation_digest(raw[:end]),
        "previous_watermark_digest": plan["previous_watermark_digest"],
    }
    if (
        plan.get("archive_sha256") != subject["archive_sha256"]
        or plan.get("new_prefix_sha256") != subject["new_prefix_sha256"]
        or plan.get("rotation_id") != canonical_digest(subject)
    ):
        raise RuntimeError(f"archive plan derivation mismatch: {source_path.name}")
    archived_live = _ei._read_rotation_archive(archive_path)
    if archived_live is not None and archived_live != archived:
        raise RuntimeError(f"archive bytes mismatch: {archive_path.name}")
    if require_archive and archived_live is None:
        raise RuntimeError(f"archive bytes missing: {archive_path.name}")
    return archived


def _load_rotation_watermark(source_path: Path, raw: bytes):
    _archive, _plan, watermark_path = _ei._rotation_paths(source_path, 0)
    watermark = _ei._rotation_record(
        watermark_path,
        kind=_ei._ROTATION_WATERMARK_KIND,
        keys=_ei._ROTATION_WATERMARK_KEYS,
    )
    if watermark is None:
        return _ei._base_rotation_watermark(source_path)
    end = watermark.get("end_offset")
    if (
        watermark.get("source") != source_path.name
        or type(end) is not int
        or not 0 < end <= len(raw)
        or watermark.get("prefix_sha256") != _ei._rotation_digest(raw[:end])
        or type(watermark.get("last_version")) is not int
        or int(watermark["last_version"]) < _ei.FIRST_STRICT_POLICY_VERSION
    ):
        raise RuntimeError(f"archive watermark identity invalid: {source_path.name}")
    for key in (
        "last_rotation_id", "last_plan_digest", "previous_watermark_digest",
        "digest",
    ):
        value = str(watermark.get(key) or "")
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise RuntimeError(f"archive watermark digest invalid:{source_path.name}:{key}")
    prior_version = int(watermark["last_version"])
    _prior_archive, prior_plan_path, _prior_watermark = _ei._rotation_paths(
        source_path,
        prior_version,
    )
    prior_plan = _ei._rotation_record(
        prior_plan_path,
        kind=_ei._ROTATION_PLAN_KIND,
        keys=_ei._ROTATION_PLAN_KEYS,
    )
    if prior_plan is None:
        raise RuntimeError(f"archive watermark plan missing: {source_path.name}")
    _ei._validate_rotation_plan(
        prior_plan,
        version=prior_version,
        source_path=source_path,
        raw=raw,
        require_completed=True,
        require_archive=True,
    )
    if (
        prior_plan["end_offset"] != end
        or prior_plan["new_prefix_sha256"] != watermark["prefix_sha256"]
        or prior_plan["rotation_id"] != watermark["last_rotation_id"]
        or prior_plan["digest"] != watermark["last_plan_digest"]
        or prior_plan["previous_watermark_digest"]
        != watermark["previous_watermark_digest"]
    ):
        raise RuntimeError(f"archive watermark chain mismatch: {source_path.name}")
    return watermark


def _rotation_receipt(plan: dict):
    return {
        "source": plan["source"],
        "rotation_id": plan["rotation_id"],
        "plan_digest": plan["digest"],
        "archive_sha256": plan["archive_sha256"],
        "start_offset": plan["start_offset"],
        "end_offset": plan["end_offset"],
        "source_preserved_append_only": True,
    }


def _rotation_digest_value(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _completed_rotation_plan_digest(subject):
    from bot_artifact import canonical_digest

    unsigned = {
        "schema_version": 2,
        "kind": _ei._ROTATION_PLAN_KIND,
        "version": subject["version"],
        "source": subject["source"],
        "start_offset": subject["start_offset"],
        "end_offset": subject["end_offset"],
        "archive": subject["archive"],
        "archive_sha256": subject["archive_sha256"],
        "new_prefix_sha256": subject["new_prefix_sha256"],
        "previous_watermark_digest": subject["previous_watermark_digest"],
        "rotation_id": subject["rotation_id"],
        "state": "completed",
    }
    return canonical_digest(unsigned)


def _rotation_subject_receipt(subject):
    return {
        "source": subject["source"],
        "rotation_id": subject["rotation_id"],
        "plan_digest": subject["completed_plan_digest"],
        "archive_sha256": subject["archive_sha256"],
        "start_offset": subject["start_offset"],
        "end_offset": subject["end_offset"],
        "source_preserved_append_only": True,
    }


def _validate_archive_rotation_plan_shape(
    rotation_plan,
    *,
    version,
    publication_id=None,
):
    """Validate the self-contained high-level plan without reading effects."""

    from bot_artifact import canonical_digest

    version = int(version)
    if (
        not isinstance(rotation_plan, dict)
        or set(rotation_plan) != set(_ei._ROTATION_SET_PLAN_KEYS)
    ):
        raise RuntimeError("archive rotation set plan fields mismatch")
    observed_publication_id = rotation_plan.get("publication_id")
    if (
        rotation_plan.get("schema_version") != 1
        or rotation_plan.get("kind") != _ei._ROTATION_SET_PLAN_KIND
        or type(rotation_plan.get("version")) is not int
        or rotation_plan["version"] != version
        or not _ei._rotation_digest_value(observed_publication_id)
        or (
            publication_id is not None
            and observed_publication_id != publication_id
        )
        or rotation_plan.get("source_policy") != "append-only-cold-prefix"
        or rotation_plan.get("source_bytes_must_be_preserved") is not True
    ):
        raise RuntimeError("archive rotation set plan identity invalid")
    snapshots = rotation_plan.get("source_snapshots")
    rotations = rotation_plan.get("expected_rotations")
    rules = list(_ei._rotation_rules())
    if not isinstance(snapshots, list) or len(snapshots) != len(rules):
        raise RuntimeError("archive rotation source snapshots incomplete")
    if not isinstance(rotations, list):
        raise RuntimeError("archive rotation expected subjects invalid")

    derived_rotations = []
    seen_sources = set()
    for snapshot, (source_path, keep_lines) in zip(snapshots, rules):
        if (
            not isinstance(snapshot, dict)
            or set(snapshot) != set(_ei._ROTATION_SOURCE_SNAPSHOT_KEYS)
        ):
            raise RuntimeError("archive rotation source snapshot fields mismatch")
        source_name = source_path.name
        if source_name in seen_sources:
            raise RuntimeError("archive rotation rule source duplicated")
        seen_sources.add(source_name)
        size = snapshot.get("snapshot_size")
        cold_end = snapshot.get("cold_end_offset")
        watermark_end = snapshot.get("watermark_end_offset")
        if (
            snapshot.get("source") != source_name
            or snapshot.get("keep_lines") != keep_lines
            or type(snapshot.get("snapshot_exists")) is not bool
            or type(size) is not int
            or type(cold_end) is not int
            or type(watermark_end) is not int
            or not 0 <= watermark_end <= cold_end <= size
            or not _ei._rotation_digest_value(snapshot.get("snapshot_sha256"))
            or not _ei._rotation_digest_value(snapshot.get("watermark_digest"))
        ):
            raise RuntimeError(
                f"archive rotation source snapshot invalid: {source_name}"
            )
        if snapshot["snapshot_exists"] is False:
            base = _ei._base_rotation_watermark(source_path)
            if (
                size != 0
                or cold_end != 0
                or watermark_end != 0
                or snapshot["snapshot_sha256"] != _ei._rotation_digest(b"")
                or snapshot["watermark_digest"] != base["digest"]
            ):
                raise RuntimeError(
                    f"archive rotation absent snapshot invalid: {source_name}"
                )

        expected = snapshot.get("expected_rotation")
        if cold_end <= watermark_end:
            if expected is not None:
                raise RuntimeError(
                    f"archive rotation unexpected subject: {source_name}"
                )
            continue
        if (
            not isinstance(expected, dict)
            or set(expected) != set(_ei._ROTATION_SUBJECT_KEYS)
        ):
            raise RuntimeError(
                f"archive rotation expected subject fields mismatch: {source_name}"
            )
        archive_path, _plan_path, _watermark_path = _ei._rotation_paths(
            source_path,
            version,
        )
        subject = {
            "version": version,
            "source": source_name,
            "start_offset": watermark_end,
            "end_offset": cold_end,
            "archive_sha256": expected.get("archive_sha256"),
            "new_prefix_sha256": expected.get("new_prefix_sha256"),
            "previous_watermark_digest": snapshot["watermark_digest"],
        }
        if (
            expected.get("version") != version
            or expected.get("source") != source_name
            or expected.get("archive") != archive_path.name
            or expected.get("start_offset") != watermark_end
            or expected.get("end_offset") != cold_end
            or not _ei._rotation_digest_value(subject["archive_sha256"])
            or not _ei._rotation_digest_value(subject["new_prefix_sha256"])
            or expected.get("previous_watermark_digest")
            != snapshot["watermark_digest"]
            or expected.get("rotation_id") != canonical_digest(subject)
            or expected.get("completed_plan_digest")
            != _ei._completed_rotation_plan_digest(expected)
        ):
            raise RuntimeError(
                f"archive rotation expected subject invalid: {source_name}"
            )
        derived_rotations.append(expected)

    if rotations != derived_rotations:
        raise RuntimeError("archive rotation expected subject set mismatch")
    if rotation_plan.get("source_snapshot_set_digest") != canonical_digest(
        snapshots
    ):
        raise RuntimeError("archive rotation source snapshot digest mismatch")
    if rotation_plan.get("expected_rotation_set_digest") != canonical_digest(
        rotations
    ):
        raise RuntimeError("archive rotation expected subject digest mismatch")
    unsigned = {
        key: value
        for key, value in rotation_plan.items()
        if key != "authority_digest"
    }
    if rotation_plan.get("authority_digest") != canonical_digest(unsigned):
        raise RuntimeError("archive rotation authority digest mismatch")
    return derived_rotations


def expected_archive_rotation_receipts(
    rotation_plan,
    *,
    version,
    publication_id=None,
):
    """Derive the exact receipt set without relying on low-level plan files."""

    rotations = _ei._validate_archive_rotation_plan_shape(
        rotation_plan,
        version=version,
        publication_id=publication_id,
    )
    return [_ei._rotation_subject_receipt(subject) for subject in rotations]


def _read_archive_rotation_plan_authority(version, *, missing_ok=False):
    path = _ei._rotation_set_plan_path(version)
    if not os.path.lexists(path):
        if missing_ok:
            return None
        raise RuntimeError("archive rotation set plan authority missing")
    with _ei._locked_state_sidecar(path, lock_type=fcntl.LOCK_SH):
        raw = _ei._read_regular_state_text(path, allow_missing=False)
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("archive rotation set plan authority invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("archive rotation set plan authority not object")
    return payload


def _publish_archive_rotation_plan_authority(plan):
    """Create one immutable plan authority; never replace existing bytes."""

    version = int(plan["version"])
    path = _ei._rotation_set_plan_path(version)
    _ei._validate_archive_rotation_plan_shape(
        plan,
        version=version,
        publication_id=plan.get("publication_id"),
    )
    Path(_ei.ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)
    _ei._fsync_directory(_ei.ARCHIVE_DIR)
    encoded = json.dumps(
        plan,
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    with _ei._locked_state_sidecar(path, lock_type=fcntl.LOCK_EX):
        if os.path.lexists(path):
            existing = _ei._read_regular_state_text(path, allow_missing=False)
            if existing != encoded:
                raise RuntimeError(
                    "archive rotation set plan authority already differs"
                )
            _ei._fsync_regular_state_file_and_parent(path)
            return plan
        # The stable sidecar makes this a create-once publication for every
        # cooperating producer.  Atomic replace of the private inode has no
        # link/unlink crash window, unlike a create-only hardlink sequence.
        _ei._atomic_publish_state_text(path, encoded)
    reopened = _ei._read_archive_rotation_plan_authority(version)
    if reopened != plan:
        raise RuntimeError("archive rotation set plan authority reproof mismatch")
    return plan


def build_archive_rotation_plan(version, publication_id):
    """Freeze every managed source before any archive effect is allowed."""

    from bot_artifact import canonical_digest

    version = int(version)
    if version < _ei.FIRST_STRICT_POLICY_VERSION:
        raise RuntimeError("pre_epoch_archive_rotation_forbidden")
    if not _ei._rotation_digest_value(publication_id):
        raise RuntimeError("archive rotation publication identity invalid")
    existing_authority = _ei._read_archive_rotation_plan_authority(
        version,
        missing_ok=True,
    )
    if existing_authority is not None:
        _ei.validate_archive_rotation_plan(
            existing_authority,
            version=version,
            publication_id=publication_id,
        )
        return existing_authority

    snapshots = []
    rotations = []
    for source_path, keep_lines in _ei._rotation_rules():
        current_archive, current_plan, watermark_path = _ei._rotation_paths(
            source_path,
            version,
        )
        if os.path.lexists(current_archive) or os.path.lexists(current_plan):
            raise RuntimeError(
                f"archive rotation effect precedes high-level plan: {source_path.name}"
            )
        snapshot_exists = os.path.lexists(source_path)
        if snapshot_exists:
            with _ei.locked_file(source_path, "rb", lock_type=fcntl.LOCK_SH) as source:
                raw = source.read()
                watermark = _ei._load_rotation_watermark(source_path, raw)
        else:
            raw = b""
            watermark = _ei._base_rotation_watermark(source_path)
            if os.path.lexists(watermark_path):
                raise RuntimeError(
                    f"archive rotation source missing with authority: {source_path.name}"
                )
        lines = raw.splitlines(keepends=True)
        cold_end = (
            sum(len(line) for line in lines[:-keep_lines])
            if len(lines) > keep_lines
            else 0
        )
        start = int(watermark["end_offset"])
        if cold_end < start:
            cold_end = start
        expected = None
        if cold_end > start:
            archive_path, _plan_path, _watermark_path = _ei._rotation_paths(
                source_path,
                version,
            )
            subject = {
                "version": version,
                "source": source_path.name,
                "start_offset": start,
                "end_offset": cold_end,
                "archive_sha256": _ei._rotation_digest(raw[start:cold_end]),
                "new_prefix_sha256": _ei._rotation_digest(raw[:cold_end]),
                "previous_watermark_digest": watermark["digest"],
            }
            expected = {
                **subject,
                "archive": archive_path.name,
                "rotation_id": canonical_digest(subject),
            }
            expected["completed_plan_digest"] = (
                _ei._completed_rotation_plan_digest(expected)
            )
            rotations.append(expected)
        snapshots.append({
            "source": source_path.name,
            "keep_lines": keep_lines,
            "snapshot_exists": snapshot_exists,
            "snapshot_size": len(raw),
            "snapshot_sha256": _ei._rotation_digest(raw),
            "cold_end_offset": cold_end,
            "watermark_end_offset": start,
            "watermark_digest": watermark["digest"],
            "expected_rotation": expected,
        })
    plan = {
        "schema_version": 1,
        "kind": _ei._ROTATION_SET_PLAN_KIND,
        "version": version,
        "publication_id": publication_id,
        "source_policy": "append-only-cold-prefix",
        "source_bytes_must_be_preserved": True,
        "source_snapshots": snapshots,
        "expected_rotations": rotations,
        "source_snapshot_set_digest": canonical_digest(snapshots),
        "expected_rotation_set_digest": canonical_digest(rotations),
    }
    plan["authority_digest"] = canonical_digest(plan)
    _ei._validate_archive_rotation_plan_shape(
        plan,
        version=version,
        publication_id=publication_id,
    )
    return _ei._publish_archive_rotation_plan_authority(plan)


def validate_archive_rotation_plan(
    rotation_plan,
    *,
    version,
    publication_id=None,
):
    """Reprove the frozen source prefixes and current predecessor authority."""

    rotations = _ei._validate_archive_rotation_plan_shape(
        rotation_plan,
        version=version,
        publication_id=publication_id,
    )
    authority = _ei._read_archive_rotation_plan_authority(int(version))
    if authority != rotation_plan:
        raise RuntimeError("archive rotation set plan authority mismatch")
    expected_by_source = {item["source"]: item for item in rotations}
    for snapshot, (source_path, keep_lines) in zip(
        rotation_plan["source_snapshots"],
        _ei._rotation_rules(),
    ):
        if not os.path.lexists(source_path):
            if snapshot["snapshot_exists"]:
                raise RuntimeError(
                    f"archive rotation snapshot source missing: {source_path.name}"
                )
            raw = b""
            current_watermark = _ei._base_rotation_watermark(source_path)
        else:
            with _ei.locked_file(source_path, "rb", lock_type=fcntl.LOCK_SH) as source:
                raw = source.read()
                current_watermark = _ei._load_rotation_watermark(source_path, raw)
        snapshot_size = snapshot["snapshot_size"]
        if (
            len(raw) < snapshot_size
            or _ei._rotation_digest(raw[:snapshot_size])
            != snapshot["snapshot_sha256"]
        ):
            raise RuntimeError(
                f"archive rotation source prefix changed: {source_path.name}"
            )
        frozen = raw[:snapshot_size]
        lines = frozen.splitlines(keepends=True)
        cold_end = (
            sum(len(line) for line in lines[:-keep_lines])
            if len(lines) > keep_lines
            else 0
        )
        if cold_end < snapshot["watermark_end_offset"]:
            cold_end = snapshot["watermark_end_offset"]
        if cold_end != snapshot["cold_end_offset"]:
            raise RuntimeError(
                f"archive rotation frozen range changed: {source_path.name}"
            )
        expected = expected_by_source.get(source_path.name)
        _archive_path, low_plan_path, _watermark_path = _ei._rotation_paths(
            source_path,
            int(version),
        )
        low_plan = None
        if os.path.lexists(low_plan_path):
            low_plan = _ei._rotation_record(
                low_plan_path,
                kind=_ei._ROTATION_PLAN_KIND,
                keys=_ei._ROTATION_PLAN_KEYS,
            )
        if expected is None:
            if low_plan is not None:
                raise RuntimeError(
                    f"archive rotation unplanned low-level plan: {source_path.name}"
                )
            if current_watermark["digest"] != snapshot["watermark_digest"]:
                raise RuntimeError(
                    f"archive rotation no-op predecessor changed: {source_path.name}"
                )
            continue
        if low_plan is None:
            if current_watermark["digest"] != snapshot["watermark_digest"]:
                raise RuntimeError(
                    f"archive rotation predecessor changed: {source_path.name}"
                )
            continue
        _ei._validate_rotation_plan(
            low_plan,
            version=int(version),
            source_path=source_path,
            raw=raw,
            require_completed=low_plan.get("state") == "completed",
            require_archive=low_plan.get("state") == "completed",
        )
        for key in _ei._ROTATION_SUBJECT_KEYS - {"completed_plan_digest"}:
            if low_plan.get(key) != expected.get(key):
                raise RuntimeError(
                    f"archive rotation low-level plan mismatch: {source_path.name}"
                )
        if expected["completed_plan_digest"] != _ei._completed_rotation_plan_digest(
            expected
        ):
            raise RuntimeError(
                f"archive rotation completion digest mismatch: {source_path.name}"
            )
        if (
            low_plan.get("state") == "completed"
            and low_plan.get("digest") != expected["completed_plan_digest"]
        ):
            raise RuntimeError(
                f"archive rotation completed plan mismatch: {source_path.name}"
            )
    return rotation_plan


def archive_rotate_files(version, rotation_plan):
    """Copy new cold JSONL ranges without truncating their live authority."""

    version = int(version)
    if version < _ei.FIRST_STRICT_POLICY_VERSION:
        raise RuntimeError("pre_epoch_archive_rotation_forbidden")
    _ei.validate_archive_rotation_plan(
        rotation_plan,
        version=version,
        publication_id=rotation_plan.get("publication_id")
        if isinstance(rotation_plan, dict)
        else None,
    )
    expected_by_source = {
        item["source"]: item
        for item in rotation_plan["expected_rotations"]
    }
    Path(_ei.ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)
    _ei._fsync_directory(_ei.ARCHIVE_DIR)
    receipts = []
    for source_path, _keep_lines in _ei._rotation_rules():
        expected = expected_by_source.get(source_path.name)
        if expected is None:
            continue
        if not os.path.lexists(source_path):
            raise RuntimeError(
                f"archive rotation planned source missing: {source_path.name}"
            )
        archive_path, plan_path, watermark_path = _ei._rotation_paths(
            source_path,
            version,
        )
        with _ei.locked_file(source_path, "rb", lock_type=fcntl.LOCK_EX) as source:
            raw = source.read()
            watermark = _ei._load_rotation_watermark(source_path, raw)
            plan = _ei._rotation_record(
                plan_path,
                kind=_ei._ROTATION_PLAN_KIND,
                keys=_ei._ROTATION_PLAN_KEYS,
            )
            if plan is None:
                if os.path.lexists(archive_path):
                    raise RuntimeError(f"unclaimed archive exists: {archive_path.name}")
                if watermark["digest"] != expected["previous_watermark_digest"]:
                    raise RuntimeError(
                        f"archive rotation planned predecessor mismatch: {source_path.name}"
                    )
                plan = _ei._write_rotation_record(
                    plan_path,
                    {
                        "schema_version": 2,
                        "kind": _ei._ROTATION_PLAN_KIND,
                        **{
                            key: value
                            for key, value in expected.items()
                            if key != "completed_plan_digest"
                        },
                        "state": "planned",
                    },
                    kind=_ei._ROTATION_PLAN_KIND,
                    keys=_ei._ROTATION_PLAN_KEYS,
                )
            for key in _ei._ROTATION_SUBJECT_KEYS - {"completed_plan_digest"}:
                if plan.get(key) != expected.get(key):
                    raise RuntimeError(
                        f"archive rotation low-level plan mismatch: {source_path.name}"
                    )
            archived = _ei._validate_rotation_plan(
                plan,
                version=version,
                source_path=source_path,
                raw=raw,
                require_completed=False,
                require_archive=False,
            )
            start = int(plan["start_offset"])
            end = int(plan["end_offset"])
            watermark_end = int(watermark["end_offset"])
            if watermark_end < start or start < watermark_end < end:
                raise RuntimeError(f"archive watermark overlaps plan: {source_path.name}")
            if watermark_end == start:
                if plan["previous_watermark_digest"] != watermark["digest"]:
                    raise RuntimeError(f"archive plan predecessor mismatch: {source_path.name}")
                _ei._publish_rotation_archive(archive_path, archived)
                if plan["state"] != "completed":
                    plan = _ei._write_rotation_record(
                        plan_path,
                        {key: value for key, value in plan.items() if key != "digest"} | {"state": "completed"},
                        kind=_ei._ROTATION_PLAN_KIND,
                        keys=_ei._ROTATION_PLAN_KEYS,
                    )
                if plan["digest"] != expected["completed_plan_digest"]:
                    raise RuntimeError(
                        f"archive rotation completed plan mismatch: {source_path.name}"
                    )
                _ei._write_rotation_record(
                    watermark_path,
                    {
                        "schema_version": 2,
                        "kind": _ei._ROTATION_WATERMARK_KIND,
                        "source": source_path.name,
                        "end_offset": end,
                        "prefix_sha256": plan["new_prefix_sha256"],
                        "last_version": version,
                        "last_rotation_id": plan["rotation_id"],
                        "last_plan_digest": plan["digest"],
                        "previous_watermark_digest": plan["previous_watermark_digest"],
                    },
                    kind=_ei._ROTATION_WATERMARK_KIND,
                    keys=_ei._ROTATION_WATERMARK_KEYS,
                )
            elif watermark_end >= end:
                _ei._validate_rotation_plan(
                    plan,
                    version=version,
                    source_path=source_path,
                    raw=raw,
                    require_completed=True,
                    require_archive=True,
                )
                if plan["digest"] != expected["completed_plan_digest"]:
                    raise RuntimeError(
                        f"archive rotation completed plan mismatch: {source_path.name}"
                    )
            receipts.append(_ei._rotation_receipt(plan))
    _ei.validate_archive_rotation_receipts(
        version,
        receipts,
        rotation_plan=rotation_plan,
    )
    return receipts


def validate_archive_rotation_receipts(version, receipts, *, rotation_plan):
    """Pure read/reproof of an already planned rotation; creates no files."""

    version = int(version)
    if not isinstance(receipts, list):
        raise RuntimeError("archive rotation receipts must be a list")
    _ei.validate_archive_rotation_plan(
        rotation_plan,
        version=version,
        publication_id=rotation_plan.get("publication_id")
        if isinstance(rotation_plan, dict)
        else None,
    )
    expected_receipts = _ei.expected_archive_rotation_receipts(
        rotation_plan,
        version=version,
        publication_id=rotation_plan.get("publication_id")
        if isinstance(rotation_plan, dict)
        else None,
    )
    by_name = {path.name: path for path, _keep in _ei._rotation_rules()}
    supplied = {}
    for receipt in receipts:
        if not isinstance(receipt, dict) or set(receipt) != set(_ei._ROTATION_RECEIPT_KEYS):
            raise RuntimeError("archive rotation receipt fields mismatch")
        source_name = receipt.get("source")
        if source_name in supplied or source_name not in by_name:
            raise RuntimeError("archive rotation receipt source invalid")
        supplied[source_name] = receipt

    # The high-level plan is the authority even before the first low-level
    # per-source plan exists.  Reject omissions before inspecting effects so a
    # forged ``rotations=[]`` cannot vacuously certify a cold source.
    if receipts != expected_receipts:
        expected_names = {item["source"] for item in expected_receipts}
        supplied_names = set(supplied)
        missing = sorted(expected_names - supplied_names)
        if missing:
            raise RuntimeError(
                f"archive rotation receipt missing: {missing[0]}"
            )
        unexpected = sorted(supplied_names - expected_names)
        if unexpected:
            raise RuntimeError(
                f"archive rotation receipt unexpected: {unexpected[0]}"
            )
        raise RuntimeError("archive rotation receipt set mismatch")

    verified = []
    expected_by_source = {
        item["source"]: item for item in expected_receipts
    }
    for source_path, _keep_lines in _ei._rotation_rules():
        source_name = source_path.name
        if source_name not in expected_by_source:
            continue
        _archive_path, plan_path, _watermark_path = _ei._rotation_paths(
            source_path,
            version,
        )
        if not os.path.lexists(plan_path):
            raise RuntimeError(f"archive rotation plan missing: {source_name}")
        if not os.path.lexists(source_path):
            raise RuntimeError(
                f"archive rotation source missing: {source_name}"
            )
        with _ei.locked_file(source_path, "rb", lock_type=fcntl.LOCK_SH) as source:
            raw = source.read()
            watermark = _ei._load_rotation_watermark(source_path, raw)
            plan = _ei._rotation_record(
                plan_path,
                kind=_ei._ROTATION_PLAN_KIND,
                keys=_ei._ROTATION_PLAN_KEYS,
            )
            if plan is None:
                raise RuntimeError(f"archive rotation plan missing: {source_name}")
            _ei._validate_rotation_plan(
                plan,
                version=version,
                source_path=source_path,
                raw=raw,
                require_completed=True,
                require_archive=True,
            )
            if int(watermark["end_offset"]) < int(plan["end_offset"]):
                raise RuntimeError(f"archive rotation watermark behind: {source_name}")
            expected = _ei._rotation_receipt(plan)
            receipt = supplied.get(source_name)
            if receipt is None:
                raise RuntimeError(
                    f"archive rotation receipt missing: {source_name}"
                )
            if receipt != expected:
                raise RuntimeError(f"archive rotation receipt mismatch: {source_name}")
            verified.append(expected)
    return verified


def archive_old_logs(keep_generations=5):
    """Retired unsafe API; strict handoff cleanup owns explicit log paths."""

    raise RuntimeError(
        "archive_old_logs_retired_use_post_publication_strict_log_cleanup"
    )
