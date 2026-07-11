from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


torch = pytest.importorskip("torch")
ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "bots" / "neural_national_lab" / "tools"
sys.path.insert(0, str(TOOLS))

import opp_multitask_runtime as runtime  # noqa: E402
import opp_multitask_ensemble_runtime as ensemble_runtime  # noqa: E402
import run_multitask_scaling_sweep as scaling  # noqa: E402
import train_opponent_multitask_net as trainer  # noqa: E402


def test_response_samples_mask_unobservable_private_cards() -> None:
    row = {
        "opponent": "national_v1",
        "request": {
            "num_players": 2,
            "dealer_id": 0,
            "my_id": 0,
            "my_chips": 19950,
            "my_cards": [48, 49],
            "public_cards": [],
            "history": [],
            "hand": 0,
            "max_hand": 70,
            "opponent_profile": {},
        },
        "state": {"pot": 150, "to_call": 50},
        "hero_action": 300,
        "hero_action_label_id": 3,
        "opponent_action_label_id": 0,
        "opponent_action_pot_ratio": 0.0,
        "delta_vs_rule": [None, 0.0, None, 100.0, None, None],
        "tail_delta_vs_rule": [None, 0.0, None, 0.0, None, None],
        "match_delta_vs_rule": [None, 0.0, None, 100.0, None, None],
        "target_mask": [0, 1, 0, 1, 0, 0],
    }

    value = trainer.build_value_sample(row, max_hist=16)
    response = trainer.build_behavior_sample(row, max_hist=16)

    assert response is not None
    assert any(value["state"][index] != 0.0 for index in trainer.PRIVATE_STATE_INDICES)
    assert all(response["state"][index] == 0.0 for index in trainer.PRIVATE_STATE_INDICES)


def test_pairwise_ranking_loss_prefers_rule_relative_direction() -> None:
    target = torch.tensor([
        [float("nan"), 0.0, 500.0, -500.0, float("nan"), float("nan")]
    ])
    rule_ids = torch.tensor([1])
    lower = torch.zeros(1, trainer.NUM_ACTIONS)
    good_mean = torch.tensor([[0.0, 0.0, 0.5, -0.5, 0.0, 0.0]])
    bad_mean = -good_mean
    kwargs = {
        "margin": 100.0,
        "temperature": 0.1,
        "positive_weight": torch.tensor(1.0),
        "action_weights": torch.ones(trainer.NUM_ACTIONS),
    }

    good, count = trainer._pairwise_ranking_loss(
        torch.cat([good_mean, lower], dim=1), target, rule_ids, **kwargs
    )
    bad, _ = trainer._pairwise_ranking_loss(
        torch.cat([bad_mean, lower], dim=1), target, rule_ids, **kwargs
    )

    assert count == 2
    assert good.item() < bad.item()

    shifted_rule = good_mean.clone()
    shifted_rule[0, 1] = 1000.0
    shifted_rule_loss, _ = trainer._pairwise_ranking_loss(
        torch.cat([shifted_rule, lower], dim=1),
        target,
        rule_ids,
        **kwargs,
    )
    assert shifted_rule_loss.item() == pytest.approx(good.item())

    good_lower, _ = trainer._pairwise_ranking_loss(
        torch.cat([lower, good_mean], dim=1),
        target,
        rule_ids,
        head="lower",
        **kwargs,
    )
    bad_lower, _ = trainer._pairwise_ranking_loss(
        torch.cat([lower, bad_mean], dim=1),
        target,
        rule_ids,
        head="lower",
        **kwargs,
    )
    assert good_lower.item() < bad_lower.item()

    shifted_lower_rule = good_mean.clone()
    shifted_lower_rule[0, 1] = -1000.0
    shifted_lower_loss, _ = trainer._pairwise_ranking_loss(
        torch.cat([lower, shifted_lower_rule], dim=1),
        target,
        rule_ids,
        head="lower",
        **kwargs,
    )
    assert shifted_lower_loss.item() == pytest.approx(good_lower.item())


