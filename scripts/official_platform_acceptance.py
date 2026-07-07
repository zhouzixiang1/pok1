#!/usr/bin/env python3
"""Run official Windows-platform acceptance for native national TCP bots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = ROOT / "web" / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(1, str(ROOT))

from official_platform_harness import (  # noqa: E402
    OfficialPlatformConfig,
    check_environment,
    run_official_acceptance_sync,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", help="Candidate bot directory or script.")
    parser.add_argument("--opponent", help="Opponent bot directory or script for non-self-play rounds.")
    parser.add_argument("--self-play-rounds", type=int, default=5, help="Candidate-vs-candidate official rounds.")
    parser.add_argument("--opponent-rounds", type=int, default=3, help="Candidate-vs-opponent official rounds.")
    parser.add_argument("--target-hands", type=int, default=70, help="Hands required per official round.")
    parser.add_argument("--results-dir", help="Evidence output directory.")
    parser.add_argument("--exe", help="Official platform EXE path.")
    parser.add_argument("--wineprefix", help="Wine prefix prepared with Chinese font support.")
    parser.add_argument("--round-timeout", type=float, default=900.0, help="Timeout per round in seconds.")
    parser.add_argument("--no-progress-timeout", type=float, default=75.0, help="No-progress timeout in seconds.")
    parser.add_argument("--check-env", action="store_true", help="Only check Wine/Xvfb/platform prerequisites.")
    return parser.parse_args(argv)


def _config_from_args(args: argparse.Namespace) -> OfficialPlatformConfig:
    base = OfficialPlatformConfig()
    return OfficialPlatformConfig(
        exe_path=Path(args.exe).expanduser() if args.exe else base.exe_path,
        wineprefix=Path(args.wineprefix).expanduser() if args.wineprefix else base.wineprefix,
        results_dir=Path(args.results_dir).expanduser() if args.results_dir else base.results_dir,
        host=base.host,
        port=base.port,
        startup_timeout_sec=base.startup_timeout_sec,
        listen_timeout_sec=base.listen_timeout_sec,
        no_progress_timeout_sec=float(args.no_progress_timeout),
        round_timeout_sec=float(args.round_timeout),
        settlement_grace_sec=base.settlement_grace_sec,
        ui=base.ui,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = _config_from_args(args)
    env_report = check_environment(config)
    if args.check_env:
        print(json.dumps(env_report, ensure_ascii=False, indent=2))
        return 0 if env_report["ok"] else 1
    if not args.candidate:
        print("error: --candidate is required unless --check-env is used", file=sys.stderr)
        return 2
    if not env_report["ok"]:
        print(json.dumps(env_report, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    result = run_official_acceptance_sync(
        args.candidate,
        opponent=args.opponent,
        self_play_rounds=args.self_play_rounds,
        opponent_rounds=args.opponent_rounds,
        target_hands=args.target_hands,
        config=config,
    )
    payload = result.model_dump()
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    report = payload.get("report", {})
    suite_dir = report.get("summary", {}).get("suite_dir")
    if suite_dir:
        print(f"summary_json={Path(suite_dir) / 'summary.json'}")
    if result.issues:
        print(json.dumps({"issues": result.issues}, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
