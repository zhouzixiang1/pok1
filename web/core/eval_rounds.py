"""Cycle-Based Deterministic Evaluation Rounds for the Glicko daemon.

Periodically triggers evaluation rounds where ALL bot pairs are played in
deterministic alphabetical order (instead of the usual 60/40 stochastic
selection).  Results are stored in append-only JSONL and summarized for
injection into the Master Architect prompt, giving it comparable cross-
generation performance data.

Design principles:
  - OPTIONAL and additive: failures never block the normal daemon loop.
  - Deterministic pairing: sorted alphabetically → comparable across rounds.
  - Same battle mechanism (mirror_battle) as regular daemon games.
  - Compact summary (<2000 chars) for Master prompt injection.
"""

import json
import os
import time
import logging
import fcntl
from datetime import datetime
from pathlib import Path
from typing import Optional

from evolution_infra import RESULTS_DIR, locked_file, pair_key

log = logging.getLogger("pok.eval_rounds")

EVAL_ROUNDS_FILE = RESULTS_DIR / "eval_rounds.jsonl"

# Trigger a new round every N daemon games (total across all pairs)
EVAL_ROUND_GAMES = 500

# Minimum games per opponent-pair for a round to be considered "complete"
EVAL_ROUND_MIN_OPP_GAMES = 10


