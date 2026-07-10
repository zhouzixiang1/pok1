"""Durable Git-backed state for the national-native evolution epoch.

The historical reaped-bot ledger is runtime-local JSONL.  It is useful as a
migration source, but it cannot remain authoritative because a fresh clone can
silently lose it.  This module moves the durable facts into annotated Git tags:

* ``national-reaped-vN`` records a permanent active-pool tombstone.
* ``national-reaped-registry-v1`` marks completion of the ledger migration.
* ``national-high-water-vN`` records a monotonic consumed version number.

Until the migration marker exists, a missing, empty, or malformed legacy ledger
is explicitly unavailable.  After migration, durable tags are authoritative and
the ledger is read only to produce diagnostics.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence


COMPLETION_TAG_PREFIX = "national-bot-v"
REAPED_TAG_PREFIX = "national-reaped-v"
HIGH_WATER_TAG_PREFIX = "national-high-water-v"
MIGRATION_MARKER_TAG = "national-reaped-registry-v1"

_COMPLETION_RE = re.compile(r"^national-bot-v([1-9][0-9]*)$")
_REAPED_RE = re.compile(r"^national-reaped-v([1-9][0-9]*)$")
_HIGH_WATER_RE = re.compile(r"^national-high-water-v([1-9][0-9]*)$")
_BOT_NAME_RE = re.compile(r"^national_v([1-9][0-9]*)$")
_BOT_HISTORY_RE = re.compile(r"^bots/national_v([1-9][0-9]*)(?:/.*)?$")
_OID_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")

DEFAULT_LEGACY_LEDGER = Path(__file__).resolve().parent / "results" / "reaped_bots.jsonl"


class RegistryError(RuntimeError):
    """Base class for registry failures."""


class RegistryUnavailableError(RegistryError):
    """Raised when neither durable tags nor a healthy legacy ledger are usable."""


class MigrationError(RegistryError):
    """Raised when a migration cannot be applied atomically."""


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class GitRunner(Protocol):
    """Injectable command boundary used by production and tests."""

    def __call__(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        input_text: str | None = None,
    ) -> GitResult: ...


def subprocess_git_runner(
    args: Sequence[str],
    *,
    cwd: Path,
    input_text: str | None = None,
) -> GitResult:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    return GitResult(proc.returncode, proc.stdout, proc.stderr)


class GitRepository:
    """Small Git facade whose runner can be replaced in unit tests."""

    def __init__(self, root: Path | str, runner: GitRunner | None = None):
        self.root = Path(root).resolve()
        self.runner = runner or subprocess_git_runner

    def run(
        self,
        *args: str,
        input_text: str | None = None,
        check: bool = True,
    ) -> GitResult:
        result = self.runner(tuple(args), cwd=self.root, input_text=input_text)
        if check and result.returncode != 0:
            command = "git " + " ".join(args)
            detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
            raise RegistryError(f"{command} failed: {detail}")
        return result


@dataclass(frozen=True)
class LegacyLedgerState:
    path: Path
    exists: bool
    available: bool
    versions: frozenset[int] = frozenset()
    entry_count: int = 0
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class TagRecord:
    name: str
    object_type: str
    object_oid: str
    peeled_oid: str = ""

    @property
    def annotated(self) -> bool:
        return self.object_type == "tag" and bool(self.peeled_oid)

    @property
    def target_oid(self) -> str:
        return self.peeled_oid if self.annotated else self.object_oid


@dataclass(frozen=True)
class RegistryState:
    available: bool
    source: str
    reaped_versions: frozenset[int]
    migration_marker: bool
    completion_versions: frozenset[int]
    high_water_versions: frozenset[int]
    history_high_water: int | None
    legacy: LegacyLedgerState
    diagnostics: tuple[str, ...] = ()

    def require_reaped_versions(self) -> frozenset[int]:
        if not self.available:
            detail = "; ".join(self.diagnostics) or "registry state is unavailable"
            raise RegistryUnavailableError(detail)
        return self.reaped_versions


@dataclass(frozen=True)
class TagSpec:
    name: str
    target_oid: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "target_oid": self.target_oid,
            "message": self.message,
        }


@dataclass(frozen=True)
class MigrationPlan:
    ready: bool
    already_migrated: bool
    legacy_entry_count: int
    legacy_versions: tuple[int, ...]
    unmappable_versions: tuple[int, ...]
    effective_high_water: int | None
    create_tags: tuple[TagSpec, ...]
    required_tags: tuple[str, ...]
    completion_preconditions: tuple[tuple[str, str], ...] = ()
    diagnostics: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "already_migrated": self.already_migrated,
            "legacy_entry_count": self.legacy_entry_count,
            "legacy_versions": list(self.legacy_versions),
            "unmappable_versions": list(self.unmappable_versions),
            "effective_high_water": self.effective_high_water,
            "create_tags": [tag.as_dict() for tag in self.create_tags],
            "required_tags": list(self.required_tags),
            "completion_preconditions": [list(item) for item in self.completion_preconditions],
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True)
class TagMutationResult:
    created_tags: tuple[str, ...]
    head_before: str
    head_after: str
    pushed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "created_tags": list(self.created_tags),
            "head_before": self.head_before,
            "head_after": self.head_after,
            "head_unchanged": self.head_before == self.head_after,
            "pushed": self.pushed,
        }


def _positive_version(value: object) -> int | None:
    if type(value) is not int or value <= 0:
        return None
    return value


def parse_legacy_ledger(path: Path | str) -> LegacyLedgerState:
    """Parse the runtime JSONL ledger without accepting partial state."""

    ledger_path = Path(path)
    try:
        text = ledger_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return LegacyLedgerState(
            path=ledger_path,
            exists=False,
            available=False,
            diagnostics=("legacy_ledger_missing",),
        )
    except (OSError, UnicodeError) as exc:
        return LegacyLedgerState(
            path=ledger_path,
            exists=ledger_path.exists(),
            available=False,
            diagnostics=(f"legacy_ledger_unreadable:{type(exc).__name__}",),
        )

    lines = text.splitlines()
    if not lines:
        return LegacyLedgerState(
            path=ledger_path,
            exists=True,
            available=False,
            diagnostics=("legacy_ledger_empty",),
        )

    versions: set[int] = set()
    diagnostics: list[str] = []
    parsed_entries = 0
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            diagnostics.append(f"line_{line_number}:blank_line")
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            diagnostics.append(f"line_{line_number}:invalid_json:{exc.msg}")
            continue
        if not isinstance(entry, dict):
            diagnostics.append(f"line_{line_number}:entry_not_object")
            continue

        explicit_version = entry.get("version") if "version" in entry else None
        version = _positive_version(explicit_version) if "version" in entry else None
        if "version" in entry and version is None:
            diagnostics.append(f"line_{line_number}:invalid_version")
            continue

        bot_version: int | None = None
        if "bot" in entry:
            bot = entry.get("bot")
            match = _BOT_NAME_RE.fullmatch(bot) if isinstance(bot, str) else None
            if match is None:
                diagnostics.append(f"line_{line_number}:invalid_bot_name")
                continue
            bot_version = int(match.group(1))

        if version is None and bot_version is None:
            diagnostics.append(f"line_{line_number}:missing_version_and_bot")
            continue
        if version is not None and bot_version is not None and version != bot_version:
            diagnostics.append(f"line_{line_number}:version_bot_mismatch")
            continue

        versions.add(version if version is not None else bot_version)  # type: ignore[arg-type]
        parsed_entries += 1

    if diagnostics:
        return LegacyLedgerState(
            path=ledger_path,
            exists=True,
            available=False,
            entry_count=parsed_entries,
            diagnostics=tuple(diagnostics),
        )
    return LegacyLedgerState(
        path=ledger_path,
        exists=True,
        available=True,
        versions=frozenset(versions),
        entry_count=parsed_entries,
    )


def _scan_tags(git: GitRepository) -> dict[str, TagRecord]:
    fmt = "%(refname:short)\t%(objecttype)\t%(objectname)\t%(*objectname)"
    output = git.run("for-each-ref", f"--format={fmt}", "refs/tags").stdout
    records: dict[str, TagRecord] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            raise RegistryError(f"unexpected for-each-ref output: {line!r}")
        name, object_type, object_oid, peeled_oid = parts
        records[name] = TagRecord(name, object_type, object_oid, peeled_oid)
    return records


def _version_set(records: Iterable[str], pattern: re.Pattern[str]) -> frozenset[int]:
    return frozenset(int(match.group(1)) for name in records if (match := pattern.fullmatch(name)))


def git_history_high_water(git: GitRepository) -> int | None:
    """Return the largest national bot version visible in reachable Git history."""

    result = git.run(
        "log",
        "--all",
        "--name-only",
        "--format=",
        "--",
        "bots",
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown git log error"
        raise RegistryError(f"cannot inspect national bot history: {detail}")
    versions = {
        int(match.group(1))
        for raw_line in result.stdout.splitlines()
        if (match := _BOT_HISTORY_RE.fullmatch(raw_line.strip()))
    }
    return max(versions) if versions else None


def load_registry_state(
    repo_root: Path | str,
    *,
    legacy_ledger: Path | str = DEFAULT_LEGACY_LEDGER,
    runner: GitRunner | None = None,
    include_history: bool = True,
) -> RegistryState:
    """Load durable/legacy state while preserving fail-closed availability."""

    git = GitRepository(repo_root, runner)
    tags = _scan_tags(git)
    legacy = parse_legacy_ledger(legacy_ledger)
    diagnostics: list[str] = []

    completion_versions = _version_set(tags, _COMPLETION_RE)
    high_water_versions = _version_set(tags, _HIGH_WATER_RE)
    marker_record = tags.get(MIGRATION_MARKER_TAG)
    marker_valid = bool(marker_record and marker_record.annotated)
    if marker_record and not marker_record.annotated:
        diagnostics.append("migration_marker_not_annotated")

    invalid_registry_tags: list[str] = []
    durable_reaped: set[int] = set()
    for name, record in tags.items():
        reaped_match = _REAPED_RE.fullmatch(name)
        high_water_match = _HIGH_WATER_RE.fullmatch(name)
        if reaped_match:
            if record.annotated:
                durable_reaped.add(int(reaped_match.group(1)))
            else:
                invalid_registry_tags.append(name)
        elif high_water_match:
            if not record.annotated:
                invalid_registry_tags.append(name)
        elif name.startswith(REAPED_TAG_PREFIX) or name.startswith(HIGH_WATER_TAG_PREFIX):
            invalid_registry_tags.append(name)
    if invalid_registry_tags:
        diagnostics.append("invalid_registry_tags:" + ",".join(sorted(invalid_registry_tags)))

    history_high_water = git_history_high_water(git) if include_history else None
    if marker_valid:
        if not legacy.available:
            diagnostics.extend(f"legacy_diagnostic:{item}" for item in legacy.diagnostics)
        elif legacy.versions != frozenset(durable_reaped):
            diagnostics.append("legacy_diagnostic:legacy_differs_from_durable_tags")
        available = not invalid_registry_tags
        source = "durable_tags" if available else "unavailable"
        reaped_versions = frozenset(durable_reaped) if available else frozenset()
    else:
        diagnostics.extend(legacy.diagnostics)
        available = legacy.available
        source = "legacy_ledger" if available else "unavailable"
        reaped_versions = legacy.versions if available else frozenset()

    return RegistryState(
        available=available,
        source=source,
        reaped_versions=reaped_versions,
        migration_marker=marker_valid,
        completion_versions=completion_versions,
        high_water_versions=high_water_versions,
        history_high_water=history_high_water,
        legacy=legacy,
        diagnostics=tuple(diagnostics),
    )


def effective_target_version(
    requested: int,
    *,
    repo_root: Path | str,
    legacy_ledger: Path | str = DEFAULT_LEGACY_LEDGER,
    runner: GitRunner | None = None,
    state: RegistryState | None = None,
) -> int:
    """Return a target that cannot decrease when completion refs are deleted."""

    requested_version = _positive_version(requested)
    if requested_version is None:
        raise ValueError("requested target version must be a positive integer")
    current = state or load_registry_state(
        repo_root,
        legacy_ledger=legacy_ledger,
        runner=runner,
        include_history=True,
    )
    candidates = [requested_version]
    if current.completion_versions:
        candidates.append(max(current.completion_versions) + 1)
    if current.high_water_versions:
        candidates.append(max(current.high_water_versions) + 1)
    if current.history_high_water is not None:
        candidates.append(current.history_high_water + 1)
    return max(candidates)


def _resolve_commit(git: GitRepository, revision: str) -> str | None:
    result = git.run("rev-parse", "--verify", f"{revision}^{{commit}}", check=False)
    oid = result.stdout.strip()
    if result.returncode != 0 or not _OID_RE.fullmatch(oid):
        return None
    return oid.lower()


def _history_commit_for_version(git: GitRepository, version: int) -> str | None:
    result = git.run(
        "log",
        "--all",
        "-n",
        "1",
        "--format=%H",
        "--",
        f"bots/national_v{version}",
        check=False,
    )
    oid = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    return oid.lower() if result.returncode == 0 and _OID_RE.fullmatch(oid) else None


def _target_for_high_water(git: GitRepository, version: int) -> str:
    completion = _resolve_commit(git, f"refs/tags/{COMPLETION_TAG_PREFIX}{version}")
    history = _history_commit_for_version(git, version)
    head = _resolve_commit(git, "HEAD")
    target = completion or history or head
    if target is None:
        raise MigrationError("cannot resolve a commit for the high-water tag")
    return target


def build_migration_plan(
    repo_root: Path | str,
    *,
    legacy_ledger: Path | str = DEFAULT_LEGACY_LEDGER,
    runner: GitRunner | None = None,
) -> MigrationPlan:
    """Preflight every legacy entry before proposing any ref mutation."""

    git = GitRepository(repo_root, runner)
    state = load_registry_state(
        repo_root,
        legacy_ledger=legacy_ledger,
        runner=runner,
        include_history=True,
    )
    tags = _scan_tags(git)
    diagnostics = list(state.diagnostics)

    observed = set(state.completion_versions) | set(state.high_water_versions)
    if state.history_high_water is not None:
        observed.add(state.history_high_water)
    effective_high_water = max(observed) if observed else None

    if state.migration_marker:
        specs: list[TagSpec] = []
        required_tags = (
            [MIGRATION_MARKER_TAG]
            + [f"{REAPED_TAG_PREFIX}{version}" for version in state.reaped_versions]
            + [f"{HIGH_WATER_TAG_PREFIX}{version}" for version in state.high_water_versions]
        )
        if effective_high_water is not None:
            high_water_name = f"{HIGH_WATER_TAG_PREFIX}{effective_high_water}"
            required_tags.append(high_water_name)
            if high_water_name not in tags:
                specs.append(
                    TagSpec(
                        high_water_name,
                        _target_for_high_water(git, effective_high_water),
                        f"National epoch monotonic high-water v{effective_high_water}",
                    )
                )
        return MigrationPlan(
            ready=state.available,
            already_migrated=True,
            legacy_entry_count=state.legacy.entry_count,
            legacy_versions=tuple(sorted(state.legacy.versions)),
            unmappable_versions=(),
            effective_high_water=effective_high_water,
            create_tags=tuple(specs) if state.available else (),
            required_tags=tuple(sorted(set(required_tags))) if state.available else (),
            diagnostics=tuple(diagnostics),
        )

    if not state.legacy.available:
        return MigrationPlan(
            ready=False,
            already_migrated=False,
            legacy_entry_count=state.legacy.entry_count,
            legacy_versions=(),
            unmappable_versions=(),
            effective_high_water=effective_high_water,
            create_tags=(),
            required_tags=(),
            diagnostics=tuple(diagnostics),
        )

    mappings: dict[int, str] = {}
    unmappable: list[int] = []
    conflicts: list[str] = []
    specs: list[TagSpec] = []
    preconditions: list[tuple[str, str]] = []
    required: list[str] = []

    for version in sorted(state.legacy.versions):
        completion_name = f"{COMPLETION_TAG_PREFIX}{version}"
        completion_ref = f"refs/tags/{COMPLETION_TAG_PREFIX}{version}"
        completion_record = tags.get(completion_name)
        completion_oid = _resolve_commit(git, completion_ref)
        if completion_record is None or not completion_record.annotated or completion_oid is None:
            unmappable.append(version)
            continue
        mappings[version] = completion_oid
        preconditions.append((completion_ref, completion_oid))
        tombstone_name = f"{REAPED_TAG_PREFIX}{version}"
        required.append(tombstone_name)
        existing = tags.get(tombstone_name)
        if existing is None:
            specs.append(
                TagSpec(
                    tombstone_name,
                    completion_oid,
                    f"National epoch durable reaped tombstone for v{version}",
                )
            )
        elif not existing.annotated or existing.target_oid.lower() != completion_oid:
            conflicts.append(f"conflicting_tombstone:{tombstone_name}")

    if unmappable:
        diagnostics.append("unmappable_legacy_versions:" + ",".join(map(str, unmappable)))
    if conflicts:
        diagnostics.extend(conflicts)

    if effective_high_water is not None:
        high_water_name = f"{HIGH_WATER_TAG_PREFIX}{effective_high_water}"
        required.append(high_water_name)
        existing_high_water = tags.get(high_water_name)
        if existing_high_water is None:
            specs.append(
                TagSpec(
                    high_water_name,
                    _target_for_high_water(git, effective_high_water),
                    f"National epoch monotonic high-water v{effective_high_water}",
                )
            )
        elif not existing_high_water.annotated:
            conflicts.append(f"conflicting_high_water:{high_water_name}")
            diagnostics.append(conflicts[-1])

    marker_target = (
        _target_for_high_water(git, effective_high_water)
        if effective_high_water is not None
        else _resolve_commit(git, "HEAD")
    )
    if marker_target is None:
        diagnostics.append("migration_marker_has_no_commit_target")
    else:
        specs.append(
            TagSpec(
                MIGRATION_MARKER_TAG,
                marker_target,
                "National reaped registry migration v1 complete",
            )
        )
        required.append(MIGRATION_MARKER_TAG)

    ready = not unmappable and not conflicts and marker_target is not None
    return MigrationPlan(
        ready=ready,
        already_migrated=False,
        legacy_entry_count=state.legacy.entry_count,
        legacy_versions=tuple(sorted(state.legacy.versions)),
        unmappable_versions=tuple(unmappable),
        effective_high_water=effective_high_water,
        create_tags=tuple(specs) if ready else (),
        required_tags=tuple(sorted(set(required))) if ready else (),
        completion_preconditions=tuple(preconditions),
        diagnostics=tuple(diagnostics),
    )


def _git_identity(git: GitRepository) -> tuple[str, str]:
    name = git.run("config", "--get", "user.name", check=False).stdout.strip()
    email = git.run("config", "--get", "user.email", check=False).stdout.strip()
    name = name if name and not any(char in name for char in "<>\n\r") else "National Epoch Registry"
    email = email if email and not any(char in email for char in "<>\n\r") else "national-epoch@local.invalid"
    return name, email


def _make_tag_object(
    git: GitRepository,
    spec: TagSpec,
    *,
    timestamp: int,
) -> str:
    name, email = _git_identity(git)
    payload = (
        f"object {spec.target_oid}\n"
        "type commit\n"
        f"tag {spec.name}\n"
        f"tagger {name} <{email}> {timestamp} +0000\n"
        "\n"
        f"{spec.message}\n"
    )
    result = git.run("mktag", input_text=payload)
    oid = result.stdout.strip()
    if not _OID_RE.fullmatch(oid):
        raise MigrationError(f"git mktag returned an invalid object id for {spec.name}")
    return oid.lower()


def _create_annotated_tags_atomic(
    git: GitRepository,
    specs: Sequence[TagSpec],
    *,
    now: Callable[[], float] = time.time,
) -> TagMutationResult:
    head_before = _resolve_commit(git, "HEAD") or ""
    if not specs:
        return TagMutationResult((), head_before, head_before)

    tag_objects = [
        (spec.name, _make_tag_object(git, spec, timestamp=int(now())))
        for spec in specs
    ]
    transaction = ["start"]
    transaction.extend(f"create refs/tags/{name} {oid}" for name, oid in tag_objects)
    transaction.extend(["prepare", "commit", ""])
    result = git.run("update-ref", "--stdin", input_text="\n".join(transaction), check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown update-ref error"
        raise MigrationError(f"atomic registry ref transaction failed: {detail}")

    head_after = _resolve_commit(git, "HEAD") or ""
    if head_before != head_after:
        raise MigrationError("registry mutation unexpectedly changed HEAD")
    return TagMutationResult(tuple(name for name, _ in tag_objects), head_before, head_after)


def apply_migration_plan(
    plan: MigrationPlan,
    *,
    repo_root: Path | str,
    runner: GitRunner | None = None,
    now: Callable[[], float] = time.time,
) -> TagMutationResult:
    """Publish all migration refs in one Git transaction without moving HEAD."""

    if not plan.ready:
        detail = "; ".join(plan.diagnostics) or "migration preflight is not ready"
        raise MigrationError(detail)
    git = GitRepository(repo_root, runner)
    for ref, expected_oid in plan.completion_preconditions:
        current_oid = _resolve_commit(git, ref)
        if current_oid != expected_oid:
            raise MigrationError(f"completion ref changed after preflight: {ref}")
    return _create_annotated_tags_atomic(git, plan.create_tags, now=now)


def push_registry_tags(
    tag_names: Sequence[str],
    *,
    repo_root: Path | str,
    remote: str = "origin",
    runner: GitRunner | None = None,
) -> None:
    """Explicitly push registry refs; callers must opt in to this operation."""

    if not tag_names:
        return
    git = GitRepository(repo_root, runner)
    refspecs = [f"refs/tags/{name}:refs/tags/{name}" for name in sorted(set(tag_names))]
    git.run("push", "--atomic", remote, *refspecs)


def create_reaped_tombstone(
    version: int,
    *,
    repo_root: Path | str,
    legacy_ledger: Path | str = DEFAULT_LEGACY_LEDGER,
    runner: GitRunner | None = None,
) -> TagMutationResult:
    """Create one permanent reaped tag after the registry migration."""

    parsed_version = _positive_version(version)
    if parsed_version is None:
        raise ValueError("reaped version must be a positive integer")
    state = load_registry_state(
        repo_root,
        legacy_ledger=legacy_ledger,
        runner=runner,
        include_history=False,
    )
    if not state.migration_marker or not state.available:
        raise RegistryUnavailableError("durable reaped registry is not available")
    git = GitRepository(repo_root, runner)
    name = f"{REAPED_TAG_PREFIX}{parsed_version}"
    tags = _scan_tags(git)
    existing = tags.get(name)
    completion_name = f"{COMPLETION_TAG_PREFIX}{parsed_version}"
    completion_record = tags.get(completion_name)
    completion_oid = _resolve_commit(git, f"refs/tags/{completion_name}")
    if completion_record is None or not completion_record.annotated or completion_oid is None:
        raise RegistryError(f"missing completion tag for national bot v{parsed_version}")
    if existing:
        if existing.annotated and existing.target_oid.lower() == completion_oid:
            head = _resolve_commit(git, "HEAD") or ""
            return TagMutationResult((), head, head)
        raise RegistryError(f"conflicting durable tombstone tag: {name}")
    return _create_annotated_tags_atomic(
        git,
        [TagSpec(name, completion_oid, f"National epoch durable reaped tombstone for v{parsed_version}")],
    )


def advance_high_water(
    version: int,
    *,
    repo_root: Path | str,
    legacy_ledger: Path | str = DEFAULT_LEGACY_LEDGER,
    runner: GitRunner | None = None,
) -> TagMutationResult:
    """Advance, but never lower, the durable version high-water."""

    parsed_version = _positive_version(version)
    if parsed_version is None:
        raise ValueError("high-water version must be a positive integer")
    state = load_registry_state(
        repo_root,
        legacy_ledger=legacy_ledger,
        runner=runner,
        include_history=True,
    )
    if not state.migration_marker or not state.available:
        raise RegistryUnavailableError("durable reaped registry is not available")
    observed = [parsed_version]
    observed.extend(state.completion_versions)
    observed.extend(state.high_water_versions)
    if state.history_high_water is not None:
        observed.append(state.history_high_water)
    desired = max(observed)
    if state.high_water_versions and max(state.high_water_versions) >= desired:
        git = GitRepository(repo_root, runner)
        head = _resolve_commit(git, "HEAD") or ""
        return TagMutationResult((), head, head)
    git = GitRepository(repo_root, runner)
    name = f"{HIGH_WATER_TAG_PREFIX}{desired}"
    if name in _scan_tags(git):
        raise RegistryError(f"conflicting high-water tag: {name}")
    return _create_annotated_tags_atomic(
        git,
        [
            TagSpec(
                name,
                _target_for_high_water(git, desired),
                f"National epoch monotonic high-water v{desired}",
            )
        ],
    )


__all__ = [
    "COMPLETION_TAG_PREFIX",
    "DEFAULT_LEGACY_LEDGER",
    "GitRepository",
    "GitResult",
    "GitRunner",
    "HIGH_WATER_TAG_PREFIX",
    "LegacyLedgerState",
    "MIGRATION_MARKER_TAG",
    "MigrationError",
    "MigrationPlan",
    "REAPED_TAG_PREFIX",
    "RegistryError",
    "RegistryState",
    "RegistryUnavailableError",
    "TagMutationResult",
    "advance_high_water",
    "apply_migration_plan",
    "build_migration_plan",
    "create_reaped_tombstone",
    "effective_target_version",
    "git_history_high_water",
    "load_registry_state",
    "parse_legacy_ledger",
    "push_registry_tags",
    "subprocess_git_runner",
]
