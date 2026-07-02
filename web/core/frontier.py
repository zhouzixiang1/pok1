"""Read-side frontier summaries for Master planning.

This is a conservative first integration: it does not change reap/selection
policy directly. It turns existing MAP-Elites telemetry into a compact planning
signal so the archive is no longer write-only.
"""

from __future__ import annotations

from typing import Any

from map_elites import archive_cells, read_behavior_archive


def load_behavior_archive() -> dict[str, Any]:
    return read_behavior_archive()


def frontier_summary(limit: int = 8) -> str:
    archive = load_behavior_archive()
    if not archive:
        return "Frontier/MAP-Elites: no behavior archive available yet."

    cells = archive_cells(archive)
    if not isinstance(cells, dict) or not cells:
        return "Frontier/MAP-Elites: archive is empty."

    entries = []
    for niche, raw in cells.items():
        if not isinstance(raw, dict):
            continue
        bot = raw.get("bot") or raw.get("name") or raw.get("label") or "unknown"
        fitness = raw.get("fitness")
        bc = raw.get("bc") or raw.get("behavior") or {}
        entries.append({
            "niche": niche,
            "bot": bot,
            "fitness": fitness,
            "bc": bc,
            "raw": raw,
        })

    def _score(item):
        value = item.get("fitness")
        try:
            return float(value)
        except (TypeError, ValueError):
            return -1.0

    entries.sort(key=_score, reverse=True)
    filled = len(entries)
    lines = [
        f"Frontier/MAP-Elites: {filled} filled behavior niche(s).",
        "Use this as a diversity signal: prefer changes that either improve the source bot's current niche or explore underrepresented niches without weakening national legality.",
    ]
    for item in entries[:limit]:
        fitness = item.get("fitness")
        fitness_text = f"{float(fitness):.3f}" if isinstance(fitness, (int, float)) else "unknown"
        bc = item.get("bc") if isinstance(item.get("bc"), dict) else {}
        bc_text = ", ".join(f"{k}={v}" for k, v in list(bc.items())[:4]) or "bc=unknown"
        lines.append(f"- {item['niche']}: {item['bot']} fitness={fitness_text}; {bc_text}")
    return "\n".join(lines)