def test_opponent_weighting_equalizes_opponents_without_flattening_events() -> None:
    rows = []
    rows.extend([
        {
            "opponent": "national_v1",
            "deck_seed_base": 10,
            "bot_seed_base": 11,
            "row": index,
        }
        for index in range(2)
    ])
    rows.append({
        "opponent": "national_v1",
        "deck_seed_base": 20,
        "bot_seed_base": 21,
        "row": 0,
    })
    rows.extend([
        {
            "opponent": "national_v2",
            "deck_seed_base": 30,
            "bot_seed_base": 31,
            "row": index,
        }
        for index in range(4)
    ])

    weighted, report = trainer._attach_training_row_weights(
        rows, scheme="opponent_balanced"
    )

    assert report["mean_row_weight"] == pytest.approx(1.0)
    assert report["per_opponent"]["national_v1"]["total_weight"] == pytest.approx(3.5)
    assert report["per_opponent"]["national_v2"]["total_weight"] == pytest.approx(3.5)
    v1_weights = [
        row["_training_loss_weight"]
        for row in weighted
        if row["opponent"] == "national_v1"
    ]
    v2_weights = [
        row["_training_loss_weight"]
        for row in weighted
        if row["opponent"] == "national_v2"
    ]
    assert len(set(v1_weights)) == 1
    assert len(set(v2_weights)) == 1
    assert v1_weights[0] == pytest.approx(3.5 / 3.0)
    assert v2_weights[0] == pytest.approx(3.5 / 4.0)


def test_value_loss_honors_row_weights() -> None:
    output = torch.zeros(2, trainer.NUM_ACTIONS * 2)
    target = torch.full((2, trainer.NUM_ACTIONS), float("nan"))
    target[0, 2] = 500.0
    target[1, 2] = -5000.0

    weighted, _ = trainer._value_loss(
        output,
        target,
        clip=5000.0,
        quantile=0.2,
        row_weights=torch.tensor([1.0, 0.0]),
    )
    first_only, _ = trainer._value_loss(
        output[:1], target[:1], clip=5000.0, quantile=0.2
    )

    assert weighted.item() == pytest.approx(first_only.item())


def test_cluster_bootstrap_resamples_shared_whole_match_groups() -> None:
    value_rows = []
    behavior_rows = []
    for opponent in ("national_v1", "national_v2"):
        for cluster in (10, 20, 30):
            base = {
                "opponent": opponent,
                "deck_seed_base": cluster,
                "bot_seed_base": cluster + 1,
            }
            value_rows.extend([{**base, "row": index} for index in range(2)])
            behavior_rows.extend([{**base, "row": index} for index in range(3)])

    first = trainer._stratified_cluster_bootstrap(
        value_rows, behavior_rows, seed=17
    )
    second = trainer._stratified_cluster_bootstrap(
        value_rows, behavior_rows, seed=17
    )

    assert first == second
    sampled_value, sampled_behavior, report = first
    assert report["source_clusters"] == 6
    assert report["sampled_draws"] == 6
    assert report["effective_value_rows"] == len(sampled_value)
    assert report["effective_behavior_rows"] == len(sampled_behavior)
    value_counts = {}
    behavior_counts = {}
    for row in sampled_value:
        key = trainer._match_cluster_key(row)
        value_counts[key] = value_counts.get(key, 0) + 1
    for row in sampled_behavior:
        key = trainer._match_cluster_key(row)
        behavior_counts[key] = behavior_counts.get(key, 0) + 1
    assert set(value_counts) == set(behavior_counts)
    assert {
        key: count // 2 for key, count in value_counts.items()
    } == {
        key: count // 3 for key, count in behavior_counts.items()
    }


def test_cluster_bootstrap_rejects_value_behavior_cluster_mismatch() -> None:
    value = [{
        "opponent": "national_v1",
        "deck_seed_base": 10,
        "bot_seed_base": 11,
    }]
    behavior = [{
        "opponent": "national_v1",
        "deck_seed_base": 20,
        "bot_seed_base": 21,
    }]

    with pytest.raises(ValueError, match="match clusters differ"):
        trainer._stratified_cluster_bootstrap(value, behavior, seed=1)


