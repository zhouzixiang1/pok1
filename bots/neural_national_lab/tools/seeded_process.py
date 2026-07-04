from __future__ import annotations

import json
import os
import subprocess
import sys
import threading


_RUN_BOT_CODE = r"""
import os
import random
import runpy
import sys

seed = int(sys.argv[1])
bot_path = sys.argv[2]
random.seed(seed)
os.environ["POK_EVAL_SEED"] = str(seed)
try:
    import numpy as _np

    _np.random.seed(seed % (2**32 - 1))
except Exception:
    pass
sys.argv = [bot_path]
sys.path.insert(0, os.path.dirname(os.path.abspath(bot_path)))
runpy.run_path(bot_path, run_name="__main__")
"""


class SeededPersistentBot:
    """Persistent bot process with deterministic Python RNG initialization."""

    def __init__(self, bot_path: str, seed: int):
        self.bot_path = bot_path
        self.seed = int(seed)
        self.proc = None
        self._alive = False
        self._start()

    def _start(self) -> None:
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = str(self.seed % (2**32 - 1))
        env["POK_EVAL_SEED"] = str(self.seed)
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-c", _RUN_BOT_CODE, str(self.seed), self.bot_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
            self._alive = True
        except Exception:
            self._alive = False

    def call(self, payload):
        if not self._alive:
            self._start()
            if not self._alive:
                return -1, "CRASH: process not started", None
        try:
            line = json.dumps(payload, separators=(",", ":"))
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except Exception:
            self._alive = False
            return -1, "CRASH: stdin write failed", None

        result_line = [None]
        error = [None]

        def _read() -> None:
            try:
                result_line[0] = self.proc.stdout.readline()
            except Exception as exc:
                error[0] = exc

        thread = threading.Thread(target=_read, daemon=True)
        thread.start()
        thread.join(timeout=60)

        if thread.is_alive():
            self._alive = False
            try:
                self.proc.kill()
            except Exception:
                pass
            return -1, "TIMEOUT", None

        if error[0] is not None:
            self._alive = False
            return -1, f"CRASH: {error[0]}", None

        if not result_line[0]:
            self._alive = False
            return -1, "EOF", None

        try:
            result = json.loads(result_line[0].strip())
            action = int(result.get("response", -1))
            bot_data = result.get("data")
            return action, "OK", bot_data
        except Exception as exc:
            self._alive = False
            return -1, f"CRASH: {exc}", None

    def close(self) -> None:
        self._alive = False
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    pass


def match_bot_seeds(bot_seed_base: int | None, bot_seed_stride: int, idx: int, side: str) -> tuple[int, int] | None:
    if bot_seed_base is None:
        return None
    block = max(2, int(bot_seed_stride))
    side_offset = 0 if side == "normal" else block
    base = int(bot_seed_base) + int(idx) * block * 2 + side_offset
    return base, base + 1
