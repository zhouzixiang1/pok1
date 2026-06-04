import sys
import os
from pathlib import Path

# Add core/ to sys.path (core/ → web/ → pok/)
CORE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CORE_DIR.parent.parent
sys.path.append(str(CORE_DIR))

from engine.battle import mirror_battle

def smoke_test(target_bot_path):
    # Use reference_bots for a stable opponent (always available, never graveyarded)
    stable_bot_path = str(CORE_DIR / "reference_bots" / "bot6" / "main.py")
    if not os.path.exists(stable_bot_path):
        stable_bot_path = str(PROJECT_ROOT / "bots" / "bot6" / "main.py")
    if not os.path.exists(stable_bot_path):
        stable_bot_path = str(PROJECT_ROOT / "bots" / "bot1" / "main.py")
        
    try:
        # Run exactly 1 match (2 games because of mirror)
        # This will quickly trigger runtime exceptions if the bot code has severe bugs
        mirror_battle(target_bot_path, stable_bot_path, 1)
        print("Smoke test passed successfully.")
        sys.exit(0)
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python smoke_tester.py <target_bot_main.py>")
        sys.exit(1)
        
    smoke_test(sys.argv[1])
