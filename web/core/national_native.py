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
    """Return fail-closed raw-name launch issues for the checked-in runtime.

    Legacy/non-strict fixtures intentionally do not carry a system-owned
    log contract.  Only an entry whose bound digest is exactly this runtime
    template *and* whose managed launch supplied a decision log is required
    to emit the name/worker evidence below.
    """

    expected_entry_digest = hashlib.sha256(
        NATIVE_BOT_TEMPLATE.encode("utf-8")
    ).hexdigest()
    if (
        spec.entry_digest != expected_entry_digest
        or process_info.get("bot_log_supported") is not True
    ):
        return []
    handshake = bot_log_summary.get("name_handshake")
    if not isinstance(handshake, dict):
        return [f"{label}: native_name_handshake_missing"]

    def count(field: str) -> int:
        value = handshake.get(field)
        return value if isinstance(value, int) and not isinstance(value, bool) else -1

    issues: list[str] = []
    received = count("received_count")
    malformed = count("malformed_count")
    if malformed > 0:
        issues.append(f"{label}: native_name_handshake_malformed")
    if handshake.get("available") is not True or received <= 0:
        issues.append(f"{label}: native_name_handshake_missing")
        return issues
    if received != 1:
        issues.append(
            f"{label}: native_name_handshake_repeated count={received}"
        )
        return issues
    if count("sent_count") != 1:
        issues.append(f"{label}: native_name_handshake_missing_raw_reply")
    generations = handshake.get("worker_generations")
    generation_valid = (
        isinstance(generations, list)
        and len(generations) == 1
        and isinstance(generations[0], int)
        and not isinstance(generations[0], bool)
        and generations[0] >= 1
    )
    if (
        count("worker_launch_started_count") != 1
        or count("worker_launch_ok_count") != 1
        or count("worker_launch_failed_count") != 0
        or not generation_valid
    ):
        issues.append(f"{label}: native_name_handshake_launch_failed")
    return issues


