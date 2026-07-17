"""
WebUI: BaseUI implementation that broadcasts evolution events via SSE
and also prints to terminal.
"""

import asyncio
import json
import logging
import time
import threading
import re
from collections import deque
from typing import Any

from pathlib import Path

from evolution_core import BaseUI, Glicko2Player

_COSTS_FILE = Path(__file__).resolve().parent / "results" / "llm_costs.jsonl"
_TASK_OWNER_ID_RE = re.compile(r"^[0-9a-f]{32}$")
log = logging.getLogger("pok.webui")


def _empty_transient_status_identity(*, emitted_at: float | None = None) -> dict[str, Any]:
    """Return the explicit fail-closed shape used by transient status events.

    A process-local WebUI object survives more lifecycle edges than the durable
    checkpoint it is describing.  ``None`` is therefore a meaningful identity:
    callers may still record a local stopped/error message, but an SSE/browser
    consumer must never mistake it for a current workflow status.
    """

    return {
        "run_id": None,
        "workflow_run_id": None,
        "checkpoint_revision": None,
        "stage": None,
        "task_owner_id": None,
        "task_lifecycle_revision": None,
        "emitted_at": time.time() if emitted_at is None else emitted_at,
    }


def _active_generation_status_identity() -> dict[str, Any]:
    """Read the one canonical checkpoint identity for a status publication.

    Status text is deliberately not durable evidence.  Every publication is
    stamped from ``strict_epoch_projection`` at emission time so a browser can
    reject a message from a prior process, run, revision, or stage.  Any
    authority/read-validation failure returns the explicit empty identity;
    fabricating an identity from WebUI memory would reintroduce the stale-text
    bug this boundary is intended to prevent.
    """

    identity = _empty_transient_status_identity()
    try:
        from epoch_authority import strict_epoch_projection

        projection = strict_epoch_projection()
    except Exception:
        return identity
    if not isinstance(projection, dict) or not projection.get("initialized"):
        return identity
    active = projection.get("active_generation")
    if not isinstance(active, dict):
        return identity

    run_id = active.get("run_id")
    workflow_run_id = active.get("workflow_run_id")
    revision = active.get("checkpoint_revision")
    stage = active.get("stage")
    if (
        not isinstance(run_id, str)
        or not run_id.strip()
        or not isinstance(workflow_run_id, str)
        or not workflow_run_id.strip()
        or type(revision) is not int
        or revision < 1
        or not isinstance(stage, str)
        or not stage.strip()
    ):
        return identity
    # A checkpoint can stay unchanged while the live task is replaced after a
    # retry or restart.  Bind human status to the current task owner at the
    # publication boundary; missing ownership is deliberately non-authority.
    try:
        from server.state import app_state

        task = app_state.task_snapshot()
    except Exception:
        return identity
    owner_id = task.get("owner_id") if isinstance(task, dict) else None
    lifecycle_revision = (
        task.get("lifecycle_revision") if isinstance(task, dict) else None
    )
    if (
        not isinstance(task, dict)
        or task.get("present") is not True
        or task.get("done") is not False
        or task.get("shutdown_requested") is not False
        or task.get("status_eligible") is not True
        or not isinstance(owner_id, str)
        or _TASK_OWNER_ID_RE.fullmatch(owner_id) is None
        or type(lifecycle_revision) is not int
        or lifecycle_revision < 0
    ):
        return identity
    return {
        "run_id": run_id,
        "workflow_run_id": workflow_run_id,
        "checkpoint_revision": revision,
        "stage": stage,
        "task_owner_id": owner_id,
        "task_lifecycle_revision": lifecycle_revision,
        "emitted_at": identity["emitted_at"],
    }


