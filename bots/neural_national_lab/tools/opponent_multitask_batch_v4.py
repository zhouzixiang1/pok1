"""Tensor collation for v4 absolute 70-hand outcome supervision."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import torch

from feature_spec import LABELS
from match_outcome_schema import (
    MATCH_OUTCOME_ESTIMAND,
    MATCH_OUTCOME_SCHEMA,
    NATIONAL_MATCH_HANDS,
)
import opponent_multitask_batch_v3 as v3


MODEL_BATCH_SCHEMA = "opponent_multitask_tensor_batch_v4"
MODEL_INFERENCE_BATCH_SCHEMA = "opponent_multitask_inference_batch_v4"
STATE_DIM = v3.STATE_DIM
HISTORY_DIM = v3.HISTORY_DIM


def _outcome_supervision(
    rows: Sequence[Mapping[str, Any]],
    *,
    device: torch.device | str,
) -> dict[str, torch.Tensor]:
    targets = []
    masks = []
    uplifts = []
    baseline_net = []
    baseline_positive = []
    for row_index, row in enumerate(rows):
        raw = row.get("match_outcome_supervision")
        if not isinstance(raw, Mapping):
            raise ValueError(f"value row {row_index} lacks match outcome supervision")
        if (
            raw.get("schema") != MATCH_OUTCOME_SCHEMA
            or raw.get("estimand") != MATCH_OUTCOME_ESTIMAND
            or int(raw.get("hands", 0) or 0) != NATIONAL_MATCH_HANDS
        ):
            raise ValueError("value row has an unsupported match outcome schema")
        target = v3._binary_vector(
            raw.get("match_positive_targets"),
            field="match_positive_targets",
            dimension=len(LABELS),
        )
        mask = v3._binary_vector(
            raw.get("target_mask"),
            field="match_outcome_target_mask",
            dimension=len(LABELS),
        )
        uplift = v3._vector(
            raw.get("match_positive_uplift_targets"),
            field="match_positive_uplift_targets",
            dimension=len(LABELS),
        )
        if any(value not in (-1.0, 0.0, 1.0) for value in uplift):
            raise ValueError("match positive uplift targets must be -1, 0, or 1")
        if any(target[index] and not mask[index] for index in range(len(LABELS))):
            raise ValueError("match positive target appears outside its mask")
        if any(uplift[index] and not mask[index] for index in range(len(LABELS))):
            raise ValueError("match positive uplift appears outside its mask")
        baseline = v3._number(
            raw.get("baseline_match_net_chips"),
            field="baseline_match_net_chips",
        )
        baseline_flag = int(raw.get("baseline_match_positive", -1))
        if baseline_flag not in (0, 1) or baseline_flag != int(baseline > 0.0):
            raise ValueError("baseline match outcome is inconsistent")
        targets.append(target)
        masks.append(mask)
        uplifts.append(uplift)
        baseline_net.append(baseline)
        baseline_positive.append(baseline_flag)
    return {
        "match_positive_targets": torch.tensor(
            targets, dtype=torch.float32, device=device
        ),
        "match_positive_target_mask": torch.tensor(
            masks, dtype=torch.float32, device=device
        ),
        "match_positive_uplift_targets": torch.tensor(
            uplifts, dtype=torch.float32, device=device
        ),
        "baseline_match_net_chips": torch.tensor(
            baseline_net, dtype=torch.float32, device=device
        ),
        "baseline_match_positive": torch.tensor(
            baseline_positive, dtype=torch.float32, device=device
        ),
    }


def collate_encoded_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    response: bool,
    device: torch.device | str = "cpu",
) -> dict[str, Any]:
    batch = v3.collate_encoded_rows(rows, response=response, device=device)
    batch["schema"] = MODEL_BATCH_SCHEMA
    if response:
        return batch
    outcome = _outcome_supervision(rows, device=device)
    value_mask = batch["supervision"]["target_masks"][
        "match_delta_vs_rule"
    ]
    if not torch.equal(outcome["match_positive_target_mask"], value_mask):
        raise ValueError("match outcome and match-value masks differ")
    rule = batch["inputs"]["rule_action"]
    if not bool(
        ((outcome["match_positive_target_mask"] * rule).sum(dim=1) == 1.0).all()
    ):
        raise ValueError("rule action must have match outcome supervision")
    batch["supervision"].update(outcome)
    return batch


def collate_inference_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    response: bool,
    device: torch.device | str = "cpu",
) -> dict[str, Any]:
    batch = v3.collate_inference_rows(rows, response=response, device=device)
    batch["schema"] = MODEL_INFERENCE_BATCH_SCHEMA
    return batch
