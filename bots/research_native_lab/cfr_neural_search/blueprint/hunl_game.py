"""Finite real-stack national HUNL training game for Route B.

Betting transitions, legality, four-street closure, all-in runouts and terminal
utility are delegated to the shared Common ``NationalGameState``.  This module
only exposes sequential chance dealing and the versioned Route-B abstraction to
the generic extensive-form solver.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar, Mapping

from bots.research_native_lab.common_contracts import NationalGameState, Street
from bots.research_native_lab.common_contracts.constants import (
    BIG_BLIND,
    HANDS_PER_MATCH,
    INITIAL_CHIPS,
    SMALL_BLIND,
)

from ..core.game import Action as GameAction
from ..core.game import CHANCE_PLAYER, TERMINAL_PLAYER
from ..core.identity import GAME_IDENTITY_SCHEMA, file_sha256, payload_sha256
from ..core.strict_io import stable_tree_manifest
from ..native_runtime.common_adapter import (
    COMMON_CONTRACT_COMMIT,
    COMMON_CONTRACT_GIT_TREE,
    COMMON_RUNTIME_FILE_SHA256,
)
from .hunl_abstraction import (
    ACTION_ABSTRACTION_VERSION,
    CARD_ABSTRACTION_VERSION,
    EQUITY_SAMPLER_VERSION,
    INFORMATION_SCHEMA_VERSION,
    HUNLAbstractionConfig,
    abstraction_asset_sha256,
    information_descriptor,
    legal_action_map,
)


HUNL_GAME_VERSION = "route-b-national-hunl-v1"
UTILITY_VERSION = "net-chips-divided-by-big-blind-v1"
COMMON_MANIFEST_SCHEMA = "route-b-common-source-manifest-v1"


def _regular_source_manifest(root: Path) -> dict[str, str]:
    """Hash every source/data file while rejecting path substitution."""

    result = stable_tree_manifest(root)
    if not result:
        raise ValueError(f"dependency source manifest is empty: {root}")
    return result


def common_dependency_payload() -> dict[str, Any]:
    """Return the checked Common commit/tree plus live complete file manifest."""

    common_root = Path(__file__).parents[2] / "common_contracts"
    files = _regular_source_manifest(common_root)
    expected = dict(COMMON_RUNTIME_FILE_SHA256)
    actual_critical = {name: files.get(name) for name in expected}
    if actual_critical != expected:
        raise ValueError("Common critical files drifted from the audited adapter binding")
    return {
        "schema": COMMON_MANIFEST_SCHEMA,
        "commit": COMMON_CONTRACT_COMMIT,
        "git_tree": COMMON_CONTRACT_GIT_TREE,
        "critical_files": expected,
        "complete_manifest_sha256": payload_sha256({"files": files}),
        "complete_file_count": len(files),
    }


def _game_source_hashes() -> dict[str, str]:
    route_root = Path(__file__).parents[1]
    paths = (
        Path(__file__),
        route_root / "blueprint" / "hunl_abstraction.py",
        route_root / "core" / "game.py",
        route_root / "core" / "identity.py",
        route_root / "native_runtime" / "common_adapter.py",
    )
    return {
        path.relative_to(route_root).as_posix(): file_sha256(path)
        for path in paths
    }


@dataclass(frozen=True, slots=True)
class HUNLTrainingState:
    """Immutable sequential wrapper around one exact Common national hand."""

    abstraction: HUNLAbstractionConfig
    small_blind: int | None = None
    private_deal: tuple[int, ...] = ()
    common_state: NationalGameState | None = None
    pending_board: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if type(self.abstraction) is not HUNLAbstractionConfig:
            raise TypeError("abstraction must be exact HUNLAbstractionConfig")
        if self.small_blind is not None and (
            type(self.small_blind) is not int or self.small_blind not in (0, 1)
        ):
            raise ValueError("small_blind must be exact int 0/1 or None")
        if type(self.private_deal) is not tuple or type(self.pending_board) is not tuple:
            raise TypeError("chance card buffers must be tuples")
        known = self.private_deal + self.pending_board
        if any(type(card) is not int or not 0 <= card < 52 for card in known):
            raise ValueError("chance cards must be exact integers in 0..51")
        if len(set(known)) != len(known):
            raise ValueError("duplicate chance card")
        if self.common_state is not None:
            if type(self.common_state) is not NationalGameState:
                raise TypeError("common_state must be exact Common NationalGameState")
            self.common_state.assert_invariants()
            if self.small_blind != self.common_state.small_blind:
                raise ValueError("wrapper blind disagrees with Common state")
            holes = self._hole_cards()
            if self.common_state.hole_cards != holes:
                raise ValueError("wrapper private deal disagrees with Common state")
            all_known = known + self.common_state.board
            if len(set(all_known)) != len(all_known):
                raise ValueError("wrapper cards conflict with Common board")
        elif self.pending_board:
            raise ValueError("board cards cannot precede Common hand construction")

    def _hole_cards(self) -> tuple[tuple[int, ...], tuple[int, ...]]:
        if len(self.private_deal) != 4:
            return ((), ())
        # Round-robin physical deal: P0, P1, P0, P1.
        return (
            tuple(sorted((self.private_deal[0], self.private_deal[2]))),
            tuple(sorted((self.private_deal[1], self.private_deal[3]))),
        )

    @property
    def depth(self) -> int:
        actions = 0 if self.common_state is None else len(self.common_state.hand_history)
        board = 0 if self.common_state is None else len(self.common_state.board)
        return (
            int(self.small_blind is not None)
            + len(self.private_deal)
            + board
            + len(self.pending_board)
            + actions
        )

    @property
    def current_player(self) -> int:
        if self.small_blind is None or len(self.private_deal) < 4:
            return CHANCE_PLAYER
        if self.common_state is None:
            raise RuntimeError("four private cards must construct the Common hand")
        if self.common_state.is_terminal:
            return TERMINAL_PLAYER
        if self.common_state.chance_pending:
            return CHANCE_PLAYER
        if self.common_state.actor not in (0, 1):
            raise RuntimeError("Common nonterminal state has no actor")
        return int(self.common_state.actor)

    def _used_cards(self) -> set[int]:
        used = set(self.private_deal)
        used.update(self.pending_board)
        if self.common_state is not None:
            used.update(self.common_state.board)
        return used

    def chance_outcomes(self) -> tuple[tuple[GameAction, float], ...]:
        if self.current_player != CHANCE_PLAYER:
            raise ValueError("not a HUNL chance node")
        if self.small_blind is None:
            return (("small_blind:0", 0.5), ("small_blind:1", 0.5))
        available = tuple(card for card in range(52) if card not in self._used_cards())
        probability = 1.0 / len(available)
        return tuple((card, probability) for card in available)

    def legal_actions(self) -> tuple[GameAction, ...]:
        if self.current_player < 0:
            return ()
        assert self.common_state is not None
        return tuple(item.label for item in legal_action_map(self.common_state))

    def child(self, action: GameAction) -> "HUNLTrainingState":
        if self.current_player == CHANCE_PLAYER:
            legal = tuple(value for value, _ in self.chance_outcomes())
            if action not in legal:
                raise ValueError(f"illegal HUNL chance action: {action!r}")
            if self.small_blind is None:
                if type(action) is not str or action not in {
                    "small_blind:0",
                    "small_blind:1",
                }:
                    raise TypeError("blind chance action must be an exact label")
                return replace(self, small_blind=int(action[-1]))
            if type(action) is not int:
                raise TypeError("card chance action must be an exact integer")
            if len(self.private_deal) < 4:
                private_deal = self.private_deal + (action,)
                if len(private_deal) < 4:
                    return replace(self, private_deal=private_deal)
                holes = (
                    (private_deal[0], private_deal[2]),
                    (private_deal[1], private_deal[3]),
                )
                common = NationalGameState.new_hand(
                    1,
                    small_blind=int(self.small_blind),
                    hole_cards=holes,
                )
                return replace(
                    self,
                    private_deal=private_deal,
                    common_state=common,
                )
            assert self.common_state is not None
            pending = self.pending_board + (action,)
            needed = 3 if self.common_state.street is Street.PREFLOP else 1
            if len(pending) < needed:
                return replace(self, pending_board=pending)
            if len(pending) != needed:
                raise RuntimeError("chance buffer exceeded next-street card count")
            common = self.common_state.apply_chance(pending)
            return replace(self, common_state=common, pending_board=())

        if type(action) is not str:
            raise TypeError("HUNL decision action must be an exact string label")
        assert self.common_state is not None
        mapped = {item.label: item.common_action for item in legal_action_map(self.common_state)}
        if action not in mapped:
            raise ValueError(f"illegal HUNL decision action: {action!r}")
        return replace(self, common_state=self.common_state.apply_action(mapped[action]))

    def information_state_key(self, player: int) -> str:
        if type(player) is not int or player not in (0, 1):
            raise ValueError("HUNL information player must be exact int 0/1")
        if self.current_player != player or self.common_state is None:
            raise ValueError("HUNL information key requires the acting player")
        return information_descriptor(
            self.common_state,
            player,
            self.abstraction,
        ).exact_key

    def returns(self) -> tuple[float, float]:
        if self.current_player != TERMINAL_PLAYER or self.common_state is None:
            raise ValueError("HUNL returns require a terminal Common state")
        utility = self.common_state.terminal_utility()
        result = (utility[0] / BIG_BLIND, utility[1] / BIG_BLIND)
        if abs(result[0] + result[1]) > 1e-12:
            raise RuntimeError("HUNL normalized utility is not zero-sum")
        return result


@dataclass(frozen=True, slots=True)
class HUNLTrainingGame:
    """Real 20k/50/100 national HUNL with versioned Route-B abstraction."""

    abstraction: HUNLAbstractionConfig = HUNLAbstractionConfig()
    name: ClassVar[str] = "route_b_national_hunl"

    def __post_init__(self) -> None:
        if type(self.abstraction) is not HUNLAbstractionConfig:
            raise TypeError("abstraction must be exact HUNLAbstractionConfig")

    def new_initial_state(self) -> HUNLTrainingState:
        return HUNLTrainingState(self.abstraction)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema": GAME_IDENTITY_SCHEMA,
            "game": self.name,
            "game_version": HUNL_GAME_VERSION,
            "rules": {
                "players": 2,
                "initial_chips": INITIAL_CHIPS,
                "small_blind": SMALL_BLIND,
                "big_blind": BIG_BLIND,
                "hands_per_match": HANDS_PER_MATCH,
                "streets": [street.value for street in Street],
                "chance_deal": "blind-uniform; round-robin private without replacement; flop3-turn1-river1",
                "legality_and_transition": "exact Common NationalGameState",
            },
            "abstraction": {
                "config": self.abstraction.to_payload(),
                "card_version": CARD_ABSTRACTION_VERSION,
                "action_version": ACTION_ABSTRACTION_VERSION,
                "information_schema": INFORMATION_SCHEMA_VERSION,
                "equity_sampler": EQUITY_SAMPLER_VERSION,
                "asset_sha256": abstraction_asset_sha256(self.abstraction),
            },
            "utility": {
                "version": UTILITY_VERSION,
                "divisor": BIG_BLIND,
                "source": "Common NationalGameState.terminal_utility",
            },
            "common": common_dependency_payload(),
            "sources": _game_source_hashes(),
        }

    def identity_sha256(self) -> str:
        return payload_sha256(self.identity_payload())


def hunl_component_identities(game: HUNLTrainingGame) -> Mapping[str, str]:
    """Expose explicit merge/checkpoint component identities."""

    if type(game) is not HUNLTrainingGame:
        raise TypeError("game must be exact HUNLTrainingGame")
    payload = game.identity_payload()
    return {
        "game_sha256": payload_sha256(payload),
        "rules_sha256": payload_sha256(payload["rules"]),
        "card_abstraction_sha256": payload_sha256(
            {
                "config": payload["abstraction"]["config"],
                "card_version": payload["abstraction"]["card_version"],
                "information_schema": payload["abstraction"]["information_schema"],
                "equity_sampler": payload["abstraction"]["equity_sampler"],
            }
        ),
        "action_abstraction_sha256": payload_sha256(
            {"action_version": payload["abstraction"]["action_version"]}
        ),
        "abstraction_asset_sha256": str(payload["abstraction"]["asset_sha256"]),
        "common_dependency_sha256": payload_sha256(payload["common"]),
        "utility_sha256": payload_sha256(payload["utility"]),
        "source_sha256": payload_sha256(payload["sources"]),
    }