def _artifact_execution_is_valid(
    payload: Any,
    expected_artifacts: dict[str, str],
) -> bool:
    """Validate the compact execution identity without reopening bot code."""

    from bot_artifact import canonical_digest

    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return False
    if payload.get("mode") != "direct_content_bound_policy_artifact":
        return False
    by_player = payload.get("by_player")
    if not isinstance(by_player, dict) or set(by_player) != set(expected_artifacts):
        return False
    for label, expected_hash in expected_artifacts.items():
        identity = by_player.get(label)
        if not isinstance(identity, dict):
            return False
        unsigned = {
            key: value for key, value in identity.items() if key != "identity_digest"
        }
        if (
            identity.get("mode") != "direct_content_bound_policy_artifact"
            or identity.get("label") != label
            or identity.get("artifact_hash") != expected_hash
            or identity.get("identity_digest") != canonical_digest(unsigned)
        ):
            return False
    return True


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
    """

    bot_dir = Path(bot_dir)
    from national_runtime_authority import current_system_native_runtime_errors

    identity_errors = current_system_native_runtime_errors(bot_dir)
    if identity_errors:
        return [
            f"{NATIVE_ENTRY}: current system-owned stream decoder required: {error}"
            for error in identity_errors
        ]

    # Tokens are checked on the system authority, never by reopening the
    # candidate after the byte-identity read above.
    text = NATIVE_BOT_TEMPLATE

    required_tokens = (
        "NATIONAL_STREAM_DECODER_VERSION = 2",
        "class NationalStreamDecoder",
        "has_pending_numeric",
        "flush_idle",
        "select.select",
    )
    missing = [token for token in required_tokens if token not in text]
    if missing:
        return [
            f"{NATIVE_ENTRY}: missing stream decoder v2 token {token!r}; "
            "new candidates must defer terminal numeric messages until a following token or idle flush"
            for token in missing
        ]

    try:
        with tempfile.TemporaryDirectory(prefix="pok_system_decoder_probe_") as raw_tmp:
            probe_root = Path(raw_tmp)
            probe_entry = probe_root / NATIVE_ENTRY
            descriptor = os.open(
                probe_entry,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o400,
            )
            try:
                content = NATIVE_BOT_TEMPLATE.encode("utf-8")
                with os.fdopen(descriptor, "wb", closefd=False) as writer:
                    writer.write(content)
                    writer.flush()
                    os.fsync(writer.fileno())
            finally:
                os.close(descriptor)
            proc = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    _NATIVE_STREAM_PROBE_SCRIPT,
                    str(probe_entry),
                ],
                cwd=str(probe_root),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"{NATIVE_ENTRY}: stream decoder behavior probe failed: {type(exc).__name__}: {exc}"]
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "no output")[-500:].strip()
        return [
            f"{NATIVE_ENTRY}: stream decoder behavior probe exited {proc.returncode}: {detail}"
        ]
    try:
        payload = json.loads(proc.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        return [
            f"{NATIVE_ENTRY}: stream decoder behavior probe returned invalid JSON: "
            f"{type(exc).__name__}: {proc.stdout[-300:]!r}"
        ]
    return [
        f"{NATIVE_ENTRY}: stream decoder behavior violation: {item}"
        for item in (payload.get("errors") or [])[:20]
    ]


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
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(text, node) or ""
    return None


def _policy_decision_has_exception_pass(text: str) -> bool:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_policy_decision":
            for child in ast.walk(node):
                if isinstance(child, ast.Try):
                    for handler in child.handlers:
                        if (
                            _handler_catches_broad_exception(handler)
                            and len(handler.body) == 1
                            and isinstance(handler.body[0], ast.Pass)
                        ):
                            return True
    return False


def _handler_catches_broad_exception(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    if isinstance(handler.type, ast.Name):
        return handler.type.id in {"Exception", "BaseException"}
    if isinstance(handler.type, ast.Tuple):
        return any(
            isinstance(item, ast.Name) and item.id in {"Exception", "BaseException"}
            for item in handler.type.elts
        )
    return False


def _bot_version(label: str) -> int:
    return parse_bot_version(label) or -1


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
    """Validate the complete caller-controlled environment ABI.

    The managed executor does not inherit arbitrary ``POK_*`` variables.  An
    unknown explicit override must therefore be rejected, not accepted and
    silently discarded as if an experiment or gate had actually run.
    """

    normalized = dict(overrides or {})
    unknown = sorted(
        str(key)
        for key in normalized
        if str(key) not in FORMAL_NATIVE_ENV_OVERRIDE_KEYS
    )
    if unknown:
        raise ValueError(
            f"unsupported formal native environment override ({side}):"
            + ",".join(unknown)
        )
    for raw_key, value in normalized.items():
        key = str(raw_key)
        if key in _FORMAL_NATIVE_TIMING_OVERRIDE_KEYS:
            raise ValueError(
                "formal native timing is fixed by NativeMatchTimingPlan:"
                f"{side}:{key}"
            )
        if key == "POK_TRACE_DECISIONS" and value is not None:
            if str(value) not in {"0", "1"}:
                raise ValueError(
                    f"invalid formal native trace override ({side}):{key}"
                )
    return normalized


def _trace_decisions_from_overrides(
    side: str,
    overrides: dict[str, str | int | None] | None,
) -> bool:
    """Accept only an explicit non-timing trace switch for a child process."""

    normalized = _validate_formal_native_env_overrides(side, overrides)
    return str(normalized.get("POK_TRACE_DECISIONS") or "0") == "1"


def _parse_decision_trace(stderr_text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_line in stderr_text.splitlines():
        if not raw_line.startswith(TRACE_PREFIX):
            continue
        payload = raw_line[len(TRACE_PREFIX):]
        try:
            row = json.loads(payload)
        except Exception:
            rows.append({"type": "parse_error", "raw": payload[:1000]})
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _compact_native_hand_records(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compile native engine events into bounded, replay-safe per-hand facts."""
    hands: dict[int, dict[str, Any]] = {}
    pending_requests: dict[tuple[int, int, str], list[dict[str, Any]]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        try:
            hand = int(event.get("hand", 0) or 0)
        except (TypeError, ValueError):
            continue
        if hand <= 0:
            continue
        row = hands.setdefault(hand, {
            "hand": hand,
            "sb_idx": None,
            "bb_idx": None,
            "hole_cards": [[], []],
            "board": [],
            "actions": [],
            "starting_pot": 150,
            "settlement": None,
        })
        event_type = event.get("type")
        if event_type == "hand_start":
            row["sb_idx"] = event.get("sb_idx")
            row["bb_idx"] = event.get("bb_idx")
            row["starting_pot"] = int(event.get("pot", 150) or 150)
        elif event_type == "cards_dealt":
            cards = event.get("hole_cards")
            if isinstance(cards, list) and len(cards) == 2:
                row["hole_cards"] = cards
        elif event_type == "stage":
            cards = event.get("cards") or []
            if isinstance(cards, list):
                row["board"].extend(str(card) for card in cards)
        elif event_type == "action_requested":
            try:
                player_idx = int(event.get("player_idx"))
            except (TypeError, ValueError):
                continue
            stage = str(event.get("stage") or "unknown")
            pending_requests.setdefault((hand, player_idx, stage), []).append({
                "pot_before": event.get("pot"),
                "player_bets_before": event.get("player_bets"),
                "timeout_budget_sec": event.get("timeout_budget_sec"),
            })
        elif event_type == "action":
            try:
                player_idx = int(event.get("player_idx"))
            except (TypeError, ValueError):
                player_idx = event.get("player_idx")
            stage = str(event.get("stage") or "unknown")
            queue = pending_requests.get((hand, player_idx, stage)) or []
            request = queue.pop(0) if queue else {}
            if not queue:
                pending_requests.pop((hand, player_idx, stage), None)
            pot_before = request.get("pot_before")
            pot_after = event.get("pot")
            # Check/fold/timeout events do not carry an engine-side post-action
            # pot.  Their legal action commits no chips, so the request pot is
            # also the truthful post-action pot.
            if pot_after is None:
                pot_after = pot_before
            row["actions"].append({
                "player_idx": player_idx,
                "stage": stage,
                "action": str(event.get("action") or "unknown"),
                "amount": event.get("amount"),
                "pot_before": pot_before,
                "pot_after": pot_after,
                "player_bets_before": request.get("player_bets_before"),
                "decision_wait_sec": event.get("decision_wait_sec"),
                "timeout_budget_sec": request.get(
                    "timeout_budget_sec", event.get("timeout_budget_sec")
                ),
            })
        elif event_type == "settle":
            row["settlement"] = {
                key: event.get(key)
                for key in (
                    "earnings", "pot", "is_showdown", "winner_idx", "reason",
                    "sb_cards", "bb_cards", "community", "sb_hand", "bb_hand",
                )
            }
            if event.get("community"):
                row["board"] = list(event.get("community") or [])
    return [
        hands[hand]
        for hand in sorted(hands)
        if isinstance(hands[hand].get("settlement"), dict)
    ]


def _safe_label_fragment(label: str) -> str:
    safe = "".join(
        char if char.isascii() and (char.isalnum() or char in "_.-") else "_"
        for char in label
    )
    return safe[:80] or "bot"


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
    bot_a_token: str | Path,
    bot_b_token: str | Path,
    hands: int,
    *,
    deck_seed_base: int | None,
    bot_seed_base: int | None,
    timeout_sec: float | None,
    timing_plan: NativeMatchTimingPlan | dict[str, Any] | None = None,
    bot_a_env_overrides: dict[str, str | int | None] | None = None,
    bot_b_env_overrides: dict[str, str | int | None] | None = None,
    native_full_match_liveness_budget: dict[str, float | int] | None = None,
    capture_events: bool = False,
    sanitize_parent_environment: bool = True,
    control_execution_ticket: dict[str, Any] | None = None,
    progress_callback: Any = None,
) -> dict[str, Any]:
    if not sanitize_parent_environment:
        raise ValueError("native strength timing must not inherit parent environment")
    if native_full_match_liveness_budget is not None:
        raise ValueError(
            "raw native full-match liveness budgets are not execution authority; "
            "pass the immutable timing_plan instead"
        )
    trace_decisions = (
        _trace_decisions_from_overrides("bot_a", bot_a_env_overrides)
        or _trace_decisions_from_overrides("bot_b", bot_b_env_overrides)
    )
    if control_execution_ticket is not None and (
        capture_events is not True
        or int(hands) != 70
    ):
        raise ValueError(
            "first strict control ticket requires one captured 70-hand "
            "direct-artifact match"
        )
    label_a, dir_a = resolve_bot(bot_a_token)
    system_control_b = control_execution_ticket is not None
    if control_execution_ticket is not None:
        from first_strict_execution_journal import normalize_execution_scope

        ticket_input = control_execution_ticket.get("input_payload") or {}
        ticket_scope = normalize_execution_scope(ticket_input.get("scope"))
        if label_a != ticket_scope["candidate_label"]:
            raise ValueError("first strict candidate label mismatch")
        dir_b = Path(bot_b_token).absolute()
        label_a = ticket_scope["candidate_label"]
        label_b = ticket_scope["control_id"]
    else:
        ticket_scope = {}
        label_b, dir_b = resolve_bot(bot_b_token)
    hands = max(1, min(70, int(hands)))
    frozen_timing_plan = _resolve_native_match_timing_plan(
        timing_plan,
        hands=hands,
        requested_timeout_sec=timeout_sec,
    )
    capacity_owner = (
        f"native_tcp:{label_a}:{label_b}:{os.getpid()}:{time.monotonic_ns()}"
    )
    capacity_lease = None
    bound_progress_callback = None
    # This digest is a runtime-only prelaunch identity, not replay or strength
    # evidence.  It is fixed before capacity wait and artifact preparation so
    # the exact provider dispatch can prove liveness across the whole bounded
    # operation.  Artifact bytes are independently bound by NativeBotSpec
    # before either process or socket is launched.
    match_run_nonce = uuid.uuid4().hex
    match_identity_digest = _canonical_timing_digest({
        "schema_version": 3,
        "identity_kind": "runtime_only_native_prelaunch",
        "bot_a_label": label_a,
        "bot_a_path": str(dir_a.absolute()),
        "bot_b_label": label_b,
        "bot_b_path": str(dir_b.absolute()),
        "system_control_b": system_control_b,
        "hands": hands,
        "deck_seed_base": deck_seed_base,
        "bot_seed_base": bot_seed_base,
        "timing_plan_digest": frozen_timing_plan.digest(),
        "control_match_run_id": str(
            (control_execution_ticket or {}).get("match_run_id") or ""
        ),
        "match_run_nonce": match_run_nonce,
    })
    operation_started_at_epoch: float | None = None
    engine_phase_started_at: float | None = None
    finalizing_phase_started_at: float | None = None
    terminal_progress_reported = False
    terminal_outcome = "runner_raised"
    launch_heartbeat_stop = asyncio.Event()
    launch_heartbeat_task: asyncio.Task | None = None

    async def bound_progress_callback(projection: dict[str, Any]) -> bool:
        nonlocal operation_started_at_epoch
        nonlocal engine_phase_started_at, finalizing_phase_started_at
        nonlocal terminal_progress_reported
        if progress_callback is None:
            return False
        if not isinstance(projection, dict):
            return False
        event_type = str(projection.get("event_type") or "")
        terminal_event = (
            projection.get("terminal") is True or event_type == "terminal"
        )
        if terminal_event:
            if terminal_progress_reported:
                return True
            outcome = str(projection.get("terminal_outcome") or "")
            if outcome not in {
                "runner_returned",
                "runner_raised",
                "runner_cancelled",
            }:
                return False
            # This event is consumed by the identity-aware reporter; it never
            # becomes a persistent liveness projection.
            enriched = {
                "event_type": "terminal",
                "terminal": True,
                "terminal_outcome": outcome,
                "match_identity_digest": match_identity_digest,
                "timing_plan_digest": frozen_timing_plan.digest(),
            }
        elif event_type == "launching":
            try:
                phase_started_at = float(
                    projection.get("phase_started_at_epoch")
                )
            except (TypeError, ValueError):
                return False
            if not math.isfinite(phase_started_at) or phase_started_at <= 0.0:
                return False
            if operation_started_at_epoch is None:
                operation_started_at_epoch = phase_started_at
            elif phase_started_at != operation_started_at_epoch:
                return False
            if engine_phase_started_at is not None:
                return False
            phase_budget_us = frozen_timing_plan.launch_timeout_us
            enriched = {
                **dict(projection),
                "hand": None,
                "liveness_phase": "launching",
                "phase_started_at_epoch": phase_started_at,
                "phase_budget_us": phase_budget_us,
                "match_identity_digest": match_identity_digest,
                "timing_plan_digest": frozen_timing_plan.digest(),
                "hands": frozen_timing_plan.hands,
                "effective_timeout_us": frozen_timing_plan.effective_timeout_us,
                "operation_started_at_epoch": operation_started_at_epoch,
                "operation_deadline_epoch": (
                    operation_started_at_epoch
                    + frozen_timing_plan.first_strict_lease_timeout_us
                    / 1_000_000.0
                ),
                "operation_budget_us": (
                    frozen_timing_plan.first_strict_lease_timeout_us
                ),
                "phase_deadline_epoch": (
                    phase_started_at + phase_budget_us / 1_000_000.0
                ),
            }
        elif event_type == "finalizing":
            if engine_phase_started_at is None or operation_started_at_epoch is None:
                return False
            if projection.get("hand") != frozen_timing_plan.hands:
                return False
            try:
                phase_started_at = float(
                    projection.get("phase_started_at_epoch")
                )
            except (TypeError, ValueError):
                return False
            if not math.isfinite(phase_started_at) or phase_started_at <= 0.0:
                return False
            if finalizing_phase_started_at is None:
                finalizing_phase_started_at = phase_started_at
            elif phase_started_at != finalizing_phase_started_at:
                return False
            phase_budget_us = frozen_timing_plan.finalization_timeout_us
            enriched = {
                **dict(projection),
                "liveness_phase": "finalizing",
                "phase_started_at_epoch": finalizing_phase_started_at,
                "phase_budget_us": phase_budget_us,
                "match_identity_digest": match_identity_digest,
                "timing_plan_digest": frozen_timing_plan.digest(),
                "hands": frozen_timing_plan.hands,
                "effective_timeout_us": frozen_timing_plan.effective_timeout_us,
                "operation_started_at_epoch": operation_started_at_epoch,
                "operation_deadline_epoch": (
                    operation_started_at_epoch
                    + frozen_timing_plan.first_strict_lease_timeout_us
                    / 1_000_000.0
                ),
                "operation_budget_us": (
                    frozen_timing_plan.first_strict_lease_timeout_us
                ),
                "phase_deadline_epoch": (
                    finalizing_phase_started_at
                    + phase_budget_us / 1_000_000.0
                ),
            }
        else:
            if event_type == "engine_started":
                try:
                    engine_phase_started_at = float(
                        projection.get("phase_started_at_epoch")
                    )
                except (TypeError, ValueError):
                    return False
                if (
                    not math.isfinite(engine_phase_started_at)
                    or engine_phase_started_at <= 0.0
                ):
                    return False
                launch_heartbeat_stop.set()
            if engine_phase_started_at is None or operation_started_at_epoch is None:
                # Only the trusted runner can declare the actual engine
                # boundary.  Do not derive it from an arbitrary first
                # action/settlement callback.
                return False
            phase_budget_us = frozen_timing_plan.effective_timeout_us
            enriched = {
                **dict(projection),
                "liveness_phase": "engine_running",
                "phase_started_at_epoch": engine_phase_started_at,
                "phase_budget_us": phase_budget_us,
                "match_identity_digest": match_identity_digest,
                "timing_plan_digest": frozen_timing_plan.digest(),
                "hands": frozen_timing_plan.hands,
                "effective_timeout_us": frozen_timing_plan.effective_timeout_us,
                "operation_started_at_epoch": operation_started_at_epoch,
                "operation_deadline_epoch": (
                    operation_started_at_epoch
                    + frozen_timing_plan.first_strict_lease_timeout_us
                    / 1_000_000.0
                ),
                "operation_budget_us": (
                    frozen_timing_plan.first_strict_lease_timeout_us
                ),
                "phase_deadline_epoch": (
                    engine_phase_started_at + phase_budget_us / 1_000_000.0
                ),
            }
        try:
            callback_result = progress_callback(enriched)
            if asyncio.iscoroutine(callback_result):
                callback_result = await callback_result
            # A reporter may explicitly reject an identity-mismatched or
            # failed unlink.  Only an acknowledged terminal clear suppresses
            # the outer finally retry; generic callbacks returning None retain
            # backward-compatible success semantics.
            if terminal_event and callback_result is not False:
                terminal_progress_reported = True
            return callback_result is not False
        except Exception:
            # The native engine remains authoritative.  A failed
            # orchestrator sidecar write must not change the match result.
            return False

    async def refresh_launch_progress(phase_started_at_epoch: float) -> None:
        """Refresh freshness only; the launch phase deadline stays immutable."""

        while not launch_heartbeat_stop.is_set():
            try:
                await asyncio.wait_for(
                    launch_heartbeat_stop.wait(),
                    timeout=NATIVE_LAUNCH_HEARTBEAT_INTERVAL_SEC,
                )
                return
            except asyncio.TimeoutError:
                accepted = await bound_progress_callback({
                    "event_type": "launching",
                    "phase_started_at_epoch": phase_started_at_epoch,
                })
                if not accepted:
                    return

    try:
        # Launch liveness begins before the bounded capacity and preparation
        # phases.  A provider reaching this tool near its original deadline
        # can therefore receive exactly one plan-bound extension instead of
        # timing out while a valid first-strict lease still owns the effect.
        if progress_callback is not None:
            launch_started_at_epoch = time.time()
            launch_accepted = await bound_progress_callback({
                "event_type": "launching",
                "phase_started_at_epoch": launch_started_at_epoch,
            })
            if launch_accepted:
                launch_heartbeat_task = asyncio.create_task(
                    refresh_launch_progress(launch_started_at_epoch),
                    name="native-tcp-launch-heartbeat",
                )
        # Queue duration is part of the immutable timing plan.  In particular
        # the first-strict journal ticket is claimed before this wait, so the
        # ticket's system-owned lease covers this bounded interval.
        capacity_lease = await acquire_match_slots_async(
            capacity_owner,
            count=1,
            timeout=frozen_timing_plan.capacity_queue_timeout_us / 1_000_000.0,
        )
        if progress_callback is not None and operation_started_at_epoch is not None:
            await bound_progress_callback({
                "event_type": "launching",
                "phase_started_at_epoch": operation_started_at_epoch,
            })
        spec_a = await _prepare_native_spec_bounded(
            label_a,
            dir_a,
            timing_plan=frozen_timing_plan,
            expected_artifact_hash=str(
                ticket_scope.get("candidate_artifact_hash") or ""
            ),
        )
        if progress_callback is not None and operation_started_at_epoch is not None:
            await bound_progress_callback({
                "event_type": "launching",
                "phase_started_at_epoch": operation_started_at_epoch,
            })
        spec_b = await _prepare_native_spec_bounded(
            label_b,
            dir_b,
            timing_plan=frozen_timing_plan,
            system_control=system_control_b,
            expected_artifact_hash=str(
                ticket_scope.get("control_artifact_hash") or ""
            ),
        )
        if progress_callback is not None and operation_started_at_epoch is not None:
            await bound_progress_callback({
                "event_type": "launching",
                "phase_started_at_epoch": operation_started_at_epoch,
            })
        runner_kwargs = {
            "hands": hands,
            "timing_plan": frozen_timing_plan,
            "deck_seed_base": deck_seed_base,
            "bot_seed_base": bot_seed_base,
            "capture_events": capture_events,
            "trace_decisions": trace_decisions,
            "progress_callback": (
                bound_progress_callback if progress_callback is not None else None
            ),
        }
        if control_execution_ticket is not None:
            runner_kwargs["control_execution_ticket"] = control_execution_ticket
        result = await _run_tcp_server_with_processes(
            spec_a,
            spec_b,
            **runner_kwargs,
        )
        if control_execution_ticket is not None:
            # The control runner seals and journals this exact object.  Do not
            # copy or mutate it after return, or the outer idempotent journal
            # completion would correctly reject the changed replay bytes.
            if not isinstance(result, dict) or validate_native_match_timing_evidence(
                result,
                timing_plan=frozen_timing_plan,
            ):
                raise RuntimeError(
                    "first strict control runner timing evidence missing or drifted"
                )
        else:
            # The production runner already annotates before returning.  Keep
            # this idempotent adapter for isolated direct-runner test doubles.
            result = _annotate_native_full_match_liveness(result, frozen_timing_plan)
        terminal_outcome = "runner_returned"
        return result
    finally:
        # A completed/failed match must not leave its last `settle` sidecar
        # eligible to extend a later non-engine provider stall.  The reporter
        # recognizes this terminal projection and clears only its own
        # checkpoint-bound heartbeat; ordinary callbacks may ignore it.
        launch_heartbeat_stop.set()
        if launch_heartbeat_task is not None:
            if not launch_heartbeat_task.done():
                launch_heartbeat_task.cancel()
            try:
                await launch_heartbeat_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if capacity_lease is not None:
            capacity_lease.release()
        if progress_callback is not None and not terminal_progress_reported:
            try:
                task = asyncio.current_task()
                final_outcome = (
                    "runner_cancelled"
                    if task is not None and task.cancelling()
                    else terminal_outcome
                )
                await bound_progress_callback({
                    "event_type": "terminal",
                    "terminal": True,
                    "terminal_outcome": final_outcome,
                })
            except Exception:
                pass


