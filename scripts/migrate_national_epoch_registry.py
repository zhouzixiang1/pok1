#!/usr/bin/env python3
"""Migrate the runtime reaped ledger into durable annotated Git tags."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = REPO_ROOT / "web" / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from national_epoch_registry import (  # noqa: E402
    RegistryError,
    apply_migration_plan,
    build_migration_plan,
    push_registry_tags,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate national reaped state to annotated Git tags (dry-run by default)."
    )
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--ledger",
        type=Path,
        help="legacy JSONL path (default: <repo>/web/core/results/reaped_bots.jsonl)",
    )
    parser.add_argument("--apply", action="store_true", help="atomically create planned tags")
    parser.add_argument("--push", action="store_true", help="explicitly push registry tags")
    parser.add_argument("--remote", default="origin", help="remote used with --push")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.push and not args.apply:
        parser.error("--push requires --apply")

    repo_root = args.repo_root.resolve()
    ledger = args.ledger or repo_root / "web" / "core" / "results" / "reaped_bots.jsonl"
    try:
        plan = build_migration_plan(repo_root, legacy_ledger=ledger)
        payload: dict[str, object] = {
            "mode": "apply" if args.apply else "dry-run",
            "repo_root": str(repo_root),
            "ledger": str(ledger),
            "plan": plan.as_dict(),
        }
        if not args.apply:
            print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
            return 0 if plan.ready else 2
        if not plan.ready:
            payload["error"] = "migration preflight is not ready"
            print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
            return 2

        result = apply_migration_plan(plan, repo_root=repo_root)
        if args.push:
            push_registry_tags(plan.required_tags, repo_root=repo_root, remote=args.remote)
        payload["result"] = {**result.as_dict(), "pushed": bool(args.push)}
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return 0
    except RegistryError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
