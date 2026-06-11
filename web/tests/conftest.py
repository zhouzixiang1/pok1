"""Shared test fixtures for the backend test suite.

Creates a FastAPI test app with all routers but no lifespan (no orchestrator/daemon).
Importing server.app ensures broadcaster/web_ui exist for endpoints that reference them.
"""

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.testclient import TestClient

# Ensure imports work
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "web" / "core"))
sys.path.insert(0, str(PROJECT_ROOT / "web" / "server"))

# Import server.app to create module-level broadcaster and web_ui
# (some endpoints do `from server.app import web_ui` inside handlers)
import server.app  # noqa: F401

from server.routes.ratings import router as ratings_router
from server.routes.matches import router as matches_router
from server.routes.evolution import router as evolution_router
from server.routes.logs import router as logs_router
from server.routes.control import router as control_router
from server.routes.bots import router as bots_router
from server.routes.pipeline import router as pipeline_router
from server.routes.prompts import router as prompts_router
from server.routes.data_stream import router as data_stream_router
from server.routes.scheduler import router as scheduler_router

# --- Bot detection for conditional test skipping ---

_has_active_bot = None
_has_graveyard_bot = None


def pytest_configure(config):
    """Register custom pytest markers."""
    config.addinivalue_line(
        "markers", "requires_active_bot: skip if no active bots found"
    )
    config.addinivalue_line(
        "markers", "requires_graveyard_bot: skip if no graveyard bots found"
    )


@pytest.fixture(scope="session", autouse=True)
def _detect_bots():
    """Detect whether active and graveyard bots exist for conditional skipping."""
    global _has_active_bot, _has_graveyard_bot
    bots_dir = PROJECT_ROOT / "bots"
    if bots_dir.exists():
        active = [
            d
            for d in bots_dir.iterdir()
            if d.is_dir()
            and d.name.startswith("claude_v")
            and not d.name.endswith(".tmp")
        ]
        _has_active_bot = len(active) > 0
    else:
        _has_active_bot = False

    gy = bots_dir / "graveyard"
    if gy.exists():
        graveyard = [
            d for d in gy.iterdir() if d.is_dir() and d.name.startswith("claude_v")
        ]
        _has_graveyard_bot = len(graveyard) > 0
    else:
        _has_graveyard_bot = False


def pytest_collection_modifyitems(config, items):
    """Skip tests marked requires_active_bot / requires_graveyard_bot when absent."""
    for item in items:
        if item.get_closest_marker("requires_active_bot") and not _has_active_bot:
            item.add_marker(
                pytest.mark.skip(reason="No active bots in environment")
            )
        if (
            item.get_closest_marker("requires_graveyard_bot")
            and not _has_graveyard_bot
        ):
            item.add_marker(
                pytest.mark.skip(reason="No graveyard bots in environment")
            )


# --- Standard fixtures ---


@pytest.fixture
def app():
    """FastAPI app with all routers but no lifespan."""
    test_app = FastAPI()
    test_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for r in [
        ratings_router, matches_router, evolution_router, logs_router,
        control_router, bots_router, pipeline_router, prompts_router,
        data_stream_router, scheduler_router,
    ]:
        test_app.include_router(r)
    return test_app


@pytest.fixture
def client(app):
    """Synchronous test client -- no lifespan, no orchestrator."""
    return TestClient(app)


@pytest.fixture
def temp_experience(tmp_path):
    """Temp experience_pool.md for write isolation."""
    f = tmp_path / "experience_pool.md"
    f.write_text("## Test\n- Lesson 1\n")
    return f


@pytest.fixture
def temp_prompt_dir(tmp_path):
    """Temp copy of prompt files for write isolation."""
    prompts_src = PROJECT_ROOT / "web" / "core" / "prompts"
    dst = tmp_path / "prompts"
    dst.mkdir()
    for f in prompts_src.glob("*.md"):
        (dst / f.name).write_text(f.read_text())
    return dst


@pytest.fixture
def sample_ratings():
    return {
        "claude_v35": {"r": 1600, "rd": 50, "sigma": 0.06, "last_period": "p10"},
        "claude_v30": {"r": 1550, "rd": 80, "sigma": 0.06, "last_period": "p10"},
        "claude_v10": {"r": 1500, "rd": 100, "sigma": 0.06, "last_period": "p9"},
    }


@pytest.fixture
def sample_h2h():
    return {
        "claude_v35 vs claude_v30": {"games": 50, "a_wins": 30, "b_wins": 20, "win_rate": 0.6},
        "claude_v35 vs claude_v10": {"games": 50, "a_wins": 35, "b_wins": 15, "win_rate": 0.7},
        "claude_v30 vs claude_v10": {"games": 50, "a_wins": 28, "b_wins": 22, "win_rate": 0.56},
    }


