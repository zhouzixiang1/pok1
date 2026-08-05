"""Native national TCP execution backend for evolved bots.

A candidate must contain ``national_bot.py`` that connects to the national TCP
server directly and sends canonical wire actions itself.
"""

from __future__ import annotations

import asyncio
import ast
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from typing import Any
import uuid

from eval_stats import paired_bootstrap_ci
from bot_namespace import (
    STRICT_ARTIFACT_FILES,
    bot_name,
    parse_bot_version,
    strict_artifact_layout_errors,
    version_sort_key,
)
from national_runtime_telemetry import (
    empty_bot_log_summary as _empty_bot_log_summary,
    empty_runtime_telemetry as _empty_runtime_telemetry,
    merge_runtime_telemetry as _merge_runtime_telemetry,
    parse_native_bot_log as _parse_native_bot_log,
    server_action_latency as _server_action_latency,
)
from managed_bot_executor import BotTiming, EndpointLease, launch_managed_bot
from national_bot_launcher import native_entry_supports_log_arg
from national_game_runtime import (
    NATIONAL_20000_CHIP_MAX_ACTION_REQUESTS_PER_HAND,
    NationalHandActionLimitExceeded,
    NationalTCPGameEngine,
)
from sever.engine.game import BettingActionLimitExceeded, MAX_ACTIONS_PER_BETTING_ROUND
from sever.server.transport import NationalTCPClient
from pipeline_schema import NationalAcceptanceResult
from runtime_capacity import DEFAULT_CAPACITY_WAIT_SECONDS, acquire_match_slots_async
from strength_order import (
    is_precommit_gate_matchup,
    is_strength_matchup,
    precommit_outcome_blockers,
)
# System-owned runtime template artifacts. These three immutable source
# templates (the generated ``national_bot.py``, ``precompute.py``, and the
# stream-decoder probe) plus the runtime-version constant live in the
# ``national_native_templates`` companion for maintainability; they are
# re-exported here so every existing ``from national_native import ...`` site
# (top-level and deferred) keeps resolving to the same byte-identical values.
# The two ``.replace()`` post-processing steps that bake the runtime version
# into the bot template and collapse triple newlines run inside the companion,
# so the exported value matches the previous in-module definition exactly
# (the hash-pinning tests in test_national_runtime_probe.py assert over the
# post-processed bytes).
from national_native_templates import (  # noqa: F401
    NATIVE_BOT_TEMPLATE,
    NATIVE_PRECOMPUTE_TEMPLATE,
    NATIONAL_DECISION_RUNTIME_VERSION,
    _NATIVE_STREAM_PROBE_SCRIPT,
)
# Match-timing plan subsystem (Group A): dataclasses, builders, validators, and
# progress projection. This is a self-contained leaf in the
# ``national_native_timing`` companion; re-exported here so every existing
# ``from national_native import build_native_match_timing_plan`` site
# (top-level and deferred, across elo_daemon / rating_snapshot / replay_analysis
# / first_strict_execution_journal / first_strict_control / tool_gates and many
# tests) keeps resolving to the same objects. The four timing constants below
# are also re-exported because the rest of this module still consumes them.
from national_native_timing import (  # noqa: F401
    FORMAL_NATIVE_ENV_OVERRIDE_KEYS,
    LOCAL_NATIVE_STRENGTH_BASELINE_TARGET_SEC,
    LOCAL_NATIVE_STRENGTH_HARD_DEADLINE_SEC,
    LOCAL_NATIVE_STRENGTH_REFINEMENT_BUDGET_SEC,
    LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
    NATIVE_ARTIFACT_PREPARATION_PER_BOT_TIMEOUT_SEC,
    NATIVE_LAUNCH_HEARTBEAT_INTERVAL_SEC,
    NATIVE_MATCH_TIMING_PLAN_SCHEMA_VERSION,
    NATIVE_MATCH_TIMING_PROFILE,
    NATIVE_MATCH_TIMING_PROFILE_DEFINITION_DIGEST,
    NATIVE_MATCH_TIMING_PROFILE_ID,
    NativeBotTimingPlan,
    NativeMatchStartupTimeout,
    NativeMatchTimingPlan,
    _FORMAL_NATIVE_TIMING_OVERRIDE_KEYS,
    _NATIVE_PROGRESS_EVENT_TYPES,
    _annotate_native_full_match_liveness,
    _artifact_preparation_timeout_sec,
    _canonical_timing_digest,
    _native_bot_timing,
    _native_match_progress_projection,
    _native_timing_environment,
    _plain_positive_int,
    _resolve_native_match_timing_plan,
    build_native_match_timing_plan,
    native_full_match_timeout_budget,
    require_native_match_timing_plan,
    validate_native_match_timing_evidence,
)
import national_native_acceptance as _nn
import national_native_analysis as _nna  # noqa: F401  (static-analysis/trace-parsing cluster)
import national_native_tcp_exec as _nte  # noqa: F401  (direct-artifact TCP execution cluster)


ROOT = Path(__file__).resolve().parents[2]
NATIVE_ENTRY = "national_bot.py"
PRECOMPUTE_ENTRY = "precompute.py"
TRACE_PREFIX = "POK_TRACE_DECISION "
DIRECT_ARTIFACT_EXECUTION_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class NativeBotSpec:
    label: str
    path: Path
    entry: Path
    artifact_hash: str
    entry_digest: str = ""
    policy_digest: str = ""
    precompute_digest: str = ""
    runtime_manifest_digest: str = ""
    artifact_contract_digest: str = ""
    epoch_receipt_digest: str = ""

    def execution_identity(self) -> dict[str, Any]:
        """Return the exact immutable artifact identity launched for a match."""

        from bot_artifact import canonical_digest

        payload = {
            "schema_version": DIRECT_ARTIFACT_EXECUTION_SCHEMA_VERSION,
            "mode": "direct_content_bound_policy_artifact",
            "label": self.label,
            "artifact_hash": self.artifact_hash,
            "entrypoint": NATIVE_ENTRY,
            "entry_digest": self.entry_digest,
            "policy_digest": self.policy_digest,
            "precompute_digest": self.precompute_digest,
            "runtime_manifest_digest": self.runtime_manifest_digest,
            "artifact_contract_digest": self.artifact_contract_digest,
            "epoch_receipt_digest": self.epoch_receipt_digest,
        }
        return {**payload, "identity_digest": canonical_digest(payload)}


def _system_native_name_handshake_issues(
    label: str,
    spec: NativeBotSpec,
    process_info: dict[str, Any],
    bot_log_summary: dict[str, Any],
) -> list[str]:
    """Delegate to national_native_analysis."""
    return _nna._system_native_name_handshake_issues(
        label, spec, process_info, bot_log_summary
    )


def _artifact_execution_is_valid(
    payload: Any,
    expected_artifacts: dict[str, str],
) -> bool:
    """Delegate to national_native_analysis."""
    return _nna._artifact_execution_is_valid(payload, expected_artifacts)


def ensure_native_entry(bot_dir: str | Path, *, overwrite: bool = False) -> Path:
    bot_dir = Path(bot_dir)
    entry = bot_dir / NATIVE_ENTRY
    if overwrite or not entry.exists():
        entry.write_text(NATIVE_BOT_TEMPLATE, encoding="utf-8")
    precompute = bot_dir / PRECOMPUTE_ENTRY
    if not precompute.exists():
        precompute.write_text(NATIVE_PRECOMPUTE_TEMPLATE, encoding="utf-8")
    return entry



def check_native_stream_decoder(bot_dir: str | Path) -> list[str]:
    """Verify the sole current decoder without executing candidate-owned bytes.

    ``runpy`` is an execution boundary, not a static checker.  In particular,
    ``python -I`` does not sandbox the target file.  Probing the candidate path
    would therefore let an edited ``national_bot.py`` execute on the host
    before the managed bot sandbox is created.  First require the exact
    system-owned entrypoint bytes, then run the behavioral probe against a
    private copy made from :data:`NATIVE_BOT_TEMPLATE` itself.

    The implementation delegates to national_native_analysis; this shell stays
    in the parent module because the national alignment matrix anchors the
    ``NationalStreamDecoder`` symbol (the checked-in runtime decoder class
    embedded in ``NATIVE_BOT_TEMPLATE``) to this source path.
    """
    return _nna.check_native_stream_decoder(bot_dir)


