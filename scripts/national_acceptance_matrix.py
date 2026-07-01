#!/usr/bin/env python3
"""Run a national-platform acceptance matrix for Botzone-style bots."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = ROOT / "web" / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(1, str(ROOT))

import national_acceptance as _na  # noqa: E402

BotSpec = _na.BotSpec


def _sync_root():
    _na.ROOT = ROOT
    _na.SEVER_DIR = ROOT / "sever"


def resolve_bot(token):
    _sync_root()
    return _na.resolve_bot(token)


def default_bots(limit: int):
    _sync_root()
    return _na.default_bots(limit)


async def run_matrix(bots, hands: int):
    _sync_root()
    return await _na.run_matrix(bots, hands)


def format_markdown(report):
    return _na.format_markdown(report)


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bots", nargs="*", help="Bot labels or paths. Defaults to latest + top conservative ratings.")
    parser.add_argument("--limit", type=int, default=4, help="Default bot discovery limit.")
    parser.add_argument("--hands", type=int, default=70, help="Hands per pair. Use 70 for national acceptance.")
    parser.add_argument("--output", help="JSON output path. Defaults under results/.")
    parser.add_argument("--markdown", help="Optional Markdown output path.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    bots = [resolve_bot(token) for token in args.bots] if args.bots else default_bots(args.limit)
    if len(bots) < 2:
        raise SystemExit("need at least two bots for an acceptance matrix")
    if args.hands <= 0:
        raise SystemExit("--hands must be positive")

    report = asyncio.run(run_matrix(bots, args.hands))
    output = Path(args.output) if args.output else (
        ROOT / "results" / f"national_acceptance_matrix_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    markdown = format_markdown(report)
    if args.markdown:
        md_path = Path(args.markdown)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"JSON written: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