@pytest.fixture(scope="session")
def active_bot_version():
    from evolution_infra import get_active_bots
    bots = get_active_bots()
    if not bots:
        return None
    versions = sorted(int(b.split("_v")[1]) for b in bots)
    return versions[len(versions) // 2]


@pytest.fixture(scope="session")
def graveyard_bot_version():
    main_bots = PROJECT_ROOT / "bots"
    graveyard = main_bots / "graveyard"
    main_names = set()
    if main_bots.exists():
        main_names = {d.name for d in main_bots.iterdir() if d.is_dir() and d.name.startswith("claude_v")}
    if graveyard.exists():
        for d in sorted(graveyard.iterdir(), reverse=True):
            if d.is_dir() and d.name.startswith("claude_v") and d.name not in main_names:
                try:
                    return int(d.name.split("_v")[1])
                except (ValueError, IndexError):
                    pass
    return None


# --- Full isolation fixture ---


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Redirect ALL persistent state to tmp so tests never touch real data files.

    Patches:
    - evolution_infra constants (RESULTS_DIR, BOTS_DIR, GRAVEYARD_DIR, all *_FILE)
    - server.route module-level constants (PROJECT_ROOT-derived paths)
    - system_log.SYSTEM_EVENTS_FILE
    - app_state._config_file
    - Clears server cache to prevent stale reads
    - Suppresses pok logger output during tests
    """

    import logging

    from server.state import app_state
    import system_log

    # --- Create temp directory structure under a private subdirectory ---
    # Use _pok_isolated to avoid colliding with tests that create their own
    # tmp_path/bots or tmp_path/results directories.
    iso = tmp_path / "_pok_isolated"
    iso.mkdir()
    results_dir = iso / "results"
    results_dir.mkdir()
    (results_dir / "match_replay").mkdir()
    (results_dir / "archive").mkdir()

    bots_dir = iso / "bots"
    bots_dir.mkdir()
    graveyard_dir = bots_dir / "graveyard"
    graveyard_dir.mkdir()

    # --- Snapshot real state for restoration ---
    real_config = app_state._config_file
    real_events = system_log.SYSTEM_EVENTS_FILE
    snapshot = {
        "running": app_state.running,
        "daemon_enabled": app_state.daemon_enabled,
        "daemon_workers": app_state.daemon_workers,
        "daemon_pairs": app_state.daemon_pairs,
    }

    # --- 1. Patch evolution_infra module-level constants ---
    import evolution_infra

    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(evolution_infra, "BOTS_DIR", bots_dir)
    monkeypatch.setattr(evolution_infra, "GRAVEYARD_DIR", graveyard_dir)
    monkeypatch.setattr(evolution_infra, "RATINGS_FILE", results_dir / "glicko_ratings.json")
    monkeypatch.setattr(evolution_infra, "STATS_FILE", results_dir / "elo_daemon_stats.json")
    monkeypatch.setattr(evolution_infra, "H2H_FILE", results_dir / "head_to_head.json")
    monkeypatch.setattr(evolution_infra, "BOT_STATS_FILE", results_dir / "bot_stats.json")
    monkeypatch.setattr(evolution_infra, "WORKER_FAILURES_FILE", results_dir / "worker_failures.jsonl")
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", results_dir / "pipeline_state.json")
    monkeypatch.setattr(evolution_infra, "REPLAY_DIR", results_dir / "match_replay")
    monkeypatch.setattr(evolution_infra, "MATCH_HISTORY_FILE", results_dir / "match_history.jsonl")
    monkeypatch.setattr(evolution_infra, "ARCHIVE_DIR", results_dir / "archive")
    monkeypatch.setattr(evolution_infra, "LLM_COSTS_FILE", results_dir / "llm_costs.jsonl")
    monkeypatch.setattr(evolution_infra, "RATING_HISTORY_FILE", results_dir / "rating_history.jsonl")
    monkeypatch.setattr(evolution_infra, "EXPERIENCE_FILE", iso / "experience_pool.md")

    # --- 2. Patch system_log module constant ---
    monkeypatch.setattr(system_log, "SYSTEM_EVENTS_FILE", iso / "system_events.jsonl")

    # --- 3. Patch app_state config file ---
    monkeypatch.setattr(app_state, "_config_file", iso / "app_config.json")

    # --- 4. Patch route module local constants ---
    # data_stream: PROJECT_ROOT, BOTS_DIR, RESULTS_DIR, RATINGS_FILE, STATS_FILE,
    #              H2H_FILE, BOT_STATS_FILE, HISTORY_FILE, MATCH_HISTORY_FILE
    import server.routes.data_stream as _ds
    monkeypatch.setattr(_ds, "PROJECT_ROOT", iso)
    monkeypatch.setattr(_ds, "BOTS_DIR", bots_dir)
    monkeypatch.setattr(_ds, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(_ds, "RATINGS_FILE", results_dir / "glicko_ratings.json")
    monkeypatch.setattr(_ds, "STATS_FILE", results_dir / "elo_daemon_stats.json")
    monkeypatch.setattr(_ds, "H2H_FILE", results_dir / "head_to_head.json")
    monkeypatch.setattr(_ds, "BOT_STATS_FILE", results_dir / "bot_stats.json")
    monkeypatch.setattr(_ds, "HISTORY_FILE", results_dir / "rating_history.jsonl")
    monkeypatch.setattr(_ds, "MATCH_HISTORY_FILE", results_dir / "match_history.jsonl")

    # ratings: PROJECT_ROOT, RESULTS_DIR, EXPERIENCE_FILE, RATINGS_FILE, STATS_FILE,
    #          H2H_FILE, BOT_STATS_FILE, HISTORY_FILE
    import server.routes.ratings as _rt
    monkeypatch.setattr(_rt, "PROJECT_ROOT", iso)
    monkeypatch.setattr(_rt, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(_rt, "EXPERIENCE_FILE", iso / "experience_pool.md")
    monkeypatch.setattr(_rt, "RATINGS_FILE", results_dir / "glicko_ratings.json")
    monkeypatch.setattr(_rt, "STATS_FILE", results_dir / "elo_daemon_stats.json")
    monkeypatch.setattr(_rt, "H2H_FILE", results_dir / "head_to_head.json")
    monkeypatch.setattr(_rt, "BOT_STATS_FILE", results_dir / "bot_stats.json")
    monkeypatch.setattr(_rt, "HISTORY_FILE", results_dir / "rating_history.jsonl")

    # matches: PROJECT_ROOT, RESULTS_DIR, STATS_FILE, RATINGS_FILE, H2H_FILE,
    #          REPLAY_DIR, MATCH_HISTORY_FILE
    import server.routes.matches as _mt
    monkeypatch.setattr(_mt, "PROJECT_ROOT", iso)
    monkeypatch.setattr(_mt, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(_mt, "STATS_FILE", results_dir / "elo_daemon_stats.json")
    monkeypatch.setattr(_mt, "RATINGS_FILE", results_dir / "glicko_ratings.json")
    monkeypatch.setattr(_mt, "H2H_FILE", results_dir / "head_to_head.json")
    monkeypatch.setattr(_mt, "REPLAY_DIR", results_dir / "match_replay")
    monkeypatch.setattr(_mt, "MATCH_HISTORY_FILE", results_dir / "match_history.jsonl")

    # bots: PROJECT_ROOT, BOTS_DIR, RESULTS_DIR, RATINGS_FILE, BOT_STATS_FILE, H2H_FILE
    import server.routes.bots as _bt
    monkeypatch.setattr(_bt, "PROJECT_ROOT", iso)
    monkeypatch.setattr(_bt, "BOTS_DIR", bots_dir)
    monkeypatch.setattr(_bt, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(_bt, "RATINGS_FILE", results_dir / "glicko_ratings.json")
    monkeypatch.setattr(_bt, "BOT_STATS_FILE", results_dir / "bot_stats.json")
    monkeypatch.setattr(_bt, "H2H_FILE", results_dir / "head_to_head.json")

    # pipeline: PROJECT_ROOT, RESULTS_DIR, PIPELINE_STATE_FILE, WORKER_FAILURES_FILE
    import server.routes.pipeline as _pl
    monkeypatch.setattr(_pl, "PROJECT_ROOT", iso)
    monkeypatch.setattr(_pl, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(_pl, "PIPELINE_STATE_FILE", results_dir / "pipeline_state.json")
    monkeypatch.setattr(_pl, "WORKER_FAILURES_FILE", results_dir / "worker_failures.jsonl")

    # logs: PROJECT_ROOT, RESULTS_DIR, ORCHESTRATOR_LOGS_DIR
    import server.routes.logs as _lg
    monkeypatch.setattr(_lg, "PROJECT_ROOT", iso)
    monkeypatch.setattr(_lg, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(_lg, "ORCHESTRATOR_LOGS_DIR", iso / "logs")

    # --- 5. Clear server cache ---
    from server.cache import _CACHE
    _CACHE.clear()

    # --- 6. Suppress pok logger ---
    pok_logger = logging.getLogger("pok")
    orig_level = pok_logger.level
    pok_logger.setLevel(logging.CRITICAL + 1)

    try:
        yield
    finally:
        # monkeypatch auto-reverts all setattr calls, but we still need to
        # restore mutable state that was patched via direct assignment.
        try:
            app_state._load_config()
        except Exception:
            pass
        app_state.running = snapshot["running"]
        app_state.daemon_enabled = snapshot["daemon_enabled"]
        app_state.daemon_workers = snapshot["daemon_workers"]
        app_state.daemon_pairs = snapshot["daemon_pairs"]
        app_state.decisions = []
        pok_logger.setLevel(orig_level)
