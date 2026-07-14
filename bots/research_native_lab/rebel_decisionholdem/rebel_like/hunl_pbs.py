"""Public-only reach-factor belief state for Common-authoritative HUNL.

ReBeL's learnable public belief state stores one private-state reach factor per
player. Two normalized 1,326-vectors are not the true marginals of a joint card
posterior. This module therefore names the vectors reach_factors and derives
the blocker-aware joint distribution

    J(h0,h1) = K_B(h0,h1) * beta0(h0) * beta1(h1) / Z.

K_B is one exactly when both hands avoid the public board and each other.
Projected marginals are sums of J. An observed action multiplies and
renormalizes only the actor's factor, while the non-actor factor stays fixed;
both projected marginals can still change through card compatibility.

The network-shaped input is derived exclusively from the Common
NationalGameState.hand_public_dict() after removing terminal payoff fields.
Known holes, future cards, outcome, match context and all private/full state IDs
never enter the PBS input.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from math import fsum
from typing import Mapping, Sequence

from ...common_contracts.actions import Action, ActionKind
from ...common_contracts.cards import all_hole_combinations, legal_combo_mask
from ...common_contracts.constants import CONTRACT_VERSION
from ...common_contracts.national_state import NationalGameState


HUNL_PBS_SCHEMA = "route-a1-hunl-reach-factor-pbs-v1"
HUNL_ACTION_SUPPORT_SCHEMA = "route-a1-hunl-public-action-support-v1"
HUNL_COMBO_ORDER = "common-all-hole-combinations-lexicographic-v1"
HUNL_COMBOS = all_hole_combinations()
HUNL_COMBO_COUNT = len(HUNL_COMBOS)
HUNL_COMBO_REGISTRY_SHA256 = hashlib.sha256(
    json.dumps(HUNL_COMBOS, separators=(",", ":")).encode("utf-8")
).hexdigest()
HUNL_NETWORK_INPUT_FIELDS = frozenset(
    {
        "schema",
        "representation",
        "combo_order",
        "combo_registry_sha256",
        "common_contract_version",
        "public_pbs_state_id",
        "public_state",
        "reach_factors",
        "private_hole_cards_in_input",
        "sampled_deal_in_input",
        "match_context_in_input",
    }
)
MAX_REBEL_ACTIONS = 9
BELIEF_POLICY_KINDS = frozenset(
    {"fixed_profile", "current_policy", "average_policy"}
)

_PUBLIC_STATE_FIELDS = frozenset(
    {
        "contract_version",
        "small_blind",
        "street",
        "actor",
        "stacks",
        "total_contributions",
        "street_bets",
        "action_counts",
        "street_actions",
        "hand_history",
        "board",
        "allin_occurred",
        "chance_pending",
        "runout_pending",
    }
)
_PUBLIC_ACTION_FIELDS = frozenset({"actor", "kind", "amount", "street"})

_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "hole_cards",
        "private_hand",
        "information_state_id",
        "observation_id",
        "full_state_id",
        "match_context_id",
        "hand_number",
        "match_net_before",
        "sampled_deal",
        "future_board",
        "deck",
        "winner",
        "earnChips",
    }
)

ComboPolicy = Sequence[Mapping[str, float]]


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _trusted_state(state: NationalGameState) -> NationalGameState:
    if type(state) is not NationalGameState:
        raise TypeError("state must be the exact Common NationalGameState type")
    state.assert_invariants()
    return state


def _assert_public_only(value: object, *, path: str = "public_state") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if type(key) is not str:
                raise ValueError(f"{path} contains a non-string key")
            if key in _FORBIDDEN_PUBLIC_KEYS:
                raise ValueError(f"{path} contains forbidden private/context key {key}")
            _assert_public_only(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_public_only(child, path=f"{path}[{index}]")
    elif value is not None and type(value) not in (str, int, bool):
        raise ValueError(f"{path} contains a non-canonical scalar")


def _validate_public_payload_schema(payload: object) -> None:
    """Require the exact nonterminal Common hand-public wire schema."""

    if not isinstance(payload, dict) or set(payload) != _PUBLIC_STATE_FIELDS:
        raise ValueError("public PBS fields differ from Common hand_public_dict")
    if payload["contract_version"] != CONTRACT_VERSION:
        raise ValueError("public PBS Common contract version differs")
    if type(payload["small_blind"]) is not int or payload["small_blind"] not in (0, 1):
        raise ValueError("public PBS small blind is invalid")
    street = payload["street"]
    board_lengths = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}
    if type(street) is not str or street not in board_lengths:
        raise ValueError("public PBS street is invalid")
    actor = payload["actor"]
    if actor is not None and (type(actor) is not int or actor not in (0, 1)):
        raise ValueError("public PBS actor is invalid")
    for field_name in (
        "stacks",
        "total_contributions",
        "street_bets",
        "action_counts",
    ):
        values = payload[field_name]
        if (
            not isinstance(values, list)
            or len(values) != 2
            or any(type(value) is not int or value < 0 for value in values)
        ):
            raise ValueError(f"public PBS {field_name} is invalid")
    board = payload["board"]
    if (
        not isinstance(board, list)
        or len(board) != board_lengths[street]
        or len(set(board)) != len(board)
        or any(type(card) is not int or not 0 <= card < 52 for card in board)
    ):
        raise ValueError("public PBS board is invalid")
    for field_name in ("allin_occurred", "chance_pending", "runout_pending"):
        if type(payload[field_name]) is not bool:
            raise ValueError(f"public PBS {field_name} is invalid")
    if payload["chance_pending"] and actor is not None:
        raise ValueError("chance-pending public PBS cannot retain an actor")
    if not payload["chance_pending"] and actor not in (0, 1):
        raise ValueError("decision public PBS requires an actor")
    for field_name in ("street_actions", "hand_history"):
        records = payload[field_name]
        if not isinstance(records, list):
            raise ValueError(f"public PBS {field_name} is invalid")
        for record in records:
            if not isinstance(record, dict) or set(record) != _PUBLIC_ACTION_FIELDS:
                raise ValueError(
                    f"public PBS {field_name} action fields differ from Common"
                )
            if (
                type(record["actor"]) is not int
                or record["actor"] not in (0, 1)
                or type(record["street"]) is not str
                or record["street"] not in board_lengths
            ):
                raise ValueError(f"public PBS {field_name} action identity is invalid")
            try:
                action = Action(ActionKind(record["kind"]), record["amount"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"public PBS {field_name} action encoding is invalid"
                ) from exc
            if action.kind is ActionKind.RAISE and int(action.amount) <= 0:
                raise ValueError(f"public PBS {field_name} raise-to is invalid")
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
        raise ValueError("public PBS is not a replay-valid Common state") from exc
    roundtrip = replayed.hand_public_dict()
    roundtrip.pop("terminal_reason")
    roundtrip.pop("winner")
    if roundtrip != payload:
        raise ValueError("public PBS differs after Common replay round-trip")


def _public_payload(state: NationalGameState) -> dict[str, object]:
    state = _trusted_state(state)
    if state.is_terminal:
        raise ValueError("a terminal/outcome state cannot become a PBS input")
    payload = state.hand_public_dict()
    if payload.pop("terminal_reason", None) is not None:
        raise ValueError("terminal reason leaked into PBS input")
    if payload.pop("winner", None) is not None:
        raise ValueError("winner leaked into PBS input")
    _assert_public_only(payload)
    _validate_public_payload_schema(payload)
    return payload


def _public_json(state: NationalGameState) -> str:
    return _canonical_bytes(_public_payload(state)).decode("utf-8")


def _masked_uniform(board: Sequence[int]) -> tuple[float, ...]:
    mask = legal_combo_mask(board)
    count = sum(mask)
    if count <= 0:
        raise ValueError("public board leaves no legal HUNL combinations")
    probability = 1.0 / count
    return tuple(probability if legal else 0.0 for legal in mask)


def _condition_factor(
    values: Sequence[float], board: Sequence[int]
) -> tuple[float, ...]:
    if len(values) != HUNL_COMBO_COUNT:
        raise ValueError("HUNL reach factor must contain exactly 1,326 values")
    mask = legal_combo_mask(board)
    retained = [
        float(value) if legal else 0.0
        for value, legal in zip(values, mask, strict=True)
    ]
    total = fsum(retained)
    if not math.isfinite(total) or total <= 0.0:
        raise ValueError("public blocker conditioning leaves zero factor mass")
    return tuple(value / total for value in retained)


def _validate_policy(
    policy: ComboPolicy,
    action_wires: tuple[str, ...],
) -> tuple[dict[str, float], ...]:
    if len(policy) != HUNL_COMBO_COUNT:
        raise ValueError("combo policy must contain exactly 1,326 rows")
    expected = set(action_wires)
    normalized: list[dict[str, float]] = []
    for index, row in enumerate(policy):
        if not isinstance(row, Mapping) or set(row) != expected:
            raise ValueError(f"combo policy row {index} does not match action support")
        converted: dict[str, float] = {}
        for wire in action_wires:
            value = row[wire]
            if type(value) not in (int, float):
                raise ValueError(f"combo policy row {index} is not numeric")
            probability = float(value)
            if not math.isfinite(probability) or probability < 0.0:
                raise ValueError(
                    f"combo policy row {index} is not finite/non-negative"
                )
            converted[wire] = probability
        if abs(fsum(converted.values()) - 1.0) > 1e-12:
            raise ValueError(f"combo policy row {index} does not sum to one")
        normalized.append(converted)
    return tuple(normalized)


def _card_masses(factor: Sequence[float]) -> tuple[float, ...]:
    terms: list[list[float]] = [[] for _ in range(52)]
    for (first, second), value in zip(HUNL_COMBOS, factor, strict=True):
        terms[first].append(float(value))
        terms[second].append(float(value))
    return tuple(fsum(values) for values in terms)


def _compatible_masses(factor: Sequence[float]) -> tuple[float, ...]:
    """Return factor mass compatible with every possible opposing hand."""

    card_masses = _card_masses(factor)
    total_mass = fsum(float(value) for value in factor)
    result: list[float] = []
    for index, (first, second) in enumerate(HUNL_COMBOS):
        mass = fsum(
            (
                total_mass,
                -card_masses[first],
                -card_masses[second],
                float(factor[index]),
            )
        )
        if mass < 0.0 and abs(mass) <= 1e-14:
            mass = 0.0
        if not math.isfinite(mass) or mass < 0.0:
            raise ValueError("reach factors produce invalid compatibility mass")
        result.append(mass)
    return tuple(result)


def _joint_projection(
    factors: tuple[tuple[float, ...], tuple[float, ...]]
) -> tuple[float, tuple[tuple[float, ...], tuple[float, ...]]]:
    compatible = (
        _compatible_masses(factors[1]),
        _compatible_masses(factors[0]),
    )
    row_weights = tuple(
        tuple(
            factors[player][index] * compatible[player][index]
            for index in range(HUNL_COMBO_COUNT)
        )
        for player in (0, 1)
    )
    normalizers = (fsum(row_weights[0]), fsum(row_weights[1]))
    if (
        not all(math.isfinite(value) and value > 0.0 for value in normalizers)
        or abs(normalizers[0] - normalizers[1]) > 1e-12
    ):
        raise ValueError("reach factors have zero/inconsistent compatible joint support")
    normalizer = fsum(normalizers) / 2.0
    projected = tuple(
        tuple(value / normalizer for value in row_weights[player])
        for player in (0, 1)
    )
    for player in (0, 1):
        if abs(fsum(projected[player]) - 1.0) > 1e-12:
            raise ValueError("projected blocker-aware marginal is not normalized")
    return normalizer, (projected[0], projected[1])


@dataclass(frozen=True, slots=True)
class HUNLPublicActionSupport:
    """Finite Common-legal solve support with an exact observed action token."""

    public_pbs_state_id: str
    action_wires: tuple[str, ...]
    observed_action_wire: str
    exact_observed_raise_to: int | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_wires", tuple(self.action_wires))
        if not _is_sha256(self.public_pbs_state_id):
            raise ValueError("action support public-state digest is invalid")
        if not self.action_wires or len(self.action_wires) > MAX_REBEL_ACTIONS:
            raise ValueError("action support must contain between one and nine actions")
        if len(set(self.action_wires)) != len(self.action_wires):
            raise ValueError("action support repeats a wire action")
        parsed = tuple(Action.from_wire(wire) for wire in self.action_wires)
        if self.observed_action_wire not in self.action_wires:
            raise ValueError("action support omitted the exact observed action")
        observed = Action.from_wire(self.observed_action_wire)
        if observed.kind is ActionKind.RAISE:
            if self.exact_observed_raise_to != observed.amount:
                raise ValueError("observed raise-to was translated or corrupted")
        elif self.exact_observed_raise_to is not None:
            raise ValueError("non-raise observation cannot carry a raise-to value")
        if tuple(action.to_wire() for action in parsed) != self.action_wires:
            raise ValueError("action support wire encoding is not canonical")

    def assert_bound(self, state: NationalGameState, action: Action) -> None:
        state = _trusted_state(state)
        if _sha256(_canonical_bytes(_public_payload(state))) != self.public_pbs_state_id:
            raise ValueError("action support is stale for this public PBS state")
        if type(action) is not Action:
            raise TypeError("observed action must be the exact Common Action type")
        if action.to_wire() != self.observed_action_wire:
            raise ValueError("observed action differs from the exact support token")
        if not state.legal_actions().contains(action):
            raise ValueError("observed action is not Common-legal")

    def snapshot(self) -> dict[str, object]:
        return {
            "schema": HUNL_ACTION_SUPPORT_SCHEMA,
            "public_pbs_state_id": self.public_pbs_state_id,
            "action_wires": list(self.action_wires),
            "observed_action_wire": self.observed_action_wire,
            "exact_observed_raise_to": self.exact_observed_raise_to,
            "nearest_action_translation_used": False,
        }


def build_public_action_support(
    state: NationalGameState,
    base_actions: Sequence[Action],
    *,
    observed_action: Action,
) -> HUNLPublicActionSupport:
    """Return a finite solve support, appending an off-tree action exactly."""

    state = _trusted_state(state)
    if state.actor is None or state.is_terminal or state.chance_pending:
        raise ValueError("action support requires a pending Common decision")
    if type(observed_action) is not Action:
        raise TypeError("observed_action must be the exact Common Action type")
    legal = state.legal_actions()
    actions: list[Action] = []
    seen: set[str] = set()
    for action in base_actions:
        if type(action) is not Action:
            raise TypeError("base action support must use exact Common Action objects")
        if not legal.contains(action):
            raise ValueError(f"base support contains illegal action {action.to_wire()}")
        if action.to_wire() not in seen:
            actions.append(action)
            seen.add(action.to_wire())
    if not legal.contains(observed_action):
        raise ValueError("observed action is not Common-legal")
    if observed_action.to_wire() not in seen:
        actions.append(observed_action)
    if len(actions) > MAX_REBEL_ACTIONS:
        raise ValueError("exact observed action would exceed ReBeL's nine-action cap")
    return HUNLPublicActionSupport(
        public_pbs_state_id=_sha256(_canonical_bytes(_public_payload(state))),
        action_wires=tuple(action.to_wire() for action in actions),
        observed_action_wire=observed_action.to_wire(),
        exact_observed_raise_to=(
            int(observed_action.amount)
            if observed_action.kind is ActionKind.RAISE
            else None
        ),
    )


@dataclass(frozen=True, slots=True)
class HUNLReachFactorPublicBeliefState:
    """Public state plus two normalized reach factors over Common combos."""

    public_state_json: str
    reach_factors: tuple[tuple[float, ...], tuple[float, ...]]
    belief_update_trace: tuple[tuple[str, str, str], ...] = field(
        default=(), compare=False
    )
    _joint_normalizer: float = field(init=False, repr=False, compare=False)
    _projected_marginals: tuple[tuple[float, ...], tuple[float, ...]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if type(self.public_state_json) is not str:
            raise ValueError("public_state_json must be canonical JSON text")
        try:
            payload = json.loads(self.public_state_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("public_state_json is invalid") from exc
        if not isinstance(payload, dict):
            raise ValueError("public_state_json must encode an object")
        _assert_public_only(payload)
        _validate_public_payload_schema(payload)
        if _canonical_bytes(payload).decode("utf-8") != self.public_state_json:
            raise ValueError("public_state_json is not canonical")
        board = payload.get("board")
        if not isinstance(board, list) or any(type(card) is not int for card in board):
            raise ValueError("public PBS board is invalid")
        if len(self.reach_factors) != 2:
            raise ValueError("HUNL PBS requires exactly two reach factors")
        legal = legal_combo_mask(board)
        normalized: list[tuple[float, ...]] = []
        for player, values in enumerate(self.reach_factors):
            if len(values) != HUNL_COMBO_COUNT:
                raise ValueError(
                    f"player {player} factor must contain exactly 1,326 combinations"
                )
            if any(
                type(value) not in (int, float)
                or not math.isfinite(float(value))
                or float(value) < 0.0
                for value in values
            ):
                raise ValueError(f"player {player} factor is not finite/non-negative")
            converted = tuple(float(value) for value in values)
            if abs(fsum(converted) - 1.0) > 1e-12:
                raise ValueError(f"player {player} factor must sum to one")
            if any(
                not allowed and converted[index] != 0.0
                for index, allowed in enumerate(legal)
            ):
                raise ValueError(
                    f"player {player} factor retains a public-blocked combo"
                )
            normalized.append(converted)
        object.__setattr__(
            self, "reach_factors", (normalized[0], normalized[1])
        )
        trace = tuple(tuple(entry) for entry in self.belief_update_trace)
        for entry in trace:
            if (
                not isinstance(entry, tuple)
                or len(entry) != 3
                or entry[0] not in BELIEF_POLICY_KINDS
                or not _is_sha256(entry[1])
                or Action.from_wire(entry[2]).to_wire() != entry[2]
            ):
                raise ValueError("belief update provenance trace is invalid")
        object.__setattr__(self, "belief_update_trace", trace)
        normalizer, projected = _joint_projection(
            (normalized[0], normalized[1])
        )
        object.__setattr__(self, "_joint_normalizer", normalizer)
        object.__setattr__(self, "_projected_marginals", projected)

    @classmethod
    def from_state(cls, state: NationalGameState) -> "HUNLReachFactorPublicBeliefState":
        state = _trusted_state(state)
        root = NationalGameState.new_hand(1, small_blind=state.small_blind)
        if _public_payload(state) != _public_payload(root):
            raise ValueError(
                "uniform factors may be initialized only at a true new-hand root"
            )
        public_json = _public_json(state)
        uniform = _masked_uniform(state.board)
        return cls(public_json, (uniform, uniform))

    @property
    def public_state(self) -> dict[str, object]:
        return json.loads(self.public_state_json)

    @property
    def public_pbs_state_id(self) -> str:
        return _sha256(self.public_state_json.encode("utf-8"))

    @property
    def board_legal_combo_count(self) -> int:
        board = self.public_state["board"]
        assert isinstance(board, list)
        return sum(legal_combo_mask(board))

    @property
    def compatible_ordered_joint_count(self) -> int:
        board = self.public_state["board"]
        assert isinstance(board, list)
        remaining = 52 - len(board)
        return math.comb(remaining, 2) * math.comb(remaining - 2, 2)

    @property
    def joint_normalizer(self) -> float:
        return self._joint_normalizer

    def reach_factor_for(self, player: int) -> tuple[float, ...]:
        if type(player) is not int or player not in (0, 1):
            raise ValueError("player must be 0 or 1")
        return tuple(self.reach_factors[player])

    def projected_marginal(self, player: int) -> tuple[float, ...]:
        if type(player) is not int or player not in (0, 1):
            raise ValueError("player must be 0 or 1")
        return tuple(self._projected_marginals[player])

    def legal_mask(self) -> tuple[bool, ...]:
        board = self.public_state["board"]
        assert isinstance(board, list)
        return legal_combo_mask(board)

    def positive_reach_mask(self, player: int) -> tuple[bool, ...]:
        return tuple(value > 0.0 for value in self.reach_factor_for(player))

    def label_valid_mask(self, player: int) -> tuple[bool, ...]:
        return tuple(value > 0.0 for value in self.projected_marginal(player))

    def joint_probability(self, first_index: int, second_index: int) -> float:
        if (
            type(first_index) is not int
            or type(second_index) is not int
            or not 0 <= first_index < HUNL_COMBO_COUNT
            or not 0 <= second_index < HUNL_COMBO_COUNT
        ):
            raise ValueError("joint combo index is invalid")
        if set(HUNL_COMBOS[first_index]).intersection(HUNL_COMBOS[second_index]):
            return 0.0
        return (
            self.reach_factors[0][first_index]
            * self.reach_factors[1][second_index]
            / self._joint_normalizer
        )

    def assert_matches(self, state: NationalGameState) -> None:
        if _public_json(_trusted_state(state)) != self.public_state_json:
            raise ValueError("PBS is stale for the supplied public state")

    def _bound_policy(
        self,
        state: NationalGameState,
        support: HUNLPublicActionSupport,
        policy: ComboPolicy,
    ) -> tuple[dict[str, float], ...]:
        self.assert_matches(state)
        if type(support) is not HUNLPublicActionSupport:
            raise TypeError("support must be HUNLPublicActionSupport")
        observed = Action.from_wire(support.observed_action_wire)
        support.assert_bound(state, observed)
        return _validate_policy(policy, support.action_wires)

    def factor_action_normalizer(
        self,
        state: NationalGameState,
        support: HUNLPublicActionSupport,
        policy: ComboPolicy,
    ) -> float:
        """Actor-factor normalizer; not the public event probability."""

        rows = self._bound_policy(state, support, policy)
        assert state.actor is not None
        return fsum(
            self.reach_factors[state.actor][index]
            * rows[index][support.observed_action_wire]
            for index in range(HUNL_COMBO_COUNT)
        )

    def action_probability(
        self,
        state: NationalGameState,
        support: HUNLPublicActionSupport,
        policy: ComboPolicy,
    ) -> float:
        """Blocker-aware public event probability under the projected joint."""

        rows = self._bound_policy(state, support, policy)
        assert state.actor is not None
        probability = fsum(
            self._projected_marginals[state.actor][index]
            * rows[index][support.observed_action_wire]
            for index in range(HUNL_COMBO_COUNT)
        )
        if not math.isfinite(probability):
            raise ValueError("observed action joint event probability is non-finite")
        if probability < -1e-12 or probability > 1.0 + 1e-12:
            raise ValueError("observed action joint event probability is outside [0, 1]")
        if probability < 0.0:
            probability = 0.0
        elif probability > 1.0:
            probability = 1.0
        return probability

    def observe_action(
        self,
        state: NationalGameState,
        support: HUNLPublicActionSupport,
        policy: ComboPolicy,
        *,
        belief_policy_kind: str,
    ) -> "HUNLReachFactorPublicBeliefState":
        """Update the actor factor and rebuild the blocker-aware joint."""

        if belief_policy_kind not in BELIEF_POLICY_KINDS:
            raise ValueError("belief policy kind is invalid")
        rows = self._bound_policy(state, support, policy)
        assert state.actor is not None
        actor = state.actor
        factor_normalizer = fsum(
            self.reach_factors[actor][index]
            * rows[index][support.observed_action_wire]
            for index in range(HUNL_COMBO_COUNT)
        )
        joint_evidence = fsum(
            self._projected_marginals[actor][index]
            * rows[index][support.observed_action_wire]
            for index in range(HUNL_COMBO_COUNT)
        )
        if not math.isfinite(factor_normalizer) or factor_normalizer <= 0.0:
            raise ValueError("observed action has zero/non-finite factor evidence")
        if not math.isfinite(joint_evidence) or joint_evidence <= 0.0:
            raise ValueError("observed action has zero/non-finite joint evidence")
        updated = [tuple(values) for values in self.reach_factors]
        updated[actor] = tuple(
            self.reach_factors[actor][index]
            * rows[index][support.observed_action_wire]
            / factor_normalizer
            for index in range(HUNL_COMBO_COUNT)
        )
        policy_sha256 = _sha256(
            _canonical_bytes(
                {
                    "action_support": support.snapshot(),
                    "combo_order": HUNL_COMBO_ORDER,
                    "combo_registry_sha256": HUNL_COMBO_REGISTRY_SHA256,
                    "rows": rows,
                }
            )
        )
        action = Action.from_wire(support.observed_action_wire)
        next_state = state.apply_action(action)
        return HUNLReachFactorPublicBeliefState(
            _public_json(next_state),
            (updated[0], updated[1]),
            self.belief_update_trace
            + ((belief_policy_kind, policy_sha256, action.to_wire()),),
        )

    def observe_public_chance(
        self,
        state: NationalGameState,
        next_state: NationalGameState,
    ) -> "HUNLReachFactorPublicBeliefState":
        """Mask both factors on a proven Common public-card transition."""

        self.assert_matches(state)
        next_state = _trusted_state(next_state)
        if not state.chance_pending:
            raise ValueError("current Common state is not awaiting public chance")
        if tuple(next_state.board[: len(state.board)]) != tuple(state.board):
            raise ValueError("next public board does not extend the current board")
        added = tuple(next_state.board[len(state.board) :])
        if not added:
            raise ValueError("public chance transition did not add cards")
        expected = state.apply_chance(added)
        if _public_payload(expected) != _public_payload(next_state):
            raise ValueError(
                "next state is not the exact Common public chance transition"
            )
        conditioned = tuple(
            _condition_factor(values, next_state.board)
            for values in self.reach_factors
        )
        return HUNLReachFactorPublicBeliefState(
            _public_json(next_state),
            (conditioned[0], conditioned[1]),
            self.belief_update_trace,
        )

    def network_input(self) -> dict[str, object]:
        """Return only common-knowledge data intended for a later value net."""

        return {
            "schema": HUNL_PBS_SCHEMA,
            "representation": "two_player_reach_factors_with_derived_joint",
            "combo_order": HUNL_COMBO_ORDER,
            "combo_registry_sha256": HUNL_COMBO_REGISTRY_SHA256,
            "common_contract_version": CONTRACT_VERSION,
            "public_pbs_state_id": self.public_pbs_state_id,
            "public_state": self.public_state,
            "reach_factors": [
                list(self.reach_factors[0]),
                list(self.reach_factors[1]),
            ],
            "private_hole_cards_in_input": False,
            "sampled_deal_in_input": False,
            "match_context_in_input": False,
        }

    @property
    def network_input_sha256(self) -> str:
        """Bind public state, both factors and fixed registries, excluding trace."""

        return _sha256(_canonical_bytes(self.network_input()))

    @property
    def pbs_state_id(self) -> str:
        """Mathematical PBS identity: public node plus both reach factors."""

        return self.network_input_sha256

    def audit_provenance(self) -> dict[str, object]:
        """Path provenance kept out of the mathematical PBS/model features."""

        return {
            "pbs_state_id": self.pbs_state_id,
            "belief_update_trace": [
                {
                    "policy_kind": policy_kind,
                    "policy_sha256": policy_sha256,
                    "action_wire": action_wire,
                }
                for policy_kind, policy_sha256, action_wire in self.belief_update_trace
            ],
        }

    @property
    def provenance_snapshot_sha256(self) -> str:
        return _sha256(_canonical_bytes(self.audit_provenance()))

    def snapshot(self) -> dict[str, object]:
        return {
            "network_input": self.network_input(),
            "network_input_sha256": self.network_input_sha256,
            "pbs_state_id": self.pbs_state_id,
            "audit_provenance": self.audit_provenance(),
            "provenance_snapshot_sha256": self.provenance_snapshot_sha256,
            "diagnostics": {
                "board_legal_combo_count": self.board_legal_combo_count,
                "compatible_ordered_joint_count": self.compatible_ordered_joint_count,
                "joint_normalizer": self._joint_normalizer,
                "projected_marginals": [
                    list(self._projected_marginals[0]),
                    list(self._projected_marginals[1]),
                ],
                "legal_mask": list(self.legal_mask()),
                "positive_reach_mask": [
                    list(self.positive_reach_mask(0)),
                    list(self.positive_reach_mask(1)),
                ],
                "label_valid_mask": [
                    list(self.label_valid_mask(0)),
                    list(self.label_valid_mask(1)),
                ],
                "joint_materialized_as_asset": False,
            },
        }


def validate_hunl_network_input(
    payload: object,
) -> HUNLReachFactorPublicBeliefState:
    """Reconstruct an exact mathematical PBS from its later-net input."""

    if not isinstance(payload, dict) or set(payload) != HUNL_NETWORK_INPUT_FIELDS:
        raise ValueError("HUNL network input fields differ")
    if (
        payload["schema"] != HUNL_PBS_SCHEMA
        or payload["representation"]
        != "two_player_reach_factors_with_derived_joint"
        or payload["combo_order"] != HUNL_COMBO_ORDER
        or payload["combo_registry_sha256"] != HUNL_COMBO_REGISTRY_SHA256
        or payload["common_contract_version"] != CONTRACT_VERSION
    ):
        raise ValueError("HUNL network input contract identity differs")
    for flag in (
        "private_hole_cards_in_input",
        "sampled_deal_in_input",
        "match_context_in_input",
    ):
        if payload[flag] is not False:
            raise ValueError(f"HUNL network input requires {flag}=false")
    if not _is_sha256(payload["public_pbs_state_id"]):
        raise ValueError("HUNL network input public-state digest is invalid")
    public_state = payload["public_state"]
    if not isinstance(public_state, dict):
        raise ValueError("HUNL network input public state is invalid")
    factors = payload["reach_factors"]
    if (
        not isinstance(factors, list)
        or len(factors) != 2
        or any(not isinstance(row, list) for row in factors)
    ):
        raise ValueError("HUNL network input reach factors are invalid")
    pbs = HUNLReachFactorPublicBeliefState(
        _canonical_bytes(public_state).decode("utf-8"),
        (tuple(factors[0]), tuple(factors[1])),
    )
    if pbs.public_pbs_state_id != payload["public_pbs_state_id"]:
        raise ValueError("HUNL network input public-state digest differs")
    if pbs.network_input() != payload:
        raise ValueError("HUNL network input differs after strict reconstruction")
    return pbs
