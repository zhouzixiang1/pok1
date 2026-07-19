"""Emit representative payloads through the real Python SSE producers.

The Node contract test consumes this JSON and runs the production TypeScript
validators over it. All filesystem writes are confined to one temporary
directory; repository runtime evidence is read-only.
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "web" / "core"))


def _drain(queue) -> dict[str, object]:
    captured: dict[str, object] = {}
    while not queue.empty():
        event = queue.get_nowait()
        captured[str(event["event"])] = json.loads(event["data"])
    return captured


def _evolution_payloads(temp_root: Path) -> dict[str, object]:
    import epoch_authority
    import event_bus
    import web_ui as web_ui_module
    from logging_config import SSEHandler
    from orchestrator_cost_policy import GenerationCostPolicy, GenerationCostScope
    from server.state import app_state
    from system_log import set_ui
    from web_ui import EventBroadcaster, WebUI

    web_ui_module._COSTS_FILE = temp_root / "llm_costs.jsonl"
    event_bus.EVENTS_FILE = temp_root / "events.jsonl"
    broadcaster = EventBroadcaster()
    broadcaster.bind_authority("a" * 64)
    _, queue = broadcaster.add_client("a" * 64)
    ui = WebUI(broadcaster)
    set_ui(ui)

    # The production status schema is intentionally accepted only when the
    # producer can bind it to one canonical active checkpoint.  This capture
    # exercises the positive path rather than treating an inactive WebUI
    # process-local message as a valid browser event.
    epoch_authority.strict_epoch_projection = lambda: {
        "evaluation_epoch": "national_tcp_policy_v1",
        "state": "fresh_bootstrap_ready",
        "initialized": True,
        "active_generation": {
            "run_id": "143#1",
            "workflow_run_id": "generation:143:producer",
            "checkpoint_revision": 7,
            "stage": "master_planning",
        },
    }
    app_state.task_snapshot = lambda: {
        "present": True,
        "done": False,
        "shutdown_requested": False,
        "status_eligible": True,
        "owner_id": "e" * 32,
        "lifecycle_revision": 7,
    }

    ui.log_history("producer history", "success")
    ui.set_status("producer status", True)
    ui.log_io("producer io", "thinking", "Master")
    ui.clear_io()
    ui.update_eval_table({}, [])
    ui.update_daemon_status({}, {})
    ui.set_header("producer header")
    ui.update_cost("Master", 0.01, {"input_tokens": 3, "output_tokens": 2})
    policy = GenerationCostPolicy()
    scope = GenerationCostScope("generation:143:producer", policy, 1.0)
    ui.begin_generation_cost(
        scope.generation_id,
        0.01,
        scope.receipt(spent_before_usd=0.01),
    )
    ui.update_metrics({"current_v": 142, "next_v": 143, "success_rate": 1.0})
    ui.emit_tool_call("run_master", {"next_v": 143}, "Orchestrator")

    normal_log = SSEHandler(broadcaster)
    normal_log.emit(logging.LogRecord(
        "pok.producer",
        logging.INFO,
        __file__,
        1,
        "producer log",
        (),
        None,
    ))
    dropped_log = SSEHandler(broadcaster, max_rate=1)
    dropped_log._drop_summary_every = 1
    dropped_log._timestamps = [time.time()]
    dropped_log.emit(logging.LogRecord(
        "pok.producer",
        logging.INFO,
        __file__,
        1,
        "producer throttled log",
        (),
        None,
    ))
    event_bus.emit(
        "pipeline.producer_contract",
        "critical",
        "producer system event",
        next_v=143,
    )
    from server.routes._helpers import post_publication_handoff_projection

    broadcaster.broadcast(
        "post_publication_handoff",
        {
            **post_publication_handoff_projection(enabled=False),
            "stream_authority_digest": "a" * 64,
        },
    )
    return _drain(queue)


def _data_payloads(temp_root: Path) -> dict[str, object]:
    import server.routes.bots as bots_route
    from evolution_infra import update_bot_stats, update_h2h
    from rate_limiter import RateLimiter
    from rating_snapshot import build_strength_rows
    from server.routes._helpers import (
        _filter_strict_match_rows,
        build_match_matrix,
        build_match_stats,
        list_generation_dirs,
    )
    from server.routes.data_stream import (
        _get_bot_stats,
        _get_daemon_status,
        _get_h2h,
        _get_history,
        _get_recent_matches,
    )

    active = ["national_v143", "national_v144"]
    ratings = {
        "national_v143": {"r": 1510.0, "rd": 90.0, "sigma": 0.06, "last_period": "2026-07-15T00:00:00"},
        "national_v144": {"r": 1490.0, "rd": 95.0, "sigma": 0.06, "last_period": "2026-07-15T00:00:00"},
    }
    h2h: dict[str, dict] = {}
    update_h2h(h2h, active[0], active[1], 1, 0, 0)
    bot_stats: dict[str, dict] = {}
    update_bot_stats(bot_stats, active[0], 1, 0, 0)
    update_bot_stats(bot_stats, active[1], 0, 1, 0)
    rows = build_strength_rows(
        ratings,
        bot_stats,
        h2h,
        active_bots=active,
        match_history_path=temp_root / "missing-match-history.jsonl",
        h2h_is_authoritative=True,
    )

    bot_root = temp_root / "bots"
    for name in active:
        bot_dir = bot_root / name
        bot_dir.mkdir(parents=True)
        (bot_dir / "policy.py").write_text("def decide(context):\n    return {'intent': 'pass'}\n", encoding="utf-8")
        (bot_dir / ".completed").write_text("complete\n", encoding="utf-8")
    bots_route.BOTS_DIR = bot_root
    generation_identities = {
        name: {
            "generation_ordinal": ordinal,
            "canonical_version": int(name.removeprefix("national_v")),
            "canonical_bot_name": name,
            "canonical_tag": f"national-bot-v{name.removeprefix('national_v')}",
        }
        for ordinal, name in enumerate(active, start=1)
    }
    bot_listing = bots_route.build_bot_listing(
        ratings,
        bot_stats,
        h2h,
        include_history=False,
        active_names=active,
        generation_identities=generation_identities,
        strength_rows_data=rows,
        strength_evidence_available=True,
    )

    identity = "d" * 64
    match = {
        "id": "producer-match",
        "timestamp": "2026-07-15T00:00:00",
        "execution_mode": "native_tcp",
        "evaluation_epoch": "national_tcp_policy_v1",
        "evaluation_identity_digest": identity,
        "bot0": active[0],
        "bot1": active[1],
        "bot0_wins": 1,
        "bot1_wins": 0,
        "draws": 0,
        "strength_sample_unit": "70_hand_match",
        "hands_per_strength_sample": 70,
        "strength_admitted": True,
        "strength_complete": True,
        "strength_compliance_passed": True,
        "strength_sample_count": 1,
        "net_chips_bot0": [125.0],
    }
    matches = _filter_strict_match_rows(
        [match],
        active_bots=set(active),
        evaluation_identity_digest=identity,
    )
    history = [{
        "period": 1,
        "timestamp": "2026-07-15T00:00:00",
        "ratings": {name: {"r": value["r"], "rd": value["rd"]} for name, value in ratings.items()},
        "win_rates": {name: {"h2h_avg_wr": row["h2h_avg_wr"], "games": 1} for name, row in zip(active, rows)},
    }]
    snapshot = {
        "match_history": matches,
        "rating_history": history,
        "h2h": h2h,
        "bot_stats": bot_stats,
    }
    generation = temp_root / "results" / "v143" / "logs"
    generation.mkdir(parents=True)
    (generation / "worker.log").write_text("producer\n", encoding="utf-8")
    limiter = RateLimiter(temp_root / "rate-limit.json")
    return {
        "ratings": rows,
        "daemon": _get_daemon_status({}),
        "rate_limit": {"blocked": limiter.is_blocked()},
        "bots": bot_listing,
        "stats": build_match_stats({"pairs": {"national_v143 vs national_v144": 1}, "total_games": 1, "total_periods": 1}),
        "matches": _get_recent_matches(100, snapshot),
        "generations": list_generation_dirs(temp_root / "results", allowed_versions={143}),
        "matrix": build_match_matrix(h2h, ratings, {"pairs": {}}),
        "history": _get_history(snapshot),
        "h2h": _get_h2h(snapshot),
        "bot_stats": _get_bot_stats(snapshot),
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pok-sse-contract-") as raw_temp:
        temp_root = Path(raw_temp)
        print(json.dumps({
            "evolution": _evolution_payloads(temp_root),
            "data": _data_payloads(temp_root),
        }, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
