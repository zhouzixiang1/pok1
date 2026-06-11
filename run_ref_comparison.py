#!/usr/bin/env python3
"""Parallel battle runner: reference bot vs all evolved bots.
Uses ProcessPoolExecutor for concurrent mirror battles.
"""
import sys, os, json, time
from concurrent.futures import ProcessPoolExecutor, as_completed

# Add engine to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'engine'))
from battle import mirror_battle

REFERENCE_BOT = os.path.abspath('bot.py')
BOTS_DIR = 'bots'
N_GAMES = 50  # mirror games per matchup (100 actual games)


def get_all_evolved_bots():
    """Get all claude_vN bots sorted by version."""
    bots = []
    for d in os.listdir(BOTS_DIR):
        if d.startswith('claude_v') and os.path.isdir(os.path.join(BOTS_DIR, d)):
            main_py = os.path.join(BOTS_DIR, d, 'main.py')
            if os.path.exists(main_py):
                v = int(d.replace('claude_v', ''))
                bots.append((v, os.path.abspath(main_py), d))
    return sorted(bots)


def run_matchup(args):
    """Run one matchup: (ref_bot, evolved_bot_path, evolved_name, n_games) -> result dict."""
    ref_path, evo_path, evo_name, n_games = args
    try:
        t0 = time.time()
        wins, draws, played, _ = mirror_battle(ref_path, evo_path, n_games=n_games)
        elapsed = time.time() - t0
        ref_wins = wins[0]  # bot0 = reference
        evo_wins = wins[1]  # bot1 = evolved
        return {
            'bot': evo_name,
            'ref_wins': ref_wins,
            'evo_wins': evo_wins,
            'draws': draws,
            'played': played,
            'elapsed': round(elapsed, 1),
            'error': None,
        }
    except Exception as e:
        return {
            'bot': evo_name,
            'ref_wins': 0,
            'evo_wins': 0,
            'draws': 0,
            'played': 0,
            'elapsed': 0,
            'error': str(e),
        }


def main():
    bots = get_all_evolved_bots()
    print(f"Found {len(bots)} evolved bots")
    print(f"Reference bot: {REFERENCE_BOT}")
    print(f"Games per matchup: {N_GAMES} mirror games")
    print(f"Running parallel battles...")
    print("=" * 70)

    # Build matchup list
    matchups = [
        (REFERENCE_BOT, evo_path, evo_name, N_GAMES)
        for _, evo_path, evo_name in bots
    ]

    results = []
    n_workers = min(12, len(matchups))

    with ProcessPoolExecutor(max_workers=n_workers) as pool:
        futures = {pool.submit(run_matchup, m): m[2] for m in matchups}
        for future in as_completed(futures):
            name = futures[future]
            result = future.result()
            results.append(result)
            status = "OK" if result['error'] is None else f"ERROR: {result['error']}"
            wr = result['evo_wins'] / max(1, result['played']) * 100
            print(f"  {name}: evo={result['evo_wins']} ref={result['ref_wins']} "
                  f"draw={result['draws']} ({result['played']} games) "
                  f"evo_wr={wr:.1f}% [{result['elapsed']}s] {status}")

    # Sort by evolved bot win rate descending
    results.sort(key=lambda r: r['evo_wins'] / max(1, r['played']), reverse=True)

    print("\n" + "=" * 70)
    print("FINAL RESULTS (sorted by evolved bot win rate)")
    print("=" * 70)
    print(f"{'Bot':<16} {'EvoW':>5} {'RefW':>5} {'Draw':>5} {'Total':>6} {'EvoWR':>7} {'Time':>6}")
    print("-" * 70)

    total_evo = 0
    total_ref = 0
    total_draw = 0
    total_played = 0

    for r in results:
        wr = r['evo_wins'] / max(1, r['played']) * 100
        print(f"{r['bot']:<16} {r['evo_wins']:>5} {r['ref_wins']:>5} "
              f"{r['draws']:>5} {r['played']:>6} {wr:>6.1f}% {r['elapsed']:>5.1f}s")
        if r['error'] is None:
            total_evo += r['evo_wins']
            total_ref += r['ref_wins']
            total_draw += r['draws']
            total_played += r['played']

    print("-" * 70)
    if total_played > 0:
        overall_wr = total_evo / total_played * 100
        print(f"{'OVERALL':<16} {total_evo:>5} {total_ref:>5} "
              f"{total_draw:>5} {total_played:>6} {overall_wr:>6.1f}%")
        print(f"\nEvolved bots overall vs reference: {overall_wr:.1f}% win rate")
        if overall_wr > 50:
            print(">>> EVOLVED BOTS ARE STRONGER <<<")
        else:
            print(">>> REFERENCE BOT IS STRONGER <<<")

    # Save results
    out_file = 'ref_vs_evolved_results.json'
    with open(out_file, 'w') as f:
        json.dump({
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'reference_bot': REFERENCE_BOT,
            'n_games': N_GAMES,
            'results': results,
            'summary': {
                'total_evo_wins': total_evo,
                'total_ref_wins': total_ref,
                'total_draws': total_draw,
                'total_played': total_played,
                'overall_evo_winrate': round(overall_wr, 1) if total_played > 0 else 0,
            }
        }, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved to {out_file}")


if __name__ == '__main__':
    main()
