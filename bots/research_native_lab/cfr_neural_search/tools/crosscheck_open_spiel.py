"""Cross-check route-B small games against an independently installed OpenSpiel.

OpenSpiel is an audit-only optional dependency.  It is never imported by the
solver or native runtime.  Run this tool in an isolated environment that has
the desired official wheel installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from importlib import metadata

from bots.research_native_lab.cfr_neural_search.blueprint.evaluation import (
    expected_returns,
    exploitability,
)
from bots.research_native_lab.cfr_neural_search.blueprint.mccfr import (
    SolverConfig,
    SolverState,
    average_policy,
    train_batches,
)
from bots.research_native_lab.cfr_neural_search.blueprint.small_games import (
    CALL,
    CHECK,
    FOLD,
    RAISE,
    KuhnPoker,
    LeducPoker,
)
from bots.research_native_lab.cfr_neural_search.core.game import (
    Action,
    CHANCE_PLAYER,
    TERMINAL_PLAYER,
)


def _route_statistics(game) -> dict[str, object]:
    states = 0
    terminals = 0
    chance = 0
    decisions = 0
    information_states = [set(), set()]

    def visit(state) -> None:
        nonlocal states, terminals, chance, decisions
        states += 1
        actor = state.current_player
        if actor == TERMINAL_PLAYER:
            terminals += 1
            return
        if actor == CHANCE_PLAYER:
            chance += 1
            for action, _ in state.chance_outcomes():
                visit(state.child(action))
            return
        decisions += 1
        information_states[actor].add(state.information_state_key(actor))
        for action in state.legal_actions():
            visit(state.child(action))

    visit(game.new_initial_state())
    value = expected_returns(game, {})
    return {
        "states": states,
        "terminals": terminals,
        "chance": chance,
        "decisions": decisions,
        "information_states": [len(items) for items in information_states],
        "uniform_value": list(value),
        "uniform_exploitability": exploitability(game, {}).exploitability,
    }


def _open_spiel_statistics(game) -> dict[str, object]:
    from open_spiel.python import policy
    from open_spiel.python.algorithms import expected_game_score
    from open_spiel.python.algorithms import exploitability as spiel_exploitability

    states = 0
    terminals = 0
    chance = 0
    decisions = 0
    information_states = [set(), set()]

    def visit(state) -> None:
        nonlocal states, terminals, chance, decisions
        states += 1
        if state.is_terminal():
            terminals += 1
            return
        if state.is_chance_node():
            chance += 1
        else:
            decisions += 1
            information_states[state.current_player()].add(
                state.information_state_string()
            )
        for action in state.legal_actions():
            visit(state.child(action))

    visit(game.new_initial_state())
    uniform = policy.TabularPolicy(game)
    value = expected_game_score.policy_value(
        game.new_initial_state(),
        [uniform, uniform],
    )
    return {
        "states": states,
        "terminals": terminals,
        "chance": chance,
        "decisions": decisions,
        "information_states": [len(items) for items in information_states],
        "uniform_value": [float(item) for item in value],
        "uniform_exploitability": float(
            spiel_exploitability.exploitability(game, uniform)
        ),
    }


def _deterministic_nonuniform_policy(game) -> dict[str, dict[Action, float]]:
    action_sets: dict[str, tuple[Action, ...]] = {}

    def visit(state) -> None:
        actor = state.current_player
        if actor == TERMINAL_PLAYER:
            return
        if actor == CHANCE_PLAYER:
            for action, _ in state.chance_outcomes():
                visit(state.child(action))
            return
        key = state.information_state_key(actor)
        action_sets[key] = state.legal_actions()
        for action in state.legal_actions():
            visit(state.child(action))

    visit(game.new_initial_state())
    result: dict[str, dict[Action, float]] = {}
    for key, actions in sorted(action_sets.items()):
        weights = {
            action: 1
            + int.from_bytes(
                hashlib.sha256(f"{key}|{action}".encode("utf-8")).digest()[:4],
                "big",
            )
            % 1000
            for action in actions
        }
        total = float(sum(weights.values()))
        result[key] = {action: weight / total for action, weight in weights.items()}
    return result


def _open_spiel_leduc_policy(game, route_policy):
    from open_spiel.python import policy

    result = policy.TabularPolicy(game)
    route_game = LeducPoker()

    def visit(spiel_state, route_state) -> None:
        if spiel_state.is_terminal():
            if route_state.current_player != TERMINAL_PLAYER:
                raise ValueError("OpenSpiel/route terminal mismatch")
            return
        if spiel_state.is_chance_node():
            if route_state.current_player != CHANCE_PLAYER:
                raise ValueError("OpenSpiel/route chance mismatch")
            spiel_outcomes = spiel_state.chance_outcomes()
            route_outcomes = route_state.chance_outcomes()
            if [item[0] for item in spiel_outcomes] != [item[0] for item in route_outcomes]:
                raise ValueError("OpenSpiel/route chance actions differ")
            for action, _ in spiel_outcomes:
                visit(spiel_state.child(action), route_state.child(action))
            return

        actor = route_state.current_player
        if spiel_state.current_player() != actor:
            raise ValueError("OpenSpiel/route actor mismatch")
        key = route_state.information_state_key(actor)
        row = result.state_lookup[spiel_state.information_state_string()]
        for spiel_action in spiel_state.legal_actions():
            if spiel_action == 0:
                route_action = FOLD
            elif spiel_action == 2:
                route_action = RAISE
            elif CHECK in route_state.legal_actions():
                route_action = CHECK
            else:
                route_action = CALL
            if route_action not in route_state.legal_actions():
                raise ValueError("OpenSpiel/route legal actions differ")
            result.action_probability_array[row, spiel_action] = route_policy[key][
                route_action
            ]
            visit(
                spiel_state.child(spiel_action),
                route_state.child(route_action),
            )

    visit(game.new_initial_state(), route_game.new_initial_state())
    return result


def _nonuniform_leduc_crosscheck(game) -> dict[str, object]:
    from open_spiel.python.algorithms import expected_game_score
    from open_spiel.python.algorithms import exploitability as spiel_exploitability

    route_game = LeducPoker()
    route_policy = _deterministic_nonuniform_policy(route_game)
    spiel_policy = _open_spiel_leduc_policy(game, route_policy)
    route_value = expected_returns(route_game, route_policy)
    route_exploitability = exploitability(route_game, route_policy)
    spiel_value = expected_game_score.policy_value(
        game.new_initial_state(),
        [spiel_policy, spiel_policy],
    )
    return {
        "route_value": list(route_value),
        "open_spiel_value": [float(item) for item in spiel_value],
        "route_best_response_values": list(route_exploitability.best_response_values),
        "route_nash_conv": route_exploitability.nash_conv,
        "open_spiel_nash_conv": float(
            spiel_exploitability.nash_conv(game, spiel_policy)
        ),
        "route_exploitability": route_exploitability.exploitability,
        "open_spiel_exploitability": float(
            spiel_exploitability.exploitability(game, spiel_policy)
        ),
    }


def _trained_leduc_crosscheck(game) -> dict[str, object]:
    """Evaluate one frozen MCCFR result with OpenSpiel's independent BR code."""

    from open_spiel.python.algorithms import expected_game_score
    from open_spiel.python.algorithms import exploitability as spiel_exploitability

    route_game = LeducPoker()
    config = SolverConfig(
        update_rule="linear",
        averaging_mode="sampled",
        seed=23,
        samples_per_player=1,
    )
    state = SolverState(route_game.name, config)
    train_batches(route_game, state, batches=500, shard_count=1)
    route_policy = average_policy(state)
    if len(route_policy) != 288:
        raise ValueError(
            "frozen trained cross-check did not discover all 288 route infosets"
        )
    spiel_policy = _open_spiel_leduc_policy(game, route_policy)
    route_value = expected_returns(route_game, route_policy)
    route_result = exploitability(route_game, route_policy)
    spiel_value = expected_game_score.policy_value(
        game.new_initial_state(),
        [spiel_policy, spiel_policy],
    )
    spiel_nash_conv = float(spiel_exploitability.nash_conv(game, spiel_policy))
    return {
        "algorithm": "synchronous_external_sampling_mccfr",
        "update_rule": config.update_rule,
        "seed": config.seed,
        "batches": 500,
        "samples_per_player": config.samples_per_player,
        "shards": 1,
        "trajectories": state.trajectories,
        "information_states": len(route_policy),
        "state_sha256": state.digest,
        "route_value": list(route_value),
        "open_spiel_value": [float(item) for item in spiel_value],
        "route_nash_conv": route_result.nash_conv,
        "open_spiel_nash_conv": spiel_nash_conv,
        "route_exploitability": route_result.exploitability,
        "open_spiel_exploitability": spiel_nash_conv / 2.0,
    }


