"""Emit a reproducible local hardware/runtime snapshot as JSON."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def _meminfo() -> dict[str, int]:
    result: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        name, raw = line.split(":", 1)
        fields = raw.strip().split()
        if fields:
            result[name] = int(fields[0]) * (1024 if len(fields) > 1 and fields[1] == "kB" else 1)
    return result


def _nvidia() -> list[dict[str, str]]:
    command = [
        "nvidia-smi",
        "--query-gpu=name,driver_version,memory.total,memory.free,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        output = subprocess.run(command, check=True, capture_output=True, text=True).stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []
    keys = ("name", "driver_version", "memory_total_mib", "memory_free_mib", "utilization_percent")
    return [dict(zip(keys, (field.strip() for field in line.split(",")))) for line in output.splitlines()]


def snapshot(path: str | Path = ".") -> dict:
    memory = _meminfo()
    disk = shutil.disk_usage(Path(path).resolve())
    payload = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.replace("\n", " "),
        "logical_cpu_count": os.cpu_count(),
        "memory_total_bytes": memory.get("MemTotal"),
        "memory_available_bytes": memory.get("MemAvailable"),
        "swap_total_bytes": memory.get("SwapTotal"),
        "swap_free_bytes": memory.get("SwapFree"),
        "disk_total_bytes": disk.total,
        "disk_free_bytes": disk.free,
        "gpus": _nvidia(),
    }
    try:
        import torch

        payload["torch"] = {
            "version": torch.__version__,
            "cuda_available": torch.cuda.is_available(),
            "cuda_version": torch.version.cuda,
        }
    except Exception as exc:  # pragma: no cover - environment-dependent evidence
        payload["torch_probe_error"] = f"{type(exc).__name__}: {exc}"
    return payload


def main() -> int:
    print(json.dumps(snapshot(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