@pytest.mark.parametrize(
    "encoder", ["gru", "gru_moe", "deep_set", "transformer"]
)
def test_scaling_config_preserves_temporal_encoder(encoder: str) -> None:
    config = scaling._parse_config(
        f"tiny_{encoder}@{encoder}:64:32:16:16:12:32",
        cross_transformer_heads=2,
        cross_moe_experts=3,
    )

    assert config["cross_sequence_encoder"] == encoder
    assert config["cross_sequence_hidden"] == 12
    assert config["cross_transformer_heads"] == 2
    assert config["cross_moe_experts"] == 3


def test_scaling_config_rejects_incompatible_transformer_heads() -> None:
    with pytest.raises(SystemExit, match="divisible by heads"):
        scaling._parse_config(
            "bad@transformer:64:32:16:16:13:32",
            cross_transformer_heads=2,
        )


def test_scaling_expands_ranking_weight_as_selection_dimension() -> None:
    base = scaling._parse_config(
        "tiny@gru:64:32:16:16:12:32",
        cross_transformer_heads=2,
        cross_moe_experts=3,
    )

    configs, weights = scaling._expand_ranking_weights(
        [base], "0,0.5,1,0.5", default=0.25
    )

    assert weights == [0.0, 0.5, 1.0]
    assert [config["name"] for config in configs] == [
        "tiny_rw0", "tiny_rw0p5", "tiny_rw1"
    ]
    assert [config["match_ranking_weight"] for config in configs] == weights

    lower_configs, lower_weights = scaling._expand_lower_ranking_weights(
        configs, "0,0.25", default=0.0
    )
    assert lower_weights == [0.0, 0.25]
    assert [config["name"] for config in lower_configs] == [
        "tiny_rw0_lrw0",
        "tiny_rw0_lrw0p25",
        "tiny_rw0p5_lrw0",
        "tiny_rw0p5_lrw0p25",
        "tiny_rw1_lrw0",
        "tiny_rw1_lrw0p25",
    ]


def test_offline_candidate_gate_rejects_relaxed_or_incomplete_evidence(
    tmp_path: Path,
) -> None:
    requirements = scaling.OFFLINE_CANDIDATE_REQUIREMENTS
    args = SimpleNamespace(
        selection_mode="policy",
        policy_min_overrides=0,
        policy_min_selection_clusters=requirements["selection_clusters"],
        policy_min_override_clusters=requirements[
            "selection_override_clusters"
        ],
        policy_min_overrides_per_opponent=requirements[
            "selection_overrides_per_opponent"
        ],
        policy_min_calibration_overrides=requirements[
            "calibration_overrides"
        ],
        policy_min_calibration_override_clusters=requirements[
            "calibration_override_clusters"
        ],
        policy_min_held_out_overrides=requirements["held_out_overrides"],
        policy_min_held_out_override_clusters=requirements[
            "held_out_override_clusters"
        ],
        policy_bootstrap_samples=requirements["bootstrap_samples"],
        policy_min_override_hand_mean=0.0,
        policy_min_selection_ci_lower=-1.0,
        policy_min_calibration_ci_lower=0.0,
        policy_min_held_out_ci_lower=0.0,
        policy_min_match_weight=requirements["minimum_match_weight"],
        policy_min_hand_lcb=-1.0,
        policy_allow_negative_opponent=False,
        allow_missing_cross_hand_sequence=False,
        allow_missing_match_outcome=False,
    )
    audit = {
        "value_rows": {"train": requirements["value_train_rows"]},
        "behavior_rows": {"train": requirements["behavior_train_rows"]},
        "opponents": {
            split: [f"{split}-{index}" for index in range(requirements[f"{split}_opponents"])]
            for split in ("train", "val", "calibration", "held_out")
        },
    }
    (tmp_path / "freeze_manifest.json").write_text(
        json.dumps({
            "allow_incomplete": True,
            "source_completed_passes": 10,
            "source_requested_passes": 160,
        }),
        encoding="utf-8",
    )

    gate = scaling._offline_candidate_gate(
        args=args,
        audit_report=audit,
        data_dir=tmp_path,
        seeds=[101, 211, 307],
        post_selection_policy={
            "passed": True,
            "policy_config": {
                "hand_weight": 0.5,
                "tail_weight": 0.25,
                "match_weight": 0.25,
                "min_hand_lcb": 0.0,
            },
        },
    )

    assert gate["passed"] is False
    assert "selection_overrides<10" in gate["errors"]
    assert "selection_ci_lower_threshold<0" in gate["errors"]
    assert "minimum_hand_lcb<0.0" in gate["errors"]
    assert "dataset_freeze_incomplete" in gate["errors"]
    assert "dataset_pass_count_incomplete" in gate["errors"]

    args.policy_min_overrides = requirements["selection_overrides"]
    args.policy_min_selection_ci_lower = 0.0
    args.policy_min_hand_lcb = requirements["minimum_hand_lcb"]
    (tmp_path / "freeze_manifest.json").write_text(
        json.dumps({
            "allow_incomplete": False,
            "source_completed_passes": 160,
            "source_requested_passes": 160,
        }),
        encoding="utf-8",
    )
    passing = scaling._offline_candidate_gate(
        args=args,
        audit_report=audit,
        data_dir=tmp_path,
        seeds=[101, 211, 307],
        post_selection_policy={
            "passed": True,
            "policy_config": {
                "hand_weight": 0.5,
                "tail_weight": 0.25,
                "match_weight": 0.25,
                "min_hand_lcb": 0.0,
            },
        },
    )

    assert passing["passed"] is True
    assert passing["errors"] == []


