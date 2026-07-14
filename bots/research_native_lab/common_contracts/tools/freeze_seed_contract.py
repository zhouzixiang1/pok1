"""Create reproducible development seeds; final entropy is generated post-freeze."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from ..seeds import DEVELOPMENT_SPLITS, SeedPartition


def _exclusive_json(path: Path, payload: dict, *, mode: int) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return hashlib.sha256(encoded).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public", type=Path, required=True)
    parser.add_argument("--master-seed", type=int, required=True)
    for split in DEVELOPMENT_SPLITS:
        parser.add_argument(f"--{split}-count", type=int, required=True)
    args = parser.parse_args(argv)

    master_seed = args.master_seed
    counts = {
        split: int(getattr(args, f"{split.replace('-', '_')}_count"))
        for split in DEVELOPMENT_SPLITS
    }
    partition = SeedPartition.freeze(master_seed, counts)
    public_payload = partition.public_manifest()
    public_sha256 = _exclusive_json(args.public, public_payload, mode=0o644)
    print(
        json.dumps(
            {
                "public": str(args.public),
                "public_sha256": public_sha256,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
