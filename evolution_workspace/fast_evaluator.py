import os
import sys
import json
import argparse
from pathlib import Path

# Add project root to sys.path to import engine
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

from battle import mirror_battle

def evaluate(bot_path, opponents, n_pairs, output_dir):
    """
    bot_path: Path to the current generation bot (e.g. bots/claude_v2/main.py)
    opponents: List of opponent bot paths
    n_pairs: Number of mirror pairs to play against each opponent
    output_dir: Directory to save the summary.json
    """
    os.makedirs(output_dir, exist_ok=True)
    
    summary = {
        "target_bot": bot_path,
        "results": {}
    }
    
    total_score = 0
    
    for opp_path in opponents:
        print(f"Testing {bot_path} vs {opp_path} ({n_pairs} mirror pairs)...")
        if not os.path.exists(opp_path):
            print(f"Warning: Opponent {opp_path} not found. Skipping.")
            continue
            
        wins, draws, n_played, all_logs = mirror_battle(bot_path, opp_path, n_games=n_pairs, verbose=False, save_log=False)
        target_wins = wins[0]
        opp_wins = wins[1]
        
        # Win rate for target
        if target_wins + opp_wins > 0:
            win_rate = target_wins / (target_wins + opp_wins)
        else:
            win_rate = 0.5
            
        summary["results"][opp_path] = {
            "wins": target_wins,
            "losses": opp_wins,
            "draws": draws,
            "win_rate": win_rate
        }
        total_score += win_rate
        
    summary["average_win_rate"] = total_score / len(opponents) if opponents else 0.0
    
    summary_path = os.path.join(output_dir, "summary.json")
    with open(summary_path, 'w') as f:
        json.dump(summary, f, indent=2)
        
    print(f"Saved summary to {summary_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("bot_path")
    parser.add_argument("opponents", nargs="+")
    parser.add_argument("-n", "--pairs", type=int, default=5)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    
    evaluate(args.bot_path, args.opponents, args.pairs, args.output_dir)
