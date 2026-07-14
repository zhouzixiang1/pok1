"""Shared test fixtures for the backend test suite.

Creates a FastAPI test app with all routers but no lifespan (no orchestrator/daemon).
Importing server.app ensures broadcaster/web_ui exist for endpoints that reference them.
"""

import sys
from pathlib import Path
import subprocess

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
from bot_namespace import (
    ACTIVE_BOT_PREFIX,
    FIRST_STRICT_POLICY_VERSION,
    NATIONAL_ENTRYPOINT,
    NATIONAL_RUNTIME_MANIFEST,
    POLICY_ENTRYPOINT,
    POLICY_EPOCH_RECEIPT,
    PRECOMPUTE_ENTRYPOINT,
    parse_bot_version,
    resolve_national_bot_spec,
)

from server.routes.ratings import router as ratings_router
from server.routes.matches import router as matches_router
from server.routes.evolution import router as evolution_router
from server.routes.logs import router as logs_router
from server.routes.control import router as control_router
from server.routes.bots import router as bots_router
from server.routes.certification import router as certification_router
from server.routes.pipeline import router as pipeline_router
from server.routes.prompts import router as prompts_router
from server.routes.data_stream import router as data_stream_router
from server.routes.national_arena import router as national_arena_router

# --- Bot detection for conditional test skipping ---
# NOTE: These must be set during pytest_configure (which runs BEFORE collection),
# not in a session fixture, because pytest_collection_modifyitems runs at
# collection time — before any fixtures execute.

_has_active_bot = False


