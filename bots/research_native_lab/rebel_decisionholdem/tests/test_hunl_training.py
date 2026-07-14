from __future__ import annotations

import copy
import hashlib
import json
import math
import subprocess
import sys

import pytest

from ...common_contracts.national_state import NationalGameState
from ..decisionholdem_like import hunl_external_sampling as hunl_training_module
from ..decisionholdem_like.hunl_abstraction import (
    abstract_actions,
    information_abstraction,
    parse_infoset_key,
)
from ..decisionholdem_like.hunl_blueprint import (
    HUNL_TRAINED_BACKOFF_LEVELS,
    build_trained_backoff_policies,
    parse_trained_backoff_key,
)
from ..decisionholdem_like.hunl_external_sampling import (
    HUNLExternalSamplingLCFR,
    HUNLTrainingConfig,
    _Accumulator,
    deterministic_deal,
    linear_simple_average_delta,
    linear_regret_delta,
    regret_matching,
    strict_json_loads,
    training_identity_digest,
    training_identity_snapshot,
)
from ..tools.train_hunl_blueprint import (
    PACKAGE_ROOT,
    TrainingRunCancelled,
    load_config,
    load_config_payload,
    seed_independence_snapshot,
    select_training_candidate,
    train_and_export,
)
from ..tools import train_hunl_blueprint as training_tool


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _rehash_checkpoint(payload: dict[str, object]) -> None:
    payload["body_sha256"] = _digest(payload["body"])


def _rehash_shard(payload: dict[str, object]) -> None:
    payload["body_sha256"] = _digest(payload["body"])


@pytest.fixture(scope="module")
def trained_once() -> HUNLExternalSamplingLCFR:
    trainer = HUNLExternalSamplingLCFR(HUNLTrainingConfig(seed=731))
    trainer.train_to(1)
    return trainer


def test_linear_regret_equation_matches_an_independent_small_oracle() -> None:
    values = {"fold": -1.0, "check_call": 1.0, "allin": 3.0}
    strategy = {"fold": 0.2, "check_call": 0.3, "allin": 0.5}
    production = linear_regret_delta(values, strategy, 4)
    independent_node_value = -1.0 * 0.2 + 1.0 * 0.3 + 3.0 * 0.5
    independent = {
        action: 4.0 * (value - independent_node_value)
        for action, value in values.items()
    }
    assert production == pytest.approx(independent, abs=1e-14)


def test_counter_based_deals_are_replayable_complete_and_card_disjoint() -> None:
    first = deterministic_deal(99, 1)
    replay = deterministic_deal(99, 1)
    next_deal = deterministic_deal(99, 2)
    assert first == replay
    assert first != next_deal
    cards = first[0] + first[1] + first[2]
    assert len(cards) == len(set(cards)) == 9
    assert all(0 <= card < 52 for card in cards)


def test_real_hunl_iteration_keeps_regret_and_average_strategy_separate(
    trained_once,
) -> None:
    assert trained_once.iterations_completed == trained_once.sampled_deals == 1
    assert trained_once.traversals_completed == 2
    assert trained_once.nodes_visited > 100
    assert len(trained_once.regrets) > 100
    assert len(trained_once.strategy_sums) > 100
    assert trained_once.regrets is not trained_once.strategy_sums
    assert any(
        value < 0.0
        for row in trained_once.regrets.values()
        for value in row.values()
    )
    assert all(
        value >= 0.0
        for row in trained_once.strategy_sums.values()
        for value in row.values()
    )
    assert all(
        math.isclose(sum(row.values()), 1.0, abs_tol=1e-12)
        for row in trained_once.average_strategy().values()
    )


