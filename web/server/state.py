"""Global state for the unified web app."""

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shutdown_manager import ShutdownManager


def _default_daemon_workers() -> int:
    """Default daemon workers = CPU cores * 7/8, clamped to [1, 128]."""
    return max(1, int(os.cpu_count() * 28 / 32))


class AppState:
    def __init__(self, config_file=None):
        self._lock = threading.RLock()
        self._config_file = config_file or Path(__file__).resolve().parents[1] / "core" / "results" / "app_config.json"
        self.mode: str = "orchestrator"
        self.running: bool = False  # Coarse-grained loop control: True = orchestrator loop is active, False = stopped or idle
        self.daemon_enabled: bool = True
        self.daemon_workers: int = _default_daemon_workers()
        self.daemon_pairs: int = 5
        self.current_v: int = 0
        self.next_v: int = 0
        self.generation_count: int = 0
        self.decisions: list = []
        self._evolution_task: asyncio.Task | None = None
        self._shutdown_mgr: "ShutdownManager | None" = None
        self._load_config()

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "mode": self.mode,
                "running": self.running,
                "daemon_enabled": self.daemon_enabled,
                "daemon_workers": self.daemon_workers,
                "daemon_pairs": self.daemon_pairs,
                "current_v": self.current_v,
                "next_v": self.next_v,
                "generation_count": self.generation_count,
                "decisions": self.decisions[-50:],
            }

    def get_config(self) -> dict:
        with self._lock:
            return {
                "mode": self.mode,
                "daemon_enabled": self.daemon_enabled,
                "daemon_workers": self.daemon_workers,
                "daemon_pairs": self.daemon_pairs,
            }

    def update_config(self, **kwargs) -> dict:
        with self._lock:
            if "daemon_enabled" in kwargs and isinstance(kwargs["daemon_enabled"], bool):
                self.daemon_enabled = kwargs["daemon_enabled"]
            if "daemon_workers" in kwargs and isinstance(kwargs["daemon_workers"], int) and not isinstance(kwargs["daemon_workers"], bool):
                self.daemon_workers = max(1, min(128, kwargs["daemon_workers"]))
            if "daemon_pairs" in kwargs and isinstance(kwargs["daemon_pairs"], int) and not isinstance(kwargs["daemon_pairs"], bool):
                self.daemon_pairs = max(1, min(20, kwargs["daemon_pairs"]))
            self._save_config()
            return self.get_config()

    def set_running(self, running: bool):
        with self._lock:
            self.running = running

    def try_set_running(self, running: bool) -> bool:
        with self._lock:
            if self.running == running:
                return False
            self.running = running
            return True

    def stop_running(self):
        """Atomically set running=False and extract the evolution task."""
        with self._lock:
            self.running = False
            task = self._evolution_task
            self._evolution_task = None
            return task

    def _load_config(self):
        try:
            from evolution_infra import locked_file
            if self._config_file.exists():
                with locked_file(self._config_file, "r") as f:
                    data = json.load(f)
                if "daemon_enabled" in data and isinstance(data["daemon_enabled"], bool):
                    self.daemon_enabled = data["daemon_enabled"]
                if "daemon_workers" in data and isinstance(data["daemon_workers"], int):
                    self.daemon_workers = max(1, min(128, data["daemon_workers"]))
                if "daemon_pairs" in data and isinstance(data["daemon_pairs"], int):
                    self.daemon_pairs = max(1, min(20, data["daemon_pairs"]))
        except (json.JSONDecodeError, OSError):
            pass

    def _save_config(self):
        try:
            from evolution_infra import locked_file
            self._config_file.parent.mkdir(parents=True, exist_ok=True)
            with locked_file(self._config_file, "w") as f:
                json.dump({
                    "daemon_enabled": self.daemon_enabled,
                    "daemon_workers": self.daemon_workers,
                    "daemon_pairs": self.daemon_pairs,
                }, f, indent=2)
        except OSError:
            pass

    def bootstrap(self, current_v: int):
        with self._lock:
            self.current_v = current_v
            self.next_v = current_v + 1
            self.generation_count = current_v

    def set_generation(self, current_v: int, next_v: int):
        with self._lock:
            self.current_v = current_v
            self.next_v = next_v
            self.generation_count += 1

    def set_task(self, task: asyncio.Task):
        with self._lock:
            self._evolution_task = task

    def cancel_task(self):
        with self._lock:
            if self._evolution_task and not self._evolution_task.done():
                self._evolution_task.cancel()
            self._evolution_task = None

    def set_shutdown_mgr(self, mgr: "ShutdownManager"):
        with self._lock:
            self._shutdown_mgr = mgr

    def request_shutdown(self):
        with self._lock:
            if self._shutdown_mgr:
                self._shutdown_mgr.request_shutdown()

    def add_decision(self, tool_name: str, result_summary: str):
        import time
        with self._lock:
            self.decisions.append({
                "tool": tool_name,
                "summary": result_summary[:200],
                "ts": time.time(),
            })
            if len(self.decisions) > 100:
                self.decisions = self.decisions[-100:]


app_state = AppState()
