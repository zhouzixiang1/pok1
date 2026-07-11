"""Shared data-building helpers for route modules.

Pure functions that accept pre-loaded data as parameters — no caching logic.
Each caller retains control of its own cache keys.
"""

import json
import re
from pathlib import Path
from typing import Any

from rating_snapshot import build_strength_rows, h2h_winrate_for_bot
from evolution_infra import count_lines


def confidence(rd: float) -> str:
    if rd < 50:
        return "very_confident"
    if rd < 100:
        return "confident"
    if rd < 200:
        return "uncertain"
    return "very_uncertain"


def build_rating_row(name: str, r_data: dict, bot_stats: dict, h2h_data: dict) -> dict:
    r, rd = r_data["r"], r_data["rd"]
    display_rd = round(rd, 1)
    bs = bot_stats.get(name, {})
    wr = h2h_winrate_for_bot(name, h2h_data)
    conservative = r - 2 * rd
    return {
        "name": name,
        "rating": round(r, 1),
        "rd": display_rd,
        "sigma": round(r_data.get("sigma", 0.06), 4),
        "conservative_rating": round(conservative, 1),
        "confidence": confidence(display_rd),
        "last_period": r_data.get("last_period", ""),
        "win_rate": bs.get("win_rate"),
        "games": bs.get("games", 0),
        "h2h_avg_wr": round(wr, 4) if wr is not None else None,
        "h2h_source": "head_to_head",
        "leaderboard_score": round(max(0.0, min(1.0, 0.5 + (conservative - 1500) / 800)), 4),
        "rank_basis": "single_bot_detail",
        "strength_confidence": confidence(display_rd),
    }


def build_ranked_ratings(
    ratings_data: dict,
    bot_stats_data: dict,
    h2h_data: dict,
    *,
    active_bots: list[str] | None = None,
    match_history_path: Path | str | None = None,
) -> list[dict]:
    if not ratings_data:
        return []
    return build_strength_rows(
        ratings_data,
        bot_stats_data,
        h2h_data,
        active_bots=active_bots,
        match_history_path=match_history_path,
    )




def build_bot_summary(
    bot_dir: Path,
    bot_name: str,
    ratings: dict,
    bot_stats_data: dict,
    h2h_data: dict,
    strength_rows: dict[str, dict] | None = None,
) -> dict:
    version_match = re.search(r"\d+", bot_name)
    version = int(version_match.group()) if version_match else 0
    py_files = list(bot_dir.glob("*.py"))
    total_lines = sum(count_lines(f) for f in py_files)
    completed = (bot_dir / ".completed").exists()
    r_data = ratings.get(bot_name)
    rating_info = None
    if r_data:
        r, rd = r_data.get("r", 1500), r_data.get("rd", 350)
        rating_info = {"r": round(r, 1), "rd": round(rd, 1), "conservative": round(r - 2 * rd, 1)}
    bs = bot_stats_data.get(bot_name, {})
    strength = (strength_rows or {}).get(bot_name, {})
    wr = strength.get("h2h_avg_wr")
    if wr is None:
        raw_wr = h2h_winrate_for_bot(bot_name, h2h_data)
        wr = round(raw_wr, 4) if raw_wr is not None else None
    summary = {
        "name": bot_name, "version": version, "completed": completed,
        "total_lines": total_lines, "files": [f.name for f in py_files], "rating": rating_info,
        "win_rate": bs.get("win_rate"), "games": bs.get("games", 0),
        "h2h_avg_wr": wr,
    }
    try:
        from official_certification import status_payload
        summary["official_certification"] = status_payload(bot_dir)
    except Exception:
        summary["official_certification"] = {
            "bot": bot_name,
            "status": "official-unavailable",
            "status_label": "official-unavailable",
            "issues": ["certification_status_unavailable"],
        }
    for key in (
        "leaderboard_score", "rank_basis", "strength_confidence", "h2h_coverage",
        "h2h_games", "h2h_opponents", "h2h_opponents_total", "h2h_source",
        "h2h_weighted_wr",
    ):
        if key in strength:
            summary[key] = strength[key]
    return summary


