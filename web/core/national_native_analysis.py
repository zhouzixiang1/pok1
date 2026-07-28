"""Static-analysis & replay/trace parsing cluster, extracted from ``national_native``.

Holds the pure-analysis helpers that were previously module-level functions
in ``web/core/national_native.py``:

- native runtime name-handshake validation (``_system_native_name_handshake_issues``)
- artifact-execution identity validation (``_artifact_execution_is_valid``)
- stream-decoder static + behavioral probe (``check_native_stream_decoder``)
- AST analysis helpers (``_function_source``,
  ``_policy_decision_has_exception_pass``, ``_handler_catches_broad_exception``)
- bot version parsing (``_bot_version``)
- decision-trace / hand-record parsing cluster
  (``_validate_formal_native_env_overrides``, ``_trace_decisions_from_overrides``,
  ``_parse_decision_trace``, ``_compact_native_hand_records``)
- safe label fragment (``_safe_label_fragment``)

The parent module (``national_native``) keeps thin delegate shells for every
moved symbol so that:

* intra-module callers (``check_native_contract``,
  ``_execute_tcp_server_with_processes``, ``_run_direct_artifact_tcp_pair``)
  continue to resolve through the parent namespace;
* test-suite direct calls such as
  ``national_native._artifact_execution_is_valid(...)`` keep working; and
* any future ``monkeypatch.setattr(national_native, "<name>", fake)`` is still
  observed because the delegates forward to this companion at call time.

Implementation contract
-----------------------
The companion imports the parent module as ``_nn`` for the few symbols that
must be read live (notably ``NativeBotSpec``, which is defined in the parent
and has no upstream companion).  Immutable template/timing constants
(``NATIVE_BOT_TEMPLATE``, ``_NATIVE_STREAM_PROBE_SCRIPT``, ``NATIVE_ENTRY``,
``TRACE_PREFIX``, ``FORMAL_NATIVE_ENV_OVERRIDE_KEYS``,
``_FORMAL_NATIVE_TIMING_OVERRIDE_KEYS``) are imported directly from their
owning companions (``national_native_templates`` / ``national_native_timing``)
since they never change between runs and are not monkeypatched by the test
suite.

Intra-cluster calls (one moved function calling another) remain bare, since
both caller and callee now live in this module.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import national_native as _nn
from bot_namespace import parse_bot_version
from national_native_templates import (
    NATIVE_BOT_TEMPLATE,
    _NATIVE_STREAM_PROBE_SCRIPT,
)
from national_native_timing import (
    FORMAL_NATIVE_ENV_OVERRIDE_KEYS,
    _FORMAL_NATIVE_TIMING_OVERRIDE_KEYS,
)

# Mirrors of the parent module's stable constants.  These four are defined
# directly in national_native.py (not in a companion) and are never
# monkeypatched by tests, so reading them through ``_nn`` keeps a single
# source of truth.
NATIVE_ENTRY = "national_bot.py"
TRACE_PREFIX = "POK_TRACE_DECISION "


def _system_native_name_handshake_issues(
    label: str,
    spec,
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
