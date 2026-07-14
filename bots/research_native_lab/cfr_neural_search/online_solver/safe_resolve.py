"""Exact oracle-certified replacement for a Kuhn public-history resolve.

The resolver changes only player 0's continuation after ``check, bet``.  It
preserves every strategy outside that public subtree and checks that player
1's counterfactual best-response value at each top information set does not
increase.  Independently, a complete-game exploitability oracle is used as a
falsifier and is part of acceptance.  This is a clean-room, exact-terminal
functional adaptation; it neither proves that the local constraints alone are
sufficient in another game nor implements a scalable HUNL resolving gadget.
"""

from __future__ import annotations

import hashlib
import itertools
import json
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
        if any(type(value) not in (int, float) for value in raw.values()):
            raise TypeError(
                f"policy probabilities at {key} must be numeric, not bool/string"
            )
        if any(
            not math.isfinite(float(value)) or float(value) < 0.0
            for value in raw.values()
        ):
            raise ValueError(f"policy contains invalid probabilities at {key}")
        # The safe-resolve convenience API explicitly expands omitted rows to
        # uniform before handing a complete profile to the strict evaluator.
        source: BehaviorPolicy = {key: raw} if key in policy else {}
        result[key] = action_probabilities(source, key, legal)
    return result


def _policy_sha256(policy: BehaviorPolicy) -> str:
    """Hash a complete normalized Kuhn policy for certificate attachment."""

    canonical = _canonical_policy(policy)
    payload: dict[str, dict[str, float]] = {}
    for key, vector in sorted(canonical.items()):
        if not all(isinstance(action, str) for action in vector):
            raise TypeError("Kuhn safety certificate requires string actions")
        payload[key] = {
            str(action): vector[action]
            for action in sorted(vector, key=str)
        }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def kuhn_check_replacement_policy(
    blueprint: BehaviorPolicy,
    call_probabilities: Iterable[float],
) -> dict[str, dict[Action, float]]:
    """Return a complete policy with only the three check-bet responses changed."""

    raw_probabilities = tuple(call_probabilities)
    if any(type(value) not in (int, float) for value in raw_probabilities):
        raise TypeError("call probabilities must be numeric, not bool/string")
    probabilities = tuple(float(value) for value in raw_probabilities)
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
class KuhnSafetyConstraint:
    """Exact opponent opt-out bounds for the replaced public subtree.

    A scalable safe resolver normally realizes these bounds with a resolving
    gadget.  This M3 small-game implementation enforces the equivalent three
    top-infoset inequalities by exhaustive filtering.  Naming the constraint
    explicitly prevents the exact Kuhn filter from being mistaken for a HUNL
    gadget implementation.
    """

    blueprint_policy_sha256: str
    opponent_player: int
    information_states: tuple[str, ...]
    maximum_counterfactual_values: tuple[float, ...]

    def __post_init__(self) -> None:
        if (
            type(self.blueprint_policy_sha256) is not str
            or len(self.blueprint_policy_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.blueprint_policy_sha256
            )
        ):
            raise ValueError("blueprint policy binding must be lowercase SHA-256")
        if type(self.opponent_player) is not int or self.opponent_player != 1:
            raise ValueError("Kuhn check-response constraint protects player 1")
        if type(self.information_states) is not tuple or any(
            type(key) is not str for key in self.information_states
        ):
            raise TypeError("safety constraint information states must be strings")
        if type(self.maximum_counterfactual_values) is not tuple:
            raise TypeError("safety constraint values must be a tuple")
        if len(self.information_states) != len(self.maximum_counterfactual_values):
            raise ValueError("safety constraint keys and values must align")
        if not self.information_states or len(set(self.information_states)) != len(
            self.information_states
        ):
            raise ValueError("safety constraint information states must be unique")
        if self.information_states != KUHN_CHECK_TOP_KEYS:
            raise ValueError("safety constraint must bind the three Kuhn top infosets")
        if any(
            type(value) not in (int, float)
            for value in self.maximum_counterfactual_values
        ):
            raise TypeError("safety constraint values must be numeric, not bool/string")
        if any(not math.isfinite(value) for value in self.maximum_counterfactual_values):
            raise ValueError("safety constraint values must be finite")

    @property
    def sha256(self) -> str:
        encoded = json.dumps(
            {
                "blueprint_policy_sha256": self.blueprint_policy_sha256,
                "opponent_player": self.opponent_player,
                "information_states": list(self.information_states),
                "maximum_counterfactual_values": list(
                    self.maximum_counterfactual_values
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def build_kuhn_check_safety_constraint(
    blueprint: BehaviorPolicy,
) -> KuhnSafetyConstraint:
    """Freeze the baseline opponent CBV bounds used by the exact filter."""

    baseline_policy = _canonical_policy(blueprint)
    response = best_response(KuhnPoker(), baseline_policy, 1)
    return KuhnSafetyConstraint(
        blueprint_policy_sha256=_policy_sha256(baseline_policy),
        opponent_player=1,
        information_states=KUHN_CHECK_TOP_KEYS,
        maximum_counterfactual_values=tuple(
            response.counterfactual_values[key] for key in KUHN_CHECK_TOP_KEYS
        ),
    )


@dataclass(frozen=True, slots=True)
class OracleCertifiedKuhnResolveCertificate:
    """Toy certificate requiring both local constraints and a global oracle."""

    accepted: bool
    local_cbv_constraints_satisfied: bool
    global_exploitability_oracle_satisfied: bool
    resolver_best_response_invariant: bool
    constraint: KuhnSafetyConstraint
    candidate_policy_sha256: str
    tolerance: float
    margins: tuple[CounterfactualSafetyMargin, ...]
    baseline_exploitability: float
    candidate_exploitability: float
    baseline_best_response_values: tuple[float, float]
    candidate_best_response_values: tuple[float, float]

    @property
    def minimum_margin(self) -> float:
        return min(item.margin for item in self.margins)


# Backward-compatible spelling for the already published M3 API.  The actual
# class name deliberately exposes that this is an oracle-certified Kuhn toy,
# not a transferable safe-solving proof.
SafeResolveCertificate = OracleCertifiedKuhnResolveCertificate


def certify_kuhn_check_replacement(
    blueprint: BehaviorPolicy,
    candidate: BehaviorPolicy,
    *,
    tolerance: float = 1e-12,
) -> SafeResolveCertificate:
    """Oracle-certify a continuation against exact top-infoset CBVs."""

    if type(tolerance) not in (int, float):
        raise TypeError("tolerance must be numeric, not bool/string")
    if not math.isfinite(tolerance) or tolerance < 0.0:
        raise ValueError("tolerance must be finite and nonnegative")
    baseline_policy = _canonical_policy(blueprint)
    candidate_policy = _canonical_policy(candidate)
    constraint = build_kuhn_check_safety_constraint(baseline_policy)

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
            baseline_value=maximum,
            candidate_value=candidate_responses[1].counterfactual_values[key],
            margin=(
                maximum - candidate_responses[1].counterfactual_values[key]
            ),
        )
        for key, maximum in zip(
            constraint.information_states,
            constraint.maximum_counterfactual_values,
            strict=True,
        )
    )
    baseline_exploitability = exploitability(game, baseline_policy).exploitability
    candidate_exploitability = exploitability(game, candidate_policy).exploitability
    player_zero_unchanged = (
        abs(candidate_responses[0].value - baseline_responses[0].value) <= tolerance
    )
    local_cbv_constraints_satisfied = all(
        item.margin >= -tolerance for item in margins
    )
    global_exploitability_oracle_satisfied = (
        candidate_exploitability <= baseline_exploitability + tolerance
    )
    accepted = (
        player_zero_unchanged
        and local_cbv_constraints_satisfied
        and global_exploitability_oracle_satisfied
    )
    return SafeResolveCertificate(
        accepted=accepted,
        local_cbv_constraints_satisfied=local_cbv_constraints_satisfied,
        global_exploitability_oracle_satisfied=(
            global_exploitability_oracle_satisfied
        ),
        resolver_best_response_invariant=player_zero_unchanged,
        constraint=constraint,
        candidate_policy_sha256=_policy_sha256(candidate_policy),
        tolerance=tolerance,
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
    certificate: OracleCertifiedKuhnResolveCertificate
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
    raw_grid = tuple(probability_grid)
    if any(type(value) not in (int, float) for value in raw_grid):
        raise TypeError("probability_grid values must be numeric, not bool/string")
    grid = tuple(sorted(set(float(value) for value in raw_grid)))
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