def _tagged_test_bot_versions() -> list[int]:
    """Return repository bot fixtures backed by annotated completion tags.

    Unit tests must not depend on the operator checkout's ignored verdict ledger,
    rating manifest, or ``.completed`` cache.  They still need a real, published
    source tree for read-only route and archive tests, so use the immutable tag
    namespace directly and build an isolated synthetic runtime around one bot.
    """

    completed = subprocess.run(
        [
            "git",
            "for-each-ref",
            "--format=%(objecttype) %(refname:short)",
            "refs/tags/national-bot-v*",
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        return []
    versions: list[int] = []
    for line in completed.stdout.splitlines():
        object_type, separator, tag = line.partition(" ")
        if object_type != "tag" or not separator:
            continue
        version = parse_bot_version(tag.replace("national-bot-v", "national_v", 1))
        if version is None or version < FIRST_STRICT_POLICY_VERSION:
            continue
        bot_dir = PROJECT_ROOT / "bots" / f"national_v{version}"
        if resolve_national_bot_spec(bot_dir, repo_root=PROJECT_ROOT).eligible:
            versions.append(version)
    return sorted(set(versions))


def pytest_configure(config):
    """Register custom pytest markers and detect bots for conditional skipping."""
    config.addinivalue_line(
        "markers", "requires_active_bot: skip if no active bots found"
    )
    config.addinivalue_line(
        # Slow tests run real bot subprocesses; deselected by default via
        # pytest.ini addopts `-m "not slow"`. Run with: pytest -m slow
        "markers", "slow: deselect by default (real subprocess, multi-second)"
    )

    # Detect bots at configure time (before collection) so
    # pytest_collection_modifyitems can use the results.
    global _has_active_bot
    _has_active_bot = bool(_tagged_test_bot_versions())


def pytest_collection_modifyitems(config, items):
    """Skip tests marked requires_active_bot when no published fixture exists."""
    for item in items:
        if item.get_closest_marker("requires_active_bot") and not _has_active_bot:
            item.add_marker(
                pytest.mark.skip(reason="No active bots in environment")
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
        control_router, bots_router, certification_router, pipeline_router, prompts_router,
        data_stream_router,
        national_arena_router,
    ]:
        test_app.include_router(r)
    return test_app


@pytest.fixture
def client(app):
    """Synchronous test client -- no lifespan, no orchestrator."""
    # Operator mutation routes require the same boundary as production: an
    # actual loopback peer and an explicit same-origin browser Origin.  Keep
    # ordinary route tests realistic while adversarial tests override either
    # value deliberately.
    return TestClient(
        app,
        base_url="http://127.0.0.1",
        headers={"Origin": "http://127.0.0.1"},
        client=("127.0.0.1", 50_000),
    )


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
        "national_v145": {"r": 1600, "rd": 50, "sigma": 0.06, "last_period": "p10"},
        "national_v144": {"r": 1550, "rd": 80, "sigma": 0.06, "last_period": "p10"},
        "national_v143": {"r": 1500, "rd": 100, "sigma": 0.06, "last_period": "p9"},
    }


@pytest.fixture
def sample_h2h():
    return {
        "national_v145 vs national_v144": {"games": 50, "a_wins": 30, "b_wins": 20, "win_rate": 0.6},
        "national_v145 vs national_v143": {"games": 50, "a_wins": 35, "b_wins": 15, "win_rate": 0.7},
        "national_v144 vs national_v143": {"games": 50, "a_wins": 28, "b_wins": 22, "win_rate": 0.56},
    }


@pytest.fixture(scope="session")
def active_bot_version():
    versions = _tagged_test_bot_versions()
    return versions[-1] if versions else None


# --- Full isolation fixture ---


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Redirect ALL persistent state to tmp so tests never touch real data files.

    Patches:
        - evolution_infra constants (RESULTS_DIR, BOTS_DIR, all *_FILE)
    - server.route module-level constants (PROJECT_ROOT-derived paths)
    - event_bus.EVENTS_FILE
    - app_state._config_file
    - Clears server cache to prevent stale reads
    - Suppresses pok logger output during tests
    """

    import logging

    from server.state import app_state

    monkeypatch.setenv(
        "POK_OFFICIAL_VERDICT_LEDGER",
        str(tmp_path / "operator-state" / "official-verdict-ledger.jsonl"),
    )

    # Reset module-global injected UI: tests that call the real orchestrator_loop
    # invoke inject_ui(stub), which mutates tool_helpers._injected_ui DIRECTLY
    # (bypassing monkeypatch, so it is NOT auto-reverted at fixture teardown).
    # Without this reset the stub leaks into later tests and breaks any code path
    # that calls _get_ui() — e.g. tool_planning.py execute_workers ui.get_output()
    # → AttributeError '_UI' has no attribute 'get_output' (4 TestWorkerFailureCircuitBreaker
    # failures under the full suite). Reset at fixture entry so every test starts clean.
    import tool_helpers
    tool_helpers.inject_ui(None)

    # H1 (2026-06-29): reset the precommit shutdown flag. test_orchestrator_timeout_extension
    # exercises the CYCLE_TIMEOUT handler, which now calls set_precommit_shutdown(); without
    # this reset the flag leaks into later tests and breaks precommit mirror-battle drain
    # loops (test_phase2_cs_sprt TestPrecommitGeneratorEarlyStop wins==0 from immediate break).
    try:
        import tool_eval
        tool_eval.reset_precommit_shutdown()
    except Exception:
        pass

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

    # Materialize one small, regular-file bot fixture.  Artifact identity rejects
    # symlink roots by design, and tests must never read through into or mutate a
    # real bot directory.
    real_bots = PROJECT_ROOT / "bots"
    tagged_versions = _tagged_test_bot_versions()
    if tagged_versions:
        fixture_version = tagged_versions[-1]
        source = real_bots / f"national_v{fixture_version}"
        target = bots_dir / source.name
        target.mkdir()
        for filename in (
            NATIONAL_ENTRYPOINT,
            POLICY_ENTRYPOINT,
            PRECOMPUTE_ENTRYPOINT,
            NATIONAL_RUNTIME_MANIFEST,
            POLICY_EPOCH_RECEIPT,
        ):
            (target / filename).write_bytes((source / filename).read_bytes())
        (target / ".completed").write_text("isolated test fixture\n", encoding="utf-8")

    # Create an identity before writing any authoritative payload, then seed
    # wholly synthetic rating data.  Production data and ignored runtime ledgers
    # are never inputs to unit tests.
    import json
    from evaluation_data_identity import ensure_evaluation_data_identity

    ensure_evaluation_data_identity(results_dir)
    active_names = sorted(
        (
            d.name
            for d in bots_dir.iterdir()
            if d.is_dir() and d.name.startswith(ACTIVE_BOT_PREFIX)
        ),
        key=lambda name: parse_bot_version(name) or 0,
    )
    ratings = {
        name: {"r": 1500 + idx * 5, "rd": 80, "sigma": 0.06, "last_period": "test"}
        for idx, name in enumerate(active_names)
    }
    bot_stats = {
        name: {"wins": 1, "losses": 1, "draws": 0, "games": 2, "win_rate": 0.5}
        for name in active_names
    }
    (results_dir / "glicko_ratings.json").write_text(json.dumps(ratings), encoding="utf-8")
    (results_dir / "bot_stats.json").write_text(json.dumps(bot_stats), encoding="utf-8")
    (results_dir / "head_to_head.json").write_text("{}", encoding="utf-8")
    (results_dir / "match_history.jsonl").write_text("", encoding="utf-8")

    # --- Snapshot real state for restoration ---
    real_config = app_state._config_file
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
    # Publication/official eligibility is covered by dedicated tests.  Generic
    # route and helper tests operate on the isolated tag-backed artifact above.
    monkeypatch.setattr(evolution_infra, "_official_parent_eligible", lambda _path: True)

    # Generic tests run inside a synthetic, already-initialized runtime.  The
    # launch-boundary suite overrides this canonical guard explicitly to cover
    # reset_required, malformed reset evidence, fresh-bootstrap-ready, and
    # strict-published states.  Keep policy_epoch_initialization itself real so
    # epoch projection/receipt tests continue to exercise its validator.
    import epoch_authority

    monkeypatch.setattr(
        epoch_authority,
        "require_policy_epoch_initialized",
        lambda operation: {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "fresh_bootstrap_ready",
            "initialized": True,
            "strict_published": False,
            "reset_receipt_valid": True,
            "reset_receipt_digest": "a" * 64,
            "operator_action": None,
            "operator_command": None,
            "test_operation": operation,
        },
    )

    # --- 2. Patch event_bus: redirect the sole event ledger to
    # tmp and reset correlation context so emit() never touches real results/.
    # Every log_system_event call forwards through emit(), so this matters for
    # ALL tests, not just event_bus's own. ---
    import event_bus
    monkeypatch.setattr(event_bus, "EVENTS_FILE", results_dir / "events.jsonl")
    event_bus.reset_for_test()

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

    # ratings: PROJECT_ROOT, RESULTS_DIR, RATINGS_FILE, STATS_FILE,
    #          H2H_FILE, BOT_STATS_FILE, HISTORY_FILE, MATCH_HISTORY_FILE
    import server.routes.ratings as _rt
    monkeypatch.setattr(_rt, "PROJECT_ROOT", iso)
    monkeypatch.setattr(_rt, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(_rt, "RATINGS_FILE", results_dir / "glicko_ratings.json")
    monkeypatch.setattr(_rt, "STATS_FILE", results_dir / "elo_daemon_stats.json")
    monkeypatch.setattr(_rt, "H2H_FILE", results_dir / "head_to_head.json")
    monkeypatch.setattr(_rt, "BOT_STATS_FILE", results_dir / "bot_stats.json")
    monkeypatch.setattr(_rt, "HISTORY_FILE", results_dir / "rating_history.jsonl")
    monkeypatch.setattr(_rt, "MATCH_HISTORY_FILE", results_dir / "match_history.jsonl")

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

    # bots: PROJECT_ROOT, BOTS_DIR, RESULTS_DIR, RATINGS_FILE, BOT_STATS_FILE, H2H_FILE, MATCH_HISTORY_FILE
    import server.routes.bots as _bt
    monkeypatch.setattr(_bt, "PROJECT_ROOT", iso)
    monkeypatch.setattr(_bt, "BOTS_DIR", bots_dir)
    monkeypatch.setattr(_bt, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(_bt, "RATINGS_FILE", results_dir / "glicko_ratings.json")
    monkeypatch.setattr(_bt, "BOT_STATS_FILE", results_dir / "bot_stats.json")
    monkeypatch.setattr(_bt, "H2H_FILE", results_dir / "head_to_head.json")
    monkeypatch.setattr(_bt, "MATCH_HISTORY_FILE", results_dir / "match_history.jsonl")

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

    # control: PROJECT_ROOT, WEB_DIR, ORCHESTRATOR_SESSION_FILE
    import server.routes.control as _ctrl
    monkeypatch.setattr(_ctrl, "PROJECT_ROOT", iso)
    monkeypatch.setattr(_ctrl, "WEB_DIR", iso / "web")
    monkeypatch.setattr(_ctrl, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(_ctrl, "ORCHESTRATOR_SESSION_FILE", results_dir / "orchestrator_session.json")

    # prompts: PROJECT_ROOT, PROMPTS_DIR
    # Copy real prompts to isolation dir so read-only tests work without
    # touching production files, and future write tests are safe.
    import server.routes.prompts as _pr
    prompts_dst = iso / "prompts"
    prompts_dst.mkdir(exist_ok=True)
    prompts_src = PROJECT_ROOT / "web" / "core" / "prompts"
    if prompts_src.exists():
        for f in prompts_src.glob("*.md"):
            (prompts_dst / f.name).write_text(f.read_text())
    monkeypatch.setattr(_pr, "PROJECT_ROOT", iso)
    monkeypatch.setattr(_pr, "PROMPTS_DIR", prompts_dst)

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
