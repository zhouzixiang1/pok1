"""
WebUI: BaseUI implementation that broadcasts evolution events via SSE
and also prints to terminal.
"""

import asyncio
import json
import time
import threading
from collections import deque
from typing import Any

import sys
from pathlib import Path

from evolution_core import BaseUI, Glicko2Player


class EventBroadcaster:
    """
    Fan-out broadcaster with ring buffer for late joiners.

    Each client gets its own asyncio.Queue. A shared ring buffer stores
    the last N events for replay when a new client connects.
    """

    def __init__(self, buffer_size=500):
        self._clients: dict[int, asyncio.Queue] = {}
        self._ring_buffer: deque[dict] = deque(maxlen=buffer_size)
        self._next_id = 0
        self._lock = threading.Lock()

    def add_client(self) -> tuple[int, asyncio.Queue]:
        with self._lock:
            cid = self._next_id
            self._next_id += 1
            q: asyncio.Queue = asyncio.Queue(maxsize=2000)
            self._clients[cid] = q
            # Replay ring buffer
            for event in self._ring_buffer:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    break
            return cid, q

    def remove_client(self, cid: int):
        with self._lock:
            self._clients.pop(cid, None)

    def broadcast(self, event_type: str, payload: dict):
        """Sync-safe broadcast. Stores in ring buffer, pushes to all queues."""
        payload["ts"] = time.time()
        sse_data = {"event": event_type, "data": json.dumps(payload)}
        with self._lock:
            self._ring_buffer.append(sse_data)
            for q in self._clients.values():
                try:
                    q.put_nowait(sse_data)
                except asyncio.QueueFull:
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        q.put_nowait(sse_data)
                    except asyncio.QueueFull:
                        pass


class WebUI(BaseUI):
    """
    Dual-output UI: prints to terminal AND broadcasts via SSE.
    Pass is_text_ui=False so all LLM output routes through log_io().
    """

    def __init__(self, broadcaster: EventBroadcaster):
        self._broadcaster = broadcaster
        self.grand_cost_total = 0.0
        self.gen_cost_total = 0.0
        self._messages = []
        self._state: dict[str, Any] = {
            "status": "Initializing...",
            "is_working": False,
            "header": "Evolution Framework",
            "metrics": {},
            "ratings": [],
            "active_bots": [],
        }

    def _emit(self, event_type: str, payload: dict):
        self._broadcaster.broadcast(event_type, payload)

    # ── BaseUI interface ──

    def log_history(self, msg, status="info"):
        icon = {"info": "[INFO]", "warn": "[WARN]", "error": "[ERR]",
                "success": "[OK]"}.get(status, "[INFO]")
        self._messages.append(f"[{status}] {msg}")
        print(f"{icon} {msg}")
        self._emit("history", {"msg": msg, "status": status})

    def set_status(self, msg, is_working=False):
        self._state["status"] = msg
        self._state["is_working"] = is_working
        work_icon = "..." if is_working else "OK"
        print(f"[STATUS] {work_icon} {msg}")
        self._emit("status", {"msg": msg, "is_working": is_working})

    def log_io(self, msg, stream_type="default"):
        prefix_map = {
            "prompt": "[PROMPT] ",
            "claude": "[CLAUDE] ",
            "thinking": "[THINK] ",
            "tool": "[TOOL] ",
            "error": "[ERR] ",
        }
        prefix = prefix_map.get(stream_type, "  ")
        for line in msg.split("\n"):
            if line.strip():
                print(f"{prefix}{line}")
        self._emit("io", {"msg": msg, "stream_type": stream_type})

    def clear_io(self):
        self._emit("clear_io", {})

    def update_eval_table(self, ratings, active_bots):
        rows = []
        active_list = [(b, ratings.get(b, Glicko2Player())) for b in active_bots]
        active_list.sort(key=lambda x: x[1].r, reverse=True)
        for i, (bot, p) in enumerate(active_list):
            rows.append({
                "rank": i + 1,
                "name": bot,
                "rating": round(p.r, 1),
                "rd": round(p.rd, 1),
                "conservative": round(p.r - 2 * p.rd, 1),
            })
        self._state["ratings"] = rows
        self._state["active_bots"] = list(active_bots)
        self._emit("eval_table", {"rows": rows})

    def update_daemon_status(self, stats, ratings):
        pairs = stats.get("pairs", {})
        self._emit("daemon", {
            "total_matches": sum(pairs.values()),
            "total_periods": stats.get("total_periods", 0),
            "total_games": stats.get("total_games", 0),
            "n_bots": len(ratings),
        })

    def set_header(self, msg):
        self._state["header"] = msg
        self._emit("header", {"msg": msg})

    def update_cost(self, role, cost_usd, usage):
        if cost_usd is not None:
            self.gen_cost_total += cost_usd
            self.grand_cost_total += cost_usd
            in_tok = usage.get("input_tokens", 0) if usage else 0
            out_tok = usage.get("output_tokens", 0) if usage else 0
            print(f"[COST] {role}: ${cost_usd:.4f} (in={in_tok} out={out_tok})")
            self._emit("cost", {
                "role": role,
                "cost_usd": cost_usd,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "gen_total": round(self.gen_cost_total, 4),
                "grand_total": round(self.grand_cost_total, 4),
            })

    def update_metrics(self, metrics):
        self._state["metrics"] = metrics
        m = metrics
        print(f"[METRICS] v{m.get('current_v','?')}→v{m.get('next_v','?')} | "
              f"Rate: {m.get('success_rate',0):.0%} | "
              f"Trend: {m.get('rating_trend',0):+.0f} | "
              f"Cost: ${self.grand_cost_total:.3f}")
        self._emit("metrics", metrics)

    def emit_tool_call(self, tool_name: str, args: dict):
        """Broadcast a structured tool call event for expandable display in the Dashboard."""
        self._emit("tool_call", {"tool_name": tool_name, "args": args})

    def reset_gen_cost(self):
        self.gen_cost_total = 0.0

    def get_state(self) -> dict:
        return {
            **self._state,
            "grand_cost_total": round(self.grand_cost_total, 4),
            "gen_cost_total": round(self.gen_cost_total, 4),
        }

    def get_output(self):
        return "\n".join(self._messages[-20:])
