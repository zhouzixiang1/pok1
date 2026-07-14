#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


_SIDE_RE = re.compile(r"g(\d+):")


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _paired_deltas(path: Path, candidate_label: str | None) -> tuple[str, dict[int, float]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    paired = data.get("paired_vs_baseline") or {}
    if candidate_label is None:
        labels = list(paired)
        if len(labels) != 1:
            raise SystemExit(f"--candidate-label required; paired file has {labels}")
        candidate_label = labels[0]
    if candidate_label not in paired:
        raise SystemExit(f"candidate label {candidate_label!r} not found in {path}")
    values = [float(v) for v in paired[candidate_label].get("delta_net_chips", [])]
    pairs = data.get("pairs") or []
    out: dict[int, float] = {}
    for idx, row in enumerate(pairs):
        if idx >= len(values):
            break
        seed = row.get("seed")
        if seed is None:
            continue
        out[int(seed)] = values[idx]
    return candidate_label, out


def _probe_seed(payload: dict[str, Any], probe: dict[str, Any]) -> int | None:
    seed_base = payload.get("seed_base")
    if seed_base is None:
        return None
    seed_offset = int(probe.get("shard_seed_offset", payload.get("seed_offset", 0)) or 0)
    seed_stride = int(payload.get("seed_stride", 1) or 1)
    side = str(probe.get("side") or "")
    match = _SIDE_RE.search(side)
    game_idx = int(match.group(1)) if match else 0
    return int(seed_base) + seed_offset + game_idx * seed_stride


def _passes_filter(probe: dict[str, Any], stage: str, kind: str) -> bool:
    if probe.get("status") != "ok":
        return False
    if stage != "any" and probe.get("stage") != stage:
        return False
    if kind != "any" and probe.get("kind") != kind:
        return False
    features = probe.get("advantage_features")
    return isinstance(features, list) and bool(features)


def _interaction_rows(
    paths: list[Path],
    paired_seed_delta: dict[int, float],
    positive_threshold: float,
    weight_scale: float,
    stage: str,
    kind: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    scanned = 0
    skipped = 0
    missing_seed_delta = 0
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for probe in payload.get("probes", []):
            scanned += 1
            if not _passes_filter(probe, stage, kind):
                skipped += 1
                continue
            seed = _probe_seed(payload, probe)
            if seed is None or seed not in paired_seed_delta:
                missing_seed_delta += 1
                continue
            group_delta = float(paired_seed_delta[seed])
            weight = max(0.05, min(5.0, abs(group_delta) / max(1.0, weight_scale)))
            rows.append(
                {
                    "features": [float(value) for value in probe["advantage_features"]],
                    "target": 1 if group_delta > positive_threshold else 0,
                    "weight": weight,
                    "delta": group_delta,
                    "source": str(path),
                    "probe_index": probe.get("probe_index"),
                    "stage": probe.get("stage"),
                    "kind": probe.get("kind"),
                    "candidate_label_id": probe.get("candidate_label_id"),
                    "rule_label_id": probe.get("rule_label_id"),
                    "top_conf": probe.get("top_conf"),
                    "advised_final": probe.get("advised_final"),
                    "base_final": probe.get("base_final"),
                    "group_seed": seed,
                    "group_delta": group_delta,
                    "local_primary_delta": probe.get("primary_delta"),
                    "local_match_delta": probe.get("match_delta"),
                    "local_hand_delta": probe.get("hand_delta"),
                    "label_source": "paired_group_delta",
                }
            )
    summary = {
        "interaction_scanned_probes": scanned,
        "interaction_skipped_probes": skipped,
        "interaction_missing_seed_delta": missing_seed_delta,
        "interaction_rows": len(rows),
    }
    return rows, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-input", action="append", type=Path, default=[])
    parser.add_argument("--probe-input", action="append", type=Path, required=True)
    parser.add_argument("--paired-input", required=True, type=Path)
    parser.add_argument("--candidate-label")
    parser.add_argument("--positive-threshold", type=float, default=0.0)
    parser.add_argument("--weight-scale", type=float, default=1000.0)
    parser.add_argument("--stage", choices=["any", "preflop", "flop", "turn", "river"], default="any")
    parser.add_argument(
        "--kind",
        choices=["any", "to_raise", "to_call", "to_fold", "to_allin", "fold_to_call"],
        default="any",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()

    base_rows: list[dict[str, Any]] = []
    for path in args.base_input:
        base_rows.extend(_iter_jsonl(path))

    candidate_label, seed_delta = _paired_deltas(args.paired_input, args.candidate_label)
    interaction, summary = _interaction_rows(
        args.probe_input,
        seed_delta,
        args.positive_threshold,
        args.weight_scale,
        args.stage,
        args.kind,
    )
    rows = base_rows + interaction
    if not rows:
        raise SystemExit("no rows generated")
    dim = len(rows[0].get("features") or [])
    bad_dims = [len(row.get("features") or []) for row in rows if len(row.get("features") or []) != dim]
    if bad_dims:
        raise SystemExit(f"inconsistent feature dimensions: expected {dim}, got {bad_dims[:5]}")

    positives = sum(1 for row in rows if int(row.get("target", 0)) == 1)
    zero_delta = sum(1 for row in rows if float(row.get("delta", 0.0)) == 0.0)
    full_summary = {
        "base_inputs": [str(path) for path in args.base_input],
        "probe_inputs": [str(path) for path in args.probe_input],
        "paired_input": str(args.paired_input),
        "candidate_label": candidate_label,
        "rows": len(rows),
        "base_rows": len(base_rows),
        "input_dim": dim,
        "positive": positives,
        "negative": len(rows) - positives,
        "positive_rate": positives / max(1, len(rows)),
        "zero_delta": zero_delta,
        "positive_threshold": args.positive_threshold,
        "weight_scale": args.weight_scale,
        **summary,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n",
        encoding="utf-8",
    )
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(full_summary, indent=2), encoding="utf-8")
    print(json.dumps(full_summary, indent=2))


if __name__ == "__main__":
    main()