def _post_selection_args() -> SimpleNamespace:
    return SimpleNamespace(
        policy_bootstrap_samples=100,
        policy_bootstrap_seed=17,
        policy_min_calibration_overrides=5,
        policy_min_calibration_override_clusters=3,
        policy_min_calibration_ci_lower=0.0,
        policy_min_held_out_overrides=10,
        policy_min_held_out_override_clusters=8,
        policy_min_held_out_ci_lower=0.0,
        policy_min_match_positive_rate_ci_lower=0.5,
        policy_min_match_positive_uplift_ci_lower=0.0,
        policy_min_opponent_match_positive_rate=0.5,
        allow_missing_match_outcome=False,
    )


@pytest.mark.parametrize("calibration_passes", [False, True])
def test_post_selection_opens_held_out_only_after_calibration_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    calibration_passes: bool,
) -> None:
    model = tmp_path / "model.json"
    calibration = tmp_path / "calibration.jsonl"
    held_out = tmp_path / "held_out.jsonl"
    model.write_text("{}", encoding="utf-8")
    calibration.write_text("calibration", encoding="utf-8")
    held_out.write_text("held-out", encoding="utf-8")
    reads = []

    monkeypatch.setattr(
        scaling.OpponentMultiTaskEnsemble,
        "load",
        staticmethod(lambda _paths: object()),
    )

    def read_rows(path: Path) -> list[dict[str, object]]:
        reads.append(path.name)
        return [{
            "split": "calibration" if path == calibration else "held_out"
        }]

    def evaluate(rows, _ensemble, **_kwargs):
        split = rows[0]["split"]
        return {
            "rows": len(rows),
            "match_mean_per_opportunity": 1.0 if split == "calibration" else 2.0,
            "gate_passes": calibration_passes if split == "calibration" else True,
        }

    monkeypatch.setattr(scaling, "read_policy_rows", read_rows)
    monkeypatch.setattr(
        scaling,
        "prepare_policy_rows",
        lambda rows, _ensemble, **_kwargs: rows,
    )
    monkeypatch.setattr(scaling, "evaluate_policy_config", evaluate)
    monkeypatch.setattr(
        scaling,
        "policy_safety_gate",
        lambda result, **_kwargs: {
            "passed": result["gate_passes"],
            "errors": [] if result["gate_passes"] else ["failed"],
        },
    )

    result = scaling._run_post_selection_policy(
        model_paths=[model],
        policy_config={"margin": 25.0},
        calibration_path=calibration,
        held_out_path=held_out,
        out_dir=tmp_path,
        args=_post_selection_args(),
    )
    payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))

    assert result["held_out_opened"] is calibration_passes
    assert payload["held_out_opened"] is calibration_passes
    if calibration_passes:
        assert reads == ["calibration.jsonl", "held_out.jsonl"]
        assert result["passed"] is True
        assert result["held_out_match_mean_per_opportunity"] == 2.0
        assert payload["data"]["held_out"]["sha256"] == scaling._sha256(held_out)
    else:
        assert reads == ["calibration.jsonl"]
        assert result["passed"] is False
        assert result["held_out_match_mean_per_opportunity"] is None
        assert payload["held_out"] is None
        assert payload["data"]["held_out"] == {
            "opened": False,
            "path": str(held_out.resolve()),
            "rows": None,
            "sha256": None,
        }


