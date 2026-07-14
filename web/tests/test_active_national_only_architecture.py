"""Fail closed if a retired protocol facility re-enters active architecture."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "web" / "core"

RETIRED_ACTIVE_PATHS = (
    ROOT / ".claude",
    ROOT / ".trae",
    ROOT / "ref",
    ROOT / "engine",
    ROOT / "rl",
    ROOT / "bots" / "neural_national_lab",
    ROOT / "sever" / "bot_adapter.py",
    CORE / "engine",
    CORE / "decision_tester.py",
    CORE / "smoke_tester.py",
    CORE / "protected_contracts.py",
    CORE / "national_acceptance.py",
    CORE / "national_eval.py",
    CORE / "behavior_diversity.py",
    CORE / "map_elites.py",
    CORE / "qd_async_eval.py",
    CORE / "exploitability_prober.py",
    CORE / "psro_meta_solver.py",
    CORE / "battle_experience.py",
    CORE / "battle_memory.py",
    CORE / "battle_scheduler.py",
    CORE / "crossover_compat.py",
    CORE / "experience_pool.md",
    CORE / "experience_pool.py",
    CORE / "experience_archivist.py",
    CORE / "experience_attribution.py",
    CORE / "legacy_facility_boundary.py",
    CORE / "test_scenarios.json",
    CORE / "reference_bots",
    CORE / "spot_analyzer.py",
    CORE / "prompts" / "dynamic_test_generator.md",
    CORE / "prompts" / "spot_analyzer.md",
    CORE / "prompts" / "worker_profile_legacy_adapter.md",
    CORE / "prompts" / "battle_experience_incremental.md",
    CORE / "prompts" / "battle_experience_update.md",
    CORE / "prompts" / "experience_consolidator.md",
    CORE / "prompts" / "experience_pool_audit.md",
    CORE / "prompts" / "initial_prompt.md",
    ROOT / "web" / "server" / "routes" / "scheduler.py",
)

RETIRED_IMPORT_ROOTS = {
    "archive",
    "engine",
    "decision_tester",
    "smoke_tester",
    "protected_contracts",
    "national_acceptance",
    "national_eval",
    "behavior_diversity",
    "map_elites",
    "qd_async_eval",
    "exploitability_prober",
    "legacy_facility_boundary",
    "battle_experience",
    "battle_scheduler",
    "experience_pool",
    "experience_archivist",
    "experience_attribution",
    "crossover_compat",
    "psro_meta_solver",
}

RETIRED_DYNAMIC_TOKENS = (
    "archive.botzone_local",
    "sever.bot_adapter",
    "engine.battle",
    "legacy_facility_boundary",
    "battle_scheduler",
)

RAW_TCP_IMPLEMENTATIONS = (
    ROOT / "sever" / "server" / "protocol.py",
    ROOT / "sever" / "server" / "tcp_server.py",
    ROOT / "sever" / "test_client.py",
    ROOT / "sever" / "server" / "transport.py",
    CORE / "national_game_runtime.py",
    CORE / "official_wire_probe.py",
    ROOT / "scripts" / "official_scripted_bot.py",
)

FORBIDDEN_STREAM_FRAMING_CALLS = {
    "readline",
    "readuntil",
    "send_line",
    "recv_line",
    "makefile",
}


def _literal_contains_line_delimiter(node: ast.AST) -> bool:
    return any(
        isinstance(item, ast.Constant)
        and isinstance(item.value, (str, bytes))
        and (
            ("\n" in item.value or "\r" in item.value)
            if isinstance(item.value, str)
            else (b"\n" in item.value or b"\r" in item.value)
        )
        for item in ast.walk(node)
    )


def test_retired_facilities_are_not_active_paths():
    present = [path.relative_to(ROOT).as_posix() for path in RETIRED_ACTIVE_PATHS if path.exists()]
    assert present == []


def test_native_workflow_uses_only_current_registered_skill_layers():
    from skill_library import valid_skill_layers
    from workflow_profiles import get_workflow_profile

    registered = valid_skill_layers()
    native_layers = get_workflow_profile("national_native").focus_skill_layers

    assert set(native_layers) <= registered
    assert "action_intent" in native_layers
    assert "action_sanitizer" not in native_layers
    assert "map_elites" not in registered


def test_active_web_core_cannot_import_retired_facilities():
    violations = []
    for path in sorted(CORE.rglob("*.py")):
        if "results" in path.parts or "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".", 1)[0] for alias in node.names}
                bad = roots.intersection(RETIRED_IMPORT_ROOTS)
                if bad:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:import {sorted(bad)}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".", 1)[0]
                if root in RETIRED_IMPORT_ROOTS:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:from {node.module}")
            elif isinstance(node, ast.Call):
                call_name = ""
                if isinstance(node.func, ast.Name):
                    call_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    call_name = node.func.attr
                if call_name not in {"__import__", "import_module"} or not node.args:
                    continue
                value = node.args[0]
                if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                    continue
                for token in RETIRED_DYNAMIC_TOKENS:
                    if token in value.value:
                        violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}:dynamic-import:{token}"
                        )
    assert violations == []


def test_retired_bootstrap_prompt_and_dead_crossover_selection_modes_have_zero_authority():
    import evaluation_contract
    import evolution_scope

    retired_active = (
        "web/core/crossover_compat.py",
        "web/core/prompts/initial_prompt.md",
    )
    archived = (
        "archive/evolution_epochs/national_native_v1/analysis/crossover_compat.py",
        "archive/evolution_epochs/national_native_v1/prompts/initial_prompt.md",
    )

    for relative in retired_active:
        assert not (ROOT / relative).exists()
        assert relative not in evolution_scope.CRITICAL_GENERATION_EXACT
        assert relative not in evolution_scope.CRITICAL_PROMPT_EXACT
        assert relative not in evaluation_contract.FULL_PIPELINE_EXACT

    contract = evaluation_contract.build_evaluation_contract(
        ROOT,
        stage="selected",
        national_execution_mode="native_tcp",
    )
    for relative in archived:
        assert (ROOT / relative).is_file()
        assert not evolution_scope.is_critical_evolution_path(relative)
        assert not evaluation_contract.is_contract_path(relative, contract)

    scheduler = (CORE / "generation_scheduler.py").read_text(encoding="utf-8")
    assert "crossover_incompatibilities.json" not in scheduler
    for retired_selection_token in (
        "child_counts",
        "blocked_pairs",
        "map_niches",
        "map_elites",
        "parent_a_niche",
        "parent_b_niche",
        "parent_a_elite",
        "parent_b_elite",
        "archive_source",
        "MAP-Elites",
        "QD archive",
    ):
        assert retired_selection_token not in scheduler


def test_active_workflow_profiles_are_raw_national_tcp_only():
    source = (CORE / "workflow_profiles.py").read_text(encoding="utf-8")
    assert 'evaluation_protocol="national"' in source
    assert 'rating_protocol="national"' in source
    assert 'national_execution_mode="native_tcp"' in source
    assert 'national_execution_mode="adapter"' not in source
    assert 'evaluation_protocol="local"' not in source
    assert 'rating_protocol="local"' not in source


def test_active_national_wire_implementations_cannot_restore_newline_framing():
    violations = []
    for path in RAW_TCP_IMPLEMENTATIONS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = ""
            receiver = ""
            if isinstance(node.func, ast.Name):
                call_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                call_name = node.func.attr
                receiver = ast.unparse(node.func.value)
            if call_name in FORBIDDEN_STREAM_FRAMING_CALLS:
                violations.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}:{call_name}"
                )
            if not node.args or not _literal_contains_line_delimiter(node.args[0]):
                continue
            if call_name == "sendall" or (
                call_name == "write" and "writer" in receiver.lower()
            ):
                violations.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}:newline-network-send"
                )
    assert violations == []


def test_system_native_template_rejects_legacy_newline_wire_apis():
    source = (CORE / "national_native.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(CORE / "national_native.py"))
    template = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "NATIVE_BOT_TEMPLATE"
            for target in node.targets
        ):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            template = node.value.value
            break
    assert template is not None
    for token in (
        "makefile(",
        ".readline(",
        'msg + "\\n"',
        "msg + '\\n'",
        'self.name + "\\n"',
        "self.name + '\\n'",
    ):
        assert token not in template


def test_late_gates_never_repair_system_owned_runtime_or_run_json_spot_checks():
    gates = (CORE / "tool_gates.py").read_text(encoding="utf-8")
    hygiene = (CORE / "candidate_hygiene.py").read_text(encoding="utf-8")

    assert "run_spot_check" not in gates
    assert "spot_analyzer" not in gates
    assert "ensure_native_entry" not in gates
    assert "ensure_native_entry" not in hygiene
    assert "immutable contract" in hygiene


def test_operator_surfaces_cannot_restore_retired_arena_auth_or_generic_tool_ui():
    """One operator authority must cover Arena and evolution mutations.

    Keeping the old Arena-only token names alive would make the browser, CLI,
    documentation, and API appear to describe different control planes.  The
    backend retains a static 410 route for old generic HTTP tool calls, but the
    frontend must not advertise or invoke that retired capability.
    """

    operator_surfaces = (
        ROOT / "web" / "frontend" / "src",
        ROOT / "web" / "server",
        ROOT / "scripts" / "national_arena.py",
        ROOT / "docs" / "national-web-arena.md",
    )
    retired_auth_tokens = (
        "X-Arena-Token",
        "POK_ARENA_CONTROL_TOKEN",
        "pok_arena_control_token",
    )
    violations = []
    for surface in operator_surfaces:
        paths = sorted(surface.rglob("*")) if surface.is_dir() else [surface]
        for path in paths:
            if not path.is_file() or path.suffix not in {".py", ".ts", ".tsx", ".md"}:
                continue
            text = path.read_text(encoding="utf-8")
            for token in retired_auth_tokens:
                if token in text:
                    violations.append(f"{path.relative_to(ROOT)}:{token}")
    assert violations == []

    frontend = ROOT / "web" / "frontend" / "src"
    frontend_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(frontend.rglob("*"))
        if path.is_file() and path.suffix in {".ts", ".tsx"}
    )
    assert "/api/control/tool/" not in frontend_text
    assert "callTool(" not in frontend_text


def test_gitignore_cannot_hide_retired_facilities_in_active_locations():
    """Retired source must be visibly absent, not merely locally ignored.

    Broad historical ignores used to let a Botzone backup, RL checkpoint tree,
    or unreviewed reference checkout reappear without showing in ``git status``.
    The one-time root ``results/*.json`` quarantine remains separate because
    the acknowledged epoch reset must archive existing runtime debris.
    """

    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for retired_ignore in (
        "**/main_backup.py",
        "evolution_workspace/",
        "rl/checkpoints/",
    ):
        assert retired_ignore not in gitignore

    # These operator-owned tool/third-party trees are intentionally untracked,
    # not active facilities. Their ignore entries must not be mistaken for an
    # execution allowance; the active-scope/import tests above remain the
    # authority barrier.
    assert ".claude/worktrees/" in gitignore
    assert "ref/llm_evolution/" in gitignore
    assert "lll/" in gitignore


def test_external_battle_jsonl_control_plane_is_physically_retired():
    """Only the native rating loop and official durable jobs may schedule work."""

    active_surfaces = (
        CORE / "elo_daemon.py",
        CORE / "daemon_management.py",
        ROOT / "web" / "server" / "app.py",
        ROOT / "web" / "server" / "routes" / "data_stream.py",
        ROOT / "web" / "frontend" / "src",
    )
    retired_tokens = (
        "battle_jobs.jsonl",
        "battle_jobs.claimed",
        "battle_results.jsonl",
        "battle_scheduler",
        "scheduler_capable",
        "/api/scheduler",
    )
    violations = []
    for surface in active_surfaces:
        paths = sorted(surface.rglob("*")) if surface.is_dir() else [surface]
        for path in paths:
            if not path.is_file() or path.suffix not in {".py", ".ts", ".tsx"}:
                continue
            text = path.read_text(encoding="utf-8")
            for token in retired_tokens:
                if token in text:
                    violations.append(f"{path.relative_to(ROOT)}:{token}")
    assert violations == []

    from server.app import app

    assert not any(
        str(getattr(route, "path", "")).startswith("/api/scheduler")
        for route in app.routes
    )