def test_fixed_hunl_trajectory_updates_simple_average_only_at_opponent_nodes() -> None:
    """Independent site/delta oracle for OpenSpiel-style simple averaging."""

    trainer = HUNLExternalSamplingLCFR(HUNLTrainingConfig(seed=2026071417))
    first_hole, second_hole, board = deterministic_deal(trainer.config.seed, 7)
    state = NationalGameState.new_hand(
        7,
        small_blind=0,
        hole_cards=(first_hole, second_hole),
    )
    root = information_abstraction(state, 0)
    root_actions = tuple(spec.action_id for spec in abstract_actions(state))
    assert "check_call" in root_actions
    # Force a nonterminal sampled opponent action at the root.  Player 1 is
    # the traverser, so a simple average must record player 0's root policy.
    root_regrets = {
        action: (1.0 if action == "check_call" else 0.0)
        for action in root_actions
    }
    accumulator = _Accumulator.empty()
    trainer._traverse(
        state,
        board,
        traverser=1,
        iteration=7,
        base_regrets={root.key: root_regrets},
        accumulator=accumulator,
        path=("fixed-hunl-simple-average-oracle",),
    )
    independent_root_policy = {
        action: (1.0 if action == "check_call" else 0.0)
        for action in root_actions
    }
    independent_delta = {
        action: 7.0 * probability
        for action, probability in independent_root_policy.items()
    }
    assert accumulator.strategy[root.key] == independent_delta
    assert linear_simple_average_delta(independent_root_policy, 7) == independent_delta
    # In this hand player 0 is always SB.  Every simple-average update in the
    # player-1 regret traversal must therefore belong to sampled opponent/SB
    # nodes; the old traverser/own-reach formula would instead produce BB rows
    # and omit the root row above.
    assert {
        parse_infoset_key(key)["position"] for key in accumulator.strategy
    } == {"sb"}
    assert {
        parse_infoset_key(key)["position"] for key in accumulator.regret
    } == {"bb"}


def test_open_spiel_simple_average_oracle_rejects_old_own_reach_estimator() -> None:
    """A two-step Kuhn-shaped counterexample with changing opponent reach."""

    # Player 1's deep infoset is reachable only after player 0 chooses R.
    # OpenSpiel simple averaging updates it on player-0 traversals, where R is
    # expanded, so the linear estimator is sum(t*sigma_t).  The retired rule
    # updated on player-1 traversals only when sampled R was reached, yielding
    # an unwanted opponent-reach factor in expectation.
    samples = (
        (1.0, 0.25, {"check_call": 0.2, "allin": 0.8}),
        (2.0, 0.75, {"check_call": 0.6, "allin": 0.4}),
    )
    simple = {
        action: sum(weight * policy[action] for weight, _, policy in samples)
        for action in ("check_call", "allin")
    }
    retired_own_reach = {
        action: sum(
            weight * opponent_reach * policy[action]
            for weight, opponent_reach, policy in samples
        )
        for action in ("check_call", "allin")
    }
    assert simple == pytest.approx({"check_call": 1.4, "allin": 1.6})
    assert retired_own_reach == pytest.approx(
        {"check_call": 0.95, "allin": 0.8}
    )
    assert retired_own_reach != pytest.approx(simple)


def test_resume_and_sequential_shard_layout_are_byte_equivalent(tmp_path) -> None:
    uninterrupted = HUNLExternalSamplingLCFR(HUNLTrainingConfig(seed=991))
    uninterrupted.train_to(2, shard_size=2)

    resumed = HUNLExternalSamplingLCFR(HUNLTrainingConfig(seed=991))
    first_shard = resumed.build_shard(1)
    shard_path = tmp_path / "shard.json"
    resumed.save_shard(shard_path, first_shard)
    resumed.apply_shard(resumed.load_shard(shard_path))
    checkpoint_path = tmp_path / "checkpoint.json"
    resumed.save_checkpoint(checkpoint_path)
    resumed = HUNLExternalSamplingLCFR.load_checkpoint(checkpoint_path)
    resumed.train_to(2, shard_size=1)

    assert resumed.checkpoint_digest() == uninterrupted.checkpoint_digest()
    assert resumed.checkpoint_payload() == uninterrupted.checkpoint_payload()
    assert resumed.average_strategy() == uninterrupted.average_strategy()
    assert not list(tmp_path.glob(".*.tmp"))