def build_match_stats(stats_data: dict | None) -> dict:
    if not stats_data:
        return {"total_games": 0, "total_pairs": 0, "total_periods": 0, "most_active_pair": "", "most_active_count": 0}
    pairs = stats_data.get("pairs", {})
    total_games = stats_data.get("total_games", sum(pairs.values()))
    most_active = max(pairs.items(), key=lambda x: x[1]) if pairs else ("", 0)
    return {
        "total_games": total_games, "total_pairs": len(pairs),
        "total_periods": stats_data.get("total_periods", 0),
        "most_active_pair": most_active[0], "most_active_count": most_active[1],
    }


def _bot_sort_key(name: str) -> int:
    m = re.search(r"\d+", name)
    return int(m.group()) if m else 0


def build_match_matrix(h2h_data: dict | None, ratings_data: dict | None, stats_data: dict | None) -> dict:
    if h2h_data:
        all_bots = set()
        for k in h2h_data:
            parts = k.split(" vs ")
            all_bots.update(parts)
        if ratings_data:
            all_bots &= set(ratings_data.keys())
        bot_names = sorted(all_bots, key=_bot_sort_key)
        idx = {name: i for i, name in enumerate(bot_names)}
        n = len(bot_names)
        wr_matrix = [[None] * n for _ in range(n)]
        for k, v in h2h_data.items():
            parts = k.split(" vs ")
            if len(parts) != 2:
                continue
            a, b = parts[0].strip(), parts[1].strip()
            if a in idx and b in idx:
                i, j = idx[a], idx[b]
                games = int(v.get("games", 0) or 0)
                if games > 0 and (
                    v.get("a_wins") is not None or v.get("draws") is not None
                ):
                    wr = (
                        float(v.get("a_wins", 0) or 0)
                        + 0.5 * float(v.get("draws", 0) or 0)
                    ) / games
                else:
                    wr = v.get("win_rate")
                if wr is not None:
                    wr_matrix[i][j] = round(wr, 4)
                    wr_matrix[j][i] = round(1.0 - wr, 4)
        return {"bots": bot_names, "matrix": wr_matrix, "source": "h2h"}

    # Fallback to legacy pair counts
    if not stats_data:
        return {"bots": [], "matrix": []}
    ratings = ratings_data or {}
    bot_names = sorted(ratings.keys(), key=_bot_sort_key)
    idx = {name: i for i, name in enumerate(bot_names)}
    n = len(bot_names)
    matrix = [[0] * n for _ in range(n)]
    pairs = stats_data.get("pairs", {})
    for key, count in pairs.items():
        parts = key.split(" vs ")
        if len(parts) == 2:
            a, b = parts[0].strip(), parts[1].strip()
            if a in idx and b in idx:
                i, j = idx[a], idx[b]
                matrix[i][j] = count
                matrix[j][i] = count
    return {"bots": bot_names, "matrix": matrix}


def read_jsonl(path: Path, limit: int | None = None, reverse: bool = True) -> list[dict]:
    if not path.exists():
        return []
    from evolution_infra import locked_file
    entries = []
    with locked_file(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    if reverse:
        entries.reverse()
    if limit is not None:
        entries = entries[:limit]
    return entries


def downsample(entries: list[dict], max_points: int = 200) -> list[dict]:
    max_points = max(1, max_points)
    if len(entries) <= max_points:
        return entries
    step = max(1, len(entries) // max_points)
    sampled = entries[::step]
    if entries[-1] is not sampled[-1] and entries[-1] not in sampled:
        sampled.append(entries[-1])
    return sampled


def list_generation_dirs(results_dir: Path) -> list[dict]:
    if not results_dir.exists():
        return []
    versions = []
    dirs = sorted(
        (p for p in results_dir.iterdir()
         if p.is_dir() and p.name.startswith("v") and (p / "logs").is_dir()),
        key=lambda p: _bot_sort_key(p.name),
    )
    for p in dirs:
        files = sorted(f.name for f in (p / "logs").iterdir() if f.is_file())
        versions.append({"version": p.name, "files": files})
    return versions
