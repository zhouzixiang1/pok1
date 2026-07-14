"""A1-owned real-HUNL depth-limited CFR-AVG and label generation.

The solver uses the Common national state for every public transition and the
frozen M5a 1,326-combo registry.  It intentionally contains no TCP runtime and
does not import the independent A2 blueprint implementation.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Protocol, Sequence

import numpy as np

from ...common_contracts.actions import Action, ActionKind
from ...common_contracts.cards import legal_combo_mask
from ...common_contracts.national_state import NationalGameState, Street
from .hunl_pbs import (
    HUNL_COMBOS,
    HUNL_COMBO_COUNT,
    HUNLReachFactorPublicBeliefState,
    _assert_public_only,
    _validate_public_payload_schema,
)
from .m5b_contract import ACTION_SLOTS, M5B_SOLVER_NAME, canonical_bytes


COMBOS_ARRAY = np.asarray(HUNL_COMBOS, dtype=np.int16)
COMBO_INDEX = {combo: index for index, combo in enumerate(HUNL_COMBOS)}
FULL_DECK = np.arange(52, dtype=np.int16)
ACTION_COUNT = len(ACTION_SLOTS)


def _public_payload(state: NationalGameState) -> dict[str, object]:
    state.assert_invariants()
    payload = state.hand_public_dict()
    payload.pop("terminal_reason")
    payload.pop("winner")
    payload.pop("hand_number", None)
    payload.pop("match_net_before", None)
    return payload


def public_state_id(state: NationalGameState) -> str:
    return hashlib.sha256(canonical_bytes(_public_payload(state))).hexdigest()


def combo_index(cards: Sequence[int]) -> int:
    combo = tuple(sorted(int(card) for card in cards))
    try:
        return COMBO_INDEX[combo]
    except KeyError as exc:
        raise ValueError(f"not a Common physical hole combination: {combo}") from exc


def _normalize_rows(values: np.ndarray, mask: np.ndarray) -> np.ndarray:
    result = np.where(mask[None, :], np.maximum(values, 0.0), 0.0).astype(
        np.float64, copy=False
    )
    totals = result.sum(axis=1, keepdims=True)
    fallback = mask.astype(np.float64)
    fallback /= fallback.sum()
    missing = totals[:, 0] <= 0.0
    if np.any(missing):
        result[missing] = fallback
        totals = result.sum(axis=1, keepdims=True)
    result /= totals
    return result


@dataclass(frozen=True, slots=True)
class ReachFactors:
    """Fast vector form of the frozen M5a mathematical PBS factors."""

    factors: np.ndarray
    board: tuple[int, ...]

    def __post_init__(self) -> None:
        values = np.asarray(self.factors, dtype=np.float64)
        if values.shape != (2, HUNL_COMBO_COUNT):
            raise ValueError("reach factors must have shape [2,1326]")
        if not np.all(np.isfinite(values)) or np.any(values < 0.0):
            raise ValueError("reach factors must be finite/non-negative")
        if not np.allclose(values.sum(axis=1), 1.0, atol=1e-12, rtol=0.0):
            raise ValueError("each reach factor must sum to one")
        board = tuple(int(card) for card in self.board)
        mask = np.asarray(legal_combo_mask(board), dtype=bool)
        if np.any(values[:, ~mask] != 0.0):
            raise ValueError("reach factor retains a public-blocked combo")
        frozen = np.array(values, dtype=np.float64, copy=True)
        frozen.setflags(write=False)
        object.__setattr__(self, "factors", frozen)
        object.__setattr__(self, "board", board)
        if self.joint_normalizer <= 0.0:
            raise ValueError("reach factors have no compatible joint support")

    @classmethod
    def from_pbs(cls, pbs: HUNLReachFactorPublicBeliefState) -> "ReachFactors":
        board = pbs.public_state["board"]
        assert isinstance(board, list)
        return cls(np.asarray(pbs.reach_factors, dtype=np.float64), tuple(board))

    def to_pbs_factors(self) -> tuple[tuple[float, ...], tuple[float, ...]]:
        return tuple(self.factors[0]), tuple(self.factors[1])

    @staticmethod
    def compatibility_mass(factor: np.ndarray) -> np.ndarray:
        """Opponent factor mass compatible with each fixed private hand."""

        factor = np.asarray(factor, dtype=np.float64)
        card_mass = np.zeros(52, dtype=np.float64)
        np.add.at(card_mass, COMBOS_ARRAY[:, 0], factor)
        np.add.at(card_mass, COMBOS_ARRAY[:, 1], factor)
        result = (
            factor.sum()
            - card_mass[COMBOS_ARRAY[:, 0]]
            - card_mass[COMBOS_ARRAY[:, 1]]
            + factor
        )
        result[np.abs(result) < 1e-15] = 0.0
        if np.any(result < 0.0) or not np.all(np.isfinite(result)):
            raise ValueError("invalid blocker compatibility mass")
        return result

    @property
    def joint_normalizer(self) -> float:
        mass = self.compatibility_mass(self.factors[1])
        return float(np.dot(self.factors[0], mass))

    def projected_marginal(self, player: int) -> np.ndarray:
        if player not in (0, 1):
            raise ValueError("player must be 0 or 1")
        compatible = self.compatibility_mass(self.factors[1 - player])
        weights = self.factors[player] * compatible
        total = float(weights.sum())
        if total <= 0.0:
            raise ValueError("projected marginal has zero mass")
        return weights / total

    def label_valid_mask(self, player: int) -> np.ndarray:
        return self.projected_marginal(player) > 0.0

    def conditional_opponent(
        self, player: int, fixed_combo_index: int
    ) -> np.ndarray:
        if player not in (0, 1):
            raise ValueError("player must be 0 or 1")
        cards = COMBOS_ARRAY[int(fixed_combo_index)]
        compatible = np.logical_and(
            np.all(COMBOS_ARRAY != cards[0], axis=1),
            np.all(COMBOS_ARRAY != cards[1], axis=1),
        )
        weights = np.where(compatible, self.factors[1 - player], 0.0)
        total = float(weights.sum())
        if total <= 0.0:
            raise ValueError("fixed hand has zero compatible opponent reach")
        return weights / total

    def sample_joint(self, rng: np.random.Generator) -> tuple[int, int]:
        first = int(rng.choice(HUNL_COMBO_COUNT, p=self.projected_marginal(0)))
        second = int(rng.choice(HUNL_COMBO_COUNT, p=self.conditional_opponent(0, first)))
        return first, second

    def observe_action(self, actor: int, likelihood: np.ndarray) -> "ReachFactors":
        if actor not in (0, 1):
            raise ValueError("actor must be 0 or 1")
        likelihood = np.asarray(likelihood, dtype=np.float64)
        if likelihood.shape != (HUNL_COMBO_COUNT,):
            raise ValueError("action likelihood must have shape [1326]")
        if not np.all(np.isfinite(likelihood)) or np.any(likelihood < 0.0):
            raise ValueError("action likelihood must be finite/non-negative")
        factor_evidence = float(np.dot(self.factors[actor], likelihood))
        joint_evidence = float(np.dot(self.projected_marginal(actor), likelihood))
        if factor_evidence <= 0.0 or joint_evidence <= 0.0:
            raise ValueError("observed action has zero evidence")
        updated = self.factors.copy()
        updated[actor] *= likelihood
        updated[actor] /= updated[actor].sum()
        return ReachFactors(updated, self.board)

    def observe_public_cards(self, board: Sequence[int]) -> "ReachFactors":
        board = tuple(int(card) for card in board)
        if board[: len(self.board)] != self.board or len(board) <= len(self.board):
            raise ValueError("public board must strictly extend current board")
        mask = np.asarray(legal_combo_mask(board), dtype=np.float64)
        updated = self.factors * mask[None, :]
        totals = updated.sum(axis=1)
        if np.any(totals <= 0.0):
            raise ValueError("public chance has zero reach evidence")
        updated /= totals[:, None]
        return ReachFactors(updated, board)

    @property
    def digest(self) -> str:
        digest = hashlib.sha256()
        digest.update(np.ascontiguousarray(self.factors, dtype="<f8").tobytes())
        digest.update(bytes(self.board))
        return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class PublicInferenceInput:
    """Strict learned-model input with no reference to a full Common state."""

    public_state_json: str
    reach_factors: np.ndarray
    legal_combo_mask: np.ndarray
    legal_action_mask: np.ndarray

    def __post_init__(self) -> None:
        try:
            payload = json.loads(self.public_state_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("public inference JSON is invalid") from exc
        if canonical_bytes(payload).decode("utf-8") != self.public_state_json:
            raise ValueError("public inference JSON is not canonical")
        try:
            _assert_public_only(payload)
            _validate_public_payload_schema(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "public inference input differs from exact Common public schema"
            ) from exc
        replay_payload = dict(payload)
        replay_payload.update(
            {
                "hand_number": 1,
                "hole_cards": [[], []],
                "match_net_before": [0, 0],
                "terminal_reason": None,
                "winner": None,
            }
        )
        try:
            replayed = NationalGameState.from_dict(replay_payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("public inference input is not Common-replay-valid") from exc
        roundtrip = _public_payload(replayed)
        if canonical_bytes(roundtrip) != canonical_bytes(payload):
            raise ValueError("public inference input differs after Common replay")

        raw_reach = np.asarray(self.reach_factors)
        raw_combo_mask = np.asarray(self.legal_combo_mask)
        raw_action_mask = np.asarray(self.legal_action_mask)
        if raw_reach.dtype != np.float64:
            raise ValueError("public inference reaches must use exact float64 storage")
        if raw_combo_mask.dtype != np.bool_ or raw_action_mask.dtype != np.bool_:
            raise ValueError("public inference masks must use exact bool storage")
        reach = np.asarray(raw_reach, dtype=np.float64)
        combo_mask = np.asarray(raw_combo_mask, dtype=bool)
        action_mask = np.asarray(raw_action_mask, dtype=bool)
        if reach.shape != (2, HUNL_COMBO_COUNT):
            raise ValueError("public inference reaches must have shape [2,1326]")
        if combo_mask.shape != (HUNL_COMBO_COUNT,):
            raise ValueError("public inference combo mask must have shape [1326]")
        if action_mask.shape != (ACTION_COUNT,):
            raise ValueError("public inference action mask must have shape [9]")
        expected_combo_mask = np.asarray(legal_combo_mask(replayed.board), dtype=bool)
        if not np.array_equal(combo_mask, expected_combo_mask):
            raise ValueError("public inference combo mask differs from Common board")
        if (
            not np.all(np.isfinite(reach))
            or np.any(reach < 0.0)
            or not np.allclose(reach.sum(axis=1), 1.0, atol=1e-12, rtol=0.0)
            or np.any(reach[:, ~combo_mask] != 0.0)
        ):
            raise ValueError("public inference reaches violate combo mask")
        # Per-player normalization is insufficient: the two factors must also
        # induce nonzero mass on at least one pair of disjoint private hands.
        validated_reach = ReachFactors(reach, tuple(replayed.board))
        if not np.array_equal(validated_reach.factors, reach):
            raise ValueError("public inference reaches differ after PBS validation")
        reach = validated_reach.factors
        base_action_mask = abstract_actions(replayed).mask
        action_mask_without_offtree = action_mask.copy()
        action_mask_without_offtree[7] = False
        if (
            not np.array_equal(action_mask_without_offtree, base_action_mask)
            or (base_action_mask[7] and not action_mask[7])
        ):
            raise ValueError("public inference action mask differs from Common legality")
        for name, value in (
            ("reach_factors", reach),
            ("legal_combo_mask", combo_mask),
            ("legal_action_mask", action_mask),
        ):
            frozen = np.array(value, copy=True)
            frozen.setflags(write=False)
            object.__setattr__(self, name, frozen)

    @classmethod
    def from_state(
        cls,
        state: NationalGameState,
        reach: ReachFactors,
        action_mask: np.ndarray | None = None,
    ) -> "PublicInferenceInput":
        payload = _public_payload(state)
        # Defensive recursive check: action history is public; no full-state
        # object or private sample is retained by this view.
        text = canonical_bytes(payload).decode("utf-8")
        combo_mask = np.asarray(legal_combo_mask(state.board), dtype=bool)
        if action_mask is None:
            action_mask = np.zeros(ACTION_COUNT, dtype=bool)
        return cls(text, reach.factors, combo_mask, action_mask)

    @property
    def public_state(self) -> dict[str, object]:
        return json.loads(self.public_state_json)

    @property
    def digest(self) -> str:
        digest = hashlib.sha256()
        digest.update(self.public_state_json.encode("utf-8"))
        digest.update(np.ascontiguousarray(self.reach_factors, dtype="<f8").tobytes())
        digest.update(np.packbits(self.legal_combo_mask).tobytes())
        digest.update(np.packbits(self.legal_action_mask).tobytes())
        return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class AbstractActionSet:
    """At most nine stable semantic slots bound to exact Common actions."""

    public_state_id: str
    slot_actions: tuple[Action | None, ...]
    exact_offtree_slot: int | None = None

    def __post_init__(self) -> None:
        if len(self.slot_actions) != ACTION_COUNT:
            raise ValueError("action set must contain exactly nine slots")
        wires = [action.to_wire() for action in self.slot_actions if action is not None]
        if len(wires) != len(set(wires)):
            raise ValueError("one exact Common action cannot occupy multiple slots")
        if not wires:
            raise ValueError("action set cannot be empty")
        if self.exact_offtree_slot is not None and self.exact_offtree_slot != 7:
            raise ValueError("exact off-tree action must occupy slot 7")

    @property
    def mask(self) -> np.ndarray:
        return np.asarray([action is not None for action in self.slot_actions], dtype=bool)

    @property
    def wires(self) -> tuple[str | None, ...]:
        return tuple(
            action.to_wire() if action is not None else None
            for action in self.slot_actions
        )

    def action(self, slot: int) -> Action:
        action = self.slot_actions[int(slot)]
        if action is None:
            raise ValueError(f"inactive action slot {slot}")
        return action

    def slot_for(self, action: Action) -> int:
        wire = action.to_wire()
        for slot, candidate in enumerate(self.slot_actions):
            if candidate is not None and candidate.to_wire() == wire:
                return slot
        raise ValueError(f"action {wire} is outside the abstract support")

    def snapshot(self) -> dict[str, object]:
        return {
            "public_state_id": self.public_state_id,
            "slot_names": list(ACTION_SLOTS),
            "action_wires": list(self.wires),
            "legal_mask": self.mask.tolist(),
            "exact_offtree_slot": self.exact_offtree_slot,
            "nearest_action_translation_used": False,
        }


def _raise_candidate(state: NationalGameState, fraction: float) -> int:
    assert state.actor is not None
    actor = state.actor
    other = 1 - actor
    call_cost = max(0, state.street_bets[other] - state.street_bets[actor])
    pot_after_call = state.pot + call_cost
    return int(round(state.street_bets[other] + fraction * pot_after_call))


def abstract_actions(
    state: NationalGameState,
    *,
    exact_offtree_action: Action | None = None,
) -> AbstractActionSet:
    state.assert_invariants()
    legal = state.legal_actions()
    slots: list[Action | None] = [None] * ACTION_COUNT
    candidates: list[tuple[int, Action]] = []
    if legal.fold:
        candidates.append((0, Action(ActionKind.FOLD)))
    if legal.check:
        candidates.append((1, Action(ActionKind.CHECK)))
    if legal.call:
        candidates.append((2, Action(ActionKind.CALL)))
    if legal.min_raise_to is not None and legal.max_raise_to is not None:
        candidates.append((3, Action(ActionKind.RAISE, legal.min_raise_to)))
        for slot, fraction in ((4, 0.5), (5, 1.0), (6, 1.5)):
            amount = min(
                legal.max_raise_to,
                max(legal.min_raise_to, _raise_candidate(state, fraction)),
            )
            candidates.append((slot, Action(ActionKind.RAISE, amount)))
    if legal.allin:
        candidates.append((8, Action(ActionKind.ALLIN)))

    seen: set[str] = set()
    for slot, action in candidates:
        wire = action.to_wire()
        if legal.contains(action) and wire not in seen:
            slots[slot] = action
            seen.add(wire)

    injected: int | None = None
    if exact_offtree_action is not None:
        if type(exact_offtree_action) is not Action:
            raise TypeError("exact off-tree action must be a Common Action")
        if not legal.contains(exact_offtree_action):
            raise ValueError("exact off-tree action is not Common-legal")
        wire = exact_offtree_action.to_wire()
        if wire not in seen:
            slots[7] = exact_offtree_action
            injected = 7
    return AbstractActionSet(public_state_id(state), tuple(slots), injected)


class PolicyProvider(Protocol):
    version: str

    def __call__(
        self,
        model_input: PublicInferenceInput,
        actions: AbstractActionSet,
    ) -> np.ndarray: ...


class PublicValueProvider(Protocol):
    """Learned-leaf boundary: public PBS in, complete value table out.

    A sampled private deal and RNG are structurally unavailable here.  The
    solver performs the private-row lookup only after this call returns.
    """

    version: str

    def __call__(
        self,
        model_input: PublicInferenceInput,
    ) -> np.ndarray: ...


@dataclass(slots=True)
class UniformPolicy:
    version: str = "a1_uniform_policy_v0"

    def __call__(
        self,
        model_input: PublicInferenceInput,
        actions: AbstractActionSet,
    ) -> np.ndarray:
        del model_input
        mask = actions.mask
        row = mask.astype(np.float64) / mask.sum()
        return np.broadcast_to(row, (HUNL_COMBO_COUNT, ACTION_COUNT)).copy()


def _sample_slot(
    probabilities: np.ndarray, mask: np.ndarray, rng: np.random.Generator
) -> int:
    row = np.where(mask, np.asarray(probabilities, dtype=np.float64), 0.0)
    total = float(row.sum())
    if total <= 0.0 or not np.all(np.isfinite(row)):
        raise ValueError("cannot sample invalid action probabilities")
    row /= total
    return int(rng.choice(ACTION_COUNT, p=row))


def _deal_public_chance(
    state: NationalGameState,
    reach: ReachFactors,
    holes: tuple[int, int],
    rng: np.random.Generator,
) -> tuple[NationalGameState, ReachFactors]:
    if not state.chance_pending:
        raise ValueError("state is not a chance node")
    if state.street is Street.PREFLOP:
        count = 3
    elif state.street in (Street.FLOP, Street.TURN):
        count = 1
    else:
        raise ValueError("river cannot await public chance")
    blocked = set(state.board)
    blocked.update(HUNL_COMBOS[holes[0]])
    blocked.update(HUNL_COMBOS[holes[1]])
    available = np.asarray([card for card in FULL_DECK if int(card) not in blocked])
    dealt = tuple(int(card) for card in rng.choice(available, size=count, replace=False))
    next_state = state.apply_chance(dealt)
    return next_state, reach.observe_public_cards(next_state.board)


def rollout_to_terminal(
    state: NationalGameState,
    reach: ReachFactors,
    holes: tuple[int, int],
    policy: PolicyProvider,
    rng: np.random.Generator,
    *,
    first_forced_slot: int | None = None,
) -> tuple[int, int]:
    """Sample a complete Common hand under a public-belief policy."""

    cursor = state
    belief = reach
    forced = first_forced_slot
    decisions = 0
    while not cursor.is_terminal:
        if cursor.chance_pending:
            cursor, belief = _deal_public_chance(cursor, belief, holes, rng)
            continue
        if decisions > 64:
            raise RuntimeError("rollout exceeded the bounded Common action horizon")
        actions = abstract_actions(cursor)
        model_input = PublicInferenceInput.from_state(cursor, belief, actions.mask)
        matrix = _normalize_rows(policy(model_input, actions), actions.mask)
        assert cursor.actor is not None
        private_index = holes[cursor.actor]
        slot = forced if forced is not None else _sample_slot(
            matrix[private_index], actions.mask, rng
        )
        forced = None
        action = actions.action(slot)
        next_state = cursor.apply_action(action)
        if not next_state.is_terminal:
            belief = belief.observe_action(cursor.actor, matrix[:, slot])
        cursor = next_state
        decisions += 1
    return cursor.terminal_utility(
        hole_cards=(HUNL_COMBOS[holes[0]], HUNL_COMBOS[holes[1]])
    )


@dataclass(slots=True)
class TerminalRolloutLeaf:
    policy: PolicyProvider
    rollouts: int = 1
    version: str = "a1_terminal_rollout_v0"

    def __call__(
        self,
        state: NationalGameState,
        reach: ReachFactors,
        holes: tuple[int, int],
        player: int,
        rng: np.random.Generator,
    ) -> float:
        values = [
            rollout_to_terminal(state, reach, holes, self.policy, rng)[player]
            for _ in range(self.rollouts)
        ]
        return float(np.mean(values))


@dataclass(slots=True)
class _Node:
    actor: int
    action_wires: tuple[str | None, ...]
    prior: np.ndarray
    regrets: np.ndarray = field(init=False)
    strategy_sum: np.ndarray = field(init=False)
    strategy_weight: np.ndarray = field(init=False)
    q_sum: np.ndarray = field(init=False)
    q_count: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        prior = np.asarray(self.prior, dtype=np.float64)
        if prior.shape != (HUNL_COMBO_COUNT, ACTION_COUNT):
            raise ValueError("node prior has the wrong shape")
        self.prior = prior
        self.regrets = np.zeros_like(prior)
        # One explicit warm-start pseudo-observation makes every legal private
        # row a defined average-policy target; it is never a value label.
        self.strategy_sum = prior.copy()
        self.strategy_weight = np.ones(HUNL_COMBO_COUNT, dtype=np.float64)
        self.q_sum = np.zeros_like(prior)
        self.q_count = np.zeros_like(prior)

    @property
    def mask(self) -> np.ndarray:
        return np.asarray([wire is not None for wire in self.action_wires], dtype=bool)

    def current_policy(self) -> np.ndarray:
        positive = np.maximum(self.regrets, 0.0)
        totals = positive.sum(axis=1, keepdims=True)
        result = self.prior.copy()
        active = totals[:, 0] > 0.0
        result[active] = positive[active] / totals[active]
        return _normalize_rows(result, self.mask)

    def average_policy(self) -> np.ndarray:
        result = self.strategy_sum / self.strategy_weight[:, None]
        return _normalize_rows(result, self.mask)


@dataclass(frozen=True, slots=True)
class SearchResult:
    solver: str
    root_public_state_id: str
    root_reach_digest: str
    solve_root_binding: str
    iterations: int
    deals_per_iteration: int
    sampled_search_iteration: int
    behavior_policy_version: str
    leaf_value_version: str
    root_actions: AbstractActionSet
    root_average_policy: np.ndarray
    root_sampled_iteration_policy: np.ndarray
    root_normalized_q: np.ndarray
    root_q_valid_mask: np.ndarray
    nodes: dict[str, _Node] = field(repr=False, compare=False)

    def policy_for(
        self,
        state: NationalGameState,
        reach: ReachFactors,
        actions: AbstractActionSet,
        fallback: PolicyProvider,
    ) -> np.ndarray:
        key = information_node_id(state, self.solve_root_binding, actions.wires)
        node = self.nodes.get(key)
        if node is None or node.action_wires != actions.wires:
            model_input = PublicInferenceInput.from_state(state, reach, actions.mask)
            return _normalize_rows(fallback(model_input, actions), actions.mask)
        return node.average_policy()

    def snapshot(self) -> dict[str, object]:
        q_valid = self.root_q_valid_mask
        q_values = self.root_normalized_q[q_valid]
        return {
            "solver": self.solver,
            "root_public_state_id": self.root_public_state_id,
            "root_reach_digest": self.root_reach_digest,
            "solve_root_binding": self.solve_root_binding,
            "iterations": self.iterations,
            "deals_per_iteration": self.deals_per_iteration,
            "sampled_search_iteration": self.sampled_search_iteration,
            "behavior_policy_version": self.behavior_policy_version,
            "leaf_value_version": self.leaf_value_version,
            "root_actions": self.root_actions.snapshot(),
            "node_count": len(self.nodes),
            "q_valid_count": int(q_valid.sum()),
            "q_min": float(q_values.min()) if q_values.size else None,
            "q_max": float(q_values.max()) if q_values.size else None,
            "q_sha256": hashlib.sha256(
                np.ascontiguousarray(self.root_normalized_q, dtype="<f8").tobytes()
            ).hexdigest(),
            "average_policy_sha256": hashlib.sha256(
                np.ascontiguousarray(self.root_average_policy, dtype="<f8").tobytes()
            ).hexdigest(),
        }


def information_node_id(
    state: NationalGameState,
    solve_root_binding: str,
    action_wires: Sequence[str | None],
) -> str:
    return hashlib.sha256(
        canonical_bytes(
            {
                "solve_root_binding": solve_root_binding,
                "public_state_id": public_state_id(state),
                "actor": state.actor,
                "action_wires": list(action_wires),
            }
        )
    ).hexdigest()


def information_node_id_from_public(
    model_input: PublicInferenceInput,
    solve_root_binding: str,
    action_wires: Sequence[str | None],
) -> str:
    payload = model_input.public_state
    return hashlib.sha256(
        canonical_bytes(
            {
                "solve_root_binding": solve_root_binding,
                "public_state_id": hashlib.sha256(
                    model_input.public_state_json.encode("utf-8")
                ).hexdigest(),
                "actor": payload.get("actor"),
                "action_wires": list(action_wires),
            }
        )
    ).hexdigest()


class SearchProfile:
    """Average subgame policy with a separately-owned A1 fallback."""

    def __init__(self, result: SearchResult, fallback: PolicyProvider):
        self.result = result
        self.fallback = fallback
        self.version = f"{result.solver}:average:{fallback.version}"
        self._fallback_cache: dict[str, np.ndarray] = {}

    def __call__(
        self,
        model_input: PublicInferenceInput,
        actions: AbstractActionSet,
    ) -> np.ndarray:
        key = information_node_id_from_public(
            model_input, self.result.solve_root_binding, actions.wires
        )
        node = self.result.nodes.get(key)
        if node is not None and node.action_wires == actions.wires:
            return node.average_policy()
        cache_key = hashlib.sha256(
            canonical_bytes(
                {
                    "input": model_input.digest,
                    "actions": list(actions.wires),
                    "provider": self.fallback.version,
                }
            )
        ).hexdigest()
        cached = self._fallback_cache.get(cache_key)
        if cached is not None:
            return cached.copy()
        first = np.asarray(self.fallback(model_input, actions), dtype=np.float64)
        second = np.asarray(self.fallback(model_input, actions), dtype=np.float64)
        if first.shape != (HUNL_COMBO_COUNT, ACTION_COUNT):
            raise ValueError("public policy provider must return shape [1326,9]")
        if not np.array_equal(first, second):
            raise ValueError("public policy provider is stateful/non-deterministic")
        checked = _normalize_rows(first, actions.mask)
        self._fallback_cache[cache_key] = checked.copy()
        return checked


class DepthLimitedCFRAvg:
    """Alternating linear external-sampling CFR over real Common HUNL states."""

    def __init__(
        self,
        *,
        iterations: int,
        deals_per_iteration: int,
        public_action_depth: int,
        warm_policy: PolicyProvider,
        rollout_leaf: TerminalRolloutLeaf | None = None,
        public_value_leaf: PublicValueProvider | None = None,
        seed: int,
    ) -> None:
        if iterations < 2 or deals_per_iteration < 1 or public_action_depth < 1:
            raise ValueError("invalid CFR scale")
        self.iterations = int(iterations)
        self.deals_per_iteration = int(deals_per_iteration)
        self.public_action_depth = int(public_action_depth)
        self.warm_policy = warm_policy
        if (rollout_leaf is None) == (public_value_leaf is None):
            raise ValueError(
                "exactly one private rollout oracle or public learned leaf is required"
            )
        self.rollout_leaf = rollout_leaf
        self.public_value_leaf = public_value_leaf
        self.seed = int(seed)
        self.nodes: dict[str, _Node] = {}
        self._root_key: str | None = None
        self._solve_root_binding: str | None = None
        self._warm_cache: dict[str, np.ndarray] = {}
        self._leaf_cache: dict[str, np.ndarray] = {}

    @property
    def leaf_version(self) -> str:
        leaf = self.public_value_leaf or self.rollout_leaf
        assert leaf is not None
        return leaf.version

    def _leaf_value(
        self,
        state: NationalGameState,
        reach: ReachFactors,
        holes: tuple[int, int],
        player: int,
        rng: np.random.Generator,
    ) -> float:
        if self.public_value_leaf is not None:
            # The learned boundary has already returned the complete public-only
            # table before the sampled private row is selected here.
            leaf_actions = abstract_actions(state)
            model_input = PublicInferenceInput.from_state(
                state, reach, leaf_actions.mask
            )
            cached = self._leaf_cache.get(model_input.digest)
            if cached is None:
                first = np.asarray(self.public_value_leaf(model_input), dtype=np.float64)
                second = np.asarray(self.public_value_leaf(model_input), dtype=np.float64)
                if not np.array_equal(first, second):
                    raise ValueError(
                        "public learned leaf is stateful/non-deterministic"
                    )
                cached = first.copy()
                self._leaf_cache[model_input.digest] = cached
            table = cached
            if table.shape != (2, HUNL_COMBO_COUNT):
                raise ValueError("public learned leaf must return shape [2,1326]")
            if not np.all(np.isfinite(table)):
                raise ValueError("public learned leaf returned non-finite values")
            return float(table[player, holes[player]])
        assert self.rollout_leaf is not None
        return float(self.rollout_leaf(state, reach, holes, player, rng))

    def _node(
        self,
        state: NationalGameState,
        reach: ReachFactors,
        actions: AbstractActionSet,
    ) -> tuple[str, _Node]:
        assert state.actor is not None
        if self._solve_root_binding is None:
            raise RuntimeError("solve root binding is not initialized")
        key = information_node_id(state, self._solve_root_binding, actions.wires)
        node = self.nodes.get(key)
        if node is None:
            model_input = PublicInferenceInput.from_state(state, reach, actions.mask)
            cache_key = hashlib.sha256(
                canonical_bytes(
                    {
                        "input": model_input.digest,
                        "actions": list(actions.wires),
                        "provider": self.warm_policy.version,
                    }
                )
            ).hexdigest()
            prior = self._warm_cache.get(cache_key)
            if prior is None:
                first = np.asarray(
                    self.warm_policy(model_input, actions), dtype=np.float64
                )
                second = np.asarray(
                    self.warm_policy(model_input, actions), dtype=np.float64
                )
                if first.shape != (HUNL_COMBO_COUNT, ACTION_COUNT):
                    raise ValueError("public warm policy must return shape [1326,9]")
                if not np.array_equal(first, second):
                    raise ValueError("public warm policy is stateful/non-deterministic")
                prior = _normalize_rows(first, actions.mask)
                self._warm_cache[cache_key] = prior.copy()
            node = _Node(state.actor, actions.wires, prior)
            self.nodes[key] = node
        elif node.actor != state.actor or node.action_wires != actions.wires:
            raise RuntimeError("information node identity collision")
        return key, node

    def _traverse(
        self,
        state: NationalGameState,
        reach: ReachFactors,
        holes: tuple[int, int],
        traverser: int,
        iteration_weight: float,
        action_depth: int,
        rng: np.random.Generator,
    ) -> float:
        if state.is_terminal:
            return float(
                state.terminal_utility(
                    hole_cards=(HUNL_COMBOS[holes[0]], HUNL_COMBOS[holes[1]])
                )[traverser]
            )
        if state.chance_pending:
            next_state, next_reach = _deal_public_chance(state, reach, holes, rng)
            return self._traverse(
                next_state,
                next_reach,
                holes,
                traverser,
                iteration_weight,
                action_depth,
                rng,
            )
        if action_depth >= self.public_action_depth:
            return self._leaf_value(state, reach, holes, traverser, rng)

        actions = abstract_actions(state)
        _, node = self._node(state, reach, actions)
        strategy = node.current_policy()
        assert state.actor is not None
        actor = state.actor
        hand = holes[actor]
        node.strategy_sum[hand] += iteration_weight * strategy[hand]
        node.strategy_weight[hand] += iteration_weight

        if actor == traverser:
            action_values = np.zeros(ACTION_COUNT, dtype=np.float64)
            for slot in np.flatnonzero(actions.mask):
                action = actions.action(int(slot))
                next_state = state.apply_action(action)
                next_reach = reach
                if not next_state.is_terminal:
                    next_reach = reach.observe_action(actor, strategy[:, int(slot)])
                action_values[int(slot)] = self._traverse(
                    next_state,
                    next_reach,
                    holes,
                    traverser,
                    iteration_weight,
                    action_depth + 1,
                    rng,
                )
            node_value = float(np.dot(strategy[hand], action_values))
            node.regrets[hand, actions.mask] += iteration_weight * (
                action_values[actions.mask] - node_value
            )
            if (
                self._root_key is not None
                and self._solve_root_binding is not None
                and information_node_id(
                    state, self._solve_root_binding, actions.wires
                )
                == self._root_key
            ):
                node.q_sum[hand, actions.mask] += action_values[actions.mask]
                node.q_count[hand, actions.mask] += 1.0
            return node_value

        slot = _sample_slot(strategy[hand], actions.mask, rng)
        action = actions.action(slot)
        next_state = state.apply_action(action)
        next_reach = reach
        if not next_state.is_terminal:
            next_reach = reach.observe_action(actor, strategy[:, slot])
        return self._traverse(
            next_state,
            next_reach,
            holes,
            traverser,
            iteration_weight,
            action_depth + 1,
            rng,
        )

    def solve(
        self,
        state: NationalGameState,
        pbs: HUNLReachFactorPublicBeliefState,
    ) -> SearchResult:
        pbs.assert_matches(state)
        if state.actor is None or state.chance_pending or state.is_terminal:
            raise ValueError("CFR root must be a Common decision node")
        reach = ReachFactors.from_pbs(pbs)
        self._solve_root_binding = pbs.pbs_state_id
        root_actions = abstract_actions(state)
        root_key, root_node = self._node(state, reach, root_actions)
        self._root_key = root_key
        rng = np.random.default_rng(self.seed)
        sampled_iteration = int(rng.integers(1, self.iterations + 1))
        sampled_policy: np.ndarray | None = None
        for iteration in range(1, self.iterations + 1):
            for _ in range(self.deals_per_iteration):
                holes = reach.sample_joint(rng)
                for traverser in (0, 1):
                    self._traverse(
                        state,
                        reach,
                        holes,
                        traverser,
                        float(iteration),
                        0,
                        rng,
                    )
            if iteration == sampled_iteration:
                sampled_policy = root_node.average_policy().copy()
        assert sampled_policy is not None
        q_valid = root_node.q_count > 0.0
        q = np.zeros_like(root_node.q_sum)
        q[q_valid] = root_node.q_sum[q_valid] / root_node.q_count[q_valid]
        return SearchResult(
            solver=M5B_SOLVER_NAME,
            root_public_state_id=public_state_id(state),
            root_reach_digest=reach.digest,
            solve_root_binding=self._solve_root_binding,
            iterations=self.iterations,
            deals_per_iteration=self.deals_per_iteration,
            sampled_search_iteration=sampled_iteration,
            behavior_policy_version=self.warm_policy.version,
            leaf_value_version=self.leaf_version,
            root_actions=root_actions,
            root_average_policy=root_node.average_policy().copy(),
            root_sampled_iteration_policy=sampled_policy,
            root_normalized_q=q,
            root_q_valid_mask=q_valid,
            nodes=dict(self.nodes),
        )


@dataclass(frozen=True, slots=True)
class PrivateTargets:
    """Full per-private-hand values plus diagnostic CFV/Q namespaces."""

    normalized_values: np.ndarray
    unnormalized_cfvs: np.ndarray
    value_valid_mask: np.ndarray
    projected_marginals: np.ndarray
    actor_normalized_q: np.ndarray
    actor_q_valid_mask: np.ndarray
    raw_weighted_zero_sum: float
    diagnostic_cross_profile_q_v_mae: float

    def __post_init__(self) -> None:
        expected_2 = (2, HUNL_COMBO_COUNT)
        if self.normalized_values.shape != expected_2:
            raise ValueError("normalized value target must have shape [2,1326]")
        if self.unnormalized_cfvs.shape != expected_2:
            raise ValueError("counterfactual value target must have shape [2,1326]")
        if self.value_valid_mask.shape != expected_2:
            raise ValueError("value-valid mask must have shape [2,1326]")
        if self.projected_marginals.shape != expected_2:
            raise ValueError("projected marginals must have shape [2,1326]")
        if self.actor_normalized_q.shape != (HUNL_COMBO_COUNT, ACTION_COUNT):
            raise ValueError("root Q target must have shape [1326,9]")
        if self.actor_q_valid_mask.shape != (HUNL_COMBO_COUNT, ACTION_COUNT):
            raise ValueError("root Q-valid mask must have shape [1326,9]")


def generate_private_targets(
    state: NationalGameState,
    pbs: HUNLReachFactorPublicBeliefState,
    result: SearchResult,
    fallback: PolicyProvider,
    *,
    rollouts_per_hand: int,
    seed: int,
) -> PrivateTargets:
    """Estimate V_i(h) for every physically possible private hand.

    Opponent hands are sampled from the exact blocker-conditioned opponent
    reach factor.  The sampled private deal is never part of a network input.
    Sparse Q is copied from actual root CFR traversals and is diagnostic only.
    """

    if rollouts_per_hand < 1:
        raise ValueError("rollouts_per_hand must be positive")
    pbs.assert_matches(state)
    reach = ReachFactors.from_pbs(pbs)
    profile = SearchProfile(result, fallback)
    rng = np.random.default_rng(int(seed))
    values = np.zeros((2, HUNL_COMBO_COUNT), dtype=np.float64)
    valid = np.stack([reach.label_valid_mask(0), reach.label_valid_mask(1)])
    marginals = np.stack(
        [reach.projected_marginal(0), reach.projected_marginal(1)]
    )
    # Two stratified importance proposals share every sampled terminal utility.
    # Component 0 chooses h0 uniformly then h1 from the exact conditional joint;
    # component 1 does the symmetric construction.  For either component,
    # weighted contributions to both players cancel sample-by-sample.  A
    # deterministic-mixture balance heuristic combines the proposals, giving
    # full hand coverage and a projected-marginal zero-sum residual limited
    # only by floating arithmetic.
    valid_indices = [np.flatnonzero(valid[player]) for player in (0, 1)]
    proposal_sample_counts = [
        len(valid_indices[player]) * rollouts_per_hand for player in (0, 1)
    ]
    for proposal_player in (0, 1):
        fixed_indices = valid_indices[proposal_player]
        for fixed in fixed_indices:
            opponent_distribution = reach.conditional_opponent(
                proposal_player, int(fixed)
            )
            for _ in range(rollouts_per_hand):
                opponent = int(
                    rng.choice(HUNL_COMBO_COUNT, p=opponent_distribution)
                )
                holes = (
                    (int(fixed), opponent)
                    if proposal_player == 0
                    else (opponent, int(fixed))
                )
                utility = rollout_to_terminal(state, reach, holes, profile, rng)
                # Deterministic-mixture balance heuristic.  Since q0(e) is
                # J(e)/(n0*mu0(h0)) and q1 is symmetric, the J term cancels
                # from J / (M0*q0 + M1*q1).  This retains exact paired
                # cancellation while avoiding the high variance of J/q_c.
                mixture_denominator_without_joint = (
                    proposal_sample_counts[0]
                    / len(valid_indices[0])
                    / marginals[0, holes[0]]
                    + proposal_sample_counts[1]
                    / len(valid_indices[1])
                    / marginals[1, holes[1]]
                )
                balance_weight = 1.0 / mixture_denominator_without_joint
                for player in (0, 1):
                    hand = holes[player]
                    values[player, hand] += (
                        balance_weight
                        * float(utility[player])
                        / marginals[player, hand]
                    )

    compatibility = np.stack(
        [
            ReachFactors.compatibility_mass(reach.factors[1]),
            ReachFactors.compatibility_mass(reach.factors[0]),
        ]
    )
    cfvs = values * compatibility
    zero_sum = float(
        np.dot(marginals[0], values[0]) + np.dot(marginals[1], values[1])
    )
    if abs(zero_sum) > 1e-8:
        raise RuntimeError(
            "paired stratified value estimator violated weighted zero-sum"
        )
    assert state.actor is not None
    actor = state.actor
    q = result.root_normalized_q.copy()
    q_valid = result.root_q_valid_mask.copy()
    consistency_errors: list[float] = []
    for hand in range(HUNL_COMBO_COUNT):
        mask = q_valid[hand] & result.root_actions.mask
        if np.all(mask == result.root_actions.mask) and np.any(mask):
            estimate = float(np.dot(result.root_average_policy[hand], q[hand]))
            if valid[actor, hand]:
                consistency_errors.append(abs(estimate - values[actor, hand]))
    return PrivateTargets(
        normalized_values=values,
        unnormalized_cfvs=cfvs,
        value_valid_mask=valid,
        projected_marginals=marginals,
        actor_normalized_q=q,
        actor_q_valid_mask=q_valid,
        raw_weighted_zero_sum=zero_sum,
        diagnostic_cross_profile_q_v_mae=(
            float(np.mean(consistency_errors)) if consistency_errors else math.nan
        ),
    )


def sampled_deal_independence_digest(
    state: NationalGameState,
    pbs: HUNLReachFactorPublicBeliefState,
    model_output: np.ndarray,
) -> str:
    """Digest the public PBS and complete output, excluding sampled deals."""

    pbs.assert_matches(state)
    digest = hashlib.sha256()
    digest.update(pbs.network_input_sha256.encode("ascii"))
    digest.update(np.ascontiguousarray(model_output, dtype="<f4").tobytes())
    return digest.hexdigest()
