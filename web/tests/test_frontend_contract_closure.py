"""Static cross-language guards for the strict dashboard authority contract."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "web" / "frontend" / "src"


def _quoted_array(source: str, constant: str) -> list[str]:
    match = re.search(
        rf"export const {re.escape(constant)} = \[(.*?)\] as const;",
        source,
        re.DOTALL,
    )
    assert match is not None, f"missing frontend constant {constant}"
    return re.findall(r'"([a-z0-9_]+)"', match.group(1))


def test_frontend_pipeline_stage_contract_matches_backend_order():
    from pipeline_state import STAGE_ORDER

    source = (FRONTEND / "constants" / "pipeline.ts").read_text(encoding="utf-8")

    assert _quoted_array(source, "PIPELINE_STAGE_CONTRACT") == STAGE_ORDER
    mapping = re.search(
        r"export const STAGE_TO_MILESTONE:.*?= \{(.*?)\n\};",
        source,
        re.DOTALL,
    )
    assert mapping is not None
    mapped_stages = re.findall(r"^\s{2}([a-z0-9_]+):", mapping.group(1), re.MULTILINE)
    assert mapped_stages == STAGE_ORDER


def test_frontend_has_no_retired_certification_launcher():
    client = (FRONTEND / "api" / "client.ts").read_text(encoding="utf-8")
    progress = (
        FRONTEND / "components" / "evolution" / "OfficialCertificationProgress.tsx"
    ).read_text(encoding="utf-8")

    assert "enqueueFullCertification" not in client
    assert "enqueueCertification" not in client
    assert "postJSON" not in client
    assert "/enqueue" not in client
    assert "certificationJobs" in client
    assert ("Certification" + "Queue") not in client
    assert "/cancel" not in progress
    assert "cancelCertification" not in client
    assert 'stage === "official_bootstrap_required"' in progress
    assert '"operator_bootstrap_full_v5_job"' in progress
    assert "row.read_only === true" in progress
    assert "row.cancel_allowed === false" in progress


def test_operator_token_is_memory_only_and_shared_across_mutations():
    source = (FRONTEND / "api" / "operatorControl.ts").read_text(encoding="utf-8")

    assert "X-Control-Token" in source
    assert "localStorage." not in source
    assert "sessionStorage." not in source
    assert "X-Arena-Token" not in source


def test_frontend_drops_stream_and_cycle_state_instead_of_merging_stale_authority():
    provider = (FRONTEND / "context" / "DataProvider.tsx").read_text(encoding="utf-8")
    evolution_api = (FRONTEND / "api" / "evolution.ts").read_text(encoding="utf-8")
    monitor = (FRONTEND / "pages" / "EvolutionMonitor.tsx").read_text(encoding="utf-8")
    logs = (FRONTEND / "pages" / "Logs.tsx").read_text(encoding="utf-8")

    assert 'addEventListener("epoch_blocked"' in provider
    assert "if (authorityBlocked) return" in provider
    assert "if (authorityBlocked) return" in evolution_api
    assert "existing?.selection_score" not in monitor
    assert "existing?.leaderboard_score" not in monitor
    assert "!visibleGenerations.some" in logs
    assert 'setLogContent("")' in logs
    assert "if (!cancelled) setLogContent(res.content)" in logs


def test_active_navigation_describes_read_only_contracts():
    sidebar = (FRONTEND / "layout" / "AppSidebar.tsx").read_text(encoding="utf-8")

    assert 'name: "严格发布 Bot"' in sidebar
    assert 'name: "提示词契约"' in sidebar
    assert "提示词编辑器" not in sidebar
    assert 'name: "Bot 管理"' not in sidebar