class EventBroadcaster:
    """
    Fan-out broadcaster with ring buffer for late joiners.

    Each client gets its own asyncio.Queue. A shared ring buffer stores
    the last N events for replay when a new client connects.
    """

    def __init__(self, buffer_size=500):
        # Each client is bound to the exact reset/publication stream digest it
        # subscribed under. Ring rows carry the same identity so a new epoch or
        # newly published active pool can never replay preceding events.
        self._clients: dict[
            int,
            tuple[asyncio.Queue, asyncio.AbstractEventLoop | None, str],
        ] = {}
        self._ring_buffer: deque[tuple[str, dict]] = deque(maxlen=buffer_size)
        self._authority_identity: str | None = None
        self._next_id = 0
        self._dropped = 0
        self._lock = threading.Lock()

    @staticmethod
    def _validated_authority_identity(identity: str | None) -> str | None:
        if identity is None:
            return None
        value = str(identity)
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("SSE authority identity must be a SHA-256 digest")
        return value

    def bind_authority(self, identity: str | None) -> None:
        """Bind future events to one stream-authority digest and clear on movement."""

        value = self._validated_authority_identity(identity)
        with self._lock:
            if value == self._authority_identity:
                return
            self._authority_identity = value
            self._ring_buffer.clear()

    def compare_and_bind_authority(
        self,
        identity: str | None,
        *,
        expected_identity: str | None,
    ) -> bool:
        """CAS a canonical authority observation without permitting rollback.

        HTTP requests sample epoch state outside the broadcaster mutex.  A
        delayed request must not overwrite a newer binding installed by a
        request that observed publication later.  Equal-value convergence is
        accepted; a changed current value rejects the stale sampler.
        """

        value = self._validated_authority_identity(identity)
        expected = self._validated_authority_identity(expected_identity)
        with self._lock:
            if self._authority_identity == value:
                return True
            if self._authority_identity != expected:
                return False
            self._authority_identity = value
            self._ring_buffer.clear()
            return True

    def authority_identity(self) -> str | None:
        with self._lock:
            return self._authority_identity

    def add_client(self, authority_identity: str) -> tuple[int, asyncio.Queue]:
        identity = self._validated_authority_identity(authority_identity)
        if identity is None:
            raise ValueError("SSE clients require initialized epoch authority")
        with self._lock:
            if identity != self._authority_identity:
                raise ValueError("SSE client authority does not match broadcaster")
            cid = self._next_id
            self._next_id += 1
            q: asyncio.Queue = asyncio.Queue(maxsize=2000)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            self._clients[cid] = (q, loop, identity)
            # Replay only rows carrying this exact stream identity. The filter
            # remains even though bind_authority clears on movement, making the
            # invariant explicit and robust to future buffer implementations.
            for event_identity, event in self._ring_buffer:
                if event_identity != identity:
                    continue
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    self._dropped += 1
                    break
            return cid, q

    def clear(self):
        with self._lock:
            self._ring_buffer.clear()

    def remove_client(self, cid: int):
        with self._lock:
            self._clients.pop(cid, None)

    def broadcast(self, event_type: str, payload: dict):
        """Thread-safe broadcast. Stores in ring buffer, pushes to all queues.

        Detects whether called from the event loop thread or a background thread
        (e.g. daemon_monitor_thread). For cross-thread calls, routes through
        loop.call_soon_threadsafe() to avoid unsafe asyncio.Queue manipulation.
        """
        payload = {**payload, "ts": time.time()}
        sse_data = {"event": event_type, "data": json.dumps(payload)}
        with self._lock:
            identity = self._authority_identity
            if identity is None:
                return
            self._ring_buffer.append((identity, sse_data))
            for cid, (q, q_loop, client_identity) in self._clients.items():
                if client_identity != identity:
                    continue
                self._safe_put(q, q_loop, sse_data)

    def _safe_put(self, q, q_loop, item):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop is q_loop:
            # Same event loop — direct put
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                self._dropped += 1
        elif q_loop is not None:
            # Cross-thread or no current loop — route through target event loop
            try:
                q_loop.call_soon_threadsafe(self._try_put, q, item)
            except RuntimeError:
                pass
        else:
            # No event loop stored for this queue (tests, CLI) — single-threaded, safe to put directly
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                self._dropped += 1

    @staticmethod
    def _try_put(q, item):
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            pass  # dropped in _safe_put caller context is not tracked for static method

    def get_stats(self):
        return {
            "dropped_events": self._dropped,
            "clients": len(self._clients),
            "authority_identity": self._authority_identity,
        }


