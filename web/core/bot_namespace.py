"""Single source of truth for the national TCP policy-bot namespace.

The active namespace deliberately has no compatibility path to Botzone bots or
to the retired ``national_native_v1`` strategy ABI.  Historical versions may
remain in Git/archive storage for audit, but only a bot rooted directly below
``bots/`` with the strict policy manifests defined here can resolve to an
active role.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any, Callable


EVALUATION_EPOCH = "national_tcp_policy_v1"

# The namespace prefixes are configurable through environment variables so a
# deployment can run a fully isolated evolution line (e.g. a cloud runtime
# branch) that never collides with the canonical national_v tag namespace.
# Defaults preserve the historical identity exactly so existing behavior on
# the main branch is unchanged when the variables are not set.
#
# POK_CLOUD_RUNTIME is the authoritative opt-in signal for the cloud evolution
# line (tencent-cloud-runtime branch, national_cloud_v namespace). When it is
# set, the namespace/branch/tag prefixes default to the cloud values unless an
# explicit POK_BOT_PREFIX/POK_TAG_PREFIX/etc. override is also present. This
# resolution runs at import time so every importer — web/main.py, the
# orchestrator/daemon CLIs, scripts, and the test suite — sees the same
# namespace from the same env, instead of relying on a web/main.py side effect
# that only fires for the web launcher entry point.
if os.environ.get("POK_CLOUD_RUNTIME", "").lower() in {"1", "true", "yes", "on"}:
    os.environ.setdefault("POK_EVOLUTION_BRANCH", "tencent-cloud-runtime")
    os.environ.setdefault("POK_BOT_PREFIX", "national_cloud_v")
    os.environ.setdefault("POK_TAG_PREFIX", "national-cloud-bot-v")
    os.environ.setdefault("POK_HIGH_WATER_TAG_PREFIX", "national-cloud-high-water-v")
ACTIVE_BOT_PREFIX = os.environ.get("POK_BOT_PREFIX", "national_v")
ACTIVE_TAG_PREFIX = os.environ.get("POK_TAG_PREFIX", "national-bot-v")
HIGH_WATER_TAG_PREFIX = os.environ.get(
    "POK_HIGH_WATER_TAG_PREFIX", "national-high-water-v"
)
# Certification / two-tier publication were removed: every eligible bot now
# publishes through a single native tier with no signed-certificate gate.
# These names are retained as inert stubs because web/server/routes/control.py,
# _helpers.py, official_certification_authority.py and scripts/official_certify.py
# still import them at module load; they always return empty/never-match values.
CERTIFIED_TAG_PREFIX = ""
ALLOW_STAGING_AS_PARENT = True
VERSION_WIDTH = 0

# The canonical evolution publication branch. Configurable so a deployment can
# publish into an isolated branch (e.g. tencent-cloud-runtime) without ever
# disturbing origin/main. Defaults to the historical "main".
EVOLUTION_BRANCH = os.environ.get("POK_EVOLUTION_BRANCH", "main")

# The archived pre-policy epoch high-water. On the canonical main branch this
# is 142 (so the first strict policy version is 143); on the tencent-cloud-runtime
# branch it is 0 so the cloud line starts from version 1 with no inherited
# version-authority floor. Both branches derive FIRST_STRICT_POLICY_VERSION
# from this single constant.
ARCHIVED_VERSION_HIGH_WATER = 0
FIRST_STRICT_POLICY_VERSION = ARCHIVED_VERSION_HIGH_WATER + 1

NATIONAL_RUNTIME_MANIFEST = "national_runtime_manifest.json"
POLICY_EPOCH_RECEIPT = "policy_epoch_receipt.json"
NATIONAL_ENTRYPOINT = "national_bot.py"
POLICY_ENTRYPOINT = "policy.py"
PRECOMPUTE_ENTRYPOINT = "precompute.py"
STRICT_ARTIFACT_FILES = frozenset(
    {
        NATIONAL_ENTRYPOINT,
        POLICY_ENTRYPOINT,
        PRECOMPUTE_ENTRYPOINT,
        NATIONAL_RUNTIME_MANIFEST,
        POLICY_EPOCH_RECEIPT,
    }
)
SYSTEM_DERIVED_IDENTITY_FILES = frozenset(
    {NATIONAL_RUNTIME_MANIFEST, POLICY_EPOCH_RECEIPT}
)

NATIONAL_RUNTIME_MANIFEST_SCHEMA_VERSION = 1
POLICY_EPOCH_RECEIPT_SCHEMA_VERSION = 1
NATIONAL_ARTIFACT_CONTRACT_SCHEMA_VERSION = 1
NATIONAL_RUNTIME_SCHEMA_VERSION = 1
NATIONAL_STREAM_SCHEMA_VERSION = 2
DECISION_CONTEXT_SCHEMA_VERSION = 1
POLICY_DECISION_SCHEMA_VERSION = 1
NATIONAL_RUNTIME_CONTRACT_ID = "national-tcp-policy-runtime-v1"
NATIONAL_PROTOCOL_ID = "official-national-raw-tcp-v1"
FORBIDDEN_LEGACY_ARTIFACT_FILES = frozenset(
    {
        "main.py",
        "state.py",
        "strategy.py",
        "postflop.py",
        "bot_adapter.py",
    }
)

ROLE_CANDIDATE = "candidate"
ROLE_PARENT_SOURCE = "parent_source"
ROLE_RATING_POOL = "rating_pool"
ROLE_OFFICIAL_OPPONENT = "official_opponent"
ACTIVE_PUBLISHED_ROLES = frozenset(
    {ROLE_PARENT_SOURCE, ROLE_RATING_POOL, ROLE_OFFICIAL_OPPONENT}
)
NATIONAL_BOT_ROLES = frozenset({ROLE_CANDIDATE, *ACTIVE_PUBLISHED_ROLES})

# The active-prefix regular expressions are compiled lazily (rather than frozen
# at import time) so they always reflect the currently configured namespace
# prefixes. The prefixes are read from environment variables at module load, so
# a frozen import-time compile would already match the deployment value; the
# lazy form is kept for robustness and to mirror the dynamic glob helpers.
def _active_name_re() -> re.Pattern:
    return re.compile(rf"^{re.escape(ACTIVE_BOT_PREFIX)}([1-9][0-9]*)$")


def _active_tag_re() -> re.Pattern:
    return re.compile(rf"^{re.escape(ACTIVE_TAG_PREFIX)}([1-9][0-9]*)$")


def _high_water_tag_re() -> re.Pattern:
    return re.compile(rf"^{re.escape(HIGH_WATER_TAG_PREFIX)}([1-9][0-9]*)$")


def _certified_tag_re() -> re.Pattern:
    # Certification tier removed: no tag ever matches the certified namespace.
    # Using ``(?!x)x`` rather than an empty prefix keeps this a compiled
    # "never match" pattern even if CERTIFIED_TAG_PREFIX is later reused.
    return re.compile(r"^(?!x)x$")
_GIT_OBJECT_ID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUNTIME_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "contract_id",
        "epoch",
        "protocol",
        "runtime_schema_version",
        "stream_schema_version",
        "decision_context_schema_version",
        "policy_decision_schema_version",
        "entrypoint",
        "policy_entrypoint",
        "precompute_entrypoint",
        "files",
    }
)
_EPOCH_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "epoch",
        "version",
        "lineage",
        "artifact_contract_digest",
    }
)
_LINEAGE_KEYS = frozenset(
    {
        "mode",
        "parent_versions",
        "version_authority_high_water",
        "source_artifact_inherited",
    }
)
_CORE_FILES = (
    NATIONAL_ENTRYPOINT,
    POLICY_ENTRYPOINT,
    PRECOMPUTE_ENTRYPOINT,
)
_WORKING_CONTROL_DIRECTORY_NAME = ".task_context"
_FORBIDDEN_EXECUTION_CACHE_DIRECTORY_NAMES = frozenset(
    {"__pycache__", ".pytest_cache"}
)
_IGNORED_RUNTIME_FILE_NAMES = frozenset({".completed"})
_FORBIDDEN_EXECUTION_CACHE_FILE_SUFFIXES = frozenset({".pyc", ".pyo"})


def strict_artifact_layout_errors(
    bot_path: str | Path,
    *,
    allow_working_task_context: bool = False,
) -> list[str]:
    """Require the five executable/identity bot files, not a five-file host.

    The active Bot directory itself is deliberately closed: it cannot contain
    a candidate helper, model, table, symlink, cache, or arbitrary data file.
    A future file-backed model/table is therefore not an exception to this
    layout check. It must live under a separately versioned system-owned asset
    root and be admitted through the asset ABI (registry, receipt, manifest,
    broker, common resolver, and influence proof) before a runtime profile can
    bind it. The current v1 profile has no external asset binding, so this
    check remains exact-five and fail-closed.

    ``.task_context`` is a compiler-owned, non-executable Worker input.  The
    one host-owned identity refresh that runs before the compiler removes that
    directory may opt into it explicitly.  Every execution, parent selection,
    certification, rating, and publication caller uses the default and rejects
    it.  Python caches are never authorized: ``-B`` prevents writes but does not
    stop an unchecked-hash ``.pyc`` from overriding a content-bound source file.
    """

    root = Path(bot_path)
    if root.is_symlink() or not root.is_dir():
        return ["active_bot_directory_missing_or_nonregular"]
    files: set[str] = set()
    errors: list[str] = []
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        return [f"artifact_layout_unreadable:{type(exc).__name__}:{exc}"]
    for child in children:
        name = child.name
        try:
            metadata = child.lstat()
        except OSError as exc:
            errors.append(f"artifact_entry_unreadable:{name}:{type(exc).__name__}")
            continue
        if stat.S_ISLNK(metadata.st_mode):
            errors.append(f"artifact_symlink_forbidden:{name}")
            continue
        if stat.S_ISDIR(metadata.st_mode):
            if name == _WORKING_CONTROL_DIRECTORY_NAME:
                if not allow_working_task_context:
                    errors.append(f"artifact_working_control_directory_forbidden:{name}")
            elif name in _FORBIDDEN_EXECUTION_CACHE_DIRECTORY_NAMES:
                errors.append(f"artifact_execution_cache_directory_forbidden:{name}")
            else:
                errors.append(f"artifact_extra_directory_forbidden:{name}")
            continue
        if not stat.S_ISREG(metadata.st_mode):
            errors.append(f"artifact_nonregular_entry_forbidden:{name}")
            continue
        if (
            name in _IGNORED_RUNTIME_FILE_NAMES
        ):
            continue
        if child.suffix.lower() in _FORBIDDEN_EXECUTION_CACHE_FILE_SUFFIXES:
            errors.append(f"artifact_execution_cache_file_forbidden:{name}")
            continue
        files.add(name)
    for relative in sorted(STRICT_ARTIFACT_FILES - files):
        errors.append(f"artifact_required_file_missing:{relative}")
    for relative in sorted(files - STRICT_ARTIFACT_FILES):
        errors.append(f"artifact_extra_file_forbidden:{relative}")
    return list(dict.fromkeys(errors))


def format_version(version: int | str) -> str:
    v = int(version)
    if v < 0:
        raise ValueError("bot version must be non-negative")
    if VERSION_WIDTH <= 0:
        return str(v)
    return f"{v:0{VERSION_WIDTH}d}"


def bot_name(version: int | str) -> str:
    return f"{ACTIVE_BOT_PREFIX}{format_version(version)}"


def bot_dir(root: Path, version: int | str) -> Path:
    return root / "bots" / bot_name(version)


def bot_relpath(version: int | str) -> str:
    return f"bots/{bot_name(version)}"


def bot_tag(version: int | str) -> str:
    return f"{ACTIVE_TAG_PREFIX}{format_version(version)}"


def strict_generation_identity(
    version: int,
    *,
    generation_ordinal: int,
) -> dict[str, int | str]:
    """Return the sole user-visible/canonical identity mapping for this epoch.

    ``generation_ordinal`` is presentation identity only.  The canonical Bot
    directory, completion tag, certification, rating and checkpoint continue
    to use the immutable version namespace.  The ordinal is supplied by the
    paired publication/checkpoint authority because canonically abandoned
    labels do not become user-visible Bot generations. Callers must not infer
    it from a directory name or accept Python's ``bool``-as-``int`` coercion.
    """

    if type(version) is not int:
        raise TypeError("strict generation version must be an integer")
    if version < FIRST_STRICT_POLICY_VERSION:
        raise ValueError(
            "strict generation version precedes national_tcp_policy_v1"
        )
    if type(generation_ordinal) is not int:
        raise TypeError("strict generation ordinal must be an integer")
    if generation_ordinal <= 0:
        raise ValueError("strict generation ordinal must be positive")
    return {
        "generation_ordinal": generation_ordinal,
        "canonical_version": version,
        "canonical_bot_name": bot_name(version),
        "canonical_tag": bot_tag(version),
    }


def bot_tag_glob() -> str:
    return f"{ACTIVE_TAG_PREFIX}*"


def high_water_tag(version: int | str) -> str:
    """Return the canonical high-water tag label for a version.

    Mirrors ``bot_tag`` for the high-water namespace so callers never hardcode
    the ``national-high-water-v`` prefix literal.
    """
    return f"{HIGH_WATER_TAG_PREFIX}{format_version(version)}"


def high_water_tag_glob() -> str:
    return f"{HIGH_WATER_TAG_PREFIX}*"


def certified_tag(version: int | str) -> str:
    """Return the certified-tier tag label for a version.

    The certification tier has been removed; this stub now always returns the
    empty string.  It is retained because web/server/routes/control.py,
    web/server/routes/_helpers.py, web/core/official_certification_authority.py
    and scripts/official_certify.py still import it.
    """

    return ""


def certified_tag_glob() -> str:
    # Inert: the certified tag namespace no longer exists, so its glob never
    # matches.  Returned for callers that still import the helper.
    return ""


def parse_certified_tag_version(tag: str | None) -> int | None:
    # Certification tier removed; no tag is ever a certified tag.
    return None


def parse_bot_version(name: str | None) -> int | None:
    """Parse only a canonical active-namespace bot label.

    Legacy ``claude_v*``, bare ``vN`` and path aliases are intentionally not
    accepted.  Callers that hold a path must pass its basename explicitly.
    """

    if not isinstance(name, str):
        return None
    match = _active_name_re().fullmatch(Path(name.replace("\\", "/")).name)
    return int(match.group(1)) if match else None


def parse_tag_version(tag: str | None) -> int | None:
    if not isinstance(tag, str):
        return None
    match = _active_tag_re().fullmatch(tag)
    return int(match.group(1)) if match else None


@dataclass(frozen=True)
class VersionNamespaceAuthority:
    """One immutable snapshot of the paired publication-tag namespace.

    A completion tag or a high-water tag by itself is an interrupted effect,
    not version authority.  A version advances the namespace only when both
    exact annotated tags peel to the same commit.  Strict-v143+ artifact
    eligibility remains a separate publication-authority check; callers must
    not treat this numeric namespace proof as executable bytes.

    ``certified_versions`` is retained as an always-empty tuple for callers
    that still read it; the certification tier has been removed and every
    paired publication is now a single native tier.
    """

    high_water: int
    paired_versions: tuple[int, ...]
    paired_commits: tuple[tuple[int, str], ...]
    unpaired_completion_versions: tuple[int, ...]
    unpaired_high_water_versions: tuple[int, ...]
    certified_versions: tuple[int, ...] = ()


def resolve_version_namespace_authority(
    git: Callable[..., str],
) -> VersionNamespaceAuthority:
    """Resolve the canonical paired completion/high-water tag authority.

    ``git`` is an injected read-only command adapter.  Keeping the resolver in
    the namespace module lets the runtime, scheduler projection, epoch-reset
    tool and stopped-runtime reconciliation command share exactly one parser
    and one commit-pairing rule.
    """

    rows = str(git(
        "for-each-ref",
        "--format=%(objecttype)%09%(*objecttype)%09%(refname:short)",
        f"refs/tags/{bot_tag_glob()}",
        f"refs/tags/{high_water_tag_glob()}",
    ) or "")
    completion: dict[int, str] = {}
    high_water: dict[int, str] = {}
    active_tag_pattern = _active_tag_re()
    high_water_pattern = _high_water_tag_re()
    for row in rows.splitlines():
        parts = row.split("\t")
        if len(parts) != 3:
            continue
        object_type, peeled_type, raw_tag = parts
        if object_type != "tag" or peeled_type != "commit":
            continue
        tag = raw_tag.strip()
        completion_match = active_tag_pattern.fullmatch(tag)
        if completion_match is not None:
            completion[int(completion_match.group(1))] = tag
            continue
        high_water_match = high_water_pattern.fullmatch(tag)
        if high_water_match is not None:
            high_water[int(high_water_match.group(1))] = tag

    paired_versions = tuple(sorted(set(completion).intersection(high_water)))
    if not paired_versions:
        raise RuntimeError(
            "paired annotated completion/high-water version authority unavailable"
        )

    paired_commits: list[tuple[int, str]] = []
    for version in paired_versions:
        completion_commit = str(git(
            "rev-parse",
            f"refs/tags/{completion[version]}^{{commit}}",
        ) or "").strip().lower()
        high_water_commit = str(git(
            "rev-parse",
            f"refs/tags/{high_water[version]}^{{commit}}",
        ) or "").strip().lower()
        if (
            _GIT_OBJECT_ID_RE.fullmatch(completion_commit) is None
            or completion_commit != high_water_commit
        ):
            raise RuntimeError(
                f"paired version authority commit mismatch for v{version}"
            )
        paired_commits.append((version, completion_commit))

    # Certification tier removed: there is no longer a third annotated tag
    # family, so certified_versions is always empty.
    return VersionNamespaceAuthority(
        high_water=paired_versions[-1],
        paired_versions=paired_versions,
        paired_commits=tuple(paired_commits),
        unpaired_completion_versions=tuple(
            sorted(set(completion).difference(high_water))
        ),
        unpaired_high_water_versions=tuple(
            sorted(set(high_water).difference(completion))
        ),
        certified_versions=(),
    )


def active_bot_glob() -> str:
    return f"{ACTIVE_BOT_PREFIX}*"


def is_active_bot_name(name: str | None) -> bool:
    return parse_bot_version(name) is not None


def version_sort_key(name: str) -> int:
    return parse_bot_version(name) or -1


def strip_bot_path_prefix(path: str) -> str:
    """Strip canonical generated-bot prefixes from a worker target path."""

    raw = path.replace("\\", "/")
    pattern = re.compile(
        rf"(?:\./)?(?:bots/)?{re.escape(ACTIVE_BOT_PREFIX)}\d+/(.+)$"
    )
    while True:
        match = pattern.match(raw)
        if not match:
            return raw
        raw = match.group(1)


def canonical_json_digest(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_regular_file(path: Path) -> str:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ValueError(f"not a regular file: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json_object(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"missing regular JSON file: {path.name}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root is not an object: {path.name}")
    return payload


def runtime_manifest_errors(
    bot_path: str | Path,
    manifest: dict[str, Any] | None = None,
) -> list[str]:
    path = Path(bot_path)
    errors: list[str] = []
    if manifest is None:
        try:
            manifest = _read_json_object(path / NATIONAL_RUNTIME_MANIFEST)
        except Exception as exc:
            return [f"runtime_manifest_unavailable:{type(exc).__name__}:{exc}"]
    if set(manifest) != _RUNTIME_MANIFEST_KEYS:
        errors.append("runtime_manifest_keys_mismatch")
    expected_scalars = {
        "schema_version": NATIONAL_RUNTIME_MANIFEST_SCHEMA_VERSION,
        "contract_id": NATIONAL_RUNTIME_CONTRACT_ID,
        "epoch": EVALUATION_EPOCH,
        "protocol": NATIONAL_PROTOCOL_ID,
        "runtime_schema_version": NATIONAL_RUNTIME_SCHEMA_VERSION,
        "stream_schema_version": NATIONAL_STREAM_SCHEMA_VERSION,
        "decision_context_schema_version": DECISION_CONTEXT_SCHEMA_VERSION,
        "policy_decision_schema_version": POLICY_DECISION_SCHEMA_VERSION,
        "entrypoint": NATIONAL_ENTRYPOINT,
        "policy_entrypoint": POLICY_ENTRYPOINT,
        "precompute_entrypoint": PRECOMPUTE_ENTRYPOINT,
    }
    for field_name, expected in expected_scalars.items():
        if manifest.get(field_name) != expected:
            errors.append(f"runtime_manifest_{field_name}_mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(_CORE_FILES):
        errors.append("runtime_manifest_files_mismatch")
        files = files if isinstance(files, dict) else {}
    for relative in _CORE_FILES:
        expected_digest = files.get(relative)
        if not isinstance(expected_digest, str) or not _HEX_SHA256_RE.fullmatch(
            expected_digest
        ):
            errors.append(f"runtime_manifest_file_digest_invalid:{relative}")
            continue
        try:
            actual_digest = _sha256_regular_file(path / relative)
        except Exception as exc:
            errors.append(f"runtime_core_file_invalid:{relative}:{type(exc).__name__}")
            continue
        if actual_digest != expected_digest:
            errors.append(f"runtime_core_file_digest_mismatch:{relative}")
    return list(dict.fromkeys(errors))


def artifact_contract_payload(runtime_manifest: dict[str, Any]) -> dict[str, Any]:
    """Return the non-circular, digest-bound policy artifact ABI contract."""

    return {
        "schema_version": NATIONAL_ARTIFACT_CONTRACT_SCHEMA_VERSION,
        "epoch": EVALUATION_EPOCH,
        "runtime_contract_id": NATIONAL_RUNTIME_CONTRACT_ID,
        "runtime_manifest_digest": canonical_json_digest(runtime_manifest),
        "required_core_files": list(_CORE_FILES),
        "runtime_manifest": NATIONAL_RUNTIME_MANIFEST,
        "epoch_receipt": POLICY_EPOCH_RECEIPT,
    }


def artifact_contract_digest(runtime_manifest: dict[str, Any]) -> str:
    return canonical_json_digest(artifact_contract_payload(runtime_manifest))


def epoch_receipt_errors(
    bot_path: str | Path,
    version: int,
    runtime_manifest: dict[str, Any],
    receipt: dict[str, Any] | None = None,
) -> list[str]:
    path = Path(bot_path)
    errors: list[str] = []
    if receipt is None:
        try:
            receipt = _read_json_object(path / POLICY_EPOCH_RECEIPT)
        except Exception as exc:
            return [f"policy_epoch_receipt_unavailable:{type(exc).__name__}:{exc}"]
    if set(receipt) != _EPOCH_RECEIPT_KEYS:
        errors.append("policy_epoch_receipt_keys_mismatch")
    if receipt.get("schema_version") != POLICY_EPOCH_RECEIPT_SCHEMA_VERSION:
        errors.append("policy_epoch_receipt_schema_mismatch")
    if receipt.get("epoch") != EVALUATION_EPOCH:
        errors.append("policy_epoch_receipt_epoch_mismatch")
    if type(receipt.get("version")) is not int or receipt.get("version") != version:
        errors.append("policy_epoch_receipt_version_mismatch")
    expected_contract_digest = artifact_contract_digest(runtime_manifest)
    if receipt.get("artifact_contract_digest") != expected_contract_digest:
        errors.append("policy_epoch_receipt_artifact_contract_digest_mismatch")

    lineage = receipt.get("lineage")
    if not isinstance(lineage, dict) or set(lineage) != _LINEAGE_KEYS:
        errors.append("policy_epoch_receipt_lineage_keys_mismatch")
        lineage = lineage if isinstance(lineage, dict) else {}
    if lineage.get("version_authority_high_water") != ARCHIVED_VERSION_HIGH_WATER:
        errors.append("policy_epoch_receipt_high_water_mismatch")
    parents = lineage.get("parent_versions")
    valid_parents = (
        isinstance(parents, list)
        and all(type(item) is int for item in parents)
        and len(parents) == len(set(parents))
        and all(FIRST_STRICT_POLICY_VERSION <= item < version for item in parents)
    )
    mode = lineage.get("mode")
    inherited = lineage.get("source_artifact_inherited")
    if version == FIRST_STRICT_POLICY_VERSION:
        if mode != "fresh_bootstrap":
            errors.append("first_strict_lineage_mode_mismatch")
        if parents != []:
            errors.append("first_strict_lineage_must_have_no_parent")
        if inherited is not False:
            errors.append("first_strict_must_not_inherit_archived_source")
    else:
        if mode != "strict_parent":
            errors.append("strict_lineage_mode_mismatch")
        if not valid_parents or not parents:
            errors.append("strict_lineage_parent_versions_invalid")
        if inherited is not True:
            errors.append("strict_lineage_inheritance_flag_mismatch")
    return list(dict.fromkeys(errors))


@dataclass(frozen=True)
class NationalBotSpec:
    path: Path
    label: str
    version: int | None
    role: str
    runtime_manifest: dict[str, Any] = field(default_factory=dict)
    epoch_receipt: dict[str, Any] = field(default_factory=dict)
    publication_identity: dict[str, Any] = field(default_factory=dict)
    certificate_digest: str = ""
    # Native tier: the certification system has been removed and every eligible
    # published bot now resolves to a single native tier.
    publication_tier: str = "native"
    issues: tuple[str, ...] = ()

    @property
    def eligible(self) -> bool:
        return not self.issues

    @property
    def entrypoint(self) -> Path:
        return self.path / NATIONAL_ENTRYPOINT

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "path": str(self.path),
            "label": self.label,
            "version": self.version,
            "role": self.role,
            "entrypoint": str(self.entrypoint),
            "epoch": EVALUATION_EPOCH,
            "runtime_manifest": self.runtime_manifest,
            "epoch_receipt": self.epoch_receipt,
            "publication_identity": self.publication_identity,
            "certificate_digest": self.certificate_digest,
            "publication_tier": self.publication_tier,
            "issues": list(self.issues),
        }


def resolve_national_bot_spec(
    path_or_label: str | Path,
    role: str = ROLE_CANDIDATE,
    *,
    repo_root: str | Path | None = None,
    require_completion: bool | None = None,
    require_certificate: bool | None = None,
    publication_resolver: Callable[[Path], dict[str, Any]] | None = None,
    certificate_resolver: Callable[[Path], dict[str, Any]] | None = None,
    ledger_fresh: bool = True,
) -> NationalBotSpec:
    """Resolve one strict policy bot without consulting archive directories.

    Published roles require an immutable annotated completion identity.  The
    certification system has been removed, so no role requires (or even loads)
    a signed certificate.  Candidate validation is structural so an in-flight
    bot can be gated before publication.
    """

    root = Path(repo_root or Path(__file__).resolve().parents[2]).absolute()
    bots_root = (root / "bots").absolute()
    token = Path(path_or_label).expanduser()
    if token.is_absolute() or len(token.parts) > 1:
        path = Path(os.path.abspath(os.fspath(token)))
    else:
        path = bots_root / token.name
    label = path.name
    version = parse_bot_version(label)
    issues: list[str] = []
    runtime_manifest: dict[str, Any] = {}
    receipt: dict[str, Any] = {}
    publication: dict[str, Any] = {}
    certificate_digest = ""

    if role not in NATIONAL_BOT_ROLES:
        issues.append("unknown_national_bot_role")
    try:
        if bots_root.is_symlink() or path.parent.is_symlink() or path.parent != bots_root:
            issues.append("bot_path_not_in_active_namespace")
    except Exception:
        issues.append("bot_path_not_in_active_namespace")
    if version is None:
        issues.append("invalid_national_bot_label")
    elif version < FIRST_STRICT_POLICY_VERSION:
        issues.append("pre_policy_epoch_bot_archived")
    if path.is_symlink() or not path.is_dir():
        issues.append("active_bot_directory_missing_or_nonregular")
    else:
        issues.extend(strict_artifact_layout_errors(path))
        if any((path / name).exists() for name in FORBIDDEN_LEGACY_ARTIFACT_FILES):
            issues.append("legacy_artifact_file_forbidden")

    if not issues or set(issues) <= {"unknown_national_bot_role"}:
        try:
            runtime_manifest = _read_json_object(path / NATIONAL_RUNTIME_MANIFEST)
        except Exception as exc:
            issues.append(f"runtime_manifest_unavailable:{type(exc).__name__}:{exc}")
        else:
            issues.extend(runtime_manifest_errors(path, runtime_manifest))
        if version is not None and runtime_manifest:
            try:
                receipt = _read_json_object(path / POLICY_EPOCH_RECEIPT)
            except Exception as exc:
                issues.append(
                    f"policy_epoch_receipt_unavailable:{type(exc).__name__}:{exc}"
                )
            else:
                issues.extend(
                    epoch_receipt_errors(path, version, runtime_manifest, receipt)
                )

    completion_required = (
        role in ACTIVE_PUBLISHED_ROLES
        if require_completion is None
        else bool(require_completion)
    )
    # Certification system removed: no role ever requires a certificate.  The
    # ``require_certificate`` argument is accepted for API compatibility but
    # can no longer turn the gate on.
    certificate_required = False
    if completion_required and version is not None and path.is_dir():
        try:
            if publication_resolver is None:
                from bot_artifact import published_bot_identity

                publication_resolver = published_bot_identity
            publication = publication_resolver(path)
        except Exception as exc:
            issues.append(f"publication_identity_error:{type(exc).__name__}:{exc}")
        else:
            if publication.get("published") is not True:
                issues.append("annotated_completion_publication_required")
            if publication.get("version") != version:
                issues.append("publication_version_mismatch")
            if publication.get("tag") != bot_tag(version):
                issues.append("publication_completion_tag_mismatch")

    # Single native publication tier.  ``certificate_digest`` and
    # ``publication_tier`` remain on the spec for API compatibility but no
    # longer influence eligibility.
    certificate_digest = None
    publication_tier = "native"
    return NationalBotSpec(
        path=path,
        label=label,
        version=version,
        role=role,
        runtime_manifest=runtime_manifest,
        epoch_receipt=receipt,
        publication_identity=publication,
        certificate_digest=certificate_digest,
        publication_tier=publication_tier,
        issues=tuple(dict.fromkeys(issues)),
    )


def build_runtime_manifest(bot_path: str | Path) -> dict[str, Any]:
    """Build the system-owned manifest after runtime/policy materialization."""

    path = Path(bot_path)
    return {
        "schema_version": NATIONAL_RUNTIME_MANIFEST_SCHEMA_VERSION,
        "contract_id": NATIONAL_RUNTIME_CONTRACT_ID,
        "epoch": EVALUATION_EPOCH,
        "protocol": NATIONAL_PROTOCOL_ID,
        "runtime_schema_version": NATIONAL_RUNTIME_SCHEMA_VERSION,
        "stream_schema_version": NATIONAL_STREAM_SCHEMA_VERSION,
        "decision_context_schema_version": DECISION_CONTEXT_SCHEMA_VERSION,
        "policy_decision_schema_version": POLICY_DECISION_SCHEMA_VERSION,
        "entrypoint": NATIONAL_ENTRYPOINT,
        "policy_entrypoint": POLICY_ENTRYPOINT,
        "precompute_entrypoint": PRECOMPUTE_ENTRYPOINT,
        "files": {
            relative: _sha256_regular_file(path / relative)
            for relative in _CORE_FILES
        },
    }


def build_policy_epoch_receipt(
    bot_path: str | Path,
    version: int,
    *,
    parent_versions: list[int] | tuple[int, ...] = (),
) -> dict[str, Any]:
    """Build receipt content; the caller owns durable atomic materialization."""

    runtime_manifest = _read_json_object(Path(bot_path) / NATIONAL_RUNTIME_MANIFEST)
    return _policy_epoch_receipt_payload(
        runtime_manifest,
        version=int(version),
        parent_versions=parent_versions,
    )


def _policy_epoch_receipt_payload(
    runtime_manifest: dict[str, Any],
    *,
    version: int,
    parent_versions: list[int] | tuple[int, ...] = (),
) -> dict[str, Any]:
    """Build the receipt from an already verified manifest without disk reads."""

    parents = [int(item) for item in parent_versions]
    fresh = int(version) == FIRST_STRICT_POLICY_VERSION
    return {
        "schema_version": POLICY_EPOCH_RECEIPT_SCHEMA_VERSION,
        "epoch": EVALUATION_EPOCH,
        "version": int(version),
        "lineage": {
            "mode": "fresh_bootstrap" if fresh else "strict_parent",
            "parent_versions": [] if fresh else parents,
            "version_authority_high_water": ARCHIVED_VERSION_HIGH_WATER,
            "source_artifact_inherited": not fresh,
        },
        "artifact_contract_digest": artifact_contract_digest(runtime_manifest),
    }


def canonical_identity_document_bytes(payload: dict[str, Any]) -> bytes:
    """Return the sole byte encoding for a system-owned identity document."""

    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def strict_lineage_parent_versions(
    version: int,
    source_v: int | None,
    parent2_v: int | None = None,
) -> tuple[int, ...]:
    """Derive the only legal lineage written by preparation/Worker harnesses."""

    target = int(version)
    if target == FIRST_STRICT_POLICY_VERSION:
        return ()
    candidates = [source_v, parent2_v]
    parents: list[int] = []
    for item in candidates:
        if item is None:
            continue
        parent = int(item)
        if parent not in parents:
            parents.append(parent)
    if (
        not parents
        or any(
            parent < FIRST_STRICT_POLICY_VERSION or parent >= target
            for parent in parents
        )
    ):
        raise ValueError(
            f"invalid strict policy lineage for v{target}: {parents!r}"
        )
    return tuple(parents)


def policy_identity_document_errors(
    bot_path: str | Path,
    version: int,
    *,
    parent_versions: list[int] | tuple[int, ...] = (),
    allow_working_task_context: bool = False,
) -> list[str]:
    """Verify semantic identity and the exact host-owned JSON byte encoding.

    A semantically equivalent JSON rewrite is still a candidate mutation.  The
    two documents are not Worker-owned configuration; their exact bytes are a
    deterministic function of the three core files, target version, and frozen
    scheduler lineage.
    """

    root = Path(bot_path)
    errors = list(strict_artifact_layout_errors(
        root,
        allow_working_task_context=allow_working_task_context,
    ))
    try:
        expected_manifest = build_runtime_manifest(root)
    except Exception as exc:
        return list(dict.fromkeys([
            *errors,
            f"policy_identity_manifest_build_failed:{type(exc).__name__}:{exc}",
        ]))
    try:
        manifest = _read_json_object(root / NATIONAL_RUNTIME_MANIFEST)
    except Exception as exc:
        manifest = {}
        errors.append(
            f"policy_identity_manifest_unavailable:{type(exc).__name__}:{exc}"
        )
    else:
        errors.extend(runtime_manifest_errors(root, manifest))
        if manifest != expected_manifest:
            errors.append("policy_identity_manifest_subject_mismatch")
        try:
            observed_bytes = (root / NATIONAL_RUNTIME_MANIFEST).read_bytes()
        except OSError as exc:
            errors.append(
                f"policy_identity_manifest_bytes_unavailable:{type(exc).__name__}:{exc}"
            )
        else:
            if observed_bytes != canonical_identity_document_bytes(expected_manifest):
                errors.append("policy_identity_manifest_noncanonical_bytes")

    expected_receipt = _policy_epoch_receipt_payload(
        expected_manifest,
        version=int(version),
        parent_versions=parent_versions,
    )
    try:
        receipt = _read_json_object(root / POLICY_EPOCH_RECEIPT)
    except Exception as exc:
        receipt = {}
        errors.append(
            f"policy_identity_receipt_unavailable:{type(exc).__name__}:{exc}"
        )
    else:
        errors.extend(
            epoch_receipt_errors(root, int(version), expected_manifest, receipt)
        )
        if receipt != expected_receipt:
            errors.append("policy_identity_receipt_subject_mismatch")
        try:
            observed_bytes = (root / POLICY_EPOCH_RECEIPT).read_bytes()
        except OSError as exc:
            errors.append(
                f"policy_identity_receipt_bytes_unavailable:{type(exc).__name__}:{exc}"
            )
        else:
            if observed_bytes != canonical_identity_document_bytes(expected_receipt):
                errors.append("policy_identity_receipt_noncanonical_bytes")
    return list(dict.fromkeys(errors))


def _atomic_replace_identity(path: Path, payload: bytes) -> None:
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(temporary, flags, 0o644)
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("identity document write made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def refresh_policy_identity_documents(
    bot_path: str | Path,
    version: int,
    *,
    parent_versions: list[int] | tuple[int, ...] = (),
) -> dict[str, Any]:
    """Atomically rebuild both identities after an authorized policy edit."""

    root = Path(bot_path)
    # The system refresh runs immediately before the compiler-owned Worker
    # briefs are removed.  This is the sole work-phase exception; caches remain
    # forbidden and every consumer of the resulting artifact revalidates with
    # the strict default before execution/publication.
    layout_errors = strict_artifact_layout_errors(
        root,
        allow_working_task_context=True,
    )
    if layout_errors:
        raise ValueError("invalid strict artifact layout: " + ";".join(layout_errors))
    manifest = build_runtime_manifest(root)
    _atomic_replace_identity(
        root / NATIONAL_RUNTIME_MANIFEST,
        canonical_identity_document_bytes(manifest),
    )
    receipt = _policy_epoch_receipt_payload(
        manifest,
        version=int(version),
        parent_versions=parent_versions,
    )
    _atomic_replace_identity(
        root / POLICY_EPOCH_RECEIPT,
        canonical_identity_document_bytes(receipt),
    )
    errors = policy_identity_document_errors(
        root,
        int(version),
        parent_versions=parent_versions,
        allow_working_task_context=True,
    )
    if errors:
        raise ValueError("refreshed policy identity is invalid: " + ";".join(errors))
    return {
        "runtime_manifest": manifest,
        "epoch_receipt": receipt,
        "runtime_manifest_digest": canonical_json_digest(manifest),
        "epoch_receipt_digest": canonical_json_digest(receipt),
    }