def test_resume_rejects_model_that_already_exposed_held_out(tmp_path: Path) -> None:
    config = scaling._parse_config(
        "tiny@gru:64:32:16:16:12:32",
        cross_transformer_heads=2,
        cross_moe_experts=3,
    )
    args = SimpleNamespace(
        match_ranking_weight=0.5,
        match_lower_ranking_weight=0.0,
        ranking_margin=100.0,
        ranking_temperature=0.1,
        direction_score_weight=0.5,
        lower_direction_score_weight=0.5,
        cluster_bootstrap=False,
        training_row_weighting="uniform",
    )
    data_paths = {}
    for name in ("value_train", "value_val", "behavior_train", "behavior_val"):
        path = tmp_path / f"{name}.jsonl"
        path.write_text(name, encoding="utf-8")
        data_paths[name] = path
    manifests = {
        name: {"sha256": scaling._sha256(path)}
        for name, path in data_paths.items()
    }
    manifests.update({"value_held_out": None, "behavior_held_out": None})
    payload = {
        "meta": {
            "format": "opp_multitask_gru_v2",
            "response_encoder": "separate_public_v1",
            "rule_action_dim": 6,
            "model": config,
            "training": {
                "seed": 101,
                "trainer_sha256": scaling._sha256(scaling.TRAINER),
                "data": manifests,
                **scaling._training_recipe(args, config),
            },
        }
    }
    model = tmp_path / "model.json"
    model.write_text(json.dumps(payload), encoding="utf-8")

    assert scaling._model_matches(
        model, config=config, seed=101, data_paths=data_paths, args=args
    ) is True

    payload["meta"]["training"]["data"]["value_held_out"] = {"sha256": "seen"}
    payload["meta"]["training"]["data"]["behavior_held_out"] = {
        "sha256": "seen"
    }
    model.write_text(json.dumps(payload), encoding="utf-8")

    assert scaling._model_matches(
        model, config=config, seed=101, data_paths=data_paths, args=args
    ) is False