class EvalRoundManager:
    """Track daemon game count and manage deterministic evaluation rounds.

    Lifecycle per round:
        1. should_trigger() → True when enough games have been played
        2. start_round(active_bots) → generates deterministic pair list
        3. record_result(bot_a, bot_b, wr, games) → accumulate results
        4. finish_round() → compute deltas, persist to JSONL

    All state is in-memory.  The daemon creates one instance at startup.
    """

    def __init__(self):
        self.games_since_last_round: int = 0
        self.current_round_id: Optional[str] = None
        self.round_data: dict = {}  # pair_key -> {wr, games, wins_a, wins_b, draws}
        self.round_active_bots: list[str] = []
        self.round_start_time: float = 0.0
        self.round_pairs_remaining: set[str] = set()
        self._total_rounds_completed: int = 0

    # ──────────────────────────────────────────
    # Trigger logic
    # ──────────────────────────────────────────

    def count_game(self, n_games: int = 1) -> bool:
        """Count games played and return True if a new round should trigger.

        Called by the daemon after each completed match.
        """
        if self.current_round_id is not None:
            # Already in a round — don't trigger another
            return False
        self.games_since_last_round += n_games
        if self.games_since_last_round >= EVAL_ROUND_GAMES:
            return True
        return False

    # ──────────────────────────────────────────
    # Round lifecycle
    # ──────────────────────────────────────────

    def start_round(self, active_bots: list[str]) -> list[tuple[str, str]]:
        """Start a new evaluation round.  Returns deterministic pair list.

        The pair list is sorted alphabetically so that results are comparable
        across generations even as bots are added/removed.
        """
        sorted_bots = sorted(active_bots)
        pairs = []
        for i, a in enumerate(sorted_bots):
            for b in sorted_bots[i + 1:]:
                pairs.append((a, b))

        self.current_round_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.round_data = {}
        self.round_active_bots = sorted_bots
        self.round_start_time = time.time()
        self.round_pairs_remaining = {pair_key(a, b) for a, b in pairs}
        self.games_since_last_round = 0

        log.info(
            "Eval round %s started: %d bots, %d pairs",
            self.current_round_id, len(sorted_bots), len(pairs),
        )
        return pairs

    def record_result(self, bot_a: str, bot_b: str,
                      wins_a: int, wins_b: int, draws: int) -> None:
        """Record a match result for the current round.

        Safe to call even when no round is active (no-op).
        """
        if self.current_round_id is None:
            return

        k = pair_key(bot_a, bot_b)
        total = wins_a + wins_b + draws
        if total == 0:
            return

        # Accumulate (a round may have multiple games per pair)
        existing = self.round_data.get(k, {
            "wins_a": 0, "wins_b": 0, "draws": 0, "games": 0,
        })
        existing["wins_a"] += wins_a
        existing["wins_b"] += wins_b
        existing["draws"] += draws
        existing["games"] += total
        self.round_data[k] = existing

        # Remove from remaining once we have enough games
        if existing["games"] >= EVAL_ROUND_MIN_OPP_GAMES:
            self.round_pairs_remaining.discard(k)

    def is_round_complete(self) -> bool:
        """Check if all pairs have enough games to finish the round."""
        if self.current_round_id is None:
            return False
        return len(self.round_pairs_remaining) == 0

    def finish_round(self, h2h_data: dict | None = None) -> Optional[dict]:
        """Finalize the round, compute per-bot win-rate deltas, save to JSONL.

        Returns the round summary dict, or None if no round is active.
        """
        if self.current_round_id is None:
            return None

        # Load current H2H for delta computation
        if h2h_data is None:
            h2h_data = self._load_h2h()

        elapsed = round(time.time() - self.round_start_time, 1)

        # Compute per-bot results
        bot_results = {}
        for k, v in self.round_data.items():
            if v["games"] == 0:
                continue
            a, b = k.split(" vs ")
            wr_a = v["wins_a"] / v["games"]

            # Store per-pair result
            bot_results.setdefault(a, {})[b] = {
                "wr": round(wr_a, 4),
                "opp_wr": round(1.0 - wr_a, 4),
                "games": v["games"],
            }
            bot_results.setdefault(b, {})[a] = {
                "wr": round(1.0 - wr_a, 4),
                "opp_wr": round(wr_a, 4),
                "games": v["games"],
            }

        # Compute deltas vs historical H2H
        bot_deltas = {}
        for bot, opponents in bot_results.items():
            total_wr = 0.0
            total_games = 0
            delta_sum = 0.0
            delta_count = 0
            for opp, res in opponents.items():
                total_wr += res["wr"] * res["games"]
                total_games += res["games"]
                # Delta vs historical
                hk = pair_key(bot, opp)
                hist = h2h_data.get(hk, {})
                hist_games = hist.get("games", 0)
                if hist_games >= 10:
                    # Compute historical WR from bot's perspective
                    if bot < opp:
                        hist_wr = hist.get("a_wins", 0) / hist_games
                    else:
                        hist_wr = hist.get("b_wins", 0) / hist_games
                    delta_sum += res["wr"] - hist_wr
                    delta_count += 1
            avg_wr = total_wr / total_games if total_games > 0 else 0.5
            avg_delta = delta_sum / delta_count if delta_count > 0 else 0.0
            bot_deltas[bot] = {
                "avg_wr": round(avg_wr, 4),
                "avg_delta": round(avg_delta, 4),
                "total_games": total_games,
                "n_opponents": len(opponents),
            }

        round_summary = {
            "round_id": self.current_round_id,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "elapsed_sec": elapsed,
            "n_bots": len(self.round_active_bots),
            "n_pairs_played": len(self.round_data),
            "bot_deltas": bot_deltas,
        }

        # Append to JSONL (crash-safe: each round is one line)
        self._save_round(round_summary)

        self._total_rounds_completed += 1
        log.info(
            "Eval round %s finished: %d bots, %d pairs, %.1fs",
            self.current_round_id, len(self.round_active_bots),
            len(self.round_data), elapsed,
        )

        # Reset state
        self.current_round_id = None
        self.round_data = {}
        self.round_active_bots = []
        self.round_pairs_remaining = set()

        return round_summary

    def cancel_round(self) -> None:
        """Cancel the current round without saving (e.g. on daemon shutdown)."""
        if self.current_round_id is not None:
            log.info("Eval round %s cancelled", self.current_round_id)
        self.current_round_id = None
        self.round_data = {}
        self.round_active_bots = []
        self.round_pairs_remaining = set()

    # ──────────────────────────────────────────
    # Summary for Master prompt injection
    # ──────────────────────────────────────────

    def get_last_round_summary(self, bot_name: str, max_chars: int = 2000) -> str:
        """Get a compact summary of the most recent round for a specific bot.

        Returns a formatted table suitable for injection into the Master prompt,
        or empty string if no round data is available.
        """
        last_round = self._load_last_round()
        if not last_round:
            return ""

        bot_deltas = last_round.get("bot_deltas", {})
        bot_data = bot_deltas.get(bot_name)
        if not bot_data:
            return ""

        lines = [
            f"## Eval Round Summary (round {last_round.get('round_id', '?')})",
            f"Bot: {bot_name}, avg_wr={bot_data['avg_wr']:.2%}, "
            f"delta={bot_data['avg_delta']:+.2%}, "
            f"games={bot_data['total_games']}, "
            f"opponents={bot_data['n_opponents']}",
            "",
            "Top deltas (worst improvements first):",
        ]

        # Sort all bots by delta (worst first) for context
        all_bots = sorted(
            bot_deltas.items(),
            key=lambda x: x[1].get("avg_delta", 0.0),
        )

        for name, data in all_bots[:10]:  # Limit to top 10 entries
            delta = data.get("avg_delta", 0.0)
            wr = data.get("avg_wr", 0.5)
            games = data.get("total_games", 0)
            marker = " <<<" if name == bot_name else ""
            lines.append(
                f"  {name}: wr={wr:.2%} delta={delta:+.2%} "
                f"({games} games vs {data.get('n_opponents', 0)} opp){marker}"
            )

        result = "\n".join(lines)
        if len(result) > max_chars:
            result = result[:max_chars - 3] + "..."
        return result

    # ──────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────

    def _load_h2h(self) -> dict:
        """Load current H2H data."""
        h2h_file = RESULTS_DIR / "head_to_head.json"
        if not h2h_file.exists():
            return {}
        try:
            with locked_file(h2h_file, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_round(self, round_summary: dict) -> None:
        """Append a round summary to the JSONL file."""
        os.makedirs(RESULTS_DIR, exist_ok=True)
        try:
            with locked_file(EVAL_ROUNDS_FILE, "a") as f:
                f.write(json.dumps(round_summary, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            log.warning("Failed to save eval round: %s", e)

    def _load_last_round(self) -> Optional[dict]:
        """Load the most recent round from the JSONL file."""
        if not EVAL_ROUNDS_FILE.exists():
            return None
        try:
            last_line = None
            with locked_file(EVAL_ROUNDS_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        last_line = line
            if last_line:
                return json.loads(last_line)
        except (json.JSONDecodeError, OSError):
            pass
        return None

    @property
    def is_active(self) -> bool:
        """Whether an eval round is currently in progress."""
        return self.current_round_id is not None

    @property
    def total_rounds_completed(self) -> int:
        return self._total_rounds_completed
