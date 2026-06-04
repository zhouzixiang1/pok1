#!/usr/bin/env python3
"""Reset evolution state to baseline (v1-v6 only).

Usage:
    python scripts/reset_evolution.py           # Interactive confirmation
    python scripts/reset_evolution.py --force   # Skip confirmation
"""

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "web" / "core"))
sys.path.insert(0, str(PROJECT_ROOT / "engine"))


def main():
    parser = argparse.ArgumentParser(description="Reset evolution to baseline (v1-v6)")
    parser.add_argument("--force", action="store_true", help="Skip confirmation prompt")
    parser.add_argument("--keep", type=int, default=None, help="Keep versions up to N (default: auto-detect)")
    args = parser.parse_args()

    if not args.force:
        keep_msg = f"v1-v{args.keep}" if args.keep else "auto-detected range"
        print(f"This will DELETE all evolution state above {keep_msg}:")
        print(f"  - bots/ directories above keep threshold")
        print(f"  - git tags above keep threshold")
        print(f"  - all match history, ratings, replays")
        print(f"  - experience pool")
        print(f"  - orchestrator logs")
        print()
        answer = input(f"Reset evolution to baseline? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            sys.exit(0)

    from reset import reset_evolution
    result = reset_evolution(keep_versions=args.keep)

    keep_v = result["keep_versions"]
    print("\nReset complete:")
    print(f"  Kept versions: v1-v{keep_v}")
    print(f"  Daemon stopped: {result['stopped_daemon']}")
    print(f"  Daemon dead: {result['daemon_dead']}")
    print(f"  Bot dirs deleted: {len(result['deleted_bot_dirs'])} (v{result['deleted_bot_dirs'][0] if result['deleted_bot_dirs'] else '-'}..v{result['deleted_bot_dirs'][-1] if result['deleted_bot_dirs'] else '-'})")
    print(f"  Git tags deleted: {len(result['deleted_tags'])}")
    print(f"  Data files reset: {len(result['reset_files'])}")
    print(f"  Directories cleared: {result['cleared_dirs']}")
    print(f"  Log dirs deleted: {len(result['deleted_log_dirs'])}")
    print(f"  Orchestrator logs deleted: {result['deleted_orch_logs']}")
    print(f"  Sentinels ensured: {result['ensured_sentinels']}")

    # Git commit — only stage bot deletions and results changes, not unrelated files
    try:
        paths_to_add = ["bots/", "web/core/results/", "web/core/experience_pool.md", "web/logs/"]
        subprocess.run(["git", "add"] + paths_to_add, cwd=str(PROJECT_ROOT), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"chore: reset evolution to baseline (v1-v{keep_v})"],
            cwd=str(PROJECT_ROOT), check=True, capture_output=True,
        )
        print(f"\nGit committed: chore: reset evolution to baseline (v1-v{keep_v})")
    except subprocess.CalledProcessError as e:
        print(f"\nGit commit skipped (nothing to commit or error: {e})")

    print("\nReady to restart: python web/main.py")


if __name__ == "__main__":
    main()