@pytest.mark.parametrize(
    "cross_sequence_encoder",
    [None, "gru", "gru_moe", "deep_set", "transformer"],
)
def test_multitask_runtime_matches_torch_outputs(
    cross_sequence_encoder: str | None,
) -> None:
    temporal_cross_hand = cross_sequence_encoder is not None
    torch.manual_seed(29)
    model = trainer.OpponentAwareMultiTaskNet(
        48,
        12,
        gru_hidden=5,
        hidden=16,
        latent=9,
        cross_hidden=7,
        head_hidden=11,
        dropout=0.0,
        cross_sequence_hidden=4 if temporal_cross_hand else 0,
        cross_sequence_encoder=cross_sequence_encoder or "none",
        cross_transformer_heads=2,
        cross_moe_experts=3,
    )
    model.eval()
    clips = {
        "delta_vs_rule": 5000.0,
        "tail_delta_vs_rule": 2000.0,
        "match_delta_vs_rule": 2000.0,
    }
    payload = {
        "meta": {
            "format": (
                "opp_multitask_gru_v2" if temporal_cross_hand
                else "opp_multitask_gru_v1"
            ),
            "labels": list(trainer.LABELS),
            "opponent_action_labels": list(trainer.OPPONENT_ACTION_LABELS),
            "value_fields": list(trainer.VALUE_FIELDS),
            "response_private_state_masked": list(trainer.PRIVATE_STATE_INDICES),
            "state_dim": 48,
            "profile_dim": 12,
            "hist_feat_dim": 15,
            "cross_hand_dim": 20,
            "cross_hand_sequence_dim": 16 if temporal_cross_hand else 0,
            "hero_action_dim": trainer.HERO_ACTION_DIM,
            "rule_action_dim": trainer.RULE_ACTION_DIM,
            "lower_calibration": {
                field: {"offsets": [10.0] * trainer.NUM_ACTIONS}
                for field in trainer.VALUE_FIELDS
            },
            "response_calibration": {"temperature": 2.0},
            "model": {
                "gru_hidden": 5,
                "max_hist": 16,
                "cross_sequence_hidden": 4 if temporal_cross_hand else 0,
                "max_cross_hands": 32,
                "cross_sequence_encoder": cross_sequence_encoder or "none",
                "cross_transformer_heads": 2,
                "cross_moe_experts": 3,
            },
            "training": {"clips": clips},
        },
        "weights": {key: value.detach().tolist() for key, value in model.state_dict().items()},
    }
    pure = runtime.OpponentMultiTaskRuntime(payload)
    hero = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.02, 0.1, 0.0, 1.0]

    for history_length, cross_length in (
        (0, 0), (1, 1), (6, 6), (15, 15), (16, 32), (16, 40)
    ):
        sample = {
            "state": [0.01] * 48,
            "profile": [0.02] * 12,
            "cross_hand": [0.03] * 20,
            "cross_hand_sequence": [
                [0.004 * (step + 1)] * 16
                for step in range(cross_length)
            ],
            "history": [[0.001 * (step + 1)] * 15 for step in range(history_length)],
            "target": [0.0] * 6,
            "rule_id": 1,
            "candidate_id": None,
        }
        (
            state, profile, history, lengths, cross,
            cross_sequence, cross_lengths,
        ) = trainer._context_tensors(
            [sample], max_hist=16, device="cpu"
        )
        with torch.no_grad():
            rule_action = torch.nn.functional.one_hot(
                torch.tensor([1]), num_classes=trainer.NUM_ACTIONS
            ).float()
            latent = model.encode(
                state, profile, history, lengths, cross,
                rule_action=rule_action,
                cross_sequence=cross_sequence,
                cross_lengths=cross_lengths,
            )
            torch_values = model.value(latent)
            public_state = state.clone()
            public_state[:, list(trainer.PRIVATE_STATE_INDICES)] = 0.0
            response_latent = model.encode(
                public_state, profile, history, lengths, cross,
                response=True,
                cross_sequence=cross_sequence,
                cross_lengths=cross_lengths,
            )
            torch_response = model.response(
                response_latent, torch.tensor([hero], dtype=torch.float32)
            )[0]

        actual_values = pure.predict_values(
            sample["state"], sample["profile"], sample["history"],
            sample["cross_hand"], 1,
            sample["cross_hand_sequence"],
        )
        for field in trainer.VALUE_FIELDS:
            raw = torch_values[field][0].tolist()
            expected_mean = [
                value * clips[field] for value in raw[:trainer.NUM_ACTIONS]
            ]
            expected_lower = [
                value * clips[field] + 10.0
                for value in raw[trainer.NUM_ACTIONS:]
            ]
            expected_mean[1] = 0.0
            expected_lower[1] = 0.0
            expected = expected_mean + expected_lower
            actual = actual_values[field]["mean"] + actual_values[field]["lower"]
            assert max(abs(left - right) for left, right in zip(expected, actual)) < 1e-3

        actual_response = pure.predict_response(
            sample["state"], sample["profile"], sample["history"],
            sample["cross_hand"], hero,
            sample["cross_hand_sequence"],
        )
        expected_probabilities = torch.softmax(
            torch_response[:len(trainer.OPPONENT_ACTION_LABELS)] / 2.0, dim=0
        ).tolist()
        actual_probabilities = [
            actual_response["probabilities"][label]
            for label in trainer.OPPONENT_ACTION_LABELS
        ]
        assert max(
            abs(left - right)
            for left, right in zip(expected_probabilities, actual_probabilities)
        ) < 1e-6
        expected_ratio = 4.0 * torch.sigmoid(torch_response[-1]).item()
        assert actual_response["raise_pot_ratio"] == pytest.approx(expected_ratio, abs=1e-6)


