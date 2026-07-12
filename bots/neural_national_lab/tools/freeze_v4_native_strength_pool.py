#!/usr/bin/env python3
"""Freeze a ratings-selected native v4 strength pool without protected data.

Each ratings observation is read through exactly one file descriptor.  A
second observation immediately before publication detects drift.  Opponents must be
present in that exact snapshot, backed by annotated national completion tags,
and absent from the durable reaped registry.  The selected current execution
trees are copied into a new immutable output directory and bound by an exact,
self-hashed plan.  Completion-tag trees are recorded separately because valid
mainline protocol migrations may intentionally change an old bot after its
original completion tag.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
from typing import Any, Iterable, Mapping

SOURCE_ROOT = Path(__file__).resolve().parents[3]
ROOT = SOURCE_ROOT
TOOLS = Path(__file__).resolve().parent
WEB_CORE = SOURCE_ROOT / "web" / "core"
for import_root in (TOOLS, WEB_CORE):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from national_epoch_registry import load_registry_state  # noqa: E402
from v4_native_strength_artifacts import (  # noqa: E402
    ArtifactError as FreezeError,
    assert_read_only_tree as _assert_read_only_tree,
    canonical_tree_digest as _canonical_tree_digest,
    code_artifact_hashes as _code_artifact_hashes,
    copy_git_tree_snapshot as _copy_git_tree_snapshot_at_root,
    copy_tree_snapshot as _copy_tree_snapshot,
    directory_identity as _directory_identity,
    fsync_directory as _fsync_directory,
    fsync_tree as _fsync_tree,
    make_read_only as _make_read_only,
    mkdir_parents_fsync as _mkdir_parents_fsync,
    path_matches_directory_identity as _path_matches_directory_identity,
    publish_tree_noreplace as _publish_tree_noreplace,
    remove_tree as _remove_tree,
    read_regular_bytes_with_stat as _read_once_fd,
    restore_owner_access as _restore_owner_access,
    tag_directory_identity as _tag_directory_identity_at_root,
    tree_digest as _tree_digest,
    write_json_fsync as _write_json_fsync,
)
from v4_native_strength_runtime import native_strength_runtime_contract  # noqa: E402
from v4_native_strength_pool_output import (  # noqa: E402
    validate_frozen_output_tree as _validate_frozen_output_tree_impl,
)

SCHEMA = "opponent_multitask_v4_native_strength_pool_plan_v1"
PLAN_FILENAME = "strength_pool_plan.json"
RATINGS_SNAPSHOT_SCHEMA = "strict_glicko_ratings_raw_snapshot_v1"
SELECTION_METHOD = (
    "unrounded_conservative_glicko_desc_version_desc_with_latest_active_v1"
)
DECK_SEED_SCHEME = "opponent_disjoint_match_blocks_v1"
HANDS_PER_LEG = 70
MINIMUM_SEED_BLOCKS = 3
MINIMUM_POOL_SIZE = 4
DEFAULT_POOL_SIZE = 8
DEFAULT_SEED_BLOCKS = 12
DEFAULT_SEED_BASE = 9_100_000
DEFAULT_SEED_STRIDE = 1_000
DEFAULT_OPPONENT_SEED_STRIDE = 10_000_000
DEFAULT_BOT_SEED_BASE = 1_000_000_000
DEFAULT_BOT_SEED_STRIDE = 10
BOT_OPPONENT_SEED_STRIDE = 100_000
MINIMUM_BOOTSTRAP_SAMPLES = 2_000
MAXIMUM_BOOTSTRAP_SAMPLES = 1_000_000
DEFAULT_BOOTSTRAP_SAMPLES = 20_000
DEFAULT_BOOTSTRAP_SEED = 20_260_712
COMPLETION_TAG_RE = re.compile(r"^national-bot-v([1-9][0-9]*)$")
REAPED_TAG_RE = re.compile(r"^national-reaped-v([1-9][0-9]*)$")
HIGH_WATER_TAG_RE = re.compile(r"^national-high-water-v([1-9][0-9]*)$")
MIGRATION_MARKER_TAG = "national-reaped-registry-v1"
BOT_LABEL_RE = re.compile(r"^national_v([1-9][0-9]*)$")
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
RATING_ROW_KEYS = {"r", "rd", "sigma", "last_period"}
FALSE_AUTHORITY = {
    "protected_data_read": False,
    "policy_roles_opened": [],
    "held_out_read": False,
    "policy_selection_opened": False,
    "policy_gate_opened": False,
    "deployment_policy_value": False,
    "deployment_eligible": False,
    "strength_evidence": False,
    "native_strength_evidence": False,
    "official_exe_accepted": False,
    "formal_release_evidence": False,
}
PLAN_ROOT_KEYS = {
    "schema",
    "repository",
    "lifecycle",
    "ratings_snapshot",
    "candidate_artifact",
    "opponent_artifacts",
    "seeds",
    "actual_deck_seed_bases",
    "deck_seed_scheme",
    "opponent_seed_stride",
    "bot_seed_base",
    "bot_seed_stride",
    "bot_opponent_seed_stride",
    "hands_per_leg",
    "paired",
    "minimum_seed_blocks_per_opponent",
    "workers",
    "runtime_contract",
    "bootstrap_samples",
    "bootstrap_seed",
    "selection",
    "code_artifacts",
    *FALSE_AUTHORITY,
    "payload_sha256",
}
RATINGS_SNAPSHOT_KEYS = {
    "schema",
    "source_path",
    "bytes",
    "sha256",
    "raw_base64",
    "fstat",
    "ratings",
}
FSTAT_KEYS = {
    "st_dev",
    "st_ino",
    "st_mode",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
}
CANDIDATE_ARTIFACT_KEYS = {
    "label",
    "repository_completed_version",
    "source_path",
    "source_directory_sha256",
    "snapshot_path",
    "snapshot_directory_sha256",
    "native_entry",
}

OPPONENT_ARTIFACT_KEYS = {
    "label",
    "version",
    "source_path",
    "snapshot_path",
    "snapshot_directory_sha256",
    "native_entry",
    "execution_commit",
    "execution_tree_oid",
    "execution_directory_sha256",
    "completion_tag",
    "tag_object",
    "tag_commit",
    "tag_tree_oid",
    "tag_directory_sha256",
    "execution_matches_completion_tag",
}

REPOSITORY_KEYS = {"root", "head_commit", "main_commit", "origin_main_commit"}
LIFECYCLE_KEYS = {
    "registry_available",
    "registry_source",
    "migration_marker",
    "completion_versions",
    "annotated_completion_versions",
    "active_annotated_completion_versions",
    "reaped_versions",
    "high_water_versions",
    "authority_tag_refs",
    "diagnostics",
}
TAG_REF_KEYS = {"object_type", "object_oid", "peeled_oid"}
SELECTION_KEYS = {
    "method",
    "pool_size",
    "eligible_count",
    "ranking",
    "selected",
    "latest_completed_active_tag",
    "latest_completed_active_version",
    "latest_forced",
    "replaced_for_latest",
}
RANKING_ROW_KEYS = {
    "label",
    "version",
    "r",
    "rd",
    "sigma",
    "last_period",
    "conservative",
    "rank",
    "selected",
    "selection_position",
}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def plan_payload_sha256(payload: Mapping[str, Any]) -> str:
    unsigned = dict(payload)
    unsigned.pop("payload_sha256", None)
    return hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, field: str) -> None:
    observed = set(value)
    if observed != expected:
        raise FreezeError(
            f"{field} keys differ: missing={sorted(expected - observed)!r} "
            f"extra={sorted(observed - expected)!r}"
        )


def _strict_json_object(raw: bytes, *, source: str) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FreezeError(f"{source} is not UTF-8") from exc

    def reject_constant(value: str) -> None:
        raise FreezeError(f"{source} contains non-finite JSON constant {value!r}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise FreezeError(f"{source} contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except json.JSONDecodeError as exc:
        raise FreezeError(f"{source} is invalid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise FreezeError(f"{source} root must be an object")
    return payload


def _finite_number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FreezeError(f"{field} must be a JSON number")
    number = float(value)
    if not math.isfinite(number):
        raise FreezeError(f"{field} must be finite")
    return number


def _validate_ratings_payload(payload: dict[str, Any]) -> None:
    if not payload:
        raise FreezeError("ratings snapshot contains no bots")
    for label, raw_row in payload.items():
        if BOT_LABEL_RE.fullmatch(label) is None:
            raise FreezeError(f"ratings label is not canonical: {label!r}")
        if not isinstance(raw_row, dict):
            raise FreezeError(f"ratings row for {label} must be an object")
        _exact_keys(raw_row, RATING_ROW_KEYS, field=f"ratings.{label}")
        _finite_number(raw_row["r"], field=f"ratings.{label}.r")
        rd = _finite_number(raw_row["rd"], field=f"ratings.{label}.rd")
        sigma = _finite_number(raw_row["sigma"], field=f"ratings.{label}.sigma")
        if rd <= 0.0 or sigma <= 0.0:
            raise FreezeError(f"ratings uncertainty fields must be positive for {label}")
        if not isinstance(raw_row["last_period"], str) or not raw_row["last_period"]:
            raise FreezeError(f"ratings.{label}.last_period must be a non-empty string")


def read_ratings_snapshot(path: Path) -> dict[str, Any]:
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    canonical = _canonical_ratings_path()
    if absolute != canonical:
        raise FreezeError(
            f"ratings must be the canonical evolution snapshot: {canonical}"
        )
    raw, fstat_receipt = _read_once_fd(absolute)
    payload = _strict_json_object(raw, source=str(absolute))
    _validate_ratings_payload(payload)
    return {
        "schema": RATINGS_SNAPSHOT_SCHEMA,
        "source_path": str(absolute),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "raw_base64": base64.b64encode(raw).decode("ascii"),
        "fstat": fstat_receipt,
        "ratings": payload,
    }


def _git(*args: str, binary: bool = False) -> bytes | str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=not binary,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr if not binary else result.stderr.decode("utf-8", "replace")
        stdout = result.stdout if not binary else result.stdout.decode("utf-8", "replace")
        raise FreezeError(
            f"git {' '.join(args)} failed: {(stderr or stdout).strip()[:500]}"
        )
    return result.stdout


def _repo_commit(ref: str) -> str:
    value = str(_git("rev-parse", "--verify", f"{ref}^{{commit}}" )).strip()
    if HEX40_RE.fullmatch(value) is None:
        raise FreezeError(f"repository ref has no commit: {ref}")
    return value


def _repository_receipt() -> dict[str, str]:
    receipt = {
        "root": str(ROOT.resolve()),
        "head_commit": _repo_commit("HEAD"),
        "main_commit": _repo_commit("main"),
        "origin_main_commit": _repo_commit("origin/main"),
    }
    commits = {
        receipt["head_commit"],
        receipt["main_commit"],
        receipt["origin_main_commit"],
    }
    if len(commits) != 1:
        raise FreezeError(
            "strength pool must be frozen from HEAD == main == origin/main"
        )
    return receipt


def _operator_root() -> Path:
    common = Path(
        str(_git("rev-parse", "--path-format=absolute", "--git-common-dir")).strip()
    )
    if not common.is_absolute() or common.name != ".git":
        raise FreezeError("cannot derive the operator checkout from Git common-dir")
    return common.parent.resolve()


def _canonical_ratings_path() -> Path:
    return (
        _operator_root()
        / ".evolution_pok"
        / "web"
        / "core"
        / "results"
        / "glicko_ratings.json"
    )


def _authority_tag_refs() -> dict[str, dict[str, str]]:
    output = str(
        _git(
            "for-each-ref",
            "--format=%(refname:short)\t%(objecttype)\t%(objectname)\t%(*objectname)",
            "refs/tags",
        )
    )
    records: dict[str, dict[str, str]] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            raise FreezeError(f"malformed authority-tag record: {line!r}")
        name, object_type, object_oid, peeled_oid = parts
        if not (
            COMPLETION_TAG_RE.fullmatch(name)
            or REAPED_TAG_RE.fullmatch(name)
            or HIGH_WATER_TAG_RE.fullmatch(name)
            or name == MIGRATION_MARKER_TAG
        ):
            continue
        if object_type not in {"tag", "commit"}:
            raise FreezeError(f"unsupported authority tag object type for {name}")
        if HEX40_RE.fullmatch(object_oid) is None:
            raise FreezeError(f"authority tag has an invalid object id: {name}")
        if object_type == "tag":
            if HEX40_RE.fullmatch(peeled_oid) is None:
                raise FreezeError(f"annotated authority tag cannot be peeled: {name}")
        elif peeled_oid:
            raise FreezeError(f"lightweight authority tag unexpectedly peeled: {name}")
        records[name] = {
            "object_type": object_type,
            "object_oid": object_oid,
            "peeled_oid": peeled_oid,
        }
    return dict(sorted(records.items()))


def _completion_tags() -> dict[int, dict[str, str]]:
    output = str(
        _git(
            "for-each-ref",
            "--format=%(refname:short)\t%(objecttype)\t%(objectname)\t%(*objectname)",
            "refs/tags/national-bot-v*",
        )
    )
    records: dict[int, dict[str, str]] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            raise FreezeError(f"malformed completion-tag record: {line!r}")
        name, object_type, object_oid, peeled_oid = parts
        match = COMPLETION_TAG_RE.fullmatch(name)
        if match is None:
            continue
        # A lightweight tag is not a formal completion proof and is ignored.
        if (
            object_type != "tag"
            or HEX40_RE.fullmatch(object_oid) is None
            or HEX40_RE.fullmatch(peeled_oid) is None
        ):
            continue
        version = int(match.group(1))
        records[version] = {
            "completion_tag": name,
            "tag_object": object_oid,
            "tag_commit": peeled_oid,
        }
    return records


def _tag_directory_identity(label: str, commit: str) -> tuple[str, str]:
    return _tag_directory_identity_at_root(ROOT, label, commit)


def _copy_git_tree_snapshot(commit: str, label: str, destination: Path) -> tuple[str, str]:
    return _copy_git_tree_snapshot_at_root(ROOT, commit, label, destination)


def _code_artifacts() -> dict[str, str]:
    # Bind a conservative transitive Python closure for the evaluator and TCP
    # engine.  Snapshot bots themselves are bound separately below.
    return _code_artifact_hashes(
        SOURCE_ROOT,
        (
            TOOLS,
            SOURCE_ROOT / "bots" / "neural_national_lab" / "runtime",
            SOURCE_ROOT / "web" / "core",
            SOURCE_ROOT / "sever",
            SOURCE_ROOT / "engine",
        ),
    )


def _validate_positive_int(value: Any, *, field: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise FreezeError(f"{field} must be an integer >= {minimum}")
    return value


def _sorted_unique_ints(value: Any, *, field: str) -> list[int]:
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) or item <= 0
        for item in value
    ):
        raise FreezeError(f"{field} must be a list of positive integers")
    if value != sorted(set(value)):
        raise FreezeError(f"{field} must be sorted and unique")
    return list(value)


def _seed_contract(
    *,
    opponents: int,
    seed_blocks: int,
    seed_base: int,
    seed_stride: int,
    opponent_seed_stride: int,
    bot_seed_base: int,
    bot_seed_stride: int,
) -> tuple[list[int], list[int]]:
    for field, value in (
        ("seed_base", seed_base),
        ("seed_stride", seed_stride),
        ("opponent_seed_stride", opponent_seed_stride),
        ("bot_seed_base", bot_seed_base),
        ("bot_seed_stride", bot_seed_stride),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise FreezeError(f"{field} must be a non-negative integer")
    if seed_stride <= 0 or opponent_seed_stride <= 0 or bot_seed_stride <= 0:
        raise FreezeError("seed strides must be positive")
    seeds = [seed_base + index * seed_stride for index in range(seed_blocks)]
    actual = sorted(
        seed + opponent_index * opponent_seed_stride
        for opponent_index in range(opponents)
        for seed in seeds
    )
    for left, right in zip(actual, actual[1:]):
        if right <= left + HANDS_PER_LEG - 1:
            raise FreezeError("deck seed windows overlap")
    bot_bases = sorted(
        bot_seed_base
        + match_index * bot_seed_stride
        + opponent_index * BOT_OPPONENT_SEED_STRIDE
        for opponent_index in range(opponents)
        for match_index in range(seed_blocks)
    )
    for left, right in zip(bot_bases, bot_bases[1:]):
        if right <= left + 1:
            raise FreezeError("per-player bot seed windows overlap")
    return seeds, actual


def _resolve_candidate(path: Path) -> Path:
    expanded = path.expanduser()
    raw_absolute = expanded if expanded.is_absolute() else ROOT / expanded
    absolute = Path(os.path.abspath(os.fspath(raw_absolute)))
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise FreezeError(f"candidate directory is unavailable: {absolute}") from exc
    if resolved != absolute:
        raise FreezeError("candidate path may not contain symbolic links")
    absolute = resolved
    if not (absolute / "national_bot.py").is_file():
        raise FreezeError(f"candidate has no native entry: {absolute}")
    _tree_digest(absolute)
    return absolute


def _candidate_repository_version(candidate: Path) -> int | None:
    match = BOT_LABEL_RE.fullmatch(candidate.name)
    if match is None:
        return None
    repository_path = Path(
        os.path.abspath(os.fspath(ROOT / "bots" / candidate.name))
    )
    if candidate != repository_path:
        return None
    return int(match.group(1))


def _load_lifecycle_receipt(
    completion_tags: Mapping[int, Mapping[str, str]],
) -> tuple[dict[str, Any], set[int]]:
    lifecycle_state = load_registry_state(
        ROOT,
        legacy_ledger=ROOT / "web" / "core" / "results" / "reaped_bots.jsonl",
        include_history=False,
    )
    if (
        not lifecycle_state.available
        or not lifecycle_state.migration_marker
        or lifecycle_state.source != "durable_tags"
    ):
        raise FreezeError(
            "durable national lifecycle registry is unavailable: "
            + ";".join(lifecycle_state.diagnostics)
        )
    reaped = set(lifecycle_state.require_reaped_versions())
    active_tag_versions = sorted(set(completion_tags) - reaped)
    if not active_tag_versions:
        raise FreezeError("repository has no annotated active completion tags")
    lifecycle = {
        "registry_available": True,
        "registry_source": lifecycle_state.source,
        "migration_marker": lifecycle_state.migration_marker,
        "completion_versions": sorted(lifecycle_state.completion_versions),
        "annotated_completion_versions": sorted(completion_tags),
        "active_annotated_completion_versions": active_tag_versions,
        "reaped_versions": sorted(reaped),
        "high_water_versions": sorted(lifecycle_state.high_water_versions),
        "authority_tag_refs": _authority_tag_refs(),
        "diagnostics": list(lifecycle_state.diagnostics),
    }
    return lifecycle, reaped


def _recompute_selection(
    ratings: Mapping[str, Any],
    lifecycle: Mapping[str, Any],
    *,
    pool_size: int,
    candidate_repository_version: int | None,
) -> dict[str, Any]:
    active_versions = _sorted_unique_ints(
        lifecycle.get("active_annotated_completion_versions"),
        field="lifecycle.active_annotated_completion_versions",
    )
    candidate_version = candidate_repository_version
    if candidate_version is not None and (
        type(candidate_version) is not int or candidate_version <= 0
    ):
        raise FreezeError("candidate repository version is invalid")
    active = set(active_versions)
    ranked: list[dict[str, Any]] = []
    for label, raw_row in ratings.items():
        match = BOT_LABEL_RE.fullmatch(label)
        if match is None:
            raise FreezeError(f"ratings label is not canonical: {label!r}")
        version = int(match.group(1))
        if version not in active or version == candidate_version:
            continue
        conservative = float(raw_row["r"]) - 2.0 * float(raw_row["rd"])
        if not math.isfinite(conservative):
            raise FreezeError(f"ratings conservative score is non-finite for {label}")
        ranked.append({
            "label": label,
            "version": version,
            "r": raw_row["r"],
            "rd": raw_row["rd"],
            "sigma": raw_row["sigma"],
            "last_period": raw_row["last_period"],
            "conservative": conservative,
        })
    ranked.sort(key=lambda row: (row["conservative"], row["version"]), reverse=True)
    if len(ranked) < pool_size:
        raise FreezeError(
            f"eligible rated active pool has {len(ranked)} bots; {pool_size} required"
        )
    opponent_active_versions = [
        version for version in active_versions if version != candidate_version
    ]
    if not opponent_active_versions:
        raise FreezeError("repository has no eligible active completion opponent")
    latest_active_version = max(opponent_active_versions)
    if latest_active_version not in {int(row["version"]) for row in ranked}:
        raise FreezeError(
            "latest annotated completed active bot is absent from the strict ratings snapshot"
        )
    selected = list(ranked[:pool_size])
    latest_row = next(row for row in ranked if row["version"] == latest_active_version)
    latest_forced = latest_row not in selected
    replaced = None
    if latest_forced:
        replaced = selected[-1]["label"]
        selected[-1] = latest_row
    selected_labels = [str(row["label"]) for row in selected]
    selected_positions = {label: index + 1 for index, label in enumerate(selected_labels)}
    ranking = [
        {
            **row,
            "rank": index + 1,
            "selected": row["label"] in selected_positions,
            "selection_position": selected_positions.get(row["label"]),
        }
        for index, row in enumerate(ranked)
    ]
    return {
        "method": SELECTION_METHOD,
        "pool_size": pool_size,
        "eligible_count": len(ranking),
        "ranking": ranking,
        "selected": selected_labels,
        "latest_completed_active_tag": f"national-bot-v{latest_active_version}",
        "latest_completed_active_version": latest_active_version,
        "latest_forced": latest_forced,
        "replaced_for_latest": replaced,
    }


def _selection_and_provenance(
    ratings: dict[str, Any],
    *,
    pool_size: int,
    candidate: Path,
    execution_commit: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    completion_tags = _completion_tags()
    lifecycle, _ = _load_lifecycle_receipt(completion_tags)
    selection = _recompute_selection(
        ratings,
        lifecycle,
        pool_size=pool_size,
        candidate_repository_version=_candidate_repository_version(candidate),
    )
    provenance: dict[str, dict[str, Any]] = {}
    for row in selection["ranking"]:
        label = str(row["label"])
        version = int(row["version"])
        tag = completion_tags[version]
        source = ROOT / "bots" / label
        if not source.is_dir() or not (source / "national_bot.py").is_file():
            raise FreezeError(f"rated completed bot has no current native entry: {label}")
        execution_tree_oid, execution_digest = _tag_directory_identity(
            label, execution_commit
        )
        tag_tree_oid, tag_digest = _tag_directory_identity(label, tag["tag_commit"])
        provenance[label] = {
            **tag,
            "execution_commit": execution_commit,
            "execution_tree_oid": execution_tree_oid,
            "execution_directory_sha256": execution_digest,
            "tag_tree_oid": tag_tree_oid,
            "tag_directory_sha256": tag_digest,
            "source_path": str(source.resolve()),
            "execution_matches_completion_tag": execution_digest == tag_digest,
        }
    return (
        selection,
        [provenance[label] for label in selection["selected"]],
        lifecycle,
    )


def _absolute_path(value: Any, *, field: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise FreezeError(f"{field} must be a non-empty path string")
    path = Path(value)
    if not path.is_absolute():
        raise FreezeError(f"{field} must be absolute")
    return path


def _hex_digest(value: Any, *, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise FreezeError(f"{field} is invalid")
    return value


def _validate_repository_receipt(repository: Any) -> dict[str, Any]:
    if not isinstance(repository, dict):
        raise FreezeError("plan.repository must be an object")
    _exact_keys(repository, REPOSITORY_KEYS, field="plan.repository")
    _absolute_path(repository["root"], field="plan.repository.root")
    _hex_digest(
        repository["head_commit"], field="plan.repository.head_commit", pattern=HEX40_RE
    )
    _hex_digest(
        repository["main_commit"], field="plan.repository.main_commit", pattern=HEX40_RE
    )
    _hex_digest(
        repository["origin_main_commit"],
        field="plan.repository.origin_main_commit",
        pattern=HEX40_RE,
    )
    if len(
        {
            repository["head_commit"],
            repository["main_commit"],
            repository["origin_main_commit"],
        }
    ) != 1:
        raise FreezeError("plan repository refs were not synchronized")
    return repository


def _validate_lifecycle_receipt(lifecycle: Any) -> dict[str, Any]:
    if not isinstance(lifecycle, dict):
        raise FreezeError("plan.lifecycle must be an object")
    _exact_keys(lifecycle, LIFECYCLE_KEYS, field="plan.lifecycle")
    if lifecycle["registry_available"] is not True:
        raise FreezeError("plan.lifecycle.registry_available must be true")
    if lifecycle["registry_source"] != "durable_tags":
        raise FreezeError("plan.lifecycle.registry_source must be durable_tags")
    if lifecycle["migration_marker"] is not True:
        raise FreezeError("plan.lifecycle.migration_marker must be true")
    completion = _sorted_unique_ints(
        lifecycle["completion_versions"], field="plan.lifecycle.completion_versions"
    )
    annotated = _sorted_unique_ints(
        lifecycle["annotated_completion_versions"],
        field="plan.lifecycle.annotated_completion_versions",
    )
    active = _sorted_unique_ints(
        lifecycle["active_annotated_completion_versions"],
        field="plan.lifecycle.active_annotated_completion_versions",
    )
    reaped = _sorted_unique_ints(
        lifecycle["reaped_versions"], field="plan.lifecycle.reaped_versions"
    )
    _sorted_unique_ints(
        lifecycle["high_water_versions"], field="plan.lifecycle.high_water_versions"
    )
    if not set(annotated).issubset(completion):
        raise FreezeError("annotated completion versions are not completion versions")
    if active != sorted(set(annotated) - set(reaped)):
        raise FreezeError("active annotated lifecycle set is inconsistent")
    diagnostics = lifecycle["diagnostics"]
    if not isinstance(diagnostics, list) or any(
        not isinstance(item, str) for item in diagnostics
    ):
        raise FreezeError("plan.lifecycle.diagnostics must be a list of strings")
    refs = lifecycle["authority_tag_refs"]
    if not isinstance(refs, dict) or not refs:
        raise FreezeError("plan.lifecycle.authority_tag_refs must be a non-empty object")
    for name, receipt in refs.items():
        if not isinstance(name, str) or not (
            COMPLETION_TAG_RE.fullmatch(name)
            or REAPED_TAG_RE.fullmatch(name)
            or HIGH_WATER_TAG_RE.fullmatch(name)
            or name == MIGRATION_MARKER_TAG
        ):
            raise FreezeError("plan lifecycle contains a non-authority tag")
        if not isinstance(receipt, dict):
            raise FreezeError(f"authority tag receipt must be an object: {name}")
        _exact_keys(receipt, TAG_REF_KEYS, field=f"authority_tag_refs.{name}")
        if receipt["object_type"] not in {"tag", "commit"}:
            raise FreezeError(f"authority tag type changed: {name}")
        _hex_digest(
            receipt["object_oid"],
            field=f"authority_tag_refs.{name}.object_oid",
            pattern=HEX40_RE,
        )
        peeled = receipt["peeled_oid"]
        if receipt["object_type"] == "tag":
            _hex_digest(
                peeled,
                field=f"authority_tag_refs.{name}.peeled_oid",
                pattern=HEX40_RE,
            )
        elif peeled != "":
            raise FreezeError(f"lightweight authority tag unexpectedly peeled: {name}")
    annotated_from_refs = sorted(
        int(match.group(1))
        for name, receipt in refs.items()
        if (match := COMPLETION_TAG_RE.fullmatch(name)) is not None
        and receipt["object_type"] == "tag"
    )
    reaped_from_refs = sorted(
        int(match.group(1))
        for name in refs
        if (match := REAPED_TAG_RE.fullmatch(name)) is not None
    )
    high_water_from_refs = sorted(
        int(match.group(1))
        for name in refs
        if (match := HIGH_WATER_TAG_RE.fullmatch(name)) is not None
    )
    if annotated_from_refs != annotated:
        raise FreezeError("annotated completion versions differ from authority refs")
    if reaped_from_refs != reaped:
        raise FreezeError("reaped versions differ from authority refs")
    if high_water_from_refs != lifecycle["high_water_versions"]:
        raise FreezeError("high-water versions differ from authority refs")
    marker = refs.get(MIGRATION_MARKER_TAG)
    if not isinstance(marker, dict) or marker.get("object_type") != "tag":
        raise FreezeError("durable lifecycle marker receipt is absent")
    return lifecycle


def _validate_ratings_snapshot(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise FreezeError("ratings snapshot must be an object")
    _exact_keys(snapshot, RATINGS_SNAPSHOT_KEYS, field="ratings_snapshot")
    if snapshot["schema"] != RATINGS_SNAPSHOT_SCHEMA:
        raise FreezeError("ratings snapshot schema changed")
    source = _absolute_path(
        snapshot["source_path"], field="ratings_snapshot.source_path"
    )
    if source != _canonical_ratings_path():
        raise FreezeError("ratings snapshot is not from the canonical evolution path")
    if not isinstance(snapshot["raw_base64"], str):
        raise FreezeError("ratings raw_base64 must be a string")
    try:
        raw = base64.b64decode(snapshot["raw_base64"], validate=True)
    except (TypeError, ValueError) as exc:
        raise FreezeError("ratings raw_base64 is invalid") from exc
    if type(snapshot["bytes"]) is not int or snapshot["bytes"] < 0:
        raise FreezeError("ratings snapshot bytes must be a non-negative integer")
    _hex_digest(snapshot["sha256"], field="ratings_snapshot.sha256", pattern=HEX64_RE)
    fstat_receipt = snapshot["fstat"]
    if not isinstance(fstat_receipt, dict):
        raise FreezeError("ratings fstat receipt must be an object")
    _exact_keys(fstat_receipt, FSTAT_KEYS, field="ratings_snapshot.fstat")
    if any(type(fstat_receipt[key]) is not int or fstat_receipt[key] < 0 for key in FSTAT_KEYS):
        raise FreezeError("ratings fstat fields must be non-negative integers")
    if not stat.S_ISREG(fstat_receipt["st_mode"]):
        raise FreezeError("ratings fstat mode is not a regular file")
    if (
        snapshot["bytes"] != len(raw)
        or fstat_receipt["st_size"] != len(raw)
        or snapshot["sha256"] != hashlib.sha256(raw).hexdigest()
    ):
        raise FreezeError("ratings raw byte binding changed")
    raw_ratings = _strict_json_object(raw, source="ratings_snapshot.raw")
    _validate_ratings_payload(raw_ratings)
    embedded_ratings = snapshot["ratings"]
    if not isinstance(embedded_ratings, dict):
        raise FreezeError("ratings_snapshot.ratings must be an object")
    _validate_ratings_payload(embedded_ratings)
    if _canonical_bytes(raw_ratings) != _canonical_bytes(embedded_ratings):
        raise FreezeError("ratings raw snapshot binding changed")
    return raw_ratings


def _validate_candidate_artifact(candidate: Any) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise FreezeError("candidate artifact must be an object")
    _exact_keys(candidate, CANDIDATE_ARTIFACT_KEYS, field="candidate_artifact")
    label = candidate["label"]
    if not isinstance(label, str) or not label or Path(label).name != label:
        raise FreezeError("candidate artifact label is invalid")
    source = _absolute_path(candidate["source_path"], field="candidate.source_path")
    snapshot = _absolute_path(
        candidate["snapshot_path"], field="candidate.snapshot_path"
    )
    if source.name != label or snapshot.name != label:
        raise FreezeError("candidate artifact paths do not match its label")
    repository_version = candidate["repository_completed_version"]
    expected_version = _candidate_repository_version(source)
    if repository_version is not None and (
        type(repository_version) is not int or repository_version <= 0
    ):
        raise FreezeError("candidate repository completed version is invalid")
    if repository_version != expected_version:
        raise FreezeError("candidate repository identity receipt changed")
    source_digest = _hex_digest(
        candidate["source_directory_sha256"],
        field="candidate.source_directory_sha256",
        pattern=HEX64_RE,
    )
    snapshot_digest = _hex_digest(
        candidate["snapshot_directory_sha256"],
        field="candidate.snapshot_directory_sha256",
        pattern=HEX64_RE,
    )
    if source_digest != snapshot_digest:
        raise FreezeError("candidate source/snapshot digest mismatch")
    if candidate["native_entry"] != "national_bot.py":
        raise FreezeError("candidate native entry changed")
    return candidate


def _validate_opponent_artifacts(
    opponents: Any,
    *,
    selected: list[str],
) -> list[dict[str, Any]]:
    if not isinstance(opponents, list) or not opponents:
        raise FreezeError("strength pool has no opponent artifacts")
    labels: list[str] = []
    snapshot_paths: set[str] = set()
    for index, artifact in enumerate(opponents):
        if not isinstance(artifact, dict):
            raise FreezeError("opponent artifact must be an object")
        _exact_keys(artifact, OPPONENT_ARTIFACT_KEYS, field="opponent_artifact")
        label = artifact["label"]
        match = BOT_LABEL_RE.fullmatch(label) if isinstance(label, str) else None
        if match is None or type(artifact["version"]) is not int:
            raise FreezeError(f"opponent artifact {index} has an invalid identity")
        version = int(match.group(1))
        if artifact["version"] != version:
            raise FreezeError(f"opponent artifact {label} version differs from label")
        source = _absolute_path(
            artifact["source_path"], field=f"opponent_artifacts[{index}].source_path"
        )
        snapshot = _absolute_path(
            artifact["snapshot_path"],
            field=f"opponent_artifacts[{index}].snapshot_path",
        )
        if source.name != label or snapshot.name != label:
            raise FreezeError(f"opponent artifact paths do not match {label}")
        if str(snapshot) in snapshot_paths:
            raise FreezeError("opponent snapshot paths must be unique")
        snapshot_paths.add(str(snapshot))
        for field in (
            "snapshot_directory_sha256",
            "execution_directory_sha256",
            "tag_directory_sha256",
        ):
            _hex_digest(
                artifact[field], field=f"opponent {label} {field}", pattern=HEX64_RE
            )
        for field in (
            "execution_commit",
            "execution_tree_oid",
            "tag_object",
            "tag_commit",
            "tag_tree_oid",
        ):
            _hex_digest(
                artifact[field], field=f"opponent {label} {field}", pattern=HEX40_RE
            )
        if artifact["execution_directory_sha256"] != artifact["snapshot_directory_sha256"]:
            raise FreezeError("opponent execution/snapshot digest mismatch")
        if artifact["execution_matches_completion_tag"] is not (
            artifact["execution_directory_sha256"] == artifact["tag_directory_sha256"]
        ):
            raise FreezeError("opponent execution/tag equality receipt changed")
        if artifact["completion_tag"] != f"national-bot-v{version}":
            raise FreezeError(f"opponent {label} completion tag is inconsistent")
        if artifact["native_entry"] != "national_bot.py":
            raise FreezeError(f"opponent {label} native entry changed")
        labels.append(label)
    if labels != selected or len(labels) != len(set(labels)):
        raise FreezeError("selected opponent order differs from artifacts")
    return opponents


def validate_plan(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise FreezeError("strength pool plan must be an object")
    _exact_keys(payload, PLAN_ROOT_KEYS, field="plan")
    if payload.get("schema") != SCHEMA:
        raise FreezeError("strength pool plan schema changed")
    observed_hash = payload.get("payload_sha256")
    if not isinstance(observed_hash, str) or HEX64_RE.fullmatch(observed_hash) is None:
        raise FreezeError("strength pool plan payload_sha256 is invalid")
    if plan_payload_sha256(payload) != observed_hash:
        raise FreezeError("strength pool plan self-hash changed")
    repository = _validate_repository_receipt(payload["repository"])
    lifecycle = _validate_lifecycle_receipt(payload["lifecycle"])
    for field, expected in FALSE_AUTHORITY.items():
        if payload.get(field) != expected or type(payload.get(field)) is not type(expected):
            raise FreezeError(f"plan.{field} must equal {expected!r}")
    if payload.get("deck_seed_scheme") != DECK_SEED_SCHEME:
        raise FreezeError("strength pool deck seed scheme changed")
    if payload.get("hands_per_leg") != HANDS_PER_LEG or payload.get("paired") is not True:
        raise FreezeError("strength pool requires paired 70-hand legs")
    if payload.get("minimum_seed_blocks_per_opponent") != MINIMUM_SEED_BLOCKS:
        raise FreezeError("minimum seed-block contract changed")
    seeds = payload["seeds"]
    if not isinstance(seeds, list) or len(seeds) < MINIMUM_SEED_BLOCKS or any(
        type(seed) is not int or seed < 0 for seed in seeds
    ):
        raise FreezeError("strength pool seeds are invalid")
    _validate_positive_int(payload.get("workers"), field="workers")
    if int(payload["workers"]) > 4:
        raise FreezeError("workers must be in the inclusive range 1..4")
    if payload.get("runtime_contract") != native_strength_runtime_contract():
        raise FreezeError("native strength runtime contract changed")
    bootstrap_samples = _validate_positive_int(
        payload.get("bootstrap_samples"),
        field="bootstrap_samples",
        minimum=MINIMUM_BOOTSTRAP_SAMPLES,
    )
    if bootstrap_samples > MAXIMUM_BOOTSTRAP_SAMPLES:
        raise FreezeError(
            f"bootstrap_samples must not exceed {MAXIMUM_BOOTSTRAP_SAMPLES}"
        )
    if (
        type(payload.get("bootstrap_seed")) is not int
        or payload["bootstrap_seed"] < 0
    ):
        raise FreezeError("bootstrap_seed must be a non-negative integer")
    candidate = _validate_candidate_artifact(payload["candidate_artifact"])
    candidate_version = candidate["repository_completed_version"]
    if candidate_version is not None:
        label = f"national_v{candidate_version}"
        _, execution_digest = _tag_directory_identity(
            label, repository["origin_main_commit"]
        )
        if candidate["snapshot_directory_sha256"] != execution_digest:
            raise FreezeError(
                "repository candidate snapshot differs from its mainline completed tree"
            )
    ratings = _validate_ratings_snapshot(payload["ratings_snapshot"])
    selection = payload["selection"]
    if not isinstance(selection, dict):
        raise FreezeError("strength pool selection must be an object")
    _exact_keys(selection, SELECTION_KEYS, field="plan.selection")
    pool_size = _validate_positive_int(
        selection["pool_size"], field="plan.selection.pool_size", minimum=MINIMUM_POOL_SIZE
    )
    expected_selection = _recompute_selection(
        ratings,
        lifecycle,
        pool_size=pool_size,
        candidate_repository_version=candidate["repository_completed_version"],
    )
    if _canonical_bytes(selection) != _canonical_bytes(expected_selection):
        raise FreezeError("strength pool ranking/selection cannot be recomputed")
    for index, row in enumerate(selection["ranking"]):
        if not isinstance(row, dict):
            raise FreezeError(f"selection ranking row {index} must be an object")
        _exact_keys(row, RANKING_ROW_KEYS, field=f"selection.ranking[{index}]")
    selected = selection["selected"]
    if not isinstance(selected, list) or any(not isinstance(label, str) for label in selected):
        raise FreezeError("selection.selected must be a list of labels")
    opponents = _validate_opponent_artifacts(
        payload["opponent_artifacts"], selected=selected
    )
    if len(opponents) != pool_size:
        raise FreezeError("opponent artifact count differs from selection pool size")
    seed_stride = _validate_positive_int(
        payload["seeds"][1] - payload["seeds"][0], field="seed_stride"
    )
    opponent_seed_stride = _validate_positive_int(
        payload["opponent_seed_stride"], field="opponent_seed_stride"
    )
    bot_seed_base = _validate_positive_int(
        payload["bot_seed_base"], field="bot_seed_base", minimum=0
    )
    bot_seed_stride = _validate_positive_int(
        payload["bot_seed_stride"], field="bot_seed_stride"
    )
    if (
        type(payload["bot_opponent_seed_stride"]) is not int
        or payload["bot_opponent_seed_stride"] != BOT_OPPONENT_SEED_STRIDE
    ):
        raise FreezeError("bot opponent seed stride contract changed")
    expected_seeds, expected_actual = _seed_contract(
        opponents=len(opponents),
        seed_blocks=len(seeds),
        seed_base=seeds[0],
        seed_stride=seed_stride,
        opponent_seed_stride=opponent_seed_stride,
        bot_seed_base=bot_seed_base,
        bot_seed_stride=bot_seed_stride,
    )
    if seeds != expected_seeds:
        raise FreezeError("strength pool seeds are not an arithmetic sequence")
    actual = payload["actual_deck_seed_bases"]
    if (
        not isinstance(actual, list)
        or any(type(seed) is not int or seed < 0 for seed in actual)
        or actual != expected_actual
    ):
        raise FreezeError("actual deck seed bases differ from plan")
    code_artifacts = payload["code_artifacts"]
    if not isinstance(code_artifacts, dict) or not code_artifacts:
        raise FreezeError("code_artifacts must be a non-empty object")
    for path, digest in code_artifacts.items():
        if (
            not isinstance(path, str)
            or not path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
        ):
            raise FreezeError("code artifact path is not repository-relative")
        _hex_digest(digest, field=f"code_artifacts.{path}", pattern=HEX64_RE)
    return payload


def _validate_frozen_output_tree(
    root: Path,
    *,
    final_root: Path,
    payload: dict[str, Any],
    raw_plan: bytes,
) -> None:
    _validate_frozen_output_tree_impl(
        root,
        final_root=final_root,
        payload=payload,
        raw_plan=raw_plan,
        plan_filename=PLAN_FILENAME,
    )


def validate_v4_native_strength_pool_plan_bytes(
    raw: bytes,
    require_snapshots: bool = True,
) -> dict[str, Any]:
    """Strictly validate raw plan bytes and their live immutable bindings."""

    if type(raw) is not bytes:
        raise FreezeError("strength pool plan input must be bytes")
    payload = validate_plan(_strict_json_object(raw, source="strength_pool_plan"))
    if Path(payload["repository"]["root"]) != ROOT.resolve():
        raise FreezeError("strength pool plan belongs to a different repository")

    completion_tags = _completion_tags()
    current_authority_refs = _authority_tag_refs()
    for name, receipt in payload["lifecycle"]["authority_tag_refs"].items():
        if current_authority_refs.get(name) != receipt:
            raise FreezeError(
                f"recorded lifecycle authority tag disappeared or moved: {name}"
            )
    if _canonical_bytes(payload["code_artifacts"]) != _canonical_bytes(_code_artifacts()):
        raise FreezeError("strength evaluator/tool code differs from the frozen receipt")

    for artifact in payload["opponent_artifacts"]:
        version = artifact["version"]
        live_tag = completion_tags.get(version)
        if live_tag is None:
            raise FreezeError(f"selected completion tag disappeared: national-bot-v{version}")
        for field in ("completion_tag", "tag_object", "tag_commit"):
            if artifact[field] != live_tag[field]:
                raise FreezeError(
                    f"selected completion tag binding changed for {artifact['label']}"
                )
        tree_oid, tree_digest = _tag_directory_identity(
            artifact["label"], live_tag["tag_commit"]
        )
        if (
            artifact["tag_tree_oid"] != tree_oid
            or artifact["tag_directory_sha256"] != tree_digest
        ):
            raise FreezeError(f"selected completion tree changed for {artifact['label']}")
        execution_tree_oid, execution_digest = _tag_directory_identity(
            artifact["label"], artifact["execution_commit"]
        )
        if (
            artifact["execution_commit"] != payload["repository"]["origin_main_commit"]
            or artifact["execution_tree_oid"] != execution_tree_oid
            or artifact["execution_directory_sha256"] != execution_digest
        ):
            raise FreezeError(
                f"selected mainline execution tree changed for {artifact['label']}"
            )

    if require_snapshots:
        candidate_snapshot = Path(payload["candidate_artifact"]["snapshot_path"])
        if len(candidate_snapshot.parents) < 3:
            raise FreezeError("candidate snapshot path is too shallow")
        output = candidate_snapshot.parents[2]
        _validate_frozen_output_tree(
            output, final_root=output, payload=payload, raw_plan=raw
        )
    return payload


def freeze_strength_pool(args: argparse.Namespace) -> dict[str, Any]:
    pool_size = _validate_positive_int(
        args.pool_size, field="pool_size", minimum=MINIMUM_POOL_SIZE
    )
    seed_blocks = _validate_positive_int(
        args.seed_blocks,
        field="seed_blocks",
        minimum=MINIMUM_SEED_BLOCKS,
    )
    workers = _validate_positive_int(args.workers, field="workers")
    if workers > 4:
        raise FreezeError("workers must be in the inclusive range 1..4")
    bootstrap_samples = _validate_positive_int(
        args.bootstrap_samples,
        field="bootstrap_samples",
        minimum=MINIMUM_BOOTSTRAP_SAMPLES,
    )
    if bootstrap_samples > MAXIMUM_BOOTSTRAP_SAMPLES:
        raise FreezeError(
            f"bootstrap_samples must not exceed {MAXIMUM_BOOTSTRAP_SAMPLES}"
        )
    if type(args.bootstrap_seed) is not int or args.bootstrap_seed < 0:
        raise FreezeError("bootstrap_seed must be a non-negative integer")
    candidate = _resolve_candidate(Path(args.candidate))
    ratings_snapshot = read_ratings_snapshot(Path(args.ratings))
    repository_receipt = _repository_receipt()
    code_artifact_receipt = _code_artifacts()
    selection, selected_provenance, lifecycle = _selection_and_provenance(
        ratings_snapshot["ratings"],
        pool_size=pool_size,
        candidate=candidate,
        execution_commit=repository_receipt["origin_main_commit"],
    )
    seeds, actual_deck_seeds = _seed_contract(
        opponents=len(selected_provenance),
        seed_blocks=seed_blocks,
        seed_base=args.seed_base,
        seed_stride=args.seed_stride,
        opponent_seed_stride=args.opponent_seed_stride,
        bot_seed_base=args.bot_seed_base,
        bot_seed_stride=args.bot_seed_stride,
    )

    output = Path(args.out_dir).expanduser()
    output = output if output.is_absolute() else ROOT / output
    output = Path(os.path.abspath(os.fspath(output)))
    if output.exists() or output.is_symlink():
        raise FreezeError(f"output directory already exists: {output}")
    frozen_sources = [
        candidate,
        *(Path(row["source_path"]) for row in selected_provenance),
    ]
    if any(output.is_relative_to(source) for source in frozen_sources):
        raise FreezeError("output directory must not be inside a frozen source tree")
    _mkdir_parents_fsync(output.parent)
    temporary: Path | None = None
    staging_identity: tuple[int, int] | None = None
    try:
        temporary = output.parent / (
            f".{output.name}.freeze-{os.getpid()}-{secrets.token_hex(16)}"
        )
        os.mkdir(temporary, 0o700)
        _fsync_directory(output.parent)
        staging_identity = _directory_identity(temporary)
        candidate_temp = temporary / "snapshots" / "candidate" / candidate.name
        candidate_digest = _copy_tree_snapshot(candidate, candidate_temp)
        candidate_final = output / candidate_temp.relative_to(temporary)
        candidate_artifact = {
            "label": candidate.name,
            "repository_completed_version": _candidate_repository_version(candidate),
            "source_path": str(candidate),
            "source_directory_sha256": candidate_digest,
            "snapshot_path": str(candidate_final),
            "snapshot_directory_sha256": candidate_digest,
            "native_entry": "national_bot.py",
        }

        opponent_artifacts = []
        for provenance in selected_provenance:
            label = str(Path(provenance["source_path"]).name)
            source = Path(provenance["source_path"])
            snapshot_temp = temporary / "snapshots" / "opponents" / label
            copied_tree_oid, copied_digest = _copy_git_tree_snapshot(
                provenance["execution_commit"], label, snapshot_temp
            )
            if (
                copied_tree_oid != provenance["execution_tree_oid"]
                or copied_digest != provenance["execution_directory_sha256"]
            ):
                raise FreezeError(f"opponent Git tree changed after selection: {label}")
            snapshot_final = output / snapshot_temp.relative_to(temporary)
            opponent_artifacts.append({
                "label": label,
                "version": int(BOT_LABEL_RE.fullmatch(label).group(1)),
                "source_path": str(source),
                "snapshot_path": str(snapshot_final),
                "snapshot_directory_sha256": copied_digest,
                "native_entry": "national_bot.py",
                "execution_commit": provenance["execution_commit"],
                "execution_tree_oid": provenance["execution_tree_oid"],
                "execution_directory_sha256": provenance[
                    "execution_directory_sha256"
                ],
                "completion_tag": provenance["completion_tag"],
                "tag_object": provenance["tag_object"],
                "tag_commit": provenance["tag_commit"],
                "tag_tree_oid": provenance["tag_tree_oid"],
                "tag_directory_sha256": provenance["tag_directory_sha256"],
                "execution_matches_completion_tag": provenance[
                    "execution_matches_completion_tag"
                ],
            })

        payload: dict[str, Any] = {
            "schema": SCHEMA,
            "repository": repository_receipt,
            "lifecycle": lifecycle,
            "ratings_snapshot": ratings_snapshot,
            "candidate_artifact": candidate_artifact,
            "opponent_artifacts": opponent_artifacts,
            "seeds": seeds,
            "actual_deck_seed_bases": actual_deck_seeds,
            "deck_seed_scheme": DECK_SEED_SCHEME,
            "opponent_seed_stride": args.opponent_seed_stride,
            "bot_seed_base": args.bot_seed_base,
            "bot_seed_stride": args.bot_seed_stride,
            "bot_opponent_seed_stride": BOT_OPPONENT_SEED_STRIDE,
            "hands_per_leg": HANDS_PER_LEG,
            "paired": True,
            "minimum_seed_blocks_per_opponent": MINIMUM_SEED_BLOCKS,
            "workers": workers,
            "runtime_contract": native_strength_runtime_contract(),
            "bootstrap_samples": bootstrap_samples,
            "bootstrap_seed": args.bootstrap_seed,
            "selection": selection,
            "code_artifacts": code_artifact_receipt,
            **FALSE_AUTHORITY,
        }
        payload["payload_sha256"] = plan_payload_sha256(payload)
        validate_plan(payload)
        _write_json_fsync(temporary / PLAN_FILENAME, payload)
        persisted_raw = (temporary / PLAN_FILENAME).read_bytes()
        persisted = _strict_json_object(persisted_raw, source=PLAN_FILENAME)
        validate_plan(persisted)
        if persisted != payload:
            raise FreezeError("persisted strength pool plan changed")

        _fsync_tree(temporary)
        _make_read_only(temporary)
        _assert_read_only_tree(temporary)
        _fsync_tree(temporary)
        _validate_frozen_output_tree(
            temporary,
            final_root=output,
            payload=payload,
            raw_plan=persisted_raw,
        )
        # Last mutable-input sandwich comes after all potentially slow staging
        # scans and immediately before the no-replace rename.
        ratings_after = read_ratings_snapshot(Path(args.ratings))
        if _canonical_bytes(ratings_after) != _canonical_bytes(ratings_snapshot):
            raise FreezeError("ratings snapshot drifted before atomic publication")
        selection_after, provenance_after, lifecycle_after = _selection_and_provenance(
            ratings_after["ratings"],
            pool_size=pool_size,
            candidate=candidate,
            execution_commit=repository_receipt["origin_main_commit"],
        )
        if (
            _canonical_bytes(selection_after) != _canonical_bytes(selection)
            or _canonical_bytes(provenance_after) != _canonical_bytes(selected_provenance)
            or _canonical_bytes(lifecycle_after) != _canonical_bytes(lifecycle)
        ):
            raise FreezeError("completion/lifecycle selection drifted before publication")
        if _canonical_tree_digest(candidate) != candidate_digest:
            raise FreezeError("candidate changed before atomic publication")
        if repository_receipt != _repository_receipt():
            raise FreezeError("repository refs drifted before atomic publication")
        if code_artifact_receipt != _code_artifacts():
            raise FreezeError("strength evaluator/tool code drifted before publication")
        _publish_tree_noreplace(temporary, output)
        if not _path_matches_directory_identity(output, staging_identity):
            raise FreezeError("published output identity changed during rename")
        _fsync_directory(output.parent)
        return payload
    except BaseException as exc:
        try:
            if temporary is None:
                raise FreezeError("freeze failed before staging directory creation")
            if staging_identity is None:
                _remove_tree(temporary)
            else:
                cleanup = next(
                    (
                        path
                        for path in (temporary, output)
                        if _path_matches_directory_identity(path, staging_identity)
                    ),
                    None,
                )
                if cleanup is None:
                    raise FreezeError("staged artifact identity disappeared during cleanup")
                _remove_tree(cleanup, expected_identity=staging_identity)
        except Exception as cleanup_exc:
            raise FreezeError(
                f"freeze failed and cleanup also failed: {cleanup_exc}"
            ) from exc
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Freeze a strict dynamic native v4 strength opponent pool."
    )
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--ratings", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--pool-size", type=int, default=DEFAULT_POOL_SIZE)
    parser.add_argument("--seed-blocks", type=int, default=DEFAULT_SEED_BLOCKS)
    parser.add_argument("--seed-base", type=int, default=DEFAULT_SEED_BASE)
    parser.add_argument("--seed-stride", type=int, default=DEFAULT_SEED_STRIDE)
    parser.add_argument(
        "--opponent-seed-stride",
        type=int,
        default=DEFAULT_OPPONENT_SEED_STRIDE,
    )
    parser.add_argument("--bot-seed-base", type=int, default=DEFAULT_BOT_SEED_BASE)
    parser.add_argument("--bot-seed-stride", type=int, default=DEFAULT_BOT_SEED_STRIDE)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--bootstrap-samples", type=int, default=DEFAULT_BOOTSTRAP_SAMPLES
    )
    parser.add_argument("--bootstrap-seed", type=int, default=DEFAULT_BOOTSTRAP_SEED)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        payload = freeze_strength_pool(args)
    except (FreezeError, OSError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
