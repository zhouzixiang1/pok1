"""Exact joint-deal public belief state for Kuhn poker.

ReBeL's paper defines a PBS over possible infostates, not merely a public action
string.  For Kuhn poker the six legal private deals are small enough to retain
their exact joint distribution.  Keeping the joint distribution avoids the
false independence introduced by storing only two marginal ranges.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Mapping

from ..common_runtime.evaluation import deal_expected_utility
from ..common_runtime.kuhn import (
    CARDS,
    Deal,
    StrategyProfile,
    current_player,
    is_terminal,
    legal_actions,
    next_history,
    ordered_deals,
)

CardPolicy = Mapping[int, Mapping[str, float]]


@dataclass(frozen=True, slots=True)
class KuhnPublicBeliefState:
    """A normalized public belief over every legal ordered private-card deal."""

    history: str
    deal_probabilities: dict[Deal, float]

    def __post_init__(self) -> None:
        legal_deals = set(ordered_deals())
        if set(self.deal_probabilities) != legal_deals:
            raise ValueError("PBS must contain exactly the six legal Kuhn deals")
        probabilities = tuple(self.deal_probabilities.values())
        if any(not isfinite(value) or value < 0.0 for value in probabilities):
            raise ValueError("PBS probabilities must be finite and non-negative")
        if abs(sum(probabilities) - 1.0) > 1e-12:
            raise ValueError("PBS probabilities must sum to one")
        if not is_terminal(self.history):
            legal_actions(self.history)  # validates the public history

    @classmethod
    def initial(cls) -> "KuhnPublicBeliefState":
        deals = ordered_deals()
        probability = 1.0 / len(deals)
        return cls("", {deal: probability for deal in deals})

    def range_for(self, player: int) -> dict[int, float]:
        if player not in (0, 1):
            raise ValueError(f"invalid player: {player}")
        result = {card: 0.0 for card in CARDS}
        for deal, probability in self.deal_probabilities.items():
            result[deal[player]] += probability
        return result

    def conditional_opponent_range(
        self, player: int, own_card: int
    ) -> dict[int, float]:
        if player not in (0, 1):
            raise ValueError(f"invalid player: {player}")
        if own_card not in CARDS:
            raise ValueError(f"invalid card: {own_card}")
        mass = sum(
            probability
            for deal, probability in self.deal_probabilities.items()
            if deal[player] == own_card
        )
        if mass <= 0.0:
            raise ValueError(f"own card has zero reach in PBS: {own_card}")
        opponent = 1 - player
        result = {card: 0.0 for card in CARDS}
        for deal, probability in self.deal_probabilities.items():
            if deal[player] == own_card:
                result[deal[opponent]] += probability / mass
        return result

    def action_probability(self, action: str, policy: CardPolicy) -> float:
        if is_terminal(self.history):
            raise ValueError("cannot act from a terminal PBS")
        if action not in legal_actions(self.history):
            raise ValueError(f"illegal action {action!r} at {self.history!r}")
        actor = current_player(self.history)
        expected_actions = set(legal_actions(self.history))
        for card in CARDS:
            card_probabilities = policy.get(card)
            if card_probabilities is None:
                raise ValueError(f"action policy is missing card {card}")
            if set(card_probabilities) != expected_actions:
                raise ValueError(
                    f"action policy for card {card} does not match legal actions"
                )
            if any(
                not isfinite(value) or value < 0.0 or value > 1.0
                for value in card_probabilities.values()
            ):
                raise ValueError(f"invalid action probabilities for card {card}")
            if abs(sum(card_probabilities.values()) - 1.0) > 1e-12:
                raise ValueError(f"action probabilities do not sum to one for card {card}")
        probability = 0.0
        for deal, reach in self.deal_probabilities.items():
            likelihood = policy[deal[actor]][action]
            probability += reach * likelihood
        return probability

    def observe(
        self, action: str, policy: CardPolicy
    ) -> "KuhnPublicBeliefState":
        """Apply an observed public action with an exact Bayes update."""

        evidence = self.action_probability(action, policy)
        if evidence <= 0.0:
            raise ValueError("observed action has zero probability under the policy")
        actor = current_player(self.history)
        posterior = {
            deal: reach * policy[deal[actor]][action] / evidence
            for deal, reach in self.deal_probabilities.items()
        }
        return KuhnPublicBeliefState(next_history(self.history, action), posterior)

    def on_policy_infostate_values(
        self, profile: StrategyProfile
    ) -> dict[int, dict[int, float]]:
        """Return exact on-policy continuation values by player and private card.

        These are a deterministic label oracle for the PBS plumbing milestone.
        They are intentionally named *on-policy* values: the full ReBeL
        counterfactual-value/search target is a later stage gate.
        """

        result: dict[int, dict[int, float]] = {0: {}, 1: {}}
        for player in (0, 1):
            marginal = self.range_for(player)
            for card, mass in marginal.items():
                if mass <= 0.0:
                    continue
                value = 0.0
                for deal, probability in self.deal_probabilities.items():
                    if deal[player] == card:
                        value += (
                            probability
                            / mass
                            * deal_expected_utility(
                                deal, self.history, profile, player=player
                            )
                        )
                result[player][card] = value
        return result

    def snapshot(self) -> dict[str, object]:
        return {
            "history": self.history,
            "deal_probabilities": {
                f"{deal[0]}:{deal[1]}": probability
                for deal, probability in sorted(self.deal_probabilities.items())
            },
            "ranges": {
                str(player): {
                    str(card): probability
                    for card, probability in self.range_for(player).items()
                }
                for player in (0, 1)
            },
        }
