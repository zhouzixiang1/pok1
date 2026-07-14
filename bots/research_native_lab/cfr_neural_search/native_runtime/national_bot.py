"""Command-line entry point for the real Route-B national TCP bot."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

from bots.research_native_lab.common_contracts.constants import OFFICIAL_ACTION_DELAY_SEC

from ..blueprint.artifact import BlueprintPolicy, load_blueprint_artifact
from ..blueprint.hunl_game import HUNLTrainingGame
from .socket_client import NativeBlueprintClient


def _official_delay() -> float:
    raw = os.environ.get("POK_OFFICIAL_ACTION_DELAY", str(OFFICIAL_ACTION_DELAY_SEC))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("POK_OFFICIAL_ACTION_DELAY must be numeric") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError("POK_OFFICIAL_ACTION_DELAY must be finite and nonnegative")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10001)
    parser.add_argument("--name", default="RouteBBlueprint")
    parser.add_argument("--policy-seed", type=int, required=True)
    parser.add_argument(
        "--wire-mode",
        choices=("official-raw", "local-sever-lf"),
        default="official-raw",
    )
    parser.add_argument(
        "--local-action-delay",
        type=float,
        default=None,
        help="mandatory explicit delay for local-sever-lf; use 0 for strength tests",
    )
    parser.add_argument("--match-timeout", type=float, default=180.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.wire_mode == "local-sever-lf":
        if args.local_action_delay is None:
            raise SystemExit("local-sever-lf requires explicit --local-action-delay 0")
        delay = args.local_action_delay
    else:
        if args.local_action_delay is not None:
            raise SystemExit("--local-action-delay is forbidden in official-raw mode")
        delay = _official_delay()
    game = HUNLTrainingGame()
    artifact = load_blueprint_artifact(
        args.artifact,
        game,
        root=args.artifact.parent,
    )
    client = NativeBlueprintClient(
        bot_name=args.name,
        policy=BlueprintPolicy(artifact),
        policy_seed=args.policy_seed,
        wire_mode=args.wire_mode,
        action_delay_sec=delay,
    )
    result = client.run(
        args.host,
        args.port,
        match_timeout_sec=args.match_timeout,
    )
    print(
        "ROUTE_B_TELEMETRY "
        + json.dumps(result.to_payload(), sort_keys=True, separators=(",", ":")),
        file=sys.stderr,
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