def test_corrupt_shard_fails_before_mutating_live_state() -> None:
    trainer = HUNLExternalSamplingLCFR(HUNLTrainingConfig(seed=1771))
    before = trainer.checkpoint_payload()
    shard = trainer.build_shard(1)
    corrupt = copy.deepcopy(shard)
    corrupt["body_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="shard content hash"):
        trainer.apply_shard(corrupt)
    assert trainer.checkpoint_payload() == before

    trainer.apply_shard(shard)
    applied = trainer.checkpoint_payload()
    with pytest.raises(ValueError, match="does not start"):
        trainer.apply_shard(shard)
    assert trainer.checkpoint_payload() == applied


@pytest.mark.parametrize("mutation", ("hash", "bool", "nan", "missing_action"))
def test_checkpoint_strictly_rejects_hash_types_nan_and_action_drift(
    trained_once,
    mutation,
) -> None:
    payload = copy.deepcopy(trained_once.checkpoint_payload())
    body = payload["body"]
    assert isinstance(body, dict)
    if mutation == "hash":
        payload["body_sha256"] = "f" * 64
    elif mutation == "bool":
        body["iterations_completed"] = True
        _rehash_checkpoint(payload)
    else:
        regrets = body["regrets"]
        assert isinstance(regrets, dict)
        key = next(iter(regrets))
        row = regrets[key]
        assert isinstance(row, dict)
        action = next(iter(row))
        if mutation == "nan":
            row[action] = float("nan")
        else:
            del row[action]
        _rehash_checkpoint(payload)
    with pytest.raises(ValueError):
        HUNLExternalSamplingLCFR.from_checkpoint_payload(payload)


def test_checkpoint_and_shard_json_reject_duplicate_keys(trained_once) -> None:
    checkpoint = json.dumps(trained_once.checkpoint_payload())
    duplicate = checkpoint[:-1] + ',"schema":"duplicate"}'
    with pytest.raises(ValueError, match="duplicate JSON key"):
        strict_json_loads(duplicate)


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
def test_strict_json_rejects_nonfinite_constants(constant) -> None:
    with pytest.raises(ValueError, match="non-finite JSON constant"):
        strict_json_loads('{"value":' + constant + "}")


@pytest.mark.parametrize("bad", (True, "1.0", float("nan"), float("inf")))
def test_regret_matching_rejects_bool_strings_and_nonfinite_values(bad) -> None:
    with pytest.raises(ValueError, match="regrets"):
        regret_matching({"fold": bad}, ("fold", "check_call"))


def test_bad_result_inside_hash_valid_shard_is_transactional() -> None:
    trainer = HUNLExternalSamplingLCFR(HUNLTrainingConfig(seed=8191))
    shard = trainer.build_shard(1)
    corrupt = copy.deepcopy(shard)
    body = corrupt["body"]
    result = body["result_checkpoint"]
    result_body = result["body"]
    result_body["config"]["seed"] = True
    result["body_sha256"] = _digest(result_body)
    _rehash_shard(corrupt)
    before = trainer.checkpoint_payload()
    with pytest.raises(ValueError, match="seed"):
        trainer.apply_shard(corrupt)
    assert trainer.checkpoint_payload() == before


def test_training_deck_and_policy_seed_roots_are_predeclared_and_disjoint() -> None:
    config = load_config(PACKAGE_ROOT / "configs/hunl_m4_smoke.json")
    snapshot = seed_independence_snapshot(config)
    roots = [
        snapshot["training"]["root_seed"],
        snapshot["tcp_deck"]["root_seed"],
        *(item["root_seed"] for item in snapshot["tcp_policy"]),
    ]
    assert len(set(roots)) == 4
    assert snapshot["all_root_seeds_distinct"] is True
    assert snapshot["smoke_inputs_excluded_from_blueprint_build"] is True

    corrupt = copy.deepcopy(config)
    corrupt["tcp_client_policy_seeds"][0] = corrupt["training"]["seed"]
    with pytest.raises(ValueError, match="must be distinct"):
        load_config_payload(corrupt)


def test_checkpoint_frozen_identity_covers_rules_common_sources_and_assets(
    trained_once,
) -> None:
    identity = training_identity_snapshot()
    assert identity["rules"] == {
        "big_blind": 100,
        "initial_chips": 20000,
        "players": 2,
        "small_blind": 50,
        "streets": ["preflop", "flop", "turn", "river"],
    }
    assert identity["assets"]["external_assets"] == []
    common_files = identity["common"]["files"]
    assert {"actions.py", "cards.py", "constants.py", "national_state.py"} <= set(
        common_files
    )
    route_files = identity["route_sources"]["files"]
    assert {
        "__init__.py",
        "decisionholdem_like/__init__.py",
        "decisionholdem_like/hunl_abstraction.py",
        "decisionholdem_like/hunl_blueprint.py",
        "decisionholdem_like/hunl_external_sampling.py",
        "decisionholdem_like/secure_files.py",
        "tools/__init__.py",
        "tools/train_hunl_blueprint.py",
    } == set(route_files)
    assert set(identity["package_ancestry"]["files"]) == {"__init__.py"}
    body = trained_once.checkpoint_payload()["body"]
    assert body["training_identity"] == identity
    assert training_identity_digest() == _digest(identity)


def test_training_cli_import_does_not_eagerly_load_legacy_route_modules() -> None:
    package = "bots.research_native_lab.rebel_decisionholdem.decisionholdem_like"
    script = f"""
import sys
import bots.research_native_lab.rebel_decisionholdem.tools.train_hunl_blueprint
forbidden = {{
    {package!r} + '.blueprint',
    {package!r} + '.common_native_entry',
    {package!r} + '.leduc_linear_cfr',
    {package!r} + '.linear_cfr',
    {package!r} + '.resolving',
}}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit('eager imports: ' + ','.join(loaded))
"""
    subprocess.run([sys.executable, "-c", script], check=True)


def test_resigned_checkpoint_cannot_forge_training_identity(trained_once) -> None:
    payload = copy.deepcopy(trained_once.checkpoint_payload())
    payload["body"]["training_identity"]["common"]["tree_sha256"] = "0" * 64
    _rehash_checkpoint(payload)
    with pytest.raises(ValueError, match="frozen training identity"):
        HUNLExternalSamplingLCFR.from_checkpoint_payload(payload)


def test_checkpoint_fails_closed_when_current_training_identity_drifts(
    trained_once,
    monkeypatch,
) -> None:
    payload = copy.deepcopy(trained_once.checkpoint_payload())
    drifted = copy.deepcopy(training_identity_snapshot())
    drifted["rules"]["big_blind"] = 101
    monkeypatch.setattr(
        hunl_training_module,
        "training_identity_snapshot",
        lambda: drifted,
    )
    with pytest.raises(ValueError, match="frozen training identity"):
        HUNLExternalSamplingLCFR.from_checkpoint_payload(payload)


def test_resigned_segment_cannot_forge_training_identity() -> None:
    trainer = HUNLExternalSamplingLCFR(HUNLTrainingConfig(seed=65537))
    shard = trainer.build_shard(1)
    corrupt = copy.deepcopy(shard)
    corrupt["body"]["training_identity_sha256"] = "0" * 64
    _rehash_shard(corrupt)
    before = trainer.checkpoint_payload()
    with pytest.raises(ValueError, match="frozen training identity"):
        trainer.apply_shard(corrupt)
    assert trainer.checkpoint_payload() == before


def test_preregistered_training_only_selector_freezes_first_passing_candidate() -> None:
    config = load_config(PACKAGE_ROOT / "configs/hunl_m4_smoke.json")
    trainer, trace = select_training_candidate(
        config["training"], source_commit=config["source_commit"]
    )
    assert trainer.iterations_completed == config["training"][
        "frozen_selected_iterations"
    ] == 32
    assert [item["iterations"] for item in trace] == [2, 4, 8, 16, 32]
    assert all(item["passed"] is False for item in trace[:-1])
    assert all(item["failure_reasons"] for item in trace[:-1])
    assert trace[-1]["passed"] is True
    assert trace[-1]["failure_reasons"] == []


@pytest.mark.parametrize(
    "collision",
    ("heartbeat_checkpoint", "output_checkpoint", "scale_output", "config_output"),
)
def test_training_cli_paths_must_not_overlap(tmp_path, collision) -> None:
    config_path = PACKAGE_ROOT / "configs/hunl_m4_smoke.json"
    config = load_config(config_path)
    output = tmp_path / "artifact.json"
    checkpoint = tmp_path / "run.json"
    heartbeat = tmp_path / "heartbeat.json"
    scale = tmp_path / "scale.json"
    config_source = config_path
    if collision == "heartbeat_checkpoint":
        heartbeat = checkpoint
    elif collision == "output_checkpoint":
        output = checkpoint
    elif collision == "scale_output":
        scale = output
    else:
        output = config_path
    with pytest.raises(ValueError, match="overlap"):
        train_and_export(
            config,
            output=output,
            scale_evidence=scale,
            checkpoint=checkpoint,
            heartbeat=heartbeat,
            config_source=config_source,
        )


def test_live_config_source_drift_stops_before_the_first_training_segment(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_bytes(
        (PACKAGE_ROOT / "configs/hunl_m4_smoke.json").read_bytes()
    )
    config = load_config(config_path)
    original_heartbeat = training_tool._write_training_heartbeat

    def mutate_after_initial_checkpoint(*args, **kwargs) -> None:
        original_heartbeat(*args, **kwargs)
        if kwargs["phase"] == "initialized":
            drifted = copy.deepcopy(config)
            drifted["scale_estimate_iterations"] += 1
            training_tool.atomic_json_write(config_path, drifted)

    monkeypatch.setattr(
        training_tool,
        "_write_training_heartbeat",
        mutate_after_initial_checkpoint,
    )
    with pytest.raises(RuntimeError, match="config source drifted"):
        train_and_export(
            config,
            output=tmp_path / "artifact.json",
            checkpoint=tmp_path / "run.json",
            heartbeat=tmp_path / "heartbeat.json",
            config_source=config_path,
        )
    checkpoint = strict_json_loads((tmp_path / "run.json").read_bytes())
    assert checkpoint["body"]["trainer_checkpoint"]["body"][
        "iterations_completed"
    ] == 0


def test_live_training_identity_drift_stops_before_the_first_segment(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = PACKAGE_ROOT / "configs/hunl_m4_smoke.json"
    config = load_config(config_path)
    original_snapshot = training_tool.training_identity_snapshot
    original_heartbeat = training_tool._write_training_heartbeat
    drifted = False

    def controlled_snapshot() -> dict[str, object]:
        snapshot = original_snapshot()
        if drifted:
            snapshot = copy.deepcopy(snapshot)
            snapshot["rules"]["big_blind"] = 101
        return snapshot

    def drift_after_initial_checkpoint(*args, **kwargs) -> None:
        nonlocal drifted
        original_heartbeat(*args, **kwargs)
        if kwargs["phase"] == "initialized":
            drifted = True

    monkeypatch.setattr(
        training_tool,
        "training_identity_snapshot",
        controlled_snapshot,
    )
    monkeypatch.setattr(
        training_tool,
        "_write_training_heartbeat",
        drift_after_initial_checkpoint,
    )
    with pytest.raises(RuntimeError, match="implementation identity drifted"):
        train_and_export(
            config,
            output=tmp_path / "artifact.json",
            checkpoint=tmp_path / "run.json",
            heartbeat=tmp_path / "heartbeat.json",
            config_source=config_path,
        )


@pytest.mark.parametrize("tamper", ("selected", "trace"))
def test_resumed_run_rejects_resigned_selected_and_trace_tampering(
    tmp_path,
    tamper,
) -> None:
    config_path = PACKAGE_ROOT / "configs/hunl_m4_smoke.json"
    config = load_config(config_path)
    output = tmp_path / "artifact.json"
    checkpoint_path = tmp_path / "run.json"
    heartbeat = tmp_path / "heartbeat.json"
    cancel = tmp_path / "CANCEL"
    cancel.write_bytes(b"cancel before work\n")
    with pytest.raises(TrainingRunCancelled):
        train_and_export(
            config,
            output=output,
            checkpoint=checkpoint_path,
            heartbeat=heartbeat,
            config_source=config_path,
        )
    wrapper = strict_json_loads(checkpoint_path.read_bytes())
    if tamper == "selected":
        wrapper["body"]["selected_iterations"] = 32
        expected_error = "without a passing trace"
    else:
        wrapper["body"]["selection_trace"] = [{"iterations": 2}]
        expected_error = "selection trace differs"
    wrapper["body_sha256"] = _digest(wrapper["body"])
    training_tool.atomic_json_write(checkpoint_path, wrapper)
    cancel.unlink()
    with pytest.raises(ValueError, match=expected_error):
        train_and_export(
            config,
            output=output,
            resume_checkpoint=checkpoint_path,
            heartbeat=heartbeat,
            config_source=config_path,
        )


def test_n32_cancel_resume_matches_uninterrupted_checkpoint_and_artifact_bytes(
    tmp_path,
    monkeypatch,
) -> None:
    """One arbitrary durable boundary preserves the complete selected run bytes."""

    config_path = PACKAGE_ROOT / "configs/hunl_m4_smoke.json"
    config = load_config(config_path)
    output = tmp_path / "artifact.json"
    checkpoint = tmp_path / "run_checkpoint.json"
    heartbeat = tmp_path / "heartbeat.json"
    cancel = tmp_path / "CANCEL"

    uninterrupted = train_and_export(
        config,
        output=output,
        checkpoint=checkpoint,
        heartbeat=heartbeat,
        config_source=config_path,
    )
    uninterrupted_artifact = output.read_bytes()
    uninterrupted_checkpoint = checkpoint.read_bytes()
    for path in (output, checkpoint, heartbeat):
        path.unlink()

    original_heartbeat = training_tool._write_training_heartbeat
    cancellation_injected = False

    def inject_cancel_after_seventh_segment(*args, **kwargs) -> None:
        nonlocal cancellation_injected
        original_heartbeat(*args, **kwargs)
        if (
            not cancellation_injected
            and kwargs["phase"] == "segment_committed"
            and kwargs["segments_this_process"] == 7
        ):
            cancel.write_bytes(b"operator cancellation\n")
            cancellation_injected = True

    monkeypatch.setattr(
        training_tool,
        "_write_training_heartbeat",
        inject_cancel_after_seventh_segment,
    )
    with pytest.raises(TrainingRunCancelled, match="durable iteration 7"):
        train_and_export(
            config,
            output=output,
            checkpoint=checkpoint,
            heartbeat=heartbeat,
            config_source=config_path,
        )
    assert not output.exists()
    cancelled_heartbeat = strict_json_loads(heartbeat.read_bytes())
    assert cancelled_heartbeat["body"]["phase"] == "cancelled_at_checkpoint"
    interrupted = strict_json_loads(checkpoint.read_bytes())
    assert interrupted["body"]["trainer_checkpoint"]["body"][
        "iterations_completed"
    ] == 7

    monkeypatch.setattr(
        training_tool,
        "_write_training_heartbeat",
        original_heartbeat,
    )
    cancel.unlink()
    resumed = train_and_export(
        config,
        output=output,
        resume_checkpoint=checkpoint,
        heartbeat=heartbeat,
        config_source=config_path,
    )
    assert output.read_bytes() == uninterrupted_artifact
    assert checkpoint.read_bytes() == uninterrupted_checkpoint
    assert resumed["body"]["artifact_sha256"] == uninterrupted["body"][
        "artifact_sha256"
    ]
    assert resumed["body"]["checkpoint_sha256"] == uninterrupted["body"][
        "checkpoint_sha256"
    ]
    final_checkpoint = strict_json_loads(checkpoint.read_bytes())
    trace = final_checkpoint["body"]["selection_trace"]
    assert [row["iterations"] for row in trace] == [2, 4, 8, 16, 32]
    assert all(row["passed"] is False for row in trace[:-1])
    assert trace[-1]["passed"] is True


def test_trained_backoff_is_strategy_sum_mass_not_cross_infoset_regret_sum() -> None:
    trainer = HUNLExternalSamplingLCFR(HUNLTrainingConfig(seed=2026071402))
    trainer.train_to(4)
    tables = build_trained_backoff_policies(trainer)
    level, fields = HUNL_TRAINED_BACKOFF_LEVELS[0]
    key, production = next(iter(tables[level].items()))
    target = parse_trained_backoff_key(key)["context"]
    manual = {action: 0.0 for action in production}
    for infoset_key, strategy_mass in trainer.strategy_sums.items():
        infoset = parse_infoset_key(infoset_key)
        if {field: infoset[field] for field in fields} != target:
            continue
        for action in manual:
            manual[action] += strategy_mass[action]
    total = sum(manual.values())
    assert total > 0.0
    assert production == pytest.approx(
        {action: value / total for action, value in manual.items()}, abs=1e-14
    )

    before = copy.deepcopy(tables)
    for row in trainer.regrets.values():
        for action in row:
            row[action] = 10**9 if action == next(iter(row)) else -(10**9)
    assert build_trained_backoff_policies(trainer) == before
