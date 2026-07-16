#!/usr/bin/env python3
"""Test-only STDIO entrypoint with explicit in-process executor injection."""

from __future__ import annotations

import argparse
from pathlib import Path

from worker_mcp.agent_executor import MockAgentExecutor
from worker_mcp.config import load_config
from worker_mcp.server import build_server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    build_server(
        config,
        executor_factory=MockAgentExecutor,
    ).run(transport="stdio")


if __name__ == "__main__":
    main()
