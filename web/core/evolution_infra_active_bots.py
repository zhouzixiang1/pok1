"""Active-bots discovery and version namespace authority for evolution_infra.

Extracted as a cohesive business cluster from evolution_infra.py: resolve the
canonical active bot pool, protocol-fingerprint bots, derive the published
high-water and the version namespace authority.  This is read-only discovery
-- it never searches the archive.

evolution_infra.py retains thin delegate shells so external
``from evolution_infra import <name>`` sites and
``monkeypatch.setattr(evolution_infra, "<name>", ...)`` patches keep resolving.

IMPORTANT -- shared-symbol access model
---------------------------------------
Many names referenced by these bodies remain in ``evolution_infra`` because
they are part of that module's monkeypatch surface -- the test suite patches
``evolution_infra.BOTS_DIR``, ``evolution_infra._git``,
``evolution_infra.evolution_git_push_required``,
``evolution_infra.load_reaped_bot_versions``,
``evolution_infra._official_parent_eligible``,
``evolution_infra.is_active_bot_protocol_eligible``,
``evolution_infra._discover_active_bots``,
``evolution_infra.get_active_bots``,
``evolution_infra.version_namespace_authority``,
``evolution_infra.find_current_v`` and reads them back through the active-bot
discovery / version-authority code paths.  Binding them at import time would
freeze the pre-patch value and silently break those tests.

Every such reference in this file is written ``_ei.<name>`` so it resolves
against the live module attribute at call time.  This includes EVERY call from
one moved body to another moved body (``get_active_bots`` ->
``_discover_active_bots``, ``find_current_v`` -> ``version_namespace_authority``,
``find_latest_active_v`` -> ``get_active_bots``, etc.): those are routed
through ``_ei.<name>`` too, so a test that does
``monkeypatch.setattr(evolution_infra, "<name>", fake)`` still sees the fake
fire even when the call originates inside this companion.

The only references kept as bare globals are:
  * ``_ACTIVE_BOT_PROTOCOL_CACHE`` (a module-level dict private to this
    companion, never patched); and the stdlib / ``bot_namespace`` constants
    imported directly below (they are immutable and not on the patch surface).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import evolution_infra as _ei  # for BOTS_DIR, _git, evolution_git_push_required,
                               # load_reaped_bot_versions, _tagged_bot_versions,
                               # _remote_published_completion_versions,
                               # _incomplete_checkpoint_publication_versions,
                               # _registry_may_be_virgin,
                               # _ensure_completed_sentinels_for_tagged_bots,
                               # _TARGET_ANNOTATION_RE, log, and the thin
                               # delegate shells that pick up test monkeypatches.

# Constants and spec resolvers re-exported by evolution_infra.  Imported here
# directly (they are immutable module constants / pure functions, not on the
# monkeypatch surface) so the moved bodies can keep referencing them as bare
# globals, exactly as they did inline.
from bot_namespace import (
    ACTIVE_BOT_PREFIX,
    ARCHIVED_VERSION_HIGH_WATER,
    EVALUATION_EPOCH,
    ROLE_CANDIDATE,
    ROLE_PARENT_SOURCE,
    bot_name,
    parse_bot_version,
    resolve_national_bot_spec,
    resolve_version_namespace_authority,
    strip_bot_path_prefix,
    version_sort_key,
)

log = logging.getLogger("pok.infra")


def active_native_contract_filter_enabled() -> bool:
    # The policy epoch has no compatibility escape hatch.  An environment flag
    # cannot reintroduce archived Botzone/strategy artifacts into active roles.
    if EVALUATION_EPOCH == "national_tcp_policy_v1":
        return True
    raw = os.environ.get("POK_ACTIVE_NATIVE_CONTRACT_FILTER")
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


_ACTIVE_BOT_PROTOCOL_CACHE: dict[tuple, tuple[str, ...]] = {}


def _bot_protocol_fingerprint(bot_dir: Path) -> tuple[tuple, ...]:
    files: list[tuple] = []
    if not bot_dir.exists():
        return (("<missing>", 0, 0),)
    for path in sorted(bot_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            st = path.stat()
            rel = str(path.relative_to(bot_dir)).replace(os.sep, "/")
            files.append(
                (
                    rel,
                    int(st.st_mtime_ns),
                    int(st.st_ctime_ns),
                    int(st.st_size),
                    int(st.st_ino),
                )
            )
        except OSError:
            continue
    return tuple(files)


def active_bot_protocol_errors(
    version: int,
    *,
    quarantine_health: dict | None = None,
) -> list[str]:
    """Return active-pool protocol errors for a tagged bot version.

    The strict namespace resolver is the first authority.  It never searches
    the archive and requires the raw-TCP runtime manifest, typed policy ABI and
    epoch receipt before the implementation-level native checks run.
    """

    if not _ei.active_native_contract_filter_enabled():
        return []
    bot_dir = _ei.BOTS_DIR / bot_name(version)
    fingerprint = _ei._bot_protocol_fingerprint(bot_dir)
    cache_key = (
        int(version),
        str(bot_dir.resolve()),
        fingerprint,
        EVALUATION_EPOCH,
    )
    cached = _ACTIVE_BOT_PROTOCOL_CACHE.get(cache_key)
    if cached is not None:
        return list(cached)

    spec = resolve_national_bot_spec(
        bot_dir,
        ROLE_CANDIDATE,
        repo_root=_ei.BOTS_DIR.parent,
        require_completion=False,
        require_certificate=False,
    )
    errors = list(spec.issues)
    if not errors:
        try:
            from national_native import check_native_contract
            errors.extend(
                check_native_contract(
                    bot_dir,
                    require_current_stream_decoder=True,
                    require_current_decision_runtime=True,
                )
            )
        except Exception as exc:
            errors.append(f"native_contract_check_error: {type(exc).__name__}: {str(exc)[:200]}")
    stale_keys = [
        key for key in _ACTIVE_BOT_PROTOCOL_CACHE
        if key[0] == int(version) and key[1] == str(bot_dir.resolve())
    ]
    for key in stale_keys:
        _ACTIVE_BOT_PROTOCOL_CACHE.pop(key, None)
    _ACTIVE_BOT_PROTOCOL_CACHE[cache_key] = tuple(errors)
    return errors


def is_active_bot_protocol_eligible(
    version: int,
    *,
    quarantine_health: dict | None = None,
) -> bool:
    return not _ei.active_bot_protocol_errors(
        version,
        quarantine_health=quarantine_health,
    )


def _protocol_eligible_for_discovery(version: int, quarantine_health: dict | None) -> bool:
    """Reuse one verified policy report while preserving test/plugin overrides."""

    eligible = _ei.is_active_bot_protocol_eligible
    if eligible is _ei._ORIGINAL_IS_ACTIVE_BOT_PROTOCOL_ELIGIBLE:
        return eligible(
            version,
            quarantine_health=quarantine_health,
        )
    return bool(eligible(version))


def _target_rel(path, version):
    raw = str(path).strip()
    if not raw:
        return ""
    raw = raw.replace("\\", "/")
    raw = _ei._TARGET_ANNOTATION_RE.sub("", raw).strip()
    # 循环剥离任意层 bots/{active_bot}{N}/ 前缀（含 source_v + 双重嵌套）。
    # root-cause-audit 2026-06-21: Master context (agent_master.py:100) 注入
    # bots/{bot}{source_v}/ 路径，worker 非确定性地把它写进 target_files，甚至双重嵌套。
    # 循环剥离直到无版本前缀。
    while True:
        stripped = strip_bot_path_prefix(raw)
        if stripped == raw:
            break
        raw = stripped
    return raw


def _discover_active_bots(
    *,
    repair_completed_sentinels: bool,
    require_completed_sentinel: bool = True,
    ledger_fresh: bool = True,
) -> list[str]:
    """Active bots = tagged, completed, and protocol-eligible bots.

    Trust model mirrors find_current_v(): the git tag for the active epoch is the single
    authoritative completion proof. A bare .completed file (written by prepare
    or left behind by a crashed/never-committed generation) is NOT trusted —
    it is exactly how a "ghost bot" like v107 (completed-but-untagged) leaked
    into find_latest_active_v() and was used as an evolution source.

    In the national TCP policy epoch, the typed manifest/receipt ABI and a full
    signed official certificate are also mandatory.  Archived bot directories
    are never traversed.

    Collecting all tags once here (instead of calling git_has_tag per bot)
    keeps this O(1 git call) regardless of bot count, plus local file checks for
    protocol eligibility.
    """
    tag_versions = _ei._tagged_bot_versions()
    if _ei.evolution_git_push_required():
        # Never allow a local-only recovery tag to manufacture lifecycle
        # completion while required origin publication is still pending.
        tag_versions = set(tag_versions).intersection(
            _ei._remote_published_completion_versions(tag_versions)
        )
    tag_versions = set(tag_versions).difference(
        _ei._incomplete_checkpoint_publication_versions(tag_versions)
    )
    try:
        reaped_versions = _ei.load_reaped_bot_versions()
    except Exception as exc:
        if _ei._registry_may_be_virgin():
            # Fresh cloud bootstrap: no completion tags + no migration marker =>
            # nothing has ever been reaped. The empty set is the correct value;
            # do not log an ERROR for the expected empty state.
            reaped_versions = set()
        else:
            log.error("National reaped registry unavailable; active pool fails closed: %s", exc)
            try:
                from system_log import log_system_event

                log_system_event(
                    "pipeline.national_epoch_registry_unavailable",
                    "error",
                    "National epoch lifecycle registry unavailable; active pool disabled",
                    {"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
                )
            except Exception:
                pass
            return []
    if repair_completed_sentinels:
        _ei._ensure_completed_sentinels_for_tagged_bots(tag_versions, reaped_versions)

    bots = []
    bots_dir = _ei.BOTS_DIR
    if bots_dir.exists():
        for d in os.listdir(bots_dir):
            v = parse_bot_version(d)
            if v is None or not d.startswith(ACTIVE_BOT_PREFIX):
                continue
            completed = (bots_dir / d / ".completed").exists()
            if os.path.isdir(bots_dir / d) and (
                completed or not require_completed_sentinel
            ):
                if (
                    v in tag_versions
                    and v not in reaped_versions
                    and _ei._protocol_eligible_for_discovery(v, None)
                    and (
                        _ei._official_parent_eligible(
                            bots_dir / d,
                            ledger_fresh=False,
                        )
                        if (
                            not ledger_fresh
                            and _ei._official_parent_eligible
                            is _ei._ORIGINAL_OFFICIAL_PARENT_ELIGIBLE
                        )
                        else _ei._official_parent_eligible(bots_dir / d)
                    )
                ):
                    bots.append(d)
    return sorted(bots, key=version_sort_key)


def get_active_bots():
    """Return active bots and repair missing sentinels for trusted tagged bots."""

    return _ei._discover_active_bots(repair_completed_sentinels=True)


def get_active_bots_read_only(*, ledger_fresh: bool = True):
    """Return active bots without performing any filesystem repair.

    Read-only HTTP/catalog code must use this API so a GET request cannot create
    completion sentinels or otherwise mutate the evolution checkout.
    """

    return _ei._discover_active_bots(
        repair_completed_sentinels=False,
        ledger_fresh=ledger_fresh,
    )


def get_published_active_bots_read_only(*, ledger_fresh: bool = True):
    """Return tagged active artifacts without requiring a local sentinel.

    View-only clones do not carry the gitignored ``.completed`` cache. Git tag,
    artifact, protocol, lifecycle, and official eligibility checks remain
    mandatory, so omitting that cache does not weaken completion authority.
    """

    return _ei._discover_active_bots(
        repair_completed_sentinels=False,
        require_completed_sentinel=False,
        ledger_fresh=ledger_fresh,
    )


def _official_parent_eligible(
    bot_dir: Path,
    *,
    ledger_fresh: bool = True,
) -> bool:
    try:
        spec = resolve_national_bot_spec(
            bot_dir,
            ROLE_PARENT_SOURCE,
            repo_root=_ei.BOTS_DIR.parent,
            ledger_fresh=ledger_fresh,
        )
        if not spec.eligible:
            log.warning(
                "Strict parent eligibility rejected %s: %s",
                bot_dir.name,
                list(spec.issues),
            )
        return spec.eligible
    except Exception as exc:
        log.error(
            "Official active-pool eligibility failed closed for %s: %s",
            bot_dir.name,
            exc,
        )
        return False


def version_namespace_authority():
    """Return the canonical paired/unpaired annotated publication-ref snapshot.

    A deployment namespace with no paired tags yet (e.g. a fresh national_cloud_v
    namespace before its first strict publication) resolves to an empty authority
    sitting at the archived high-water floor, rather than raising. Default/main
    behavior (paired tags present) is unchanged.
    """

    from bot_namespace import VersionNamespaceAuthority

    try:
        return resolve_version_namespace_authority(
            lambda *args: _ei._git(*args, check=False)
        )
    except RuntimeError:
        return VersionNamespaceAuthority(
            high_water=ARCHIVED_VERSION_HIGH_WATER,
            paired_versions=(),
            paired_commits=(),
            unpaired_completion_versions=(),
            unpaired_high_water_versions=(),
        )


def find_current_v():
    """Return the immutable version-authority high-water.

    Only annotated completion/high-water tags which peel to commits advance the
    published namespace.  Directory names, sentinels, bare commits, checkpoint
    counters and runtime ledgers are deliberately absent from this read.
    """

    try:
        authority = _ei.version_namespace_authority()
    except RuntimeError:
        # An empty namespace (no paired completion/high-water tags yet) is the
        # legitimate bootstrap floor for an isolated deployment namespace such
        # as national_cloud_v: it has no strict versions, so it sits at the
        # archived high-water. This keeps version allocation and epoch state
        # well-defined before the first strict bot is published there.
        return ARCHIVED_VERSION_HIGH_WATER
    return int(authority.high_water)


def find_latest_active_v():
    """Find the highest version in the strict published active pool.
    Returns 0 if no active bots exist.
    """
    active = _ei.get_active_bots()
    if not active:
        return 0
    return max(version_sort_key(b) for b in active)


def find_latest_rating_eligible_active_v():
    """Find the highest *rating-pool-eligible* (fully certified / completed)
    version in the strict published active pool.

    The eval-source selection must pick from bots that are actually completed —
    a newly published staging master that has not yet closed the two-tier gap
    (no signed full certificate) is structurally excluded from the rating
    daemon's match queue, so it can never accrue the strength sample the next
    generation waits on. Selecting it would deadlock the next generation on an
    unreachable games floor. This returns the newest bot that CAN be evaluated,
    falling back past any not-yet-certified higher versions.

    Returns 0 if the active pool is empty or no active bot is rating-eligible.
    """
    active = _ei.get_active_bots()
    if not active:
        return 0
    eligible_versions: list[int] = []
    for bot in active:
        version = version_sort_key(bot)
        try:
            from bot_namespace import resolve_national_bot_spec, ROLE_RATING_POOL

            spec = resolve_national_bot_spec(bot, role=ROLE_RATING_POOL)
            if getattr(spec, "eligible", False):
                eligible_versions.append(version)
        except Exception:
            # An unreadable/unresolvable bot is not rating-eligible; skip it
            # rather than letting discovery fail closed for the whole pool.
            continue
    if not eligible_versions:
        return 0
    return max(eligible_versions)
