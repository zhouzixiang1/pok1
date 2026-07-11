#!/usr/bin/env python3
"""Counterfactual probe plus deterministic replay of exact strategy context."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any


TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import native_tcp_counterfactual_probe as base_probe  # noqa: E402
from feature_spec import LABELS  # noqa: E402
from strategy_context_trace_rows import attach_strategy_context  # noqa: E402


EVALUATOR = TOOLS / "native_tcp_evaluate.py"


def _canonical_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


def enrich_payload(
    payload: dict[str, Any], trace_payload: dict[str, Any]
) -> dict[str, Any]:
    trace_rows = trace_payload.get("rows")
    if not isinstance(trace_rows, list) or len(trace_rows) != 1:
        raise ValueError("strategy replay must contain exactly one native match")
    replay = trace_rows[0]
    if (
        replay.get("leg") != "forward"
        or replay.get("passed_compliance") is not True
        or int(replay.get("candidate_illegal", -1)) != 0
        or int(replay.get("candidate_timeouts", -1)) != 0
        or replay.get("wrapper_used") is True
        or replay.get("issues")
    ):
        raise ValueError("strategy replay failed native compliance")
    if int(replay.get("net_chips", 0)) != int(payload.get("baseline_net_chips", 0)):
        raise ValueError("strategy replay changed baseline net chips")
    native = replay.get("candidate_native")
    decision_trace = native.get("decision_trace") if isinstance(native, dict) else None
    if not isinstance(decision_trace, list) or not decision_trace:
        raise ValueError("strategy replay contains no candidate decision trace")
    attached, join = attach_strategy_context(
        list(payload.get("rows") or []), decision_trace
    )
    if any(
        "strategy_context_features" in row
        for row in payload.get("behavior_rows") or []
    ):
        raise ValueError("strategy context leaked into opponent-response rows")
    enriched = dict(payload)
    enriched["execution_mode"] = "native_tcp_counterfactual_strategy_context"
    enriched["rows"] = attached
    enriched["strategy_context_join"] = join
    enriched["strategy_context_replay"] = {
        "format": trace_payload.get("format"),
        "deck_seed_base": replay.get("deck_seed_base"),
        "bot_seed_base": replay.get("bot_seed_base"),
        "hands_played": replay.get("hands_played"),
        "net_chips": replay.get("net_chips"),
        "passed_compliance": replay.get("passed_compliance"),
        "candidate_illegal": replay.get("candidate_illegal"),
        "candidate_timeouts": replay.get("candidate_timeouts"),
        "wrapper_used": replay.get("wrapper_used"),
        "trace_sha256": _canonical_sha256(trace_payload),
    }
    return enriched


def _trace_replay(args: argparse.Namespace, output: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        str(EVALUATOR),
        "--candidate", str(args.candidate),
        "--opponent", str(args.opponent),
        "--hands", str(args.hands),
        "--seeds", str(args.seed_base),
        "--bot-seed-base", str(args.bot_seed_base),
        "--workers", "1",
        "--timeout-sec", str(args.timeout_sec),
        "--trace-decisions",
        "--output", str(output),
    ]
    completed = subprocess.run(
        command,
        cwd=base_probe.ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=float(args.timeout_sec) * 2.0 + 60.0,
    )
    if completed.returncode != 0 or not output.exists():
        raise RuntimeError(
            f"strategy trace replay failed ({completed.returncode}): "
            f"{completed.stderr[-1000:]}"
        )
    return json.loads(output.read_text(encoding="utf-8"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--opponent", required=True)
    parser.add_argument("--hands", type=int, default=10)
    parser.add_argument("--seed-base", type=int, required=True)
    parser.add_argument("--bot-seed-base", type=int, required=True)
    parser.add_argument(
        "--stage",
        choices=["any", "preflop", "flop", "turn", "river"],
        default="any",
    )
    parser.add_argument("--min-hand", type=int, default=1)
    parser.add_argument("--min-opponent-actions", type=int, default=0)
    parser.add_argument("--max-opponent-actions", type=int, default=0)
    parser.add_argument("--max-opponent-raise-rate", type=float, default=None)
    parser.add_argument("--initial-sb-only", action="store_true")
    parser.add_argument("--initial-sb-max-to-call", type=float, default=60.0)
    parser.add_argument("--max-decisions", type=int, default=8)
    parser.add_argument("--max-alternatives", type=int, default=2)
    parser.add_argument("--probe-workers", type=int, default=1)
    parser.add_argument(
        "--decision-sampling", choices=("first", "uniform"), default="uniform"
    )
    parser.add_argument("--rule-label", action="append", default=[], choices=LABELS)
    parser.add_argument(
        "--alternative-label", action="append", default=[], choices=LABELS
    )
    parser.add_argument("--timeout-sec", type=float, default=90.0)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--jsonl-output", type=Path)
    parser.add_argument("--behavior-jsonl-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args.probe_workers = max(1, min(4, int(args.probe_workers)))
    args.allowed_rule_label_ids = {
        int(LABELS.index(label)) for label in args.rule_label
    }
    args.allowed_alternative_label_ids = {
        int(LABELS.index(label)) for label in args.alternative_label
    }
    payload = asyncio.run(base_probe._collect(args))
    with tempfile.TemporaryDirectory(prefix="pok_strategy_trace_") as temporary:
        trace_payload = _trace_replay(args, Path(temporary) / "trace.json")
    payload = enrich_payload(payload, trace_payload)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if args.jsonl_output:
        args.jsonl_output.parent.mkdir(parents=True, exist_ok=True)
        args.jsonl_output.write_text(
            "\n".join(
                json.dumps(row, separators=(",", ":"))
                for row in payload["rows"]
            ) + "\n",
            encoding="utf-8",
        )
    if args.behavior_jsonl_output:
        args.behavior_jsonl_output.parent.mkdir(parents=True, exist_ok=True)
        args.behavior_jsonl_output.write_text(
            "\n".join(
                json.dumps(row, separators=(",", ":"))
                for row in payload["behavior_rows"]
            ) + "\n",
            encoding="utf-8",
        )
    print(json.dumps({
        "rows": len(payload["rows"]),
        "behavior_rows": len(payload["behavior_rows"]),
        "strategy_context_join": payload["strategy_context_join"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