def _strict_runtime_fixture() -> tuple[dict, dict]:
    torch.manual_seed(43)
    model = trainer.OpponentAwareMultiTaskNet(
        48,
        12,
        gru_hidden=5,
        hidden=16,
        latent=9,
        cross_hidden=7,
        head_hidden=11,
        dropout=0.0,
        cross_sequence_hidden=4,
        cross_sequence_encoder="gru",
    )
    model.eval()
    payload = {
        "meta": {
            "format": "opp_multitask_gru_v2",
            "labels": list(trainer.LABELS),
            "opponent_action_labels": list(trainer.OPPONENT_ACTION_LABELS),
            "value_fields": list(trainer.VALUE_FIELDS),
            "response_private_state_masked": list(trainer.PRIVATE_STATE_INDICES),
            "state_dim": 48,
            "profile_dim": 12,
            "hist_feat_dim": 15,
            "cross_hand_dim": 20,
            "cross_hand_sequence_dim": 16,
            "hero_action_dim": trainer.HERO_ACTION_DIM,
            "rule_action_dim": trainer.RULE_ACTION_DIM,
            "model": {
                "gru_hidden": 5,
                "max_hist": 16,
                "cross_sequence_hidden": 4,
                "max_cross_hands": 32,
                "cross_sequence_encoder": "gru",
            },
            "training": {
                "clips": {field: 2000.0 for field in trainer.VALUE_FIELDS}
            },
        },
        "weights": {
            key: value.detach().tolist()
            for key, value in model.state_dict().items()
        },
    }
    inputs = {
        "state": [0.01] * 48,
        "profile": [0.02] * 12,
        "history": [[0.03] * 15],
        "cross_hand": [0.04] * 20,
        "cross_sequence": [[0.05] * 16],
        "hero_action": [1.0] + [0.0] * (trainer.HERO_ACTION_DIM - 1),
        "rule_id": 1,
    }
    return payload, inputs


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("state", [0.01] * 47),
        ("profile", [0.02] * 11),
        ("history", [[0.03] * 14]),
        ("cross_hand", [0.04] * 19),
        ("cross_sequence", [[0.05] * 15]),
        ("rule_id", 6),
    ],
)
def test_runtime_fails_closed_on_context_dimension_mismatch(
    field: str,
    bad_value,
) -> None:
    payload, inputs = _strict_runtime_fixture()
    pure = runtime.OpponentMultiTaskRuntime(payload)
    assert pure.predict_values(
        inputs["state"],
        inputs["profile"],
        inputs["history"],
        inputs["cross_hand"],
        inputs["rule_id"],
        inputs["cross_sequence"],
    )

    inputs[field] = bad_value

    assert pure.predict_values(
        inputs["state"],
        inputs["profile"],
        inputs["history"],
        inputs["cross_hand"],
        inputs["rule_id"],
        inputs["cross_sequence"],
    ) == {}


def test_runtime_fails_closed_on_hero_action_dimension_mismatch() -> None:
    payload, inputs = _strict_runtime_fixture()
    pure = runtime.OpponentMultiTaskRuntime(payload)

    assert pure.predict_response(
        inputs["state"],
        inputs["profile"],
        inputs["history"],
        inputs["cross_hand"],
        inputs["hero_action"],
        inputs["cross_sequence"],
    )
    assert pure.predict_response(
        inputs["state"],
        inputs["profile"],
        inputs["history"],
        inputs["cross_hand"],
        inputs["hero_action"][:-1],
        inputs["cross_sequence"],
    ) == {}


def test_runtime_fails_closed_on_malformed_weight_matrix() -> None:
    payload, inputs = _strict_runtime_fixture()
    payload = copy.deepcopy(payload)
    payload["weights"]["value_heads.delta_vs_rule.0.weight"][0].pop()
    pure = runtime.OpponentMultiTaskRuntime(payload)

    assert pure.predict_values(
        inputs["state"],
        inputs["profile"],
        inputs["history"],
        inputs["cross_hand"],
        inputs["rule_id"],
        inputs["cross_sequence"],
    ) == {}


