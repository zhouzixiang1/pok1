#!/usr/bin/env python3
"""Audit opponent-model value and behavior JSONL splits before training."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any


TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from cross_hand_sequence import (  # noqa: E402
    CROSS_HAND_SEQUENCE_DIM,
    CROSS_HAND_SEQUENCE_SCHEMA,
    MAX_CROSS_HANDS,
)

SPLITS = ("train", "val", "held_out")
VALUE_FIELDS = ("delta_vs_rule", "tail_delta_vs_rule", "match_delta_vs_rule")
ACTION_LABELS = ("fold", "check", "call", "raise", "allin")


def _read(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            row["__line__"] = line_number
            rows.append(row)
    return rows


def _opponent(row: dict[str, Any]) -> str:
    return str(row.get("_opponent_label") or row.get("opponent") or "")


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _audit_cross_hand_sequence(
    row: dict[str, Any],
    *,
    location: str,
    errors: list[str],
    required: bool,
) -> int | None:
    raw = row.get("cross_hand_sequence")
    if raw is None:
        if required:
            errors.append(f"{location}: missing cross_hand_sequence")
        return None
    if row.get("cross_hand_sequence_schema") != CROSS_HAND_SEQUENCE_SCHEMA:
        errors.append(f"{location}: invalid cross_hand_sequence_schema")
    if not isinstance(raw, list):
        errors.append(f"{location}: cross_hand_sequence must be a list")
        return None
    hand = int(row.get("hand", 0) or 0)
    expected = min(MAX_CROSS_HANDS, max(0, hand - 1))
    if len(raw) != expected:
        errors.append(
            f"{location}: cross_hand_sequence length {len(raw)} != "
            f"strictly-prior expected {expected}"
        )
    for row_index, vector in enumerate(raw):
        if not isinstance(vector, list) or len(vector) != CROSS_HAND_SEQUENCE_DIM:
            errors.append(
                f"{location}: cross_hand_sequence[{row_index}] must have "
                f"{CROSS_HAND_SEQUENCE_DIM} values"
            )
            continue
        for feature_index, value in enumerate(vector):
            if not _finite(value):
                errors.append(
                    f"{location}: cross_hand_sequence[{row_index}]"
                    f"[{feature_index}] is non-finite"
                )
                continue
            numeric = float(value)
            lower = -1.0 if feature_index == CROSS_HAND_SEQUENCE_DIM - 1 else 0.0
            if numeric < lower or numeric > 1.0:
                errors.append(
                    f"{location}: cross_hand_sequence[{row_index}]"
                    f"[{feature_index}] outside [{lower}, 1]"
                )
    return len(raw)


def audit(
    data_dir: Path,
    *,
    min_value_rows: int,
    min_behavior_rows: int,
    required_alternative_labels: set[str] | None = None,
    require_cross_hand_sequence: bool = False,
) -> dict[str, Any]:
    errors: list[str] = []
    value_rows = {
        split: _read(data_dir / f"cf_{split}.jsonl") for split in SPLITS
    }
    behavior_rows = {
        split: _read(data_dir / f"opponent_actions_{split}.jsonl") for split in SPLITS
    }
    opponents = {
        split: {
            _opponent(row)
            for row in value_rows[split] + behavior_rows[split]
            if _opponent(row)
        }
        for split in SPLITS
    }
    for left_index, left in enumerate(SPLITS):
        for right in SPLITS[left_index + 1:]:
            overlap = opponents[left] & opponents[right]
            if overlap:
                errors.append(f"opponent leakage {left}/{right}: {sorted(overlap)}")

    value_counts = {split: len(rows) for split, rows in value_rows.items()}
    behavior_counts = {split: len(rows) for split, rows in behavior_rows.items()}
    if value_counts["train"] < min_value_rows:
        errors.append(
            f"train value rows {value_counts['train']} < required {min_value_rows}"
        )
    if behavior_counts["train"] < min_behavior_rows:
        errors.append(
            f"train behavior rows {behavior_counts['train']} < required {min_behavior_rows}"
        )

    seen_value_keys = set()
    distributions = {field: [] for field in VALUE_FIELDS}
    valid_probes = invalid_probes = 0
    alternative_classes = Counter()
    sequence_lengths: list[int] = []
    for split, rows in value_rows.items():
        for row in rows:
            location = f"cf_{split}.jsonl:{row['__line__']}"
            sequence_length = _audit_cross_hand_sequence(
                row,
                location=location,
                errors=errors,
                required=require_cross_hand_sequence,
            )
            if sequence_length is not None:
                sequence_lengths.append(sequence_length)
            key = (
                _opponent(row), row.get("deck_seed_base"), row.get("bot_seed_base"),
                row.get("hand"), row.get("hand_decision_index"),
            )
            if key in seen_value_keys:
                errors.append(f"{location}: duplicate decision key {key}")
            seen_value_keys.add(key)
            masks = row.get("target_masks") or {}
            for field in VALUE_FIELDS:
                values = row.get(field)
                mask = masks.get(field) if isinstance(masks, dict) else row.get("target_mask")
                if not isinstance(values, list) or len(values) != 6:
                    errors.append(f"{location}: {field} must have six values")
                    continue
                if not isinstance(mask, list) or len(mask) != 6:
                    errors.append(f"{location}: {field} mask must have six values")
                    continue
                for index, (value, observed) in enumerate(zip(values, mask)):
                    if observed and not _finite(value):
                        errors.append(f"{location}: {field}[{index}] observed but non-finite")
                    if not observed and value is not None:
                        errors.append(f"{location}: {field}[{index}] masked but populated")
                    if observed and value is not None:
                        distributions[field].append(float(value))
            rule_id = int(row.get("rule_label_id", -1) or 0)
            if not 0 <= rule_id < 6:
                errors.append(f"{location}: invalid rule_label_id={rule_id}")
            else:
                for field in VALUE_FIELDS:
                    values = row.get(field) or []
                    if len(values) == 6 and values[rule_id] != 0.0:
                        errors.append(f"{location}: {field} rule target is not zero")
            for probe in row.get("probes") or []:
                if probe.get("status") != "ok":
                    invalid_probes += 1
                    continue
                valid_probes += 1
                alternative_classes[str(probe.get("forced_label", ""))] += 1
                if probe.get("force_confirmed") is not True:
                    errors.append(f"{location}: valid probe lacks force confirmation")
                if int(probe.get("illegal_actions", 0) or 0) != 0:
                    errors.append(f"{location}: valid probe contains illegal action")
                if probe.get("issues"):
                    errors.append(f"{location}: valid probe contains issues")
                hand_delta = probe.get("delta_vs_rule")
                tail_delta = probe.get("tail_delta_vs_rule")
                match_delta = probe.get("match_delta_vs_rule")
                if all(_finite(value) for value in (hand_delta, tail_delta, match_delta)):
                    if abs(float(hand_delta) + float(tail_delta) - float(match_delta)) > 1e-6:
                        errors.append(f"{location}: probe hand + tail != match")
    for label in sorted(required_alternative_labels or set()):
        if alternative_classes[label] <= 0:
            errors.append(f"required alternative label has no valid probes: {label}")

    seen_behavior_keys = set()
    behavior_classes = Counter()
    for split, rows in behavior_rows.items():
        for row in rows:
            location = f"opponent_actions_{split}.jsonl:{row['__line__']}"
            sequence_length = _audit_cross_hand_sequence(
                row,
                location=location,
                errors=errors,
                required=require_cross_hand_sequence,
            )
            if sequence_length is not None:
                sequence_lengths.append(sequence_length)
            key = (
                _opponent(row), row.get("deck_seed_base"), row.get("bot_seed_base"),
                row.get("hand"), row.get("hand_decision_index"),
            )
            if key in seen_behavior_keys:
                errors.append(f"{location}: duplicate behavior key {key}")
            seen_behavior_keys.add(key)
            action = str(row.get("opponent_action", ""))
            label_id = row.get("opponent_action_label_id")
            if action not in ACTION_LABELS:
                errors.append(f"{location}: unknown opponent action {action!r}")
            elif label_id != ACTION_LABELS.index(action):
                errors.append(f"{location}: action label id does not match {action}")
            else:
                behavior_classes[action] += 1
            if row.get("source") != "baseline_native_action_response":
                errors.append(f"{location}: unexpected behavior source")

    files = {}
    for split in SPLITS:
        for prefix in ("cf", "opponent_actions"):
            path = data_dir / f"{prefix}_{split}.jsonl"
            if path.exists():
                files[path.name] = {
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "bytes": path.stat().st_size,
                }
    return {
        "schema_version": 1,
        "passed": not errors,
        "errors": errors,
        "data_dir": str(data_dir.resolve()),
        "value_rows": value_counts,
        "behavior_rows": behavior_counts,
        "opponents": {split: sorted(values) for split, values in opponents.items()},
        "valid_probes": valid_probes,
        "invalid_probes_excluded_by_masks": invalid_probes,
        "alternative_classes": dict(sorted(alternative_classes.items())),
        "behavior_classes": dict(sorted(behavior_classes.items())),
        "cross_hand_sequence": {
            "required": bool(require_cross_hand_sequence),
            "schema": CROSS_HAND_SEQUENCE_SCHEMA,
            "rows": len(sequence_lengths),
            "min_hands": min(sequence_lengths) if sequence_lengths else None,
            "max_hands": max(sequence_lengths) if sequence_lengths else None,
        },
        "targets": {
            field: {
                "samples": len(values),
                "min": min(values) if values else None,
                "p05": _percentile(values, 0.05),
                "median": _percentile(values, 0.5),
                "p95": _percentile(values, 0.95),
                "max": max(values) if values else None,
            }
            for field, values in distributions.items()
        },
        "files": files,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--min-value-rows", type=int, default=1)
    parser.add_argument("--min-behavior-rows", type=int, default=1)
    parser.add_argument("--require-alternative-label", action="append", default=[])
    parser.add_argument("--require-cross-hand-sequence", action="store_true")
    args = parser.parse_args()
    report = audit(
        args.data_dir.resolve(),
        min_value_rows=args.min_value_rows,
        min_behavior_rows=args.min_behavior_rows,
        required_alternative_labels=set(args.require_alternative_label),
        require_cross_hand_sequence=args.require_cross_hand_sequence,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
