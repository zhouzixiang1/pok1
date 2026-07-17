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


ROOT = Path(__file__).resolve().parents[2]
NATIVE_ENTRY = "national_bot.py"
PRECOMPUTE_ENTRY = "precompute.py"
TRACE_PREFIX = "POK_TRACE_DECISION "
NATIONAL_DECISION_RUNTIME_VERSION = 10
LOCAL_NATIVE_STRENGTH_HARD_DEADLINE_SEC = 2.0
LOCAL_NATIVE_STRENGTH_REFINEMENT_BUDGET_SEC = 1.8
LOCAL_NATIVE_STRENGTH_BASELINE_TARGET_SEC = 0.20
# This is a caller-requested ceiling, not the effective full-match liveness
# budget.  ``native_full_match_timeout_budget`` raises it when the configured
# local decision envelope needs longer for a complete 70-hand sample.
LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC = 420.0
NATIVE_BETTING_ROUNDS_PER_HAND = 4
# This active-native bound is stricter than the engine's generic 100-action
# safety cap: it is the system-owned, fixed-20,000-chip national hand envelope.
# The runtime enforces it and emits a typed fail-closed abort if it is reached.
NATIVE_FULL_MATCH_MAX_DECISION_SLOTS_PER_HAND = (
    NATIONAL_20000_CHIP_MAX_ACTION_REQUESTS_PER_HAND
)
NATIVE_FULL_MATCH_FIXED_LIVENESS_SLACK_SEC = 60.0
NATIVE_FULL_MATCH_DECISION_OVERHEAD_SEC = 0.25
NATIVE_MIN_MATCH_TIMEOUT_SEC = 90.0
NATIVE_ARTIFACT_PREPARATION_PER_BOT_TIMEOUT_SEC = 30.0
NATIVE_POST_EXECUTION_COMPLETION_TIMEOUT_SEC = 30.0
NATIVE_LAUNCH_HEARTBEAT_INTERVAL_SEC = 30.0
NATIVE_MATCH_TIMING_PLAN_SCHEMA_VERSION = 5
NATIVE_MATCH_TIMING_PROFILE_ID = "national_local_strength_v1"
NATIVE_MATCH_TIMING_PROFILE = {
    "action_delay_us": 0,
    "hard_deadline_us": 2_000_000,
    "refinement_budget_us": 1_800_000,
    "baseline_target_us": 200_000,
}
NATIVE_MATCH_TIMING_PROFILE_DEFINITION_DIGEST = hashlib.sha256(
    json.dumps(
        {
            "schema_version": NATIVE_MATCH_TIMING_PLAN_SCHEMA_VERSION,
            "profile_id": NATIVE_MATCH_TIMING_PROFILE_ID,
            "timing": NATIVE_MATCH_TIMING_PROFILE,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()
FORMAL_NATIVE_ENV_OVERRIDE_KEYS = frozenset({
    "POK_NATIVE_LOCAL_ACTION_DELAY",
    "POK_NATIVE_DECISION_HARD_DEADLINE_SEC",
    "POK_NATIVE_DECISION_REFINEMENT_BUDGET_SEC",
    "POK_NATIVE_DECISION_BASELINE_TARGET_SEC",
    "POK_TRACE_DECISIONS",
})
_FORMAL_NATIVE_TIMING_OVERRIDE_KEYS = FORMAL_NATIVE_ENV_OVERRIDE_KEYS - {
    "POK_TRACE_DECISIONS"
}


class NativeMatchStartupTimeout(TimeoutError):
    """The single monotonic native client-startup watchdog expired."""


def _canonical_timing_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class NativeBotTimingPlan:
    """Integer-only, system-owned child timing used for one match seat."""

    action_delay_us: int
    hard_deadline_us: int
    refinement_budget_us: int
    baseline_target_us: int

    def snapshot(self) -> dict[str, int]:
        return {
            "action_delay_us": self.action_delay_us,
            "hard_deadline_us": self.hard_deadline_us,
            "refinement_budget_us": self.refinement_budget_us,
            "baseline_target_us": self.baseline_target_us,
        }

    def to_bot_timing(self) -> BotTiming:
        timing = BotTiming(
            action_delay=self.action_delay_us / 1_000_000.0,
            hard_deadline=self.hard_deadline_us / 1_000_000.0,
            refinement_budget=self.refinement_budget_us / 1_000_000.0,
            baseline_target=self.baseline_target_us / 1_000_000.0,
        )
        timing.environment()
        return timing

    @classmethod
    def system_profile(cls) -> "NativeBotTimingPlan":
        return cls(**NATIVE_MATCH_TIMING_PROFILE)


@dataclass(frozen=True)
class NativeMatchTimingPlan:
    """Frozen timing contract shared by launch, lease, replay and admission."""

    hands: int
    requested_timeout_us: int
    effective_timeout_us: int
    liveness_floor_us: int
    decision_slot_us: int
    protocol_action_timeout_us: int
    connect_timeout_us: int
    name_timeout_us: int
    process_drain_timeout_us: int
    capacity_queue_timeout_us: int
    artifact_preparation_per_bot_timeout_us: int
    artifact_preparation_timeout_us: int
    startup_timeout_us: int
    cleanup_timeout_us: int
    post_execution_completion_timeout_us: int
    launch_timeout_us: int
    finalization_timeout_us: int
    execution_timeout_us: int
    first_strict_lease_timeout_us: int
    bot_a: NativeBotTimingPlan
    bot_b: NativeBotTimingPlan

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": NATIVE_MATCH_TIMING_PLAN_SCHEMA_VERSION,
            "profile_id": NATIVE_MATCH_TIMING_PROFILE_ID,
            "profile_definition_digest": NATIVE_MATCH_TIMING_PROFILE_DEFINITION_DIGEST,
            "hands": self.hands,
            "requested_timeout_us": self.requested_timeout_us,
            "effective_timeout_us": self.effective_timeout_us,
            "liveness_floor_us": self.liveness_floor_us,
            "decision_slot_us": self.decision_slot_us,
            "protocol_action_timeout_us": self.protocol_action_timeout_us,
            "connect_timeout_us": self.connect_timeout_us,
            "name_timeout_us": self.name_timeout_us,
            "process_drain_timeout_us": self.process_drain_timeout_us,
            "capacity_queue_timeout_us": self.capacity_queue_timeout_us,
            "artifact_preparation_per_bot_timeout_us": (
                self.artifact_preparation_per_bot_timeout_us
            ),
            "artifact_preparation_timeout_us": (
                self.artifact_preparation_timeout_us
            ),
            # These aggregate budgets are explicit, not an unaccounted-for
            # grace period.  The native runner launches two clients, waits for
            # server acceptance, performs two name reads, drains both clients,
            # server and processes, then completes bounded sealing/projection.
            "startup_timeout_us": self.startup_timeout_us,
            "cleanup_timeout_us": self.cleanup_timeout_us,
            "post_execution_completion_timeout_us": (
                self.post_execution_completion_timeout_us
            ),
            "launch_timeout_us": self.launch_timeout_us,
            "finalization_timeout_us": self.finalization_timeout_us,
            "execution_timeout_us": self.execution_timeout_us,
            "first_strict_lease_timeout_us": self.first_strict_lease_timeout_us,
            "fixed_liveness_slack_us": int(
                NATIVE_FULL_MATCH_FIXED_LIVENESS_SLACK_SEC * 1_000_000
            ),
            "betting_rounds_per_hand": NATIVE_BETTING_ROUNDS_PER_HAND,
            "national_hand_action_request_cap": (
                NATIVE_FULL_MATCH_MAX_DECISION_SLOTS_PER_HAND
            ),
            "engine_action_cap_per_betting_round": MAX_ACTIONS_PER_BETTING_ROUND,
            "bot_a": self.bot_a.snapshot(),
            "bot_b": self.bot_b.snapshot(),
        }

    def digest(self) -> str:
        return _canonical_timing_digest(self._payload())

    def snapshot(self) -> dict[str, Any]:
        return {
            **self._payload(),
            "timing_plan_digest": self.digest(),
        }

    def liveness_budget_snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": NATIVE_MATCH_TIMING_PLAN_SCHEMA_VERSION,
            "timing_plan_digest": self.digest(),
            "requested_timeout_sec": self.requested_timeout_us / 1_000_000.0,
            "effective_timeout_sec": self.effective_timeout_us / 1_000_000.0,
            "liveness_floor_sec": self.liveness_floor_us / 1_000_000.0,
            "hands": self.hands,
            # Every normalized hand count uses this same strict runtime
            # envelope.  A diagnostic one-hand match must not report a zero
            # action bound while its timeout was actually derived from 34
            # possible national action requests.
            "decision_slots_per_hand": NATIVE_FULL_MATCH_MAX_DECISION_SLOTS_PER_HAND,
            "decision_slot_sec": self.decision_slot_us / 1_000_000.0,
            "fixed_slack_sec": NATIVE_FULL_MATCH_FIXED_LIVENESS_SLACK_SEC,
            "betting_rounds_per_hand": NATIVE_BETTING_ROUNDS_PER_HAND,
            "national_hand_action_request_cap": NATIVE_FULL_MATCH_MAX_DECISION_SLOTS_PER_HAND,
            "engine_action_cap_per_betting_round": MAX_ACTIONS_PER_BETTING_ROUND,
            # Capacity wait is outside the engine stopwatch, but an
            # first-strict effect is claimed before the runner can acquire its
            # shared slot.  It therefore belongs to the frozen effect lease,
            # not to a caller-selectable retry timeout.
            "capacity_queue_timeout_sec": (
                self.capacity_queue_timeout_us / 1_000_000.0
            ),
            "artifact_preparation_per_bot_timeout_sec": (
                self.artifact_preparation_per_bot_timeout_us / 1_000_000.0
            ),
            "artifact_preparation_timeout_sec": (
                self.artifact_preparation_timeout_us / 1_000_000.0
            ),
            "startup_timeout_sec": self.startup_timeout_us / 1_000_000.0,
            "cleanup_timeout_sec": self.cleanup_timeout_us / 1_000_000.0,
            "post_execution_completion_timeout_sec": (
                self.post_execution_completion_timeout_us / 1_000_000.0
            ),
            "launch_timeout_sec": self.launch_timeout_us / 1_000_000.0,
            "finalization_timeout_sec": (
                self.finalization_timeout_us / 1_000_000.0
            ),
            "execution_timeout_sec": self.execution_timeout_us / 1_000_000.0,
            "first_strict_lease_timeout_sec": (
                self.first_strict_lease_timeout_us / 1_000_000.0
            ),
        }


def _plain_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def build_native_match_timing_plan(
    *,
    hands: int,
    requested_timeout_sec: float | None,
) -> NativeMatchTimingPlan:
    """Build the only local-strength timing contract accepted in production."""

    normalized_hands = max(1, min(70, int(hands)))
    if requested_timeout_sec is None:
        requested_timeout_us = int(
            max(NATIVE_MIN_MATCH_TIMEOUT_SEC, float(normalized_hands) * 4.0)
            * 1_000_000
        )
    else:
        try:
            requested_value = float(requested_timeout_sec)
        except (TypeError, ValueError) as exc:
            raise ValueError("native match requested timeout is invalid") from exc
        if not math.isfinite(requested_value) or requested_value < 0.0:
            raise ValueError("native match requested timeout is invalid")
        requested_timeout_us = int(round(requested_value * 1_000_000))
    bot_a = NativeBotTimingPlan.system_profile()
    bot_b = NativeBotTimingPlan.system_profile()
    decision_slot_us = max(
        bot_a.hard_deadline_us + bot_a.action_delay_us,
        bot_b.hard_deadline_us + bot_b.action_delay_us,
    ) + int(NATIVE_FULL_MATCH_DECISION_OVERHEAD_SEC * 1_000_000)
    # The local-strength profile is not an assertion that a broken bot may
    # consume its official 60-second action allowance on every request.  It is
    # the bounded system-owned worker envelope (2.0 s) plus scheduler/transport
    # overhead that a healthy strict artifact may actually use.  Apply it to
    # every requested hand count: a one-hand diagnostic must not silently use
    # a weaker timeout identity than the exact same runtime launched for 70
    # hands.
    liveness_floor_us = max(
        int(NATIVE_MIN_MATCH_TIMEOUT_SEC * 1_000_000),
        int(NATIVE_FULL_MATCH_FIXED_LIVENESS_SLACK_SEC * 1_000_000)
        + normalized_hands
        * NATIVE_FULL_MATCH_MAX_DECISION_SLOTS_PER_HAND
        * decision_slot_us,
    )
    effective_timeout_us = max(requested_timeout_us, liveness_floor_us)
    capacity_queue_timeout_us = int(
        round(float(DEFAULT_CAPACITY_WAIT_SECONDS) * 1_000_000)
    )
    if capacity_queue_timeout_us <= 0:
        raise RuntimeError("native capacity queue timeout must be positive")
    artifact_preparation_per_bot_timeout_us = int(
        round(NATIVE_ARTIFACT_PREPARATION_PER_BOT_TIMEOUT_SEC * 1_000_000)
    )
    if artifact_preparation_per_bot_timeout_us <= 0:
        raise RuntimeError("native artifact preparation timeout must be positive")
    artifact_preparation_timeout_us = (
        2 * artifact_preparation_per_bot_timeout_us
    )
    connect_timeout_us = max(
        1_000_000,
        min(20_000_000, effective_timeout_us // 3),
    )
    name_timeout_us = max(
        1_000_000,
        min(30_000_000, effective_timeout_us // 3),
    )
    process_drain_timeout_us = max(
        1_000_000,
        min(5_000_000, effective_timeout_us // 6),
    )
    # The runner launches two local child clients and waits for both TCP
    # connections plus both name replies.  Cleanup can await two client
    # closes, one server close, and up to two normal/kill process waits.  Bind
    # all of those bounded phases explicitly so a provider heartbeat never
    # depends on a hidden post-engine slack allowance.
    # Two synchronous endpoint connections are followed by the asyncio
    # server's acceptance of both sockets, then two sequential name reads.
    # The acceptance wait consumes a third connect window; omitting it would
    # let the real startup path outlive the immutable timing identity.
    startup_timeout_us = 3 * connect_timeout_us + 2 * name_timeout_us
    cleanup_timeout_us = 7 * process_drain_timeout_us
    post_execution_completion_timeout_us = int(
        round(NATIVE_POST_EXECUTION_COMPLETION_TIMEOUT_SEC * 1_000_000)
    )
    if post_execution_completion_timeout_us <= 0:
        raise RuntimeError("native post-execution completion timeout must be positive")
    launch_timeout_us = (
        capacity_queue_timeout_us
        + artifact_preparation_timeout_us
        + startup_timeout_us
    )
    finalization_timeout_us = (
        cleanup_timeout_us + post_execution_completion_timeout_us
    )
    execution_timeout_us = (
        startup_timeout_us
        + effective_timeout_us
        + finalization_timeout_us
    )
    return NativeMatchTimingPlan(
        hands=normalized_hands,
        requested_timeout_us=requested_timeout_us,
        effective_timeout_us=effective_timeout_us,
        liveness_floor_us=liveness_floor_us,
        decision_slot_us=decision_slot_us,
        protocol_action_timeout_us=min(60_000_000, effective_timeout_us),
        connect_timeout_us=connect_timeout_us,
        name_timeout_us=name_timeout_us,
        process_drain_timeout_us=process_drain_timeout_us,
        capacity_queue_timeout_us=capacity_queue_timeout_us,
        artifact_preparation_per_bot_timeout_us=(
            artifact_preparation_per_bot_timeout_us
        ),
        artifact_preparation_timeout_us=artifact_preparation_timeout_us,
        startup_timeout_us=startup_timeout_us,
        cleanup_timeout_us=cleanup_timeout_us,
        post_execution_completion_timeout_us=(
            post_execution_completion_timeout_us
        ),
        launch_timeout_us=launch_timeout_us,
        finalization_timeout_us=finalization_timeout_us,
        execution_timeout_us=execution_timeout_us,
        # The journal ticket is created before the per-match capacity lease is
        # acquired.  It covers the explicit capacity, startup, engine and
        # cleanup phases below; no caller-selected "extra minute" is hidden
        # outside the immutable plan.
        first_strict_lease_timeout_us=(
            capacity_queue_timeout_us
            + artifact_preparation_timeout_us
            + execution_timeout_us
        ),
        bot_a=bot_a,
        bot_b=bot_b,
    )


def require_native_match_timing_plan(
    raw: Any,
    *,
    hands: int,
    requested_timeout_sec: float | None,
) -> NativeMatchTimingPlan:
    """Rebuild and require the exact system plan; no caller bytes are trusted."""

    expected = build_native_match_timing_plan(
        hands=hands,
        requested_timeout_sec=requested_timeout_sec,
    )
    if isinstance(raw, NativeMatchTimingPlan):
        observed = raw.snapshot()
    elif isinstance(raw, dict):
        observed = dict(raw)
    else:
        raise ValueError("native match timing plan is missing")
    if observed != expected.snapshot():
        raise ValueError("native match timing plan does not bind system profile")
    return expected


def _resolve_native_match_timing_plan(
    timing_plan: NativeMatchTimingPlan | dict[str, Any] | None,
    *,
    hands: int,
    requested_timeout_sec: float | None,
) -> NativeMatchTimingPlan:
    """Create or exactly validate the one plan allowed to launch a match."""

    if timing_plan is None:
        return build_native_match_timing_plan(
            hands=hands,
            requested_timeout_sec=requested_timeout_sec,
        )
    return require_native_match_timing_plan(
        timing_plan,
        hands=hands,
        requested_timeout_sec=requested_timeout_sec,
    )


def _native_timing_environment(
    env_overrides: dict[str, str | int | None] | None,
    *,
    sanitize_parent_environment: bool,
) -> dict[str, str]:
    """Return one system-owned native timing input projection.

    Strength, quality, precommit, and rating evidence must never depend on a
    mutable parent ``POK_NATIVE_*`` environment.  Callers may provide the
    small allowlisted timing override map, which is recorded by the liveness
    evidence; ambient inheritance is rejected rather than silently sampled.
    """

    if not sanitize_parent_environment:
        raise ValueError("native strength timing must not inherit parent environment")
    environment: dict[str, str] = {}
    for key, value in (env_overrides or {}).items():
        if value is None:
            environment.pop(str(key), None)
        else:
            environment[str(key)] = str(value)
    return environment


def _native_bot_timing(
    environment: dict[str, str],
    *,
    action_timeout: float,
) -> BotTiming:
    """Resolve one native bot's bounded timing exactly once for launch/budgeting."""

    action_delay = environment.get("POK_NATIVE_LOCAL_ACTION_DELAY", "0")
    try:
        # The system-owned native entry clamps this setting to two seconds;
        # budget the actual child behavior rather than an unused parent value.
        action_delay_value = max(0.0, min(2.0, float(action_delay)))
    except (TypeError, ValueError):
        action_delay_value = 0.0
    default_local_hard_deadline = max(
        0.05,
        min(
            LOCAL_NATIVE_STRENGTH_HARD_DEADLINE_SEC,
            float(action_timeout) - 0.25,
        ),
    )
    local_hard_deadline_raw = environment.get(
        "POK_NATIVE_DECISION_HARD_DEADLINE_SEC",
        str(default_local_hard_deadline),
    )
    try:
        local_hard_deadline_value = max(
            0.05,
            min(55.0, float(local_hard_deadline_raw)),
        )
    except (TypeError, ValueError):
        local_hard_deadline_value = default_local_hard_deadline
    default_refinement_budget = max(
        0.04,
        min(
            LOCAL_NATIVE_STRENGTH_REFINEMENT_BUDGET_SEC,
            local_hard_deadline_value - 0.10,
        ),
    )
    refinement_budget_raw = environment.get(
        "POK_NATIVE_DECISION_REFINEMENT_BUDGET_SEC",
        str(default_refinement_budget),
    )
    refinement_ceiling = max(
        0.04,
        local_hard_deadline_value
        - min(0.10, local_hard_deadline_value * 0.10),
    )
    try:
        refinement_budget = max(
            0.04,
            min(float(refinement_budget_raw), refinement_ceiling),
        )
    except (TypeError, ValueError):
        refinement_budget = min(
            default_refinement_budget,
            refinement_ceiling,
        )
    default_baseline_target = min(
        LOCAL_NATIVE_STRENGTH_BASELINE_TARGET_SEC,
        max(0.01, local_hard_deadline_value * 0.25),
    )
    baseline_target_raw = environment.get(
        "POK_NATIVE_DECISION_BASELINE_TARGET_SEC",
        str(default_baseline_target),
    )
    baseline_ceiling = max(
        0.01,
        refinement_budget - min(0.05, refinement_budget * 0.10),
    )
    try:
        baseline_target = max(
            0.01,
            min(float(baseline_target_raw), baseline_ceiling),
        )
    except (TypeError, ValueError):
        baseline_target = min(default_baseline_target, baseline_ceiling)
    timing = BotTiming(
        action_delay=action_delay_value,
        hard_deadline=local_hard_deadline_value,
        refinement_budget=refinement_budget,
        baseline_target=baseline_target,
    )
    # Reuse the managed-executor validation boundary rather than silently
    # normalising a malformed caller timing environment in the timeout model.
    timing.environment()
    return timing


def native_full_match_timeout_budget(
    hands: int,
    requested_timeout_sec: float | None,
    *,
    bot_a_env_overrides: dict[str, str | int | None] | None = None,
    bot_b_env_overrides: dict[str, str | int | None] | None = None,
    sanitize_parent_environment: bool = True,
) -> dict[str, Any]:
    """Calculate the fail-closed liveness floor for one native TCP match.

    A local strength run deliberately permits a 2 s decision envelope and a
    1.8 s refinement window.  A fixed 280/420/600 s whole-match timeout can
    therefore truncate a healthy 70-hand match before its final settlement.
    For a complete 70-hand sample the upper bound comes from the authoritative
    engine cap for every betting round, plus bounded startup and settlement
    slack. A one-hand smoke keeps its short 90 s minimum. It does not turn a
    timeout into a pass: a match that exceeds this effective budget remains
    incomplete and fails closed.
    """

    if bot_a_env_overrides or bot_b_env_overrides or not sanitize_parent_environment:
        raise ValueError(
            "native full-match timing must use the fixed system local-strength profile"
        )
    return build_native_match_timing_plan(
        hands=hands,
        requested_timeout_sec=requested_timeout_sec,
    ).liveness_budget_snapshot()


def _annotate_native_full_match_liveness(
    execution: dict[str, Any],
    timing_plan: NativeMatchTimingPlan,
) -> dict[str, Any]:
    """Attach one frozen timing fact before any control-path sealing.

    Only the whole-match watchdog gets the liveness label.  A name/connection
    timeout is still fail-closed, but it is a startup/transport failure rather
    than evidence that a healthy complete match exceeded its legal envelope.
    """

    if not isinstance(execution, dict):
        raise RuntimeError("native TCP runner returned a non-dictionary result")
    if int(execution.get("hands_requested") or timing_plan.hands) != timing_plan.hands:
        raise RuntimeError("native TCP runner timing-plan hand count drift")
    snapshot = timing_plan.snapshot()
    liveness_budget = timing_plan.liveness_budget_snapshot()
    existing_plan = execution.get("native_match_timing_plan")
    if existing_plan is not None and existing_plan != snapshot:
        raise RuntimeError("native TCP runner timing plan drift")
    existing_digest = execution.get("native_match_timing_plan_digest")
    if existing_digest is not None and existing_digest != timing_plan.digest():
        raise RuntimeError("native TCP runner timing plan digest drift")
    existing_budget = execution.get("native_full_match_liveness_budget")
    if existing_budget is not None and existing_budget != liveness_budget:
        raise RuntimeError("native TCP runner liveness budget drift")
    result = dict(execution)
    result["native_match_timing_plan"] = snapshot
    result["native_match_timing_plan_digest"] = timing_plan.digest()
    # Keep this small, human-readable projection for existing reports, but
    # never treat it as authority.  Consumers validate the full immutable
    # timing plan above.
    result["native_full_match_liveness_budget"] = liveness_budget
    if result.get("native_match_timeout_phase") == "whole_match_liveness":
        marker = (
            "native_full_match_liveness_budget_exceeded:"
            f"effective_timeout_sec={liveness_budget['effective_timeout_sec']:g}"
        )
        issues = [str(item) for item in (result.get("issues") or [])]
        if marker not in issues:
            issues.append(marker)
        result["issues"] = issues
        result["passed_compliance"] = False
    terminal_abort = result.get("native_terminal_abort")
    if terminal_abort is not None:
        code = str((terminal_abort or {}).get("code") or "unknown")
        marker = f"native_terminal_abort:{code}"
        issues = [str(item) for item in (result.get("issues") or [])]
        if marker not in issues:
            issues.append(marker)
        result["issues"] = issues
        result["passed_compliance"] = False
    return result


def validate_native_match_timing_evidence(
    execution: Any,
    *,
    timing_plan: NativeMatchTimingPlan,
) -> list[str]:
    """Return fail-closed issues for timing/terminal evidence admission.

    This intentionally validates the full plan snapshot, not merely an
    effective timeout that an upstream caller could enlarge or forge.  Quality,
    precommit, first-strict recovery, and rating admission share this function
    so a malformed timing record cannot become historical strength evidence.
    """

    if not isinstance(execution, dict):
        return ["native_timing_execution_not_object"]
    issues: list[str] = []
    expected_snapshot = timing_plan.snapshot()
    expected_digest = timing_plan.digest()
    if execution.get("native_match_timing_plan") != expected_snapshot:
        issues.append("native_match_timing_plan_missing_or_drifted")
    if execution.get("native_match_timing_plan_digest") != expected_digest:
        issues.append("native_match_timing_plan_digest_missing_or_drifted")
    if execution.get("native_full_match_liveness_budget") != timing_plan.liveness_budget_snapshot():
        issues.append("native_match_liveness_projection_missing_or_drifted")
    if execution.get("native_match_timeout_phase") is not None:
        issues.append("native_match_timeout_phase_present")
    if execution.get("native_terminal_abort") is not None:
        issues.append("native_terminal_abort_present")
    return issues


_NATIVE_PROGRESS_EVENT_TYPES = frozenset({
    "engine_started",
    "hand_start",
    "action_requested",
    "action",
    "settle",
})


def _native_match_progress_projection(
    event: Any,
    *,
    timing_plan: NativeMatchTimingPlan,
) -> dict[str, Any] | None:
    """Project a system engine event without leaking cards or wire traffic.

    The optional callback is solely an orchestrator liveness signal.  It never
    becomes replay/evidence data and only receives event facts already emitted
    by the trusted game engine.
    """

    if not isinstance(event, dict):
        return None
    event_type = str(event.get("type") or "")
    if event_type not in _NATIVE_PROGRESS_EVENT_TYPES:
        return None
    try:
        hand = int(event.get("hand") or 0)
    except (TypeError, ValueError):
        return None
    if hand < 1 or hand > timing_plan.hands:
        return None
    return {
        "schema_version": 1,
        "event_type": event_type,
        "hand": hand,
        "hands": timing_plan.hands,
        "timing_plan_digest": timing_plan.digest(),
        **(
            {
                "phase_started_at_epoch": float(
                    event["phase_started_at_epoch"]
                )
            }
            if event_type == "engine_started"
            and isinstance(event.get("phase_started_at_epoch"), (int, float))
            and not isinstance(event.get("phase_started_at_epoch"), bool)
            else {}
        ),
    }


NATIVE_BOT_TEMPLATE = r'''#!/usr/bin/env python3
"""Native national TCP entrypoint for this bot.

This file is the formal national-platform submission entry. It connects to the
TCP server, maintains authoritative raw-stream state, calls a typed local
``policy.py`` in an isolated worker, and sends only canonical national wire
actions: raise <amount>, fold, call, check, allin.
No candidate policy code owns sockets or writes protocol output.
"""

from __future__ import annotations

import argparse
from collections import deque
import importlib
import json
import multiprocessing as mp
import os
import random
import re
try:
    import resource as _resource
except ImportError:  # pragma: no cover - non-POSIX submission host
    _resource = None
import select
import signal
import socket
import subprocess
import sys
import time
import traceback


BOT_DIR = os.path.dirname(os.path.abspath(__file__))
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)

SMALL_BLIND = 50
BIG_BLIND = 100
INITIAL_CHIPS = 20000
TOTAL_HANDS = 70
CARD_RE = re.compile(r"<(\d+),(\d+)>")
ACTION_PREFIX_RE = re.compile(r"^raise [0-9]+")
EARN_PREFIX_RE = re.compile(r"^earnChips -?[0-9]+")
DEFAULT_OFFICIAL_ACTION_DELAY_SEC = 0.30
OFFICIAL_ACTION_DELAY_ENV = "POK_OFFICIAL_ACTION_DELAY"
NATIONAL_STREAM_DECODER_VERSION = 2
NATIONAL_DECISION_RUNTIME_VERSION = __POK_DECISION_RUNTIME_VERSION__
DEFAULT_STREAM_IDLE_FLUSH_SEC = 0.10
STREAM_IDLE_FLUSH_ENV = "POK_NATIONAL_STREAM_IDLE_FLUSH"
MAX_STREAM_BUFFER_CHARS = 16_384
OPPONENT_TRACKER_SCHEMA_VERSION = 4
LINE_CONTEXT_SCHEMA_VERSION = 1
DECISION_CONTEXT_SCHEMA_VERSION = 1
POLICY_DECISION_SCHEMA_VERSION = 1
NATIONAL_CARD_ENCODING = "national_tcp_suit_rank_v1"
MAX_DECISION_HISTORY_ACTIONS = 64
SHOWDOWN_RANGE_SCHEMA_VERSION = 1
OPPONENT_PRIOR_WEIGHT = 8.0
OPPONENT_ADAPTATION_CAP = 0.65
SHOWDOWN_RANGE_PRIOR_SOURCE = "uniform_1326_hole_combinations_v1"
SHOWDOWN_RANGE_BUCKET_COMBOS = {
    "premium_pair": 30,
    "small_pair": 48,
    "ace_broadway": 64,
    "broadway": 96,
    "suited_connector": 64,
    "suited_ace": 32,
    "offsuit_ace": 96,
    "suited_other": 176,
    "offsuit_other": 720,
}
SHOWDOWN_RANGE_BUCKET_PRIORS = {
    bucket: combinations / 1326.0
    for bucket, combinations in SHOWDOWN_RANGE_BUCKET_COMBOS.items()
}
RANK_SYMBOLS = "23456789TJQKA"
DEFAULT_DECISION_HARD_DEADLINE_SEC = 55.0
DEFAULT_DECISION_BASELINE_TARGET_SEC = 0.25
DEFAULT_DECISION_REFINEMENT_BUDGET_SEC = 54.0
DECISION_HARD_DEADLINE_ENV = "POK_DECISION_HARD_DEADLINE_SEC"
DECISION_BASELINE_TARGET_ENV = "POK_DECISION_BASELINE_TARGET_SEC"
DECISION_REFINEMENT_BUDGET_ENV = "POK_DECISION_REFINEMENT_BUDGET_SEC"
STRATEGY_WORKER_KILL_GRACE_SEC = 0.05
MAX_REFINEMENT_MESSAGES = 4096

_LOG_FP = None


def _log_open(path: str) -> None:
    global _LOG_FP
    if not path:
        return
    try:
        _LOG_FP = open(path, "a", encoding="utf-8", buffering=1)
    except Exception:
        _LOG_FP = None


def _log(msg: str) -> None:
    if _LOG_FP is None:
        return
    try:
        import time as _time
        _LOG_FP.write(f"[{_time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _official_action_delay_sec() -> float:
    raw = os.environ.get(OFFICIAL_ACTION_DELAY_ENV, str(DEFAULT_OFFICIAL_ACTION_DELAY_SEC))
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        delay = DEFAULT_OFFICIAL_ACTION_DELAY_SEC
    return max(0.0, min(delay, 2.0))


def _bounded_runtime_seconds(env_name: str, default: float, upper: float) -> float:
    raw = os.environ.get(env_name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    return max(0.001, min(value, upper))


def _decision_runtime_limits() -> tuple[float, float, float]:
    hard_deadline = _bounded_runtime_seconds(
        DECISION_HARD_DEADLINE_ENV,
        DEFAULT_DECISION_HARD_DEADLINE_SEC,
        DEFAULT_DECISION_HARD_DEADLINE_SEC,
    )
    refinement_ceiling = max(0.002, hard_deadline - min(0.5, hard_deadline * 0.1))
    refinement_budget = _bounded_runtime_seconds(
        DECISION_REFINEMENT_BUDGET_ENV,
        min(DEFAULT_DECISION_REFINEMENT_BUDGET_SEC, refinement_ceiling),
        refinement_ceiling,
    )
    baseline_ceiling = max(0.001, refinement_budget - min(0.05, refinement_budget * 0.1))
    baseline_target = _bounded_runtime_seconds(
        DECISION_BASELINE_TARGET_ENV,
        min(DEFAULT_DECISION_BASELINE_TARGET_SEC, baseline_ceiling),
        baseline_ceiling,
    )
    return hard_deadline, baseline_target, refinement_budget


def _decision_process_context():
    # Normal submissions run as __main__ and use portable spawn. Contract probes
    # import the entry under a synthetic module name on POSIX; fork keeps that
    # isolated test path executable while preserving killable process semantics.
    if os.name != "nt" and __name__ not in {"__main__", "__mp_main__"}:
        return mp.get_context("fork")
    return mp.get_context("spawn")


def _stream_idle_flush_sec() -> float:
    raw = os.environ.get(STREAM_IDLE_FLUSH_ENV, str(DEFAULT_STREAM_IDLE_FLUSH_SEC))
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        delay = DEFAULT_STREAM_IDLE_FLUSH_SEC
    return max(0.01, min(delay, 0.50))


def _parse_cards(text: str) -> list[tuple[int, int]]:
    """Keep cards in the national protocol's native ``(suit, rank)`` space."""
    cards = []
    for raw_suit, raw_rank in CARD_RE.findall(text):
        suit = int(raw_suit)
        rank = int(raw_rank)
        if not (0 <= suit <= 3 and 0 <= rank <= 12):
            raise ValueError(f"invalid national card <{suit},{rank}>")
        cards.append((suit, rank))
    return cards


def _context_card(card: tuple[int, int]) -> dict:
    suit, rank = card
    return {"suit": int(suit), "rank": int(rank)}


def _parse_action(raw: str) -> tuple[str, int | None]:
    if re.fullmatch(r"raise [0-9]+", raw):
        return "raise", int(raw.split(" ", 1)[1])
    if raw in {"call", "check", "fold", "allin"}:
        return raw, None
    return "unknown", None


def _take_card_message(buffer: str, prefix: str, count: int) -> tuple[str | None, str]:
    if not buffer.startswith(prefix):
        return None, buffer
    pos = len(prefix)
    for _ in range(count):
        match = CARD_RE.match(buffer, pos)
        if not match:
            return None, buffer
        pos = match.end()
    return buffer[:pos], buffer[pos:]


def _take_message(buffer: str, *, flush_numeric: bool = False) -> tuple[str | None, str]:
    if not buffer:
        return None, ""
    if buffer.startswith("name"):
        return "name", buffer[4:]
    for blind in ("SMALLBLIND", "BIGBLIND"):
        msg, rest = _take_card_message(buffer, f"preflop|{blind}|", 2)
        if msg is not None:
            return msg, rest
    for prefix, count in (("flop|", 3), ("turn|", 1), ("river|", 1), ("oppo_hands|", 2)):
        msg, rest = _take_card_message(buffer, prefix, count)
        if msg is not None:
            return msg, rest
    match = EARN_PREFIX_RE.match(buffer)
    if match:
        if match.end() == len(buffer) and not flush_numeric:
            return None, buffer
        return buffer[:match.end()], buffer[match.end():]
    match = ACTION_PREFIX_RE.match(buffer)
    if match:
        if match.end() == len(buffer) and not flush_numeric:
            return None, buffer
        return buffer[:match.end()], buffer[match.end():]
    for word in ("allin", "check", "call", "fold"):
        if buffer.startswith(word):
            return word, buffer[len(word):]
    return None, buffer


def _split_messages(buffer: str, *, flush_numeric: bool = False) -> tuple[list[str], str]:
    messages: list[str] = []
    while buffer:
        msg, rest = _take_message(buffer, flush_numeric=flush_numeric)
        if msg is None:
            return messages, rest
        messages.append(msg)
        buffer = rest
    return messages, ""


def _has_ambiguous_numeric_tail(buffer: str) -> bool:
    candidate = buffer
    if not candidate:
        return False
    return any(
        match is not None and match.end() == len(candidate)
        for match in (EARN_PREFIX_RE.match(candidate), ACTION_PREFIX_RE.match(candidate))
    )


class NationalStreamDecoder:
    """Decode the delimiter-free official stream without truncating numbers.

    A terminal ``raise 2`` may be the first fragment of ``raise 200``. Numeric
    messages are therefore committed only after a following protocol token or a
    short socket-quiescence window. Fixed-width/card and keyword messages remain
    immediately dispatchable.
    """

    def __init__(self, *, max_buffer_chars: int = MAX_STREAM_BUFFER_CHARS):
        self.buffer = ""
        self.max_buffer_chars = max_buffer_chars

    def feed(self, chunk: str) -> list[str]:
        self.buffer += chunk
        if len(self.buffer) > self.max_buffer_chars:
            raise ValueError(
                f"national protocol buffer exceeded {self.max_buffer_chars} characters"
            )
        messages, self.buffer = _split_messages(self.buffer)
        return messages

    @property
    def has_pending_numeric(self) -> bool:
        return _has_ambiguous_numeric_tail(self.buffer)

    def flush_idle(self) -> list[str]:
        messages, self.buffer = _split_messages(self.buffer, flush_numeric=True)
        return messages


class OpponentTracker:
    """Bounded connection-level facts for confidence-scaled opponent adaptation."""

    _STREETS = ("preflop", "flop", "turn", "river")
    _ACTIONS = ("fold", "check", "call", "raise", "allin")
    _SEMANTIC_ACTIONS = (*_ACTIONS, "pass")

    def __init__(self) -> None:
        self.reset_match()

    def reset_match(self) -> None:
        self.hands_started = 0
        self.hands_completed = 0
        self.showdowns = 0
        self.total_actions = 0
        self.opponent_wins = 0
        self.hero_wins = 0
        self.net_earned = 0
        self.preflop_vpip_hands = 0
        self.preflop_raise_hands = 0
        self.raise_total_sum = 0
        self.raise_total_count = 0
        self.street_actions = {
            street: {action: 0 for action in self._ACTIONS}
            for street in self._STREETS
        }
        self.semantic_street_actions = {
            street: {action: 0 for action in self._SEMANTIC_ACTIONS}
            for street in self._STREETS
        }
        self.facing_check = self._response_counter(include_pass=True)
        self.facing_check_by_street = {
            street: self._response_counter(include_pass=True)
            for street in self._STREETS
        }
        self.street_raise_sum = {street: 0.0 for street in self._STREETS}
        self.street_raise_sumsq = {street: 0.0 for street in self._STREETS}
        self.street_raise_count = {street: 0 for street in self._STREETS}
        self.facing_raise = self._response_counter()
        self.facing_allin = self._response_counter()
        self.facing_raise_by_street = {
            street: self._response_counter() for street in self._STREETS
        }
        self.facing_allin_by_street = {
            street: self._response_counter() for street in self._STREETS
        }
        self.context_actions: dict[str, dict] = {}
        self.showdown_hole_classes = {"pair": 0, "suited": 0, "offsuit": 0}
        self.showdown_range_samples = 0
        self.showdown_range_buckets = {
            bucket: 0 for bucket in SHOWDOWN_RANGE_BUCKET_PRIORS
        }
        self.showdown_hand_classes: dict[str, int] = {}
        self.showdown_contexts: dict[str, dict] = {}
        self._active_hand: dict | None = None
        self._pending_hero_pressure: dict[str, object] | None = None
        self._last_hero_action: str | None = None
        self._settled_hands: set[int] = set()
        self._showdown_hands: set[int] = set()
        self._recent_hands = deque(maxlen=8)

    def _response_counter(self, *, include_pass: bool = False) -> dict[str, int]:
        counter = {
            "opportunities": 0,
            "fold": 0,
            "call": 0,
            "raise": 0,
            "allin": 0,
        }
        if include_pass:
            counter["check"] = 0
            counter["pass"] = 0
        return counter

    def begin_hand(self, hand: int, *, opponent_is_sb: bool) -> None:
        self.hands_started += 1
        self._active_hand = {
            "hand": int(hand),
            "opponent_is_sb": bool(opponent_is_sb),
            "opponent_actions": 0,
            "opponent_vpip": False,
            "opponent_pfr": False,
            "opponent_preflop_line": "none",
            "showdown": False,
            "last_opponent_action": None,
        }
        self._pending_hero_pressure = None
        self._last_hero_action = None

    def begin_street(self) -> None:
        self._pending_hero_pressure = None
        self._last_hero_action = None

    @staticmethod
    def _size_bucket(amount: int | None) -> str:
        if not amount:
            return "none"
        bb = max(0.0, float(amount) / float(BIG_BLIND))
        return "small" if bb <= 4.0 else "medium" if bb <= 10.0 else "large"

    def _context_key(self, street: str, amount: int | None) -> str:
        position = "sb" if (self._active_hand or {}).get("opponent_is_sb") else "bb"
        pressure = self._pending_hero_pressure
        facing = str(pressure["action"]) if pressure else (
            "check" if self._last_hero_action == "check" else "unopened"
        )
        # A response context is conditioned on the hero's pressure size, never
        # on the observed outcome.  In particular, an omitted terminal fold has
        # no amount while a terminal call carries the final stage bet; using the
        # response amount would split the same decision into outcome-dependent
        # buckets and introduce selection bias.
        context_amount = pressure.get("amount") if pressure else amount
        return "|".join((street, position, facing, self._size_bucket(context_amount)))

    def observe_action(
        self,
        actor: str,
        street: str,
        action: str,
        *,
        amount: int | None = None,
        committed: int | None = None,
    ) -> None:
        if actor == "hero":
            self._pending_hero_pressure = (
                {"action": action, "amount": amount}
                if action in {"raise", "allin"}
                else None
            )
            self._last_hero_action = action
            return
        if actor != "opponent" or action not in self._ACTIONS:
            return

        street = street if street in self._STREETS else "preflop"
        semantic_action = action
        if (
            street != "preflop"
            and action == "call"
            and committed == 0
            and self._last_hero_action == "check"
        ):
            # The official protocol encodes the second player's zero-chip pass
            # after a check as ``call``.  Preserve the raw wire action above but
            # expose its poker meaning to every persistent statistic.
            semantic_action = "pass"
        self.total_actions += 1
        self.street_actions[street][action] += 1
        self.semantic_street_actions[street][semantic_action] += 1
        context_key = self._context_key(street, amount)
        context = self.context_actions.setdefault(
            context_key,
            {
                "samples": 0,
                "raw_actions": {name: 0 for name in self._ACTIONS},
                "semantic_actions": {
                    name: 0 for name in self._SEMANTIC_ACTIONS
                },
            },
        )
        context["samples"] += 1
        context["raw_actions"][action] += 1
        context["semantic_actions"][semantic_action] += 1
        if action in {"raise", "allin"} and amount is not None:
            try:
                raise_total = max(0, int(amount))
            except (TypeError, ValueError):
                raise_total = 0
            if raise_total:
                self.raise_total_sum += raise_total
                self.raise_total_count += 1
                raise_bb = raise_total / float(BIG_BLIND)
                self.street_raise_sum[street] += raise_bb
                self.street_raise_sumsq[street] += raise_bb * raise_bb
                self.street_raise_count[street] += 1
        if self._active_hand is not None:
            self._active_hand["opponent_actions"] += 1
            self._active_hand["last_opponent_action"] = action
            if street == "preflop" and action in {"call", "raise", "allin"}:
                if not self._active_hand["opponent_vpip"]:
                    self.preflop_vpip_hands += 1
                self._active_hand["opponent_vpip"] = True
            if street == "preflop" and action in {"raise", "allin"}:
                if not self._active_hand["opponent_pfr"]:
                    self.preflop_raise_hands += 1
                self._active_hand["opponent_pfr"] = True
                self._active_hand["opponent_preflop_line"] = "raise"
            elif street == "preflop" and action == "call":
                self._active_hand["opponent_preflop_line"] = (
                    "called_pressure" if self._pending_hero_pressure else "limp"
                )
            elif street == "preflop" and action == "check":
                self._active_hand["opponent_preflop_line"] = "checked_option"

        if self._last_hero_action == "check" and street != "preflop":
            self.facing_check["opportunities"] += 1
            self.facing_check[semantic_action] += 1
            street_check = self.facing_check_by_street[street]
            street_check["opportunities"] += 1
            street_check[semantic_action] += 1

        if self._pending_hero_pressure is not None:
            pressure_action = str(self._pending_hero_pressure["action"])
            counter = (
                self.facing_allin
                if pressure_action == "allin"
                else self.facing_raise
            )
            counter["opportunities"] += 1
            if action in counter:
                counter[action] += 1
            street_counter = (
                self.facing_allin_by_street[street]
                if pressure_action == "allin"
                else self.facing_raise_by_street[street]
            )
            street_counter["opportunities"] += 1
            if action in street_counter:
                street_counter[action] += 1
            self._pending_hero_pressure = None

    @staticmethod
    def _showdown_class(cards: list[tuple[int, int]]) -> tuple[str, str]:
        rank_a, rank_b = cards[0][1] + 2, cards[1][1] + 2
        high, low = max(rank_a, rank_b), min(rank_a, rank_b)
        suited = cards[0][0] == cards[1][0]
        if high == low:
            hand_class = RANK_SYMBOLS[high - 2] * 2
            bucket = "premium_pair" if high >= 10 else "small_pair"
        else:
            hand_class = (
                RANK_SYMBOLS[high - 2]
                + RANK_SYMBOLS[low - 2]
                + ("s" if suited else "o")
            )
            if high == 14 and low >= 10:
                bucket = "ace_broadway"
            elif low >= 10:
                bucket = "broadway"
            elif suited and high - low <= 2:
                bucket = "suited_connector"
            elif suited and high == 14:
                bucket = "suited_ace"
            elif not suited and high == 14:
                bucket = "offsuit_ace"
            elif suited:
                bucket = "suited_other"
            else:
                bucket = "offsuit_other"
        return hand_class, bucket

    def _showdown_context_key(self) -> str:
        active = self._active_hand or {}
        position = "sb" if active.get("opponent_is_sb") else "bb"
        if active.get("opponent_pfr"):
            line = "pfr"
        elif active.get("opponent_vpip"):
            line = "passive_vpip"
        else:
            line = str(active.get("opponent_preflop_line") or "unobserved")
        return f"{position}|{line}"

    def observe_settlement(self, hand: int, *, hero_earned: int) -> None:
        hand = int(hand)
        if hand in self._settled_hands:
            return
        self._settled_hands.add(hand)
        self.hands_completed += 1
        self.net_earned += int(hero_earned)
        if hero_earned > 0:
            self.hero_wins += 1
        elif hero_earned < 0:
            self.opponent_wins += 1
        summary = dict(self._active_hand or {"hand": hand})
        summary["hand"] = hand
        summary["hero_earned"] = int(hero_earned)
        summary["showdown"] = hand in self._showdown_hands or bool(summary.get("showdown"))
        self._recent_hands.append(summary)
        self._pending_hero_pressure = None

    def observe_showdown(
        self,
        hand: int,
        opponent_cards: list[tuple[int, int]] | None = None,
        public_cards: list[tuple[int, int]] | None = None,
    ) -> None:
        hand = int(hand)
        if hand in self._showdown_hands:
            return
        self._showdown_hands.add(hand)
        self.showdowns += 1
        cards = list(opponent_cards or [])
        if len(cards) == 2:
            ranks = [card[1] for card in cards]
            suits = [card[0] for card in cards]
            hole_class = "pair" if ranks[0] == ranks[1] else "suited" if suits[0] == suits[1] else "offsuit"
            self.showdown_hole_classes[hole_class] += 1
            hand_class, bucket = self._showdown_class(cards)
            self.showdown_range_samples += 1
            self.showdown_range_buckets[bucket] += 1
            self.showdown_hand_classes[hand_class] = (
                self.showdown_hand_classes.get(hand_class, 0) + 1
            )
            context_key = self._showdown_context_key()
            context = self.showdown_contexts.setdefault(
                context_key,
                {
                    "samples": 0,
                    "buckets": {
                        name: 0 for name in SHOWDOWN_RANGE_BUCKET_PRIORS
                    },
                },
            )
            context["samples"] += 1
            context["buckets"][bucket] += 1
        if self._active_hand is not None and self._active_hand.get("hand") == hand:
            self._active_hand["showdown"] = True
        for summary in reversed(self._recent_hands):
            if summary.get("hand") == hand:
                summary["showdown"] = True
                break

    @staticmethod
    def _smoothed_rate(successes: int, total: int, prior: float) -> float:
        return (float(successes) + prior * OPPONENT_PRIOR_WEIGHT) / (
            float(total) + OPPONENT_PRIOR_WEIGHT
        )

    def snapshot(self) -> dict:
        aggressive = sum(
            counts["raise"] + counts["allin"]
            for counts in self.street_actions.values()
        )
        fold_to_raise = self._smoothed_rate(
            self.facing_raise["fold"],
            self.facing_raise["opportunities"],
            0.35,
        )
        fold_to_allin = self._smoothed_rate(
            self.facing_allin["fold"],
            self.facing_allin["opportunities"],
            0.45,
        )
        confidence = self.total_actions / (self.total_actions + 24.0)
        adaptation_weight = min(OPPONENT_ADAPTATION_CAP, confidence * OPPONENT_ADAPTATION_CAP)
        postflop_actions = sum(
            sum(self.street_actions[street].values())
            for street in ("flop", "turn", "river")
        )
        postflop_aggressive = sum(
            self.street_actions[street]["raise"] + self.street_actions[street]["allin"]
            for street in ("flop", "turn", "river")
        )
        postflop_checks = sum(
            self.semantic_street_actions[street]["check"]
            + self.semantic_street_actions[street]["pass"]
            for street in ("flop", "turn", "river")
        )
        allins = sum(counts["allin"] for counts in self.street_actions.values())

        def street_aggression(street: str, prior: float = 0.36) -> float:
            counts = self.street_actions[street]
            aggressive_count = counts["raise"] + counts["allin"]
            return self._smoothed_rate(aggressive_count, sum(counts.values()), prior)

        def average_raise(street: str, default: float) -> float:
            count = self.street_raise_count[street]
            return self.street_raise_sum[street] / count if count else default

        def street_polarity(street: str) -> float:
            count = self.street_raise_count[street]
            if count < 3:
                return 0.0
            mean = self.street_raise_sum[street] / count
            variance = max(0.0, self.street_raise_sumsq[street] / count - mean * mean)
            cv = variance ** 0.5 / max(mean, 0.1)
            return max(-1.0, min(1.0, (cv - 0.30) / 0.30)) * min(1.0, count / 8.0)

        flop_aggr = street_aggression("flop")
        turn_aggr = street_aggression("turn")
        river_aggr = street_aggression("river")
        avg_flop_raise = average_raise("flop", 5.0)
        avg_turn_raise = average_raise("turn", 5.5)
        avg_river_raise = average_raise("river", 5.5)
        postflop_raise_samples = sum(
            self.street_raise_count[street] for street in ("flop", "turn", "river")
        )
        postflop_shoves = sum(
            self.street_actions[street]["allin"] for street in ("flop", "turn", "river")
        )
        context_profiles = {}
        for key, counts in sorted(self.context_actions.items()):
            samples = int(counts["samples"])
            context_confidence = samples / (samples + OPPONENT_PRIOR_WEIGHT)
            context_profiles[key] = {
                "samples": samples,
                "confidence": round(context_confidence, 6),
                "adaptation_weight": round(
                    min(OPPONENT_ADAPTATION_CAP, context_confidence * OPPONENT_ADAPTATION_CAP),
                    6,
                ),
                # ``actions`` remains the schema-3 raw-wire compatibility view.
                "actions": {
                    action: int(counts["raw_actions"][action])
                    for action in self._ACTIONS
                },
                "raw_actions": {
                    action: int(counts["raw_actions"][action])
                    for action in self._ACTIONS
                },
                "semantic_actions": {
                    action: int(counts["semantic_actions"][action])
                    for action in self._SEMANTIC_ACTIONS
                },
            }
        showdown_denominator = self.showdown_range_samples + OPPONENT_PRIOR_WEIGHT
        showdown_confidence = self.showdown_range_samples / showdown_denominator
        showdown_reach_rate = min(
            1.0,
            self.showdown_range_samples / max(1.0, float(self.hands_started)),
        )
        # Revealed hands are selected by reaching showdown, so even a large
        # posterior must not become an uncapped unconditional range estimate.
        # Reach coverage discounts sparse/selected observations and the shared
        # adaptation cap bounds their maximum effect on live decisions.
        showdown_adaptation_weight = min(
            OPPONENT_ADAPTATION_CAP,
            OPPONENT_ADAPTATION_CAP * showdown_confidence * showdown_reach_rate,
        )
        showdown_bucket_rates = {
            bucket: round(
                (self.showdown_range_buckets[bucket] + prior * OPPONENT_PRIOR_WEIGHT)
                / showdown_denominator,
                6,
            )
            for bucket, prior in SHOWDOWN_RANGE_BUCKET_PRIORS.items()
        }
        showdown_tightness = sum(
            showdown_bucket_rates[bucket]
            for bucket in ("premium_pair", "ace_broadway", "broadway")
        )
        showdown_context_profiles = {}
        for key, context in sorted(self.showdown_contexts.items()):
            samples = int(context["samples"])
            denominator = samples + OPPONENT_PRIOR_WEIGHT
            showdown_context_profiles[key] = {
                "samples": samples,
                "confidence": round(samples / denominator, 6),
                "adaptation_weight": round(
                    min(
                        OPPONENT_ADAPTATION_CAP,
                        OPPONENT_ADAPTATION_CAP
                        * (samples / denominator)
                        * showdown_reach_rate,
                    ),
                    6,
                ),
                "bucket_rates": {
                    bucket: round(
                        (context["buckets"][bucket] + prior * OPPONENT_PRIOR_WEIGHT)
                        / denominator,
                        6,
                    )
                    for bucket, prior in SHOWDOWN_RANGE_BUCKET_PRIORS.items()
                },
            }
        river_raise_responses = self.facing_raise_by_street["river"]
        river_allin_responses = self.facing_allin_by_street["river"]
        river_overcall_opportunities = (
            river_raise_responses["opportunities"]
            + river_allin_responses["opportunities"]
        )
        river_overcalls = (
            river_raise_responses["call"] + river_allin_responses["call"]
        )
        river_overcall_freq = self._smoothed_rate(
            river_overcalls,
            river_overcall_opportunities,
            0.55,
        )
        terminal_response_samples = (
            self.facing_raise["opportunities"] + self.facing_allin["opportunities"]
        )
        terminal_response_confidence = terminal_response_samples / (
            terminal_response_samples + OPPONENT_PRIOR_WEIGHT
        )
        return {
            "schema_version": OPPONENT_TRACKER_SCHEMA_VERSION,
            "hands_started": self.hands_started,
            "hands_completed": self.hands_completed,
            "showdowns": self.showdowns,
            "total_actions": self.total_actions,
            "confidence": round(confidence, 6),
            "adaptation_weight": round(adaptation_weight, 6),
            "rates": {
                "aggression": round(self._smoothed_rate(aggressive, self.total_actions, 0.30), 6),
                "preflop_vpip": round(
                    self._smoothed_rate(self.preflop_vpip_hands, self.hands_started, 0.55),
                    6,
                ),
                "fold_to_raise": round(fold_to_raise, 6),
                "fold_to_allin": round(fold_to_allin, 6),
            },
            "samples": {
                "preflop_vpip": self.hands_started,
                "fold_to_raise": self.facing_raise["opportunities"],
                "fold_to_allin": self.facing_allin["opportunities"],
                "raises": self.raise_total_count,
            },
            "contexts": context_profiles,
            "raw_street_actions": {
                street: dict(counts) for street, counts in self.street_actions.items()
            },
            "semantic_street_actions": {
                street: dict(counts)
                for street, counts in self.semantic_street_actions.items()
            },
            "terminal_response": {
                "samples": terminal_response_samples,
                "confidence": round(terminal_response_confidence, 6),
                "adaptation_weight": round(
                    min(
                        OPPONENT_ADAPTATION_CAP,
                        terminal_response_confidence * OPPONENT_ADAPTATION_CAP,
                    ),
                    6,
                ),
                "fold_to_raise": round(fold_to_raise, 6),
                "fold_to_jam": round(fold_to_allin, 6),
                "river_overcall": round(river_overcall_freq, 6),
                "facing_raise": dict(self.facing_raise),
                "facing_allin": dict(self.facing_allin),
                "facing_raise_by_street": {
                    street: dict(counts)
                    for street, counts in self.facing_raise_by_street.items()
                },
                "facing_allin_by_street": {
                    street: dict(counts)
                    for street, counts in self.facing_allin_by_street.items()
                },
                "contexts": {
                    key: profile
                    for key, profile in context_profiles.items()
                    if "|raise|" in key or "|allin|" in key
                },
            },
            "showdown_hole_classes": dict(self.showdown_hole_classes),
            "showdown_range": {
                "schema_version": SHOWDOWN_RANGE_SCHEMA_VERSION,
                "samples": self.showdown_range_samples,
                "confidence": round(showdown_confidence, 6),
                "adaptation_weight": round(showdown_adaptation_weight, 6),
                "showdown_reach_rate": round(showdown_reach_rate, 6),
                "selection_scope": "reached_showdown_only",
                "selection_bias_guard": "reach_rate_discount_and_capped_influence",
                "prior_source": SHOWDOWN_RANGE_PRIOR_SOURCE,
                "bucket_combo_counts": dict(SHOWDOWN_RANGE_BUCKET_COMBOS),
                "bucket_priors": dict(SHOWDOWN_RANGE_BUCKET_PRIORS),
                "bucket_counts": dict(self.showdown_range_buckets),
                "bucket_rates": showdown_bucket_rates,
                "tightness": round(showdown_tightness, 6),
                "class_counts": dict(sorted(self.showdown_hand_classes.items())),
                "contexts": showdown_context_profiles,
            },
            "average_raise_total": round(
                self.raise_total_sum / self.raise_total_count,
                3,
            ) if self.raise_total_count else 0.0,
            # Flat compatibility projection consumed by existing strategy modules.
            # Values are updated incrementally; no decision-time history scan is needed.
            "vpip": round(
                self._smoothed_rate(self.preflop_vpip_hands, self.hands_started, 0.55), 6
            ),
            "pfr": round(
                self._smoothed_rate(self.preflop_raise_hands, self.hands_started, 0.28), 6
            ),
            "allin_rate": round(self._smoothed_rate(allins, self.total_actions, 0.08), 6),
            "postflop_aggr": round(
                self._smoothed_rate(postflop_aggressive, postflop_actions, 0.36), 6
            ),
            "postflop_check_rate": round(
                self._smoothed_rate(postflop_checks, postflop_actions, 0.42), 6
            ),
            "postflop_checkback_rate": round(
                self._smoothed_rate(
                    self.facing_check["pass"],
                    self.facing_check["opportunities"],
                    0.50,
                ),
                6,
            ),
            "postflop_checkback_samples": self.facing_check["opportunities"],
            "fold_to_raise": round(fold_to_raise, 6),
            "aggression": round(
                self._smoothed_rate(aggressive, self.total_actions, 0.32), 6
            ),
            "avg_raise_bb": round(
                self.raise_total_sum / float(BIG_BLIND * self.raise_total_count), 3
            ) if self.raise_total_count else 3.0,
            "raise_samples": self.raise_total_count,
            "flop_aggr": round(flop_aggr, 6),
            "turn_aggr": round(turn_aggr, 6),
            "river_aggr": round(river_aggr, 6),
            "avg_flop_raise_bb": round(avg_flop_raise, 3),
            "avg_turn_raise_bb": round(avg_turn_raise, 3),
            "avg_river_raise_bb": round(avg_river_raise, 3),
            "barrel_freq": 0.45,
            "river_overcall_freq": round(river_overcall_freq, 6),
            "river_overcall_samples": river_overcall_opportunities,
            "fold_to_jam_rate": round(fold_to_allin, 6),
            "fold_to_jam_samples": self.facing_allin["opportunities"],
            "betsize_polarity": round(
                (street_polarity("flop") + street_polarity("turn") + street_polarity("river")) / 3.0,
                6,
            ),
            "shove_rate": round(
                self._smoothed_rate(postflop_shoves, postflop_aggressive, 0.08), 6
            ),
            "postflop_raise_samples": postflop_raise_samples,
            "postflop_shove_samples": postflop_shoves,
            "flop_shove_rate": round(
                self._smoothed_rate(
                    self.street_actions["flop"]["allin"],
                    self.street_actions["flop"]["raise"] + self.street_actions["flop"]["allin"],
                    0.08,
                ),
                6,
            ),
            "turn_shove_rate": round(
                self._smoothed_rate(
                    self.street_actions["turn"]["allin"],
                    self.street_actions["turn"]["raise"] + self.street_actions["turn"]["allin"],
                    0.08,
                ),
                6,
            ),
            "river_shove_rate": round(
                self._smoothed_rate(
                    self.street_actions["river"]["allin"],
                    self.street_actions["river"]["raise"] + self.street_actions["river"]["allin"],
                    0.08,
                ),
                6,
            ),
            "flop_shove_samples": self.street_actions["flop"]["allin"],
            "turn_shove_samples": self.street_actions["turn"]["allin"],
            "river_shove_samples": self.street_actions["river"]["allin"],
            "flop_polarity": round(street_polarity("flop"), 6),
            "turn_polarity": round(street_polarity("turn"), 6),
            "river_polarity": round(street_polarity("river"), 6),
            "street_actions": {
                street: dict(counts) for street, counts in self.street_actions.items()
            },
            "facing_raise_by_street": {
                street: dict(counts)
                for street, counts in self.facing_raise_by_street.items()
            },
            "facing_allin_by_street": {
                street: dict(counts)
                for street, counts in self.facing_allin_by_street.items()
            },
            "facing_check_by_street": {
                street: dict(counts)
                for street, counts in self.facing_check_by_street.items()
            },
            "match_result": {
                "hero_wins": self.hero_wins,
                "opponent_wins": self.opponent_wins,
                "hero_net_earned": self.net_earned,
            },
            "recent_hands": [dict(item) for item in self._recent_hands],
        }


def _resolve_seat(name: str, seat: str) -> str:
    value = seat.lower()
    if value in {"upper", "lower"}:
        return value
    lowered = name.strip().lower()
    if lowered.endswith(("b", "2", "_lower", "-lower", "lower", "bottom")):
        return "lower"
    if lowered.endswith(("a", "1", "_upper", "-upper", "upper", "top")):
        return "upper"
    return "unknown"


def _safe_policy_payload(raw_value) -> dict:
    """Copy an untrusted policy result into a primitive, pickle-safe shape.

    This is serialization hygiene only.  The socket owner remains the sole
    authority that validates a decision against the live betting state.
    """
    if type(raw_value) is not dict:
        return {"kind": "__invalid_non_mapping__"}
    if not set(raw_value).issubset({"kind", "raise_to"}):
        return {"kind": "__invalid_mapping_shape__"}
    kind = raw_value.get("kind")
    if type(kind) is not str:
        return {"kind": "__invalid_kind__"}
    payload = {"kind": kind}
    if "raise_to" in raw_value:
        raise_to = raw_value.get("raise_to")
        if type(raise_to) is not int:
            return {"kind": "__invalid_raise_to__"}
        payload["raise_to"] = raise_to
    return payload


def _policy_worker_candidate(raw_value):
    """Normalize one incremental policy result without legalizing it."""
    metadata = {}
    decision = raw_value
    if type(raw_value) is dict and "decision" in raw_value:
        if not set(raw_value).issubset({
            "decision", "sample_count", "confidence", "reason", "complete"
        }):
            decision = {"kind": "__invalid_envelope_shape__"}
        else:
            decision = raw_value.get("decision")
            metadata = {
                str(key): raw_value[key]
                for key in ("sample_count", "confidence", "reason", "complete")
                if key in raw_value
                and type(raw_value[key]) in (str, int, float, bool, type(None))
            }
    return _safe_policy_payload(decision), metadata


def _apply_policy_worker_resource_limits() -> None:
    if _resource is None:
        return
    limits = [
        (_resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024)),
        (_resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024)),
        (_resource.RLIMIT_NOFILE, (64, 64)),
        (_resource.RLIMIT_CORE, (0, 0)),
    ]
    for resource_id, value in limits:
        try:
            _resource.setrlimit(resource_id, value)
        except (ValueError, OSError):
            pass
    if hasattr(_resource, "RLIMIT_NPROC"):
        try:
            _resource.setrlimit(_resource.RLIMIT_NPROC, (32, 32))
        except (ValueError, OSError):
            pass


def _policy_worker_main(connection, bot_dir: str, random_seed: int | None) -> None:
    """Persistent, killable candidate policy runtime with no socket authority."""
    started = time.monotonic()
    try:
        if hasattr(os, "setsid"):
            # Own a process group so any candidate-created CPU workers inherit a
            # tree the socket process can terminate atomically at the deadline.
            try:
                os.setsid()
            except OSError:
                pass
        _apply_policy_worker_resource_limits()
        if random_seed is not None:
            random.seed(int(random_seed))
        if bot_dir not in sys.path:
            sys.path.insert(0, bot_dir)
        # Standardized pure-fact precompute is loaded once per worker lifetime.
        # Policy modules may import and consume it without rebuilding tables per turn.
        if os.path.isfile(os.path.join(bot_dir, "precompute.py")):
            importlib.import_module("precompute")
        policy_module = importlib.import_module("policy")
        get_baseline_decision = getattr(policy_module, "get_baseline_decision")
        iter_decisions = getattr(policy_module, "iter_decisions")
        if not callable(get_baseline_decision) or not callable(iter_decisions):
            raise TypeError(
                "policy.py must define callable get_baseline_decision(context) "
                "and iter_decisions(context, baseline, deadline)"
            )
        connection.send({
            "kind": "ready",
            "import_elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
            "pid": os.getpid(),
            "has_baseline": True,
            "has_iterator": True,
            "random_seed": random_seed,
            "process_group": os.getpgrp() if hasattr(os, "getpgrp") else None,
        })
    except BaseException as exc:
        try:
            connection.send({
                "kind": "startup_error",
                "error": f"{type(exc).__name__}:{str(exc)[:400]}",
                "pid": os.getpid(),
            })
        except BaseException:
            pass
        return

    while True:
        try:
            job = connection.recv()
        except (EOFError, OSError):
            return
        if not isinstance(job, dict):
            continue
        if job.get("kind") == "stop":
            return
        if job.get("kind") != "decide":
            continue
        decision_id = int(job.get("decision_id") or 0)
        context = job.get("decision_context") or {}
        deadline = float(job.get("deadline_monotonic") or time.monotonic())
        fallback = _safe_policy_payload(job.get("fallback_decision") or {})
        raw_candidate_limit = job.get("max_refinement_candidates")
        candidate_limit = (
            None
            if raw_candidate_limit is None
            else max(0, min(MAX_REFINEMENT_MESSAGES, int(raw_candidate_limit)))
        )
        sequence = 0
        try:
            def publish(phase: str, raw_decision, metadata=None, trusted=None) -> dict:
                nonlocal sequence
                decision = _safe_policy_payload(raw_decision)
                sequence += 1
                connection.send({
                    "kind": "candidate",
                    "decision_id": decision_id,
                    "phase": phase,
                    "sequence": sequence,
                    "decision": decision,
                    "published_monotonic": time.monotonic(),
                    "metadata": dict(metadata or {}),
                    "trusted": dict(trusted or {}),
                })
                return decision

            baseline = fallback
            if time.monotonic() < deadline:
                baseline = publish(
                    "baseline",
                    get_baseline_decision(context),
                    {"source": "get_baseline_decision"},
                )

            refinement_started_cpu = time.process_time_ns()
            refinement_started_wall = time.monotonic_ns()
            iterator_steps = 0
            iterator_exhausted = False
            termination_reason = "not_available"
            if (
                candidate_limit != 0
                and time.monotonic() < deadline
            ):
                iterator = iter_decisions(context, baseline, deadline)
                termination_reason = "deadline"
                while True:
                    if iterator_steps >= MAX_REFINEMENT_MESSAGES:
                        termination_reason = "message_limit"
                        break
                    if candidate_limit is not None and iterator_steps >= candidate_limit:
                        termination_reason = "probe_candidate_limit"
                        break
                    if time.monotonic() >= deadline:
                        termination_reason = "deadline"
                        break
                    try:
                        raw_candidate = next(iterator)
                    except StopIteration:
                        iterator_exhausted = True
                        termination_reason = "iterator_exhausted"
                        break
                    iterator_steps += 1
                    candidate, metadata = _policy_worker_candidate(raw_candidate)
                    baseline = publish(
                        "refinement",
                        candidate,
                        metadata,
                        {
                            "iterator_step": iterator_steps,
                            "process_cpu_ms": round(
                                (time.process_time_ns() - refinement_started_cpu) / 1_000_000.0,
                                6,
                            ),
                            "elapsed_ms": round(
                                (time.monotonic_ns() - refinement_started_wall) / 1_000_000.0,
                                6,
                            ),
                        },
                    )
            elif candidate_limit == 0:
                termination_reason = "probe_candidate_limit"
            connection.send({
                "kind": "done",
                "decision_id": decision_id,
                "sequence": sequence,
                "decision": baseline,
                "completed_monotonic": time.monotonic(),
                "refinement_stats": {
                    "iterator_steps": iterator_steps,
                    "iterator_exhausted": iterator_exhausted,
                    "termination_reason": termination_reason,
                    "process_cpu_ms": round(
                        (time.process_time_ns() - refinement_started_cpu) / 1_000_000.0,
                        6,
                    ),
                    "elapsed_ms": round(
                        (time.monotonic_ns() - refinement_started_wall) / 1_000_000.0,
                        6,
                    ),
                },
            })
        except BaseException as exc:
            try:
                connection.send({
                    "kind": "decision_error",
                    "decision_id": decision_id,
                    "sequence": sequence,
                    "error": f"{type(exc).__name__}:{str(exc)[:400]}",
                })
            except BaseException:
                return


class NativeNationalBot:
    def __init__(self, name: str, seat: str = "auto"):
        self.name = name
        self.seat = _resolve_seat(name, seat)
        self._official_action_delay_sec = _official_action_delay_sec()
        (
            self._decision_hard_deadline_sec,
            self._decision_baseline_target_sec,
            self._decision_refinement_budget_sec,
        ) = _decision_runtime_limits()
        self._last_platform_message_at = 0.0
        self._reset_match()
        self._mp_context = _decision_process_context()
        self._strategy_process = self._strategy_connection = None
        self._strategy_worker_generation = 0
        # Raw name starts the persistent worker but never waits for readiness.
        self._name_handshake_count = 0
        try:
            self._strategy_base_seed = int(os.environ["POK_NATIVE_BOT_SEED"])
        except (KeyError, TypeError, ValueError):
            self._strategy_base_seed = None
        self._strategy_worker_seed = None
        self._strategy_max_refinement_candidates = None
        self._decision_serial = 0
        self._retired_strategy_processes = []
        self._unconfirmed_strategy_tree_pids = set()
        self._last_stopped_worker_pid = None
        self._last_stopped_worker_exitcode = None
        self._last_stopped_worker_tree_termination_requested = False
        self._last_stopped_worker_terminated = False

    def _reset_match(self) -> None:
        self._buf = ""
        self._my_cards: list[tuple[int, int]] = []
        self._public_cards: list[tuple[int, int]] = []
        self._is_sb = False
        self._hand_num = 0
        self._history: list[dict] = []
        self._stage = "preflop"
        self._my_id = 0
        self._opponent_id = 1
        self._my_action_count = 0
        self._my_chips = INITIAL_CHIPS
        self._my_stage_bet = 0
        self._opponent_chips = INITIAL_CHIPS
        self._opponent_stage_bet = 0
        self._pot = 0
        self._in_allin_runout = False
        self._showdown_by_hand: dict[int, dict] = {}
        self._earned_by_hand: dict[int, int] = {}
        self._opponent_tracker = OpponentTracker()
        self._last_decision_source = "uninitialized"
        self._last_decision_metrics = {}

    def _acts_first_postflop(self) -> bool:
        return not self._is_sb

    def _responding_to_check(self) -> bool:
        round_num = self._round_num()
        return bool(
            self._my_action_count == 0
            and self._history
            and self._history[-1].get("round") == round_num
            and self._history[-1].get("player_id") == self._opponent_id
            and self._history[-1].get("action_type") == "check"
        )

    def _round_num(self) -> int:
        return {"preflop": 0, "flop": 1, "turn": 2, "river": 3}.get(self._stage, 0)

    def _actor_label(self, player_id: int | None) -> str:
        if player_id == self._my_id:
            return "hero"
        if player_id == self._opponent_id:
            return "opponent"
        return "unknown"

    def _street_records(self, round_num: int) -> list[dict]:
        return [
            record for record in self._history
            if record.get("round") == round_num
        ]

    def _semantic_street_summary(self, round_num: int) -> dict:
        records = self._street_records(round_num)
        actions = []
        saw_opening_check = False
        for record in records:
            action_type = str(record.get("action_type") or "unknown")
            committed = int(record.get("committed", 0) or 0)
            semantic_action = action_type
            if action_type == "check":
                saw_opening_check = True
                semantic_action = "check"
            elif action_type == "call" and committed == 0 and saw_opening_check:
                semantic_action = "pass"
            elif action_type == "call":
                semantic_action = "match"
            actions.append({
                "actor": self._actor_label(record.get("player_id")),
                "action": action_type,
                "semantic_action": semantic_action,
                "committed": committed,
                "inferred": bool(record.get("inferred")),
            })
        checked_through = bool(
            len(actions) >= 2
            and actions[0]["semantic_action"] == "check"
            and actions[-1]["semantic_action"] == "pass"
            and not any(
                item["action"] in {"raise", "allin"} for item in actions
            )
        )
        last = actions[-1] if actions else None
        return {
            "round": round_num,
            "actions": actions,
            "checked_through": checked_through,
            "closed_by": last["actor"] if last else None,
            "opponent_checked_back": bool(
                checked_through
                and last
                and last["actor"] == "opponent"
            ),
            "hero_checked_back": bool(
                checked_through
                and last
                and last["actor"] == "hero"
            ),
        }

    def _preflop_line(self) -> tuple[str, str]:
        records = self._street_records(0)
        raises = [
            record for record in records
            if record.get("action_type") in {"raise", "allin"}
        ]
        aggressor = (
            self._actor_label(raises[-1].get("player_id"))
            if raises else "none"
        )
        opponent_raised = any(
            record.get("player_id") == self._opponent_id
            and record.get("action_type") in {"raise", "allin"}
            for record in records
        )
        hero_raised = any(
            record.get("player_id") == self._my_id
            and record.get("action_type") in {"raise", "allin"}
            for record in records
        )
        first_raise_index = next(
            (
                index for index, record in enumerate(records)
                if record.get("action_type") in {"raise", "allin"}
            ),
            len(records),
        )
        opponent_limped = any(
            record.get("player_id") == self._opponent_id
            and record.get("action_type") == "call"
            for record in records[:first_raise_index]
        )
        hero_limped = any(
            record.get("player_id") == self._my_id
            and record.get("action_type") == "call"
            for record in records[:first_raise_index]
        )
        if not self._is_sb:
            if opponent_raised:
                spot = "bb_vs_raise"
            elif opponent_limped:
                spot = "bb_vs_limp"
            else:
                spot = "bb_option"
        elif opponent_raised:
            spot = "sb_vs_reraise"
        elif hero_raised:
            spot = "sb_open"
        elif hero_limped:
            spot = "sb_limp"
        else:
            spot = "sb_open"
        return aggressor, spot

    def _line_state(self) -> dict:
        """Return socket-authoritative cross-street line semantics."""
        round_num = self._round_num()
        current = self._semantic_street_summary(round_num)
        previous = (
            self._semantic_street_summary(round_num - 1)
            if round_num > 0 else None
        )
        preflop_aggressor, preflop_spot = self._preflop_line()
        street_open = not current["actions"]
        hero_position = "sb" if self._is_sb else "bb"
        can_donk = bool(
            round_num == 1
            and street_open
            and hero_position == "bb"
            and preflop_aggressor == "opponent"
        )
        can_delayed_probe = bool(
            round_num in {2, 3}
            and street_open
            and hero_position == "bb"
            and preflop_aggressor == "opponent"
            and previous
            and previous["opponent_checked_back"]
        )
        line_tags = []
        if can_donk:
            line_tags.append("donk_opportunity")
        if can_delayed_probe:
            line_tags.append("delayed_probe_opportunity")
        if previous and previous["checked_through"]:
            line_tags.append("previous_street_checked_through")
        if self._responding_to_check():
            line_tags.append("responding_to_check")
        return {
            "schema_version": LINE_CONTEXT_SCHEMA_VERSION,
            "street": self._stage,
            "street_index": round_num,
            "position": "small_blind" if self._is_sb else "big_blind",
            "hero_in_position_postflop": self._is_sb,
            "preflop_aggressor": preflop_aggressor,
            "preflop_spot": preflop_spot,
            "street_open": street_open,
            "responding_to_check": self._responding_to_check(),
            "can_donk": can_donk,
            "can_delayed_probe": can_delayed_probe,
            "line_tags": line_tags,
            "current_street": current,
            "previous_street": previous,
        }

    def _match_control_state(self, remaining_including_current: int) -> dict:
        """Publish the exact worst-case fold-to-finish match bound.

        Each national hand resets to ``INITIAL_CHIPS`` and seat/blind roles
        alternate.  Folding now loses only this hand's committed exposure;
        folding every later hand loses that seat's forced blind.  Strictly
        exceeding the sum guarantees a match win, while equality guarantees
        only a draw and is deliberately not marked locked.
        """

        remaining = max(1, min(TOTAL_HANDS, int(remaining_including_current)))
        future_hands = remaining - 1
        complete_pairs, odd = divmod(future_hands, 2)
        future_forced_blinds = complete_pairs * (SMALL_BLIND + BIG_BLIND)
        if odd:
            future_forced_blinds += BIG_BLIND if self._is_sb else SMALL_BLIND
        current_exposure = max(0, INITIAL_CHIPS - int(self._my_chips))
        forced_fold_loss_bound = current_exposure + future_forced_blinds
        hero_net_earned = int(self._opponent_tracker.net_earned)
        return {
            "schema_version": 1,
            "initial_chips": INITIAL_CHIPS,
            "small_blind": SMALL_BLIND,
            "big_blind": BIG_BLIND,
            "current_position": "small_blind" if self._is_sb else "big_blind",
            "current_exposure": current_exposure,
            "future_forced_blinds": future_forced_blinds,
            "forced_fold_loss_bound": forced_fold_loss_bound,
            "hero_net_earned": hero_net_earned,
            "fold_locks_win": hero_net_earned > forced_fold_loss_bound,
        }

    def _semantic_hand_history(self) -> dict:
        actions = []
        last_by_round = {}
        for record in self._history:
            round_num = int(record.get("round", 0))
            wire_kind = str(record.get("action_type") or "unknown")
            committed = int(record.get("committed", 0) or 0)
            previous = last_by_round.get(round_num)
            if wire_kind == "check":
                semantic_kind = "pass"
            elif wire_kind == "call" and (
                committed == 0
                or (previous and previous.get("wire_kind") == "check")
            ):
                semantic_kind = "pass"
            elif wire_kind == "call":
                semantic_kind = "match"
            else:
                semantic_kind = wire_kind
            item = {
                "street": record.get("street", self._stage),
                "street_index": round_num,
                "actor": self._actor_label(record.get("player_id")),
                "wire_kind": wire_kind,
                "semantic_kind": semantic_kind,
                "committed": committed,
                "stage_bet_after": int(record.get("stage_bet", 0) or 0),
                "inferred": bool(record.get("inferred")),
            }
            if wire_kind == "raise":
                item["raise_to"] = int(record.get("stage_bet", 0) or 0)
            if record.get("inferred"):
                item["inference_boundary"] = str(
                    record.get("inference_boundary") or ""
                )
            actions.append(item)
            last_by_round[round_num] = item
        truncated = max(0, len(actions) - MAX_DECISION_HISTORY_ACTIONS)
        return {
            "schema_version": 1,
            "actions": actions[-MAX_DECISION_HISTORY_ACTIONS:],
            "truncated_count": truncated,
        }

    def _pass_wire_kind(self) -> str:
        """Map candidate ``pass`` to the only legal official pass token."""
        if self._opponent_stage_bet > self._my_stage_bet:
            return "call"
        if self._responding_to_check():
            return "call"
        return "check"

    def _legal_policy_state(self) -> dict:
        policy_kinds = ["fold", "pass"]
        allin_occurred = self._current_round_has_allin()
        if not allin_occurred and self._my_chips > 0:
            policy_kinds.append("allin")
        minimum = self._minimum_raise_total()
        # An exact stack commitment must use the official ``allin`` token.
        maximum = self._my_stage_bet + max(0, self._my_chips - 1)
        if not allin_occurred and minimum <= maximum:
            policy_kinds.append("raise")
            min_raise_to = minimum
            max_raise_to = maximum
        else:
            min_raise_to = None
            max_raise_to = None
        return {
            "schema_version": POLICY_DECISION_SCHEMA_VERSION,
            "policy_kinds": policy_kinds,
            "pass_wire_kind": self._pass_wire_kind(),
            "min_raise_to": min_raise_to,
            "max_raise_to": max_raise_to,
            "raise_boundary": "inclusive_exact_2x_raise_to",
        }

    def _build_decision_context(
        self,
        *,
        decision_id: int,
        hard_deadline: float,
        refinement_deadline: float,
    ) -> dict:
        """Build the sole bounded, versioned candidate policy input."""
        to_call = max(0, self._opponent_stage_bet - self._my_stage_bet)
        effective_stack = min(self._my_chips, self._opponent_chips)
        remaining = max(1, TOTAL_HANDS - max(0, self._hand_num - 1))
        return {
            "schema_version": DECISION_CONTEXT_SCHEMA_VERSION,
            "runtime_version": NATIONAL_DECISION_RUNTIME_VERSION,
            "decision_id": int(decision_id),
            "cards": {
                "encoding": NATIONAL_CARD_ENCODING,
                "hole": [_context_card(card) for card in self._my_cards],
                "board": [_context_card(card) for card in self._public_cards],
            },
            "hand": {
                "number": int(self._hand_num),
                "total_hands": TOTAL_HANDS,
                "remaining_including_current": remaining,
                "street": self._stage,
                "street_index": self._round_num(),
                "position": "small_blind" if self._is_sb else "big_blind",
                "acts_first_postflop": self._acts_first_postflop(),
                "match_control": self._match_control_state(remaining),
            },
            "betting": {
                "pot": int(self._pot),
                "hero_stack": int(self._my_chips),
                "opponent_stack": int(self._opponent_chips),
                "effective_stack": int(effective_stack),
                "hero_street_bet": int(self._my_stage_bet),
                "opponent_street_bet": int(self._opponent_stage_bet),
                "to_call": int(to_call),
                "spr": round(effective_stack / max(1.0, float(self._pot)), 6),
                "pot_odds": round(
                    to_call / max(1.0, float(self._pot + to_call)), 6
                ),
                "call_closes_allin_runout": bool(
                    to_call > 0
                    and (
                        self._opponent_chips == 0
                        or self._my_chips <= to_call
                    )
                ),
            },
            "history": self._semantic_hand_history(),
            "line": self._line_state(),
            "legal": self._legal_policy_state(),
            "opponent": self._opponent_tracker.snapshot(),
            "deadline": {
                "clock": "time.monotonic",
                "hard_monotonic": float(hard_deadline),
                "refinement_monotonic": float(refinement_deadline),
                "hard_budget_ms": int(self._decision_hard_deadline_sec * 1000),
                "baseline_target_ms": int(
                    self._decision_baseline_target_sec * 1000
                ),
                "refinement_budget_ms": int(
                    self._decision_refinement_budget_sec * 1000
                ),
            },
        }

    def _socket_safe_fallback_decision(self) -> dict:
        """Return a typed risk-safe decision before candidate code runs."""
        if self._opponent_stage_bet > self._my_stage_bet:
            return {"kind": "fold"}
        return {"kind": "pass"}

    def _legalize_policy_decision(self, raw_decision, fallback: dict) -> dict:
        """Validate untrusted policy output against socket-owned live state."""
        safe_fallback = dict(fallback)
        if type(raw_decision) is not dict:
            return safe_fallback
        if not set(raw_decision).issubset({"kind", "raise_to"}):
            return safe_fallback
        kind = raw_decision.get("kind")
        if type(kind) is not str:
            return safe_fallback
        legal = self._legal_policy_state()
        if kind not in legal["policy_kinds"]:
            return safe_fallback
        if kind != "raise":
            if "raise_to" in raw_decision:
                return safe_fallback
            return {"kind": kind}
        if set(raw_decision) != {"kind", "raise_to"}:
            return safe_fallback
        raise_to = raw_decision.get("raise_to")
        if type(raise_to) is not int:
            return safe_fallback
        minimum = legal.get("min_raise_to")
        maximum = legal.get("max_raise_to")
        if minimum is None or maximum is None or not minimum <= raise_to <= maximum:
            return safe_fallback
        return {"kind": "raise", "raise_to": raise_to}

    def _strategy_worker_alive(self) -> bool:
        return bool(self._strategy_process is not None and self._strategy_process.is_alive())

    @staticmethod
    def _terminate_worker_tree(process, *, force: bool) -> bool:
        """Request whole-tree termination, returning whether that request landed.

        A parent-only ``Process.kill`` fallback is deliberately not reported as
        whole-tree success: candidate-created children could otherwise survive
        while the runtime advertises a clean kill.  Callers separately verify
        that the multiprocessing worker itself has actually exited.
        """
        pid = getattr(process, "pid", None)
        tree_requested = False
        if pid and os.name == "posix" and hasattr(os, "killpg"):
            try:
                os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
                tree_requested = True
            except ProcessLookupError:
                # No process remains in that process group: cleanup is already
                # complete and replacement is safe.
                return True
            except (PermissionError, OSError):
                pass
        elif pid and os.name == "nt":
            try:
                killer = subprocess.Popen(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
                try:
                    tree_requested = (
                        killer.wait(timeout=STRATEGY_WORKER_KILL_GRACE_SEC) == 0
                    )
                except subprocess.TimeoutExpired:
                    try:
                        killer.kill()
                        killer.wait(timeout=STRATEGY_WORKER_KILL_GRACE_SEC)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            except OSError:
                pass
        if not tree_requested:
            try:
                if force and hasattr(process, "kill"):
                    process.kill()
                else:
                    process.terminate()
            except (AssertionError, OSError, ValueError):
                pass
        return tree_requested

    def _reap_retired_strategy_workers(self, *, wait: bool) -> None:
        """Reap workers terminated on a decision deadline at a safe point."""
        remaining = []
        for process in self._retired_strategy_processes:
            pid = getattr(process, "pid", None)
            try:
                process.join(timeout=0)
                if wait:
                    tree_confirmed = self._terminate_worker_tree(process, force=True)
                    if tree_confirmed and pid is not None:
                        self._unconfirmed_strategy_tree_pids.discard(pid)
                    elif pid is not None:
                        self._unconfirmed_strategy_tree_pids.add(pid)
                    process.join(timeout=STRATEGY_WORKER_KILL_GRACE_SEC)
                if (
                    process.is_alive()
                    or pid in self._unconfirmed_strategy_tree_pids
                ):
                    remaining.append(process)
            except (AssertionError, OSError, ValueError):
                if pid is not None:
                    self._unconfirmed_strategy_tree_pids.add(pid)
                if process.is_alive() or pid in self._unconfirmed_strategy_tree_pids:
                    remaining.append(process)
        self._retired_strategy_processes = remaining

    def _stop_strategy_worker(self, reason: str, *, wait: bool = True) -> None:
        process = self._strategy_process
        connection = self._strategy_connection
        tree_termination_requested = False
        terminated = process is None
        if connection is not None and wait:
            try:
                connection.send({"kind": "stop", "reason": reason})
            except (BrokenPipeError, EOFError, OSError):
                pass
        if process is not None:
            stopped_pid = process.pid
            if wait:
                # A polite worker exit is insufficient when candidate code may
                # have spawned a fixed CPU pool or subprocess. Always target the
                # process group/tree while the parent PID is still available;
                # otherwise a clean parent return could orphan live compute.
                tree_termination_requested = self._terminate_worker_tree(
                    process,
                    force=True,
                )
                process.join(timeout=STRATEGY_WORKER_KILL_GRACE_SEC)
            else:
                # The socket deadline owns this path. Request termination but
                # use the reserved hard-deadline margin for one bounded join.
                # SIGKILL/taskkill cannot be ignored, unlike the old SIGTERM-only
                # path that could accumulate one live CPU worker per decision.
                try:
                    if process.is_alive():
                        tree_termination_requested = self._terminate_worker_tree(
                            process,
                            force=True,
                        )
                    process.join(timeout=STRATEGY_WORKER_KILL_GRACE_SEC)
                except (AssertionError, OSError, ValueError):
                    tree_termination_requested = False
                if process.is_alive():
                    self._retired_strategy_processes.append(process)
            terminated = not process.is_alive()
            if tree_termination_requested:
                self._unconfirmed_strategy_tree_pids.discard(stopped_pid)
            else:
                self._unconfirmed_strategy_tree_pids.add(stopped_pid)
            if (
                process.is_alive()
                or stopped_pid in self._unconfirmed_strategy_tree_pids
            ) and process not in self._retired_strategy_processes:
                self._retired_strategy_processes.append(process)
            _log(
                f"DECISION_WORKER stop reason={reason} pid={process.pid} "
                f"alive={process.is_alive()} exitcode={process.exitcode} wait={wait} "
                f"tree_request={tree_termination_requested} terminated={terminated}"
            )
            self._last_stopped_worker_pid = stopped_pid
            self._last_stopped_worker_exitcode = process.exitcode
            self._last_stopped_worker_tree_termination_requested = (
                tree_termination_requested
            )
            self._last_stopped_worker_terminated = terminated
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        self._strategy_process = None
        self._strategy_connection = None

    def _ensure_strategy_worker(self) -> bool:
        # Never replace a timed-out worker until the old process is confirmed
        # dead.  This is fail-closed on platforms where whole-tree termination
        # cannot be confirmed and prevents silent CPU oversubscription.
        self._reap_retired_strategy_workers(wait=True)
        if self._retired_strategy_processes or self._unconfirmed_strategy_tree_pids:
            _log(
                "DECISION_WORKER replacement_blocked "
                f"retired={len(self._retired_strategy_processes)} "
                f"unconfirmed_trees={len(self._unconfirmed_strategy_tree_pids)}"
            )
            return False
        if self._strategy_worker_alive() and self._strategy_connection is not None:
            return True
        self._stop_strategy_worker("replace_dead_worker")
        if self._retired_strategy_processes or self._unconfirmed_strategy_tree_pids:
            _log("DECISION_WORKER replacement_blocked_after_dead_worker_cleanup")
            return False
        parent_connection, child_connection = self._mp_context.Pipe(duplex=True)
        next_generation = self._strategy_worker_generation + 1
        worker_seed = (
            None
            if self._strategy_base_seed is None
            else self._strategy_base_seed + next_generation - 1
        )
        process = self._mp_context.Process(
            target=_policy_worker_main,
            args=(child_connection, BOT_DIR, worker_seed),
            name=f"national-policy-{next_generation}",
            # Non-daemon is intentional: policy may own a bounded fixed CPU
            # pool. The socket owner enforces the deadline on its whole process
            # group/tree rather than allowing descendants to escape.
            daemon=False,
        )
        try:
            process.start()
        except BaseException as exc:
            parent_connection.close()
            child_connection.close()
            _log(f"DECISION_WORKER start_error={type(exc).__name__}:{str(exc)[:240]}")
            return False
        child_connection.close()
        self._strategy_worker_generation += 1
        self._strategy_worker_seed = worker_seed
        self._strategy_process = process
        self._strategy_connection = parent_connection
        _log(
            f"DECISION_WORKER started pid={process.pid} "
            f"generation={self._strategy_worker_generation} seed={worker_seed}"
        )
        return True

    def close(self) -> None:
        self._stop_strategy_worker("client_close")
        self._reap_retired_strategy_workers(wait=True)

    def _policy_decision(self) -> dict:
        started = time.monotonic()
        hard_deadline = started + self._decision_hard_deadline_sec
        baseline_target = started + self._decision_baseline_target_sec
        refinement_deadline = started + self._decision_refinement_budget_sec
        baseline = self._socket_safe_fallback_decision()
        self._last_decision_source = "socket_baseline"
        self._decision_serial += 1
        decision_id = self._decision_serial
        try:
            context = self._build_decision_context(
                decision_id=decision_id,
                hard_deadline=hard_deadline,
                refinement_deadline=refinement_deadline,
            )
        except BaseException as exc:
            _log(
                f"DECIDE context_error={type(exc).__name__}:{str(exc)[:160]!r} "
                f"fallback={baseline}"
            )
            self._last_decision_source = "context_error_baseline"
            return baseline
        decision_metrics = {
            "runtime_version": NATIONAL_DECISION_RUNTIME_VERSION,
            "decision_id": decision_id,
            "socket_fallback_decision": dict(baseline),
            "socket_fallback_ready_ms": round((time.monotonic() - started) * 1000.0, 3),
            "baseline_published_ms": None,
            "baseline_target_ms": round(
                self._decision_baseline_target_sec * 1000.0,
                3,
            ),
            "baseline_target_met": False,
            "policy_baseline_decision": None,
            "refinement_messages": 0,
            "refinement_decision_changes": 0,
            "refinement_progress": [],
            "trusted_refinement_steps": 0,
            "trusted_refinement_cpu_ms": 0.0,
            "trusted_refinement_elapsed_ms": 0.0,
            "refinement_iterator_exhausted": False,
            "refinement_termination_reason": None,
            "latest_sequence": 0,
            "timed_out": False,
            "worker_terminated": False,
            "worker_generation": None,
            "worker_pid": None,
            "completed": False,
        }
        self._last_decision_metrics = decision_metrics
        if not self._ensure_strategy_worker():
            self._last_decision_source = "worker_start_error_baseline"
            return baseline
        decision_metrics["worker_generation"] = self._strategy_worker_generation
        decision_metrics["worker_pid"] = self._strategy_process.pid
        decision_metrics["worker_seed"] = self._strategy_worker_seed
        connection = self._strategy_connection
        try:
            connection.send({
                "kind": "decide",
                "decision_id": decision_id,
                "decision_context": context,
                "fallback_decision": baseline,
                "deadline_monotonic": refinement_deadline,
                "max_refinement_candidates": self._strategy_max_refinement_candidates,
            })
        except (BrokenPipeError, EOFError, OSError) as exc:
            _log(f"DECIDE worker_send_error={type(exc).__name__}:{str(exc)[:160]}")
            self._stop_strategy_worker("send_error")
            self._last_decision_source = "worker_send_error_baseline"
            return baseline

        baseline_target_logged = False
        latest_sequence = 0
        while True:
            now = time.monotonic()
            if not baseline_target_logged and now >= baseline_target:
                baseline_target_logged = True
                _log(
                    f"DECIDE baseline_target_missed decision_id={decision_id} "
                    f"target={self._decision_baseline_target_sec:.3f}s safe_decision={baseline}"
                )
            remaining = min(refinement_deadline, hard_deadline) - now
            if remaining <= 0:
                timed_out_pid = self._strategy_process.pid if self._strategy_process else None
                self._stop_strategy_worker("decision_deadline", wait=False)
                decision_metrics["timed_out"] = True
                decision_metrics["worker_terminated"] = (
                    timed_out_pid is not None
                    and self._last_stopped_worker_pid == timed_out_pid
                    and self._last_stopped_worker_terminated
                    and self._last_stopped_worker_tree_termination_requested
                )
                self._last_decision_source = "refinement_deadline_latest_safe"
                _log(
                    f"DECIDE refinement_deadline decision_id={decision_id} "
                    f"latest_safe={baseline} sequence={latest_sequence} "
                    f"refinement_budget={self._decision_refinement_budget_sec:.3f}s "
                    f"hard_deadline={self._decision_hard_deadline_sec:.3f}s"
                )
                return baseline
            if not self._strategy_worker_alive():
                self._stop_strategy_worker("worker_exited")
                self._last_decision_source = "worker_exited_latest_safe"
                return baseline
            wait_for = min(remaining, 0.25)
            if not baseline_target_logged:
                wait_for = min(wait_for, max(0.001, baseline_target - now))
            try:
                if not connection.poll(wait_for):
                    continue
                message = connection.recv()
            except (BrokenPipeError, EOFError, OSError) as exc:
                _log(f"DECIDE worker_recv_error={type(exc).__name__}:{str(exc)[:160]}")
                self._stop_strategy_worker("recv_error")
                self._last_decision_source = "worker_recv_error_latest_safe"
                return baseline
            if not isinstance(message, dict):
                continue
            kind = message.get("kind")
            if kind == "ready":
                _log(
                    f"DECISION_WORKER ready pid={message.get('pid')} "
                    f"import_ms={message.get('import_elapsed_ms')} "
                    f"baseline={message.get('has_baseline')} iterator={message.get('has_iterator')}"
                )
                continue
            if kind == "startup_error":
                _log(f"DECIDE worker_startup_error={message.get('error')!r}")
                self._stop_strategy_worker("startup_error")
                self._last_decision_source = "worker_startup_error_baseline"
                return baseline
            if int(message.get("decision_id") or -1) != decision_id:
                _log(
                    f"DECIDE stale_worker_message expected={decision_id} "
                    f"actual={message.get('decision_id')} kind={kind}"
                )
                continue
            result_at = time.monotonic()
            if result_at >= hard_deadline:
                timed_out_pid = self._strategy_process.pid if self._strategy_process else None
                self._stop_strategy_worker("hard_deadline", wait=False)
                decision_metrics["timed_out"] = True
                decision_metrics["worker_terminated"] = (
                    timed_out_pid is not None
                    and self._last_stopped_worker_pid == timed_out_pid
                    and self._last_stopped_worker_terminated
                    and self._last_stopped_worker_tree_termination_requested
                )
                self._last_decision_source = "hard_deadline_latest_safe"
                _log(
                    f"DECIDE hard_deadline decision_id={decision_id} "
                    f"latest_safe={baseline} sequence={latest_sequence} "
                    f"hard_deadline={self._decision_hard_deadline_sec:.3f}s"
                )
                return baseline
            if kind == "candidate":
                sequence = int(message.get("sequence") or 0)
                if sequence <= latest_sequence:
                    continue
                latest_sequence = sequence
                decision_metrics["latest_sequence"] = sequence
                previous_decision = baseline
                baseline = self._legalize_policy_decision(
                    message.get("decision"),
                    baseline,
                )
                phase = str(message.get("phase") or "refinement")
                elapsed = result_at - started
                metadata = message.get("metadata") or {}
                trusted = message.get("trusted") or {}
                if phase == "baseline":
                    decision_metrics["policy_baseline_decision"] = dict(baseline)
                    decision_metrics["baseline_published_ms"] = round(elapsed * 1000.0, 3)
                    decision_metrics["baseline_target_met"] = (
                        elapsed <= self._decision_baseline_target_sec
                    )
                    self._last_decision_source = "policy_baseline"
                    _log(
                        f"DECIDE baseline decision_id={decision_id} decision={baseline} "
                        f"elapsed={elapsed:.3f}s "
                        f"target_met={elapsed <= self._decision_baseline_target_sec}"
                    )
                else:
                    decision_metrics["refinement_messages"] += 1
                    if baseline != previous_decision:
                        decision_metrics["refinement_decision_changes"] += 1
                    if len(decision_metrics["refinement_progress"]) < 64:
                        decision_metrics["refinement_progress"].append({
                            "sequence": sequence,
                            "decision": dict(baseline),
                            "reported_sample_count": metadata.get("sample_count"),
                            "reported_confidence": metadata.get("confidence"),
                            "reported_complete": metadata.get("complete"),
                            "trusted_iterator_step": trusted.get("iterator_step"),
                            "trusted_process_cpu_ms": trusted.get("process_cpu_ms"),
                            "trusted_elapsed_ms": trusted.get("elapsed_ms"),
                        })
                    decision_metrics["trusted_refinement_steps"] = max(
                        int(decision_metrics["trusted_refinement_steps"]),
                        int(trusted.get("iterator_step") or 0),
                    )
                    decision_metrics["trusted_refinement_cpu_ms"] = max(
                        float(decision_metrics["trusted_refinement_cpu_ms"]),
                        float(trusted.get("process_cpu_ms") or 0.0),
                    )
                    decision_metrics["trusted_refinement_elapsed_ms"] = max(
                        float(decision_metrics["trusted_refinement_elapsed_ms"]),
                        float(trusted.get("elapsed_ms") or 0.0),
                    )
                    self._last_decision_source = "incremental_refinement"
                    _log(
                        f"DECIDE refinement decision_id={decision_id} sequence={sequence} "
                        f"decision={baseline} elapsed={elapsed:.3f}s "
                        f"trusted_step={trusted.get('iterator_step')} "
                        f"trusted_cpu_ms={trusted.get('process_cpu_ms')} "
                        f"reported_samples={metadata.get('sample_count')} "
                        f"reported_confidence={metadata.get('confidence')}"
                    )
                continue
            if kind == "done":
                decision_metrics["completed"] = True
                refinement_stats = message.get("refinement_stats") or {}
                decision_metrics["trusted_refinement_steps"] = max(
                    int(decision_metrics["trusted_refinement_steps"]),
                    int(refinement_stats.get("iterator_steps") or 0),
                )
                decision_metrics["trusted_refinement_cpu_ms"] = max(
                    float(decision_metrics["trusted_refinement_cpu_ms"]),
                    float(refinement_stats.get("process_cpu_ms") or 0.0),
                )
                decision_metrics["trusted_refinement_elapsed_ms"] = max(
                    float(decision_metrics["trusted_refinement_elapsed_ms"]),
                    float(refinement_stats.get("elapsed_ms") or 0.0),
                )
                decision_metrics["refinement_iterator_exhausted"] = bool(
                    refinement_stats.get("iterator_exhausted")
                )
                decision_metrics["refinement_termination_reason"] = str(
                    refinement_stats.get("termination_reason") or ""
                )
                _log(
                    f"DECIDE worker_done decision_id={decision_id} sequence={latest_sequence} "
                    f"latest_safe={baseline} elapsed={result_at - started:.3f}s "
                    f"trusted_steps={decision_metrics['trusted_refinement_steps']} "
                    f"trusted_cpu_ms={decision_metrics['trusted_refinement_cpu_ms']} "
                    f"iterator_exhausted={decision_metrics['refinement_iterator_exhausted']} "
                    f"termination={decision_metrics['refinement_termination_reason']}"
                )
                return baseline
            if kind == "decision_error":
                _log(
                    f"DECIDE policy_error decision_id={decision_id} "
                    f"error={message.get('error')!r} latest_safe={baseline}"
                )
                self._last_decision_source = "policy_error_latest_safe"
                return baseline

    def _current_round_has_allin(self) -> bool:
        round_num = self._round_num()
        return any(h.get("round") == round_num and h.get("action_type") == "allin" for h in self._history)

    def _record_action(
        self,
        player_id: int,
        action_type: str,
        amount: int | None,
        committed: int = 0,
        *,
        inferred_boundary: str | None = None,
    ) -> None:
        if action_type not in {"call", "check", "fold", "allin", "raise"}:
            return
        if action_type == "raise" and amount is None:
            return
        entry = {
            "street": self._stage,
            "round": self._round_num(),
            "player_id": player_id,
            "action_type": action_type,
            "committed": int(committed),
        }
        if inferred_boundary is not None:
            entry["inferred"] = True
            entry["inference_reason"] = "official_suppressed_terminal_action"
            entry["inference_boundary"] = inferred_boundary
        if action_type in {"call", "raise", "allin"}:
            if player_id == self._my_id:
                entry["stage_bet"] = self._my_stage_bet
                entry["chips_after"] = self._my_chips
            else:
                entry["stage_bet"] = self._opponent_stage_bet
                entry["chips_after"] = self._opponent_chips
        self._history.append(entry)
        self._opponent_tracker.observe_action(
            "hero" if player_id == self._my_id else "opponent",
            self._stage,
            action_type,
            amount=entry.get("stage_bet", amount),
            committed=committed,
        )

    def _infer_suppressed_terminal_opponent_action(self, boundary: str) -> str | None:
        """Repair the official EXE's omitted peer pass before a proven boundary.

        The local server mirrors the official EXE and may advance directly
        after a terminal call/check without relaying it.  A
        street/settlement/showdown boundary proves the peer response only when
        our last action still required one:
        calls close our raise/allin, postflop calls close our first check, and the
        big blind checks behind our opening small-blind limp.  Recording through
        the normal action path keeps chips, pot, current-hand history, and the
        persistent opponent tracker consistent.  If a server relayed the token,
        the last actor is already the opponent and this method is a no-op.
        """
        if self._hand_num <= 0 or self._in_allin_runout:
            return None
        round_num = self._round_num()
        round_history = [
            record for record in self._history
            if record.get("round") == round_num
        ]
        if not round_history:
            return None
        last = round_history[-1]
        if last.get("player_id") != self._my_id:
            return None

        hero_action = last.get("action_type")
        inferred_action = None
        if hero_action in {"raise", "allin"}:
            inferred_action = "call"
        elif self._stage != "preflop" and hero_action == "check":
            inferred_action = "call"
        elif (
            self._stage == "preflop"
            and self._is_sb
            and hero_action == "call"
            and len(round_history) == 1
        ):
            inferred_action = "check"
        if inferred_action is None:
            return None

        committed = self._apply_opponent_action(inferred_action, None)
        self._record_action(
            self._opponent_id,
            inferred_action,
            None,
            committed,
            inferred_boundary=boundary,
        )
        if inferred_action == "call" and (
            self._current_round_has_allin()
            or self._my_chips == 0
            or self._opponent_chips == 0
        ):
            self._in_allin_runout = True
        _log(
            f"INFER_SUPPRESSED_TERMINAL hand={self._hand_num} stage={self._stage} "
            f"action={inferred_action} committed={committed} boundary={boundary}"
        )
        return inferred_action

    def _last_raise_total(self) -> int | None:
        round_num = self._round_num()
        for record in reversed(self._history):
            if record.get("round") != round_num:
                continue
            if record.get("action_type") != "raise":
                continue
            value = record.get("stage_bet")
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    def _minimum_raise_total(self) -> int:
        last_raise = self._last_raise_total()
        if last_raise is not None:
            # The controlled official oracle proves exact inclusive 2x legal.
            minimum = last_raise * 2
        elif self._stage == "preflop":
            minimum = 2 * BIG_BLIND
        else:
            minimum = BIG_BLIND
        return max(minimum, self._my_stage_bet + 1, self._opponent_stage_bet + 1)

    def _apply_opponent_action(self, action_type: str, amount: int | None) -> int:
        committed = 0
        if action_type == "call":
            committed = min(max(0, self._my_stage_bet - self._opponent_stage_bet), self._opponent_chips)
        elif action_type == "raise" and amount is not None:
            committed = min(max(0, amount - self._opponent_stage_bet), self._opponent_chips)
        elif action_type == "allin":
            committed = self._opponent_chips
        if committed > 0:
            self._opponent_chips -= committed
            self._opponent_stage_bet += committed
            self._pot += committed
        return committed

    def _apply_my_action(self, action_type: str, amount: int | None) -> int:
        committed = 0
        if action_type == "call":
            committed = min(max(0, self._opponent_stage_bet - self._my_stage_bet), self._my_chips)
        elif action_type == "raise" and amount is not None:
            committed = min(max(0, amount - self._my_stage_bet), self._my_chips)
        elif action_type == "allin":
            committed = self._my_chips
        if committed > 0:
            self._my_chips -= committed
            self._my_stage_bet += committed
            self._pot += committed
        return committed

    def _decision_to_tcp(self, raw_decision) -> tuple[str, str, int | None]:
        """Final socket-owner legalization and canonical wire translation."""
        fallback = self._socket_safe_fallback_decision()
        decision = self._legalize_policy_decision(raw_decision, fallback)
        kind = decision["kind"]
        if kind == "fold":
            return "fold", "fold", None
        if kind == "pass":
            wire_kind = self._pass_wire_kind()
            return wire_kind, wire_kind, None
        if kind == "allin":
            return "allin", "allin", None
        target = int(decision["raise_to"])
        return f"raise {target}", "raise", target

    def _should_respond(self, action_type: str) -> bool:
        if action_type == "fold":
            return False
        if action_type in {"raise", "allin"}:
            return True
        if action_type == "call":
            return self._stage == "preflop" and not self._is_sb and self._my_action_count == 0
        if action_type == "check":
            return self._stage != "preflop" and self._my_action_count == 0
        return False

    def _send_wire_action(self, sock: socket.socket, msg: str) -> None:
        if self._official_action_delay_sec > 0 and self._last_platform_message_at > 0:
            elapsed = time.perf_counter() - self._last_platform_message_at
            wait_sec = self._official_action_delay_sec - elapsed
            if wait_sec > 0:
                _log(f"OFFICIAL_ACTION_DELAY wait={wait_sec:.3f}s target={self._official_action_delay_sec:.3f}s")
                time.sleep(wait_sec)
        sock.sendall(msg.encode("utf-8"))

    def _send_decision(self, sock: socket.socket) -> None:
        t0 = time.perf_counter()
        _log(
            f"DECIDE start name={self.name} hand={self._hand_num} stage={self._stage} "
            f"act_cnt={self._my_action_count} my_sb={self._my_stage_bet} "
            f"opp_sb={self._opponent_stage_bet} my_chips={self._my_chips} "
            f"opp_chips={self._opponent_chips} is_sb={self._is_sb}"
        )
        try:
            decision = self._policy_decision()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            _log("DECIDE exception -> fold")
            decision = {"kind": "fold"}
        elapsed = time.perf_counter() - t0
        _log(
            f"DECIDE done decision={decision!r} source={self._last_decision_source} "
            f"elapsed={elapsed:.3f}s"
        )
        msg, action_type, amount = self._decision_to_tcp(decision)
        self._send_wire_action(sock, msg)
        _log(
            f"SEND name={self.name} hand={self._hand_num} stage={self._stage} "
            f"act_cnt={self._my_action_count} my_sb={self._my_stage_bet} "
            f"opp_sb={self._opponent_stage_bet} my_chips={self._my_chips} "
            f"opp_chips={self._opponent_chips} is_sb={self._is_sb} msg={msg!r}"
        )
        committed = self._apply_my_action(action_type, amount)
        self._record_action(self._my_id, action_type, amount, committed)
        if action_type == "call" and (self._current_round_has_allin() or self._my_chips == 0 or self._opponent_chips == 0):
            self._in_allin_runout = True
        self._my_action_count += 1

    def handle(self, line: str, sock: socket.socket) -> None:
        self._last_platform_message_at = time.perf_counter()
        if line.startswith("name"):
            self._name_handshake_count += 1
            handshake_count, generation_before = self._name_handshake_count, self._strategy_worker_generation
            launch_ok = self._ensure_strategy_worker(); generation = self._strategy_worker_generation
            launch_started = launch_ok and generation > generation_before
            _log(f"NAME_HANDSHAKE count={handshake_count} worker_launch_started={launch_started} worker_generation={generation} launch_ok={launch_ok}")
            if handshake_count != 1 or not launch_started: return
            sock.sendall(self.name.encode("utf-8"))
            _log(f"SEND name_handshake name={self.name!r} count={handshake_count} worker_generation={generation}")
            return
        if line.startswith("preflop"):
            # Infer only a boundary-proven omitted closer before clearing state.
            self._infer_suppressed_terminal_opponent_action("hand_start")
            parts = line.split("|", 2)
            blind = parts[1]
            self._is_sb = blind == "SMALLBLIND"
            self._my_cards = _parse_cards(parts[2])
            self._public_cards = []
            self._stage = "preflop"
            self._hand_num += 1
            self._history = []
            self._my_action_count = 0
            self._my_chips = INITIAL_CHIPS
            self._opponent_chips = INITIAL_CHIPS
            self._pot = SMALL_BLIND + BIG_BLIND
            self._in_allin_runout = False
            if self._is_sb:
                self._my_chips -= SMALL_BLIND
                self._my_stage_bet = SMALL_BLIND
                self._opponent_chips -= BIG_BLIND
                self._opponent_stage_bet = BIG_BLIND
            else:
                self._my_chips -= BIG_BLIND
                self._my_stage_bet = BIG_BLIND
                self._opponent_chips -= SMALL_BLIND
                self._opponent_stage_bet = SMALL_BLIND
            self._opponent_tracker.begin_hand(
                self._hand_num,
                opponent_is_sb=not self._is_sb,
            )
            if self._is_sb:
                self._send_decision(sock)
            return
        if line.startswith(("flop", "turn", "river")):
            stage, cards = line.split("|", 1)
            self._infer_suppressed_terminal_opponent_action(f"street:{stage}")
            self._stage = stage
            self._public_cards.extend(_parse_cards(cards))
            self._my_action_count = 0
            self._my_stage_bet = 0
            self._opponent_stage_bet = 0
            self._opponent_tracker.begin_street()
            if not self._in_allin_runout and self._acts_first_postflop():
                self._send_decision(sock)
            return
        if line.startswith("earnChips"):
            earned = int(line.split()[1])
            self._infer_suppressed_terminal_opponent_action("settlement")
            self._earned_by_hand[self._hand_num] = earned
            showdown = self._showdown_by_hand.get(self._hand_num)
            if showdown is not None:
                showdown["earned"] = earned
            self._opponent_tracker.observe_settlement(
                self._hand_num,
                hero_earned=earned,
            )
            _log(
                "OPPONENT_TRACKER "
                + json.dumps(
                    self._opponent_tracker.snapshot(),
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            self._in_allin_runout = False
            return
        if line.startswith("oppo_hands|"):
            self._infer_suppressed_terminal_opponent_action("showdown")
            record = self._showdown_by_hand.get(self._hand_num)
            if record is None:
                record = {
                    "hand": self._hand_num,
                    "opponent_cards": _parse_cards(line.split("|", 1)[1]),
                    "my_cards": list(self._my_cards),
                    "public_cards": list(self._public_cards),
                    "history": list(self._history),
                    "earned": self._earned_by_hand.get(self._hand_num),
                }
                self._showdown_by_hand[self._hand_num] = record
            self._opponent_tracker.observe_showdown(
                self._hand_num,
                record["opponent_cards"],
                record["public_cards"],
            )
            # Official/local ordering is earnChips followed by oppo_hands. The
            # settlement snapshot above therefore precedes showdown learning;
            # emit again so the final hand's revealed range is not lost.
            _log(
                "OPPONENT_TRACKER "
                + json.dumps(
                    self._opponent_tracker.snapshot(),
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return

        action_type, amount = _parse_action(line)
        if action_type == "unknown":
            _log(f"UNKNOWN message={line!r}")
            return
        committed = self._apply_opponent_action(action_type, amount)
        self._record_action(self._opponent_id, action_type, amount, committed)
        if action_type == "call" and (self._current_round_has_allin() or self._my_chips == 0 or self._opponent_chips == 0):
            self._in_allin_runout = True
        if self._should_respond(action_type):
            self._send_decision(sock)


def run_client(host: str, port: int, name: str, log_path: str = "", seat: str = "auto") -> int:
    _log_open(log_path)
    bot = NativeNationalBot(name, seat)
    _log(f"START name={name} seat={bot.seat} host={host} port={port} log={log_path or '-'}")
    try:
        with socket.create_connection((host, port), timeout=30) as sock:
            sock.settimeout(180)
            decoder = NationalStreamDecoder()
            while True:
                try:
                    data = sock.recv(4096)
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as exc:
                    _log(f"RECV closed_by_server exception={type(exc).__name__}: {exc}")
                    return 0
                if not data:
                    # A delimiter-free numeric settlement can be followed
                    # immediately by EOF. EOF is a proven token boundary; do
                    # not discard the final complete token merely because no
                    # idle window elapsed first.
                    for line in decoder.flush_idle():
                        _log(f"DISPATCH eof_flush line={line!r}")
                        bot.handle(line, sock)
                    _log("RECV empty -> server closed")
                    return 0
                chunk = data.decode("ascii")
                messages = decoder.feed(chunk)
                _log(f"RECV raw={chunk!r} buffer={decoder.buffer!r}")
                for line in messages:
                    _log(f"DISPATCH line={line!r}")
                    bot.handle(line, sock)
                if decoder.has_pending_numeric:
                    readable, _, _ = select.select([sock], [], [], _stream_idle_flush_sec())
                    if not readable:
                        messages = decoder.flush_idle()
                        _log(f"RECV idle_flush buffer={decoder.buffer!r}")
                        for line in messages:
                            _log(f"DISPATCH line={line!r}")
                            bot.handle(line, sock)
    finally:
        bot.close()


def main() -> int:
    mp.freeze_support()
    parser = argparse.ArgumentParser(description="Native national TCP bot")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10001)
    parser.add_argument("--name", default="Bot")
    parser.add_argument("--seat", choices=("auto", "upper", "lower"), default="auto",
                        help="Desktop seat hint; action order is still inferred from blind state.")
    parser.add_argument("--log", default="", help="Log file path. Empty disables file logging.")
    args = parser.parse_args()
    try:
        return run_client(args.host, args.port, args.name, args.log, args.seat)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
'''
NATIVE_BOT_TEMPLATE = NATIVE_BOT_TEMPLATE.replace(
    "__POK_DECISION_RUNTIME_VERSION__",
    str(NATIONAL_DECISION_RUNTIME_VERSION),
)
# Keep the system-owned runtime below the same fail-closed file-size ceiling
# enforced for every published candidate.  The raw template uses triple
# newlines for source readability; generated artifacts retain one blank line
# between definitions without carrying the redundant separator line.
NATIVE_BOT_TEMPLATE = NATIVE_BOT_TEMPLATE.replace("\n\n\n", "\n\n")


NATIVE_PRECOMPUTE_TEMPLATE = r'''"""System-owned poker facts and evaluators loaded once per policy worker.

The module is deliberately stdlib-only and performs no file I/O.  Candidate
``policy.py`` may consume these immutable tables and pure helpers, but cannot
replace them.  Card ids use the national protocol rank/suit space:
``card_id = rank_index * 4 + suit`` where rank 0 is deuce and 12 is ace.
"""

from __future__ import annotations

import hashlib
import itertools
import json


PRECOMPUTE_SCHEMA_VERSION = 4
CARD_ENCODING = "national_tcp_card_id_v1:card_id=rank_index*4+suit"
GENERATOR_VERSION = "national-precompute-v3"
PREFLOP_EQUITY_METHOD = "fixed_seed_uniform_opponent_board_mc_v1"
PREFLOP_EQUITY_SAMPLES_PER_CLASS = 65_536
PREFLOP_EQUITY_BASE_SEED = 0x4E4154494F4E414C
PREFLOP_EQUITY_CLASS_SEED_DERIVATION = (
    "base_seed_xor_uint64(class_index*0x9e3779b97f4a7c15)"
)
PREFLOP_EQUITY_DRAW_CONTRACT = (
    "python_random_sample_without_replacement_7:opponent2_then_board5"
)
PREFLOP_EQUITY_BUILD_RUNTIME = "CPython-3.14.4"
PREFLOP_EQUITY_RANDOM_SOURCE_SHA256 = (
    "62dca8cdae7482513b99bb093ff038afd5131954e7eb78166d673a772cee871c"
)
PREFLOP_EQUITY_EVALUATOR_SOURCE = "sever/engine/evaluator.py"
PREFLOP_EQUITY_EVALUATOR_SHA256 = (
    "9992ee2608db9aef0320a586117f9ced8bdf33ad79581b9356686210cabd425f"
)
PREFLOP_EQUITY_CARD_SOURCE = "sever/engine/deck.py"
PREFLOP_EQUITY_CARD_SOURCE_SHA256 = (
    "8afb902bc936bca5659997e9b36a923d69304946f5659b35c054cd8c702851d5"
)
PREFLOP_EQUITY_GENERATOR_SHA256 = (
    "5aa6808974f9af67ac7bb5189c431791d9aed9e791869f9428b1ab8e04cf62d3"
)
FULL_DECK = tuple(range(52))
FIVE_OF_SEVEN_INDICES = tuple(itertools.combinations(range(7), 5))
RANK_SYMBOLS = "23456789TJQKA"


def card_id(suit: int, rank_index: int) -> int:
    suit, rank_index = int(suit), int(rank_index)
    if not 0 <= suit < 4 or not 0 <= rank_index < 13:
        raise ValueError("national card outside suit=0..3/rank=0..12")
    return rank_index * 4 + suit


def card_parts(card: int) -> tuple[int, int]:
    card = int(card)
    if not 0 <= card < 52:
        raise ValueError("card id outside 0..51")
    return card % 4, card // 4


def _hole_fact(card_a: int, card_b: int) -> tuple[int, int, bool, bool, int]:
    rank_a, rank_b = card_a // 4 + 2, card_b // 4 + 2
    high, low = max(rank_a, rank_b), min(rank_a, rank_b)
    return high, low, card_a % 4 == card_b % 4, high == low, high - low


def _hole_class_index(card_a: int, card_b: int) -> int:
    rank_a, rank_b = card_a // 4, card_b // 4
    high, low = max(rank_a, rank_b), min(rank_a, rank_b)
    if high == low:
        row, column = high, high
    elif card_a % 4 == card_b % 4:
        row, column = high, low
    else:
        row, column = low, high
    return row * 13 + column


def _preflop_bucket(card_a: int, card_b: int) -> str:
    rank_a, rank_b = card_a // 4 + 2, card_b // 4 + 2
    high, low = max(rank_a, rank_b), min(rank_a, rank_b)
    suited = card_a % 4 == card_b % 4
    if high == low:
        return "premium_pair" if high >= 10 else "small_pair"
    if high == 14 and low >= 10:
        return "ace_broadway"
    if low >= 10:
        return "broadway"
    if suited and high - low <= 2:
        return "suited_connector"
    if suited and high == 14:
        return "suited_ace"
    if not suited and high == 14:
        return "offsuit_ace"
    if suited:
        return "suited_other"
    return "offsuit_other"


def _straight_high(rank_mask: int) -> int:
    mask = int(rank_mask) & 0x1FFF
    for high_index in range(12, 3, -1):
        window = 0b11111 << (high_index - 4)
        if mask & window == window:
            return high_index + 2
    wheel = (1 << 12) | 0b1111
    return 5 if mask & wheel == wheel else 0


HOLE_COMBO_FACTS = {
    (card_a, card_b): _hole_fact(card_a, card_b)
    for card_a in range(52)
    for card_b in range(card_a + 1, 52)
}
HOLE_CLASS_INDEX_BY_COMBO = {
    key: _hole_class_index(*key) for key in HOLE_COMBO_FACTS
}
HOLE_BUCKET_BY_COMBO = {
    key: _preflop_bucket(*key) for key in HOLE_COMBO_FACTS
}
STRAIGHT_HIGH_BY_MASK = {
    rank_mask: _straight_high(rank_mask)
    for rank_mask in range(1 << 13)
}
# Compact system-owned facts generated offline with the evaluator below.  For
# each canonical 169 class, a fixed per-class seed samples 65,536 uniformly
# random opponent-hole/board completions.  Runtime performs no simulation or
# file I/O; the literal is content-bound by PRECOMPUTE_MANIFEST.  Rows/columns
# retain `_hole_class_index`: diagonal=pair, lower triangle=suited, upper
# triangle=offsuit.
PREFLOP_CLASS_EQUITY = (
    0.505562, 0.322670, 0.331421, 0.345146, 0.342972, 0.341774, 0.368095, 0.387222, 0.416359, 0.442924, 0.474236, 0.501106, 0.551247,
    0.357185, 0.538025, 0.349358, 0.362289, 0.359276, 0.367126, 0.373817, 0.400490, 0.424042, 0.453644, 0.481262, 0.517700, 0.557213,
    0.372108, 0.386574, 0.569916, 0.382294, 0.378036, 0.381859, 0.395424, 0.407585, 0.436333, 0.460594, 0.491196, 0.519691, 0.565475,
    0.381386, 0.397804, 0.415733, 0.604012, 0.400528, 0.405571, 0.414703, 0.423820, 0.441399, 0.472992, 0.503845, 0.533699, 0.579208,
    0.379669, 0.395454, 0.412865, 0.428802, 0.632370, 0.423744, 0.432610, 0.443848, 0.462898, 0.480812, 0.508186, 0.545547, 0.576149,
    0.385216, 0.398239, 0.418434, 0.435295, 0.454697, 0.663002, 0.451591, 0.460457, 0.479599, 0.494904, 0.518990, 0.549400, 0.584755,
    0.406075, 0.405975, 0.426208, 0.443024, 0.466362, 0.483116, 0.690964, 0.483467, 0.498352, 0.517311, 0.535866, 0.561302, 0.599449,
    0.420876, 0.432449, 0.436714, 0.457077, 0.476410, 0.493073, 0.507797, 0.721916, 0.515305, 0.533386, 0.554527, 0.579147, 0.604622,
    0.449333, 0.457863, 0.466248, 0.473595, 0.489662, 0.509956, 0.522110, 0.540840, 0.749001, 0.552902, 0.572510, 0.598198, 0.627930,
    0.476631, 0.482178, 0.490730, 0.500206, 0.505775, 0.519569, 0.537994, 0.554581, 0.574120, 0.775276, 0.581406, 0.603699, 0.635590,
    0.502884, 0.512283, 0.521706, 0.530334, 0.535919, 0.542717, 0.563805, 0.575523, 0.594666, 0.602203, 0.797539, 0.612953, 0.642410,
    0.532730, 0.541290, 0.545059, 0.558815, 0.565666, 0.573570, 0.579514, 0.599289, 0.617752, 0.626503, 0.634270, 0.821808, 0.652756,
    0.572975, 0.580811, 0.588860, 0.601120, 0.597969, 0.609055, 0.619118, 0.624474, 0.645691, 0.655029, 0.662453, 0.668808, 0.853325,
)


def _validated_cards(cards, expected=None) -> tuple[int, ...]:
    result = tuple(int(card) for card in cards)
    if expected is not None and len(result) != int(expected):
        raise ValueError(f"expected {expected} cards, got {len(result)}")
    if len(result) < 5 or len(result) > 7:
        raise ValueError("hand evaluator requires five through seven cards")
    if any(card < 0 or card >= 52 for card in result):
        raise ValueError("card id outside 0..51")
    if len(set(result)) != len(result):
        raise ValueError("duplicate card in hand")
    return result


def _evaluate_five_unchecked(cards) -> tuple:
    ranks = sorted((card // 4 for card in cards), reverse=True)
    suits = [card % 4 for card in cards]
    counts = {}
    rank_mask = 0
    for rank in ranks:
        counts[rank] = counts.get(rank, 0) + 1
        rank_mask |= 1 << rank
    groups = sorted(counts.items(), key=lambda item: (item[1], item[0]), reverse=True)
    pattern = tuple(item[1] for item in groups)
    kickers = tuple(item[0] for item in groups)
    flush = len(set(suits)) == 1
    straight = STRAIGHT_HIGH_BY_MASK[rank_mask]
    # ``straight_high`` stores natural rank values 5..14; sever's evaluator
    # uses protocol rank indices 3..12, hence the subtraction here.
    straight_high = straight - 2 if straight else 0
    if flush and straight:
        return (9, straight_high)
    if pattern == (4, 1):
        return (8, kickers)
    if pattern == (3, 2):
        return (7, kickers)
    if flush:
        return (6, tuple(ranks))
    if straight:
        return (5, straight_high)
    if pattern == (3, 1, 1):
        return (4, kickers)
    if pattern == (2, 2, 1):
        return (3, kickers)
    if pattern == (2, 1, 1, 1):
        return (2, kickers)
    return (1, tuple(ranks))


def evaluate_five(cards) -> tuple:
    """Return the complete, directly comparable five-card rank tuple."""

    return _evaluate_five_unchecked(_validated_cards(cards, 5))


def best_hand_rank(cards) -> tuple:
    """Return the best five-card rank from a valid five-, six-, or seven-card set."""

    cards = _validated_cards(cards)
    if len(cards) == 5:
        return _evaluate_five_unchecked(cards)
    indices = (
        FIVE_OF_SEVEN_INDICES
        if len(cards) == 7
        else itertools.combinations(range(len(cards)), 5)
    )
    return max(
        _evaluate_five_unchecked(tuple(cards[index] for index in selected))
        for selected in indices
    )


def evaluate_seven(cards) -> tuple:
    return best_hand_rank(_validated_cards(cards, 7))


def compare_hands(left, right) -> int:
    left_rank, right_rank = best_hand_rank(left), best_hand_rank(right)
    return (left_rank > right_rank) - (left_rank < right_rank)


def deck_without(excluded=()) -> tuple[int, ...]:
    excluded = tuple(int(card) for card in excluded)
    if any(card < 0 or card >= 52 for card in excluded):
        raise ValueError("excluded card id outside 0..51")
    if len(set(excluded)) != len(excluded):
        raise ValueError("duplicate excluded card")
    blocked = set(excluded)
    return tuple(card for card in FULL_DECK if card not in blocked)


def deterministic_draw(deck, count: int, state: int) -> tuple[tuple[int, ...], int]:
    """Draw without replacement using a stable xorshift64 stream."""

    pool = list(deck)
    count = int(count)
    if count < 0 or count > len(pool):
        raise ValueError("draw count outside deck")
    state = int(state) & 0xFFFFFFFFFFFFFFFF or 0x9E3779B97F4A7C15
    for offset in range(count):
        state ^= (state << 13) & 0xFFFFFFFFFFFFFFFF
        state ^= state >> 7
        state ^= (state << 17) & 0xFFFFFFFFFFFFFFFF
        state &= 0xFFFFFFFFFFFFFFFF
        selected = offset + state % (len(pool) - offset)
        pool[offset], pool[selected] = pool[selected], pool[offset]
    return tuple(pool[:count]), state


def _content_digest() -> str:
    payload = {
        "five_of_seven": FIVE_OF_SEVEN_INDICES,
        "hole_combo_facts": sorted((list(key), list(value)) for key, value in HOLE_COMBO_FACTS.items()),
        "hole_class_indices": sorted((list(key), value) for key, value in HOLE_CLASS_INDEX_BY_COMBO.items()),
        "hole_buckets": sorted((list(key), value) for key, value in HOLE_BUCKET_BY_COMBO.items()),
        "preflop_class_equity": PREFLOP_CLASS_EQUITY,
        "straight_high": [STRAIGHT_HIGH_BY_MASK[index] for index in range(1 << 13)],
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("ascii")
    ).hexdigest()


PRECOMPUTE_MANIFEST = {
    "schema_version": PRECOMPUTE_SCHEMA_VERSION,
    "generator_version": GENERATOR_VERSION,
    "card_encoding": CARD_ENCODING,
    "hole_combo_entries": len(HOLE_COMBO_FACTS),
    "hole_class_entries": len(PREFLOP_CLASS_EQUITY),
    "hole_bucket_entries": len(HOLE_BUCKET_BY_COMBO),
    "preflop_equity_method": PREFLOP_EQUITY_METHOD,
    "preflop_equity_samples_per_class": PREFLOP_EQUITY_SAMPLES_PER_CLASS,
    "preflop_equity_base_seed": PREFLOP_EQUITY_BASE_SEED,
    "preflop_equity_class_seed_derivation": (
        PREFLOP_EQUITY_CLASS_SEED_DERIVATION
    ),
    "preflop_equity_draw_contract": PREFLOP_EQUITY_DRAW_CONTRACT,
    "preflop_equity_build_runtime": PREFLOP_EQUITY_BUILD_RUNTIME,
    "preflop_equity_random_source_sha256": (
        PREFLOP_EQUITY_RANDOM_SOURCE_SHA256
    ),
    "preflop_equity_evaluator_source": PREFLOP_EQUITY_EVALUATOR_SOURCE,
    "preflop_equity_evaluator_sha256": PREFLOP_EQUITY_EVALUATOR_SHA256,
    "preflop_equity_card_source": PREFLOP_EQUITY_CARD_SOURCE,
    "preflop_equity_card_source_sha256": PREFLOP_EQUITY_CARD_SOURCE_SHA256,
    "preflop_equity_generator_sha256": PREFLOP_EQUITY_GENERATOR_SHA256,
    "straight_mask_entries": len(STRAIGHT_HIGH_BY_MASK),
    "five_of_seven_entries": len(FIVE_OF_SEVEN_INDICES),
    # These are system-provided domain facts.  They may accelerate a live
    # decision, but a plan may not claim them alone as a state-learning
    # innovation; the runtime probe must still prove value-sensitive wire
    # influence for any selected precompute primary.
    "foundation_pure_facts": [
        "HOLE_COMBO_FACTS",
        "HOLE_CLASS_INDEX_BY_COMBO",
        "HOLE_BUCKET_BY_COMBO",
        "PREFLOP_CLASS_EQUITY",
        "STRAIGHT_HIGH_BY_MASK",
        "FIVE_OF_SEVEN_INDICES",
    ],
    "content_digest": _content_digest(),
}


def hole_combo_fact(card_a: int, card_b: int):
    key = (card_a, card_b) if card_a < card_b else (card_b, card_a)
    return HOLE_COMBO_FACTS.get(key)


def hole_class_index(card_a: int, card_b: int) -> int:
    key = (card_a, card_b) if card_a < card_b else (card_b, card_a)
    if key not in HOLE_CLASS_INDEX_BY_COMBO:
        raise ValueError("hole cards must be two distinct card ids")
    return HOLE_CLASS_INDEX_BY_COMBO[key]


def preflop_equity(card_a: int, card_b: int) -> float:
    return PREFLOP_CLASS_EQUITY[hole_class_index(card_a, card_b)]


def preflop_bucket(card_a: int, card_b: int) -> str:
    key = (card_a, card_b) if card_a < card_b else (card_b, card_a)
    if key not in HOLE_BUCKET_BY_COMBO:
        raise ValueError("hole cards must be two distinct card ids")
    return HOLE_BUCKET_BY_COMBO[key]


def straight_high(rank_mask: int) -> int:
    return STRAIGHT_HIGH_BY_MASK.get(int(rank_mask) & 0x1FFF, 0)
'''


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


_NATIVE_STREAM_PROBE_SCRIPT = r'''
import contextlib
import io
import json
import runpy
import sys

entry = sys.argv[1]
captured = io.StringIO()
errors = []
try:
    with contextlib.redirect_stdout(captured):
        namespace = runpy.run_path(entry, run_name="_national_stream_contract_probe")
except BaseException as exc:
    errors.append(f"entry_load:{type(exc).__name__}:{exc}")
    namespace = {}
if captured.getvalue():
    errors.append(f"stdout_pollution:{captured.getvalue()[:160]!r}")

decoder_class = namespace.get("NationalStreamDecoder")
cases = (
    ("raise 200", ["raise 200"], ""),
    ("earnChips -100", ["earnChips -100"], ""),
    ("raise 200call", ["raise 200", "call"], ""),
    ("raise 200earnChips -100", ["raise 200", "earnChips -100"], ""),
    (
        "earnChips -100preflop|SMALLBLIND|<0,3><1,3>",
        ["earnChips -100", "preflop|SMALLBLIND|<0,3><1,3>"],
        "",
    ),
    ("allinriver|<3,12>", ["allin", "river|<3,12>"], ""),
    ("earnChips\t-100", [], "earnChips\t-100"),
    ("earnChips  -100", [], "earnChips  -100"),
    ("earnChips\n-100", [], "earnChips\n-100"),
    ("raise  200", [], "raise  200"),
    ("raise ", [], "raise "),
    ("preflop|SMALLBLIND|<0,3>", [], "preflop|SMALLBLIND|<0,3>"),
    ("call\n", ["call"], "\n"),
)

def decode(chunks):
    decoder = decoder_class()
    emitted = []
    for chunk in chunks:
        emitted.extend(decoder.feed(chunk))
    emitted.extend(decoder.flush_idle())
    return emitted, decoder.buffer

if decoder_class is None:
    errors.append("missing_decoder_class")
else:
    for raw, expected, expected_remainder in cases:
        chunkings = [(raw,)]
        chunkings.extend((raw[:split], raw[split:]) for split in range(1, len(raw)))
        chunkings.append(tuple(raw))
        for chunks in chunkings:
            try:
                actual, remainder = decode(chunks)
            except BaseException as exc:
                errors.append(
                    f"decode_exception:{raw!r}:{type(exc).__name__}:{exc}"
                )
                break
            if actual != expected or remainder != expected_remainder:
                errors.append(
                    f"decode_mismatch:{raw!r}:chunks={chunks!r}:"
                    f"actual={actual!r}:remainder={remainder!r}:"
                    f"expected={expected!r}:expected_remainder={expected_remainder!r}"
                )
                break

print(json.dumps({"errors": errors[:20]}, ensure_ascii=True))
'''


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
