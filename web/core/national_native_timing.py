"""National native match-timing plan subsystem.

Extracted from national_native.py for maintainability. Contains the
match-timing plan dataclasses, builders, validators, and progress
projection (Group A of national_native). This is a self-contained leaf
subsystem: it imports only shared dependencies (managed_bot_executor,
runtime_capacity, sever.engine.game, national_game_runtime) and does not
call back into national_native.

All public symbols are re-exported by national_native.py for backward
compatibility (every existing ``from national_native import
build_native_match_timing_plan`` site, top-level and deferred, keeps
resolving to the same objects). No test monkeypatches these symbols, so a
plain re-export suffices (no proxy forwarding needed).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any

from managed_bot_executor import BotTiming
from national_game_runtime import NATIONAL_20000_CHIP_MAX_ACTION_REQUESTS_PER_HAND
from runtime_capacity import DEFAULT_CAPACITY_WAIT_SECONDS
from sever.engine.game import MAX_ACTIONS_PER_BETTING_ROUND

# Native-runtime envelope constants consumed by the timing plan. The full set
# (LOCAL_*, NATIVE_MATCH_TIMING_*, NATIVE_LAUNCH_HEARTBEAT_*, etc.) is defined
# verbatim below in the migrated timing body. ``NATIVE_ARTIFACT_PREPARATION_PER_BOT_TIMEOUT_SEC``
# is the one timing-read constant that a test monkeypatches on ``national_native``
# (test_native_artifact_preparation_timeout_is_enforced); the reader below
# resolves it live through ``national_native`` so that patch stays effective.


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


def _artifact_preparation_timeout_sec() -> float:
    """Resolve the per-bot artifact-preparation timeout live through national_native.

    ``NATIVE_ARTIFACT_PREPARATION_PER_BOT_TIMEOUT_SEC`` is the only timing-read
    constant a test monkeypatches on ``national_native`` (to force a fast
    timeout). Reading it live through the re-exporting module keeps that patch
    effective without forcing the rest of the timing subsystem (which is not
    monkeypatched) onto the same indirection.
    """
    try:
        import sys as _sys

        nn = _sys.modules.get("national_native")
        if nn is not None:
            return float(getattr(nn, "NATIVE_ARTIFACT_PREPARATION_PER_BOT_TIMEOUT_SEC"))
    except Exception:
        pass
    return float(NATIVE_ARTIFACT_PREPARATION_PER_BOT_TIMEOUT_SEC)


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
        round(_artifact_preparation_timeout_sec() * 1_000_000)
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
