#!/usr/bin/env python3
"""Print redaction-safe component health without exposing routing identity."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from worker_mcp.config import load_config
from worker_mcp.healthcheck import HealthChecker
from worker_mcp.task_service import TaskService


async def run(config_path: Path, deep: bool) -> int:
    config = load_config(config_path)
    service = TaskService(config)
    await service.start()
    try:
        result = await HealthChecker(config, service).check(deep=deep)
        print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return 0 if result.status == "healthy" else 1
    finally:
        await service.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--deep", action="store_true")
    args = parser.parse_args()
    return asyncio.run(run(args.config, args.deep))


if __name__ == "__main__":
    raise SystemExit(main())
