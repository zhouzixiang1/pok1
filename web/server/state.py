"""Global state for the unified web app."""

import asyncio
import json
import threading
from pathlib import Path

_CONFIG_FILE = Path(__file__).resolve().parents[1] / "core" / "results" / "app_config.json"


class AppState:
    def __init__(self):
        self._lock = threading.RLock()
        self.mode: str = "orchestrator"
        self.running: bool = False
        self.daemon_enabled: bool = True
        self.daemon_workers: int = 14
        self.daemon_pairs: int = 5
        self.current_v: int = 0
        self.next_v: int = 0
        self.generation_count: int = 0
        self.decisions: list = []
        self._evolution_task: asyncio.Task | None = None
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
                self.daemon_workers = max(1, min(32, kwargs["daemon_workers"]))
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

    def _load_config(self):
        try:
            if _CONFIG_FILE.exists():
                data = json.loads(_CONFIG_FILE.read_text())
                if "daemon_enabled" in data and isinstance(data["daemon_enabled"], bool):
                    self.daemon_enabled = data["daemon_enabled"]
                if "daemon_workers" in data and isinstance(data["daemon_workers"], int):
                    self.daemon_workers = max(1, min(32, data["daemon_workers"]))
                if "daemon_pairs" in data and isinstance(data["daemon_pairs"], int):
                    self.daemon_pairs = max(1, min(20, data["daemon_pairs"]))
        except (json.JSONDecodeError, OSError):
            pass

    def _save_config(self):
        try:
            _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            _CONFIG_FILE.write_text(json.dumps({
                "daemon_enabled": self.daemon_enabled,
                "daemon_workers": self.daemon_workers,
                "daemon_pairs": self.daemon_pairs,
            }, indent=2))
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