def _close(first: float, second: float) -> bool:
    return abs(first - second) <= 1e-12


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-version")
    args = parser.parse_args(argv)

    try:
        import pyspiel
    except ImportError as exc:
        raise SystemExit("OpenSpiel is not installed in this audit environment") from exc

    version = metadata.version("open-spiel")
    if args.expected_version is not None and version != args.expected_version:
        raise SystemExit(
            f"OpenSpiel version mismatch: expected {args.expected_version}, got {version}"
        )

    route_kuhn = _route_statistics(KuhnPoker())
    route_leduc = _route_statistics(LeducPoker())
    spiel_kuhn = _open_spiel_statistics(pyspiel.load_game("kuhn_poker"))
    spiel_leduc_physical = _open_spiel_statistics(
        pyspiel.load_game("leduc_poker", {"suit_isomorphism": False})
    )
    spiel_leduc_isomorphic = _open_spiel_statistics(
        pyspiel.load_game("leduc_poker", {"suit_isomorphism": True})
    )
    nonuniform_leduc = _nonuniform_leduc_crosscheck(
        pyspiel.load_game("leduc_poker", {"suit_isomorphism": False})
    )
    trained_leduc = _trained_leduc_crosscheck(
        pyspiel.load_game("leduc_poker", {"suit_isomorphism": False})
    )

    checks = {
        "kuhn_tree": all(
            route_kuhn[key] == spiel_kuhn[key]
            for key in ("states", "terminals", "information_states")
        ),
        "kuhn_uniform": all(
            _close(float(route_kuhn["uniform_value"][index]), float(spiel_kuhn["uniform_value"][index]))
            for index in (0, 1)
        )
        and _close(
            float(route_kuhn["uniform_exploitability"]),
            float(spiel_kuhn["uniform_exploitability"]),
        ),
        "leduc_physical_tree": all(
            route_leduc[key] == spiel_leduc_physical[key]
            for key in ("states", "terminals")
        ),
        "leduc_rank_information_abstraction": (
            route_leduc["information_states"]
            == spiel_leduc_isomorphic["information_states"]
        ),
        "leduc_uniform": all(
            _close(
                float(route_leduc["uniform_value"][index]),
                float(spiel_leduc_physical["uniform_value"][index]),
            )
            for index in (0, 1)
        )
        and _close(
            float(route_leduc["uniform_exploitability"]),
            float(spiel_leduc_physical["uniform_exploitability"]),
        ),
        "leduc_nonuniform_value": all(
            _close(
                float(nonuniform_leduc["route_value"][index]),
                float(nonuniform_leduc["open_spiel_value"][index]),
            )
            for index in (0, 1)
        ),
        "leduc_nonuniform_br_exploitability": _close(
            float(nonuniform_leduc["route_nash_conv"]),
            float(nonuniform_leduc["open_spiel_nash_conv"]),
        )
        and _close(
            float(nonuniform_leduc["route_exploitability"]),
            float(nonuniform_leduc["open_spiel_exploitability"]),
        ),
        "leduc_trained_policy_value": all(
            _close(
                float(trained_leduc["route_value"][index]),
                float(trained_leduc["open_spiel_value"][index]),
            )
            for index in (0, 1)
        ),
        "leduc_trained_policy_br_exploitability": _close(
            float(trained_leduc["route_nash_conv"]),
            float(trained_leduc["open_spiel_nash_conv"]),
        )
        and _close(
            float(trained_leduc["route_exploitability"]),
            float(trained_leduc["open_spiel_exploitability"]),
        ),
        "leduc_trained_policy_improves_uniform": (
            float(trained_leduc["open_spiel_exploitability"])
            < float(spiel_leduc_physical["uniform_exploitability"])
        ),
    }
    payload = {
        "open_spiel_version": version,
        "route": {"kuhn": route_kuhn, "leduc": route_leduc},
        "open_spiel": {
            "kuhn": spiel_kuhn,
            "leduc_physical": spiel_leduc_physical,
            "leduc_suit_isomorphic": spiel_leduc_isomorphic,
        },
        "nonuniform_leduc": nonuniform_leduc,
        "trained_leduc": trained_leduc,
        "checks": checks,
        "passed": all(checks.values()),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
