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
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

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
        data_stream_router,
    ]:
        test_app.include_router(r)
    return test_app


@pytest.fixture
def client(app):
    """Synchronous test client — no lifespan, no orchestrator."""
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
