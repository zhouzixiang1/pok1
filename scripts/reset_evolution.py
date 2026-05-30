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
    parser.add_argument("--keep", type=int, default=6, help="Keep versions up to N (default: 6)")
    args = parser.parse_args()

    if not args.force:
        print(f"This will DELETE all evolution state above v{args.keep}:")
        print(f"  - bots/claude_v{args.keep+1}+ directories")
        print(f"  - git tags bot-v{args.keep+1}+")
        print(f"  - all match history, ratings, replays")
        print(f"  - experience pool")
        print(f"  - orchestrator logs")
        print()
        answer = input(f"Reset evolution to v1-v{args.keep} baseline? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            sys.exit(0)

    from reset import reset_evolution
    result = reset_evolution(keep_versions=args.keep)

    print("\nReset complete:")
    print(f"  Daemon stopped: {result['stopped_daemon']}")
    print(f"  Bot dirs deleted: {len(result['deleted_bot_dirs'])} (v{result['deleted_bot_dirs'][0] if result['deleted_bot_dirs'] else '-'}..v{result['deleted_bot_dirs'][-1] if result['deleted_bot_dirs'] else '-'})")
    print(f"  Git tags deleted: {len(result['deleted_tags'])}")
    print(f"  Data files reset: {len(result['reset_files'])}")
    print(f"  Directories cleared: {result['cleared_dirs']}")
    print(f"  Log dirs deleted: {len(result['deleted_log_dirs'])}")
    print(f"  Orchestrator logs deleted: {result['deleted_orch_logs']}")

    # Git commit
    try:
        subprocess.run(["git", "add", "-A"], cwd=str(PROJECT_ROOT), check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"chore: reset evolution to baseline (v1-v{args.keep})"],
            cwd=str(PROJECT_ROOT), check=True, capture_output=True,
        )
        print(f"\nGit committed: chore: reset evolution to baseline (v1-v{args.keep})")
    except subprocess.CalledProcessError as e:
        print(f"\nGit commit skipped (nothing to commit or error: {e})")

    print("\nReady to restart: python web/main.py")


if __name__ == "__main__":
    main()
