"""Exact small-game safety certificate for a Kuhn public-history resolve.

The resolver changes only player 0's continuation after ``check, bet``.  It
preserves every strategy outside that public subtree and certifies that player
1's counterfactual best-response value at each top information set does not
increase.  Because the game is small, the certificate also recomputes global
exploitability.  This is a clean-room, exact-terminal correctness scaffold;
it is not a scalable HUNL resolving gadget.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Iterable

from ..blueprint.evaluation import (
    BehaviorPolicy,
    action_probabilities,
    best_response,
    expected_returns,
    exploitability,
)
from ..blueprint.small_games import BET, CALL, CHECK, FOLD, KuhnPoker
from ..core.game import Action, CHANCE_PLAYER, TERMINAL_PLAYER


def _key(player: int, rank: int, history: tuple[str, ...]) -> str:
    encoded = ",".join(history) or "root"
    return f"kuhn:p{player}:r{rank}:h={encoded}"


KUHN_CHECK_RESPONSE_KEYS = tuple(
    _key(0, rank, (CHECK, BET)) for rank in range(3)
)
KUHN_CHECK_TOP_KEYS = tuple(_key(1, rank, (CHECK,)) for rank in range(3))


def _information_state_actions() -> dict[str, tuple[Action, ...]]:
    game = KuhnPoker()
    result: dict[str, tuple[Action, ...]] = {}

    def visit(state) -> None:
        actor = state.current_player
        if actor == TERMINAL_PLAYER:
            return
        if actor == CHANCE_PLAYER:
            for action, _ in state.chance_outcomes():
                visit(state.child(action))
            return
        key = state.information_state_key(actor)
        actions = state.legal_actions()
        previous = result.get(key)
        if previous is not None and previous != actions:
            raise ValueError(f"inconsistent Kuhn action set for {key}")
        result[key] = actions
        for action in actions:
            visit(state.child(action))

    visit(game.new_initial_state())
    return result


def _canonical_policy(policy: BehaviorPolicy) -> dict[str, dict[Action, float]]:
    action_sets = _information_state_actions()
    unknown_keys = set(policy) - set(action_sets)
    if unknown_keys:
        raise ValueError(f"policy contains unknown information states: {sorted(unknown_keys)!r}")

    result: dict[str, dict[Action, float]] = {}
    for key, legal in sorted(action_sets.items()):
        raw = policy.get(key, {})
        unknown_actions = set(raw) - set(legal)
        if unknown_actions:
            raise ValueError(f"policy contains illegal actions at {key}: {unknown_actions!r}")
        if any(
            not math.isfinite(float(value)) or float(value) < 0.0
            for value in raw.values()
        ):
            raise ValueError(f"policy contains invalid probabilities at {key}")
        result[key] = action_probabilities(policy, key, legal)
    return result


def kuhn_check_replacement_policy(
    blueprint: BehaviorPolicy,
    call_probabilities: Iterable[float],
) -> dict[str, dict[Action, float]]:
    """Return a complete policy with only the three check-bet responses changed."""

    probabilities = tuple(float(value) for value in call_probabilities)
    if len(probabilities) != 3:
        raise ValueError("exactly three rank-conditioned call probabilities are required")
    if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in probabilities):
        raise ValueError("call probabilities must be finite and in [0, 1]")
    candidate = _canonical_policy(blueprint)
    for key, probability in zip(KUHN_CHECK_RESPONSE_KEYS, probabilities, strict=True):
        candidate[key] = {CALL: probability, FOLD: 1.0 - probability}
    return candidate


@dataclass(frozen=True, slots=True)
class CounterfactualSafetyMargin:
    information_state: str
    baseline_value: float
    candidate_value: float
    margin: float


@dataclass(frozen=True, slots=True)
class SafeResolveCertificate:
    accepted: bool
    margins: tuple[CounterfactualSafetyMargin, ...]
    baseline_exploitability: float
    candidate_exploitability: float
    baseline_best_response_values: tuple[float, float]
    candidate_best_response_values: tuple[float, float]

    @property
    def minimum_margin(self) -> float:
        return min(item.margin for item in self.margins)


def certify_kuhn_check_replacement(
    blueprint: BehaviorPolicy,
    candidate: BehaviorPolicy,
    *,
    tolerance: float = 1e-12,
) -> SafeResolveCertificate:
    """Certify a continuation replacement against exact top-infoset CBVs."""

    if not math.isfinite(tolerance) or tolerance < 0.0:
        raise ValueError("tolerance must be finite and nonnegative")
    baseline_policy = _canonical_policy(blueprint)
    candidate_policy = _canonical_policy(candidate)

    for key, baseline_distribution in baseline_policy.items():
        if key in KUHN_CHECK_RESPONSE_KEYS:
            continue
        candidate_distribution = candidate_policy[key]
        if any(
            abs(candidate_distribution[action] - probability) > tolerance
            for action, probability in baseline_distribution.items()
        ):
            raise ValueError(f"candidate changed policy outside the Kuhn check subtree: {key}")

    game = KuhnPoker()
    baseline_responses = (
        best_response(game, baseline_policy, 0),
        best_response(game, baseline_policy, 1),
    )
    candidate_responses = (
        best_response(game, candidate_policy, 0),
        best_response(game, candidate_policy, 1),
    )
    margins = tuple(
        CounterfactualSafetyMargin(
            information_state=key,
            baseline_value=baseline_responses[1].counterfactual_values[key],
            candidate_value=candidate_responses[1].counterfactual_values[key],
            margin=(
                baseline_responses[1].counterfactual_values[key]
                - candidate_responses[1].counterfactual_values[key]
            ),
        )
        for key in KUHN_CHECK_TOP_KEYS
    )
    baseline_exploitability = exploitability(game, baseline_policy).exploitability
    candidate_exploitability = exploitability(game, candidate_policy).exploitability
    player_zero_unchanged = (
        abs(candidate_responses[0].value - baseline_responses[0].value) <= tolerance
    )
    accepted = (
        player_zero_unchanged
        and all(item.margin >= -tolerance for item in margins)
        and candidate_exploitability <= baseline_exploitability + tolerance
    )
    return SafeResolveCertificate(
        accepted=accepted,
        margins=margins,
        baseline_exploitability=baseline_exploitability,
        candidate_exploitability=candidate_exploitability,
        baseline_best_response_values=(
            baseline_responses[0].value,
            baseline_responses[1].value,
        ),
        candidate_best_response_values=(
            candidate_responses[0].value,
            candidate_responses[1].value,
        ),
    )


@dataclass(frozen=True, slots=True)
class KuhnResolveResult:
    mode: str
    call_probabilities: tuple[float, float, float]
    policy: dict[str, dict[Action, float]]
    certificate: SafeResolveCertificate
    resolver_value: float
    candidates_considered: int
    safe_candidates: int


def resolve_kuhn_check_subgame(
    blueprint: BehaviorPolicy,
    *,
    probability_grid: Iterable[float] = (0.0, 0.25, 0.5, 0.75, 1.0),
    mode: str = "safe",
    tolerance: float = 1e-12,
) -> KuhnResolveResult:
    """Enumerate a deterministic exact-terminal plain or safe replacement.

    ``plain`` maximizes player 0's value against the fixed blueprint profile.
    ``safe`` applies the same objective but admits only candidates with an
    exact top-information-set safety certificate.
    """

    if mode not in {"plain", "safe"}:
        raise ValueError("mode must be 'plain' or 'safe'")
    grid = tuple(sorted(set(float(value) for value in probability_grid)))
    if not grid or any(
        not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in grid
    ):
        raise ValueError("probability_grid must contain finite values in [0, 1]")

    baseline_policy = _canonical_policy(blueprint)
    baseline_calls = tuple(
        baseline_policy[key][CALL] for key in KUHN_CHECK_RESPONSE_KEYS
    )
    candidate_points = set(itertools.product(grid, repeat=3))
    candidate_points.add(baseline_calls)

    best: tuple[
        tuple[float, float, float],
        dict[str, dict[Action, float]],
        SafeResolveCertificate,
        float,
    ] | None = None
    safe_candidates = 0
    for calls in sorted(candidate_points):
        candidate = kuhn_check_replacement_policy(baseline_policy, calls)
        certificate = certify_kuhn_check_replacement(
            baseline_policy,
            candidate,
            tolerance=tolerance,
        )
        if certificate.accepted:
            safe_candidates += 1
        if mode == "safe" and not certificate.accepted:
            continue
        resolver_value = expected_returns(KuhnPoker(), candidate)[0]
        if best is None:
            best = (calls, candidate, certificate, resolver_value)
            continue
        if resolver_value > best[3] + 1e-15 or (
            abs(resolver_value - best[3]) <= 1e-15 and calls < best[0]
        ):
            best = (calls, candidate, certificate, resolver_value)

    if best is None:
        raise RuntimeError("safe resolver found no certified candidate, including blueprint")
    calls, policy, certificate, resolver_value = best
    return KuhnResolveResult(
        mode=mode,
        call_probabilities=calls,
        policy=policy,
        certificate=certificate,
        resolver_value=resolver_value,
        candidates_considered=len(candidate_points),
        safe_candidates=safe_candidates,
    )