class WebUI(BaseUI):
    """
    Dual-output UI: prints to terminal AND broadcasts via SSE.
    All LLM output routes through log_io().
    """

    def __init__(self, broadcaster: EventBroadcaster):
        self._broadcaster = broadcaster
        self.grand_cost_total = self._load_grand_cost()
        self.gen_cost_total = 0.0
        self.generation_cost_identity = None
        self.generation_cost_policy = None
        self.costs = []
        self._messages = []
        self._output_since_clear = []
        self._current_role = ""
        self._state: dict[str, Any] = {
            "status": "Initializing...",
            "is_working": False,
            # A plain status string has no authority.  The route and browser
            # display it only when this exact transient identity still agrees
            # with the canonical active checkpoint and live task owner.
            "status_identity": _empty_transient_status_identity(),
            "header": "Evolution Framework",
            "metrics": {},
            "ratings": [],
            "active_bots": [],
        }

    @staticmethod
    def _load_grand_cost() -> float:
        total = 0.0
        try:
            if _COSTS_FILE.exists():
                from evolution_infra import locked_file
                with locked_file(_COSTS_FILE, "r") as f:
                    for line in f:
                        try:
                            total += json.loads(line).get("cost_usd", 0)
                        except json.JSONDecodeError as e:
                            log.debug("Malformed cost entry: %s", e)
        except OSError as e:
            log.debug("Cost file read failed: %s", e)
        # Rotation copies digest-bound cold ranges but deliberately preserves
        # the complete append-only live ledger.  The live JSONL is therefore
        # the sole total-cost authority; adding an archive summary here would
        # count every copied row twice.
        return total

    def _emit(self, event_type: str, payload: dict):
        self._broadcaster.broadcast(event_type, payload)

    # ── BaseUI interface ──

    def log_history(self, msg, status="info"):
        icon = {"info": "[INFO]", "warn": "[WARN]", "error": "[ERR]",
                "success": "[OK]"}.get(status, "[INFO]")
        level = {"info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR,
                 "success": logging.INFO}.get(status, logging.INFO)
        self._messages.append(f"[{status}] {msg}")
        self._output_since_clear.append(f"[{status}] {msg}")
        if len(self._messages) > 200:
            self._messages = self._messages[-200:]
        if len(self._output_since_clear) > 200:
            self._output_since_clear = self._output_since_clear[-200:]
        log.log(level, "%s %s", icon, msg)
        self._emit("history", {"msg": msg, "status": status})

    def set_status(self, msg, is_working=False):
        """Set evolution status message and fine-grained per-task activity flag.

        Args:
            msg: Human-readable status text broadcast to SSE clients.
            is_working: Fine-grained activity indicator. True means a specific
                pipeline task (master/workers/review/critic/commit) is actively
                running. Distinct from AppState.running which is coarse-grained
                orchestrator loop control.
        """
        identity = _active_generation_status_identity()
        self._state["status"] = msg
        self._state["is_working"] = is_working
        self._state["status_identity"] = identity
        work_icon = "..." if is_working else "OK"
        log.info("[STATUS] %s %s", work_icon, msg)
        self._emit("status", {
            "msg": msg,
            "is_working": is_working,
            **identity,
        })

    def log_io(self, msg, stream_type="default", role=""):
        if role:
            self._current_role = role
        prefix_map = {
            "prompt": "[PROMPT] ",
            "claude": "[CLAUDE] ",
            "thinking": "[THINK] ",
            "tool": "[TOOL] ",
            "tool_result": "[RESULT] ",
            "error": "[ERR] ",
        }
        prefix = prefix_map.get(stream_type, "  ")
        for line in msg.split("\n"):
            if line.strip():
                log.debug("%s%s", prefix, line)
        # Truncate _output_since_clear in log_io too, since clear_io() is
        # skipped in parallel mode and log_io() is called hundreds of times
        # per worker. Without this, the list grows without bound.
        if len(self._output_since_clear) > 500:
            self._output_since_clear = self._output_since_clear[-400:]
        self._emit("io", {"msg": msg, "stream_type": stream_type, "role": role})

    def clear_io(self):
        self._output_since_clear.clear()
        self._emit("clear_io", {})

    def update_eval_table(self, ratings, active_bots):
        # The daemon arguments are an update signal, not display authority.
        # Reopen only the immutable cycle bound to the live reset receipt and
        # strict published pool; never derive a second table from live aliases.
        try:
            from evaluation_bundle import load_current_strict_evaluation_bundle

            bundle = load_current_strict_evaluation_bundle()
        except Exception:
            bundle = {"available": False}
        if not isinstance(bundle, dict):
            bundle = {"available": False}
        if bundle.get("available") is True:
            selection = bundle.get("selection") or {}
            rows = [
                dict(row)
                for row in (selection.get("rows") or [])
                if isinstance(row, dict)
            ]
            current_active = list(bundle.get("active_bots") or [])
        else:
            rows = []
            current_active = []
        self._state["ratings"] = rows
        self._state["active_bots"] = current_active
        self._emit("eval_table", {"rows": rows})

    def update_daemon_status(self, stats, ratings):
        try:
            from evaluation_bundle import load_current_strict_evaluation_bundle

            bundle = load_current_strict_evaluation_bundle()
        except Exception:
            bundle = {"available": False}
        if not isinstance(bundle, dict):
            bundle = {"available": False}
        if bundle.get("available") is True:
            stats = bundle.get("daemon_stats") or {}
            ratings = bundle.get("ratings") or {}
        else:
            stats = {}
            ratings = {}
        pairs = stats.get("pairs", {})
        self._emit("daemon_stats", {
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
            self.costs.append({"role": role, "cost_usd": cost_usd})
            if len(self.costs) > 500:
                self.costs = self.costs[-500:]
            self.gen_cost_total += cost_usd
            self.grand_cost_total += cost_usd
            in_tok = usage.get("input_tokens", 0) if usage else 0
            out_tok = usage.get("output_tokens", 0) if usage else 0
            try:
                _COSTS_FILE.parent.mkdir(parents=True, exist_ok=True)
                from evolution_infra import locked_file
                with locked_file(_COSTS_FILE, "a") as f:
                    f.write(json.dumps({"role": role, "cost_usd": cost_usd, "grand_total": round(self.grand_cost_total, 6), "input_tokens": in_tok, "output_tokens": out_tok, "ts": time.time()}) + "\n")
            except OSError as e:
                log.warning("Cost JSONL write failed: %s", e)
            if usage is None:
                log.warning("[COST] %s: $%.4f (usage=None - SDK missing token counts)", role, cost_usd)
            log.info("[COST] %s: $%.4f (in=%d out=%d)", role, cost_usd, in_tok, out_tok)
            self._emit("cost", {
                "role": role,
                "cost_usd": cost_usd,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "gen_total": round(self.gen_cost_total, 4),
                "grand_total": round(self.grand_cost_total, 4),
            })

    def begin_generation_cost(self, generation_id, spent_usd, policy_receipt=None):
        """Project the durable generation ledger into dashboard state.

        Unlike the old per-session counter, this is restored by workflow_run_id
        after a checkpoint hand-off or process restart.
        """
        self.generation_cost_identity = str(generation_id or "") or None
        self.gen_cost_total = max(0.0, float(spent_usd or 0.0))
        self.generation_cost_policy = dict(policy_receipt or {}) or None
        self._emit("generation_cost_policy", {
            "generation_id": self.generation_cost_identity,
            "spent_usd": round(self.gen_cost_total, 4),
            "policy": self.generation_cost_policy,
        })

    def update_metrics(self, metrics):
        self._state["metrics"] = metrics
        m = metrics
        log.info("[METRICS] v%s→v%s | Rate: %.0f%% | Trend: %+.0f | Cost: $%.3f",
                 m.get('current_v', '?'), m.get('next_v', '?'),
                 m.get('success_rate', 0) * 100, m.get('rating_trend', 0),
                 self.grand_cost_total)
        self._emit("metrics", metrics)

    def emit_tool_call(self, tool_name: str, args: dict, role: str = ""):
        """Broadcast a structured tool call event for expandable display in the Dashboard."""
        effective_role = role or self._current_role
        self._emit("tool_call", {"tool_name": tool_name, "args": args, "role": effective_role})

    def reset_gen_cost(self):
        self.gen_cost_total = 0.0
        self.generation_cost_identity = None
        self.generation_cost_policy = None
        self._emit("generation_cost_policy", {
            "generation_id": None,
            "spent_usd": 0.0,
            "policy": None,
        })

    def get_state(self) -> dict:
        pipeline_stage = None
        try:
            from evolution_infra import read_pipeline_checkpoint
            ckpt = read_pipeline_checkpoint()
            if ckpt:
                pipeline_stage = ckpt.get("stage")
        except Exception as e:
            log.debug("Pipeline checkpoint read failed: %s", e)
        return {
            **self._state,
            "grand_cost_total": round(self.grand_cost_total, 4),
            "gen_cost_total": round(self.gen_cost_total, 4),
            "generation_cost_identity": self.generation_cost_identity,
            "generation_cost_policy": self.generation_cost_policy,
            "pipeline_stage": pipeline_stage,
        }

    def get_output(self):
        return "\n".join(self._output_since_clear[-20:])
