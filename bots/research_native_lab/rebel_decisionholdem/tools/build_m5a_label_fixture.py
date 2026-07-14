"""Build or verify the complete M5a exact PBS/value-label fixture."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from ..decisionholdem_like.secure_files import (
    atomic_json_write,
    stable_read_path,
    stable_selected_file_map,
    strict_json_loads,
)
from ..rebel_like.label_contract import (
    M5A_CRITICAL_SOURCE_PATHS,
    build_label_artifact,
    load_m5a_config,
    validate_label_artifact,
    verify_label_artifact_files,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PACKAGE_ROOT.parent
DEFAULT_CONFIG = PACKAGE_ROOT / "configs" / "m5a_pbs_label_contract.json"
DEFAULT_OUTPUT = PACKAGE_ROOT / "artifacts" / "m5a_exact_label_fixture.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="verify the existing artifact and current source/config bindings",
    )
    return parser


def _summary(payload: dict[str, object], *, mode: str) -> dict[str, object]:
    body = payload["body"]
    assert isinstance(body, dict)
    examples = body["examples"]
    assert isinstance(examples, list)
    return {
        "ok": True,
        "mode": mode,
        "schema": payload["schema"],
        "body_sha256": payload["body_sha256"],
        "example_count": len(examples),
        "game_counts": dict(sorted(Counter(item["game"] for item in examples).items())),
        "split_counts": body["split_counts"],
        "large_training_authorized": body["large_training_authorized"],
        "network_training_started": body["network_training_started"],
        "online_search_implemented": body["online_search_implemented"],
    }


def run(args: argparse.Namespace) -> dict[str, object]:
    config_path = args.config.resolve(strict=True)
    output_path = args.output.resolve()
    config, config_sha256 = load_m5a_config(config_path)
    if args.verify:
        payload = strict_json_loads(stable_read_path(output_path))
        verified = verify_label_artifact_files(
            payload,
            config_path=config_path,
            source_root=SOURCE_ROOT,
        )
        return _summary(verified, mode="verify")

    sources = stable_selected_file_map(
        SOURCE_ROOT, sorted(M5A_CRITICAL_SOURCE_PATHS)
    )
    payload = build_label_artifact(
        config=config,
        config_file_sha256=config_sha256,
        source_snapshot=sources,
    )
    validate_label_artifact(payload, config)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(output_path, payload)
    published = strict_json_loads(stable_read_path(output_path))
    if published != payload:
        raise RuntimeError("published M5a artifact bytes decode differently")
    verify_label_artifact_files(
        published,
        config_path=config_path,
        source_root=SOURCE_ROOT,
    )
    return _summary(payload, mode="build")


def main() -> int:
    result = run(build_parser().parse_args())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
