"""In-process national-platform acceptance runner.

This module runs Botzone-style JSON bots through sever/bot_adapter.py and the
national GameEngine without opening TCP sockets. It is the reusable gate API;
scripts/national_acceptance_matrix.py is only a CLI wrapper.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any

from pipeline_schema import NationalAcceptanceResult


ROOT = Path(__file__).resolve().parents[2]
SEVER_DIR = ROOT / "sever"
if str(SEVER_DIR) not in sys.path:
    sys.path.insert(0, str(SEVER_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(1, str(ROOT))

from bot_adapter import BotAdapter  # noqa: E402
from engine.game import GameEngine  # noqa: E402
from engine.thp_recorder import THPRecorder  # noqa: E402


@dataclass(frozen=True)
class BotSpec:
    label: str
    path: Path


class MatrixBotAdapter(BotAdapter):
    """BotAdapter variant that receives server messages in-process."""

    def __init__(self, bot: BotSpec):
        super().__init__("127.0.0.1", 0, str(bot.path), bot.label)
        self.outbox: list[str] = []

    async def _send_line(self, msg: str):
        self.outbox.append(msg)

    async def feed(self, msg: str):
        await self._handle(msg)

    def pop_action(self) -> str | None:
        if not self.outbox:
            return None
        return self.outbox.pop(0)


class MatrixGameEngine(GameEngine):
    """GameEngine wrapper that connects two in-process adapters."""

    def __init__(self, adapters: list[MatrixBotAdapter], recorder: THPRecorder | None = None):
        self.adapters = adapters
        self.events: list[dict[str, Any]] = []
        super().__init__(
            send_func=self._send_to_adapter,
            broadcast_func=self._record_event,
            recorder=recorder,
        )

    async def _send_to_adapter(self, player_idx: int, message: str):
        await self.adapters[player_idx].feed(message)

    async def _recv_action(self, player_idx: int) -> str | None:
        return self.adapters[player_idx].pop_action()

    async def _record_event(self, event: dict[str, Any]):
        self.events.append(dict(event))

    async def run_limited_match(self, name1: str, name2: str, hands: int):
        self.players[0].name = name1
        self.players[1].name = name2
        self.total_earnings = [0, 0]
        self.match_over = False

        for hand_num in range(1, hands + 1):
            self.hand_num = hand_num
            result = await self._run_hand(hand_num)
            if result is None:
                break
            self.total_earnings[0] += result.earnings[0]
            self.total_earnings[1] += result.earnings[1]
            if self.match_over:
                break


def _bot_version(label: str) -> int:
    if label.startswith("claude_v"):
        suffix = label.removeprefix("claude_v")
    elif label.startswith("v"):
        suffix = label.removeprefix("v")
    else:
        suffix = ""
    return int(suffix) if suffix.isdigit() else -1


def resolve_bot(token: str | Path) -> BotSpec:
    token_str = str(token)
    raw = Path(token_str)
    candidates: list[Path] = []
    if raw.exists():
        candidates.append(raw)
    if token_str.startswith("v") and token_str[1:].isdigit():
        candidates.append(ROOT / "bots" / f"claude_v{token_str[1:]}")
    if token_str.isdigit():
        candidates.append(ROOT / "bots" / f"claude_v{token_str}")
        candidates.append(ROOT / "bots" / f"bot{token_str}")
    if token_str.startswith("claude_v") or token_str.startswith("bot"):
        candidates.append(ROOT / "bots" / token_str)

    for path in candidates:
        if path.is_dir() and (path / "main.py").exists():
            return BotSpec(path.name, path.resolve())
        if path.is_file():
            return BotSpec(path.parent.name if path.name == "main.py" else path.stem, path.resolve())
    raise ValueError(f"bot not found or missing main.py: {token_str}")


def _completed_claude_bots() -> list[BotSpec]:
    bots_dir = ROOT / "bots"
    specs = []
    for path in bots_dir.glob("claude_v*"):
        if not path.is_dir() or not (path / "main.py").exists():
            continue
        if not (path / ".completed").exists():
            continue
        specs.append(BotSpec(path.name, path.resolve()))
    return sorted(specs, key=lambda b: _bot_version(b.label), reverse=True)


def _is_completed_claude_bot(spec: BotSpec) -> bool:
    if not spec.label.startswith("claude_v"):
        return True
    bot_dir = spec.path if spec.path.is_dir() else spec.path.parent
    return (bot_dir / ".completed").exists()


def default_bots(limit: int) -> list[BotSpec]:
    chosen: list[BotSpec] = []
    seen: set[str] = set()

    def add(spec: BotSpec):
        if spec.label not in seen and spec.path.exists():
            chosen.append(spec)
            seen.add(spec.label)

    completed = _completed_claude_bots()
    if completed:
        add(completed[0])

    ratings_path = ROOT / "web" / "core" / "results" / "glicko_ratings.json"
    if ratings_path.exists():
        ratings = json.loads(ratings_path.read_text(encoding="utf-8"))
        ranked = sorted(
            ratings.items(),
            key=lambda item: item[1].get("r", 1500) - 2 * item[1].get("rd", 350),
            reverse=True,
        )
        for label, _ in ranked:
            try:
                spec = resolve_bot(label)
            except ValueError:
                continue
            if not _is_completed_claude_bot(spec):
                continue
            add(spec)
            if len(chosen) >= limit:
                break

    for spec in completed:
        add(spec)
        if len(chosen) >= limit:
            break
    return chosen[:limit]


def select_acceptance_opponents(candidate: BotSpec, source_v: int | None, limit: int = 2) -> list[BotSpec]:
    chosen: list[BotSpec] = []
    seen = {candidate.label}

    def add(spec: BotSpec):
        if spec.label not in seen and spec.path.exists():
            chosen.append(spec)
            seen.add(spec.label)

    if source_v is not None:
        try:
            add(resolve_bot(f"claude_v{source_v}"))
        except ValueError:
            pass
    for spec in default_bots(limit + 2):
        add(spec)
        if len(chosen) >= limit:
            break
    return chosen[:limit]


def _event_counts(events: list[dict[str, Any]], player_idx: int, prefix: str) -> int:
    return sum(
        1
        for event in events
        if event.get("type") == "action"
        and event.get("player_idx") == player_idx
        and str(event.get("action", "")).startswith(prefix)
    )


def _critical_adapter_issues(telemetry: dict[str, Any]) -> list[str]:
    issues = []
    for key in (
        "bot_failures",
        "invalid_actions",
        "would_be_illegal_raise",
        "clamped_raises",
    ):
        value = int(telemetry.get(key, 0) or 0)
        if value:
            issues.append(f"{key}={value}")
    return issues


async def run_pair(bot_a: BotSpec, bot_b: BotSpec, hands: int) -> dict[str, Any]:
    adapters = [MatrixBotAdapter(bot_a), MatrixBotAdapter(bot_b)]
    for adapter in adapters:
        adapter.bot.start()

    recorder = THPRecorder(bot_a.label, bot_b.label)
    engine = MatrixGameEngine(adapters, recorder=recorder)
    try:
        await engine.run_limited_match(bot_a.label, bot_b.label, hands)
    finally:
        for adapter in adapters:
            adapter.bot.close()

    per_player = {}
    issues = []
    for idx, spec in enumerate((bot_a, bot_b)):
        illegal = _event_counts(engine.events, idx, "illegal:")
        timeout = _event_counts(engine.events, idx, "timeout")
        telemetry = dict(adapters[idx].telemetry)
        per_player[spec.label] = {
            "earnings": engine.total_earnings[idx],
            "illegal_actions": illegal,
            "timeouts": timeout,
            "adapter": telemetry,
        }
        if illegal:
            issues.append(f"{spec.label}: illegal_actions={illegal}")
        if timeout:
            issues.append(f"{spec.label}: timeouts={timeout}")
        for detail in _critical_adapter_issues(telemetry):
            issues.append(f"{spec.label}: {detail}")

    if engine.hand_num != hands:
        issues.append(f"hands_played={engine.hand_num}, expected={hands}")

    return {
        "bot_a": bot_a.label,
        "bot_b": bot_b.label,
        "hands_requested": hands,
        "hands_played": engine.hand_num,
        "per_player": per_player,
        "net_chips_a": engine.total_earnings[0],
        "net_chips_b": engine.total_earnings[1],
        "net_chips_a_per_hand": round(engine.total_earnings[0] / max(1, engine.hand_num), 3),
        "passed_compliance": not issues,
        "issues": issues,
    }


async def run_matrix(bots: list[BotSpec], hands: int) -> dict[str, Any]:
    results = []
    for i, bot_a in enumerate(bots):
        for bot_b in bots[i + 1:]:
            results.append(await run_pair(bot_a, bot_b, hands))

    summary = {
        bot.label: {
            "matches": 0,
            "net_chips": 0,
            "illegal_actions": 0,
            "timeouts": 0,
            "bot_failures": 0,
            "invalid_actions": 0,
            "clamped_raises": 0,
            "allin_conversions": 0,
            "would_be_illegal_raise": 0,
            "postflop_pass_conversions": 0,
            "passed_compliance": True,
        }
        for bot in bots
    }
    matrix: dict[str, dict[str, Any]] = {bot.label: {} for bot in bots}

    for result in results:
        a = result["bot_a"]
        b = result["bot_b"]
        for label in (a, b):
            pdata = result["per_player"][label]
            adapter = pdata["adapter"]
            summary[label]["matches"] += 1
            summary[label]["net_chips"] += pdata["earnings"]
            summary[label]["illegal_actions"] += pdata["illegal_actions"]
            summary[label]["timeouts"] += pdata["timeouts"]
            for key in (
                "bot_failures",
                "invalid_actions",
                "clamped_raises",
                "allin_conversions",
                "would_be_illegal_raise",
                "postflop_pass_conversions",
            ):
                summary[label][key] += int(adapter.get(key, 0) or 0)
            summary[label]["passed_compliance"] = (
                summary[label]["passed_compliance"]
                and result["passed_compliance"]
                and pdata["illegal_actions"] == 0
                and pdata["timeouts"] == 0
                and not _critical_adapter_issues(adapter)
            )

        matrix[a][b] = {
            "net_chips": result["net_chips_a"],
            "per_hand": result["net_chips_a_per_hand"],
            "passed_compliance": result["passed_compliance"],
            "issues": result["issues"],
        }
        matrix[b][a] = {
            "net_chips": result["net_chips_b"],
            "per_hand": round(result["net_chips_b"] / max(1, result["hands_played"]), 3),
            "passed_compliance": result["passed_compliance"],
            "issues": result["issues"],
        }

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "hands_per_pair": hands,
        "bots": [{"label": bot.label, "path": str(bot.path)} for bot in bots],
        "results": results,
        "summary": summary,
        "matrix": matrix,
    }


async def run_acceptance_for_candidate(
    candidate_token: str | Path,
    *,
    source_v: int | None = None,
    opponent_tokens: list[str | Path] | None = None,
    hands: int = 10,
    max_opponents: int = 2,
) -> NationalAcceptanceResult:
    candidate = resolve_bot(candidate_token)
    if opponent_tokens:
        opponents = [resolve_bot(token) for token in opponent_tokens]
    else:
        opponents = select_acceptance_opponents(candidate, source_v, limit=max_opponents)
    bots = [candidate] + [opp for opp in opponents if opp.label != candidate.label]
    if len(bots) < 2:
        return NationalAcceptanceResult(
            candidate=candidate.label,
            opponents=[],
            hands_per_pair=hands,
            passed=False,
            issues=["need at least one opponent for national acceptance"],
        )
    report = await run_matrix(bots, hands)
    candidate_summary = report["summary"].get(candidate.label, {})
    issues = []
    for result in report["results"]:
        if result["bot_a"] == candidate.label or result["bot_b"] == candidate.label:
            issues.extend(result.get("issues", []))
    passed = bool(candidate_summary.get("passed_compliance")) and not issues
    return NationalAcceptanceResult(
        candidate=candidate.label,
        opponents=[opp.label for opp in bots[1:]],
        hands_per_pair=hands,
        passed=passed,
        issues=issues,
        summary=candidate_summary,
        matrix=report.get("matrix", {}).get(candidate.label, {}),
        report=report,
    )


def run_acceptance_for_candidate_sync(*args, **kwargs) -> NationalAcceptanceResult:
    return asyncio.run(run_acceptance_for_candidate(*args, **kwargs))


def format_markdown(report: dict[str, Any]) -> str:
    labels = [bot["label"] for bot in report["bots"]]
    lines = [
        f"# National Acceptance Matrix ({report['generated_at']})",
        "",
        f"Hands per pair: {report['hands_per_pair']}",
        "",
        "| bot | compliance | net chips | illegal | timeout | bot failures | invalid actions | clamped raises | allin conversions |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label in labels:
        row = report["summary"][label]
        lines.append(
            "| {label} | {compliance} | {net} | {illegal} | {timeout} | {failures} | {invalid} | {clamped} | {allin} |".format(
                label=label,
                compliance="PASS" if row["passed_compliance"] else "FAIL",
                net=row["net_chips"],
                illegal=row["illegal_actions"],
                timeout=row["timeouts"],
                failures=row["bot_failures"],
                invalid=row["invalid_actions"],
                clamped=row.get("clamped_raises", 0),
                allin=row.get("allin_conversions", 0),
            )
        )

    lines.extend(["", "## Pairwise Net Chips Per Hand", ""])
    header = "| bot | " + " | ".join(labels) + " |"
    sep = "|---" + "|---:" * len(labels) + "|"
    lines.extend([header, sep])
    for row_label in labels:
        cells = []
        for col_label in labels:
            if row_label == col_label:
                cells.append("-")
                continue
            cell = report["matrix"].get(row_label, {}).get(col_label)
            if not cell:
                cells.append("")
                continue
            status = "PASS" if cell["passed_compliance"] else "FAIL"
            cells.append(f"{cell['per_hand']} ({status})")
        lines.append("| " + row_label + " | " + " | ".join(cells) + " |")

    failures = [result for result in report["results"] if not result["passed_compliance"]]
    if failures:
        lines.extend(["", "## Issues", ""])
        for result in failures:
            lines.append(f"- {result['bot_a']} vs {result['bot_b']}: " + "; ".join(result["issues"]))
    return "\n".join(lines) + "\n"