def _acceptance_opponent_runtime_mode(label: str, path: Path) -> str:
    """Prove that an acceptance opponent is a strict direct artifact."""

    resolved_label, resolved_path = resolve_bot(path)
    if resolved_label != label or resolved_path != Path(path).absolute():
        raise RuntimeError("strict_policy_opponent_identity_mismatch")
    return "direct_content_bound_policy_artifact"


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
) -> dict[str, Any]:
    """Run a minimal direct-TCP national smoke match for a candidate bot."""
    hands = max(1, min(70, int(hands)))
    try:
        candidate_label, candidate_dir = resolve_bot(candidate_token)
    except Exception as exc:
        return {
            "passed": False,
            "execution_mode": "native_tcp",
            "hands": hands,
            "issues": [f"native_smoke_candidate_error={type(exc).__name__}: {str(exc)[:300]}"],
            "outcome": "candidate_failure",
            "failure_side": "candidate",
        }

    if self_play and opponent_token is not None:
        return {
            "candidate": candidate_label,
            "passed": False,
            "execution_mode": "native_tcp",
            "hands": hands,
            "issues": ["native_smoke_self_play_and_opponent_are_mutually_exclusive"],
            "outcome": "infrastructure_failure",
            "failure_side": "harness",
        }
    if self_play:
        opponents = [(candidate_label, candidate_dir)]
    elif opponent_token is not None:
        try:
            opponents = [resolve_bot(opponent_token)]
        except Exception as exc:
            return {
                "candidate": candidate_label,
                "passed": False,
                "execution_mode": "native_tcp",
                "hands": hands,
                "issues": [f"native_smoke_opponent_error={type(exc).__name__}: {str(exc)[:300]}"],
                "outcome": "infrastructure_failure",
                "failure_side": "opponent",
            }
    else:
        opponents = select_acceptance_opponents(candidate_label, source_v, limit=1)

    if not opponents:
        return {
            "candidate": candidate_label,
            "passed": False,
            "execution_mode": "native_tcp",
            "hands": hands,
            "issues": ["native_smoke_no_opponent"],
            "outcome": "infrastructure_failure",
            "failure_side": "opponent",
        }

    opponent_label, opponent_dir = opponents[0]
    try:
        opponent_mode = _acceptance_opponent_runtime_mode(
            opponent_label,
            opponent_dir,
        )
        result = await run_native_tcp_pair(
            candidate_dir,
            opponent_dir,
            hands,
            timeout_sec=timeout_sec,
            timing_plan=timing_plan,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        return {
            "candidate": candidate_label,
            "opponent": opponent_label,
            "passed": False,
            "execution_mode": "native_tcp",
            "hands": hands,
            "issues": [f"native_smoke_exception={type(exc).__name__}: {str(exc)[:500]}"],
            "outcome": "infrastructure_failure",
            "failure_side": "harness",
        }

    player_rows = list((result.get("per_player") or {}).values())
    if self_play:
        candidate_issues = [
            str(issue)
            for row in player_rows
            for issue in (row.get("compliance_issues") or [])
        ]
        opponent_issues = []
    else:
        candidate_row = (result.get("per_player") or {}).get(candidate_label) or {}
        opponent_row = (result.get("per_player") or {}).get(opponent_label) or {}
        candidate_issues = list(candidate_row.get("compliance_issues") or [])
        opponent_issues = list(opponent_row.get("compliance_issues") or [])
    attributed = set(candidate_issues + opponent_issues)
    unscoped_issues = [
        str(item) for item in result.get("issues") or []
        if str(item) not in attributed
    ]
    if candidate_issues:
        outcome, failure_side, issues = "candidate_failure", "candidate", candidate_issues
    elif opponent_issues or unscoped_issues:
        outcome, failure_side = "infrastructure_failure", (
            "opponent" if opponent_issues and not unscoped_issues else "harness"
        )
        issues = opponent_issues + unscoped_issues
    else:
        outcome, failure_side, issues = "passed", "", []
    passed = outcome == "passed"
    return {
        "candidate": candidate_label,
        "opponent": opponent_label,
        "self_play": bool(self_play),
        "opponent_runtime_mode": opponent_mode,
        "passed": passed,
        "execution_mode": "native_tcp",
        "artifact_execution": result.get("artifact_execution") or {},
        "native_full_match_liveness_budget": result.get(
            "native_full_match_liveness_budget"
        ),
        "native_match_timing_plan": result.get("native_match_timing_plan"),
        "native_match_timing_plan_digest": result.get(
            "native_match_timing_plan_digest"
        ),
        "native_match_timeout_phase": result.get("native_match_timeout_phase"),
        "native_terminal_abort": result.get("native_terminal_abort"),
        "hands": hands,
        "issues": issues,
        "outcome": outcome,
        "failure_side": failure_side,
        "result": result,
    }


def _summary_from_results(bots: list[tuple[str, Path]], results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    runtime_rows: dict[str, list[dict[str, Any]]] = {label: [] for label, _ in bots}
    summary = {
        label: {
            "matches": 0,
            "net_chips": 0,
            "illegal_actions": 0,
            "timeouts": 0,
            "native_process_failures": 0,
            "json_response_stdout": 0,
            "artifact_executions": [],
            "passed_compliance": True,
            "runtime_telemetry": _empty_runtime_telemetry(),
        }
        for label, _ in bots
    }
    for result in results:
        for label, pdata in result["per_player"].items():
            row = summary[label]
            row["matches"] += 1
            row["net_chips"] += int(pdata.get("earnings", 0) or 0)
            row["illegal_actions"] += int(pdata.get("illegal_actions", 0) or 0)
            row["timeouts"] += int(pdata.get("timeouts", 0) or 0)
            runtime_rows.setdefault(label, []).append(pdata.get("runtime_telemetry", {}) or {})
            native = pdata.get("native", {}) or {}
            row["native_process_failures"] += int(native.get("process_failures", 0) or 0)
            row["json_response_stdout"] += int(native.get("json_response_stdout", 0) or 0)
            row["artifact_executions"].append(
                dict(pdata.get("artifact_execution") or {})
            )
            row["passed_compliance"] = (
                row["passed_compliance"]
                and bool(pdata.get("passed_compliance", result.get("passed_compliance", False)))
            )
    for label, rows in runtime_rows.items():
        if label in summary:
            summary[label]["runtime_telemetry"] = _merge_runtime_telemetry(rows)
    return summary


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
    candidate = resolve_bot(candidate_token)
    if opponent_tokens:
        opponents = [resolve_bot(token) for token in opponent_tokens]
    else:
        opponents = select_acceptance_opponents(candidate[0], source_v, limit=max_opponents)
    bots = [candidate] + [opp for opp in opponents if opp[0] != candidate[0]]
    if len(bots) < 2:
        return NationalAcceptanceResult(
            candidate=candidate[0],
            opponents=[],
            hands_per_pair=hands,
            passed=False,
            outcome="infrastructure_failure",
            failure_side="opponent",
            issues=["need at least one opponent for native national acceptance"],
            summary={"passed_compliance": False},
            report={"execution_mode": "native_tcp"},
        )
    pair_indices = [(0, idx) for idx in range(1, len(bots))]
    if timeout_sec is None:
        timeout_sec = max(180.0, float(hands * len(pair_indices) * 5))

    results: list[dict[str, Any]] = []
    opponent_runtime_modes: dict[str, str] = {}
    try:
        for pair_index, (i, j) in enumerate(pair_indices):
            pair_seed = 71_000 + pair_index * 1_000
            bot_seed = 171_000 + pair_index * 1_000
            mode = _acceptance_opponent_runtime_mode(bots[j][0], bots[j][1])
            opponent_runtime_modes[bots[j][0]] = mode
            result = await run_native_tcp_pair(
                bots[i][1],
                bots[j][1],
                hands,
                deck_seed_base=pair_seed,
                bot_seed_base=bot_seed,
                timeout_sec=timeout_sec,
                timing_plan=timing_plan,
                progress_callback=progress_callback,
            )
            results.append(result)
    except TimeoutError:
        issue = f"native_national_acceptance_timeout: exceeded {timeout_sec:g}s"
        return NationalAcceptanceResult(
            candidate=candidate[0],
            opponents=[opp[0] for opp in bots[1:]],
            hands_per_pair=hands,
            passed=False,
            outcome="infrastructure_failure",
            failure_side="harness",
            issues=[issue],
            summary={
                "matches": 0,
                "net_chips": 0,
                "passed_compliance": False,
            },
            report={
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "hands_per_pair": hands,
                "execution_mode": "native_tcp",
                "candidate_only": True,
                "timeout_sec": timeout_sec,
                "timed_out": True,
                "issues": [issue],
            },
        )

    summary = _summary_from_results(bots, results)
    matrix: dict[str, dict[str, Any]] = {label: {} for label, _ in bots}
    for result in results:
        a = result["bot_a"]
        b = result["bot_b"]
        matrix[a][b] = {
            "net_chips": result["net_chips_a"],
            "per_hand": result["net_chips_a_per_hand"],
            "passed_compliance": result["passed_compliance"],
            "artifact_execution": result.get("artifact_execution") or {},
            "native_full_match_liveness_budget": result.get(
                "native_full_match_liveness_budget"
            ),
            "native_match_timing_plan": result.get("native_match_timing_plan"),
            "native_match_timing_plan_digest": result.get(
                "native_match_timing_plan_digest"
            ),
            "native_match_timeout_phase": result.get("native_match_timeout_phase"),
            "native_terminal_abort": result.get("native_terminal_abort"),
            "issues": result["issues"],
        }
        matrix[b][a] = {
            "net_chips": result["net_chips_b"],
            "per_hand": round(result["net_chips_b"] / max(1, result["hands_played"]), 3),
            "passed_compliance": result["passed_compliance"],
            "artifact_execution": result.get("artifact_execution") or {},
            "native_full_match_liveness_budget": result.get(
                "native_full_match_liveness_budget"
            ),
            "native_match_timing_plan": result.get("native_match_timing_plan"),
            "native_match_timing_plan_digest": result.get(
                "native_match_timing_plan_digest"
            ),
            "native_match_timeout_phase": result.get("native_match_timeout_phase"),
            "native_terminal_abort": result.get("native_terminal_abort"),
            "issues": result["issues"],
        }
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "hands_per_pair": hands,
        "execution_mode": "native_tcp",
        "artifact_executions": [
            dict(result.get("artifact_execution") or {}) for result in results
        ],
        "pair_count": len(pair_indices),
        "bots": [{"label": label, "path": str(path)} for label, path in bots],
        "results": results,
        "full_match_liveness_budgets": [
            result.get("native_full_match_liveness_budget")
            for result in results
        ],
        "native_match_timing_plans": [
            result.get("native_match_timing_plan") for result in results
        ],
        "native_match_timing_plan_digests": [
            result.get("native_match_timing_plan_digest") for result in results
        ],
        "opponent_runtime_modes": opponent_runtime_modes,
        "summary": summary,
        "matrix": matrix,
        "candidate_only": True,
        "timeout_sec": timeout_sec,
    }
    candidate_summary = summary.get(candidate[0], {})
    candidate_issues: list[str] = []
    opponent_issues: list[str] = []
    unscoped_issues: list[str] = []
    for result in results:
        rows = result.get("per_player") or {}
        candidate_issues.extend((rows.get(candidate[0]) or {}).get("compliance_issues") or [])
        for opponent in bots[1:]:
            opponent_issues.extend((rows.get(opponent[0]) or {}).get("compliance_issues") or [])
        attributed = set(candidate_issues + opponent_issues)
        unscoped_issues.extend(
            str(item) for item in result.get("issues") or []
            if str(item) not in attributed
        )
    if candidate_issues:
        outcome, failure_side, issues = "candidate_failure", "candidate", candidate_issues
    elif opponent_issues or unscoped_issues:
        outcome = "infrastructure_failure"
        failure_side = "opponent" if opponent_issues and not unscoped_issues else "harness"
        issues = opponent_issues + unscoped_issues
    else:
        outcome, failure_side, issues = "passed", "", []
    return NationalAcceptanceResult(
        candidate=candidate[0],
        opponents=[opp[0] for opp in bots[1:]],
        hands_per_pair=hands,
        passed=outcome == "passed" and bool(candidate_summary.get("passed_compliance")),
        outcome=outcome,
        failure_side=failure_side,
        issues=issues,
        summary=candidate_summary,
        matrix=matrix.get(candidate[0], {}),
        report=report,
    )


def _mean(values: list[int]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _rounded(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def _ci(values: list[int]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    return paired_bootstrap_ci(values)


def _first_strict_batch_progress(
    *,
    batch_plan: dict[str, Any],
    control_execution_scope: dict[str, Any],
    timing_plan: NativeMatchTimingPlan,
    completed_receipts: list[dict[str, Any]],
    state: str,
    next_repeat: int | None,
) -> dict[str, Any]:
    """Build a replay-validated durable projection for a partial v143 batch.

    The journal remains the sole execution authority.  This projection is the
    checkpoint-visible index that proves which ordered physical samples are
    already complete and which exact sample may be requested next.  It does
    not contain raw replay bytes and it never authorizes an unverified receipt.
    """

    from bot_artifact import canonical_digest
    from first_strict_execution_journal import (
        execution_scope_digest,
        normalize_execution_scope,
        read_control_execution_receipt,
    )

    if state not in {"pending_next_sample", "waiting_live_lease", "completed"}:
        raise RuntimeError("first_strict_batch_progress_state_invalid")
    scope = normalize_execution_scope(control_execution_scope)
    expected_digest = str(batch_plan.get("batch_plan_digest") or "")
    if (
        batch_plan.get("schema_version") != 1
        or batch_plan.get("authority") != "native_precommit_batch_v1"
        or len(expected_digest) != 64
        or batch_plan.get("timing_plan_digest") != timing_plan.digest()
        or batch_plan.get("max_new_samples_per_invocation") != 1
    ):
        raise RuntimeError("first_strict_batch_plan_invalid")
    raw_rows = batch_plan.get("ordered_samples")
    if not isinstance(raw_rows, list) or len(raw_rows) != 8:
        raise RuntimeError("first_strict_batch_plan_rows_invalid")
    scope_digest = execution_scope_digest(scope)
    planned_rows: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_rows, start=1):
        if not isinstance(raw, dict):
            raise RuntimeError("first_strict_batch_plan_row_invalid")
        repeat = raw.get("repeat")
        deck_seed = raw.get("deck_seed_base")
        bot_seed = raw.get("bot_seed_base")
        if (
            raw.get("opponent") != "first_strict_control_v1"
            or raw.get("opponent_index") != 0
            or repeat != index
            or type(deck_seed) is not int
            or type(bot_seed) is not int
            or bot_seed != deck_seed + 1_000_000_000
            or raw.get("native_match_timing_plan_digest") != timing_plan.digest()
        ):
            raise RuntimeError("first_strict_batch_plan_row_binding_invalid")
        match_identity = {
            "scope": scope,
            "scope_digest": scope_digest,
            "repeat": repeat,
            "deck_seed_base": deck_seed,
            "bot_seed_base": bot_seed,
            "hands": 70,
            "timing_plan": timing_plan.snapshot(),
            "timing_plan_digest": timing_plan.digest(),
        }
        planned_rows.append({
            "repeat": repeat,
            "deck_seed_base": deck_seed,
            "bot_seed_base": bot_seed,
            "match_run_id": "first-strict-native:" + canonical_digest(
                match_identity
            ),
        })
    if batch_plan.get("sample_plan_digest") != canonical_digest({
        "sample_plan": [
            {
                "opponent": "first_strict_control_v1",
                "opponent_index": 0,
                "repeat": row["repeat"],
                "deck_seed_base": row["deck_seed_base"],
                "bot_seed_base": row["bot_seed_base"],
                "native_match_timing_plan_digest": timing_plan.digest(),
            }
            for row in planned_rows
        ]
    }):
        raise RuntimeError("first_strict_batch_sample_digest_invalid")
    if batch_plan.get("batch_plan_digest") != canonical_digest({
        key: value
        for key, value in batch_plan.items()
        if key != "batch_plan_digest"
    }):
        raise RuntimeError("first_strict_batch_digest_invalid")

    completed_by_repeat: dict[int, dict[str, Any]] = {}
    for entry in completed_receipts:
        if not isinstance(entry, dict) or type(entry.get("repeat")) is not int:
            raise RuntimeError("first_strict_batch_completed_entry_invalid")
        repeat = int(entry["repeat"])
        if repeat in completed_by_repeat or not 1 <= repeat <= len(planned_rows):
            raise RuntimeError("first_strict_batch_completed_repeat_invalid")
        receipt = entry.get("execution_receipt")
        evidence, issues = read_control_execution_receipt(
            receipt,
            expected_scope=scope,
        )
        if issues or not isinstance(evidence, dict):
            raise RuntimeError(
                "first_strict_batch_completed_receipt_invalid:"
                + ";".join(str(issue) for issue in issues[:8])
            )
        expected = planned_rows[repeat - 1]
        input_payload = evidence.get("input") or {}
        result_payload = evidence.get("result") or {}
        if (
            input_payload.get("repeat") != repeat
            or input_payload.get("deck_seed_base") != expected["deck_seed_base"]
            or input_payload.get("bot_seed_base") != expected["bot_seed_base"]
            or input_payload.get("match_run_id") != expected["match_run_id"]
            or result_payload.get("match_run_id") != expected["match_run_id"]
            or receipt.get("match_run_id") != expected["match_run_id"]
        ):
            raise RuntimeError("first_strict_batch_completed_binding_invalid")
        completed_by_repeat[repeat] = {
            **expected,
            "execution_receipt": dict(receipt),
        }
    completed_repeats = sorted(completed_by_repeat)
    if completed_repeats != list(range(1, len(completed_repeats) + 1)):
        raise RuntimeError("first_strict_batch_completed_order_invalid")
    if next_repeat is not None and (
        type(next_repeat) is not int
        or not 1 <= next_repeat <= len(planned_rows)
        or next_repeat != len(completed_repeats) + 1
    ):
        raise RuntimeError("first_strict_batch_next_repeat_invalid")
    if state == "completed" and (
        next_repeat is not None or len(completed_repeats) != len(planned_rows)
    ):
        raise RuntimeError("first_strict_batch_completion_invalid")
    return {
        "schema_version": 1,
        "kind": "first-strict-native-precommit-batch-progress",
        "state": state,
        "batch_plan_digest": expected_digest,
        "sample_plan_digest": batch_plan.get("sample_plan_digest"),
        "scope_digest": scope_digest,
        "candidate_artifact_hash": scope["candidate_artifact_hash"],
        "control_artifact_hash": scope["control_artifact_hash"],
        "timing_plan_digest": timing_plan.digest(),
        "sample_count": len(planned_rows),
        "max_new_samples_per_invocation": 1,
        "planned_samples": planned_rows,
        "completed_samples": [completed_by_repeat[key] for key in completed_repeats],
        "next_repeat": next_repeat,
    }


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
    from bot_artifact import hash_path

    candidate = resolve_bot(candidate_token)
    hands = int(hands)
    if hands != 70:
        raise ValueError(
            f"native precommit strength samples must contain exactly 70 hands; got {hands}"
        )
    precommit_timing_plan = _resolve_native_match_timing_plan(
        timing_plan,
        hands=hands,
        requested_timeout_sec=LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
    )
    matches_per_opponent = max(1, int(matches_per_opponent))
    matchups: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    aggregate_net_chips: list[int] = []
    total_wins = total_losses = total_draws = 0
    resolved_opponents: list[dict[str, Any]] = []
    frozen_samples: dict[tuple[str, int], dict[str, Any]] = {}
    if sample_plan is not None:
        for row in sample_plan:
            if not isinstance(row, dict):
                raise ValueError("native precommit sample plan contains a non-object row")
            key = (str(row.get("opponent") or ""), int(row.get("repeat") or 0))
            if not key[0] or key[1] < 1 or key in frozen_samples:
                raise ValueError("native precommit sample plan has an invalid or duplicate key")
            frozen_samples[key] = dict(row)
        expected_rows = len(opponents) * matches_per_opponent
        if len(frozen_samples) != expected_rows:
            raise ValueError(
                f"native precommit sample plan has {len(frozen_samples)} rows; "
                f"expected {expected_rows}"
            )
    system_control_items = [
        item
        for item in opponents
        if str(item.get("authority") or "") == "system_first_strict_control"
    ]
    first_strict_batch_plan: dict[str, Any] | None = None
    if system_control_items:
        if len(system_control_items) != 1 or len(opponents) != 1:
            raise ValueError("first strict control batch shape is invalid")
        if sample_plan is None or not isinstance(batch_plan, dict):
            raise ValueError("first strict control batch plan is missing")
        try:
            from precommit_eval_contract import build_native_precommit_batch_plan

            expected_batch_plan = build_native_precommit_batch_plan(
                list(sample_plan),
                native_timing_plan=precommit_timing_plan,
                first_strict_control=True,
            )
        except Exception as exc:
            raise ValueError("first strict control batch plan is invalid") from exc
        if batch_plan != expected_batch_plan:
            raise ValueError("first strict control batch plan drifted")
        first_strict_batch_plan = expected_batch_plan
    elif batch_plan is not None:
        raise ValueError("ordinary native precommit must not carry a batch plan")
    if not opponents:
        blockers.append({"reason": "native_no_opponents", "details": "Native precommit requires at least one opponent."})
    def raise_if_cancelled() -> None:
        if cancel_token is not None and cancel_token.is_set():
            raise asyncio.CancelledError(
                "native precommit attempt cancelled before the next full match"
            )

    completed_batch_receipts: list[dict[str, Any]] = []
    new_batch_samples = 0
    for opp_index, item in enumerate(opponents):
        raise_if_cancelled()
        reason = str(item.get("reason") or "precommit")
        token = item.get("path") or item.get("token") or item.get("name")
        system_control = str(item.get("authority") or "") == "system_first_strict_control"
        if system_control:
            from first_strict_control import validate_control_receipt
            from first_strict_execution_journal import normalize_execution_scope

            control_receipt = item.get("control_receipt") or {}
            control_identity = control_receipt.get("control") or {}
            opponent = (
                str(item.get("name") or control_identity.get("control_id") or ""),
                Path(str(token)).absolute(),
            )
            if str(opponent[1]) != str(control_identity.get("path") or ""):
                raise RuntimeError("first_strict_control_path_binding_mismatch")
            control_active_bots = list(
                control_receipt.get("active_policy_bots") or []
            )

            expected_control_flags = {
                "precommit_gate_admitted": True,
                "formal_bootstrap_opponent_admitted": True,
                "strength_admitted": False,
                "rating_eligible": False,
                "official_opponent_eligible": False,
            }
            invalid_flags = [
                field for field, expected in expected_control_flags.items()
                if item.get(field) is not expected
            ]
            if invalid_flags:
                raise RuntimeError(
                    "first_strict_control_flags_invalid:"
                    + ",".join(invalid_flags)
                )
            if item.get("formal_bootstrap_scope") != "first_policy_bot_empty_pool_only":
                raise RuntimeError("first_strict_control_formal_scope_invalid")
            gate_authoritative = True
            strength_authoritative = False
            rating_eligible = False

            control_issues = validate_control_receipt(
                control_receipt,
                candidate_version=control_receipt.get(
                    "candidate_version"
                ),
                source_version=control_receipt.get(
                    "source_version"
                ),
                active_bots=control_active_bots,
                # Plan/receipt construction already performs a full refresh.
                # A cold process or changed ref/stat cache key still forces a
                # complete refresh here; the function also closes with an
                # unconditional full refresh below.
                force_protocol_refresh=False,
            )
            if control_issues:
                raise RuntimeError(
                    "first_strict_control_contract_invalid:"
                    + ";".join(control_issues[:8])
                )
            try:
                normalized_control_execution_scope = normalize_execution_scope(
                    control_execution_scope
                )
            except Exception as exc:
                raise RuntimeError(
                    "first_strict_control_execution_scope_invalid:"
                    + str(exc)
                ) from exc
            expected_execution_bindings = {
                "candidate_version": int(
                    control_receipt.get("candidate_version") or 0
                ),
                "candidate_label": candidate[0],
                "candidate_artifact_hash": hash_path(candidate[1]),
                "control_id": str(item.get("name") or opponent[0]),
                "control_artifact_hash": str(
                    ((control_receipt.get("control") or {}).get("artifact_hash"))
                    or ""
                ),
                "control_receipt_digest": str(
                    control_receipt.get("receipt_digest") or ""
                ),
                "native_match_timing_plan_digest": precommit_timing_plan.digest(),
            }
            mismatched_execution_bindings = [
                field
                for field, expected in expected_execution_bindings.items()
                if normalized_control_execution_scope.get(field) != expected
            ]
            if mismatched_execution_bindings:
                raise RuntimeError(
                    "first_strict_control_execution_scope_binding_mismatch:"
                    + ",".join(mismatched_execution_bindings)
                )
            opponent_runtime_mode = "system_first_strict_control"
        else:
            opponent = resolve_bot(token)
            normalized_control_execution_scope = None
            gate_authoritative = is_precommit_gate_matchup(item)
            strength_authoritative = is_strength_matchup(item)
            rating_eligible = bool(
                item.get("rating_eligible", strength_authoritative)
            )
            opponent_runtime_mode = _acceptance_opponent_runtime_mode(
                opponent[0], opponent[1]
            )
        resolved_opponents.append({
            "name": item.get("name") or opponent[0],
            "reason": reason,
            "path": str(opponent[1]),
            "runtime_mode": opponent_runtime_mode,
            "precommit_gate_admitted": gate_authoritative,
            "formal_bootstrap_opponent_admitted": bool(
                item.get("formal_bootstrap_opponent_admitted", False)
            ),
            "formal_bootstrap_scope": str(
                item.get("formal_bootstrap_scope") or ""
            ),
            "strength_admitted": strength_authoritative,
            "rating_eligible": rating_eligible,
            "official_opponent_eligible": bool(
                item.get("official_opponent_eligible", not system_control)
            ),
        })
        samples: list[int] = []
        repeats: list[dict[str, Any]] = []
        candidate_issues: list[str] = []
        opponent_issues: list[str] = []
        hands_played_total = 0
        for repeat in range(matches_per_opponent):
            # A first-strict provider invocation may create at most one new
            # physical sample.  Recovered receipts are cheap reads and may be
            # traversed first, but once a fresh runner has completed its
            # journalled receipt, return a durable continuation boundary rather
            # than relying on the same SDK stream for the remaining 7 matches.
            if (
                system_control
                and first_strict_batch_plan is not None
                and new_batch_samples >= int(
                    first_strict_batch_plan[
                        "max_new_samples_per_invocation"
                    ]
                )
            ):
                return {
                    "evaluation_protocol": "national_native_tcp",
                    "candidate": candidate[0],
                    "candidate_path": str(candidate[1]),
                    "opponents": resolved_opponents,
                    "matchups": [],
                    "sample_plan": list(sample_plan or []),
                    "native_match_timing_plan": precommit_timing_plan.snapshot(),
                    "native_match_timing_plan_digest": precommit_timing_plan.digest(),
                    "control_execution_scope": normalized_control_execution_scope,
                    "first_strict_batch_pending": _first_strict_batch_progress(
                        batch_plan=first_strict_batch_plan,
                        control_execution_scope=normalized_control_execution_scope,
                        timing_plan=precommit_timing_plan,
                        completed_receipts=completed_batch_receipts,
                        state="pending_next_sample",
                        next_repeat=repeat + 1,
                    ),
                    "blockers": [],
                    "passed": False,
                }
            raise_if_cancelled()
            if system_control:
                from first_strict_control import validate_control_receipt

                control_issues = validate_control_receipt(
                    control_receipt,
                    candidate_version=control_receipt.get(
                        "candidate_version"
                    ),
                    source_version=control_receipt.get(
                        "source_version"
                    ),
                    active_bots=control_active_bots,
                    force_protocol_refresh=False,
                )
                if control_issues:
                    raise RuntimeError(
                        "first_strict_control_contract_drift:"
                        + ";".join(control_issues[:8])
                    )
            sample_key = (str(item.get("name") or opponent[0]), repeat + 1)
            frozen = frozen_samples.get(sample_key) if sample_plan is not None else None
            if sample_plan is not None and frozen is None:
                raise ValueError(
                    f"native precommit sample plan is missing {sample_key[0]} repeat {sample_key[1]}"
                )
            if frozen is not None and frozen.get(
                "native_match_timing_plan_digest"
            ) != precommit_timing_plan.digest():
                raise ValueError(
                    "native precommit sample plan timing plan digest mismatch:"
                    f"{sample_key[0]}:{sample_key[1]}"
                )
            seed = (
                frozen.get("deck_seed_base")
                if frozen is not None
                else (
                    None
                    if deck_seed_base is None
                    else int(deck_seed_base) + (opp_index * 100_000) + (repeat * 1_000)
                )
            )
            bot_seed = (
                frozen.get("bot_seed_base")
                if frozen is not None
                else (None if seed is None else int(seed) + 1_000_000_000)
            )
            execution_ticket = None
            if system_control:
                from first_strict_execution_journal import begin_control_execution

                execution_ticket = begin_control_execution(
                    scope=normalized_control_execution_scope,
                    repeat=repeat + 1,
                    deck_seed_base=int(seed),
                    bot_seed_base=int(bot_seed),
                    timing_plan=precommit_timing_plan,
                )
                if execution_ticket.get("pending") is True:
                    if first_strict_batch_plan is None:
                        raise RuntimeError("first_strict_live_lease_without_batch_plan")
                    return {
                        "evaluation_protocol": "national_native_tcp",
                        "candidate": candidate[0],
                        "candidate_path": str(candidate[1]),
                        "opponents": resolved_opponents,
                        "matchups": [],
                        "sample_plan": list(sample_plan or []),
                        "native_match_timing_plan": precommit_timing_plan.snapshot(),
                        "native_match_timing_plan_digest": precommit_timing_plan.digest(),
                        "control_execution_scope": normalized_control_execution_scope,
                        "control_execution_pending": execution_ticket,
                        "first_strict_batch_pending": _first_strict_batch_progress(
                            batch_plan=first_strict_batch_plan,
                            control_execution_scope=normalized_control_execution_scope,
                            timing_plan=precommit_timing_plan,
                            completed_receipts=completed_batch_receipts,
                            state="waiting_live_lease",
                            next_repeat=repeat + 1,
                        ),
                        "blockers": [],
                        "passed": False,
                    }
            recovered_execution = bool(
                system_control and execution_ticket.get("recovered") is True
            )
            if recovered_execution:
                result = execution_ticket["execution"]
                execution_receipt = execution_ticket["execution_receipt"]
            else:
                result = await run_native_strength_pair(
                    candidate[1],
                    opponent[1],
                    hands,
                    deck_seed_base=seed,
                    bot_seed_base=bot_seed,
                    timeout_sec=LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
                    timing_plan=precommit_timing_plan,
                    capture_events=system_control,
                    progress_callback=progress_callback,
                    **(
                        {"control_execution_ticket": execution_ticket}
                        if system_control
                        else {}
                    ),
                )
                execution_receipt = None
            timing_issues = validate_native_match_timing_evidence(
                result,
                timing_plan=precommit_timing_plan,
            )
            if system_control and timing_issues:
                raise RuntimeError(
                    "first_strict_control_execution_timing_evidence_drift:"
                    + ";".join(timing_issues)
                )
            if system_control and not recovered_execution:
                # The runner has already made the atomic durable transition.
                # This idempotent reference read is still bounded so a later
                # operator SQLite lock cannot hang the precommit coroutine.
                reference_deadline = (
                    time.monotonic()
                    + precommit_timing_plan.post_execution_completion_timeout_us
                    / 1_000_000.0
                )
                execution_receipt = await _await_first_strict_control_completion(
                    execution_ticket,
                    result,
                    deadline_monotonic=reference_deadline,
                )
            if system_control:
                if not isinstance(execution_receipt, dict):
                    raise RuntimeError("first_strict_batch_execution_receipt_missing")
                completed_batch_receipts.append({
                    "repeat": repeat + 1,
                    "execution_receipt": execution_receipt,
                })
                if not recovered_execution:
                    new_batch_samples += 1
            # A complete match/journal receipt is the smallest interruptible
            # evidence unit.  Never admit it or launch the next sample after the
            # owning cycle has timed out.
            raise_if_cancelled()
            if system_control:
                # Revalidate after every full match as well as before it.  A
                # concurrently published strict bot, altered system asset, or
                # runtime-template drift revokes the empty-pool authority and
                # must force replanning before this sample is admitted.
                control_issues = validate_control_receipt(
                    control_receipt,
                    candidate_version=control_receipt.get(
                        "candidate_version"
                    ),
                    source_version=control_receipt.get(
                        "source_version"
                    ),
                    active_bots=control_active_bots,
                    force_protocol_refresh=False,
                )
                if control_issues:
                    raise RuntimeError(
                        "first_strict_control_contract_drift_after_match:"
                        + ";".join(control_issues[:8])
                    )
            net = int(result.get("net_chips_a", 0) or 0)
            hands_played = int(result.get("hands_played", 0) or 0)
            hands_played_total += hands_played
            c_issues = [
                str(issue)
                for issue in result.get("issues", [])
                if str(issue).startswith(candidate[0] + ":") or str(issue).startswith("hands_played=")
            ]
            o_issues = [
                str(issue)
                for issue in result.get("issues", [])
                if not (str(issue).startswith(candidate[0] + ":") or str(issue).startswith("hands_played="))
            ]
            complete = hands_played == hands
            compliance_passed = bool(result.get("passed_compliance", False))
            artifact_execution = result.get("artifact_execution") or {}
            expected_execution_artifacts = {
                candidate[0]: (
                    str(normalized_control_execution_scope.get(
                        "candidate_artifact_hash"
                    ) or "")
                    if system_control
                    else hash_path(candidate[1])
                ),
                opponent[0]: (
                    str(normalized_control_execution_scope.get(
                        "control_artifact_hash"
                    ) or "")
                    if system_control
                    else hash_path(opponent[1])
                ),
            }
            artifact_execution_valid = _artifact_execution_is_valid(
                artifact_execution,
                expected_execution_artifacts,
            )
            if not artifact_execution_valid:
                c_issues.append("native_artifact_execution_identity_invalid")
            c_issues.extend(timing_issues)
            sample_valid = (
                complete
                and compliance_passed
                and artifact_execution_valid
                and not c_issues
                and not o_issues
            )
            gate_sample_admitted = gate_authoritative and sample_valid
            strength_sample_admitted = strength_authoritative and sample_valid
            if gate_sample_admitted:
                samples.append(net)
                aggregate_net_chips.append(net)
            candidate_issues.extend(c_issues)
            opponent_issues.extend(o_issues)
            repeat_result = {
                "repeat": repeat + 1,
                "deck_seed_base": seed,
                "bot_seed_base": bot_seed,
                "hands_played": hands_played,
                "net_chips": net,
                "candidate_issues": c_issues,
                "opponent_issues": o_issues,
                "complete": complete,
                "passed_compliance": compliance_passed,
                "sample_valid": sample_valid,
                "precommit_gate_admitted": gate_sample_admitted,
                "formal_bootstrap_opponent_admitted": bool(
                    item.get("formal_bootstrap_opponent_admitted", False)
                ),
                "formal_bootstrap_scope": str(
                    item.get("formal_bootstrap_scope") or ""
                ),
                "strength_admitted": strength_sample_admitted,
                "opponent_runtime_mode": opponent_runtime_mode,
                "rating_eligible": rating_eligible,
                "official_opponent_eligible": bool(
                    item.get("official_opponent_eligible", not system_control)
                ),
                "evaluation_authority": (
                    "first_strict_bootstrap_regression_v1"
                    if system_control
                    else "local_precommit_strength"
                ),
                "artifact_execution": artifact_execution,
                "artifact_execution_valid": artifact_execution_valid,
                "local_runtime_budget": {
                    "profile_id": NATIVE_MATCH_TIMING_PROFILE_ID,
                    "timing_plan": precommit_timing_plan.snapshot(),
                    "timing_plan_digest": precommit_timing_plan.digest(),
                    "hard_deadline_sec": (
                        precommit_timing_plan.bot_a.hard_deadline_us / 1_000_000.0
                    ),
                    "refinement_budget_sec": (
                        precommit_timing_plan.bot_a.refinement_budget_us / 1_000_000.0
                    ),
                    "baseline_target_sec": (
                        precommit_timing_plan.bot_a.baseline_target_us / 1_000_000.0
                    ),
                    "match_timeout_request_sec": LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
                    "match_timeout_effective_sec": (
                        precommit_timing_plan.effective_timeout_us / 1_000_000.0
                    ),
                    "full_match_liveness_budget": (
                        precommit_timing_plan.liveness_budget_snapshot()
                    ),
                    "scope": (
                        "first_strict_bootstrap_regression_only"
                        if system_control
                        else "local_strength_only"
                    ),
                },
            }
            if system_control:
                # This result is produced by the live native runner. Make the
                # zero-migration authority explicit so the first-strict
                # validator never has to infer it from a missing field.
                repeat_result["migration_projection"] = False
                # Full events/hand records/settlements live only in the
                # content-addressed execution authority.  The checkpoint result
                # carries a small reference plus independently recomputed
                # summary fields.
                repeat_result["execution_receipt"] = execution_receipt
            else:
                repeat_result["raw"] = result
            repeats.append(repeat_result)
        if system_control:
            # Close the cached per-match guard with a full Git/artifact refresh
            # before any samples can leave this function as admitted evidence.
            control_issues = validate_control_receipt(
                control_receipt,
                candidate_version=control_receipt.get("candidate_version"),
                source_version=control_receipt.get("source_version"),
                active_bots=control_active_bots,
                force_protocol_refresh=True,
            )
            if control_issues:
                raise RuntimeError(
                    "first_strict_control_contract_drift_final:"
                    + ";".join(control_issues[:8])
                )
        wins = sum(1 for value in samples if value > 0)
        losses = sum(1 for value in samples if value < 0)
        draws = sum(1 for value in samples if value == 0)
        if gate_authoritative:
            total_wins += wins
            total_losses += losses
            total_draws += draws
        mean = _mean(samples)
        ci_lo, ci_hi = _ci(samples)
        matchup = {
            "opponent": item.get("name") or opponent[0],
            "reason": reason,
            "precommit_gate_admitted": gate_authoritative,
            "formal_bootstrap_opponent_admitted": bool(
                item.get("formal_bootstrap_opponent_admitted", False)
            ),
            "formal_bootstrap_scope": str(
                item.get("formal_bootstrap_scope") or ""
            ),
            "strength_authoritative": strength_authoritative,
            "strength_admitted": strength_authoritative,
            "opponent_runtime_mode": opponent_runtime_mode,
            "rating_eligible": rating_eligible,
            "official_opponent_eligible": bool(
                item.get("official_opponent_eligible", not system_control)
            ),
            "evaluation_authority": (
                "first_strict_bootstrap_regression_v1"
                if system_control
                else "local_precommit_strength"
            ),
            "protocol": "national_native_tcp",
            "hands_per_match": hands,
            "matches": matches_per_opponent,
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "n_played": len(samples),
            "samples_expected": matches_per_opponent,
            "hands_played_total": hands_played_total,
            "net_chips": samples,
            "net_chips_mean": _rounded(mean),
            "net_chip_ci": [_rounded(ci_lo), _rounded(ci_hi)],
            "candidate_compliance_issues": candidate_issues,
            "opponent_compliance_issues": opponent_issues,
            "artifact_executions": [
                row.get("artifact_execution") or {} for row in repeats
            ],
            "repeats": repeats,
        }
        if system_control:
            # The aggregate carries the same explicit zero-migration boundary
            # as every repeat. Absence remains invalid at the fail-closed
            # consumer in first_strict_control.py.
            matchup["migration_projection"] = False
        matchups.append(matchup)
        if gate_authoritative and candidate_issues:
            blockers.append({"reason": "native_candidate_compliance", "opponent": matchup["opponent"], "details": "; ".join(candidate_issues[:5])})
        if gate_authoritative and opponent_issues:
            blockers.append({"reason": "native_opponent_compliance", "opponent": matchup["opponent"], "details": "; ".join(opponent_issues[:5])})
        if gate_authoritative and any(not row["complete"] for row in repeats):
            blockers.append({"reason": "native_incomplete_match", "opponent": matchup["opponent"], "details": f"{hands_played_total}/{hands * matches_per_opponent} hands completed"})
        if gate_authoritative and len(samples) != matches_per_opponent:
            blockers.append({
                "reason": "native_precommit_sample_shortfall",
                "opponent": matchup["opponent"],
                "details": f"{len(samples)}/{matches_per_opponent} complete compliant 70-hand samples admitted",
            })
    agg_mean = _mean(aggregate_net_chips)
    agg_ci_lower, agg_ci_upper = _ci(aggregate_net_chips)
    if not aggregate_net_chips:
        blockers.append({"reason": "native_no_samples", "details": "Native precommit produced zero completed match samples."})
    outcome_blockers, outcome_gate = precommit_outcome_blockers(
        matchups,
        parent_label=parent_label,
        aggregate_reason="aggregate_native_regression",
    )
    blockers.extend(outcome_blockers)
    control_gate = None
    if any(
        str(item.get("authority") or "") == "system_first_strict_control"
        for item in opponents
    ):
        from first_strict_control import control_gate_blockers

        control_blockers, control_gate = control_gate_blockers(
            matchups,
            expected_execution_scope=control_execution_scope,
        )
        blockers.extend(control_blockers)
    paired_payload = {
        "protocol": "national_native_tcp",
        "hands_per_match": hands,
        "matches_per_opponent": matches_per_opponent,
        "aggregate_ci_lower": _rounded(agg_ci_lower),
        "aggregate_ci_upper": _rounded(agg_ci_upper),
        "aggregate_threshold": None,
        "aggregate_gate_bound": outcome_gate.get("primary_match_score"),
        "aggregate_gate_rule": "complete_70_hand_wld_loss_margin",
        "outcome_gate": outcome_gate,
        "first_strict_control_gate": control_gate,
        "net_chips_samples": len(aggregate_net_chips),
        "strength_net_chips_samples": sum(
            len(matchup.get("net_chips") or [])
            for matchup in matchups
            if is_strength_matchup(matchup)
        ),
        "gate_degraded": len(aggregate_net_chips) < 2,
        "net_chips_mean": _rounded(agg_mean),
        "net_chips_std": round(statistics.pstdev(aggregate_net_chips), 1) if len(aggregate_net_chips) > 1 else None,
        "net_chips_min": min(aggregate_net_chips) if aggregate_net_chips else None,
        "net_chips_max": max(aggregate_net_chips) if aggregate_net_chips else None,
        "secondary_net_chip_ci": [_rounded(agg_ci_lower), _rounded(agg_ci_upper)],
    }
    return {
        "evaluation_protocol": "national_native_tcp",
        "candidate": candidate[0],
        "candidate_path": str(candidate[1]),
        "opponents": resolved_opponents,
        "matchups": matchups,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "total_draws": total_draws,
        "aggregate_net_chips": aggregate_net_chips,
        "sample_plan": list(sample_plan or []),
        "native_match_timing_plan": precommit_timing_plan.snapshot(),
        "native_match_timing_plan_digest": precommit_timing_plan.digest(),
        "native_precommit_batch_plan": first_strict_batch_plan,
        "first_strict_batch": (
            _first_strict_batch_progress(
                batch_plan=first_strict_batch_plan,
                control_execution_scope=normalized_control_execution_scope,
                timing_plan=precommit_timing_plan,
                completed_receipts=completed_batch_receipts,
                state="completed",
                next_repeat=None,
            )
            if first_strict_batch_plan is not None
            else None
        ),
        "control_execution_scope": (
            normalized_control_execution_scope
            if any(
                str(item.get("authority") or "")
                == "system_first_strict_control"
                for item in opponents
            )
            else None
        ),
        "paired_bootstrap": paired_payload,
        "artifact_execution_contract": {
            "schema_version": DIRECT_ARTIFACT_EXECUTION_SCHEMA_VERSION,
            "mode": "direct_content_bound_policy_artifact",
        },
        "blockers": blockers,
        "passed": not blockers,
    }
