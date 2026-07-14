"""Exact public-belief and counterfactual-label oracle for limit Leduc.

The learnable ReBeL representation is a tuple of player ranges.  In this small
game gate we retain the complete 120-deal posterior as ground truth and expose
its two private-rank marginals.  The exact joint is intentional: it makes card
removal, public-card conditioning and changes to either player's range
falsifiable instead of pretending that two marginals uniquely encode blockers.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from ..common_runtime.leduc import (
    LeducDeal,
    LeducState,
    LeducStrategy,
    apply_action,
    card_rank,
    information_set,
    initial_state,
    legal_actions,
    ordered_deals,
    terminal_utility,
    validate_strategy,
)


def _continuation_value(
    state: LeducState,
    deal: LeducDeal,
    profile: LeducStrategy,
    player: int,
) -> float:
    if state.terminal:
        return terminal_utility(state, deal, player)
    key = information_set(state, deal)
    return sum(
        profile[key][action]
        * _continuation_value(apply_action(state, action), deal, profile, player)
        for action in legal_actions(state)
    )


@dataclass(frozen=True, slots=True)
class LeducPublicBeliefState:
    """Exact posterior over physical private/private/public Leduc deals."""

    state: LeducState
    public_rank: int | None
    deal_probabilities: dict[LeducDeal, float]

    def __post_init__(self) -> None:
        if set(self.deal_probabilities) != set(ordered_deals()):
            raise ValueError("Leduc PBS must contain all 120 physical deals")
        probabilities = tuple(self.deal_probabilities.values())
        if any(not isfinite(value) or value < 0.0 for value in probabilities):
            raise ValueError("Leduc PBS probabilities must be finite and non-negative")
        if abs(sum(probabilities) - 1.0) > 1e-12:
            raise ValueError("Leduc PBS probabilities must sum to one")
        if self.state.street == 0:
            if self.public_rank is not None:
                raise ValueError("preflop Leduc PBS cannot expose a public rank")
        elif self.public_rank is not None:
            if type(self.public_rank) is not int or not 0 <= self.public_rank < 3:
                raise ValueError("public Leduc rank must be 0, 1 or 2")
            if any(
                probability > 0.0 and card_rank(deal[2]) != self.public_rank
                for deal, probability in self.deal_probabilities.items()
            ):
                raise ValueError("positive PBS mass conflicts with the public rank")

    @classmethod
    def initial(cls) -> "LeducPublicBeliefState":
        deals = ordered_deals()
        chance = 1.0 / len(deals)
        return cls(initial_state(), None, {deal: chance for deal in deals})

    @property
    def chance_pending(self) -> bool:
        return self.state.street == 1 and self.public_rank is None and not self.state.terminal

    def range_for(self, player: int) -> dict[int, float]:
        if player not in (0, 1):
            raise ValueError("player must be 0 or 1")
        result = {rank: 0.0 for rank in range(3)}
        for deal, probability in self.deal_probabilities.items():
            result[card_rank(deal[player])] += probability
        return result

    def action_probability(self, action: str, profile: LeducStrategy) -> float:
        validate_strategy(profile)
        if self.state.terminal:
            raise ValueError("terminal Leduc PBS has no action")
        if self.chance_pending:
            raise ValueError("public rank must be observed before postflop action")
        if action not in legal_actions(self.state):
            raise ValueError(f"illegal Leduc action {action!r}")
        return sum(
            probability * profile[information_set(self.state, deal)][action]
            for deal, probability in self.deal_probabilities.items()
        )

    def observe_action(
        self,
        action: str,
        profile: LeducStrategy,
    ) -> "LeducPublicBeliefState":
        """Bayes-condition the exact deal posterior on one public action."""

        evidence = self.action_probability(action, profile)
        if evidence <= 0.0:
            raise ValueError("observed Leduc action has zero probability")
        posterior = {
            deal: probability
            * profile[information_set(self.state, deal)][action]
            / evidence
            for deal, probability in self.deal_probabilities.items()
        }
        return LeducPublicBeliefState(
            apply_action(self.state, action),
            self.public_rank,
            posterior,
        )

    def observe_public_rank(self, rank: int) -> "LeducPublicBeliefState":
        """Condition both private ranges on the public chance observation."""

        if not self.chance_pending:
            raise ValueError("Leduc PBS is not awaiting a public rank")
        if type(rank) is not int or not 0 <= rank < 3:
            raise ValueError("public Leduc rank must be 0, 1 or 2")
        evidence = sum(
            probability
            for deal, probability in self.deal_probabilities.items()
            if card_rank(deal[2]) == rank
        )
        if evidence <= 0.0:
            raise ValueError("observed public rank has zero probability")
        posterior = {
            deal: (
                probability / evidence if card_rank(deal[2]) == rank else 0.0
            )
            for deal, probability in self.deal_probabilities.items()
        }
        return LeducPublicBeliefState(self.state, rank, posterior)

    def on_policy_private_values(
        self,
        profile: LeducStrategy,
    ) -> dict[int, dict[int, float]]:
        """Return exact continuation values conditional on each private rank."""

        validate_strategy(profile)
        if self.chance_pending:
            raise ValueError("value labels require an observed public rank")
        result: dict[int, dict[int, float]] = {0: {}, 1: {}}
        for player in (0, 1):
            for rank, mass in self.range_for(player).items():
                if mass <= 0.0:
                    continue
                result[player][rank] = sum(
                    probability
                    / mass
                    * _continuation_value(self.state, deal, profile, player)
                    for deal, probability in self.deal_probabilities.items()
                    if card_rank(deal[player]) == rank
                )
        return result

    def conditional_deviation_action_values(
        self,
        profile: LeducStrategy,
    ) -> dict[int, dict[str, float]]:
        """Posterior-normalized per-rank values after forcing each action.

        This is a conditional label oracle.  It is deliberately distinct from
        :meth:`cfr_counterfactual_action_values`, which omits own reach and is
        unnormalized as required by CFR.
        """

        validate_strategy(profile)
        if self.state.terminal or self.chance_pending:
            raise ValueError("counterfactual actions require a decision node")
        actor = self.state.actor
        result: dict[int, dict[str, float]] = {}
        for rank, mass in self.range_for(actor).items():
            if mass <= 0.0:
                continue
            result[rank] = {
                action: sum(
                    probability
                    / mass
                    * _continuation_value(
                        apply_action(self.state, action),
                        deal,
                        profile,
                        actor,
                    )
                    for deal, probability in self.deal_probabilities.items()
                    if card_rank(deal[actor]) == rank
                )
                for action in legal_actions(self.state)
            }
        return result

    def cfr_counterfactual_action_values(
        self,
        profile: LeducStrategy,
    ) -> dict[int, dict[str, float]]:
        """Return the current actor's standard unnormalized CFR action values."""

        validate_strategy(profile)
        if self.state.terminal or self.chance_pending:
            raise ValueError("counterfactual actions require a decision node")
        actor = self.state.actor
        chance = 1.0 / len(ordered_deals())

        def reaches(deal: LeducDeal) -> tuple[float, float]:
            cursor = initial_state()
            result = [1.0, 1.0]
            target_actions = tuple(
                token for token in self.state.history if token != "/"
            )
            for observed in target_actions:
                player = cursor.actor
                key = information_set(cursor, deal)
                result[player] *= profile[key][observed]
                cursor = apply_action(cursor, observed)
            if cursor.history != self.state.history:
                raise AssertionError("Leduc public history reach reconstruction diverged")
            return result[0], result[1]

        consistent_deals = tuple(
            deal
            for deal in ordered_deals()
            if self.public_rank is None or card_rank(deal[2]) == self.public_rank
        )
        profile_weights = {
            deal: chance * reaches(deal)[0] * reaches(deal)[1]
            for deal in consistent_deals
        }
        profile_reach = sum(profile_weights.values())
        if profile_reach <= 0.0:
            raise ValueError("profile has zero reach to the Leduc PBS")
        if any(
            abs(
                self.deal_probabilities[deal]
                - profile_weights.get(deal, 0.0) / profile_reach
            )
            > 1e-10
            for deal in ordered_deals()
        ):
            raise ValueError("Leduc PBS posterior does not match supplied profile reach")
        result: dict[int, dict[str, float]] = {}
        for rank in range(3):
            result[rank] = {
                action: sum(
                    chance
                    * reaches(deal)[1 - actor]
                    * _continuation_value(
                        apply_action(self.state, action),
                        deal,
                        profile,
                        actor,
                    )
                    for deal in consistent_deals
                    if card_rank(deal[actor]) == rank
                )
                for action in legal_actions(self.state)
            }
        return result

    def snapshot(self) -> dict[str, object]:
        return {
            "history": list(self.state.history),
            "street": self.state.street,
            "actor": None if self.state.terminal else self.state.actor,
            "public_rank": self.public_rank,
            "chance_pending": self.chance_pending,
            "ranges": {
                str(player): {
                    str(rank): probability
                    for rank, probability in self.range_for(player).items()
                }
                for player in (0, 1)
            },
        }
