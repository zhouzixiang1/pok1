"""Shared launch-plan builder for native national bot subprocesses."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Mapping


SEEDED_NATIVE_LAUNCHER = (
    "import os, random, runpy, sys\n"
    "entry = os.environ['POK_NATIVE_ENTRY']\n"
    "seed = os.environ.get('POK_NATIVE_BOT_SEED')\n"
    "if seed not in (None, ''):\n"
    "    random.seed(int(seed))\n"
    "sys.argv = [entry] + sys.argv[1:]\n"
    "runpy.run_path(entry, run_name='__main__')\n"
)
SAFE_INHERITED_ENV_KEYS = frozenset({
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "PYTHONIOENCODING",
    "TMPDIR",
    "TZ",
})


@dataclass(frozen=True)
class NativeBotLaunchPlan:
    command: tuple[str, ...]
    environment: dict[str, str]
    cwd: Path
    decision_log_supported: bool


def native_entry_supports_log_arg(entry: str | Path) -> bool:
    try:
        text = Path(entry).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "--log" in text and (
        'add_argument("--log"' in text or "add_argument('--log'" in text
    )


def safe_bot_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    source = source or os.environ
    environment = {
        key: str(source[key])
        for key in SAFE_INHERITED_ENV_KEYS
        if key in source
    }
    environment.setdefault("PATH", os.defpath)
    environment.setdefault("LANG", "C.UTF-8")
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    return environment


def build_native_bot_launch(
    *,
    bot_dir: str | Path,
    entry: str | Path,
    label: str,
    host: str,
    port: int,
    action_delay: float,
    hard_deadline: float,
    refinement_budget: float,
    baseline_target: float,
    decision_log: str | Path | None = None,
    seed: int | None = None,
    base_environment: Mapping[str, str] | None = None,
    inherit_all_environment: bool = False,
    extra_environment: Mapping[str, str] | None = None,
) -> NativeBotLaunchPlan:
    bot_path = Path(bot_dir).resolve()
    entry_path = Path(entry).resolve()
    if entry_path.parent != bot_path or entry_path.name != "national_bot.py":
        raise ValueError("native entry must be national_bot.py inside bot_dir")
    source = os.environ if base_environment is None else base_environment
    environment = (
        {str(key): str(value) for key, value in source.items()}
        if inherit_all_environment
        else safe_bot_environment(source)
    )
    environment.update({
        "POK_OFFICIAL_ACTION_DELAY": str(max(0.0, float(action_delay))),
        "POK_DECISION_HARD_DEADLINE_SEC": str(max(0.05, float(hard_deadline))),
        "POK_DECISION_REFINEMENT_BUDGET_SEC": str(max(0.04, float(refinement_budget))),
        "POK_DECISION_BASELINE_TARGET_SEC": str(max(0.01, float(baseline_target))),
        "POK_DECISION_BUDGET_SEC": str(max(0.05, float(hard_deadline))),
        "PYTHONPATH": str(bot_path) + os.pathsep + environment.get("PYTHONPATH", ""),
    })
    if extra_environment:
        for key, value in extra_environment.items():
            if not str(key).startswith("POK_"):
                raise ValueError(f"native launch extra environment is not POK_-scoped: {key}")
            environment[str(key)] = str(value)

    args = [
        "--host", str(host),
        "--port", str(int(port)),
        "--name", str(label),
    ]
    if seed is None:
        command = [sys.executable, str(entry_path), *args]
    else:
        environment.update({
            "POK_NATIVE_ENTRY": str(entry_path),
            "POK_NATIVE_BOT_SEED": str(int(seed)),
            "PYTHONHASHSEED": str(int(seed) % 4_294_967_295),
        })
        command = [sys.executable, "-c", SEEDED_NATIVE_LAUNCHER, *args]
    supports_log = native_entry_supports_log_arg(entry_path)
    if decision_log is not None and supports_log:
        command.extend(["--log", str(Path(decision_log).resolve())])
    return NativeBotLaunchPlan(
        command=tuple(command),
        environment=environment,
        cwd=bot_path,
        decision_log_supported=supports_log,
    )