def check_native_contract(
    bot_dir: str | Path,
    *,
    require_current_stream_decoder: bool = False,
    require_current_decision_runtime: bool = False,
) -> list[str]:
    bot_dir = Path(bot_dir)
    entry = bot_dir / NATIVE_ENTRY
    # Keep native-contract diagnostics aligned with the static capability and
    # publication boundaries.  An extra candidate file (including a model or
    # precomputed table) must fail before native TCP launch; future system-owned
    # assets are external and require their own bound asset profile, never a
    # sixth file in this directory.  Do not return early: callers still need
    # the concrete protocol/runtime diagnostics for the same malformed bot.
    errors = list(strict_artifact_layout_errors(bot_dir))
    if not entry.exists():
        errors.append(
            f"{NATIVE_ENTRY} missing; national_native bots must have a direct TCP entrypoint"
        )
        return list(dict.fromkeys(errors))
    try:
        text = entry.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{NATIVE_ENTRY} unreadable: {exc}"]
    forbidden = ("bot_adapter", "BotAdapter", '"response"', "'response'")
    for token in forbidden:
        if token in text:
            errors.append(f"{NATIVE_ENTRY}: forbidden alternate-entry token {token!r}")
    legacy_policy_abi_tokens = (
        'import_module("main")',
        'import_module("state")',
        'import_module("strategy")',
        "current_request_view",
        "self._requests",
        "self._responses",
        "def _action_to_tcp",
        "def _strategy_action",
    )
    for token in legacy_policy_abi_tokens:
        if token in text:
            errors.append(
                f"{NATIVE_ENTRY}: forbidden Botzone-derived candidate ABI token {token!r}"
            )
    legacy_wire_tokens = (
        "makefile(",
        ".readline(",
        "readline()",
        "newline=\"\\n\"",
        "newline='\\n'",
        "msg + \"\\n\"",
        "msg + '\\n'",
        "self.name + \"\\n\"",
        "self.name + '\\n'",
    )
    for token in legacy_wire_tokens:
        if token in text:
            errors.append(f"{NATIVE_ENTRY}: forbidden legacy newline TCP token {token!r}")
    required = ("socket", "raise ", "fold", "call", "check", "allin")
    for token in required:
        if token not in text:
            errors.append(f"{NATIVE_ENTRY}: missing native TCP token {token!r}")
    for token in ("sock.recv", "_split_messages"):
        if token not in text:
            errors.append(f"{NATIVE_ENTRY}: missing official raw TCP splitter token {token!r}")
    official_delay_tokens = ("POK_OFFICIAL_ACTION_DELAY", "_send_wire_action", "DEFAULT_OFFICIAL_ACTION_DELAY_SEC")
    for token in official_delay_tokens:
        if token not in text:
            errors.append(
                f"{NATIVE_ENTRY}: missing official EXE action throttle token {token!r}; "
                "native bots must delay action sends for the official Windows platform"
            )
    if "wire value is the extra chips added" in text:
        errors.append(f"{NATIVE_ENTRY}: TCP raise amount is documented as an increment; it must be raise-to-total")
    if "committed = min(max(0, amount), self._opponent_chips)" in text:
        errors.append(f"{NATIVE_ENTRY}: opponent raise amount is treated as an increment; it must be raise-to-total")
    if "return f\"raise {needed}\", \"raise\", action" in text:
        errors.append(f"{NATIVE_ENTRY}: outgoing raise uses delta-style wire amount; it must send raise-to-total")
    formal_wrapper = "class NativeNationalBot" in text
    if formal_wrapper:
        decision_to_tcp = _function_source(text, "_decision_to_tcp")
        if decision_to_tcp is None:
            errors.append(f"{NATIVE_ENTRY}: missing typed _decision_to_tcp translator")
        elif "_legalize_policy_decision" not in decision_to_tcp:
            errors.append(
                f"{NATIVE_ENTRY}: _decision_to_tcp must perform final socket-owner legalization"
            )
        pass_mapper = _function_source(text, "_pass_wire_kind")
        if pass_mapper is None:
            errors.append(f"{NATIVE_ENTRY}: missing abstract pass-to-wire mapper")
        elif "_responding_to_check()" not in pass_mapper:
            errors.append(
                f"{NATIVE_ENTRY}: pass mapper missing prior-check guard; the second "
                "official pass token must be call"
            )
    if _policy_decision_has_exception_pass(text):
        errors.append(
            f"{NATIVE_ENTRY}: _policy_decision must not continue with an unvalidated decision"
        )
    if require_current_stream_decoder:
        errors.extend(check_native_stream_decoder(bot_dir))
    if require_current_decision_runtime:
        policy_entry = bot_dir / "policy.py"
        if not policy_entry.is_file():
            errors.append(
                "policy.py missing; current national candidates require the typed "
                "get_baseline_decision/iter_decisions ABI"
            )
        else:
            try:
                policy_tree = ast.parse(
                    policy_entry.read_text(encoding="utf-8"),
                    filename=str(policy_entry),
                )
            except (OSError, UnicodeError, SyntaxError) as exc:
                errors.append(f"policy.py unreadable or invalid: {type(exc).__name__}: {exc}")
            else:
                policy_functions = {
                    node.name
                    for node in policy_tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                for required_function in (
                    "get_baseline_decision",
                    "iter_decisions",
                ):
                    if required_function not in policy_functions:
                        errors.append(
                            f"policy.py missing required function {required_function}"
                        )
        runtime_tokens = (
            f"NATIONAL_DECISION_RUNTIME_VERSION = {NATIONAL_DECISION_RUNTIME_VERSION}",
            "DECISION_CONTEXT_SCHEMA_VERSION = 1",
            'NATIONAL_CARD_ENCODING = "national_tcp_suit_rank_v1"',
            'importlib.import_module("policy")',
            "get_baseline_decision",
            "iter_decisions",
            "decision_context",
            "hard_monotonic",
            "refinement_monotonic",
            "baseline_target_ms",
            "mp.get_context(\"spawn\")",
            "def _policy_worker_main",
            'os.environ["POK_NATIVE_BOT_SEED"]',
            "random.seed(int(random_seed))",
            "decision_id",
            "process.terminate()",
            "process.kill()",
            "daemon=False",
            "os.killpg",
            '"taskkill"',
            "trusted_refinement_steps",
            "reported_sample_count",
            "def _reap_retired_strategy_workers",
            '_stop_strategy_worker("decision_deadline", wait=False)',
            "def _socket_safe_fallback_decision",
            'return {"kind": "fold"}',
            'return {"kind": "pass"}',
            "def _legalize_policy_decision",
            "def _decision_to_tcp",
            "def _build_decision_context",
            "def _match_control_state",
            '"match_control": self._match_control_state(remaining)',
            '"call_closes_allin_runout": bool(',
            '"policy_kinds": policy_kinds',
            '"raise_boundary": "inclusive_exact_2x_raise_to"',
            "def _infer_suppressed_terminal_opponent_action",
            "official_suppressed_terminal_action",
        )
        for token in runtime_tokens:
            if token not in text:
                errors.append(
                    f"{NATIVE_ENTRY}: missing current bounded decision runtime token {token!r}"
                )
    return errors


def _function_source(text: str, name: str) -> str | None:
    """Delegate to national_native_analysis."""
    return _nna._function_source(text, name)


def _policy_decision_has_exception_pass(text: str) -> bool:
    """Delegate to national_native_analysis."""
    return _nna._policy_decision_has_exception_pass(text)


def _handler_catches_broad_exception(handler: ast.ExceptHandler) -> bool:
    """Delegate to national_native_analysis."""
    return _nna._handler_catches_broad_exception(handler)


def _bot_version(label: str) -> int:
    """Delegate to national_native_analysis."""
    return _nna._bot_version(label)


def resolve_bot(token: str | Path) -> tuple[str, Path]:
    """Resolve only a strict policy artifact in the active ``bots/`` root.

    Path aliases into ``archive/`` and the old ``vN``/``botN``/``claude_vN``
    namespaces are rejected lexically before any artifact bytes are opened.
    """

    from bot_namespace import ROLE_CANDIDATE, resolve_national_bot_spec

    token_str = str(token)
    raw = Path(token_str).expanduser()
    if raw.is_absolute() or len(raw.parts) > 1:
        candidate = raw.parent if raw.name == NATIVE_ENTRY else raw
        candidate = Path(os.path.abspath(os.fspath(candidate)))
    else:
        if parse_bot_version(token_str) is None:
            raise ValueError(f"invalid active national bot label: {token_str}")
        candidate = (ROOT / "bots" / token_str).absolute()
    active_root = (ROOT / "bots").absolute()
    if candidate.parent != active_root:
        raise ValueError(
            f"bot path is outside the active strict namespace: {candidate}"
        )
    spec = resolve_national_bot_spec(
        candidate,
        ROLE_CANDIDATE,
        repo_root=ROOT,
        require_completion=False,
        require_certificate=False,
    )
    if not spec.eligible:
        raise ValueError(
            f"invalid strict policy artifact {spec.label}: "
            + ";".join(spec.issues[:8])
        )
    return spec.label, spec.path


def _resolve_bot_or_in_flight(token: str | Path) -> tuple[str, Path]:
    """Resolve a bot token, falling back to an in-flight workspace validation.

    This mirrors :func:`resolve_bot` for published ``bots/`` artifacts, but
    when the token is an in-flight crossover workspace path (under
    ``results/crossover_workspaces/`` or ``results/workflow/artifacts/``),
    it validates the five strict ABI files structurally and returns a
    synthetic ``in_flight_crossover_smoke:*`` label instead of raising.

    The match executor (``_run_direct_artifact_tcp_pair``) calls this so the
    crossover smoke test can run a transient candidate before it is published
    under ``bots/``, without weakening the strict-namespace guard for every
    other caller.  The synthetic label namespace can never collide with a real
    ``national_cloud_v*`` bot.  See the analogous bypass in
    ``national_native_acceptance.run_native_tcp_smoke`` (in_flight_candidate_dir).
    """

    try:
        return resolve_bot(token)
    except ValueError as exc:
        # Only bypass the strict-namespace guard for paths NOT under bots/.
        # A ValueError from a bots/ path means the artifact is genuinely broken
        # (hash mismatch, missing certificate, invalid label) — that must NOT
        # be masked into an in_flight bypass, or a corrupt published artifact
        # would run as a synthetic candidate.  Re-raise those verbatim.
        active_root = (ROOT / "bots").absolute()
        candidate_path = Path(os.path.abspath(os.fspath(token)))
        if candidate_path.name == NATIVE_ENTRY:
            candidate_path = candidate_path.parent
        if active_root in candidate_path.parents or candidate_path.parent == active_root:
            raise  # bots/ artifact with a real defect — do not mask
        # The path is outside bots/ (crossover workspace / draft candidate).
        # The outer smoke wrapper already structurally validated it (the five
        # strict ABI files).  Re-check here so the executor is self-contained.
        from bot_namespace import STRICT_ARTIFACT_FILES

        candidate = candidate_path
        missing = [
            name
            for name in STRICT_ARTIFACT_FILES
            if not (candidate / name).is_file()
        ]
        if missing:
            raise ValueError(
                f"in_flight path missing strict artifacts: "
                f"{candidate}: {','.join(sorted(missing))}"
            )
        label = f"in_flight_crossover_smoke:{candidate.name}"
        return label, candidate


def _completed_active_bots() -> list[tuple[str, Path]]:
    from evolution_infra import get_active_bots

    specs: list[tuple[str, Path]] = []
    for name in get_active_bots():
        try:
            specs.append(resolve_bot(name))
        except ValueError:
            continue
    return sorted(specs, key=lambda item: version_sort_key(item[0]), reverse=True)


def select_acceptance_opponents(candidate_label: str, source_v: int | None, limit: int = 2) -> list[tuple[str, Path]]:
    chosen: list[tuple[str, Path]] = []
    seen = {candidate_label}

    def add(spec: tuple[str, Path]):
        if spec[0] not in seen and spec[1].exists():
            chosen.append(spec)
            seen.add(spec[0])

    if source_v is not None:
        try:
            add(resolve_bot(bot_name(source_v)))
        except ValueError:
            pass
    for spec in _completed_active_bots():
        add(spec)
        if len(chosen) >= limit:
            break
    return chosen[:limit]


# Cache directories/files forbidden inside a strict bot dir by
# ``strict_artifact_layout_errors`` (bot_namespace.py).  Worker py_compile /
# import-time compilation can leave these in the candidate dir; they must be
# purged before native validation or the native match fails with 0W-0L-0D.
_PURGE_CACHE_DIR_NAMES = frozenset({"__pycache__", ".pytest_cache"})
_PURGE_CACHE_FILE_SUFFIXES = frozenset({".pyc", ".pyo"})


def _purge_execution_cache(bot_dir) -> None:
    """Remove forbidden execution-cache artifacts from a candidate bot dir.

    Deletes ``__pycache__``/``.pytest_cache`` directories and ``*.pyc``/``*.pyo``
    files anywhere under ``bot_dir`` (non-recursive into the cache dirs
    themselves -- ``shutil.rmtree`` handles those).  Only well-known cache
    names/suffixes are touched; source files are never modified.  Failures are
    swallowed (best-effort): a purge failure surfaces later as the original
    ``artifact_execution_cache_directory_forbidden`` error, which is the same
    outcome as not purging.
    """
    import shutil

    try:
        root = Path(bot_dir)
        for path in root.rglob("*"):
            try:
                name = path.name
                if path.is_dir() and name in _PURGE_CACHE_DIR_NAMES:
                    shutil.rmtree(path, ignore_errors=True)
                elif path.is_file() and path.suffix.lower() in _PURGE_CACHE_FILE_SUFFIXES:
                    path.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass


def _prepare_native_spec(
    label: str,
    bot_dir: Path,
    *,
    system_control: bool = False,
    expected_artifact_hash: str = "",
) -> NativeBotSpec:
    """Validate and bind the exact artifact that will be executed.

    No files are copied, generated, replaced, or projected here.  The sole
    exception to the active ``bots/`` namespace is the receipt-bound first
    strict system control, which is validated by its own materializer.
    """

    # PURGE execution-cache artifacts (``__pycache__``, ``.pytest_cache``,
    # ``*.pyc``, ``*.pyo``) left in the candidate bot dir by Worker
    # ``py_compile`` / import-time compilation.  ``strict_artifact_layout_errors``
    # (bot_namespace.py) FORBIDS these, so without this purge a candidate that
    # was py_compiled during quality gates reaches precommit contaminated and
    # ``resolve_bot`` raises ``artifact_execution_cache_directory_forbidden``
    # -> the native match produces 0W-0L-0D -> precommit wrongly fails as a
    # strategy regression (observed: v46).  This purge is the single highest-
    # leverage fix for that failure class.  It only removes well-known cache
    # names/suffixes inside the bot dir; it never touches source files.
    _purge_execution_cache(bot_dir)

    from bot_artifact import canonical_digest, hash_path
    from bot_namespace import (
        NATIONAL_RUNTIME_MANIFEST,
        POLICY_EPOCH_RECEIPT,
        artifact_contract_digest,
    )
    from national_runtime_authority import current_system_native_runtime_errors

    bot_dir = Path(bot_dir).absolute()
    if system_control:
        from first_strict_control import validate_materialized_control

        control_errors = validate_materialized_control(bot_dir)
        if control_errors:
            raise ValueError(
                f"invalid first-strict control {label}: "
                + ";".join(control_errors[:8])
            )
    else:
        # An in-flight crossover smoke candidate/opponent (synthetic
        # ``in_flight_crossover_smoke*`` label) is NOT under ``bots/`` and was
        # already structurally validated (the five strict ABI files) by the
        # outer smoke wrapper / _resolve_bot_or_in_flight.  Skip the
        # strict-namespace resolve_bot re-check for those labels so the match
        # executor's _prepare_native_spec does not reject the transient
        # workspace.  Published ``national_cloud_v*`` labels still get the full
        # resolve_bot identity check.
        if not str(label).startswith("in_flight_crossover_smoke"):
            resolved_label, resolved_path = resolve_bot(bot_dir)
            if resolved_label != label or resolved_path != bot_dir:
                raise ValueError(f"strict artifact resolution mismatch: {label}")

    runtime_errors = current_system_native_runtime_errors(bot_dir)
    if runtime_errors:
        raise ValueError(
            f"non_system_owned_native_runtime_forbidden:{label}:{runtime_errors[0]}"
        )
    contract_errors = check_native_contract(
        bot_dir,
        require_current_stream_decoder=True,
        require_current_decision_runtime=True,
    )
    if contract_errors:
        raise ValueError(
            f"{label}: invalid strict policy artifact: "
            + "; ".join(contract_errors[:5])
        )

    runtime_manifest = json.loads(
        (bot_dir / NATIONAL_RUNTIME_MANIFEST).read_text(encoding="utf-8")
    )
    epoch_receipt = json.loads(
        (bot_dir / POLICY_EPOCH_RECEIPT).read_text(encoding="utf-8")
    )
    artifact_hash = hash_path(bot_dir)
    if expected_artifact_hash and artifact_hash != expected_artifact_hash:
        raise ValueError(f"{label}: artifact hash does not match execution authority")
    core_digests = dict(runtime_manifest.get("files") or {})
    spec = NativeBotSpec(
        label=label,
        path=bot_dir,
        entry=bot_dir / NATIVE_ENTRY,
        artifact_hash=artifact_hash,
        entry_digest=str(core_digests.get(NATIVE_ENTRY) or ""),
        policy_digest=str(core_digests.get("policy.py") or ""),
        precompute_digest=str(core_digests.get(PRECOMPUTE_ENTRY) or ""),
        runtime_manifest_digest=canonical_digest(runtime_manifest),
        artifact_contract_digest=artifact_contract_digest(runtime_manifest),
        epoch_receipt_digest=canonical_digest(epoch_receipt),
    )
    if hash_path(bot_dir) != artifact_hash:
        raise ValueError(f"{label}: artifact changed while binding execution identity")
    return spec


async def _prepare_native_spec_bounded(
    label: str,
    bot_dir: Path,
    *,
    timing_plan: NativeMatchTimingPlan,
    system_control: bool = False,
    expected_artifact_hash: str = "",
) -> NativeBotSpec:
    """Bind one read-only artifact within the immutable preparation budget.

    Artifact validation performs bounded-size local reads and hashes only,
    before any Bot process or socket exists.  Keep it on the owning event-loop
    thread: the host default executor may be starved by unrelated match loads,
    while this five-file ABI check is deliberately small and deterministic.
    Wall time is measured against the frozen plan; an over-budget result is
    rejected and never receives process, socket, journal-completion, or
    callback authority.
    """

    timeout_sec = (
        timing_plan.artifact_preparation_per_bot_timeout_us / 1_000_000.0
    )
    if timeout_sec <= 0.0:
        raise RuntimeError("native artifact preparation timeout is invalid")
    started = time.monotonic()
    result = _prepare_native_spec(
        label,
        bot_dir,
        system_control=system_control,
        expected_artifact_hash=expected_artifact_hash,
    )
    if time.monotonic() - started > timeout_sec:
        raise RuntimeError(
            f"native_artifact_preparation_timeout:{label}"
        )
    return result


def _native_bot_seed(bot_seed_base: int | None, player_idx: int) -> int | None:
    if bot_seed_base is None:
        return None
    return int(bot_seed_base) + int(player_idx)


def _validate_formal_native_env_overrides(
    side: str,
    overrides: dict[str, str | int | None] | None,
) -> dict[str, str | int | None]:
    """Delegate to national_native_analysis."""
    return _nna._validate_formal_native_env_overrides(side, overrides)


def _trace_decisions_from_overrides(
    side: str,
    overrides: dict[str, str | int | None] | None,
) -> bool:
    """Delegate to national_native_analysis."""
    return _nna._trace_decisions_from_overrides(side, overrides)


def _parse_decision_trace(stderr_text: str) -> list[dict[str, Any]]:
    """Delegate to national_native_analysis."""
    return _nna._parse_decision_trace(stderr_text)


def _compact_native_hand_records(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Delegate to national_native_analysis."""
    return _nna._compact_native_hand_records(events)


def _safe_label_fragment(label: str) -> str:
    """Delegate to national_native_analysis."""
    return _nna._safe_label_fragment(label)


async def _execute_tcp_server_with_processes(
    bot_a: NativeBotSpec,
    bot_b: NativeBotSpec,
    *,
    timing_plan: NativeMatchTimingPlan,
    deck_seed_base: int | None,
    bot_seed_base: int | None = None,
    capture_events: bool = False,
    trace_decisions: bool = False,
    progress_callback: Any = None,
) -> dict[str, Any]:
    """Run one immutable timing plan through the native TCP server.

    Timing is intentionally not read from the parent environment here.  The
    caller has already frozen and validated ``timing_plan``; the same plan
    controls child launch, handshake, engine action timeout, whole-match
    watchdog, process drain, and downstream evidence.
    """

    hands = timing_plan.hands
    timeout_sec = timing_plan.effective_timeout_us / 1_000_000.0
    clients: list[NationalTCPClient] = []
    connected = asyncio.Event()
    events: list[dict[str, Any]] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        if len(clients) >= 2:
            writer.close()
            await writer.wait_closed()
            return
        clients.append(NationalTCPClient(reader, writer, idle_flush_sec=0.003))
        if len(clients) == 2:
            connected.set()

    # Server creation belongs to the same absolute startup watchdog as both
    # managed-client launches and the name handshake.  Keep the handle
    # nullable so a bind that fails or is cancelled by the watchdog can still
    # take the ordinary result/cleanup path without dereferencing a server
    # that never came into existence.
    server: asyncio.AbstractServer | None = None
    run_labels = [bot_a.label, bot_b.label]
    if run_labels[0] == run_labels[1]:
        run_labels = [f"{run_labels[0]}_A", f"{run_labels[1]}_B"]
    procs: list[subprocess.Popen] = []
    proc_streams = []
    process_isolation: dict[str, dict[str, Any]] = {}
    stdout_stderr: dict[str, dict[str, Any]] = {}
    log_temp_root = Path(tempfile.mkdtemp(prefix="pok_native_logs_"))
    bot_log_paths: dict[str, Path] = {}
    engine = None
    run_error = ""
    match_timeout_phase: str | None = None
    terminal_abort: dict[str, Any] | None = None
    connect_timeout = timing_plan.connect_timeout_us / 1_000_000.0
    name_timeout = timing_plan.name_timeout_us / 1_000_000.0
    action_timeout = timing_plan.protocol_action_timeout_us / 1_000_000.0
    process_drain_timeout = timing_plan.process_drain_timeout_us / 1_000_000.0
    bot_seeds: dict[str, int | None] = {}
    finalization_started_monotonic: float | None = None
    startup_complete = False

    async def report_finalizing_progress() -> None:
        """Transfer hand-70 liveness to the fixed cleanup/seal phase."""

        nonlocal finalization_started_monotonic
        if finalization_started_monotonic is not None:
            return
        finalization_started_monotonic = time.monotonic()
        if progress_callback is None:
            return
        try:
            callback_result = progress_callback({
                "event_type": "finalizing",
                "hand": hands,
                "phase_started_at_epoch": time.time(),
            })
            if asyncio.iscoroutine(callback_result):
                await callback_result
        except Exception:
            # Liveness telemetry is never execution authority.
            return

    async def report_engine_progress(event: dict[str, Any]) -> None:
        projection = _native_match_progress_projection(
            event,
            timing_plan=timing_plan,
        )
        if projection is None:
            return
        if progress_callback is not None:
            try:
                callback_result = progress_callback(projection)
                if asyncio.iscoroutine(callback_result):
                    await callback_result
            except Exception:
                # A sidecar heartbeat must never fabricate a successful match
                # or make the TCP engine less fail-closed.
                pass
        # A complete hand-70 settlement ends engine authority but not the
        # physical operation: child drain, replay normalization, spec re-hash
        # and (for bootstrap) durable journal completion remain.
        if (
            projection.get("event_type") == "settle"
            and int(projection.get("hand") or 0) == hands
        ):
            await report_finalizing_progress()

    try:
        loop = asyncio.get_running_loop()
        startup_deadline = (
            loop.time() + timing_plan.startup_timeout_us / 1_000_000.0
        )

        def startup_remaining(step_cap: float, phase: str) -> float:
            remaining = min(float(step_cap), startup_deadline - loop.time())
            if remaining <= 0.0:
                raise NativeMatchStartupTimeout(
                    f"native TCP startup watchdog expired during {phase}"
                )
            return remaining

        async with asyncio.timeout_at(startup_deadline):
            server = await asyncio.start_server(handle, "127.0.0.1", 0)
            # A coroutine that suppresses cancellation must not escape the
            # absolute deadline merely by returning after it.  This explicit
            # monotonic check also bounds the following synchronous socket
            # validation within the same immutable startup identity.
            startup_remaining(connect_timeout, "server_bind")
            sockets = tuple(server.sockets or ())
            if not sockets:
                raise RuntimeError("native TCP server exposed no listening socket")
            socket_name = sockets[0].getsockname()
            if (
                not isinstance(socket_name, tuple)
                or len(socket_name) < 2
                or not isinstance(socket_name[0], str)
                or not socket_name[0]
                or isinstance(socket_name[1], bool)
                or not isinstance(socket_name[1], int)
                or not 1 <= socket_name[1] <= 65_535
            ):
                raise RuntimeError("native TCP server listening socket is invalid")
            host, port = socket_name[:2]
            startup_remaining(connect_timeout, "server_socket_validation")
            for idx, (spec, label) in enumerate(zip((bot_a, bot_b), run_labels)):
                timing = (timing_plan.bot_a, timing_plan.bot_b)[idx].to_bot_timing()
                seed = _native_bot_seed(bot_seed_base, idx)
                bot_seeds[label] = seed
                log_path = None
                if native_entry_supports_log_arg(spec.entry):
                    log_path = log_temp_root / f"{idx}_{_safe_label_fragment(label)}.log"
                    bot_log_paths[label] = log_path
                stdout_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
                stderr_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
                try:
                    child_environment = (
                        {"POK_TRACE_DECISIONS": "1"} if trace_decisions else {}
                    )
                    try:
                        with EndpointLease.connect(
                            str(host),
                            int(port),
                            timeout=startup_remaining(
                                connect_timeout,
                                f"endpoint_connect_{idx}",
                            ),
                        ) as endpoint:
                            managed = launch_managed_bot(
                                spec.path,
                                endpoint,
                                entry_relative=spec.entry.relative_to(spec.path),
                                name=label,
                                decision_log=log_path,
                                seed=seed,
                                timing=timing,
                                environment=child_environment,
                                stdin=subprocess.DEVNULL,
                                stdout=stdout_file,
                                stderr=stderr_file,
                                expected_artifact_hash=spec.artifact_hash,
                                required_artifact_files=tuple(sorted(STRICT_ARTIFACT_FILES)),
                            )
                    except TimeoutError as exc:
                        raise NativeMatchStartupTimeout(
                            f"native TCP endpoint connect timed out for seat {idx}"
                        ) from exc
                    proc = managed.process
                    process_isolation[label] = asdict(managed.isolation)
                except Exception:
                    stdout_file.close()
                    stderr_file.close()
                    raise
                # Transfer process/stream ownership before yielding or checking
                # the absolute deadline, so a slow synchronous launch is still
                # cleaned up fail-closed.
                proc_streams.append((stdout_file, stderr_file))
                procs.append(proc)
                startup_remaining(connect_timeout, f"managed_launch_{idx}")
                await asyncio.sleep(0)
                startup_remaining(connect_timeout, f"managed_launch_{idx}")
            await asyncio.wait_for(
                connected.wait(),
                timeout=startup_remaining(connect_timeout, "socket_accept"),
            )
            for idx, client in enumerate(clients[:2]):
                await asyncio.wait_for(
                    client.send_message("name"),
                    timeout=startup_remaining(connect_timeout, f"name_request_{idx}"),
                )

            async def receive_name(client: NationalTCPClient, idx: int) -> str | None:
                step_timeout = startup_remaining(name_timeout, f"name_reply_{idx}")
                step_deadline = loop.time() + step_timeout
                value = await client.recv_name(timeout=step_timeout)
                if value is None and loop.time() >= step_deadline:
                    raise NativeMatchStartupTimeout(
                        f"native TCP name reply timed out for seat {idx}"
                    )
                return value

            name0 = await receive_name(clients[0], 0)
            name1 = await receive_name(clients[1], 1)
        if not name0 or not name1:
            raise RuntimeError("native TCP bot name handshake failed")
        startup_complete = True
        clients[0].name = name0
        clients[1].name = name1
        ordered_clients = clients
        clients_by_name = {client.name: client for client in clients}
        if run_labels[0] in clients_by_name and run_labels[1] in clients_by_name:
            ordered_clients = [clients_by_name[run_labels[0]], clients_by_name[run_labels[1]]]
            if ordered_clients != clients:
                events.append({
                    "type": "client_order",
                    "order": list(run_labels),
                    "connection_order": [name0, name1],
                })
        engine = NationalTCPGameEngine(
            ordered_clients,
            events,
            deck_seed_base=deck_seed_base,
            action_timeout_sec=action_timeout,
            event_sink=report_engine_progress,
        )
        # The engine liveness stopwatch starts only after both local clients
        # completed their bounded TCP/name startup phase.  Earlier launch time
        # is represented separately by the explicit execution phase budget.
        await report_engine_progress({
            "type": "engine_started",
            "hand": 1,
            "phase_started_at_epoch": time.time(),
        })
        match_task = asyncio.create_task(
            engine.run_limited_match(name0, name1, hands),
            name="native-tcp-limited-match",
        )
        try:
            done, _pending = await asyncio.wait(
                {match_task},
                timeout=timeout_sec,
            )
            if match_task not in done:
                # Only a task that is still pending at this exact outer
                # deadline exhausted the whole-match liveness envelope.  If
                # the engine itself raised TimeoutError, awaiting its completed
                # task below preserves that distinct transport/engine fact.
                match_timeout_phase = "whole_match_liveness"
                match_task.cancel()
                try:
                    await match_task
                except asyncio.CancelledError:
                    pass
                raise asyncio.TimeoutError(
                    "native TCP match exhausted whole-match liveness budget"
                )
            await match_task
        finally:
            if not match_task.done():
                match_task.cancel()
                try:
                    await match_task
                except asyncio.CancelledError:
                    pass
    except NativeMatchStartupTimeout as exc:
        match_timeout_phase = "startup_watchdog"
        run_error = f"{type(exc).__name__}: {str(exc)[:500]}"
    except asyncio.TimeoutError as exc:
        if not startup_complete:
            match_timeout_phase = "startup_watchdog"
            typed = NativeMatchStartupTimeout(
                "native TCP startup watchdog expired"
            )
            run_error = f"{type(typed).__name__}: {str(typed)[:500]}"
        else:
            run_error = f"{type(exc).__name__}: {str(exc)[:500]}"
    except NationalHandActionLimitExceeded as exc:
        limit_event = next(
            (
                event
                for event in reversed(events)
                if event.get("type") == "hand_action_limit_reached"
            ),
            {},
        )
        terminal_abort = {
            "code": "national_20000_chip_hand_action_limit_exceeded",
            "message": str(exc)[:500],
            "hand": limit_event.get("hand"),
            "limit": limit_event.get("limit"),
            "actions_observed": limit_event.get("actions_observed"),
        }
        run_error = f"{type(exc).__name__}: {str(exc)[:500]}"
    except BettingActionLimitExceeded as exc:
        limit_event = next(
            (
                event
                for event in reversed(events)
                if event.get("type") == "action_limit_reached"
            ),
            {},
        )
        terminal_abort = {
            "code": "engine_betting_round_action_limit_exceeded",
            "message": str(exc)[:500],
            "hand": limit_event.get("hand"),
            "stage": limit_event.get("stage"),
            "limit": limit_event.get("limit"),
            "actions_observed": limit_event.get("actions_observed"),
        }
        run_error = f"{type(exc).__name__}: {str(exc)[:500]}"
    except Exception as exc:
        run_error = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        try:
            if server is not None:
                server.close()
            for client in clients:
                await client.close(timeout=process_drain_timeout)
            if server is not None:
                try:
                    await asyncio.wait_for(
                        server.wait_closed(),
                        timeout=process_drain_timeout,
                    )
                except asyncio.TimeoutError:
                    pass
            for label, proc, streams in zip(run_labels, procs, proc_streams):
                stdout_file, stderr_file = streams
                stderr_note = ""
                try:
                    proc.wait(timeout=process_drain_timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=process_drain_timeout)
                    except subprocess.TimeoutExpired:
                        stderr_note = "process did not exit after kill"
                stdout_file.seek(0)
                stderr_file.seek(0)
                out = stdout_file.read() or ""
                err = stderr_file.read() or ""
                if stderr_note:
                    err = (err + "\n" + stderr_note).strip()
                bot_log_text = ""
                bot_log_path = bot_log_paths.get(label)
                if bot_log_path is not None:
                    try:
                        bot_log_text = bot_log_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except OSError as exc:
                        err = (err + f"\nfailed to read bot log: {exc}").strip()
                stdout_file.close()
                stderr_file.close()
                stdout_stderr[label] = {
                    "returncode": proc.returncode,
                    "stdout": out or "",
                    "stderr": err or "",
                    "bot_log": bot_log_text,
                    "bot_log_supported": label in bot_log_paths,
                }
        finally:
            shutil.rmtree(log_temp_root, ignore_errors=True)

    illegal = {
        0: sum(1 for e in events if e.get("type") == "action" and e.get("player_idx") == 0 and str(e.get("action", "")).startswith("illegal:")),
        1: sum(1 for e in events if e.get("type") == "action" and e.get("player_idx") == 1 and str(e.get("action", "")).startswith("illegal:")),
    }
    timeouts = {
        0: sum(1 for e in events if e.get("type") == "action" and e.get("player_idx") == 0 and e.get("action") == "timeout"),
        1: sum(1 for e in events if e.get("type") == "action" and e.get("player_idx") == 1 and e.get("action") == "timeout"),
    }
    earnings = getattr(engine, "total_earnings", [0, 0]) if engine is not None else [0, 0]
    hands_played = int(getattr(engine, "hand_num", 0) or 0) if engine is not None else 0
    settlements = [
        {
            "hand": int(event.get("hand", 0) or 0),
            "earnings": [int(value) for value in event.get("earnings", [0, 0])],
            "pot": int(event.get("pot", 0) or 0),
            "is_showdown": bool(event.get("is_showdown", False)),
            "winner_idx": event.get("winner_idx"),
            "reason": event.get("reason", ""),
        }
        for event in events
        if event.get("type") == "settle"
    ]
    per_player = {}
    issues: list[str] = []
    if run_error:
        issues.append(f"native_tcp_match_error={run_error}")
    for idx, label in enumerate(run_labels):
        spec = (bot_a, bot_b)[idx]
        proc_info = stdout_stderr.get(label, {})
        proc_failed = bool(proc_info.get("returncode") not in (0, None))
        stdout_text = str(proc_info.get("stdout") or "")
        stderr_text = str(proc_info.get("stderr") or "")
        bot_log_text = str(proc_info.get("bot_log") or "")
        decision_trace = _parse_decision_trace(stderr_text)
        bot_log_summary = (
            _parse_native_bot_log(bot_log_text)
            if bot_log_text
            else _empty_bot_log_summary()
        )
        runtime_telemetry = {
            "schema_version": 1,
            "server_action_latency": _server_action_latency(events, idx),
            "bot_log_supported": bool(proc_info.get("bot_log_supported")),
            "bot_log": bot_log_summary,
            "trace_decision_count": len(decision_trace),
        }
        per_player[label] = {
            "earnings": int(earnings[idx]),
            "illegal_actions": illegal[idx],
            "timeouts": timeouts[idx],
            "artifact_execution": spec.execution_identity(),
            "runtime_telemetry": runtime_telemetry,
            "native": {
                "returncode": proc_info.get("returncode"),
                "bot_seed": bot_seeds.get(label),
                "managed_isolation": process_isolation.get(label, {}),
                "stdout_tail": stdout_text[-2000:] if stdout_text else "",
                "stderr_tail": stderr_text[-2000:] if stderr_text else "",
                "bot_log_supported": bool(proc_info.get("bot_log_supported")),
                "decision_trace": decision_trace,
                "process_failures": 1 if proc_failed else 0,
                "json_response_stdout": 1 if '"response"' in stdout_text or "'response'" in stdout_text else 0,
            },
        }
        player_issues = []
        player_issues.extend(
            _system_native_name_handshake_issues(
                label,
                spec,
                proc_info,
                bot_log_summary,
            )
        )
        if illegal[idx]:
            player_issues.append(f"{label}: illegal_actions={illegal[idx]}")
        if timeouts[idx]:
            player_issues.append(f"{label}: timeouts={timeouts[idx]}")
        if proc_failed:
            player_issues.append(f"{label}: native_process_returncode={proc_info.get('returncode')}")
        if per_player[label]["native"]["json_response_stdout"]:
            player_issues.append(f"{label}: json_response_stdout")
        per_player[label]["compliance_issues"] = player_issues
        per_player[label]["passed_compliance"] = not player_issues
        issues.extend(player_issues)
    if hands_played != hands:
        issues.append(f"hands_played={hands_played}, expected={hands}")
    from bot_artifact import hash_path

    for run_label, spec in zip(run_labels, (bot_a, bot_b)):
        if hash_path(spec.path) != spec.artifact_hash:
            issue = f"{run_label}: artifact_changed_during_execution"
            issues.append(issue)
            per_player[run_label]["compliance_issues"].append(issue)
            per_player[run_label]["passed_compliance"] = False
    if finalization_started_monotonic is not None and (
        time.monotonic() - finalization_started_monotonic
        > timing_plan.cleanup_timeout_us / 1_000_000.0
    ):
        match_timeout_phase = "finalizing_cleanup"
        issue = "native_tcp_finalizing_cleanup_timeout"
        if issue not in issues:
            issues.append(issue)
    return {
        "bot_a": run_labels[0],
        "bot_b": run_labels[1],
        "hands_requested": hands,
        "hands_played": hands_played,
        "per_player": per_player,
        "net_chips_a": int(earnings[0]),
        "net_chips_b": int(earnings[1]),
        "net_chips_a_per_hand": round(int(earnings[0]) / max(1, hands_played), 3),
        "execution_mode": "native_tcp",
        "artifact_execution": {
            "schema_version": DIRECT_ARTIFACT_EXECUTION_SCHEMA_VERSION,
            "mode": "direct_content_bound_policy_artifact",
            "by_player": {
                run_labels[0]: bot_a.execution_identity(),
                run_labels[1]: bot_b.execution_identity(),
            },
        },
        "deck_seed_base": deck_seed_base,
        "bot_seed_base": bot_seed_base,
        "settlements": settlements,
        "hand_records": _compact_native_hand_records(events),
        "passed_compliance": not issues,
        "issues": issues,
        "events_tail": events[-20:],
        "native_match_timeout_phase": match_timeout_phase,
        "native_terminal_abort": terminal_abort,
        **({"events": list(events)} if capture_events else {}),
    }


async def _await_first_strict_control_completion(
    ticket: dict[str, Any],
    execution: dict[str, Any],
    *,
    deadline_monotonic: float,
) -> dict[str, Any]:
    """Await the bounded journal writer without blocking the asyncio loop.

    The synchronous writer owns the absolute deadline for flock and SQLite
    waits.  This coroutine intentionally has no cancelling outer timeout: it
    awaits the thread to natural completion, so returning from here proves no
    journal writer was detached.  An arbitrary kernel/fsync stall is not
    asynchronously interruptible; a successful COMMIT remains authoritative.
    """

    from first_strict_execution_journal import (
        complete_control_execution,
        control_execution_completion_deadline,
    )

    with control_execution_completion_deadline(deadline_monotonic):
        context = copy_context()
        executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="first-strict-journal",
        )
        cancellation: asyncio.CancelledError | None = None
        try:
            writer = executor.submit(
                context.run,
                complete_control_execution,
                ticket,
                execution=execution,
            )
            while not writer.done():
                try:
                    # Poll the concurrent Future with a loop-owned timer.  Do
                    # not wrap it in an asyncio Future: on this deployment a
                    # cross-thread completion wake can be lost, and use of the
                    # default executor can then hang asyncio.run() shutdown.
                    await asyncio.sleep(0.05)
                except asyncio.CancelledError as exc:
                    # The synchronous authority writer is not cancelled.
                    # Drain it even after repeated caller cancellation, then
                    # preserve the first cancellation below.
                    if cancellation is None:
                        cancellation = exc
            try:
                result = writer.result()
            except BaseException as exc:
                if cancellation is not None:
                    raise cancellation from exc
                raise
            if cancellation is not None:
                raise cancellation
            return result
        finally:
            # writer.done() is required before this point except if submit
            # itself failed.  The join is therefore local and cannot leave a
            # journal mutation detached from the caller.
            executor.shutdown(
                wait=True,
                cancel_futures=False,
            )


def _build_first_strict_runner_authority(execute_native_match):
    """Keep the one-shot completion authority inside the real runner closure.

    The execution ticket is only a durable workflow fence; it is deliberately
    not a capability and contains no secret.  A completion capability comes
    into existence only after the captured 70-hand TCP implementation above
    returns a terminal result for the exact content-bound candidate/control
    pair.  The opaque object is retained in process memory and is consumed once
    by the journal.  The only system-owned augmentation before sealing is the
    frozen full-match liveness budget, so lease, timer, replay, and receipt
    retain exactly the same timing identity.

    This protects the checkpoint/LLM/shell/public-API boundary.  As elsewhere
    in this repository, arbitrary same-UID Python memory inspection or runtime
    monkeypatching is outside the security boundary.
    """

    seal_lock = threading.Lock()
    pending_seals: dict[str, Any] = {}
    digest_chars = frozenset("0123456789abcdef")

    class RunnerSeal:
        __slots__ = (
            "nonce",
            "ticket_digest",
            "match_run_id",
            "deck_seed_base",
            "bot_seed_base",
            "execution_identity",
            "execution_digest",
            "engine_projection_digest",
            "bot_spec_digest",
        )

        def __init__(
            self,
            *,
            ticket_digest: str,
            match_run_id: str,
            deck_seed_base: int,
            bot_seed_base: int,
            execution: dict[str, Any],
            execution_digest: str,
            engine_projection_digest: str,
            bot_spec_digest: str,
        ) -> None:
            self.nonce = os.urandom(32)
            self.ticket_digest = ticket_digest
            self.match_run_id = match_run_id
            self.deck_seed_base = deck_seed_base
            self.bot_seed_base = bot_seed_base
            self.execution_identity = id(execution)
            self.execution_digest = execution_digest
            self.engine_projection_digest = engine_projection_digest
            self.bot_spec_digest = bot_spec_digest

    def valid_digest(value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(char in digest_chars for char in value)
        )

    def plain_int(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    def ticket_binding(ticket: Any) -> dict[str, Any]:
        from bot_artifact import canonical_digest
        from first_strict_execution_journal import (
            execution_scope_digest,
            normalize_execution_scope,
        )

        if not isinstance(ticket, dict) or set(ticket) != {
            "authority_run_id",
            "effect_id",
            "lease_epoch",
            "match_run_id",
            "input_payload",
        }:
            raise RuntimeError("first_strict_execution_runner_ticket_invalid")
        if not plain_int(ticket.get("lease_epoch")) or ticket["lease_epoch"] < 1:
            raise RuntimeError("first_strict_execution_runner_lease_invalid")
        input_payload = ticket.get("input_payload")
        if not isinstance(input_payload, dict) or set(input_payload) != {
            "scope",
            "scope_digest",
            "repeat",
            "deck_seed_base",
            "bot_seed_base",
            "hands",
            "timing_plan",
            "timing_plan_digest",
            "match_run_id",
        }:
            raise RuntimeError("first_strict_execution_runner_input_invalid")
        scope = normalize_execution_scope(input_payload.get("scope"))
        if input_payload.get("scope") != scope:
            raise RuntimeError("first_strict_execution_runner_scope_not_canonical")
        scope_digest = execution_scope_digest(scope)
        if input_payload.get("scope_digest") != scope_digest:
            raise RuntimeError("first_strict_execution_runner_scope_digest_mismatch")
        repeat = input_payload.get("repeat")
        deck_seed_base = input_payload.get("deck_seed_base")
        bot_seed_base = input_payload.get("bot_seed_base")
        if not plain_int(repeat) or not 1 <= repeat <= 8:
            raise RuntimeError("first_strict_execution_runner_repeat_invalid")
        if not plain_int(deck_seed_base) or not plain_int(bot_seed_base):
            raise RuntimeError("first_strict_execution_runner_seed_invalid")
        if bot_seed_base != deck_seed_base + 1_000_000_000:
            raise RuntimeError("first_strict_execution_runner_seed_relation_invalid")
        if input_payload.get("hands") != 70:
            raise RuntimeError("first_strict_execution_runner_hands_invalid")
        try:
            timing_plan = require_native_match_timing_plan(
                input_payload.get("timing_plan"),
                hands=70,
                requested_timeout_sec=LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
            )
        except ValueError as exc:
            raise RuntimeError(
                "first_strict_execution_runner_timing_plan_invalid"
            ) from exc
        if input_payload.get("timing_plan_digest") != timing_plan.digest():
            raise RuntimeError(
                "first_strict_execution_runner_timing_plan_digest_mismatch"
            )
        match_identity = {
            "scope": scope,
            "scope_digest": scope_digest,
            "repeat": repeat,
            "deck_seed_base": deck_seed_base,
            "bot_seed_base": bot_seed_base,
            "hands": 70,
            "timing_plan": timing_plan.snapshot(),
            "timing_plan_digest": timing_plan.digest(),
        }
        match_run_id = "first-strict-native:" + canonical_digest(match_identity)
        authority_run_id = f"first-strict-control:{scope_digest}"
        effect_id = f"{authority_run_id}:repeat-{repeat}"
        if (
            input_payload.get("match_run_id") != match_run_id
            or ticket.get("match_run_id") != match_run_id
            or ticket.get("authority_run_id") != authority_run_id
            or ticket.get("effect_id") != effect_id
        ):
            raise RuntimeError("first_strict_execution_runner_ticket_binding_mismatch")
        canonical_ticket = {
            "authority_run_id": authority_run_id,
            "effect_id": effect_id,
            "lease_epoch": ticket["lease_epoch"],
            "match_run_id": match_run_id,
            "input_payload": {**match_identity, "match_run_id": match_run_id},
        }
        if ticket != canonical_ticket:
            raise RuntimeError("first_strict_execution_runner_ticket_not_canonical")
        return {
            "ticket_digest": canonical_digest(canonical_ticket),
            "match_run_id": match_run_id,
            "deck_seed_base": deck_seed_base,
            "bot_seed_base": bot_seed_base,
            "scope": scope,
            "timing_plan": timing_plan,
        }

    def bot_spec_identity(
        spec: NativeBotSpec,
        *,
        expected_label: str,
        expected_artifact_hash: str,
    ) -> dict[str, Any]:
        from bot_artifact import canonical_digest, hash_path

        if (
            spec.label != expected_label
            or spec.entry != spec.path / NATIVE_ENTRY
            or not valid_digest(spec.artifact_hash)
            or spec.artifact_hash != expected_artifact_hash
            or not valid_digest(spec.entry_digest)
            or not valid_digest(spec.policy_digest)
            or not valid_digest(spec.precompute_digest)
            or not valid_digest(spec.runtime_manifest_digest)
            or not valid_digest(spec.artifact_contract_digest)
            or not valid_digest(spec.epoch_receipt_digest)
        ):
            raise RuntimeError("first_strict_execution_runner_bot_spec_invalid")
        if hash_path(spec.path) != spec.artifact_hash:
            raise RuntimeError("first_strict_execution_runner_artifact_hash_mismatch")
        if hashlib.sha256(spec.entry.read_bytes()).hexdigest() != spec.entry_digest:
            raise RuntimeError("first_strict_execution_runner_entry_digest_mismatch")
        identity = spec.execution_identity()
        if identity.get("identity_digest") != canonical_digest({
            key: value
            for key, value in identity.items()
            if key != "identity_digest"
        }):
            raise RuntimeError("first_strict_execution_runner_identity_digest_mismatch")
        return identity

    def engine_projection(execution: dict[str, Any]) -> dict[str, Any]:
        return {
            "execution_mode": execution.get("execution_mode"),
            "bot_a": execution.get("bot_a"),
            "bot_b": execution.get("bot_b"),
            "hands_requested": execution.get("hands_requested"),
            "hands_played": execution.get("hands_played"),
            "deck_seed_base": execution.get("deck_seed_base"),
            "bot_seed_base": execution.get("bot_seed_base"),
            "net_chips_a": execution.get("net_chips_a"),
            "net_chips_b": execution.get("net_chips_b"),
            "settlements": execution.get("settlements"),
            "hand_records": execution.get("hand_records"),
            "events": execution.get("events"),
            "native_match_timing_plan": execution.get("native_match_timing_plan"),
            "native_match_timing_plan_digest": execution.get(
                "native_match_timing_plan_digest"
            ),
            "native_full_match_liveness_budget": execution.get(
                "native_full_match_liveness_budget"
            ),
            "native_match_timeout_phase": execution.get("native_match_timeout_phase"),
            "native_terminal_abort": execution.get("native_terminal_abort"),
        }

    def validate_terminal_result(
        execution: Any,
        *,
        bot_a: NativeBotSpec,
        bot_b: NativeBotSpec,
        binding: dict[str, Any],
    ) -> tuple[str, str]:
        from bot_artifact import canonical_digest
        from first_strict_execution_journal import _terminal_execution_issues

        issues, _proof = _terminal_execution_issues(
            execution,
            deck_seed_base=binding["deck_seed_base"],
            bot_seed_base=binding["bot_seed_base"],
            timing_plan=binding["timing_plan"],
        )
        if issues:
            raise RuntimeError(
                "first_strict_execution_runner_terminal_invalid:"
                + ";".join(issues[:12])
            )
        if execution.get("bot_a") != bot_a.label or execution.get("bot_b") != bot_b.label:
            raise RuntimeError("first_strict_execution_runner_label_mismatch")
        artifact_execution = execution.get("artifact_execution") or {}
        by_player = artifact_execution.get("by_player") or {}
        if (
            artifact_execution.get("schema_version")
            != DIRECT_ARTIFACT_EXECUTION_SCHEMA_VERSION
            or artifact_execution.get("mode")
            != "direct_content_bound_policy_artifact"
            or set(by_player) != {bot_a.label, bot_b.label}
            or any(
                by_player.get(spec.label) != spec.execution_identity()
                for spec in (bot_a, bot_b)
            )
        ):
            raise RuntimeError(
                "first_strict_execution_runner_artifact_execution_invalid"
            )
        try:
            return (
                canonical_digest(execution),
                canonical_digest(engine_projection(execution)),
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "first_strict_execution_runner_result_not_canonical"
            ) from exc

    async def run_tcp_server_with_processes(
        bot_a: NativeBotSpec,
        bot_b: NativeBotSpec,
        *,
        hands: int,
        timing_plan: NativeMatchTimingPlan,
        deck_seed_base: int | None,
        bot_seed_base: int | None = None,
        capture_events: bool = False,
        trace_decisions: bool = False,
        progress_callback: Any = None,
        control_execution_ticket: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        binding = None
        before_specs = None
        if control_execution_ticket is not None:
            from bot_artifact import canonical_digest

            binding = ticket_binding(control_execution_ticket)
            scope = binding["scope"]
            if (
                hands != 70
                or deck_seed_base != binding["deck_seed_base"]
                or bot_seed_base != binding["bot_seed_base"]
                or capture_events is not True
                or bot_a.label == bot_b.label
                or timing_plan != binding["timing_plan"]
            ):
                raise RuntimeError("first_strict_execution_runner_arguments_mismatch")
            before_specs = [
                bot_spec_identity(
                    bot_a,
                    expected_label=scope["candidate_label"],
                    expected_artifact_hash=scope["candidate_artifact_hash"],
                ),
                bot_spec_identity(
                    bot_b,
                    expected_label=scope["control_id"],
                    expected_artifact_hash=scope["control_artifact_hash"],
                ),
            ]
            with seal_lock:
                if binding["ticket_digest"] in pending_seals:
                    raise RuntimeError("first_strict_execution_runner_seal_already_pending")
        execution = await execute_native_match(
            bot_a,
            bot_b,
            timing_plan=timing_plan,
            deck_seed_base=deck_seed_base,
            bot_seed_base=bot_seed_base,
            capture_events=capture_events,
            trace_decisions=trace_decisions,
            progress_callback=progress_callback,
        )
        completion_deadline_monotonic = (
            time.monotonic()
            + timing_plan.post_execution_completion_timeout_us / 1_000_000.0
        )

        def require_completion_deadline(phase: str) -> None:
            if time.monotonic() > completion_deadline_monotonic:
                raise RuntimeError(
                    f"native_post_execution_completion_timeout:{phase}"
                )

        # This must happen before terminal validation, the in-memory seal, and
        # the journal write.  A post-return append would make the outer
        # idempotent completion observe different evidence bytes.
        execution = _annotate_native_full_match_liveness(execution, timing_plan)
        require_completion_deadline("timing_annotation")
        if binding is not None:
            from bot_artifact import canonical_digest

            scope = binding["scope"]
            after_specs = [
                bot_spec_identity(
                    bot_a,
                    expected_label=scope["candidate_label"],
                    expected_artifact_hash=scope["candidate_artifact_hash"],
                ),
                bot_spec_identity(
                    bot_b,
                    expected_label=scope["control_id"],
                    expected_artifact_hash=scope["control_artifact_hash"],
                ),
            ]
            if after_specs != before_specs:
                raise RuntimeError("first_strict_execution_runner_bot_spec_changed")
            require_completion_deadline("artifact_rehash")
            execution_digest, projection_digest = validate_terminal_result(
                execution,
                bot_a=bot_a,
                bot_b=bot_b,
                binding=binding,
            )
            require_completion_deadline("terminal_validation")
            spec_digest = canonical_digest({"bot_specs": after_specs})
            seal = RunnerSeal(
                ticket_digest=binding["ticket_digest"],
                match_run_id=binding["match_run_id"],
                deck_seed_base=binding["deck_seed_base"],
                bot_seed_base=binding["bot_seed_base"],
                execution=execution,
                execution_digest=execution_digest,
                engine_projection_digest=projection_digest,
                bot_spec_digest=spec_digest,
            )
            with seal_lock:
                if binding["ticket_digest"] in pending_seals:
                    raise RuntimeError("first_strict_execution_runner_seal_already_pending")
                pending_seals[binding["ticket_digest"]] = seal
            # Persist the terminal body at the real runner boundary before it
            # can escape to a higher layer.  The journal commits the complete
            # replay in the same fenced SQLite transaction, consumes this seal
            # only after that commit, and can reconstruct its file projection
            # after a process death.  The outer caller's second completion call
            # is an exact, idempotent reference lookup.
            # The same absolute monotonic boundary must govern validation,
            # command-lock acquisition, SQLite busy waits, and the atomic
            # effect/event commit.  The helper fully awaits its internally
            # bounded writer, preserving finalizing/reproof without detaching.
            await _await_first_strict_control_completion(
                control_execution_ticket,
                execution,
                deadline_monotonic=completion_deadline_monotonic,
            )
        return execution

    def _matched_runner_execution_seal(
        ticket: Any,
        execution: dict[str, Any],
    ) -> tuple[dict[str, Any], RunnerSeal]:
        from bot_artifact import canonical_digest

        binding = ticket_binding(ticket)
        with seal_lock:
            seal = pending_seals.get(binding["ticket_digest"])
        if seal is None:
            raise RuntimeError("first_strict_execution_runner_seal_missing")
        try:
            execution_digest = canonical_digest(execution)
            projection_digest = canonical_digest(engine_projection(execution))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "first_strict_execution_runner_result_not_canonical"
            ) from exc
        if (
            not isinstance(seal, RunnerSeal)
            or seal.ticket_digest != binding["ticket_digest"]
            or seal.match_run_id != binding["match_run_id"]
            or seal.deck_seed_base != binding["deck_seed_base"]
            or seal.bot_seed_base != binding["bot_seed_base"]
            or seal.execution_identity != id(execution)
            or seal.execution_digest != execution_digest
            or seal.engine_projection_digest != projection_digest
            or not valid_digest(seal.bot_spec_digest)
        ):
            raise RuntimeError("first_strict_execution_runner_seal_mismatch")

        return binding, seal

    def validate_runner_execution_seal(
        ticket: Any,
        execution: dict[str, Any],
    ) -> None:
        """Prove a real terminal runner result without consuming its authority."""

        _matched_runner_execution_seal(ticket, execution)

    def consume_runner_execution_seal(
        ticket: Any,
        execution: dict[str, Any],
    ) -> None:
        """Commit-consume a previously validated seal after durable completion."""

        binding, seal = _matched_runner_execution_seal(ticket, execution)
        with seal_lock:
            current = pending_seals.get(binding["ticket_digest"])
            if current is not seal:
                raise RuntimeError("first_strict_execution_runner_seal_mismatch")
            del pending_seals[binding["ticket_digest"]]

    return (
        run_tcp_server_with_processes,
        validate_runner_execution_seal,
        consume_runner_execution_seal,
    )


(
    _run_tcp_server_with_processes,
    _validate_first_strict_runner_execution_seal,
    _consume_first_strict_runner_execution_seal,
) = _build_first_strict_runner_authority(_execute_tcp_server_with_processes)
del _execute_tcp_server_with_processes


async def run_native_tcp_pair(
    bot_a_token: str | Path,
    bot_b_token: str | Path,
    hands: int,
    *,
    deck_seed_base: int | None = None,
    bot_seed_base: int | None = None,
    timeout_sec: float | None = None,
    timing_plan: NativeMatchTimingPlan | dict[str, Any] | None = None,
    bot_a_env_overrides: dict[str, str | int | None] | None = None,
    bot_b_env_overrides: dict[str, str | int | None] | None = None,
    native_full_match_liveness_budget: dict[str, float | int] | None = None,
    capture_events: bool = False,
    sanitize_parent_environment: bool = True,
    progress_callback: Any = None,
) -> dict[str, Any]:
    """Execute both strict policy artifacts directly over national raw TCP."""

    return await _run_direct_artifact_tcp_pair(
        bot_a_token,
        bot_b_token,
        hands,
        deck_seed_base=deck_seed_base,
        bot_seed_base=bot_seed_base,
        timeout_sec=timeout_sec,
        timing_plan=timing_plan,
        bot_a_env_overrides=bot_a_env_overrides,
        bot_b_env_overrides=bot_b_env_overrides,
        native_full_match_liveness_budget=native_full_match_liveness_budget,
        capture_events=capture_events,
        sanitize_parent_environment=sanitize_parent_environment,
        progress_callback=progress_callback,
    )


async def run_native_strength_pair(
    bot_a_token: str | Path,
    bot_b_token: str | Path,
    hands: int,
    *,
    deck_seed_base: int | None = None,
    bot_seed_base: int | None = None,
    timeout_sec: float | None = None,
    timing_plan: NativeMatchTimingPlan | dict[str, Any] | None = None,
    bot_a_env_overrides: dict[str, str | int | None] | None = None,
    bot_b_env_overrides: dict[str, str | int | None] | None = None,
    native_full_match_liveness_budget: dict[str, float | int] | None = None,
    capture_events: bool = False,
    sanitize_parent_environment: bool = True,
    control_execution_ticket: dict[str, Any] | None = None,
    progress_callback: Any = None,
) -> dict[str, Any]:
    """Execute one local-strength sample from the exact submitted artifacts."""

    return await _run_direct_artifact_tcp_pair(
        bot_a_token,
        bot_b_token,
        hands,
        deck_seed_base=deck_seed_base,
        bot_seed_base=bot_seed_base,
        timeout_sec=timeout_sec,
        timing_plan=timing_plan,
        bot_a_env_overrides=bot_a_env_overrides,
        bot_b_env_overrides=bot_b_env_overrides,
        native_full_match_liveness_budget=native_full_match_liveness_budget,
        capture_events=capture_events,
        sanitize_parent_environment=sanitize_parent_environment,
        control_execution_ticket=control_execution_ticket,
        progress_callback=progress_callback,
    )


async def _run_direct_artifact_tcp_pair(
    bot_a_token,
    bot_b_token,
    hands,
    *,
    deck_seed_base=None,
    bot_seed_base=None,
    timeout_sec=None,
    timing_plan=None,
    bot_a_env_overrides=None,
    bot_b_env_overrides=None,
    native_full_match_liveness_budget=None,
    capture_events=False,
    sanitize_parent_environment=True,
    control_execution_ticket=None,
    progress_callback=None,
):
    """Delegate to national_native_tcp_exec."""
    return await _nte._run_direct_artifact_tcp_pair(
        bot_a_token,
        bot_b_token,
        hands,
        deck_seed_base=deck_seed_base,
        bot_seed_base=bot_seed_base,
        timeout_sec=timeout_sec,
        timing_plan=timing_plan,
        bot_a_env_overrides=bot_a_env_overrides,
        bot_b_env_overrides=bot_b_env_overrides,
        native_full_match_liveness_budget=native_full_match_liveness_budget,
        capture_events=capture_events,
        sanitize_parent_environment=sanitize_parent_environment,
        control_execution_ticket=control_execution_ticket,
        progress_callback=progress_callback,
    )


def _acceptance_opponent_runtime_mode(label: str, path: Path) -> str:
    return _nn._acceptance_opponent_runtime_mode(label, path)


async def run_native_tcp_smoke(
    candidate_token: str | Path,
    *,
    source_v: int | None = None,
    opponent_token: str | Path | None = None,
    self_play: bool = False,
    hands: int = 1,
    timeout_sec: float | None = 90.0,
    timing_plan: NativeMatchTimingPlan | dict[str, Any] | None = None,
    progress_callback: Any = None,
    in_flight_candidate_dir: str | Path | None = None,
    in_flight_opponent_dir: str | Path | None = None,
) -> dict[str, Any]:
    return await _nn.run_native_tcp_smoke(
        candidate_token,
        source_v=source_v,
        opponent_token=opponent_token,
        self_play=self_play,
        hands=hands,
        timeout_sec=timeout_sec,
        timing_plan=timing_plan,
        progress_callback=progress_callback,
        in_flight_candidate_dir=in_flight_candidate_dir,
        in_flight_opponent_dir=in_flight_opponent_dir,
    )


def _summary_from_results(bots: list[tuple[str, Path]], results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return _nn._summary_from_results(bots, results)


async def run_native_acceptance_for_candidate(
    candidate_token: str | Path,
    *,
    source_v: int | None = None,
    opponent_tokens: list[str | Path] | None = None,
    hands: int = 70,
    max_opponents: int = 2,
    timeout_sec: float | None = None,
    timing_plan: NativeMatchTimingPlan | dict[str, Any] | None = None,
    progress_callback: Any = None,
) -> NationalAcceptanceResult:
    return await _nn.run_native_acceptance_for_candidate(
        candidate_token,
        source_v=source_v,
        opponent_tokens=opponent_tokens,
        hands=hands,
        max_opponents=max_opponents,
        timeout_sec=timeout_sec,
        timing_plan=timing_plan,
        progress_callback=progress_callback,
    )


def _mean(values: list[int]) -> float | None:
    return _nn._mean(values)


def _rounded(value: float | None) -> float | None:
    return _nn._rounded(value)


def _ci(values: list[int]) -> tuple[float | None, float | None]:
    return _nn._ci(values)


def _first_strict_batch_progress(
    *,
    batch_plan: dict[str, Any],
    control_execution_scope: dict[str, Any],
    timing_plan: NativeMatchTimingPlan,
    completed_receipts: list[dict[str, Any]],
    state: str,
    next_repeat: int | None,
) -> dict[str, Any]:
    return _nn._first_strict_batch_progress(
        batch_plan=batch_plan,
        control_execution_scope=control_execution_scope,
        timing_plan=timing_plan,
        completed_receipts=completed_receipts,
        state=state,
        next_repeat=next_repeat,
    )


async def run_native_precommit(
    candidate_token: str | Path,
    opponents: list[dict[str, Any]],
    *,
    hands: int = 70,
    matches_per_opponent: int = 1,
    parent_label: str = "",
    deck_seed_base: int | None = 91_000,
    sample_plan: list[dict[str, Any]] | None = None,
    batch_plan: dict[str, Any] | None = None,
    control_execution_scope: dict[str, Any] | None = None,
    cancel_token: threading.Event | None = None,
    timing_plan: NativeMatchTimingPlan | dict[str, Any] | None = None,
    progress_callback: Any = None,
) -> dict[str, Any]:
    return await _nn.run_native_precommit(
        candidate_token,
        opponents,
        hands=hands,
        matches_per_opponent=matches_per_opponent,
        parent_label=parent_label,
        deck_seed_base=deck_seed_base,
        sample_plan=sample_plan,
        batch_plan=batch_plan,
        control_execution_scope=control_execution_scope,
        cancel_token=cancel_token,
        timing_plan=timing_plan,
        progress_callback=progress_callback,
    )