def test_runtime_rejects_malformed_shared_input_contract() -> None:
    payload, _ = _strict_runtime_fixture()
    payload["weights"]["shared.0.weight"][0].pop()

    with pytest.raises(ValueError, match="shared encoder input contract mismatch"):
        runtime.OpponentMultiTaskRuntime(payload)


def test_runtime_requires_schema_for_nonlegacy_state() -> None:
    payload, _ = _strict_runtime_fixture()
    payload["meta"]["state_dim"] = 66
    payload["meta"]["response_private_state_masked"] = (
        list(range(5, 10)) + list(range(48, 66))
    )

    with pytest.raises(ValueError, match="missing state feature schema"):
        runtime.OpponentMultiTaskRuntime(payload)


def test_runtime_rejects_private_state_mask_contract_mismatch() -> None:
    payload, _ = _strict_runtime_fixture()
    payload["meta"]["response_private_state_masked"] = list(range(5, 9))

    with pytest.raises(ValueError, match="legacy state feature contract mismatch"):
        runtime.OpponentMultiTaskRuntime(payload)


def test_runtime_infers_legacy_schema_and_private_mask() -> None:
    payload, _ = _strict_runtime_fixture()
    payload["meta"].pop("response_private_state_masked")
    pure = runtime.OpponentMultiTaskRuntime(payload)

    assert pure.state_feature_schema == "legacy48_v1"
    assert pure.response_private_state_masked == tuple(range(5, 10))


def test_runtime_supports_explicit_legacy_context_only_model() -> None:
    model_path = (
        ROOT
        / "bots"
        / "neural_national_lab"
        / "versions"
        / "v150_national_v149_multitask_p1_shadow_tcp"
        / "opp_multitask_p1_large_seed101.json"
    )
    pure = runtime.OpponentMultiTaskRuntime.load(model_path)

    assert pure is not None
    assert pure.value_input_contract == "context_only_v1"
    assert pure.response_encoder_contract == "shared_context_public_v1"
    assert pure.predict_values(
        [0.0] * pure.state_dim,
        [0.0] * pure.profile_dim,
        [],
        [0.0] * pure.cross_hand_dim,
        1,
    )
    assert pure.predict_response(
        [0.0] * pure.state_dim,
        [0.0] * pure.profile_dim,
        [],
        [0.0] * pure.cross_hand_dim,
        [0.0] * pure.hero_action_dim,
    )


def test_ensemble_uses_member_and_seed_uncertainty() -> None:
    class Stub:
        labels = list(trainer.LABELS)
        response_labels = list(trainer.OPPONENT_ACTION_LABELS)
        value_fields = list(trainer.VALUE_FIELDS)
        max_hist = 16

        def __init__(self, offset: float) -> None:
            self.offset = offset

        def predict_values(self, *_args):
            return {
                field: {
                    "mean": [self.offset + index for index in range(6)],
                    "lower": [self.offset + index - 2.0 for index in range(6)],
                }
                for field in self.value_fields
            }

        def predict_response(self, *_args):
            return {
                "probabilities": {
                    label: (0.6 if label == "fold" else 0.1)
                    for label in self.response_labels
                },
                "raise_pot_ratio": 1.0 + self.offset,
            }

    ensemble = ensemble_runtime.OpponentMultiTaskEnsemble(
        [Stub(0.0), Stub(2.0)], std_multiplier=1.0
    )

    values = ensemble.predict_values([], [], [], [], 1)
    response = ensemble.predict_response([], [], [], [], [])

    assert values["match_delta_vs_rule"]["mean"][2] == 3.0
    assert values["match_delta_vs_rule"]["std"][2] == 1.0
    assert values["match_delta_vs_rule"]["lower"][2] == 0.0
    assert values["match_delta_vs_rule"]["mean"][1] == 0.0
    assert response["probabilities"]["fold"] == pytest.approx(0.6)
    assert response["raise_pot_ratio"] == 2.0
    assert response["members"] == 2
