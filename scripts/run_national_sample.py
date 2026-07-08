#!/usr/bin/env python3
"""Run the root official sample script against a chosen national TCP endpoint."""

from __future__ import annotations

import argparse
from pathlib import Path
import runpy
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def _patched_source(source: str, *, host: str, port: int) -> str:
    marker = 'server_address = ("47.98.125.65", 10001)'
    replacement = f"server_address = ({host!r}, {int(port)})"
    if marker not in source:
        raise ValueError("sample script server_address marker not found")
    return source.replace(marker, replacement, 1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", default=str(ROOT / "untitled0-1.py"), help="Official sample script path.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10001)
    parser.add_argument("--name", default="", help="Accepted for launcher compatibility; the sample has a fixed name.")
    parser.add_argument("--seat", default="", help="Accepted for launcher compatibility; ignored.")
    parser.add_argument("--log", default="", help="Accepted for launcher compatibility; ignored.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source_path = Path(args.script).expanduser().resolve()
    source = source_path.read_text(encoding="utf-8")
    patched = _patched_source(source, host=args.host, port=args.port)
    with tempfile.TemporaryDirectory(prefix="pok_official_sample_") as tmp:
        temp_script = Path(tmp) / source_path.name
        temp_script.write_text(patched, encoding="utf-8")
        sys.argv = [str(temp_script)]
        runpy.run_path(str(temp_script), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

