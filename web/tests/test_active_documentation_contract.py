"""Keep operator/agent guidance aligned with the strict national TCP epoch."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from bot_namespace import bot_tag
from conftest import STRICT_SOURCE_V, STRICT_TARGET_V, strict_bot_name


ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_primary_guides_name_one_epoch_and_one_version_authority():
    guides = (
        "AGENTS.md",
        "README.md",
        "SETUP_GUIDE.md",
        "ONBOARDING.md",
    )
    # Branch-portable version literals the guides must name consistently:
    # the first strict candidate, a representative high published version,
    # and the source high-water completion tag.
    candidate_literal = strict_bot_name()
    high_version_literal = strict_bot_name(STRICT_TARGET_V + 12)
    source_tag_literal = bot_tag(STRICT_SOURCE_V)
    for relative in guides:
        text = _read(relative)
        assert "national_tcp_policy_v1" in text, relative
        assert candidate_literal in text, relative
        assert high_version_literal in text, relative

    combined = "\n".join(_read(relative) for relative in guides)
    assert source_tag_literal in combined
    assert "numeric high-water" in combined or "numeric high-water" in combined.lower()
    assert "old-wrapper" in combined
    agents = _read("AGENTS.md")
    assert "There is no active free-standing lesson or" in agents
    assert "Any future lesson facility" in agents


def test_primary_guides_do_not_advertise_retired_entrypoints():
    operational_docs = (
        "AGENTS.md",
        "CLAUDE.md",
        "README.md",
        "SETUP_GUIDE.md",
        "ONBOARDING.md",
        "web/CLAUDE.md",
        "sever/CLAUDE.md",
    )
    combined = "\n".join(_read(relative) for relative in operational_docs)
    for retired_command in (
        "python engine/battle.py",
        "python engine/ladder.py",
        "python -m rl.scripts",
        "python scripts/botzone",
        "python sever/bot_adapter.py",
        "bootstrap-full",
        "X-Arena-Token",
        "POK_ARENA_CONTROL_TOKEN",
    ):
        assert retired_command not in combined

    setup = _read("SETUP_GUIDE.md")
    assert "pip install -r requirements.txt" not in setup
    assert "-r web/requirements.txt -r sever/requirements.txt" in setup
    assert "claude-agent-sdk==0.2.91" in setup


def test_raw_wire_formal_and_strength_authorities_are_explicit():
    agents = _read("AGENTS.md")
    readme = _read("README.md")
    arena = _read("docs/national-web-arena.md")
    stages = _read("docs/llm-stages.md")
    telemetry = _read("docs/national-runtime-telemetry.md")
    wire_probe = _read("docs/official-wire-probe.md")

    for text in (agents, readme):
        assert "70-hand" in text or "70 independent hands" in text
        assert "official EXE" in text
        assert "zero strength" in text or "zero rating" in text
    assert "Never append `\\n` or `\\r\\n`" in agents
    assert "no `\\n`/`\\r\\n`" in readme
    assert "diagnostic_only" in arena
    assert "one complete 70-hand" in stages
    assert "policy.py only" in stages
    assert "helpers/assets only" not in stages
    telemetry_flat = " ".join(telemetry.split())
    assert "Web Arena session separately retains" in telemetry_flat
    assert "diagnostic_only" in telemetry
    assert "retained only by the official EXE" not in telemetry
    assert "only formal compliance gate" in wire_probe
    assert "official-full-v5" in wire_probe
    assert "cannot certify or rate a bot" in wire_probe


def test_arena_docs_and_cli_share_operator_auth_and_diagnostic_scope():
    arena_doc = _read("docs/national-web-arena.md")
    arena_cli = _read("scripts/national_arena.py")
    combined = arena_doc + "\n" + arena_cli

    assert "POK_CONTROL_TOKEN" in combined
    assert "X-Control-Token" in combined
    assert "diagnostic_only" in arena_doc
    assert "results never certify or rate a bot" in arena_cli
    assert "schema-7 neural collector" not in arena_doc
    for token in (
        "X-Arena-Token",
        "POK_ARENA_CONTROL_TOKEN",
        "pok_arena_control_token",
    ):
        assert token not in combined


def test_frontend_readme_describes_the_real_dashboard_contract():
    frontend = _read("web/frontend/README.md")
    assert "National TCP Poker Evolution Dashboard" in frontend
    assert "national_tcp_policy_v1" in frontend
    assert "v142" in frontend and "v143" in frontend
    assert "national_v155" in frontend
    assert "one complete, compliant 70-hand local native TCP match" in frontend
    assert "official-full-v5" in frontend
    assert "diagnostic_only" in frontend
    assert "POK_CONTROL_TOKEN" in frontend
    assert "X-Control-Token" in frontend
    assert "npm ci" in frontend
    for stale_template_text in (
        "TailAdmin React - Free",
        "git clone https://github.com/TailAdmin",
        "npm install",
        "yarn install",
    ):
        assert stale_template_text not in frontend


def test_active_agent_prompts_use_current_evidence_authority():
    combined = _read("web/core/prompts/combined_analyst.md")
    critic = _read("web/core/prompts/critic_prompt.md")
    degeneration = _read("web/core/prompts/degeneration_diagnosis.md")
    official = _read("web/core/prompts/official_platform_analysis.md")
    worker_profile = _read("web/core/prompts/worker_profile_national_native.md")
    audit_runtime = _read("web/core/audit_agents.py")
    scheduler = _read("web/core/generation_scheduler.py")

    for text in (combined, critic, degeneration, official, worker_profile):
        assert "national_tcp_policy_v1" in text
    assert "complete current-epoch 70-hand" in critic
    assert "chip-only confidence interval cannot establish" in critic
    assert "does not choose a source, parent, branch, crossover" in degeneration
    assert "mechanical urgent-degeneration predicate passed" in degeneration
    assert "Never open or cite repository `archive/` material" in official
    assert "No per-opponent H2H delta rows were supplied" in audit_runtime
    assert "system_mechanical_urgent_intervention=" in scheduler


def test_launcher_help_text_cannot_relabel_current_authorities():
    web_launcher = _read("web/main.py")
    orchestrator_launcher = _read("web/core/orchestrator.py")
    server_launcher = _read("sever/main.py")
    rating_launcher = _read("web/core/elo_daemon.py")
    official_diagnostic = _read("scripts/official_platform_acceptance.py")
    registry_migration = _read("scripts/migrate_national_epoch_registry.py")
    registry = _read("web/core/national_epoch_registry.py")

    assert "national_tcp_policy_v1 evolution Web/API control plane" in web_launcher
    assert "national_tcp_policy_v1 LLM evolution orchestrator" in orchestrator_launcher
    assert "禁止追加换行" in server_launcher
    assert "Windows EXE " in server_launcher
    assert "official-full-v5" in server_launcher
    assert "one sample is one complete" in rating_launcher
    assert "Glicko-2 + H2H from complete 70-hand" in rating_launcher
    assert "per-game Elo" not in rating_launcher
    assert "Mirror pairs per match" not in rating_launcher
    assert "never issues a formal certificate" in official_diagnostic
    assert "Identity-only migration of retired reaped state" in registry_migration
    assert "no bot/evidence migration" in registry_migration
    assert "lifecycle identity for national_tcp_policy_v1" in registry
    assert "preserves identity/tombstone/version continuity only" in registry


def test_rating_daemon_help_is_read_only_before_epoch_reset():
    completed = subprocess.run(
        [sys.executable, "web/core/elo_daemon.py", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "national_tcp_policy_v1 Glicko-2 daemon" in completed.stdout
    assert "Complete 70-hand native matches" in completed.stdout


def test_operator_helper_help_preserves_epoch_and_evidence_boundaries():
    reset = _read("scripts/reset_national_tcp_policy_epoch.py")
    identity = _read("scripts/evaluation_data_identity.py")
    restart = _read("scripts/pok_restart_observe.sh")
    process_control = _read("pokctl.sh")

    # The reset help is branch-portable: it names the archived version-authority
    # high-water floor and the first strict target via the active namespace
    # rather than hardcoding main-branch literals (142 / national_v143).
    assert "One-time national_tcp_policy_v1 runtime reset" in reset
    assert f"high-water ({STRICT_SOURCE_V} on this branch)" in reset
    assert f"first target is {strict_bot_name()}" in reset
    assert "No bot, rating," in reset
    assert "Inspect the national_tcp_policy_v1 rating-data identity" in identity
    assert "never migrated into the new" in identity
    assert "do not carry old strength evidence forward" in identity
    assert "complete current-cycle 70-hand local raw native TCP matches" in restart
    assert "complete 70-hand matches per scheduled pairing" in restart
    assert "national_tcp_policy_v1 Web/API" in process_control
    assert "Arena 诊断或官方 EXE 筹码" in process_control


def test_archive_roots_override_historical_restore_and_evidence_claims():
    archive = _read("archive/README.md")
    docs_archive = _read("docs/archive/README.md")

    assert "Everything below `archive/`" in archive
    assert "legacy-untrusted" in archive
    assert "Nothing in this manifest is a current" in archive
    assert "no archived code, prompt, test, bot" in archive

    assert "Files below this directory are **legacy-untrusted**" in docs_archive
    assert "not inputs to the active evolution system" in docs_archive
    assert "must not be field-upgraded into current evidence" in docs_archive


def test_active_document_tree_has_no_retired_operator_credentials_or_commands():
    excluded = {
        ROOT / "docs" / "official-raise-boundary-oracle-2026-07-11.md",
        ROOT / "docs" / "official-terminal-settlement-oracle-2026-07-11.md",
        ROOT / "docs" / "official-allin-runout-wire-oracle-2026-07-19.md",
    }
    active_docs = [
        path
        for path in ROOT.rglob("*.md")
        if path not in excluded
        and "archive" not in path.relative_to(ROOT).parts
        and "node_modules" not in path.parts
        and ".codex_worktrees" not in path.parts
        and ".claude" not in path.parts
        and "frontend/dist" not in path.as_posix()
        and "server/static" not in path.as_posix()
    ]
    violations: list[str] = []
    forbidden = (
        "X-Arena-Token",
        "POK_ARENA_CONTROL_TOKEN",
        "pok_arena_control_token",
        "python engine/battle.py",
        "python engine/ladder.py",
        "python scripts/botzone_upload_match.py",
        "python sever/bot_adapter.py",
        "git clone https://github.com/TailAdmin",
        "TailAdmin React - Free",
    )
    for path in active_docs:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                violations.append(f"{path.relative_to(ROOT)}:{token}")
    assert violations == []
